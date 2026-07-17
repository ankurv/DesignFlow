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
        if re.search(
            r"api.?key|authorization|password|secret|credential|(?:access|refresh|bearer|session).?token",
            key, re.I,
        ):
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
        discovery_fallbacks = [
            event for event in events
            if event.get("kind") == "phase"
            and event.get("data", {}).get("phase") == "discovery"
            and event.get("data", {}).get("status") == "fallback"
        ]
        provider_failovers = [
            event for event in events
            if event.get("kind") == "phase"
            and event.get("data", {}).get("status") == "provider_failover"
        ]
        discovery_approvals = [
            event for event in events
            if event.get("kind") == "phase"
            and event.get("data", {}).get("phase") == "approval"
            and event.get("data", {}).get("status") == "waiting_for_approval"
        ]
        files = [str(event.get("data", {}).get("file", "")) for event in events if event.get("kind") == "file_write"]
        insights = []

        # Turn events also carry their current phase, so counting every event
        # double-counts a single review. A phase event represents one entry.
        peer_reviews = sum(
            1 for event in events
            if event.get("kind") == "phase"
            and event.get("data", {}).get("phase") == "peer_review"
        )
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
        if discovery_fallbacks:
            insights.append(self._insight(
                "adaptive_discovery_fallback", "medium",
                "Adaptive discovery could not use the preferred model and switched to local fallback questions.",
                "Show the provider failure in the UI and try another healthy configured agent before using the deterministic fallback.",
            ))
        if provider_failovers:
            insights.append(self._insight(
                "provider_failover", "medium",
                f"Discovery failed over between providers {len(provider_failovers)} time(s).",
                "Review model suitability, timeout settings, and terminal turn-state recording.",
            ))
        # Debate depth intentionally permits up to three distinct discovery
        # decisions. More than that exceeds the orchestrator's hard bound and
        # indicates a loop regardless of the configured debate level.
        if len(discovery_approvals) > 3:
            insights.append(self._insight(
                "repeated_discovery_checkpoint", "high",
                f"Discovery paused for approval {len(discovery_approvals)} times in one run.",
                "Resume from drafting after the first discovery answer and reject duplicate decisions.",
            ))
        token_totals = [
            int(event.get("data", {}).get("run_total_tokens", 0) or 0)
            for event in events if event.get("kind") == "turn_end"
        ]
        token_limits = [
            int(event.get("data", {}).get("run_max_tokens", 0) or 0)
            for event in events if event.get("kind") == "turn_end"
        ]
        if (token_totals and token_limits and max(token_limits) > 0
                and max(token_totals) >= 0.6 * max(token_limits)):
            insights.append(self._insight(
                "high_token_burn", "high",
                f"The run used {max(token_totals):,} of {max(token_limits):,} tokens before completion.",
                "Bound repeated phases and warn before another high-cost model call.",
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
