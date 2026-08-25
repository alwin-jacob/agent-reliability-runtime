"""Strict, versioned domain models for runtime state and evidence."""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from agent_runtime.versions import (
    ARTIFACT_SCHEMA_VERSION,
    RUN_CONFIG_SCHEMA_VERSION,
    TASK_SCHEMA_VERSION,
    ArtifactSchemaVersion,
    RunConfigSchemaVersion,
    TaskSchemaVersion,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PurchaseChannel = Literal["online", "store"]
DecisionCode = Literal["RETURN_ELIGIBLE", "RETURN_INELIGIBLE"]
_CALENDAR_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class StrictModel(BaseModel):
    """Base for durable domain values: strict inputs and no unknown fields."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RuntimePhase(StrEnum):
    INITIALIZED = "initialized"
    PLANNING = "planning"
    EXECUTING_WORKERS = "executing_workers"
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ALLOWED_TRANSITIONS: dict[RuntimePhase, set[RuntimePhase]] = {
    RuntimePhase.INITIALIZED: {RuntimePhase.PLANNING, RuntimePhase.CANCELLED},
    RuntimePhase.PLANNING: {
        RuntimePhase.EXECUTING_WORKERS,
        RuntimePhase.FAILED,
        RuntimePhase.CANCELLED,
    },
    RuntimePhase.EXECUTING_WORKERS: {
        RuntimePhase.FINALIZING,
        RuntimePhase.FAILED,
        RuntimePhase.CANCELLED,
    },
    RuntimePhase.FINALIZING: {
        RuntimePhase.SUCCEEDED,
        RuntimePhase.FAILED,
        RuntimePhase.CANCELLED,
    },
    RuntimePhase.SUCCEEDED: set(),
    RuntimePhase.FAILED: set(),
    RuntimePhase.CANCELLED: set(),
}


def is_valid_transition(current: RuntimePhase, target: RuntimePhase) -> bool:
    return target in _ALLOWED_TRANSITIONS[current]


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class FailureOrigin(StrEnum):
    CONFIGURATION = "configuration"
    MODEL_PROVIDER = "model_provider"
    MODEL_OUTPUT = "model_output"
    TOOL_POLICY = "tool_policy"
    TOOL_INPUT = "tool_input"
    TOOL_EXECUTION = "tool_execution"
    TOOL_OUTPUT = "tool_output"
    ORCHESTRATION = "orchestration"
    ARTIFACT = "artifact"
    CANCELLATION = "cancellation"
    UNEXPECTED_INTERNAL = "unexpected_internal"


class TaskSpec(StrictModel):
    schema_version: TaskSchemaVersion = TASK_SCHEMA_VERSION
    task_id: str = Field(min_length=1)
    customer_request: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    as_of_date: str
    item_condition: str = Field(min_length=1)
    market: str = Field(min_length=1)
    item_category: str = Field(min_length=1)
    purchase_channel: PurchaseChannel

    @field_validator("as_of_date")
    @classmethod
    def validate_as_of_date(cls, value: str) -> str:
        if _CALENDAR_DATE.fullmatch(value) is None:
            raise ValueError("as_of_date must use exact YYYY-MM-DD format")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("as_of_date must be a valid calendar date") from error
        if parsed.isoformat() != value:  # pragma: no cover - regex and fromisoformat are exact
            raise ValueError("as_of_date must use exact YYYY-MM-DD format")
        return value


class RetryPolicy(StrictModel):
    max_attempts: int = Field(ge=1, le=10)
    timeout_seconds: float = Field(gt=0, le=300)
    initial_backoff_seconds: float = Field(ge=0, le=60)
    max_backoff_seconds: float = Field(ge=0, le=300)
    jitter_ratio: float = Field(ge=0, le=0)

    @model_validator(mode="after")
    def validate_backoff(self) -> RetryPolicy:
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("max_backoff_seconds cannot be less than initial_backoff_seconds")
        return self


class RunConfig(StrictModel):
    schema_version: RunConfigSchemaVersion = RUN_CONFIG_SCHEMA_VERSION
    provider: Literal["fixture"]
    model_fixture: str = Field(min_length=1)
    order_fixture: str = Field(min_length=1)
    policy_fixture: str = Field(min_length=1)
    max_worker_concurrency: int = Field(ge=1, le=32)
    model_retry: RetryPolicy
    tool_retry: RetryPolicy


class FailureRecord(StrictModel):
    failure_id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    origin: FailureOrigin
    phase: RuntimePhase
    source_component: str = Field(min_length=1)
    retryable: bool
    attempt: int | None = Field(default=None, ge=1)
    message: str = Field(min_length=1)
    exception_type: str | None = None
    timestamp: str = Field(min_length=1)
    model_attempt_id: str | None = None
    tool_call_id: str | None = None
    span_id: str | None = None


class WorkerAssignment(StrictModel):
    worker_id: str = Field(min_length=1)
    allowed_tools: list[str] = Field(min_length=1)
    purpose: str = Field(min_length=1)


class SupervisorPlan(StrictModel):
    assignments: list[WorkerAssignment] = Field(min_length=1)


class WorkerResult(StrictModel):
    worker_id: str = Field(min_length=1)
    succeeded: bool
    tool_name: str | None = None
    output: dict[str, JsonValue] | None = None
    failure: FailureRecord | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> WorkerResult:
        if self.succeeded and (self.tool_name is None or self.output is None or self.failure):
            raise ValueError("successful worker result requires tool name/output and no failure")
        if not self.succeeded and self.failure is None:
            raise ValueError("failed worker result requires failure evidence")
        return self


class UsageRecord(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    token_source: Literal["provider_measured", "synthetic"] | None = None

    @model_validator(mode="after")
    def validate_token_source(self) -> UsageRecord:
        has_tokens = self.input_tokens is not None or self.output_tokens is not None
        if has_tokens and self.token_source is None:
            raise ValueError("token counts require an explicit token_source label")
        if not has_tokens and self.token_source is not None:
            raise ValueError("token_source requires at least one token count")
        return self


class ModelRequestRecord(StrictModel):
    request_id: str = Field(min_length=1)
    logical_turn_id: str = Field(min_length=1)
    source_component: str = Field(min_length=1)
    phase: RuntimePhase
    fixture_key: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    payload: dict[str, JsonValue]
    payload_sha256: Sha256


# Public compatibility name for the provider protocol. The durable record is the
# provider input; there is no separate ephemeral request model in the fixture adapter.
ModelRequest = ModelRequestRecord


class ModelResponse(StrictModel):
    response_id: str = Field(min_length=1)
    raw_json: str
    model_id: str = Field(min_length=1)
    provider: Literal["fixture"]
    finish_reason: str = Field(min_length=1)
    usage: UsageRecord
    metadata: dict[str, JsonValue]


class ModelAttempt(StrictModel):
    attempt_id: str = Field(min_length=1)
    logical_turn_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    source_component: str = Field(min_length=1)
    phase: RuntimePhase
    parent_span_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    outcome: AttemptOutcome
    started_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    duration_ms: float = Field(ge=0)
    response: ModelResponse | None = None
    failure: FailureRecord | None = None

    @model_validator(mode="after")
    def validate_outcome_evidence(self) -> ModelAttempt:
        if self.outcome == AttemptOutcome.SUCCEEDED:
            if self.response is None or self.failure is not None:
                raise ValueError("successful model attempt requires a response and no failure")
        elif self.response is not None or self.failure is None:
            raise ValueError("failed/timed-out model attempt requires failure and no response")
        return self


class ToolDefinition(StrictModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    side_effects: Literal["none"] = "none"
    idempotent: Literal[True] = True
    network_access: Literal[False] = False
    filesystem_access: Literal[False] = False


class ToolCall(StrictModel):
    call_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    input: dict[str, JsonValue]
    span_id: str = Field(min_length=1)


class WorkerToolRequest(StrictModel):
    tool_name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]


class ToolResult(StrictModel):
    result_id: str = Field(min_length=1)
    attempt_span_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    source_component: str = Field(min_length=1)
    phase: RuntimePhase
    parent_span_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    outcome: AttemptOutcome
    started_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    duration_ms: float = Field(ge=0)
    output: dict[str, JsonValue] | None = None
    failure: FailureRecord | None = None

    @model_validator(mode="after")
    def validate_outcome_evidence(self) -> ToolResult:
        if self.outcome == AttemptOutcome.SUCCEEDED:
            if self.output is None or self.failure is not None:
                raise ValueError("successful tool attempt requires output and no failure")
        elif self.output is not None or self.failure is None:
            raise ValueError("failed/timed-out tool attempt requires failure and no output")
        return self


class AgentEvent(StrictModel):
    event_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    timestamp: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    source_component: str = Field(min_length=1)
    phase: RuntimePhase
    span_id: str = Field(min_length=1)
    parent_span_id: str | None = None
    payload: dict[str, JsonValue]


class FinalDecision(StrictModel):
    eligible: bool
    decision_code: DecisionCode
    reason: str = Field(min_length=1)
    next_action: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    as_of_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    days_since_delivery: int = Field(ge=0)
    policy_window_days: int = Field(ge=0)
    applicable_fees: str = Field(min_length=1)
    evidence_worker_ids: list[str] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_evidence(self) -> FinalDecision:
        if len(self.evidence_worker_ids) != len(set(self.evidence_worker_ids)):
            raise ValueError("evidence_worker_ids must be unique")
        return self


class AgentState(StrictModel):
    phase: RuntimePhase
    task: TaskSpec
    plan: SupervisorPlan | None = None
    worker_results: list[WorkerResult]
    final_decision: FinalDecision | None = None


class Accounting(StrictModel):
    """Counts logical operations separately from retry attempts.

    A logical model turn is one runtime request for a model decision, excluding
    retries. A model attempt is each provider try. A logical tool call is one
    requested tool operation, while a tool attempt is each execution try.
    """

    logical_model_turns: int = Field(ge=0)
    model_attempts: int = Field(ge=0)
    successful_model_attempts: int = Field(ge=0)
    failed_model_attempts: int = Field(ge=0)
    logical_tool_calls: int = Field(ge=0)
    tool_attempts: int = Field(ge=0)
    successful_tool_attempts: int = Field(ge=0)
    failed_tool_attempts: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float = Field(ge=0)
    external_model_calls: bool


class ContentDigests(StrictModel):
    task_sha256: Sha256
    config_sha256: Sha256
    fixture_sha256: dict[str, Sha256]


class Provenance(StrictModel):
    package_version: str = Field(min_length=1)
    python_version: str = Field(min_length=1)
    platform_system: str = Field(min_length=1)
    machine_architecture: str = Field(min_length=1)
    langgraph_version: str = Field(min_length=1)
    dependency_lock_sha256: Sha256
    source_commit: str | None
    git_dirty: bool | None
    task_sha256: Sha256
    config_sha256: Sha256
    fixture_sha256: dict[str, Sha256]


class RunArtifact(StrictModel):
    schema_version: ArtifactSchemaVersion = ARTIFACT_SCHEMA_VERSION
    artifact_type: Literal["agent_runtime_run"] = "agent_runtime_run"
    run_id: str = Field(min_length=1)
    status: RunStatus
    started_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    task: TaskSpec
    run_config: RunConfig
    final_state: AgentState
    final_decision: FinalDecision | None
    events: list[AgentEvent]
    model_requests: list[ModelRequestRecord]
    model_attempts: list[ModelAttempt]
    tool_calls: list[ToolCall]
    tool_results: list[ToolResult]
    failures: list[FailureRecord]
    accounting: Accounting
    provenance: Provenance
    content_digests: ContentDigests
    configuration_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
