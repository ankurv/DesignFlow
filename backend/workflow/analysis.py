from __future__ import annotations

import hashlib
import re

from backend.semantic.interface import SemanticAnalyzer

from .materiality import classify_materiality
from .models import ExpertProposal, PlanningClaim, PlanningConflict


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "claim"


def _claim_id(proposal_id: str, kind: str, topic: str, text: str) -> str:
    digest = hashlib.sha256(f"{proposal_id}\x1f{kind}\x1f{topic}\x1f{text}".encode()).hexdigest()[:12]
    return f"{_slug(topic)}-{digest}"


def extract_claims(proposal_id: str, proposal: ExpertProposal) -> list[PlanningClaim]:
    claims: list[PlanningClaim] = []
    for component in proposal.components:
        text = f"{component.name}: {component.responsibility}"
        claims.append(PlanningClaim(
            id=_claim_id(proposal_id, "component", component.name, text), proposal_id=proposal_id,
            claim_type="component", topic=component.name, text=text,
        ))
    for decision in proposal.decisions:
        claims.append(PlanningClaim(
            id=_claim_id(proposal_id, "decision", decision.topic, decision.recommendation),
            proposal_id=proposal_id, claim_type="decision", topic=decision.topic,
            text=decision.recommendation,
        ))
    for risk in proposal.risks:
        claims.append(PlanningClaim(
            id=_claim_id(proposal_id, "risk", risk.risk, risk.mitigation), proposal_id=proposal_id,
            claim_type="risk", topic=risk.risk, text=f"{risk.risk}: {risk.mitigation}",
        ))
    for assumption in proposal.assumptions:
        claims.append(PlanningClaim(
            id=_claim_id(proposal_id, "assumption", assumption, assumption), proposal_id=proposal_id,
            claim_type="assumption", topic=assumption, text=assumption,
        ))
    for unknown in proposal.unknowns:
        claims.append(PlanningClaim(
            id=_claim_id(proposal_id, "unknown", unknown.question, unknown.validation),
            proposal_id=proposal_id, claim_type="unknown", topic=unknown.question,
            text=f"{unknown.question} Validation: {unknown.validation}",
        ))
    return claims


def analyze_claims(claims: list[PlanningClaim], analyzer: SemanticAnalyzer) -> tuple[list[list[str]], list[PlanningConflict]]:
    """Return local agreement clusters and explicit same-topic decision conflicts."""
    agreements: list[list[str]] = []
    matches = analyzer.classify_pairs([(claim.id, claim.text) for claim in claims])
    for match in matches:
        if match.relation == "duplicate":
            agreements.append(sorted((match.left_id, match.right_id)))

    conflicts: list[PlanningConflict] = []
    decision_topics: dict[str, list[PlanningClaim]] = {}
    for claim in claims:
        if claim.claim_type == "decision":
            decision_topics.setdefault(_slug(claim.topic), []).append(claim)
    for topic_key, topic_claims in sorted(decision_topics.items()):
        distinct = []
        for claim in sorted(topic_claims, key=lambda item: item.id):
            if not any(analyzer.similarity(claim.text, existing.text) >= analyzer.duplicate_threshold for existing in distinct):
                distinct.append(claim)
        if len(distinct) < 2:
            continue
        options = [claim.text for claim in distinct]
        digest = hashlib.sha256("\x1f".join(sorted(claim.id for claim in distinct)).encode()).hexdigest()[:12]
        conflicts.append(PlanningConflict(
            id=f"conflict-{topic_key}-{digest}", topic=distinct[0].topic,
            claim_ids=[claim.id for claim in distinct], options=options,
            materiality=classify_materiality(distinct[0].topic, options),
        ))
    return agreements, conflicts
