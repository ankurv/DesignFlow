from __future__ import annotations

import hashlib
import struct
from datetime import datetime, timezone


class SQLiteSemanticIndex:
    def __init__(self, store):
        self.store = store

    def put(self, run_id: str, item_id: str, text: str, vector: list[float], model_name: str, model_version: str):
        if not vector:
            raise ValueError("embedding vector cannot be empty")
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        blob = struct.pack(f"<{len(vector)}f", *vector)
        now = datetime.now(timezone.utc).isoformat()
        with self.store._lock, self.store._db:
            self.store._db.execute(
                "INSERT INTO semantic_embeddings(run_id,item_id,model_name,model_version,dimensions,content_hash,"
                "vector_blob,created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,item_id,model_name,model_version) "
                "DO UPDATE SET dimensions=excluded.dimensions,content_hash=excluded.content_hash,"
                "vector_blob=excluded.vector_blob,created_at=excluded.created_at",
                (run_id, item_id, model_name, model_version, len(vector), content_hash, blob, now),
            )

    def get(self, run_id: str, item_id: str, model_name: str, model_version: str) -> list[float] | None:
        with self.store._lock:
            row = self.store._db.execute(
                "SELECT dimensions,vector_blob FROM semantic_embeddings WHERE run_id=? AND item_id=? "
                "AND model_name=? AND model_version=?",
                (run_id, item_id, model_name, model_version),
            ).fetchone()
        if not row:
            return None
        dimensions = int(row["dimensions"])
        blob = bytes(row["vector_blob"])
        if len(blob) != dimensions * 4:
            raise ValueError(f"stored embedding {item_id} has invalid dimensions")
        return list(struct.unpack(f"<{dimensions}f", blob))
