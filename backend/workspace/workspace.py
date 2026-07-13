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
        "DECISIONS_UPDATE",
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
        "dist", "build",
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

    def brief(self) -> str:
        if self.brief_path.exists():
            return self.brief_path.read_text(errors="replace")
        if self.legacy_brief_path.exists():
            return self.legacy_brief_path.read_text(errors="replace")
        return ""

    def write_brief(self, content: str):
        self.ensure()
        self.brief_path.write_text(content.rstrip() + "\n")

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
            "questions": "QUESTIONS.md", "logbook": "LOGBOOK.md", "context": "CONTEXT.md"
        }
        return self.root / names[key]

    def read(self, key: str) -> str:
        path = self._file(key)
        return path.read_text(errors="replace") if path.exists() else "(empty)"

    def write(self, key: str, content: str):
        self.ensure()
        self._file(key).write_text(content)
        if key in self.FILES or key == "questions":
            self.refresh_context()

    def clear_questions(self):
        path = self._file("questions")
        if path.exists():
            path.unlink()
            self.refresh_context()

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
        self.write(key, self.read(key) + header + section + "\n")

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
        count = 0
        for path in sorted(self.project_root.rglob("*")):
            if not path.is_file():
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
        out = [f"=== {key.upper()}.md ===\n{self.read(key)}" for key in keys]

        if roles is None or "src_index" in requested:
            out.append(f"=== PROJECT_FILES.md ===\n{self.src_index()}")

        if roles is None or "src" in requested:
            out.extend(
                f"=== {name} ===\n```\n{content}\n```"
                for name, content in (self._project_files(context_only=True) or [])
            )

        return "\n\n".join(out) if out else "(empty context)"

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
                out.append(f"=== {key.upper()}.md (updated) ===\n{content}")
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

        decision_body = re.sub(r"^#.*$", "", decisions, flags=re.MULTILINE).strip()
        if decisions == "(empty)" or len(decision_body) < 40:
            errors.append("DECISIONS.md must record substantive choices, trade-offs, and rationale.")

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
            "src": src,
            "src_files": list(src.keys()),
        }
