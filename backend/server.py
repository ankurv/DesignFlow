"""FastAPI backend for project selection, orchestration, persistence, and SSE."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from backend.auth import auth_manager, Session
from pydantic import BaseModel
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Depends, Cookie, Response, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agents.base import AgentConfig, AgentStatus
from .agents.providers import AGENT_KINDS, create_agent, discover_models
from .events import Event, EventKind
from .orchestration import Orchestration
from .interaction import InteractionKind, InteractionService
from .workflow import WorkflowRepository, WorkflowState
from .storage import ProjectStore
from .workspace.workspace import Workspace
from .errors import DesignFlowError, PlanningQualityError, classify_provider_error
from .debug_observer import DebugObserver
from .designflow_mcp import designflow_mcp_app
from .mcp_access import mcp_access_tokens
from .prompt_catalog import prompt_catalog
from .audit import audit_log
from .version import __version__

logger = logging.getLogger(__name__)
SSE_SHUTDOWN = object()


@asynccontextmanager
async def lifespan(app: FastAPI):
    mcp_lifespan = designflow_mcp_app.router.lifespan_context(designflow_mcp_app)
    await mcp_lifespan.__aenter__()
    lease_task = asyncio.create_task(lease_cleanup_loop())
    try:
        yield
    finally:
        lease_task.cancel()
        await asyncio.gather(lease_task, return_exceptions=True)
        tasks = []
        all_states = list(app_states.values()) + list(unbound_states.values())
        for state in all_states:
            if state.orchestrator:
                state.orchestrator.stop()
            if state.run_task and not state.run_task.done():
                state.run_task.cancel()
                tasks.append(state.run_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for state in all_states:
            if state.store and state.run_id and state.status in {"running", "paused", "needs_attention"}:
                if state.orchestrator:
                    state.orchestrator.save_state()
                agents = [agent.state_dict() for agent in state.orchestrator.participants] if state.orchestrator else []
                state.store.finish_run(state.run_id, "stopped", agents)
                if state.workspace:
                    state.workspace.finish_logbook_run(state.run_id, "stopped", agents)
            state.status = "idle"
            state.awaiting_input = False
            state.close()
        app_states.clear()
        unbound_states.clear()
        session_projects.clear()
        session_last_seen.clear()
        await mcp_lifespan.__aexit__(None, None, None)


app = FastAPI(title="DesignFlow", version=__version__, lifespan=lifespan)
app.state.shutting_down = False
app.state.request_shutdown = None
app.state.debug_observer_enabled = False
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


def audit_action(method: str, path: str) -> str:
    if method == "GET" and path in {"/admin/runtime-diagnostics", "/admin/audit"}:
        return "admin." + ("runtime_diagnostics" if path.endswith("runtime-diagnostics") else "audit.read")
    if method not in {"POST", "PUT", "DELETE"} or path in {"/auth/login", "/session/heartbeat"}:
        return ""
    rules = (
        (r"^/auth/logout$", "auth.logout"), (r"^/users/password$", "user.password_change"),
        (r"^/users(?:/.*)?$", "user.manage"), (r"^/project/open$", "project.open"),
        (r"^/project/brief$", "project.brief_update"), (r"^/project/settings$", "project.settings_update"),
        (r"^/agents/test$", "agent.test"), (r"^/agents/models$", "agent.models_discover"),
        (r"^/agents(?:/.*)?$", "agent.configure"), (r"^/run/start$", "run.start"),
        (r"^/run/pause$", "run.pause"), (r"^/run/resume$", "run.resume"),
        (r"^/run/retry$", "run.retry"), (r"^/run/stop$", "run.stop"),
        (r"^/run/reset$", "run.reset"), (r"^/run/steer$", "run.steer"),
        (r"^/run/checkpoint(?:/.*)?$", "checkpoint.answer"),
        (r"^/mcp/(?:servers|access-token)(?:/.*)?$", "mcp.configure"),
        (r"^/mcp/?$", "mcp.invoke"),
        (r"^/workspace/file/.*$", "artifact.update"),
        (r"^/workspace/src/.*$", "source.update"), (r"^/admin/shutdown$", "admin.shutdown"),
    )
    return next((action for pattern, action in rules if re.match(pattern, path)), "")


@app.middleware("http")
async def audit_requests(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    action = audit_action(request.method, request.url.path)
    session_id = request.headers.get("X-DesignFlow-Session") or request.cookies.get("session_id") or ""
    session = auth_manager.get_session(session_id) if session_id else None
    project_path = session_projects.get(session_id, "")
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception:
        if action:
            audit_log.record(
                request_id=request_id, session_id=session_id,
                username=session.username if session else "", role=session.role if session else "",
                project_path=project_path, action=action, target=request.url.path,
                result="error", source_ip=request.client.host if request.client else "",
                metadata={"method": request.method},
            )
        raise
    response.headers["X-Request-ID"] = request_id
    if action:
        project_path = session_projects.get(session_id, project_path)
        audit_log.record(
            request_id=request_id, session_id=session_id,
            username=session.username if session else "", role=session.role if session else "",
            project_path=project_path, action=action, target=request.url.path,
            result="success" if status_code < 400 else "denied" if status_code in {401, 403} else "failed",
            source_ip=request.client.host if request.client else "",
            metadata={"method": request.method, "status_code": status_code},
        )
    return response


@app.get("/healthz")
def healthz():
    return {"ok": True, "status": "healthy", "version": __version__}


@app.get("/version")
def version():
    return {"version": __version__}
class AppState:
    def __init__(self):
        self.configs: list[dict] = []
        self.orchestrator: Optional[Orchestration] = None
        self.workspace: Optional[Workspace] = None
        self.store: Optional[ProjectStore] = None
        self.event_log: list[Event] = []
        self.next_event_id = 1
        self.sse_clients: list[asyncio.Queue] = []
        self.run_id: Optional[str] = None
        self.run_task: Optional[asyncio.Task] = None
        self.status = "idle"
        self.awaiting_input = False
        self.current_idea = ""
        self.last_transition = "initialized"
        self.debug_observer: Optional[DebugObserver] = None

    def open_project(self, path: str) -> Workspace:
        if self.status in {"running", "paused", "needs_attention"}:
            raise ValueError("Stop the active run before changing projects")
        workspace = Workspace(path)
        workspace.ensure()
        workspace.reconcile_interrupted_logbook_runs()
        if self.store:
            self.store.close()
        if self.debug_observer:
            self.debug_observer.close()
        self.workspace = workspace
        self.store = ProjectStore(workspace.root)
        self.store.reconcile_interrupted_runs()
        self.store.reject_malformed_checkpoints()
        current = self.store.latest_current_checkpoint()
        if current:
            workspace.write("questions", "# Decision Checkpoint\n\n" + checkpoint_projection(current))
        else:
            workspace.clear_questions()
        self.debug_observer = DebugObserver(workspace.root) if getattr(app.state, "debug_observer_enabled", False) else None
        self.configs = self.store.load_agents()
        self.event_log.clear()
        self.orchestrator = None
        self.run_id = None
        self.run_task = None
        self.status = "idle"
        self.awaiting_input = False
        self.current_idea = workspace.brief()
        self.last_transition = "project_opened"
        return workspace

    def persist_agents(self):
        if not self.workspace or not self.store:
            raise ValueError("Open a project first")
        self.store.save_agents(self.configs)

    def close(self):
        if self.orchestrator:
            self.orchestrator.stop()
        if self.store:
            self.store.close()
            self.store = None
        if self.debug_observer:
            self.debug_observer.close()
            self.debug_observer = None

    @property
    def merged_configs(self) -> list[dict]:
        return list(self.configs)


def close_sse_connections() -> int:
    """Ask every event stream to finish so the HTTP server can shut down cleanly."""
    closed = 0
    seen: set[int] = set()
    states = list(app_states.values()) + list(unbound_states.values())
    for state in states:
        if id(state) in seen:
            continue
        seen.add(id(state))
        for queue in list(state.sse_clients):
            try:
                queue.put_nowait(SSE_SHUTDOWN)
                closed += 1
            except asyncio.QueueFull:
                # A full stream is already unhealthy. Make room for the close
                # signal instead of waiting for its client to drain old events.
                try:
                    queue.get_nowait()
                    queue.put_nowait(SSE_SHUTDOWN)
                    closed += 1
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
    return closed


# Project runtimes are shared; browser sessions only select a project.
app_states: dict[str, AppState] = {}
session_projects: dict[str, str] = {}
session_last_seen: dict[str, float] = {}
unbound_states: dict[str, AppState] = {}
runtime_registry_lock = threading.RLock()

def get_session(request: Request) -> Session:
    session_id = (
        request.headers.get("X-DesignFlow-Session")
        or request.query_params.get("session_id")
        or request.cookies.get("session_id")
    )
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    session = auth_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid session")
    session_last_seen[session_id] = time.monotonic()
    return session

def get_state(session: Session = Depends(get_session)) -> AppState:
    project_path = session_projects.get(session.session_id)
    if project_path:
        with runtime_registry_lock:
            state = app_states.get(project_path)
            if state:
                return state
            session_projects.pop(session.session_id, None)
    return unbound_states.setdefault(session.session_id, AppState())


async def release_project_binding(session_id: str) -> None:
    session_last_seen.pop(session_id, None)
    with runtime_registry_lock:
        project_path = session_projects.pop(session_id, None)
        if not project_path or project_path in session_projects.values():
            return
        state = app_states.get(project_path)
        # Browser presence controls observation, not execution. Keep an active
        # project runtime alive when its last tab disappears.
        if state and state.status in {"running", "paused", "needs_attention"}:
            # Browser/MCP leases control observation only. An active workflow
            # owns its project runtime even if the client disappears or the
            # task reference is temporarily reconciling around a provider
            # thread. Explicit stop/reset or terminal completion releases it.
            return
        state = app_states.pop(project_path, None)
    if not state:
        return
    was_active = state.status in {"running", "paused", "needs_attention"}
    if state.orchestrator and was_active:
        state.orchestrator.stop()
    if state.run_task and not state.run_task.done():
        state.run_task.cancel()
        await asyncio.gather(state.run_task, return_exceptions=True)
    if state.store and state.run_id and was_active:
        if state.orchestrator:
            state.orchestrator.save_state()
        agents = [agent.state_dict() for agent in state.orchestrator.participants] if state.orchestrator else []
        state.store.finish_run(state.run_id, "stopped", agents)
        if state.workspace:
            state.workspace.finish_logbook_run(state.run_id, "stopped", agents)
    state.status = "idle"
    state.awaiting_input = False
    state.close()


def close_detached_terminal_runtime(state: AppState) -> None:
    """Release a background runtime after it finishes with no attached tabs."""
    if not state.workspace or state.status in {"running", "paused", "needs_attention"}:
        return
    project_path = state.workspace.path
    with runtime_registry_lock:
        if project_path in session_projects.values() or app_states.get(project_path) is not state:
            return
        app_states.pop(project_path, None)
    state.close()


async def bind_project(session: Session, path: str) -> AppState:
    canonical = str(Path(path).expanduser().resolve())
    current = session_projects.get(session.session_id)
    if current == canonical and canonical in app_states:
        return app_states[canonical]
    if current:
        await release_project_binding(session.session_id)
    with runtime_registry_lock:
        state = app_states.get(canonical)
        if state is None:
            state = AppState()
            state.open_project(canonical)
            app_states[canonical] = state
        session_projects[session.session_id] = canonical
        session_last_seen[session.session_id] = time.monotonic()
        detached = unbound_states.pop(session.session_id, None)
        if detached:
            detached.close()
        return state


async def expire_stale_bindings(now: Optional[float] = None, ttl_seconds: int = 75) -> list[str]:
    current = time.monotonic() if now is None else now
    stale = [
        session_id for session_id, project_path in list(session_projects.items())
        if project_path and current - session_last_seen.get(session_id, 0) > ttl_seconds
    ]
    for session_id in stale:
        await release_project_binding(session_id)
    return stale


async def lease_cleanup_loop():
    while True:
        await asyncio.sleep(15)
        await expire_stale_bindings()

class LoginBody(BaseModel):
    username: str
    password: str

@app.post("/auth/login")
def login(body: LoginBody, response: Response, request: Request):
    session = auth_manager.login(body.username, body.password)
    if not session:
        audit_log.record(
            action="auth.login", target=body.username, result="failed",
            username=body.username, source_ip=request.client.host if request.client else "",
            metadata={"reason": "invalid_credentials"},
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")
    response.set_cookie(key="session_id", value=session.session_id, httponly=True)
    audit_log.record(
        session_id=session.session_id, username=session.username, role=session.role,
        action="auth.login", target=session.username, result="success",
        source_ip=request.client.host if request.client else "",
    )
    return {"ok": True, "username": session.username, "role": session.role, "session_id": session.session_id}


# User Management Endpoints
@app.get("/users")
def get_users(session: Session = Depends(get_session)):
    if session.role != "admin":
        raise HTTPException(403, "Admins only")
    return {"users": auth_manager.list_users()}

class AddUserBody(BaseModel):
    username: str
    password: str
    role: str = "user"

@app.post("/users")
def add_user(body: AddUserBody, session: Session = Depends(get_session)):
    if session.role != "admin":
        raise HTTPException(403, "Admins only")
    # Force role to user
    success = auth_manager.add_user(body.username, body.password, "user")
    if not success:
        raise HTTPException(400, "User already exists")
    return {"ok": True}

@app.delete("/users/{username}")
def delete_user(username: str, session: Session = Depends(get_session)):
    if session.role != "admin":
        raise HTTPException(403, "Admins only")
    if username == "admin":
        raise HTTPException(400, "Cannot delete root admin")
    success = auth_manager.delete_user(username)
    if not success:
        raise HTTPException(404, "User not found")
    return {"ok": True}

class ChangePasswordBody(BaseModel):
    username: str
    new_password: str

@app.put("/users/password")
def change_password(body: ChangePasswordBody, session: Session = Depends(get_session)):
    if session.role != "admin" and session.username != body.username:
        raise HTTPException(403, "Not authorized to change this user's password")
    success = auth_manager.change_password(body.username, body.new_password)
    if not success:
        raise HTTPException(404, "User not found")
    return {"ok": True}

@app.get("/users/me")
def get_me(session: Session = Depends(get_session)):
    return {"username": session.username, "role": session.role}

@app.post("/auth/logout")
async def logout(response: Response, session: Session = Depends(get_session)):
    auth_manager.logout(session.session_id)
    await release_project_binding(session.session_id)
    detached = unbound_states.pop(session.session_id, None)
    if detached:
        detached.close()
    response.delete_cookie("session_id")
    return {"ok": True}


@app.post("/session/heartbeat")
def session_heartbeat(session: Session = Depends(get_session)):
    session_last_seen[session.session_id] = time.monotonic()
    return {"ok": True}






def broadcast(event: Event, state):
    data = event.to_dict()
    data["event_id"] = state.next_event_id
    state.next_event_id += 1
    if event.kind == EventKind.ERROR and event.data.get("recoverable"):
        state.status = "needs_attention"
        state.awaiting_input = False
    elif event.kind == EventKind.TURN_START and event.data.get("resumed"):
        state.status = "running"
        state.awaiting_input = False
    elif event.kind == EventKind.PHASE and event.data.get("status") in {"waiting_for_approval", "waiting_for_continuation", "budget_exhausted"}:
        state.status = "paused"
        state.awaiting_input = event.data.get("status") == "waiting_for_approval"
    elif event.kind == EventKind.PHASE and event.data.get("status") == "continuing_debate":
        state.status = "running"
        state.awaiting_input = False
    elif event.kind in {EventKind.DONE, EventKind.ERROR}:
        state.awaiting_input = False
    state.event_log.append(data)
    if state.debug_observer:
        state.debug_observer.observe({**data, "run_id": state.run_id})
    if state.store:
        state.store.append_event(state.run_id, data)
        if event.kind == EventKind.TURN_END:
            if state.workspace:
                state.workspace.append(
                    "logbook", event.data.get("response", ""), event.agent,
                    label=event.data.get("phase", "proposal"),
                )
            if state.orchestrator:
                state.store.update_run_metrics(
                    state.run_id,
                    [agent.state_dict() for agent in state.orchestrator.participants]
                )
    dead = []
    for queue in state.sse_clients:
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            dead.append(queue)
    for queue in dead:
        state.sse_clients.remove(queue)


@app.get("/events")
async def sse_stream(request: Request, state: AppState = Depends(get_state)):
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    state.sse_clients.append(queue)

    async def generator():
        try:
            # The live stream begins at subscription time. Persisted events are
            # loaded explicitly from Run History and never replayed on project open.
            # A browser reconnect may request only events missed after its last
            # live event, which preserves transient network reliability.
            try:
                last_event_id = int(request.headers.get("last-event-id", "0") or 0)
            except ValueError:
                last_event_id = 0
            if last_event_id > 0:
                for missed in state.event_log:
                    if int(missed.get("event_id", 0) or 0) > last_event_id:
                        yield f"id: {missed.get('event_id', '')}\ndata: {json.dumps(missed)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5)
                    if event is SSE_SHUTDOWN:
                        break
                    yield f"id: {event.get('event_id', '')}\ndata: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if queue in state.sse_clients:
                state.sse_clients.remove(queue)

    return StreamingResponse(
        generator(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ProjectOpenIn(BaseModel):
    path: str


class ProjectBriefIn(BaseModel):
    content: str


def project_payload(state) -> dict:
    reconcile_runtime_status(state)
    if not state.workspace:
        return {"open": False, "path": "", "brief": "", "recent_runs": []}
    return {
        "open": True,
        "path": state.workspace.path,
        "brief": state.workspace.brief(),
        "recent_runs": state.store.recent_runs() if state.store else [],
        "settings": state.workspace.settings(),
    }


def reconcile_runtime_status(state: AppState) -> str:
    """Repair stale active flags left after restart, cancellation, or task failure."""
    active = state.status in {"running", "paused", "needs_attention"}
    has_live_task = bool(state.run_task and not state.run_task.done())
    if active and (not state.orchestrator or not has_live_task or not state.run_id):
        state.status = "idle"
        state.awaiting_input = False
        state.run_task = None
        state.orchestrator = None
        state.run_id = None
        state.last_transition = "reconciled_stale_runtime_to_idle"
    return state.status


def runtime_invariant_errors(state: AppState) -> list[str]:
    errors = []
    active = state.status in {"running", "paused", "needs_attention"}
    live_task = bool(state.run_task and not state.run_task.done())
    if active and not state.run_id:
        errors.append("active runtime has no run id")
    if active and not state.orchestrator:
        errors.append("active runtime has no orchestrator")
    if active and not live_task:
        errors.append("active runtime has no live task")
    if state.awaiting_input and state.status != "paused":
        errors.append("awaiting input outside paused state")
    if state.status == "idle" and live_task:
        errors.append("idle runtime still has a live task")
        
    if state.store and state.run_id:
        # Check at most one active checkpoint
        active_checkpoints = state.store._db.execute(
            "SELECT count(*) FROM decision_checkpoints WHERE run_id=? AND status='active'",
            (state.run_id,)
        ).fetchone()[0]
        if active_checkpoints > 1:
            errors.append(f"run {state.run_id} has {active_checkpoints} active checkpoints (max 1)")
            
        if state.status == "paused" and state.awaiting_input and active_checkpoints != 1:
            errors.append("paused awaiting input but no active checkpoint exists")
            
        if state.status == "needs_attention":
            active_recoveries = state.store._db.execute(
                "SELECT count(*) FROM system_recovery_actions WHERE run_id=? AND resolved_at IS NULL",
                (state.run_id,)
            ).fetchone()[0]
            if active_recoveries != 1:
                errors.append(f"needs_attention but has {active_recoveries} active recovery actions")
                
        if state.status in {"done", "error"}:
            if active_checkpoints > 0:
                errors.append(f"completed/stopped run has {active_checkpoints} active checkpoints")
            
    return errors


def runtime_diagnostic(state: AppState, project_path: str = "") -> dict:
    task_state = "none"
    if state.run_task:
        task_state = "done" if state.run_task.done() else "live"
    failed = state.orchestrator.failed_turn if state.orchestrator else None
    return {
        "project_path": project_path or (state.workspace.path if state.workspace else ""),
        "status": state.status,
        "run_id": state.run_id,
        "task": task_state,
        "orchestrator": bool(state.orchestrator),
        "phase": state.orchestrator.phase.value if state.orchestrator else "",
        "awaiting_input": state.awaiting_input,
        "attached_sessions": sum(1 for path in session_projects.values() if path == project_path),
        "failed_agent": (failed or {}).get("agent", ""),
        "last_transition": state.last_transition,
        "invariant_errors": runtime_invariant_errors(state),
    }


def public_agent_config(config: dict) -> dict:
    """Return UI-safe agent metadata without disclosing stored credentials."""
    public = dict(config)
    has_api_key = bool(public.get("api_key"))
    public["api_key"] = "****" if has_api_key else ""
    public["has_api_key"] = has_api_key
    return public


def public_agent_configs(configs: list[dict]) -> list[dict]:
    return [public_agent_config(config) for config in configs]


@app.get("/project")
def get_project(state: AppState = Depends(get_state)):
    return project_payload(state)


@app.post("/project/open")
async def open_project(body: ProjectOpenIn, session: Session = Depends(get_session)):
    try:
        state = await bind_project(session, body.path)
    except (OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, **project_payload(state), "agents": public_agent_configs(state.configs)}


@app.put("/project/brief")
def save_project_brief(body: ProjectBriefIn, state: AppState = Depends(get_state)):
    if not state.workspace:
        raise HTTPException(400, "Open a project first")
    state.workspace.write_brief(body.content)
    return {"ok": True, "brief": state.workspace.brief()}

class ProjectSettingsIn(BaseModel):
    max_tokens: int

@app.put("/project/settings")
def save_project_settings(body: ProjectSettingsIn, state: AppState = Depends(get_state)):
    if not state.workspace:
        raise HTTPException(400, "Open a project first")
    state.workspace.save_settings({"max_tokens": body.max_tokens})
    return {"ok": True, "settings": state.workspace.settings()}


class AgentConfigIn(BaseModel):
    id: str = ""
    name: str
    kind: str
    role: str = ""
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    cli_command: str = ""
    system_prompt: str = ""
    max_history_turns: int = 20
    is_paused: bool = False
    extra: dict = Field(default_factory=dict)


def to_agent_config(config: dict, state: AppState = None) -> AgentConfig:
    return AgentConfig(
        id=config.get("id", ""), base_id=config.get("base_id", ""), name=config["name"], kind=config["kind"],
        role=config.get("role", ""), model=config.get("model", ""),
        api_key=config.get("api_key", ""),
        base_url=config.get("base_url", ""), cli_command=config.get("cli_command", ""),
        working_directory=state.workspace.path if state and state.workspace else "",
        system_prompt=config.get("system_prompt", ""),
        max_history_turns=config.get("max_history_turns", 20),
        extra=config.get("extra", {}),
    )


def model_pool_for_config(config: dict) -> list[str]:
    extra = dict(config.get("extra", {}) or {})
    discovered = [
        str(model).strip() for model in extra.get("available_models", [])
        if str(model).strip()
    ]
    configured = [str(config.get("model", "")).strip()] if config.get("model") else []
    return list(dict.fromkeys(configured + discovered))


def is_design_capable_model(model: str) -> bool:
    """Exclude specialist moderation/classification models from design roles."""
    normalized = (model or "").lower().replace("_", "-")
    return not any(marker in normalized for marker in (
        "content-safety", "moderation", "prompt-guard", "llama-guard",
    ))


def config_supports_design(config: dict) -> bool:
    pool = model_pool_for_config(config)
    return not pool or any(is_design_capable_model(model) for model in pool)


def model_for_virtual_agent(config: dict, role_index: int, provider_count: int) -> str:
    pool = model_pool_for_config(config)
    if not pool:
        return str(config.get("model", "") or "")
    capable_pool = [model for model in pool if is_design_capable_model(model)]
    if capable_pool:
        pool = capable_pool
    del provider_count
    return pool[role_index % len(pool)]


def live_agents_all_sessions(agent_id: str):
    found = []
    for s in app_states.values():
        if s.orchestrator and s.status in {"running", "paused", "needs_attention"}:
            for agent in s.orchestrator.agents:
                if agent.config.id == agent_id or agent.config.base_id == agent_id:
                    found.append((s, agent))
    return found


@app.get("/agents")
def list_agents(state: AppState = Depends(get_state)):
    return {
        "agents": public_agent_configs(state.configs),
        "kinds": list(AGENT_KINDS.keys())
    }


@app.get("/debug/insights")
def debug_insights(state: AppState = Depends(get_state)):
    if not getattr(app.state, "debug_observer_enabled", False):
        return {"enabled": False, "insights": []}
    if not state.workspace:
        return {"enabled": True, "insights": [], "message": "Open a project to collect diagnostics"}
    path = state.workspace.root / "debug" / "insights.json"
    if not path.exists():
        return {"enabled": True, "insights": [], "message": "No workflow events observed yet"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"enabled": True, "insights": [], "message": "Diagnostics are being updated"}
    return {"enabled": True, **payload}


@app.post("/agents")
def add_agent(body: AgentConfigIn, state: AppState = Depends(get_state)):
    if not state.store:
        raise HTTPException(400, "Open a project before adding an agent")
    if state.status in {"running", "paused", "needs_attention"}:
        raise HTTPException(400, "Stop the active run before adding an agent")
    config = body.model_dump()
    config["id"] = str(uuid.uuid4())[:8]
    state.configs.append(config)
    state.persist_agents()
    return {"ok": True, "agent": public_agent_config(config)}


def _reassign_agent_if_paused(s: AppState, active, agent_id: str):
    available = [c for c in s.merged_configs if not c.get("is_paused") and c["id"] != agent_id]
    if not available:
        raise HTTPException(400, "Cannot pause the only active agent. Please unpause another agent first.")
    # Prefer the least-used remaining provider so several affected specialists do
    # not all collapse onto the same fallback model.
    assignments = {config["id"]: 0 for config in available}
    for runtime_agent in s.orchestrator.agents:
        base_id = runtime_agent.config.base_id or runtime_agent.config.id
        if base_id in assignments and runtime_agent is not active:
            assignments[base_id] += 1
    new_base = min(available, key=lambda config: (assignments[config["id"]], config["id"]))
    expert = new_base.copy()
    expert["id"] = active.config.id
    expert["base_id"] = new_base.get("id", "")
    expert["name"] = active.name
    expert["role"] = active.config.role
    expert["system_prompt"] = active.config.system_prompt
    expert.setdefault("extra", {})["runtime_base_name"] = new_base.get("name", new_base.get("id", "provider"))

    new_agent = create_agent(to_agent_config(expert, s))
    active.transfer_runtime_state_to(new_agent)
    # The logical specialist keeps history and usage, but the replacement
    # provider must not inherit the depleted provider's error presentation.
    new_agent.status = AgentStatus.IDLE
    new_agent.error_message = ""
    new_agent.retry_at = ""
    new_agent.retry_reason = ""

    for i, a in enumerate(s.orchestrator.agents):
        if a is active:
            s.orchestrator.agents[i] = new_agent
            break
    return new_agent

@app.put("/agents/{agent_id}")
def update_agent(agent_id: str, body: AgentConfigIn, state: AppState = Depends(get_state)):
    for index, current in enumerate(state.configs):
        if current["id"] == agent_id:
            updated = body.model_dump()
            updated["id"] = agent_id

            if not updated.get("api_key") or updated.get("api_key") == "****":
                updated["api_key"] = current.get("api_key", "")

            if updated.get("is_paused") and not current.get("is_paused"):
                for s, active in live_agents_all_sessions(agent_id):
                    _reassign_agent_if_paused(s, active, agent_id)
            else:
                for s, active in live_agents_all_sessions(agent_id):
                    if updated["kind"] != current["kind"] or updated["name"] != current["name"]:
                        raise HTTPException(
                            400, "An active agent's name and kind cannot change; stop the run first"
                        )
                    try:
                        active.reconfigure(to_agent_config(updated, None))
                    except Exception as exc:
                        raise HTTPException(400, f"Agent configuration is invalid: {exc}") from exc
            state.configs[index] = updated
            state.persist_agents()
            return {"ok": True, "agent": public_agent_config(updated)}
    raise HTTPException(404, "Agent not found")


@app.delete("/agents/{agent_id}")
def delete_agent(agent_id: str, state: AppState = Depends(get_state)):
    if state.status in {"running", "paused", "needs_attention"}:
        raise HTTPException(400, "Stop the active run before removing an agent")
    state.configs = [config for config in state.configs if config["id"] != agent_id]
    state.persist_agents()
    return {"ok": True}





@app.post("/agents/test")
def test_agent_config(body: AgentConfigIn, state: AppState = Depends(get_state)):
    try:
        config = to_agent_config(body.model_dump(), state)
        agent = create_agent(config)
        agent.send("ping")
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, **classify_provider_error(exc).to_dict()}


@app.post("/agents/models")
def list_provider_models(body: AgentConfigIn, state: AppState = Depends(get_state)):
    try:
        submitted = body.model_dump()
        persisted = next((item for item in state.configs if item.get("id") == body.id), None) if body.id else None
        # Public agent payloads deliberately contain a masked credential. Model
        # refresh must resolve that marker server-side, never send it to a provider.
        if persisted and submitted.get("api_key") in {"", "****"}:
            submitted["api_key"] = persisted.get("api_key", "")
        config = to_agent_config(submitted, state)
        models = discover_models(config)
        if not models:
            raise ValueError("No compatible text-generation models were returned")
        if body.id and state.store:
            if persisted is not None:
                persisted.setdefault("extra", {})["available_models"] = models
                persisted["extra"]["model_catalog_kind"] = (
                    "bedrock_inference_profiles" if body.kind == "aws-bedrock" else "provider_models"
                )
                state.persist_agents()
        return {"ok": True, "models": models}
    except Exception as exc:
        return {"ok": False, "models": [], **classify_provider_error(exc).to_dict()}


class StartBody(BaseModel):
    idea: str = ""
    project_path: str = ""
    save_brief: bool = False
    max_debate_rounds: int = 6
    max_tokens: int = 100000
    max_build_iterations: int = 5
    mode: str = "auto"


@app.post("/run/start")
async def start_run(
    body: StartBody,
    state: AppState = Depends(get_state),
    session: Session = Depends(get_session),
):
    if app.state.shutting_down:
        raise HTTPException(503, "Server shutdown is in progress")
    reconcile_runtime_status(state)
    if state.status in {"running", "paused", "needs_attention"}:
        raise HTTPException(400, "A run is already in progress")
    if body.project_path:
        requested = str(Path(body.project_path).expanduser().resolve())
        if not state.workspace or state.workspace.path != requested:
            try:
                state = await bind_project(session, requested)
            except (OSError, ValueError) as exc:
                raise HTTPException(400, str(exc)) from exc
    if not state.workspace:
        raise HTTPException(400, "Open a project folder first")
    workflow_repository = WorkflowRepository(state.store) if state.store else None
    resumable_workflow = workflow_repository.latest_resumable() if workflow_repository else None
    brief = state.workspace.brief().strip()
    persisted_goal = state.workspace.product_goal().strip()
    saved_goal = workflow_repository.goal(resumable_workflow.run_id) if resumable_workflow and workflow_repository else ""
    entered_goal = body.idea.strip()
    if not entered_goal:
        raise HTTPException(400, "Enter a question or planning goal before starting a run")
    product_goal = saved_goal or persisted_goal or entered_goal
    resumes_saved_run = bool(
        saved_goal
        and saved_goal == product_goal
    )
    saved_run_id = resumable_workflow.run_id if resumable_workflow else ""
    task = body.idea.strip() if (persisted_goal or saved_goal) else ""
    effective_mode = body.mode
    if not state.merged_configs:
        raise HTTPException(400, "No agents configured")
    names = [config["name"].strip() for config in state.merged_configs]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise HTTPException(400, "Every agent needs a unique non-empty name")
    agents = []
    try:
        base_configs = [
            c for c in state.merged_configs
            if not c.get("is_paused") and config_supports_design(c)
        ]
        if not base_configs:
            raise HTTPException(400, "No available agents to spawn the team. Please unpause at least one agent.")
        missing_catalogs = [
            config.get("name", config.get("id", "agent")) for config in base_configs
            if config.get("kind") != "cli" and not model_pool_for_config(config)
        ]
        if missing_catalogs:
            raise HTTPException(
                400,
                "Choose a model or discover and save the model catalog for: " + ", ".join(missing_catalogs),
            )
        stale_bedrock_catalogs = [
            config.get("name", config.get("id", "agent")) for config in base_configs
            if config.get("kind") == "aws-bedrock"
            and config.get("extra", {}).get("model_catalog_kind") != "bedrock_inference_profiles"
        ]
        if stale_bedrock_catalogs:
            raise HTTPException(
                400,
                "Rediscover Bedrock models in Agents; the saved catalog contains non-invocable foundation IDs for: "
                + ", ".join(stale_bedrock_catalogs),
            )

        # 1. Spawn the Virtual Company, distributing roles across all provided base configs (Round-Robin)
        personas, _, _, _ = state.workspace.parse_personas()
        try:
            persona_catalog = {
                item.get("id"): item
                for item in json.loads(state.workspace.read("personas")).get("personas", [])
                if item.get("id")
            }
        except (TypeError, json.JSONDecodeError):
            persona_catalog = {}
        assigned_models: set[str] = set()
        for i, (role, system_prompt) in enumerate(personas.items()):
            base_config = base_configs[i % len(base_configs)]
            expert = base_config.copy()
            pool = model_pool_for_config(base_config)
            preferred = model_for_virtual_agent(base_config, i, len(base_configs))
            expert["model"] = next((model for model in pool if model not in assigned_models), preferred)
            if expert["model"]:
                assigned_models.add(expert["model"])
            expert["id"] = f"{base_config.get('id', 'base')}_{role}"
            expert["base_id"] = base_config.get("id", "")
            expert.setdefault("extra", {})["runtime_base_name"] = base_config.get("name", base_config.get("id", "provider"))
            expert["name"] = role
            expert["role"] = role
            expert["system_prompt"] = system_prompt
            profile = persona_catalog.get(role, {})
            expert.setdefault("extra", {}).update({
                "review_category": profile.get("category", ""),
                "review_signals": profile.get("signals", []),
                "design_focus": profile.get("design_focus", []),
                "plan_focus": profile.get("plan_focus", []),
            })
            agents.append(create_agent(to_agent_config(expert, state)))

        # 2. Also include any custom agents the user explicitly defined
        for config in state.merged_configs:
            explicitly_specialized = bool(config.get("role") and config.get("system_prompt"))
            if (explicitly_specialized and config["name"] not in personas and not config.get("is_paused")
                    and config_supports_design(config)):
                agents.append(create_agent(to_agent_config(config, state)))
    except Exception as exc:
        raise HTTPException(400, f"Could not initialize agent team: {exc}") from exc

    # Persist the user's request before routing or provider work. This is the
    # durable intake boundary: reload/restart must never erase an accepted prompt.
    intake_run_id = saved_run_id if resumes_saved_run and saved_run_id else str(uuid.uuid4())[:8]
    state.event_log.clear()
    state.run_id = intake_run_id
    state.current_idea = entered_goal
    state.status = "running"
    state.awaiting_input = False
    if not resumes_saved_run:
        state.store.reconcile_interrupted_runs()
        state.store.start_run(intake_run_id, entered_goal, run_kind="request_intake")

    workflow_context = resumable_workflow.model_dump(mode="json") if resumable_workflow else {}
    router_agent = next(
        (agent for agent in agents if "haiku" in (agent.config.model or "").lower()), agents[0],
    )
    interaction = InteractionService(router_agent, state.workspace, state.store, workflow_context)
    if body.save_brief or body.mode in {"debate", "all"}:
        interaction_kind = InteractionKind.PLANNING
        decision = None
    else:
        decision = await interaction.route(entered_goal)
        interaction_kind = decision.kind

    if interaction_kind == InteractionKind.ANSWER:
        state.orchestrator = None
        state.status = "running"
        state.store.update_run_contract(state.run_id, "chat")
        state.workspace.begin_logbook_run(state.run_id, entered_goal)
        turn_id = f"answer-{state.run_id}"
        broadcast(Event(EventKind.TURN_START, agent=router_agent.name, data={
            "turn_id": turn_id, "phase": "answer", "attempt": 1,
            "visibility": "internal",
        }), state)
        try:
            response = decision.answer.strip() if decision else ""
            if not response:
                response = await interaction.answer(entered_goal)
            interaction.context_tree.upsert(
                node_type="handoff", source_type="interaction", source_ref=state.run_id,
                title="Project conversation handoff",
                content=json.dumps({"request": entered_goal, "answer": response}, ensure_ascii=False),
                summary=interaction.context_tree.summarize(
                    decision.reason if decision else "Read-only project answer", response,
                ),
                authority=2, importance=4,
            )
            broadcast(Event(EventKind.TURN_END, agent=router_agent.name, data={
                "turn_id": turn_id, "phase": "answer", "attempt": 1,
                "response": response, "usage": router_agent.last_usage.to_dict(),
                "tokens": router_agent.last_usage.total_tokens,
                "visibility": "user",
            }), state)
            state.store.finish_run(state.run_id, "done", [router_agent.state_dict()], outcome={
                "status": "answered", "kind": "chat",
            })
            state.workspace.finish_logbook_run(state.run_id, "done", [router_agent.state_dict()])
            state.status = "done"
            broadcast(Event(EventKind.DONE, data={
                "completion_kind": "answer", "run_kind": "chat", "answer": response,
                "outcome": {"status": "answered"}, "files": [],
                "visibility": "internal",
            }), state)
            return {"ok": True, "run_id": state.run_id, "run_kind": "chat", "answer": response}
        except Exception as exc:
            state.status = "error"
            state.store.finish_run(state.run_id, "error", [router_agent.state_dict()], outcome={
                "status": "failed", "kind": "chat",
            })
            state.workspace.finish_logbook_run(state.run_id, "error", [router_agent.state_dict()])
            raise HTTPException(502, Orchestration._public_error(exc)[0]) from exc

    if interaction_kind == InteractionKind.RECOVERY:
        if not resumable_workflow:
            raise HTTPException(409, "There is no interrupted design workflow to continue.")
    elif interaction_kind == InteractionKind.PLANNING and resumable_workflow:
        if resumable_workflow.state == WorkflowState.WAITING_FOR_USER:
            raise HTTPException(409, "Answer or cancel the active checkpoint before starting new planning work")
        raise HTTPException(409, "Continue or cancel the active planning workflow before starting a new one")
    else:
        saved_goal = ""
        saved_run_id = ""
        resumes_saved_run = False

    if not product_goal:
        raise HTTPException(400, "Enter a planning goal before starting a planning workflow")
    if not brief and product_goal:
        # Product identity is persisted only after semantic routing selects the
        # planning path. Read-only questions can never become the project brief.
        state.workspace.write_brief(product_goal)
        brief = product_goal
    if body.save_brief and entered_goal:
        state.workspace.write_brief(entered_goal)
        product_goal = entered_goal

    state.event_log.clear()
    state.run_id = intake_run_id
    state.current_idea = product_goal
    state.status = "running"
    state.last_transition = "run_started"
    state.awaiting_input = False
    if state.store:
        # A project may have been opened from another browser runtime before
        # process/session reconciliation. There can be only one active logical
        # run in its database; preserve abandoned rows as interrupted first.
        state.store.reconcile_interrupted_runs()
        if resumes_saved_run and saved_run_id:
            state.store.resume_run(state.run_id)
        else:
            state.store.update_run_contract(state.run_id, "planning_workflow")
    if resumes_saved_run and saved_run_id:
        state.workspace.resume_logbook_run(state.run_id)
    else:
        state.workspace.begin_logbook_run(state.run_id, task or product_goal)
    if state.debug_observer:
        state.debug_observer.start_run(state.run_id, task or product_goal, effective_mode)

    run_workspace = state.workspace.staged_for_run(state.run_id)
    run_workspace.freeze_planning_evidence(product_goal)
    state.orchestrator = Orchestration(
        agents=agents,
        workspace=run_workspace,
        event_cb=lambda e: broadcast(e, state),
        max_tokens=body.max_tokens,
        max_debate_rounds=body.max_debate_rounds,
        store=state.store,
        run_id=state.run_id,
    )

    async def run_and_update():
        try:
            snapshot = await state.orchestrator.run(product_goal, task=task)
            if state.status != "idle":
                alignment_errors = state.orchestrator.ws.validate_goal_alignment(product_goal)
                if state.orchestrator.completion_files and alignment_errors:
                    raise PlanningQualityError("Artifact promotion blocked: " + " | ".join(alignment_errors))
                state.orchestrator.ws.promote_staged_artifacts()
                state.status = "done"
                state.awaiting_input = False
                if state.store and state.run_id:
                    agent_states = [agent.state_dict() for agent in state.orchestrator.participants]
                    completed_turns = [
                        turn for turn in state.store.run_turns(state.run_id)
                        if turn.get("status") == "completed"
                    ]
                    participant_names = list(dict.fromkeys(
                        str(turn.get("agent", "")) for turn in completed_turns if turn.get("agent")
                    ))
                    participant_states = [
                        agent for agent in agent_states if agent.get("name") in participant_names
                    ]
                    debate_proof = {
                        "participants": participant_names,
                        "opposing_architects": [
                            name for name in participant_names
                            if name.lower().startswith("architect_")
                            and name != state.orchestrator._coordinator_name
                        ],
                    }
                    state.store.finish_run(
                        state.run_id, "done",
                        agent_states,
                        outcome={
                            "status": "verified",
                            "kind": state.orchestrator.completion_kind,
                            "files": state.orchestrator.completion_files,
                            "debate": debate_proof,
                        },
                    )
                    state.workspace.finish_logbook_run(state.run_id, "done", participant_states)
                if state.workspace and snapshot:
                    try:
                        proj_name = state.workspace.project_root.name or "project"
                        export_dir = state.workspace.root / "exports"
                        export_dir.mkdir(parents=True, exist_ok=True)
                        bundle_path = export_dir / f"{proj_name}.md"
                        bundled = (
                            snapshot.get("design", "").rstrip()
                            + "\n\n"
                            + snapshot.get("plan", "").lstrip()
                        )
                        bundle_path.write_text(bundled)
                    except Exception:
                        pass
                broadcast(Event(kind=EventKind.DONE, data={
                    "workspace": snapshot or {},
                    "completion_kind": state.orchestrator.completion_kind,
                    "run_kind": state.orchestrator.contract.kind.value if state.orchestrator.contract else "",
                    "outcome": {
                        "status": "verified",
                        "files": state.orchestrator.completion_files,
                        "debate": debate_proof if state.store and state.run_id else {},
                    },
                    "files": state.orchestrator.completion_files,
                }), state)
        except asyncio.CancelledError:
            if state.orchestrator and hasattr(state.orchestrator, "ws"):
                state.orchestrator.ws.preserve_staged_artifacts("stopped")
        except Exception as exc:
            if state.orchestrator and hasattr(state.orchestrator, "ws"):
                state.orchestrator.ws.preserve_staged_artifacts("error")
            state.status = "error"
            state.awaiting_input = False
            logger.exception("Workflow run failed")
            public_error, error_code = Orchestration._public_error(exc)
            diagnostic = str(exc)[:4000] if isinstance(exc, DesignFlowError) else ""
            broadcast(Event(kind=EventKind.ERROR, data={
                "error": public_error, "error_code": error_code, "details": diagnostic,
            }), state)
            if state.store and state.run_id:
                agent_states = [agent.state_dict() for agent in state.orchestrator.participants]
                state.store.finish_run(
                    state.run_id, "error",
                    agent_states,
                    outcome={
                        "status": "failed",
                        "kind": state.orchestrator.contract.kind.value if state.orchestrator.contract else "",
                        "error_code": error_code,
                    },
                )
                state.workspace.finish_logbook_run(state.run_id, "error", agent_states)
        finally:
            if state.orchestrator:
                state.orchestrator.stop()
            close_detached_terminal_runtime(state)

    state.run_task = asyncio.create_task(run_and_update())
    idea_source = "saved_run" if resumes_saved_run else ("prompt" if body.idea.strip() else "DESIGNFLOW.md")
    return {"ok": True, "run_id": state.run_id, "idea_source": idea_source, "resumed": resumes_saved_run}


@app.post("/run/reset")
async def reset_run(state: AppState = Depends(get_state)):
    if state.status == "running":
        raise HTTPException(400, "Cannot reset while running. Stop first.")

    if state.run_task and not state.run_task.done():
        state.run_task.cancel()
        await asyncio.gather(state.run_task, return_exceptions=True)

    state.status = "idle"
    state.awaiting_input = False
    state.orchestrator = None
    state.event_log.clear()
    return {"ok": True}


@app.post("/run/pause")
def pause_run(state: AppState = Depends(get_state)):
    if state.status == "needs_attention":
        raise HTTPException(409, "Fix the failed agent and retry its turn")
    reconcile_runtime_status(state)
    if state.status != "running" or not state.orchestrator:
        raise HTTPException(409, "There is no running workflow to pause")
    state.orchestrator.pause()
    state.status = "paused"
    state.last_transition = "paused_by_user"
    state.awaiting_input = False
    return {"ok": True, "status": state.status}


class ResumeBody(BaseModel):
    max_tokens: Optional[int] = None

@app.post("/run/resume")
def resume_run(body: Optional[ResumeBody] = None, state: AppState = Depends(get_state)):
    reconcile_runtime_status(state)
    if state.orchestrator and state.orchestrator.failed_turn:
        raise HTTPException(409, "Use Retry failed turn after fixing the agent")
    if state.status == "running" and state.orchestrator and body and body.max_tokens is not None:
        state.orchestrator.max_tokens = body.max_tokens
        return {"ok": True, "status": state.status}
    if state.status != "paused" or not state.orchestrator:
        raise HTTPException(409, "There is no paused workflow to resume")
    if state.awaiting_input and state.store and state.store.current_checkpoint(state.run_id):
        raise HTTPException(409, "Answer the active decision checkpoint before resuming")
    if body and body.max_tokens is not None:
        state.orchestrator.max_tokens = body.max_tokens
    state.orchestrator.resume()
    state.status = "running"
    state.last_transition = "resumed_by_user"
    state.awaiting_input = False
    return {"ok": True, "status": state.status}


@app.post("/run/retry")
def retry_failed_turn(state: AppState = Depends(get_state)):
    if not state.orchestrator or not state.orchestrator.failed_turn:
        raise HTTPException(400, "There is no failed turn to retry")
    state.orchestrator.retry_failed_turn()
    state.status = "running"
    state.last_transition = "failed_turn_retry_requested"
    state.awaiting_input = False
    failed = state.orchestrator.failed_turn or {}
    return {"ok": True, "status": state.status, "turn": {
        "turn_id": failed.get("turn_id"),
        "attempt": failed.get("attempt"),
        "agent": failed.get("agent"),
    }}


class ProviderRecoveryBody(BaseModel):
    action: str


@app.post("/run/recover-provider")
def recover_provider_turn(body: ProviderRecoveryBody, state: AppState = Depends(get_state)):
    if not state.orchestrator or not state.orchestrator.failed_turn:
        raise HTTPException(400, "There is no failed provider turn to recover")
    if body.action not in {"auto_failover", "wait_and_retry"}:
        raise HTTPException(400, "Recovery action must be auto_failover or wait_and_retry")
    failed = state.orchestrator.failed_turn
    if body.action == "auto_failover":
        active = next(
            (agent for agent in state.orchestrator.agents if agent.config.id == failed.get("agent_id")),
            None,
        )
        if active is None:
            raise HTTPException(409, "The failed logical agent is no longer active")
        failed_provider_id = failed.get("provider_id") or active.config.base_id or active.config.id
        current_provider_id = active.config.base_id or active.config.id
        # Pausing a provider already rebinds its live specialists. Do not move
        # the same specialist again when Auto-failover is clicked afterwards.
        if current_provider_id == failed_provider_id:
            active = _reassign_agent_if_paused(state, active, failed_provider_id)
    state.orchestrator.recover_failed_turn(body.action)
    state.status = "running"
    state.last_transition = f"provider_recovery_{body.action}"
    state.awaiting_input = False
    return {
        "ok": True, "status": state.status, "action": body.action,
        "provider_id": active.config.base_id or active.config.id if body.action == "auto_failover" else "",
    }


@app.post("/run/stop")
async def stop_run(state: AppState = Depends(get_state)):
    durable = None
    if state.store and state.run_id:
        try:
            durable = WorkflowRepository(state.store).get(state.run_id)
        except KeyError:
            pass
    if durable and durable.state in {WorkflowState.COMPLETED, WorkflowState.CANCELLED, WorkflowState.FAILED}:
        state.status = "idle"
        state.awaiting_input = False
        return {"ok": True, "already_terminal": True}

    if state.run_task and not state.run_task.done():
        state.run_task.cancel()
        await asyncio.gather(state.run_task, return_exceptions=True)

    if state.orchestrator:
        state.orchestrator.stop()
        if hasattr(state.orchestrator, "ws"):
            state.orchestrator.ws.preserve_staged_artifacts("stopped")
        state.orchestrator.resume()
        for agent in state.orchestrator.agents:
            if agent.status == AgentStatus.WAITING:
                agent.status = AgentStatus.IDLE
                agent.retry_at = ""
                agent.retry_reason = ""
        if state.store and state.run_id:
            # Keep the last safe workflow position so an empty fresh start can
            # continue the design instead of silently beginning again.
            state.orchestrator.save_state()
            agent_states = [agent.state_dict() for agent in state.orchestrator.participants]
            state.store.finish_run(
                state.run_id, "stopped",
                agent_states,
            )
            if state.workspace:
                state.workspace.finish_logbook_run(state.run_id, "stopped", agent_states)
    state.status = "idle"
    state.last_transition = "stopped_by_user"
    state.awaiting_input = False
    broadcast(Event(kind=EventKind.PHASE, data={
        "phase": "run", "status": "stopped", "message": "Run stopped. Scheduled retries were cancelled."
    }), state)
    return {"ok": True}


class SteerBody(BaseModel):
    message: str


@app.post("/run/steer")
async def steer_run(
    body: SteerBody, session: Session = Depends(get_session), state: AppState = Depends(get_state),
):
    reconcile_runtime_status(state)
    if state.status not in {"running", "paused", "needs_attention"} or not state.orchestrator:
        raise HTTPException(409, "There is no active workflow to steer")
    await state.orchestrator.steer(body.message, session.username)
    return {"ok": True}


class CheckpointAnswerBody(BaseModel):
    option_id: str = ""
    custom_answer: str = ""


def checkpoint_projection(checkpoint: dict) -> str:
    if not checkpoint:
        return ""
    parts = [checkpoint["question"]]
    if checkpoint.get("rationale"):
        parts.append(f"Why this matters: {checkpoint['rationale']}")
    for option in checkpoint.get("options", []):
        suffix = f" — {option['consequence']}" if option.get("consequence") else ""
        recommended = " (Recommended)" if option.get("recommended") else ""
        parts.append(f"- [{option['label']}] {option['summary']}{suffix}{recommended}")
    if checkpoint.get("recommendation"):
        parts.append(f"Recommendation: {checkpoint['recommendation']}")
    return "\n\n".join(parts)


def ensure_structured_checkpoint(state: AppState) -> dict:
    if not state.store:
        return {}
    current = (state.store.current_checkpoint(state.run_id)
               if state.run_id else state.store.latest_current_checkpoint())
    if current and state.workspace:
        state.workspace.write("questions", "# Decision Checkpoint\n\n" + checkpoint_projection(current))
    return current


@app.get("/run/checkpoint/current")
def current_checkpoint(state: AppState = Depends(get_state)):
    checkpoint = ensure_structured_checkpoint(state)
    return {"checkpoint": checkpoint or None}


@app.post("/run/checkpoint/{checkpoint_id}/answer")
async def answer_checkpoint(
    checkpoint_id: str, body: CheckpointAnswerBody,
    session: Session = Depends(get_session), state: AppState = Depends(get_state),
):
    if not state.store:
        raise HTTPException(409, "There is no active checkpoint")
    checkpoint = state.store.checkpoint(checkpoint_id)
    if not checkpoint:
        raise HTTPException(409, "This checkpoint no longer exists")
    try:
        answered, next_checkpoint = state.store.answer_checkpoint(
            checkpoint["run_id"], checkpoint_id, session.username, body.option_id, body.custom_answer,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    answer = answered["answer"]
    requires_resume = not state.orchestrator or not getattr(state.orchestrator, "_running", False)
    if state.workspace:
        decision_workspace = state.orchestrator.ws if state.orchestrator else state.workspace
        decision_workspace.record_user_decision(answered["question"], answer)
        if next_checkpoint:
            state.workspace.write("questions", "# Decision Checkpoint\n\n" + checkpoint_projection(next_checkpoint))
        else:
            state.workspace.clear_questions()
    if state.orchestrator:
        await state.orchestrator.accept_structured_checkpoint_answer(
            answer, bool(next_checkpoint), session.username,
        )
        if state.status == "paused":
            state.orchestrator.resume()
            state.status = "running"
            state.awaiting_input = False
    return {
        "ok": True,
        "answered": answered,
        "next_checkpoint": next_checkpoint or None,
        "requires_resume": requires_resume and not next_checkpoint,
    }


@app.get("/runs/{run_id}/checkpoints")
def checkpoint_history(run_id: str, state: AppState = Depends(get_state)):
    return {"checkpoints": state.store.run_checkpoints(run_id) if state.store else []}


@app.get("/admin/runtime-diagnostics")
def runtime_diagnostics(session: Session = Depends(get_session)):
    if session.role != "admin":
        raise HTTPException(403, "Admins only")
    with runtime_registry_lock:
        diagnostics = []
        for project_path, state in app_states.items():
            reconcile_runtime_status(state)
            diagnostics.append(runtime_diagnostic(state, project_path))
    return {"runtimes": diagnostics}


@app.get("/admin/audit")
def read_audit_log(
    username: str = "", action: str = "", result: str = "", limit: int = 100,
    session: Session = Depends(get_session),
):
    if session.role != "admin":
        raise HTTPException(403, "Admins only")
    return {
        "events": audit_log.query(username=username, action=action, result=result, limit=limit),
        "dropped_events": audit_log.dropped,
    }


@app.get("/run/status")
def run_status(state: AppState = Depends(get_state)):
    reconcile_runtime_status(state)
    agents = [agent.state_dict() for agent in state.orchestrator.participants] if state.orchestrator else []
    workflow = None
    effective_run_id = state.run_id
    effective_status = state.status
    effective_awaiting_input = state.awaiting_input
    if state.store and not effective_run_id:
        saved = WorkflowRepository(state.store).latest_resumable()
        if saved:
            workflow = saved.model_dump(mode="json")
            effective_run_id = saved.run_id
            if saved.state == WorkflowState.WAITING_FOR_USER:
                effective_status = "paused"
                effective_awaiting_input = True
            elif saved.state in {WorkflowState.RETRYABLE_FAILURE, WorkflowState.WAITING_FOR_RECOVERY}:
                effective_status = "needs_attention"
    if state.store and effective_run_id and workflow is None:
        try:
            workflow = WorkflowRepository(state.store).get(effective_run_id).model_dump(mode="json")
        except KeyError:
            pass
    if state.store and effective_run_id and not agents:
        with state.store._lock:
            rows = state.store._db.execute(
                "SELECT agent_id id,agent_id base_id,role,provider_name,provider_kind kind,model,"
                "'completed' status FROM run_participants WHERE run_id=? ORDER BY ordinal",
                (effective_run_id,),
            ).fetchall()
        agents = [dict(row) for row in rows]
    return {
        "status": effective_status,
        "awaiting_input": effective_awaiting_input,
        "run_id": effective_run_id,
        "idea": state.current_idea,
        "project_path": state.workspace.path if state.workspace else "",
        "agents": agents,
        "project_usage": state.store.project_usage() if state.store else {
            "total_tokens": 0, "cached_input_tokens": 0,
            "estimated_cost_usd": 0, "pricing_complete": True, "run_count": 0,
        },
        "failed_turn": state.orchestrator.failed_turn if state.orchestrator else None,
        "phase_usage": state.orchestrator.phase_usage if state.orchestrator else {},
        "workflow": workflow,
    }


@app.get("/run/progress")
def run_progress(state: AppState = Depends(get_state)):
    """Return a read-only workflow summary without changing recovery state."""
    reconcile_runtime_status(state)
    if not state.workspace:
        raise HTTPException(400, "Open a project folder first")

    saved = WorkflowRepository(state.store).latest_resumable() if state.store else None
    orchestrator = state.orchestrator
    phase = (
        orchestrator.phase.value if orchestrator
        else (saved.state.value.lower() if saved else "not_started")
    )
    idea = state.current_idea or (
        WorkflowRepository(state.store).goal(saved.run_id) if saved and state.store else ""
    ) or state.workspace.brief().strip()
    snapshot = state.workspace.snapshot()
    completed_artifacts = [
        name.upper() for name in ("design", "plan", "decisions")
        if str(snapshot.get(name, "")).strip() not in {"", "(empty)"}
    ]
    questions = str(snapshot.get("questions", "")).strip()
    has_pending_question = questions not in {"", "(empty)"}
    resumable = bool(saved and state.status in {"idle", "done", "error"})
    next_actions = {
        "created": "start requirements discovery",
        "discovering": "clarify the product goal and constraints",
        "waiting_for_user": "receive your answer to the current checkpoint",
        "diverging": "collect independent typed expert proposals",
        "analyzing": "cluster agreements and identify conflicts locally",
        "resolving": "resolve material architecture conflicts",
        "synthesizing": "render the planning artifacts from accepted state",
        "validating": "validate the deterministic projections",
        "completed": "review or extend the completed planning baseline",
        "waiting_for_recovery": "select a recovery action for the failed operation",
        "not_started": "start the first design run",
    }
    effective_status = "stopped (ready to continue)" if resumable else state.status
    artifacts_text = ", ".join(completed_artifacts) if completed_artifacts else "none yet"
    message = (
        f"Status: {effective_status}. Phase: {phase.replace('_', ' ')}. "
        f"Completed artifacts: {artifacts_text}. "
        f"Next: {next_actions.get(phase, 'continue the current design workflow')}."
    )
    if has_pending_question:
        message += " A user decision or clarification is currently pending."
    return {
        "status": effective_status,
        "phase": phase,
        "idea": idea,
        "completed_artifacts": completed_artifacts,
        "awaiting_input": state.awaiting_input or has_pending_question,
        "resumable": resumable,
        "message": message,
    }


@app.get("/runs")
def recent_runs(state: AppState = Depends(get_state)):
    return {"runs": state.store.recent_runs() if state.store else []}


@app.get("/runs/{run_id}/events")
def run_transcript(run_id: str, limit: int = 200, offset: int = 0, state: AppState = Depends(get_state)):
    if not state.store:
        raise HTTPException(400, "Open a project before viewing run history")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise HTTPException(400, "Invalid run id")
    events = state.store.run_events(run_id, limit=limit, offset=offset)
    return {"run_id": run_id, "events": events, "offset": offset, "has_more": len(events) == min(limit, 200)}


@app.get("/run/recent-activity")
def recent_run_activity(limit: int = 8, state: AppState = Depends(get_state)):
    """Restore conversation, never a tail of workflow telemetry."""
    if not state.store:
        return {"run_id": "", "events": []}
    run_id = state.run_id or state.store.latest_run_id()
    recent = state.store.recent_runs(limit=max(20, limit * 2))
    run = next((item for item in recent if item["run_id"] == run_id), {})
    events = []
    # The live workspace represents the current run, not a cross-run transcript.
    # Older Q&A remains available in Run History and must not greet a user on
    # login as though it were current status.
    chat_runs = [run] if run.get("run_kind") == "chat" else []
    for chat_run in chat_runs:
        events.append({
            "event_id": f"prompt-{chat_run['run_id']}",
            "run_id": chat_run["run_id"],
            "timestamp": chat_run.get("started_at", ""),
            "kind": "user_prompt",
            "agent": "human",
            "audience": "conversation",
            "data": {"message": chat_run.get("idea", "")},
        })
        answer = next((
            event for event in reversed(state.store.run_events(chat_run["run_id"], limit=200))
            if event.get("kind") == "turn_end" and event.get("data", {}).get("phase") == "answer"
        ), None)
        if answer:
            answer["audience"] = "conversation"
            events.append(answer)
    if run.get("run_kind") in {"request_intake", "planning_workflow"}:
        events.append({
            "event_id": f"prompt-{run_id}", "run_id": run_id,
            "timestamp": run.get("started_at", ""), "kind": "user_prompt",
            "agent": "human", "audience": "conversation",
            "data": {"message": run.get("idea", "")},
        })
        if run.get("run_kind") == "request_intake" and run.get("status") == "running":
            events.append({
                "event_id": f"routing-{run_id}", "run_id": run_id,
                "timestamp": run.get("started_at", ""), "kind": "turn_start",
                "agent": "DesignFlow", "audience": "conversation",
                "data": {"turn_id": f"routing-{run_id}", "phase": "routing", "role": "request routing"},
            })
        elif run.get("status") == "error":
            events.append({
                "event_id": f"intake-error-{run_id}", "run_id": run_id,
                "timestamp": run.get("completed_at", ""), "kind": "error",
                "agent": "DesignFlow", "audience": "conversation",
                "data": {
                    "error_code": "request_intake_failed",
                    "error": "This request stopped before work began. Review the run details or submit it again.",
                },
            })
        elif run.get("run_kind") == "planning_workflow" and run.get("status") in {"done", "completed"}:
            visible = [
                event for event in state.store.run_events(run_id, limit=200)
                if event.get("data", {}).get("visibility") == "user"
            ]
            for event in visible:
                event["audience"] = "conversation"
                events.append(event)
    resumable_workflow = WorkflowRepository(state.store).latest_resumable()
    resumable = bool(
        run.get("status") == "interrupted"
        and resumable_workflow
        and resumable_workflow.run_id == run_id
        and resumable_workflow.state != WorkflowState.WAITING_FOR_USER
    )
    if resumable:
        paused_names = {str(config.get("name", "")) for config in state.configs if config.get("is_paused")}
        for event in reversed(events):
            data = event.get("data", {})
            if event.get("kind") == "error" and data.get("recoverable"):
                data["restart_recovery"] = True
                data["provider_paused"] = str(data.get("provider_agent", "")) in paused_names
                break
    return {"run_id": run_id, "events": events, "resumable": resumable}


@app.get("/runs/{run_id}/turns")
def run_turns(run_id: str, state: AppState = Depends(get_state)):
    return {"turns": state.store.run_turns(run_id) if state.store else []}

class MCPServerIn(BaseModel):
    name: str
    command: str
    args: list[str] = []
    env: dict = {}
    username: str = ""
    password: str = ""


def require_mcp_admin(session: Session = Depends(get_session)) -> Session:
    if session.role != "admin":
        raise HTTPException(403, "Only administrators can manage the DesignFlow MCP access token")
    return session


@app.get("/mcp/access-token")
def get_mcp_access_token_status(session: Session = Depends(require_mcp_admin)):
    return mcp_access_tokens.status()


@app.post("/mcp/access-token")
def generate_mcp_access_token(session: Session = Depends(require_mcp_admin)):
    return mcp_access_tokens.generate()


@app.delete("/mcp/access-token")
def revoke_mcp_access_token(session: Session = Depends(require_mcp_admin)):
    return {"revoked": mcp_access_tokens.revoke()}

@app.get("/mcp/servers")
def get_mcp_servers(state: AppState = Depends(get_state)):
    if not state.store:
        return {"servers": []}
    return {"servers": state.store.get_mcp_servers()}

@app.post("/mcp/servers")
def add_mcp_server(body: MCPServerIn, state: AppState = Depends(get_state)):
    if not state.store:
        raise HTTPException(400, "No active workspace")
    server_id = uuid.uuid4().hex[:8]
    state.store.add_mcp_server(server_id, body.name, body.command, body.args, body.env, body.username, body.password)
    return {"ok": True, "id": server_id}

@app.delete("/mcp/servers/{server_id}")
def delete_mcp_server(server_id: str, state: AppState = Depends(get_state)):
    if not state.store:
        raise HTTPException(400, "No active workspace")
    state.store.delete_mcp_server(server_id)
    return {"ok": True}


@app.get("/workspace")
def get_workspace(state: AppState = Depends(get_state)):
    if not state.workspace:
        return {"project_path": "", "src": {}, "src_files": []}
    return state.workspace.snapshot()


@app.get("/workspace/file/{key}")
def get_file(key: str, state: AppState = Depends(get_state)):
    if not state.workspace:
        raise HTTPException(404, "No active workspace")
    allowed = ["context", "design", "plan", "decisions", "questions", "logbook"]
    if key not in allowed:
        raise HTTPException(400, f"key must be one of {allowed}")
    if key == "questions":
        state.workspace.normalize_checkpoint_queue()
    return {"key": key, "content": state.workspace.read(key)}


@app.get("/workspace/staged/{run_id}")
def get_staged_workspace(run_id: str, state: AppState = Depends(get_state)):
    if not state.workspace:
        raise HTTPException(404, "No active workspace")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise HTTPException(400, "Invalid run id")
    staged = state.workspace.staged_artifact_summary(run_id)
    if not staged:
        raise HTTPException(404, "No preserved staged artifacts for this run")
    return staged


class FileUpdateBody(BaseModel):
    content: str

class ExportBody(BaseModel):
    bundled_content: str = ""
    provider: str
    model: str

@app.post("/workspace/export")
async def export_workspace(body: ExportBody, state: AppState = Depends(get_state)):
    if not state.workspace:
        raise HTTPException(404, "No active workspace")
    
    project_path = state.workspace.project_root
    if not project_path.exists() or not project_path.is_dir():
        raise HTTPException(400, "Project path does not exist")
        
    project_name = project_path.name or "project"
    active_checkpoint = state.store.latest_current_checkpoint() if state.store else {}
    if active_checkpoint:
        raise HTTPException(409, "Resolve the active decision checkpoint before exporting the planning baseline")
    validation_errors = state.workspace.validate_planning_artifacts()
    if validation_errors:
        raise HTTPException(409, {"message": "Planning baseline is not export-ready", "errors": validation_errors})
    bundled_content = state.workspace.build_export_bundle()
    
    try:
        config = AgentConfig(provider=body.provider, model=body.model)
        agent = create_agent(config)
        prompt = prompt_catalog.render(
            "agents_export",
            project_name=project_name,
            project_plan=bundled_content,
            engineering_invariants=state.workspace.engineering_invariants_context(),
        )
        agents_md = await agent.generate(prompt)
    except Exception as e:
        # Fallback to a rigid template if LLM fails
        agents_md = (
            "# Agent Guidelines for this Project\n\n"
            "## 1. Strict Architecture Adherence\n"
            f"- **Source of Truth**: You MUST refer to `{project_name}.md` in this directory for the full architecture, tech stack, and implementation plan.\n"
            f"- **No Unauthorized Deviations**: DO NOT deviate from the architecture outlined in `{project_name}.md` without explicitly asking the user for permission.\n"
            f"- **Documentation Updates**: If a fundamental design decision changes during implementation with user approval, proactively update `{project_name}.md` to reflect the new state.\n\n"
            "## 2. Task Evaluation & Proactive Recommendations\n"
            "- **Evaluate Before Acting**: For every task, analyze if the requested change makes sense within the existing repository structure and architecture.\n"
            "- **Identify Risks**: Actively look for edge cases, bugs, or performance implications in the user's request.\n"
            "- **Suggest Better Alternatives**: Always suggest alternative approaches, design improvements, or cleaner implementation patterns if a better solution is available. Do not simply execute the task verbatim if a superior path exists.\n\n"
            "## 3. Code Quality & Enterprise Best Practices\n"
            "- **Defensive Programming**: Write robust code with comprehensive error handling and logging.\n"
            "- **Modularity**: Keep functions small, focused, and decoupled. Avoid monolithic files.\n"
            "- **Clean Code**: Adhere to language-specific best practices, strict type-checking, and maintainability standards.\n\n"
            "## 4. Testing & Validation\n"
            "- **Verify Work**: Do not blindly commit code. Proactively verify that your changes compile, pass tests, and achieve the desired outcome before concluding your task.\n\n"
            "## 5. Knowledge Items (KI) Usage\n"
            "- **Check Context First**: If you receive Knowledge Items (KIs) or summaries at the start of a conversation, you MUST read the relevant KI artifacts before performing independent research or writing code to ensure you follow established project patterns.\n"
        )
    agents_md = state.workspace.apply_engineering_invariants(agents_md)

    plan_file = project_path / f"{project_name}.md"
    plan_file.write_text(bundled_content, encoding="utf-8")
    
    agents_file = project_path / "AGENTS.md"
    agents_file.write_text(agents_md, encoding="utf-8")
    
    return {"ok": True, "plan_file": str(plan_file), "agents_file": str(agents_file)}

@app.post("/workspace/file/{key}")
def update_file(key: str, body: FileUpdateBody, state: AppState = Depends(get_state)):
    if not state.workspace:
        raise HTTPException(404, "No active workspace")
    allowed = ["design", "plan", "decisions", "questions"]
    if key not in allowed:
        raise HTTPException(400, f"key must be one of {allowed}")
    state.workspace.write(key, body.content)
    return {"ok": True}


@app.get("/workspace/src/{filename:path}")
def get_src_file(filename: str, state: AppState = Depends(get_state)):
    if not state.workspace:
        raise HTTPException(404, "No active workspace")
    src = state.workspace.read_src()
    if filename not in src:
        raise HTTPException(404, "File not found")
    return {"filename": filename, "content": src[filename]}


@app.post("/workspace/src/{filename:path}")
def update_src_file(filename: str, body: FileUpdateBody, state: AppState = Depends(get_state)):
    if not state.workspace:
        raise HTTPException(404, "No active workspace")
    try:
        state.workspace.write_src(filename, body.content)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/events/history")
def event_history(state: AppState = Depends(get_state)):
    # Kept for diagnostics; the normal workspace never uses this to rebuild UI.
    return {"events": state.event_log}


@app.post("/admin/shutdown")
def admin_shutdown(background_tasks: BackgroundTasks, session: Session = Depends(get_session)):
    if session.username != "admin":
        raise HTTPException(403, "Only admin can shut down the server")
    callback = app.state.request_shutdown
    if not callable(callback):
        raise HTTPException(503, "Graceful shutdown is unavailable in this server launcher")
    if not app.state.shutting_down:
        app.state.shutting_down = True
        close_sse_connections()
        background_tasks.add_task(callback)
    return {"ok": True, "message": "Graceful server shutdown started"}

_frontend = Path(__file__).parent.parent / "frontend"


class MCPAccessMiddleware:
    """Keep MCP local by default; optionally protect remote use with a bearer token."""

    def __init__(self, asgi_app):
        self.asgi_app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in {"http", "websocket"}:
            await self.asgi_app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
        client_host = (scope.get("client") or ("", 0))[0]
        bearer_prefix = "Bearer "
        supplied_token = supplied[len(bearer_prefix):] if supplied.startswith(bearer_prefix) else ""
        access_status = mcp_access_tokens.status()
        token_required = access_status["configured"] or access_status["environment_token_configured"]
        if token_required:
            allowed = mcp_access_tokens.verify(supplied_token)
        else:
            allowed = client_host in {"127.0.0.1", "::1", "localhost", "testclient"}
        if not allowed:
            response = JSONResponse(
                {"detail": "MCP access requires localhost or a valid DESIGNFLOW_MCP_TOKEN"},
                status_code=401 if token_required else 403,
            )
            await response(scope, receive, send)
            return
        await self.asgi_app(scope, receive, send)


app.mount("/mcp", MCPAccessMiddleware(designflow_mcp_app), name="designflow-mcp")
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")
