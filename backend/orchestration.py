from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any, Callable

from pydantic import ValidationError

from .agents.base import AgentBase
from .context import ContextTree
from .events import Event, EventKind
from .workflow import (
    LoopCommandKind, LoopManager, LoopSignal, WorkflowEngine, WorkflowEvent, WorkflowRepository, WorkflowState,
)
from .workflow.models import (
    DebateReview, DebateRevision, DiscoveryAssessment, ExpertProposal, WorkflowOperation,
)
from .workflow.planning import PlanningService


PROPOSAL_SYSTEM = """You are one expert in a design-only planning workflow.
Return JSON only. Do not write Markdown and do not address the other experts.
Be concrete, proportionate, and explicit about trade-offs. Use exactly this shape:
{"components":[{"name":"","responsibility":"","interfaces":[]}],
 "decisions":[{"topic":"","recommendation":"","rationale":"","alternatives":[]}],
 "risks":[{"risk":"","mitigation":""}],"assumptions":[],
 "unknowns":[{"question":"","validation":""}]}
When prior debate turns are supplied, directly evaluate them. Preserve sound decisions, challenge
weak consequential assumptions, and propose a materially better alternative where warranted.
Agreement is allowed only after attempting falsification; do not manufacture stylistic disagreement.
"""

REVIEW_SYSTEM = """You are reviewing a concrete architecture proposal, not creating an independent proposal.
Return JSON only with this exact shape:
{"challenges":[{"id":"stable-short-id","target_topic":"","claim":"","evidence":"",
"consequence":"","proposed_change":"","materiality":"low|medium|high"}],"validated_topics":[]}
Challenge only a consequential defect, unsupported assumption, missed requirement, or unsafe trade-off.
Evidence must cite supplied product or repository context. Do not manufacture disagreement and do not repeat
an earlier challenge. If the proposal is sound, return an empty challenges list and name validated topics.
"""

REVISION_SYSTEM = """You are the coordinating architect. Respond to every supplied challenge and produce the
single revised canonical design. Return JSON only with this exact shape:
{"proposal":{"components":[],"decisions":[],"risks":[],"assumptions":[],"unknowns":[]},
"dispositions":[{"challenge_id":"","status":"accepted|defended|merged|unresolved","rationale":"",
"resulting_decision":""}]}
There must be exactly one disposition per challenge ID. Accept or merge valid criticism, defend a decision with
grounded evidence, and use unresolved only when repository evidence cannot choose between materially different
product outcomes. Preserve useful specificity from the opening proposal.
"""

DISCOVERY_SYSTEM = """You are the discovery gate for a product-design workflow.
Judge only whether the supplied evidence is sufficient to begin concrete architecture work.
Use ordinary product language as evidence: for example, "personal" establishes the primary actor as
the person using the product. Infer only what the supplied words directly entail. Put reasonable,
reversible defaults for omitted details in provisional_assumptions; do not ask the user to restate
facts already implied by the goal.

A question is blocking only when no safe reversible assumption permits concrete design, or when the
answer changes a core capability, trust boundary, data ownership, irreversible commitment, or
acceptance criterion. Preferences, implementation details, and facts that can be validated later are
not blocking. A short but actionable goal with an actor, action, and outcome is adequate.

Return JSON only:
{"adequate":true,"evidence_summary":"brief factual basis","provisional_assumptions":["reversible default"],"blocking_questions":[]}
Return at most three blocking questions, ordered by information value. If adequate=true,
blocking_questions must be empty. Never ask a generic persona or target-user question when the goal
already identifies a personal tool or otherwise establishes its primary actor.
"""


def _json_object(text: str) -> dict[str, Any]:
    candidate = (text or "").strip()
    fenced = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", candidate, re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        # Some providers wrap an otherwise valid object in a short preamble.
        # Decode the first complete JSON object without accepting arbitrary
        # trailing model prose as part of the typed payload.
        start = candidate.find("{")
        if start < 0:
            raise ValueError("expert response was not valid JSON") from exc
        try:
            value, _ = json.JSONDecoder().raw_decode(candidate[start:])
        except json.JSONDecodeError as nested_exc:
            raise ValueError("expert response was not valid JSON") from nested_exc
    if not isinstance(value, dict):
        raise ValueError("expert response must be a JSON object")
    return value


