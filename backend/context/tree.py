"""SQLite-backed semantic context tree with whole-node retrieval."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from backend.semantic import LocalEmbeddingAnalyzer

from .compiler import estimate_tokens


@dataclass(frozen=True)
class ContextNode:
    id: str
    node_type: str
    parent_id: str | None
    source_type: str
    source_ref: str
    title: str
    content: str
    summary: str
    authority: int
    importance: int
    token_count: int
    summary_token_count: int


@dataclass(frozen=True)
class RetrievedContext:
    text: str
    node_ids: tuple[str, ...]
    summary_node_ids: tuple[str, ...]
    estimated_tokens: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _node_id(run_id: str, source_type: str, source_ref: str, node_type: str) -> str:
    material = "\x1f".join((run_id, source_type, source_ref, node_type))
    return "ctx:" + hashlib.sha256(material.encode()).hexdigest()[:24]


def _complete_lines(lines: list[str], limit: int = 12) -> str:
    """Select complete structural statements; never cut a statement mid-text."""
    selected = []
    for line in lines:
        clean = line.strip()
        if clean and clean not in selected:
            selected.append(clean)
        if len(selected) >= limit:
            break
    return "\n".join(selected)


def structural_summary(name: str, content: str, suffix: str = "") -> str:
    """Create a deterministic, loss-aware summary from complete semantic lines."""
    lines = content.splitlines()
    if suffix.lower() in {".md", ".mdx"}:
        semantic = [
            line for line in lines
            if re.match(r"^#{1,6}\s+\S", line) or re.match(r"^\s*(?:[-*]|\d+[.)])\s+\S", line)
        ]
    else:
        semantic = [
            line for line in lines
            if re.match(
                r"^\s*(?:class|def|async def|func|function|interface|type|struct|enum|"
                r"public class|private class|export (?:class|function|interface|type))\s+",
                line,
            )
        ]
    body = _complete_lines(semantic)
    if not body:
        sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", " ".join(line.strip() for line in lines if line.strip()))
        body = _complete_lines(sentences, limit=6)
    return f"{name}\n{body}".strip() if body else name


class ContextTree:
    """Persist and retrieve complete context nodes for every model operation."""

    def __init__(self, store, analyzer=None):
        self.store = store
        self.analyzer = analyzer or LocalEmbeddingAnalyzer()

    @staticmethod
    def summarize(title: str, content: str, suffix: str = "") -> str:
        return structural_summary(title, content, suffix)

    def upsert(
        self, *, run_id: str = "", node_type: str, source_type: str, source_ref: str,
        title: str, content: str, summary: str = "", parent_id: str | None = None,
        authority: int = 3, importance: int = 3,
    ) -> str:
        node_id = _node_id(run_id, source_type, source_ref, node_type)
        digest = hashlib.sha256(content.encode()).hexdigest()
        now = _now()
        with self.store._lock, self.store._db:
            self.store._db.execute(
                "INSERT INTO context_nodes(id,run_id,node_type,parent_id,source_type,source_ref,title,content,summary,"
                "content_hash,status,authority,importance,token_count,summary_token_count,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?) "
                "ON CONFLICT(run_id,source_type,source_ref,node_type) DO UPDATE SET "
                "parent_id=excluded.parent_id,title=excluded.title,content=excluded.content,summary=excluded.summary,"
                "content_hash=excluded.content_hash,status='active',authority=excluded.authority,"
                "importance=excluded.importance,token_count=excluded.token_count,"
                "summary_token_count=excluded.summary_token_count,updated_at=excluded.updated_at",
                (node_id, run_id, node_type, parent_id, source_type, source_ref, title, content, summary,
                 digest, authority, importance, estimate_tokens(content), estimate_tokens(summary) if summary else 0,
                 now, now),
            )
        return node_id

    def link(self, from_node_id: str, to_node_id: str, relation: str) -> None:
        with self.store._lock, self.store._db:
            self.store._db.execute(
                "INSERT OR IGNORE INTO context_edges(from_node_id,to_node_id,relation,created_at) VALUES(?,?,?,?)",
                (from_node_id, to_node_id, relation, _now()),
            )

    def sync_workspace(self, workspace, run_id: str = "") -> None:
        seen: set[str] = set()
        brief = workspace.brief().strip()
        if brief:
            seen.add(self.upsert(
                run_id=run_id, node_type="goal", source_type="brief", source_ref="DESIGNFLOW.md",
                title="Product goal", content=brief, summary=structural_summary("Product goal", brief, ".md"),
                authority=6, importance=1,
            ))
        for key in ("design", "plan", "decisions", "questions"):
            content = workspace.read(key)
            if content == "(empty)":
                continue
            seen.update(self._ingest_markdown(run_id, "artifact", f"{key.upper()}.md", content, 5, 2))
        for name, content in (workspace._project_files(context_only=True) or []):
            if name == "DESIGNFLOW.md":
                continue
            suffix = Path(name).suffix.lower()
            if suffix in {".md", ".mdx"}:
                seen.update(self._ingest_markdown(run_id, "repository", name, content, 4, 3))
            else:
                seen.add(self.upsert(
                    run_id=run_id, node_type="file", source_type="repository", source_ref=name,
                    title=name, content=content, summary=structural_summary(name, content, suffix),
                    authority=4, importance=4,
                ))
        # Nodes for deleted repository/artifact sources become stale, but run-owned
        # proposal and decision nodes remain independently lifecycle-managed.
        with self.store._lock, self.store._db:
            rows = self.store._db.execute(
                "SELECT id FROM context_nodes WHERE run_id=? AND source_type IN ('brief','artifact','repository')",
                (run_id,),
            ).fetchall()
            stale = [row["id"] for row in rows if row["id"] not in seen]
            if stale:
                self.store._db.executemany(
                    "UPDATE context_nodes SET status='stale',updated_at=? WHERE id=?",
                    [(_now(), node_id) for node_id in stale],
                )

    def _ingest_markdown(
        self, run_id: str, source_type: str, name: str, content: str,
        authority: int, importance: int,
    ) -> set[str]:
        file_id = self.upsert(
            run_id=run_id, node_type="document", source_type=source_type, source_ref=name,
            title=name, content=content, summary=structural_summary(name, content, ".md"),
            authority=authority, importance=importance + 1,
        )
        seen = {file_id}
        matches = list(re.finditer(r"^(#{1,6})\s+(.+?)\s*$", content, re.MULTILINE))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
            section = content[match.start():end].strip()
            if not section:
                continue
            heading = match.group(2).strip()
            ref = f"{name}#{index}:{heading}"
            child_id = self.upsert(
                run_id=run_id, node_type="section", source_type=source_type, source_ref=ref,
                title=f"{name} — {heading}", content=section,
                summary=structural_summary(f"{name} — {heading}", section, ".md"),
                parent_id=file_id, authority=authority, importance=importance,
            )
            self.link(file_id, child_id, "contains")
            seen.add(child_id)
        return seen

    def nodes(self, run_id: str = "") -> list[ContextNode]:
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT * FROM context_nodes WHERE run_id=? AND status='active' ORDER BY id", (run_id,),
            ).fetchall()
        return [ContextNode(
            id=row["id"], node_type=row["node_type"], parent_id=row["parent_id"],
            source_type=row["source_type"], source_ref=row["source_ref"], title=row["title"],
            content=row["content"], summary=row["summary"], authority=int(row["authority"]),
            importance=int(row["importance"]), token_count=int(row["token_count"]),
            summary_token_count=int(row["summary_token_count"]),
        ) for row in rows]

    def retrieve(
        self, *, query: str, run_id: str = "", max_tokens: int,
        mandatory_types: tuple[str, ...] = ("goal",), limit: int = 24,
    ) -> RetrievedContext:
        nodes = self.nodes(run_id)
        mandatory = [node for node in nodes if node.node_type in mandatory_types]
        parent_ids = {node.parent_id for node in nodes if node.parent_id}
        optional = [
            node for node in nodes
            if node not in mandatory and not (node.node_type == "document" and node.id in parent_ids)
        ]
        ranked_ids = dict(self.analyzer.rank(
            query, [(node.id, f"{node.title}\n{node.summary or node.content}") for node in optional],
            len(optional),
        )) if optional else {}
        optional.sort(key=lambda node: (
            -ranked_ids.get(node.id, 0.0), -node.authority, node.importance, node.id,
        ))
        by_id = {node.id: node for node in nodes}
        ordered: list[ContextNode] = list(mandatory)
        forced_summary_ids: set[str] = set()
        for node in optional[:limit]:
            if node.parent_id and node.parent_id in by_id:
                parent = by_id[node.parent_id]
                if parent not in ordered:
                    ordered.append(parent)
                forced_summary_ids.add(parent.id)
            if node not in ordered:
                ordered.append(node)
        blocks: list[str] = []
        full_ids: list[str] = []
        summary_ids: list[str] = []
        used = 0
        mandatory_ids = {node.id for node in mandatory}
        for node in ordered:
            if node.id in forced_summary_ids and node.summary:
                parent_summary = f"=== {node.title} [{node.source_ref}] — PARENT SUMMARY ===\n{node.summary}"
                parent_cost = estimate_tokens(parent_summary)
                if used + parent_cost <= max_tokens:
                    blocks.append(parent_summary)
                    summary_ids.append(node.id)
                    used += parent_cost
                continue
            full = f"=== {node.title} [{node.source_ref}] ===\n{node.content}"
            full_cost = estimate_tokens(full)
            if used + full_cost <= max_tokens:
                blocks.append(full)
                full_ids.append(node.id)
                used += full_cost
                continue
            if node.summary:
                summary = f"=== {node.title} [{node.source_ref}] — SUMMARY ===\n{node.summary}"
                summary_cost = estimate_tokens(summary)
                if used + summary_cost <= max_tokens:
                    blocks.append(summary)
                    summary_ids.append(node.id)
                    used += summary_cost
                    continue
            if node.id in mandatory_ids:
                raise ValueError(f"mandatory context node does not fit budget: {node.title}")
        return RetrievedContext("\n\n".join(blocks), tuple(full_ids), tuple(summary_ids), used)
