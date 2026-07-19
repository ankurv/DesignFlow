import tempfile
import threading
import unittest
import asyncio
from pathlib import Path

from backend.context import ContextCompiler, ContextTree
from backend.interaction import InteractionKind, InteractionService
from backend.semantic import LexicalSemanticAnalyzer, LocalEmbeddingAnalyzer, SQLiteSemanticIndex
from backend.storage import ProjectStore
from backend.agents.base import AgentBase, AgentConfig, Usage
from backend.orchestration import DISCOVERY_SYSTEM, PROPOSAL_SYSTEM, Orchestration
from backend.workflow.models import DiscoveryAssessment
from backend.workspace.workspace import Workspace
from backend.workflow import (
    LoopCommandKind, LoopManager, LoopSignal, StoredJSONError, WorkflowEngine, WorkflowEvent,
    WorkflowRepository, WorkflowState,
)
from backend.workflow.materiality import classify_materiality
from backend.workflow.models import (
    ContextItem, DebateChallenge, DebateReview, ExpertProposal, ProposalComponent, ProposalDecision, ProposalRisk,
    ProposalUnknown,
)
from backend.workflow.planning import PlanningService


async def run_with_approved_review(orchestrator, goal):
    task = asyncio.create_task(orchestrator.run(goal))
    for _ in range(100):
        await asyncio.sleep(0)
        checkpoint = orchestrator.store.current_checkpoint(orchestrator.run_id)
        if checkpoint and checkpoint.get("phase") == "design_review":
            option = checkpoint["options"][0]
            orchestrator.store.answer_checkpoint(
                orchestrator.run_id, checkpoint["id"], "test", option["id"], "",
            )
            await orchestrator.accept_structured_checkpoint_answer(
                f"{option['label']} — {option['summary']}", False, "test",
            )
            return await task
    task.cancel()
    raise AssertionError("design review checkpoint was not created")


class JsonProposalAgent(AgentBase):
    manages_context = True

    def _raw_send(self, messages, system, *args, **kwargs):
        if "discovery gate" in system:
            return '{"adequate":true,"evidence_summary":"The goal and repository evidence establish a bounded planner product.","blocking_questions":[]}', Usage(input_tokens=10, output_tokens=10)
        if "reviewing a concrete architecture proposal" in system:
            return '{"challenges":[],"validated_topics":["state ownership"]}', Usage(input_tokens=20, output_tokens=10)
        if "coordinating architect" in system:
            return """{"proposal":{"components":[{"name":"Workflow engine","responsibility":"Persist legal transitions","interfaces":["SQLite"]}],"decisions":[{"topic":"state ownership","recommendation":"SQLite is authoritative","rationale":"Restart safety","alternatives":["memory"]}],"risks":[{"risk":"lock contention","mitigation":"WAL and short transactions"}],"assumptions":["single local project"],"unknowns":[{"question":"What is the write load?","validation":"Run concurrency tests"}]},"dispositions":[]}""", Usage(input_tokens=40, output_tokens=40)
        return """{"components":[{"name":"Workflow engine","responsibility":"Persist legal transitions","interfaces":["SQLite"]}],"decisions":[{"topic":"state ownership","recommendation":"SQLite is authoritative","rationale":"Restart safety","alternatives":["memory"]}],"risks":[{"risk":"lock contention","mitigation":"WAL and short transactions"}],"assumptions":["single local project"],"unknowns":[{"question":"What is the write load?","validation":"Run concurrency tests"}]}""", Usage(input_tokens=30, output_tokens=40)


class MustNotRunAgent(AgentBase):
    manages_context = True

    def _raw_send(self, messages, system, *args, **kwargs):  # pragma: no cover - failure is the assertion
        raise AssertionError("persisted proposal should prevent provider replay")


class ScriptedInteractionAgent(AgentBase):
    manages_context = True

    def __init__(self, config, responses):
        super().__init__(config)
        self.responses = iter(responses)

    def _raw_send(self, messages, system, *args, **kwargs):
        return next(self.responses), Usage(input_tokens=10, output_tokens=10)


class BlockingDiscoveryAgent(JsonProposalAgent):
    def _raw_send(self, messages, system, *args, **kwargs):
        if "discovery gate" in system:
            return '{"adequate":false,"evidence_summary":"No product users or outcomes are established.","blocking_questions":["Who uses this product and what outcome must they achieve?"]}', Usage(input_tokens=10, output_tokens=10)
        return super()._raw_send(messages, system, *args, **kwargs)


class InvalidDiscoveryAgent(JsonProposalAgent):
    def _raw_send(self, messages, system, *args, **kwargs):
        if "discovery gate" in system:
            return "I cannot produce the requested object.", Usage(input_tokens=4, output_tokens=6)
        return super()._raw_send(messages, system, *args, **kwargs)


class SteeringAwareAgent(JsonProposalAgent):
    def __init__(self, config):
        super().__init__(config)
        self.revision_input = ""

    def _raw_send(self, messages, system, *args, **kwargs):
        if "coordinating architect" in system:
            self.revision_input = "\n".join(str(getattr(item, "content", item)) for item in messages)
        return super()._raw_send(messages, system, *args, **kwargs)


