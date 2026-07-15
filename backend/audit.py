"""Server-wide append-only audit trail for security and state-changing actions."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SENSITIVE_KEYS = ("password", "secret", "token", "api_key", "authorization", "credential", "cookie")


class AuditLog:
    def __init__(self, path: Path | None = None, max_queue: int = 1000, retention_days: int = 90):
        test_path = Path("/tmp") / f"designflow-audit-test-{os.getpid()}.db"
        self.path = path or (test_path if os.environ.get("DESIGNFLOW_TEST") == "1" else Path.home() / ".designflow" / "audit.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._queue: queue.Queue[dict | None] = queue.Queue(maxsize=max_queue)
        self._dropped = 0
        self._closed = False
        self._initialize()
        self._thread = threading.Thread(target=self._worker, name="designflow-audit-writer", daemon=True)
        self._thread.start()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        connection = self._connect()
        try:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    session_hash TEXT NOT NULL,
                    username TEXT NOT NULL,
                    role TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    result TEXT NOT NULL,
                    source_ip TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_events(username, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action, timestamp DESC);
            """)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).isoformat()
            connection.execute("DELETE FROM audit_events WHERE timestamp < ?", (cutoff,))
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def session_hash(session_id: str) -> str:
        return hashlib.sha256((session_id or "").encode()).hexdigest()[:16] if session_id else ""

    @staticmethod
    def project_id(project_path: str) -> str:
        return hashlib.sha256((project_path or "").encode()).hexdigest()[:16] if project_path else ""

    @classmethod
    def sanitize(cls, value: Any, key: str = "") -> Any:
        if any(marker in key.lower() for marker in SENSITIVE_KEYS):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(k): cls.sanitize(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [cls.sanitize(item) for item in value[:30]]
        if isinstance(value, str):
            return value[:300] + ("…" if len(value) > 300 else "")
        return value

    def record(self, *, request_id: str = "", session_id: str = "", username: str = "",
               role: str = "", project_path: str = "", action: str, target: str = "",
               result: str, source_ip: str = "", metadata: dict | None = None) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id or str(uuid.uuid4()),
            "session_hash": self.session_hash(session_id),
            "username": username[:120], "role": role[:40],
            "project_id": self.project_id(project_path),
            "action": action[:120], "target": target[:300], "result": result[:40],
            "source_ip": source_ip[:80],
            "metadata_json": json.dumps(self.sanitize(metadata or {}), ensure_ascii=False),
        }
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._dropped += 1

    def _worker(self):
        connection = self._connect()
        try:
            while True:
                event = self._queue.get()
                if event is None:
                    self._queue.task_done()
                    break
                with connection:
                    connection.execute(
                        """INSERT INTO audit_events(timestamp, request_id, session_hash, username, role,
                           project_id, action, target, result, source_ip, metadata_json)
                           VALUES (:timestamp, :request_id, :session_hash, :username, :role,
                           :project_id, :action, :target, :result, :source_ip, :metadata_json)""",
                        event,
                    )
                self._queue.task_done()
        finally:
            connection.close()

    def query(self, *, username: str = "", action: str = "", result: str = "", limit: int = 100) -> list[dict]:
        self._queue.join()
        clauses, params = [], []
        for column, value in (("username", username), ("action", action), ("result", result)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        sql = "SELECT * FROM audit_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        connection = self._connect()
        try:
            rows = connection.execute(sql, params).fetchall()
        finally:
            connection.close()
        events = []
        for row in rows:
            event = dict(row)
            event["metadata"] = json.loads(event.pop("metadata_json"))
            events.append(event)
        return events

    @property
    def dropped(self) -> int:
        return self._dropped

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._thread.join(timeout=2)


audit_log = AuditLog()
atexit.register(audit_log.close)
