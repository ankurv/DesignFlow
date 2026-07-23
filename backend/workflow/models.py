from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowState(str, Enum):
    CREATED = "CREATED"
    DISCOVERING = "DISCOVERING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    DIVERGING = "DIVERGING"
    ANALYZING = "ANALYZING"
    RESOLVING = "RESOLVING"
    SYNTHESIZING = "SYNTHESIZING"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    WAITING_FOR_RECOVERY = "WAITING_FOR_RECOVERY"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


TERMINAL_STATES = {WorkflowState.COMPLETED, WorkflowState.CANCELLED, WorkflowState.FAILED}


class WorkflowEvent(str, Enum):
    START = "start"
    QUESTION_REQUIRED = "question_required"
    DISCOVERY_COMPLETE = "discovery_complete"
    ANSWER_RECORDED = "answer_recorded"
    PROPOSALS_COMPLETE = "all_required_proposals_stored"
    REVIEW_REQUIRED = "review_required"
    NO_MATERIAL_CONFLICTS = "no_material_conflicts"
    MATERIAL_CONFLICTS_FOUND = "material_conflicts_found"
    RESOLUTION_COMPLETE = "resolution_complete"
    USER_CHOICE_REQUIRED = "user_choice_required"
    PROJECTIONS_CREATED = "projections_created"
    VALID = "valid"
    REPAIRABLE = "repairable"
    PROVIDER_FAILURE = "provider_failure"
    RECOVERY_REQUIRED = "recovery_required"
    RETRY = "retry"
    CANCEL = "cancel"
    FAIL = "fail"
    REOPEN_DISCOVERY = "reopen_discovery"
    REDIVERGE = "rediverge"
    RETURN_TO_ANALYSIS = "return_to_analysis"
    RESYNTHESIZE = "resynthesize"


class WorkflowSnapshot(StrictModel):
    run_id: str
    state: WorkflowState
    resume_state: WorkflowState | None = None
    state_version: int = 1
    active_operation_id: str | None = None
    failure_code: str | None = None
    failure_detail: dict[str, Any] = Field(default_factory=dict)
    allowed_actions: list[str] = Field(default_factory=list)


class TransitionPayload(RootModel[dict[str, JsonValue]]):
    """JSON-object envelope for event-specific transition data.

    This model guarantees only that the root is an object and every nested value is
    JSON-serializable. Required keys are event-dependent and are validated by
    ``WorkflowEngine.transition``. The authoritative field matrix is documented in
    ``docs/STATE_MACHINE_CONTRACT.md`` under "Transition Payload Contract".
    """