class BareRevisionAgent(JsonProposalAgent):
    def __init__(self, config):
        super().__init__(config)
        self.revision_calls = 0

    def _raw_send(self, messages, system, *args, **kwargs):
        if "coordinating architect" in system:
            self.revision_calls += 1
            return super()._raw_send(messages, PROPOSAL_SYSTEM, *args, **kwargs)
        return super()._raw_send(messages, system, *args, **kwargs)


class OneChallengeAgent(JsonProposalAgent):
    def __init__(self, config):
        super().__init__(config)
        self.review_calls = 0

    def _raw_send(self, messages, system, *args, **kwargs):
        if "reviewing a concrete architecture proposal" in system:
            self.review_calls += 1
            if self.review_calls == 1:
                return ('{"challenges":[{"id":"auth-boundary","target_topic":"Authorization",'
                        '"claim":"Authorization ownership is unspecified","evidence":"The proposal omits an enforcement boundary",'
                        '"consequence":"Protected writes may bypass policy","proposed_change":"Name the server authorization boundary",'
                        '"materiality":"high","authority_basis":"explicit_requirement","scope_effect":"clarifies",'
                        '"related_challenge_id":"","relation":"distinct"}],"validated_topics":[]}'), Usage(input_tokens=20, output_tokens=20)
            return '{"challenges":[],"validated_topics":["authorization boundary"]}', Usage(input_tokens=20, output_tokens=10)
        return super()._raw_send(messages, system, *args, **kwargs)


class AcceptingCoordinatorAgent(JsonProposalAgent):
    def _raw_send(self, messages, system, *args, **kwargs):
        if "coordinating architect" in system:
            return """{"proposal":{"components":[{"name":"Workflow engine","responsibility":"Enforce legal transitions and authorization","interfaces":["SQLite"]}],"decisions":[{"topic":"authorization","recommendation":"Server owns authorization","rationale":"Trusted enforcement boundary","alternatives":["client checks"]}],"risks":[],"assumptions":[],"unknowns":[]},"dispositions":[{"challenge_id":"auth-boundary","status":"accepted","rationale":"The boundary was missing","resulting_decision":"Server authorization is explicit"}]}""", Usage(input_tokens=40, output_tokens=40)
        return super()._raw_send(messages, system, *args, **kwargs)


class ScopeExpansionReviewer(JsonProposalAgent):
    def _raw_send(self, messages, system, *args, **kwargs):
        if "reviewing a concrete architecture proposal" in system:
            return ('{"challenges":[{"id":"configurable-reminders","target_topic":"Notification timing",'
                    '"claim":"Fixed reminders may not suit every preference","evidence":"The explicit goal says notify 15 minutes before",'
                    '"consequence":"Some users may want another offset","proposed_change":"Add configurable reminder offsets",'
                    '"materiality":"medium","authority_basis":"expert_judgment","scope_effect":"expands",'
                    '"related_challenge_id":"","relation":"distinct"}],"validated_topics":[]}'), Usage(input_tokens=20, output_tokens=20)
        return super()._raw_send(messages, system, *args, **kwargs)


