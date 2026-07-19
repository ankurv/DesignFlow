from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def idempotency_key(run_id: str, from_state: str, event: str, payload: dict | None = None) -> str:
    """Return the same key for the same logical event and canonical payload.

    The source state is intentionally excluded. A retry observes the state after the
    original commit, so including that observed state would give the replay a new key.
    Events that may occur more than once in one run must include stable operation,
    conflict, checkpoint, or attempt identity in their payload.
    """
    # from_state remains in the API for readable call sites, but is deliberately
    # excluded: a replay arrives after state has advanced and must retain its key.
    del from_state
    material = "\x1f".join((run_id, event, canonical_json(payload or {})))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
