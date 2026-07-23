"""Semantic interaction routing and read-only project conversation."""

from __future__ import annotations

import asyncio
from enum import Enum

from pydantic import BaseModel, ConfigDict

from .context import ContextTree
from .semantic import LocalEmbeddingAnalyzer


class InteractionKind(str, Enum):
    ANSWER = "answer"
    PLANNING = "planning_workflow"
    RECOVERY = "recovery"


class InteractionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: InteractionKind
    reason: str
    answer: str = ""


ANSWER_SYSTEM = """You are the user's project-aware DesignFlow agent.
Answer the user's question directly and naturally using only the supplied project evidence.
When the user asks which agent, provider, or model is being used, answer from the supplied
DesignFlow runtime identity. Do not reinterpret that as a question about models mentioned
inside the product design. Answer in one or two sentences with the provider and exact model;
do not append project status, decisions, next steps, or workflow commentary unless requested.
Explain current progress, decisions, gaps, or next steps when relevant.
Do not start a debate, claim to edit files, or invent facts absent from the evidence.
If evidence is insufficient, say what is unknown and ask one useful follow-up question.
"""


ROUTING_SYSTEM = """You are an intent classifier for DesignFlow.
Read the user's prompt and output exactly one word from the following choices based on their intent:
1. "ANSWER": The user is asking a conversational question, inquiring about system status, agents, project details, or making a general chat statement (e.g. "hello", "are agents ready?").
2. "PLANNING": The user is providing a design goal, requirements, architecture instruction, or asking to build/create/refine a product/app/system.
3. "RECOVERY": The user is explicitly asking to continue, resume, retry, or recover a previous workflow.
Output EXACTLY ONE WORD: ANSWER, PLANNING, or RECOVERY. Do not output anything else.
"""


class InteractionService:
    def __init__(self, agent, workspace, store, workflow_snapshot: dict | None = None):
        self.agent = agent
        self.workspace = workspace
        self.context_tree = ContextTree(store)
        self.workflow_snapshot = workflow_snapshot or {}
        self.semantic = LocalEmbeddingAnalyzer()

    def _state_context(self, query: str) -> str:
        self.context_tree.sync_workspace(self.workspace)
        import json
        workflow = json.dumps(self.workflow_snapshot, ensure_ascii=False, sort_keys=True)
        if workflow != "{}":
            self.context_tree.upsert(
                node_type="workflow", source_type="workflow", source_ref="current",
                title="Current durable workflow", content=workflow, summary=workflow,
                authority=6, importance=1,
            )
        return self.context_tree.retrieve(
            query=query, max_tokens=3000, mandatory_types=("goal", "workflow"),
        ).text

    async def route(self, request: str) -> InteractionDecision:
        """Route using LLM classification, falling back to local semantics."""
        workflow_state = str(self.workflow_snapshot.get("state", ""))
        try:
            prompt = f"Classify this prompt:\n\n{request}"
            response = await asyncio.to_thread(
                self.agent.send, prompt, ROUTING_SYSTEM, "", context_only=True,
            )
            choice = response.strip().upper()
            if "ANSWER" in choice:
                selected = InteractionKind.ANSWER
            elif "RECOVERY" in choice:
                selected = InteractionKind.RECOVERY
            else:
                selected = InteractionKind.PLANNING
                
            if selected == InteractionKind.RECOVERY and not workflow_state:
                selected = InteractionKind.PLANNING
            
            return InteractionDecision(
                kind=selected,
                reason=f"LLM semantic route selected {selected.value}."
            )
        except Exception as e:
            # Fallback to local semantic routing
            anchors = [
                (InteractionKind.ANSWER, "answer a question explain current status progress decisions models files or what has been done"),
                (InteractionKind.PLANNING, "design architect plan refine change build debate requirements scope implementation approach"),
                (InteractionKind.RECOVERY, "continue resume retry recover the interrupted or paused planning workflow"),
            ]
            scores = {
                kind: self.semantic.similarity(request, text) for kind, text in anchors
            }
            ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0].value))
            selected, score = ranked[0]
            if selected == InteractionKind.RECOVERY and not workflow_state:
                selected = InteractionKind.PLANNING
            # Ambiguous requests are read-only unless they have stronger planning or
            # recovery evidence. This prevents a vague question from mutating design.
            if score < 0.08:
                selected = InteractionKind.ANSWER
            return InteractionDecision(
                kind=selected,
                reason=f"Local semantic route selected {selected.value} (score={score:.3f}). Fallback from LLM error.",
            )

    async def answer(self, request: str) -> str:
        config = self.agent.config
        runtime_identity = (
            f"agent={config.name}; provider={config.kind}; "
            f"model={config.model or 'provider default'}"
        )
        prompt = (
            f"User question:\n{request}\n\n"
            f"Current DesignFlow runtime identity:\n{runtime_identity}\n\n"
            f"Current project evidence:\n{self._state_context(request)}"
        )
        return await asyncio.to_thread(
            self.agent.send, prompt, ANSWER_SYSTEM, "", context_only=True,
        )
