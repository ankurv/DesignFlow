import asyncio
import os
os.environ["DESIGNFLOW_TEST"] = "1"
import json
import re
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.agents.base import AgentBase, AgentConfig, Message, Usage
from backend.agents.providers import CLIAgent, GroqAgent, discover_models
from backend.orchestrator import COORDINATOR_SYSTEM, EventKind, Orchestrator, OrchestratorPhase
from backend.errors import classify_provider_error
from backend.debug_observer import DebugObserver
from backend.audit import AuditLog
from backend.storage import ProjectStore
from backend.workspace.workspace import Workspace


class ReleaseVersionTests(unittest.TestCase):
    def test_release_version_is_semver_and_consistent(self):
        from backend.server import app, healthz, version
        from backend.version import VERSION_PATTERN, __version__

        root = Path(__file__).parents[1]
        extension = json.loads((root / "vscode-extension" / "package.json").read_text())
        extension_lock = json.loads((root / "vscode-extension" / "package-lock.json").read_text())
        self.assertRegex(__version__, VERSION_PATTERN)
        self.assertEqual((root / "VERSION").read_text().strip(), __version__)
        self.assertEqual(app.version, __version__)
        self.assertEqual(healthz()["version"], __version__)
        self.assertEqual(version()["version"], __version__)
        self.assertEqual(extension["version"], __version__)
        self.assertEqual(extension_lock["version"], __version__)
        self.assertEqual(extension_lock["packages"][""]["version"], __version__)

    def test_cli_exposes_release_version(self):
        root = Path(__file__).parents[1]
        result = subprocess.run(
            [sys.executable, str(root / "run.py"), "--version"],
            cwd=root, capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), "DesignFlow 0.1.0")

    def test_browser_places_version_in_usage_bar(self):
        root = Path(__file__).parents[1]
        html = (root / "frontend" / "index.html").read_text()
        token_bar = re.search(r'<div class="token-bar">([\s\S]*?)</div>', html)
        self.assertIsNotNone(token_bar)
        self.assertIn('id="appVersion"', token_bar.group(1))
        self.assertEqual(html.count('id="appVersion"'), 1)


class StatefulFake(AgentBase):
    manages_context = True

    def __init__(self, config, replies=None):
        super().__init__(config)
        self.received = []
        self.received_systems = []
        self.replies = iter(replies or ["ok"])

    def _raw_send(self, messages, system, *args, **kwargs):
        self.received.append(messages)
        self.received_systems.append(system)
        return next(self.replies), Usage(
            input_tokens=100,
            cached_input_tokens=40,
            output_tokens=20,
        )


class AuditLogTests(unittest.TestCase):
    def test_audit_log_redacts_sensitive_metadata_and_hashes_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            log = AuditLog(Path(directory) / "audit.db")
            log.record(
                session_id="session-secret", project_path="/private/project",
                username="admin", role="admin", action="agent.configure",
                target="agent-1", result="success", source_ip="127.0.0.1",
                metadata={"api_key": "sk-secret", "changed_fields": ["model"]},
            )
            events = log.query(limit=10)
            log.close()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["metadata"]["api_key"], "[REDACTED]")
            self.assertNotEqual(events[0]["session_hash"], "session-secret")
            self.assertNotIn("/private/project", json.dumps(events[0]))

    def test_audit_action_classification_covers_sensitive_mutations(self):
        from backend.server import audit_action
        self.assertEqual(audit_action("POST", "/run/start"), "run.start")
        self.assertEqual(audit_action("PUT", "/agents/a1"), "agent.configure")
        self.assertEqual(audit_action("POST", "/workspace/file/design"), "artifact.update")
        self.assertEqual(audit_action("POST", "/session/heartbeat"), "")


class StructuredCheckpointTests(unittest.TestCase):
    def test_resuming_run_preserves_identity_start_time_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectStore(Path(directory))
            store.start_run("run-stable", "Build it")
            before = store.recent_runs()[0]
            store.finish_run("run-stable", "stopped", [{
                "total_tokens": 25, "cached_input_tokens": 5,
                "cost_usd": 0.01, "pricing_known": True,
            }])
            store.resume_run("run-stable")
            after = store.recent_runs()[0]
            self.assertEqual(after["run_id"], "run-stable")
            self.assertEqual(after["started_at"], before["started_at"])
            self.assertEqual(after["total_tokens"], 25)
            self.assertEqual(after["status"], "running")
            self.assertIsNone(after["completed_at"])
            store.close()

    def test_resuming_logbook_appends_without_overwriting_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.begin_logbook_run("run-stable", "Original task")
            transcript = workspace.root / "logbook" / "run-stable.md"
            original = transcript.read_text()
            workspace.finish_logbook_run("run-stable", "stopped")
            workspace.resume_logbook_run("run-stable")
            resumed = transcript.read_text()
            self.assertIn("Original task", resumed)
            self.assertIn("## Resumed", resumed)
            self.assertTrue(resumed.startswith(original.split("## Transcript")[0]))

    def test_server_orchestrator_persists_bundled_checkpoint_text_in_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("Build a service")
            store = ProjectStore(workspace.root)
            store.start_run("run-structured", "Build a service")
            orchestrator = Orchestrator([], workspace, store=store, run_id="run-structured")
            bundled = """Decision 1: Which transport should clients use?
- [A] HTTP
- [B] Local socket

Decision 2: How long should events be retained?
- [A] 7 days
- [B] 30 days
"""
            self.assertTrue(orchestrator._enqueue_checkpoint_text(bundled))
            checkpoints = store.run_checkpoints("run-structured")
            self.assertEqual([item["status"] for item in checkpoints], ["active", "pending"])
            projection = workspace.read("questions")
            self.assertIn("Which transport", projection)
            self.assertNotIn("How long should", projection)
            store.close()

    def test_resume_cannot_bypass_active_structured_checkpoint(self):
        from fastapi import HTTPException
        from backend.server import AppState, resume_run

        with tempfile.TemporaryDirectory() as directory:
            state = AppState()
            state.store = ProjectStore(Path(directory))
            state.run_id = "run-paused"
            state.store.start_run(state.run_id, "Build a service")
            state.store.enqueue_checkpoint(
                state.run_id, "approval", "Which transport should clients use?", "Affects deployment.",
                [{"label": "A", "summary": "HTTP"}, {"label": "B", "summary": "Local socket"}],
            )
            state.orchestrator = SimpleNamespace(failed_turn=None, resume=lambda: None, stop=lambda: None)
            state.status = "paused"
            state.awaiting_input = True
            with self.assertRaises(HTTPException) as raised:
                resume_run(None, state)
            self.assertEqual(raised.exception.status_code, 409)
            state.close()

    def test_bold_unbulleted_legacy_decisions_split_and_convert(self):
        legacy = """# Decision Checkpoint

**Decision 31: Delivery Contract**:

**Option A (Recommended)**: Best-effort, at-most-once delivery.
**Option B**: Selectable durability acknowledgments.
**Decision 32: Version Compatibility Policy**:

**Option A (Recommended)**: Exact major versioning.
**Option B**: Permissive versionless parsing.
"""
        questions = Workspace.split_checkpoint_questions(legacy)
        self.assertEqual(len(questions), 2)
        first = Orchestrator._checkpoint_payload_from_text(questions[0])
        second = Orchestrator._checkpoint_payload_from_text(questions[1])
        self.assertIn("Decision 31", first["question"])
        self.assertEqual([option["label"] for option in first["options"]], ["A", "B"])
        self.assertIn("Decision 32", second["question"])

    def test_legacy_logger_checkpoint_converts_once_to_structured_options(self):
        legacy = """We have unresolved proposed decisions.

1. **Decision 24: Routing Authority & Destination Validation**:
   - **Option A (Recommended)**: Validate destination aliases against an allowlist.
   - **Option B**: Trust client service-name claims.
"""
        payload = Orchestrator._checkpoint_payload_from_text(legacy)
        self.assertIn("Decision 24", payload["question"])
        self.assertEqual([option["label"] for option in payload["options"]], ["A", "B"])
        self.assertTrue(payload["options"][0]["recommended"])
        self.assertIn("unresolved proposed decisions", payload["rationale"])

    def test_only_one_checkpoint_is_active_and_answers_advance_transactionally(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectStore(Path(directory))
            first = store.enqueue_checkpoint(
                "run-1", "discovery", "Where should this run?", "Deployment changes packaging.",
                [{"label": "A", "summary": "Cloud", "consequence": "Use managed services", "recommended": True},
                 {"label": "B", "summary": "On premises", "consequence": "Package every dependency"}],
            )
            second = store.enqueue_checkpoint(
                "run-1", "discovery", "What scale is required?", "Scale changes topology.",
                [{"label": "A", "summary": "Small"}, {"label": "B", "summary": "Large"}],
            )
            self.assertEqual(first["status"], "active")
            self.assertEqual(second["status"], "pending")
            answered, next_checkpoint = store.answer_checkpoint(
                "run-1", first["id"], "admin", option_id=first["options"][0]["id"],
            )
            self.assertEqual(answered["status"], "answered")
            self.assertEqual(next_checkpoint["id"], second["id"])
            self.assertEqual(next_checkpoint["status"], "active")
            decision = store.decision(first["decision_id"])
            self.assertEqual(decision["status"], "confirmed")
            self.assertEqual(decision["chosen_option"], "A — Cloud")
            self.assertEqual(decision["answered_by"], "admin")
            self.assertEqual([event["action"] for event in decision["history"]], ["proposed", "confirmed"])
            with self.assertRaises(ValueError):
                store.answer_checkpoint("run-1", first["id"], "admin", custom_answer="stale")
            store.close()

    def test_custom_checkpoint_answer_is_recorded_in_decision_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectStore(Path(directory))
            checkpoint = store.enqueue_checkpoint(
                "run-custom", "approval", "Where should data be stored?", "Controls operations.",
                [{"label": "A", "summary": "SQLite"}, {"label": "B", "summary": "Postgres"}],
            )
            store.answer_checkpoint(
                "run-custom", checkpoint["id"], "alice", custom_answer="Use DuckDB for the first release",
            )
            decision = store.decision(checkpoint["decision_id"])
            self.assertEqual(decision["chosen_option"], "Use DuckDB for the first release")
            self.assertEqual(decision["status"], "confirmed")
            self.assertEqual(len(store.run_decisions("run-custom")), 1)
            store.close()

    def test_existing_checkpoint_is_backfilled_into_decision_ledger_once(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory)
            store = ProjectStore(metadata)
            checkpoint = store.enqueue_checkpoint(
                "run-legacy", "approval", "Choose the delivery contract?", "Controls durability.",
                [{"label": "A", "summary": "Fast"}, {"label": "B", "summary": "Durable"}],
            )
            store.answer_checkpoint("run-legacy", checkpoint["id"], "alice", custom_answer="Fast for MVP")
            decision_id = checkpoint["decision_id"]
            with store._db:
                store._db.execute("UPDATE decision_checkpoints SET decision_id=NULL WHERE id=?", (checkpoint["id"],))
                store._db.execute("DELETE FROM decision_history WHERE decision_id=?", (decision_id,))
                store._db.execute("DELETE FROM decisions WHERE id=?", (decision_id,))
            store.close()

            reopened = ProjectStore(metadata)
            restored_checkpoint = reopened.checkpoint(checkpoint["id"])
            restored = reopened.decision(restored_checkpoint["decision_id"])
            self.assertEqual(restored["status"], "confirmed")
            self.assertEqual(restored["chosen_option"], "Fast for MVP")
            self.assertEqual(len(restored["history"]), 2)
            reopened.close()

    def test_checkpoint_survives_store_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory)
            store = ProjectStore(metadata)
            checkpoint = store.enqueue_checkpoint(
                "run-2", "approval", "Choose delivery semantics", "This controls durability.",
                [{"label": "A", "summary": "At most once"}, {"label": "B", "summary": "At least once"}],
            )
            store.close()
            reopened = ProjectStore(metadata)
            restored = reopened.current_checkpoint("run-2")
            self.assertEqual(restored["id"], checkpoint["id"])
            self.assertEqual(len(restored["options"]), 2)
            reopened.close()

    def test_latest_checkpoint_recovers_without_in_memory_run_id(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory)
            store = ProjectStore(metadata)
            store.start_run("run-older", "older")
            store.enqueue_checkpoint(
                "run-older", "approval", "Old question", "",
                [{"label": "A", "summary": "Old answer"}],
            )
            store.start_run("run-current", "current")
            first = store.enqueue_checkpoint(
                "run-current", "approval", "First current question", "",
                [{"label": "A", "summary": "First answer"}],
            )
            store.enqueue_checkpoint(
                "run-current", "approval", "Second current question", "",
                [{"label": "A", "summary": "Second answer"}],
            )
            store.close()

            reopened = ProjectStore(metadata)
            restored = reopened.latest_current_checkpoint()
            self.assertEqual(restored["id"], first["id"])
            self.assertEqual(restored["question"], "First current question")
            reopened.close()


