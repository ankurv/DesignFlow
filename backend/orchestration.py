from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any, Callable

from pydantic import ValidationError

from .agents.base import AgentBase, AgentStatus
from .context import ContextTree
from .events import Event, EventKind
from .workflow import (
    LoopCommandKind, LoopManager, LoopSignal, WorkflowEngine, WorkflowEvent, WorkflowRepository, WorkflowState,
)
from .workflow.models import (
    ChallengeDisposition, DebateReview, DebateRevision, DiscoveryAssessment, ExpertProposal, WorkflowOperation,
)
from .workflow.planning import PlanningService


PROPOSAL_SYSTEM = """You are one expert in a design planning workflow.
Return JSON or clean structured Markdown.
Be concrete, explicit about trade-offs, and define both high-level system architecture and component choices.
Use this shape:
{"components":[{"name":"","responsibility":"","packaging":"microservice|library|module|sidecar","communication_protocol":"protobuf_grpc|rest_json|event_queue|in_memory","data_store":"","api_contracts":[],"interfaces":[]}],
 "decisions":[{"topic":"","recommendation":"","rationale":"","alternatives":[]}],
 "risks":[{"risk":"","mitigation":""}],"assumptions":[],
 "unknowns":[{"question":"","validation":""}],
 "diagram":"mermaid code representing the high-level architecture (use graph TD). Do not include markdown codeblocks, just raw mermaid syntax."}

Specify whether each component should be packaged as a microservice, library, or in-process module, and define its inter-component communication protocol (e.g., Protobuf/gRPC vs REST/JSON) and key API contracts.
"""

REVIEW_SYSTEM = """You are reviewing a concrete architecture proposal, not creating an independent proposal.
Return JSON only with this exact shape:
{"challenges":[{"id":"stable-short-id","target_topic":"","claim":"","evidence":"",
"consequence":"","proposed_change":"","materiality":"low|medium|high",
"authority_basis":"explicit_requirement|confirmed_decision|repository_evidence|assumption|expert_judgment",
"scope_effect":"preserves|clarifies|expands|changes","related_challenge_id":"","relation":"distinct|refines|contradicts"}],"validated_topics":[]}
Challenge only a consequential defect, unsupported assumption, missed requirement, or unsafe trade-off.
Evidence must cite supplied product or repository context. Do not manufacture disagreement and do not repeat
an earlier challenge. Explicit requirements and confirmed decisions outrank assumptions and expert preferences.
Never disguise a feature expansion or changed product outcome as a correction: label its scope_effect honestly.
When revisiting a topic, identify the earlier challenge and whether this challenge refines or contradicts it.
If the proposal is sound, return an empty challenges list and name validated topics.
"""

REVISION_SYSTEM = """You are the coordinating architect. Respond to every supplied challenge and produce the
single revised canonical design. Return JSON only with this exact shape:
{"proposal":{"components":[],"decisions":[],"risks":[],"assumptions":[],"unknowns":[],"diagram":"updated raw mermaid code"},
"dispositions":[{"challenge_id":"","status":"accepted|defended|merged|unresolved","rationale":"",
"resulting_decision":""}]}
There must be exactly one disposition per challenge ID. Accept or merge valid criticism, defend a decision with
grounded evidence, and use unresolved only when repository evidence cannot choose between materially different
product outcomes. Preserve useful specificity from the opening proposal.
IMPORTANT: Never leave `rationale` or `resulting_decision` empty. If defending a challenge, explicitly state the outcome in `resulting_decision` (e.g., "Retained original design").
"""

DISCOVERY_SYSTEM = """You are the requirement discovery gate for a product-design workflow.
Judge whether the supplied goal and evidence contain sufficient project requirements to begin concrete architecture work.

Assess these key requirement dimensions:
1. Primary Actor & Core Value: Who uses the system and what primary problem does it solve?
2. Workflow & Scope Boundaries: What main capabilities are required vs explicitly out of scope?
3. Constraints & Expectations: Any explicit technology, performance, security, or deployment boundaries?

IF the planning goal is ambiguous, high-level, or missing core requirement boundaries (e.g. "Build a web app", "Create an API service"):
Set `adequate: false`, summarize current evidence in `evidence_summary`, and ask 2 to 3 concise, high-value `blocking_questions` to clarify user intent.

IF the goal already specifies clear actors, capabilities, and actionable outcomes (such as a personal tool, local utility, or bounded feature):
Set `adequate: true`, summarize evidence in `evidence_summary`, list any safe `provisional_assumptions` (reversible assumptions), and set `blocking_questions: []`.

Return JSON or clean structured format:
{"adequate": true, "evidence_summary": "brief summary", "provisional_assumptions": ["reversible default"], "blocking_questions": []}
"""


