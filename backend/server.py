"""FastAPI backend for project selection, orchestration, persistence, and SSE."""

from __future__ import annotations

import asyncio
import json
import logging
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
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agents.base import AgentConfig, AgentStatus
from .agents.providers import AGENT_KINDS, create_agent, discover_models
from .orchestrator import Event, EventKind, Orchestrator
from .storage import ProjectStore
from .workspace.workspace import Workspace
from .crypto import encrypt_key, decrypt_key
from .errors import classify_provider_error

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    lease_task = asyncio.create_task(lease_cleanup_loop())
    yield
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
            agents = [agent.state_dict() for agent in state.orchestrator.agents] if state.orchestrator else []
            state.store.finish_run(state.run_id, "stopped", agents)
            state.store.clear_run_state()
            if state.workspace:
                state.workspace.finish_logbook_run(state.run_id, "stopped", agents)
        state.status = "idle"
        state.awaiting_input = False
        state.close()
    app_states.clear()
    unbound_states.clear()
    session_projects.clear()
    session_last_seen.clear()


app = FastAPI(title="DesignFlow", version="1.1.0", lifespan=lifespan)
app.state.shutting_down = False
app.state.request_shutdown = None
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


class AppState:
    def __init__(self):
        self.configs: list[dict] = []
        self.orchestrator: Optional[Orchestrator] = None
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

    def open_project(self, path: str) -> Workspace:
        if self.status in {"running", "paused", "needs_attention"}:
            raise ValueError("Stop the active run before changing projects")
        workspace = Workspace(path)
        workspace.ensure()
        workspace.reconcile_interrupted_logbook_runs()
        if self.store:
            self.store.close()
        self.workspace = workspace
        self.store = ProjectStore(workspace.root)
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

    @property
    def merged_configs(self) -> list[dict]:
        # If project has its own agents, use ONLY those. Otherwise, fallback to global.
        if self.configs:
            return list(self.configs)
        return load_global_agents()


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
        state = app_states.pop(project_path, None)
    if not state:
        return
    if state.orchestrator:
        state.orchestrator.stop()
    if state.run_task and not state.run_task.done():
        state.run_task.cancel()
        await asyncio.gather(state.run_task, return_exceptions=True)
    if state.store and state.run_id:
        agents = [agent.state_dict() for agent in state.orchestrator.agents] if state.orchestrator else []
        state.store.finish_run(state.run_id, "stopped", agents)
        state.store.clear_run_state()
        if state.workspace:
            state.workspace.finish_logbook_run(state.run_id, "stopped", agents)
    state.status = "idle"
    state.awaiting_input = False
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
def login(body: LoginBody, response: Response):
    session = auth_manager.login(body.username, body.password)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    response.set_cookie(key="session_id", value=session.session_id, httponly=True)
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


GLOBAL_AGENTS_PATH = Path.home() / ".designflow" / "global_agents.json"

def load_global_agents() -> list[dict]:
    GLOBAL_AGENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not GLOBAL_AGENTS_PATH.exists():
        return []
    try:
        configs = json.loads(GLOBAL_AGENTS_PATH.read_text())
        for c in configs:
            c["api_key"] = decrypt_key(c.get("api_key", ""))
        return configs
    except Exception:
        return []

