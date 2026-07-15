"""Passive, opt-in diagnostics for DesignFlow development runs."""

from __future__ import annotations

import json
import queue
import re
import threading
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DebugObserver:
    """Observe events without participating in or delaying the workflow."""

    def __init__(self, metadata_dir: Path, max_events: int = 500):
        self.root = Path(metadata_dir) / "debug"
        self.root.mkdir(parents=True, exist_ok=True)
        self.events_path = self.root / "events.jsonl"
        self.insights_path = self.root / "insights.json"
        self._queue: queue.Queue[dict | None] = queue.Queue(maxsize=max_events)
        self._recent = deque(maxlen=max_events)
        self._run_prompts: dict[str, str] = {}
        self._dropped = 0
        self._closed = False
        self._thread = threading.Thread(target=self._worker, name="designflow-debug-observer", daemon=True)
        self._thread.start()

    @staticmethod
    def _redact(value: Any, key: str = "") -> Any:
        if re.search(r"api.?key|authorization|password|secret|credential|token", key, re.I):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(k): DebugObserver._redact(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [DebugObserver._redact(item) for item in value[:30]]
        if isinstance(value, str):
            text = re.sub(r"/(?:Users|home)/[^/\s]+", "/[HOME]", value)
            text = re.sub(r"\b(?:sk|gsk|nvapi)-[A-Za-z0-9_-]{8,}\b", "[REDACTED_KEY]", text)
            return text[:500] + ("…" if len(text) > 500 else "")
        return value

    def start_run(self, run_id: str, prompt: str, mode: str) -> None:
        self._run_prompts[run_id] = self._redact(prompt)
        self.observe({
            "kind": "debug_run_start", "agent": "DesignFlow",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": {"run_id": run_id, "prompt": prompt, "mode": mode},
        })

    def observe(self, event: dict) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(self._redact(event))
        except queue.Full:
            self._dropped += 1

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            self._recent.append(item)
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            self._write_insights()
            self._queue.task_done()

    def _write_insights(self) -> None:
        events = list(self._recent)
        last_start = max((index for index, event in enumerate(events) if event.get("kind") == "debug_run_start"), default=0)
        events = events[last_start:]
        kinds = Counter(event.get("kind", "") for event in events)
        phases = [str(event.get("data", {}).get("phase", "")) for event in events]
        files = [str(event.get("data", {}).get("file", "")) for event in events if event.get("kind") == "file_write"]
        insights = []

        peer_reviews = phases.count("peer_review")
        if peer_reviews > 3:
            insights.append(self._insight(
                "repeated_peer_review", "medium",
                f"The run entered peer review {peer_reviews} times.",
                "Check whether a bounded task was incorrectly routed into the team workflow.",
            ))
        if kinds["retry"]:
            insights.append(self._insight(
                "provider_retries", "medium", f"The run emitted {kinds['retry']} provider retries.",
                "Surface the retry reason and next retry time near the active prompt.",
            ))
        if kinds["error"]:
            insights.append(self._insight(
                "run_errors", "high", f"The observed timeline contains {kinds['error']} errors.",
                "Review the error events and ensure the UI presents a recovery action.",
            ))
        diagram_requested = any("mermaid" in prompt.lower() or "visual design" in prompt.lower() for prompt in self._run_prompts.values())
        completed = kinds["done"] > 0
        if diagram_requested and completed and "DESIGN.md" not in files:
            insights.append(self._insight(
                "missing_requested_artifact", "high",
                "A visual-design request completed without writing DESIGN.md.",
                "Treat this as a failed outcome and offer a retry instead of reporting completion.",
            ))
        if self._dropped:
            insights.append(self._insight(
                "observer_queue_saturated", "low", f"The observer dropped {self._dropped} events.",
                "Increase the debug observer queue only if the missing diagnostic detail is needed.",
            ))

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "event_count": len(events),
            "dropped_event_count": self._dropped,
            "insights": insights,
        }
        temporary = self.insights_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.insights_path)

    @staticmethod
    def _insight(code: str, severity: str, evidence: str, suggestion: str) -> dict:
        return {"code": code, "severity": severity, "evidence": evidence, "suggestion": suggestion}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # Make room for the stop marker without blocking application shutdown.
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                self._queue.put_nowait(None)
            except queue.Empty:
                pass
        self._thread.join(timeout=1.0)
