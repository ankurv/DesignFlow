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
from typing import Any, Callable, Optional
from backend.mcp_client import MCPManager

from .agents.base import AgentBase, Message, Usage
from .errors import classify_provider_error
from .workspace.workspace import Workspace


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

DEBATE_SYSTEM = """You are one of {n} agents collaborating to design a software product.
Each agent may have a standing perspective. Contribute through your own expertise while
challenging weak ideas and building on strong ones. Be specific and opinionated.

CRITICAL: If the product involves a frontend or user interface, you must explicitly evaluate every design decision through the lens of an external end-user. Usability, user journeys, and intuitive UX must take precedence over technical backend implementation details during the early stages of debate. For purely backend or API systems, focus directly on architecture, data models, and performance.

Architecture Diagrams: Intelligently evaluate if the architecture is complex enough to warrant a visual diagram. If it is, you MUST include professional Mermaid.js diagrams inside your DESIGN_UPDATE block using standard ```mermaid ... ``` code fences.

Diagram quality rules:
- Optimize for readability first, not visual flair.
- Prefer 2-3 focused diagrams over one giant diagram when the system has multiple concerns.
- Keep each diagram on a single level of abstraction where possible; do not mix UI screens, state lifecycle, backend services, and infra all in one crowded map unless the system is truly tiny.
- Use short, scan-friendly node labels.
- Use subgraphs only when they materially clarify grouping.
- Avoid excessive styling, decorative class definitions, or noisy edge labels.
- When useful, split into distinct diagrams such as: lifecycle/state flow, user journey/screen flow, and technical architecture/service map.
- If a diagram starts becoming hard to read, split it rather than expanding it.

Respond in this exact format:

## DESIGN_UPDATE
<your complete, updated architecture design — proposal, critique, refinement, and user-experience evaluation. Include mermaid diagrams if proposing complex architecture. This will OVERWRITE the previous design, so ensure it is comprehensive.>

## PLAN_UPDATE
<complete updated content of PLAN.md — use nested tree-structures (e.g. nested lists) for sub-tasks>

Only finalize when you genuinely believe the design is solid and complete."""

BUILD_SYSTEMS = {
    "developer": """You are the DEVELOPER this iteration.
Read the workspace and write or update source code based on the design and any review feedback.

Respond in this exact format:

## FILE: src/filename.py
<complete file content — no markdown fences>

## FILE: src/another_file.py
<complete file content>

## PLAN_UPDATE
<updated PLAN.md — check off completed tasks with [x]>""",

    "reviewer": """You are the CODE REVIEWER this iteration.
Read the code and design carefully. Add inline comments as # REVIEW: your note
directly in the source files. Update the design document with architectural notes.

Respond in this exact format:

## FILE: src/filename.py
<file content with your # REVIEW: comments added inline>

## DESIGN_APPEND
<architectural notes, concerns, or decisions>

## VERDICT
APPROVE
or
CHANGES NEEDED
<specific issues that must be fixed>""",

}

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

SPECIALIST_SIGNALS = {
    "architecture": ({"architecture", "system", "service", "scale", "performance", "distributed"}, {"architect_alpha", "architect_beta"}),
    "product": ({"product", "user", "mvp", "feature", "market", "workflow"}, {"product_manager", "product_strategist"}),
    "ux": ({"ui", "ux", "screen", "dashboard", "website", "mobile", "user", "flow"}, {"ux_simplifier", "ui_designer", "workflow_designer"}),
    "data": ({"data", "database", "schema", "query", "storage", "migration", "analytics"}, {"data_architect"}),
    "security": ({"auth", "security", "privacy", "payment", "secret", "permission", "tenant", "compliance"}, {"security_auditor", "red_team"}),
    "api": ({"api", "rest", "graphql", "webhook", "integration", "client", "backend"}, {"api_designer"}),
    "operations": ({"cloud", "deploy", "docker", "kubernetes", "aws", "azure", "gcp", "monitor", "reliability"}, {"cloud_architect", "devops_engineer"}),
    "research": ({"existing", "repository", "codebase", "legacy", "migration", "refactor"}, {"researcher"}),
    "growth": ({"sales", "marketing", "pricing", "growth", "acquisition", "seo"}, {"sales_alpha", "sales_beta", "marketing_alpha", "marketing_beta"}),
}

