"""
Orchestrator — runs debate + build phases.
All state changes emit events via an async queue so the UI gets live updates.
Human steering: pause, inject a message into the debate, swap agent roles.
"""

from __future__ import annotations
import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
from typing import Any, Callable, Optional
from backend.mcp_client import MCPManager

from .agents.base import AgentBase, Message, Usage
from .workspace.workspace import Workspace


# ─── Events ──────────────────────────────────────────────────────────────────

class EventKind(str, Enum):
    PHASE       = "phase"        # phase change
    TURN_START  = "turn_start"   # agent about to speak
    TURN_END    = "turn_end"     # agent finished, includes response
    VOTE        = "vote"         # consensus vote cast
    VERDICT     = "verdict"      # reviewer/tester verdict
    CONSENSUS   = "consensus"    # all agreed
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

Architecture Diagrams: Intelligently evaluate if the architecture is complex enough to warrant a visual diagram. If it is, you MUST include a rich, stylized visual Mermaid.js flowchart of your proposed component connections inside your DESIGN_UPDATE block using standard ```mermaid ... ``` code fences. Use rich Mermaid features (like styling nodes and grouping).

Respond in this exact format:

## DESIGN_UPDATE
<your complete, updated architecture design — proposal, critique, refinement, and user-experience evaluation. Include mermaid diagrams if proposing complex architecture. This will OVERWRITE the previous design, so ensure it is comprehensive.>

## PLAN_UPDATE
<complete updated content of PLAN.md — use nested tree-structures (e.g. nested lists) for sub-tasks>

## CONSENSUS_APPEND
<your reasoning this round>
VOTE: AGREE
or
VOTE: DISAGREE
<one sentence reason>

Only VOTE: AGREE when you genuinely believe the design is solid and complete."""

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

    "tester": """You are the TESTER this iteration.
Read the code and evaluate it against the plan. Write test cases, describe expected
vs actual behavior, and record results.

Respond in this exact format:

## TEST_RESULTS_APPEND
<your test run: list test cases, results, failures>

## PLAN_UPDATE
<updated PLAN.md — check off tested items, add bug tasks if needed>

