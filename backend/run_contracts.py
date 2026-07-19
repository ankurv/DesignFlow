"""Typed execution contracts shared by routing, validation, persistence, and UI."""

from __future__ import annotations

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


def classify_run_contract(text: str, mode: str = "auto", local_intent: str = "") -> RunContract:
    """Apply structured UI overrides; defer natural language to semantic routing."""
    request = (text or "").strip()
    if local_intent:
        return RunContract(RunKind.STATUS_QUERY, request, local_intent=local_intent)
    if mode in {"debate", "all"}:
        return RunContract(RunKind.PLANNING_WORKFLOW, request)
    if mode == "direct":
        return RunContract(RunKind.CHAT, request)
    return RunContract(RunKind.INTENT_ROUTING, request)