SPECIALIZED_PERSONAS = {
    "architect_alpha": "You are ARCHITECT ALPHA. Propose robust, scalable system designs. Vigorously debate competing designs and highlight their flaws while defending your own.",
    "architect_beta": "You are ARCHITECT BETA. Propose alternative, highly-optimized system designs. Challenge Architect Alpha's assumptions and fight for a superior approach.",
    "researcher": "You are the RESEARCHER. Read existing workspace files to ground the debate in reality. Fact-check the Architects to ensure they do not hallucinate APIs or codebase assumptions.",
    "red_team": "You are the RED TEAM agent. Your sole purpose is to hunt for edge-cases, race conditions, security vulnerabilities, and ways to break the proposed design.",
    "ux_simplifier": "You are the UX SIMPLIFIER. You fiercely advocate for the external user. You must aggressively fight to simplify complex UI flows, remove unnecessary features, and ensure the system is intuitive.",
    "ui_designer": "You are the UI DESIGNER. Focus on visual hierarchy, layout clarity, affordances, states, feedback, density, and readability. Challenge designs that are cluttered, ambiguous, or visually noisy, and propose cleaner interaction surfaces.",
    "workflow_designer": "You are the WORKFLOW DESIGNER. Focus on end-to-end user journeys, re-entry after failure, iteration loops, approvals, empty states, and operational usability. You must make sure the product feels smooth over repeated real-world use, not just the happy path.",
    "cloud_architect": "You are the CLOUD ARCHITECT. Focus strictly on scalability, database indexing, infrastructure bottlenecks, and deployment environments.",
    "product_manager": "You are the PRODUCT MANAGER. You enforce MVP constraints and fight YAGNI (You Aren't Gonna Need It). You ensure the team is only building what is strictly necessary to validate the idea and get to market fast.",
    "product_strategist": "You are the PRODUCT STRATEGIST. Focus on product framing, user value, positioning, differentiation, and what the product promise should actually be. Challenge solutions that are technically elegant but weak in user value or narrative clarity.",
    "data_architect": "You are the DATA ARCHITECT. Focus purely on schema design, normalization vs. denormalization, migration strategies, and complex query performance.",
    "security_auditor": "You are the SECURITY AUDITOR. Enforce secure defaults (OWASP Top 10). Ensure that proper authentication, data encryption at rest, and input sanitization are baked into the architecture.",
    "devops_engineer": "You are the DEVOPS ENGINEER. Plan the deployment pipelines, containerization (Docker/Kubernetes), and observability. Ensure logging, monitoring, and rollback strategies are part of the plan.",
    "api_designer": "You are the API DESIGNER. Focus purely on the communication layer. Ensure REST/GraphQL endpoints are intuitive, stateless, properly versioned, and standardized.",
    "sales_alpha": "You are SALES STRATEGIST ALPHA. Pitch aggressive, high-growth go-to-market strategies and pricing models. Vigorously debate competing sales strategies and defend your approach.",
    "sales_beta": "You are SALES STRATEGIST BETA. Pitch alternative, calculated go-to-market strategies (e.g. product-led growth vs sales-led). Challenge Sales Alpha's assumptions and fight for a superior approach.",
    "marketing_alpha": "You are MARKETING EXPERT ALPHA. Focus on brand positioning, viral loops, and aggressive user acquisition. Vigorously debate competing marketing plans and defend your approach.",
    "marketing_beta": "You are MARKETING EXPERT BETA. Focus on long-term SEO, content marketing, and community building. Challenge Marketing Alpha's short-term strategies and fight for a superior approach."
}