## VERDICT
PASS
or
FAIL
<specific failures and what needs to change>""",
}

ROLE_NEEDS = {
    "architect_alpha": ["design", "plan", "decisions", "questions"],
    "architect_beta": ["design", "plan", "decisions", "questions"],
    "ux_simplifier": ["design", "plan", "decisions", "questions"],
    "product_manager": ["design", "plan", "decisions", "questions"],
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

SPECIALIZED_PERSONAS = {
    "architect_alpha": "You are ARCHITECT ALPHA. Propose robust, scalable system designs. Vigorously debate competing designs and highlight their flaws while defending your own.",
    "architect_beta": "You are ARCHITECT BETA. Propose alternative, highly-optimized system designs. Challenge Architect Alpha's assumptions and fight for a superior approach.",
    "researcher": "You are the RESEARCHER. Read existing workspace files to ground the debate in reality. Fact-check the Architects to ensure they do not hallucinate APIs or codebase assumptions.",
    "red_team": "You are the RED TEAM agent. Your sole purpose is to hunt for edge-cases, race conditions, security vulnerabilities, and ways to break the proposed design.",
    "ux_simplifier": "You are the UX SIMPLIFIER. You fiercely advocate for the external user. You must aggressively fight to simplify complex UI flows, remove unnecessary features, and ensure the system is intuitive.",
    "cloud_architect": "You are the CLOUD ARCHITECT. Focus strictly on scalability, database indexing, infrastructure bottlenecks, and deployment environments.",
    "product_manager": "You are the PRODUCT MANAGER. You enforce MVP constraints and fight YAGNI (You Aren't Gonna Need It). You ensure the team is only building what is strictly necessary to validate the idea and get to market fast.",
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
Your SOLE goal is to coordinate the team's agents to architect a system and produce a comprehensive DESIGN.md, a crisp PLAN.md implementation checklist, and a DECISIONS.md ledger of the key choices based on the user's idea. 
CRITICAL: You MUST NOT write any executable code (e.g. .py, .js, .html). Your output will be fed to a separate coding agent.
Your debate depth limit is: {max_debate_rounds} rounds.

Guidelines for Design & Architectural Gathering:
1. **Context-Aware Usability First**: IF the project involves a user interface, frontend, or human interaction, you MUST explicitly map out the user journey, UI flows, and UX interactions before diving into backend architectures. Prioritize summoning ux_simplifier or product_manager early in these cases. If the project is purely backend, CLI, or API-driven, skip UI/UX design and focus directly on architecture and data models.
2. **User-Centric Simplification**: Evaluate all designs from an external user's perspective. Simplify complex UI/UX.
3. **API Contract (CRITICAL)**: If a frontend and backend are involved, you MUST establish a firm API contract. Document all required API endpoints (methods, paths, payloads, responses) clearly in a dedicated section in DESIGN.md so the UI and Backend can be developed independently.
4. **Data Models & Database**: Document the schema layout, tables/collections, and relationships.
5. **Scalability Analysis**: Dedicate a "## Scalability, Bottlenecks & Design Choices" section in DESIGN.md.
6. **Architecture Diagrams**: Intelligently evaluate if the architecture warrants a visual diagram. If it does, ensure the agents include a rich, stylized Mermaid.js flowchart in DESIGN.md.
7. **Plan Structure (CRITICAL)**: PLAN.md MUST be a clean, crisp, end-to-end implementation checklist that we will feed to a separate coding agent. It should contain clear chronologically-ordered implementation phases and checkable tasks. Do NOT include debate transcripts in PLAN.md or DESIGN.md.
8. **Planned User Checkpoints**: Actively involve the user at three moments when useful: after framing assumptions, after choosing a major architecture direction, and before finalizing the plan. Keep these checkpoints concise, decision-oriented, and easy to answer.

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
<ONLY output this section if VERDICT is PAUSE_FOR_INPUT. Use this exact structure when possible: a short Decision line, 2-3 options as markdown bullets like - [A] choice, - [B] choice, a Recommendation line, and a brief consequence/trade-off note for each option.>

## QUALITY_GATE
<ONLY output this section if VERDICT is COMPLETE. Verify requirement coverage, task acceptance criteria, unresolved questions, contradictory decisions, risk mitigations, and valid diagrams. Output PASS or FAIL.>

## VERDICT
<CONTINUE, COMPLETE, or PAUSE_FOR_INPUT>
(Note: When the design and plan are finalized, set VERDICT to COMPLETE. If you need user clarification or approval on a major decision, set VERDICT to PAUSE_FOR_INPUT.)"""


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
    ):
        self.agents = agents
        self.ws = workspace
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
        self._budget_exhausted = False
        self._coordinator_name = ""
        self._context_invocations: dict[str, int] = {}
        self._context_full_refresh_gap = 8
        self._context_full_refresh_every = 4

        if self.restore:
            self.load_state()

    # ── Public controls ───────────────────────────────────────────────────────

    def pause(self):
        self._paused = True
        self._pause_event.clear()

    def resume(self):
        self._paused = False
        self._pause_event.set()

    async def steer(self, message: str):
        """Inject a human message that all agents will see next turn."""
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

    async def run(self, idea: str):
        self._running = True
        self.idea = idea
        
        # Load and start MCP servers if present
        if hasattr(self.ws, "store") and self.ws.store:
            mcp_configs = self.ws.store.get_mcp_servers()
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

        coordinator = next(
            (a for a in self.agents if "coordinator" in (a.config.role or "").lower() or "coordinator" in (a.config.name or "").lower() or a.config.extra.get("is_coordinator")),
            None
        )
        self._coordinator_name = coordinator.name if coordinator else ""

        if not coordinator and not os.environ.get("AGENTFLOW_TEST"):
            # Pick the best model as the coordinator
            def get_model_score(agent: AgentBase) -> int:
                kind = (agent.config.kind or "").lower()
                model = (agent.config.model or "").lower()
                if kind == "claude":
                    if "opus" in model:
                        return 100
                    if "sonnet" in model:
                        return 95
                    return 85
                elif kind == "openai":
                    if "o1" in model or "o3" in model:
                        return 98
                    if "gpt-4" in model or "o1-mini" in model:
                        return 90
                    return 80
                elif kind == "gemini":
                    if "pro" in model:
                        return 88
                    if "flash" in model:
                        return 78
                    return 70
                elif kind == "ollama":
                    return 50
                elif kind == "cli":
                    return 10
                return 0

            coordinator = max(self.agents, key=get_model_score)
            self._coordinator_name = coordinator.name

        # Parse explicit @mentions first
        target_agent = None
        prompt_text = idea
        import re
        match = re.search(r'@(\w+)', idea)
        if match:
            mention = match.group(1).lower()
            for agent in self.agents:
                if agent.name.lower() == mention:
                    target_agent = agent
                    prompt_text = re.sub(rf'@{match.group(1)}\s*', '', idea, flags=re.IGNORECASE).strip()
                    break

        idea_lower = idea.lower()
        explicit_debate = any(k in idea_lower for k in ["debate", "discuss", "discussion", "project", "loop", "run debate"]) or os.environ.get("AGENTFLOW_TEST") == "1"
        
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
            self._record_turn_usage(target_agent)
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

        if coordinator:
            await self._coordinator_loop(coordinator)
            return self.ws.snapshot()

        # If mode is "all" or "debate", run debate phase
        if self.mode in {"all", "debate"}:
            while self._running:
                # The first useful turn carries the seed. This avoids one full model
                # generation per agent whose only purpose used to be "wait".
                await self._debate_phase(n)
                if not self._running:
                    return

                if self.require_approval:
                    self.pause()
                    self._emit(Event(EventKind.PHASE, data={"phase": "debate", "status": "waiting_for_approval"}))
                    await self._wait_if_paused()
                    if not self._running:
                        return

                    if not self._steer_queue.empty():
                        self._emit(Event(EventKind.PHASE, data={"phase": "debate", "status": "continuing_debate"}))
                        continue

                break

        return self.ws.snapshot()

    # ── Debate phase ──────────────────────────────────────────────────────────

    async def _debate_phase(self, n: int):
        self._emit(Event(EventKind.PHASE, data={"phase": "debate"}))

        for round_num in range(1, self.max_debate_rounds + 1):
            await self._wait_if_paused()
            if not self._running:
                return

            votes: dict[str, str] = {}
            steering_accumulated = ""

            for agent in self.agents:
                await self._wait_if_paused()
                if not self._running:
                    return
                new_steering = await self._drain_steer()
                if new_steering:
                    if steering_accumulated:
                        steering_accumulated += "\n" + new_steering
                    else:
                        steering_accumulated = new_steering

                full_ctx = self._agent_context(agent)
                steer_block = f"\n\n[HUMAN STEERING]\n{steering_accumulated}" if steering_accumulated else ""
                seed_block = f"\n\nProduct idea: {self.idea}" if round_num == 1 else ""
                prompt = (
                    f"{DEBATE_SYSTEM.format(n=len(self.agents))}"
                    f"{seed_block}\n\n"
                    f"Debate round {round_num}/{self.max_debate_rounds}.\n\n"
                    f"{steer_block}\n\n"
                    "Add your contributions now."
                )
                ephemeral = f"Current Workspace snapshot:\n{full_ctx}"

                turn_context = {"round": round_num, "phase": "debate",
                                "standing_role": agent.config.role}
                turn_id = self._begin_turn(agent, turn_context)

                response = await self._send_agent(
                    agent, prompt, turn_id, turn_context, ephemeral_context=ephemeral
                )
                self._record_turn_usage(agent)

                self._apply_debate_response(agent.name, round_num, response)
                vote = self.ws.parse_vote(response)
                votes[agent.name] = vote

                self.ws.append("logbook", response, agent.name, "Turn completed")
                self._emit(Event(EventKind.TURN_END, agent=agent.name, data={
                    "turn_id": turn_id, "attempt": self._turn_attempts[turn_id],
                    "round": round_num, "response": response,
                    **self._usage_event(agent),
                }))
                if await self._enforce_token_budget("debate", {"round": round_num, "agent": agent.name}):
                    return
                self._emit(Event(EventKind.VOTE, agent=agent.name,
                                 data={"vote": vote, "round": round_num}))

            if all(v == "AGREE" for v in votes.values()):
                self._emit(Event(EventKind.CONSENSUS, data={
                    "round": round_num, "votes": votes
                }))
                return

            disagree = [a for a, v in votes.items() if v == "DISAGREE"]
            self._emit(Event(EventKind.PHASE, data={
                "phase": "debate", "round": round_num,
                "status": f"no consensus — {disagree} disagree"
            }))

            if self.require_approval and round_num % 2 == 0 and round_num < self.max_debate_rounds:
                self.pause()
                self._emit(Event(EventKind.PHASE, data={
                    "phase": "debate", "round": round_num,
                    "status": "waiting_for_continuation"
                }))
                await self._wait_if_paused()
                if not self._running:
                    return

        # Max rounds: proceed anyway
        self._emit(Event(EventKind.CONSENSUS, data={"forced": True, "votes": {}}))

    def _apply_debate_response(self, agent: str, round_num: int, response: str):
        design_bit = self.ws.parse_section(response, "DESIGN_APPEND")
        plan_update = self.ws.parse_section(response, "PLAN_UPDATE")
        consensus_bit = self.ws.parse_section(response, "CONSENSUS_APPEND")

        if design_bit:
            self.ws.append("design", design_bit, agent, f"Round {round_num}")
            self._emit(Event(EventKind.FILE_WRITE, agent=agent,
                             data={"file": "DESIGN.md", "preview": design_bit[:120]}))
        if plan_update:
            self.ws.write("plan", f"# Plan\n\n{plan_update}")
            self._emit(Event(EventKind.FILE_WRITE, agent=agent,
                             data={"file": "PLAN.md", "preview": plan_update[:120]}))
        if consensus_bit:
            self.ws.append("consensus", consensus_bit, agent, f"Round {round_num}")



    # ── Helpers ───────────────────────────────────────────────────────────────

    def save_state(self):
        state = {
            "mode": self.mode,
            "turn_sequence": self._turn_sequence,
            "run_token_total": self.run_token_total,
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
        try:
            (self.ws.root / "run_state.json").write_text(json.dumps(state, indent=2))
        except Exception:
            pass

    def load_state(self):
        state_file = self.ws.root / "run_state.json"
        if not state_file.exists():
            return False
        try:
            state = json.loads(state_file.read_text())
            self.mode = state.get("mode", self.mode)
            self._turn_sequence = state.get("turn_sequence", 0)
            self.run_token_total = int(state.get("run_token_total", 0) or 0)
            
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
                    agent.send, prompt, self._agent_system(agent), ephemeral_context,
                    mcp_tools=self.mcp_tools, tool_handler=call_mcp_tool
                )
                self._failed_turn = None
                return response
            except RuntimeError as exc:
                if not self._is_rate_limit(exc):
                    agent.mark_error(str(exc))
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
                        **self.failed_turn,
                        "recoverable": True,
                        "message": "Fix this agent's configuration, save it, then retry the failed turn.",
                    }))
                    await self._recovery_event.wait()
                    if not self._running:
                        raise asyncio.CancelledError()
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

    def _record_turn_usage(self, agent: AgentBase):
        self.run_token_total += agent.last_usage.total_tokens

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
        roles = ["design", "plan", "decisions", "questions", "src_index"]
        if self._should_force_full_context(agent_name):
            context = self.ws.scoped_context(roles)
        else:
            context = self.ws.changed_context(agent_name, roles)
        self._remember_context_delivery(agent_name)
        return context

    def _agent_context(self, agent: AgentBase) -> str:
        roles = self._role_context_needs(agent.name)
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
        return errors

    async def _coordinator_loop(self, coordinator: AgentBase):
        other_agents = [a for a in self.agents if a.name != coordinator.name]
        agents_list = "\n".join(f"- {a.name}: {a.config.role or 'Contributor'}" for a in other_agents)
        system_prompt = COORDINATOR_SYSTEM.format(agents_list=agents_list, mode=self.mode, max_debate_rounds=self.max_debate_rounds)

        orig_system = coordinator.config.system_prompt
        coordinator.config.system_prompt = system_prompt

        max_steps = max(10, self.max_debate_rounds * 2)
        previous_summon = None
        consecutive_loops = 0
        for step in range(1, max_steps + 1):
            await self._wait_if_paused()
            if not self._running:
                break

            # 1. Call Coordinator
            delta = self._coordinator_context()
            new_steering = await self._drain_steer()
            steer_block = f"\n\n[HUMAN STEERING]\n{new_steering}" if new_steering else ""
            seed_block = f"\n\nProduct idea: {self.idea}" if step == 1 else ""
            agent_feedback_block = f"\n\n[LAST AGENT REPLY]\n{self._last_agent_feedback}" if getattr(self, "_last_agent_feedback", None) else ""

            prompt = (
                f"Coordinator execution step {step}/{max_steps}.\n"
                f"{steer_block}{seed_block}{agent_feedback_block}\n\n"
                "Determine the NEXT_AGENT to run, provide both internal instructions and user-facing summary fields, and state the VERDICT (CONTINUE or COMPLETE)."
            )
            ephemeral = f"Current Workspace snapshot:\n{delta}"

            turn_context = {"step": step, "phase": "coordinator", "standing_role": coordinator.config.role}
            turn_id = self._begin_turn(coordinator, turn_context)

            response = await self._send_agent(
                coordinator, prompt, turn_id, turn_context, ephemeral_context=ephemeral
            )
            self._record_turn_usage(coordinator)

            # Apply coordinator's own plan/design updates or file writes
            self._apply_coordinator_agent_response(coordinator.name, response)
            self.ws.append("logbook", response, coordinator.name, "Turn completed")
            self._emit(Event(EventKind.TURN_END, agent=coordinator.name, data={
                "turn_id": turn_id, "attempt": self._turn_attempts[turn_id],
                "step": step, "response": response,
                **self._event_actor_meta(coordinator),
                **self._usage_event(coordinator),
            }))
            if await self._enforce_token_budget("coordinator", {"step": step, "agent": coordinator.name}):
                break

            parsed = self._parse_coordinator_response(response)
            next_agent_name = parsed["next_agent"]
            instructions = parsed["instructions"]
            verdict = parsed["verdict"]
            completion_errors = parsed["completion_errors"]
            if completion_errors:
                self._emit(Event(EventKind.PHASE, data={
                    "phase": "coordinator",
                    "status": "quality_gate_failed",
                    "step": step,
                    "errors": completion_errors,
                }))
                self._last_agent_feedback = (
                    "System validation blocked completion.\n"
                    + "\n".join(f"- {error}" for error in completion_errors)
                )
                continue
            
            # Loop detection
            if verdict == "CONTINUE":
                summon_hash = f"{next_agent_name}:{instructions}"
                if summon_hash == previous_summon:
                    consecutive_loops += 1
                    if consecutive_loops >= 1: # Tried exact same thing twice
                        self._emit(Event(EventKind.ERROR, agent=coordinator.name, data={"error": "Loop detected. Terminating debate to prevent stall."}))
                        break
                else:
                    consecutive_loops = 0
                    previous_summon = summon_hash

            if verdict in {"COMPLETE", "PAUSE", "PAUSE_FOR_INPUT"}:
                status = "complete" if verdict == "COMPLETE" else "waiting_for_approval"
                self._emit(Event(EventKind.PHASE, data={
                    "phase": "coordinator", "status": status, "step": step
                }))
                if verdict != "COMPLETE":
                    self.pause()
                    await self._wait_if_paused()
                    if not self._running:
                        break
                    continue
                break

            # Sanitize the name in case the LLM wrapped it in **bold** or `code` tags
            clean_name = next_agent_name.replace('*', '').replace('`', '').strip().lower()
            selected_agent = next((a for a in other_agents if a.name.lower() == clean_name), None)
            if not selected_agent:
                error_msg = f"Coordinator selected invalid agent: '{next_agent_name}'"
                self._emit(Event(EventKind.ERROR, agent=coordinator.name, data={"error": error_msg}))
                if other_agents:
                    selected_agent = other_agents[0]
                else:
                    break

            # 2. Call selected agent
            agent_full_ctx = self._agent_context(selected_agent)
            agent_prompt = (
                f"Coordinator Instructions:\n{instructions}\n\n"
                f"Current Workspace snapshot:\n{agent_full_ctx}\n\n"
                f"[OPTIONAL FILE UPDATES]\n"
                f"You may optionally update workspace files directly by including these exact headers anywhere in your response. DO NOT wrap them in markdown code blocks:\n"
                f"## DESIGN_UPDATE\n<complete updated design document>\n\n"
                f"## PLAN_UPDATE\n<complete updated plan document>\n\n"
                f"## DECISIONS_UPDATE\n<complete updated decision log with rationale and trade-offs>\n\n"
                f"## DECISION_CHECKPOINT\n<if you need the user to make a critical decision, use markdown bullets like - [A] option, - [B] option, plus a Recommendation line and concise trade-offs>\n\n"
                f"## QUESTIONS\n<if you need clarification from the user, write your questions here>\n\n"
                f"## FILE: path/to/file.ext\n<complete file content>\n\n"
                f"Please execute your turn now."
            )

            agent_turn_context = {"step": step, "phase": "coordinator_agent", "standing_role": selected_agent.config.role}
            agent_turn_id = self._begin_turn(selected_agent, agent_turn_context)

            agent_response = await self._send_agent(selected_agent, agent_prompt, agent_turn_id, agent_turn_context)
            self._record_turn_usage(selected_agent)
            self._last_agent_feedback = f"{selected_agent.name} said:\n{agent_response}"

            agent_decision = self.ws.parse_section(agent_response, "DECISION_CHECKPOINT").strip()
            agent_questions = self.ws.parse_section(agent_response, "QUESTIONS").strip()

            self._apply_coordinator_agent_response(selected_agent.name, agent_response)
            
            if agent_decision:
                self.ws.write("questions", f"# Decision Checkpoint\n\n{agent_decision}")
                self._emit(Event(EventKind.FILE_WRITE, agent=selected_agent.name, data={"file": "QUESTIONS.md"}))
            if agent_questions:
                self.ws.write("questions", f"# Clarifying Questions\n\n{agent_questions}")
                self._emit(Event(EventKind.FILE_WRITE, agent=selected_agent.name, data={"file": "QUESTIONS.md"}))

            self.ws.append("logbook", agent_response, selected_agent.name, "Turn completed")
            self._emit(Event(EventKind.TURN_END, agent=selected_agent.name, data={
                "turn_id": agent_turn_id, "attempt": self._turn_attempts[agent_turn_id],
                "step": step, "response": agent_response,
                **self._event_actor_meta(selected_agent),
                **self._usage_event(selected_agent),
            }))
            if await self._enforce_token_budget("coordinator_agent", {"step": step, "agent": selected_agent.name}):
                break

            if agent_decision or agent_questions:
                self._emit(Event(EventKind.PHASE, data={
                    "phase": "coordinator_agent", "status": "waiting_for_approval", "step": step
                }))
                self.pause()
                await self._wait_if_paused()
                if not self._running:
                    break



        coordinator.config.system_prompt = orig_system

    def _parse_coordinator_response(self, response: str) -> dict[str, Any]:
        next_agent = self.ws.parse_section(response, "NEXT_AGENT").strip()
        instructions = self.ws.parse_section(response, "INSTRUCTIONS").strip()
        verdict = self.ws.parse_section(response, "VERDICT").strip().upper()
        completion_errors: list[str] = []

        if verdict == "PAUSE":
            verdict = "PAUSE_FOR_INPUT"
        if verdict not in {"CONTINUE", "COMPLETE", "PAUSE_FOR_INPUT"}:
            raise RuntimeError(
                "Coordinator VERDICT must be CONTINUE, COMPLETE, or PAUSE_FOR_INPUT."
            )

        if verdict == "COMPLETE":
            quality_gate = self.ws.parse_section(response, "QUALITY_GATE").strip()
            completion_errors = self._coordinator_completion_errors(quality_gate)
                
        # Decision Checkpoint & Questions Check
        decision = self.ws.parse_section(response, "DECISION_CHECKPOINT").strip()
        questions = self.ws.parse_section(response, "QUESTIONS").strip()

        if verdict == "PAUSE_FOR_INPUT" and not decision and not questions:
            raise RuntimeError("VERDICT was PAUSE_FOR_INPUT, but neither DECISION_CHECKPOINT nor QUESTIONS were provided. You must provide options or questions for the user.")

        if decision:
            self.ws.write("questions", f"# Decision Checkpoint\n\n{decision}")
            self._emit(Event(EventKind.FILE_WRITE, agent=self._coordinator_name or "Coordinator", data={"file": "QUESTIONS.md"}))
            if verdict == "CONTINUE":
                verdict = "PAUSE_FOR_INPUT"
        
        if questions:
            self.ws.write("questions", f"# Clarifying Questions\n\n{questions}")
            self._emit(Event(EventKind.FILE_WRITE, agent=self._coordinator_name or "Coordinator", data={"file": "QUESTIONS.md"}))
            if verdict == "CONTINUE":
                verdict = "PAUSE_FOR_INPUT"

        if next_agent.upper() == "USER" and verdict == "CONTINUE":
            verdict = "PAUSE_FOR_INPUT"

        if verdict == "CONTINUE" and not next_agent:
            raise RuntimeError("Coordinator must provide NEXT_AGENT when continuing.")
        if verdict == "CONTINUE" and not instructions:
            raise RuntimeError("Coordinator must provide INSTRUCTIONS when continuing.")

        return {
            "next_agent": next_agent,
            "instructions": instructions,
            "verdict": verdict,
            "completion_errors": completion_errors,
        }

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

        test_bit = self.ws.parse_section(response, "TEST_RESULTS_APPEND")
        if test_bit:
            self.ws.append("tests", test_bit, agent_name, "Coordinator-led Turn")
