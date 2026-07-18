# DesignFlow Stability and Behavior Improvement Plan

## Objective

DesignFlow must enter a dedicated stabilization release before adding new product features.
The target is simple: every workflow transition must be deterministic, recoverable, observable,
and tested through the same public APIs used by the UI.

The stabilization release is complete only when critical workflows repeatedly pass without
manual database edits, file repair, or internal recovery prompts leaking into the user experience.

## 1. Freeze Feature Development

During stabilization:

- Do not add agents, dashboards, integrations, or new planning features.
- Accept changes only for correctness, recovery, observability, testability, and usability.
- Maintain one prioritized defect register with severity, reproduction, owner, and regression test.
- Require every fixed defect to include an automated test reproducing the complete failure.

Exit condition: all critical workflow journeys pass repeatedly without manual intervention.

## 2. Define Authoritative State Ownership

| State | Authoritative owner |
| --- | --- |
| Product goal | Frozen run evidence |
| Workflow position | Persisted typed run state |
| Active run | Project database |
| Active product decision | Structured checkpoint database row |
| Work in progress | Run-scoped staged artifacts |
| Completed baseline | Canonical artifacts |
| Provider condition | Provider runtime state |
| Browser display | Read-only projection of server state |

Required invariants:

- UI state is never authoritative.
- `QUESTIONS.md` remains a projection and is never parsed as runtime input.
- Canonical files change only after complete validation and atomic promotion.
- Generated prose cannot directly control workflow transitions.
- A project has at most one active logical run.
- Restart reconciles database, staged files, checkpoints, and runtime state before serving a project.
- Every derived UI state can be reconstructed from authoritative server state.

## 3. Implement a Formal Workflow State Machine

```text
idle
  -> discovering
  -> waiting_for_decision
  -> drafting
  -> reviewing
  -> refining
  -> validating
  -> completed

Any active state
  -> provider_attention
  -> paused
  -> stopped
  -> interrupted
  -> failed
```

For every transition, define:

- Preconditions
- Database mutation
- Artifact mutation
- Emitted event
- Browser-visible status
- Available user actions
- Recovery behavior
- Restart behavior
- Idempotency behavior

Illegal transitions must return typed conflicts. They must never trigger an inferred recovery path.

## 4. Separate Product Decisions from System Recovery

Create two unrelated typed concepts:

### ProductDecisionCheckpoint

Every product checkpoint must contain:

- One concrete product or architecture question
- Two or three viable options
- A concrete consequence for every option
- An evidence-based recommendation
- No internal workflow or quality-repair language

### SystemRecoveryAction

Every recovery action must contain:

- Failure category
- Affected provider or subsystem
- Failed turn identity
- Retry eligibility
- Auto-failover eligibility
- Retry time when known
- Stop and preservation behavior

Internal messages such as “identify the missing decision” must never enter the checkpoint table or UI.

## 5. Treat Generated Output as Untrusted Input

Every model response must follow this pipeline:

1. Parse it into a typed response structure.
2. Reject unknown, conflicting, or malformed protocol sections.
3. Validate artifact structure.
4. Validate product checkpoint quality.
5. Check contradictions against confirmed decisions.
6. Validate capability-contract completeness.
7. Write accepted output only to staged artifacts.
8. Promote only after the complete deterministic quality gate passes.

When validation fails, return structured feedback to refinement. Never invent a user decision as
a fallback for an internal quality failure.

## 6. Add a Workflow Invariant Auditor

Run deterministic invariant checks:

- After every state transition
- Before returning workflow API responses
- When opening a project
- Before restart recovery
- Before artifact promotion
- Before export

Minimum invariant set:

- At most one active run exists per project.
- At most one active checkpoint exists per run.
- `waiting_for_decision` requires one valid active checkpoint.
- `provider_attention` requires a failed turn and valid recovery actions.
- Completed or stopped runs have no live turns.
- Completed runs have no active checkpoints.
- Canonical files contain no unresolved or nonexistent checkpoint references.
- Promoted artifacts match their validated fingerprints.
- Staged artifacts cannot be presented as canonical.
- Browser status is derivable entirely from server state.

Every violation must have a stable diagnostic code, severity, evidence, and safe repair action.

## 7. Build End-to-End FastAPI Journey Tests

Tests must use the same APIs as the browser and assert state after every step.

Required journeys:

1. New project -> discovery -> decisions -> completion -> export.
2. UI-entered goal without `DESIGNFLOW.md`.
3. Stop during every workflow phase.
4. Server restart during every workflow phase.
5. Browser refresh while waiting for a decision.
6. Provider quota failure -> wait -> retry.
7. Provider failure -> auto-failover.
8. All providers unavailable.
9. Token-budget pause -> increase limit -> resume.
10. Invalid model output during drafting.
11. Invalid checkpoint during discovery and refinement.
12. Truncated model response.
13. Partial staged-artifact write.
14. Validation failure after refinement.
15. Export before completion.
16. Two browser sessions opening the same project.
17. Duplicate submit, retry, resume, answer, and stop requests.
18. SQLite interruption and WAL recovery.
19. Canonical promotion failure halfway through replacement.
20. Stopped or interrupted run -> staged preview -> resume -> completion.