COORDINATOR_SYSTEM = """You are the COORDINATOR of an autonomous software architecture and design team.
Your SOLE goal is to coordinate the team's agents to turn a high-level goal into a credible planning baseline: a comprehensive DESIGN.md, a crisp PLAN.md implementation checklist, and a DECISIONS.md ledger of the key choices. The artifacts should give a coding agent a strong starting point, but MUST NOT claim to be a final or perfectly complete implementation specification.
CRITICAL: You MUST NOT write any executable code (e.g. .py, .js, .html). Your output will be fed to a separate coding agent.
Your debate depth limit is: {max_debate_rounds} rounds.

Guidelines for Design & Architectural Gathering:
1. **Context-Aware Usability First**: IF the project involves a user interface, frontend, or human interaction, you MUST explicitly map out the user journey, UI flows, UX interactions, and repeat-use workflow before diving into backend architectures. Prioritize summoning ux_simplifier, ui_designer, workflow_designer, or product_manager early in these cases. If the project is purely backend, CLI, or API-driven, skip UI/UX design and focus directly on architecture and data models.
2. **User-Centric Simplification**: Evaluate all designs from an external user's perspective. Simplify complex UI/UX.
3. **Product Framing**: When the user is shaping a product rather than a raw technical system, ensure the team explicitly debates product framing, user value, differentiation, and scope discipline before over-specifying implementation details.
4. **API Contract (CRITICAL)**: If a frontend and backend are involved, you MUST establish a firm API contract. Document all required API endpoints (methods, paths, payloads, responses) clearly in a dedicated section in DESIGN.md so the UI and Backend can be developed independently.
5. **Data Models & Database**: Document the schema layout, tables/collections, and relationships.
6. **Scalability Analysis**: Dedicate a "## Scalability, Bottlenecks & Design Choices" section in DESIGN.md.
7. **Architecture Diagrams**: Intelligently evaluate if the architecture warrants a visual diagram. If it does, ensure the agents include professional Mermaid diagrams in DESIGN.md. Prefer clarity over density: split large systems into multiple focused diagrams, keep labels short, keep abstraction levels separated, and avoid decorative styling that makes diagrams harder to scan.
8. **Plan Structure (CRITICAL)**: PLAN.md MUST be a clean, crisp, end-to-end implementation checklist that we will feed to a separate coding agent. It should contain clear chronologically-ordered implementation phases and checkable tasks. Do NOT include debate transcripts in PLAN.md or DESIGN.md.
9. **Planned User Checkpoints**: Actively involve the user at three moments when useful: after framing assumptions, after choosing a major architecture direction, and before finalizing the plan. Keep these checkpoints concise, decision-oriented, and easy to answer.
10. **Known Unknowns Are Required**: DESIGN.md MUST contain a dedicated "## Known Unknowns & Validation Plan" section. Record uncertain assumptions, provider/framework details that need verification, product questions deferred to implementation, and the cheapest way to validate each one. Do not hide uncertainty behind confident prose.
11. **Implementation Discovery**: PLAN.md MUST contain a "## Discovery Checkpoints" section. Identify the points where the coding agent should pause, test a spike, inspect real data, or ask the user before locking in an implementation choice.
12. **Concrete Technical Depth**: Where relevant, cover API payload/response/error shapes, schema constraints and indexes, state transitions, failure and recovery behavior, security boundaries, observability, external-provider degradation, and test strategy. Mark non-applicable areas instead of inventing unnecessary architecture.
13. **Canonical Artifacts**: Treat DESIGN.md, PLAN.md, and DECISIONS.md in the DesignFlow workspace as the only canonical planning artifacts. Consolidate contradictions instead of producing parallel or duplicate design documents.
14. **Specialist Coverage Before Completion**: For a multi-agent team, consult at least three distinct, relevant specialists before completion (or every available specialist when fewer than three exist). Give each specialist a bounded technical question; do not summon agents merely to repeat the whole plan.
15. **Debate Before Agreement**: Material choices such as platform, data ownership, authentication, privacy, deployment, external providers, consistency, cost, or irreversible scope must include at least two credible options, explicit trade-offs, and a recommendation. Ask a second specialist to challenge high-impact recommendations when an appropriate specialist is available.
16. **User Confirmation On Complex Choices**: Before marking a multi-agent planning run complete, pause at least once for the user to confirm a material product or architecture choice. Ask exactly ONE decision per checkpoint with 2-3 concise options, recommend one, explain consequences, and allow a custom answer. Never bundle several unrelated questions into one pause; ask the next material question only after the user answers the current one. Do not ask the user to approve trivial implementation details.

Structured workflow description:
1. **Dynamic Summoning (Strict Needs-Based)**: You have access to a large pool of specialized experts. You MUST selectively summon them by setting ## NEXT_AGENT ONLY if their specific expertise is strictly required by the project scope. Do not summon UI agents for backend projects, or database architects for static sites, etc. If multiple competing roles are available for a required domain, force them to rigorously debate trade-offs.
2. **Debate Limits (CRITICAL)**: Do NOT debate a single sub-item for more than {max_debate_rounds} turns. Force a decision and consolidate the architecture into DESIGN.md and PLAN.md.

Available agents in the virtual company pool:
{agents_list}

Read the current workspace files carefully and decide what should happen next.

[OPTIONAL FILE UPDATES]
You may optionally update workspace files directly by including these exact headers anywhere in your response. DO NOT wrap them in markdown code blocks:

## DESIGN_UPDATE
<complete updated design document>

## PLAN_UPDATE
<complete updated plan document>

## DECISIONS_UPDATE
<complete updated decision log with the current architectural choices, trade-offs, and rationale>

## QUESTIONS
<if you need clarification from the user, write your questions here>

Respond in this EXACT format:

## SUMMON_REASON
<Why are you choosing this specific agent? What is their exact expertise needed right now?>

## EXPECTED_CONTRIBUTION
<What exactly do you expect them to output in their turn?>

## NEXT_AGENT
<exact name of the agent to run next, or USER>

## USER_SUMMARY
<2-4 sentence user-facing summary of what the team is doing right now; do not expose raw internal prompting>

## WHY_THIS_NOW
<one short paragraph on why this step matters at this moment>

## EXPECTED_OUTPUT
<what concrete artifact, answer, or refinement should come out of this step>

## NEEDS_USER_INPUT
<write NONE if no user action is needed; otherwise briefly state the exact decision, approval, or clarification needed from the user>

## INSTRUCTIONS
<specific internal instructions or guidance for this agent's turn, or your clarifying question for the user>

## DECISION_CHECKPOINT
<ONLY output this section if VERDICT is PAUSE_FOR_INPUT. Include exactly one material decision. Use this structure: a short Decision line, 2-3 options as markdown bullets like - [A] choice, - [B] choice, a Recommendation line, and a brief consequence/trade-off note. Never bundle multiple decisions in this section.>

## QUALITY_GATE
<ONLY output this section if VERDICT is COMPLETE. Verify requirement coverage, task acceptance criteria, known unknowns, discovery checkpoints, unresolved questions, contradictory decisions, risk mitigations, and valid diagrams. PASS means the planning baseline is coherent enough to begin implementation; it does not mean implementation discovery is finished. Output PASS or FAIL.>

## VERDICT
<CONTINUE, COMPLETE, or PAUSE_FOR_INPUT>
(Note: When a coherent planning baseline is ready, set VERDICT to COMPLETE. This means ready to begin iterative implementation, not final specification certainty. If you need user clarification or approval on a major decision, set VERDICT to PAUSE_FOR_INPUT.)"""