def save_global_agents(configs: list[dict]):
    GLOBAL_AGENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    configs_copy = []
    for original in configs:
        c = dict(original)
        c["api_key"] = encrypt_key(c.get("api_key", ""))
        configs_copy.append(c)
    GLOBAL_AGENTS_PATH.write_text(json.dumps(configs_copy, indent=2))



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
    if state.store:
        state.store.append_event(state.run_id, data)
        if event.kind == EventKind.TURN_END and state.orchestrator:
            state.store.update_run_metrics(
                state.run_id,
                [agent.state_dict() for agent in state.orchestrator.agents]
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
            try:
                last_event_id = int(request.headers.get("last-event-id", "0") or 0)
            except ValueError:
                last_event_id = 0
            for past in state.event_log:
                if int(past.get("event_id", 0) or 0) <= last_event_id:
                    continue
                yield f"id: {past.get('event_id', '')}\ndata: {json.dumps(past)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5)
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


@app.get("/project")
def get_project(state: AppState = Depends(get_state)):
    return project_payload(state)


@app.post("/project/open")
async def open_project(body: ProjectOpenIn, session: Session = Depends(get_session)):
    try:
        state = await bind_project(session, body.path)
    except (OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, **project_payload(state), "agents": state.configs}


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


def model_for_virtual_agent(config: dict, role_index: int, provider_count: int) -> str:
    pool = model_pool_for_config(config)
    if not pool:
        return str(config.get("model", "") or "")
    provider_turn = role_index // max(1, provider_count)
    return pool[provider_turn % len(pool)]


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
        "global": load_global_agents(),
        "project": state.configs,
        "merged": state.merged_configs,
        "kinds": list(AGENT_KINDS.keys())
    }


@app.post("/agents")
def add_agent(body: AgentConfigIn, state: AppState = Depends(get_state)):
    if state.status in {"running", "paused", "needs_attention"}:
        raise HTTPException(400, "Stop the active run before adding an agent")
    config = body.model_dump()
    config["id"] = str(uuid.uuid4())[:8]
    state.configs.append(config)
    state.persist_agents()
    return {"ok": True, "agent": config}


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
    expert["role"] = active.role
    expert["system_prompt"] = active.config.system_prompt
    expert.setdefault("extra", {})["runtime_base_name"] = new_base.get("name", new_base.get("id", "provider"))

    new_agent = create_agent(to_agent_config(expert, s))
    active.transfer_runtime_state_to(new_agent)

    for i, a in enumerate(s.orchestrator.agents):
        if a is active:
            s.orchestrator.agents[i] = new_agent
            break

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
            return {"ok": True, "agent": updated}
    raise HTTPException(404, "Agent not found")


@app.delete("/agents/{agent_id}")
def delete_agent(agent_id: str, state: AppState = Depends(get_state)):
    if state.status in {"running", "paused", "needs_attention"}:
        raise HTTPException(400, "Stop the active run before removing an agent")
    state.configs = [config for config in state.configs if config["id"] != agent_id]
    state.persist_agents()
    return {"ok": True}


@app.get("/agents/global")
def list_global_agents(state: AppState = Depends(get_state)):
    return {"agents": load_global_agents()}


@app.post("/agents/global")
def add_global_agent(body: AgentConfigIn, session: Session = Depends(get_session)):

    if session.role != "admin":
        raise HTTPException(403, "Only admins can modify global agents")

    configs = load_global_agents()
    config = body.model_dump()
    config["id"] = str(uuid.uuid4())[:8]
    configs.append(config)
    save_global_agents(configs)
    return {"ok": True, "agent": config}


@app.delete("/agents/global/{agent_id}")
def delete_global_agent(agent_id: str, session: Session = Depends(get_session)):

    if session.role != "admin":
        raise HTTPException(403, "Only admins can modify global agents")

    configs = load_global_agents()
    configs = [c for c in configs if c["id"] != agent_id]
    save_global_agents(configs)
    return {"ok": True}


