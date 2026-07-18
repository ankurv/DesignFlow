"""
Orchestrator — runs debate + build phases.
All state changes emit events via an async queue so the UI gets live updates.
Human steering: pause, inject a message into the debate, swap agent roles.
"""

from __future__ import annotations
import asyncio
from difflib import SequenceMatcher, get_close_matches
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional
from backend.mcp_client import MCPManager

from .agents.base import AgentBase, Message, Usage
from .errors import PlanningQualityError, classify_provider_error
from .run_contracts import RunContract, RunKind, classify_run_contract
from .prompt_catalog import prompt_catalog
from .workspace.workspace import Workspace

logger = logging.getLogger(__name__)


# ─── Events ──────────────────────────────────────────────────────────────────

class OrchestratorPhase(str, Enum):
    DISCOVERY = "discovery"
    DRAFTING = "drafting"
    PEER_REVIEW = "peer_review"
    REFINEMENT = "refinement"
    APPROVAL = "approval"
    COMPLETE = "complete"

class EventKind(str, Enum):
    PHASE       = "phase"        # phase change
    TURN_START  = "turn_start"   # agent about to speak
    TURN_END    = "turn_end"     # agent finished, includes response
    VERDICT     = "verdict"      # reviewer/tester verdict
    FILE_WRITE  = "file_write"   # workspace file updated
    STEER       = "steer"        # human injected a message
    DONE        = "done"         # entire run finished
    ERROR       = "error"        # something failed
    RETRY       = "retry"        # agent is waiting for a usage limit reset


@dataclass
class Event:
    kind: EventKind
    agent: str = ""
    data: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "agent": self.agent,
            "data": self.data,
            "timestamp": self.timestamp,
        }


# ─── Prompts ─────────────────────────────────────────────────────────────────

ROLE_NEEDS = {
    "architect_alpha": ["design", "plan", "decisions", "questions"],
    "architect_beta": ["design", "plan", "decisions", "questions"],
    "ux_simplifier": ["design", "plan", "decisions", "questions"],
    "ui_designer": ["design", "plan", "decisions", "questions"],
    "workflow_designer": ["design", "plan", "decisions", "questions"],
    "product_manager": ["design", "plan", "decisions", "questions"],
    "product_strategist": ["design", "plan", "decisions", "questions"],
    "data_architect": ["design", "plan", "decisions", "questions"],
    "security_auditor": ["design", "plan", "decisions", "questions"],
    "api_designer": ["design", "plan", "decisions", "questions"],
    "researcher": ["design", "plan", "decisions", "questions", "src_index"],
    "red_team": ["design", "plan", "decisions", "questions"],
    "cloud_architect": ["design", "plan", "decisions", "questions"],
    "devops_engineer": ["design", "plan", "decisions", "questions"],
    "sales_alpha": ["design", "plan", "decisions"],
    "sales_beta": ["design", "plan", "decisions"],
    "marketing_alpha": ["design", "plan", "decisions"],
    "marketing_beta": ["design", "plan", "decisions"],
}

# Application-owned prompt files are validated at import. These assignments
# preserve the public constants used by tests and integrations.
COORDINATOR_SYSTEM = prompt_catalog.render(
    "coordinator_system", max_debate_rounds="{max_debate_rounds}", agents_list="{agents_list}",
)
SYNTHESIS_SYSTEM = prompt_catalog.text("synthesis_system")


# ─── Orchestrator ─────────────────────────────────────────────────────────────

