from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SemanticMatch:
    left_id: str
    right_id: str
    score: float
    relation: str


class SemanticAnalyzer(Protocol):
    def similarity(self, left: str, right: str) -> float: ...
    def classify_pairs(self, claims: list[tuple[str, str]]) -> list[SemanticMatch]: ...
    def rank(self, query: str, items: list[tuple[str, str]], limit: int) -> list[tuple[str, float]]: ...