@app.put("/agents/global/{agent_id}")
def update_global_agent(agent_id: str, body: AgentConfigIn, session: Session = Depends(get_session)):

    if session.role != "admin":
        raise HTTPException(403, "Only admins can modify global agents")

    configs = load_global_agents()
    for index, current in enumerate(configs):
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

            configs[index] = updated
            save_global_agents(configs)
            return {"ok": True, "agent": updated}
    raise HTTPException(404, "Global agent not found")


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
        config = to_agent_config(body.model_dump(), state)
        models = discover_models(config)
        if not models:
            raise ValueError("No compatible text-generation models were returned")
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
    mode: str = "debate"


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
    if not state.merged_configs:
        raise HTTPException(400, "No agents configured")
    names = [config["name"].strip() for config in state.merged_configs]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise HTTPException(400, "Every agent needs a unique non-empty name")

    brief = state.workspace.brief().strip()
    product_goal = brief or body.idea.strip()
    task = body.idea.strip() if brief else ""
    if not product_goal:
        raise HTTPException(400, "Describe what to build or add DESIGNFLOW.md to the project")
    if body.save_brief and body.idea.strip():
        state.workspace.write_brief(body.idea)

    agents = []
    try:
        from .orchestrator import SPECIALIZED_PERSONAS
        base_configs = [c for c in state.merged_configs if not c.get("is_paused")]
        if not base_configs:
            raise HTTPException(400, "No available agents to spawn the team. Please unpause at least one agent.")

        # 1. Spawn the Virtual Company, distributing roles across all provided base configs (Round-Robin)
        for i, (role, system_prompt) in enumerate(SPECIALIZED_PERSONAS.items()):
            base_config = base_configs[i % len(base_configs)]
            expert = base_config.copy()
            expert["model"] = model_for_virtual_agent(base_config, i, len(base_configs))
            expert["id"] = f"{base_config.get('id', 'base')}_{role}"
            expert["base_id"] = base_config.get("id", "")
            expert.setdefault("extra", {})["runtime_base_name"] = base_config.get("name", base_config.get("id", "provider"))
            expert["name"] = role
            expert["role"] = role
            expert["system_prompt"] = system_prompt
            agents.append(create_agent(to_agent_config(expert, state)))

        # 2. Also include any custom agents the user explicitly defined
        for config in state.merged_configs:
            if config["name"] not in SPECIALIZED_PERSONAS and not config.get("is_paused"):
                agents.append(create_agent(to_agent_config(config, state)))
    except Exception as exc:
        raise HTTPException(400, f"Could not initialize agent team: {exc}") from exc

    state.event_log.clear()
    state.run_id = str(uuid.uuid4())[:8]
    state.current_idea = product_goal
    state.status = "running"
    state.last_transition = "run_started"
    state.awaiting_input = False
    if state.store:
        state.store.start_run(state.run_id, task or product_goal)
    state.workspace.begin_logbook_run(state.run_id, task or product_goal)

    state.orchestrator = Orchestrator(
        agents=agents,
        workspace=state.workspace,
        event_cb=lambda e: broadcast(e, state),
        max_debate_rounds=body.max_debate_rounds,
        max_tokens=body.max_tokens,
        max_build_iterations=body.max_build_iterations,
        require_approval=True,
        mode=body.mode,
        restore=True,
        store=state.store,
    )

    async def run_and_update():
        try:
            snapshot = await state.orchestrator.run(product_goal, task=task)
            if state.status != "idle":
                state.status = "done"
                state.awaiting_input = False
                if state.store and state.run_id:
                    agent_states = [agent.state_dict() for agent in state.orchestrator.agents]
                    state.store.finish_run(
                        state.run_id, "done",
                        agent_states,
                    )
                    state.workspace.finish_logbook_run(state.run_id, "done", agent_states)
                if state.workspace and snapshot:
                    try:
                        proj_name = state.workspace.project_root.name or "project"
                        bundle_path = state.workspace.project_root / f"{proj_name}.md"
                        bundled = f"# Architecture Design\n\n{snapshot.get('design', '')}\n\n# Implementation Plan\n\n{snapshot.get('plan', '')}"
                        bundle_path.write_text(bundled)
                    except Exception:
                        pass
                broadcast(Event(kind=EventKind.DONE, data={"workspace": snapshot or {}}), state)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            state.status = "error"
            state.awaiting_input = False
            logger.exception("Orchestrator run failed")
            public_error, error_code = Orchestrator._public_error(exc)
            broadcast(Event(kind=EventKind.ERROR, data={"error": public_error, "error_code": error_code}), state)
            if state.store and state.run_id:
                agent_states = [agent.state_dict() for agent in state.orchestrator.agents]
                state.store.finish_run(
                    state.run_id, "error",
                    agent_states,
                )
                state.workspace.finish_logbook_run(state.run_id, "error", agent_states)
                state.store.clear_run_state()

    state.run_task = asyncio.create_task(run_and_update())
    return {"ok": True, "run_id": state.run_id, "idea_source": "prompt" if body.idea.strip() else "DESIGNFLOW.md"}