class Orchestrator:
    def __init__(
        self,
        agents: list[AgentBase],
        workspace: Workspace,
        event_cb: Optional[Callable[[Event], Any]] = None,
        max_debate_rounds: int = 6,
        max_tokens: int = 100000,
        max_build_iterations: int = 5,
        require_approval: bool = True,
        mode: str = "all",
        restore: bool = False,
        allow_artifact_changes_on_restore: bool = False,
        store: Optional[Any] = None,
        run_id: str = "",
    ):
        self.agents = agents
        self.ws = workspace
        self._personas, self._signals, self._keywords, self._allowed_mcp_servers = self.ws.parse_personas()
        self.store = store
        self.run_id = run_id
        if self.store and hasattr(self.store, "set_planning_run"):
            self.store.set_planning_run(run_id)
        self._cb = event_cb
        self.max_debate_rounds = max_debate_rounds
        # Debate depth also bounds requirements discovery, but discovery stays
        # much smaller than the debate itself. Low=1, standard=2, deep=3.
        self.max_discovery_questions = min(3, max(1, (max_debate_rounds + 1) // 2))
        self.max_tokens = max_tokens
        self.max_build_iterations = max_build_iterations
        self.require_approval = require_approval
        self.mode = mode
        self.restore = restore
        self.allow_artifact_changes_on_restore = allow_artifact_changes_on_restore

        self.mcp_manager = None
        self.mcp_tools = []

        # Steering controls
        self._paused = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # not paused initially
        self._steer_queue: asyncio.Queue[str] = asyncio.Queue()
        self._running = False
        self._turn_sequence = 0
        self._turn_attempts: dict[str, int] = {}
        self._failed_turn: dict[str, Any] | None = None
        self._recovery_event = asyncio.Event()
        self.run_token_total = 0
        self.phase_usage: dict[str, dict[str, float]] = {}
        self._budget_exhausted = False
        self._coordinator_name = ""
        self._context_invocations: dict[str, int] = {}
        self._provider_turn_peak: dict[str, int] = (
            store.load_provider_turn_peaks() if store and hasattr(store, "load_provider_turn_peaks") else {}
        )
        self._context_full_refresh_gap = 8
        self._context_full_refresh_every = 4
        self._consulted_specialists: set[str] = set()
        self._user_checkpoint_count = 0
        self.phase = OrchestratorPhase.DISCOVERY
        self.peer_review_index = 0
        self.post_approval_phase = None
        self._selected_peer_names: list[str] = []
        self._refinement_attempts = 0
        self._deterministic_feedback = ""
        self._checkpoint_validation_errors: list[str] = []
        self._pending_user_input = ""
        self.idea = ""
        self.task = ""
        self._state_loaded = False
        self._checkpoint_has_more = False
        self._discovery_questions_asked = 0
        self._discovery_question_keys: set[str] = set()
        self._adaptive_discovery_unavailable = False
        self._discovery_failed_providers: set[str] = set()
        self._pending_discovery_checkpoint: dict | None = None
        self.completion_kind = "planning_complete"
        self.completion_files: list[str] = []
        self.contract: RunContract | None = None

    # ── Public controls ───────────────────────────────────────────────────────

    def pause(self):
        self._paused = True
        self._pause_event.clear()

    def resume(self):
        self._paused = False
        self._pause_event.set()

    async def steer(self, message: str, username: str = "human"):
        """Inject a human message that all agents will see next turn."""
        # File-backed behavior remains only for embedded/legacy callers that
        # have no ProjectStore. Server runtimes always use the structured
        # checkpoint answer endpoint and never parse QUESTIONS.md here.
        pending_question = self.ws.read("questions").strip() if not self.store else ""
        event_kind = "user_decision" if pending_question and pending_question != "(empty)" else "user_steering"
        self.ws.add_context_event(event_kind, message, self.phase.value, "user")
        if pending_question and pending_question != "(empty)":
            self._user_checkpoint_count += 1
            self._checkpoint_has_more = self.ws.record_checkpoint_answer(pending_question, message)
        await self._steer_queue.put(message)
        if not self.store and (not pending_question or pending_question == "(empty)"):
            self.ws.clear_questions()
        self.ws.reset_context_tracking()
        self._context_invocations.clear()
        self._emit(Event(EventKind.STEER, agent=username or "human", data={"message": message}))

    async def accept_structured_checkpoint_answer(
        self, message: str, has_more: bool, username: str = "human"
    ) -> None:
        """Inject a transactionally recorded checkpoint answer without parsing Markdown state."""
        self.ws.add_context_event("user_decision", message, self.phase.value, "user")
        if self.store and hasattr(self.store, "replace_planning_document"):
            # The endpoint has already appended the human decision to the
            # staged projection. Import that authoritative user mutation before
            # the next discovery query is assembled.
            self.store.replace_planning_document("decisions", self.ws.read("decisions"), username)
        await self._steer_queue.put(message)
        self._checkpoint_has_more = has_more
        self.ws.reset_context_tracking()
        self._context_invocations.clear()
        self._emit(Event(
            EventKind.STEER, agent=username or "human",
            data={"message": message, "checkpoint": True},
        ))

    def stop(self):
        self._running = False
        self._pause_event.set()
        self._recovery_event.set()
        if self.mcp_manager:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.mcp_manager.stop())
            except RuntimeError:
                asyncio.run(self.mcp_manager.stop())

    @property
    def failed_turn(self) -> dict[str, Any] | None:
        if not self._failed_turn or self._failed_turn.get("recovery_started"):
            return None
        return {key: value for key, value in self._failed_turn.items() if key != "prompt"}

    def retry_failed_turn(self):
        if not self._failed_turn:
            raise ValueError("There is no failed turn to retry")
        self._paused = False
        self._pause_event.set()
        self._recovery_event.set()

    def recover_failed_turn(self, action: str):
        """Resume the exact failed turn after the caller selects a recovery policy."""
        if not self._failed_turn:
            raise ValueError("There is no failed turn to recover")
        if action not in {"auto_failover", "wait_and_retry"}:
            raise ValueError("Unknown provider recovery action")
        self._failed_turn["recovery_action"] = action
        self._failed_turn["recovery_started"] = True
        self.retry_failed_turn()

    # ── Main entry ────────────────────────────────────────────────────────────

    @staticmethod
    def _fuzzy_intent(text: str) -> str:
        """Classify only a few low-risk commands; ambiguity falls through to AI."""
        normalized = re.sub(r"[^a-z0-9@ ]+", " ", (text or "").lower())
        normalized = " ".join(normalized.split())
        if not normalized or len(normalized.split()) > 7:
            return ""
        intents = {
            "help": ("help", "show help", "what can i do", "how do i use this"),
            "status": ("status", "show status", "project status", "run status", "where are we"),
            "agents": ("list agents", "show agents", "who are the agents", "what agents are available"),
        }
        best_intent, best_score = "", 0.0
        for intent, examples in intents.items():
            score = max(SequenceMatcher(None, normalized, example).ratio() for example in examples)
            if score > best_score:
                best_intent, best_score = intent, score
        return best_intent if best_score >= 0.84 else ""

    def _local_command_response(self, idea: str) -> str:
        intent = self._fuzzy_intent(idea)
        if intent == "help":
            return "Describe a product goal to start a design debate. You can also use @AgentName, ask for status, or list agents."
        if intent == "agents":
            return "Available agents: " + ", ".join(f"{agent.name} ({agent.config.role or 'generalist'})" for agent in self.agents) + "."
        if intent == "status":
            snapshot = self.ws.snapshot()
            ready = [name.upper() for name in ("design", "plan", "decisions", "questions") if str(snapshot.get(name, "")).strip()]
            artifacts = ", ".join(ready) if ready else "no planning artifacts yet"
            return f"Project status: {artifacts}. Run usage is {self.run_token_total:,} of {self.max_tokens:,} configured tokens."
        return ""

    @staticmethod
    def _is_targeted_artifact_update(text: str) -> bool:
        """Compatibility wrapper around the authoritative run classifier."""
        return classify_run_contract(text).kind == RunKind.ARTIFACT_EDIT

    @staticmethod
    def _effective_request(idea: str, task: str) -> str:
        """Use the latest instruction for routing while retaining the saved product goal."""
        return (task or "").strip() or (idea or "").strip()

    @staticmethod
    def _should_run_team_workflow(text: str, mode: str) -> bool:
        """Compatibility wrapper around the authoritative run classifier."""
        return classify_run_contract(text, mode).uses_team_workflow

    async def run(self, idea: str, task: str = ""):
        self._running = True
        self.idea = idea
        self.task = task.strip()
        request_text = self._effective_request(idea, self.task)
        self.contract = classify_run_contract(
            request_text, self.mode, local_intent=self._fuzzy_intent(request_text),
        )
        if self.store and self.run_id:
            self.store.update_run_contract(self.run_id, self.contract.kind.value)
        if self.task and self._is_explicit_user_correction(self.task):
            self.ws.add_context_event("user_decision", self.task, "discovery", "user")
            self.ws.record_user_directive(self.task)
        if self.restore and not self._state_loaded:
            self._state_loaded = self.load_state()
            if not self._state_loaded:
                # A new goal must not inherit an unresolved checkpoint from an
                # older or legacy run in the same project.
                self.ws.clear_questions()

        local_response = self._local_command_response(request_text)
        if self.contract.kind == RunKind.STATUS_QUERY and local_response:
            self.completion_kind = RunKind.STATUS_QUERY.value
            self._emit(Event(EventKind.TURN_END, agent="DesignFlow", data={
                "turn_id": "local-command", "attempt": 1, "step": 0,
                "response": local_response, "actor_role": "system",
                "is_coordinator": False, "usage": Usage().to_dict(),
                "cost_usd": 0.0, "pricing_known": True,
            }))
            self._running = False
            return self.ws.snapshot()

        # Load and start MCP servers if present
        if self.store:
            mcp_configs = self.store.get_mcp_servers()
            if mcp_configs:
                self.mcp_manager = MCPManager(mcp_configs)
                await self.mcp_manager.start()
                self.mcp_tools = await self.mcp_manager.list_tools()

        # Only initialize workspace files if design doesn't exist yet or is empty.
        # We never overwrite existing design/plan docs, so the agents can preserve context across server restarts.
        design_file = self.ws._file("design")
        if not design_file.exists() or design_file.stat().st_size == 0:
            self.ws.init(idea)
        else:
            self.ws.align_generated_goal_header(idea)
        self._seed_planning_store()

        n = len(self.agents)
        if n == 0:
            return self.ws.snapshot()

        # Python owns routing. The strongest available model is used only for
        # quality-critical synthesis, regardless of which agent is marked manager.
        coordinator = max(self.agents, key=self._synthesis_score)
        self._coordinator_name = coordinator.name
        coordinator.config.max_history_turns = min(coordinator.config.max_history_turns, 6)

        if self.contract.kind == RunKind.INTENT_ROUTING:
            self.contract = await self._resolve_request_contract(coordinator, request_text)
            if self.store and self.run_id:
                self.store.update_run_contract(self.run_id, self.contract.kind.value)

        # Parse explicit @mentions first
        target_agent = None
        prompt_text = request_text
        match = re.search(r'@(\w+)', request_text)
        if match:
            mention = match.group(1).lower()
            names = {agent.name.lower(): agent for agent in self.agents}
            matched_name = mention if mention in names else next(iter(get_close_matches(mention, names, n=1, cutoff=0.82)), "")
            if matched_name:
                target_agent = names[matched_name]
                prompt_text = re.sub(rf'@{match.group(1)}\s*', '', request_text, flags=re.IGNORECASE).strip()

        # An explicit agent mention is an execution override, but it does not
        # weaken an artifact contract: the named agent must still write it.
        if target_agent is not None and self.contract.kind == RunKind.PLANNING_WORKFLOW:
            self.contract = RunContract(RunKind.CHAT, prompt_text)
            if self.store and self.run_id:
                self.store.update_run_contract(self.run_id, self.contract.kind.value)
        team_workflow = self.contract.uses_team_workflow

        # Basic questions use one capable agent. Team planning is reserved for
        # explicit design/build/debate work, and @mentions always stay direct.
        is_direct = target_agent is not None or not team_workflow

        if is_direct:
            if not target_agent:
                target_agent = coordinator or self.agents[0]

            self._emit(Event(EventKind.PHASE, data={
                "phase": "direct_chat", "status": f"Direct chat with {target_agent.name}"
            }))

            turn_context = {"step": 1, "phase": "direct_chat", "standing_role": target_agent.config.role}
            turn_id = self._begin_turn(target_agent, turn_context)

            context = self.ws.scoped_context(["design", "plan", "decisions"])
            if len(context) > 12000:
                context = context[:12000].rstrip() + "\n\n[context truncated]"
            artifact_update = self.contract.requires_artifact_change
            if artifact_update:
                mermaid_update = self.contract.requires_diagram_delta
                before_mermaid = len(re.findall(r"^```mermaid\s*$", self.ws.read("design"), re.MULTILINE | re.IGNORECASE))
                output_instruction = (
                    "Return only a `## DESIGN_APPEND` section containing a titled architecture-diagram section "
                    "and the fenced Mermaid block. Do not repeat the existing document."
                    if mermaid_update else
                    "Return the complete updated document under the matching `## DESIGN_UPDATE`, "
                    "`## PLAN_UPDATE`, or `## DECISIONS_UPDATE` header."
                )
                prompt = prompt_catalog.render("artifact_edit", request=prompt_text, output_instruction=output_instruction, context=context)
            else:
                prompt = prompt_catalog.render("chat", request=prompt_text, context=context)

            response = await self._send_agent(target_agent, prompt, turn_id, turn_context)
            self._record_turn_usage(target_agent, "direct")
            if artifact_update:
                written_files = self._apply_coordinator_agent_response(target_agent.name, response)
                missing_files = set(self.contract.target_artifacts) - written_files
                if missing_files:
                    raise RuntimeError(
                        "Targeted artifact edit produced no applicable update for required file(s): "
                        + ", ".join(sorted(missing_files))
                    )
                if mermaid_update:
                    after_mermaid = len(re.findall(
                        r"^```mermaid\s*$", self.ws.read("design"), re.MULTILINE | re.IGNORECASE,
                    ))
                    if after_mermaid <= before_mermaid:
                        raise RuntimeError(
                            "Diagram edit completed without adding a valid fenced Mermaid diagram to DESIGN.md."
                        )
                self.completion_kind = RunKind.ARTIFACT_EDIT.value
                self.completion_files = sorted(written_files)
            else:
                self.completion_kind = RunKind.CHAT.value
            self.ws.append("logbook", response, target_agent.name, "Turn completed")

            self._emit(Event(EventKind.TURN_END, agent=target_agent.name, data={
                "turn_id": turn_id, "attempt": self._turn_attempts[turn_id],
                "step": 1, "response": response,
                **self._event_actor_meta(target_agent),
                **self._usage_event(target_agent),
            }))
            if await self._enforce_token_budget("direct_chat", {"step": 1, "agent": target_agent.name}):
                return self.ws.snapshot()

            self._running = False
            return self.ws.snapshot()

        if not coordinator:
            coordinator = self.agents[0] if self.agents else None

        if coordinator:
            self._coordinator_name = coordinator.name
            await self._run_state_machine(coordinator)

        return self.ws.snapshot()

    async def _resolve_request_contract(self, coordinator: AgentBase, request_text: str) -> RunContract:
        """Resolve ambiguous language from request meaning plus persisted project state."""
        validation_errors = self.ws.validate_planning_artifacts()
        artifact_state = {
            name: self.ws.read(name) not in {"", "(empty)"}
            for name in ("design", "plan", "decisions")
        }
        prompt = prompt_catalog.render("intent_router_task", request=request_text, artifact_state=json.dumps(artifact_state, sort_keys=True), validation_errors=json.dumps(validation_errors))
        try:
            turn_context = {"step": 0, "phase": "intent_routing", "standing_role": coordinator.config.role}
            turn_id = self._begin_turn(coordinator, turn_context)
            try:
                response = await self._send_agent(
                    coordinator, prompt, turn_id, turn_context,
                    system_override=prompt_catalog.text("intent_router_system"),
                )
            finally:
                # Routing is control-plane work. Its JSON must not contaminate
                # the coordinator's later design conversation or remote session.
                coordinator.reset_conversation()
            self._record_turn_usage(coordinator, "intent_routing")
            match = re.search(r"\{[\s\S]*\}", response)
            payload = json.loads(match.group(0) if match else response)
            kind = RunKind(str(payload.get("kind", "")))
            if kind not in {RunKind.CHAT, RunKind.ARTIFACT_EDIT, RunKind.PLANNING_WORKFLOW}:
                raise ValueError("unsupported routed kind")
            allowed = {"DESIGN.md", "PLAN.md", "DECISIONS.md"}
            targets = tuple(item for item in payload.get("target_artifacts", []) if item in allowed)
            if kind == RunKind.ARTIFACT_EDIT and not targets:
                raise ValueError("artifact edit requires explicit targets")
            return RunContract(kind, request_text, target_artifacts=targets)
        except Exception as exc:
            logger.warning("Intent router failed; applying state-safe fallback: %s", exc)
            # An ambiguous mutation must never silently degrade to prose. The
            # planning workflow is safe because it validates before promotion.
            return RunContract(RunKind.PLANNING_WORKFLOW, request_text)

    # ── State Machine ─────────────────────────────────────────────────────────

    def _select_peer_review_agents(self) -> list[AgentBase]:
        """Choose a small, relevant, domain-diverse review panel without an LLM call."""
        candidates = [agent for agent in self.agents if agent.name != self._coordinator_name]
        if not candidates:
            return []
        words = set(re.findall(r"[a-z0-9]+", f"{self.idea} {self.task} {self.ws.brief()}".lower()))
        task_words = set(re.findall(r"[a-z0-9]+", self.task.lower()))
        scored: list[tuple[int, str, AgentBase]] = []
        for agent in candidates:
            identity = f"{agent.name} {agent.config.role}".lower()
            score = sum(3 for word in words if len(word) > 3 and word in identity)
            domains = []
            for domain, (signals, names) in self._signals.items():
                if agent.name.lower() in names and words.intersection(signals):
                    score += 6 + len(words.intersection(signals))
                    if task_words.intersection(signals):
                        score += 8 + len(task_words.intersection(signals))
                    domains.append(domain)
            meaningful_source = any(
                name != "DESIGNFLOW.md" and Path(name).suffix.lower() in {".py", ".js", ".ts", ".go", ".java", ".rs", ".rb", ".cs"}
                for name in self.ws.read_src()
            )
            if agent.name.lower() == "researcher" and meaningful_source:
                score += 5
                domains.append("research")
            if agent.name.lower().startswith("architect_"):
                score += 2
                domains.append("architecture")
            if agent.name.lower() == "product_manager":
                score += 1
                domains.append("product")
            scored.append((score, domains[0] if domains else agent.name.lower(), agent))

        # A planning "debate" must contain an adversarial architecture turn.
        # Domain diversity used to let product/API/UI reviewers consume all
        # three slots, silently excluding Architect Beta even when Architect
        # Alpha synthesized the draft. Reserve the first slot for an available
        # opposing architect so the product's core debate promise is real.
        opposing_architect = next(
            (
                agent for agent in sorted(candidates, key=lambda item: item.name)
                if agent.name.lower().startswith("architect_")
            ),
            None,
        )
        selected: list[AgentBase] = [opposing_architect] if opposing_architect else []
        used_domains: set[str] = {"architecture"} if opposing_architect else set()
        for score, domain, agent in sorted(scored, key=lambda item: (-item[0], item[2].name)):
            if agent in selected or score <= 0 or domain in used_domains:
                continue
            selected.append(agent)
            used_domains.add(domain)
            if len(selected) >= 3:
                break
        minimum = min(3, len(candidates))
        if len(selected) < minimum:
            for _, _, agent in sorted(scored, key=lambda item: (-item[0], item[2].name)):
                if agent not in selected:
                    selected.append(agent)
                if len(selected) >= minimum:
                    break
        return selected

    def _deterministic_discovery_question(self) -> str:
        """Fallback question used only when adaptive discovery returns invalid output."""
        existing_design = self.ws.read("design")
        existing_plan = self.ws.read("plan")
        brief = self.ws.brief()
        decisions = self.ws.read("decisions")
        decisions_lower = decisions.lower()
        confirmed_answers = " ".join(re.findall(r"^- \*\*Decision:\*\*\s*(.+)$", decisions, re.I | re.M))
        context = "\n".join((self.idea, brief, existing_design, existing_plan))
        seed_words = set(re.findall(r"[a-z0-9]+", self.idea.lower()))
        words = set(re.findall(r"[a-z0-9]+", context.lower()))
        answer_words = set(re.findall(r"[a-z0-9]+", confirmed_answers.lower()))
        # Existing substantive artifacts already establish the product context;
        # discovery should refine them instead of asking a generic seed question.
        has_product_context = (
            len(brief.strip()) >= 80
            or len(existing_design.replace("(empty)", "").strip()) >= 300
            or len(existing_plan.replace("(empty)", "").strip()) >= 200
        )
        if len(seed_words) < 6 and not has_product_context and "who is the primary user" not in decisions_lower:
            return (
                "Who is the primary user, and what is the single most important outcome they must achieve?\n\n"
                "- [A] I’ll describe the primary user and outcome\n"
                "- [B] Infer a provisional user and outcome from the project\n\n"
                "Recommendation: A — explicit product intent prevents architecture built around the wrong workflow."
            )

        deployment_signals = {"aws", "azure", "gcp", "cloud", "onprem", "premises", "selfhosted", "agnostic", "portable"}
        deployment_answered = "what deployment constraint should drive the architecture" in decisions_lower
        if not deployment_answered and not words.intersection(deployment_signals):
            return (
                "What deployment constraint should drive the architecture?\n\n"
                "- [A] Cloud-agnostic and portable across providers\n"
                "- [B] Optimize for one cloud provider (you can name it as a custom answer)\n"
                "- [C] Self-hosted or on-premises deployment\n\n"
                "Recommendation: A — keep provider coupling low unless a specific cloud capability is a firm requirement."
            )

        specific_cloud = "one cloud provider" in confirmed_answers.lower() or "cloud-specific" in confirmed_answers.lower()
        named_provider = words.intersection({"aws", "azure", "gcp"}) or answer_words.intersection({"aws", "azure", "gcp"})
        if specific_cloud and not named_provider:
            return (
                "Which cloud provider should the design optimize for?\n\n"
                "- [A] AWS\n"
                "- [B] Microsoft Azure\n"
                "- [C] Google Cloud Platform\n\n"
                "Recommendation: Choose the provider your team already operates; use Other if the provider is not listed."
            )

        scale_signals = {"users", "requests", "rps", "traffic", "throughput", "events", "volume", "concurrent", "tenants"}
        scale_answered = "what initial scale should the architecture support" in decisions_lower
        if not scale_answered and not words.intersection(scale_signals):
            return (
                "What initial scale should the architecture support without redesign?\n\n"
                "- [A] Small launch: up to 1,000 active users\n"
                "- [B] Growing product: up to 100,000 active users\n"
                "- [C] Large or enterprise workload with explicit throughput targets\n\n"
                "Recommendation: A — start simple unless growth or contractual requirements justify additional complexity."
            )

        constraint_signals = {"compliance", "privacy", "residency", "retention", "gdpr", "hipaa", "pci", "soc2", "sensitive"}
        constraints_answered = "mandatory security, compliance, data-residency" in decisions_lower
        if not constraints_answered and not words.intersection(constraint_signals):
            return (
                "Are there mandatory security, compliance, data-residency, or retention constraints?\n\n"
                "- [A] No special constraints beyond standard security practices\n"
                "- [B] Yes — I’ll provide the required standards or regions\n"
                "- [C] Unknown — record them as validation items before implementation\n\n"
                "Recommendation: C when uncertain — make the unknown visible instead of silently assuming it away."
            )
        return ""

    def _product_rule_discovery_question(self) -> str:
        """Resolve high-confidence product families without spending a model turn."""
        evidence = " ".join((self.idea, self.task, self.ws.brief())).lower()
        decisions = self.ws.read("decisions").lower()
        is_messaging = any(term in evidence for term in ("messaging", "message app", "chat app", "whatsapp"))
        if not is_messaging:
            return ""
        encryption_question = (
            "Should every private and group conversation use end-to-end encryption that prevents the service "
            "operator from reading message content?"
        )
        if (any(term in evidence for term in ("secure", "private", "encrypted")) and not any(
            term in decisions for term in ("end-to-end encryption", "service-readable", "operator-readable")
        ) and not self._is_repeated_discovery_question(self._question_key(encryption_question))):
            return (
                encryption_question + "\n\n"
                "Why this matters: This fixes the trust boundary for message content and changes key management, "
                "multi-device synchronization, backup recovery, moderation, search, and operational diagnostics.\n\n"
                "- [A] End-to-end encryption for every conversation — Users gain a consistent privacy guarantee; "
                "server-side content recovery, inspection, and search are unavailable.\n"
                "- [B] User-selectable end-to-end encryption — Users can trade privacy for server-assisted features, "
                "but the product loses a simple uniform security guarantee.\n"
                "- [C] Service-readable encrypted storage — Recovery and moderation are simpler, but the operator "
                "or a server compromise can access message content.\n\n"
                "Recommendation: A — it best matches a secure private messaging product."
            )
        identity_question = "How should people establish their Chatbay account identity on a new device?"
        if (not any(term in decisions for term in (
            "phone-number", "phone number", "email sign-in", "identity provider",
        )) and not self._is_repeated_discovery_question(self._question_key(identity_question))):
            return (
                identity_question + "\n\n"
                "Why this matters: The identity anchor determines onboarding, contact discovery, abuse controls, "
                "device linking, account recovery, privacy exposure, and session-revocation behavior.\n\n"
                "- [A] Phone-number verification with optional usernames — Familiar contact discovery and recovery, "
                "with phone-number privacy and SIM-swap risks that require explicit controls.\n"
                "- [B] Email verification with unique usernames — Less carrier dependence, but weaker address-book "
                "discovery and a different recovery and abuse model.\n"
                "- [C] External identity provider — Faster onboarding for some users, but account access depends on "
                "a third party and exposes provider-linkage metadata.\n\n"
                "Recommendation: A — it matches the established WhatsApp-like onboarding model while keeping "
                "usernames optional."
            )
        return ""

    @staticmethod
    def _question_key(question: str) -> str:
        words = re.findall(r"[a-z0-9]+", (question or "").lower())
        stop = {"the", "a", "an", "is", "are", "what", "which", "should", "do", "does", "to", "for", "and", "or"}
        return " ".join(word for word in words if word not in stop)[:180]

    @staticmethod
    def _question_terms(question: str) -> set[str]:
        stop = {
            "the", "a", "an", "is", "are", "what", "which", "should", "do", "does",
            "to", "for", "and", "or", "their", "in", "of", "with", "as", "that",
            "it", "be", "only", "across", "may", "will", "would", "could", "this",
        }
        return {
            word for word in re.findall(r"[a-z0-9]+", (question or "").lower())
            if word not in stop
        }

    def _known_discovery_questions(self) -> set[str]:
        known = set(self._discovery_question_keys)
        if self.store and hasattr(self.store, "answered_checkpoint_questions"):
            known.update(
                self._question_key(question)
                for question in self.store.answered_checkpoint_questions()
            )
        return {question for question in known if question}

    def _is_repeated_discovery_question(self, key: str) -> bool:
        terms = self._question_terms(key)
        for previous in self._known_discovery_questions():
            if SequenceMatcher(None, key, previous).ratio() >= 0.72:
                return True
            previous_terms = self._question_terms(previous)
            smaller = min(len(terms), len(previous_terms))
            if smaller >= 3 and len(terms & previous_terms) / smaller >= 0.60:
                return True
        return False

    def _parse_discovery_proposal(self, response: str) -> Optional[str]:
        match = re.search(r"\{[\s\S]*\}", response or "")
        if not match:
            return None
        try:
            proposal = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if str(proposal.get("status", "")).lower() in {"ready", "ready_to_draft"}:
            self._discovery_questions_asked = max(1, self._discovery_questions_asked)
            return ""
        question = str(proposal.get("question", "")).strip()
        reason = str(proposal.get("reason", "")).strip()
        options = proposal.get("options", [])
        if len(question) < 12 or len(reason) < 12 or not isinstance(options, list) or not 2 <= len(options) <= 3:
            return None
        # Internal ledger headings such as "Decision 31: Delivery Contract"
        # are not answerable prompts. A checkpoint must stand on its own for a
        # user who has not followed the agents' internal discussion.
        if not question.endswith("?") or re.match(r"^\s*decision(?:\s+\d+)?\s*:", question, re.I):
            return None
        key = self._question_key(question)
        if not key or self._is_repeated_discovery_question(key):
            return None
        rendered_options = []
        for index, option in enumerate(options):
            if isinstance(option, dict):
                label = str(option.get("label", "")).strip()
                consequence = str(option.get("consequence", "")).strip()
                if len(label) < 3 or len(consequence) < 12:
                    return None
                text = f"{label} — {consequence}" if consequence else label
            else:
                text = str(option).strip()
            if not text:
                return None
            rendered_options.append(f"- [{chr(65 + index)}] {text}")
        recommendation = str(proposal.get("recommended", "")).strip()
        blocking = bool(proposal.get("blocking", True))
        self._discovery_question_keys.add(key)
        self._discovery_questions_asked += 1
        self._pending_discovery_checkpoint = {
            "phase": "discovery", "dimension": str(proposal.get("dimension", "")),
            "question": question, "rationale": reason, "recommendation": recommendation,
            "blocking": blocking,
            "options": [
                {
                    "label": chr(65 + index),
                    "summary": str(option.get("label", "")) if isinstance(option, dict) else str(option),
                    "consequence": str(option.get("consequence", "")) if isinstance(option, dict) else "",
                    "recommended": recommendation.lower() in {
                        chr(65 + index).lower(),
                        (str(option.get("label", "")) if isinstance(option, dict) else str(option)).lower(),
                    },
                }
                for index, option in enumerate(options)
            ],
        }
        parts = [question, f"Why this matters: {reason}", "\n".join(rendered_options)]
        if recommendation:
            parts.append(f"Recommendation: {recommendation}")
        if not blocking:
            parts.append("You may choose Other and defer this as a documented validation item.")
        return "\n\n".join(parts)

    async def _adaptive_discovery_question(self, coordinator, step: int) -> str:
        if self._discovery_questions_asked >= self.max_discovery_questions:
            return ""
        if self._adaptive_discovery_unavailable:
            return self._deterministic_discovery_question()
        if self.store and hasattr(self.store, "planning_context"):
            planning = self.store.planning_context({
                "design": ["product", "scope", "security", "privacy", "recovery", "unknown"],
                "plan": ["requirement", "acceptance", "risk"],
                "decisions": ["confirmed", "decision", "rationale"],
            }, max_chars=5500)
            context = (
                planning + "\n\n" + self.ws.planning_capabilities_context()
            )[:9000]
        else:
            context = self.ws.scoped_context(
                ["design", "plan", "decisions", "questions", "capabilities", "src_index"]
            )[:9000]
        prompt = (
            "Act as a requirements discovery analyst, not a designer or debater. Inspect the project evidence and "
            "identify the single highest-impact unresolved question whose answer could materially change the architecture. "
            "Do not ask anything answerable from the repository or already confirmed. Do not ask implementation trivia, "
            "bundle topics, or assume deployment, scale, security, data, integration, operational, budget, or team constraints. "
            "Treat release/version upgrades, auditability, and operational logging as required design coverage. Infer a "
            "proportionate default from project evidence, but ask when the implementation materially depends on the user's "
            "hosting, privacy, retention, compliance, or operational preferences. "
            "Do not ask about or reintroduce a concern the user explicitly excluded; record that override instead. "
            "Use PRODUCT_CAPABILITIES.json as an editable evaluation catalog: mode=include is mandatory, mode=exclude "
            "is an authoritative opt-out, and mode=auto requires relevance judgment. Missing entries are out of scope. "
            "Write for a project owner who has not seen the agents' discussion. The question must be a complete, "
            "plain-language sentence ending in '?', name the component or user interaction being decided, and never "
            "use an internal heading such as 'Decision 12: Delivery Contract'. The reason must explain the current "
            "project context and the practical impact of the choice. Each option label must describe the choice in "
            "plain language, and each consequence must state what the user gains or gives up. "
            "Return JSON only with: status ('ask' or 'ready_to_draft'), dimension, question, reason, "
            "options (2-3 objects with label and consequence), recommended, and blocking. "
            "Use ready_to_draft only when no material architecture uncertainty remains.\n\n"
            f"Product goal: {self.idea}\nCurrent request: {self.task or self.idea}\n"
            f"Questions already asked: {sorted(self._discovery_question_keys)}\n"
            "The scoped project-evidence packet is supplied separately and is authoritative."
        )
        def discovery_cost(candidate: AgentBase) -> tuple[int, float]:
            """Prefer low-overhead analysts; reserve the synthesis model for synthesis."""
            command = str(candidate.config.cli_command or "").lower()
            runtime = str(candidate.config.extra.get("runtime_base_name", "")).lower()
            if "gemini" in command or "gemini" in runtime:
                provider_cost = 0
            elif candidate.config.kind != "cli":
                provider_cost = 1
            elif "codex" in command or "codex" in runtime:
                provider_cost = 3
            else:
                provider_cost = 2
            return provider_cost, -self._synthesis_score(candidate)

        analysts = []
        seen_providers = set()
        candidates = [agent for agent in self.agents if agent is not coordinator]
        if not candidates:
            candidates = [coordinator]
        for candidate in sorted(candidates, key=discovery_cost):
            provider_id = candidate.config.base_id or candidate.config.id or candidate.name
            if provider_id in seen_providers or provider_id in self._discovery_failed_providers:
                continue
            seen_providers.add(provider_id)
            analysts.append(candidate)
            if len(analysts) >= 3:
                break

        last_error = None
        for attempt_index, analyst in enumerate(analysts):
            turn_context = {"step": step, "phase": "discovery_analysis", "standing_role": analyst.config.role}
            turn_id = self._begin_turn(analyst, turn_context)
            self._emit(Event(EventKind.TURN_START, agent=analyst.name, data={
                "turn_id": turn_id, "attempt": self._turn_attempts.get(turn_id, 1),
                **turn_context, **self._event_actor_meta(analyst),
            }))
            original_timeout = analyst.config.extra.get("timeout")
            analyst.config.extra["timeout"] = min(int(original_timeout or 45), 45)
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(analyst.send, prompt, "", context, context_only=True),
                    timeout=45,
                )
            except (RuntimeError, asyncio.TimeoutError) as exc:
                last_error = exc
                self._discovery_failed_providers.add(analyst.config.base_id or analyst.config.id or analyst.name)
                self._emit(Event(EventKind.ERROR, agent=analyst.name, data={
                    "turn_id": turn_id, "attempt": self._turn_attempts.get(turn_id, 1),
                    "phase": "discovery_analysis", "error": str(exc) or type(exc).__name__,
                    "error_code": classify_provider_error(exc).code, "recoverable": False,
                    **self._event_actor_meta(analyst),
                }))
                self._emit(Event(EventKind.PHASE, agent=analyst.name, data={
                    "phase": "discovery", "status": "provider_failover",
                    "message": "Discovery analyst failed; trying another configured agent.",
                    "reason": classify_provider_error(exc).code,
                    "attempted_provider": analyst.config.base_id or analyst.config.id,
                }))
                continue
            finally:
                if original_timeout is None:
                    analyst.config.extra.pop("timeout", None)
                else:
                    analyst.config.extra["timeout"] = original_timeout

            question = self._parse_discovery_proposal(response)
            if question is None:
                last_error = RuntimeError("Discovery analyst returned an invalid or duplicate question")
                self._emit(Event(EventKind.ERROR, agent=analyst.name, data={
                    "turn_id": turn_id, "attempt": self._turn_attempts.get(turn_id, 1),
                    "phase": "discovery_analysis", "error": str(last_error),
                    "error_code": "invalid_discovery_response", "recoverable": False,
                    **self._event_actor_meta(analyst),
                }))
                continue
            self._record_turn_usage(analyst, "discovery")
            self._emit(Event(EventKind.TURN_END, agent=analyst.name, data={
                "turn_id": turn_id, "attempt": self._turn_attempts[turn_id], "step": step,
                "response": "Discovery analysis complete.", **self._event_actor_meta(analyst),
                **self._usage_event(analyst),
            }))
            return question

        self._adaptive_discovery_unavailable = True
        self._emit(Event(EventKind.PHASE, agent=coordinator.name, data={
            "phase": "discovery", "status": "fallback",
            "message": "All discovery analysts were unavailable; continuing with bounded local discovery questions.",
            "reason": classify_provider_error(last_error or RuntimeError("No discovery analyst available")).code,
        }))
        return self._deterministic_discovery_question()

    @staticmethod
    def _is_explicit_user_correction(text: str) -> bool:
        normalized = " ".join((text or "").lower().replace("’", "'").split())
        negative = re.search(
            r"\b(?:i|we)\s+(?:don't|do not|dont|no longer|won't|will not|can't|cannot)\s+"
            r"(?:want|need|support|use|build|include|implement|do|keep|require)\b",
            normalized,
        )
        explicit_choice = re.search(
            r"\b(?:i|we)\s+(?:have decided to|decided to|choose|must use|will use|are switching to)\b",
            normalized,
        )
        return bool(negative or explicit_choice)

    @staticmethod
    def _synthesis_score(agent: AgentBase) -> int:
        """Reserve the strongest configured model for drafting/refinement, not routing."""
        kind = (agent.config.kind or "").lower()
        model = (agent.config.model or "").lower()
        explicit = int(agent.config.extra.get("synthesis_priority", 0) or 0)
        score = {"claude": 80, "openai": 78, "gemini": 72, "groq": 62, "ollama": 35, "cli": 30}.get(kind, 20)
        quality_markers = {
            "opus": 25, "sonnet": 18, "o3": 24, "o1": 20,
            "gpt-5": 24, "gpt-4.1": 18, "gpt-4o": 15,
            "pro": 16, "70b": 12, "72b": 12, "large": 8,
        }
        cheap_markers = {"flash": -8, "mini": -10, "small": -12, "8b": -14, "7b": -15, "3b": -18}
        score += max((value for marker, value in quality_markers.items() if marker in model), default=0)
        score += min((value for marker, value in cheap_markers.items() if marker in model), default=0)
        return score + explicit

    async def _run_state_machine(self, coordinator):
        max_steps = 30
        for step in range(1, max_steps + 1):
            await self._wait_if_paused()
            if not self._running:
                break

            if self.phase == OrchestratorPhase.DISCOVERY:
                await self._run_discovery_phase(coordinator, step)
            elif self.phase == OrchestratorPhase.DRAFTING:
                await self._run_drafting_phase(coordinator, step)
            elif self.phase == OrchestratorPhase.PEER_REVIEW:
                await self._run_peer_review_phase(step)
            elif self.phase == OrchestratorPhase.REFINEMENT:
                await self._run_refinement_phase(coordinator, step)
            elif self.phase == OrchestratorPhase.APPROVAL:
                await self._run_approval_phase(step)
            elif self.phase == OrchestratorPhase.COMPLETE:
                completion_errors = self._coordinator_completion_errors("PASS")
                if completion_errors:
                    raise PlanningQualityError(
                        "Planning quality gate blocked completion: " + " | ".join(completion_errors)
                    )
                self.completion_kind = "planning_complete"
                self.completion_files = ["DESIGN.md", "PLAN.md", "DECISIONS.md"]
                self._emit(Event(EventKind.PHASE, data={"phase": "coordinator", "status": "complete", "step": step}))
                if self.store:
                    self.store.clear_run_state()
                break
        else:
            raise RuntimeError("Planning workflow exceeded its deterministic step limit.")

    async def _run_discovery_phase(self, coordinator, step):
        self._emit(Event(EventKind.PHASE, data={"phase": "discovery", "status": "running", "step": step}))
        question = self._product_rule_discovery_question() if self.require_approval else ""
        if self.require_approval and not question:
            question = await self._adaptive_discovery_question(coordinator, step)
        if self.require_approval and not question and self._discovery_questions_asked == 0:
            question = self._deterministic_discovery_question()
        if question and self.require_approval:
            # Adaptive discovery registers valid model questions itself. Also
            # register deterministic fallbacks and test/custom providers here
            # so the configured discovery depth is always enforced.
            lines = [line.strip() for line in question.splitlines() if line.strip()]
            question_line = next((line for line in lines if line.endswith("?")), lines[0] if lines else "")
            question_key = self._question_key(question_line)
            if question_key and question_key not in self._discovery_question_keys:
                self._discovery_question_keys.add(question_key)
                self._discovery_questions_asked += 1
            payload = self._pending_discovery_checkpoint or self._checkpoint_payload_from_text(question)
            self._checkpoint_validation_errors = self._checkpoint_quality_errors(payload)
            if not self._checkpoint_validation_errors and self._enqueue_checkpoint_payloads([payload]):
                self.post_approval_phase = (
                    OrchestratorPhase.DISCOVERY
                    if self._discovery_questions_asked < self.max_discovery_questions
                    else OrchestratorPhase.DRAFTING
                )
                self.phase = OrchestratorPhase.APPROVAL
            else:
                self._deterministic_feedback = "\n".join(self._checkpoint_validation_errors)
                self.phase = OrchestratorPhase.DISCOVERY
        else:
            self.phase = OrchestratorPhase.DRAFTING
        self.save_state()

    @staticmethod
    def _checkpoint_payload_from_text(text: str) -> dict:
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        question_candidates = [
            line for line in lines
            if line.endswith("?") and not line.startswith("-")
            and not line.lower().startswith("recommendation:")
        ]
        legacy_candidates = [
            line for line in lines
            if re.search(r"\b(?:decision|question)(?:\s+\d+)?\b", re.sub(r"[*_`]", "", line), re.I)
            and "option" not in line.lower() and not line.lower().startswith("recommendation:")
        ]
        raw_question = question_candidates[0] if question_candidates else (legacy_candidates[-1] if legacy_candidates else next(
            (line for line in lines if not line.startswith("-") and not line.lower().startswith("recommendation:")),
            "Decision required",
        ))
        question = re.sub(r"^\s*(?:\d+[.)]\s*)?[*_`]*", "", raw_question)
        question = re.sub(r"[*_`]+", "", question).strip().rstrip(":")
        rationale_line = next((line for line in lines if line.lower().startswith("why this matters:")), "")
        if rationale_line:
            rationale = rationale_line.split(":", 1)[1].strip()
        else:
            question_index = lines.index(raw_question) if raw_question in lines else 0
            rationale = " ".join(line for line in lines[:question_index] if not line.startswith("#"))
        recommendation = next((line.split(":", 1)[1].strip() for line in lines if line.lower().startswith("recommendation:")), "")
        options = []
        for line in lines:
            match = re.match(
                r"^(?:-\s*)?(?:\[([A-Z])\]|(?:\*\*)?Option\s+([A-Z])(?:\s*\([^)]*\))?(?:\*\*)?\s*:)\s*(.+)$",
                line, re.I,
            )
            if match:
                label = (match.group(1) or match.group(2)).upper()
                summary, _, consequence = match.group(3).partition(" — ")
                options.append({
                    "label": label, "summary": summary, "consequence": consequence,
                    "recommended": "recommended" in line.lower() or recommendation.lower().startswith(label.lower()),
                })
        return {
            "phase": "discovery", "dimension": "", "question": question,
            "rationale": rationale, "recommendation": recommendation,
            "blocking": True, "options": options,
        }

    @staticmethod
    def _checkpoint_projection(checkpoint: dict) -> str:
        parts = [checkpoint.get("question", "")]
        if checkpoint.get("rationale"):
            parts.append(f"Why this matters: {checkpoint['rationale']}")
        options = []
        for option in checkpoint.get("options", []):
            consequence = f" — {option['consequence']}" if option.get("consequence") else ""
            recommended = " (Recommended)" if option.get("recommended") else ""
            options.append(f"- [{option['label']}] {option['summary']}{consequence}{recommended}")
        if options:
            parts.append("\n".join(options))
        if checkpoint.get("recommendation"):
            parts.append(f"Recommendation: {checkpoint['recommendation']}")
        return "\n\n".join(part for part in parts if part)

    def _enqueue_checkpoint_payloads(self, payloads: list[dict]) -> bool:
        """Persist checkpoints first; QUESTIONS.md is only the active-row projection."""
        if not self.store or not self.run_id:
            # Embedded callers without a ProjectStore retain the legacy file
            # adapter; server-managed projects never take this branch.
            projections = [self._checkpoint_projection(payload) for payload in payloads if payload.get("options")]
            if not projections:
                return False
            self.ws.write("questions", "# Decision Checkpoint\n\n" + "\n\n".join(projections))
            self.ws.normalize_checkpoint_queue()
            return True
        inserted = []
        for payload in payloads:
            if payload.get("question") and payload.get("options"):
                inserted.append(self.store.enqueue_checkpoint(self.run_id, **payload))
        if not inserted:
            return False
        current = self.store.current_checkpoint(self.run_id)
        self.ws.write("questions", "# Decision Checkpoint\n\n" + self._checkpoint_projection(current))
        return True

    def _enqueue_checkpoint_text(self, text: str) -> bool:
        payloads = [
            self._checkpoint_payload_from_text(question)
            for question in self.ws.split_checkpoint_questions(text)
        ]
        return self._enqueue_checkpoint_payloads(payloads)

    @staticmethod
    def _checkpoint_quality_errors(payload: dict) -> list[str]:
        errors = []
        question = str(payload.get("question", "")).strip()
        rationale = str(payload.get("rationale", "")).strip()
        options = payload.get("options", [])
        combined = " ".join((
            question, rationale,
            *(str(item.get("summary", "")) + " " + str(item.get("consequence", "")) for item in options),
        ))
        if not question.endswith("?"):
            errors.append("Decision checkpoint must contain one explicit question ending in '?'.")
        if len(options) not in {2, 3}:
            errors.append("Decision checkpoint must offer exactly 2 or 3 distinct options.")
        if len(rationale) < 30:
            errors.append("Decision checkpoint must explain the concrete product or architecture consequence.")
        if re.search(r"\bemail\b.{0,50}\bmore secure than\b.{0,30}\bphone", combined, re.I) or re.search(
            r"\bphone\b.{0,50}\bmore secure than\b.{0,30}\bemail", combined, re.I
        ):
            errors.append(
                "Decision checkpoint makes an unsupported channel-level security ranking; compare explicit threats, recovery, privacy, abuse, cost, and discoverability instead."
            )
        return errors

    async def _run_drafting_phase(self, coordinator, step):
        self._emit(Event(EventKind.PHASE, data={"phase": "drafting", "status": "running", "step": step}))
        new_steering = "\n".join(filter(None, (self._pending_user_input, await self._drain_steer())))
        self._pending_user_input = ""
        steer_block = f"\n\n[HUMAN STEERING]\n{new_steering}" if new_steering else ""

        prompt = prompt_catalog.render(
            "drafting", idea=self.idea, steering=steer_block or "None",
            task=self.task or "Develop the product goal into a credible planning baseline.",
            capabilities=self.ws.planning_capabilities_context(),
        )
        full_ctx = self._agent_context(coordinator)
        response = await self._send_agent_basic(
            coordinator, prompt, "drafting", step, ephemeral=full_ctx, synthesis=True, context_only=True,
        )
        self._apply_coordinator_agent_response(coordinator.name, response, replace_complete=True)
        self.phase = OrchestratorPhase.PEER_REVIEW
        self.save_state()

    async def _run_peer_review_phase(self, step):
        if not self._selected_peer_names:
            self._selected_peer_names = [agent.name for agent in self._select_peer_review_agents()]
            self.save_state()
        by_name = {agent.name: agent for agent in self.agents}
        other_agents = [by_name[name] for name in self._selected_peer_names if name in by_name]
        if not other_agents:
            self.phase = OrchestratorPhase.REFINEMENT
            self.save_state()
            return

        if self.peer_review_index >= len(other_agents):
            self.phase = OrchestratorPhase.REFINEMENT
            self.peer_review_index = 0
            self.save_state()
            return

        agent = other_agents[self.peer_review_index]
        self._emit(Event(EventKind.PHASE, data={"phase": "peer_review", "status": f"Review by {agent.name}", "step": step}))

        new_steering = await self._drain_steer()
        steer_block = f"\n\n[HUMAN STEERING]\n{new_steering}" if new_steering else ""

        prompt = prompt_catalog.render(
            "peer_review", role=agent.config.role or "Specialist", steering=steer_block or "None",
        )

        full_ctx = self._agent_context(agent)
        response = await self._send_agent_basic(
            agent, prompt, "peer_review", step, ephemeral=full_ctx, context_only=True,
        )
        review_errors = self._peer_review_errors(agent, response)
        if review_errors:
            raise PlanningQualityError(
                f"Peer review by {agent.name} was rejected: " + " | ".join(review_errors)
            )
        self.ws.append("logbook", response, agent.name, "Peer review critique")
        self.ws.add_context_event("peer_critique", response, "refinement", agent.name)
        self._apply_coordinator_agent_response(agent.name, response)
        self._consulted_specialists.add(agent.name)
        self.peer_review_index += 1
        self.save_state()

    def _peer_review_errors(self, agent: AgentBase, response: str) -> list[str]:
        """Reject tool narration or agreement masquerading as a completed review."""
        deltas = [
            self.ws.parse_section(response, marker).strip()
            for marker in ("DESIGN_APPEND", "PLAN_APPEND", "DECISIONS_APPEND")
        ]
        substantive = "\n".join(delta for delta in deltas if delta)
        errors = []
        if not substantive:
            errors.append(
                "response contained no bounded DESIGN_APPEND, PLAN_APPEND, or DECISIONS_APPEND delta"
            )
            return errors
        if len(re.findall(r"[A-Za-z0-9]+", substantive)) < 25:
            errors.append("review delta was too shallow to establish a substantive critique")
        if agent.name.lower().startswith("architect_"):
            # The critique may introduce its challenge before the bounded artifact
            # deltas and then use the append sections only for the resulting edits.
            # Validate the complete review for debate semantics while continuing to
            # require a substantive bounded delta for artifact mutation.
            lowered = response.lower()
            challenge_markers = (
                "alternative", "assumption", "challenge", "instead", "risk", "trade-off", "tradeoff",
            )
            decision_markers = (
                "prefer", "recommend", "should", "must", "adopt", "advocate",
                "favor", "favour", "choose", "win", "superior",
            )
            if not any(marker in lowered for marker in challenge_markers):
                errors.append("opposing architect did not challenge an assumption or present an alternative")
            if not any(marker in lowered for marker in decision_markers):
                errors.append("opposing architect did not state which approach should win")
        return errors

    async def _run_refinement_phase(self, coordinator, step):
        self._emit(Event(EventKind.PHASE, data={"phase": "refinement", "status": "running", "step": step}))
        new_steering = "\n".join(filter(None, (self._pending_user_input, await self._drain_steer())))
        self._pending_user_input = ""
        steer_block = f"\n\n[HUMAN STEERING]\n{new_steering}" if new_steering else ""

        prompt = prompt_catalog.render(
            "refinement", steering=steer_block or "None",
            capabilities=self.ws.planning_capabilities_context(),
            quality_feedback=self._deterministic_feedback or "None",
        )
        full_ctx = self._agent_context(coordinator)
        response = await self._send_agent_basic(
            coordinator, prompt, "refinement", step, ephemeral=full_ctx, synthesis=True, context_only=True,
        )
        
        # Clear deterministic feedback AFTER passing it to the prompt, so we can accumulate fresh errors from this turn.
        self._deterministic_feedback = ""
        self._apply_coordinator_agent_response(coordinator.name, response, replace_complete=False)
        self.ws.resolve_context_events({"peer_critique", "user_steering", "user_decision", "quality_failure"})
        self._refinement_attempts += 1

        decision = self.ws.parse_section(response, "DECISION_CHECKPOINT").strip()
        if self._is_none_text(decision):
            decision = ""

        decision_payloads = [
            self._checkpoint_payload_from_text(item)
            for item in self.ws.split_checkpoint_questions(decision)
        ] if decision else []
        self._checkpoint_validation_errors = [
            error for payload in decision_payloads for error in self._checkpoint_quality_errors(payload)
        ]
        if decision and not self._checkpoint_validation_errors and self._enqueue_checkpoint_payloads(decision_payloads):
            self.post_approval_phase = OrchestratorPhase.COMPLETE
            self.phase = OrchestratorPhase.APPROVAL
        else:
            errors = self._coordinator_completion_errors("PASS")
            errors = [*self._checkpoint_validation_errors, *errors]
            if errors and self._refinement_attempts < 3:
                error_lines = "\n".join(f"- {error}" for error in errors)
                self._deterministic_feedback = "\n".join(filter(None, (
                    self._deterministic_feedback,
                    "The following validation errors remain:",
                    error_lines
                )))
                self.ws.add_context_event(
                    "quality_failure", self._deterministic_feedback, "refinement", "deterministic_quality_gate"
                )
                self.phase = OrchestratorPhase.REFINEMENT
            elif errors and self.require_approval and any("user decision" in error for error in errors):
                raise PlanningQualityError(
                    "Planning requires a material user decision, but the coordinator did not provide "
                    "one concrete validated checkpoint with 2-3 trade-off options. Internal workflow "
                    "repair prompts must never be presented as product decisions. "
                    + " | ".join(errors)
                )
            elif not errors:
                self.phase = OrchestratorPhase.COMPLETE
            else:
                raise PlanningQualityError(
                    "Planning quality gate remained unsatisfied after bounded refinement: "
                    + " | ".join(errors)
                )
        self.save_state()

    async def _run_approval_phase(self, step):
        if not self.require_approval:
            self.phase = self.post_approval_phase or OrchestratorPhase.COMPLETE
            self.post_approval_phase = None
            self.save_state()
            return
        self._emit(Event(EventKind.PHASE, data={"phase": "approval", "status": "waiting_for_approval", "step": step}))
        self.pause()
        await self._wait_if_paused()

        if not self._running:
            return

        new_steering = await self._drain_steer()
        if new_steering:
            self.ws.append("logbook", new_steering, "User", "Provided approval/clarification")
            self._pending_user_input = "\n".join(filter(None, (self._pending_user_input, new_steering)))
        self._user_checkpoint_count += 1
        if self._checkpoint_has_more:
            self._checkpoint_has_more = False
            self.phase = OrchestratorPhase.APPROVAL
        else:
            self.phase = self.post_approval_phase or OrchestratorPhase.COMPLETE
            self.post_approval_phase = None
        self.save_state()

    async def _send_agent_basic(self, agent, prompt, phase_name, step, ephemeral="", synthesis=False, context_only=False):
        turn_context = {"step": step, "phase": phase_name, "standing_role": agent.config.role}
        turn_id = self._begin_turn(agent, turn_context)

        response = await self._send_agent(
            agent, prompt, turn_id, turn_context, ephemeral_context=ephemeral,
            system_override=SYNTHESIS_SYSTEM if synthesis else None, context_only=context_only,
        )
        self._record_turn_usage(agent, phase_name)
        self.ws.append("logbook", response, agent.name, "Turn completed")
        self._emit(Event(EventKind.TURN_END, agent=agent.name, data={
            "turn_id": turn_id, "attempt": self._turn_attempts[turn_id],
            "step": step, "response": response,
            **self._event_actor_meta(agent),
            **self._usage_event(agent),
        }))
        if await self._enforce_token_budget(phase_name, {"step": step, "agent": agent.name}):
            self._running = False
        return response


    # ── Helpers ───────────────────────────────────────────────────────────────

    def save_state(self):
        self.ws.refresh_context(
            goal=self.idea,
            phase=self.phase.value,
            consulted_specialists=sorted(self._consulted_specialists),
            next_action=self._context_next_action(),
        )
        state = {
            "run_id": self.run_id,
            "idea": self.idea,
            "task": self.task,
            "mode": self.mode,
            "run_contract": self.contract.to_dict() if self.contract else None,
            "turn_sequence": self._turn_sequence,
            "run_token_total": self.run_token_total,
            "phase_usage": self.phase_usage,
            "provider_turn_peak": self._provider_turn_peak,
            "phase": self.phase.value,
            "peer_review_index": self.peer_review_index,
            "post_approval_phase": self.post_approval_phase.value if self.post_approval_phase else None,
            "selected_peer_names": self._selected_peer_names,
            "refinement_attempts": self._refinement_attempts,
            "deterministic_feedback": self._deterministic_feedback,
            "pending_user_input": self._pending_user_input,
            "consulted_specialists": sorted(self._consulted_specialists),
            "user_checkpoint_count": self._user_checkpoint_count,
            "discovery_questions_asked": self._discovery_questions_asked,
            "discovery_question_keys": sorted(self._discovery_question_keys),
            "discovery_failed_providers": sorted(self._discovery_failed_providers),
            "artifact_fingerprints": self.ws.artifact_fingerprints(),
            "capability_contract_schema_version": self.ws.capability_contract_catalog()["schema_version"],
            "capability_contract_ids": [
                item["id"] for item in self.ws.selected_capability_contracts()
            ],
            "prompt_versions": prompt_catalog.versions(),
            "agents": {
                a.name: [
                    {
                        "role": m.role,
                        "content": m.content,
                        "timestamp": m.timestamp,
                        "usage": m.usage.to_dict(),
                    } for m in a.history
                ] for a in self.agents
            }
        }
        if self.store:
            self.store.save_run_state(state)
        else:
            try:
                (self.ws.root / "run_state.json").write_text(json.dumps(state, indent=2))
            except Exception:
                pass

    def load_state(self):
        if self.store:
            state = self.store.load_run_state()
            if state:
                if self.idea and state.get("idea") != self.idea:
                    return False
                saved_fingerprints = state.get("artifact_fingerprints")
                if (
                    saved_fingerprints
                    and saved_fingerprints != self.ws.artifact_fingerprints()
                    and not self.allow_artifact_changes_on_restore
                ):
                    return False
                self.task = self.task or state.get("task", "")
                self.mode = state.get("mode", self.mode)
                saved_contract = RunContract.from_dict(state.get("run_contract"))
                if saved_contract:
                    self.contract = RunContract(
                        RunKind.RECOVERY,
                        self._effective_request(self.idea, self.task),
                        saved_contract.target_artifacts,
                        saved_contract.requires_diagram_delta,
                        recovery_of=saved_contract.effective_kind,
                    )
                    if self.store and self.run_id:
                        self.store.update_run_contract(self.run_id, self.contract.kind.value)
                self._turn_sequence = state.get("turn_sequence", 0)
                self.run_token_total = int(state.get("run_token_total", 0) or 0)
                self.phase_usage = dict(state.get("phase_usage", {}))
                for key, value in state.get("provider_turn_peak", {}).items():
                    self._provider_turn_peak[str(key)] = max(self._provider_turn_peak.get(str(key), 0), int(value))
                self.phase = OrchestratorPhase(state.get("phase", OrchestratorPhase.DISCOVERY.value))
                self.peer_review_index = state.get("peer_review_index", 0)
                pap = state.get("post_approval_phase")
                self.post_approval_phase = OrchestratorPhase(pap) if pap else None
                self._selected_peer_names = list(state.get("selected_peer_names", []))
                self._refinement_attempts = int(state.get("refinement_attempts", 0) or 0)
                self._deterministic_feedback = str(state.get("deterministic_feedback", ""))
                self._pending_user_input = str(state.get("pending_user_input", ""))
                self._consulted_specialists = set(state.get("consulted_specialists", []))
                self._user_checkpoint_count = int(state.get("user_checkpoint_count", 0) or 0)
                self._discovery_questions_asked = int(state.get("discovery_questions_asked", 0) or 0)
                self._discovery_question_keys = set(state.get("discovery_question_keys", []))
                self._discovery_failed_providers = set(state.get("discovery_failed_providers", []))

                agent_states = state.get("agents", {})
                for a in self.agents:
                    if a.name in agent_states:
                        a.history = []
                        for m in agent_states[a.name]:
                            usage = Usage.from_dict(m.get("usage", {}))
                            msg = Message(role=m.get("role", ""), content=m.get("content", ""), timestamp=m.get("timestamp", ""), usage=usage)
                            a.history.append(msg)
                return True

        # Fallback to json file
        state_file = self.ws.root / "run_state.json"
        if not state_file.exists():
            return False
        try:
            state = json.loads(state_file.read_text())
            if self.idea and state.get("idea") != self.idea:
                return False
            saved_fingerprints = state.get("artifact_fingerprints")
            if (
                saved_fingerprints
                and saved_fingerprints != self.ws.artifact_fingerprints()
                and not self.allow_artifact_changes_on_restore
            ):
                return False
            self.task = self.task or state.get("task", "")
            self.mode = state.get("mode", self.mode)
            saved_contract = RunContract.from_dict(state.get("run_contract"))
            if saved_contract:
                self.contract = RunContract(
                    RunKind.RECOVERY,
                    self._effective_request(self.idea, self.task),
                    saved_contract.target_artifacts,
                    saved_contract.requires_diagram_delta,
                    recovery_of=saved_contract.effective_kind,
                )
            self._turn_sequence = state.get("turn_sequence", 0)
            self.run_token_total = int(state.get("run_token_total", 0) or 0)
            self.phase_usage = dict(state.get("phase_usage", {}))
            for key, value in state.get("provider_turn_peak", {}).items():
                self._provider_turn_peak[str(key)] = max(self._provider_turn_peak.get(str(key), 0), int(value))
            self.phase = OrchestratorPhase(state.get("phase", OrchestratorPhase.DISCOVERY.value))
            self.peer_review_index = state.get("peer_review_index", 0)
            pap = state.get("post_approval_phase")
            self.post_approval_phase = OrchestratorPhase(pap) if pap else None
            self._selected_peer_names = list(state.get("selected_peer_names", []))
            self._refinement_attempts = int(state.get("refinement_attempts", 0) or 0)
            self._deterministic_feedback = str(state.get("deterministic_feedback", ""))
            self._pending_user_input = str(state.get("pending_user_input", ""))
            self._consulted_specialists = set(state.get("consulted_specialists", []))
            self._user_checkpoint_count = int(state.get("user_checkpoint_count", 0) or 0)
            self._discovery_questions_asked = int(state.get("discovery_questions_asked", 0) or 0)
            self._discovery_question_keys = set(state.get("discovery_question_keys", []))
            self._discovery_failed_providers = set(state.get("discovery_failed_providers", []))

            agent_states = state.get("agents", {})
            for a in self.agents:
                if a.name in agent_states:
                    a.history = []
                    for m in agent_states[a.name]:
                        usage = Usage.from_dict(m.get("usage", {}))
                        msg = Message(role=m.get("role", ""), content=m.get("content", ""), timestamp=m.get("timestamp", ""), usage=usage)
                        a.history.append(msg)
            return True
        except Exception as e:
            print(f"Failed to load state: {e}")
            return False

    def _emit(self, event: Event):
        if self._cb:
            try:
                self._cb(event)
            except Exception:
                pass

        if event.kind in {EventKind.TURN_END, EventKind.STEER, EventKind.FILE_WRITE}:
            self.save_state()

    def _event_actor_meta(self, agent: AgentBase) -> dict:
        is_coordinator = bool(agent.config.extra.get("is_coordinator")) or (self._coordinator_name and agent.name == self._coordinator_name)
        return {
            "actor_role": "coordinator" if is_coordinator else "agent",
            "is_coordinator": is_coordinator,
            "standing_role": agent.config.role,
            "provider_agent": agent.config.extra.get("runtime_base_name") or agent.config.base_id or agent.name,
            "provider_id": agent.config.base_id or agent.config.id,
            "provider_kind": agent.config.kind,
            "provider_model": agent.config.model or "default",
        }

    def _begin_turn(self, agent: AgentBase, context: dict) -> str:
        self._turn_sequence += 1
        turn_id = f"turn-{self._turn_sequence:04d}"
        self._turn_attempts[turn_id] = 1
        return turn_id

    def _usage_event(self, agent: AgentBase) -> dict:
        usage = agent.last_usage.to_dict()
        cost = agent._cost(agent.last_usage)
        _, _, _, known = agent._pricing()
        return {
            "tokens": usage["total_tokens"],
            "usage": usage,
            "agent_totals": agent.usage_dict(),
            "cost_usd": cost,
            "pricing_known": known,
            "run_total_tokens": self.run_token_total,
            "run_max_tokens": self.max_tokens,
        }

    @staticmethod
    def _agent_system(agent: AgentBase) -> str:
        identity = f"You are {agent.name}."
        if agent.config.role:
            identity += f" Your standing role and perspective is: {agent.config.role}."
        if agent.config.system_prompt:
            identity += f"\n\nBehavior instructions:\n{agent.config.system_prompt}"
        if agent.config.working_directory:
            identity += (
                f"\n\nWorkspace invariant: the active project root is {agent.config.working_directory}. "
                "The DesignFlow context supplied with this turn is authoritative. Do not use a CLI scratch "
                "workspace, global conversation, or unrelated files, and do not claim supplied artifacts are missing."
            )
        return identity

    async def _send_agent(
        self,
        agent: AgentBase,
        prompt: str,
        turn_id: str = "turn-manual",
        turn_context: Optional[dict] = None,
        ephemeral_context: Optional[str] = None,
        system_override: Optional[str] = None,
        context_only: bool = False,
    ) -> str:
        attempt = self._turn_attempts.get(turn_id, 1)
        self._turn_attempts[turn_id] = attempt
        context = dict(turn_context or {})
        max_retries = int(agent.config.extra.get("rate_limit_max_retries", 0) or 0)
        effective_system = system_override or self._agent_system(agent)
        estimated_input = agent.estimate_input_tokens(prompt, effective_system, ephemeral_context or "")
        output_reserve = int(agent.config.extra.get("max_tokens", 2000) or 2000)
        # A historical turn peak is the whole turn (input + output). Using it as
        # an output reserve double-counts input and lets one large CLI context
        # permanently poison future preflight checks. The configured provider
        # output limit is the actual upper bound needed for this projection.
        projected_turn_reserve = output_reserve
        per_turn_cap = int(agent.config.extra.get("max_input_tokens_per_turn", 32000) or 32000)
        remaining = max(0, self.max_tokens - self.run_token_total) if self.max_tokens > 0 else per_turn_cap + projected_turn_reserve

        if estimated_input > per_turn_cap or estimated_input + projected_turn_reserve > remaining:
            compact = self.ws.read("context")
            compact_estimate = agent.estimate_input_tokens(prompt, effective_system, compact)
            if compact_estimate < estimated_input:
                ephemeral_context = compact
                estimated_input = compact_estimate
                self._emit(Event(EventKind.PHASE, agent=agent.name, data={
                    "phase": context.get("phase", "turn"),
                    "status": "context_compacted",
                    "estimated_input_tokens": estimated_input,
                    "reason": "Turn context was compacted before contacting the provider.",
                }))

        while estimated_input > per_turn_cap:
            error = (
                f"Preflight blocked an oversized prompt estimated at {estimated_input} input tokens "
                f"(per-turn limit {per_turn_cap})."
            )
            agent.mark_error(error)
            self._failed_turn = {
                "turn_id": turn_id,
                "attempt": attempt,
                "agent_id": agent.config.id,
                "agent": agent.name,
                "error": error,
                "public_error": (
                    "This turn exceeded the model context limit. DesignFlow compacted the available "
                    "workspace context and history; use Compact & Retry to run preflight again."
                ),
                "error_code": "context_too_large",
                "prompt": prompt,
                "estimated_input_tokens": estimated_input,
                "per_turn_limit": per_turn_cap,
                **context,
            }
            self._recovery_event.clear()
            self._paused = True
            self._pause_event.clear()
            self._emit(Event(EventKind.ERROR, agent=agent.name, data={
                "turn_id": turn_id,
                "attempt": attempt,
                "agent_id": agent.config.id,
                "error": self._failed_turn["public_error"],
                "error_code": "context_too_large",
                "recoverable": True,
                "estimated_input_tokens": estimated_input,
                "per_turn_limit": per_turn_cap,
                **self._event_actor_meta(agent),
                **context,
            }))
            await self._recovery_event.wait()
            if not self._running:
                raise asyncio.CancelledError()
            agent = next(
                (candidate for candidate in self.agents if candidate.config.id == self._failed_turn["agent_id"]),
                agent,
            )
            attempt += 1
            self._turn_attempts[turn_id] = attempt
            agent.error_message = ""
            current_history_cap = int(agent.config.extra.get("max_history_chars", 24000) or 24000)
            agent.config.extra["max_history_chars"] = max(4000, current_history_cap // 2)
            effective_system = system_override or self._agent_system(agent)
            compact = self.ws.read("context")
            ephemeral_context = compact
            estimated_input = agent.estimate_input_tokens(prompt, effective_system, compact)
            per_turn_cap = int(agent.config.extra.get("max_input_tokens_per_turn", 32000) or 32000)
        self._failed_turn = None
        while self.max_tokens > 0 and estimated_input + projected_turn_reserve > remaining:
            self._budget_exhausted = True
            self._emit(Event(EventKind.PHASE, agent=agent.name, data={
                "phase": context.get("phase", "turn"),
                "status": "budget_exhausted",
                "run_total_tokens": self.run_token_total,
                "run_max_tokens": self.max_tokens,
                "estimated_input_tokens": estimated_input,
                "projected_turn_tokens": projected_turn_reserve,
                "message": "The next agent turn was paused before contacting the provider because it may exceed the project budget.",
            }))
            self.pause()
            await self._wait_if_paused()
            if not self._running:
                raise asyncio.CancelledError()
            remaining = max(0, self.max_tokens - self.run_token_total)
        self._budget_exhausted = False
        self._emit(Event(EventKind.TURN_START, agent=agent.name, data={
            "turn_id": turn_id,
            "attempt": self._turn_attempts.get(turn_id, 1),
            **self._event_actor_meta(agent),
            **context,
        }))
        while self._running:
            try:
                main_loop = asyncio.get_running_loop()
                def call_mcp_tool(name: str, args: dict) -> str:
                    # Execute async MCP tool call in the main thread's event loop
                    if not self.mcp_manager:
                        raise RuntimeError("MCP Manager not initialized")
                    future = asyncio.run_coroutine_threadsafe(
                        self.mcp_manager.call_tool(name, args),
                        main_loop
                    )
                    return future.result()
                    
                agent_allowed_servers = self._allowed_mcp_servers.get(agent.name.lower(), [])
                if context_only:
                    agent_mcp_tools = []
                elif "*" in agent_allowed_servers:
                    agent_mcp_tools = self.mcp_tools
                else:
                    agent_mcp_tools = [t for t in self.mcp_tools if t.get("server") in agent_allowed_servers]

                provider_timeout = max(15, int(agent.config.extra.get("orchestrator_timeout", 300) or 300))
                attempt_token = agent.begin_attempt()
                try:
                    response = await asyncio.wait_for(
                        asyncio.to_thread(
                            agent.send, prompt, effective_system, ephemeral_context,
                            mcp_tools=agent_mcp_tools, tool_handler=call_mcp_tool,
                            attempt_token=attempt_token, context_only=context_only,
                        ),
                        timeout=provider_timeout,
                    )
                except asyncio.TimeoutError as exc:
                    agent.invalidate_attempt(attempt_token)
                    raise RuntimeError(
                        f"[{agent.name}] send failed: provider timed out after {provider_timeout} seconds"
                    ) from exc
                self._failed_turn = None
                return response
            except RuntimeError as exc:
                provider_error = classify_provider_error(exc)
                user_selectable_recovery = provider_error.code in {
                    "quota_exhausted", "rate_limited", "provider_timeout",
                }
                if not provider_error.retryable or user_selectable_recovery:
                    agent.mark_error(str(exc))
                    public_error, error_code = provider_error.message, provider_error.code
                    action_id = ""
                    if self.store and self.run_id:
                        action_id = self.store.enqueue_recovery_action(
                            self.run_id, "provider_error", agent.config.base_id or agent.config.id,
                            turn_id, provider_error.retryable, user_selectable_recovery, ""
                        )
                    self._failed_turn = {
                        "action_id": action_id,
                        "turn_id": turn_id,
                        "attempt": attempt,
                        "agent_id": agent.config.id,
                        "provider_id": agent.config.base_id or agent.config.id,
                        "agent": agent.name,
                        "error": str(exc),
                        "public_error": public_error,
                        "error_code": error_code,
                        "prompt": prompt,
                        "recovery_options": ["auto_failover", "wait_and_retry", "stop"],
                        **context,
                    }
                    self._recovery_event.clear()
                    self._paused = True
                    self._pause_event.clear()
                    self._emit(Event(EventKind.ERROR, agent=agent.name, data={
                        "turn_id": turn_id,
                        "attempt": attempt,
                        "agent_id": agent.config.id,
                        "error": public_error,
                        "error_code": error_code,
                        "recoverable": True,
                        "message": "Pause or fix this provider, then retry the failed turn.",
                        **self._event_actor_meta(agent),
                    }))
                    await self._recovery_event.wait()
                    if not self._running:
                        raise asyncio.CancelledError()
                    # A user may have paused the failed provider and substituted
                    # this logical specialist with another model. Resolve it only
                    # after explicit recovery, at this safe turn boundary.
                    recovery_action = self._failed_turn.get("recovery_action", "wait_and_retry")
                    agent = next(
                        (candidate for candidate in self.agents if candidate.config.id == self._failed_turn["agent_id"]),
                        agent,
                    )
                    attempt += 1
                    self._turn_attempts[turn_id] = attempt
                    agent.error_message = ""
                    self._emit(Event(EventKind.TURN_START, agent=agent.name, data={
                        "turn_id": turn_id, "attempt": attempt, "resumed": True,
                        "retry_reason": recovery_action, **context, **self._event_actor_meta(agent),
                    }))
                    continue
                if max_retries and attempt >= max_retries + 1:
                    raise
                delay = self._retry_delay(exc, attempt, agent)
                retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                agent.mark_waiting(retry_at.isoformat(), str(exc))
                self._emit(Event(EventKind.RETRY, agent=agent.name, data={
                    "turn_id": turn_id, "attempt": attempt,
                    "retry_in_seconds": delay,
                    "retry_at": retry_at.isoformat(),
                    "reason": str(exc),
                    **self._event_actor_meta(agent),
                }))
                remaining = delay
                while remaining > 0 and self._running:
                    step = min(5, remaining)
                    await asyncio.sleep(step)
                    remaining -= step
                if self._running:
                    attempt += 1
                    self._turn_attempts[turn_id] = attempt
                    self._emit(Event(EventKind.TURN_START, agent=agent.name, data={
                        "turn_id": turn_id, "attempt": attempt, "resumed": True,
                        "retry_reason": "usage_limit", **context, **self._event_actor_meta(agent),
                    }))
        raise asyncio.CancelledError()

    @staticmethod
    def _is_rate_limit(exc: Exception) -> bool:
        message = str(exc).lower()
        markers = (
            "rate limit", "usage limit", "quota exceeded", "quota exhausted",
            "resource exhausted", "too many requests", "status code: 429",
            "error 429", "limit reached",
        )
        return any(marker in message for marker in markers)

    @staticmethod
    def _public_error(exc: Exception) -> tuple[str, str]:
        """Return a bounded UI-safe error without prompts or provider dumps."""
        public = classify_provider_error(exc)
        return public.message, public.code

    @staticmethod
    def _retry_delay(exc: Exception, attempt: int, agent: AgentBase) -> int:
        message = str(exc).lower()
        match = re.search(
            r"(?:retry|try again|resets?)\s+(?:after|in)\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)?",
            message,
        )
        if match:
            value = float(match.group(1))
            unit = match.group(2) or "seconds"
            if unit.startswith(("h", "hr")):
                value *= 3600
            elif unit.startswith(("m", "min")):
                value *= 60
            return max(1, int(value))
        clock = re.search(r"(?:retry|try again|resets?)\s+(?:at|on)\s+(\d{1,2}:\d{2}\s*(?:am|pm))", message)
        if clock:
            now = datetime.now().astimezone()
            target_time = datetime.strptime(clock.group(1).upper(), "%I:%M %p").time()
            target = datetime.combine(now.date(), target_time, tzinfo=now.tzinfo)
            if target <= now:
                target += timedelta(days=1)
            return max(1, int((target - now).total_seconds()))
        base = int(agent.config.extra.get("rate_limit_retry_seconds", 30) or 30)
        cap = int(agent.config.extra.get("rate_limit_max_wait_seconds", 900) or 900)
        return min(cap, base * (2 ** min(attempt - 1, 6)))

    async def _wait_if_paused(self):
        await self._pause_event.wait()

    async def _drain_steer(self) -> str:
        msgs = []
        while not self._steer_queue.empty():
            msgs.append(await self._steer_queue.get())
        return "\n".join(msgs)

    def _record_turn_usage(self, agent: AgentBase, phase: str = "unknown"):
        self.run_token_total += agent.last_usage.total_tokens
        provider_key = agent.config.base_id or agent.config.id or agent.name
        self._provider_turn_peak[provider_key] = max(
            self._provider_turn_peak.get(provider_key, 0),
            agent.last_usage.total_tokens,
        )
        if self.store and hasattr(self.store, "record_provider_turn_peak"):
            self.store.record_provider_turn_peak(provider_key, agent.last_usage.total_tokens)
        bucket = self.phase_usage.setdefault(phase, {"tokens": 0, "cost_usd": 0.0, "turns": 0})
        bucket["tokens"] += agent.last_usage.total_tokens
        bucket["cost_usd"] += agent._cost(agent.last_usage)
        bucket["turns"] += 1

    async def _enforce_token_budget(self, phase: str, context: Optional[dict] = None) -> bool:
        while self.max_tokens > 0 and self.run_token_total >= self.max_tokens:
            self._budget_exhausted = True
            payload = {
                "phase": phase,
                "status": "budget_exhausted",
                "run_total_tokens": self.run_token_total,
                "run_max_tokens": self.max_tokens,
            }
            if context:
                payload.update(context)
            self._emit(Event(EventKind.PHASE, data=payload))
            self.pause()
            await self._wait_if_paused()
            if not self._running:
                break

        self._budget_exhausted = False
        return False

    @staticmethod
    def _role_context_needs(agent_name: str) -> list[str]:
        return ROLE_NEEDS.get((agent_name or "").lower(), ["design", "plan", "decisions", "questions"])

    def _context_next_action(self) -> str:
        actions = {
            OrchestratorPhase.DISCOVERY: "Confirm whether any essential product context is missing.",
            OrchestratorPhase.DRAFTING: "Draft the architecture and implementation plan.",
            OrchestratorPhase.PEER_REVIEW: "Collect the next relevant specialist critique.",
            OrchestratorPhase.REFINEMENT: "Resolve critiques and update the canonical artifacts.",
            OrchestratorPhase.APPROVAL: "Wait for the user's answer to the active question.",
            OrchestratorPhase.COMPLETE: "Planning baseline is ready for implementation discovery.",
        }
        return actions.get(self.phase, "Continue the planning workflow.")

    def _should_force_full_context(self, agent_name: str) -> bool:
        count = self._context_invocations.get(agent_name, 0)
        if count == 0:
            return True
        if count % self._context_full_refresh_every == 0:
            return True
        return False

    def _remember_context_delivery(self, agent_name: str):
        self._context_invocations[agent_name] = self._context_invocations.get(agent_name, 0) + 1

    def _coordinator_context(self) -> str:
        agent_name = self._coordinator_name or "coordinator"
        if self.store and hasattr(self.store, "planning_context"):
            self._remember_context_delivery(agent_name)
            return self.store.planning_context({
                "design": ["product", "scope", "architecture", "security", "failure", "contract", "unknown"],
                "plan": ["requirement", "phase", "acceptance", "test", "risk"],
                "decisions": ["confirmed", "decision", "trade-off", "rationale"],
            }, max_chars=14000)
        roles = ["context", "design", "plan", "decisions", "questions", "src_index"]
        if self._should_force_full_context(agent_name):
            context = self.ws.scoped_context(roles)
        else:
            context = self.ws.changed_context(agent_name, roles)
        self._remember_context_delivery(agent_name)
        return context

    def _agent_context(self, agent: AgentBase) -> str:
        design_keywords, plan_keywords = self._keywords.get(
            agent.name.lower(),
            (["architecture", "requirements", "reliability"], ["requirements", "phases", "testing"]),
        )
        max_chars = int(agent.config.extra.get("specialist_context_max_chars", 8000) or 8000)
        max_chars = max(3000, min(max_chars, 8000))
        if self.store and hasattr(self.store, "planning_context"):
            context = self.store.planning_context({
                "design": design_keywords,
                "plan": plan_keywords,
                "decisions": ["confirmed", "decision", "trade-off", "rationale"],
            }, max_chars=max_chars)
        else:
            context = self.ws.specialist_context(design_keywords, plan_keywords, max_chars=max_chars)
        self._remember_context_delivery(agent.name)
        return context

    def _seed_planning_store(self) -> None:
        """Import legacy Markdown once; subsequent mutations are database-first."""
        if not self.store or not hasattr(self.store, "replace_planning_document"):
            return
        counts = self.store.planning_state_counts()
        for artifact in ("design", "plan", "decisions"):
            if counts.get(artifact, 0) == 0:
                self.store.replace_planning_document(artifact, self.ws.read(artifact), "migration")

    def _project_planning_document(self, artifact: str) -> None:
        """Generate the human-facing Markdown projection from normalized state."""
        if not self.store or not hasattr(self.store, "planning_document"):
            return
        rendered = self.store.planning_document(artifact)
        if rendered:
            self.ws.write(artifact, rendered)

    @staticmethod
    def _quality_gate_passed(quality_gate: str) -> bool:
        if not quality_gate.strip():
            return False
        first_line = quality_gate.strip().splitlines()[0].strip().upper()
        return first_line == "PASS"

    def _coordinator_completion_errors(self, quality_gate: str) -> list[str]:
        errors = []
        if not self._quality_gate_passed(quality_gate):
            errors.append("Coordinator QUALITY_GATE must start with PASS before completion.")
        errors.extend(self.ws.validate_planning_artifacts())
        errors.extend(self.ws.validate_goal_alignment(self.idea))
        specialists = [agent for agent in self.agents if agent.name != self._coordinator_name]
        required_specialists = min(3, len(specialists))
        if len(self._consulted_specialists) < required_specialists:
            missing = required_specialists - len(self._consulted_specialists)
            errors.append(
                f"Consult {missing} more distinct relevant specialist(s) before completion "
                f"({len(self._consulted_specialists)}/{required_specialists} consulted)."
            )
        opposing_architects = {
            agent.name for agent in specialists if agent.name.lower().startswith("architect_")
        }
        if opposing_architects and not opposing_architects.intersection(self._consulted_specialists):
            errors.append(
                "An opposing architect must complete an adversarial review before a planning debate can finish."
            )
        if self.require_approval and required_specialists >= 2 and self._user_checkpoint_count < 1:
            errors.append(
                "Pause for at least one material user decision and receive the user's confirmation before completion."
            )
        return errors


    def _is_none_text(self, text: str) -> bool:
        t = text.lower().strip()
        if not t: return True
        if t in {"none", "n/a", "none.", "not applicable", "no decision needed", "no questions", "none at this time", "*none*", "*n/a*"}: return True
        if t.startswith("none ") or t.startswith("none-") or t.startswith("*none"): return True
        return False

    def _apply_coordinator_agent_response(
        self, agent_name: str, response: str, replace_complete: bool = False,
    ) -> set[str]:
        written_files: set[str] = set()
        for filename, content in self.ws.parse_files(response).items():
            self.ws.write_src(filename, content)
            written_files.add(filename)
            self._emit(Event(EventKind.FILE_WRITE, agent=agent_name,
                             data={"file": filename, "preview": content[:120]}))

        plan_update = self.ws.parse_section(response, "PLAN_UPDATE")
        if plan_update:
            if self.store and hasattr(self.store, "merge_planning_document"):
                if replace_complete:
                    self.store.replace_planning_document("plan", f"# Plan\n\n{plan_update.strip()}", agent_name)
                    written, reason = True, "database replacement"
                else:
                    written = self.store.merge_planning_document("plan", plan_update, agent_name)
                    reason = "database section merge" if written else "PLAN_UPDATE contained no H2 sections (you must use '## Heading' inside the block)"
                if written:
                    self._project_planning_document("plan")
            else:
                writer = self.ws.replace_complete_artifact if replace_complete else self.ws.merge_artifact_update
                written, reason = writer("plan", plan_update, "Plan")
            if written:
                written_files.add("PLAN.md")
                self._emit(Event(EventKind.FILE_WRITE, agent=agent_name, data={"file": "PLAN.md"}))
            else:
                self._deterministic_feedback = "\n".join(filter(None, (self._deterministic_feedback, reason)))

        decisions_update = self.ws.parse_section(response, "DECISIONS_UPDATE")
        if decisions_update:
            if self.store and hasattr(self.store, "merge_planning_document"):
                if replace_complete:
                    self.store.replace_planning_document(
                        "decisions", f"# Key Decisions\n\n{decisions_update.strip()}", agent_name,
                    )
                    written, reason = True, "database replacement"
                else:
                    written = self.store.merge_planning_document("decisions", decisions_update, agent_name)
                    reason = "database section merge" if written else "DECISIONS_UPDATE contained no H2 sections (you must use '## Heading' inside the block)"
                if written:
                    self._project_planning_document("decisions")
            else:
                writer = self.ws.replace_complete_artifact if replace_complete else self.ws.merge_artifact_update
                written, reason = writer("decisions", decisions_update, "Key Decisions")
            if written:
                written_files.add("DECISIONS.md")
                self._emit(Event(EventKind.FILE_WRITE, agent=agent_name, data={"file": "DECISIONS.md"}))
            else:
                self._deterministic_feedback = "\n".join(filter(None, (self._deterministic_feedback, reason)))

        design_update = self.ws.parse_section(response, "DESIGN_UPDATE")
        if design_update:
            if self.store and hasattr(self.store, "merge_planning_document"):
                if replace_complete:
                    self.store.replace_planning_document(
                        "design", f"# Architecture Design\n\n{design_update.strip()}", agent_name,
                    )
                    written, reason = True, "database replacement"
                else:
                    written = self.store.merge_planning_document("design", design_update, agent_name)
                    reason = "database section merge" if written else "DESIGN_UPDATE contained no H2 sections (you must use '## Heading' inside the block)"
                if written:
                    self._project_planning_document("design")
            else:
                writer = self.ws.replace_complete_artifact if replace_complete else self.ws.merge_artifact_update
                written, reason = writer("design", design_update, "Architecture Design")
            if written:
                written_files.add("DESIGN.md")
                self._emit(Event(EventKind.FILE_WRITE, agent=agent_name, data={"file": "DESIGN.md"}))
            else:
                self._deterministic_feedback = "\n".join(filter(None, (self._deterministic_feedback, reason)))

        design_bit = self.ws.parse_section(response, "DESIGN_APPEND")
        if design_bit:
            if self.store and hasattr(self.store, "merge_planning_document"):
                heading = f"Review Delta — {agent_name} — {self._turn_sequence}"
                self.store.merge_planning_document("design", f"## {heading}\n\n{design_bit}", agent_name)
                self._project_planning_document("design")
            else:
                self.ws.append("design", design_bit, agent_name, "Coordinator-led Turn")
            written_files.add("DESIGN.md")
            self._emit(Event(EventKind.FILE_WRITE, agent=agent_name, data={"file": "DESIGN.md"}))

        plan_bit = self.ws.parse_section(response, "PLAN_APPEND")
        if plan_bit:
            if self.store and hasattr(self.store, "merge_planning_document"):
                heading = f"Review Delta — {agent_name} — {self._turn_sequence}"
                self.store.merge_planning_document("plan", f"## {heading}\n\n{plan_bit}", agent_name)
                self._project_planning_document("plan")
            else:
                self.ws.append("plan", plan_bit, agent_name, "Peer Review")
            written_files.add("PLAN.md")
            self._emit(Event(EventKind.FILE_WRITE, agent=agent_name, data={"file": "PLAN.md"}))

        decisions_bit = self.ws.parse_section(response, "DECISIONS_APPEND")
        if decisions_bit:
            if self.store and hasattr(self.store, "merge_planning_document"):
                heading = f"Review Delta — {agent_name} — {self._turn_sequence}"
                self.store.merge_planning_document("decisions", f"## {heading}\n\n{decisions_bit}", agent_name)
                self._project_planning_document("decisions")
            else:
                self.ws.append("decisions", decisions_bit, agent_name, "Peer Review")
            written_files.add("DECISIONS.md")
            self._emit(Event(EventKind.FILE_WRITE, agent=agent_name, data={"file": "DECISIONS.md"}))
        return written_files
