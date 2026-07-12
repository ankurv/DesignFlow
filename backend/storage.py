"""Small per-project SQLite store for reusable DesignFlow state."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .crypto import encrypt_key, decrypt_key


class ProjectStore:
    def __init__(self, metadata_dir: Path):
        metadata_dir.mkdir(parents=True, exist_ok=True)
        self.path = metadata_dir / "agentflow.db"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self._lock, self._db:
            self._db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    sort_order INTEGER NOT NULL,
                    config_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    idea TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    timestamp TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
                CREATE TABLE IF NOT EXISTS turns (
                    run_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    round_number INTEGER,
                    iteration INTEGER,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    response_preview TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (run_id, turn_id)
                );
                CREATE INDEX IF NOT EXISTS idx_turns_run ON turns(run_id, turn_id);
                CREATE TABLE IF NOT EXISTS mcp_servers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    command TEXT NOT NULL,
                    args_json TEXT NOT NULL DEFAULT '[]',
                    env_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(runs)").fetchall()}
            if "cached_input_tokens" not in columns:
                self._db.execute("ALTER TABLE runs ADD COLUMN cached_input_tokens INTEGER NOT NULL DEFAULT 0")
            if "pricing_complete" not in columns:
                self._db.execute("ALTER TABLE runs ADD COLUMN pricing_complete INTEGER NOT NULL DEFAULT 1")

    def load_agents(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, config_json FROM agents ORDER BY sort_order"
            ).fetchall()
        configs = []
        for row in rows:
            config = json.loads(row["config_json"])
            config["id"] = row["id"]
            config["api_key"] = decrypt_key(config.get("api_key", ""))
            configs.append(config)
        return configs

    def save_agents(self, configs: list[dict]):
        with self._lock, self._db:
            self._db.execute("DELETE FROM agents")
            for index, original in enumerate(configs):
                config = dict(original)
                agent_id = config.pop("id")
                # Encrypt API credentials into a project-local database.
                config["api_key"] = encrypt_key(config.get("api_key", ""))
                self._db.execute(
                    "INSERT INTO agents(id, sort_order, config_json) VALUES (?, ?, ?)",
                    (agent_id, index, json.dumps(config)),
                )

    def start_run(self, run_id: str, idea: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO runs(run_id, idea, status, started_at) VALUES (?, ?, ?, ?)",
                (run_id, idea, "running", now),
            )

    def finish_run(self, run_id: str, status: str, agents: list[dict]):
        now = datetime.now(timezone.utc).isoformat()
        tokens = sum(int(agent.get("total_tokens", 0) or 0) for agent in agents)
        cached = sum(int(agent.get("cached_input_tokens", 0) or 0) for agent in agents)
        cost = sum(float(agent.get("cost_usd", 0) or 0) for agent in agents)
        pricing_complete = int(all(agent.get("pricing_known", False) for agent in agents))
        with self._lock, self._db:
            self._db.execute(
                """UPDATE runs
                   SET status=?, completed_at=?, total_tokens=?, cached_input_tokens=?,
                       estimated_cost_usd=?, pricing_complete=?
                   WHERE run_id=?""",
                (status, now, tokens, cached, cost, pricing_complete, run_id),
            )

    def update_run_metrics(self, run_id: str, agents: list[dict]):
        tokens = sum(int(agent.get("total_tokens", 0) or 0) for agent in agents)
        cached = sum(int(agent.get("cached_input_tokens", 0) or 0) for agent in agents)
        cost = sum(float(agent.get("cost_usd", 0) or 0) for agent in agents)
        pricing_complete = int(all(agent.get("pricing_known", False) for agent in agents))
        with self._lock, self._db:
            self._db.execute(
                """UPDATE runs
                   SET total_tokens=?, cached_input_tokens=?, estimated_cost_usd=?, pricing_complete=?
                   WHERE run_id=?""",
                (tokens, cached, cost, pricing_complete, run_id),
            )

    def project_usage(self) -> dict:
        """Return durable cumulative usage for every run in this project."""
        with self._lock:
            row = self._db.execute(
                """SELECT COALESCE(SUM(total_tokens), 0) AS total_tokens,
                          COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                          COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                          COALESCE(MIN(pricing_complete), 1) AS pricing_complete,
                          COUNT(*) AS run_count
                   FROM runs"""
            ).fetchone()
        return {
            "total_tokens": int(row["total_tokens"] or 0),
            "cached_input_tokens": int(row["cached_input_tokens"] or 0),
            "estimated_cost_usd": float(row["estimated_cost_usd"] or 0),
            "pricing_complete": bool(row["pricing_complete"]),
            "run_count": int(row["run_count"] or 0),
        }

    def append_event(self, run_id: str | None, event: dict):
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO events(run_id, timestamp, kind, agent, data_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    run_id,
                    event.get("timestamp", ""),
                    event.get("kind", ""),
                    event.get("agent", ""),
                    json.dumps(event.get("data", {})),
                ),
            )
            self._record_turn(run_id, event)

    def _record_turn(self, run_id: str | None, event: dict):
        data = event.get("data", {})
        turn_id = data.get("turn_id")
        if not run_id or not turn_id:
            return
        kind = event.get("kind", "")
        timestamp = event.get("timestamp", "")
        if kind == "turn_start":
            self._db.execute(
                """INSERT INTO turns(
                       run_id, turn_id, agent, phase, role, round_number, iteration,
                       status, attempt, started_at, completed_at, error
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, NULL, '')
                   ON CONFLICT(run_id, turn_id) DO UPDATE SET
                       agent=excluded.agent, phase=excluded.phase, role=excluded.role,
                       round_number=excluded.round_number, iteration=excluded.iteration,
                       status='running', attempt=excluded.attempt, completed_at=NULL, error=''""",
                (
                    run_id, turn_id, event.get("agent", ""), data.get("phase", ""),
                    data.get("role", ""), data.get("round"), data.get("iteration"),
                    int(data.get("attempt", 1) or 1), timestamp,
                ),
            )
        elif kind == "retry":
            self._db.execute(
                "UPDATE turns SET status='waiting', attempt=? WHERE run_id=? AND turn_id=?",
                (int(data.get("attempt", 1) or 1), run_id, turn_id),
            )
        elif kind == "error" and data.get("recoverable"):
            self._db.execute(
                """UPDATE turns SET status='failed', attempt=?, error=?
                   WHERE run_id=? AND turn_id=?""",
                (int(data.get("attempt", 1) or 1), data.get("error", ""), run_id, turn_id),
            )
        elif kind == "turn_end":
            self._db.execute(
                """UPDATE turns SET status='completed', attempt=?, completed_at=?,
                       error='', usage_json=?, response_preview=?
                   WHERE run_id=? AND turn_id=?""",
                (
                    int(data.get("attempt", 1) or 1), timestamp,
                    json.dumps(data.get("usage", {})), data.get("response", "")[:500],
                    run_id, turn_id,
                ),
            )

    def run_turns(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """SELECT turn_id, agent, phase, role, round_number, iteration,
                          status, attempt, started_at, completed_at, error,
                          usage_json, response_preview
                   FROM turns WHERE run_id=? ORDER BY turn_id""",
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["usage"] = json.loads(item.pop("usage_json"))
            result.append(item)
        return result

    def recent_runs(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """SELECT run_id, idea, status, started_at, completed_at,
                          total_tokens, cached_input_tokens, estimated_cost_usd, pricing_complete
                   FROM runs ORDER BY started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_mcp_servers(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM mcp_servers ORDER BY name").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["args"] = json.loads(item.pop("args_json"))
            item["env"] = json.loads(item.pop("env_json"))
            result.append(item)
        return result

    def add_mcp_server(self, server_id: str, name: str, command: str, args: list[str], env: dict):
        with self._lock, self._db:
            self._db.execute(
                """INSERT OR REPLACE INTO mcp_servers (id, name, command, args_json, env_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (server_id, name, command, json.dumps(args), json.dumps(env))
            )

    def delete_mcp_server(self, server_id: str):
        with self._lock, self._db:
            self._db.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))

    def close(self):
        with self._lock:
            self._db.close()
