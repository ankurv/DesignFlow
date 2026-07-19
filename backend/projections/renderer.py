from __future__ import annotations

from dataclasses import dataclass
import re

from backend.workflow.models import ExpertProposal, PlanningConflict


@dataclass(frozen=True)
class PlanningProjection:
    design: str
    plan: str
    decisions: str


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _architecture_diagram(proposals: list[ExpertProposal]) -> list[str]:
    components = _unique([
        component.name for proposal in proposals for component in proposal.components
    ])
    if not components:
        return ["flowchart LR", '    Goal["Product goal"] --> Validate["Validate component model"]']
    ids = {name: f"C{index + 1}" for index, name in enumerate(components)}
    lines = ["flowchart LR"]
    for name in components:
        label = re.sub(r'["\n\r]', " ", name).strip()
        lines.append(f'    {ids[name]}["{label}"]')
    edges = []
    by_fold = {name.casefold(): name for name in components}
    for proposal in proposals:
        for component in proposal.components:
            if component.name not in ids:
                continue
            for interface in component.interfaces:
                target = by_fold.get(interface.casefold())
                if target and target != component.name:
                    edges.append(f"    {ids[component.name]} --> {ids[target]}")
    lines.extend(_unique(edges) or [
        f"    {ids[left]} --> {ids[right]}" for left, right in zip(components, components[1:])
    ])
    return lines


def render_design(goal: str, proposals: list[ExpertProposal], conflicts: list[PlanningConflict]) -> str:
    components = _unique([
        f"- **{component.name}**: {component.responsibility}"
        for proposal in proposals for component in proposal.components
    ]) or ["- The detailed component model will be established from validated proposals."]
    risks = _unique([
        f"- **{risk.risk}**: {risk.mitigation}" for proposal in proposals for risk in proposal.risks
    ]) or ["- No material implementation risk has been accepted yet."]
    unknowns = _unique([
        f"- **{unknown.question}** Validate by: {unknown.validation}"
        for proposal in proposals for unknown in proposal.unknowns
    ]) or ["- Validate provider behavior and performance with representative fixtures."]
    conflict_lines = [
        f"- **{conflict.topic}** ({conflict.materiality}): " + " vs. ".join(conflict.options)
        for conflict in conflicts if conflict.status == "open"
    ] or ["- No unresolved material conflicts remain."]
    return "\n".join((
        "# Architecture Design", "", "## Product Goal", "", goal, "",
        "## Architecture", "", *components, "",
        "```mermaid", *_architecture_diagram(proposals), "```", "",
        "## Capability Behavioral Contracts", "",
        "Selected capabilities are represented by typed proposals and validated before projection.", "",
        "## Product Operations & Evolution", "",
        "Define deployment, observability, data lifecycle, failure recovery, compatibility, and rollback behavior "
        "for each accepted component before production use. Logs must redact sensitive fields.", "",
        "## Risks", "", *risks, "", "## Open Conflicts", "", *conflict_lines, "",
        "## Known Unknowns & Validation Plan", "", *unknowns, "",
    )).rstrip() + "\n"


def render_plan(goal: str, proposals: list[ExpertProposal], conflicts: list[PlanningConflict]) -> str:
    assumptions = _unique([item for proposal in proposals for item in proposal.assumptions])
    risks = _unique([risk.risk for proposal in proposals for risk in proposal.risks])
    decisions = _unique([
        f"{decision.topic}: {decision.recommendation}"
        for proposal in proposals for decision in proposal.decisions
    ])
    tasks = _unique([
        f"- [ ] Implement {component.name}: {component.responsibility}"
        for proposal in proposals for component in proposal.components
    ]) or ["- [ ] Validate and implement the accepted architecture components."]
    trace = _unique([
        f"- Requirement `{component.name}` -> DESIGN Architecture -> Implementation task `{component.name}` "
        f"-> acceptance evidence: component contract tests."
        for proposal in proposals for component in proposal.components
    ]) or ["- Product goal -> DESIGN Product Goal -> implementation phases -> end-to-end acceptance journey."]
    checkpoints = [
        f"- Resolve `{conflict.topic}` ({conflict.materiality}) before dependent implementation."
        for conflict in conflicts if conflict.status == "open"
    ] or ["- No unresolved discovery checkpoint is currently blocking implementation."]
    return "\n".join((
        "# Implementation Plan", "", "## Requirements", "", f"- Deliver: {goal}", "",
        "## Non-Goals", "", "- Do not add capabilities absent from accepted proposals or user decisions.", "",
        "## Assumptions", "", *([f"- {item}" for item in assumptions] or ["- Assumptions require explicit validation."]), "",
        "## Alternatives", "", "- Alternatives remain represented as conflict options until resolved.", "",
        "## Decisions", "", *([f"- {item}" for item in decisions] or ["- No implementation decision is accepted yet."]), "",
        "## Risks", "", *([f"- {item}" for item in risks] or ["- Validate operational and failure risks during implementation."]), "",
        "## Acceptance Criteria", "", "- The end-to-end workflow and every accepted component contract pass.", "",
        "## Requirement Traceability", "", *trace, "", "## Implementation Phases", "", *tasks, "",
        "## Discovery Checkpoints", "", *checkpoints, "",
    )).rstrip() + "\n"


def render_decisions(proposals: list[ExpertProposal], conflicts: list[PlanningConflict]) -> str:
    accepted = _unique([
        f"- **{decision.topic}**: {decision.recommendation}. Rationale: {decision.rationale}"
        for proposal in proposals for decision in proposal.decisions
    ]) or ["- No decision has been accepted yet."]
    unresolved = [
        f"- **{conflict.topic}** ({conflict.materiality}): " + " | ".join(conflict.options)
        for conflict in conflicts if conflict.status == "open"
    ] or ["- None."]
    resolved = [
        f"- **{conflict.topic}**: {conflict.resolution} (source: {conflict.resolution_source})"
        for conflict in conflicts if conflict.status == "resolved"
    ] or ["- None."]
    return "\n".join((
        "# Key Decisions", "", "## Proposed Decisions", "", *accepted, "",
        "## Resolved Conflicts", "", *resolved, "", "## Unresolved Conflicts", "", *unresolved, "",
    )).rstrip() + "\n"
