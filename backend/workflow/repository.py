from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .models import (
    ExpertProposal, PlanningClaim, PlanningConflict, WorkflowOperation, WorkflowSnapshot,
    WorkflowState, WorkflowTransition,
)


class StoredJSONError(ValueError):
    """Persisted JSON is corrupt or does not match its required container shape."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_object(raw: str, field: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise StoredJSONError(f"stored {field} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise StoredJSONError(f"stored {field} must be a JSON object")
    return value


class WorkflowRepository:
    """Transactional persistence over the project store's SQLite connection."""

    def __init__(self, store):
        self.store = store

    def create(self, run_id: str) -> WorkflowSnapshot:
        now = _now()
        try:
            with self.store._lock, self.store._db:
                existing = self.store._db.execute(
                    "SELECT 1 FROM workflow_instances WHERE run_id=?", (run_id,)
                ).fetchone()
                if not existing:
                    terminal_states = (WorkflowState.COMPLETED.value, WorkflowState.CANCELLED.value, WorkflowState.FAILED.value)
                    active = self.store._db.execute(
                        "SELECT run_id FROM workflow_instances WHERE state NOT IN (?,?,?)",
                        terminal_states,
                    ).fetchone()
                    if active and active["run_id"] != run_id:
                        raise ValueError(f"workflow instance '{active['run_id']}' is already active")
                    self.store._db.execute(
                        "INSERT INTO workflow_instances(run_id,state,state_version,created_at,updated_at) "
                        "VALUES(?,?,?,?,?)",
                        (run_id, WorkflowState.CREATED.value, 1, now, now),
                    )
        except sqlite3.IntegrityError as exc:
            raise ValueError("another planning workflow is already active") from exc
        return self.get(run_id)

    def latest_resumable(self) -> WorkflowSnapshot | None:
        terminal = tuple(state.value for state in (
            WorkflowState.COMPLETED, WorkflowState.CANCELLED, WorkflowState.FAILED,
        ))
        with self.store._lock:
            row = self.store._db.execute(
                "SELECT run_id FROM workflow_instances WHERE state NOT IN (?,?,?) ORDER BY updated_at DESC LIMIT 1",
                terminal,
            ).fetchone()
        return self.get(row["run_id"]) if row else None

    def goal(self, run_id: str) -> str:
        with self.store._lock:
            row = self.store._db.execute("SELECT goal FROM planning_goals WHERE run_id=?", (run_id,)).fetchone()
        return str(row["goal"]) if row else ""

    def get(self, run_id: str) -> WorkflowSnapshot:
        with self.store._lock:
            row = self.store._db.execute(
                "SELECT * FROM workflow_instances WHERE run_id=?", (run_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"workflow {run_id} does not exist")
        state = WorkflowState(row["state"])
        return WorkflowSnapshot(
            run_id=run_id,
            state=state,
            resume_state=WorkflowState(row["resume_state"]) if row["resume_state"] else None,
            state_version=int(row["state_version"]),
            active_operation_id=row["active_operation_id"],
            failure_code=row["failure_code"],
            failure_detail=_decode_object(row["failure_detail_json"], "failure_detail_json"),
            allowed_actions=self.allowed_actions(state),
        )

    @staticmethod
    def allowed_actions(state: WorkflowState) -> list[str]:
        if state == WorkflowState.WAITING_FOR_USER:
            return ["answer", "cancel"]
        if state == WorkflowState.WAITING_FOR_RECOVERY:
            return ["retry", "failover", "cancel"]
        if state in {WorkflowState.COMPLETED, WorkflowState.CANCELLED, WorkflowState.FAILED}:
            return []
        return ["cancel"]

    def has_transition(self, run_id: str, key: str) -> bool:
        with self.store._lock:
            return self.store._db.execute(
                "SELECT 1 FROM workflow_transitions WHERE run_id=? AND idempotency_key=?",
                (run_id, key),
            ).fetchone() is not None

    def commit_transition(self, transition: WorkflowTransition, resume_state: WorkflowState | None = None) -> WorkflowSnapshot:
        """Compare-and-commit a transition and its structured failure metadata."""
        now = _now()
        payload_json = json.dumps(transition.payload, sort_keys=True)
        with self.store._lock, self.store._db:
            existing = self.store._db.execute(
                "SELECT to_state FROM workflow_transitions WHERE run_id=? AND idempotency_key=?",
                (transition.run_id, transition.idempotency_key),
            ).fetchone()
            if existing:
                return self.get(transition.run_id)
            current = self.store._db.execute(
                "SELECT state,state_version FROM workflow_instances WHERE run_id=?", (transition.run_id,)
            ).fetchone()
            if not current:
                raise KeyError(f"workflow {transition.run_id} does not exist")
            if current["state"] != transition.from_state.value:
                raise ValueError(
                    f"stale transition: expected {transition.from_state.value}, found {current['state']}"
                )
            self.store._db.execute(
                "INSERT INTO workflow_transitions(run_id,from_state,event,to_state,idempotency_key,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (transition.run_id, transition.from_state.value, transition.event.value,
                 transition.to_state.value, transition.idempotency_key, payload_json, now),
            )
            terminal = transition.to_state in {
                WorkflowState.COMPLETED, WorkflowState.CANCELLED, WorkflowState.FAILED,
            }
            failure_code = str(transition.payload.get("failure_code", "")) or None
            failure_detail = transition.payload.get("failure_detail", {})
            if not isinstance(failure_detail, dict):
                raise ValueError("failure_detail must be a JSON object")
            self.store._db.execute(
                "UPDATE workflow_instances SET state=?,resume_state=?,state_version=?,updated_at=?,"
                "completed_at=?,failure_code=?,failure_detail_json=? WHERE run_id=?",
                (transition.to_state.value, resume_state.value if resume_state else None,
                 int(current["state_version"]) + 1, now, now if terminal else None,
                 failure_code, json.dumps(failure_detail, sort_keys=True), transition.run_id),
            )
        return self.get(transition.run_id)

    def commit_corrective_transition(self, transition: WorkflowTransition) -> WorkflowSnapshot:
        """Atomically invalidate downstream evidence and append a corrective transition."""
        now = _now()
        target = transition.to_state
        invalidated: list[str] = []
        with self.store._lock, self.store._db:
            current = self.store._db.execute(
                "SELECT state,state_version FROM workflow_instances WHERE run_id=?", (transition.run_id,)
            ).fetchone()
            if not current or current["state"] != transition.from_state.value:
                raise ValueError("stale corrective transition")
            if target in {WorkflowState.DISCOVERING, WorkflowState.DIVERGING}:
                for table in ("expert_proposals", "planning_claims", "planning_conflicts", "context_summaries"):
                    self.store._db.execute(f"DELETE FROM {table} WHERE run_id=?", (transition.run_id,))
                    invalidated.append(table)
                self.store._db.execute(
                    "UPDATE context_nodes SET status='superseded',updated_at=? "
                    "WHERE run_id=? AND node_type IN ('proposal','discovery_assessment','assumption')",
                    (now, transition.run_id),
                )
                invalidated.append("planning_context")
            elif target == WorkflowState.ANALYZING:
                for table in ("planning_claims", "planning_conflicts"):
                    self.store._db.execute(f"DELETE FROM {table} WHERE run_id=?", (transition.run_id,))
                    invalidated.append(table)
            self.store._db.execute(
                "UPDATE workflow_operations SET status='invalidated',completed_at=? "
                "WHERE run_id=? AND status IN ('running','completed')",
                (now, transition.run_id),
            )
            invalidated.append("workflow_operations")
            self.store._db.execute(
                "UPDATE decision_checkpoints SET status='rejected',answered_at=?,answered_by='DesignFlow' "
                "WHERE run_id=? AND status IN ('active','pending')",
                (now, transition.run_id),
            )
            self.store._db.execute(
                "INSERT INTO workflow_transitions(run_id,from_state,event,to_state,idempotency_key,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (transition.run_id, transition.from_state.value, transition.event.value,
                 target.value, transition.idempotency_key, json.dumps(transition.payload, sort_keys=True), now),
            )
            self.store._db.execute(
                "INSERT INTO workflow_invalidations(run_id,transition_event,target_state,reason,invalidated_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (transition.run_id, transition.event.value, target.value,
                 str(transition.payload["reason"]), json.dumps(invalidated), now),
            )
            self.store._db.execute(
                "UPDATE workflow_instances SET state=?,resume_state=NULL,state_version=?,active_operation_id=NULL,"
                "updated_at=?,completed_at=NULL,failure_code=NULL,failure_detail_json='{}' WHERE run_id=?",
                (target.value, int(current["state_version"]) + 1, now, transition.run_id),
            )
        return self.get(transition.run_id)

    def start_operation(self, operation: WorkflowOperation) -> WorkflowOperation:
        now = _now()
        with self.store._lock, self.store._db:
            self.store._db.execute(
                "INSERT OR IGNORE INTO workflow_operations(id,run_id,operation_type,state,status,attempt,input_ref,output_ref,"
                "error_json,started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (operation.id, operation.run_id, operation.operation_type, operation.state.value,
                 operation.status, operation.attempt, operation.input_ref, operation.output_ref,
                 json.dumps(operation.error, sort_keys=True), now),
            )
            self.store._db.execute(
                "UPDATE workflow_instances SET active_operation_id=?,updated_at=? WHERE run_id=?",
                (operation.id, now, operation.run_id),
            )
        return operation

    def complete_operation(self, operation_id: str, output_ref: str | None = None) -> None:
        """Mark one durable operation complete after all accepted outputs persist."""
        now = _now()
        with self.store._lock, self.store._db:
            cursor = self.store._db.execute(
                "UPDATE workflow_operations SET status='completed',output_ref=?,completed_at=? "
                "WHERE id=? AND status='running'",
                (output_ref, now, operation_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("workflow operation is missing or not running")
            self.store._db.execute(
                "UPDATE workflow_instances SET active_operation_id=NULL,updated_at=? "
                "WHERE active_operation_id=?",
                (now, operation_id),
            )

    def save_proposal(
        self, *, run_id: str, operation_id: str, expert_id: str, perspective: str,
        round_number: int, proposal: ExpertProposal, raw_response: str = "",
    ) -> str:
        """Persist one validated expert proposal once per operation and expert."""
        proposal_id = f"{operation_id}:{expert_id}"
        now = _now()
        with self.store._lock, self.store._db:
            self.store._db.execute(
                "INSERT INTO expert_proposals(id,run_id,operation_id,expert_id,perspective,round,status,"
                "proposal_json,raw_response,created_at) VALUES(?,?,?,?,?,?,'accepted',?,?,?) "
                "ON CONFLICT(run_id,operation_id,expert_id) DO NOTHING",
                (proposal_id, run_id, operation_id, expert_id, perspective, round_number,
                 proposal.model_dump_json(), raw_response, now),
            )
        return proposal_id

    def save_debate_turn(
        self, *, run_id: str, operation_id: str, sequence: int, agent: str,
        turn_kind: str, payload: dict[str, Any],
    ) -> str:
        turn_id = f"{operation_id}:turn:{sequence}"
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self.store._lock, self.store._db:
            self.store._db.execute(
                "INSERT INTO planning_debate_turns(id,run_id,operation_id,sequence,agent,turn_kind,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,operation_id,sequence) DO NOTHING",
                (turn_id, run_id, operation_id, sequence, agent, turn_kind, encoded, _now()),
            )
        return turn_id

    def debate_turns(self, run_id: str) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT * FROM planning_debate_turns WHERE run_id=? ORDER BY sequence,id", (run_id,)
            ).fetchall()
        return [{**dict(row), "payload": _decode_object(row["payload_json"], "payload_json")} for row in rows]

    def save_summary(self, run_id: str, source_type: str, source_id: str, summary_type: str, summary: dict) -> str:
        if not isinstance(summary, dict):
            raise ValueError("summary must be a JSON object")
        encoded = json.dumps(summary, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        summary_id = f"summary:{source_type}:{source_id}:{summary_type}:{content_hash[:12]}"
        now = _now()
        with self.store._lock, self.store._db:
            self.store._db.execute(
                "INSERT OR IGNORE INTO context_summaries(id,run_id,source_type,source_id,summary_type,summary_json,"
                "content_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (summary_id, run_id, source_type, source_id, summary_type, encoded, content_hash, now),
            )
        return summary_id

    def summaries(self, run_id: str) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT * FROM context_summaries WHERE run_id=? ORDER BY source_type,source_id,summary_type,id",
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            result.append({**dict(row), "summary": _decode_object(row["summary_json"], "summary_json")})
        return result

    def proposals(self, run_id: str) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT * FROM expert_proposals WHERE run_id=? ORDER BY round,expert_id,id", (run_id,)
            ).fetchall()
        result = []
        for row in rows:
            try:
                proposal = ExpertProposal.model_validate_json(row["proposal_json"])
            except Exception as exc:
                raise ValueError(f"stored proposal {row['id']} is invalid") from exc
            result.append({**dict(row), "proposal": proposal})
        return result

    def save_goal(self, run_id: str, goal: str, constraints: list[str] | None = None, non_goals: list[str] | None = None):
        now = _now()
        with self.store._lock, self.store._db:
            self.store._db.execute(
                "INSERT INTO planning_goals(run_id,goal,constraints_json,non_goals_json,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET goal=excluded.goal,"
                "constraints_json=excluded.constraints_json,non_goals_json=excluded.non_goals_json,updated_at=excluded.updated_at",
                (run_id, goal, json.dumps(constraints or []), json.dumps(non_goals or []), now),
            )

    def save_analysis(self, run_id: str, claims: list[PlanningClaim], conflicts: list[PlanningConflict]):
        now = _now()
        with self.store._lock, self.store._db:
            self.store._db.execute("DELETE FROM planning_claims WHERE run_id=?", (run_id,))
            self.store._db.execute("DELETE FROM planning_conflicts WHERE run_id=?", (run_id,))
            for claim in claims:
                self.store._db.execute(
                    "INSERT INTO planning_claims(id,run_id,proposal_id,claim_type,topic,normalized_text,confidence,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (claim.id, run_id, claim.proposal_id, claim.claim_type, claim.topic, claim.text,
                     claim.confidence, claim.status, now),
                )
            for conflict in conflicts:
                self.store._db.execute(
                    "INSERT INTO planning_conflicts(id,run_id,topic,status,materiality,options_json,resolution,"
                    "resolution_source,created_at,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                    (conflict.id, run_id, conflict.topic, conflict.status, conflict.materiality,
                     json.dumps(conflict.options), conflict.resolution, conflict.resolution_source, now),
                )

    def conflicts(self, run_id: str) -> list[PlanningConflict]:
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT * FROM planning_conflicts WHERE run_id=? ORDER BY materiality,topic,id", (run_id,)
            ).fetchall()
        result = []
        for row in rows:
            try:
                options = json.loads(row["options_json"])
            except json.JSONDecodeError as exc:
                raise ValueError(f"stored conflict {row['id']} has invalid options JSON") from exc
            if not isinstance(options, list):
                raise ValueError(f"stored conflict {row['id']} options must be a JSON array")
            claim_rows = self.store._db.execute(
                "SELECT id FROM planning_claims WHERE run_id=? AND topic=? ORDER BY id", (run_id, row["topic"])
            ).fetchall()
            claim_ids = [item["id"] for item in claim_rows]
            if len(claim_ids) < 2:
                claim_ids = [f"{row['id']}:option:{index}" for index, _ in enumerate(options)]
            result.append(PlanningConflict(
                id=row["id"], topic=row["topic"], claim_ids=claim_ids,
                options=options, materiality=row["materiality"], status=row["status"],
                resolution=row["resolution"], resolution_source=row["resolution_source"],
            ))
        return result

    def resolve_conflict(self, run_id: str, conflict_id: str, resolution: str, source: str):
        now = _now()
        with self.store._lock, self.store._db:
            cursor = self.store._db.execute(
                "UPDATE planning_conflicts SET status='resolved',resolution=?,resolution_source=?,resolved_at=? "
                "WHERE run_id=? AND id=? AND status='open'",
                (resolution, source, now, run_id, conflict_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("conflict is missing or already resolved")
