from __future__ import annotations

import logging

from .idempotency import idempotency_key
from .models import (
    CorrectiveTransitionPayload, TERMINAL_STATES, TransitionPayload, WorkflowEvent, WorkflowSnapshot, WorkflowState,
    WorkflowTransition,
)
from .repository import WorkflowRepository


logger = logging.getLogger(__name__)


STATIC_TRANSITIONS: dict[tuple[WorkflowState, WorkflowEvent], WorkflowState] = {
    (WorkflowState.CREATED, WorkflowEvent.START): WorkflowState.DISCOVERING,
    (WorkflowState.DISCOVERING, WorkflowEvent.QUESTION_REQUIRED): WorkflowState.WAITING_FOR_USER,
    (WorkflowState.DISCOVERING, WorkflowEvent.DISCOVERY_COMPLETE): WorkflowState.DIVERGING,
    (WorkflowState.DIVERGING, WorkflowEvent.PROPOSALS_COMPLETE): WorkflowState.ANALYZING,
    (WorkflowState.DIVERGING, WorkflowEvent.REVIEW_REQUIRED): WorkflowState.WAITING_FOR_USER,
    (WorkflowState.ANALYZING, WorkflowEvent.NO_MATERIAL_CONFLICTS): WorkflowState.SYNTHESIZING,
    (WorkflowState.ANALYZING, WorkflowEvent.MATERIAL_CONFLICTS_FOUND): WorkflowState.RESOLVING,
    (WorkflowState.RESOLVING, WorkflowEvent.RESOLUTION_COMPLETE): WorkflowState.SYNTHESIZING,
    (WorkflowState.RESOLVING, WorkflowEvent.USER_CHOICE_REQUIRED): WorkflowState.WAITING_FOR_USER,
    (WorkflowState.SYNTHESIZING, WorkflowEvent.PROJECTIONS_CREATED): WorkflowState.VALIDATING,
    (WorkflowState.VALIDATING, WorkflowEvent.VALID): WorkflowState.COMPLETED,
    (WorkflowState.VALIDATING, WorkflowEvent.REPAIRABLE): WorkflowState.SYNTHESIZING,
    (WorkflowState.RETRYABLE_FAILURE, WorkflowEvent.RECOVERY_REQUIRED): WorkflowState.WAITING_FOR_RECOVERY,
}

CORRECTIVE_TARGETS: dict[WorkflowEvent, WorkflowState] = {
    WorkflowEvent.REOPEN_DISCOVERY: WorkflowState.DISCOVERING,
    WorkflowEvent.REDIVERGE: WorkflowState.DIVERGING,
    WorkflowEvent.RETURN_TO_ANALYSIS: WorkflowState.ANALYZING,
    WorkflowEvent.RESYNTHESIZE: WorkflowState.SYNTHESIZING,
}

CORRECTIVE_ORIGINS: dict[WorkflowEvent, set[WorkflowState]] = {
    WorkflowEvent.REOPEN_DISCOVERY: {
        WorkflowState.DIVERGING, WorkflowState.ANALYZING, WorkflowState.RESOLVING,
        WorkflowState.SYNTHESIZING, WorkflowState.VALIDATING, WorkflowState.COMPLETED,
    },
    WorkflowEvent.REDIVERGE: {
        WorkflowState.ANALYZING, WorkflowState.RESOLVING, WorkflowState.SYNTHESIZING,
        WorkflowState.VALIDATING, WorkflowState.COMPLETED,
    },
    WorkflowEvent.RETURN_TO_ANALYSIS: {
        WorkflowState.RESOLVING, WorkflowState.SYNTHESIZING,
        WorkflowState.VALIDATING, WorkflowState.COMPLETED,
    },
    WorkflowEvent.RESYNTHESIZE: {WorkflowState.VALIDATING, WorkflowState.COMPLETED},
}