class UsageSerializationTests(unittest.TestCase):
    def test_usage_round_trip_ignores_derived_total_tokens(self):
        original = Usage(input_tokens=120, cached_input_tokens=40, output_tokens=30, estimated=True)

        restored = Usage.from_dict(original.to_dict())

        self.assertEqual(restored.input_tokens, 120)
        self.assertEqual(restored.cached_input_tokens, 40)
        self.assertEqual(restored.output_tokens, 30)
        self.assertEqual(restored.total_tokens, 150)
        self.assertTrue(restored.estimated)

    def test_usage_deserialization_ignores_unknown_future_fields(self):
        restored = Usage.from_dict({"input_tokens": 5, "output_tokens": 2, "provider_detail": "ignored"})
        self.assertEqual(restored.total_tokens, 7)


class FakeCLI(CLIAgent):
    def __init__(self, config, outputs):
        self.outputs = iter(outputs)
        self.commands = []
        self.fake_conversation_id = str(uuid.uuid4())
        super().__init__(config)

    def _run(self, argv, cwd=None):
        self.commands.append((argv, cwd))
        if "--log-file" in argv:
            log_path = Path(argv[argv.index("--log-file") + 1])
            log_path.write_text(
                f"Print mode: conversation={self.fake_conversation_id}, sending message\n"
            )
        stdout = next(self.outputs)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


class RateLimitedFake(StatefulFake):
    def __init__(self, config, reply):
        super().__init__(config, replies=[reply])
        self.attempts = 0

    def _raw_send(self, messages, system, *args, **kwargs):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("429 rate limit reached; retry after 1 second")
        return super()._raw_send(messages, system, *args, **kwargs)


class ImmediateRetryOrchestrator(Orchestrator):
    @staticmethod
    def _retry_delay(exc, attempt, agent):
        return 0


class RepairableFake(StatefulFake):
    def _raw_send(self, messages, system, *args, **kwargs):
        if self.config.model != "fixed":
            raise RuntimeError("invalid model configuration")
        return super()._raw_send(messages, system)


class QuotaExhaustedFake(StatefulFake):
    def _raw_send(self, messages, system, *args, **kwargs):
        raise RuntimeError("429 insufficient_quota: quota exhausted; retry after 9 hours")


VALID_PLAN = """## Requirements
- define the product

## Non-Goals
- no implementation

## Assumptions
- single-user planning workflow

## Alternatives
- sqlite
- postgres

## Decisions
- start with sqlite

## Risks
- changing requirements

## Acceptance Criteria
- plan is actionable

## Implementation Phases
- [ ] Phase 1: validate the riskiest assumption
- [ ] Phase 2: implement the baseline

## Discovery Checkpoints
- Verify provider behavior with a small spike before integration.
"""

VALID_DESIGN = """## Architecture
```mermaid
flowchart TD
    Idea --> Plan
    Plan --> Review
```

## Notes
Initial architecture.

## Product Operations & Evolution
- Version releases and database migrations with rollback-safe compatibility checks.
- Audit important administrative actions with privacy-aware retention.
- Use structured application logs, monitoring, and failure diagnostics proportionate to this small deployment.

## Known Unknowns & Validation Plan
- Validate the storage choice with representative data before implementation.
"""

VALID_DECISIONS = """## Accepted Decisions
- Start with SQLite for the planning baseline because deployment simplicity matters more than scale initially.

## Trade-offs
- Revisit the database choice after measuring representative concurrency and query patterns.
"""


