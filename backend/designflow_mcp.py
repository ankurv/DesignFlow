"""First-class MCP interface for DesignFlow projects and coding agents."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .storage import ProjectStore
from .workspace.workspace import Workspace


designflow_mcp = FastMCP(
    "DesignFlow",
    instructions=(
        "Use DesignFlow as the canonical architecture and decision context for implementation. "
        "Read scoped context before coding, validate artifacts before trusting completion, and "
        "record implementation mismatches instead of silently deviating from confirmed decisions."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            "localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*",
            "[::1]", "[::1]:*", "testserver", "testserver:*",
        ],
        allowed_origins=["http://localhost:*", "http://127.0.0.1:*"],
    ),
)
designflow_mcp_app = designflow_mcp.streamable_http_app()


def _project(project_path: str) -> tuple[Path, Workspace]:
    raw = (project_path or "").strip()
    if not raw:
        raise ValueError("project_path is required")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("project_path must be an existing directory")
    if not (root / "DESIGNFLOW.md").exists() and not (root / ".designflow").is_dir():
        raise ValueError("The directory is not an initialized DesignFlow project")
    return root, Workspace(str(root))


def _with_store(workspace: Workspace):
    return ProjectStore(workspace.root)


def _section_candidates(text: str, source: str) -> list[dict[str, str]]:
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text or ""))
    if not matches:
        return [{"source": source, "heading": source, "content": text.strip()}] if text.strip() else []
    result = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result.append({
            "source": source,
            "heading": match.group(1).strip(),
            "content": text[match.start():end].strip(),
        })
    return result


@designflow_mcp.resource(
    "designflow://capabilities",
    title="DesignFlow MCP capabilities",
    description="Stable overview of the project context and validation interface.",
    mime_type="application/json",
)
def capabilities() -> dict[str, Any]:
    return {
        "version": "1",
        "transport": "streamable-http",
        "canonical_artifacts": ["DESIGN.md", "PLAN.md", "DECISIONS.md", "DESIGNFLOW.md"],
        "principles": [
            "Artifacts and the SQLite decision ledger remain canonical.",
            "Implementation context is scoped to the requested task.",
            "Confirmed decisions cannot be silently changed by coding agents.",
        ],
    }


@designflow_mcp.tool(description="Return persisted run state, agents, checkpoints, and artifact validation for a project.")
def get_project_status(project_path: str) -> dict[str, Any]:
    root, workspace = _project(project_path)
    store = _with_store(workspace)
    try:
        runs = store.recent_runs(limit=1)
        latest = runs[0] if runs else None
        checkpoint = store.latest_current_checkpoint()
        agents = [{
            "id": item.get("id", ""), "name": item.get("name", ""),
            "kind": item.get("kind", ""), "model": item.get("model", ""),
            "paused": bool(item.get("is_paused", False)),
        } for item in store.load_agents()]
        validation = workspace.validate_planning_artifacts()
        return {
            "project_path": str(root), "latest_run": latest,
            "active_checkpoint": checkpoint or None, "agents": agents,
            "validation": {"passed": not validation, "errors": validation},
        }
    finally:
        store.close()


@designflow_mcp.tool(description="Read one canonical DesignFlow artifact without loading the whole workspace.")
def read_artifact(project_path: str, artifact: str) -> dict[str, Any]:
    _, workspace = _project(project_path)
    key_by_name = {
        "DESIGN.MD": "design", "PLAN.MD": "plan", "DECISIONS.MD": "decisions",
        "QUESTIONS.MD": "questions", "CONTEXT.MD": "context", "DESIGNFLOW.MD": "brief",
    }
    normalized = (artifact or "").strip().upper()
    if normalized not in key_by_name:
        raise ValueError("artifact must be DESIGN.md, PLAN.md, DECISIONS.md, QUESTIONS.md, CONTEXT.md, or DESIGNFLOW.md")
    content = workspace.brief_path.read_text(errors="replace") if normalized == "DESIGNFLOW.MD" else workspace.read(key_by_name[normalized])
    return {"artifact": normalized, "content": content}


@designflow_mcp.tool(description="Return only architecture, plan, and decision sections relevant to an implementation task.")
def get_implementation_context(project_path: str, task: str, max_chars: int = 12000) -> dict[str, Any]:
    _, workspace = _project(project_path)
    task_text = (task or "").strip()
    if not task_text:
        raise ValueError("task is required")
    limit = max(2000, min(int(max_chars), 30000))
    words = set(re.findall(r"[a-z0-9_]+", task_text.lower())) - {
        "the", "a", "an", "and", "or", "to", "for", "of", "in", "on", "with", "implement", "add", "fix",
    }
    candidates = []
    for key, filename in (("design", "DESIGN.md"), ("plan", "PLAN.md"), ("decisions", "DECISIONS.md")):
        for section in _section_candidates(workspace.read(key), filename):
            haystack = f"{section['heading']} {section['content']}".lower()
            score = sum(3 if word in section["heading"].lower() else 1 for word in words if word in haystack)
            section["score"] = score
            candidates.append(section)
    selected = sorted(candidates, key=lambda item: (-int(item["score"]), item["source"], item["heading"]))
    output, used = [], 0
    for item in selected:
        if item["score"] <= 0 and output:
            continue
        remaining = limit - used
        if remaining <= 0:
            break
        content = item["content"][:remaining]
        output.append({"source": item["source"], "heading": item["heading"], "content": content})
        used += len(content)
    validation = workspace.validate_planning_artifacts()
    return {
        "task": task_text, "sections": output, "chars": used,
        "validation_passed": not validation, "validation_errors": validation,
    }


@designflow_mcp.tool(description="Run the deterministic DesignFlow planning-artifact quality gate.")
def validate_project(project_path: str) -> dict[str, Any]:
    root, workspace = _project(project_path)
    errors = workspace.validate_planning_artifacts()
    return {"project_path": str(root), "passed": not errors, "errors": errors}


@designflow_mcp.tool(description="Return the latest meaningful persisted activity without replaying the full run.")
def get_recent_activity(project_path: str, limit: int = 8) -> dict[str, Any]:
    _, workspace = _project(project_path)
    store = _with_store(workspace)
    try:
        run_id = store.latest_run_id()
        return {
            "run_id": run_id,
            "events": store.recent_run_activity(run_id, limit=max(1, min(int(limit), 20))) if run_id else [],
        }
    finally:
        store.close()


@designflow_mcp.tool(description="Record implementation evidence, a design mismatch, or a question for later DesignFlow review.")
def record_implementation_report(
    project_path: str, kind: str, task: str, summary: str,
    actor: str = "coding-agent", code_references: list[str] | None = None,
) -> dict[str, Any]:
    _, workspace = _project(project_path)
    normalized = (kind or "").strip().lower()
    if normalized not in {"evidence", "mismatch", "question"}:
        raise ValueError("kind must be evidence, mismatch, or question")
    if not (task or "").strip() or not (summary or "").strip():
        raise ValueError("task and summary are required")
    store = _with_store(workspace)
    try:
        report = store.add_implementation_report(
            actor=(actor or "coding-agent").strip(), kind=normalized,
            task=task.strip(), summary=summary.strip(), code_references=code_references or [],
        )
        return {"recorded": True, "report": report}
    finally:
        store.close()


@designflow_mcp.tool(description="List implementation evidence and unresolved design mismatches reported by coding agents.")
def list_implementation_reports(project_path: str, status: str = "open", limit: int = 50) -> dict[str, Any]:
    _, workspace = _project(project_path)
    store = _with_store(workspace)
    try:
        return {"reports": store.implementation_reports(status=status, limit=limit)}
    finally:
        store.close()