class WorkflowEngine:
    def __init__(self, repository: WorkflowRepository):
        self.repository = repository

    def create(self, run_id: str) -> WorkflowSnapshot:
        return self.repository.create(run_id)

    def legal_events(self, run_id: str) -> list[WorkflowEvent]:
        current = self.repository.get(run_id)
        events = {event for (state, event), _ in STATIC_TRANSITIONS.items() if state == current.state}
        if current.state not in TERMINAL_STATES:
            events.update({WorkflowEvent.CANCEL, WorkflowEvent.FAIL, WorkflowEvent.PROVIDER_FAILURE})
        if current.state == WorkflowState.WAITING_FOR_USER:
            events.add(WorkflowEvent.ANSWER_RECORDED)
        if current.state == WorkflowState.WAITING_FOR_RECOVERY:
            events.add(WorkflowEvent.RETRY)
        events.update(event for event, origins in CORRECTIVE_ORIGINS.items() if current.state in origins)
        return sorted(events, key=lambda item: item.value)

    def transition(
        self, run_id: str, event: WorkflowEvent | str, payload: dict | None = None,
    ) -> WorkflowSnapshot:
        """Validate and atomically apply one legal, replay-safe workflow event.

        Payloads must be JSON objects. Logs contain transition metadata only; payloads
        are deliberately excluded because they may contain user or failure context.
        """
        event = WorkflowEvent(event)
        payload = TransitionPayload.model_validate({} if payload is None else payload).root
        current = self.repository.get(run_id)
        key = idempotency_key(run_id, current.state.value, event.value, payload)
        if self.repository.has_transition(run_id, key):
            return current
        corrective = event in CORRECTIVE_TARGETS
        if corrective:
            payload = CorrectiveTransitionPayload.model_validate(payload).model_dump(mode="json")
        if current.state in TERMINAL_STATES and not corrective:
            raise ValueError(f"terminal workflow {current.state.value} cannot transition")

        resume_state = None
        if corrective:
            if current.state not in CORRECTIVE_ORIGINS[event]:
                raise ValueError(f"event {event.value} is illegal from {current.state.value}")
            target = CORRECTIVE_TARGETS[event]
        elif event == WorkflowEvent.CANCEL:
            target = WorkflowState.CANCELLED
        elif event == WorkflowEvent.FAIL:
            target = WorkflowState.FAILED
        elif event == WorkflowEvent.PROVIDER_FAILURE:
            target = WorkflowState.RETRYABLE_FAILURE
            resume_state = current.state
        elif current.state == WorkflowState.WAITING_FOR_USER and event == WorkflowEvent.ANSWER_RECORDED:
            if not current.resume_state:
                raise ValueError("waiting workflow has no resume_state")
            target = current.resume_state
        elif current.state == WorkflowState.WAITING_FOR_RECOVERY and event == WorkflowEvent.RETRY:
            if not current.resume_state:
                raise ValueError("recovery workflow has no resume_state")
            target = current.resume_state
        else:
            target = STATIC_TRANSITIONS.get((current.state, event))
            if not target:
                raise ValueError(f"event {event.value} is illegal from {current.state.value}")

        if target == WorkflowState.WAITING_FOR_USER:
            requested_resume = payload.get("resume_state")
            if not requested_resume:
                raise ValueError("waiting-for-user transition requires resume_state")
            resume_state = WorkflowState(requested_resume)
        elif target == WorkflowState.WAITING_FOR_RECOVERY:
            resume_state = current.resume_state

        transition = WorkflowTransition(
            run_id=run_id,
            from_state=current.state,
            event=event,
            to_state=target,
            idempotency_key=key,
            payload=payload,
        )
        committed = (
            self.repository.commit_corrective_transition(transition)
            if corrective else self.repository.commit_transition(transition, resume_state=resume_state)
        )
        logger.info(
            "workflow transition run_id=%s from_state=%s event=%s to_state=%s key=%s",
            run_id, current.state.value, event.value, target.value, key[:12],
        )
        return committed
