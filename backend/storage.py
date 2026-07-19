"""Small per-project SQLite store for reusable DesignFlow state."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing

from .crypto import encrypt_key, decrypt_key


class ProjectStore:
    def __init__(self, metadata_dir: Path):
        metadata_dir.mkdir(parents=True, exist_ok=True)
        self.path = metadata_dir / "designflow.db"
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self._lock, self._db:
            self._db.executescript(
                """
                PRAGMA foreign_keys=ON;
                PRAGMA busy_timeout=5000;
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    sort_order INTEGER NOT NULL,
                    config_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    idea TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0,
                    run_kind TEXT NOT NULL DEFAULT 'planning_workflow',
                    outcome_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    timestamp TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
                CREATE TABLE IF NOT EXISTS turns (
                    run_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    round_number INTEGER,
                    iteration INTEGER,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    response_preview TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (run_id, turn_id)
                );
                CREATE INDEX IF NOT EXISTS idx_turns_run ON turns(run_id, turn_id);
                CREATE TABLE IF NOT EXISTS mcp_servers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    command TEXT NOT NULL,
                    args_json TEXT NOT NULL DEFAULT '[]',
                    env_json TEXT NOT NULL DEFAULT '{}',
                    username TEXT NOT NULL DEFAULT '',
                    password TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS key_value (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decision_checkpoints (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    dimension TEXT NOT NULL DEFAULT '',
                    question TEXT NOT NULL,
                    rationale TEXT NOT NULL DEFAULT '',
                    recommendation TEXT NOT NULL DEFAULT '',
                    blocking INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    answered_at TEXT,
                    answered_by TEXT NOT NULL DEFAULT '',
                    selected_option_id TEXT,
                    custom_answer TEXT NOT NULL DEFAULT '',
                    decision_id TEXT,
                    UNIQUE(run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS decision_options (
                    id TEXT PRIMARY KEY,
                    checkpoint_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    consequence TEXT NOT NULL DEFAULT '',
                    recommended INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(checkpoint_id) REFERENCES decision_checkpoints(id) ON DELETE CASCADE,
                    UNIQUE(checkpoint_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS system_recovery_actions (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    failure_category TEXT NOT NULL,
                    affected_provider TEXT NOT NULL DEFAULT '',
                    failed_turn_id TEXT NOT NULL DEFAULT '',
                    retry_eligible INTEGER NOT NULL DEFAULT 0,
                    auto_failover_eligible INTEGER NOT NULL DEFAULT 0,
                    retry_time_known TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolution_action TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_checkpoint
                    ON decision_checkpoints(run_id) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS idx_checkpoint_run_status
                    ON decision_checkpoints(run_id, status, sequence);
                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'proposed',
                    chosen_option TEXT NOT NULL DEFAULT '',
                    rationale TEXT NOT NULL DEFAULT '',
                    answered_by TEXT NOT NULL DEFAULT '',
                    source_ref TEXT NOT NULL DEFAULT '',
                    raw_markdown TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions(run_id, created_at);
                CREATE TABLE IF NOT EXISTS decision_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    value TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(decision_id) REFERENCES decisions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_decision_history
                    ON decision_history(decision_id, id);
                CREATE TABLE IF NOT EXISTS implementation_reports (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    task TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    code_references_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'open'
                );
                CREATE INDEX IF NOT EXISTS idx_implementation_reports_status
                    ON implementation_reports(status, created_at);
                CREATE TABLE IF NOT EXISTS workflow_instances (
                    run_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    resume_state TEXT,
                    state_version INTEGER NOT NULL DEFAULT 1,
                    active_operation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    failure_code TEXT,
                    failure_detail_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS workflow_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    from_state TEXT NOT NULL,
                    event TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_transitions_run
                    ON workflow_transitions(run_id, id);
                CREATE TABLE IF NOT EXISTS workflow_invalidations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    transition_event TEXT NOT NULL,
                    target_state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    invalidated_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_invalidations_run
                    ON workflow_invalidations(run_id, id);
                CREATE TABLE IF NOT EXISTS workflow_operations (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    input_ref TEXT,
                    output_ref TEXT,
                    error_json TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_operations_run
                    ON workflow_operations(run_id, status, id);
                CREATE TABLE IF NOT EXISTS planning_goals (
                    run_id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    constraints_json TEXT NOT NULL DEFAULT '[]',
                    non_goals_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS expert_proposals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    expert_id TEXT NOT NULL,
                    perspective TEXT NOT NULL,
                    round INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    proposal_json TEXT NOT NULL,
                    raw_response TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, operation_id, expert_id)
                );
                CREATE INDEX IF NOT EXISTS idx_expert_proposals_run_operation
                    ON expert_proposals(run_id, operation_id, expert_id);
                CREATE TABLE IF NOT EXISTS planning_debate_turns (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    agent TEXT NOT NULL,
                    turn_kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, operation_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_planning_debate_turns_run
                    ON planning_debate_turns(run_id, operation_id, sequence);
                CREATE TABLE IF NOT EXISTS run_participants (
                    run_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    provider_name TEXT NOT NULL,
                    provider_kind TEXT NOT NULL,
                    model TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY(run_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS planning_claims (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    claim_type TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    confidence REAL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_planning_claims_run_topic
                    ON planning_claims(run_id, topic, id);
                CREATE TABLE IF NOT EXISTS planning_conflicts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    status TEXT NOT NULL,
                    materiality TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    resolution TEXT,
                    resolution_source TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_planning_conflicts_run_status
                    ON planning_conflicts(run_id, status, materiality, id);
                CREATE TABLE IF NOT EXISTS context_summaries (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    summary_type TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_type, source_id, summary_type, content_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_context_summaries_run_source
                    ON context_summaries(run_id, source_type, source_id);
                CREATE TABLE IF NOT EXISTS context_nodes (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL DEFAULT '',
                    node_type TEXT NOT NULL,
                    parent_id TEXT,
                    source_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    authority INTEGER NOT NULL DEFAULT 3,
                    importance INTEGER NOT NULL DEFAULT 3,
                    token_count INTEGER NOT NULL,
                    summary_token_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(parent_id) REFERENCES context_nodes(id) ON DELETE CASCADE,
                    UNIQUE(run_id, source_type, source_ref, node_type)
                );
                CREATE INDEX IF NOT EXISTS idx_context_nodes_scope
                    ON context_nodes(run_id, status, node_type, authority, importance, id);
                CREATE INDEX IF NOT EXISTS idx_context_nodes_parent
                    ON context_nodes(parent_id, status, id);
                CREATE TABLE IF NOT EXISTS context_edges (
                    from_node_id TEXT NOT NULL,
                    to_node_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(from_node_id, to_node_id, relation),
                    FOREIGN KEY(from_node_id) REFERENCES context_nodes(id) ON DELETE CASCADE,
                    FOREIGN KEY(to_node_id) REFERENCES context_nodes(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS semantic_embeddings (
                    run_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    vector_blob BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, item_id, model_name, model_version)
                );
                CREATE INDEX IF NOT EXISTS idx_semantic_embeddings_run
                    ON semantic_embeddings(run_id, model_name, model_version, item_id);
                """
            )
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(runs)").fetchall()}
            if "cached_input_tokens" not in columns:
                self._db.execute("ALTER TABLE runs ADD COLUMN cached_input_tokens INTEGER NOT NULL DEFAULT 0")
            if "pricing_complete" not in columns:
                self._db.execute("ALTER TABLE runs ADD COLUMN pricing_complete INTEGER NOT NULL DEFAULT 1")
            if "run_kind" not in columns:
                self._db.execute("ALTER TABLE runs ADD COLUMN run_kind TEXT NOT NULL DEFAULT 'planning_workflow'")
            if "outcome_json" not in columns:
                self._db.execute("ALTER TABLE runs ADD COLUMN outcome_json TEXT NOT NULL DEFAULT '{}'")
            checkpoint_columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(decision_checkpoints)").fetchall()
            }
            if "decision_id" not in checkpoint_columns:
                self._db.execute("ALTER TABLE decision_checkpoints ADD COLUMN decision_id TEXT")
            decision_columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(decisions)").fetchall()
            }
            if "source_ref" not in decision_columns:
                self._db.execute("ALTER TABLE decisions ADD COLUMN source_ref TEXT NOT NULL DEFAULT ''")
            if "raw_markdown" not in decision_columns:
                self._db.execute("ALTER TABLE decisions ADD COLUMN raw_markdown TEXT NOT NULL DEFAULT ''")

            self._retire_legacy_discovery_waits()
            self._repair_multiple_active_workflows()
            self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_nonterminal_workflow "
                "ON workflow_instances((1)) "
                "WHERE state NOT IN ('COMPLETED','CANCELLED','FAILED')"
            )
            self._backfill_checkpoint_decisions()

    def _retire_legacy_discovery_waits(self):
        """Invalidate questions produced before the material-blocker discovery contract."""
        policy_version = "3"
        row = self._db.execute(
            "SELECT value FROM key_value WHERE key='discovery_policy_version'"
        ).fetchone()
        if row and str(row["value"]) == policy_version:
            return
        now = datetime.now(timezone.utc).isoformat()
        legacy = self._db.execute(
            "SELECT run_id FROM workflow_instances "
            "WHERE state='WAITING_FOR_USER' AND resume_state='DISCOVERING'"
        ).fetchall()
        for item in legacy:
            run_id = str(item["run_id"])
            self._db.execute(
                "UPDATE workflow_instances SET state='CANCELLED',resume_state=NULL,"
                "active_operation_id=NULL,state_version=state_version+1,updated_at=?,completed_at=? "
                "WHERE run_id=?",
                (now, now, run_id),
            )
            self._db.execute(
                "UPDATE decision_checkpoints SET status='rejected',answered_at=?,answered_by='DesignFlow' "
                "WHERE run_id=? AND status IN ('active','pending')",
                (now, run_id),
            )
            self._db.execute(
                "UPDATE workflow_operations SET status='cancelled',completed_at=? "
                "WHERE run_id=? AND status='running'",
                (now, run_id),
            )
        self._db.execute(
            "INSERT INTO key_value(key,value) VALUES('discovery_policy_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (policy_version,),
        )

    def _repair_multiple_active_workflows(self):
        """Keep the newest nonterminal workflow and close stale competing state."""
        rows = self._db.execute(
            "SELECT run_id FROM workflow_instances "
            "WHERE state NOT IN ('COMPLETED','CANCELLED','FAILED') "
            "ORDER BY updated_at DESC, created_at DESC, run_id DESC"
        ).fetchall()
        if len(rows) <= 1:
            return
        now = datetime.now(timezone.utc).isoformat()
        stale_ids = [str(row["run_id"]) for row in rows[1:]]
        for run_id in stale_ids:
            self._db.execute(
                "UPDATE workflow_instances SET state='CANCELLED',resume_state=NULL,"
                "active_operation_id=NULL,state_version=state_version+1,updated_at=?,completed_at=? "
                "WHERE run_id=?",
                (now, now, run_id),
            )
            self._db.execute(
                "UPDATE decision_checkpoints SET status='rejected',answered_at=?,answered_by='DesignFlow' "
                "WHERE run_id=? AND status IN ('active','pending')",
                (now, run_id),
            )
            self._db.execute(
                "UPDATE workflow_operations SET status='cancelled',completed_at=? "
                "WHERE run_id=? AND status='running'",
                (now, run_id),
            )

    def _backfill_checkpoint_decisions(self):
        """Give pre-ledger checkpoints durable decisions without parsing Markdown artifacts."""
        rows = self._db.execute(
            "SELECT * FROM decision_checkpoints WHERE decision_id IS NULL OR decision_id='' ORDER BY created_at"
        ).fetchall()
        for checkpoint in rows:
            decision_id = str(uuid.uuid4())
            chosen = str(checkpoint["custom_answer"] or "").strip()
            if not chosen and checkpoint["selected_option_id"]:
                option = self._db.execute(
                    "SELECT label, summary FROM decision_options WHERE id=?",
                    (checkpoint["selected_option_id"],),
                ).fetchone()
                if option:
                    chosen = f"{option['label']} — {option['summary']}"
            confirmed = checkpoint["status"] == "answered"
            status = "confirmed" if confirmed else "proposed"
            updated_at = checkpoint["answered_at"] or checkpoint["created_at"]
            actor = checkpoint["answered_by"] or "DesignFlow"
            self._db.execute(
                """INSERT INTO decisions(
                   id, run_id, title, status, chosen_option, rationale, answered_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (decision_id, checkpoint["run_id"], checkpoint["question"], status, chosen,
                 checkpoint["rationale"], checkpoint["answered_by"], checkpoint["created_at"], updated_at),
            )
            self._db.execute(
                """INSERT INTO decision_history(decision_id, timestamp, actor, action, value)
                   VALUES (?, ?, 'DesignFlow', 'proposed', ?)""",
                (decision_id, checkpoint["created_at"], checkpoint["question"]),
            )
            if confirmed:
                self._db.execute(
                    """INSERT INTO decision_history(decision_id, timestamp, actor, action, value)
                       VALUES (?, ?, ?, 'confirmed', ?)""",
                    (decision_id, updated_at, actor, chosen),
                )
            self._db.execute(
                "UPDATE decision_checkpoints SET decision_id=? WHERE id=?",
                (decision_id, checkpoint["id"]),
            )

    def enqueue_checkpoint(self, run_id: str, phase: str, question: str, rationale: str,
                           options: list[dict], recommendation: str = "", dimension: str = "",
                           blocking: bool = True, decision_id: str = "") -> dict:
        checkpoint_id = str(uuid.uuid4())
        decision_id = decision_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._db:
            existing_decision = self._db.execute(
                "SELECT 1 FROM decisions WHERE id=?", (decision_id,)
            ).fetchone()
            if not existing_decision:
                self._db.execute(
                    """INSERT INTO decisions(
                       id, run_id, title, status, rationale, created_at, updated_at)
                       VALUES (?, ?, ?, 'proposed', ?, ?, ?)""",
                    (decision_id, run_id, question, rationale, now, now),
                )
                self._db.execute(
                    """INSERT INTO decision_history(decision_id, timestamp, actor, action, value)
                       VALUES (?, ?, 'DesignFlow', 'proposed', ?)""",
                    (decision_id, now, question),
                )
            row = self._db.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM decision_checkpoints WHERE run_id=?",
                (run_id,),
            ).fetchone()
            sequence = int(row["next_sequence"])
            has_active = self._db.execute(
                "SELECT 1 FROM decision_checkpoints WHERE run_id=? AND status='active'", (run_id,)
            ).fetchone()
            status = "pending" if has_active else "active"
            self._db.execute(
                """INSERT INTO decision_checkpoints(
                   id, run_id, sequence, phase, dimension, question, rationale, recommendation,
                   blocking, status, created_at, decision_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (checkpoint_id, run_id, sequence, phase, dimension, question, rationale,
                 recommendation, int(blocking), status, now, decision_id),
            )
            for index, option in enumerate(options):
                option_id = str(option.get("id") or uuid.uuid4())
                self._db.execute(
                    """INSERT INTO decision_options(
                       id, checkpoint_id, sequence, label, summary, consequence, recommended)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (option_id, checkpoint_id, index, str(option.get("label", "")),
                     str(option.get("summary", "")), str(option.get("consequence", "")),
                     int(bool(option.get("recommended")))),
                )
        return self.checkpoint(checkpoint_id)

    def enqueue_recovery_action(
        self, run_id: str, failure_category: str, affected_provider: str,
        failed_turn_id: str, retry_eligible: bool, auto_failover_eligible: bool,
        retry_time_known: str
    ) -> str:
        with self._lock:
            action_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            self._db.execute("""
                INSERT INTO system_recovery_actions
                (id, run_id, failure_category, affected_provider, failed_turn_id,
                 retry_eligible, auto_failover_eligible, retry_time_known, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                action_id, run_id, failure_category, affected_provider, failed_turn_id,
                int(retry_eligible), int(auto_failover_eligible), retry_time_known, now
            ))
            self._db.commit()
            return action_id

    def active_recovery_action(self, run_id: str) -> dict:
        with self._lock:
            row = self._db.execute("""
                SELECT * FROM system_recovery_actions
                WHERE run_id = ? AND resolved_at IS NULL
                ORDER BY created_at DESC LIMIT 1
            """, (run_id,)).fetchone()
            if not row:
                return None
            return dict(row)

    def resolve_recovery_action(self, action_id: str, resolution_action: str):
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            self._db.execute("""
                UPDATE system_recovery_actions
                SET resolved_at = ?, resolution_action = ?
                WHERE id = ?
            """, (now, resolution_action, action_id))
            self._db.commit()

    def checkpoint(self, checkpoint_id: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM decision_checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
            if not row:
                return {}
            options = self._db.execute(
                "SELECT * FROM decision_options WHERE checkpoint_id=? ORDER BY sequence", (checkpoint_id,)
            ).fetchall()
        data = dict(row)
        data["blocking"] = bool(data["blocking"])
        data["options"] = [{**dict(option), "recommended": bool(option["recommended"])} for option in options]
        return data

    def current_checkpoint(self, run_id: str) -> dict:
        if not run_id:
            return {}
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT id FROM decision_checkpoints WHERE run_id=? AND status='active' ORDER BY sequence LIMIT 1",
                (run_id,),
            ).fetchone()
            if not row:
                row = self._db.execute(
                    "SELECT id FROM decision_checkpoints WHERE run_id=? AND status='pending' ORDER BY sequence LIMIT 1",
                    (run_id,),
                ).fetchone()
                if row:
                    self._db.execute("UPDATE decision_checkpoints SET status='active' WHERE id=?", (row["id"],))
        return self.checkpoint(row["id"]) if row else {}

    def latest_current_checkpoint(self) -> dict:
        """Recover the newest durable checkpoint, independent of later chat runs."""
        with self._lock, self._db:
            row = self._db.execute(
                """SELECT checkpoint.id
                   FROM decision_checkpoints AS checkpoint
                   JOIN workflow_instances AS workflow ON workflow.run_id=checkpoint.run_id
                   WHERE checkpoint.status IN ('active', 'pending')
                     AND workflow.state='WAITING_FOR_USER'
                   ORDER BY workflow.updated_at DESC,
                            CASE checkpoint.status WHEN 'active' THEN 0 ELSE 1 END,
                            checkpoint.sequence
                   LIMIT 1""",
            ).fetchone()
            if row:
                checkpoint = self._db.execute(
                    "SELECT run_id, status FROM decision_checkpoints WHERE id=?", (row["id"],)
                ).fetchone()
                if checkpoint["status"] == "pending":
                    self._db.execute(
                        "UPDATE decision_checkpoints SET status='active' WHERE id=?", (row["id"],)
                    )
        return self.checkpoint(row["id"]) if row else {}

    def answer_checkpoint(self, run_id: str, checkpoint_id: str, answered_by: str,
                          option_id: str = "", custom_answer: str = "") -> tuple[dict, dict]:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._db:
            checkpoint = self._db.execute(
                "SELECT * FROM decision_checkpoints WHERE id=? AND run_id=?", (checkpoint_id, run_id)
            ).fetchone()
            if not checkpoint or checkpoint["status"] != "active":
                raise ValueError("This checkpoint is no longer active")
            option = None
            if option_id:
                option = self._db.execute(
                    "SELECT * FROM decision_options WHERE id=? AND checkpoint_id=?", (option_id, checkpoint_id)
                ).fetchone()
                if not option:
                    raise ValueError("The selected option does not belong to this checkpoint")
            if not option and not custom_answer.strip():
                raise ValueError("Select an option or provide a custom answer")
            self._db.execute(
                """UPDATE decision_checkpoints SET status='answered', answered_at=?, answered_by=?,
                   selected_option_id=?, custom_answer=? WHERE id=?""",
                (now, answered_by, option_id or None, custom_answer.strip(), checkpoint_id),
            )
            chosen = custom_answer.strip() if custom_answer.strip() else f"{option['label']} — {option['summary']}"
            if checkpoint["decision_id"]:
                self._db.execute(
                    """UPDATE decisions SET status='confirmed', chosen_option=?, answered_by=?, updated_at=?
                       WHERE id=?""",
                    (chosen, answered_by, now, checkpoint["decision_id"]),
                )
                self._db.execute(
                    """INSERT INTO decision_history(decision_id, timestamp, actor, action, value)
                       VALUES (?, ?, ?, 'confirmed', ?)""",
                    (checkpoint["decision_id"], now, answered_by, chosen),
                )
            next_row = self._db.execute(
                "SELECT id FROM decision_checkpoints WHERE run_id=? AND status='pending' ORDER BY sequence LIMIT 1",
                (run_id,),
            ).fetchone()
            if next_row:
                self._db.execute("UPDATE decision_checkpoints SET status='active' WHERE id=?", (next_row["id"],))
        answer = chosen
        answered = self.checkpoint(checkpoint_id)
        answered["answer"] = answer
        return answered, self.checkpoint(next_row["id"]) if next_row else {}

    def decision(self, decision_id: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
            if not row:
                return {}
            history = self._db.execute(
                "SELECT timestamp, actor, action, value FROM decision_history WHERE decision_id=? ORDER BY id",
                (decision_id,),
            ).fetchall()
        result = dict(row)
        result["history"] = [dict(item) for item in history]
        return result

    def run_decisions(self, run_id: str) -> list[dict]:
        with self._lock:
            ids = [row["id"] for row in self._db.execute(
                "SELECT id FROM decisions WHERE run_id=? ORDER BY created_at", (run_id,)
            ).fetchall()]
        return [self.decision(decision_id) for decision_id in ids]

    def run_checkpoints(self, run_id: str) -> list[dict]:
        with self._lock:
            ids = [row["id"] for row in self._db.execute(
                "SELECT id FROM decision_checkpoints WHERE run_id=? ORDER BY sequence", (run_id,)
            ).fetchall()]
        return [self.checkpoint(checkpoint_id) for checkpoint_id in ids]

    def reject_malformed_checkpoints(self) -> list[str]:
        """Quarantine legacy/meta checkpoints that cannot be valid user decisions."""
        now = datetime.now(timezone.utc).isoformat()
        rejected = []
        with self._lock, self._db:
            rows = self._db.execute(
                """SELECT id, decision_id, question FROM decision_checkpoints
                   WHERE status IN ('active', 'pending')"""
            ).fetchall()
            for row in rows:
                question = str(row["question"] or "").strip()
                malformed = (
                    not question.endswith("?")
                    or question.lower().startswith("recommendation:")
                    or "what unresolved product decision must be settled" in question.lower()
                )
                if not malformed:
                    continue
                rejected.append(str(row["id"]))
                self._db.execute(
                    "UPDATE decision_checkpoints SET status='rejected', answered_at=? WHERE id=?",
                    (now, row["id"]),
                )
                if row["decision_id"]:
                    self._db.execute(
                        "UPDATE decisions SET status='rejected', updated_at=? WHERE id=? AND status='proposed'",
                        (now, row["decision_id"]),
                    )
        return rejected

    def answered_checkpoint_questions(self) -> list[str]:
        """Return durable answered questions for cross-run duplicate detection."""
        with self._lock:
            rows = self._db.execute(
                """SELECT question FROM decision_checkpoints
                   WHERE status='answered' ORDER BY answered_at"""
            ).fetchall()
        return [str(row["question"]) for row in rows if str(row["question"]).strip()]

    def load_agents(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, config_json FROM agents ORDER BY sort_order"
            ).fetchall()
        configs = []
        for row in rows:
            config = json.loads(row["config_json"])
            config["id"] = row["id"]
            config["api_key"] = decrypt_key(config.get("api_key", ""))
            configs.append(config)
        return configs

    def save_agents(self, configs: list[dict]):
        with self._lock, self._db:
            self._db.execute("DELETE FROM agents")
            for index, original in enumerate(configs):
                config = dict(original)
                agent_id = config.pop("id")
                # Encrypt API credentials into a project-local database.
                config["api_key"] = encrypt_key(config.get("api_key", ""))
                self._db.execute(
                    "INSERT INTO agents(id, sort_order, config_json) VALUES (?, ?, ?)",
                    (agent_id, index, json.dumps(config)),
                )

    def start_run(self, run_id: str, idea: str, run_kind: str = "planning_workflow"):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO runs(run_id, idea, status, started_at, run_kind) VALUES (?, ?, ?, ?, ?)",
                (run_id, idea, "running", now, run_kind),
            )

    def update_run_contract(self, run_id: str, run_kind: str):
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET run_kind=? WHERE run_id=?", (run_kind, run_id))

    def resume_run(self, run_id: str):
        """Reopen an existing logical run without replacing its identity or metrics."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE runs SET status='running', completed_at=NULL WHERE run_id=?", (run_id,)
            )
            if cursor.rowcount != 1:
                raise ValueError("Saved run no longer exists")

    def reconcile_interrupted_runs(self) -> list[str]:
        """Mark process-abandoned active rows without discarding resumable state."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT run_id FROM runs WHERE status IN ('running', 'paused', 'needs_attention')"
            ).fetchall()
            run_ids = [str(row["run_id"]) for row in rows]
            if not run_ids:
                return []
            placeholders = ",".join("?" for _ in run_ids)
            self._db.execute(
                f"UPDATE runs SET status='interrupted', completed_at=? WHERE run_id IN ({placeholders})",
                (now, *run_ids),
            )
            self._db.execute(
                f"""UPDATE turns SET status='interrupted', completed_at=?,
                    error=CASE WHEN error='' THEN 'Server process interrupted this turn' ELSE error END
                    WHERE run_id IN ({placeholders}) AND status IN ('running', 'waiting')""",
                (now, *run_ids),
            )
        return run_ids

    def latest_run_id(self) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return str(row["run_id"]) if row else ""

    def finish_run(self, run_id: str, status: str, agents: list[dict], outcome: dict | None = None):
        now = datetime.now(timezone.utc).isoformat()
        tokens = sum(int(agent.get("total_tokens", 0) or 0) for agent in agents)
        cached = sum(int(agent.get("cached_input_tokens", 0) or 0) for agent in agents)
        cost = sum(float(agent.get("cost_usd", 0) or 0) for agent in agents)
        pricing_complete = int(all(agent.get("pricing_known", False) for agent in agents))
        with self._lock, self._db:
            terminal_turn_status = "cancelled" if status in {"stopped", "cancelled"} else "failed"
            self._db.execute(
                """UPDATE turns SET status=?, completed_at=?,
                          error=CASE WHEN error='' THEN ? ELSE error END
                   WHERE run_id=? AND status IN ('running', 'waiting')""",
                (terminal_turn_status, now, f"Run {status}", run_id),
            )
            self._db.execute(
                """UPDATE runs
                   SET status=?, completed_at=?, total_tokens=?, cached_input_tokens=?,
                       estimated_cost_usd=?, pricing_complete=?, outcome_json=?
                   WHERE run_id=?""",
                (status, now, tokens, cached, cost, pricing_complete, json.dumps(outcome or {}), run_id),
            )

    def update_run_metrics(self, run_id: str, agents: list[dict]):
        tokens = sum(int(agent.get("total_tokens", 0) or 0) for agent in agents)
        cached = sum(int(agent.get("cached_input_tokens", 0) or 0) for agent in agents)
        cost = sum(float(agent.get("cost_usd", 0) or 0) for agent in agents)
        pricing_complete = int(all(agent.get("pricing_known", False) for agent in agents))
        with self._lock, self._db:
            self._db.execute(
                """UPDATE runs
                   SET total_tokens=?, cached_input_tokens=?, estimated_cost_usd=?, pricing_complete=?
                   WHERE run_id=?""",
                (tokens, cached, cost, pricing_complete, run_id),
            )

    def project_usage(self) -> dict:
        """Return durable cumulative usage for every run in this project."""
        with self._lock:
            row = self._db.execute(
                """SELECT COALESCE(SUM(total_tokens), 0) AS total_tokens,
                          COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                          COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                          COALESCE(MIN(pricing_complete), 1) AS pricing_complete,
                          COUNT(*) AS run_count
                   FROM runs"""
            ).fetchone()
        return {
            "total_tokens": int(row["total_tokens"] or 0),
            "cached_input_tokens": int(row["cached_input_tokens"] or 0),
            "estimated_cost_usd": float(row["estimated_cost_usd"] or 0),
            "pricing_complete": bool(row["pricing_complete"]),
            "run_count": int(row["run_count"] or 0),
        }

    def append_event(self, run_id: str | None, event: dict):
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO events(run_id, timestamp, kind, agent, data_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    run_id,
                    event.get("timestamp", ""),
                    event.get("kind", ""),
                    event.get("agent", ""),
                    json.dumps(event.get("data", {})),
                ),
            )
            self._record_turn(run_id, event)

    def _record_turn(self, run_id: str | None, event: dict):
        data = event.get("data", {})
        turn_id = data.get("turn_id")
        if not run_id or not turn_id:
            return
        kind = event.get("kind", "")
        timestamp = event.get("timestamp", "")
        if kind == "turn_start":
            self._db.execute(
                """INSERT INTO turns(
                       run_id, turn_id, agent, phase, role, round_number, iteration,
                       status, attempt, started_at, completed_at, error
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, NULL, '')
                   ON CONFLICT(run_id, turn_id) DO UPDATE SET
                       agent=excluded.agent, phase=excluded.phase, role=excluded.role,
                       round_number=excluded.round_number, iteration=excluded.iteration,
                       status='running', attempt=excluded.attempt, completed_at=NULL, error=''""",
                (
                    run_id, turn_id, event.get("agent", ""), data.get("phase", ""),
                    data.get("role", ""), data.get("round"), data.get("iteration"),
                    int(data.get("attempt", 1) or 1), timestamp,
                ),
            )
        elif kind == "retry":
            self._db.execute(
                "UPDATE turns SET status='waiting', attempt=? WHERE run_id=? AND turn_id=?",
                (int(data.get("attempt", 1) or 1), run_id, turn_id),
            )
        elif kind == "error":
            self._db.execute(
                """UPDATE turns SET status='failed', attempt=?, error=?
                   WHERE run_id=? AND turn_id=?""",
                (int(data.get("attempt", 1) or 1), data.get("error", ""), run_id, turn_id),
            )
        elif kind == "turn_end":
            self._db.execute(
                """UPDATE turns SET status='completed', attempt=?, completed_at=?,
                       error='', usage_json=?, response_preview=?
                   WHERE run_id=? AND turn_id=?""",
                (
                    int(data.get("attempt", 1) or 1), timestamp,
                    json.dumps(data.get("usage", {})), data.get("response", "")[:500],
                    run_id, turn_id,
                ),
            )

    def run_turns(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """SELECT turn_id, agent, phase, role, round_number, iteration,
                          status, attempt, started_at, completed_at, error,
                          usage_json, response_preview
                   FROM turns WHERE run_id=? ORDER BY turn_id""",
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["usage"] = json.loads(item.pop("usage_json"))
            result.append(item)
        return result

    def recent_runs(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """SELECT run_id, idea, status, started_at, completed_at,
                          total_tokens, cached_input_tokens, estimated_cost_usd, pricing_complete,
                          run_kind, outcome_json
                   FROM runs ORDER BY started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["outcome"] = json.loads(item.pop("outcome_json") or "{}")
            result.append(item)
        return result

    def run_events(self, run_id: str, limit: int = 200, offset: int = 0) -> list[dict]:
        """Load one persisted transcript page only when a user requests it."""
        with self._lock:
            rows = self._db.execute(
                """SELECT id, run_id, timestamp, kind, agent, data_json
                   FROM events WHERE run_id=? ORDER BY id LIMIT ? OFFSET ?""",
                (run_id, max(1, min(int(limit), 200)), max(0, int(offset))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["event_id"] = item.pop("id")
            item["data"] = json.loads(item.pop("data_json") or "{}")
            result.append(item)
        return result

    def recent_run_activity(self, run_id: str, limit: int = 8) -> list[dict]:
        """Return a small chronological activity tail without replaying a run."""
        meaningful = ("turn_end", "error", "phase", "file_write", "steer", "done", "retry")
        placeholders = ",".join("?" for _ in meaningful)
        with self._lock:
            rows = self._db.execute(
                f"""SELECT id, run_id, timestamp, kind, agent, data_json
                    FROM events WHERE run_id=? AND kind IN ({placeholders})
                    ORDER BY id DESC LIMIT ?""",
                (run_id, *meaningful, max(1, min(int(limit), 20))),
            ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["event_id"] = item.pop("id")
            item["data"] = json.loads(item.pop("data_json") or "{}")
            result.append(item)
        return result

    def get_mcp_servers(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM mcp_servers ORDER BY name").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["args"] = json.loads(item.pop("args_json"))
            item["env"] = json.loads(item.pop("env_json"))
            result.append(item)
        return result

    def add_implementation_report(self, actor: str, kind: str, task: str, summary: str,
                                  code_references: list[str]) -> dict:
        report_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO implementation_reports(
                   id, created_at, actor, kind, task, summary, code_references_json, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'open')""",
                (report_id, now, actor, kind, task, summary, json.dumps(code_references)),
            )
        return {
            "id": report_id, "created_at": now, "actor": actor, "kind": kind,
            "task": task, "summary": summary, "code_references": code_references, "status": "open",
        }

    def implementation_reports(self, status: str = "open", limit: int = 50) -> list[dict]:
        bounded = max(1, min(int(limit), 200))
        query = "SELECT * FROM implementation_reports"
        params: tuple = ()
        if status:
            query += " WHERE status=?"
            params = (status,)
        query += " ORDER BY created_at DESC LIMIT ?"
        params += (bounded,)
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["code_references"] = json.loads(item.pop("code_references_json") or "[]")
            result.append(item)
        return result

    def add_mcp_server(self, server_id: str, name: str, command: str, args: list[str], env: dict, username: str = "", password: str = ""):
        with self._lock, self._db:
            self._db.execute(
                """INSERT OR REPLACE INTO mcp_servers (id, name, command, args_json, env_json, username, password)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (server_id, name, command, json.dumps(args), json.dumps(env), username, password)
            )

    def delete_mcp_server(self, server_id: str):
        with self._lock, self._db:
            self._db.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))

    def load_provider_turn_peaks(self) -> dict[str, int]:
        """Load durable per-provider turn sizes, deriving legacy values from stored events once."""
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM key_value WHERE key = ?", ("provider_turn_peaks",)
            ).fetchone()
            if row:
                return {str(key): int(value) for key, value in json.loads(row["value"]).items()}
            event_rows = self._db.execute(
                "SELECT data_json FROM events WHERE kind = 'turn_end'"
            ).fetchall()
        peaks: dict[str, int] = {}
        for event_row in event_rows:
            try:
                data = json.loads(event_row["data_json"])
                provider = str(data.get("provider_id") or "")
                usage = data.get("usage", {})
                total = int(usage.get("total_tokens", 0) or 0)
                if provider and total > 0:
                    peaks[provider] = max(peaks.get(provider, 0), total)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        if peaks:
            with self._lock, self._db:
                self._db.execute(
                    "INSERT OR REPLACE INTO key_value (key, value) VALUES (?, ?)",
                    ("provider_turn_peaks", json.dumps(peaks)),
                )
        return peaks

    def record_provider_turn_peak(self, provider_id: str, total_tokens: int) -> None:
        if not provider_id or total_tokens <= 0:
            return
        peaks = self.load_provider_turn_peaks()
        if total_tokens <= peaks.get(provider_id, 0):
            return
        peaks[provider_id] = int(total_tokens)
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO key_value (key, value) VALUES (?, ?)",
                ("provider_turn_peaks", json.dumps(peaks)),
            )

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
