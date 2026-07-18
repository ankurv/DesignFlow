# Core Workflow Recovery Plan

DesignFlow is not releasable unless a new product idea can complete the planning journey without changing identity, faking participation, promoting irrelevant output, or requiring paid-provider calls in tests.

## Release-blocking invariants

| Invariant | Enforcement | Required evidence |
|---|---|---|
| Product identity is stable | Persist the first product goal in `DESIGNFLOW.md`; later text is a task or continuation command | A refinement, retry, refresh, and restart leave the goal unchanged |
| Debate is real | Reserve a review slot for an opposing architect and block completion unless its turn completes | Persisted turns and run outcome name Alpha/Beta participants |
| Decisions are relevant | Structured checkpoint validation plus goal-alignment checks | Every checkpoint names a product consequence and offers 2–3 trade-offs |
| Artifacts are relevant | Validate product-specific goal anchors outside generated headers | A generic DesignFlow-process diagram is rejected |
| Promotion is safe | Keep canonical files unchanged until the staged set passes all completion gates | Failed/stopped runs remain staged; successful runs promote atomically with history |
| UI input is stable | Do not rebuild an unchanged checkpoint or refocus an already-open modal | A custom answer remains focused and unchanged across polling cycles |
| Reporting is truthful | Derive participants from completed persisted turns, not configured personas | Transcripts and outcomes never list an agent with zero turns |
| Retry is recovery, not a new idea | Treat `retry` as continuation and reject it when no interrupted workflow exists | No run can acquire the product goal `retry` |

## Deterministic acceptance journey

The release gate must use fake agents and exercise the HTTP/state-machine boundary:

1. Open an empty temporary project and submit a distinctive product goal.
2. Confirm the goal is persisted before the first provider turn.
3. Complete bounded discovery and answer the structured checkpoint.
4. Confirm Architect Alpha drafts and Architect Beta performs an adversarial review.
5. Inject one irrelevant diagram response and prove canonical artifacts remain unchanged.
6. Retry with aligned artifacts and prove atomic promotion plus artifact history.
7. Refresh/rebind the project and verify the current checkpoint, transcript, participants, and goal.
8. Restart the runtime, issue `continue`, and prove exact phase/run restoration.
9. Issue `retry` without resumable state and require a typed conflict instead of a new run.
10. Assert no network provider was called and the run stayed within deterministic turn/token bounds.

## Exit criteria

- The deterministic journey passes repeatedly.
- The full session, journey, provider, MCP, and frontend syntax suites pass without hangs.
- A clean manual smoke run shows the same persisted evidence.
- No known high-severity core-workflow issue remains open.