class CorrectiveTransitionPayload(StrictModel):
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("reason")
    @classmethod
    def reason_is_meaningful(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("corrective transition reason cannot be blank")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_are_nonempty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("corrective transition evidence references cannot be blank")
        return values


class WorkflowTransition(StrictModel):
    run_id: str
    from_state: WorkflowState
    event: WorkflowEvent
    to_state: WorkflowState
    idempotency_key: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class WorkflowOperation(StrictModel):
    id: str
    run_id: str
    operation_type: str
    state: WorkflowState
    status: str
    attempt: int = Field(default=1, ge=1)
    input_ref: str | None = None
    output_ref: str | None = None
    error: dict[str, Any] = Field(default_factory=dict)


class ProposalComponent(StrictModel):
    name: str = Field(min_length=1)
    responsibility: str = Field(min_length=1)
    interfaces: list[str] = Field(default_factory=list)
    packaging: str = Field(default="")
    communication_protocol: str = Field(default="")
    data_store: str = Field(default="")
    api_contracts: list[str] = Field(default_factory=list)

    @field_validator("api_contracts", mode="before")
    @classmethod
    def _normalize_api_contracts(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        normalized = []
        for item in value:
            if isinstance(item, str):
                normalized.append(item.strip())
            elif isinstance(item, dict):
                methods = item.get("methods", [])
                method = ", ".join(methods) if isinstance(methods, list) else str(methods)
                endpoint = item.get("endpoint", item.get("path", item.get("name", "")))
                req = json.dumps(item.get("request_schema", item.get("request", {})), ensure_ascii=False)
                resp = json.dumps(item.get("response_schema", item.get("response", {})), ensure_ascii=False)
                if endpoint:
                    normalized.append(f"{method or 'API'} {endpoint} | Req: {req} | Resp: {resp}")
                else:
                    normalized.append(json.dumps(item, ensure_ascii=False))
        return [item for item in normalized if item]


class ProposalDecision(StrictModel):
    topic: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    alternatives: list[str] = Field(default_factory=list)


class ProposalRisk(StrictModel):
    risk: str = Field(min_length=1)
    mitigation: str = Field(min_length=1)


class ProposalUnknown(StrictModel):
    question: str = Field(min_length=1)
    validation: str = Field(min_length=1)


class ExpertProposal(StrictModel):
    components: list[ProposalComponent] = Field(default_factory=list)
    decisions: list[ProposalDecision] = Field(default_factory=list)
    risks: list[ProposalRisk] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unknowns: list[ProposalUnknown] = Field(default_factory=list)
    diagram: str = ""

    @field_validator("assumptions")
    @classmethod
    def assumptions_are_nonempty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("assumptions cannot contain empty strings")
        return values


class DebateChallenge(StrictModel):
    id: str = ""
    target_topic: str = ""
    claim: str = ""
    evidence: str = ""
    consequence: str = ""
    proposed_change: str = ""
    materiality: str
    authority_basis: str
    scope_effect: str
    related_challenge_id: str = ""
    relation: str

    @field_validator("materiality")
    @classmethod
    def valid_materiality(cls, value: str) -> str:
        val = (value or "").strip().lower()
        if val in {"critical", "severe", "major"}:
            return "high"
        if val in {"minor", "trivial"}:
            return "low"
        if val in {"low", "medium", "high"}:
            return val
        return "medium"

    @field_validator("authority_basis")
    @classmethod
    def valid_authority_basis(cls, value: str) -> str:
        val = (value or "").strip().lower()
        valid = {"explicit_requirement", "confirmed_decision", "repository_evidence", "assumption", "expert_judgment"}
        if val in valid:
            return val
        for v in valid:
            if v in val or val in v:
                return v
        return "expert_judgment"

    @field_validator("scope_effect")
    @classmethod
    def valid_scope_effect(cls, value: str) -> str:
        val = (value or "").strip().lower()
        valid = {"preserves", "clarifies", "expands", "changes"}
        if val in valid:
            return val
        if "expand" in val or "add" in val:
            return "expands"
        if "change" in val or "modify" in val:
            return "changes"
        if "clarif" in val:
            return "clarifies"
        return "preserves"

    @field_validator("relation")
    @classmethod
    def valid_relation(cls, value: str) -> str:
        val = (value or "").strip().lower()
        valid = {"distinct", "refines", "contradicts"}
        if val in valid:
            return val
        if "refin" in val:
            return "refines"
        if "contradict" in val or "oppose" in val:
            return "contradicts"
        return "distinct"


class DebateReview(StrictModel):
    challenges: list[DebateChallenge] = Field(default_factory=list)
    validated_topics: list[str] = Field(default_factory=list)


class ChallengeDisposition(StrictModel):
    challenge_id: str = Field(min_length=1)
    status: str
    rationale: str = Field(min_length=1)
    resulting_decision: str = Field(min_length=1)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        val = (value or "").strip().lower()
        valid = {"accepted", "defended", "merged", "unresolved"}
        if val in valid:
            return val
        if "accept" in val or "agree" in val:
            return "accepted"
        if "defend" in val or "reject" in val:
            return "defended"
        if "merge" in val:
            return "merged"
        return "defended"


class DebateRevision(StrictModel):
    proposal: ExpertProposal
    dispositions: list[ChallengeDisposition]




class DiscoveryAssessment(StrictModel):
    adequate: bool
    evidence_summary: str = Field(default="Discovery evidence assessment completed", min_length=1)
    provisional_assumptions: list[str] = Field(default_factory=list)
    blocking_questions: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("provisional_assumptions")
    @classmethod
    def assumptions_are_nonempty(cls, values: list[str]) -> list[str]:
        return [v.strip() for v in values if v and v.strip()]

    @field_validator("blocking_questions")
    @classmethod
    def questions_match_verdict(cls, values: list[str], info):
        clean_q = [v.strip() for v in values if v and v.strip()]
        adequate = bool(info.data.get("adequate"))
        if adequate and clean_q:
            info.data["adequate"] = False
        return clean_q[:3]


class ContextItem(StrictModel):
    id: str
    text: str
    source_type: str
    source_id: str
    priority: int = Field(ge=1, le=6)
    relevance: float = Field(default=0.0, ge=0.0, le=1.0)


class ContextPacket(StrictModel):
    goal: str
    constraints: list[str] = Field(default_factory=list)
    confirmed_decisions: list[str] = Field(default_factory=list)
    operation_instructions: str
    unresolved_conflicts: list[ContextItem] = Field(default_factory=list)
    relevant_summaries: list[ContextItem] = Field(default_factory=list)
    raw_evidence: list[ContextItem] = Field(default_factory=list)
    provenance: list[dict[str, str]] = Field(default_factory=list)
    estimated_tokens: int = Field(default=0, ge=0)


class PlanningClaim(StrictModel):
    id: str
    proposal_id: str
    claim_type: str
    topic: str
    text: str
    status: str = "candidate"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class PlanningConflict(StrictModel):
    id: str
    topic: str
    claim_ids: list[str] = Field(min_length=2)
    options: list[str] = Field(min_length=2)
    materiality: str
    status: str = "open"
    resolution: str | None = None
    resolution_source: str | None = None
