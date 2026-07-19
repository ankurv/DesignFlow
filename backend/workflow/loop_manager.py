from __future__ import annotations

from enum import Enum

from .engine import WorkflowEngine
from .models import StrictModel, WorkflowEvent, WorkflowSnapshot, WorkflowState


class LoopCommandKind(str, Enum):
    START = "start"
    ASSESS_DISCOVERY = "assess_discovery"
    PRODUCE_PROPOSALS = "produce_proposals"
    ANALYZE = "analyze"
    RESOLVE = "resolve"
    SYNTHESIZE = "synthesize"
    VALIDATE = "validate"
    WAIT_FOR_USER = "wait_for_user"
    WAIT_FOR_RECOVERY = "wait_for_recovery"
    COMPLETE = "complete"


class LoopSignal(str, Enum):
    BEGIN = "begin"
    INPUT_REQUIRED = "input_required"
    DISCOVERY_ADEQUATE = "discovery_adequate"
    ANSWER_RECORDED = "answer_recorded"
    PROPOSALS_READY = "proposals_ready"
    REVIEW_REQUIRED = "review_required"
    MATERIAL_CONFLICTS = "material_conflicts"
    NO_MATERIAL_CONFLICTS = "no_material_conflicts"
    USER_CHOICE_REQUIRED = "user_choice_required"
    CONFLICTS_RESOLVED = "conflicts_resolved"
    PROJECTIONS_READY = "projections_ready"
    VALIDATION_PASSED = "validation_passed"
    VALIDATION_REPAIRABLE = "validation_repairable"
    FAILED = "failed"


EVENT_BY_STATE_AND_SIGNAL = {
    (WorkflowState.CREATED, LoopSignal.BEGIN): WorkflowEvent.START,
    (WorkflowState.DISCOVERING, LoopSignal.INPUT_REQUIRED): WorkflowEvent.QUESTION_REQUIRED,
    (WorkflowState.DISCOVERING, LoopSignal.DISCOVERY_ADEQUATE): WorkflowEvent.DISCOVERY_COMPLETE,
    (WorkflowState.WAITING_FOR_USER, LoopSignal.ANSWER_RECORDED): WorkflowEvent.ANSWER_RECORDED,
    (WorkflowState.DIVERGING, LoopSignal.PROPOSALS_READY): WorkflowEvent.PROPOSALS_COMPLETE,
    (WorkflowState.DIVERGING, LoopSignal.REVIEW_REQUIRED): WorkflowEvent.REVIEW_REQUIRED,
    (WorkflowState.ANALYZING, LoopSignal.MATERIAL_CONFLICTS): WorkflowEvent.MATERIAL_CONFLICTS_FOUND,
    (WorkflowState.ANALYZING, LoopSignal.NO_MATERIAL_CONFLICTS): WorkflowEvent.NO_MATERIAL_CONFLICTS,
    (WorkflowState.RESOLVING, LoopSignal.USER_CHOICE_REQUIRED): WorkflowEvent.USER_CHOICE_REQUIRED,
    (WorkflowState.RESOLVING, LoopSignal.CONFLICTS_RESOLVED): WorkflowEvent.RESOLUTION_COMPLETE,
    (WorkflowState.SYNTHESIZING, LoopSignal.PROJECTIONS_READY): WorkflowEvent.PROJECTIONS_CREATED,
    (WorkflowState.VALIDATING, LoopSignal.VALIDATION_PASSED): WorkflowEvent.VALID,
    (WorkflowState.VALIDATING, LoopSignal.VALIDATION_REPAIRABLE): WorkflowEvent.REPAIRABLE,
}


class LoopCommand(StrictModel):
    kind: LoopCommandKind
    state: WorkflowState
    state_version: int
    legal_events: list[WorkflowEvent]


