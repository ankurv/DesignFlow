"""Server-wide MCP bearer-token lifecycle with one-time secret disclosure."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path


def _default_path() -> Path:
    if os.getenv("DESIGNFLOW_TEST") == "1":
        return Path("/tmp") / f"designflow-tests-{os.getpid()}" / "mcp_access.json"
    return Path.home() / ".designflow" / "mcp_access.json"


class MCPAccessTokenStore:
    def __init__(self, path: Path | None = None):
        self.path = path or _default_path()
        self._lock = threading.RLock()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text())
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def status(self) -> dict:
        with self._lock:
            data = self._read()
        return {
            "configured": bool(data.get("token_hash")),
            "created_at": str(data.get("created_at", "")),
            "environment_token_configured": bool(os.getenv("DESIGNFLOW_MCP_TOKEN", "").strip()),
        }

    def generate(self) -> dict:
        token = "dfmcp_" + secrets.token_urlsafe(32)
        created_at = datetime.now(timezone.utc).isoformat()
        payload = {"token_hash": self._digest(token), "created_at": created_at}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2) + "\n")
            temporary.chmod(0o600)
            temporary.replace(self.path)
            self.path.chmod(0o600)
        return {"token": token, "created_at": created_at}

    def revoke(self) -> bool:
        with self._lock:
            existed = bool(self._read().get("token_hash"))
            if self.path.exists():
                self.path.unlink()
        return existed

    def verify(self, token: str) -> bool:
        candidate = (token or "").strip()
        if not candidate:
            return False
        environment_token = os.getenv("DESIGNFLOW_MCP_TOKEN", "").strip()
        if environment_token and hmac.compare_digest(candidate, environment_token):
            return True
        with self._lock:
            stored_hash = str(self._read().get("token_hash", ""))
        return bool(stored_hash) and hmac.compare_digest(self._digest(candidate), stored_hash)


mcp_access_tokens = MCPAccessTokenStore()