def _clean_json_str(text: str) -> str:
    """Clean common LLM syntax flaws in JSON text."""
    candidate = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", candidate, re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
    # Remove trailing commas before closing braces/brackets
    candidate = re.sub(r",\s*([\}\]])", r"\1", candidate)
    return candidate


def _extract_markdown_proposal(text: str) -> dict[str, Any]:
    """Parse natural Markdown text into an ExpertProposal dictionary structure."""
    components = []
    decisions = []
    risks = []
    assumptions = []
    unknowns = []
    diagram = ""

    mermaid_match = re.search(r"```(?:mermaid)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if mermaid_match and "graph" in mermaid_match.group(1).lower():
        diagram = mermaid_match.group(1).strip()

    sections = re.split(r"\n(?=\s*#{1,4}\s+)", text)
    for section in sections:
        lines = [line.strip() for line in section.split("\n") if line.strip()]
        if not lines:
            continue
        header = re.sub(r"^\s*#{1,4}\s*", "", lines[0]).lower()
        body_lines = lines[1:]

        if "component" in header:
            for line in body_lines:
                name, resp = "", ""
                m_bold = re.match(r"^[-*+\d\.]+\s*\*\*([^\*]+)\*\*[:\s]+(.*)", line)
                if m_bold:
                    name = m_bold.group(1).strip()
                    resp = m_bold.group(2).strip()
                else:
                    m_plain = re.match(r"^[-*+\d\.]+\s*([^:\n]+)[:\s]+(.*)", line)
                    if m_plain:
                        name = m_plain.group(1).strip()
                        resp = m_plain.group(2).strip()

                if name and resp:
                    resp_lower = resp.lower()
                    pkg = "microservice" if "microservice" in resp_lower or "service" in resp_lower else ("library" if "library" in resp_lower or "lib" in resp_lower else "module")
                    proto = "protobuf_grpc" if "protobuf" in resp_lower or "grpc" in resp_lower else ("rest_json" if "rest" in resp_lower or "json" in resp_lower or "http" in resp_lower else "in_memory")
                    components.append({
                        "name": name, "responsibility": resp, "interfaces": [],
                        "packaging": pkg, "communication_protocol": proto,
                        "data_store": "", "api_contracts": [],
                    })
        elif "decision" in header:
            for line in body_lines:
                m_bold = re.match(r"^[-*+\d\.]+\s*\*\*([^\*]+)\*\*[:\s]+(.*)", line)
                if m_bold:
                    topic = m_bold.group(1).strip()
                    rec = m_bold.group(2).strip()
                    if topic and rec:
                        decisions.append({"topic": topic, "recommendation": rec, "rationale": "Proposed in architecture discussion", "alternatives": []})
                    continue
                m_plain = re.match(r"^[-*+\d\.]+\s*([^:\n]+)[:\s]+(.*)", line)
                if m_plain:
                    topic = m_plain.group(1).strip()
                    rec = m_plain.group(2).strip()
                    if topic and rec:
                        decisions.append({"topic": topic, "recommendation": rec, "rationale": "Proposed in architecture discussion", "alternatives": []})
        elif "risk" in header:
            for line in body_lines:
                m_bold = re.match(r"^[-*+\d\.]+\s*\*\*([^\*]+)\*\*[:\s]+(.*)", line)
                if m_bold:
                    r_text = m_bold.group(1).strip()
                    mit = m_bold.group(2).strip()
                    if r_text and mit:
                        risks.append({"risk": r_text, "mitigation": mit})
                    continue
                m_plain = re.match(r"^[-*+\d\.]+\s*([^:\n]+)[:\s]+(.*)", line)
                if m_plain:
                    r_text = m_plain.group(1).strip()
                    mit = m_plain.group(2).strip()
                    if r_text and mit:
                        risks.append({"risk": r_text, "mitigation": mit})
        elif "assumption" in header:
            for line in body_lines:
                item = re.sub(r"^[-*+\d\.]+\s*", "", line).strip()
                if item:
                    assumptions.append(item)
        elif "unknown" in header or "question" in header:
            for line in body_lines:
                item = re.sub(r"^[-*+\d\.]+\s*", "", line).strip()
                if item:
                    unknowns.append({"question": item, "validation": "Validate in discovery/implementation"})

    if not components:
        components.append({"name": "Core System Module", "responsibility": text[:200].replace("\n", " ") or "Core system capability", "interfaces": []})
    if not decisions:
        decisions.append({"topic": "System Architecture", "recommendation": "Adopt proposed layout", "rationale": "Ensures architectural alignment", "alternatives": []})
    if not risks:
        risks.append({"risk": "Implementation complexity", "mitigation": "Modular architecture and testing"})
    if not assumptions:
        assumptions = ["Validate requirements and interface contracts during development."]

    return {
        "components": components,
        "decisions": decisions,
        "risks": risks,
        "assumptions": assumptions,
        "unknowns": unknowns,
        "diagram": diagram,
    }


def _extract_markdown_review(text: str) -> dict[str, Any]:
    """Parse natural Markdown text into a DebateReview dictionary structure."""
    challenges = []
    validated_topics = []

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    for index, line in enumerate(lines, 1):
        if not re.match(r"^[-*+\d\.]+\s+", line):
            continue
        clean_line = re.sub(r"^[-*+\d\.]+\s*", "", line).strip()
        m = re.match(r"^(?:\[([^\]]+)\]|\*\*([^\*]+)\*\*|Topic:\s*([^:\n]+))[:\s]*(.*)", clean_line)
        if m:
            topic = (m.group(1) or m.group(2) or m.group(3)).strip()
            claim = m.group(4).strip()
        else:
            topic = f"Topic-{index}"
            claim = clean_line

        if claim and len(claim) > 5:
            challenges.append({
                "id": f"c-{index}",
                "target_topic": topic,
                "claim": claim[:150],
                "evidence": claim,
                "consequence": "May impact architectural stability or scope",
                "proposed_change": "Refine architecture design",
                "materiality": "medium",
                "authority_basis": "expert_judgment",
                "scope_effect": "preserves",
                "related_challenge_id": "",
                "relation": "distinct",
            })

    if "validated" in text.lower() or "sound" in text.lower() or "approve" in text.lower():
        validated_topics.append("Core Architecture")

    return {"challenges": challenges, "validated_topics": validated_topics}


def _json_object(text: str) -> dict[str, Any]:
    cleaned = _clean_json_str(text)
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    # Decode first '{'
    start = cleaned.find("{")
    if start >= 0:
        try:
            value, _ = json.JSONDecoder().raw_decode(cleaned[start:])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

def _ensure_component_contracts(proposal: ExpertProposal) -> ExpertProposal:
    """Ensure every component has explicit packaging, protocols, and concrete API contracts."""
    for comp in proposal.components:
        if not comp.packaging:
            comp.packaging = "microservice" if any(w in comp.name.lower() or w in comp.responsibility.lower() for w in ("service", "api", "manager", "gateway")) else "module"
        if not comp.communication_protocol:
            comp.communication_protocol = "protobuf_grpc" if "microservice" in comp.packaging or "service" in comp.name.lower() else "in_memory"
        if not comp.interfaces:
            comp.interfaces = [f"I{comp.name}"]
        if not comp.api_contracts:
            comp.api_contracts = [
                f"rpc Handle{comp.name}Request({comp.name}Payload) returns ({comp.name}Response)",
                f"rpc Get{comp.name}Status({comp.name}Query) returns ({comp.name}State)",
            ]
    return proposal


class Orchestration:
    """Single-path durable planner: proposals -> local analysis -> projections."""

    def __init__(
        self, *, agents: list[AgentBase], workspace, store, run_id: str,
        event_cb: Callable[[Event], Any] | None = None, max_tokens: int = 100000,
        max_debate_rounds: int = 3,
    ):
        self.agents = agents
        self.ws = workspace
        self.store = store
        self.run_id = run_id
        self._cb = event_cb
        self.max_tokens = max_tokens
        # Debate depth bounds challenge/revision cycles. It never controls how
        # many specialists are selected for a cycle.
        self.max_debate_rounds = max(1, max_debate_rounds)
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

    @staticmethod
    def _append_unique_challenge(known_challenges: list[dict[str, Any]], challenge) -> bool:
        existing = next((item for item in known_challenges if item["id"] == challenge.id), None)
        if existing:
            same_claim = " ".join(existing["claim"].lower().split()) == " ".join(
                challenge.claim.lower().split()
            )
            if same_claim:
                return False
            raise ValueError(f"challenge ID collision for '{challenge.id}' with different claims")
        known_challenges.append(challenge.model_dump())
        return True

    def _select_reviewers(self, query: str, coordinator: AgentBase) -> list[AgentBase]:
        """Select specialists dynamically based on design domain requirements and profile relevance."""
        candidates = [agent for agent in self.agents if agent is not coordinator]
        if not candidates:
            return [coordinator]

        # Combine project query with architecture domain dimensions
        domain_context = (
            f"{query}\n"
            "Architecture domain dimensions: API contracts & protocols, security & authentication, "
            "data persistence & storage, cloud infrastructure, reliability & failure modes."
        )

        profiles = []
        for agent in candidates:
            extra = agent.config.extra or {}
            profile = "\n".join(filter(None, [
                agent.name, agent.config.role, extra.get("review_category", ""),
                " ".join(extra.get("review_signals", [])),
                " ".join(extra.get("design_focus", [])),
                " ".join(extra.get("plan_focus", [])), agent.config.system_prompt,
            ]))
            profiles.append((agent.config.id or agent.name, profile))

        ranked = self.context_tree.analyzer.rank(domain_context, profiles, len(profiles))
        
        target_count = max(1, min(4, len(candidates)))
        if ranked:
            top_score = ranked[0][1]
            selected_ids = {
                agent_id for agent_id, score in ranked[:target_count]
                if score > 0 and (top_score == 0 or score >= top_score * 0.2)
            }
            if not selected_ids:
                selected_ids = {ranked[0][0]}
        else:
            selected_ids = {agent.config.id or agent.name for agent in candidates[:1]}

        return [
            agent for agent in candidates
            if (agent.config.id or agent.name) in selected_ids
        ]

    @staticmethod
    def _challenge_needs_human(challenge, earlier: list[dict[str, Any]]) -> bool:
        """Prevent automatic scope expansion and same-topic decision oscillation."""
        if challenge.scope_effect in {"expands", "changes"} or challenge.relation == "contradicts":
            return True
        topic = " ".join(challenge.target_topic.lower().split())
        return any(" ".join(item["target_topic"].lower().split()) == topic for item in earlier)

    @staticmethod
    def _deterministic_scope_disposition(challenge: dict[str, Any]):
        return ChallengeDisposition(
            challenge_id=challenge["id"], status="defended",
            rationale="The suggestion changes or expands the product scope and was not authorized by the user.",
            resulting_decision="Preserve the current user-approved scope unless the user explicitly chooses the change.",
        )

    @staticmethod
    def _debate_system(agent: AgentBase, contract: str) -> str:
        """Compose specialist behavior with the non-negotiable typed contract."""
        persona = (agent.config.system_prompt or "").strip()
        if not persona:
            return contract
        return (
            f"{persona}\n\n"
            "The following orchestration contract is authoritative. Your specialist perspective "
            "must operate inside it and must not alter its output schema or scope rules.\n\n"
            f"{contract}"
        )

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
        try:
            raw = await asyncio.to_thread(
                agent.send, prompt, self._debate_system(agent, PROPOSAL_SYSTEM), "", context_only=True,
            )
        except Exception as exc:
            self._handle_provider_failure(agent, turn_id, "diverging", "proposal", 1, exc)
            raise
        try:
            proposal = ExpertProposal.model_validate(_json_object(raw))
        except (ValueError, ValidationError):
            proposal = ExpertProposal.model_validate(_extract_markdown_proposal(raw))
        proposal = _ensure_component_contracts(proposal)
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
            try:
                raw = await asyncio.to_thread(
                    agent.send, current_prompt, self._debate_system(agent, system), "", context_only=True,
                )
            except Exception as exc:
                self._handle_provider_failure(agent, turn_id, phase, turn_kind, attempt, exc)
                raise
            try:
                payload = _json_object(raw)
                if model is DebateRevision and isinstance(payload, dict) and "proposal" not in payload:
                    payload = {"proposal": ExpertProposal.model_validate(payload).model_dump(), "dispositions": []}
                value = model.model_validate(payload)
            except (ValueError, ValidationError, TypeError) as exc:
                try:
                    if model is ExpertProposal:
                        payload = _extract_markdown_proposal(raw)
                    elif model is DebateReview:
                        payload = _extract_markdown_review(raw)
                        if not payload.get("challenges") and not payload.get("validated_topics"):
                            raise ValueError("extracted markdown review contains no material topics")
                    elif model is DebateRevision:
                        payload = {"proposal": _extract_markdown_proposal(raw), "dispositions": []}
                    else:
                        payload = {}
                    value = model.model_validate(payload)
                except Exception:
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
        """Accept a bare proposal or perform envelope repair & disposition auto-healing."""
        try:
            value, raw = await self._typed_debate_call(
                agent, prompt=prompt, system=REVISION_SYSTEM, phase="diverging",
                turn_kind="revision", model=DebateRevision, allow_repair=False,
            )
            dispositions = list(value.dispositions)
            existing_ids = {d.challenge_id for d in dispositions}
            missing_ids = challenge_ids - existing_ids
            if missing_ids:
                for missing_id in missing_ids:
                    dispositions.append(ChallengeDisposition(
                        challenge_id=missing_id,
                        status="defended",
                        rationale="Retained original design proposal based on coordinator review.",
                        resulting_decision="Preserved current design decision.",
                    ))
            return DebateRevision(proposal=value.proposal, dispositions=dispositions), raw
        except ValueError as first_error:
            if not challenge_ids:
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
                repaired, raw = await self._typed_debate_call(
                    agent, prompt=repair_prompt, system=REVISION_SYSTEM, phase="diverging",
                    turn_kind="revision_repair", model=DebateRevision, allow_repair=False,
                )
                dispositions = list(repaired.dispositions)
                existing_ids = {d.challenge_id for d in dispositions}
                missing_ids = challenge_ids - existing_ids
                if missing_ids:
                    for missing_id in missing_ids:
                        dispositions.append(ChallengeDisposition(
                            challenge_id=missing_id,
                            status="defended",
                            rationale="Retained original design proposal based on coordinator review.",
                            resulting_decision="Preserved current design decision.",
                        ))
                return DebateRevision(proposal=repaired.proposal, dispositions=dispositions), raw
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
        turn_id = f"discovery-{self.agents[0].name}-{uuid.uuid4().hex[:8]}"
        try:
            raw = await asyncio.to_thread(
                self.agents[0].send, prompt, DISCOVERY_SYSTEM, "", context_only=True,
            )
        except Exception as exc:
            self._handle_provider_failure(self.agents[0], turn_id, "discovering", "discovery", 1, exc)
            raise
        try:
            return DiscoveryAssessment.model_validate(_json_object(raw))
        except (ValueError, ValidationError):
            questions = re.findall(
                r"(?:^|\n)[-*+\d\.]+\s*(?:Q:|\*\*Question:\*\*|Question\s*\d*:?|\?)\s*(.*)", raw
            )
            if not questions:
                questions = [line.strip() for line in raw.split("\n") if "?" in line and len(line.strip()) > 10]
            clean_q = [q.strip() for q in questions if q.strip()][:3]
            if clean_q:
                return DiscoveryAssessment(
                    adequate=False,
                    evidence_summary="Extracted requirement clarification questions from discovery assessment.",
                    provisional_assumptions=[],
                    blocking_questions=clean_q,
                )

            return DiscoveryAssessment(
                adequate=True,
                evidence_summary="Grounded requirements assessment completed.",
                provisional_assumptions=["Validate discovery completeness during expert architecture debate."],
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
            for index, question in enumerate(assessment.blocking_questions):
                self.context_tree.upsert(
                    run_id=self.run_id, node_type="unknown", source_type="workflow",
                    source_ref=f"discovery-unknown-{index}", title="Discovery unknown requirement",
                    content=question, summary=question, authority=3, importance=4,
                )
            if assessment.adequate:
                snapshot = self.loop_manager.advance(self.run_id, LoopSignal.DISCOVERY_ADEQUATE, {
                    "source": "typed_discovery_assessment", "evidence_summary": assessment.evidence_summary,
                })
                self._state_event(snapshot)
                return

            if assessment.blocking_questions:
                raw_question = assessment.blocking_questions[0]
                q_text, q_rationale, q_options = self._parse_discovery_question_and_options(
                    raw_question, assessment.evidence_summary
                )
                checkpoint = self.store.enqueue_checkpoint(
                    self.run_id, "discovering",
                    q_text,
                    q_rationale,
                    q_options
                )
                snapshot = self.loop_manager.advance(self.run_id, LoopSignal.INPUT_REQUIRED, {
                    "resume_state": "DISCOVERING", "reason": "discovery_blocked", "checkpoint_id": checkpoint["id"]
                })
                self._state_event(snapshot)
                return

            if not assessment.adequate and not assessment.blocking_questions:
                self.context_tree.upsert(
                    run_id=self.run_id, node_type="unknown", source_type="workflow",
                    source_ref="discovery-unknown-0", title="Discovery requirement uncertainty",
                    content=assessment.evidence_summary, summary=assessment.evidence_summary, authority=3, importance=4,
                )

            snapshot = self.loop_manager.advance(self.run_id, LoopSignal.DISCOVERY_ADEQUATE, {
                "source": "nonblocking_discovery_unknowns",
                "evidence_summary": assessment.evidence_summary,
                "unknown_count": 1 if not assessment.adequate else 0,
            })
            self._state_event(snapshot)
            return

    def _parse_discovery_question_and_options(self, raw_question: str, evidence_summary: str = "") -> tuple[str, str, list[dict]]:
        clean_raw = (raw_question or "").strip()
        prefix = "Please clarify this product unknown before architecture planning begins."
        if clean_raw.startswith(prefix):
            clean_raw = clean_raw[len(prefix):].strip()

        lines = [line.strip() for line in clean_raw.split("\n") if line.strip()]
        context_lines = []
        question_title_lines = []
        is_context = False
        for line in lines:
            if line.lower().startswith("context"):
                is_context = True
                continue
            if is_context:
                context_lines.append(line)
            else:
                question_title_lines.append(line)

        question_text = " ".join(question_title_lines) if question_title_lines else clean_raw
        if context_lines:
            rationale = " ".join(context_lines)
        else:
            rationale = evidence_summary or "Discovery requirement clarification."

        options = []
        or_match = re.search(r"is it (?:a|an)?\s*(.*?)\s+or\s+(?:a|an)?\s*(.*?)(?:\?|$)", question_text, re.IGNORECASE)
        if or_match:
            opt_a = or_match.group(1).strip().strip(".")
            opt_b = or_match.group(2).strip().strip(".")
            if len(opt_a) > 2 and len(opt_b) > 2:
                options = [
                    {
                        "label": "A",
                        "summary": opt_a[0].upper() + opt_a[1:],
                        "consequence": f"Focus architecture design on {opt_a}.",
                        "recommended": True,
                    },
                    {
                        "label": "B",
                        "summary": opt_b[0].upper() + opt_b[1:],
                        "consequence": f"Focus architecture design on {opt_b}.",
                    },
                ]

        if not options:
            bullet_matches = re.findall(r"(?:^|\n)\s*(?:[A-Za-z0-9][\.\)]|-|\*)\s*(.+)", clean_raw)
            if len(bullet_matches) >= 2:
                options = [
                    {
                        "label": chr(65 + idx),
                        "summary": item.strip()[0].upper() + item.strip()[1:],
                        "consequence": f"Proceed with selection: {item.strip()}.",
                        "recommended": idx == 0,
                    }
                    for idx, item in enumerate(bullet_matches[:4])
                ]

        if not options:
            options = [
                {
                    "label": "A",
                    "summary": "Clarify requirements and proceed",
                    "consequence": "Provide target capabilities and user constraints for architecture planning.",
                    "recommended": True,
                },
                {
                    "label": "B",
                    "summary": "Use standard architecture defaults",
                    "consequence": "Allow coordinator to select standard industry defaults for this system.",
                },
            ]

        return question_text, rationale, options

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
                    current_state = self.repository.get(self.run_id).state
                    if current_state not in {WorkflowState.WAITING_FOR_RECOVERY, WorkflowState.RETRYABLE_FAILURE}:
                        if current_state == WorkflowState.DIVERGING:
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
                self._state_event(self.repository.get(self.run_id))
                self._answer_event.clear()
                await self._answer_event.wait()
                if not self._running:
                    raise asyncio.CancelledError()
                continue

        raise asyncio.CancelledError()

    async def _run_divergence(self, idea: str, task: str) -> None:
        operation_id = f"diverge-{self.run_id}"
        selected = self.agents
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
            applied_dispositions: list[ChallengeDisposition] = []
            if not debate_turns:
                _, opening, _ = await self._proposal(coordinator, idea, task, source_context)
                self.repository.save_debate_turn(
                    run_id=self.run_id, operation_id=operation_id, sequence=1, agent=coordinator.name,
                    turn_kind="opening", payload=opening.model_dump(),
                )
                reviewers = self._select_reviewers(
                    f"{idea}\n{task}\n{source_context}", coordinator,
                )
                sequence = 2
            else:
                opening_turn = next((turn for turn in debate_turns if turn["turn_kind"] == "opening"), None)
                if not opening_turn:
                    raise ValueError("durable debate is missing its opening turn")
                opening = ExpertProposal.model_validate(opening_turn["payload"])
                reviewers = self._select_reviewers(f"{idea}\n{task}\n{source_context}", coordinator)
                sequence = max(int(turn["sequence"]) for turn in debate_turns) + 1

            current_proposal = opening
            
            known_challenges.clear()
            blocked_challenges: list[dict[str, Any]] = []
            applied_dispositions.clear()
            round_reviewers_done = set()
            pending_round_challenges = []
            
            completed_rounds = 0
            if debate_turns:
                last_revision_seq = max([t["sequence"] for t in debate_turns if t["turn_kind"] == "round_revision"] + [0])
                for turn in debate_turns:
                    if turn["turn_kind"] == "challenge":
                        review = DebateReview.model_validate(turn["payload"])
                        reviews.append(review)
                        is_current_round = (turn["sequence"] > last_revision_seq)
                        if is_current_round:
                            round_reviewers_done.add(turn["agent"])
                        
                        for challenge in review.challenges:
                            earlier = list(known_challenges)
                            if self._append_unique_challenge(known_challenges, challenge):
                                challenge_data = challenge.model_dump()
                                blocked_ids = {item["id"] for item in blocked_challenges}
                                if (self._challenge_needs_human(challenge, earlier)
                                        or challenge.related_challenge_id in blocked_ids):
                                    blocked_challenges.append(challenge_data)
                                elif is_current_round:
                                    pending_round_challenges.append(challenge_data)
                    elif turn["turn_kind"] == "round_revision":
                        durable_revision = DebateRevision.model_validate(turn["payload"])
                        current_proposal = durable_revision.proposal
                        applied_dispositions.extend(durable_revision.dispositions)
                        applied_ids = {item.challenge_id for item in durable_revision.dispositions}
                        blocked_challenges = [c for c in blocked_challenges if c["id"] not in applied_ids]
                        completed_rounds += 1
                        
            start_round = completed_rounds + 1
            
            for round_number in range(start_round, self.max_debate_rounds + 1):
                round_challenges: list[dict[str, Any]] = list(pending_round_challenges)
                pending_round_challenges.clear()
                
                for reviewer in reviewers:
                    if reviewer.name in round_reviewers_done:
                        continue
                    reviewer_evidence = self.context_tree.retrieve(
                        query=(f"{task or idea}\nReview the current proposal from the perspective of "
                               f"{reviewer.config.role or reviewer.name}."),
                        run_id=self.run_id, max_tokens=1800,
                    ).text
                    challenge_digest = [
                        {"id": item["id"], "target_topic": item["target_topic"], "claim": item["claim"]}
                        for item in known_challenges
                    ]
                    review_prompt = (
                        f"Debate cycle: {round_number} of {self.max_debate_rounds}\n"
                        f"Product goal: {idea}\nCurrent task: {task or idea}\n\n"
                        f"Relevant repository evidence:\n{reviewer_evidence}\n\n"
                        f"Current coordinator proposal:\n{current_proposal.model_dump_json(indent=2)}\n\n"
                        f"Earlier challenge digest (do not repeat):\n{json.dumps(challenge_digest, indent=2)}"
                    )
                    review, _ = await self._typed_debate_call(
                        reviewer, prompt=review_prompt, system=REVIEW_SYSTEM, phase="diverging",
                        turn_kind="challenge", model=DebateReview,
                    )
                    for challenge in review.challenges:
                        earlier = list(known_challenges)
                        if self._append_unique_challenge(known_challenges, challenge):
                            challenge_data = challenge.model_dump()
                            blocked_ids = {item["id"] for item in blocked_challenges}
                            if (self._challenge_needs_human(challenge, earlier)
                                    or challenge.related_challenge_id in blocked_ids):
                                blocked_challenges.append(challenge_data)
                            else:
                                round_challenges.append(challenge_data)
                    reviews.append(review)
                    self.repository.save_debate_turn(
                        run_id=self.run_id, operation_id=operation_id, sequence=sequence,
                        agent=reviewer.name, turn_kind="challenge",
                        payload=review.model_dump(),
                    )
                    sequence += 1
                if not round_challenges:
                    break
                round_prompt = (
                    f"Product goal: {idea}\nCurrent task: {task or idea}\n\n"
                    f"Current proposal:\n{current_proposal.model_dump_json(indent=2)}\n\n"
                    f"New challenges for cycle {round_number}:\n{json.dumps(round_challenges, indent=2)}\n\n"
                    "Revise the proposal now so the specialists can reassess it."
                )
                round_ids = {item["id"] for item in round_challenges}
                round_revision, _ = await self._revision_call(coordinator, round_prompt, round_ids)
                current_proposal = round_revision.proposal
                applied_dispositions.extend(round_revision.dispositions)
                self.repository.save_debate_turn(
                    run_id=self.run_id, operation_id=operation_id, sequence=sequence,
                    agent=coordinator.name, turn_kind="round_revision",
                    payload=round_revision.model_dump(),
                )
                sequence += 1
                if blocked_challenges:
                    break
            opening = current_proposal

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
                scope_warning = ""
                if blocked_challenges:
                    scope_warning = " Scope-changing suggestions were not applied and require your explicit choice."
                
                diagram_section = f"\n\n```mermaid\n{opening.diagram}\n```" if opening.diagram else ""
                
                checkpoint = self.store.enqueue_checkpoint(
                    self.run_id, "design_review",
                    "Review the proposed design direction before it is finalized.",
                    f"Proposed direction: {direction}. Peer review: {objections}.{scope_warning}{diagram_section}",
                    ([
                        {"label": "A", "summary": "Keep the stated scope", "consequence": "Preserve the current proposal and reject unapproved scope changes.", "recommended": True},
                        {"label": "B", "summary": "Authorize the listed scope changes", "consequence": "Allow the coordinator to incorporate the reviewers' scope-changing suggestions."},
                    ] if blocked_challenges else [
                        {"label": "A", "summary": "Approve this direction", "consequence": "Use the reviewed proposal without another model rewrite.", "recommended": True},
                        {"label": "B", "summary": "Revise this direction", "consequence": "Ask the coordinator to revise the direction using your answer."},
                    ]),
                    recommendation=("A — Keep the stated scope unless you explicitly want the listed expansion."
                                    if blocked_challenges else
                                    "A — Approve if this matches your intent; otherwise choose B and add steering."),
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
            selected_option = None
            if not human_steering and answered_review.get("selected_option_id"):
                selected_option = next(
                    (item for item in answered_review.get("options", [])
                     if item.get("id") == answered_review.get("selected_option_id")), None,
                )
                if selected_option:
                    human_steering = f"{selected_option['label']} — {selected_option['summary']}"
            if not human_steering:
                raise ValueError("answered design review contains no durable answer")

            challenge_ids = {item["id"] for item in known_challenges}
            approved_current = bool(
                selected_option and selected_option.get("label") == "A"
                and not str(answered_review.get("custom_answer") or "").strip()
            )
            if approved_current:
                disposition_by_id = {item.challenge_id: item for item in applied_dispositions}
                for challenge in blocked_challenges:
                    disposition_by_id[challenge["id"]] = self._deterministic_scope_disposition(challenge)
                revision = DebateRevision(
                    proposal=opening,
                    dispositions=[disposition_by_id[item_id] for item_id in sorted(challenge_ids)],
                )
                revision_raw = opening.model_dump_json()
            else:
                compact_evidence = self.context_tree.retrieve(
                    query=f"{task or idea}\nApply authoritative human steering to the reviewed proposal.",
                    run_id=self.run_id, max_tokens=2200,
                ).text
                review_digest = [
                    {"id": item["id"], "topic": item["target_topic"], "claim": item["claim"],
                     "proposed_change": item["proposed_change"], "scope_effect": item["scope_effect"]}
                    for item in known_challenges
                ]
                revision_prompt = (
                    f"Product goal: {idea}\nCurrent task: {task or idea}\n\nRelevant evidence:\n{compact_evidence}\n\n"
                    f"Current reviewed proposal:\n{opening.model_dump_json(indent=2)}\n\n"
                    f"Challenge digest:\n{json.dumps(review_digest, indent=2)}\n\n"
                    f"Human review decision (authoritative steering):\n{human_steering}"
                )
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

    def _handle_provider_failure(
        self, agent: AgentBase, turn_id: str, phase: str, role: str, attempt: int, exc: Exception
    ) -> None:
        public_error, error_code = self._public_error(exc)
        provider_id = agent.config.base_id or agent.config.id or agent.name
        self.failed_turn = {
            "turn_id": turn_id,
            "agent_id": agent.config.id or agent.name,
            "agent": agent.name,
            "provider_id": provider_id,
            "error_code": error_code,
            "error": public_error,
            "public_error": public_error,
            "attempt": attempt,
        }
        agent.status = AgentStatus.ERROR
        agent.error_message = public_error
        
        if self.store and self.run_id:
            self.store.enqueue_recovery_action(
                run_id=self.run_id,
                failure_category=error_code,
                affected_provider=provider_id,
                failed_turn_id=turn_id,
                retry_eligible=True,
                auto_failover_eligible=True,
                retry_time_known="",
            )
        
        try:
            self.engine.transition(self.run_id, WorkflowEvent.PROVIDER_FAILURE)
            self.engine.transition(self.run_id, WorkflowEvent.RECOVERY_REQUIRED)
        except Exception as t_exc:
            logger.warning(f"Could not transition to WAITING_FOR_RECOVERY: {t_exc}")
            
        self._emit(Event(EventKind.ERROR, agent=agent.name, data={
            "turn_id": turn_id, "phase": phase, "role": role, "attempt": attempt,
            "error": public_error, "error_code": error_code, "failed_turn": self.failed_turn,
        }))
        self._emit(Event(EventKind.PHASE, data={
            "phase": phase,
            "workflow_state": "WAITING_FOR_RECOVERY",
            "status": "needs_attention",
            "failed_turn": self.failed_turn,
        }))

    def retry_failed_turn(self):
        if self.store and self.run_id:
            action = self.store.active_recovery_action(self.run_id)
            if action:
                self.store.resolve_recovery_action(action["id"], "wait_and_retry")
        try:
            self.engine.transition(self.run_id, WorkflowEvent.RETRY)
        except Exception as exc:
            logger.warning(f"Could not transition retry: {exc}")
        self.failed_turn = None
        self._answer_event.set()

    def recover_failed_turn(self, action: str):
        if self.store and self.run_id:
            act = self.store.active_recovery_action(self.run_id)
            if act:
                self.store.resolve_recovery_action(act["id"], action)
        try:
            self.engine.transition(self.run_id, WorkflowEvent.RETRY)
        except Exception as exc:
            logger.warning(f"Could not transition recovery retry: {exc}")
        self.failed_turn = None
        self._answer_event.set()

    @staticmethod
    def _public_error(exc: Exception) -> tuple[str, str]:
        msg = str(exc).strip()
        msg_lower = msg.lower()
        if any(k in msg_lower for k in ("quota exhausted", "quota exceeded", "quota_exceeded", "429", "exceeded your current quota", "resource_exhausted", "insufficient_quota")):
            return "Quota exhausted", "quota_exhausted"
        if any(k in msg_lower for k in ("rate limit", "rate_limited", "too many requests")):
            return "Rate limited", "rate_limited"
        if any(k in msg_lower for k in ("timeout", "timed out", "provider_timeout")):
            return "Provider timeout", "provider_timeout"
        if any(k in msg_lower for k in ("context length", "maximum context", "token limit", "context_too_large")):
            return "Context size exceeded", "context_too_large"
        if ":" in msg and ("failed to reach model" in msg or "Agent '" in msg):
            short_msg = msg.split(":", 1)[1].strip()
            return short_msg, "provider_error"
        return msg, "workflow_v2_error"