class OrchestrationTests(unittest.TestCase):
    def test_identical_peer_challenge_is_deduplicated_but_id_collision_is_rejected(self):
        first = DebateChallenge(
            id="retry-policy", target_topic="Retries", claim="Three retries are insufficient",
            evidence="Failure analysis", consequence="Notifications may be lost",
            proposed_change="Validate retry limits", materiality="high",
            authority_basis="repository_evidence", scope_effect="clarifies",
            related_challenge_id="", relation="distinct",
        )
        duplicate = first.model_copy(update={"claim": "  Three   retries are insufficient "})
        collision = first.model_copy(update={"claim": "Retries consume too much battery"})
        known = []
        self.assertTrue(Orchestration._append_unique_challenge(known, first))
        self.assertFalse(Orchestration._append_unique_challenge(known, duplicate))
        with self.assertRaisesRegex(ValueError, "collision"):
            Orchestration._append_unique_challenge(known, collision)

    def test_discovery_contract_treats_grounded_personal_goal_as_actionable(self):
        self.assertIn("personal tool", DISCOVERY_SYSTEM)
        self.assertIn("reversible assumption", DISCOVERY_SYSTEM)
        assessment = DiscoveryAssessment.model_validate({
            "adequate": True,
            "evidence_summary": "A person enters events and receives a reminder 15 minutes before them.",
            "provisional_assumptions": ["Use the device's local timezone until configured otherwise."],
            "blocking_questions": [],
        })
        self.assertTrue(assessment.adequate)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(Path(self.tmp.name))
        self.repository = WorkflowRepository(self.store)
        self.engine = WorkflowEngine(self.repository)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_transition_replay_is_idempotent_after_state_advances(self):
        self.engine.create("run-1")
        first = self.engine.transition("run-1", WorkflowEvent.START, {"source": "test"})
        replay = self.engine.transition("run-1", WorkflowEvent.START, {"source": "test"})
        self.assertEqual(first.state, WorkflowState.DISCOVERING)
        self.assertEqual(replay.state, WorkflowState.DISCOVERING)
        count = self.store._db.execute(
            "SELECT COUNT(*) count FROM workflow_transitions WHERE run_id='run-1'"
        ).fetchone()["count"]
        self.assertEqual(count, 1)

    def test_sqlite_rejects_a_second_nonterminal_workflow(self):
        self.repository.create("active-run")
        with self.assertRaisesRegex(ValueError, "already active"):
            self.repository.create("competing-run")
        self.assertEqual(self.repository.latest_resumable().run_id, "active-run")

    def test_loop_manager_selects_from_durable_state_and_legal_graph(self):
        self.engine.create("managed-run")
        manager = LoopManager(self.engine)
        created = manager.select(self.repository.get("managed-run"))
        self.assertEqual(created.kind, LoopCommandKind.START)
        self.assertIn(WorkflowEvent.START, created.legal_events)
        self.engine.transition("managed-run", WorkflowEvent.START)
        discovering = manager.select(self.repository.get("managed-run"))
        self.assertEqual(discovering.kind, LoopCommandKind.ASSESS_DISCOVERY)
        self.assertIn(WorkflowEvent.DISCOVERY_COMPLETE, discovering.legal_events)
        diverging = manager.advance("managed-run", LoopSignal.DISCOVERY_ADEQUATE, {"source": "test"})
        self.assertEqual(diverging.state, WorkflowState.DIVERGING)
        with self.assertRaisesRegex(ValueError, "illegal from DIVERGING"):
            manager.advance("managed-run", LoopSignal.VALIDATION_PASSED)

    def test_corrective_transition_invalidates_downstream_state_without_erasing_history(self):
        self.engine.create("corrected-run")
        self.engine.transition("corrected-run", WorkflowEvent.START)
        self.engine.transition("corrected-run", WorkflowEvent.DISCOVERY_COMPLETE)
        self.repository.save_proposal(
            run_id="corrected-run", operation_id="op-1", expert_id="architect",
            perspective="system", round_number=1,
            proposal=ExpertProposal(components=[
                ProposalComponent(name="Old component", responsibility="Must be invalidated"),
            ]),
        )
        before = self.repository.get("corrected-run").state_version
        corrected = LoopManager(self.engine).correct(
            "corrected-run", WorkflowEvent.REOPEN_DISCOVERY, "The product boundary changed",
        )
        self.assertEqual(corrected.state, WorkflowState.DISCOVERING)
        self.assertEqual(corrected.state_version, before + 1)
        self.assertEqual(self.repository.proposals("corrected-run"), [])
        transitions = self.store._db.execute(
            "SELECT event FROM workflow_transitions WHERE run_id='corrected-run' ORDER BY id"
        ).fetchall()
        self.assertEqual([row["event"] for row in transitions], [
            "start", "discovery_complete", "reopen_discovery",
        ])
        invalidation = self.store._db.execute(
            "SELECT reason,target_state FROM workflow_invalidations WHERE run_id='corrected-run'"
        ).fetchone()
        self.assertEqual(invalidation["reason"], "The product boundary changed")
        self.assertEqual(invalidation["target_state"], "DISCOVERING")

    def test_corrective_reason_is_enforced_by_typed_payload_model(self):
        self.engine.create("reason-run")
        self.engine.transition("reason-run", WorkflowEvent.START)
        self.engine.transition("reason-run", WorkflowEvent.DISCOVERY_COMPLETE)
        for payload in ({}, {"reason": "   "}, {"reason": "valid", "unexpected": True}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.engine.transition("reason-run", WorkflowEvent.REOPEN_DISCOVERY, payload)
        self.assertEqual(self.repository.get("reason-run").state, WorkflowState.DIVERGING)

    def test_fresh_schema_has_no_section_documents_or_global_run_state(self):
        tables = {
            row["name"] for row in self.store._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertNotIn("planning_documents", tables)
        self.assertNotIn("planning_sections", tables)
        self.assertNotIn("planning_mutations", tables)
        self.assertFalse(hasattr(self.store, "save_run_state"))
        self.assertFalse(hasattr(self.store, "load_run_state"))

    def test_waiting_state_resumes_exact_persisted_state(self):
        self.engine.create("run-2")
        self.engine.transition("run-2", "start")
        waiting = self.engine.transition(
            "run-2", "question_required", {"resume_state": "DISCOVERING", "question_id": "q1"}
        )
        self.assertEqual(waiting.state, WorkflowState.WAITING_FOR_USER)
        self.assertEqual(waiting.allowed_actions, ["answer", "cancel"])
        resumed = self.engine.transition("run-2", "answer_recorded", {"question_id": "q1"})
        self.assertEqual(resumed.state, WorkflowState.DISCOVERING)
        self.assertIsNone(resumed.resume_state)

    def test_illegal_and_malformed_transitions_are_rejected(self):
        self.engine.create("run-3")
        with self.assertRaisesRegex(ValueError, "illegal"):
            self.engine.transition("run-3", "valid")
        with self.assertRaisesRegex(ValueError, "failure_detail must be a JSON object"):
            self.engine.transition("run-3", "cancel", {"failure_detail": []})
        with self.assertRaisesRegex(ValueError, "valid JSON"):
            self.engine.transition("run-3", "cancel", {"not_json": object()})
        with self.assertRaises(ValueError):
            self.engine.transition("run-3", "cancel", [])

    def test_corrupt_stored_json_has_a_typed_failure(self):
        self.engine.create("run-corrupt")
        self.store._db.execute(
            "UPDATE workflow_instances SET failure_detail_json='not-json' WHERE run_id='run-corrupt'"
        )
        self.store._db.commit()
        with self.assertRaises(StoredJSONError):
            self.repository.get("run-corrupt")

    def test_proposals_validate_before_persistence_and_are_idempotent(self):
        proposal = ExpertProposal(components=[ProposalComponent(
            name="Workflow engine", responsibility="Own transitions", interfaces=["SQLite"],
        )])
        for _ in range(2):
            self.repository.save_proposal(
                run_id="run-4", operation_id="op-1", expert_id="architect",
                perspective="system", round_number=1, proposal=proposal,
            )
        stored = self.repository.proposals("run-4")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["proposal"].components[0].name, "Workflow engine")
        summary_id = self.repository.save_summary(
            "run-4", "proposal", stored[0]["id"], "planning-v1", {"components": ["Workflow engine"]},
        )
        self.assertEqual(self.repository.save_summary(
            "run-4", "proposal", stored[0]["id"], "planning-v1", {"components": ["Workflow engine"]},
        ), summary_id)
        self.assertEqual(len(self.repository.summaries("run-4")), 1)

    def test_concurrent_proposal_writes_do_not_deadlock_or_duplicate(self):
        errors = []
        second_store = ProjectStore(Path(self.tmp.name))
        second_repository = WorkflowRepository(second_store)

        def write(index):
            try:
                repository = self.repository if index % 2 == 0 else second_repository
                repository.save_proposal(
                    run_id="run-5", operation_id="op-1", expert_id=f"expert-{index}",
                    perspective="test", round_number=1, proposal=ExpertProposal(),
                )
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(index,)) for index in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(self.repository.proposals("run-5")), 12)
        second_store.close()

    def test_lexical_semantic_fallback_is_stable(self):
        analyzer = LexicalSemanticAnalyzer()
        claims = [("b", "API rate limiting layer"), ("a", "API rate limiting layer"),
                  ("c", "user profile avatar storage")]
        first = analyzer.classify_pairs(claims)
        second = analyzer.classify_pairs(list(reversed(claims)))
        self.assertEqual(first, second)
        self.assertEqual(first[0].relation, "duplicate")
        self.assertEqual(analyzer.rank("rate limiting", claims, 2)[0][0], "a")

    def test_local_embedding_analyzer_uses_offline_vectors_and_stable_ties(self):
        vectors = {
            "rate limiter": [1.0, 0.0], "throttling service": [0.99, 0.01],
            "avatar store": [0.0, 1.0], "query": [1.0, 0.0],
        }
        analyzer = LocalEmbeddingAnalyzer(encoder=lambda texts: [vectors[text] for text in texts])
        self.assertTrue(analyzer.available)
        self.assertGreater(analyzer.similarity("rate limiter", "throttling service"), 0.99)
        ranked = analyzer.rank("query", [("b", "throttling service"), ("a", "rate limiter"),
                                          ("c", "avatar store")], 2)
        self.assertEqual([item[0] for item in ranked], ["a", "b"])

    def test_local_embedding_analyzer_never_downloads_and_falls_back(self):
        analyzer = LocalEmbeddingAnalyzer(model_path="/path/that/does/not/exist")
        self.assertFalse(analyzer.available)
        self.assertEqual(
            analyzer.similarity("same local claim", "same local claim"),
            LexicalSemanticAnalyzer().similarity("same local claim", "same local claim"),
        )

    def test_sqlite_semantic_index_round_trips_vector_metadata(self):
        index = SQLiteSemanticIndex(self.store)
        index.put("run-vector", "claim-1", "rate limiter", [0.25, 0.5, 0.75], "test-model", "v1")
        vector = index.get("run-vector", "claim-1", "test-model", "v1")
        self.assertEqual(vector, [0.25, 0.5, 0.75])

    def test_context_budget_preserves_mandatory_fields_and_drops_low_priority(self):
        compiler = ContextCompiler(max_tokens=256)
        summaries = [ContextItem(
            id=f"s-{index}", text="x" * 1200, source_type="proposal", source_id=str(index),
            priority=5, relevance=0.1,
        ) for index in range(3)]
        packet = compiler.compile(
            goal="Build a reliable planner", constraints=["Local first"],
            confirmed_decisions=["SQLite is authoritative"], operation_instructions="Review proposals",
            summaries=summaries,
        )
        self.assertEqual(packet.goal, "Build a reliable planner")
        self.assertEqual(packet.confirmed_decisions, ["SQLite is authoritative"])
        self.assertEqual(packet.relevant_summaries, [])
        self.assertLessEqual(packet.estimated_tokens, 256)

    def test_materiality_is_deterministic(self):
        self.assertEqual(classify_materiality("message encryption", ["on", "off"]), "high")
        self.assertEqual(classify_materiality("deployment cost", ["small", "large"]), "medium")
        self.assertEqual(classify_materiality("component naming", ["A", "B"]), "low")
        self.assertEqual(classify_materiality("naming", [], conflicts_with_confirmed=True), "high")
        self.assertEqual(classify_materiality("GDPR handling", ["retain PII", "delete PII"]), "high")

    def test_sqlite_durability_pragmas_are_enabled(self):
        self.assertEqual(self.store._db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertEqual(self.store._db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertGreaterEqual(self.store._db.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_local_analysis_persists_conflicts_and_projects_required_artifacts(self):
        first = ExpertProposal(
            components=[ProposalComponent(name="Workflow", responsibility="Persist states")],
            decisions=[ProposalDecision(
                topic="message encryption", recommendation="Encrypt end to end",
                rationale="Protect message content", alternatives=["Service readable"],
            )],
            risks=[ProposalRisk(risk="State corruption", mitigation="Use transactions")],
            assumptions=["SQLite is available"],
            unknowns=[ProposalUnknown(question="Provider retry behavior?", validation="Use contract tests")],
        )
        second = ExpertProposal(decisions=[ProposalDecision(
            topic="message encryption", recommendation="Use service-readable storage",
            rationale="Support moderation", alternatives=["End to end"],
        )])
        for expert, proposal in (("architect", first), ("product", second)):
            self.repository.save_proposal(
                run_id="run-6", operation_id="op-1", expert_id=expert,
                perspective=expert, round_number=1, proposal=proposal,
            )
        service = PlanningService(self.repository)
        analysis = service.analyze("run-6")
        self.assertEqual(len(analysis.conflicts), 1)
        self.assertEqual(analysis.conflicts[0].materiality, "high")
        projection = service.project("run-6", "Build a private messaging product")
        self.assertIn("```mermaid", projection.design)
        self.assertIn("## Known Unknowns & Validation Plan", projection.design)
        self.assertIn("## Requirement Traceability", projection.plan)
        self.assertIn("## Implementation Phases", projection.plan)
        self.assertIn("- [ ]", projection.plan)
        self.assertIn("message encryption", projection.decisions)

    def test_projection_diagram_represents_product_components(self):
        proposal = ExpertProposal(components=[
            ProposalComponent(name="Mobile Client", responsibility="Capture commands", interfaces=["Control API"]),
            ProposalComponent(name="Control API", responsibility="Authorize commands", interfaces=[]),
        ])
        self.repository.save_proposal(
            run_id="run-diagram", operation_id="op-1", expert_id="architect",
            perspective="system", round_number=1, proposal=proposal,
        )
        projection = PlanningService(self.repository).project("run-diagram", "Control field equipment")
        self.assertIn('C1["Mobile Client"]', projection.design)
        self.assertIn('C2["Control API"]', projection.design)
        self.assertIn("C1 --> C2", projection.design)
        self.assertNotIn("Typed SQLite blackboard", projection.design)

    def test_repository_document_is_included_in_proposal_context(self):
        workspace = Workspace(self.tmp.name)
        workspace.ensure()
        workspace.write_src("PRODUCT_SPEC.md", "A dispatch console coordinates ambulance crews.")
        tree = ContextTree(self.store)
        tree.sync_workspace(workspace, "run-context")
        context = tree.retrieve(
            query="ambulance dispatch", run_id="run-context", max_tokens=1000,
        )
        self.assertIn("PRODUCT_SPEC.md", context.text)
        self.assertIn("ambulance crews", context.text)

    def test_context_tree_uses_complete_node_or_complete_summary_never_prefix_cut(self):
        tree = ContextTree(self.store)
        full = "BEGIN-EVIDENCE\n" + ("implementation detail without boundary\n" * 400) + "END-EVIDENCE"
        summary = "Complete structural summary."
        node_id = tree.upsert(
            node_type="section", source_type="repository", source_ref="SPEC.md#large",
            title="Large specification", content=full, summary=summary,
        )
        packet = tree.retrieve(query="specification", max_tokens=256, mandatory_types=())
        self.assertIn(summary, packet.text)
        self.assertNotIn("BEGIN-EVIDENCE", packet.text)
        self.assertNotIn("END-EVIDENCE", packet.text)
        self.assertIn(node_id, packet.summary_node_ids)

    def test_context_tree_includes_parent_summary_for_selected_section(self):
        workspace = Workspace(self.tmp.name)
        workspace.ensure()
        workspace.write_src(
            "SPEC.md",
            "# Dispatch Product\n\n## Authentication\nAmbulance dispatchers use hardware-backed identity.\n",
        )
        tree = ContextTree(self.store)
        tree.sync_workspace(workspace, "run-parent")
        packet = tree.retrieve(
            query="hardware-backed dispatcher identity", run_id="run-parent", max_tokens=1000,
            mandatory_types=(),
        )
        self.assertIn("PARENT SUMMARY", packet.text)
        self.assertIn("## Authentication", packet.text)

    def test_agents_receive_rendered_context_not_blackboard_access(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Build a context-safe planner")
        agent = JsonProposalAgent(AgentConfig(name="architect", kind="openai"))
        orchestrator = Orchestration(
            agents=[agent], workspace=workspace, store=self.store, run_id="run-boundary",
        )
        self.assertFalse(hasattr(agent, "store"))
        self.assertFalse(hasattr(agent, "repository"))
        self.assertFalse(hasattr(agent, "context_tree"))
        asyncio.run(run_with_approved_review(orchestrator, "Build a context-safe planner"))
        self.assertGreater(
            self.store._db.execute("SELECT COUNT(*) FROM context_nodes").fetchone()[0], 0,
        )

    def test_semantic_interaction_routes_question_to_read_only_answer(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Build a dispatch console")
        workspace.write("decisions", "# Key Decisions\n\n- SQLite is authoritative.\n")
        agent = ScriptedInteractionAgent(
            AgentConfig(name="architect", kind="openai"),
            ["The design has selected SQLite; implementation remains pending."],
        )
        service = InteractionService(agent, workspace, self.store, {})
        decision = asyncio.run(service.route("Can you summarize our progress in plain language"))
        self.assertEqual(decision.kind, InteractionKind.ANSWER)
        answer = asyncio.run(service.answer("Can you summarize our progress in plain language"))
        self.assertIn("implementation remains pending", answer)

    def test_direct_answer_receives_exact_runtime_provider_and_model(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Build a dispatch console")
        agent = ScriptedInteractionAgent(
            AgentConfig(
                name="architect_alpha", kind="aws-bedrock",
                model="apac.anthropic.claude-3-haiku-20240307-v1:0",
            ),
            ["I am using Claude 3 Haiku through AWS Bedrock."],
        )
        service = InteractionService(agent, workspace, self.store, {})
        asyncio.run(service.answer("Are you using any specific model?"))
        self.assertIn("provider=aws-bedrock", agent.history[0].content)
        self.assertIn(
            "model=apac.anthropic.claude-3-haiku-20240307-v1:0",
            agent.history[0].content,
        )

    def test_typed_challenge_gets_one_bounded_schema_repair(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Build a dispatch console")
        agent = ScriptedInteractionAgent(
            AgentConfig(name="architect_beta", kind="aws-bedrock"),
            ["not json", '{"challenges":[],"validated_topics":["offline sync"]}'],
        )
        events = []
        orchestrator = Orchestration(
            agents=[agent], workspace=workspace, store=self.store, run_id="repair-run",
            event_cb=events.append,
        )
        value, _ = asyncio.run(orchestrator._typed_debate_call(
            agent, prompt="Review the proposal", system="Return JSON", phase="diverging",
            turn_kind="challenge", model=DebateReview,
        ))
        self.assertEqual(value.validated_topics, ["offline sync"])
        attempts = [
            event.data["attempt"] for event in events
            if event.kind.value == "turn_start"
        ]
        self.assertEqual(attempts, [1, 2])
        participants = self.store._db.execute(
            "SELECT agent_id FROM run_participants WHERE run_id='repair-run'",
        ).fetchall()
        self.assertEqual([row["agent_id"] for row in participants], ["architect_beta"])

    def test_ambiguous_local_interaction_route_fails_safe_to_read_only(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Build a dispatch console")
        agent = ScriptedInteractionAgent(
            AgentConfig(name="architect", kind="openai"), ["not-json"],
        )
        decision = asyncio.run(InteractionService(agent, workspace, self.store).route("Ambiguous request"))
        self.assertEqual(decision.kind, InteractionKind.ANSWER)

    def test_local_interaction_router_selects_planning_without_calling_provider(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Build a dispatch console")
        agent = MustNotRunAgent(AgentConfig(name="architect", kind="openai"))
        decision = asyncio.run(
            InteractionService(agent, workspace, self.store).route(
                "Design and refine the architecture and implementation plan"
            )
        )
        self.assertEqual(decision.kind, InteractionKind.PLANNING)

    def test_semantic_router_contract_keeps_discovery_inside_planning(self):
        router_prompt = (
            Path(__file__).parents[1] / "backend" / "prompts" / "intent_router_task.md"
        ).read_text()
        self.assertIn("Missing information does not turn a planning request into an answer", router_prompt)
        self.assertIn("plain user-facing Markdown with no JSON", router_prompt)

    def test_v2_end_to_end_reaches_completed_from_typed_proposal(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Build a reliable planner")
        agent = JsonProposalAgent(AgentConfig(name="architect", kind="openai", role="System architect"))
        orchestrator = Orchestration(
            agents=[agent], workspace=workspace, store=self.store, run_id="run-e2e",
        )
        snapshot = asyncio.run(run_with_approved_review(orchestrator, "Build a reliable planner"))
        durable = self.repository.get("run-e2e")
        self.assertEqual(durable.state, WorkflowState.COMPLETED)
        self.assertIn("## Architecture", snapshot["design"])
        self.assertIn("## Implementation Phases", snapshot["plan"])
        self.assertEqual(orchestrator.completion_files, ["DESIGN.md", "PLAN.md", "DECISIONS.md"])

    def test_human_review_steering_is_authoritative_revision_input(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Build a reliable planner")
        agent = SteeringAwareAgent(AgentConfig(name="architect", kind="openai"))
        orchestrator = Orchestration(
            agents=[agent], workspace=workspace, store=self.store, run_id="run-human-review",
        )

        async def exercise():
            task = asyncio.create_task(orchestrator.run("Build a reliable planner"))
            for _ in range(100):
                await asyncio.sleep(0)
                checkpoint = self.store.current_checkpoint("run-human-review")
                if checkpoint and checkpoint.get("phase") == "design_review":
                    steering = "Keep all data local and remove cloud synchronization."
                    self.store.answer_checkpoint("run-human-review", checkpoint["id"], "test", "", steering)
                    await orchestrator.accept_structured_checkpoint_answer(steering, False, "test")
                    await task
                    return
            task.cancel()
            self.fail("design review checkpoint was not created")

        asyncio.run(exercise())
        self.assertIn("Keep all data local and remove cloud synchronization", agent.revision_input)

    def test_approved_unchallenged_proposal_skips_redundant_revision_call(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Build a reliable planner")
        agent = BareRevisionAgent(AgentConfig(name="architect", kind="openai"))
        orchestrator = Orchestration(
            agents=[agent],
            workspace=workspace, store=self.store, run_id="run-bare-revision",
        )
        asyncio.run(run_with_approved_review(orchestrator, "Build a reliable planner"))
        self.assertEqual(self.repository.get("run-bare-revision").state, WorkflowState.COMPLETED)
        revision = self.repository.debate_turns("run-bare-revision")[-1]
        self.assertEqual(revision["turn_kind"], "revision")
        self.assertEqual(revision["payload"]["dispositions"], [])
        self.assertEqual(agent.revision_calls, 0)

    def test_discovery_uncertainty_is_recorded_without_interrupting_user(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("just testing")
        events = []
        orchestrator = Orchestration(
            agents=[BlockingDiscoveryAgent(AgentConfig(name="architect", kind="openai"))],
            workspace=workspace, store=self.store, run_id="run-discovery-blocked",
            event_cb=events.append,
        )

        async def exercise():
            await run_with_approved_review(orchestrator, "just testing")
            self.assertEqual(self.repository.get("run-discovery-blocked").state, WorkflowState.COMPLETED)
            self.assertEqual(self.store.current_checkpoint("run-discovery-blocked"), {})
            unknowns = self.store._db.execute(
                "SELECT content FROM context_nodes "
                "WHERE run_id='run-discovery-blocked' AND node_type='unknown'"
            ).fetchall()
            self.assertEqual(len(unknowns), 1)
            self.assertIn("Who uses this product", unknowns[0]["content"])
            waiting_events = [
                event for event in events
                if event.kind.value == "phase" and event.data.get("workflow_state") == "WAITING_FOR_USER"
                and event.data.get("phase") == "discovering"
            ]
            self.assertEqual(waiting_events, [])

        asyncio.run(exercise())

    def test_invalid_discovery_json_falls_back_without_failing_run(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Build a bounded planner")
        orchestrator = Orchestration(
            agents=[InvalidDiscoveryAgent(AgentConfig(name="architect", kind="openai"))],
            workspace=workspace, store=self.store, run_id="run-invalid-discovery",
        )
        asyncio.run(run_with_approved_review(orchestrator, "Build a bounded planner"))
        self.assertEqual(self.repository.get("run-invalid-discovery").state, WorkflowState.COMPLETED)
        assumptions = self.store._db.execute(
            "SELECT content FROM context_nodes "
            "WHERE run_id='run-invalid-discovery' AND node_type='assumption'"
        ).fetchall()
        self.assertTrue(any("Validate discovery completeness" in row["content"] for row in assumptions))

    def test_sequential_debate_persists_one_canonical_revision_and_one_user_verdict(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Build a bounded planner")
        events = []
        orchestrator = Orchestration(
            agents=[
                JsonProposalAgent(AgentConfig(name="architect_alpha", kind="openai")),
                JsonProposalAgent(AgentConfig(name="architect_beta", kind="openai")),
            ],
            workspace=workspace, store=self.store, run_id="run-duplicate-debate",
            event_cb=events.append,
        )
        asyncio.run(run_with_approved_review(orchestrator, "Build a bounded planner"))
        self.assertEqual(self.repository.get("run-duplicate-debate").state, WorkflowState.COMPLETED)
        self.assertEqual(len(self.repository.proposals("run-duplicate-debate")), 1)
        turns = self.repository.debate_turns("run-duplicate-debate")
        self.assertEqual([turn["turn_kind"] for turn in turns], ["opening", "challenge", "revision"])
        visible_verdicts = [event for event in events if event.kind.value == "verdict"]
        self.assertEqual(len(visible_verdicts), 1)

    def test_debate_depth_bounds_revision_cycles_not_reviewer_count(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Build a bounded planner")
        reviewer = OneChallengeAgent(AgentConfig(
            name="security_auditor", kind="openai", role="security_auditor",
            extra={"review_signals": ["authorization", "permissions", "security"]},
        ))
        orchestrator = Orchestration(
            agents=[
                AcceptingCoordinatorAgent(AgentConfig(name="architect_alpha", kind="openai")),
                reviewer,
            ],
            workspace=workspace, store=self.store, run_id="run-depth-three",
            max_debate_rounds=3,
        )
        asyncio.run(run_with_approved_review(orchestrator, "Build a planner with server authorization"))
        turns = self.repository.debate_turns("run-depth-three")
        self.assertEqual(
            [turn["turn_kind"] for turn in turns],
            ["opening", "challenge", "round_revision", "challenge", "revision"],
        )
        self.assertEqual(reviewer.review_calls, 2)
        self.assertEqual([agent.name for agent in orchestrator.participants], ["architect_alpha", "security_auditor"])

    def test_generic_provider_is_not_eligible_as_logical_reviewer(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Notify me 15 minutes before an event")
        coordinator = JsonProposalAgent(AgentConfig(name="architect_alpha", kind="openai"))
        security = JsonProposalAgent(AgentConfig(
            name="security_auditor", kind="openai", role="security_auditor",
            system_prompt="Review authorization and privacy boundaries.",
            extra={"review_category": "security", "review_signals": ["privacy", "authorization"]},
        ))
        generic_provider = JsonProposalAgent(AgentConfig(name="bedrock-2", kind="aws-bedrock"))
        orchestrator = Orchestration(
            agents=[coordinator, security, generic_provider], workspace=workspace,
            store=self.store, run_id="run-specialist-selection",
        )
        selected = orchestrator._select_reviewers("Review privacy and authorization", coordinator)
        self.assertEqual([agent.name for agent in selected], ["security_auditor"])

    def test_debate_system_includes_persona_and_authoritative_contract(self):
        agent = JsonProposalAgent(AgentConfig(
            name="security_auditor", kind="openai", role="security_auditor",
            system_prompt="Hunt for authorization and privacy failures.",
        ))
        composed = Orchestration._debate_system(agent, "Return the typed review JSON.")
        self.assertIn("Hunt for authorization and privacy failures.", composed)
        self.assertIn("orchestration contract is authoritative", composed)
        self.assertTrue(composed.endswith("Return the typed review JSON."))

    def test_scope_expansion_waits_for_human_and_approval_does_not_rewrite(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Notify me 15 minutes before an event")
        coordinator = BareRevisionAgent(AgentConfig(name="architect_alpha", kind="openai"))
        reviewer = ScopeExpansionReviewer(AgentConfig(
            name="ux_simplifier", kind="openai", role="ux_simplifier",
            system_prompt="Review user experience without expanding scope.",
            extra={"review_category": "ux", "review_signals": ["notification", "preference"]},
        ))
        orchestrator = Orchestration(
            agents=[coordinator, reviewer], workspace=workspace, store=self.store,
            run_id="run-scope-gate", max_debate_rounds=3,
        )
        asyncio.run(run_with_approved_review(orchestrator, "Notify me 15 minutes before an event"))
        turns = self.repository.debate_turns("run-scope-gate")
        self.assertEqual([turn["turn_kind"] for turn in turns], ["opening", "challenge", "revision"])
        self.assertEqual(coordinator.revision_calls, 0)
        self.assertEqual(turns[-1]["payload"]["dispositions"][0]["status"], "defended")
        checkpoint = self.store.run_checkpoints("run-scope-gate")[0]
        self.assertEqual(checkpoint["options"][0]["summary"], "Keep the stated scope")

    def test_restart_from_diverging_reuses_persisted_proposal(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Build a restart-safe planner")
        self.engine.create("run-resume")
        self.engine.transition("run-resume", "start", {"goal": "Build a restart-safe planner"})
        self.engine.transition("run-resume", "discovery_complete", {"source": "brief"})
        self.repository.save_proposal(
            run_id="run-resume", operation_id="diverge-run-resume-1", expert_id="architect",
            perspective="system", round_number=1, proposal=ExpertProposal(
                components=[ProposalComponent(name="State engine", responsibility="Resume safely")],
            ),
        )
        agent = MustNotRunAgent(AgentConfig(name="architect", kind="openai", role="System architect"))
        orchestrator = Orchestration(
            agents=[agent], workspace=workspace, store=self.store, run_id="run-resume",
        )
        asyncio.run(orchestrator.run("Build a restart-safe planner"))
        self.assertEqual(self.repository.get("run-resume").state, WorkflowState.COMPLETED)

    def test_restart_from_analyzing_does_not_replay_discovery_or_providers(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Build a restart-safe planner")
        self.engine.create("run-analyzing")
        self.engine.transition("run-analyzing", "start")
        self.engine.transition("run-analyzing", "discovery_complete", {"source": "test"})
        self.repository.save_proposal(
            run_id="run-analyzing", operation_id="diverge-run-analyzing-1", expert_id="architect",
            perspective="system", round_number=1, proposal=ExpertProposal(
                components=[ProposalComponent(name="State engine", responsibility="Resume analysis")],
            ),
        )
        self.engine.transition("run-analyzing", "all_required_proposals_stored", {
            "operation_id": "diverge-run-analyzing-1", "accepted": 1, "requested": 1,
        })
        orchestrator = Orchestration(
            agents=[MustNotRunAgent(AgentConfig(name="architect", kind="openai"))],
            workspace=workspace, store=self.store, run_id="run-analyzing",
        )
        asyncio.run(orchestrator.run("Build a restart-safe planner"))
        self.assertEqual(self.repository.get("run-analyzing").state, WorkflowState.COMPLETED)

    def test_completed_run_closes_active_proposal_operation(self):
        workspace = Workspace(self.tmp.name)
        workspace.init("Build an operation-safe planner")
        orchestrator = Orchestration(
            agents=[JsonProposalAgent(AgentConfig(name="architect", kind="openai"))],
            workspace=workspace, store=self.store, run_id="run-operation",
        )
        asyncio.run(run_with_approved_review(orchestrator, "Build an operation-safe planner"))
        row = self.store._db.execute(
            "SELECT status,completed_at FROM workflow_operations WHERE run_id='run-operation'"
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertIsNotNone(row["completed_at"])
        self.assertIsNone(self.repository.get("run-operation").active_operation_id)


if __name__ == "__main__":
    unittest.main()