class CrossCuttingDesignTests(unittest.TestCase):
    def test_planning_validation_requires_product_operations_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.write("plan", VALID_PLAN)
            workspace.write("decisions", VALID_DECISIONS)
            workspace.write("design", VALID_DESIGN)
            self.assertFalse(any("Product Operations" in error for error in workspace.validate_planning_artifacts()))

            incomplete = re.sub(
                r"\n## Product Operations & Evolution[\s\S]*?(?=\n## Known Unknowns)", "", VALID_DESIGN,
            )
            workspace.write("design", incomplete)
            self.assertTrue(any("Product Operations" in error for error in workspace.validate_planning_artifacts()))

    def test_coordinator_requires_user_aligned_cross_cutting_design(self):
        self.assertIn("Product Operations & Evolution Must Be Evaluated, Not Forced", COORDINATOR_SYSTEM)
        self.assertIn("user's hosting model", COORDINATOR_SYSTEM)
        self.assertIn("never force enterprise infrastructure onto a small MVP", COORDINATOR_SYSTEM)
        self.assertIn("may explicitly exclude any or all", COORDINATOR_SYSTEM)

    def test_explicit_user_opt_out_satisfies_evaluation_without_forcing_implementation(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.write("plan", VALID_PLAN)
            workspace.write("decisions", VALID_DECISIONS)
            design = re.sub(
                r"## Product Operations & Evolution[\s\S]*?(?=\n## Known Unknowns)",
                """## Product Operations & Evolution
- **Versioning and upgrades — not required by user:** This is a disposable isolated prototype.
- **Audit trail — not required by user:** No users, administrators, or retained data exist.
- **Operational logging and monitoring — not required by user:** Console diagnostics are sufficient for this experiment.
""",
                VALID_DESIGN,
            )
            workspace.write("design", design)
            errors = workspace.validate_planning_artifacts()
            self.assertFalse(any("Product Operations" in error for error in errors), errors)


class ProductCapabilityCatalogTests(unittest.TestCase):
    def test_new_project_gets_editable_commercial_capability_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            path = workspace.root / "product_capabilities.json"
            catalog = json.loads(path.read_text())
            items = catalog["capabilities"]
            ids = {item["id"] for item in items}
            self.assertGreaterEqual(len(items), 50)
            self.assertEqual(len(ids), len(items))
            self.assertTrue({"auto"} >= {item["mode"] for item in items})
            for required in (
                "commerce.payments", "delivery.compose", "ops.audit", "ops.logging",
                "identity.authentication", "data.backup_restore", "security.privacy",
                "api.webhooks", "ai.model_ops",
            ):
                self.assertIn(required, ids)

    def test_catalog_is_seeded_once_and_manual_changes_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            path = workspace.root / "product_capabilities.json"
            catalog = json.loads(path.read_text())
            catalog["capabilities"] = [{
                "id": "custom.robot", "category": "Custom", "name": "Robot controller",
                "description": "Control a local robot.", "signals": ["robot"],
                "mode": "include", "notes": "Required by the user",
            }]
            path.write_text(json.dumps(catalog, indent=2))
            workspace.ensure()
            preserved = json.loads(path.read_text())
            self.assertEqual([item["id"] for item in preserved["capabilities"]], ["custom.robot"])

    def test_catalog_modes_and_user_notes_reach_ai_context(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            path = workspace.root / "product_capabilities.json"
            catalog = json.loads(path.read_text())
            catalog["capabilities"][0]["mode"] = "exclude"
            catalog["capabilities"][0]["notes"] = "Disposable experiment"
            path.write_text(json.dumps(catalog))
            context = workspace.scoped_context(["capabilities"])
            self.assertIn("PRODUCT_CAPABILITIES.json", context)
            self.assertIn("mode=exclude", context)
            self.assertIn("user notes: Disposable experiment", context)
            self.assertIn("commerce.payments", context)


class ArtifactMergeSafetyTests(unittest.TestCase):
    def test_section_update_preserves_unmentioned_existing_sections_and_archives_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.write("design", "# Design\n\n## Architecture\nOld architecture.\n\n## Security\nKeep this detail.\n")
            written, mode = workspace.merge_artifact_update(
                "design", "## Architecture\nUpdated architecture.\n\n## Operations\nNew operations.", "Architecture Design",
            )
            self.assertTrue(written)
            self.assertEqual(mode, "merged")
            result = workspace.read("design")
            self.assertIn("Updated architecture", result)
            self.assertNotIn("Old architecture", result)
            self.assertIn("Keep this detail", result)
            self.assertIn("New operations", result)
            revisions = list((workspace.root / "artifact_history" / "design").glob("*.md"))
            self.assertEqual(len(revisions), 1)
            self.assertIn("Old architecture", revisions[0].read_text())

    def test_unsectioned_model_update_cannot_replace_existing_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            original = "# Design\n\n## Architecture\nDetailed existing architecture.\n"
            workspace.write("design", original)
            written, reason = workspace.merge_artifact_update(
                "design", "A short generic replacement with no sections.", "Architecture Design",
            )
            self.assertFalse(written)
            self.assertIn("no `##` sections", reason)
            self.assertEqual(workspace.read("design"), original)


class SessionTests(unittest.TestCase):
    def setUp(self):
        import backend.auth
        import backend.server

        self._backend_auth = backend.auth
        self._backend_server = backend.server
        self._orig_users_path = backend.auth.USERS_PATH
        self._orig_auth_manager = backend.auth.auth_manager
        self._orig_server_auth_manager = backend.server.auth_manager
        self._auth_tmpdir = tempfile.TemporaryDirectory()

        backend.auth.USERS_PATH = Path(self._auth_tmpdir.name) / "users.json"
        test_auth_manager = backend.auth.AuthManager()
        backend.auth.auth_manager = test_auth_manager
        backend.server.auth_manager = test_auth_manager
        backend.server.app_states.clear()
        backend.server.session_projects.clear()
        backend.server.session_last_seen.clear()
        backend.server.unbound_states.clear()

    def tearDown(self):
        for state in self._backend_server.app_states.values():
            if getattr(state, "orchestrator", None):
                state.orchestrator.stop()
            if getattr(state, "store", None):
                state.store.close()
        for state in self._backend_server.unbound_states.values():
            if getattr(state, "store", None):
                state.store.close()
        self._backend_auth.USERS_PATH = self._orig_users_path
        self._backend_auth.auth_manager = self._orig_auth_manager
        self._backend_server.auth_manager = self._orig_server_auth_manager
        self._backend_server.app_states.clear()
        self._backend_server.session_projects.clear()
        self._backend_server.session_last_seen.clear()
        self._backend_server.unbound_states.clear()
        self._auth_tmpdir.cleanup()

    def test_stateful_agent_sends_only_new_turn_and_tracks_cost(self):
        agent = StatefulFake(
            AgentConfig(
                name="stateful",
                kind="openai",
                model="gpt-4o",
            ),
            replies=["one", "two"],
        )
        agent.send("first")
        agent.send("second")

        self.assertEqual(agent.received[0], [{"role": "user", "content": "first"}])
        self.assertEqual(agent.received[1], [{"role": "user", "content": "second"}])
        self.assertEqual(agent.total_tokens, 240)
        self.assertEqual(agent.total_cached_input_tokens, 80)
        self.assertGreater(agent.total_cost_usd, 0)

    def test_groq_agent_uses_native_sdk_and_tracks_usage(self):
        calls = []

        class FakeCompletions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="Groq reply"))],
                    usage=SimpleNamespace(
                        prompt_tokens=21,
                        completion_tokens=8,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=3),
                    ),
                )

        class FakeGroq:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.chat = SimpleNamespace(completions=FakeCompletions())
                self.models = SimpleNamespace(list=lambda: SimpleNamespace(data=[
                    SimpleNamespace(id="llama-3.1-8b-instant"),
                    SimpleNamespace(id="llama-3.3-70b-versatile"),
                    SimpleNamespace(id="whisper-large-v3"),
                ]))

        with patch.dict("sys.modules", {"groq": SimpleNamespace(Groq=FakeGroq)}):
            agent = GroqAgent(AgentConfig(
                name="groq-agent",
                kind="groq",
                model="llama-3.3-70b-versatile",
                api_key="groq-test-key",
                system_prompt="Be concise",
            ))
            reply = agent.send("Review the design")
            models = discover_models(agent.config)

        self.assertEqual(reply, "Groq reply")
        self.assertEqual(calls[0]["model"], "llama-3.3-70b-versatile")
        self.assertEqual(calls[0]["messages"][0], {"role": "system", "content": "Be concise"})
        self.assertEqual(agent.total_input_tokens, 21)
        self.assertEqual(agent.total_cached_input_tokens, 3)
        self.assertEqual(agent.total_output_tokens, 8)
        self.assertEqual(models, ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"])

    def test_virtual_company_rotates_discovered_models(self):
        from backend.server import model_for_virtual_agent

        config = {
            "model": "llama-3.3-70b-versatile",
            "extra": {"available_models": [
                "llama-3.3-70b-versatile",
                "qwen-qwq-32b",
                "llama-3.1-8b-instant",
            ]},
        }
        assigned = [model_for_virtual_agent(config, index, 1) for index in range(6)]
        self.assertEqual(assigned, [
            "llama-3.3-70b-versatile",
            "qwen-qwq-32b",
            "llama-3.1-8b-instant",
            "llama-3.3-70b-versatile",
            "qwen-qwq-32b",
            "llama-3.1-8b-instant",
        ])

    def test_codex_cli_resumes_exact_thread(self):
        first = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "abc-123"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "first reply"}}),
            json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 4,
            }}),
        ])
        second = "\n".join([
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "second reply"}}),
            json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 12, "cached_input_tokens": 8, "output_tokens": 3,
            }}),
        ])
        project = tempfile.mkdtemp()
        agent = FakeCLI(
            AgentConfig(
                name="codex",
                kind="cli",
                working_directory=project,
                cli_command="codex exec --ephemeral --skip-git-repo-check",
            ),
            [first, second],
        )

        self.assertEqual(agent.send("turn one"), "first reply")
        self.assertEqual(agent.send("turn two"), "second reply")

        first_command = agent.commands[0][0]
        second_command = agent.commands[1][0]
        self.assertNotIn("--ephemeral", first_command)
        self.assertIn("--json", first_command)
        self.assertEqual(second_command[:3], ["codex", "exec", "resume"])
        self.assertIn("abc-123", second_command)
        self.assertEqual(second_command[-1], "turn two")
        self.assertNotIn("turn one", second_command[-1])
        self.assertEqual(agent.total_cached_input_tokens, 10)
        self.assertEqual(Path(agent.commands[0][1]), Path(project).resolve())
        self.assertEqual(Path(agent.commands[1][1]), Path(project).resolve())

    def test_codex_cli_normalizes_explicit_cumulative_usage(self):
        first = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "cumulative-1"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "one"}}),
            json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10,
            }}),
        ])
        second = "\n".join([
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "two"}}),
            json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 160, "cached_input_tokens": 50, "output_tokens": 18,
            }}),
        ])
        agent = FakeCLI(
            AgentConfig(
                name="codex", kind="cli", working_directory=tempfile.mkdtemp(),
                cli_command="codex exec", extra={"cli_usage_mode": "cumulative"},
            ),
            [first, second],
        )

        agent.send("first")
        agent.send("second")

        self.assertEqual(agent.total_input_tokens, 160)
        self.assertEqual(agent.total_cached_input_tokens, 50)
        self.assertEqual(agent.total_output_tokens, 18)
        self.assertEqual(agent.last_usage.total_tokens, 68)

    def test_antigravity_resumes_exact_isolated_conversation(self):
        project = tempfile.mkdtemp()
        agent = FakeCLI(
            AgentConfig(id="agy-1", name="agy", kind="cli", cli_command="agy -p",
                        working_directory=project),
            ["first", "second"],
        )
        agent.send("turn one")
        agent.send("turn two")

        first_command, first_cwd = agent.commands[0]
        second_command, second_cwd = agent.commands[1]
        self.assertEqual(first_command[0], "agy")
        self.assertNotIn("--continue", second_command)
        self.assertIn("--conversation", second_command)
        self.assertIn(agent.fake_conversation_id, second_command)
        self.assertIn("--add-dir", first_command)
        self.assertEqual(first_command[first_command.index("--add-dir") + 1], str(Path(project).resolve()))
        self.assertEqual(first_cwd, second_cwd)
        self.assertEqual(Path(first_cwd), Path(project).resolve())
        log_path = Path(first_command[first_command.index("--log-file") + 1])
        self.assertEqual(log_path.parent.parent, Path(project).resolve() / ".designflow" / "sessions")
        self.assertEqual(agent.provider_session_id(), agent.fake_conversation_id)

    def test_duplicate_cli_agents_have_independent_sessions(self):
        project = tempfile.mkdtemp()
        config_a = AgentConfig(id="architect", name="architect", role="architecture",
                               kind="cli", cli_command="agy", working_directory=project)
        config_b = AgentConfig(id="skeptic", name="skeptic", role="risk review",
                               kind="cli", cli_command="agy", working_directory=project)
        first = FakeCLI(config_a, ["one"])
        second = FakeCLI(config_b, ["two"])
        first.send("hello")
        second.send("hello")
        self.assertNotEqual(first._session_cwd, second._session_cwd)
        self.assertNotEqual(first.provider_session_id(), second.provider_session_id())
        self.assertIn(first.fake_conversation_id, first.commands[1][0] if len(first.commands) > 1 else [first.provider_session_id()])

    def test_stateless_cli_runs_from_selected_project(self):
        project = tempfile.mkdtemp()
        agent = FakeCLI(
            AgentConfig(name="custom", kind="cli", cli_command="custom-agent",
                        working_directory=project, extra={"session_mode": "stateless"}),
            ["reply"],
        )
        agent.send("hello")
        self.assertEqual(Path(agent.commands[0][1]), Path(project).resolve())

    def test_real_cli_process_inherits_selected_project_directory(self):
        with tempfile.TemporaryDirectory() as project:
            agent = CLIAgent(AgentConfig(
                name="pwd", kind="cli", cli_command="/bin/sh -c pwd",
                working_directory=project, extra={"session_mode": "stateless"},
            ))
            self.assertEqual(Path(agent.send("where am I?").strip()), Path(project).resolve())

    def test_role_and_behavior_are_initialized_as_system_identity(self):
        agent = StatefulFake(
            AgentConfig(
                name="Ada", role="Security skeptic", kind="openai",
                model="gpt-4o", system_prompt="Challenge unsafe assumptions.",
            ),
            replies=["ok"],
        )
        orchestrator = Orchestrator([agent], Workspace(tempfile.mkdtemp()), require_approval=False)
        orchestrator._running = True
        asyncio.run(orchestrator._send_agent(agent, "Review this"))
        self.assertIn("You are Ada", agent.received_systems[0])
        self.assertIn("Security skeptic", agent.received_systems[0])
        self.assertIn("Challenge unsafe assumptions", agent.received_systems[0])

    def test_rate_limit_is_retried_without_losing_the_logical_session(self):
        retry_events = []
        agent = RateLimitedFake(
            AgentConfig(name="retry", kind="openai", model="gpt-4o"),
            reply="recovered",
        )
        orchestrator = ImmediateRetryOrchestrator(
            [agent], Workspace(tempfile.mkdtemp()), event_cb=lambda event: retry_events.append(event),
            require_approval=False,
        )
        orchestrator._running = True
        reply = asyncio.run(orchestrator._send_agent(agent, "try"))
        self.assertEqual(reply, "recovered")
        self.assertEqual(agent.attempts, 2)
        self.assertEqual(retry_events[0].kind.value, "turn_start")
        self.assertTrue(any(event.kind.value == "retry" for event in retry_events))

    def test_failed_turn_can_be_fixed_and_resumed_without_advancing(self):
        events = []
        agent = RepairableFake(
            AgentConfig(id="agent-1", name="repair", kind="openai", model="broken"),
            replies=["recovered"],
        )
        orchestrator = Orchestrator(
            [agent], Workspace(tempfile.mkdtemp()), event_cb=events.append,
            require_approval=False,
        )
        orchestrator._running = True

        async def exercise():
            task = asyncio.create_task(orchestrator._send_agent(
                agent, "same turn", "turn-0001", {"phase": "debate", "round": 1},
            ))
            while not orchestrator.failed_turn:
                await asyncio.sleep(0)
            self.assertEqual(orchestrator.failed_turn["turn_id"], "turn-0001")
            agent.reconfigure(AgentConfig(
                id="agent-1", name="repair", kind="openai", model="fixed",
            ))
            orchestrator.retry_failed_turn()
            return await task

        self.assertEqual(asyncio.run(exercise()), "recovered")
        self.assertEqual(len(agent.history), 2)
        self.assertEqual(orchestrator._turn_attempts["turn-0001"], 2)
        self.assertTrue(any(
            event.kind.value == "error" and event.data.get("recoverable")
            for event in events
        ))
        public_error = next(event for event in events if event.kind.value == "error")
        self.assertNotIn("prompt", public_error.data)
        self.assertNotIn("same turn", json.dumps(public_error.data))
        self.assertLess(len(json.dumps(public_error.data)), 500)
        self.assertTrue(any(
            event.kind.value == "turn_start" and event.data.get("resumed")
            for event in events
        ))

    def test_quota_exhaustion_waits_for_user_recovery_instead_of_long_retry(self):
        events = []
        agent = QuotaExhaustedFake(AgentConfig(id="quota-agent", name="quota", kind="gemini"))
        orchestrator = Orchestrator(
            [agent], Workspace(tempfile.mkdtemp()), event_cb=events.append, require_approval=False,
        )
        orchestrator._running = True

        async def exercise():
            task = asyncio.create_task(orchestrator._send_agent(agent, "draft", "turn-0001"))
            while not orchestrator.failed_turn:
                await asyncio.sleep(0)
            self.assertEqual(orchestrator.failed_turn["agent_id"], "quota-agent")
            orchestrator.stop()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(exercise())
        self.assertFalse(any(event.kind.value == "retry" for event in events))
        error = next(event for event in events if event.kind.value == "error")
        self.assertEqual(error.data["error_code"], "quota_exhausted")
        self.assertEqual(error.data["error"], "Model quota or provider credits are exhausted.")
        self.assertEqual(orchestrator.failed_turn["error_code"], "quota_exhausted")
        self.assertEqual(orchestrator.failed_turn["public_error"], "Model quota or provider credits are exhausted.")

    def test_failed_turn_uses_user_substituted_agent_on_retry(self):
        failed = RepairableFake(
            AgentConfig(id="specialist-1", base_id="provider-a", name="security", kind="openai", model="broken"),
        )
        replacement = RepairableFake(
            AgentConfig(id="specialist-1", base_id="provider-b", name="security", kind="groq", model="fixed"),
            replies=["fallback response"],
        )
        orchestrator = Orchestrator([failed], Workspace(tempfile.mkdtemp()), require_approval=False)
        orchestrator._running = True

        async def exercise():
            task = asyncio.create_task(orchestrator._send_agent(
                failed, "same failed turn", "turn-0001", {"phase": "peer_review"},
            ))
            while not orchestrator.failed_turn:
                await asyncio.sleep(0)
            failed.transfer_runtime_state_to(replacement)
            orchestrator.agents[0] = replacement
            orchestrator.retry_failed_turn()
            return await task

        self.assertEqual(asyncio.run(exercise()), "fallback response")
        self.assertEqual(replacement.config.base_id, "provider-b")
        self.assertEqual(len(replacement.history), 2)
        self.assertEqual(orchestrator._turn_attempts["turn-0001"], 2)

    def test_provider_substitution_preserves_usage_and_history(self):
        original = StatefulFake(
            AgentConfig(id="specialist-1", base_id="provider-a", name="architect", kind="openai"),
            replies=["first response"],
        )
        original.send("first prompt")
        original.mark_error("quota exhausted")
        replacement = StatefulFake(
            AgentConfig(id="specialist-1", base_id="provider-b", name="architect", kind="groq"),
        )

        original.transfer_runtime_state_to(replacement)

        self.assertEqual(replacement.history, original.history)
        self.assertEqual(replacement.total_tokens, original.total_tokens)
        self.assertEqual(replacement.total_cost_usd, original.total_cost_usd)
        self.assertEqual(replacement.error_message, "quota exhausted")
        self.assertEqual(replacement.state_dict()["base_id"], "provider-b")

    def test_agent_events_identify_underlying_provider_and_model(self):
        agent = StatefulFake(AgentConfig(
            id="base-security", base_id="gemini-main", name="security_auditor",
            kind="gemini", model="gemini-2.5-pro",
            extra={"runtime_base_name": "Gemini Primary"},
        ))
        orchestrator = Orchestrator([agent], Workspace(tempfile.mkdtemp()), require_approval=False)

        metadata = orchestrator._event_actor_meta(agent)

        self.assertEqual(metadata["provider_agent"], "Gemini Primary")
        self.assertEqual(metadata["provider_id"], "gemini-main")
        self.assertEqual(metadata["provider_kind"], "gemini")
        self.assertEqual(metadata["provider_model"], "gemini-2.5-pro")

    def test_workspace_writes_into_project_and_reads_designflow_brief(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.write_brief("Build a small service")
            workspace.init(workspace.brief())
            workspace.write_src("src/api/app.py", "print('ok')")

            self.assertEqual(workspace.brief().strip(), "Build a small service")
            self.assertEqual((workspace.project_root / "src/api/app.py").read_text(), "print('ok')")
            self.assertIn("src/api/app.py", workspace.read_src())
            self.assertNotIn(".designflow/DESIGN.md", workspace.read_src())
            self.assertTrue((workspace.root / "DECISIONS.md").exists())

    def test_sqlite_store_reuses_agents_and_run_history_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = Workspace(directory)
            metadata.ensure()
            store = ProjectStore(metadata.root)
            store.save_agents([{
                "id": "agent-1", "name": "Builder", "role": "Developer",
                "kind": "openai", "model": "gpt-4o", "api_key": "secret",
                "cli_command": "", "system_prompt": "", "max_history_turns": 20,
                "extra": {},
            }])
            store.start_run("run-1", "Build it")
            store.append_event("run-1", {
                "timestamp": "now", "kind": "retry", "agent": "Builder", "data": {"attempt": 1},
            })
            store.finish_run("run-1", "done", [{"total_tokens": 42, "cost_usd": 0.01}])

            loaded = store.load_agents()
            runs = store.recent_runs()
            self.assertEqual(loaded[0]["role"], "Developer")
            self.assertEqual(loaded[0]["api_key"], "secret")
            self.assertEqual(runs[0]["total_tokens"], 42)
            self.assertEqual(runs[0]["status"], "done")
            store.close()

    def test_sqlite_tracks_turn_attempt_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            store = ProjectStore(workspace.root)
            store.start_run("run-1", "Build it")
            store.append_event("run-1", {
                "timestamp": "t1", "kind": "turn_start", "agent": "Builder",
                "data": {"turn_id": "turn-0001", "attempt": 1, "phase": "debate", "round": 1},
            })
            store.append_event("run-1", {
                "timestamp": "t2", "kind": "error", "agent": "Builder",
                "data": {"turn_id": "turn-0001", "attempt": 1,
                         "recoverable": True, "error": "bad config"},
            })
            store.append_event("run-1", {
                "timestamp": "t3", "kind": "turn_start", "agent": "Builder",
                "data": {"turn_id": "turn-0001", "attempt": 2, "phase": "debate", "round": 1},
            })
            store.append_event("run-1", {
                "timestamp": "t4", "kind": "turn_end", "agent": "Builder",
                "data": {"turn_id": "turn-0001", "attempt": 2,
                         "usage": {"total_tokens": 12}, "response": "done"},
            })
            turns = store.run_turns("run-1")
            self.assertEqual(turns[0]["status"], "completed")
            self.assertEqual(turns[0]["attempt"], 2)
            self.assertEqual(turns[0]["usage"]["total_tokens"], 12)
            store.close()

    def test_coordinator_orchestrated_loop(self):
        boss = StatefulFake(
            AgentConfig(name="boss", kind="openai", model="gpt-4o", extra={"is_coordinator": True}),
            replies=[f"## PLAN_UPDATE\n{VALID_PLAN}\n## DESIGN_UPDATE\n{VALID_DESIGN}\n## DECISIONS_UPDATE\n{VALID_DECISIONS}\n", "## DECISION_CHECKPOINT\n"]
        )
        worker = StatefulFake(
            AgentConfig(name="worker", kind="openai", model="gpt-4o"),
            replies=["## DESIGN_APPEND\nLooks good.\n"]
        )
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(
                agents=[boss, worker],
                workspace=Workspace(directory),
                require_approval=False,
            )
            asyncio.run(orchestrator.run("build tiny product"))
            self.assertEqual(len(boss.received), 2)
            self.assertEqual(len(worker.received), 1)
            self.assertIn("senior architecture synthesizer", boss.received_systems[0])
            self.assertIn("DECISIONS_UPDATE", boss.received[0][-1]["content"])
            self.assertIn("Requirements", orchestrator.ws.read("plan"))

    def test_user_checkpoint_answer_is_recorded_as_confirmed_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("logging framework")
            workspace.write(
                "questions",
                "# Decision Checkpoint\n\nShould tenant isolation use separate schemas or tenant-scoped rows?",
            )
            orchestrator = Orchestrator([], workspace, require_approval=True)
            orchestrator.phase = OrchestratorPhase.APPROVAL

            asyncio.run(orchestrator.steer("Use tenant-scoped rows with mandatory tenant_id and database RLS."))

            decisions = workspace.read("decisions")
            self.assertIn("Should tenant isolation", decisions)
            self.assertIn("tenant-scoped rows", decisions)
            self.assertIn("Confirmed by user", decisions)
            self.assertEqual(workspace.read("questions"), "(empty)")

    def test_bundled_checkpoint_answers_are_presented_sequentially(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("logging framework")
            workspace.write("questions", """# Decision Checkpoint

Decision 1: Choose ingestion.
- [A] HTTP
- [B] Kafka
Recommendation: B

Decision 2: Choose retention.
- [A] 30 days
- [B] 90 days
Recommendation: A
""")
            orchestrator = Orchestrator([], workspace, require_approval=True)
            orchestrator.phase = OrchestratorPhase.APPROVAL

            asyncio.run(orchestrator.steer("B — Kafka"))

            remaining = workspace.read("questions")
            self.assertNotIn("Choose ingestion", remaining)
            self.assertIn("Choose retention", remaining)
            decisions = workspace.read("decisions")
            self.assertIn("Choose ingestion", decisions)
            self.assertNotIn("Choose retention", decisions)

    def test_restored_checkpoint_normalizes_before_it_is_presented(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("logging framework")
            bundled = """# Decision Checkpoint

Question 1: Choose transport.
- [A] HTTP
- [B] gRPC

Question 2: Choose storage.
- [A] S3
- [B] Local disk
"""
            workspace.write("questions", bundled)
            self.assertTrue(workspace.normalize_checkpoint_queue())
            visible = workspace.read("questions")
            self.assertIn("Choose transport", visible)
            self.assertNotIn("Choose storage", visible)

            restored = Workspace(directory)
            self.assertTrue(restored.record_checkpoint_answer(visible, "B — gRPC"))
            next_visible = restored.read("questions")
            self.assertNotIn("Choose transport", next_visible)
            self.assertIn("Choose storage", next_visible)

    def test_checkpoint_rationale_is_not_presented_without_its_first_question(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("logging framework")
            workspace.write("questions", """# Decision Checkpoint

Three major design debates require user feedback.

Decision 1: Choose transport.
- [A] HTTP
- [B] Kafka

Decision 2: Choose retention.
- [A] 30 days
- [B] 90 days
""")
            self.assertTrue(workspace.normalize_checkpoint_queue())
            visible = workspace.read("questions")
            self.assertIn("Three major design debates", visible)
            self.assertIn("Choose transport", visible)
            self.assertIn("- [A] HTTP", visible)
            self.assertNotIn("Choose retention", visible)

    def test_legacy_rationale_only_checkpoint_promotes_queued_question(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("logging framework")
            workspace.write("questions", "# Decision Checkpoint\n\nThree debates require feedback.")
            (workspace.root / "checkpoint_queue.json").write_text(json.dumps([
                "Decision 1: Choose transport.\n- [A] HTTP\n- [B] Kafka",
                "Decision 2: Choose retention.\n- [A] 30 days\n- [B] 90 days",
            ]))

            self.assertTrue(workspace.normalize_checkpoint_queue())
            visible = workspace.read("questions")
            self.assertIn("Three debates require feedback", visible)
            self.assertIn("Choose transport", visible)
            self.assertIn("- [A] HTTP", visible)

    def test_explicit_user_reversal_is_recorded_without_ai_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("enterprise logging framework")
            orchestrator = Orchestrator([], workspace, require_approval=False)

            asyncio.run(orchestrator.run(
                "enterprise logging framework",
                task="I don't want to do multi-tenancy in the first version.",
            ))

            decisions = workspace.read("decisions")
            self.assertIn("I don't want to do multi-tenancy", decisions)
            self.assertIn("Confirmed by user", decisions)
            self.assertIn("Supersedes any conflicting earlier decision", decisions)
            self.assertTrue(any(
                event.get("kind") == "user_decision" and "multi-tenancy" in event.get("content", "")
                for event in workspace.context_events()
            ))

    def test_ordinary_work_request_is_not_misclassified_as_decision(self):
        self.assertFalse(Orchestrator._is_explicit_user_correction(
            "Review the architecture and ask specialists to improve the plan."
        ))
        self.assertTrue(Orchestrator._is_explicit_user_correction(
            "We no longer want to support multi tenancy."
        ))

    def test_agents_endpoint_requires_project_for_writes(self):
        from fastapi.testclient import TestClient
        import backend.server

        client = TestClient(backend.server.app)
        res = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(res.status_code, 200)
        payload = {"name": "Project Bot", "kind": "openai", "role": "helper"}
        res = client.post("/agents", json=payload)
        self.assertEqual(res.status_code, 400)
        self.assertIn("project", res.json()["detail"].lower())

    def test_project_store_encryption(self):
        from backend.storage import ProjectStore
        import tempfile
        import json

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectStore(Path(tmpdir))
            agent_payload = [{
                "id": "bot1",
                "name": "Bot One",
                "kind": "openai",
                "role": "builder",
                "model": "gpt-4o",
                "api_key": "my-secret-key-abc",
                "system_prompt": "hello",
                "max_history_turns": 20,
                "extra": {}
            }]

            store.save_agents(agent_payload)

            # Assert raw DB storage is encrypted
            cursor = store._db.execute("SELECT config_json FROM agents")
            row = cursor.fetchone()
            config_stored = json.loads(row["config_json"])
            self.assertNotEqual(config_stored["api_key"], "my-secret-key-abc")
            self.assertTrue(config_stored["api_key"].startswith("gAAAA")) # Fernet header

            # Assert loading decrypts correctly
            loaded = store.load_agents()
            self.assertEqual(loaded[0]["api_key"], "my-secret-key-abc")
            store.close()

    def test_coordinator_pause_for_input(self):
        boss = StatefulFake(
            AgentConfig(name="boss", kind="openai", model="gpt-4o", extra={"is_coordinator": True}),
            replies=[
                f"## PLAN_UPDATE\n{VALID_PLAN}\n## DESIGN_UPDATE\n{VALID_DESIGN}\n## DECISIONS_UPDATE\n{VALID_DECISIONS}",
                "## DECISION_CHECKPOINT\n"
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(
                agents=[boss],
                workspace=Workspace(directory),
                require_approval=True,
                mode="all",
                max_debate_rounds=1,
            )
            async def deterministic_discovery(_coordinator, _step):
                return orchestrator._deterministic_discovery_question()
            orchestrator._adaptive_discovery_question = deterministic_discovery

            async def run_test():
                run_task = asyncio.create_task(orchestrator.run("social app"))
                answers = [
                    "Primary users are small friend groups planning meetups",
                    "A — Cloud-agnostic and portable across providers",
                    "A — Small launch: up to 1,000 active users",
                    "A — No special constraints beyond standard security practices",
                    "A — Approve the baseline",
                ]
                for answer in answers:
                    for _ in range(100):
                        if orchestrator._paused or run_task.done():
                            break
                        await asyncio.sleep(0.01)
                    if run_task.done():
                        break
                    self.assertTrue(orchestrator._paused)
                    await orchestrator.steer(answer)
                    orchestrator.resume()

                # Wait for task to finish
                await run_task

                # Check that the next prompt received the steering response!
                self.assertIn("Cloud-agnostic", boss.received[-1][-1]["content"])
            asyncio.run(run_test())

    def test_need_based_review_selects_small_relevant_panel(self):
        agents = [
            StatefulFake(AgentConfig(name=name, kind="openai", model="gpt-4o"))
            for name in (
                "boss", "security_auditor", "data_architect", "api_designer",
                "ui_designer", "marketing_alpha", "sales_alpha", "devops_engineer",
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(agents, Workspace(directory), require_approval=False)
            orchestrator.idea = "Secure multi-tenant payment API with a relational database schema"
            orchestrator._coordinator_name = "boss"
            selected = orchestrator._select_peer_review_agents()
            names = {agent.name for agent in selected}
            self.assertLessEqual(len(selected), 3)
            self.assertIn("security_auditor", names)
            self.assertIn("data_architect", names)
            self.assertIn("api_designer", names)
            self.assertNotIn("marketing_alpha", names)

    def test_backend_logging_project_avoids_ui_and_scratch_research_roles(self):
        agents = [
            StatefulFake(AgentConfig(name=name, kind="openai", model="gpt-4o"))
            for name in ("boss", "architect_beta", "api_designer", "devops_engineer", "ui_designer", "researcher")
        ]
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.write_brief(
                "Build an enterprise logging framework for multiple services and languages "
                "with runtime log-level configuration."
            )
            orchestrator = Orchestrator(agents, workspace, require_approval=False)
            orchestrator.idea = workspace.brief()
            orchestrator._coordinator_name = "boss"

            names = {agent.name for agent in orchestrator._select_peer_review_agents()}

            self.assertEqual(len(names), 3)
            self.assertIn("api_designer", names)
            self.assertIn("devops_engineer", names)
            self.assertNotIn("ui_designer", names)
            self.assertNotIn("researcher", names)

    def test_current_task_influences_specialist_selection(self):
        agents = [
            StatefulFake(AgentConfig(name=name, kind="openai"))
            for name in ("boss", "product_manager", "architect_beta", "data_architect", "security_auditor", "api_designer")
        ]
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.write_brief("Build a useful product with user-configurable features.")
            orchestrator = Orchestrator(agents, workspace, require_approval=False)
            orchestrator.idea = workspace.brief()
            orchestrator.task = "Challenge storage concurrency, API contracts, security boundaries, and failure recovery."
            orchestrator._coordinator_name = "boss"

            names = {agent.name for agent in orchestrator._select_peer_review_agents()}

            self.assertIn("architect_beta", names)
            self.assertIn("api_designer", names)
            self.assertNotIn("product_manager", names)

    def test_strong_model_is_reserved_for_synthesis(self):
        cheap_manager = StatefulFake(AgentConfig(
            name="manager", kind="gemini", model="gemini-2.5-flash",
            extra={"is_coordinator": True},
        ))
        strong_model = StatefulFake(AgentConfig(
            name="architect", kind="claude", model="claude-opus-4",
        ))
        self.assertGreater(
            Orchestrator._synthesis_score(strong_model),
            Orchestrator._synthesis_score(cheap_manager),
        )

    def test_project_agent_create_update_and_list(self):
        from fastapi.testclient import TestClient
        import backend.server

        with tempfile.TemporaryDirectory() as tmpdir:
            client = TestClient(backend.server.app)
            res = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
            self.assertEqual(res.status_code, 200)
            res = client.post("/project/open", json={"path": tmpdir})
            self.assertEqual(res.status_code, 200)
            local_agent = {
                    "name": "Standard Dev",
                    "kind": "openai",
                    "role": "developer",
                    "model": "gpt-4o-mini",
                    "api_key": "local-secret",
                    "system_prompt": "local override system",
                    "max_history_turns": 20,
                    "extra": {}
                }
            res = client.post("/agents", json=local_agent)
            self.assertEqual(res.status_code, 200)
            local_id = res.json()["agent"]["id"]

            local_agent["system_prompt"] = "updated local system"
            res = client.put(f"/agents/{local_id}", json=local_agent)
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["agent"]["system_prompt"], "updated local system")

            res = client.get("/agents")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(len(data["agents"]), 1)
            self.assertEqual(data["agents"][0]["model"], "gpt-4o-mini")
            self.assertNotIn("global", data)
            self.assertNotIn("merged", data)

    def test_agent_probe_endpoint(self):
        from fastapi.testclient import TestClient
        import backend.server
        from unittest.mock import patch, MagicMock
        client = TestClient(backend.server.app)
        res = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(res.status_code, 200)
        with patch("backend.server.create_agent") as mock_create:
            mock_agent = MagicMock()
            mock_agent.send.return_value = "ok"
            mock_create.return_value = mock_agent

            agent_payload = {
                "name": "Test Probe",
                "kind": "openai",
                "role": "helper",
                "model": "gpt-4o",
                "api_key": "secret",
                "system_prompt": "hello",
                "max_history_turns": 20,
                "extra": {}
            }
            res = client.post("/agents/test", json=agent_payload)
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), {"ok": True})
            mock_agent.send.assert_called_once_with("ping")

    def test_agent_probe_endpoint_failure(self):
        from fastapi.testclient import TestClient
        import backend.server
        from unittest.mock import patch, MagicMock
        client = TestClient(backend.server.app)
        res = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(res.status_code, 200)
        with patch("backend.server.create_agent") as mock_create:
            mock_agent = MagicMock()
            mock_agent.send.side_effect = Exception("API Key Expired")
            mock_create.return_value = mock_agent

            agent_payload = {
                "name": "Test Probe Fail",
                "kind": "openai",
                "role": "helper",
                "model": "gpt-4o",
                "api_key": "expired",
                "system_prompt": "hello",
                "max_history_turns": 20,
                "extra": {}
            }
            res = client.post("/agents/test", json=agent_payload)
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["error_code"], "authentication_failed")
            self.assertEqual(data["error"], "Provider authentication or model access failed.")
            self.assertNotIn("Expired", data["error"])

    def test_project_settings_persistence(self):
        from fastapi.testclient import TestClient
        import backend.server
        client = TestClient(backend.server.app)
        res = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(res.status_code, 200)

        with tempfile.TemporaryDirectory() as td:
            res = client.post("/project/open", json={"path": td})
            self.assertEqual(res.status_code, 200)

            # Save settings
            res = client.put("/project/settings", json={"max_tokens": 123456})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["settings"]["max_tokens"], 123456)

            # Ensure it persists by re-opening the project
            res = client.post("/project/open", json={"path": td})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["settings"]["max_tokens"], 123456)

    def test_same_project_is_shared_across_browser_sessions_and_logout_only_detaches(self):
        from fastapi.testclient import TestClient
        import backend.server

        first = TestClient(backend.server.app)
        second = TestClient(backend.server.app)
        self.assertEqual(first.post("/auth/login", json={"username": "admin", "password": "admin123"}).status_code, 200)
        self.assertEqual(second.post("/auth/login", json={"username": "admin", "password": "admin123"}).status_code, 200)

        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(first.post("/project/open", json={"path": directory}).status_code, 200)
            self.assertEqual(second.post("/project/open", json={"path": directory}).status_code, 200)
            canonical = str(Path(directory).resolve())

            self.assertEqual(len(backend.server.app_states), 1)
            self.assertIs(
                backend.server.get_state(backend.server.auth_manager.get_session(first.cookies.get("session_id"))),
                backend.server.get_state(backend.server.auth_manager.get_session(second.cookies.get("session_id"))),
            )
            shared_store = backend.server.app_states[canonical].store
            self.assertEqual(first.post("/auth/logout").status_code, 200)
            self.assertIs(backend.server.app_states[canonical].store, shared_store)
            self.assertEqual(second.get("/project").status_code, 200)
            self.assertTrue(second.get("/project").json()["open"])
            self.assertEqual(second.post("/auth/logout").status_code, 200)
            self.assertNotIn(canonical, backend.server.app_states)
            self.assertTrue(shared_store._closed)

    def test_explicit_tab_session_prevents_one_tab_logout_from_logging_out_another(self):
        from fastapi.testclient import TestClient
        import backend.server

        client = TestClient(backend.server.app)
        first_login = client.post("/auth/login", json={"username": "admin", "password": "admin123"}).json()
        second_login = client.post("/auth/login", json={"username": "admin", "password": "admin123"}).json()
        first_headers = {"X-DesignFlow-Session": first_login["session_id"]}
        second_headers = {"X-DesignFlow-Session": second_login["session_id"]}

        self.assertEqual(client.post("/auth/logout", headers=first_headers).status_code, 200)
        self.assertEqual(client.get("/users/me", headers=first_headers).status_code, 401)
        remaining = client.get("/users/me", headers=second_headers)
        self.assertEqual(remaining.status_code, 200)
        self.assertEqual(remaining.json()["username"], "admin")

    def test_admin_shutdown_uses_graceful_server_callback(self):
        from fastapi.testclient import TestClient
        import backend.server

        client = TestClient(backend.server.app)
        login = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(login.status_code, 200)
        called = []
        original_callback = backend.server.app.state.request_shutdown
        original_flag = backend.server.app.state.shutting_down
        try:
            backend.server.app.state.request_shutdown = lambda: called.append(True)
            backend.server.app.state.shutting_down = False

            response = client.post("/admin/shutdown")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(called, [True])
            self.assertTrue(backend.server.app.state.shutting_down)
        finally:
            backend.server.app.state.request_shutdown = original_callback
            backend.server.app.state.shutting_down = original_flag

    def test_shutdown_signals_open_sse_connections(self):
        import backend.server

        state = backend.server.AppState()
        first = asyncio.Queue()
        second = asyncio.Queue()
        state.sse_clients.extend([first, second])
        backend.server.unbound_states["shutdown-test"] = state
        try:
            self.assertEqual(backend.server.close_sse_connections(), 2)
            self.assertIs(first.get_nowait(), backend.server.SSE_SHUTDOWN)
            self.assertIs(second.get_nowait(), backend.server.SSE_SHUTDOWN)
        finally:
            backend.server.unbound_states.pop("shutdown-test", None)

    def test_non_admin_cannot_shutdown_server(self):
        from fastapi.testclient import TestClient
        import backend.server

        client = TestClient(backend.server.app)
        login = client.post("/auth/login", json={"username": "user", "password": "user123"})
        self.assertEqual(login.status_code, 200)
        response = client.post("/admin/shutdown")
        self.assertEqual(response.status_code, 403)

    def test_switching_away_releases_project_when_session_is_last_collaborator(self):
        from fastapi.testclient import TestClient
        import backend.server

        client = TestClient(backend.server.app)
        self.assertEqual(client.post("/auth/login", json={"username": "admin", "password": "admin123"}).status_code, 200)
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            self.assertEqual(client.post("/project/open", json={"path": first}).status_code, 200)
            first_path = str(Path(first).resolve())
            first_store = backend.server.app_states[first_path].store

            self.assertEqual(client.post("/project/open", json={"path": second}).status_code, 200)

            self.assertNotIn(first_path, backend.server.app_states)
            self.assertTrue(first_store._closed)

    def test_stale_running_state_without_live_task_reconciles_to_idle(self):
        import backend.server

        state = backend.server.AppState()
        state.status = "running"
        state.run_id = "stale-run"
        state.orchestrator = SimpleNamespace()
        state.run_task = None
        state.awaiting_input = True

        self.assertEqual(backend.server.reconcile_runtime_status(state), "idle")
        self.assertIsNone(state.run_id)
        self.assertIsNone(state.orchestrator)
        self.assertFalse(state.awaiting_input)

    def test_runtime_diagnostics_expose_invariants_without_secrets(self):
        import backend.server

        state = backend.server.AppState()
        state.status = "running"
        state.awaiting_input = True
        diagnostic = backend.server.runtime_diagnostic(state, "/tmp/example")

        self.assertIn("active runtime has no run id", diagnostic["invariant_errors"])
        self.assertIn("active runtime has no orchestrator", diagnostic["invariant_errors"])
        self.assertIn("active runtime has no live task", diagnostic["invariant_errors"])
        self.assertIn("awaiting input outside paused state", diagnostic["invariant_errors"])
        self.assertNotIn("api_key", json.dumps(diagnostic))

    def test_invalid_pause_and_resume_return_clear_conflicts(self):
        import backend.server

        state = backend.server.AppState()
        with self.assertRaises(backend.server.HTTPException) as pause_error:
            backend.server.pause_run(state)
        self.assertEqual(pause_error.exception.status_code, 409)
        with self.assertRaises(backend.server.HTTPException) as resume_error:
            backend.server.resume_run(None, state)
        self.assertEqual(resume_error.exception.status_code, 409)

    def test_stop_cancels_retry_and_emits_terminal_event(self):
        import backend.server

        state = backend.server.AppState()
        agent = StatefulFake(AgentConfig(name="waiting", kind="openai"))
        agent.mark_waiting("2099-01-01T00:00:00Z", "rate limited")
        state.orchestrator = SimpleNamespace(
            agents=[agent], stop=lambda: None, resume=lambda: None,
        )
        state.status = "running"
        state.run_id = "run-1"

        async def exercise():
            state.run_task = asyncio.create_task(asyncio.sleep(3600))
            return await backend.server.stop_run(state)

        response = asyncio.run(exercise())

        self.assertTrue(response["ok"])
        self.assertEqual(state.status, "idle")
        self.assertEqual(agent.status.value, "idle")
        self.assertEqual(agent.retry_at, "")
        self.assertEqual(state.event_log[-1]["data"]["status"], "stopped")
        self.assertIn("retries were cancelled", state.event_log[-1]["data"]["message"])
        self.assertEqual(state.event_log[-1]["event_id"], 1)

    def test_stop_preserves_recovery_state_for_empty_fresh_start(self):
        import backend.server

        with tempfile.TemporaryDirectory() as directory:
            state = backend.server.AppState()
            state.open_project(directory)
            state.run_id = "run-1"
            state.status = "paused"
            state.orchestrator = SimpleNamespace(
                agents=[], stop=lambda: None, resume=lambda: None,
                save_state=lambda: state.store.save_run_state({
                    "idea": "Finish the interrupted design",
                    "phase": "peer_review",
                    "agents": {},
                }),
            )

            response = asyncio.run(backend.server.stop_run(state))

            self.assertTrue(response["ok"])
            self.assertEqual(state.store.load_run_state()["idea"], "Finish the interrupted design")
            self.assertEqual(state.store.load_run_state()["phase"], "peer_review")

    def test_frontend_allows_empty_start_to_resume_saved_run(self):
        api_js = (Path(__file__).parents[1] / "frontend" / "js" / "api.js").read_text()

        self.assertNotIn("Please type a prompt/task in the bottom chat input to start the run.", api_js)
        self.assertIn("if (data.resumed) notify('Continuing the previous design run.');", api_js)

    def test_only_empty_or_continuation_prompts_resume_previous_workflow(self):
        from backend.server import is_continuation_prompt

        for prompt in ("", "continue", "Please resume the design", "keep going", "carry on with the review"):
            self.assertTrue(is_continuation_prompt(prompt), prompt)
        for prompt in ("Design a billing API", "Add SSO", "Replace Postgres with DynamoDB"):
            self.assertFalse(is_continuation_prompt(prompt), prompt)

    def test_progress_endpoint_reads_saved_phase_without_consuming_it(self):
        import backend.server

        with tempfile.TemporaryDirectory() as directory:
            state = backend.server.AppState()
            state.open_project(directory)
            state.workspace.init("Design a billing API")
            recovery = {
                "idea": "Design a billing API",
                "phase": "peer_review",
                "agents": {},
            }
            state.store.save_run_state(recovery)

            result = backend.server.run_progress(state)

            self.assertEqual(result["phase"], "peer_review")
            self.assertTrue(result["resumable"])
            self.assertIn("ready to continue", result["message"])
            self.assertEqual(state.store.load_run_state(), recovery)

    def test_explicit_continuation_restores_phase_after_manual_artifact_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("Design a billing API")
            store = ProjectStore(workspace.root)
            store.save_run_state({
                "idea": "Design a billing API",
                "phase": "peer_review",
                "artifact_fingerprints": workspace.artifact_fingerprints(),
                "agents": {},
            })
            workspace.write("design", "# Manually revised design\n")
            orchestrator = Orchestrator(
                [], workspace, restore=True, store=store,
                allow_artifact_changes_on_restore=True,
            )
            orchestrator.idea = "Design a billing API"

            self.assertTrue(orchestrator.load_state())
            self.assertEqual(orchestrator.phase.value, "peer_review")
            self.assertEqual(workspace.read("design"), "# Manually revised design\n")

    def test_automatic_restore_still_rejects_unexpected_artifact_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("Design a billing API")
            store = ProjectStore(workspace.root)
            store.save_run_state({
                "idea": "Design a billing API",
                "phase": "peer_review",
                "artifact_fingerprints": workspace.artifact_fingerprints(),
                "agents": {},
            })
            workspace.write("design", "# Unexpected external change\n")
            orchestrator = Orchestrator([], workspace, restore=True, store=store)
            orchestrator.idea = "Design a billing API"

            self.assertFalse(orchestrator.load_state())
            self.assertEqual(orchestrator.phase.value, "discovery")

    def test_frontend_exposes_progress_as_a_dedicated_action(self):
        api_js = (Path(__file__).parents[1] / "frontend" / "js" / "api.js").read_text()
        index_html = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()

        self.assertNotIn("isProgressQuestion", api_js)
        self.assertNotIn("answerProgressQuestion", api_js)
        self.assertIn('id="statusBtn"', index_html)
        self.assertIn('onclick="showRunProgress()"', index_html)
        self.assertIn("fetch('/run/progress')", api_js)

    def test_expired_last_tab_lease_releases_project_runtime(self):
        import backend.server

        with tempfile.TemporaryDirectory() as directory:
            state = backend.server.AppState()
            state.open_project(directory)
            canonical = str(Path(directory).resolve())
            backend.server.app_states[canonical] = state
            backend.server.session_projects["closed-tab"] = canonical
            backend.server.session_last_seen["closed-tab"] = 10.0
            store = state.store

            expired = asyncio.run(backend.server.expire_stale_bindings(now=100.0, ttl_seconds=75))

            self.assertEqual(expired, ["closed-tab"])
            self.assertNotIn("closed-tab", backend.server.session_projects)
            self.assertNotIn(canonical, backend.server.app_states)
            self.assertTrue(store._closed)

    def test_active_tab_heartbeat_prevents_lease_expiry(self):
        import backend.server

        backend.server.session_projects["active-tab"] = "/tmp/project"
        backend.server.session_last_seen["active-tab"] = 50.0

        expired = asyncio.run(backend.server.expire_stale_bindings(now=100.0, ttl_seconds=75))

        self.assertEqual(expired, [])
        self.assertIn("active-tab", backend.server.session_projects)

    def test_workspace_scoped_context_and_reset_tracking(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.init("idea")
            workspace.write("decisions", "# Key Decisions\n\n- Use FastAPI")

            scoped = workspace.scoped_context(["design", "decisions"])
            self.assertIn("DESIGN.md", scoped)
            self.assertIn("Use FastAPI", scoped)

            first = workspace.changed_context("architect", ["design", "decisions"])
            second = workspace.changed_context("architect", ["design", "decisions"])
            self.assertIn("DESIGN.md", first)
            self.assertEqual(second, "(no changes since your last turn)")

            workspace.reset_context_tracking("architect")
            third = workspace.changed_context("architect", ["design", "decisions"])
            self.assertIn("DESIGN.md", third)

    def test_workspace_snapshot_exposes_logbook_and_questions_for_sidebar(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("logging framework")
            workspace.write("questions", "# Decision Checkpoint\n\nChoose a storage model.")
            workspace.append("logbook", "Architecture reviewed", "architect")

            snapshot = workspace.snapshot()

            self.assertIn("Architecture reviewed", snapshot["logbook"])
            self.assertIn("Choose a storage model", snapshot["questions"])

    def test_designflow_brief_has_a_dedicated_sidebar_entry(self):
        html = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()
        workspace_js = (Path(__file__).parents[1] / "frontend" / "js" / "workspace.js").read_text()
        self.assertIn('id="wsbtn-brief"', html)
        self.assertIn("loadWsFile('DESIGNFLOW.md')", html)
        self.assertIn("f !== 'DESIGNFLOW.md'", workspace_js)
        project_files_label = html.index("Project files")
        brief_button = html.index('id="wsbtn-brief"')
        self.assertGreater(brief_button, project_files_label)
        self.assertIn("DesignFlow documents", html)
        self.assertNotIn(">DesignFlow state<", html)

    def test_specialist_context_is_bounded_and_section_specific(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.write("context", "# Context\n\nBackend logging framework.")
            workspace.write("decisions", "# Key Decisions\n\nUse versioned protocols.")
            workspace.write(
                "design",
                "# Design\n\n## API Protocols & Formats\n" + "contract detail " * 500
                + "\n\n## Visual Interface Design\n" + "irrelevant visual detail " * 1000,
            )
            workspace.write(
                "plan",
                "# Plan\n\n## SDK Protocol Work\n" + "sdk task " * 400
                + "\n\n## Marketing Launch\n" + "campaign task " * 1000,
            )

            context = workspace.specialist_context(
                ["api", "protocol", "format"], ["sdk", "protocol"], max_chars=9000,
            )

            self.assertLessEqual(len(context), 9000)
            self.assertIn("API Protocols & Formats", context)
            self.assertIn("SDK Protocol Work", context)
            self.assertNotIn("Visual Interface Design", context)
            self.assertNotIn("Marketing Launch", context)

    def test_logbook_rotates_legacy_content_and_indexes_per_run_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("logging framework")
            workspace.append("logbook", "legacy verbose response", "old-agent", "Turn completed")

            workspace.begin_logbook_run("run-123", "Review logging architecture")
            workspace.append("logbook", "new specialist critique", "api-designer", "Peer review")
            workspace.finish_logbook_run(
                "run-123", "done", [{"name": "api-designer", "total_tokens": 1250}],
            )

            index = workspace.read("logbook")
            transcript = (workspace.root / "logbook" / "run-123.md").read_text()
            legacy_files = list((workspace.root / "logbook").glob("legacy-*.md"))
            self.assertEqual(len(legacy_files), 1)
            self.assertIn("legacy verbose response", legacy_files[0].read_text())
            self.assertIn("status: done", index)
            self.assertIn("1,250 tokens", index)
            self.assertNotIn("new specialist critique", index)
            self.assertIn("new specialist critique", transcript)
            self.assertIn("api-designer", transcript)
            self.assertLess(len(index), 1000)

    def test_logbook_reconciles_run_left_open_by_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("logging framework")
            workspace.begin_logbook_run("crashed-run", "Draft architecture")
            workspace._active_logbook_run_id = ""  # simulate a fresh process after a hard exit

            interrupted = workspace.reconcile_interrupted_logbook_runs()

            self.assertEqual(interrupted, ["crashed-run"])
            self.assertIn("status: interrupted", workspace.read("logbook"))
            transcript = (workspace.root / "logbook" / "crashed-run.md").read_text()
            self.assertIn("**Status:** interrupted", transcript)

    def test_legacy_run_state_without_goal_is_not_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            (workspace.root / "run_state.json").write_text(json.dumps({
                "mode": "debate",
                "turn_sequence": 99,
                "agents": {},
            }))
            orchestrator = Orchestrator([], workspace, restore=True)
            orchestrator.idea = "Design a friend meetup planning application"

            self.assertFalse(orchestrator.load_state())
            self.assertEqual(orchestrator._turn_sequence, 0)

    def test_existing_project_is_asked_for_missing_deployment_constraint(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.write_brief(
                "Design an application where groups of friends choose a fair meeting point, "
                "coordinate arrival using live location, chat, and archive completed events."
            )
            workspace.init("short")
            orchestrator = Orchestrator([], workspace, require_approval=True)
            orchestrator.idea = "continue"

            self.assertIn("deployment constraint", orchestrator._deterministic_discovery_question())

    def test_enterprise_brief_gets_architecture_constraints_before_specialists(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.write_brief(
                "Build an enterprise logging framework used by multiple services and languages "
                "with runtime log-level configuration by feature and destination."
            )
            workspace.init(workspace.brief())
            orchestrator = Orchestrator([], workspace, require_approval=True)
            orchestrator.idea = workspace.brief()

            self.assertIn("deployment constraint", orchestrator._deterministic_discovery_question())

    def test_discovery_asks_specific_provider_after_cloud_specific_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.write_brief("Build an internal enterprise logging product for engineering teams.")
            workspace.init(workspace.brief())
            workspace.record_user_decision(
                "What deployment constraint should drive the architecture?",
                "B — Optimize for one cloud provider",
            )
            orchestrator = Orchestrator([], workspace, require_approval=True)
            orchestrator.idea = workspace.brief()
            self.assertIn("Which cloud provider", orchestrator._deterministic_discovery_question())

    def test_adaptive_discovery_validates_structured_question(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator([], Workspace(directory), require_approval=True)
            response = json.dumps({
                "status": "ask",
                "dimension": "operations",
                "question": "Who will operate this system after launch?",
                "reason": "Operational ownership changes deployment and observability choices.",
                "options": [
                    {"label": "Product team", "consequence": "Prefer managed components."},
                    {"label": "Platform team", "consequence": "More operational control is viable."},
                ],
                "recommended": "Product team",
                "blocking": True,
            })
            question = orchestrator._parse_discovery_proposal(response)
            self.assertIn("Who will operate", question)
            self.assertIn("Why this matters", question)
            self.assertEqual(orchestrator._discovery_questions_asked, 1)

    def test_adaptive_discovery_rejects_internal_decision_heading(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator([], Workspace(directory), require_approval=True)
            response = json.dumps({
                "status": "ask",
                "dimension": "delivery",
                "question": "Decision 31: Delivery Contract",
                "reason": "The acknowledgement boundary controls latency and possible event loss.",
                "options": [
                    {"label": "Accept into memory", "consequence": "Lower latency with a small loss window."},
                    {"label": "Confirm durable storage", "consequence": "Lower loss risk with additional latency."},
                ],
                "recommended": "Accept into memory",
                "blocking": True,
            })
            self.assertIsNone(orchestrator._parse_discovery_proposal(response))

    def test_adaptive_discovery_fails_over_to_another_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("Build a logging service")
            failed = StatefulFake(AgentConfig(id="provider-a", name="failed", kind="cli"))
            failed.replies = iter([])
            healthy_response = json.dumps({
                "status": "ask",
                "dimension": "deployment",
                "question": "Where must this logging service be deployed?",
                "reason": "Deployment constraints determine packaging and managed-service choices.",
                "options": ["Cloud-agnostic", "One cloud provider", "Self-hosted"],
                "recommended": "Cloud-agnostic",
                "blocking": True,
            })
            healthy = StatefulFake(
                AgentConfig(id="provider-b", name="healthy", kind="openai", model="gpt-4o"),
                replies=[healthy_response],
            )
            events = []
            orchestrator = Orchestrator([failed, healthy], workspace, event_cb=events.append, require_approval=True)
            orchestrator.idea = "Build a logging service"
            question = asyncio.run(orchestrator._adaptive_discovery_question(failed, 1))
            self.assertIn("Where must", question)
            self.assertTrue(any(
                event.kind == EventKind.PHASE and event.data.get("status") == "provider_failover"
                for event in events
            ))
            self.assertFalse(orchestrator._adaptive_discovery_unavailable)
            self.assertIn("provider-a", orchestrator._discovery_failed_providers)

    def test_inline_questions_are_not_announced_as_workspace_artifact_writes(self):
        source = (Path(__file__).parents[1] / "backend" / "orchestrator.py").read_text()
        self.assertNotIn('EventKind.FILE_WRITE, agent=coordinator.name, data={"file": "QUESTIONS.md"}', source)

    def test_task_instruction_does_not_replace_product_goal_in_context(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            goal = "Build a multi-language enterprise logging framework."
            workspace.write_brief(goal)
            workspace.init(goal)
            orchestrator = Orchestrator([], workspace, store=ProjectStore(workspace.root))
            orchestrator.idea = goal
            orchestrator.task = "Review the requirements and drive a rigorous debate."

            orchestrator.save_state()

            context = workspace.read("context")
            self.assertIn(goal, context)
            self.assertNotIn("## Product Goal\nReview the requirements", context)

    def test_generated_design_goal_header_is_repaired_without_replacing_content(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("Review the requirements and drive a debate")
            workspace.append("design", "Keep this architecture section", "author")

            workspace.align_generated_goal_header("Build an enterprise logging framework")

            design = workspace.read("design")
            self.assertIn("**Idea:** Build an enterprise logging framework", design)
            self.assertIn("Keep this architecture section", design)

    def test_context_memory_is_generated_without_model_and_tracks_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.write_brief("Build a collaborative meeting-point planner for groups of friends.")
            workspace.init("meeting planner")
            workspace.write("decisions", "# Key Decisions\n\n- Use explicit consent for live location sharing.")

            context = workspace.refresh_context(
                phase="peer_review",
                consulted_specialists=["security_auditor", "ux_simplifier"],
                next_action="Review location privacy boundaries.",
            )

            self.assertIn("Build a collaborative meeting-point planner", context)
            self.assertIn("peer_review", context)
            self.assertIn("security_auditor", context)
            self.assertIn("Use explicit consent", context)
            self.assertIn("Artifact Fingerprints", context)
            self.assertEqual(workspace.read("context"), context)

    def test_changed_context_uses_compact_memory_without_logbook(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.init("meeting planner")
            workspace.append("logbook", "A very large audit entry", "agent")

            context = workspace.changed_context("reviewer", ["context", "design"])

            self.assertIn("CONTEXT.md", context)
            self.assertNotIn("A very large audit entry", context)

    def test_context_events_keep_complete_boundaries_and_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.init("meeting planner")
            critique = "The location-sharing consent boundary must be explicit and revocable."
            workspace.add_context_event("peer_critique", critique, "refinement", "security_auditor")
            workspace.add_context_event("user_steering", "Prioritize mobile usability.", "drafting", "user")

            context = workspace.refresh_context(phase="refinement")

            self.assertIn(critique, context)
            self.assertIn("Prioritize mobile usability.", context)
            self.assertNotIn(critique[:20] + "…", context)
            workspace.resolve_context_events({"peer_critique"})
            refreshed = workspace.refresh_context(phase="refinement")
            self.assertNotIn(critique, refreshed)
            self.assertEqual(workspace.context_events(statuses=("incorporated",))[0]["status"], "incorporated")

    def test_phase_context_excludes_irrelevant_open_events(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.init("meeting planner")
            workspace.add_context_event("peer_critique", "Database critique", "refinement", "data_architect")
            workspace.add_context_event("user_decision", "Use invite-only groups", "approval", "user")

            drafting_context = workspace.refresh_context(phase="drafting")

            self.assertNotIn("Database critique", drafting_context)
            self.assertIn("Use invite-only groups", drafting_context)

    def test_workspace_changed_context_can_send_src_index_without_file_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.init("idea")
            workspace.write_src("src/api/app.py", "print('ok')")

            context = workspace.changed_context("researcher", ["design", "plan", "src_index"])
            self.assertIn("src/api/app.py", context)
            self.assertNotIn("print('ok')", context)

    def test_source_index_skips_binary_and_irrelevant_generated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.write_src("src/app.py", "print('ok')")
            workspace.write_src("assets/raw.dat", "large irrelevant payload")

            index = workspace.src_index()

            self.assertIn("src/app.py", index)
            self.assertNotIn("assets/raw.dat", index)

    def test_usage_is_accounted_by_workflow_phase(self):
        agent = StatefulFake(AgentConfig(name="reviewer", kind="openai"), replies=["review"])
        orchestrator = Orchestrator([agent], Workspace(tempfile.mkdtemp()), require_approval=False)
        agent.send("review this")

        orchestrator._record_turn_usage(agent, "peer_review")

        self.assertEqual(orchestrator.phase_usage["peer_review"]["tokens"], 120)
        self.assertEqual(orchestrator.phase_usage["peer_review"]["turns"], 1)

    def test_oversized_artifact_context_is_compacted_before_provider_call(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.init("small product")
            agent = StatefulFake(
                AgentConfig(name="reviewer", kind="openai", extra={"max_input_tokens_per_turn": 1500}),
                replies=["ok"],
            )
            orchestrator = Orchestrator(
                [agent], workspace, event_cb=events.append, max_tokens=5000, require_approval=False,
            )
            orchestrator._running = True

            reply = asyncio.run(orchestrator._send_agent(
                agent, "Review the design", ephemeral_context="x" * 12000,
            ))

            self.assertEqual(reply, "ok")
            self.assertTrue(any(
                event.kind.value == "phase" and event.data.get("status") == "context_compacted"
                for event in events
            ))
            self.assertLess(len(agent.received[0][-1]["content"]), 12000)

    def test_oversized_prompt_enters_recoverable_compact_retry_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.init("small product")
            events = []
            agent = StatefulFake(
                AgentConfig(name="reviewer", kind="openai", extra={"max_input_tokens_per_turn": 100}),
            )
            orchestrator = Orchestrator(
                [agent], workspace, max_tokens=5000, require_approval=False, event_cb=events.append,
            )
            orchestrator._running = True

            async def exercise():
                task = asyncio.create_task(orchestrator._send_agent(agent, "x" * 4000))
                while not orchestrator.failed_turn:
                    await asyncio.sleep(0)
                self.assertEqual(orchestrator.failed_turn["error_code"], "context_too_large")
                self.assertEqual(agent.received, [])
                agent.config.extra["max_input_tokens_per_turn"] = 2000
                orchestrator.retry_failed_turn()
                self.assertEqual(await task, "ok")

            asyncio.run(exercise())
            self.assertTrue(any(
                event.kind == EventKind.ERROR and event.data.get("recoverable")
                for event in events
            ))

    def test_oversized_historical_response_is_bounded_before_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("logging framework")
            agent = StatefulFake(
                AgentConfig(name="architect", kind="openai", extra={"max_input_tokens_per_turn": 8000}),
                replies=["ok"],
            )
            agent.manages_context = False
            agent.history = [
                Message(role="user", content="Draft the architecture"),
                Message(role="assistant", content="x" * 140000),
            ]
            orchestrator = Orchestrator([agent], workspace, max_tokens=50000, require_approval=False)
            orchestrator._running = True

            result = asyncio.run(orchestrator._send_agent(
                agent, "Answer this basic question", ephemeral_context=workspace.read("context"),
            ))

            self.assertEqual(result, "ok")
            sent_text = "\n".join(message["content"] for message in agent.received[0])
            self.assertIn("older response truncated", sent_text)
            self.assertLess(len(sent_text), 40000)

    def test_historical_total_turn_peak_is_not_double_counted_as_output(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.init("logging framework")
            agent = StatefulFake(AgentConfig(
                name="architect", kind="cli", id="virtual", base_id="codex-base",
                extra={"max_tokens": 100},
            ))
            events = []
            orchestrator = Orchestrator([agent], workspace, event_cb=events.append, max_tokens=1000)
            orchestrator._running = True
            orchestrator.run_token_total = 700
            orchestrator._provider_turn_peak["codex-base"] = 68976

            self.assertEqual(asyncio.run(orchestrator._send_agent(agent, "Review this design")), "ok")
            self.assertFalse(orchestrator._paused)
            self.assertFalse(any(event.data.get("status") == "budget_exhausted" for event in events))

    def test_provider_turn_peak_survives_run_state_cleanup_and_store_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory)
            store = ProjectStore(metadata)
            store.record_provider_turn_peak("codex-base", 62575)
            store.save_run_state({"provider_turn_peak": {"codex-base": 62575}})
            store.clear_run_state()
            store.close()

            reopened = ProjectStore(metadata)
            self.assertEqual(reopened.load_provider_turn_peaks()["codex-base"], 62575)
            reopened.close()

    def test_provider_turn_peak_is_derived_from_legacy_events(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectStore(Path(directory))
            store.append_event("run-1", {
                "timestamp": "now", "kind": "turn_end", "agent": "architect",
                "data": {
                    "turn_id": "turn-1", "provider_id": "gemini-base",
                    "usage": {"total_tokens": 11035}, "response": "review",
                },
            })

            self.assertEqual(store.load_provider_turn_peaks()["gemini-base"], 11035)
            store.close()

    def test_workspace_create_file(self):
        from fastapi.testclient import TestClient
        import backend.server
        client = TestClient(backend.server.app)
        res = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(res.status_code, 200)

        with tempfile.TemporaryDirectory() as td:
            res = client.post("/project/open", json={"path": td})
            self.assertEqual(res.status_code, 200)

            # Create a file via POST empty content
            res = client.post("/workspace/src/new_test_file.txt", json={"content": ""})
            self.assertEqual(res.status_code, 200)

            # Read the file via GET
            res = client.get("/workspace/src/new_test_file.txt")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["content"], "")

            # Write some content
            res = client.post("/workspace/src/new_test_file.txt", json={"content": "hello world"})
            self.assertEqual(res.status_code, 200)

            # Read the file again
            res = client.get("/workspace/src/new_test_file.txt")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["content"], "hello world")

            # Decisions root file is supported too
            res = client.post("/workspace/file/decisions", json={"content": "# Key Decisions\n\n- Use FastAPI"})
            self.assertEqual(res.status_code, 200)
            res = client.get("/workspace/file/decisions")
            self.assertEqual(res.status_code, 200)
            self.assertIn("Use FastAPI", res.json()["content"])

    def test_empty_project_does_not_inherit_agents(self):
        from fastapi.testclient import TestClient
        import backend.server
        client = TestClient(backend.server.app)
        res = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(res.status_code, 200)

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            res = client.post("/project/open", json={"path": str(td_path / "project")})
            self.assertEqual(res.status_code, 200)
            res = client.get("/agents")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["agents"], [])

            local_agent = {
                "name": "LocalAgent",
                "kind": "openai",
                "role": "LocalRole",
                "model": "gpt-4",
                "api_key": "local_secret",
                "system_prompt": "local system",
                "max_history_turns": 20,
                "extra": {}
            }
            res = client.post("/agents", json=local_agent)
            self.assertEqual(res.status_code, 200)
            res = client.get("/agents")
            self.assertEqual(res.status_code, 200)
            self.assertEqual([agent["name"] for agent in res.json()["agents"]], ["LocalAgent"])


class FrontendPrivacyTests(unittest.TestCase):
    def test_project_picker_does_not_ship_a_personal_path(self):
        html = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()
        self.assertNotIn("/Users/", html)
        self.assertNotIn("/home/", html)
        self.assertIn('placeholder="/path/to/your/project"', html)

    def test_visual_design_action_submits_hidden_prompt_directly(self):
        workspace_js = (Path(__file__).parents[1] / "frontend" / "js" / "workspace.js").read_text()
        api_js = (Path(__file__).parents[1] / "frontend" / "js" / "api.js").read_text()
        self.assertIn("await startRun(prompt, {hiddenPrompt: true})", workspace_js)
        self.assertNotIn("steerInput.value = \"Update DESIGN.md directly", workspace_js)
        self.assertIn("!options.hiddenPrompt", api_js)

    def test_project_binding_reconnects_event_stream(self):
        state_js = (Path(__file__).parents[1] / "frontend" / "js" / "state.js").read_text()
        api_js = (Path(__file__).parents[1] / "frontend" / "js" / "api.js").read_text()
        main_js = (Path(__file__).parents[1] / "frontend" / "js" / "main.js").read_text()
        self.assertIn("if (typeof connectSSE === 'function') connectSSE(true);", state_js)
        self.assertIn("activeEventSource.close()", api_js)
        self.assertIn("activeEventSource = es", api_js)
        self.assertIn("loadCurrentProject().finally(() => connectSSE(true));", main_js)
        self.assertNotIn("connectSSE();\nloadCurrentProject();", main_js)
        self.assertIn("seenEventIds.has(eventId)", api_js)
        self.assertIn("ev.data.status === 'stopped'", api_js)
        self.assertIn("Still waiting for the model", api_js)
        self.assertIn("Agent turn cancelled when the run was stopped", api_js)

    def test_agent_capacity_uses_runtime_failure_not_only_health_probe(self):
        api_js = (Path(__file__).parents[1] / "frontend" / "js" / "api.js").read_text()
        agents_js = (Path(__file__).parents[1] / "frontend" / "js" / "agents.js").read_text()
        self.assertIn("agent.base_id || agent.id", api_js)
        self.assertIn("res.failed_turn || {}", api_js)
        self.assertIn("capacity.error_code === 'quota_exhausted'", agents_js)
        self.assertIn("'Quota exhausted'", agents_js)


class DeterministicRoutingTests(unittest.TestCase):
    def test_simple_commands_use_high_confidence_fuzzy_routing(self):
        self.assertEqual(Orchestrator._fuzzy_intent("show staus"), "status")
        self.assertEqual(Orchestrator._fuzzy_intent("list agents"), "agents")
        self.assertEqual(Orchestrator._fuzzy_intent("help"), "help")
        self.assertEqual(Orchestrator._fuzzy_intent("design a secure payment architecture"), "")

    def test_auto_mode_routes_basic_questions_without_team_debate(self):
        self.assertFalse(Orchestrator._should_run_team_workflow("Why did we choose Kafka?", "auto"))
        self.assertFalse(Orchestrator._should_run_team_workflow("What does this setting do?", "auto"))
        self.assertTrue(Orchestrator._should_run_team_workflow("Design a secure payment architecture", "auto"))
        self.assertTrue(Orchestrator._should_run_team_workflow("I want to build a logging platform", "auto"))
        self.assertTrue(Orchestrator._should_run_team_workflow("Debate the storage approach", "auto"))
        self.assertFalse(Orchestrator._should_run_team_workflow("Design a system", "direct"))
        self.assertTrue(Orchestrator._should_run_team_workflow("What is this?", "debate"))

    def test_targeted_artifact_edits_use_one_agent(self):
        prompt = "Update DESIGN.md to include a Mermaid architecture diagram."
        self.assertTrue(Orchestrator._is_targeted_artifact_update(prompt))
        self.assertFalse(Orchestrator._should_run_team_workflow(prompt, "auto"))
        self.assertTrue(Orchestrator._should_run_team_workflow(
            "Debate the options, then update DESIGN.md", "auto"
        ))

    def test_latest_task_controls_routing_for_existing_project(self):
        saved_goal = "Design a distributed logging platform"
        task = "Update DESIGN.md to add a Mermaid architecture diagram"
        request = Orchestrator._effective_request(saved_goal, task)
        self.assertEqual(request, task)
        self.assertFalse(Orchestrator._should_run_team_workflow(request, "auto"))

    def test_debug_observer_is_enabled_by_command_line_argument(self):
        from run import build_parser
        self.assertFalse(build_parser().parse_args([]).debug_observer)
        self.assertTrue(build_parser().parse_args(["--debug-observer"]).debug_observer)

    def test_debug_observer_redacts_and_reports_missing_design_write(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = DebugObserver(Path(directory), max_events=20)
            observer.start_run("run-1", "Generate a Mermaid visual design with sk-secret123456", "auto")
            observer.observe({"kind": "done", "data": {"api_key": "sk-secret123456"}})
            observer.close()
            events = (Path(directory) / "debug" / "events.jsonl").read_text()
            insights = json.loads((Path(directory) / "debug" / "insights.json").read_text())
            self.assertNotIn("sk-secret123456", events)
            self.assertTrue(any(item["code"] == "missing_requested_artifact" for item in insights["insights"]))

    def test_provider_errors_prefer_structured_status_codes(self):
        forbidden = RuntimeError("a very long provider response")
        forbidden.status_code = 403
        public = classify_provider_error(forbidden)
        self.assertEqual(public.code, "authentication_failed")
        self.assertNotIn("long provider response", public.message)

        limited = RuntimeError("request rejected")
        limited.status_code = 429
        public = classify_provider_error(limited)
        self.assertEqual(public.code, "rate_limited")
        self.assertTrue(public.retryable)

        quota = RuntimeError("insufficient_quota: add billing credits")
        quota.status_code = 429
        self.assertEqual(classify_provider_error(quota).code, "quota_exhausted")

    def test_decision_checkpoint_state_has_an_explicit_lifecycle(self):
        from backend.server import AppState, broadcast
        from backend.orchestrator import Event, EventKind

        state = AppState()
        broadcast(Event(EventKind.PHASE, data={"status": "waiting_for_approval"}), state)
        self.assertEqual(state.status, "paused")
        self.assertTrue(state.awaiting_input)

        broadcast(Event(EventKind.PHASE, data={"status": "continuing_debate"}), state)
        self.assertEqual(state.status, "running")
        self.assertFalse(state.awaiting_input)

        broadcast(Event(EventKind.DONE, data={}), state)
        self.assertFalse(state.awaiting_input)

    def test_web_checkpoint_offers_one_click_options_and_custom_answer(self):
        source = (Path(__file__).parents[1] / "frontend" / "js" / "api.js").read_text()
        self.assertIn("submitDecisionOption", source)
        self.assertIn("Other — type my own answer in the prompt box", source)
        self.assertIn('type="radio" name="decisionChoice"', source)
        self.assertNotIn("Continue with this choice", source)
        self.assertNotIn("One decision at a time", source)
        self.assertIn("await window.submitSelectedDecision()", source)
        self.assertIn("body: JSON.stringify({option_id: optionId, custom_answer: customAnswer})", source)
        self.assertIn("why this matters", source.lower())
        self.assertIn("await fetch('/run/resume'", source)

    def test_user_events_display_authenticated_username(self):
        server = (Path(__file__).parents[1] / "backend" / "server.py").read_text()
        orchestrator = (Path(__file__).parents[1] / "backend" / "orchestrator.py").read_text()
        web = (Path(__file__).parents[1] / "frontend" / "js" / "api.js").read_text()
        self.assertIn("body.message, session.username", server)
        self.assertIn("bool(next_checkpoint), session.username", server)
        self.assertIn('agent=username or "human"', orchestrator)
        self.assertIn("ev.agent === 'human' && currentUser?.username", web)

    def test_vscode_checkpoint_has_option_and_other_answer_flow(self):
        source = (Path(__file__).parents[1] / "vscode-extension" / "src" / "extension.ts").read_text()
        self.assertIn("one question at a time", source)
        self.assertIn("showCheckpoint", source)
        self.assertIn("O — Other…", source)
        self.assertIn("/workspace/file/questions", source)

    def test_project_usage_survives_store_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / ".designflow"
            store = ProjectStore(metadata)
            store.start_run("run-1", "first")
            store.finish_run("run-1", "done", [{
                "total_tokens": 120, "cached_input_tokens": 30,
                "cost_usd": 0.012, "pricing_known": True,
            }])
            store.start_run("run-2", "second")
            store.update_run_metrics("run-2", [{
                "total_tokens": 80, "cached_input_tokens": 10,
                "cost_usd": 0.008, "pricing_known": False,
            }])
            store.close()

            reopened = ProjectStore(metadata)
            usage = reopened.project_usage()
            self.assertEqual(usage["total_tokens"], 200)
            self.assertEqual(usage["cached_input_tokens"], 40)
            self.assertAlmostEqual(usage["estimated_cost_usd"], 0.02)
            self.assertFalse(usage["pricing_complete"])
            self.assertEqual(usage["run_count"], 2)
            reopened.close()

if __name__ == "__main__":
    unittest.main()
