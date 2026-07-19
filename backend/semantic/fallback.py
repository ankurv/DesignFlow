from __future__ import annotations

import re
from difflib import SequenceMatcher

from .interface import SemanticMatch


TOKEN_RE = re.compile(r"[a-z0-9]+")


class LexicalSemanticAnalyzer:
    """Deterministic offline fallback and calibration baseline."""

    duplicate_threshold = 0.82
    related_threshold = 0.50

    @staticmethod
    def _tokens(text: str) -> tuple[str, ...]:
        return tuple(sorted(set(TOKEN_RE.findall((text or "").lower()))))

    def similarity(self, left: str, right: str) -> float:
        left_tokens, right_tokens = self._tokens(left), self._tokens(right)
        if not left_tokens and not right_tokens:
            return 1.0
        if not left_tokens or not right_tokens:
            return 0.0
        left_set, right_set = set(left_tokens), set(right_tokens)
        jaccard = len(left_set & right_set) / len(left_set | right_set)
        sequence = SequenceMatcher(None, " ".join(left_tokens), " ".join(right_tokens)).ratio()
        return round((0.7 * jaccard) + (0.3 * sequence), 6)

    def classify_pairs(self, claims: list[tuple[str, str]]) -> list[SemanticMatch]:
        matches = []
        ordered = sorted(claims, key=lambda item: item[0])
        for index, (left_id, left_text) in enumerate(ordered):
            for right_id, right_text in ordered[index + 1:]:
                score = self.similarity(left_text, right_text)
                relation = "duplicate" if score >= self.duplicate_threshold else (
                    "related" if score >= self.related_threshold else "distinct"
                )
                matches.append(SemanticMatch(left_id, right_id, score, relation))
        return matches

    def rank(self, query: str, items: list[tuple[str, str]], limit: int) -> list[tuple[str, float]]:
        ranked = ((item_id, self.similarity(query, text)) for item_id, text in items)
        return sorted(ranked, key=lambda item: (-item[1], item[0]))[:max(0, limit)]