class Orchestration:
    """Single-path durable planner: proposals -> local analysis -> projections."""

    def __init__(
        self, *, agents: list[AgentBase], workspace, store, run_id: str,
        event_cb: Callable[[Event], Any] | None = None, max_tokens: int = 100000,
    ):
        self.agents = agents
        self.ws = workspace
        self.store = store
        self.run_id = run_id
        self._cb = event_cb
        self.max_tokens = max_tokens
        self.repository = WorkflowRepository(store)
        self.context_tree = ContextTree(store)
        self.engine = WorkflowEngine(self.repository)
        self.loop_manager = LoopManager(self.engine)
        self.planning = PlanningService(self.repository)
        self._running = False
        self._answer_event = asyncio.Event()
        self._pending_answer = ""
        self._pending_conflict_id = ""
        self.completion_kind = ""
        self.completion_files: list[str] = []
        self.phase_usage: dict[str, int] = {}
        self.failed_turn = None
        self.contract = None
        self._coordinator_name = agents[0].name if agents else ""
        self._participant_ids: set[str] = set()

    @property
    def participants(self) -> list[AgentBase]:
        """Agents that have actually crossed a provider invocation boundary."""
        return [agent for agent in self.agents if (agent.config.id or agent.name) in self._participant_ids]

    def _register_participant(self, agent: AgentBase) -> None:
        agent_id = agent.config.id or agent.name
        if agent_id in self._participant_ids:
            return
        self._participant_ids.add(agent_id)
        ordinal = self.agents.index(agent)
        with self.store._lock, self.store._db:
            self.store._db.execute(
                "INSERT OR REPLACE INTO run_participants(run_id,agent_id,role,provider_id,provider_name,"
                "provider_kind,model,ordinal) VALUES(?,?,?,?,?,?,?,?)",
                (self.run_id, agent_id, agent.config.role or agent.name,
                 agent.config.base_id or agent.config.id, agent.config.extra.get("runtime_base_name", agent.config.name),
                 agent.config.kind, agent.config.model or "default model", ordinal),
            )

    @property
    def phase(self):
        try:
            return type("Phase", (), {"value": self.repository.get(self.run_id).state.value.lower()})()
        except KeyError:
            return type("Phase", (), {"value": "created"})()

    def _emit(self, event: Event):
        if self._cb:
            self._cb(event)

    def _state_event(self, snapshot):
        self._emit(Event(EventKind.PHASE, data={
            "phase": snapshot.state.value.lower(),
            "workflow_state": snapshot.state.value,
            "state_version": snapshot.state_version,
            "allowed_actions": snapshot.allowed_actions,
            "status": "waiting_for_approval" if snapshot.state == WorkflowState.WAITING_FOR_USER else "running",
        }))

    async def _proposal(
        self, agent: AgentBase, goal: str, task: str, source_context: str,
    ) -> tuple[AgentBase, ExpertProposal, str]:
        prompt = (
            f"Product goal: {goal}\nCurrent planning task: {task or goal}\n"
            f"Your perspective: {agent.config.role or agent.name}\n"
            "Use the repository evidence below. Do not invent a generic application when the evidence "
            "describes a specific product.\n\n"
            f"{source_context}\n\nReturn your bounded debate turn now."
        )
        turn_id = f"proposal-{agent.name}-{uuid.uuid4().hex[:8]}"
        self._emit(Event(EventKind.TURN_START, agent=agent.name, data={
            "turn_id": turn_id, "phase": "diverging", "attempt": 1,
        }))
        self._register_participant(agent)
        raw = await asyncio.to_thread(
            agent.send, prompt, PROPOSAL_SYSTEM, "", context_only=True,
        )
        try:
            proposal = ExpertProposal.model_validate(_json_object(raw))
        except (ValueError, ValidationError) as exc:
            raise ValueError(f"{agent.name} returned an invalid typed proposal: {exc}") from exc
        self._emit(Event(EventKind.TURN_END, agent=agent.name, data={
            "turn_id": turn_id, "phase": "diverging", "attempt": 1, "response": raw,
            "usage": agent.last_usage.to_dict(), "tokens": agent.last_usage.total_tokens,
        }))
        return agent, proposal, raw

    async def _typed_debate_call(self, agent: AgentBase, *, prompt: str, system: str,
                                 phase: str, turn_kind: str, model, allow_repair: bool = True):
        turn_id = f"{turn_kind}-{agent.name}-{uuid.uuid4().hex[:8]}"
        current_prompt = prompt
        last_error = None
        for attempt in ((1, 2) if allow_repair else (1,)):
            self._emit(Event(EventKind.TURN_START, agent=agent.name, data={
                "turn_id": turn_id, "phase": phase, "role": turn_kind, "attempt": attempt,
            }))
            self._register_participant(agent)
            raw = await asyncio.to_thread(agent.send, current_prompt, system, "", context_only=True)
            try:
                payload = _json_object(raw)
                if model is DebateRevision and "proposal" not in payload:
                    payload = {"proposal": ExpertProposal.model_validate(payload).model_dump(), "dispositions": []}
                value = model.model_validate(payload)
            except (ValueError, ValidationError) as exc:
                last_error = exc
                self._emit(Event(EventKind.TURN_END, agent=agent.name, data={
                    "turn_id": turn_id, "phase": phase, "role": turn_kind, "attempt": attempt,
                    "error": "The agent response did not match the required structured contract.",
                }))
                if attempt == 1 and allow_repair:
                    current_prompt = (
                        prompt
                        + "\n\nSchema repair: your prior response was invalid. Return one JSON object only, "
                        "with no Markdown fences or prose, matching this JSON Schema:\n"
                        + json.dumps(model.model_json_schema(), ensure_ascii=False)
                    )
                    continue
                break
            self._emit(Event(EventKind.TURN_END, agent=agent.name, data={
                "turn_id": turn_id, "phase": phase, "role": turn_kind, "attempt": attempt,
                "response": raw, "usage": agent.last_usage.to_dict(), "tokens": agent.last_usage.total_tokens,
            }))
            return value, raw
        raise ValueError(f"{agent.name} returned an invalid {turn_kind} after one repair attempt: {last_error}") from last_error

    async def _revision_call(self, agent: AgentBase, prompt: str,
                             challenge_ids: set[str]) -> tuple[DebateRevision, str]:
        """Accept a harmless bare proposal or perform one bounded envelope repair."""
        try:
            value, raw = await self._typed_debate_call(
                agent, prompt=prompt, system=REVISION_SYSTEM, phase="diverging",
                turn_kind="revision", model=DebateRevision, allow_repair=False,
            )
            disposition_ids = [item.challenge_id for item in value.dispositions]
            if len(disposition_ids) != len(set(disposition_ids)) or set(disposition_ids) != challenge_ids:
                raise ValueError("revision did not disposition every challenge exactly once")
            return value, raw
        except ValueError as first_error:
            # Providers sometimes follow the nested proposal schema but omit the
            # outer revision envelope. This is losslessly repairable only when
            # there are no challenges requiring a disposition.
            if not challenge_ids:
                # Re-run once with an explicit correction rather than attempting
                # to scrape the validation error or infer challenged decisions.
                repair_prompt = prompt + (
                    "\n\nSchema correction: your prior response omitted the outer envelope. "
                    "Return exactly {\"proposal\": <the complete proposal object>, \"dispositions\": []}."
                )
            else:
                repair_prompt = prompt + (
                    "\n\nSchema correction: return the required outer proposal and dispositions fields. "
                    f"Disposition every challenge ID exactly once: {json.dumps(sorted(challenge_ids))}."
                )
            try:
                return await self._typed_debate_call(
                    agent, prompt=repair_prompt, system=REVISION_SYSTEM, phase="diverging",
                    turn_kind="revision_repair", model=DebateRevision, allow_repair=False,
                )
            except ValueError as repair_error:
                raise ValueError(
                    "The coordinating architect could not return the required revised-design contract after one retry."
                ) from repair_error

    async def _assess_discovery(self, idea: str, task: str) -> DiscoveryAssessment:
        if not self.agents:
            raise ValueError("planning requires at least one available expert")
        packet = self.context_tree.retrieve(
            query=(
                f"Assess whether this planning goal has enough grounded product evidence: {task or idea}. "
                "Extract the actor, requested action, outcome, explicit constraints, and repository evidence. "
                "Separate genuine blockers from reversible assumptions and later validation."
            ),
            run_id=self.run_id, max_tokens=3000,
        )
        prompt = (
            f"Planning goal: {idea}\nCurrent task: {task or idea}\n\n"
            f"Evidence packet:\n{packet.text}\n\nReturn the discovery assessment."
        )
        self._register_participant(self.agents[0])
        raw = await asyncio.to_thread(
            self.agents[0].send, prompt, DISCOVERY_SYSTEM, "", context_only=True,
        )
        try:
            return DiscoveryAssessment.model_validate(_json_object(raw))
        except (ValueError, ValidationError):
            # Discovery is advisory and cannot create a user checkpoint. A
            # malformed assessment must not prevent grounded debate work.
            return DiscoveryAssessment(
                adequate=True,
                evidence_summary="Discovery assessment output was unavailable; proceed using grounded project evidence.",
                provisional_assumptions=[
                    "Validate discovery completeness during sequential debate and artifact validation."
                ],
                blocking_questions=[],
            )

    async def _complete_discovery(self, idea: str, task: str):
        while True:
            current = self.repository.get(self.run_id)
            if current.state == WorkflowState.WAITING_FOR_USER:
                checkpoint = self.store.current_checkpoint(self.run_id)
                if not checkpoint or checkpoint.get("phase") != "discovering":
                    return
                self._answer_event.clear()
                await self._answer_event.wait()
                if not self._running:
                    raise asyncio.CancelledError()
                answer = self._pending_answer.strip()
                self.context_tree.upsert(
                    run_id=self.run_id, node_type="discovery_answer", source_type="user",
                    source_ref=checkpoint["id"], title=checkpoint["question"], content=answer,
                    summary=answer, authority=6, importance=6,
                )
                current = self.loop_manager.advance(self.run_id, LoopSignal.ANSWER_RECORDED, {
                    "checkpoint_id": checkpoint["id"], "phase": "discovering",
                })
                self._pending_answer = ""
                self._state_event(current)

            assessment = await self._assess_discovery(idea, task)
            self.context_tree.upsert(
                run_id=self.run_id, node_type="discovery_assessment", source_type="workflow",
                source_ref=f"assessment-{uuid.uuid4().hex[:8]}", title="Discovery adequacy assessment",
                content=assessment.model_dump_json(indent=2), summary=assessment.evidence_summary,
                authority=5, importance=6,
            )
            for index, assumption in enumerate(assessment.provisional_assumptions):
                self.context_tree.upsert(
                    run_id=self.run_id, node_type="assumption", source_type="workflow",
                    source_ref=f"discovery-assumption-{index}", title="Provisional discovery assumption",
                    content=assumption, summary=assumption, authority=3, importance=4,
                )
            if assessment.adequate:
                snapshot = self.loop_manager.advance(self.run_id, LoopSignal.DISCOVERY_ADEQUATE, {
                    "source": "typed_discovery_assessment", "evidence_summary": assessment.evidence_summary,
                })
                self._state_event(snapshot)
                return

            # Discovery uncertainty is context for later validation, not authority
            # to interrupt the user. Only a conflict grounded in accepted proposals
            # may create a product-decision checkpoint.
            for index, question in enumerate(assessment.blocking_questions):
                unknown = f"Unresolved discovery unknown: {question}"
                self.context_tree.upsert(
                    run_id=self.run_id, node_type="unknown", source_type="workflow",
                    source_ref=f"discovery-unknown-{index}", title="Discovery unknown for later validation",
                    content=unknown, summary=unknown, authority=2, importance=3,
                )
            snapshot = self.loop_manager.advance(self.run_id, LoopSignal.DISCOVERY_ADEQUATE, {
                "source": "nonblocking_discovery_unknowns",
                "evidence_summary": assessment.evidence_summary,
                "unknown_count": len(assessment.blocking_questions),
            })
            self._state_event(snapshot)
            return

    async def run(self, idea: str, task: str = ""):
        self._running = True
        self.repository.create(self.run_id)
        self.repository.save_goal(self.run_id, idea)
        self.context_tree.sync_workspace(self.ws, self.run_id)
        while self._running:
            current = self.repository.get(self.run_id)
            command = self.loop_manager.select(current)

            if command.kind == LoopCommandKind.COMPLETE:
                self.completion_kind = "planning_complete"
                self.completion_files = ["DESIGN.md", "PLAN.md", "DECISIONS.md"]
                self._running = False
                return self.ws.snapshot()
            if command.kind == LoopCommandKind.START:
                self._state_event(self.loop_manager.advance(self.run_id, LoopSignal.BEGIN, {"goal": idea}))
                continue
            if command.kind == LoopCommandKind.ASSESS_DISCOVERY:
                await self._complete_discovery(idea, task)
                continue
            if command.kind == LoopCommandKind.WAIT_FOR_USER:
                if current.resume_state == WorkflowState.DISCOVERING:
                    await self._complete_discovery(idea, task)
                elif current.resume_state == WorkflowState.RESOLVING:
                    await self._resume_waiting_conflict()
                elif current.resume_state == WorkflowState.DIVERGING:
                    await self._resume_waiting_debate_review()
                else:
                    raise ValueError(f"unsupported user-wait resume state {current.resume_state}")
                continue
            if command.kind == LoopCommandKind.PRODUCE_PROPOSALS:
                try:
                    await self._run_divergence(idea, task)
                except Exception as exc:
                    if self.repository.get(self.run_id).state == WorkflowState.DIVERGING:
                        self.loop_manager.advance(self.run_id, LoopSignal.FAILED, {
                            "failure_code": "debate_contract_failed",
                            "failure_detail": {"error": str(exc)},
                        })
                    raise
                continue
            if command.kind == LoopCommandKind.ANALYZE:
                conflicts = self.planning.analyze(self.run_id).conflicts
                signal = LoopSignal.MATERIAL_CONFLICTS if conflicts else LoopSignal.NO_MATERIAL_CONFLICTS
                payload = {"conflict_ids": [item.id for item in conflicts]} if conflicts else {}
                self._state_event(self.loop_manager.advance(self.run_id, signal, payload))
                continue
            if command.kind == LoopCommandKind.RESOLVE:
                await self._run_resolution(idea)
                continue
            if command.kind == LoopCommandKind.SYNTHESIZE:
                projection = self.planning.project(self.run_id, idea)
                self.ws.write("design", projection.design)
                self.ws.write("plan", projection.plan)
                self.ws.write("decisions", projection.decisions)
                self._state_event(self.loop_manager.advance(self.run_id, LoopSignal.PROJECTIONS_READY, {
                    "files": ["DESIGN.md", "PLAN.md", "DECISIONS.md"],
                }))
                continue
            if command.kind == LoopCommandKind.VALIDATE:
                projection = self.planning.project(self.run_id, idea)
                errors = self._projection_errors(projection)
                if errors:
                    self.loop_manager.advance(self.run_id, LoopSignal.FAILED, {
                        "failure_code": "projection_invalid", "failure_detail": {"errors": errors},
                    })
                    raise ValueError("projection validation failed: " + " | ".join(errors))
                self._state_event(self.loop_manager.advance(self.run_id, LoopSignal.VALIDATION_PASSED, {}))
                continue
            if command.kind == LoopCommandKind.WAIT_FOR_RECOVERY:
                raise ValueError(f"workflow requires recovery from {current.state.value}")

        raise asyncio.CancelledError()

    async def _run_divergence(self, idea: str, task: str) -> None:
        operation_id = f"diverge-{self.run_id}"
        selected = self.agents[:min(3, len(self.agents))]
        stored_proposals = self.repository.proposals(self.run_id)
        if not stored_proposals:
            if not selected:
                raise ValueError("planning requires at least one available expert")
            debate_turns = self.repository.debate_turns(self.run_id)
            if not debate_turns:
                self.repository.start_operation(WorkflowOperation(
                    id=operation_id, run_id=self.run_id, operation_type="sequential_design_review",
                    state=WorkflowState.DIVERGING, status="running",
                ))
            source_context = self.context_tree.retrieve(
                query=f"{task or idea}\nProduce an architecture proposal.",
                run_id=self.run_id, max_tokens=6000,
            ).text
            coordinator = selected[0]
            reviews: list[DebateReview] = []
            known_challenges: list[dict[str, Any]] = []
            if not debate_turns:
                _, opening, _ = await self._proposal(coordinator, idea, task, source_context)
                self.repository.save_debate_turn(
                    run_id=self.run_id, operation_id=operation_id, sequence=1, agent=coordinator.name,
                    turn_kind="opening", payload=opening.model_dump(),
                )
                reviewers = selected[1:] or [coordinator]
                sequence = 2
            else:
                opening_turn = next((turn for turn in debate_turns if turn["turn_kind"] == "opening"), None)
                if not opening_turn:
                    raise ValueError("durable debate is missing its opening turn")
                opening = ExpertProposal.model_validate(opening_turn["payload"])
                for turn in debate_turns:
                    if turn["turn_kind"] == "challenge":
                        review = DebateReview.model_validate(turn["payload"])
                        reviews.append(review)
                        known_challenges.extend(item.model_dump() for item in review.challenges)
                reviewers = []
                sequence = 2 + len(reviews)
            for reviewer in reviewers:
                review_prompt = (
                    f"Product goal: {idea}\nCurrent task: {task or idea}\n\nRepository evidence:\n{source_context}\n\n"
                    f"Opening proposal:\n{opening.model_dump_json(indent=2)}\n\n"
                    f"Challenges already raised (do not repeat):\n{json.dumps(known_challenges, indent=2)}"
                )
                review, _ = await self._typed_debate_call(
                    reviewer, prompt=review_prompt, system=REVIEW_SYSTEM, phase="diverging",
                    turn_kind="challenge", model=DebateReview,
                )
                for challenge in review.challenges:
                    if any(item["id"] == challenge.id for item in known_challenges):
                        raise ValueError(f"duplicate challenge id {challenge.id}")
                    known_challenges.append(challenge.model_dump())
                reviews.append(review)
                self.repository.save_debate_turn(
                    run_id=self.run_id, operation_id=operation_id, sequence=sequence,
                    agent=reviewer.name, turn_kind="challenge", payload=review.model_dump(),
                )
                sequence += 1

            review_checkpoints = [
                item for item in self.store.run_checkpoints(self.run_id)
                if item.get("phase") == "design_review"
            ]
            if not review_checkpoints:
                direction = "; ".join(
                    f"{item.topic}: {item.recommendation}" for item in opening.decisions[:3]
                ) or ", ".join(item.name for item in opening.components[:3])
                objections = "; ".join(
                    f"{item['target_topic']}: {item['claim']}" for item in known_challenges[:3]
                ) or "Peer review found no material objection."
                checkpoint = self.store.enqueue_checkpoint(
                    self.run_id, "design_review",
                    "Review the proposed design direction before it is finalized.",
                    f"Proposed direction: {direction}. Peer review: {objections}",
                    [
                        {"label": "A", "summary": "Approve this direction", "consequence": "The coordinator will finalize it after applying valid challenges.", "recommended": True},
                        {"label": "B", "summary": "Revise this direction", "consequence": "Write the scope or direction change you want the coordinator to apply."},
                    ],
                    recommendation="A — Approve if this matches your intent; otherwise choose B and add steering.",
                    dimension="canonical_design_direction", blocking=True,
                )
                snapshot = self.loop_manager.advance(self.run_id, LoopSignal.REVIEW_REQUIRED, {
                    "resume_state": "DIVERGING", "checkpoint_id": checkpoint["id"],
                })
                self._state_event(snapshot)
                await self._resume_waiting_debate_review()
                return
            answered_review = next(
                (item for item in reversed(review_checkpoints) if item.get("status") == "answered"), None,
            )
            if not answered_review:
                raise ValueError("design review checkpoint has not been answered")
            human_steering = str(answered_review.get("custom_answer") or "").strip()
            if not human_steering and answered_review.get("selected_option_id"):
                selected_option = next(
                    (item for item in answered_review.get("options", [])
                     if item.get("id") == answered_review.get("selected_option_id")), None,
                )
                if selected_option:
                    human_steering = f"{selected_option['label']} — {selected_option['summary']}"
            if not human_steering:
                raise ValueError("answered design review contains no durable answer")

            revision_prompt = (
                f"Product goal: {idea}\nCurrent task: {task or idea}\n\nRepository evidence:\n{source_context}\n\n"
                f"Opening proposal:\n{opening.model_dump_json(indent=2)}\n\n"
                f"Peer reviews:\n{json.dumps([item.model_dump() for item in reviews], indent=2)}\n\n"
                f"Human review decision (authoritative steering):\n{human_steering}"
            )
            challenge_ids = {item["id"] for item in known_challenges}
            revision, revision_raw = await self._revision_call(
                coordinator, revision_prompt, challenge_ids,
            )
            disposition_ids = [item.challenge_id for item in revision.dispositions]
            if len(disposition_ids) != len(set(disposition_ids)) or set(disposition_ids) != challenge_ids:
                raise ValueError("coordinator revision must disposition every challenge exactly once")
            self.repository.save_debate_turn(
                run_id=self.run_id, operation_id=operation_id, sequence=sequence,
                agent=coordinator.name, turn_kind="revision", payload=revision.model_dump(),
            )
            proposal_id = self.repository.save_proposal(
                run_id=self.run_id, operation_id=operation_id, expert_id="canonical-revision",
                perspective=coordinator.config.role or coordinator.name, round_number=1,
                proposal=revision.proposal, raw_response=revision_raw,
            )
            summary = self._proposal_summary(revision.proposal)
            self.repository.save_summary(self.run_id, "proposal", proposal_id, "planning-v2", summary)
            self.context_tree.upsert(
                run_id=self.run_id, node_type="proposal", source_type="proposal", source_ref=proposal_id,
                title="Canonical revised design", content=revision.proposal.model_dump_json(indent=2),
                summary=json.dumps(summary, sort_keys=True), authority=5, importance=6,
            )
            changed = [item for item in revision.dispositions if item.status in {"accepted", "merged"}]
            self._emit(Event(EventKind.VERDICT, agent=coordinator.name, data={
                "visibility": "user", "role": "revised position",
                "verdict": f"Design review complete. {len(known_challenges)} material challenge(s) reviewed; "
                           f"{len(changed)} changed the design. The canonical design and plan are updated.",
            }))
            valid = [revision.proposal]
            self.repository.complete_operation(operation_id, output_ref="canonical_revision:1")
        else:
            valid = stored_proposals
        self._state_event(self.loop_manager.advance(self.run_id, LoopSignal.PROPOSALS_READY, {
            "operation_id": operation_id, "accepted": len(valid), "requested": len(selected),
        }))

    async def _resume_waiting_debate_review(self):
        checkpoint = self.store.current_checkpoint(self.run_id)
        if not checkpoint or checkpoint.get("phase") != "design_review":
            raise ValueError("workflow is waiting for design review but no active review checkpoint exists")
        self._state_event(self.repository.get(self.run_id))
        self._answer_event.clear()
        await self._answer_event.wait()
        if not self._running:
            raise asyncio.CancelledError()
        snapshot = self.loop_manager.advance(self.run_id, LoopSignal.ANSWER_RECORDED, {
            "checkpoint_id": checkpoint["id"], "phase": "design_review",
        })
        self._pending_answer = ""
        self._state_event(snapshot)

    async def _run_resolution(self, idea: str) -> None:
        conflicts = self.repository.conflicts(self.run_id)
        for conflict in conflicts:
            if conflict.status != "open":
                continue
            if conflict.materiality == "high":
                await self._request_conflict_decision(conflict)
            elif conflict.materiality == "medium":
                await self._resolve_medium_conflict(conflict, idea)
            else:
                self.repository.resolve_conflict(
                    self.run_id, conflict.id, conflict.options[0], "deterministic-first-option",
                )
        if self.repository.get(self.run_id).state == WorkflowState.RESOLVING:
            self._state_event(self.loop_manager.advance(self.run_id, LoopSignal.CONFLICTS_RESOLVED, {
                "conflicts": len(conflicts),
            }))

    @staticmethod
    def _proposal_summary(proposal: ExpertProposal) -> dict:
        return {
            "components": [item.name for item in proposal.components],
            "decisions": [f"{item.topic}: {item.recommendation}" for item in proposal.decisions],
            "risks": [item.risk for item in proposal.risks],
            "assumptions": proposal.assumptions,
            "unknowns": [item.question for item in proposal.unknowns],
        }

    @staticmethod
    def _proposal_position_signature(proposal: ExpertProposal) -> tuple[tuple[str, str], ...]:
        """Canonical consequential positions used to reject fake debate quorum."""
        normalize = lambda value: " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())
        return tuple(sorted(
            (normalize(item.topic), normalize(item.recommendation)) for item in proposal.decisions
        ))

    async def _resolve_medium_conflict(self, conflict, goal: str):
        resolver = self.agents[0] if self.agents else None
        if resolver is None:
            await self._request_conflict_decision(conflict)
            return
        packet = self.context_tree.retrieve(
            query=(
                f"Resolve only this medium-impact conflict: {conflict.topic}. "
                f"Allowed options: {json.dumps(conflict.options)}"
            ),
            run_id=self.run_id, max_tokens=2000,
        )
        prompt = (
            "Choose exactly one allowed option. Return JSON only as "
            '{"choice":"exact option","rationale":"short reason"}.\n'
            + packet.text
        )
        try:
            raw = await asyncio.to_thread(
                resolver.send, prompt, "Resolve one bounded architecture conflict using supplied evidence.",
                "", context_only=True,
            )
            result = _json_object(raw)
            choice = str(result.get("choice", "")).strip()
            if choice not in conflict.options:
                raise ValueError("resolver chose an option outside the allowed set")
            self.repository.resolve_conflict(self.run_id, conflict.id, choice, f"resolver:{resolver.name}")
        except Exception:
            # An uncertain or invalid model resolution becomes an explicit user
            # decision; it is never converted into an arbitrary first-option win.
            await self._request_conflict_decision(conflict)

    async def _resume_waiting_conflict(self):
        checkpoint = self.store.current_checkpoint(self.run_id)
        if not checkpoint:
            raise ValueError("workflow is waiting for a user decision but no active checkpoint exists")
        conflicts = [item for item in self.repository.conflicts(self.run_id) if item.status == "open"]
        conflict = next((item for item in conflicts if item.topic == checkpoint.get("dimension")), None)
        if not conflict:
            raise ValueError("active checkpoint does not map to an open planning conflict")
        self._pending_conflict_id = conflict.id
        self._state_event(self.repository.get(self.run_id))
        self._answer_event.clear()
        await self._answer_event.wait()
        if not self._running:
            raise asyncio.CancelledError()
        self.repository.resolve_conflict(self.run_id, conflict.id, self._pending_answer, "user")
        snapshot = self.loop_manager.advance(self.run_id, LoopSignal.ANSWER_RECORDED, {
            "checkpoint_id": checkpoint["id"], "conflict_id": conflict.id,
        })
        self._state_event(snapshot)
        self._pending_answer = ""
        self._pending_conflict_id = ""

    async def _request_conflict_decision(self, conflict):
        options = [
            {"label": chr(65 + index), "summary": option, "consequence": "Select this architecture behavior.",
             "recommended": index == 0}
            for index, option in enumerate(conflict.options[:3])
        ]
        checkpoint = self.store.enqueue_checkpoint(
            self.run_id, "resolving", f"Which option should resolve {conflict.topic}?",
            "This conflict materially changes product behavior or its trust boundary.", options,
            dimension=conflict.topic, recommendation=options[0]["label"], blocking=True,
        )
        snapshot = self.loop_manager.advance(self.run_id, LoopSignal.USER_CHOICE_REQUIRED, {
            "resume_state": "RESOLVING", "checkpoint_id": checkpoint["id"], "conflict_id": conflict.id,
        })
        self._pending_conflict_id = conflict.id
        self.ws.write("questions", "# Decision Checkpoint\n\n" + self._checkpoint_projection(checkpoint))
        self._state_event(snapshot)
        self._answer_event.clear()
        await self._answer_event.wait()
        if not self._running:
            raise asyncio.CancelledError()
        self.repository.resolve_conflict(self.run_id, conflict.id, self._pending_answer, "user")
        snapshot = self.loop_manager.advance(self.run_id, LoopSignal.ANSWER_RECORDED, {
            "checkpoint_id": checkpoint["id"], "conflict_id": conflict.id,
        })
        self._state_event(snapshot)
        self._pending_answer = ""
        self._pending_conflict_id = ""

    @staticmethod
    def _checkpoint_projection(checkpoint: dict) -> str:
        lines = [checkpoint.get("question", ""), "", f"Why this matters: {checkpoint.get('rationale', '')}", ""]
        for option in checkpoint.get("options", []):
            lines.append(f"- [{option['label']}] {option['summary']} — {option.get('consequence', '')}")
        return "\n".join(lines).strip()

    @staticmethod
    def _projection_errors(projection) -> list[str]:
        errors = []
        for heading in (
            "Requirements", "Non-Goals", "Assumptions", "Alternatives", "Decisions", "Risks",
            "Acceptance Criteria", "Requirement Traceability", "Implementation Phases", "Discovery Checkpoints",
        ):
            if f"## {heading}" not in projection.plan:
                errors.append(f"PLAN is missing {heading}")
        if "```mermaid" not in projection.design:
            errors.append("DESIGN is missing Mermaid")
        if "## Known Unknowns & Validation Plan" not in projection.design:
            errors.append("DESIGN is missing validation plan")
        return errors

    async def accept_structured_checkpoint_answer(self, message: str, has_more: bool, username: str):
        del has_more, username
        self._pending_answer = message
        self._answer_event.set()

    async def steer(self, message: str, username: str = "human"):
        del username
        if self.repository.get(self.run_id).state == WorkflowState.WAITING_FOR_USER:
            self._pending_answer = message
            self._answer_event.set()

    def pause(self):
        return None

    def resume(self):
        return None

    def stop(self):
        try:
            snapshot = self.repository.get(self.run_id)
            if snapshot.state not in {WorkflowState.COMPLETED, WorkflowState.CANCELLED, WorkflowState.FAILED}:
                self.engine.transition(self.run_id, WorkflowEvent.CANCEL, {"source": "stop"})
        except (KeyError, ValueError):
            pass
        self._running = False
        self._answer_event.set()

    def save_state(self):
        return None

    @staticmethod
    def _public_error(exc: Exception) -> tuple[str, str]:
        return str(exc), "workflow_v2_error"
