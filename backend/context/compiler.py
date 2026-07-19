from __future__ import annotations

import json

from backend.workflow.models import ContextItem, ContextPacket


DEFAULT_MAX_TOKENS = 2000


def estimate_tokens(value: object) -> int:
    """Conservative provider-independent estimate used for deterministic budgets."""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return max(1, (len(text) + 3) // 4)


class ContextCompiler:
    def __init__(self, max_tokens: int = DEFAULT_MAX_TOKENS):
        if max_tokens < 256:
            raise ValueError("context budget must be at least 256 tokens")
        self.max_tokens = max_tokens

    def compile(
        self,
        *,
        goal: str,
        constraints: list[str],
        confirmed_decisions: list[str],
        operation_instructions: str,
        conflicts: list[ContextItem] | None = None,
        summaries: list[ContextItem] | None = None,
        raw_evidence: list[ContextItem] | None = None,
    ) -> ContextPacket:
        mandatory = {
            "goal": goal,
            "constraints": constraints,
            "confirmed_decisions": confirmed_decisions,
            "operation_instructions": operation_instructions,
        }
        used = estimate_tokens(mandatory)
        if used > self.max_tokens:
            raise ValueError("goal, constraints, confirmed decisions, and operation exceed context budget")

        selected: dict[str, list[ContextItem]] = {
            "conflicts": [], "summaries": [], "raw_evidence": [],
        }
        candidates = [
            ("conflicts", item) for item in (conflicts or [])
        ] + [
            ("summaries", item) for item in (summaries or [])
        ] + [
            ("raw_evidence", item) for item in (raw_evidence or [])
        ]
        candidates.sort(key=lambda pair: (pair[1].priority, -pair[1].relevance, pair[1].id))
        provenance = []
        for bucket, item in candidates:
            cost = estimate_tokens(item.model_dump())
            if used + cost > self.max_tokens:
                continue
            selected[bucket].append(item)
            used += cost
            provenance.append({
                "item_id": item.id,
                "source": f"{item.source_type}:{item.source_id}",
                "reason": f"priority={item.priority};relevance={item.relevance:.3f}",
            })
        return ContextPacket(
            **mandatory,
            unresolved_conflicts=selected["conflicts"],
            relevant_summaries=selected["summaries"],
            raw_evidence=selected["raw_evidence"],
            provenance=provenance,
            estimated_tokens=used,
        )
