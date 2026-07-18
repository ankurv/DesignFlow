"""Typed execution contracts shared by routing, validation, persistence, and UI."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class RunKind(str, Enum):
    STATUS_QUERY = "status_query"
    CHAT = "chat"
    ARTIFACT_EDIT = "artifact_edit"
    PLANNING_WORKFLOW = "planning_workflow"
    RECOVERY = "recovery"
    INTENT_ROUTING = "intent_routing"


@dataclass(frozen=True)
class RunContract:
    kind: RunKind
    request: str
    target_artifacts: tuple[str, ...] = ()
    requires_diagram_delta: bool = False
    local_intent: str = ""
    recovery_of: RunKind | None = None

    @property
    def effective_kind(self) -> RunKind:
        return self.recovery_of if self.kind == RunKind.RECOVERY and self.recovery_of else self.kind

    @property
    def requires_artifact_change(self) -> bool:
        return self.effective_kind == RunKind.ARTIFACT_EDIT

    @property
    def uses_team_workflow(self) -> bool:
        return self.effective_kind == RunKind.PLANNING_WORKFLOW

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "request": self.request,
            "target_artifacts": list(self.target_artifacts),
            "requires_diagram_delta": self.requires_diagram_delta,
            "local_intent": self.local_intent,
            "recovery_of": self.recovery_of.value if self.recovery_of else None,
        }

    @classmethod
    def from_dict(cls, value: dict | None) -> "RunContract | None":
        if not value or not value.get("kind"):
            return None
        try:
            kind = RunKind(value["kind"])
        except (TypeError, ValueError):
            return None
        recovery_value = value.get("recovery_of")
        try:
            recovery_of = RunKind(recovery_value) if recovery_value else None
        except (TypeError, ValueError):
            recovery_of = None
        return cls(
            kind=kind,
            request=str(value.get("request", "")),
            target_artifacts=tuple(str(item) for item in value.get("target_artifacts", [])),
            requires_diagram_delta=bool(value.get("requires_diagram_delta", False)),
            local_intent=str(value.get("local_intent", "")),
            recovery_of=recovery_of,
        )


def _artifact_targets(normalized: str) -> tuple[str, ...]:
    targets = []
    if re.search(r"\b(?:design\.md|design document|existing design|(?:system |architecture |visual )?diagrams?)\b", normalized):
        targets.append("DESIGN.md")
    if re.search(r"\b(?:plan\.md|implementation plan|project plan)\b", normalized):
        targets.append("PLAN.md")
    if re.search(r"\b(?:decisions\.md|decision ledger|key decisions)\b", normalized):
        targets.append("DECISIONS.md")
    return tuple(targets)


def classify_run_contract(text: str, mode: str = "auto", local_intent: str = "") -> RunContract:
    request = (text or "").strip()
    normalized = " ".join(request.lower().split())
    if local_intent:
        return RunContract(RunKind.STATUS_QUERY, request, local_intent=local_intent)

    bounded_direct_edit = bool(re.search(
        r"\b(?:one agent|single agent|bounded (?:document )?edit|do not (?:start a )?debate|without (?:a )?debate|update directly)\b",
        normalized,
    ))
    substantive_design_refinement = bool(re.search(
        r"\b(?:refine|revise|improve|challenge)\b.*\b(?:design|architecture|diagram)s?\b",
        normalized,
    )) and not bounded_direct_edit and not normalized.endswith("?")
    explicit_team_request = bool(re.search(
        r"\b(?:debate|multi-agent|team review|challenge the design)\b", normalized,
    )) and not bounded_direct_edit
    explicit_team = mode in {"debate", "all"} or substantive_design_refinement or explicit_team_request
    targets = _artifact_targets(normalized)
    edit_verb = bool(re.search(
        r"\b(?:add|append|include|insert|update|edit|fix|refresh|refine|revise|improve|generate|create)\b",
        normalized,
    ))
    if not explicit_team and targets and edit_verb:
        return RunContract(
            RunKind.ARTIFACT_EDIT,
            request,
            target_artifacts=targets,
            requires_diagram_delta=bool(re.search(r"\b(?:mermaid|diagram|visual)\b", normalized)),
        )
    if explicit_team:
        return RunContract(RunKind.PLANNING_WORKFLOW, request)
    if mode == "direct":
        return RunContract(RunKind.CHAT, request)

    # Questions have no mutation contract. All other natural-language requests
    # remain deliberately unresolved until the state-aware router sees current
    # artifacts and validation state; vocabulary matching is not an intent model.
    if normalized.endswith("?"):
        return RunContract(RunKind.CHAT, request)
    return RunContract(RunKind.INTENT_ROUTING, request)