@app.post("/run/reset")
def reset_run(state: AppState = Depends(get_state)):
    if state.status == "running":
        raise HTTPException(400, "Cannot reset while running. Stop first.")

    if state.store:
        state.store.clear_run_state()

    if state.workspace:
        try:
            (state.workspace.root / "run_state.json").unlink(missing_ok=True)
        except OSError:
            pass
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


@app.post("/run/stop")
async def stop_run(state: AppState = Depends(get_state)):
    if state.run_task and not state.run_task.done():
        state.run_task.cancel()
        await asyncio.gather(state.run_task, return_exceptions=True)

    if state.orchestrator:
        state.orchestrator.stop()
        state.orchestrator.resume()
        for agent in state.orchestrator.agents:
            if agent.status == AgentStatus.WAITING:
                agent.status = AgentStatus.IDLE
                agent.retry_at = ""
                agent.retry_reason = ""
        if state.store and state.run_id:
            agent_states = [agent.state_dict() for agent in state.orchestrator.agents]
            state.store.finish_run(
                state.run_id, "stopped",
                agent_states,
            )
            if state.workspace:
                state.workspace.finish_logbook_run(state.run_id, "stopped", agent_states)
            state.store.clear_run_state()
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
async def steer_run(body: SteerBody, state: AppState = Depends(get_state)):
    reconcile_runtime_status(state)
    if state.status not in {"running", "paused", "needs_attention"} or not state.orchestrator:
        raise HTTPException(409, "There is no active workflow to steer")
    await state.orchestrator.steer(body.message)
    return {"ok": True}


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


@app.get("/run/status")
def run_status(state: AppState = Depends(get_state)):
    reconcile_runtime_status(state)
    agents = [agent.state_dict() for agent in state.orchestrator.agents] if state.orchestrator else []
    return {
        "status": state.status,
        "awaiting_input": state.awaiting_input,
        "run_id": state.run_id,
        "idea": state.current_idea,
        "project_path": state.workspace.path if state.workspace else "",
        "agents": agents,
        "project_usage": state.store.project_usage() if state.store else {
            "total_tokens": 0, "cached_input_tokens": 0,
            "estimated_cost_usd": 0, "pricing_complete": True, "run_count": 0,
        },
        "failed_turn": state.orchestrator.failed_turn if state.orchestrator else None,
        "phase_usage": state.orchestrator.phase_usage if state.orchestrator else {},
    }


@app.get("/runs")
def recent_runs(state: AppState = Depends(get_state)):
    return {"runs": state.store.recent_runs() if state.store else []}


@app.get("/runs/{run_id}/turns")
def run_turns(run_id: str, state: AppState = Depends(get_state)):
    return {"turns": state.store.run_turns(run_id) if state.store else []}

class MCPServerIn(BaseModel):
    name: str
    command: str
    args: list[str] = []
    env: dict = {}

@app.get("/mcp")
def get_mcp_servers(state: AppState = Depends(get_state)):
    if not state.store:
        return {"servers": []}
    return {"servers": state.store.get_mcp_servers()}

@app.post("/mcp")
def add_mcp_server(body: MCPServerIn, state: AppState = Depends(get_state)):
    if not state.store:
        raise HTTPException(400, "No active workspace")
    server_id = hashlib.md5(f"{body.name}{datetime.now().isoformat()}".encode()).hexdigest()[:8]
    state.store.add_mcp_server(server_id, body.name, body.command, body.args, body.env)
    return {"ok": True, "id": server_id}

@app.delete("/mcp/{server_id}")
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
    return {"key": key, "content": state.workspace.read(key)}


class FileUpdateBody(BaseModel):
    content: str

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
        background_tasks.add_task(callback)
    return {"ok": True, "message": "Graceful server shutdown started"}

_frontend = Path(__file__).parent.parent / "frontend"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")