SYNTHESIS_SYSTEM = """You are DesignFlow's senior architecture synthesizer. Python controls workflow and routing; do not select agents or narrate orchestration. Convert the product goal, repository context, user decisions, and specialist critiques into coherent canonical planning artifacts. Be concrete about interfaces, data, failure recovery, security, observability, testing, known unknowns, and implementation discovery. Preserve valid existing decisions, resolve contradictions explicitly, and never invent executable code or claim the plan is a final specification."""


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
        store: Optional[Any] = None,
    ):
        self.agents = agents
        self.ws = workspace
        self.store = store
        self._cb = event_cb
        self.max_debate_rounds = max_debate_rounds
        self.max_tokens = max_tokens
        self.max_build_iterations = max_build_iterations
        self.require_approval = require_approval
        self.mode = mode
        self.restore = restore

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
        self._pending_user_input = ""
        self.idea = ""
        self._state_loaded = False

    # ── Public controls ───────────────────────────────────────────────────────

    def pause(self):
        self._paused = True
        self._pause_event.clear()

    def resume(self):
        self._paused = False
        self._pause_event.set()

    async def steer(self, message: str):
        """Inject a human message that all agents will see next turn."""
        pending_question = self.ws.read("questions").strip()
        event_kind = "user_decision" if pending_question and pending_question != "(empty)" else "user_steering"
        self.ws.add_context_event(event_kind, message, self.phase.value, "user")
        if pending_question and pending_question != "(empty)":
            self._user_checkpoint_count += 1
        await self._steer_queue.put(message)
        self.ws.clear_questions()
        self.ws.reset_context_tracking()
        self._context_invocations.clear()
        self._emit(Event(EventKind.STEER, agent="human", data={"message": message}))

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
        if not self._failed_turn:
            return None
        return {key: value for key, value in self._failed_turn.items() if key != "prompt"}

    def retry_failed_turn(self):
        if not self._failed_turn:
            raise ValueError("There is no failed turn to retry")
        self._paused = False
        self._pause_event.set()
        self._recovery_event.set()

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

    async def run(self, idea: str):
        self._running = True
        self.idea = idea
        if self.restore and not self._state_loaded:
            self._state_loaded = self.load_state()
            if not self._state_loaded:
                # A new goal must not inherit an unresolved checkpoint from an
                # older or legacy run in the same project.
                self.ws.clear_questions()

        local_response = self._local_command_response(idea)
        if local_response:
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

        n = len(self.agents)
        if n == 0:
            return self.ws.snapshot()

        # Python owns routing. The strongest available model is used only for
        # quality-critical synthesis, regardless of which agent is marked manager.
        coordinator = max(self.agents, key=self._synthesis_score)
        self._coordinator_name = coordinator.name
        coordinator.config.max_history_turns = min(coordinator.config.max_history_turns, 6)

        # Parse explicit @mentions first
        target_agent = None
        prompt_text = idea
        match = re.search(r'@(\w+)', idea)
        if match:
            mention = match.group(1).lower()
            names = {agent.name.lower(): agent for agent in self.agents}
            matched_name = mention if mention in names else next(iter(get_close_matches(mention, names, n=1, cutoff=0.82)), "")
            if matched_name:
                target_agent = names[matched_name]
                prompt_text = re.sub(rf'@{match.group(1)}\s*', '', idea, flags=re.IGNORECASE).strip()

        idea_lower = idea.lower()
        debate_terms = ("debate", "discuss", "discussion", "project", "loop", "run debate")
        words = re.findall(r"[a-z]+", idea_lower)
        fuzzy_debate = any(get_close_matches(word, debate_terms[:3], n=1, cutoff=0.84) for word in words)
        explicit_debate = any(k in idea_lower for k in debate_terms) or fuzzy_debate or os.environ.get("DESIGNFLOW_TEST") == "1"

        # Default to continuous multi-agent loop if we have a coordinator, UNLESS they @mentioned a specific agent.
        is_direct = target_agent is not None or (not explicit_debate and not coordinator)

        if is_direct:
            if not target_agent:
                target_agent = coordinator or self.agents[0]

            self._emit(Event(EventKind.PHASE, data={
                "phase": "direct_chat", "status": f"Direct chat with {target_agent.name}"
            }))

            turn_context = {"step": 1, "phase": "direct_chat", "standing_role": target_agent.config.role}
            turn_id = self._begin_turn(target_agent, turn_context)

            snapshot = self.ws.snapshot()
            prompt = (
                f"Conversational Turn.\n"
                f"User Prompt: {prompt_text}\n"
                f"Workspace context (if any):\n{snapshot}\n"
            )

            response = await self._send_agent(target_agent, prompt, turn_id, turn_context)
            self._record_turn_usage(target_agent, "direct")
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

    # ── State Machine ─────────────────────────────────────────────────────────

    def _select_peer_review_agents(self) -> list[AgentBase]:
        """Choose a small, relevant, domain-diverse review panel without an LLM call."""
        candidates = [agent for agent in self.agents if agent.name != self._coordinator_name]
        if not candidates:
            return []
        words = set(re.findall(r"[a-z0-9]+", f"{self.idea} {self.ws.brief()}".lower()))
        scored: list[tuple[int, str, AgentBase]] = []
        for agent in candidates:
            identity = f"{agent.name} {agent.config.role}".lower()
            score = sum(3 for word in words if len(word) > 3 and word in identity)
            domains = []
            for domain, (signals, names) in SPECIALIST_SIGNALS.items():
                if agent.name.lower() in names and words.intersection(signals):
                    score += 6 + len(words.intersection(signals))
                    domains.append(domain)
            if agent.name.lower() == "researcher" and self.ws.read_src():
                score += 5
                domains.append("research")
            if agent.name.lower().startswith("architect_"):
                score += 2
                domains.append("architecture")
            if agent.name.lower() == "product_manager":
                score += 1
                domains.append("product")
            scored.append((score, domains[0] if domains else agent.name.lower(), agent))

        selected: list[AgentBase] = []
        used_domains: set[str] = set()
        for score, domain, agent in sorted(scored, key=lambda item: (-item[0], item[2].name)):
            if score <= 0 or domain in used_domains:
                continue
            selected.append(agent)
            used_domains.add(domain)
            if len(selected) >= 4:
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
        """Ask one high-value question only when the seed is genuinely underspecified."""
        existing_design = self.ws.read("design")
        existing_plan = self.ws.read("plan")
        brief = self.ws.brief()
        context = "\n".join((self.idea, brief, existing_design, existing_plan))
        seed_words = set(re.findall(r"[a-z0-9]+", self.idea.lower()))
        words = set(re.findall(r"[a-z0-9]+", context.lower()))
        # Existing substantive artifacts already establish the product context;
        # discovery should refine them instead of asking a generic seed question.
        has_product_context = (
            len(brief.strip()) >= 80
            or len(existing_design.replace("(empty)", "").strip()) >= 300
            or len(existing_plan.replace("(empty)", "").strip()) >= 200
        )
        if len(seed_words) < 6 and not has_product_context:
            return "Who is the primary user, and what outcome must they achieve with this product?"
        if words.intersection({"enterprise", "team", "tenant", "organization"}) and not words.intersection({"admin", "member", "role", "permission", "sso"}):
            return "What user roles and access boundaries must the first version support?"
        if words.intersection({"payment", "health", "medical", "finance", "bank", "identity"}) and not words.intersection({"compliance", "privacy", "region", "retention"}):
            return "Are there mandatory compliance, data residency, or retention constraints?"
        return ""

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
                self._emit(Event(EventKind.PHASE, data={"phase": "coordinator", "status": "complete", "step": step}))
                if self.store:
                    self.store.clear_run_state()
                break
        else:
            raise RuntimeError("Planning workflow exceeded its deterministic step limit.")

    async def _run_discovery_phase(self, coordinator, step):
        self._emit(Event(EventKind.PHASE, data={"phase": "discovery", "status": "running", "step": step}))
        question = self._deterministic_discovery_question()
        if question and self.require_approval:
            self.ws.write("questions", f"# Clarifying Question\n\n{question}")
            self._emit(Event(EventKind.FILE_WRITE, agent=coordinator.name, data={"file": "QUESTIONS.md"}))
            self.post_approval_phase = OrchestratorPhase.DRAFTING
            self.phase = OrchestratorPhase.APPROVAL
        else:
            self.phase = OrchestratorPhase.DRAFTING
        self.save_state()

    async def _run_drafting_phase(self, coordinator, step):
        self._emit(Event(EventKind.PHASE, data={"phase": "drafting", "status": "running", "step": step}))
        new_steering = "\n".join(filter(None, (self._pending_user_input, await self._drain_steer())))
        self._pending_user_input = ""
        steer_block = f"\n\n[HUMAN STEERING]\n{new_steering}" if new_steering else ""

        prompt = (
            f"Product Idea: {self.idea}\n{steer_block}\n"
            f"Draft the initial architecture and implementation plan.\n"
            f"Output `## DESIGN_UPDATE` and `## PLAN_UPDATE` with your complete initial drafts."
        )
        full_ctx = self._agent_context(coordinator)
        response = await self._send_agent_basic(coordinator, prompt, "drafting", step, ephemeral=full_ctx, synthesis=True)
        self._apply_coordinator_agent_response(coordinator.name, response)
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

        prompt = (
            f"You are the {agent.config.role or 'Specialist'}.\n"
            f"Read the current DESIGN.md and PLAN.md from the workspace.\n"
            f"Provide your specialized critique and alternative suggestions.\n{steer_block}\n"
            f"Output `## DESIGN_APPEND` or `## PLAN_APPEND` if you want to add notes directly, or just provide text."
        )

        full_ctx = self._agent_context(agent)
        response = await self._send_agent_basic(agent, prompt, "peer_review", step, ephemeral=full_ctx)
        self.ws.append("logbook", response, agent.name, "Peer review critique")
        self.ws.add_context_event("peer_critique", response, "refinement", agent.name)
        self._apply_coordinator_agent_response(agent.name, response)
        self._consulted_specialists.add(agent.name)
        self.peer_review_index += 1
        self.save_state()

    async def _run_refinement_phase(self, coordinator, step):
        self._emit(Event(EventKind.PHASE, data={"phase": "refinement", "status": "running", "step": step}))
        new_steering = "\n".join(filter(None, (self._pending_user_input, await self._drain_steer())))
        self._pending_user_input = ""
        steer_block = f"\n\n[HUMAN STEERING]\n{new_steering}" if new_steering else ""

        prompt = (
            f"Use the complete unresolved peer critiques in CONTEXT.md and update DESIGN.md and PLAN.md.\n{steer_block}\n"
            f"Output `## DESIGN_UPDATE` and `## PLAN_UPDATE` and `## DECISIONS_UPDATE`.\n"
            f"If there are major unresolved decisions, output `## DECISION_CHECKPOINT`."
        )
        if self._deterministic_feedback:
            prompt += f"\n\nDeterministic quality checks that must be fixed:\n{self._deterministic_feedback}"
        full_ctx = self._agent_context(coordinator)
        response = await self._send_agent_basic(coordinator, prompt, "refinement", step, ephemeral=full_ctx, synthesis=True)
        self._apply_coordinator_agent_response(coordinator.name, response)
        self.ws.resolve_context_events({"peer_critique", "user_steering", "user_decision", "quality_failure"})
        self._refinement_attempts += 1

        decision = self.ws.parse_section(response, "DECISION_CHECKPOINT").strip()
        if self._is_none_text(decision):
            decision = ""

        if decision:
            self.ws.write("questions", f"# Decision Checkpoint\n\n{decision}")
            self._emit(Event(EventKind.FILE_WRITE, agent=coordinator.name, data={"file": "QUESTIONS.md"}))
            self.post_approval_phase = OrchestratorPhase.COMPLETE
            self.phase = OrchestratorPhase.APPROVAL
        else:
            errors = self._coordinator_completion_errors("PASS")
            if errors and self._refinement_attempts < 3:
                self._deterministic_feedback = "\n".join(f"- {error}" for error in errors)
                self.ws.add_context_event(
                    "quality_failure", self._deterministic_feedback, "refinement", "deterministic_quality_gate"
                )
                self.phase = OrchestratorPhase.REFINEMENT
            elif errors and self.require_approval and any("user decision" in error for error in errors):
                checkpoint = (
                    "Decision: Is this planning baseline ready to hand to a coding agent?\n\n"
                    "- [A] Approve the baseline and continue with implementation discovery\n"
                    "- [B] Request another focused refinement pass\n\n"
                    "Recommendation: A — proceed while treating Known Unknowns and Discovery Checkpoints as required follow-up work."
                )
                self.ws.write("questions", f"# Decision Checkpoint\n\n{checkpoint}")
                self._emit(Event(EventKind.FILE_WRITE, agent=coordinator.name, data={"file": "QUESTIONS.md"}))
                self.post_approval_phase = OrchestratorPhase.COMPLETE
                self.phase = OrchestratorPhase.APPROVAL
            else:
                self.phase = OrchestratorPhase.COMPLETE
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
            self._pending_user_input = new_steering
        self._user_checkpoint_count += 1
        self.phase = self.post_approval_phase or OrchestratorPhase.COMPLETE
        self.post_approval_phase = None
        self.save_state()

    async def _send_agent_basic(self, agent, prompt, phase_name, step, ephemeral="", synthesis=False):
        turn_context = {"step": step, "phase": phase_name, "standing_role": agent.config.role}
        turn_id = self._begin_turn(agent, turn_context)

        response = await self._send_agent(
            agent, prompt, turn_id, turn_context, ephemeral_context=ephemeral,
            system_override=SYNTHESIS_SYSTEM if synthesis else None,
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
            "idea": self.idea,
            "mode": self.mode,
            "turn_sequence": self._turn_sequence,
            "run_token_total": self.run_token_total,
            "phase_usage": self.phase_usage,
            "phase": self.phase.value,
            "peer_review_index": self.peer_review_index,
            "post_approval_phase": self.post_approval_phase.value if self.post_approval_phase else None,
            "selected_peer_names": self._selected_peer_names,
            "refinement_attempts": self._refinement_attempts,
            "deterministic_feedback": self._deterministic_feedback,
            "pending_user_input": self._pending_user_input,
            "consulted_specialists": sorted(self._consulted_specialists),
            "user_checkpoint_count": self._user_checkpoint_count,
            "artifact_fingerprints": self.ws.artifact_fingerprints(),
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
                if saved_fingerprints and saved_fingerprints != self.ws.artifact_fingerprints():
                    return False
                self.mode = state.get("mode", self.mode)
                self._turn_sequence = state.get("turn_sequence", 0)
                self.run_token_total = int(state.get("run_token_total", 0) or 0)
                self.phase_usage = dict(state.get("phase_usage", {}))
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

                agent_states = state.get("agents", {})
                for a in self.agents:
                    if a.name in agent_states:
                        a.history = []
                        for m in agent_states[a.name]:
                            usage = Usage(**m.get("usage", {}))
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
            if saved_fingerprints and saved_fingerprints != self.ws.artifact_fingerprints():
                return False
            self.mode = state.get("mode", self.mode)
            self._turn_sequence = state.get("turn_sequence", 0)
            self.run_token_total = int(state.get("run_token_total", 0) or 0)
            self.phase_usage = dict(state.get("phase_usage", {}))
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

            agent_states = state.get("agents", {})
            for a in self.agents:
                if a.name in agent_states:
                    a.history = []
                    for m in agent_states[a.name]:
                        usage = Usage(**m.get("usage", {}))
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
        }

    def _begin_turn(self, agent: AgentBase, context: dict) -> str:
        self._turn_sequence += 1
        turn_id = f"turn-{self._turn_sequence:04d}"
        self._turn_attempts[turn_id] = 1
        self._emit(Event(EventKind.TURN_START, agent=agent.name, data={
            "turn_id": turn_id, "attempt": 1, **self._event_actor_meta(agent), **context,
        }))
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
        return identity

    async def _send_agent(
        self,
        agent: AgentBase,
        prompt: str,
        turn_id: str = "turn-manual",
        turn_context: Optional[dict] = None,
        ephemeral_context: Optional[str] = None,
        system_override: Optional[str] = None,
    ) -> str:
        attempt = self._turn_attempts.get(turn_id, 1)
        self._turn_attempts[turn_id] = attempt
        context = dict(turn_context or {})
        max_retries = int(agent.config.extra.get("rate_limit_max_retries", 0) or 0)
        while self._running:
            try:
                def call_mcp_tool(name: str, args: dict) -> str:
                    # Execute async MCP tool call in the main thread's event loop
                    if not self.mcp_manager:
                        raise RuntimeError("MCP Manager not initialized")
                    future = asyncio.run_coroutine_threadsafe(
                        self.mcp_manager.call_tool(name, args),
                        asyncio.get_running_loop()
                    )
                    return future.result()

                response = await asyncio.to_thread(
                    agent.send, prompt, system_override or self._agent_system(agent), ephemeral_context,
                    mcp_tools=self.mcp_tools, tool_handler=call_mcp_tool
                )
                self._failed_turn = None
                return response
            except RuntimeError as exc:
                if not self._is_rate_limit(exc):
                    agent.mark_error(str(exc))
                    public_error, error_code = self._public_error(exc)
                    self._failed_turn = {
                        "turn_id": turn_id,
                        "attempt": attempt,
                        "agent_id": agent.config.id,
                        "agent": agent.name,
                        "error": str(exc),
                        "prompt": prompt,
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
                        "message": "Fix this agent's configuration, save it, then retry the failed turn.",
                    }))
                    await self._recovery_event.wait()
                    if not self._running:
                        raise asyncio.CancelledError()
                    # A user may have paused the failed provider and substituted
                    # this logical specialist with another model. Resolve it only
                    # after explicit recovery, at this safe turn boundary.
                    agent = next(
                        (candidate for candidate in self.agents if candidate.config.id == self._failed_turn["agent_id"]),
                        agent,
                    )
                    attempt += 1
                    self._turn_attempts[turn_id] = attempt
                    agent.error_message = ""
                    self._emit(Event(EventKind.TURN_START, agent=agent.name, data={
                        "turn_id": turn_id, "attempt": attempt, "resumed": True,
                        "retry_reason": "manual_recovery", **context,
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
                        "retry_reason": "usage_limit", **context,
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
        roles = ["context", "design", "plan", "decisions", "questions", "src_index"]
        if self._should_force_full_context(agent_name):
            context = self.ws.scoped_context(roles)
        else:
            context = self.ws.changed_context(agent_name, roles)
        self._remember_context_delivery(agent_name)
        return context

    def _agent_context(self, agent: AgentBase) -> str:
        # CONTEXT.md carries cross-domain memory. Specialists receive at most two
        # authoritative domain artifacts to avoid repeating the whole workspace.
        domain_roles = [role for role in self._role_context_needs(agent.name) if role != "questions"][:2]
        roles = ["context"] + domain_roles
        if self._should_force_full_context(agent.name):
            context = self.ws.scoped_context(roles)
        else:
            context = self.ws.changed_context(agent.name, roles)
        self._remember_context_delivery(agent.name)
        return context

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
        specialists = [agent for agent in self.agents if agent.name != self._coordinator_name]
        required_specialists = min(3, len(specialists))
        if len(self._consulted_specialists) < required_specialists:
            missing = required_specialists - len(self._consulted_specialists)
            errors.append(
                f"Consult {missing} more distinct relevant specialist(s) before completion "
                f"({len(self._consulted_specialists)}/{required_specialists} consulted)."
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

    def _apply_coordinator_agent_response(self, agent_name: str, response: str):
        for filename, content in self.ws.parse_files(response).items():
            self.ws.write_src(filename, content)
            self._emit(Event(EventKind.FILE_WRITE, agent=agent_name,
                             data={"file": filename, "preview": content[:120]}))

        plan_update = self.ws.parse_section(response, "PLAN_UPDATE")
        if plan_update:
            self.ws.write("plan", f"# Plan\n\n{plan_update}")
            self._emit(Event(EventKind.FILE_WRITE, agent=agent_name, data={"file": "PLAN.md"}))

        decisions_update = self.ws.parse_section(response, "DECISIONS_UPDATE")
        if decisions_update:
            self.ws.write("decisions", f"# Key Decisions\n\n{decisions_update}")
            self._emit(Event(EventKind.FILE_WRITE, agent=agent_name, data={"file": "DECISIONS.md"}))

        design_update = self.ws.parse_section(response, "DESIGN_UPDATE")
        if design_update:
            self.ws.write("design", f"# Architecture Design\n\n{design_update}")
            self._emit(Event(EventKind.FILE_WRITE, agent=agent_name, data={"file": "DESIGN.md"}))

        design_bit = self.ws.parse_section(response, "DESIGN_APPEND")
        if design_bit:
            self.ws.append("design", design_bit, agent_name, "Coordinator-led Turn")
            self._emit(Event(EventKind.FILE_WRITE, agent=agent_name, data={"file": "DESIGN.md"}))

        plan_bit = self.ws.parse_section(response, "PLAN_APPEND")
        if plan_bit:
            self.ws.append("plan", plan_bit, agent_name, "Peer Review")
            self._emit(Event(EventKind.FILE_WRITE, agent=agent_name, data={"file": "PLAN.md"}))

        decisions_bit = self.ws.parse_section(response, "DECISIONS_APPEND")
        if decisions_bit:
            self.ws.append("decisions", decisions_bit, agent_name, "Peer Review")
            self._emit(Event(EventKind.FILE_WRITE, agent=agent_name, data={"file": "DECISIONS.md"}))
