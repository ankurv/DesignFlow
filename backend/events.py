from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class EventKind(str, Enum):
    PHASE = "phase"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    VERDICT = "verdict"
    FILE_WRITE = "file_write"
    STEER = "steer"
    DONE = "done"
    ERROR = "error"
    RETRY = "retry"


class EventAudience(str, Enum):
    CONVERSATION = "conversation"
    DIAGNOSTIC = "diagnostic"


@dataclass
class Event:
    kind: EventKind
    agent: str = ""
    data: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        audience = EventAudience.CONVERSATION if (
            self.kind == EventKind.ERROR
            or self.kind == EventKind.TURN_END and self.data.get("phase") == "answer"
            or self.kind == EventKind.VERDICT and self.data.get("visibility") == "user"
        ) else EventAudience.DIAGNOSTIC
        return {
            "kind": self.kind.value,
            "agent": self.agent,
            "data": self.data,
            "timestamp": self.timestamp,
            "audience": audience.value,
        }
