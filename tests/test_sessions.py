"""Session and runtime contracts for the single DesignFlow orchestration engine."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

# Server modules initialize process-wide infrastructure at import time. Keep its
# audit store isolated from the developer's real DesignFlow data.
os.environ.setdefault("DESIGNFLOW_TEST", "1")

from backend.agents.base import AgentBase, AgentConfig, Usage
from backend.orchestration import Orchestration
from backend.server import app, model_for_virtual_agent
from backend.storage import ProjectStore
from backend.workflow import WorkflowRepository, WorkflowState
from backend.workspace.workspace import Workspace


class ProposalAgent(AgentBase):
    manages_context = True

    def __init__(self, config: AgentConfig, payload: dict | None = None):
        super().__init__(config)
        self.calls = 0
        self.payload = payload or {
            "components": [{
                "name": "Workflow engine",
                "responsibility": "Persist legal transitions",
                "interfaces": ["SQLite"],
            }],
            "decisions": [{
                "topic": "state ownership",
                "recommendation": "SQLite is authoritative",
                "rationale": "A restart must reconstruct the exact state",
                "alternatives": ["process memory"],
            }],
            "risks": [{"risk": "write contention", "mitigation": "WAL and bounded transactions"}],
            "assumptions": ["one active planning run per project"],
            "unknowns": [{"question": "What is peak write load?", "validation": "Run concurrency tests"}],
        }

    def _raw_send(self, messages, system, *args, **kwargs):
        self.calls += 1
        if "discovery gate" in system:
            return json.dumps({
                "adequate": True,
                "evidence_summary": "A bounded planner and its reliability outcome are established.",
                "blocking_questions": [],
            }), Usage(input_tokens=10, output_tokens=10)
        if "reviewing a concrete architecture proposal" in system:
            return json.dumps({"challenges": [], "validated_topics": ["state ownership"]}), Usage(input_tokens=20, output_tokens=10)
        if "coordinating architect" in system:
            return json.dumps({"proposal": self.payload, "dispositions": []}), Usage(input_tokens=40, output_tokens=60)
        return json.dumps(self.payload), Usage(input_tokens=40, output_tokens=60)


class InvalidProposalAgent(AgentBase):
    manages_context = True

    def _raw_send(self, messages, system, *args, **kwargs):
        if "discovery gate" in system:
            return '{"adequate":true,"evidence_summary":"The test goal is bounded.","blocking_questions":[]}', Usage(input_tokens=5, output_tokens=5)
        return "not-json", Usage(input_tokens=5, output_tokens=2)


async def run_with_approved_review(orchestrator, goal):
    task = asyncio.create_task(orchestrator.run(goal))
    for _ in range(100):
        await asyncio.sleep(0)
        checkpoint = orchestrator.store.current_checkpoint(orchestrator.run_id)
        if checkpoint and checkpoint.get("phase") == "design_review":
            option = checkpoint["options"][0]
            orchestrator.store.answer_checkpoint(orchestrator.run_id, checkpoint["id"], "test", option["id"], "")
            await orchestrator.accept_structured_checkpoint_answer(f"{option['label']} — {option['summary']}", False, "test")
            return await task
    task.cancel()
    raise AssertionError("design review checkpoint was not created")


class SessionTests(unittest.TestCase):
    def test_virtual_roles_rotate_through_explicit_model_pool(self):
        config = {"extra": {"available_models": ["model-a", "model-b", "model-c"]}}
        self.assertEqual(
            [model_for_virtual_agent(config, index, 2) for index in range(3)],
            ["model-a", "model-b", "model-c"],
        )

    def test_run_start_never_performs_provider_model_discovery(self):
        server = (Path(__file__).parents[1] / "backend" / "server.py").read_text()
        start = server.index("async def start_run")
        end = server.index("@app.post", start + 20)
        implementation = server[start:end]
        self.assertNotIn("discover_models(", implementation)
        self.assertIn("missing_catalogs", implementation)

    def test_chat_ui_hides_internal_lifecycle_envelopes(self):
        frontend = (Path(__file__).parents[1] / "frontend" / "js" / "api.js").read_text()
        server = (Path(__file__).parents[1] / "backend" / "server.py").read_text()
        self.assertIn("if (ev?.data?.visibility === 'internal' && !internalTurn) return;", frontend)
        self.assertIn("ev.data?.phase !== 'answer'", frontend)
        self.assertGreaterEqual(server.count('"visibility": "internal"'), 2)
        self.assertIn('"visibility": "user"', server)

    def test_run_roster_does_not_substitute_unlaunched_configured_agents(self):
        frontend = (Path(__file__).parents[1] / "frontend" / "js" / "api.js").read_text()
        roster_start = frontend.index("async function fetchAgentStatus")
        roster_end = frontend.index("function workflowUiState", roster_start)
        roster = frontend[roster_start:roster_end]
        self.assertNotIn("const configured = await fetch('/agents')", roster)
        self.assertIn("Run team · no agents active", roster)

    def test_new_prompt_preserves_timeline_and_is_appended_before_dispatch(self):
        frontend = (Path(__file__).parents[1] / "frontend" / "js" / "api.js").read_text()
        start = frontend.index("async function startRun")
        end = frontend.index("function formatDuration", start)
        implementation = frontend[start:end]
        self.assertNotIn("document.getElementById('feed').innerHTML = '';", implementation)
        self.assertLess(
            implementation.index("appendUserPrompt(idea)"),
            implementation.index("fetch('/run/start'"),
        )

    def test_login_does_not_render_workflow_envelopes_as_chat(self):
        frontend = (Path(__file__).parents[1] / "frontend" / "js" / "api.js").read_text()
        server = (Path(__file__).parents[1] / "backend" / "server.py").read_text()
        self.assertIn("ev.kind === 'phase' || ev.kind === 'file_write' || ev.kind === 'done'", frontend)
        self.assertIn(
            "resumable_workflow.state != WorkflowState.WAITING_FOR_USER",
            server,
        )
        self.assertIn('chat_runs = [run] if run.get("run_kind") == "chat" else []', server)
        self.assertNotIn('f"safe-error-{run_id}"', server)

    def test_saved_brief_is_context_not_authorization_for_empty_run(self):
        server = (Path(__file__).parents[1] / "backend" / "server.py").read_text()
        self.assertIn(
            'if not entered_goal:\n        raise HTTPException(400, "Enter a question or planning goal before starting a run")',
            server,
        )
        self.assertNotIn("explicit_planning_action", server)
        self.assertNotIn('effective_mode = "debate"', server)
        self.assertIn(
            "Answer or cancel the active checkpoint before starting new planning work",
            server,
        )

    def test_project_restore_does_not_probe_model_providers(self):
        agents_ui = (Path(__file__).parents[1] / "frontend" / "js" / "agents.js").read_text()
        self.assertNotIn("setTimeout(() => checkAgentHealth(cfg, uid)", agents_ui)
        self.assertIn("Provider checks may consume quota", agents_ui)
        index = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()
        self.assertIn("/js/api.js?v=20260719h", index)
        self.assertIn("/js/agents.js?v=20260719b", index)

    def test_checkpoint_answer_is_transactional_and_confirms_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectStore(Path(directory))
            checkpoint = store.enqueue_checkpoint(
                "run-1", "resolving", "Which storage model should win?",
                "The choice changes consistency and recovery behavior.",
                [
                    {"label": "A", "summary": "SQLite", "consequence": "Local transactional state"},
                    {"label": "B", "summary": "Files", "consequence": "Manual consistency"},
                ],
                dimension="state storage",
            )
            answered, next_checkpoint = store.answer_checkpoint(
                "run-1", checkpoint["id"], "architect", checkpoint["options"][0]["id"], "",
            )
            self.assertEqual(answered["status"], "answered")
            self.assertEqual(next_checkpoint, {})
            decision = store.decision(answered["decision_id"])
            self.assertEqual(decision["status"], "confirmed")
            self.assertIn("SQLite", decision["chosen_option"])
            store.close()

    def test_only_one_checkpoint_is_active_and_pending_promotes_after_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectStore(Path(directory))
            first = store.enqueue_checkpoint(
                "run-2", "resolving", "First choice?", "First material decision rationale.",
                [{"label": "A", "summary": "One"}],
            )
            second = store.enqueue_checkpoint(
                "run-2", "resolving", "Second choice?", "Second material decision rationale.",
                [{"label": "A", "summary": "Two"}],
            )
            self.assertEqual(first["status"], "active")
            self.assertEqual(second["status"], "pending")
            _, promoted = store.answer_checkpoint(
                "run-2", first["id"], "architect", first["options"][0]["id"], "",
            )
            self.assertEqual(promoted["id"], second["id"])
            self.assertEqual(promoted["status"], "active")
            store.close()

    def test_later_chat_run_does_not_hide_durable_planning_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectStore(Path(directory))
            repository = WorkflowRepository(store)
            repository.create("planning-run")
            from backend.workflow import WorkflowEngine
            engine = WorkflowEngine(repository)
            engine.transition("planning-run", "start")
            checkpoint = store.enqueue_checkpoint(
                "planning-run", "discovering", "Who is the user?", "Required scope",
                [
                    {"label": "A", "summary": "Operator", "consequence": "Operator workflow"},
                    {"label": "B", "summary": "Customer", "consequence": "Customer workflow"},
                ],
            )
            engine.transition("planning-run", "question_required", {
                "resume_state": "DISCOVERING", "checkpoint_id": checkpoint["id"],
            })
            store.start_run("later-chat", "What model is this?", run_kind="chat")
            store.finish_run("later-chat", "done", [], outcome={"status": "answered"})
            self.assertEqual(store.latest_current_checkpoint()["id"], checkpoint["id"])
            store.close()

    def test_workflow_and_checkpoint_survive_store_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = ProjectStore(path)
            repository = WorkflowRepository(store)
            repository.create("run-restart")
            repository.save_goal("run-restart", "Build a restart-safe planner")
            checkpoint = store.enqueue_checkpoint(
                "run-restart", "resolving", "Resume choice?", "Required after restart.",
                [{"label": "A", "summary": "Continue"}],
            )
            store.close()

            reopened = ProjectStore(path)
            restored = WorkflowRepository(reopened).get("run-restart")
            restored_checkpoint = reopened.current_checkpoint("run-restart")
            self.assertEqual(restored.state, WorkflowState.CREATED)
            self.assertEqual(restored_checkpoint["id"], checkpoint["id"])
            self.assertEqual(WorkflowRepository(reopened).goal("run-restart"), "Build a restart-safe planner")
            reopened.close()

    def test_agent_credentials_are_encrypted_and_public_config_is_masked(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectStore(Path(directory))
            store.save_agents([{
                "id": "agent-1", "name": "Architect", "kind": "openai", "role": "architect",
                "model": "test", "api_key": "secret-value", "system_prompt": "", "max_history_turns": 2,
                "extra": {},
            }])
            raw = store._db.execute("SELECT config_json FROM agents WHERE id='agent-1'").fetchone()["config_json"]
            self.assertNotIn("secret-value", raw)
            self.assertEqual(store.load_agents()[0]["api_key"], "secret-value")
            store.close()

    def test_completed_run_reopens_without_provider_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("Build a planner")
            store = ProjectStore(workspace.root)
            agent = ProposalAgent(AgentConfig(name="architect", kind="openai", role="architect"))
            first = Orchestration(agents=[agent], workspace=workspace, store=store, run_id="run-complete")
            asyncio.run(run_with_approved_review(first, "Build a planner"))
            self.assertEqual(agent.calls, 3)
            second = Orchestration(agents=[agent], workspace=workspace, store=store, run_id="run-complete")
            asyncio.run(second.run("Build a planner"))
            self.assertEqual(agent.calls, 3)
            self.assertEqual(WorkflowRepository(store).get("run-complete").state, WorkflowState.COMPLETED)
            store.close()

    def test_invalid_expert_output_fails_without_partial_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("Build a planner")
            original_design = workspace.read("design")
            store = ProjectStore(workspace.root)
            engine = Orchestration(
                agents=[InvalidProposalAgent(AgentConfig(name="bad", kind="openai"))],
                workspace=workspace, store=store, run_id="run-invalid",
            )
            with self.assertRaisesRegex(ValueError, "invalid typed proposal"):
                asyncio.run(engine.run("Build a planner"))
            self.assertEqual(WorkflowRepository(store).get("run-invalid").state, WorkflowState.FAILED)
            self.assertEqual(workspace.read("design"), original_design)
            self.assertEqual(store._db.execute(
                "SELECT COUNT(*) count FROM expert_proposals WHERE run_id='run-invalid'"
            ).fetchone()["count"], 0)
            store.close()

    def test_server_status_returns_durable_workflow_shape(self):
        # Do not enter the application lifespan here: the MCP integration suite
        # intentionally owns the process-wide StreamableHTTP session manager.
        client = TestClient(app)
        try:
            login = client.post("/auth/login", json={"username": "admin", "password": "admin"})
            self.assertEqual(login.status_code, 200)
            status = client.get("/run/status")
            self.assertEqual(status.status_code, 200)
            payload = status.json()
            self.assertIn("workflow", payload)
            self.assertIn("status", payload)
            client.post("/auth/logout")
        finally:
            client.close()

    def test_status_source_restores_durable_waiting_workflow_without_live_runtime(self):
        server = (Path(__file__).parents[1] / "backend" / "server.py").read_text()
        frontend = (Path(__file__).parents[1] / "frontend" / "js" / "api.js").read_text()
        self.assertIn("effective_run_id = state.run_id", server)
        self.assertIn('effective_status = "paused"', server)
        self.assertIn("effective_awaiting_input = True", server)
        self.assertIn("if (awaitingDecisionInput) await showInteractiveQuestions();", frontend)

    def test_frontend_maps_authoritative_workflow_states(self):
        source = (Path(__file__).parents[1] / "frontend" / "js" / "api.js").read_text()
        self.assertIn("function workflowUiState(workflow)", source)
        self.assertIn("WAITING_FOR_USER: 'paused'", source)
        self.assertIn("WAITING_FOR_RECOVERY: 'needs_attention'", source)
        self.assertIn("COMPLETED: 'done'", source)
        self.assertIn("awaitingDecision: state === 'WAITING_FOR_USER'", source)

    def test_review_depth_and_user_identity_are_wired_to_the_ui(self):
        root = Path(__file__).parents[1]
        server = (root / "backend" / "server.py").read_text()
        frontend = (root / "frontend" / "js" / "api.js").read_text()
        page = (root / "frontend" / "index.html").read_text()
        self.assertIn("max_debate_rounds=body.max_debate_rounds", server)
        self.assertIn("currentUser?.username || 'User'", frontend)
        self.assertNotIn('<span class="feed-agent">You</span>', frontend)
        self.assertIn("Debate loop depth", page)

    def test_runtime_has_no_v1_or_section_state_authority(self):
        root = Path(__file__).parents[1]
        self.assertFalse((root / "backend" / "orchestrator.py").exists())
        server = (root / "backend" / "server.py").read_text()
        storage = (root / "backend" / "storage.py").read_text()
        self.assertNotIn("backend.orchestrator", server)
        self.assertNotIn("planning_documents", storage)
        self.assertNotIn("planning_sections", storage)
        self.assertNotIn("save_run_state", storage)


if __name__ == "__main__":
    unittest.main()
