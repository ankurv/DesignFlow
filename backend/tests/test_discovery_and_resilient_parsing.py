"""Tests for resilient agent communication parsing and discovery requirement gathering."""
from __future__ import annotations

import unittest
from backend.workflow.models import DebateChallenge, ChallengeDisposition, DiscoveryAssessment, ExpertProposal
from backend.orchestration import _clean_json_str, _json_object, _extract_markdown_proposal, _extract_markdown_review


class TestResilientParsing(unittest.TestCase):

    def test_json_cleaner_and_object_decoding(self):
        # Test trailing comma removal & fenced code block extraction
        raw = """```json
        {
            "name": "AuthService",
            "responsibility": "Handles auth tokens",
        }
        ```"""
        cleaned = _clean_json_str(raw)
        data = _json_object(cleaned)
        self.assertEqual(data["name"], "AuthService")
        self.assertEqual(data["responsibility"], "Handles auth tokens")

    def test_markdown_proposal_extraction(self):
        markdown_text = """
        # Proposal Overview
        
        ## Components
        - **AuthModule**: Manages user authentication and tokens.
        - **DatabaseModule**: Handles persistent data storage.

        ## Key Decisions
        - **Database**: Use PostgreSQL for relational data storage.

        ## Risks
        - **Security**: Risk of unencrypted traffic. Mitigation: Enforce TLS 1.3.

        ## Assumptions
        - Redis is available for session caching.

        ```mermaid
        graph TD
            A[Client] --> B[AuthModule]
        ```
        """
        parsed = _extract_markdown_proposal(markdown_text)
        proposal = ExpertProposal.model_validate(parsed)
        self.assertEqual(len(proposal.components), 2)
        self.assertEqual(proposal.components[0].name, "AuthModule")
        self.assertEqual(len(proposal.decisions), 1)
        self.assertEqual(proposal.decisions[0].topic, "Database")
        self.assertEqual(len(proposal.risks), 1)
        self.assertIn("Redis", proposal.assumptions[0])
        self.assertIn("graph TD", proposal.diagram)

    def test_markdown_review_extraction(self):
        markdown_text = """
        Here is my expert review:
        - **Database Scale**: The query throughput might exceed single-node limits.
        - **Auth Token Expiry**: Token lifetime is too long, increasing compromise window.

        Overall, the core architecture is validated and sound.
        """
        parsed = _extract_markdown_review(markdown_text)
        self.assertEqual(len(parsed["challenges"]), 2)
        self.assertIn("Core Architecture", parsed["validated_topics"])

    def test_model_enum_resilience(self):
        # Test uppercase/whitespace and fallback mappings in DebateChallenge
        challenge = DebateChallenge.model_validate({
            "target_topic": "Security",
            "claim": "Insecure defaults",
            "materiality": "CRITICAL",
            "authority_basis": "EXPLICIT_REQUIREMENT",
            "scope_effect": "EXPANDS",
            "relation": "REFINTES",
        })
        self.assertEqual(challenge.materiality, "high")
        self.assertEqual(challenge.authority_basis, "explicit_requirement")
        self.assertEqual(challenge.scope_effect, "expands")
        self.assertEqual(challenge.relation, "refines")

    def test_challenge_disposition_resilience(self):
        disposition = ChallengeDisposition.model_validate({
            "challenge_id": "c-1",
            "status": "ACCEPTED",
            "rationale": "Valid point",
            "resulting_decision": "Updated design",
        })
        self.assertEqual(disposition.status, "accepted")

    def test_discovery_assessment_questions(self):
        # Test discovery assessment with questions
        assessment = DiscoveryAssessment.model_validate({
            "adequate": False,
            "evidence_summary": "High-level goal provided",
            "provisional_assumptions": [],
            "blocking_questions": ["What is the target scale?", "What auth provider is required?"],
        })
        self.assertFalse(assessment.adequate)
        self.assertEqual(len(assessment.blocking_questions), 2)

    def test_discovery_checkpoint_transition_payload(self):
        # Verify that discovery waiting-for-user payload includes resume_state
        from backend.storage import ProjectStore
        from backend.orchestration import Orchestration
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectStore(Path(tmpdir))
            orch = Orchestration(agents=[], workspace=None, store=store, run_id="run-1")
            orch.repository.create("run-1")

            # Advance to DISCOVERING state
            orch.engine.transition("run-1", "start", {})
            
            # Verify transition to WAITING_FOR_USER with resume_state: DISCOVERING
            checkpoint = store.enqueue_checkpoint("run-1", "discovering", "Question", "Target actor?", [])
            snapshot = orch.loop_manager.advance("run-1", "input_required", {
                "resume_state": "DISCOVERING", "reason": "discovery_blocked", "checkpoint_id": checkpoint["id"]
            })
            self.assertEqual(snapshot.state.value, "WAITING_FOR_USER")
            self.assertEqual(snapshot.resume_state.value, "DISCOVERING")

    def test_parse_discovery_question_and_options(self):
        from backend.orchestration import Orchestration
        orch = Orchestration(agents=[], workspace=None, store=None, run_id="run-test")
        raw_question = (
            "Please clarify this product unknown before architecture planning begins.\n"
            "Context\n"
            "Can you confirm the exact product goal? Is it a personal calendar and meeting assistant "
            "or a resilient event notification engine with SQLite cache and gRPC Protobuf microservices?"
        )
        q_text, q_rationale, q_options = orch._parse_discovery_question_and_options(raw_question, "High-level goal")
        self.assertIn("Can you confirm the exact product goal?", q_text)
        self.assertEqual(len(q_options), 2)
        self.assertEqual(q_options[0]["label"], "A")
        self.assertIn("Personal calendar and meeting assistant", q_options[0]["summary"])
        self.assertEqual(q_options[1]["label"], "B")
        self.assertIn("Resilient event notification engine", q_options[1]["summary"])


if __name__ == "__main__":
    unittest.main()
