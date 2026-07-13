import asyncio
import os
os.environ["DESIGNFLOW_TEST"] = "1"
import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.agents.base import AgentBase, AgentConfig, Usage
from backend.agents.providers import CLIAgent, GroqAgent, discover_models
from backend.orchestrator import Orchestrator
from backend.errors import classify_provider_error
from backend.storage import ProjectStore
from backend.workspace.workspace import Workspace


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

## Known Unknowns & Validation Plan
- Validate the storage choice with representative data before implementation.
"""

VALID_DECISIONS = """## Accepted Decisions
- Start with SQLite for the planning baseline because deployment simplicity matters more than scale initially.

## Trade-offs
- Revisit the database choice after measuring representative concurrency and query patterns.
"""


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
        self.assertEqual(retry_events[0].kind.value, "retry")

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
            self.assertIn("Requirements", orchestrator.ws.read("plan"))

    def test_global_agents_endpoints(self):
        from fastapi.testclient import TestClient
        import backend.server

        with tempfile.TemporaryDirectory() as tmpdir:
            orig_path = backend.server.GLOBAL_AGENTS_PATH
            backend.server.GLOBAL_AGENTS_PATH = Path(tmpdir) / "global_agents.json"

            try:
                client = TestClient(backend.server.app)
                res = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
                self.assertEqual(res.status_code, 200)
                res = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
                self.assertEqual(res.status_code, 200)

                res = client.get("/agents/global")
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json(), {"agents": []})

                agent_payload = {
                    "name": "Global Bot",
                    "kind": "openai",
                    "role": "helper",
                    "model": "gpt-4o",
                    "api_key": "my-secret-key-xyz",
                    "system_prompt": "hello",
                    "max_history_turns": 20,
                    "extra": {"is_coordinator": True}
                }
                res = client.post("/agents/global", json=agent_payload)
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertTrue(data["ok"])
                agent_id = data["agent"]["id"]
                self.assertEqual(data["agent"]["name"], "Global Bot")
                self.assertEqual(data["agent"]["extra"].get("is_coordinator"), True)

                # Verify file on disk is encrypted
                raw_file_content = backend.server.GLOBAL_AGENTS_PATH.read_text()
                self.assertNotIn("my-secret-key-xyz", raw_file_content)
                self.assertIn("gAAAA", raw_file_content) # Fernet signature

                # Verify API returns decrypted value
                res = client.get("/agents/global")
                self.assertEqual(res.status_code, 200)
                agents = res.json()["agents"]
                self.assertEqual(len(agents), 1)
                self.assertEqual(agents[0]["id"], agent_id)
                self.assertEqual(agents[0]["api_key"], "my-secret-key-xyz")

                res = client.delete(f"/agents/global/{agent_id}")
                self.assertEqual(res.status_code, 200)
                self.assertTrue(res.json()["ok"])

                res = client.get("/agents/global")
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json(), {"agents": []})

            finally:
                backend.server.GLOBAL_AGENTS_PATH = orig_path

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

            async def run_test():
                run_task = asyncio.create_task(orchestrator.run("social app"))
                await asyncio.sleep(0.05)
                self.assertTrue(orchestrator._paused)

                # Steer the coordinator with user response
                await orchestrator.steer("We want SQLite")
                orchestrator.resume()

                # Wait for task to finish
                await run_task

                # Check that the next prompt received the steering response!
                self.assertIn("We want SQLite", boss.received[-1][-1]["content"])
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
            self.assertLessEqual(len(selected), 4)
            self.assertIn("security_auditor", names)
            self.assertIn("data_architect", names)
            self.assertIn("api_designer", names)
            self.assertNotIn("marketing_alpha", names)

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

    def test_global_plus_project_configs(self):
        from fastapi.testclient import TestClient
        import backend.server

        with tempfile.TemporaryDirectory() as tmpdir:
            orig_path = backend.server.GLOBAL_AGENTS_PATH
            backend.server.GLOBAL_AGENTS_PATH = Path(tmpdir) / "global_agents.json"

            try:
                client = TestClient(backend.server.app)
                res = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
                self.assertEqual(res.status_code, 200)

                # 1. Create a Global Agent
                global_agent = {
                    "name": "Standard Dev",
                    "kind": "openai",
                    "role": "developer",
                    "model": "gpt-4o",
                    "api_key": "global-secret",
                    "system_prompt": "global system",
                    "max_history_turns": 20,
                    "extra": {}
                }
                res = client.post("/agents/global", json=global_agent)
                self.assertEqual(res.status_code, 200)
                global_id = res.json()["agent"]["id"]

                # 2. Update Global Agent via PUT
                global_agent["system_prompt"] = "updated global system"
                res = client.put(f"/agents/global/{global_id}", json=global_agent)
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json()["agent"]["system_prompt"], "updated global system")

                # 3. Simulate opening a project and adding a project-level agent via POST /agents
                payload = {"path": tmpdir}
                res = client.post("/project/open", json=payload)
                self.assertEqual(res.status_code, 200)
                session_cookie = client.cookies.get("session_id")
                client.cookies.set("session_id", session_cookie)
                local_agent = {
                    "name": "Standard Dev",  # Name collision!
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

                # Test PUT /agents/{agent_id}
                local_agent["system_prompt"] = "updated local system"
                res = client.put(f"/agents/{local_id}", json=local_agent)
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json()["agent"]["system_prompt"], "updated local system")

                # 4. GET /agents should list both and resolve merged correctly
                res = client.get("/agents")
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertEqual(len(data["global"]), 1)
                self.assertEqual(len(data["project"]), 1)
                self.assertEqual(len(data["merged"]), 1)

                # Local override should win
                self.assertEqual(data["merged"][0]["model"], "gpt-4o-mini")
                self.assertEqual(data["merged"][0]["system_prompt"], "updated local system")

            finally:
                backend.server.GLOBAL_AGENTS_PATH = orig_path

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

    def test_existing_project_context_suppresses_generic_discovery_question(self):
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

            self.assertEqual(orchestrator._deterministic_discovery_question(), "")

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

    def test_oversized_prompt_is_blocked_before_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.init("small product")
            agent = StatefulFake(
                AgentConfig(name="reviewer", kind="openai", extra={"max_input_tokens_per_turn": 100}),
            )
            orchestrator = Orchestrator([agent], workspace, max_tokens=1000, require_approval=False)
            orchestrator._running = True

            with self.assertRaisesRegex(RuntimeError, "Preflight blocked"):
                asyncio.run(orchestrator._send_agent(agent, "x" * 4000))

            self.assertEqual(agent.received, [])

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

    def test_global_agent_inheritance_exclusive(self):
        from fastapi.testclient import TestClient
        import backend.server
        client = TestClient(backend.server.app)
        res = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(res.status_code, 200)

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            global_agents_file = td_path / "global_agents.json"
            global_agents = [{
                "id": "global_1",
                "name": "GlobalAgent",
                "kind": "openai",
                "role": "GlobalRole",
                "model": "gpt-4",
                "api_key": backend.crypto.encrypt_key("global_secret"),
                "base_url": "",
                "cli_command": "",
                "system_prompt": "global system",
                "max_history_turns": 20,
                "extra": {}
            }]
            global_agents_file.write_text(json.dumps(global_agents))

            orig_path = backend.server.GLOBAL_AGENTS_PATH
            backend.server.GLOBAL_AGENTS_PATH = global_agents_file

            try:
                # Open empty project
                res = client.post("/project/open", json={"path": str(td_path / "project")})
                self.assertEqual(res.status_code, 200)

                # Fetch agents. Should inherit global agent
                res = client.get("/agents")
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertEqual(len(data["project"]), 0)
                self.assertEqual(len(data["merged"]), 1)
                self.assertEqual(data["merged"][0]["name"], "GlobalAgent")

                # Create a local agent
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

                # Fetch agents again. Local agents exist, so global agent should NOT be inherited!
                res = client.get("/agents")
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertEqual(len(data["project"]), 1)
                self.assertEqual(len(data["merged"]), 1)
                self.assertEqual(data["merged"][0]["name"], "LocalAgent")

            finally:
                backend.server.GLOBAL_AGENTS_PATH = orig_path


class FrontendPrivacyTests(unittest.TestCase):
    def test_project_picker_does_not_ship_a_personal_path(self):
        html = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()
        self.assertNotIn("/Users/", html)
        self.assertNotIn("/home/", html)
        self.assertIn('placeholder="/path/to/your/project"', html)


class DeterministicRoutingTests(unittest.TestCase):
    def test_simple_commands_use_high_confidence_fuzzy_routing(self):
        self.assertEqual(Orchestrator._fuzzy_intent("show staus"), "status")
        self.assertEqual(Orchestrator._fuzzy_intent("list agents"), "agents")
        self.assertEqual(Orchestrator._fuzzy_intent("help"), "help")
        self.assertEqual(Orchestrator._fuzzy_intent("design a secure payment architecture"), "")

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
