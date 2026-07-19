from __future__ import annotations

from dataclasses import dataclass
import hashlib

from backend.projections import PlanningProjection, render_decisions, render_design, render_plan
from backend.semantic import LocalEmbeddingAnalyzer, SQLiteSemanticIndex

from .analysis import analyze_claims, extract_claims
from .models import PlanningClaim, PlanningConflict
from .repository import WorkflowRepository


@dataclass(frozen=True)
class AnalysisResult:
    claims: list[PlanningClaim]
    agreements: list[list[str]]
    conflicts: list[PlanningConflict]


class PlanningService:
    def __init__(self, repository: WorkflowRepository, semantic_analyzer=None):
        self.repository = repository
        self.semantic_analyzer = semantic_analyzer or LocalEmbeddingAnalyzer()
        self.semantic_index = SQLiteSemanticIndex(repository.store)

    def analyze(self, run_id: str) -> AnalysisResult:
        claims = []
        for stored in self.repository.proposals(run_id):
            claims.extend(extract_claims(stored["id"], stored["proposal"]))
        if getattr(self.semantic_analyzer, "available", False) and claims:
            vectors = self.semantic_analyzer.embed([claim.text for claim in claims])
            for claim, vector in zip(claims, vectors):
                self.semantic_index.put(
                    run_id, claim.id, claim.text, vector,
                    self.semantic_analyzer.model_name, self.semantic_analyzer.model_version,
                )
        agreements, conflicts = analyze_claims(claims, self.semantic_analyzer)
        turns = self.repository.debate_turns(run_id)
        challenges = {}
        for turn in turns:
            if turn["turn_kind"] == "challenge":
                for challenge in turn["payload"].get("challenges", []):
                    challenges[challenge.get("id")] = challenge
        revision_turn = next((turn for turn in reversed(turns) if turn["turn_kind"] == "revision"), None)
        if revision_turn:
            for disposition in revision_turn["payload"].get("dispositions", []):
                if disposition.get("status") != "unresolved":
                    continue
                challenge = challenges.get(disposition.get("challenge_id"), {})
                proposed = challenge.get("proposed_change", "")
                current = disposition.get("resulting_decision", "")
                if not proposed or not current or proposed == current:
                    raise ValueError("unresolved challenge must retain two concrete decision options")
                digest = hashlib.sha256(
                    f"{disposition['challenge_id']}\x1f{proposed}\x1f{current}".encode()
                ).hexdigest()[:12]
                conflicts.append(PlanningConflict(
                    id=f"conflict-debate-{digest}",
                    topic=challenge.get("target_topic", disposition["challenge_id"]),
                    claim_ids=[f"challenge:{disposition['challenge_id']}", f"revision:{disposition['challenge_id']}"],
                    options=[current, proposed], materiality=challenge.get("materiality", "high"),
                ))
        self.repository.save_analysis(run_id, claims, conflicts)
        return AnalysisResult(claims, agreements, conflicts)

    def project(self, run_id: str, goal: str) -> PlanningProjection:
        proposals = [stored["proposal"] for stored in self.repository.proposals(run_id)]
        conflicts = self.repository.conflicts(run_id)
        return PlanningProjection(
            design=render_design(goal, proposals, conflicts),
            plan=render_plan(goal, proposals, conflicts),
            decisions=render_decisions(proposals, conflicts),
        )
