"""Shared agent session, history, usage, and cost accounting."""

from __future__ import annotations

import hashlib
import inspect
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class AgentStatus(str, Enum):
    IDLE = "idle"
    THINKING = "thinking"
    DONE = "done"
    ERROR = "error"
    WAITING = "waiting"


@dataclass
class Usage:
    """Normalized token usage returned by every provider."""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated": self.estimated,
        }

    @classmethod
    def from_dict(cls, value: Optional[dict]) -> "Usage":
        """Deserialize provider/storage usage while ignoring derived or future fields."""
        raw = value or {}
        return cls(
            input_tokens=int(raw.get("input_tokens", 0) or 0),
            cached_input_tokens=int(raw.get("cached_input_tokens", 0) or 0),
            output_tokens=int(raw.get("output_tokens", 0) or 0),
            estimated=bool(raw.get("estimated", False)),
        )


@dataclass
class Message:
    role: str
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    usage: Usage = field(default_factory=Usage)

    @property
    def tokens(self) -> int:
        return self.usage.total_tokens


@dataclass
class AgentConfig:
    name: str
    kind: str
    id: str = ""
    base_id: str = ""
    role: str = ""
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    # Runtime-only project root. It is injected by the server and never persisted.
    working_directory: str = ""
    cli_command: str = ""
    system_prompt: str = ""
    max_history_turns: int = 20
    extra: dict = field(default_factory=dict)


# USD per million tokens. Exact model matches only; custom models should set
# rates in config.extra so an estimate is never silently based on another model.
DEFAULT_PRICING = {
    ("claude", "claude-sonnet-4-6"): (3.0, 0.30, 15.0),
    ("openai", "gpt-4o"): (2.50, 1.25, 10.0),
    ("gemini", "gemini-2.5-flash"): (0.30, 0.03, 2.50),
    ("ollama", "llama3"): (0.0, 0.0, 0.0),
}

DEFAULT_MODELS = {
    "claude": "claude-sonnet-4-6",
    "openai": "gpt-4o",
    "groq": "llama-3.3-70b-versatile",
    "gemini": "gemini-2.5-flash",
    "ollama": "llama3",
}