After each action, assert:

- HTTP response and error code
- Authoritative workflow state
- Run and turn database rows
- Active checkpoint and decision records
- Staged artifact contents and manifest
- Canonical artifact contents and fingerprints
- Emitted events
- Available UI actions
- Restart outcome

## 8. Add Deterministic Fault Injection

Provide test hooks for:

- Provider timeout
- Rate limiting and quota exhaustion
- Malformed JSON and Markdown
- Missing protocol sections
- Truncated output
- Process termination
- SQLite busy and write failure
- Disk-full behavior
- Artifact replacement failure
- SSE disconnection
- Duplicate API submissions
- Browser refresh
- Invalid or expired session
- Stale provider state
- Configuration changes during an active run

Every injected failure must end in one bounded state: retry, failover, pause, stop, or explicit
failure. No test may hang, loop indefinitely, or require manual recovery.

## 9. Improve Diagnostics and Observability

Record for every run and transition:

- Run ID and turn ID
- State before and after
- Transition reason
- Provider identity and model
- Artifact fingerprints
- Prompt versions
- Capability-contract snapshot
- Recovery action
- Validation error codes
- Mutation outcome

Add a read-only endpoint:

```text
GET /run/diagnostics
```

It should report:

- Current authoritative state
- Active and stale run rows
- Current checkpoint
- Failed turn
- Staged-artifact status
- Canonical fingerprints
- Invariant violations
- Safe recovery actions

The UI should display these in a diagnostics view. Diagnostic warnings must never be converted
into product questions or model comments.

## 10. Make the UI Server-Driven

The server must explicitly provide the valid actions for the current state:

| State | UI actions |
| --- | --- |
| Product decision | Answer one option or provide a custom answer |
| Provider failure | Auto-failover, wait and retry, or stop |
| Interrupted run | Retry saved turn, preview staged draft, or stop |
| Budget pause | Increase limit or stop |
| Validation failure | Inspect failures or retry refinement |
| Completed | Export or begin a new refinement |

Remove client-side recovery inference based on error phrases, event history, or guessed status.

Every blocking view must explain:

- What happened
- Why it happened
- Whether work is safe
- Which actions are valid
- Which action is recommended

## 11. Add Project Doctor and Repair Tooling

Provide:

```text
designflow doctor <project>
designflow doctor <project> --repair
```

Checks must include:

- Stale active runs and turns
- Malformed or orphaned checkpoints
- Orphaned staged directories
- Invalid stage manifests
- Missing planning evidence
- Canonical/staged fingerprint mismatches
- Duplicate document sections
- Missing canonical artifacts
- Database integrity and WAL state
- Unsupported schema versions
- Invalid provider recovery state

Repairs must be deterministic, logged, backed up, and safe to repeat.

## 12. Establish Stabilization Release Gates

The release cannot be declared stable until:

- All end-to-end journey tests pass 20 consecutive times.
- Fault-injection tests contain no hangs or unbounded loops.
- Every blocking UI state presents a valid recovery action.
- Restart tests pass from every workflow phase.
- Two empty-folder projects complete as clean acceptance runs.
- No manual file or database repair is required.
- Exported plans pass deterministic validation.
- Debug Observer reports no unexplained high-severity events.
- A run can be stopped, restarted, resumed, completed, and exported without losing or duplicating work.

## Execution Order

### Phase 1: State Correctness

- Formal state machine
- Single-active-run enforcement
- Product-decision and recovery separation
- Startup reconciliation
- Workflow invariant auditor

### Phase 2: Artifact Correctness

- Typed model responses
- Strict staged-write discipline
- Deterministic validation
- Atomic canonical promotion
- Structural deduplication
- Artifact recovery tooling

### Phase 3: Recovery Correctness

- Provider failures
- Token-budget pauses
- Stop, restart, and resume
- Duplicate-request idempotency
- Multi-browser behavior

### Phase 4: Test Infrastructure

- End-to-end FastAPI journey harness
- Deterministic fault injection
- Repeated-run stability testing
- Database, event, and artifact assertions

### Phase 5: Usability and Acceptance

- Server-driven UI actions
- Diagnostics view
- `designflow doctor`
- Two fresh acceptance projects

## Definition of a Fixed Defect

A defect is fixed only when:

1. Its root invariant violation is identified.
2. The authoritative state transition is corrected.
3. Existing project data has a safe reconciliation or migration path.
4. The complete user journey becomes an automated API-level regression test.
5. The test proves database, runtime, staged artifacts, canonical artifacts, events, and visible
   recovery actions remain consistent.

Local wording changes and UI-only suppression do not qualify as complete fixes.
