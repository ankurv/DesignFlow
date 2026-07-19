from __future__ import annotations

import math
import os
from typing import Callable

from .fallback import LexicalSemanticAnalyzer
from .interface import SemanticMatch


class LocalEmbeddingAnalyzer:
    """Offline embedding analyzer with a deterministic lexical fallback.

    A model must already exist on disk and be provided through
    DESIGNFLOW_EMBEDDING_MODEL_PATH. Runtime downloads are intentionally forbidden.
    """

    duplicate_threshold = 0.86
    related_threshold = 0.62

    def __init__(self, model_path: str | None = None, encoder: Callable[[list[str]], list[list[float]]] | None = None):
        self.model_path = model_path or os.environ.get("DESIGNFLOW_EMBEDDING_MODEL_PATH", "")
        self._encoder = encoder
        self._model = None
        self.fallback = LexicalSemanticAnalyzer()
        self.model_name = "lexical-fallback"
        self.model_version = "1"
        if encoder:
            self.model_name = "injected-local-encoder"
        elif self.model_path:
            self._load_local_model()

    @property
    def available(self) -> bool:
        return self._encoder is not None or self._model is not None

    def _load_local_model(self):
        if not os.path.isdir(self.model_path):
            return
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_path, local_files_only=True)
            self.model_name = os.path.basename(self.model_path.rstrip(os.sep)) or "local-model"
            self.model_version = "local"
        except (ImportError, OSError, ValueError):
            self._model = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._encoder:
            return [list(map(float, vector)) for vector in self._encoder(texts)]
        if self._model is None:
            raise RuntimeError("local embedding model is unavailable")
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [list(map(float, vector)) for vector in vectors]

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if not left_norm or not right_norm:
            return 0.0
        return max(0.0, min(1.0, dot / (left_norm * right_norm)))

    def similarity(self, left: str, right: str) -> float:
        if not self.available:
            return self.fallback.similarity(left, right)
        vectors = self.embed([left, right])
        return round(self._cosine(vectors[0], vectors[1]), 6)

    def classify_pairs(self, claims: list[tuple[str, str]]) -> list[SemanticMatch]:
        if not self.available:
            return self.fallback.classify_pairs(claims)
        ordered = sorted(claims, key=lambda item: item[0])
        vectors = self.embed([text for _, text in ordered])
        matches = []
        for index, (left_id, _) in enumerate(ordered):
            for right_index in range(index + 1, len(ordered)):
                score = round(self._cosine(vectors[index], vectors[right_index]), 6)
                relation = "duplicate" if score >= self.duplicate_threshold else (
                    "related" if score >= self.related_threshold else "distinct"
                )
                matches.append(SemanticMatch(left_id, ordered[right_index][0], score, relation))
        return matches

    def rank(self, query: str, items: list[tuple[str, str]], limit: int) -> list[tuple[str, float]]:
        if not self.available:
            return self.fallback.rank(query, items, limit)
        ordered = sorted(items, key=lambda item: item[0])
        vectors = self.embed([query, *[text for _, text in ordered]])
        ranked = [
            (item_id, round(self._cosine(vectors[0], vectors[index + 1]), 6))
            for index, (item_id, _) in enumerate(ordered)
        ]
        return sorted(ranked, key=lambda item: (-item[1], item[0]))[:max(0, limit)]
