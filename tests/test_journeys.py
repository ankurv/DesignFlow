import unittest
import json
import sqlite3
import os
import shutil
import tempfile
from pathlib import Path
from fastapi.testclient import TestClient
import backend.server
from backend.storage import ProjectStore

class StabilizationJourneyTests(unittest.TestCase):
    def setUp(self):
        self.workspace_dir = tempfile.mkdtemp()
        self.project_path = str(Path(self.workspace_dir) / "test_project")
        os.makedirs(self.project_path)
        
        self.client = TestClient(backend.server.app)
        
        # Authenticate
        resp = self.client.post("/auth/login", json={"username": "admin", "password": "admin"})
        self.assertEqual(resp.status_code, 200)
        
        # Reset any leftover state
        self.client.post("/run/stop")
        self.client.post("/run/reset")
        
    def tearDown(self):
        self.client.post("/run/stop")
        self.client.post("/run/reset")
        shutil.rmtree(self.workspace_dir)
        backend.server.app_states.pop(self.project_path, None)

    def test_new_project_completion_journey(self):
        # 1. Open project
        resp = self.client.post("/project/open", json={"path": self.project_path})
        self.assertEqual(resp.status_code, 200)
        
        # Verify invariant auditor doesn't throw errors
        status_resp = self.client.get("/run/status")
        self.assertEqual(status_resp.status_code, 200)
        self.assertEqual(status_resp.json()["status"], "idle")
        
        # Test invariant auditor via internal function
        canonical = str(Path(self.project_path).resolve())
        state = backend.server.app_states.get(canonical)
        errors = backend.server.runtime_invariant_errors(state)
        self.assertEqual(len(errors), 0, f"Invariant errors found: {errors}")
        
    def test_single_active_run_enforcement(self):
        self.client.post("/project/open", json={"path": self.project_path})
        canonical = str(Path(self.project_path).resolve())
        state = backend.server.app_states.get(canonical)
        
        # Fake a running state
        state.status = "running"
        state.run_id = "fake_run"
        # Should raise an error when starting a new run
        resp = self.client.post("/run/start", json={"idea": "test", "mode": "auto"})
        self.assertEqual(resp.status_code, 400)
        
        # Reconcile should clear it if no actual task
        state.status = "running"
        backend.server.reconcile_runtime_status(state)
        self.assertEqual(state.status, "idle")

    def test_system_recovery_action_isolation(self):
        self.client.post("/project/open", json={"path": self.project_path})
        canonical = str(Path(self.project_path).resolve())
        state = backend.server.app_states.get(canonical)
        state.run_id = "test_run_123"
        
        # Manually invoke enqueue_recovery_action
        action_id = state.store.enqueue_recovery_action(
            state.run_id, "provider_error", "gemini-pro", "turn_123", 
            retry_eligible=True, auto_failover_eligible=False, retry_time_known=""
        )
        self.assertIsNotNone(action_id)
        
        # Verify it went to system_recovery_actions and NOT decision_checkpoints
        recovery = state.store.active_recovery_action(state.run_id)
        self.assertIsNotNone(recovery)
        self.assertEqual(recovery["failure_category"], "provider_error")
        self.assertEqual(recovery["affected_provider"], "gemini-pro")
        
        checkpoints = state.store._db.execute("SELECT count(*) FROM decision_checkpoints").fetchone()[0]
        self.assertEqual(checkpoints, 0)
        
        # Resolve it
        state.store.resolve_recovery_action(action_id, "wait_and_retry")
        self.assertIsNone(state.store.active_recovery_action(state.run_id))

if __name__ == '__main__':
    unittest.main()