COMMAND_BY_STATE = {
    WorkflowState.CREATED: LoopCommandKind.START,
    WorkflowState.DISCOVERING: LoopCommandKind.ASSESS_DISCOVERY,
    WorkflowState.DIVERGING: LoopCommandKind.PRODUCE_PROPOSALS,
    WorkflowState.ANALYZING: LoopCommandKind.ANALYZE,
    WorkflowState.RESOLVING: LoopCommandKind.RESOLVE,
    WorkflowState.SYNTHESIZING: LoopCommandKind.SYNTHESIZE,
    WorkflowState.VALIDATING: LoopCommandKind.VALIDATE,
    WorkflowState.WAITING_FOR_USER: LoopCommandKind.WAIT_FOR_USER,
    WorkflowState.WAITING_FOR_RECOVERY: LoopCommandKind.WAIT_FOR_RECOVERY,
    WorkflowState.RETRYABLE_FAILURE: LoopCommandKind.WAIT_FOR_RECOVERY,
    WorkflowState.COMPLETED: LoopCommandKind.COMPLETE,
}


class LoopManager:
    """Sole deterministic selector of the next orchestration command."""

    def __init__(self, engine: WorkflowEngine):
        self.engine = engine

    def select(self, snapshot: WorkflowSnapshot) -> LoopCommand:
        kind = COMMAND_BY_STATE.get(snapshot.state)
        if kind is None:
            raise ValueError(f"no loop command exists for {snapshot.state.value}")
        legal = self.engine.legal_events(snapshot.run_id)
        self._assert_state_invariants(snapshot, kind, legal)
        return LoopCommand(
            kind=kind, state=snapshot.state, state_version=snapshot.state_version,
            legal_events=legal,
        )

    def correct(self, run_id: str, event: WorkflowEvent, reason: str) -> WorkflowSnapshot:
        if event not in {
            WorkflowEvent.REOPEN_DISCOVERY, WorkflowEvent.REDIVERGE,
            WorkflowEvent.RETURN_TO_ANALYSIS, WorkflowEvent.RESYNTHESIZE,
        }:
            raise ValueError(f"{event.value} is not a corrective transition")
        if event not in self.engine.legal_events(run_id):
            current = self.engine.repository.get(run_id)
            raise ValueError(f"event {event.value} is illegal from {current.state.value}")
        return self.engine.transition(run_id, event, {"reason": reason})

    def advance(self, run_id: str, signal: LoopSignal, payload: dict | None = None) -> WorkflowSnapshot:
        snapshot = self.engine.repository.get(run_id)
        if signal == LoopSignal.FAILED:
            event = WorkflowEvent.FAIL
        else:
            event = EVENT_BY_STATE_AND_SIGNAL.get((snapshot.state, signal))
            if event is None:
                raise ValueError(f"signal {signal.value} is illegal from {snapshot.state.value}")
        if event not in self.engine.legal_events(run_id):
            raise ValueError(
                f"loop selected event {event.value}, but it is illegal from {snapshot.state.value}"
            )
        return self.engine.transition(run_id, event, payload)

    @staticmethod
    def _assert_state_invariants(
        snapshot: WorkflowSnapshot, kind: LoopCommandKind, legal: list[WorkflowEvent],
    ) -> None:
        if kind == LoopCommandKind.WAIT_FOR_USER and snapshot.resume_state is None:
            raise ValueError("WAITING_FOR_USER has no durable resume state")
        if kind == LoopCommandKind.WAIT_FOR_RECOVERY and snapshot.state == WorkflowState.WAITING_FOR_RECOVERY:
            if snapshot.resume_state is None:
                raise ValueError("WAITING_FOR_RECOVERY has no durable resume state")
        required = {
            LoopCommandKind.START: WorkflowEvent.START,
            LoopCommandKind.PRODUCE_PROPOSALS: WorkflowEvent.PROPOSALS_COMPLETE,
            LoopCommandKind.ANALYZE: WorkflowEvent.NO_MATERIAL_CONFLICTS,
            LoopCommandKind.RESOLVE: WorkflowEvent.RESOLUTION_COMPLETE,
            LoopCommandKind.SYNTHESIZE: WorkflowEvent.PROJECTIONS_CREATED,
            LoopCommandKind.VALIDATE: WorkflowEvent.VALID,
        }.get(kind)
        if required is not None and required not in legal:
            raise ValueError(
                f"state {snapshot.state.value} cannot execute {kind.value}: "
                f"required event {required.value} is illegal"
            )
