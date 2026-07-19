# Orchestration Redesign Gap Ledger

This file records the gaps identified during design review and their disposition in the single orchestration engine. The current runtime contracts live in [ARCHITECTURE.md](ARCHITECTURE.md).

| # | Area | Status | Implemented contract or remaining work |
| --- | --- | --- | --- |
| 1 | Idempotency-key strategy | Closed | `backend/workflow/idempotency.py` hashes run ID, source state, event, and canonical payload. Duplicate committed transitions are replay-safe. |
| 2 | Semantic analyzer fallback | Closed | `LexicalSemanticAnalyzer` is deterministic, orders pairs by claim ID, uses duplicate threshold `0.82` and related threshold `0.50`, and breaks ranking ties by item ID. |
| 3 | Context-packet token budgets | Closed | `ContextCompiler` defaults to a hard 2,000-token estimate. Mandatory context cannot be truncated; optional whole items are admitted by priority, relevance, and stable ID. |
| 4 | Materiality classification | Closed | `backend/workflow/materiality.py` classifies conflicts deterministically and persists the result with each conflict. |
| 5 | UI-state test coverage | Partial | Unit tests verify the authoritative state-to-view mapping. Full Playwright coverage of modal/button behavior across every durable state remains open. |
| 6 | Stored JSON validation | Closed | Pydantic validates typed models; repository reads reject malformed or incorrectly shaped stored JSON; writes require structured dictionaries/models. |
| 7 | Concurrent-write behavior | Partial | SQLite uses WAL, bounded transactions, locks, uniqueness constraints, and run/operation indexes. A sustained multi-writer benchmark remains open. |

## Remaining verification work

- Add browser-level tests for `WAITING_FOR_USER`, `WAITING_FOR_RECOVERY`, terminal states, stale checkpoint submission, and reconnect behavior.
- Add a bounded concurrent proposal/transition stress test that records lock latency and proves no deadlock or duplicate acceptance.
- Exercise recoverable failures against each real provider adapter; state support exists, but provider-specific classification needs live validation.
