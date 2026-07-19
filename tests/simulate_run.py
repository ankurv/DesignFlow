"""Black-box DesignFlow journey through FastAPI plus MCP observation."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests


TERMINAL = {"completed", "failed", "stopped", "done", "error", "cancelled"}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8010")
    parser.add_argument("--project", default="/Users/avarsheny/PCM")
    parser.add_argument("--prompt", default="Continue refining the product design")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin")
    parser.add_argument("--mcp-token", default=os.getenv("DESIGNFLOW_MCP_TOKEN", ""))
    parser.add_argument("--mcp-token-stdin", action="store_true")
    parser.add_argument(
        "--rotate-mcp-token", action="store_true",
        help="Replace an existing one-time MCP token when its plaintext value is unavailable.",
    )
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument(
        "--refresh-models", action="store_true",
        help="Rediscover and persist each enabled provider's current model catalog before starting.",
    )
    parser.add_argument(
        "--verify-reload", action="store_true",
        help="Log in with a fresh session and verify the latest prompt/answer ordering.",
    )
    parser.add_argument(
        "--approve-checkpoints", action="store_true",
        help="Approve the recommended option at durable human checkpoints.",
    )
    return parser.parse_args()


def mcp_status(session: requests.Session, api_url: str, token: str, project: str) -> dict:
    response = session.post(
        f"{api_url}/mcp/",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_project_status", "arguments": {"project_path": project}},
        },
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    content = payload.get("result", {}).get("content", [])
    if not content:
        raise RuntimeError(f"MCP status response has no content: {payload}")
    return json.loads(content[0]["text"])


def approve_checkpoint(session: requests.Session, api_url: str) -> bool:
    response = session.get(f"{api_url}/run/checkpoint/current", timeout=10)
    response.raise_for_status()
    checkpoint = response.json().get("checkpoint")
    if not checkpoint:
        return False
    recommended = next(
        (option for option in checkpoint.get("options", []) if option.get("recommended")),
        (checkpoint.get("options") or [None])[0],
    )
    body = {"option_id": recommended["id"], "custom_answer": ""} if recommended else {
        "option_id": "", "custom_answer": "Approve the proposed direction and continue.",
    }
    answer = session.post(
        f"{api_url}/run/checkpoint/{checkpoint['id']}/answer", json=body, timeout=10,
    )
    answer.raise_for_status()
    return True


def verify_reload(config: argparse.Namespace, project: str) -> None:
    fresh = requests.Session()
    login = fresh.post(
        f"{config.api_url}/auth/login",
        json={"username": config.username, "password": config.password}, timeout=10,
    )
    login.raise_for_status()
    fresh.headers["X-DesignFlow-Session"] = login.json()["session_id"]
    opened = fresh.post(f"{config.api_url}/project/open", json={"path": project}, timeout=15)
    opened.raise_for_status()
    activity = fresh.get(f"{config.api_url}/run/recent-activity", timeout=10)
    activity.raise_for_status()
    events = activity.json().get("events", [])
    prompt_positions = [
        index for index, event in enumerate(events)
        if event.get("kind") == "user_prompt"
        and event.get("data", {}).get("message") == config.prompt
    ]
    answer_positions = [
        index for index, event in enumerate(events)
        if event.get("kind") == "turn_end" and event.get("data", {}).get("phase") == "answer"
    ]
    if not prompt_positions:
        raise RuntimeError("reload verification did not restore the latest user prompt")
    if answer_positions and prompt_positions[-1] > answer_positions[-1]:
        raise RuntimeError("reload verification restored the answer before its user prompt")
    print(f"reload: restored {len(events)} conversation event(s) in chronological order")


def run_simulation(config: argparse.Namespace) -> int:
    project = str(Path(config.project).resolve())
    session = requests.Session()
    login = session.post(
        f"{config.api_url}/auth/login",
        json={"username": config.username, "password": config.password}, timeout=10,
    )
    login.raise_for_status()
    session.headers["X-DesignFlow-Session"] = login.json()["session_id"]

    opened = session.post(f"{config.api_url}/project/open", json={"path": project}, timeout=15)
    opened.raise_for_status()
    if config.refresh_models:
        for agent in opened.json().get("agents", []):
            if agent.get("is_paused") or agent.get("kind") == "cli":
                continue
            refreshed = session.post(f"{config.api_url}/agents/models", json=agent, timeout=30)
            refreshed.raise_for_status()
            result = refreshed.json()
            if not result.get("ok"):
                print(f"model refresh failed for {agent.get('name', agent.get('id'))}: {result.get('error', 'unknown error')}")
                return 1
            print(f"models: {agent.get('name', agent.get('id'))} discovered {len(result.get('models', []))}")
    token_status = session.get(f"{config.api_url}/mcp/access-token", timeout=10)
    token_status.raise_for_status()
    token_info = token_status.json()
    token = (sys.stdin.readline().strip() if config.mcp_token_stdin else config.mcp_token.strip())
    if not token:
        if token_info.get("configured") and not config.rotate_mcp_token:
            print(
                "MCP token already exists but its plaintext is intentionally not retrievable. "
                "Pass --mcp-token/ DESIGNFLOW_MCP_TOKEN, or explicitly use --rotate-mcp-token."
            )
            return 3
        token_response = session.post(f"{config.api_url}/mcp/access-token", timeout=10)
        token_response.raise_for_status()
        token = token_response.json()["token"]  # Deliberately never printed.

    started_at = time.monotonic()
    started = session.post(
        f"{config.api_url}/run/start", json={"idea": config.prompt, "mode": "auto"}, timeout=30,
    )
    print(f"start: HTTP {started.status_code} in {time.monotonic() - started_at:.2f}s")
    if not started.ok:
        print(f"start error: {started.text[:500]}")
        return 1
    print(f"run: {started.json().get('run_id')} kind={started.json().get('run_kind', 'planning_workflow')}")

    deadline = time.monotonic() + config.timeout
    last_marker = None
    while time.monotonic() < deadline:
        status_payload = mcp_status(session, config.api_url, token, project)
        runtime_response = session.get(f"{config.api_url}/run/status", timeout=10)
        runtime_response.raise_for_status()
        runtime = runtime_response.json()
        latest = status_payload.get("latest_run", {})
        workflow = runtime.get("workflow") or status_payload.get("workflow", {}) or {}
        marker = (latest.get("status"), workflow.get("state"), workflow.get("state_version"))
        if marker != last_marker:
            print(f"status: run={marker[0]} workflow={marker[1]} version={marker[2]}")
            last_marker = marker
        if (workflow.get("state") == "WAITING_FOR_USER" or runtime.get("awaiting_input")) and config.approve_checkpoints:
            if approve_checkpoint(session, config.api_url):
                print("checkpoint: approved recommended option")
        if str(latest.get("status", "")).lower() in TERMINAL:
            succeeded = str(latest.get("status", "")).lower() in {"completed", "done"}
            if succeeded and config.verify_reload:
                verify_reload(config, project)
            return 0 if succeeded else 1
        time.sleep(config.poll_seconds)
    print(f"timeout: workflow did not finish within {config.timeout}s")
    return 2


if __name__ == "__main__":
    raise SystemExit(run_simulation(arguments()))
