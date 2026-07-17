import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["DESIGNFLOW_TEST"] = "1"

from fastapi.testclient import TestClient

from backend.server import app
from backend.storage import ProjectStore
from backend.workspace.workspace import Workspace


MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def mcp_request(client: TestClient, method: str, params: dict, request_id: int = 1, headers=None):
    return client.post(
        "/mcp/",
        headers=headers or MCP_HEADERS,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )


class DesignFlowMCPProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def test_initialize_and_tools_list_use_streamable_http_protocol(self):
        initialized = mcp_request(self.client, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "designflow-tests", "version": "1"},
        })
        self.assertEqual(initialized.status_code, 200)
        self.assertEqual(initialized.json()["result"]["serverInfo"]["name"], "DesignFlow")

        listed = mcp_request(self.client, "tools/list", {}, request_id=2)
        self.assertEqual(listed.status_code, 200)
        names = {tool["name"] for tool in listed.json()["result"]["tools"]}
        self.assertTrue({
            "get_project_status", "read_artifact", "get_implementation_context",
            "validate_project", "get_recent_activity", "record_implementation_report",
            "list_implementation_reports",
        }.issubset(names))

    def test_tool_calls_read_context_and_persist_implementation_report(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.write("design", "# Design\n\n## Broker adapters\nUse a stable adapter interface for brokers.\n")
            workspace.write("plan", "# Plan\n\n## Adapter work\nImplement broker conformance tests.\n")
            workspace.write("decisions", "# Decisions\n\n## D-001 Broker boundary\nAdapters isolate vendor APIs.\n")

            context = mcp_request(self.client, "tools/call", {
                "name": "get_implementation_context",
                "arguments": {"project_path": directory, "task": "implement broker adapter"},
            })
            self.assertEqual(context.status_code, 200)
            payload = context.json()["result"]["structuredContent"]
            self.assertIn("Broker adapters", json.dumps(payload))

            recorded = mcp_request(self.client, "tools/call", {
                "name": "record_implementation_report",
                "arguments": {
                    "project_path": directory,
                    "kind": "evidence",
                    "task": "implement broker adapter",
                    "summary": "Adapter conformance tests pass.",
                    "code_references": ["tests/test_brokers.py"],
                },
            }, request_id=2)
            self.assertEqual(recorded.status_code, 200)
            self.assertFalse(recorded.json()["result"]["isError"])

            store = ProjectStore(workspace.root)
            try:
                reports = store.implementation_reports()
                self.assertEqual(reports[0]["summary"], "Adapter conformance tests pass.")
                self.assertEqual(reports[0]["code_references"], ["tests/test_brokers.py"])
            finally:
                store.close()

    def test_configured_token_is_required_by_mcp_transport(self):
        with patch.dict(os.environ, {"DESIGNFLOW_MCP_TOKEN": "test-secret"}):
            denied = mcp_request(self.client, "tools/list", {})
            self.assertEqual(denied.status_code, 401)
            authorized_headers = {**MCP_HEADERS, "Authorization": "Bearer test-secret"}
            allowed = mcp_request(self.client, "tools/list", {}, headers=authorized_headers)
            self.assertEqual(allowed.status_code, 200)


if __name__ == "__main__":
    unittest.main()