class AgentBase(ABC):
    """A logical agent session with normalized usage and pricing."""

    # Stateful adapters receive only the new turn. The provider/session owns
    # prior context. Stateless adapters receive the local sliding window.
    manages_context = False

    def __init__(self, config: AgentConfig):
        self.config = config
        self.history: list[Message] = []
        self.status = AgentStatus.IDLE
        self.total_input_tokens = 0
        self.total_cached_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0
        self.last_usage = Usage()
        self.retry_at = ""
        self.retry_reason = ""
        self.error_message = ""
        self._session_id = hashlib.md5(
            f"{config.name}{datetime.now(timezone.utc).isoformat()}".encode()
        ).hexdigest()[:8]
        self._attempt_lock = threading.Lock()
        self._active_attempt_token = ""

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    def estimate_input_tokens(
        self, message: str, system: str = "", ephemeral_context: str = ""
    ) -> int:
        """Conservative local estimate used before a provider request."""
        prior = self._bounded_history(self._windowed_history())
        prior_text = "\n".join(item.content for item in prior)
        characters = len(system) + len(message) + len(ephemeral_context) + len(prior_text)
        # Four characters/token is a common English/code approximation. Add
        # message framing overhead and round upward rather than underestimating.
        return max(1, (characters + 3) // 4 + (len(prior) + 2) * 8)

    def _pricing(self) -> tuple[float, float, float, bool]:
        extra = self.config.extra
        configured = any(
            key in extra for key in (
                "input_cost_per_million",
                "cached_input_cost_per_million",
                "output_cost_per_million",
            )
        )
        if configured:
            return (
                float(extra.get("input_cost_per_million", 0) or 0),
                float(extra.get("cached_input_cost_per_million", 0) or 0),
                float(extra.get("output_cost_per_million", 0) or 0),
                True,
            )
        model = self.config.model or DEFAULT_MODELS.get(self.config.kind, "")
        rates = DEFAULT_PRICING.get((self.config.kind, model))
        if rates is None:
            return 0.0, 0.0, 0.0, False
        return *rates, True

    def _cost(self, usage: Usage) -> float:
        input_rate, cached_rate, output_rate, known = self._pricing()
        if not known:
            return 0.0
        cached = min(usage.cached_input_tokens, usage.input_tokens)
        uncached = max(0, usage.input_tokens - cached)
        return (
            uncached * input_rate
            + cached * cached_rate
            + usage.output_tokens * output_rate
        ) / 1_000_000

    @abstractmethod
    def _raw_send(self, messages: list[dict], system: str, mcp_tools: list[dict] = None, tool_handler: Callable = None) -> tuple[str, Usage]:
        """Return response text and normalized token usage."""

    def begin_attempt(self) -> str:
        """Make a new provider attempt authoritative for logical state commits."""
        token = str(uuid.uuid4())
        with self._attempt_lock:
            self._active_attempt_token = token
        return token

    def invalidate_attempt(self, token: str) -> None:
        with self._attempt_lock:
            if self._active_attempt_token == token:
                self._active_attempt_token = ""

    def _attempt_is_current(self, token: str | None) -> bool:
        if token is None:
            return True
        with self._attempt_lock:
            return self._active_attempt_token == token

    def send(self, message: str, system_override: Optional[str] = None, ephemeral_context: Optional[str] = None, mcp_tools: list[dict] = None, tool_handler: Callable = None, attempt_token: str | None = None, context_only: bool = False) -> str:
        """Sends a message to the model and updates the internal usage metrics."""
        if self._attempt_is_current(attempt_token):
            self.status = AgentStatus.THINKING
        user_message = Message(role="user", content=message)

        if self.manages_context:
            window = [user_message]
        else:
            counted_window = self._windowed_history()
            # Preserve the current request exactly; only historical messages
            # are compacted. Canonical workspace artifacts are supplied
            # separately as ephemeral context.
            window = self._bounded_history(counted_window) + [user_message]

        system = system_override or self.config.system_prompt
        raw_msgs = [{"role": m.role, "content": m.content} for m in window]
        if ephemeral_context:
            raw_msgs[-1]["content"] = f"{raw_msgs[-1]['content']}\n\n{ephemeral_context}"

        import os
        log_prompts = os.environ.get("DESIGNFLOW_LOG_PROMPTS") == "1"
        if log_prompts:
            print(f"\n{'='*20} LLM REQUEST ({self.name}) {'='*20}")
            print(f"SYSTEM:\n{system}\n{'-'*50}")
            for m in raw_msgs:
                print(f"{m['role'].upper()}:\n{m['content']}\n{'-'*50}")
            if mcp_tools:
                print(f"TOOLS: {len(mcp_tools)} provided")

        try:
            send_kwargs = {"mcp_tools": mcp_tools, "tool_handler": tool_handler}
            parameters = inspect.signature(self._raw_send).parameters.values()
            if any(
                parameter.name == "context_only" or parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            ):
                send_kwargs["context_only"] = context_only
            reply, usage = self._raw_send(raw_msgs, system, **send_kwargs)
            if log_prompts:
                print(f"\n{'='*20} LLM RESPONSE ({self.name}) {'='*20}")
                print(f"{reply}\n{'='*55}\n")

        except Exception as exc:
            if self._attempt_is_current(attempt_token):
                self.status = AgentStatus.ERROR
            raise RuntimeError(f"[{self.name}] send failed: {exc}") from exc

        # A timeout cannot kill a worker thread. Discard its late result unless
        # this is still the authoritative attempt for the logical specialist.
        if not self._attempt_is_current(attempt_token):
            return reply
        self.history.append(user_message)
        self.last_usage = usage
        self.total_input_tokens += usage.input_tokens
        self.total_cached_input_tokens += usage.cached_input_tokens
        self.total_output_tokens += usage.output_tokens
        self.total_cost_usd += self._cost(usage)
        self.history.append(Message(role="assistant", content=reply, usage=usage))
        self.status = AgentStatus.DONE
        self.retry_at = ""
        self.retry_reason = ""
        self.error_message = ""
        if attempt_token is not None:
            self.invalidate_attempt(attempt_token)
        return reply

    def mark_waiting(self, retry_at: str, reason: str):
        self.status = AgentStatus.WAITING
        self.retry_at = retry_at
        self.retry_reason = reason

    def mark_error(self, reason: str):
        self.status = AgentStatus.ERROR
        self.error_message = reason
        self.retry_at = ""
        self.retry_reason = ""

    def reconfigure(self, config: AgentConfig):
        """Apply a repaired configuration without discarding logical history."""
        self.config = config
        self.error_message = ""

    def reset(self):
        self.history.clear()
        self.total_input_tokens = 0
        self.total_cached_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0
        self.last_usage = Usage()
        self.retry_at = ""
        self.retry_reason = ""
        self.error_message = ""
        self.status = AgentStatus.IDLE
        self._reset_provider_session()

    def reset_conversation(self):
        """Discard routing/utility context while preserving cumulative usage accounting."""
        self.history.clear()
        self._reset_provider_session()

    def _reset_provider_session(self):
        """Stateful providers override this when they have a remote session."""

    def _windowed_history(self) -> list[Message]:
        max_messages = max(2, self.config.max_history_turns * 2)
        if len(self.history) <= max_messages:
            return self.history
        # Preserve the initial exchange and the most recent turns.
        return self.history[:2] + self.history[-(max_messages - 2):]

    def _bounded_history(self, messages: list[Message]) -> list[Message]:
        """Bound history by content size so one verbose turn cannot poison future prompts."""
        max_chars = int(self.config.extra.get("max_history_chars", 24000) or 24000)
        if max_chars <= 0 or sum(len(item.content) for item in messages) <= max_chars:
            return messages
        selected: list[Message] = []
        remaining = max_chars
        for item in reversed(messages):
            if remaining <= 0:
                break
            content = item.content
            if len(content) > remaining:
                marker = "\n\n[older response truncated]\n\n"
                available = max(0, remaining - len(marker))
                head = available // 2
                tail = available - head
                content = content[:head] + marker + (content[-tail:] if tail else "")
            selected.append(Message(
                role=item.role,
                content=content,
                timestamp=item.timestamp,
                usage=item.usage,
            ))
            remaining -= len(content)
        return list(reversed(selected))

    def usage_dict(self) -> dict:
        _, _, _, pricing_known = self._pricing()
        return {
            "input_tokens": self.total_input_tokens,
            "cached_input_tokens": self.total_cached_input_tokens,
            "output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.total_cost_usd,
            "pricing_known": pricing_known,
        }

    def state_dict(self) -> dict:
        return {
            "id": self.config.id,
            "base_id": self.config.base_id,
            "name": self.name,
            "kind": self.config.kind,
            "role": self.config.role,
            "model": self.config.model,
            "provider_name": self.config.extra.get("runtime_base_name", self.config.name),
            "status": self.status.value,
            "history_turns": len(self.history),
            "session_id": self._session_id,
            "provider_session": self.provider_session_id(),
            "retry_at": self.retry_at,
            "retry_reason": self.retry_reason,
            "error_message": self.error_message,
            **self.usage_dict(),
        }

    def transfer_runtime_state_to(self, replacement: "AgentBase") -> None:
        """Move logical specialist state to a replacement provider instance."""
        replacement.history = list(self.history)
        replacement.total_input_tokens = self.total_input_tokens
        replacement.total_cached_input_tokens = self.total_cached_input_tokens
        replacement.total_output_tokens = self.total_output_tokens
        replacement.total_cost_usd = self.total_cost_usd
        replacement.last_usage = self.last_usage
        replacement.status = self.status
        replacement.retry_at = self.retry_at
        replacement.retry_reason = self.retry_reason
        replacement.error_message = self.error_message

    def provider_session_id(self) -> str:
        return ""
