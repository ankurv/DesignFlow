"""Project-root workspace with private DesignFlow metadata and delta tracking."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class Workspace:
    """A real project folder plus an internal DesignFlow control area."""

    FILES = ["design", "plan", "decisions"]
    PROTOCOL_HEADERS = [
        "SUMMON_REASON",
        "EXPECTED_CONTRIBUTION",
        "NEXT_AGENT",
        "USER_SUMMARY",
        "WHY_THIS_NOW",
        "EXPECTED_OUTPUT",
        "NEEDS_USER_INPUT",
        "INSTRUCTIONS",
        "DECISION_CHECKPOINT",
        "QUALITY_GATE",
        "QUESTIONS",
        "VERDICT",
        "DESIGN_UPDATE",
        "DESIGN_APPEND",
        "PLAN_UPDATE",
        "PLAN_APPEND",
        "DECISIONS_UPDATE",
        "DECISIONS_APPEND",
    ]
    REQUIRED_PLAN_HEADERS = [
        "Requirements",
        "Non-Goals",
        "Assumptions",
        "Alternatives",
        "Decisions",
        "Risks",
        "Acceptance Criteria",
        "Implementation Phases",
        "Discovery Checkpoints",
    ]
    EXCLUDED_PARTS = {
        ".designflow", ".git", ".hg", ".svn", "node_modules", "vendor",
        ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
        "dist", "build", "artifact_history",
    }
    MAX_CONTEXT_FILE_BYTES = 512_000
    MAX_CONTEXT_FILES = 300
    CONTEXT_INDEX_EXTENSIONS = {
        ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
        ".swift", ".rb", ".php", ".cs", ".c", ".cc", ".cpp", ".h",
        ".html", ".css", ".scss", ".sql", ".graphql", ".proto", ".md",
        ".json", ".yaml", ".yml", ".toml", ".ini", ".env", ".sh",
    }
    CONTEXT_INDEX_FILENAMES = {"Dockerfile", "Makefile", "Procfile", "go.mod", "go.sum"}

    def __init__(self, project_path: str):
        if not project_path.strip():
            raise ValueError("A project folder is required")
        self.project_root = Path(project_path).expanduser().resolve()

        self.root = self.project_root / ".designflow"
        self.brief_path = self.project_root / "DESIGNFLOW.md"
        self.legacy_brief_path = self.project_root / "AGENTFLOW.md"
        self._checksums: dict[str, dict[str, str]] = {}
        self._active_logbook_run_id = ""

    @property
    def path(self) -> str:
        return str(self.project_root)

    def settings(self) -> dict:
        import json
        path = self.root / "settings.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def save_settings(self, new_settings: dict):
        import json
        self.ensure()
        current = self.settings()
        current.update(new_settings)
        path = self.root / "settings.json"
        path.write_text(json.dumps(current, indent=2))

    def ensure(self):

        self.project_root.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(exist_ok=True)
        ignore = self.root / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n!.gitignore\n")
        capabilities = self.root / "product_capabilities.json"
        if not capabilities.exists():
            bundled = Path(__file__).resolve().parents[1] / "product_capabilities.json"
            capabilities.write_text(bundled.read_text(encoding="utf-8"), encoding="utf-8")
            
        personas = self.root / "agent_personas.json"
        if not personas.exists():
            bundled = Path(__file__).resolve().parents[1] / "agent_personas.json"
            personas.write_text(bundled.read_text(encoding="utf-8"), encoding="utf-8")

    def brief(self) -> str:
        if self.brief_path.exists():
            return self.brief_path.read_text(errors="replace")
        if self.legacy_brief_path.exists():
            return self.legacy_brief_path.read_text(errors="replace")
        return ""

    def write_brief(self, content: str):
        self.ensure()
        self.brief_path.write_text(content.rstrip() + "\n")

    def align_generated_goal_header(self, goal: str) -> None:
        """Repair only DesignFlow's generated Idea header; preserve authored design content."""
        design_path = self._file("design")
        if not goal.strip() or not design_path.exists():
            return
        content = design_path.read_text(errors="replace")
        if not content.startswith("# Design Document\n") or not re.search(r"^\*\*Idea:\*\*", content, re.MULTILINE):
            return
        updated = re.sub(r"^\*\*Idea:\*\*.*$", f"**Idea:** {goal.strip()}", content, count=1, flags=re.MULTILINE)
        if updated != content:
            design_path.write_text(updated)

    def init(self, idea: str):
        self.ensure()
        ts = datetime.now(timezone.utc).isoformat()
        self._file("design").write_text(
            f"# Design Document\n**Idea:** {idea}\n**Started:** {ts}\n\n"
            "## Debate History\n\n## Agreed Architecture\n"
        )
        self._file("plan").write_text(
            "# Plan\n\n## Tasks\n<!-- Format: - [ ] task  or  - [x] done -->\n"
        )
        self._file("decisions").write_text("# Key Decisions\n\n")
        self._file("logbook").write_text("# Workflow Log Book\n")
        self._checksums.clear()
        self.refresh_context(goal=idea, phase="discovery")

    def _file(self, key: str) -> Path:
        names = {
            "design": "DESIGN.md", "plan": "PLAN.md",
            "decisions": "DECISIONS.md",
            "questions": "QUESTIONS.md", "logbook": "LOGBOOK.md", "context": "CONTEXT.md",
            "capabilities": "product_capabilities.json",
            "personas": "agent_personas.json",
        }
        return self.root / names[key]

    def read(self, key: str) -> str:
        path = self._file(key)
        return path.read_text(errors="replace") if path.exists() else "(empty)"

    def capabilities_context(self, compact: bool = False) -> str:
        """Render the editable JSON catalog compactly so every entry reaches the model."""
        raw = self.read("capabilities")
        try:
            catalog = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return "Catalog error: product_capabilities.json is not valid JSON."
        lines = [str(catalog.get("instructions", "")).strip()]
        for item in catalog.get("capabilities", []):
            signals = ", ".join(str(value) for value in item.get("signals", []))
            notes = str(item.get("notes", "")).strip()
            line = (
                f"- {item.get('id', '')} [mode={item.get('mode', 'auto')}] {item.get('name', '')}"
            )
            if not compact and item.get("description"):
                line += f": {item.get('description')}"
            if not compact and signals:
                line += f" | signals: {signals}"
            if notes:
                line += f" | user notes: {notes}"
            lines.append(line)
        return "\n".join(filter(None, lines))

    def parse_personas(self) -> tuple[dict[str, str], dict[str, tuple[set[str], set[str]]], dict[str, tuple[list[str], list[str]]], dict[str, list[str]]]:
        """Parse agent_personas.json into the dicts used by the orchestrator."""
        self.ensure()
        raw = self.read("personas")
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}, {}, {}, {}
        
        personas = {}
        signals = {}
        keywords = {}
        allowed_mcp = {}
        for p in data.get("personas", []):
            pid = p.get("id")
            if not pid:
                continue
            personas[pid] = p.get("prompt", "")
            
            cat = p.get("category", "unknown")
            sig_set = set(p.get("signals", []))
            if cat not in signals:
                signals[cat] = (set(), set())
            signals[cat][0].update(sig_set)
            signals[cat][1].add(pid)
            
            keywords[pid] = (p.get("design_focus", []), p.get("plan_focus", []))
            allowed_mcp[pid] = p.get("allowed_mcp_servers", [])
            
        return personas, signals, keywords, allowed_mcp

    def write(self, key: str, content: str):
        self.ensure()
        self._file(key).write_text(content)
        if key in self.FILES or key == "questions":
            self.refresh_context()

    def _archive_artifact(self, key: str, content: str) -> None:
        if not content or content == "(empty)":
            return
        archive = self.root / "artifact_history" / key
        archive.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        digest = self._md5(content)[:10]
        (archive / f"{timestamp}-{digest}.md").write_text(content, encoding="utf-8")

    @staticmethod
    def _markdown_h2_sections(content: str) -> tuple[str, list[tuple[str, str]]]:
        # Only H2 headings are canonical artifact sections. H3 headings are
        # details owned by their parent section and must travel with it.
        matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", content or ""))
        if not matches:
            return (content or "").strip(), []
        preamble = (content or "")[:matches[0].start()].strip()
        sections = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
            sections.append((match.group(1).strip(), content[match.start():end].strip()))
        return preamble, sections

    def merge_artifact_update(self, key: str, update: str, title: str) -> tuple[bool, str]:
        """Merge model updates by H2 section and preserve prior unmatched content."""
        current = self.read(key)
        substantive = re.sub(r"(?m)^#{1,6}\s+.*$|^\*\*(?:Idea|Started):\*\*.*$", "", current)
        substantive = re.sub(r"<!--[\s\S]*?-->", "", substantive).strip()
        if current == "(empty)" or not substantive:
            self.write(key, f"# {title}\n\n{update.strip()}\n")
            return True, "created"

        _, incoming = self._markdown_h2_sections(update)
        if not incoming:
            return False, (
                f"Rejected {key.upper()}.md replacement because it contained no `##` sections. "
                "Return sectioned updates so existing project detail can be preserved."
            )

        preamble, existing = self._markdown_h2_sections(current)
        if not existing:
            return False, f"Rejected {key.upper()}.md update because the existing document cannot be safely section-merged."

        # Models occasionally repeat a complete section while refining. The
        # last version is the most recent synthesis, so retain it once rather
        # than allowing duplicate H2 sections into the canonical artifact.
        incoming_by_heading = {heading.casefold(): body for heading, body in incoming}
        incoming_order = list(dict.fromkeys(heading.casefold() for heading, _ in reversed(incoming)))[::-1]
        protocol_headings = {name.casefold() for name in self.PROTOCOL_HEADERS}
        # Repair artifacts produced by older versions: discard leaked protocol
        # sections and collapse duplicate canonical headings to their last
        # occurrence before applying the new synthesis.
        existing_by_heading = {
            heading.casefold(): body
            for heading, body in existing
            if heading.casefold() not in protocol_headings
        }
        existing_order = list(dict.fromkeys(
            heading.casefold() for heading, _ in reversed(existing)
            if heading.casefold() not in protocol_headings
        ))[::-1]
        merged = []
        consumed = set()
        for lookup in existing_order:
            if lookup in incoming_by_heading:
                merged.append(incoming_by_heading[lookup])
                consumed.add(lookup)
            else:
                merged.append(existing_by_heading[lookup])
        merged.extend(incoming_by_heading[heading] for heading in incoming_order if heading not in consumed)
        result = "\n\n".join(filter(None, (preamble or f"# {title}", *merged))).rstrip() + "\n"
        self._archive_artifact(key, current)
        self.write(key, result)
        return True, "merged"

    def clear_questions(self):
        path = self._file("questions")
        if path.exists():
            path.unlink()
        queue_path = self.root / "checkpoint_queue.json"
        if queue_path.exists():
            queue_path.unlink()
        self.refresh_context()

    @staticmethod
    def split_checkpoint_questions(content: str) -> list[str]:
        """Split a legacy bundled checkpoint without discarding later decisions."""
        body = re.sub(r"^#\s*(?:Decision Checkpoint|Clarifying Questions?)\s*$", "", content or "", flags=re.I | re.M).strip()
        if not body:
            return []
        lines = body.splitlines()
        marker = re.compile(
            r"^\s*(?:#{1,6}\s*)?(?:\d+[.)]\s+)?(?:\*\*)?(?:decision|question)(?:\s+\d+)?\b",
            re.I,
        )
        starts = [
            index for index, line in enumerate(lines)
            if marker.match(re.sub(r"[*_`]", "", line))
        ]
        if len(starts) < 2:
            return [body]
        if starts[0] != 0:
            # Introductory rationale belongs with the first real question; it
            # must never become a standalone checkpoint with no choices.
            starts[0] = 0
        starts.append(len(lines))
        return ["\n".join(lines[start:end]).strip() for start, end in zip(starts, starts[1:]) if "\n".join(lines[start:end]).strip()]

    def record_checkpoint_answer(self, checkpoint: str, answer: str) -> bool:
        """Record only the active question and persist any bundled remainder."""
        self.normalize_checkpoint_queue(checkpoint)
        active = self.read("questions")
        active_questions = self.split_checkpoint_questions(active)
        if not active_questions:
            self.clear_questions()
            return False
        self.record_user_decision(active_questions[0], answer)
        queue_path = self.root / "checkpoint_queue.json"
        try:
            remaining = json.loads(queue_path.read_text()) if queue_path.exists() else []
        except (OSError, json.JSONDecodeError):
            remaining = []
        if remaining:
            self.write("questions", "# Decision Checkpoint\n\n" + remaining.pop(0))
            if remaining:
                queue_path.write_text(json.dumps(remaining, indent=2))
            elif queue_path.exists():
                queue_path.unlink()
            return True
        self.clear_questions()
        return False

    def normalize_checkpoint_queue(self, checkpoint: str | None = None) -> bool:
        """Expose one active question and durably queue any bundled followers."""
        content = checkpoint if checkpoint is not None else self.read("questions")
        questions = self.split_checkpoint_questions(content)
        queue_path = self.root / "checkpoint_queue.json"
        existing: list[str] = []
        try:
            existing = json.loads(queue_path.read_text()) if queue_path.exists() else []
        except (OSError, json.JSONDecodeError):
            existing = []
        if len(questions) < 2:
            # Repair checkpoints normalized by older builds that accidentally
            # left only the introductory rationale visible and queued every
            # actual question. Preserve the rationale with the first question.
            has_choices = bool(re.search(r"^\s*(?:-\s*\[[A-Z]\]|[A-Z][).:\-])\s+", content or "", re.M))
            if existing and not has_choices:
                promoted = existing.pop(0)
                rationale = re.sub(
                    r"^#\s*(?:Decision Checkpoint|Clarifying Questions?)\s*$",
                    "", content or "", flags=re.I | re.M,
                ).strip()
                visible = "\n\n".join(filter(None, (rationale, promoted)))
                self.write("questions", "# Decision Checkpoint\n\n" + visible)
                if existing:
                    queue_path.write_text(json.dumps(existing, indent=2))
                else:
                    queue_path.unlink(missing_ok=True)
                return True
            return False
        self.write("questions", "# Decision Checkpoint\n\n" + questions[0])
        queue_path.write_text(json.dumps(questions[1:] + existing, indent=2))
        return True

    def record_user_decision(self, question: str, answer: str) -> None:
        """Append a durable, explicitly confirmed decision without an LLM call."""
        clean_question = re.sub(r"^#.*$", "", question, flags=re.MULTILINE).strip()
        clean_answer = answer.strip()
        if not clean_question or not clean_answer:
            return
        timestamp = datetime.now(timezone.utc).isoformat()
        entry = (
            f"### User-confirmed decision — {timestamp}\n"
            f"- **Question:** {clean_question}\n"
            f"- **Decision:** {clean_answer}\n"
            f"- **Status:** Confirmed by user\n"
        )
        decisions = self.read("decisions")
        if decisions == "(empty)":
            decisions = "# Key Decisions\n\n"
        self.write("decisions", decisions.rstrip() + "\n\n" + entry)

    def record_user_directive(self, directive: str) -> None:
        """Record an unsolicited, explicit user correction while preserving decision history."""
        clean = directive.strip()
        if not clean:
            return
        timestamp = datetime.now(timezone.utc).isoformat()
        entry = (
            f"### User-directed correction — {timestamp}\n"
            f"- **Directive:** {clean}\n"
            f"- **Status:** Confirmed by user\n"
            f"- **Reconciliation:** Supersedes any conflicting earlier decision or assumption; "
            f"the next synthesis pass must mark conflicts as Superseded and update DESIGN.md and PLAN.md.\n"
        )
        decisions = self.read("decisions")
        if decisions == "(empty)":
            decisions = "# Key Decisions\n\n"
        if clean in decisions:
            return
        self.write("decisions", decisions.rstrip() + "\n\n" + entry)

    @staticmethod
    def _section_excerpt(text: str, headings: list[str], limit: int = 1800) -> str:
        chunks = []
        for heading in headings:
            match = re.search(
                rf"^##\s*{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s|\Z)",
                text or "", re.MULTILINE | re.IGNORECASE,
            )
            if match and match.group(1).strip():
                chunks.append(f"### {heading}\n{match.group(1).strip()}")
        result = "\n\n".join(chunks)
        return result[:limit].rstrip()

    def artifact_fingerprints(self) -> dict[str, str]:
        return {
            key: self._md5(self.read(key))
            for key in (*self.FILES, "questions")
        }

    @property
    def context_events_path(self) -> Path:
        return self.root / "context_events.jsonl"

    def add_context_event(self, kind: str, content: str, phase: str, actor: str = "system") -> str:
        """Persist one complete context-worthy record; routine transcript stays in LOGBOOK.md."""
        self.ensure()
        timestamp = datetime.now(timezone.utc).isoformat()
        event_id = hashlib.sha256(f"{timestamp}:{kind}:{actor}:{content}".encode()).hexdigest()[:12]
        record = {
            "id": event_id, "kind": kind, "phase": phase, "actor": actor,
            "status": "open", "timestamp": timestamp, "content": content.strip(),
        }
        with self.context_events_path.open("a") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return event_id

    def context_events(self, statuses: tuple[str, ...] = ("open",)) -> list[dict]:
        if not self.context_events_path.exists():
            return []
        records = []
        for line in self.context_events_path.read_text(errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("status") in statuses and record.get("content"):
                records.append(record)
        return records

    def resolve_context_events(self, kinds: set[str], status: str = "incorporated") -> None:
        if not self.context_events_path.exists():
            return
        records = self.context_events(statuses=("open", "incorporated", "rejected", "superseded"))
        changed = False
        for record in records:
            if record.get("status") == "open" and record.get("kind") in kinds:
                record["status"] = status
                changed = True
        if changed:
            self.context_events_path.write_text(
                "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
            )

    def relevant_context_events(self, phase: str) -> list[dict]:
        allowed = {
            "discovery": {"user_steering"},
            "drafting": {"user_steering", "user_decision"},
            "peer_review": {"user_steering", "user_decision"},
            "refinement": {"peer_critique", "user_steering", "user_decision", "quality_failure"},
            "approval": {"user_steering", "user_decision", "quality_failure"},
            "complete": {"quality_failure"},
        }.get(phase, {"user_steering", "user_decision", "peer_critique", "quality_failure"})
        return [record for record in self.context_events() if record.get("kind") in allowed]

    def refresh_context(
        self,
        goal: str = "",
        phase: str = "",
        consulted_specialists: Optional[list[str]] = None,
        next_action: str = "",
    ) -> str:
        """Rebuild compact project memory locally without an LLM call."""
        self.ensure()
        explicit_goal = bool(goal.strip())
        existing = self.read("context")
        if not goal and existing != "(empty)":
            match = re.search(r"^## Product Goal\s*\n([\s\S]*?)(?=^##\s|\Z)", existing, re.MULTILINE)
            goal = match.group(1).strip() if match else ""
        brief = self.brief().strip()
        if not explicit_goal and len(brief) > len(goal):
            goal = brief
        goal = goal or brief or "Not recorded yet."
        design = self.read("design")
        plan = self.read("plan")
        decisions = self.read("decisions")
        questions = self.read("questions")
        relevant_events = self.relevant_context_events(phase)
        event_blocks = []
        for record in relevant_events:
            label = record.get("kind", "context").replace("_", " ").title()
            event_blocks.append(
                f"### {label} — {record.get('actor', 'system')}\n{record.get('content', '')}"
            )
        bounded_events = "\n\n".join(event_blocks[-12:]) or "No unresolved context records for this phase."
        fingerprints = self.artifact_fingerprints()
        fingerprint_lines = "\n".join(f"- {key}: `{value}`" for key, value in fingerprints.items())
        specialists = ", ".join(consulted_specialists or []) or "None recorded yet."
        open_questions = questions if questions != "(empty)" else "None."
        memory = f"""# DesignFlow Project Context

<!-- Deterministically generated. DESIGN.md, PLAN.md, and DECISIONS.md remain authoritative. -->

## Product Goal
{goal[:2000]}

## Current State
- Phase: {phase or 'unknown'}
- Consulted specialists: {specialists}
- Recommended next action: {next_action or 'Continue from the current planning phase.'}

## Confirmed Requirements and Constraints
{self._section_excerpt(plan, ['Requirements', 'Non-Goals', 'Assumptions'], 2200) or 'Not established yet.'}

## Architecture Summary
{self._section_excerpt(design, ['Architecture', 'Agreed Architecture', 'Known Unknowns & Validation Plan'], 2200) or 'Not established yet.'}

## Decisions
{decisions[:2200] if decisions != '(empty)' else 'No decisions recorded yet.'}

## Open Question
{open_questions[:1400]}

## Phase-Relevant Unresolved Context
{bounded_events}

## Artifact Fingerprints
{fingerprint_lines}
"""
        self._file("context").write_text(memory.rstrip() + "\n")
        return memory

    def append(self, key: str, section: str, agent: str, label: str = ""):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        header = f"\n### [{agent.upper()}{' — ' + label if label else ''} @ {ts}]\n"
        if key == "logbook" and self._active_logbook_run_id:
            target = self.root / "logbook" / f"{self._active_logbook_run_id}.md"
            existing = target.read_text(errors="replace") if target.exists() else ""
            target.write_text(existing + header + section + "\n")
            return
        self.write(key, self.read(key) + header + section + "\n")

    @staticmethod
    def _safe_run_id(run_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", run_id or "")
        if not safe:
            raise ValueError("A run id is required for logbook rotation")
        return safe

    def _rotate_legacy_logbook(self) -> None:
        index = self._file("logbook")
        existing = index.read_text(errors="replace") if index.exists() else ""
        body = existing.replace("# Workflow Log Book", "", 1).strip()
        if not body or "<!-- run:" in existing:
            if not index.exists():
                index.write_text("# Workflow Log Book\n\n## Runs\n")
            return
        archive_dir = self.root / "logbook"
        archive_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        legacy_name = f"legacy-{timestamp}.md"
        (archive_dir / legacy_name).write_text(existing)
        index.write_text(
            "# Workflow Log Book\n\n## Runs\n"
            f"- Legacy transcript: [logbook/{legacy_name}](logbook/{legacy_name})\n"
        )

    def begin_logbook_run(self, run_id: str, task: str) -> None:
        """Start a deterministic per-run transcript and keep LOGBOOK.md as a compact index."""
        self.ensure()
        safe_id = self._safe_run_id(run_id)
        self._rotate_legacy_logbook()
        archive_dir = self.root / "logbook"
        archive_dir.mkdir(parents=True, exist_ok=True)
        started = datetime.now(timezone.utc).isoformat()
        transcript = archive_dir / f"{safe_id}.md"
        transcript.write_text(
            f"# DesignFlow Run {safe_id}\n\n"
            f"- **Started:** {started}\n"
            f"- **Status:** running\n"
            f"- **Task:** {task.strip()}\n\n"
            "## Transcript\n"
        )
        index = self._file("logbook")
        content = index.read_text(errors="replace") if index.exists() else "# Workflow Log Book\n\n## Runs\n"
        if "## Runs" not in content:
            content = content.rstrip() + "\n\n## Runs\n"
        marker = f"<!-- run:{safe_id} -->"
        if marker not in content:
            content = content.rstrip() + (
                f"\n- {marker} [{safe_id}](logbook/{safe_id}.md) · {started} · status: running\n"
            )
            index.write_text(content)
        self._active_logbook_run_id = safe_id

    def resume_logbook_run(self, run_id: str) -> None:
        """Continue the same transcript instead of overwriting it on restart."""
        safe_id = self._safe_run_id(run_id)
        resumed = datetime.now(timezone.utc).isoformat()
        transcript = self.root / "logbook" / f"{safe_id}.md"
        if transcript.exists():
            content = transcript.read_text(errors="replace")
            content = re.sub(
                r"^- \*\*Status:\*\* .*$", "- **Status:** running", content,
                count=1, flags=re.MULTILINE,
            )
            transcript.write_text(content.rstrip() + f"\n\n## Resumed\n\n- **At:** {resumed}\n")
        index = self._file("logbook")
        if index.exists():
            content = index.read_text(errors="replace")
            content = re.sub(
                rf"(<!-- run:{re.escape(safe_id)} -->[^\n]*status:) [A-Za-z_]+",
                rf"\1 running", content, count=1,
            )
            index.write_text(content)
        self._active_logbook_run_id = safe_id

    def reconcile_interrupted_logbook_runs(self) -> list[str]:
        """Mark transcripts left running by an ungraceful process exit."""
        index = self._file("logbook")
        if not index.exists() or self._active_logbook_run_id:
            return []
        content = index.read_text(errors="replace")
        run_ids = re.findall(r"<!-- run:([A-Za-z0-9_.-]+) -->[^\n]*status: running", content)
        for run_id in run_ids:
            content = re.sub(
                rf"(<!-- run:{re.escape(run_id)} -->[^\n]*status:) running",
                rf"\1 interrupted",
                content,
                count=1,
            )
            transcript = self.root / "logbook" / f"{run_id}.md"
            if transcript.exists():
                transcript_content = transcript.read_text(errors="replace")
                transcript.write_text(re.sub(
                    r"^- \*\*Status:\*\* running$",
                    "- **Status:** interrupted",
                    transcript_content,
                    count=1,
                    flags=re.MULTILINE,
                ))
        if run_ids:
            index.write_text(content)
        return run_ids

    def finish_logbook_run(self, run_id: str, status: str, agents: Optional[list[dict]] = None) -> None:
        safe_id = self._safe_run_id(run_id)
        completed = datetime.now(timezone.utc).isoformat()
        agents = agents or []
        total_tokens = sum(int(agent.get("total_tokens", 0) or 0) for agent in agents)
        names = sorted({str(agent.get("name", "")).strip() for agent in agents if agent.get("name")})
        transcript = self.root / "logbook" / f"{safe_id}.md"
        if transcript.exists():
            content = transcript.read_text(errors="replace")
            content = re.sub(r"^- \*\*Status:\*\* .*$", f"- **Status:** {status}", content, count=1, flags=re.MULTILINE)
            content += (
                f"\n## Run Result\n\n- **Completed:** {completed}\n- **Status:** {status}\n"
                f"- **Tokens:** {total_tokens}\n- **Agents:** {', '.join(names) or 'None'}\n"
            )
            transcript.write_text(content)
        index = self._file("logbook")
        if index.exists():
            content = index.read_text(errors="replace")
            pattern = rf"^- <!-- run:{re.escape(safe_id)} --> .*?$"
            replacement = (
                f"- <!-- run:{safe_id} --> [{safe_id}](logbook/{safe_id}.md) · {completed} · "
                f"status: {status} · {total_tokens:,} tokens"
            )
            updated = re.sub(pattern, replacement, content, count=1, flags=re.MULTILINE)
            index.write_text(updated)
        if self._active_logbook_run_id == safe_id:
            self._active_logbook_run_id = ""

    def _safe_project_path(self, filename: str) -> Path:
        relative = Path(filename.strip().lstrip("/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe project path: {filename}")
        if any(part in {".designflow", ".git"} for part in relative.parts):
            raise ValueError(f"Cannot write DesignFlow metadata path: {filename}")
        target = (self.project_root / relative).resolve()
        if target != self.project_root and self.project_root not in target.parents:
            raise ValueError(f"Path escapes project folder: {filename}")
        return target

    def write_src(self, filename: str, content: str):
        target = self._safe_project_path(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def _project_files(self, context_only: bool = False):
        if not self.project_root.exists():
            return
        
        project_name = self.project_root.name or "project"
        excluded_files = {"AGENTS.md", f"{project_name}.md"}
        
        count = 0
        for path in sorted(self.project_root.rglob("*")):
            if not path.is_file():
                continue
            
            # Skip dynamically exported design plans from context only
            if context_only and path.parent == self.project_root and path.name in excluded_files:
                continue
                
            relative = path.relative_to(self.project_root)
            if any(part in self.EXCLUDED_PARTS for part in relative.parts):
                continue
            if context_only and path.suffix.lower() not in self.CONTEXT_INDEX_EXTENSIONS and path.name not in self.CONTEXT_INDEX_FILENAMES:
                continue
            try:
                if path.stat().st_size > self.MAX_CONTEXT_FILE_BYTES:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw:
                continue
            yield relative.as_posix(), raw.decode("utf-8", errors="replace")
            count += 1
            if count >= self.MAX_CONTEXT_FILES:
                return

    def read_src(self) -> dict[str, str]:
        return dict(self._project_files() or [])

    def list_src(self) -> list[str]:
        return list(self.read_src())

    @staticmethod
    def _md5(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()

    def src_index(self, limit: int = 120) -> str:
        files = [name for name, _ in (self._project_files(context_only=True) or [])]
        if not files:
            return "(no project files found)"
        visible = files[:limit]
        lines = [f"- {name}" for name in visible]
        remaining = len(files) - len(visible)
        if remaining > 0:
            lines.append(f"- ... and {remaining} more files")
        return "\n".join(lines)

    def scoped_context(self, roles: Optional[list[str]] = None) -> str:
        requested = roles or self.FILES
        keys = [key for key in requested if key not in {"src", "src_index"}]
        out = [
            f"=== {'PRODUCT_CAPABILITIES.json' if key == 'capabilities' else key.upper() + '.md'} ===\n"
            f"{self.capabilities_context(compact=True) if key == 'capabilities' else self.read(key)}"
            for key in keys
        ]

        if roles is None or "src_index" in requested:
            out.append(f"=== PROJECT_FILES.md ===\n{self.src_index()}")

        if roles is None or "src" in requested:
            out.extend(
                f"=== {name} ===\n```\n{content}\n```"
                for name, content in (self._project_files(context_only=True) or [])
            )

        return "\n\n".join(out) if out else "(empty context)"

    @staticmethod
    def _focused_h2_sections(text: str, keywords: list[str], limit: int) -> str:
        sections = re.findall(r"^##\s+(.+?)\s*$([\s\S]*?)(?=^##\s|\Z)", text or "", re.MULTILINE)
        if not sections:
            return (text or "")[:limit].rstrip()
        lowered = [keyword.lower() for keyword in keywords]
        ranked = []
        for index, (heading, body) in enumerate(sections):
            heading_lower = heading.lower()
            score = sum(1 for keyword in lowered if keyword in heading_lower)
            if score:
                ranked.append((-score, index, heading, body))
        if not ranked:
            ranked = [(0, 0, *sections[0])]
        chunks = []
        used = 0
        for _, _, heading, body in sorted(ranked):
            chunk = f"## {heading.strip()}\n{body.strip()}".strip()
            remaining = limit - used
            if remaining <= 0:
                break
            chunks.append(chunk[:remaining].rstrip())
            used += min(len(chunk), remaining)
        return "\n\n".join(chunks)

    def specialist_context(
        self,
        design_keywords: list[str],
        plan_keywords: list[str],
        max_chars: int = 12000,
    ) -> str:
        """Return bounded authoritative excerpts rather than complete planning documents."""
        context = self.read("context")
        decisions = self.read("decisions")
        questions = self.read("questions")
        design = self._focused_h2_sections(self.read("design"), design_keywords, 4200)
        plan = self._focused_h2_sections(self.read("plan"), plan_keywords, 2800)
        blocks = [
            f"=== CONTEXT.md ===\n{context[:3200]}",
            f"=== DECISIONS.md ===\n{decisions[:1800]}",
            f"=== ACTIVE_QUESTION.md ===\n{questions[:800]}",
            f"=== DESIGN.md RELEVANT EXCERPTS ===\n{design}",
            f"=== PLAN.md RELEVANT EXCERPTS ===\n{plan}",
        ]
        return "\n\n".join(blocks)[:max_chars].rstrip()

    def reset_context_tracking(self, agent: Optional[str] = None):
        if agent is None:
            self._checksums.clear()
        else:
            self._checksums.pop(agent, None)

    def changed_context(self, agent: str, roles: Optional[list[str]] = None) -> str:
        seen = self._checksums.setdefault(agent, {})
        requested = roles or self.FILES
        keys = [key for key in requested if key not in {"src", "src_index"}]
        out = []

        for key in keys:
            content = self.read(key)
            checksum = self._md5(content)
            if seen.get(key) != checksum:
                label = "PRODUCT_CAPABILITIES.json" if key == "capabilities" else f"{key.upper()}.md"
                rendered = self.capabilities_context(compact=True) if key == "capabilities" else content
                out.append(f"=== {label} (updated) ===\n{rendered}")
                seen[key] = checksum

        if roles is None or "src_index" in requested:
            index_text = self.src_index()
            checksum = self._md5(index_text)
            if seen.get("src_index") != checksum:
                out.append(f"=== PROJECT_FILES.md (updated) ===\n{index_text}")
                seen["src_index"] = checksum

        if roles is None or "src" in requested:
            for name, content in (self._project_files(context_only=True) or []):
                checksum = self._md5(content)
                lookup = f"src:{name}"
                if seen.get(lookup) != checksum:
                    out.append(f"=== {name} (updated) ===\n```\n{content}\n```")
                    seen[lookup] = checksum

        return "\n\n".join(out) if out else "(no changes since your last turn)"

    def full_context(self) -> str:
        parts = [f"=== {key.upper()}.md ===\n{self.read(key)}" for key in self.FILES]
        parts.extend(f"=== {name} ===\n```\n{content}\n```" for name, content in (self._project_files(context_only=True) or []))
        return "\n\n".join(parts)

    @staticmethod
    def _has_markdown_h2(text: str, heading: str) -> bool:
        pattern = rf"^##\s*{re.escape(heading)}\s*$"
        return re.search(pattern, text, re.MULTILINE) is not None

    def validate_planning_artifacts(self) -> list[str]:
        errors: list[str] = []
        plan = self.read("plan")
        design = self.read("design")
        decisions = self.read("decisions")

        missing_headers = [
            heading for heading in self.REQUIRED_PLAN_HEADERS
            if not self._has_markdown_h2(plan, heading)
        ]
        if missing_headers:
            errors.append(
                "PLAN.md is missing required headers: " + ", ".join(missing_headers)
            )

        if "```mermaid" not in design.lower() or "```" not in design:
            errors.append("DESIGN.md must include at least one Mermaid diagram block.")

        if not self._has_markdown_h2(design, "Known Unknowns & Validation Plan"):
            errors.append("DESIGN.md must include a 'Known Unknowns & Validation Plan' section.")

        operations = re.search(
            r"^##\s*Product Operations & Evolution\s*$([\s\S]*?)(?=^##\s|\Z)",
            design, re.MULTILINE | re.IGNORECASE,
        )
        if not operations:
            errors.append("DESIGN.md must include a 'Product Operations & Evolution' section.")
        else:
            coverage = operations.group(1).lower()
            missing = []
            if not re.search(r"\b(?:version|upgrade|migration|rollback|compatib)", coverage):
                missing.append("versioning and safe upgrades")
            if not re.search(r"\b(?:audit|accountability|action history)", coverage):
                missing.append("auditability and retention/privacy")
            if not re.search(r"\b(?:log|observab|monitor|diagnostic)", coverage):
                missing.append("operational logging and diagnostics")
            if missing:
                errors.append("DESIGN.md Product Operations & Evolution must cover: " + ", ".join(missing) + ".")

        decision_body = re.sub(r"^#.*$", "", decisions, flags=re.MULTILINE).strip()
        if decisions == "(empty)" or len(decision_body) < 40:
            errors.append("DECISIONS.md must record substantive choices, trade-offs, and rationale.")

        unresolved_confirmation = re.search(
            r"^#{2,6}\s+.*(?:questions?|recommendations?).{0,30}(?:for\s+)?confirmation.*$"
            r"([\s\S]*?)(?=^#{1,6}\s|\Z)",
            decisions,
            re.MULTILINE | re.IGNORECASE,
        )
        if unresolved_confirmation and re.search(
            r"(?:^\s*[-*]\s+|\?|\b(?:confirm|choose|decide|approve)\b)",
            unresolved_confirmation.group(1),
            re.MULTILINE | re.IGNORECASE,
        ):
            errors.append(
                "DECISIONS.md contains unresolved questions for confirmation; convert them into a user decision checkpoint."
            )

        leaked_markers = sorted(set(re.findall(
            r"^##\s+(DESIGN|PLAN|DECISIONS)_(?:UPDATE|APPEND)\b",
            "\n".join((design, plan, decisions)),
            re.MULTILINE | re.IGNORECASE,
        )))
        if leaked_markers:
            errors.append("Planning artifacts contain leaked response-protocol control markers.")

        brief = self.brief_path.read_text(errors="replace") if self.brief_path.exists() else ""
        observer_is_optional = bool(re.search(
            r"(?:optional(?:ly)?[\s\w-]{0,40}AI Observer|AI Observer[\s\w-]{0,40}optional)",
            brief,
            re.IGNORECASE,
        ))
        if observer_is_optional:
            mandatory_observer_lines = [
                line for line in plan.splitlines()
                if "ai observer" in line.lower()
                and "optional" not in line.lower()
                and re.search(r"(?:^- \[[ xX]\]|\b(?:depend(?:s|ency)?|requires?|must)\b)", line, re.I)
            ]
            if mandatory_observer_lines:
                errors.append(
                    "PLAN.md makes the explicitly optional AI Observer a mandatory task or dependency. "
                    "Keep core workflows independent and label Observer work optional."
                )

        phases = re.search(
            r"^##\s*Implementation Phases\s*$([\s\S]*?)(?=^##\s|\Z)",
            plan,
            re.MULTILINE,
        )
        if not phases or not re.search(r"^- \[[ xX]\] ", phases.group(1), re.MULTILINE):
            errors.append("PLAN.md Implementation Phases must contain checkable '- [ ]' tasks.")

        questions_path = self._file("questions")
        if questions_path.exists():
            questions = questions_path.read_text(errors="replace").strip()
            if questions and questions not in {"# Clarifying Questions", "# Decision Checkpoint"}:
                errors.append("QUESTIONS.md still contains unresolved user decisions.")

        return errors

    @staticmethod
    def parse_section(text: str, header: str) -> str:
        protocol_headers = "|".join(re.escape(name) for name in Workspace.PROTOCOL_HEADERS)
        pattern = (
            rf"##\s*{re.escape(header)}[:\s]*"
            rf"(.*?)(?=\n##\s*(?:{protocol_headers})[:\s]|\Z)"
        )
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def parse_files(text: str) -> dict[str, str]:
        files: dict[str, str] = {}
        pattern = r"## FILE: ([^\n]+)\n(.*?)(?=\n## FILE:|\n## [A-Z]|\Z)"
        for match in re.finditer(pattern, text, re.DOTALL):
            raw_name = match.group(1).strip()
            relative = Path(raw_name.lstrip("/"))
            if relative.is_absolute() or ".." in relative.parts:
                continue
            if any(part in {".designflow", ".git"} for part in relative.parts):
                continue
            content = match.group(2).strip()
            content = re.sub(r"^```\w*\n?", "", content)
            content = re.sub(r"\n?```$", "", content)
            files[relative.as_posix()] = content
        return files

    @staticmethod
    def parse_vote(text: str) -> str:
        return "AGREE" if "VOTE: AGREE" in text else "DISAGREE"

    @staticmethod
    def parse_verdict(text: str, role: str) -> str:
        if role == "reviewer":
            return "APPROVE" if "VERDICT: APPROVE" in text else "CHANGES NEEDED"
        return "PASS" if "VERDICT: PASS" in text else "FAIL"

    def snapshot(self) -> dict:
        src = self.read_src()
        return {
            "project_path": self.path,
            "brief": self.brief(),
            "context": self.read("context"),
            "design": self.read("design"),
            "plan": self.read("plan"),
            "decisions": self.read("decisions"),
            "questions": self.read("questions"),
            "logbook": self.read("logbook"),
            "src": src,
            "src_files": list(src.keys()),
        }
