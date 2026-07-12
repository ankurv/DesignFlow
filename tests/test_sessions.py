import asyncio
import os
os.environ["AGENTFLOW_TEST"] = "1"
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
            raise RuntimeError("429 usage limit reached; retry after 1 second")
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

    def tearDown(self):
        for state in self._backend_server.app_states.values():
            if getattr(state, "orchestrator", None):
                state.orchestrator.stop()
            if getattr(state, "store", None):
                state.store.close()
        self._backend_auth.USERS_PATH = self._orig_users_path
        self._backend_auth.auth_manager = self._orig_auth_manager
        self._backend_server.auth_manager = self._orig_server_auth_manager
        self._backend_server.app_states.clear()
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
        self.assertEqual(log_path.parent.parent, Path(project).resolve() / ".agentflow" / "sessions")
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
            self.assertNotIn(".agentflow/DESIGN.md", workspace.read_src())
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
        boss_response_1 = f"""## NEXT_AGENT
worker

## INSTRUCTIONS
write main script

## VERDICT
CONTINUE

## PLAN_UPDATE
{VALID_PLAN}

## DESIGN_UPDATE
{VALID_DESIGN}

## DECISIONS_UPDATE
{VALID_DECISIONS}
"""
        boss_response_2 = """## NEXT_AGENT
worker

## INSTRUCTIONS
finish task

## VERDICT
COMPLETE

## QUALITY_GATE
PASS
"""
        worker_response = f"""## FILE: src/main.py
print('hello world')
## PLAN_UPDATE
{VALID_PLAN}
"""
        boss = StatefulFake(
            AgentConfig(name="boss", kind="openai", model="gpt-4o", extra={"is_coordinator": True}),
            replies=[boss_response_1, boss_response_2]
        )
        worker = StatefulFake(
            AgentConfig(name="worker", kind="openai", model="gpt-4o"),
            replies=[worker_response]
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
            self.assertIn("write main script", worker.received[0][0]["content"])
            self.assertEqual(orchestrator.ws.read_src()["src/main.py"], "print('hello world')")
            self.assertIn("Requirements", orchestrator.ws.read("plan"))
            self.assertIn("Initial architecture", orchestrator.ws.read("design"))

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

    def test_coordinator_pause_for_input(self):
        boss = StatefulFake(
            AgentConfig(name="boss", kind="openai", model="gpt-4o", extra={"is_coordinator": True}),
            replies=[
                "## NEXT_AGENT\nUSER\n## INSTRUCTIONS\nWhich db?\n## DECISION_CHECKPOINT\nsqlite or pg\n## VERDICT\nPAUSE_FOR_INPUT",
                f"## NEXT_AGENT\nboss\n## INSTRUCTIONS\nFinished db question\n## QUALITY_GATE\nPASS\n## PLAN_UPDATE\n{VALID_PLAN}\n## DESIGN_UPDATE\n{VALID_DESIGN}\n## DECISIONS_UPDATE\n{VALID_DECISIONS}\n## VERDICT\nCOMPLETE"
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(
                agents=[boss],
                workspace=Workspace(directory),
                require_approval=False,
                mode="all",
                max_debate_rounds=1,
            )
            
            async def run_test():
                run_task = asyncio.create_task(orchestrator.run("discuss product design"))
                await asyncio.sleep(0.05)
                self.assertTrue(orchestrator._paused)
                
                # Steer the coordinator with user response
                await orchestrator.steer("We want SQLite")
                orchestrator.resume()
                
                # Wait for task to finish
                await run_task
                
                # Check that step 2 received the steering response!
                self.assertIn("We want SQLite", boss.received[-1][-1]["content"])
            
            asyncio.run(run_test())

    def test_quality_gate_validation_retries_until_plan_is_complete(self):
        boss = StatefulFake(
            AgentConfig(name="boss", kind="openai", model="gpt-4o", extra={"is_coordinator": True}),
            replies=[
                "## NEXT_AGENT\nboss\n## INSTRUCTIONS\nfinalize now\n## QUALITY_GATE\nPASS\n## VERDICT\nCOMPLETE",
                f"## NEXT_AGENT\nboss\n## INSTRUCTIONS\nfinalize after fixing validation feedback\n## QUALITY_GATE\nPASS\n## PLAN_UPDATE\n{VALID_PLAN}\n## DESIGN_UPDATE\n{VALID_DESIGN}\n## DECISIONS_UPDATE\n{VALID_DECISIONS}\n## VERDICT\nCOMPLETE",
            ]
        )
        events = []

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(
                agents=[boss],
                workspace=Workspace(directory),
                require_approval=False,
                mode="debate",
                event_cb=events.append,
            )

            asyncio.run(orchestrator.run("debate product design"))

            self.assertEqual(len(boss.received), 2)
            self.assertTrue(any(
                event.kind.value == "phase" and event.data.get("status") == "quality_gate_failed"
                for event in events
            ))
            self.assertIn("Requirements", orchestrator.ws.read("plan"))
            self.assertIn("```mermaid", orchestrator.ws.read("design"))

    def test_completion_requires_specialist_coverage_and_user_checkpoint(self):
        agents = [
            StatefulFake(AgentConfig(name="boss", kind="openai", model="gpt-4o", extra={"is_coordinator": True})),
            StatefulFake(AgentConfig(name="architect", kind="openai", model="gpt-4o")),
            StatefulFake(AgentConfig(name="security", kind="openai", model="gpt-4o")),
            StatefulFake(AgentConfig(name="product", kind="openai", model="gpt-4o")),
        ]
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(
                agents=agents,
                workspace=Workspace(directory),
                require_approval=True,
            )
            orchestrator._coordinator_name = "boss"
            orchestrator.ws.ensure()
            orchestrator.ws.write("plan", f"# Plan\n\n{VALID_PLAN}")
            orchestrator.ws.write("design", f"# Architecture Design\n\n{VALID_DESIGN}")
            orchestrator.ws.write("decisions", f"# Key Decisions\n\n{VALID_DECISIONS}")

            errors = orchestrator._coordinator_completion_errors("PASS")
            self.assertTrue(any("3 more distinct" in error for error in errors))
            self.assertTrue(any("material user decision" in error for error in errors))

            orchestrator._consulted_specialists.update({"architect", "security", "product"})
            orchestrator._user_checkpoint_count = 1
            self.assertEqual(orchestrator._coordinator_completion_errors("PASS"), [])

    def test_run_token_budget_stops_after_budget_is_hit(self):
        debate = """## DESIGN_APPEND
design
## PLAN_UPDATE
- [ ] build
## CONSENSUS_APPEND
not ready yet
VOTE: DISAGREE
"""
        agent = StatefulFake(
            AgentConfig(name="solo", kind="openai", model="gpt-4o"),
            replies=[debate, debate],
        )
        events = []

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(
                agents=[agent],
                workspace=Workspace(directory),
                max_debate_rounds=2,
                max_tokens=120,
                require_approval=False,
                event_cb=events.append,
            )

            async def test_run():
                task = asyncio.create_task(orchestrator.run("debate product design"))
                
                # Wait for budget exhaustion event
                for _ in range(100):
                    if any(e.kind.value == "phase" and e.data.get("status") == "budget_exhausted" for e in events):
                        break
                    await asyncio.sleep(0.01)

                self.assertTrue(any(
                    event.kind.value == "phase" and event.data.get("status") == "budget_exhausted"
                    for event in events
                ))
                self.assertEqual(len(agent.received), 1)
                self.assertEqual(orchestrator.run_token_total, 120)
                
                # Stop the orchestrator so the task can exit
                orchestrator.stop()
                await task
                
            asyncio.run(test_run())

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

    def test_workspace_changed_context_can_send_src_index_without_file_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.init("idea")
            workspace.write_src("src/api/app.py", "print('ok')")

            context = workspace.changed_context("researcher", ["design", "plan", "src_index"])
            self.assertIn("src/api/app.py", context)
            self.assertNotIn("print('ok')", context)

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
            metadata = Path(directory) / ".agentflow"
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
