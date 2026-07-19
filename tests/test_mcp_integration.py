import asyncio
import tempfile
import sys
import unittest
import os

from backend.agents.base import AgentBase, AgentConfig, Usage
from backend.orchestration import Orchestration
from backend.storage import ProjectStore
from backend.workspace.workspace import Workspace


class ToolBoundaryFake(AgentBase):
    manages_context = True

    def __init__(self, config):
        super().__init__(config)
        self.received_kwargs = []

    def _raw_send(self, messages, system, *args, **kwargs):
        self.received_kwargs.append(kwargs)
        if "discovery gate" in system:
            return (
                '{"adequate":true,"evidence_summary":"A bounded local planner is established.","blocking_questions":[]}',
                Usage(input_tokens=10, output_tokens=10),
            )
        if "reviewing a concrete architecture proposal" in system:
            return ('{"challenges":[],"validated_topics":[]}', Usage(input_tokens=10, output_tokens=10))
        if "coordinating architect" in system:
            return ('{"proposal":{"components":[],"decisions":[],"risks":[],"assumptions":[],"unknowns":[]},"dispositions":[]}', Usage(input_tokens=10, output_tokens=10))
        return (
            '{"components":[],"decisions":[],"risks":[],"assumptions":[],"unknowns":[]}',
            Usage(input_tokens=10, output_tokens=10),
        )

class MCPIntegrationTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(self.tmp.name)
        # Initialize workspace to copy json files
        self.ws.ensure()
        self.store = ProjectStore(Path(self.tmp.name) / "test.db")

        # Add the dummy MCP server to the store
        server_path = os.path.join(os.path.dirname(__file__), "dummy_mcp_server.py")
        self.store.add_mcp_server("dummy_mcp", "dummy_server", sys.executable, [server_path], {})

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_v2_planning_does_not_expose_mcp_tools_to_experts(self):
        agent = ToolBoundaryFake(AgentConfig(name="architect", role="Architect", kind="openai"))
        orchestrator = Orchestration(
            agents=[agent], workspace=self.ws, store=self.store, run_id="mcp-boundary",
        )
        task = asyncio.create_task(orchestrator.run("Design a local planner"))
        for _ in range(100):
            await asyncio.sleep(0)
            checkpoint = self.store.current_checkpoint("mcp-boundary")
            if checkpoint:
                option = checkpoint["options"][0]
                self.store.answer_checkpoint("mcp-boundary", checkpoint["id"], "test", option["id"], "")
                await orchestrator.accept_structured_checkpoint_answer(f"{option['label']} — {option['summary']}", False, "test")
                break
        await task
        # Discovery, opening proposal, and peer review. An approved proposal
        # with no material challenges is not regenerated as a revision.
        self.assertEqual(len(agent.received_kwargs), 3)
        for received in agent.received_kwargs:
            self.assertIn("mcp_tools", received)
            self.assertIsNone(received["mcp_tools"])
