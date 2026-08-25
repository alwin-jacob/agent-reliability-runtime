"""Artifact fingerprints, semantic validation, and atomic persistence."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

from agent_runtime.domain import (
    Accounting,
    AgentEvent,
    AttemptOutcome,
    ContentDigests,
    FailureOrigin,
    FailureRecord,
    FinalDecision,
    ModelAttempt,
    ModelRequestRecord,
    RetryPolicy,
    RunArtifact,
    RunConfig,
    RunStatus,
    RuntimePhase,
    SupervisorPlan,
    TaskSpec,
    ToolCall,
    ToolResult,
    WorkerToolRequest,
    is_valid_transition,
)
from agent_runtime.errors import ArtifactError
from agent_runtime.integrity import canonical_json, canonical_sha256, sha256_bytes
from agent_runtime.semantics import (
    CANONICAL_EVIDENCE_WORKER_IDS,
    ORDER_WORKER_ID,
    POLICY_WORKER_ID,
    REQUIRED_WORKER_IDS,
    SemanticValidationError,
    validate_stage_one_plan,
    validate_successful_decision,
    validate_worker_tool_request,
)
from agent_runtime.tools.retail import LookupOrderOutput, LookupReturnPolicyOutput

_PRIVATE_PATH = re.compile(r"(?:/(?:Users|home)/[^/\s]+|[A-Za-z]:\\Users\\[^\\\s]+)")
_SECRET_VALUE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{12,}\b|\bAKIA[0-9A-Z]{16}\b|\bBearer\s+[A-Za-z0-9._~+/-]{12,})"
)
_ALLOWED_EVENT_TYPES = frozenset(
    {
        "run_start",
        "state_transition",
        "supervisor_planning_start",
        "supervisor_planning_end",
        "worker_dispatch",
        "worker_start",
        "worker_end",
        "worker_failure",
        "model_attempt_start",
        "model_attempt_end",
        "model_attempt_failure",
        "model_attempt_timeout",
        "tool_attempt_start",
        "tool_attempt_end",
        "tool_attempt_failure",
        "tool_attempt_timeout",
        "supervisor_finalization_start",
        "supervisor_finalization_end",
        "run_success",
        "run_failure",
        "run_cancellation",
        "artifact_persistence_start",
    }
)
_FIXTURE_DIGEST_KEYS = frozenset({"model", "orders", "policies"})


def configuration_fingerprint(digests: ContentDigests, task: TaskSpec, config: RunConfig) -> str:
    return sha256_bytes(
        canonical_json(
            {
                "content_digests": digests.model_dump(mode="json"),
                "task": task.model_dump(mode="json"),
                "run_config": config.model_dump(mode="json"),
            }
        )
    )


def build_accounting(
    model_requests: list[ModelRequestRecord],
    model_attempts: list[ModelAttempt],
    tool_calls: list[ToolCall],
    tool_results: list[ToolResult],
) -> Accounting:
    model_successes = sum(item.outcome == AttemptOutcome.SUCCEEDED for item in model_attempts)
    tool_successes = sum(item.outcome == AttemptOutcome.SUCCEEDED for item in tool_results)
    input_values = [
        item.response.usage.input_tokens
        for item in model_attempts
        if item.response is not None and item.response.usage.input_tokens is not None
    ]
    output_values = [
        item.response.usage.output_tokens
        for item in model_attempts
        if item.response is not None and item.response.usage.output_tokens is not None
    ]
    return Accounting(
        logical_model_turns=len(model_requests),
        model_attempts=len(model_attempts),
        successful_model_attempts=model_successes,
        failed_model_attempts=len(model_attempts) - model_successes,
        logical_tool_calls=len(tool_calls),
        tool_attempts=len(tool_results),
        successful_tool_attempts=tool_successes,
        failed_tool_attempts=len(tool_results) - tool_successes,
        input_tokens=sum(input_values) if input_values else None,
        output_tokens=sum(output_values) if output_values else None,
        cost_usd=round(
            sum(
                item.response.usage.cost_usd for item in model_attempts if item.response is not None
            ),
            12,
        ),
        external_model_calls=any(
            item.response is not None and item.response.provider != "fixture"
            for item in model_attempts
        ),
    )


def semantic_fingerprint(
    *,
    digests: ContentDigests,
    final_decision: FinalDecision | None,
    events: list[AgentEvent],
    model_requests: list[ModelRequestRecord],
    model_attempts: list[ModelAttempt],
    tool_calls: list[ToolCall],
    tool_results: list[ToolResult],
    failures: list[FailureRecord],
    accounting: Accounting,
) -> str:
    request_by_id = {item.request_id: item for item in model_requests}
    model_outcomes = []
    for item in sorted(model_attempts, key=lambda value: (value.logical_turn_id, value.attempt)):
        request = request_by_id.get(item.request_id)
        raw: Any = None
        if item.response is not None:
            try:
                raw = json.loads(item.response.raw_json)
            except json.JSONDecodeError:
                raw = item.response.raw_json
        model_outcomes.append(
            {
                "turn": item.logical_turn_id,
                "source": item.source_component,
                "phase": item.phase.value,
                "fixture_key": request.fixture_key if request else None,
                "request_payload_sha256": request.payload_sha256 if request else None,
                "attempt": item.attempt,
                "outcome": item.outcome.value,
                "raw": raw,
                "failure_code": item.failure.code if item.failure else None,
            }
        )
    call_by_id = {item.call_id: item for item in tool_calls}
    tool_outcomes = [
        {
            "worker": item.worker_id,
            "tool": item.tool_name,
            "input": call_by_id[item.tool_call_id].input,
            "attempt": item.attempt,
            "outcome": item.outcome.value,
            "output": item.output,
            "failure_code": item.failure.code if item.failure else None,
        }
        for item in sorted(tool_results, key=lambda value: (value.worker_id, value.attempt))
    ]
    payload = {
        "content_digests": digests.model_dump(mode="json"),
        "final_decision": final_decision.model_dump(mode="json") if final_decision else None,
        "model_requests": [
            {
                "logical_turn_id": item.logical_turn_id,
                "source_component": item.source_component,
                "phase": item.phase.value,
                "fixture_key": item.fixture_key,
                "payload_sha256": item.payload_sha256,
            }
            for item in sorted(model_requests, key=lambda value: value.logical_turn_id)
        ],
        "model_outcomes": model_outcomes,
        "tool_outcomes": tool_outcomes,
        "event_types_and_sources": sorted(
            (item.event_type, item.source_component) for item in events
        ),
        "failure_codes": sorted(item.code for item in failures),
        "accounting": accounting.model_dump(mode="json"),
    }
    return sha256_bytes(canonical_json(payload))


def attach_content_hash(artifact: RunArtifact) -> RunArtifact:
    payload = artifact.model_dump(mode="json")
    payload.pop("content_sha256")
    return artifact.model_copy(update={"content_sha256": sha256_bytes(canonical_json(payload))})


def validate_artifact(artifact: RunArtifact) -> None:
    payload = artifact.model_dump(mode="json")
    schema_errors = sorted(
        Draft202012Validator(RunArtifact.model_json_schema()).iter_errors(payload),
        key=lambda item: list(item.path),
    )
    if schema_errors:
        raise ArtifactError(f"artifact JSON Schema validation failed: {schema_errors[0].message}")
    without_hash = dict(payload)
    without_hash.pop("content_sha256")
    expected_hash = sha256_bytes(canonical_json(without_hash))
    if artifact.content_sha256 != expected_hash:
        raise ArtifactError("artifact content_sha256 does not match canonical content")
    _reject_private_or_secret_data(payload)
    if artifact.task != artifact.final_state.task:
        raise ArtifactError("artifact task does not match final_state.task")
    if artifact.final_decision != artifact.final_state.final_decision:
        raise ArtifactError("artifact final_decision does not match final_state.final_decision")
    sequences = [event.sequence for event in artifact.events]
    if sequences != list(range(1, len(sequences) + 1)):
        raise ArtifactError("artifact event sequences must be unique and strictly increasing")
    _validate_unique_identifiers(artifact)
    _validate_event_vocabulary(artifact)
    _validate_span_identities(artifact)
    expected_accounting = build_accounting(
        artifact.model_requests,
        artifact.model_attempts,
        artifact.tool_calls,
        artifact.tool_results,
    )
    if artifact.accounting != expected_accounting:
        raise ArtifactError("artifact accounting does not reconcile with attempt records")
    if artifact.status == RunStatus.SUCCEEDED:
        if artifact.final_decision is None or artifact.final_state.final_decision is None:
            raise ArtifactError("successful artifact requires a final decision")
        if artifact.final_state.phase != RuntimePhase.SUCCEEDED:
            raise ArtifactError("successful artifact requires succeeded final state")
        if artifact.final_state.plan is None:
            raise ArtifactError("successful artifact requires a persisted Stage 1 plan")
    else:
        if not artifact.failures:
            raise ArtifactError("failed or cancelled artifact requires FailureRecord evidence")
        if artifact.final_decision is not None and not any(
            item.logical_turn_id == "turn-supervisor-finalize" for item in artifact.model_requests
        ):
            raise ArtifactError(
                "failed or cancelled artifact must not contain a final decision without "
                "accepted finalizer evidence"
            )
        expected_phase = (
            RuntimePhase.FAILED if artifact.status == RunStatus.FAILED else RuntimePhase.CANCELLED
        )
        if artifact.final_state.phase != expected_phase:
            raise ArtifactError("artifact status does not match final runtime phase")
    expected_config = configuration_fingerprint(
        artifact.content_digests, artifact.task, artifact.run_config
    )
    if artifact.configuration_fingerprint != expected_config:
        raise ArtifactError("artifact configuration fingerprint is inconsistent")
    if set(artifact.content_digests.fixture_sha256) != _FIXTURE_DIGEST_KEYS:
        raise ArtifactError("fixture digest keys must be exactly model, orders, and policies")
    if artifact.status == RunStatus.SUCCEEDED:
        _validate_success_records(artifact)
    _validate_request_and_attempt_trace(artifact)
    _validate_timestamps(artifact)
    _validate_fixture_response_evidence(artifact)
    _validate_successful_tool_outputs(artifact)
    _validate_worker_evidence(artifact)
    _validate_response_action_causality(artifact)
    _validate_failure_graph(artifact)
    _validate_required_events(artifact)
    expected_semantic = semantic_fingerprint(
        digests=artifact.content_digests,
        final_decision=artifact.final_decision,
        events=artifact.events,
        model_requests=artifact.model_requests,
        model_attempts=artifact.model_attempts,
        tool_calls=artifact.tool_calls,
        tool_results=artifact.tool_results,
        failures=artifact.failures,
        accounting=artifact.accounting,
    )
    if artifact.semantic_fingerprint != expected_semantic:
        raise ArtifactError("artifact semantic fingerprint is inconsistent")
    if (
        artifact.provenance.task_sha256 != artifact.content_digests.task_sha256
        or artifact.provenance.config_sha256 != artifact.content_digests.config_sha256
        or artifact.provenance.fixture_sha256 != artifact.content_digests.fixture_sha256
    ):
        raise ArtifactError("artifact provenance digests are inconsistent")


def _validate_request_and_attempt_trace(artifact: RunArtifact) -> None:
    requests_by_id = {item.request_id: item for item in artifact.model_requests}
    model_groups: dict[str, list[ModelAttempt]] = defaultdict(list)
    model_event_types = {
        "model_attempt_start",
        "model_attempt_end",
        "model_attempt_failure",
        "model_attempt_timeout",
    }
    tool_event_types = {
        "tool_attempt_start",
        "tool_attempt_end",
        "tool_attempt_failure",
        "tool_attempt_timeout",
    }

    for request in artifact.model_requests:
        if request.payload_sha256 != canonical_sha256(request.payload):
            raise ArtifactError("model request payload_sha256 does not match canonical payload")

    for attempt in artifact.model_attempts:
        model_groups[attempt.request_id].append(attempt)
    for attempts in model_groups.values():
        numbers = sorted(item.attempt for item in attempts)
        if numbers != list(range(1, len(numbers) + 1)):
            raise ArtifactError("model attempt numbers must be contiguous from 1")

    for attempt in artifact.model_attempts:
        request_record = requests_by_id.get(attempt.request_id)
        if request_record is None:
            raise ArtifactError("model attempt references an absent durable request")
        if (
            attempt.logical_turn_id != request_record.logical_turn_id
            or attempt.source_component != request_record.source_component
            or attempt.phase != request_record.phase
        ):
            raise ArtifactError("model attempt context does not match its durable request")
        if attempt.outcome == AttemptOutcome.SUCCEEDED:
            if attempt.response is None or attempt.failure is not None:
                raise ArtifactError("successful model attempt has contradictory evidence")
        elif attempt.response is not None or attempt.failure is None:
            raise ArtifactError("failed model attempt has contradictory evidence")
        if _parse_utc_timestamp(
            attempt.started_at, "model attempt started_at"
        ) > _parse_utc_timestamp(attempt.completed_at, "model attempt completed_at"):
            raise ArtifactError("model attempt started_at must not be after completed_at")
        events = [
            item
            for item in artifact.events
            if item.span_id == attempt.attempt_id and item.event_type in model_event_types
        ]
        starts = [item for item in events if item.event_type == "model_attempt_start"]
        terminals = [item for item in events if item.event_type != "model_attempt_start"]
        if len(starts) != 1 or len(terminals) != 1:
            raise ArtifactError("model attempt requires exactly one start and one terminal event")
        terminal_type = {
            AttemptOutcome.SUCCEEDED: "model_attempt_end",
            AttemptOutcome.FAILED: "model_attempt_failure",
            AttemptOutcome.TIMED_OUT: "model_attempt_timeout",
        }[attempt.outcome]
        if terminals[0].event_type != terminal_type:
            raise ArtifactError("model attempt terminal event contradicts its outcome")
        expected_start = {
            "attempt": attempt.attempt,
            "logical_turn_id": attempt.logical_turn_id,
            "request_id": attempt.request_id,
        }
        expected_terminal = {
            **expected_start,
            "outcome": attempt.outcome.value,
            "failure_id": attempt.failure.failure_id if attempt.failure else None,
            "failure_code": attempt.failure.code if attempt.failure else None,
        }
        _validate_attempt_event(starts[0], attempt, expected_start)
        _validate_attempt_event(terminals[0], attempt, expected_terminal)
        if starts[0].sequence >= terminals[0].sequence:
            raise ArtifactError("model attempt terminal event must follow its start event")

    attempt_ids = {item.attempt_id for item in artifact.model_attempts}
    if any(
        event.event_type in model_event_types and event.span_id not in attempt_ids
        for event in artifact.events
    ):
        raise ArtifactError("artifact contains an orphan model-attempt event")

    for request in artifact.model_requests:
        attempts = sorted(model_groups.get(request.request_id, []), key=lambda item: item.attempt)
        _validate_effective_retry_group(
            attempts,
            artifact.run_config.model_retry,
            artifact.status,
            label="model",
            timeout_code="model_attempt_timeout",
            timeout_origin=FailureOrigin.MODEL_PROVIDER,
        )
        created = _parse_utc_timestamp(request.created_at, "model request created_at")
        if attempts and created > _parse_utc_timestamp(
            attempts[0].started_at, "model attempt started_at"
        ):
            raise ArtifactError("model request must be created before its first provider attempt")
        numbers = [item.attempt for item in attempts]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ArtifactError("model attempt numbers must be contiguous from 1")
        model_successes = [item for item in attempts if item.outcome == AttemptOutcome.SUCCEEDED]
        if len(model_successes) > 1 or (model_successes and attempts[-1] != model_successes[0]):
            raise ArtifactError("successful model attempt must be unique and terminal")
        previous_terminal_sequence: int | None = None
        previous_completed_at: datetime | None = None
        for attempt in attempts:
            start_event = next(
                item
                for item in artifact.events
                if item.span_id == attempt.attempt_id and item.event_type == "model_attempt_start"
            )
            terminal_event = next(
                item
                for item in artifact.events
                if item.span_id == attempt.attempt_id
                and item.event_type != "model_attempt_start"
                and item.event_type in model_event_types
            )
            started = _parse_utc_timestamp(attempt.started_at, "model attempt started_at")
            if (
                previous_terminal_sequence is not None
                and previous_terminal_sequence >= start_event.sequence
            ) or (previous_completed_at is not None and previous_completed_at > started):
                raise ArtifactError(
                    "model retry attempt must start after the previous attempt terminates"
                )
            previous_terminal_sequence = terminal_event.sequence
            previous_completed_at = _parse_utc_timestamp(
                attempt.completed_at, "model attempt completed_at"
            )

    call_by_id = {item.call_id: item for item in artifact.tool_calls}
    tool_groups: dict[str, list[ToolResult]] = defaultdict(list)
    for result in artifact.tool_results:
        tool_groups[result.tool_call_id].append(result)
    for tool_attempts in tool_groups.values():
        numbers = sorted(item.attempt for item in tool_attempts)
        if numbers != list(range(1, len(numbers) + 1)):
            raise ArtifactError("tool attempt numbers must be contiguous from 1")
    for result in artifact.tool_results:
        call = call_by_id.get(result.tool_call_id)
        if call is None:
            raise ArtifactError("tool result references an unknown logical tool call")
        if (
            result.worker_id != call.worker_id
            or result.tool_name != call.tool_name
            or result.source_component != call.tool_name
            or result.phase != RuntimePhase.EXECUTING_WORKERS
            or result.parent_span_id != call.span_id
        ):
            raise ArtifactError("tool result context does not match its logical tool call")
        if result.outcome == AttemptOutcome.SUCCEEDED:
            if result.output is None or result.failure is not None:
                raise ArtifactError("successful tool attempt has contradictory evidence")
        elif result.output is not None or result.failure is None:
            raise ArtifactError("failed tool attempt has contradictory evidence")
        events = [
            item
            for item in artifact.events
            if item.span_id == result.attempt_span_id and item.event_type in tool_event_types
        ]
        starts = [item for item in events if item.event_type == "tool_attempt_start"]
        terminals = [item for item in events if item.event_type != "tool_attempt_start"]
        if len(starts) != 1 or len(terminals) != 1:
            raise ArtifactError("tool attempt requires exactly one start and one terminal event")
        terminal_type = {
            AttemptOutcome.SUCCEEDED: "tool_attempt_end",
            AttemptOutcome.FAILED: "tool_attempt_failure",
            AttemptOutcome.TIMED_OUT: "tool_attempt_timeout",
        }[result.outcome]
        if terminals[0].event_type != terminal_type:
            raise ArtifactError("tool attempt terminal event contradicts its outcome")
        expected_start = {
            "attempt": result.attempt,
            "tool_call_id": result.tool_call_id,
            "worker_id": result.worker_id,
            "tool_name": result.tool_name,
        }
        expected_terminal = {
            **expected_start,
            "outcome": result.outcome.value,
            "failure_id": result.failure.failure_id if result.failure else None,
            "failure_code": result.failure.code if result.failure else None,
        }
        _validate_tool_attempt_event(starts[0], result, expected_start)
        _validate_tool_attempt_event(terminals[0], result, expected_terminal)
        if starts[0].sequence >= terminals[0].sequence:
            raise ArtifactError("tool attempt terminal event must follow its start event")

    result_spans = {item.attempt_span_id for item in artifact.tool_results}
    if any(
        event.event_type in tool_event_types and event.span_id not in result_spans
        for event in artifact.events
    ):
        raise ArtifactError("artifact contains an orphan tool-attempt event")
    for tool_attempts in tool_groups.values():
        ordered = sorted(tool_attempts, key=lambda item: item.attempt)
        _validate_effective_retry_group(
            ordered,
            artifact.run_config.tool_retry,
            artifact.status,
            label="tool",
            timeout_code="tool_attempt_timeout",
            timeout_origin=FailureOrigin.TOOL_EXECUTION,
        )
        numbers = [item.attempt for item in ordered]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ArtifactError("tool attempt numbers must be contiguous from 1")
        tool_successes = [item for item in ordered if item.outcome == AttemptOutcome.SUCCEEDED]
        if len(tool_successes) > 1 or (tool_successes and ordered[-1] != tool_successes[0]):
            raise ArtifactError("successful tool attempt must be unique and terminal")
        previous_terminal_sequence = None
        previous_completed_at = None
        for result in ordered:
            start_event = next(
                item
                for item in artifact.events
                if item.span_id == result.attempt_span_id
                and item.event_type == "tool_attempt_start"
            )
            terminal_event = next(
                item
                for item in artifact.events
                if item.span_id == result.attempt_span_id
                and item.event_type != "tool_attempt_start"
                and item.event_type in tool_event_types
            )
            started = _parse_utc_timestamp(result.started_at, "tool result started_at")
            if (
                previous_terminal_sequence is not None
                and previous_terminal_sequence >= start_event.sequence
            ) or (previous_completed_at is not None and previous_completed_at > started):
                raise ArtifactError(
                    "tool retry attempt must start after the previous attempt terminates"
                )
            previous_terminal_sequence = terminal_event.sequence
            previous_completed_at = _parse_utc_timestamp(
                result.completed_at, "tool result completed_at"
            )


AttemptRecord = TypeVar("AttemptRecord", ModelAttempt, ToolResult)


def _validate_effective_retry_group(
    attempts: list[AttemptRecord],
    policy: RetryPolicy,
    status: RunStatus,
    *,
    label: str,
    timeout_code: str,
    timeout_origin: FailureOrigin,
) -> None:
    """Apply one effective RunConfig retry policy to one logical invocation group."""

    numbers = [item.attempt for item in attempts]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ArtifactError(f"{label} attempt numbers must be contiguous from 1")
    if len(attempts) > policy.max_attempts:
        raise ArtifactError(f"{label} attempts exceed effective max_attempts")

    successes = [item for item in attempts if item.outcome == AttemptOutcome.SUCCEEDED]
    if len(successes) > 1 or (successes and successes[0] != attempts[-1]):
        raise ArtifactError(f"successful {label} attempt must be unique and terminal")

    for index, attempt in enumerate(attempts):
        failure = attempt.failure
        if (
            failure is not None
            and (
                failure.code == "invocation_cancelled"
                or failure.origin == FailureOrigin.CANCELLATION
                or failure.exception_type == "CancelledError"
            )
            and attempt.outcome != AttemptOutcome.FAILED
        ):
            raise ArtifactError(f"{label} invocation cancellation evidence is inconsistent")
        if attempt.outcome == AttemptOutcome.TIMED_OUT:
            if (
                failure is None
                or failure.code != timeout_code
                or failure.origin != timeout_origin
                or not failure.retryable
                or failure.exception_type != "TimeoutError"
            ):
                raise ArtifactError(f"{label} timeout has inconsistent runtime taxonomy")
        elif failure is not None:
            _validate_failed_attempt_taxonomy(
                failure,
                attempt.outcome,
                index=index,
                attempt_count=len(attempts),
                label=label,
                timeout_code=timeout_code,
            )
        if index < len(attempts) - 1:
            if failure is None or not failure.retryable:
                raise ArtifactError(f"nonterminal {label} attempt must have a retryable failure")

    if (
        status == RunStatus.FAILED
        and attempts
        and attempts[-1].outcome != AttemptOutcome.SUCCEEDED
        and attempts[-1].failure is not None
        and attempts[-1].failure.retryable
        and len(attempts) < policy.max_attempts
    ):
        raise ArtifactError(
            f"failed run stopped on retryable {label} failure before configured maximum"
        )


def _validate_failed_attempt_taxonomy(
    failure: FailureRecord,
    outcome: AttemptOutcome,
    *,
    index: int,
    attempt_count: int,
    label: str,
    timeout_code: str,
) -> None:
    """Mirror the exact failure boundaries reachable inside invoke_with_policy()."""

    is_terminal = index == attempt_count - 1
    if outcome != AttemptOutcome.FAILED:
        raise ArtifactError(f"{label} failure has an invalid attempt outcome")
    if failure.code == timeout_code:
        raise ArtifactError(f"{label} timeout code is reserved for the timed_out outcome")

    has_cancellation_marker = (
        failure.code == "invocation_cancelled"
        or failure.origin == FailureOrigin.CANCELLATION
        or failure.exception_type == "CancelledError"
    )
    if has_cancellation_marker:
        if (
            failure.code != "invocation_cancelled"
            or failure.origin != FailureOrigin.CANCELLATION
            or failure.retryable
            or failure.exception_type != "CancelledError"
            or not is_terminal
        ):
            raise ArtifactError(f"{label} invocation cancellation evidence is inconsistent")
        return

    if failure.code == "unexpected_invocation_error":
        if (
            failure.origin != FailureOrigin.UNEXPECTED_INTERNAL
            or failure.retryable
            or not is_terminal
        ):
            raise ArtifactError(
                f"{label} unexpected invocation error has inconsistent retry taxonomy"
            )
        return

    if failure.origin == FailureOrigin.UNEXPECTED_INTERNAL:
        if (
            failure.code != "transient_infrastructure"
            or failure.exception_type != "TransientInfrastructureError"
            or not failure.retryable
        ):
            raise ArtifactError(
                f"{label} unexpected-internal failure is not the typed transient taxonomy"
            )
        return

    if label == "model":
        if (
            failure.origin != FailureOrigin.MODEL_PROVIDER
            or failure.exception_type != "ProviderError"
        ):
            raise ArtifactError("model failure must use the provider-error runtime taxonomy")
        return

    if failure.origin == FailureOrigin.TOOL_EXECUTION:
        if failure.exception_type != "ToolExecutionError":
            raise ArtifactError("tool-execution failure has inconsistent runtime taxonomy")
        return
    if failure.origin == FailureOrigin.TOOL_OUTPUT:
        if failure.exception_type != "ToolOutputError" or failure.retryable or not is_terminal:
            raise ArtifactError(
                "tool-output failure must be nonretryable and terminal in runtime taxonomy"
            )
        return
    raise ArtifactError("tool failure must use tool-execution or tool-output runtime taxonomy")


def _validate_attempt_event(
    event: AgentEvent, attempt: ModelAttempt, expected_payload: dict[str, Any]
) -> None:
    if (
        event.source_component != attempt.source_component
        or event.phase != attempt.phase
        or event.parent_span_id != attempt.parent_span_id
        or event.payload != expected_payload
    ):
        raise ArtifactError("model-attempt event context or payload contradicts its record")


def _validate_tool_attempt_event(
    event: AgentEvent, result: ToolResult, expected_payload: dict[str, Any]
) -> None:
    if (
        event.source_component != result.source_component
        or event.phase != result.phase
        or event.parent_span_id != result.parent_span_id
        or event.payload != expected_payload
    ):
        raise ArtifactError("tool-attempt event context or payload contradicts its record")


def _validate_response_action_causality(artifact: RunArtifact) -> None:
    allowed_contexts = {
        "turn-supervisor-plan": ("supervisor", RuntimePhase.PLANNING, "supervisor_plan"),
        "turn-order-worker": (
            ORDER_WORKER_ID,
            RuntimePhase.EXECUTING_WORKERS,
            f"worker:{ORDER_WORKER_ID}",
        ),
        "turn-policy-worker": (
            POLICY_WORKER_ID,
            RuntimePhase.EXECUTING_WORKERS,
            f"worker:{POLICY_WORKER_ID}",
        ),
        "turn-supervisor-finalize": (
            "supervisor",
            RuntimePhase.FINALIZING,
            "supervisor_finalize",
        ),
    }
    requests_by_turn = {item.logical_turn_id: item for item in artifact.model_requests}
    attempts_by_request: dict[str, list[ModelAttempt]] = defaultdict(list)
    for attempt in artifact.model_attempts:
        attempts_by_request[attempt.request_id].append(attempt)

    for request in artifact.model_requests:
        expected = allowed_contexts.get(request.logical_turn_id)
        if expected is None:
            raise ArtifactError("artifact contains an unrelated Stage 1 logical model turn")
        if (request.source_component, request.phase, request.fixture_key) != expected:
            raise ArtifactError("durable model request has the wrong Stage 1 context")

    plan = artifact.final_state.plan
    planner_request = requests_by_turn.get("turn-supervisor-plan")
    if planner_request is not None and planner_request.payload != artifact.task.model_dump(
        mode="json"
    ):
        raise ArtifactError("planner request payload does not equal the persisted task")
    if plan is not None:
        if planner_request is None:
            raise ArtifactError("persisted plan requires a durable planner request")
        try:
            validate_stage_one_plan(plan)
        except SemanticValidationError as error:
            raise ArtifactError(str(error), code=error.code) from error
        parsed_plan = _parse_terminal_response(
            planner_request, attempts_by_request, type(plan), "persisted plan"
        )
        if parsed_plan != plan:
            raise ArtifactError("planner terminal response does not equal the persisted plan")

    assignments = {item.worker_id: item for item in plan.assignments} if plan is not None else {}
    for worker_id in CANONICAL_EVIDENCE_WORKER_IDS:
        worker_request = requests_by_turn.get(f"turn-{worker_id}")
        if worker_request is None:
            continue
        assignment = assignments.get(worker_id)
        if assignment is None:
            raise ArtifactError("worker request exists without an accepted plan assignment")
        expected_payload = {
            "task": artifact.task.model_dump(mode="json"),
            "assignment": assignment.model_dump(mode="json"),
        }
        if worker_request.payload != expected_payload:
            raise ArtifactError("worker request payload does not match task and assignment")

    for worker in artifact.final_state.worker_results:
        worker_request = requests_by_turn.get(f"turn-{worker.worker_id}")
        if worker_request is None:
            raise ArtifactError("persisted worker result requires its durable model request")
        worker_attempts = sorted(
            attempts_by_request.get(worker_request.request_id, []),
            key=lambda item: item.attempt,
        )
        if not worker_attempts:
            raise ArtifactError("persisted worker result requires terminal provider evidence")

    calls_by_worker: dict[str, list[ToolCall]] = defaultdict(list)
    for call in artifact.tool_calls:
        calls_by_worker[call.worker_id].append(call)
    if any(len(items) != 1 for items in calls_by_worker.values()):
        raise ArtifactError("a Stage 1 worker may persist at most one logical tool call")
    if set(calls_by_worker) - REQUIRED_WORKER_IDS:
        raise ArtifactError("tool call references an unknown Stage 1 worker")
    for worker_id, calls in calls_by_worker.items():
        worker_request = requests_by_turn.get(f"turn-{worker_id}")
        if worker_request is None:
            raise ArtifactError("persisted tool call requires its durable worker request")
        parsed_call = _parse_terminal_response(
            worker_request, attempts_by_request, WorkerToolRequest, "persisted tool call"
        )
        call = calls[0]
        if parsed_call.tool_name != call.tool_name or parsed_call.arguments != call.input:
            raise ArtifactError("worker terminal response does not equal the persisted tool call")
        try:
            validate_worker_tool_request(artifact.task, worker_id, call.tool_name, call.input)
        except SemanticValidationError as error:
            raise ArtifactError(str(error), code=error.code) from error

    finalizer_request = requests_by_turn.get("turn-supervisor-finalize")
    workers_by_id = {item.worker_id: item for item in artifact.final_state.worker_results}
    if finalizer_request is not None:
        if (
            len(workers_by_id) != 2
            or set(workers_by_id) != REQUIRED_WORKER_IDS
            or any(not item.succeeded for item in workers_by_id.values())
        ):
            raise ArtifactError("finalizer request exists without both successful worker results")
        finalizer_payload = {
            "task": artifact.task.model_dump(mode="json"),
            "worker_results": [
                workers_by_id[worker_id].model_dump(mode="json")
                for worker_id in CANONICAL_EVIDENCE_WORKER_IDS
            ],
        }
        if finalizer_request.payload != finalizer_payload:
            raise ArtifactError(
                "finalizer request payload does not match canonical worker evidence"
            )

    if artifact.final_decision is not None:
        if finalizer_request is None:
            raise ArtifactError("persisted final decision requires a durable finalizer request")
        parsed_decision = _parse_terminal_response(
            finalizer_request, attempts_by_request, FinalDecision, "persisted final decision"
        )
        if parsed_decision != artifact.final_decision:
            raise ArtifactError("finalizer terminal response does not equal the persisted decision")
        try:
            validate_successful_decision(
                artifact.task, artifact.final_state.worker_results, artifact.final_decision
            )
        except SemanticValidationError as error:
            raise ArtifactError(str(error), code=error.code) from error

    if artifact.status == RunStatus.SUCCEEDED:
        if set(requests_by_turn) != set(allowed_contexts) or len(artifact.model_requests) != 4:
            raise ArtifactError("success requires exactly the four Stage 1 logical model turns")
        for request in artifact.model_requests:
            _terminal_success(request, attempts_by_request)

    _validate_failed_response_causality(artifact, requests_by_turn, attempts_by_request)


def _validate_failed_response_causality(
    artifact: RunArtifact,
    requests_by_turn: dict[str, ModelRequestRecord],
    attempts_by_request: dict[str, list[ModelAttempt]],
) -> None:
    """Reclassify stored terminal responses and reconcile failed-path acceptance."""

    calls_by_worker: dict[str, list[ToolCall]] = defaultdict(list)
    for call in artifact.tool_calls:
        calls_by_worker[call.worker_id].append(call)

    for turn, request in requests_by_turn.items():
        attempts = sorted(
            attempts_by_request.get(request.request_id, []), key=lambda item: item.attempt
        )
        if not attempts or attempts[-1].outcome != AttemptOutcome.SUCCEEDED:
            continue
        terminal = attempts[-1]
        if terminal.response is None:  # guarded by strict attempt validation
            raise ArtifactError("terminal successful model attempt is missing its response")
        raw_json = terminal.response.raw_json
        try:
            raw_value = json.loads(raw_json)
        except json.JSONDecodeError:
            _require_output_failure(artifact, request, terminal, "malformed_model_json")
            continue

        if turn == "turn-supervisor-plan":
            try:
                parsed_plan = SupervisorPlan.model_validate(raw_value)
            except (ValidationError, ValueError):
                _require_output_failure(artifact, request, terminal, "model_output_schema_invalid")
                continue
            try:
                validate_stage_one_plan(parsed_plan)
            except SemanticValidationError as error:
                _require_output_failure(artifact, request, terminal, error.code)
                continue
            _reject_contradictory_output_failures(artifact, request)
            if artifact.final_state.plan is None:
                _require_cancelled_or_unexpected_gap(artifact, request, terminal, "accepted plan")
            elif artifact.final_state.plan != parsed_plan:
                raise ArtifactError("valid planner response contradicts the accepted plan")
            continue

        if turn in {"turn-order-worker", "turn-policy-worker"}:
            worker_id = request.source_component
            try:
                parsed_call = WorkerToolRequest.model_validate(raw_value)
            except (ValidationError, ValueError):
                _require_output_failure(artifact, request, terminal, "model_output_schema_invalid")
                continue
            try:
                validate_worker_tool_request(
                    artifact.task,
                    worker_id,
                    parsed_call.tool_name,
                    parsed_call.arguments,
                )
            except SemanticValidationError as error:
                _require_output_failure(artifact, request, terminal, error.code)
                continue
            _reject_contradictory_output_failures(artifact, request)
            calls = calls_by_worker.get(worker_id, [])
            if not calls:
                _require_cancelled_or_unexpected_gap(
                    artifact, request, terminal, "accepted tool call"
                )
            elif len(calls) != 1 or (
                calls[0].tool_name,
                calls[0].input,
            ) != (parsed_call.tool_name, parsed_call.arguments):
                raise ArtifactError("valid worker response contradicts the accepted tool call")
            continue

        if turn == "turn-supervisor-finalize":
            try:
                parsed_decision = FinalDecision.model_validate(raw_value)
            except (ValidationError, ValueError):
                _require_output_failure(artifact, request, terminal, "model_output_schema_invalid")
                continue
            try:
                validate_successful_decision(
                    artifact.task,
                    artifact.final_state.worker_results,
                    parsed_decision,
                )
            except SemanticValidationError as error:
                _require_output_failure(artifact, request, terminal, error.code)
                continue
            _reject_contradictory_output_failures(artifact, request)
            if artifact.final_decision is None:
                _require_cancelled_or_unexpected_gap(
                    artifact, request, terminal, "accepted final decision"
                )
            elif artifact.final_decision != parsed_decision:
                raise ArtifactError(
                    "valid finalizer response contradicts the accepted final decision"
                )


def _output_failures_for_request(
    artifact: RunArtifact, request: ModelRequestRecord
) -> list[FailureRecord]:
    """Return downstream failure claims made by the request's accepting component."""

    return [
        failure
        for failure in artifact.failures
        if failure.phase == request.phase
        and failure.source_component == request.source_component
        and failure.model_attempt_id is None
    ]


def _require_output_failure(
    artifact: RunArtifact,
    request: ModelRequestRecord,
    terminal: ModelAttempt,
    expected_code: str,
) -> None:
    failures = _output_failures_for_request(artifact, request)
    matching = [
        failure
        for failure in failures
        if failure.code == expected_code
        and failure.origin == FailureOrigin.MODEL_OUTPUT
        and not failure.retryable
        and failure.phase == request.phase
        and failure.source_component == request.source_component
        and failure.span_id == terminal.parent_span_id
        and failure.attempt is None
        and failure.model_attempt_id is None
        and failure.tool_call_id is None
        and failure.exception_type == "ModelOutputError"
    ]
    if artifact.status == RunStatus.CANCELLED and not failures:
        return
    if len(matching) != 1 or len(failures) != 1:
        raise ArtifactError(
            "terminal model-output failure code is not causally linked to parse result "
            f"{expected_code}"
        )
    failure = matching[0]
    if _parse_utc_timestamp(
        failure.timestamp, "model-output failure timestamp"
    ) < _parse_utc_timestamp(terminal.completed_at, "terminal provider completion"):
        raise ArtifactError(
            "model-output failure timestamp must follow its provider attempt completion"
        )

    provider_terminal = next(
        (
            event
            for event in artifact.events
            if event.span_id == terminal.attempt_id and event.event_type == "model_attempt_end"
        ),
        None,
    )
    failure_events = [
        event for event in artifact.events if event.payload.get("failure_id") == failure.failure_id
    ]
    run_terminal = next(
        (
            event
            for event in artifact.events
            if event.event_type in {"run_success", "run_failure", "run_cancellation"}
        ),
        None,
    )
    if (
        provider_terminal is None
        or not failure_events
        or run_terminal is None
        or any(event.sequence <= provider_terminal.sequence for event in failure_events)
        or run_terminal.sequence <= provider_terminal.sequence
    ):
        raise ArtifactError("model-output failure events must follow the provider terminal event")


def _reject_contradictory_output_failures(
    artifact: RunArtifact, request: ModelRequestRecord
) -> None:
    if _output_failures_for_request(artifact, request):
        raise ArtifactError("valid terminal response contradicts persisted model-output failure")


def _require_cancelled_or_unexpected_gap(
    artifact: RunArtifact,
    request: ModelRequestRecord,
    terminal: ModelAttempt,
    label: str,
) -> None:
    if artifact.status == RunStatus.CANCELLED:
        return
    run_span = next(
        (event.span_id for event in artifact.events if event.event_type == "run_start"), None
    )
    terminal_completed = _parse_utc_timestamp(
        terminal.completed_at, "terminal model attempt completed_at"
    )
    linked = [
        failure
        for failure in artifact.failures
        if failure.code == "unexpected_internal_error"
        and failure.origin == FailureOrigin.UNEXPECTED_INTERNAL
        and failure.phase == request.phase
        and failure.source_component == "runtime"
        and failure.span_id == run_span
        and not failure.retryable
        and _parse_utc_timestamp(failure.timestamp, "unexpected failure timestamp")
        >= terminal_completed
    ]
    if artifact.status != RunStatus.FAILED or len(linked) != 1:
        raise ArtifactError(f"valid terminal response is missing its {label}")


ModelValue = TypeVar("ModelValue", bound=BaseModel)


def _parse_terminal_response(
    request: ModelRequestRecord,
    attempts_by_request: dict[str, list[ModelAttempt]],
    model: type[ModelValue],
    label: str,
) -> ModelValue:
    terminal = _terminal_success(request, attempts_by_request)
    if terminal.response is None:  # guarded by the attempt model and trace validation
        raise ArtifactError(f"{label} requires a terminal successful provider response")
    try:
        value = json.loads(terminal.response.raw_json)
        return model.model_validate(value)
    except (json.JSONDecodeError, ValidationError, ValueError) as error:
        raise ArtifactError(
            f"terminal provider response cannot parse as {model.__name__}"
        ) from error


def _terminal_success(
    request: ModelRequestRecord,
    attempts_by_request: dict[str, list[ModelAttempt]],
) -> ModelAttempt:
    attempts = sorted(
        attempts_by_request.get(request.request_id, []), key=lambda item: item.attempt
    )
    if not attempts or attempts[-1].outcome != AttemptOutcome.SUCCEEDED:
        raise ArtifactError("downstream state requires a terminal successful provider response")
    return attempts[-1]


def _validate_failure_graph(artifact: RunArtifact) -> None:
    failures_by_id = {item.failure_id: item for item in artifact.failures}
    attempts_by_id = {item.attempt_id: item for item in artifact.model_attempts}
    calls_by_id = {item.call_id: item for item in artifact.tool_calls}
    results_by_span = {item.attempt_span_id: item for item in artifact.tool_results}
    results_by_call: dict[str, list[ToolResult]] = defaultdict(list)
    attempts_by_request: dict[str, list[ModelAttempt]] = defaultdict(list)
    requests_by_turn = {item.logical_turn_id: item for item in artifact.model_requests}
    for result in artifact.tool_results:
        results_by_call[result.tool_call_id].append(result)
    for attempt in artifact.model_attempts:
        attempts_by_request[attempt.request_id].append(attempt)
    referenced_failure_ids: set[str] = set()

    for attempt in artifact.model_attempts:
        failure = attempt.failure
        if failure is None:
            continue
        referenced_failure_ids.add(failure.failure_id)
        _require_top_level_failure(failure, failures_by_id)
        if (
            failure.attempt != attempt.attempt
            or failure.model_attempt_id != attempt.attempt_id
            or failure.tool_call_id is not None
            or failure.source_component != attempt.source_component
            or failure.phase != attempt.phase
            or failure.span_id != attempt.attempt_id
        ):
            raise ArtifactError("model-attempt failure contradicts its containing record")

    for result in artifact.tool_results:
        failure = result.failure
        if failure is None:
            continue
        referenced_failure_ids.add(failure.failure_id)
        _require_top_level_failure(failure, failures_by_id)
        if (
            failure.attempt != result.attempt
            or failure.model_attempt_id is not None
            or failure.tool_call_id != result.tool_call_id
            or failure.source_component != result.source_component
            or failure.phase != result.phase
            or failure.span_id != result.attempt_span_id
        ):
            raise ArtifactError("tool-attempt failure contradicts its containing record")

    for worker in artifact.final_state.worker_results:
        failure = worker.failure
        if failure is None:
            continue
        referenced_failure_ids.add(failure.failure_id)
        _require_top_level_failure(failure, failures_by_id)
        if failure.phase != RuntimePhase.EXECUTING_WORKERS:
            raise ArtifactError("worker failure has the wrong phase")
        request = requests_by_turn.get(f"turn-{worker.worker_id}")
        if request is None:
            raise ArtifactError("failed worker is missing its durable model request")
        model_attempts = sorted(
            attempts_by_request.get(request.request_id, []), key=lambda item: item.attempt
        )
        if not model_attempts:
            raise ArtifactError("failed worker is missing terminal model-attempt evidence")
        terminal_model = model_attempts[-1]
        if failure.model_attempt_id is not None:
            linked_attempt = attempts_by_id.get(failure.model_attempt_id)
            if (
                linked_attempt is None
                or linked_attempt.logical_turn_id != f"turn-{worker.worker_id}"
                or linked_attempt != terminal_model
                or linked_attempt.failure != failure
            ):
                raise ArtifactError("worker failure references the wrong model attempt")
        elif failure.tool_call_id is not None:
            call = calls_by_id.get(failure.tool_call_id)
            if call is None or call.worker_id != worker.worker_id:
                raise ArtifactError("worker failure references the wrong tool call")
            tool_attempts = sorted(
                results_by_call.get(call.call_id, []), key=lambda item: item.attempt
            )
            if (
                terminal_model.outcome != AttemptOutcome.SUCCEEDED
                or not tool_attempts
                or tool_attempts[-1].failure != failure
            ):
                raise ArtifactError("worker failure does not match terminal tool evidence")
        else:
            worker_starts = [
                item
                for item in artifact.events
                if item.event_type == "worker_start" and item.source_component == worker.worker_id
            ]
            if (
                terminal_model.outcome != AttemptOutcome.SUCCEEDED
                or len(worker_starts) != 1
                or failure.source_component != worker.worker_id
                or failure.span_id != worker_starts[0].span_id
                or failure.attempt is not None
            ):
                raise ArtifactError("worker failure context is not causally linked")

    for failure in artifact.failures:
        if failure.model_attempt_id is not None and failure.model_attempt_id not in attempts_by_id:
            raise ArtifactError("failure references an absent model attempt")
        if failure.tool_call_id is not None and failure.tool_call_id not in calls_by_id:
            raise ArtifactError("failure references an absent tool call")
        if failure.span_id is not None and failure.model_attempt_id is not None:
            if failure.span_id != failure.model_attempt_id:
                raise ArtifactError("failure span does not match its model attempt")
        if failure.span_id is not None and failure.span_id in results_by_span:
            result = results_by_span[failure.span_id]
            if failure.tool_call_id != result.tool_call_id:
                raise ArtifactError("failure span does not match its tool attempt")

    failure_event_types = {
        "model_attempt_failure",
        "model_attempt_timeout",
        "tool_attempt_failure",
        "tool_attempt_timeout",
        "worker_failure",
        "run_failure",
        "run_cancellation",
    }
    for event in artifact.events:
        failure_id = event.payload.get("failure_id")
        if failure_id is None:
            continue
        if event.event_type not in failure_event_types:
            raise ArtifactError("non-failure event cannot reference a failure")
        if not isinstance(failure_id, str):
            raise ArtifactError("failure event contains an invalid failure ID")
        failure = failures_by_id.get(failure_id)
        if failure is None or event.payload.get("failure_code") != failure.code:
            raise ArtifactError("failure event references an absent or contradictory failure")
        referenced_failure_ids.add(failure_id)

    if set(failures_by_id) != referenced_failure_ids:
        raise ArtifactError("top-level failure collection contains unreferenced evidence")


def _require_top_level_failure(
    failure: FailureRecord, failures_by_id: dict[str, FailureRecord]
) -> None:
    top_level = failures_by_id.get(failure.failure_id)
    if top_level is None:
        raise ArtifactError("nested failure is absent from the top-level failure collection")
    if top_level != failure:
        raise ArtifactError("nested failure fields contradict the top-level failure record")


def _validate_success_records(artifact: RunArtifact) -> None:
    decision = artifact.final_decision
    if decision is None:  # guarded by validate_artifact; retained for type narrowing
        raise ArtifactError("successful artifact requires a final decision")
    workers = artifact.final_state.worker_results
    worker_ids = [item.worker_id for item in workers]
    if len(workers) != 2 or len(set(worker_ids)) != 2 or set(worker_ids) != REQUIRED_WORKER_IDS:
        raise ArtifactError("success requires exactly one result for each Stage 1 worker")
    if any(not item.succeeded for item in workers):
        raise ArtifactError("successful artifact contains a failed worker result")

    calls_by_worker: dict[str, list[ToolCall]] = defaultdict(list)
    for call in artifact.tool_calls:
        calls_by_worker[call.worker_id].append(call)
    if set(calls_by_worker) != REQUIRED_WORKER_IDS or any(
        len(calls_by_worker[worker_id]) != 1 for worker_id in REQUIRED_WORKER_IDS
    ):
        raise ArtifactError("success requires exactly one logical tool call per Stage 1 worker")

    try:
        validate_successful_decision(artifact.task, workers, decision)
    except SemanticValidationError as error:
        raise ArtifactError(str(error), code=error.code) from error


def _validate_worker_evidence(artifact: RunArtifact) -> None:
    workers = artifact.final_state.worker_results
    worker_ids = [item.worker_id for item in workers]
    if len(worker_ids) != len(set(worker_ids)):
        raise ArtifactError("final-state worker IDs must be unique")
    calls_by_worker: dict[str, list[ToolCall]] = defaultdict(list)
    for call in artifact.tool_calls:
        calls_by_worker[call.worker_id].append(call)
    results_by_call: dict[str, list[ToolResult]] = defaultdict(list)
    for result in artifact.tool_results:
        results_by_call[result.tool_call_id].append(result)

    for worker in workers:
        calls = calls_by_worker.get(worker.worker_id, [])
        if not worker.succeeded:
            if any(
                result.outcome == AttemptOutcome.SUCCEEDED
                for call in calls
                for result in results_by_call.get(call.call_id, [])
            ):
                raise ArtifactError("failed worker contradicts terminal successful tool evidence")
            continue
        if len(calls) != 1:
            raise ArtifactError(
                "successful worker requires exactly one corresponding logical tool call"
            )
        call = calls[0]
        try:
            validate_worker_tool_request(artifact.task, call.worker_id, call.tool_name, call.input)
        except SemanticValidationError as error:
            raise ArtifactError(str(error), code=error.code) from error
        attempts = sorted(results_by_call.get(call.call_id, []), key=lambda item: item.attempt)
        successes = [item for item in attempts if item.outcome == AttemptOutcome.SUCCEEDED]
        if not attempts or len(successes) != 1 or attempts[-1] != successes[0]:
            raise ArtifactError(
                "successful worker requires one terminal successful tool-result attempt"
            )
        if worker.tool_name != call.tool_name or worker.output != successes[0].output:
            raise ArtifactError("successful worker output does not match accepted tool evidence")


def _validate_successful_tool_outputs(artifact: RunArtifact) -> None:
    """Strictly reparse every successful tool attempt, including partial failed runs."""

    output_models: dict[str, type[BaseModel]] = {
        "lookup_order": LookupOrderOutput,
        "lookup_return_policy": LookupReturnPolicyOutput,
    }
    for result in artifact.tool_results:
        if result.outcome != AttemptOutcome.SUCCEEDED:
            if result.output is not None:
                raise ArtifactError("failed or timed-out tool attempt must not retain output")
            continue
        model = output_models.get(result.tool_name)
        if model is None:
            raise ArtifactError("unknown successful tool name is not valid Stage 1 evidence")
        try:
            parsed = model.model_validate(result.output)
        except ValidationError as error:
            raise ArtifactError(f"successful {result.tool_name} tool output is invalid") from error
        if parsed.model_dump(mode="json") != result.output:
            raise ArtifactError(
                f"successful {result.tool_name} tool output is not canonical strict evidence"
            )


def _validate_fixture_response_evidence(artifact: RunArtifact) -> None:
    requests_by_id = {item.request_id: item for item in artifact.model_requests}
    model_ids: set[str] = set()
    for attempt in artifact.model_attempts:
        if attempt.outcome != AttemptOutcome.SUCCEEDED:
            continue
        response = attempt.response
        request = requests_by_id.get(attempt.request_id)
        if response is None or request is None:
            raise ArtifactError("successful model attempt is missing fixture response context")
        if response.provider != "fixture":
            raise ArtifactError("successful fixture response must identify the fixture provider")
        if response.finish_reason != "scripted":
            raise ArtifactError("successful fixture response finish_reason must be scripted")
        if response.usage.cost_usd != 0.0:
            raise ArtifactError("successful fixture response cost must be zero")
        usage = response.usage
        has_tokens = usage.input_tokens is not None or usage.output_tokens is not None
        if usage.token_source == "provider_measured" or (
            has_tokens and usage.token_source != "synthetic"
        ):
            raise ArtifactError("fixture token counts must be explicitly synthetic")
        if response.metadata != {
            "fixture_key": request.fixture_key,
            "behavior": "success",
        }:
            raise ArtifactError("successful fixture response metadata is inconsistent")
        model_ids.add(response.model_id)
    if len(model_ids) > 1:
        raise ArtifactError("successful fixture responses require one consistent model ID")


def _validate_unique_identifiers(artifact: RunArtifact) -> None:
    _require_unique((item.event_id for item in artifact.events), "event_id")
    _require_unique((item.request_id for item in artifact.model_requests), "model request_id")
    _require_unique(
        (item.logical_turn_id for item in artifact.model_requests), "logical model turn_id"
    )
    _require_unique((item.attempt_id for item in artifact.model_attempts), "model attempt_id")
    _require_unique((item.call_id for item in artifact.tool_calls), "tool call_id")
    _require_unique((item.result_id for item in artifact.tool_results), "tool result_id")
    _require_unique(
        (item.attempt_span_id for item in artifact.tool_results),
        "tool attempt_span_id",
    )
    _require_unique(
        (
            item.response.response_id
            for item in artifact.model_attempts
            if item.outcome == AttemptOutcome.SUCCEEDED and item.response is not None
        ),
        "successful model response_id",
    )
    _require_unique((item.failure_id for item in artifact.failures), "failure_id")


def _validate_event_vocabulary(artifact: RunArtifact) -> None:
    unknown = {event.event_type for event in artifact.events} - _ALLOWED_EVENT_TYPES
    if unknown:
        raise ArtifactError(f"artifact contains unknown event type: {sorted(unknown)[0]}")


def _validate_span_identities(artifact: RunArtifact) -> None:
    run_spans = {event.span_id for event in artifact.events if event.event_type == "run_start"}
    planning_spans = {
        event.span_id
        for event in artifact.events
        if event.event_type == "supervisor_planning_start"
    }
    worker_starts = [event for event in artifact.events if event.event_type == "worker_start"]
    worker_spans = {event.span_id for event in worker_starts}
    finalization_spans = {
        event.span_id
        for event in artifact.events
        if event.event_type == "supervisor_finalization_start"
    }
    model_attempt_spans = {attempt.attempt_id for attempt in artifact.model_attempts}
    tool_attempt_spans = {result.attempt_span_id for result in artifact.tool_results}
    reached_finalization = any(
        event.event_type == "state_transition" and event.phase == RuntimePhase.FINALIZING
        for event in artifact.events
    )
    cancelled_before_planning = _is_cancelled_before_planning_lifecycle(artifact)

    if len(run_spans) != 1 or len(planning_spans) != int(not cancelled_before_planning):
        raise ArtifactError(
            "run_start and reached planning lifecycle spans must each have one identity"
        )
    if len(worker_spans) != len(worker_starts):
        raise ArtifactError("worker lifecycle spans must be distinct")
    if len(finalization_spans) != int(reached_finalization):
        raise ArtifactError(
            "finalization lifecycle requires one span identity exactly when reached"
        )

    categories = {
        "run": run_spans,
        "planning": planning_spans,
        "workers": worker_spans,
        "finalization": finalization_spans,
        "model attempts": model_attempt_spans,
        "tool attempts": tool_attempt_spans,
    }
    names = list(categories)
    for index, left_name in enumerate(names):
        for right_name in names[index + 1 :]:
            if categories[left_name] & categories[right_name]:
                raise ArtifactError(f"span identity collision between {left_name} and {right_name}")


def _is_cancelled_before_planning_lifecycle(artifact: RunArtifact) -> bool:
    """Recognize only the truthful no-planning-work startup cancellation shape."""

    event_types = [event.event_type for event in artifact.events]
    transitions = [
        event.phase for event in artifact.events if event.event_type == "state_transition"
    ]
    return (
        artifact.status == RunStatus.CANCELLED
        and event_types
        == [
            "run_start",
            "state_transition",
            "state_transition",
            "run_cancellation",
            "artifact_persistence_start",
        ]
        and transitions == [RuntimePhase.PLANNING, RuntimePhase.CANCELLED]
        and artifact.model_requests == []
        and artifact.model_attempts == []
        and artifact.tool_calls == []
        and artifact.tool_results == []
        and artifact.final_state.plan is None
        and artifact.final_state.worker_results == []
        and artifact.final_decision is None
        and len(artifact.failures) == 1
        and artifact.failures[0].code == "run_cancelled"
    )


def _require_unique(values: Any, label: str) -> None:
    observed = list(values)
    if len(observed) != len(set(observed)):
        raise ArtifactError(f"artifact {label} values must be unique")


def _validate_timestamps(artifact: RunArtifact) -> None:
    started = _parse_utc_timestamp(artifact.started_at, "run started_at")
    completed = _parse_utc_timestamp(artifact.completed_at, "run completed_at")
    if started > completed:
        raise ArtifactError("run started_at must not be after completed_at")

    def within_run(value: datetime, label: str) -> None:
        if value < started or value > completed:
            raise ArtifactError(f"{label} must fall within the run interval")

    event_times = [
        _parse_utc_timestamp(event.timestamp, "event timestamp") for event in artifact.events
    ]
    if event_times != sorted(event_times):
        raise ArtifactError("event timestamps must be nondecreasing in sequence order")
    for value in event_times:
        within_run(value, "event timestamp")
    events_by_span: dict[str, list[AgentEvent]] = defaultdict(list)
    for event in artifact.events:
        events_by_span[event.span_id].append(event)
    for request in artifact.model_requests:
        created = _parse_utc_timestamp(request.created_at, "model request created_at")
        within_run(created, "model request created_at")
    for attempt in artifact.model_attempts:
        attempt_started = _parse_utc_timestamp(attempt.started_at, "model attempt started_at")
        attempt_completed = _parse_utc_timestamp(attempt.completed_at, "model attempt completed_at")
        if attempt_started > attempt_completed:
            raise ArtifactError("model attempt started_at must not be after completed_at")
        within_run(attempt_started, "model attempt started_at")
        within_run(attempt_completed, "model attempt completed_at")
        start_events = [
            event
            for event in events_by_span[attempt.attempt_id]
            if event.event_type == "model_attempt_start"
        ]
        terminal_events = [
            event
            for event in events_by_span[attempt.attempt_id]
            if event.event_type
            in {"model_attempt_end", "model_attempt_failure", "model_attempt_timeout"}
        ]
        if len(start_events) == 1:
            start_event_time = _parse_utc_timestamp(
                start_events[0].timestamp, "model attempt start event timestamp"
            )
            if start_event_time < attempt_started or start_event_time > attempt_completed:
                raise ArtifactError("model attempt start event timestamp contradicts its record")
        if len(terminal_events) == 1:
            terminal_time = _parse_utc_timestamp(
                terminal_events[0].timestamp, "model attempt terminal event timestamp"
            )
            if terminal_time < attempt_completed:
                raise ArtifactError("model attempt terminal event timestamp precedes completion")
        if attempt.failure is not None:
            failure_time = _parse_utc_timestamp(
                attempt.failure.timestamp, "model attempt failure timestamp"
            )
            within_run(failure_time, "model attempt failure timestamp")
            if failure_time != attempt_completed:
                raise ArtifactError(
                    "model failure timestamp must equal its containing attempt completion timestamp"
                )
    for result in artifact.tool_results:
        result_started = _parse_utc_timestamp(result.started_at, "tool attempt started_at")
        result_completed = _parse_utc_timestamp(result.completed_at, "tool attempt completed_at")
        if result_started > result_completed:
            raise ArtifactError("tool attempt started_at must not be after completed_at")
        within_run(result_started, "tool attempt started_at")
        within_run(result_completed, "tool attempt completed_at")
        start_events = [
            event
            for event in events_by_span[result.attempt_span_id]
            if event.event_type == "tool_attempt_start"
        ]
        terminal_events = [
            event
            for event in events_by_span[result.attempt_span_id]
            if event.event_type
            in {"tool_attempt_end", "tool_attempt_failure", "tool_attempt_timeout"}
        ]
        if len(start_events) == 1:
            start_event_time = _parse_utc_timestamp(
                start_events[0].timestamp, "tool attempt start event timestamp"
            )
            if start_event_time < result_started or start_event_time > result_completed:
                raise ArtifactError("tool attempt start event timestamp contradicts its record")
        if len(terminal_events) == 1:
            terminal_time = _parse_utc_timestamp(
                terminal_events[0].timestamp, "tool attempt terminal event timestamp"
            )
            if terminal_time < result_completed:
                raise ArtifactError("tool attempt terminal event timestamp precedes completion")
        if result.failure is not None:
            failure_time = _parse_utc_timestamp(
                result.failure.timestamp, "tool attempt failure timestamp"
            )
            within_run(failure_time, "tool attempt failure timestamp")
            if failure_time != result_completed:
                raise ArtifactError(
                    "tool failure timestamp must equal its containing attempt completion timestamp"
                )
    for failure in artifact.failures:
        failure_time = _parse_utc_timestamp(failure.timestamp, "failure timestamp")
        within_run(failure_time, "failure timestamp")
    for worker in artifact.final_state.worker_results:
        if worker.failure is not None:
            failure_time = _parse_utc_timestamp(
                worker.failure.timestamp, "worker failure timestamp"
            )
            within_run(failure_time, "worker failure timestamp")


def _parse_utc_timestamp(value: str, label: str) -> datetime:
    if not value.endswith("Z") or "T" not in value:
        raise ArtifactError(f"{label} must be a valid UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as error:
        raise ArtifactError(f"{label} must be a valid UTC timestamp ending in Z") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ArtifactError(f"{label} must be a valid UTC timestamp ending in Z")
    return parsed


def _validate_required_events(artifact: RunArtifact) -> None:
    def matching(
        event_type: str,
        source: str | None = None,
        phase: RuntimePhase | None = None,
    ) -> list[AgentEvent]:
        return [
            event
            for event in artifact.events
            if event.event_type == event_type
            and (source is None or event.source_component == source)
            and (phase is None or event.phase == phase)
        ]

    events_by_type: dict[str, list[AgentEvent]] = defaultdict(list)
    for event in artifact.events:
        events_by_type[event.event_type].append(event)

    run_starts = events_by_type["run_start"]
    if (
        len(run_starts) != 1
        or run_starts[0].sequence != 1
        or run_starts[0].source_component != "runtime"
        or run_starts[0].phase != RuntimePhase.INITIALIZED
    ):
        raise ArtifactError("artifact requires exactly one initial runtime run_start event")
    run_start = run_starts[0]
    if (
        run_start.span_id == ""
        or run_start.parent_span_id is not None
        or run_start.payload != {"run_id": artifact.run_id, "task_id": artifact.task.task_id}
    ):
        raise ArtifactError("run_start span context is invalid")
    persistence = events_by_type["artifact_persistence_start"]
    if (
        len(persistence) != 1
        or persistence[0].sequence != len(artifact.events)
        or persistence[0].source_component != "artifact"
        or persistence[0].phase != artifact.final_state.phase
    ):
        raise ArtifactError("artifact requires exactly one final persistence-start event")
    if (
        persistence[0].span_id != run_start.span_id
        or persistence[0].parent_span_id is not None
        or persistence[0].payload != {}
    ):
        raise ArtifactError("persistence-start span context is invalid")

    transitions = matching("state_transition")
    current = RuntimePhase.INITIALIZED
    for event in transitions:
        previous = event.payload.get("from")
        target = event.payload.get("to")
        if previous != current.value or target != event.phase.value:
            raise ArtifactError("artifact state-transition events do not form a valid chain")
        try:
            next_phase = RuntimePhase(cast(str, target))
        except ValueError as error:
            raise ArtifactError("artifact state-transition target is invalid") from error
        if not is_valid_transition(current, next_phase):
            raise ArtifactError("artifact contains an invalid state-transition event")
        expected_source = {
            RuntimePhase.PLANNING: "runtime",
            RuntimePhase.EXECUTING_WORKERS: "supervisor",
            RuntimePhase.FINALIZING: "supervisor",
            RuntimePhase.SUCCEEDED: "supervisor",
            RuntimePhase.CANCELLED: "runtime",
        }.get(next_phase)
        if expected_source is not None and event.source_component != expected_source:
            raise ArtifactError("state-transition source contradicts the Stage 1 graph")
        if (
            event.span_id != run_start.span_id
            or event.parent_span_id is not None
            or event.payload != {"from": current.value, "to": next_phase.value}
        ):
            raise ArtifactError("state-transition event context is invalid")
        current = next_phase
    if current != artifact.final_state.phase:
        raise ArtifactError("artifact transition chain does not reach the final state")

    planning_starts = events_by_type["supervisor_planning_start"]
    planning_transitions = [item for item in transitions if item.phase == RuntimePhase.PLANNING]
    cancelled_before_planning = _is_cancelled_before_planning_lifecycle(artifact)
    planning_start: AgentEvent | None = None
    if not cancelled_before_planning:
        if (
            len(planning_starts) != 1
            or planning_starts[0].source_component != "supervisor"
            or planning_starts[0].phase != RuntimePhase.PLANNING
        ):
            raise ArtifactError("artifact requires exactly one supervisor planning start event")
        planning_start = planning_starts[0]
        if planning_start.parent_span_id != run_start.span_id or planning_start.payload != {}:
            raise ArtifactError("planning-start parent span is invalid")
        if (
            len(planning_transitions) != 1
            or planning_transitions[0].sequence >= planning_start.sequence
        ):
            raise ArtifactError("planning start must follow the accepted planning transition")

    requests_by_turn = {item.logical_turn_id: item for item in artifact.model_requests}
    attempts_by_request: dict[str, list[ModelAttempt]] = defaultdict(list)
    for attempt in artifact.model_attempts:
        attempts_by_request[attempt.request_id].append(attempt)
    planner_request = requests_by_turn.get("turn-supervisor-plan")
    if planner_request is not None:
        if planning_start is None:
            raise ArtifactError("planner request requires a planning lifecycle start")
        if _parse_utc_timestamp(
            planner_request.created_at, "planner request created_at"
        ) < _parse_utc_timestamp(planning_start.timestamp, "planning lifecycle start"):
            raise ArtifactError("model request must not predate its lifecycle start")
        for attempt in attempts_by_request.get(planner_request.request_id, []):
            if attempt.parent_span_id != planning_start.span_id:
                raise ArtifactError("planner attempt has the wrong planning parent span")

    plan = artifact.final_state.plan
    planning_ends = events_by_type["supervisor_planning_end"]
    if len(planning_ends) > 1 or any(
        item.source_component != "supervisor" or item.phase != RuntimePhase.PLANNING
        for item in planning_ends
    ):
        raise ArtifactError("artifact contains duplicate supervisor planning end events")
    if planning_ends:
        if planner_request is None or planning_start is None:
            raise ArtifactError("planning end exists without a planner request")
        planner_terminal = _terminal_event_for_request(
            artifact, planner_request, attempts_by_request
        )
        if (
            planner_terminal is None
            or planner_terminal.event_type != "model_attempt_end"
            or planner_terminal.sequence >= planning_ends[0].sequence
            or planning_ends[0].span_id != planning_start.span_id
            or planning_ends[0].parent_span_id != run_start.span_id
            or plan is None
            or planning_ends[0].payload != {"assignment_count": len(plan.assignments)}
        ):
            raise ArtifactError("planning end is not caused by the planner terminal response")

    if plan is not None:
        if len(planning_ends) != 1 or planning_start is None:
            raise ArtifactError("accepted plan requires exactly one supervisor planning end event")
        execution_transitions = [
            item for item in transitions if item.phase == RuntimePhase.EXECUTING_WORKERS
        ]
        if (
            len(execution_transitions) != 1
            or planning_ends[0].sequence >= execution_transitions[0].sequence
        ):
            raise ArtifactError("worker execution transition must follow planning end")
        dispatches = events_by_type["worker_dispatch"]
        dispatch_ids = [cast(str, event.payload.get("worker_id")) for event in dispatches]
        if (
            len(dispatches) != len(plan.assignments)
            or set(dispatch_ids) != {item.worker_id for item in plan.assignments}
            or any(
                event.payload != {"worker_id": cast(str, event.payload.get("worker_id"))}
                or event.source_component != "supervisor"
                or event.phase != RuntimePhase.EXECUTING_WORKERS
                or event.span_id != planning_start.span_id
                or event.parent_span_id != run_start.span_id
                or event.sequence <= execution_transitions[0].sequence
                for event in dispatches
            )
        ):
            raise ArtifactError("artifact worker-dispatch evidence does not match the plan")
    elif events_by_type["worker_dispatch"]:
        raise ArtifactError("artifact contains worker dispatch without an accepted plan")

    worker_terminals = [
        event for event in artifact.events if event.event_type in {"worker_end", "worker_failure"}
    ]
    result_ids = {item.worker_id for item in artifact.final_state.worker_results}
    planned_ids = {item.worker_id for item in plan.assignments} if plan is not None else set()
    worker_starts = matching("worker_start")
    if any(
        item.source_component not in planned_ids or item.phase != RuntimePhase.EXECUTING_WORKERS
        for item in worker_starts
    ) or any(
        sum(item.source_component == worker_id for item in worker_starts) > 1
        for worker_id in planned_ids
    ):
        raise ArtifactError("worker-start evidence does not match accepted assignments")
    if any(
        item.parent_span_id != run_start.span_id or item.span_id == "" for item in worker_starts
    ):
        raise ArtifactError("worker-start parent span contradicts the worker lifecycle")
    if any(event.source_component not in result_ids for event in worker_terminals):
        raise ArtifactError("worker terminal event has no persisted worker result")

    for worker_id in CANONICAL_EVIDENCE_WORKER_IDS:
        request = requests_by_turn.get(f"turn-{worker_id}")
        if request is None:
            continue
        starts = matching("worker_start", worker_id, RuntimePhase.EXECUTING_WORKERS)
        if len(starts) != 1:
            raise ArtifactError("worker request requires exactly one worker-start lifecycle event")
        if _parse_utc_timestamp(
            request.created_at, "worker request created_at"
        ) < _parse_utc_timestamp(starts[0].timestamp, "worker lifecycle start"):
            raise ArtifactError("model request must not predate its lifecycle start")
        if any(
            item.parent_span_id != starts[0].span_id
            for item in attempts_by_request.get(request.request_id, [])
        ):
            raise ArtifactError("worker model attempt contradicts its worker lifecycle span")

    for result in artifact.final_state.worker_results:
        starts = matching("worker_start", result.worker_id, RuntimePhase.EXECUTING_WORKERS)
        if len(starts) != 1:
            raise ArtifactError("persisted worker requires exactly one worker-start event")
        terminal = "worker_end" if result.succeeded else "worker_failure"
        terminals = matching(terminal, result.worker_id, RuntimePhase.EXECUTING_WORKERS)
        if len(terminals) != 1 or starts[0].sequence >= terminals[0].sequence:
            raise ArtifactError("persisted worker requires one causally ordered terminal event")
        if (
            starts[0].span_id != terminals[0].span_id
            or starts[0].parent_span_id != run_start.span_id
        ):
            raise ArtifactError("worker lifecycle span context is invalid")
        expected_payload = {
            "succeeded": result.succeeded,
            "failure_id": result.failure.failure_id if result.failure else None,
            "failure_code": result.failure.code if result.failure else None,
        }
        if (
            terminals[0].payload != expected_payload
            or terminals[0].parent_span_id != run_start.span_id
        ):
            raise ArtifactError("worker terminal payload contradicts its result")
        calls = [item for item in artifact.tool_calls if item.worker_id == result.worker_id]
        if calls and any(item.span_id != starts[0].span_id for item in calls):
            raise ArtifactError("worker tool call has the wrong worker span")
        request = requests_by_turn.get(f"turn-{result.worker_id}")
        if request is not None and any(
            item.parent_span_id != starts[0].span_id
            for item in attempts_by_request.get(request.request_id, [])
        ):
            raise ArtifactError("worker model attempt has the wrong worker parent span")
        if request is None:
            raise ArtifactError("worker lifecycle is missing its durable request")
        model_terminal = _terminal_event_for_request(artifact, request, attempts_by_request)
        if model_terminal is None or model_terminal.sequence >= terminals[0].sequence:
            raise ArtifactError("worker terminal event must follow terminal model evidence")
        tool_terminals = [
            item
            for call in calls
            for tool_result in artifact.tool_results
            if tool_result.tool_call_id == call.call_id
            for item in artifact.events
            if item.span_id == tool_result.attempt_span_id
            and item.event_type
            in {"tool_attempt_end", "tool_attempt_failure", "tool_attempt_timeout"}
        ]
        if (
            tool_terminals
            and max(item.sequence for item in tool_terminals) >= terminals[0].sequence
        ):
            raise ArtifactError("worker terminal event must follow terminal tool evidence")

    dispatch_sequence = {
        cast(str, item.payload.get("worker_id")): item.sequence
        for item in events_by_type["worker_dispatch"]
    }
    for start in worker_starts:
        assignment = next(
            (
                item
                for item in (plan.assignments if plan is not None else [])
                if item.worker_id == start.source_component
            ),
            None,
        )
        if assignment is None or start.payload != {"purpose": assignment.purpose}:
            raise ArtifactError("worker-start payload does not match its assignment")
        if start.sequence <= dispatch_sequence.get(
            start.source_component, len(artifact.events) + 1
        ):
            raise ArtifactError("worker start must follow its dispatch event")

    for call in artifact.tool_calls:
        starts = matching("worker_start", call.worker_id, RuntimePhase.EXECUTING_WORKERS)
        if len(starts) != 1 or call.span_id != starts[0].span_id:
            raise ArtifactError("tool call contradicts its worker lifecycle span")
        request = requests_by_turn.get(f"turn-{call.worker_id}")
        if request is None:
            raise ArtifactError("tool call is missing its worker request")
        model_terminal = _terminal_event_for_request(artifact, request, attempts_by_request)
        tool_starts = [
            event
            for result in artifact.tool_results
            if result.tool_call_id == call.call_id
            for event in artifact.events
            if event.span_id == result.attempt_span_id and event.event_type == "tool_attempt_start"
        ]
        if model_terminal is None or any(
            event.sequence <= model_terminal.sequence for event in tool_starts
        ):
            raise ArtifactError("tool attempt must follow the worker terminal model response")

    final_starts = events_by_type["supervisor_finalization_start"]
    final_ends = events_by_type["supervisor_finalization_end"]
    if (
        len(final_starts) > 1
        or len(final_ends) > 1
        or any(
            item.source_component != "supervisor" or item.phase != RuntimePhase.FINALIZING
            for item in [*final_starts, *final_ends]
        )
    ):
        raise ArtifactError("artifact contains duplicate supervisor finalization lifecycle events")
    finalizing_transitions = [item for item in transitions if item.phase == RuntimePhase.FINALIZING]
    if finalizing_transitions:
        if len(finalizing_transitions) != 1 or len(final_starts) != 1:
            raise ArtifactError(
                "reaching finalization requires one transition and one finalization-start event"
            )
        if (
            finalizing_transitions[0].sequence >= final_starts[0].sequence
            or any(item.sequence >= finalizing_transitions[0].sequence for item in worker_terminals)
            or final_starts[0].parent_span_id != run_start.span_id
            or final_starts[0].payload != {}
        ):
            raise ArtifactError("finalization must follow completed worker evidence")
    elif final_starts:
        raise ArtifactError("finalization-start event requires a finalizing transition")
    finalizer_request = requests_by_turn.get("turn-supervisor-finalize")
    if finalizer_request is not None:
        if len(final_starts) != 1:
            raise ArtifactError("finalizer request requires one finalization-start event")
        if _parse_utc_timestamp(
            finalizer_request.created_at, "finalizer request created_at"
        ) < _parse_utc_timestamp(final_starts[0].timestamp, "finalization lifecycle start"):
            raise ArtifactError("model request must not predate its lifecycle start")
        if any(
            item.parent_span_id != final_starts[0].span_id
            for item in attempts_by_request.get(finalizer_request.request_id, [])
        ):
            raise ArtifactError("finalizer attempt has the wrong finalization parent span")
    if final_ends:
        if finalizer_request is None or not final_starts:
            raise ArtifactError("finalization end exists without a finalizer request")
        final_terminal = _terminal_event_for_request(
            artifact, finalizer_request, attempts_by_request
        )
        if (
            final_terminal is None
            or final_terminal.event_type != "model_attempt_end"
            or final_terminal.sequence >= final_ends[0].sequence
            or final_ends[0].span_id != final_starts[0].span_id
            or final_ends[0].parent_span_id != run_start.span_id
            or artifact.final_decision is None
            or final_ends[0].payload != {"decision_code": artifact.final_decision.decision_code}
        ):
            raise ArtifactError("finalization end is not caused by the finalizer response")
    if artifact.final_decision is not None and len(final_ends) != 1:
        raise ArtifactError("accepted final decision requires one finalization-end event")

    terminal_types = {"run_success", "run_failure", "run_cancellation"}
    run_terminals = [event for event in artifact.events if event.event_type in terminal_types]
    if len(run_terminals) != 1 or run_terminals[0].sequence >= persistence[0].sequence:
        raise ArtifactError("artifact requires exactly one terminal run event before persistence")
    run_terminal = run_terminals[0]
    expected_terminal = {
        RunStatus.SUCCEEDED: ("run_success", RuntimePhase.SUCCEEDED),
        RunStatus.FAILED: ("run_failure", RuntimePhase.FAILED),
        RunStatus.CANCELLED: ("run_cancellation", RuntimePhase.CANCELLED),
    }[artifact.status]
    if (run_terminal.event_type, run_terminal.phase) != expected_terminal:
        raise ArtifactError("terminal run event contradicts artifact status")
    if artifact.status == RunStatus.SUCCEEDED:
        if len(planning_ends) != 1 or len(final_starts) != 1 or len(final_ends) != 1:
            raise ArtifactError("successful artifact is missing required lifecycle evidence")
        if artifact.final_decision is None:  # guarded by top-level status validation
            raise ArtifactError("successful artifact requires a final decision")
        if run_terminal.payload != {"decision_code": artifact.final_decision.decision_code}:
            raise ArtifactError("run_success payload contradicts the final decision")
        if (
            run_terminal.source_component != "runtime"
            or run_terminal.span_id != run_start.span_id
            or run_terminal.parent_span_id is not None
        ):
            raise ArtifactError("run_success context contradicts the finalization lifecycle")
        success_transitions = [item for item in transitions if item.phase == RuntimePhase.SUCCEEDED]
        if (
            len(success_transitions) != 1
            or final_ends[0].sequence >= success_transitions[0].sequence
            or success_transitions[0].sequence >= run_terminal.sequence
        ):
            raise ArtifactError("run success must follow finalization end and success transition")
    else:
        failure_id = run_terminal.payload.get("failure_id")
        failure_code = run_terminal.payload.get("failure_code")
        failure = next((item for item in artifact.failures if item.failure_id == failure_id), None)
        if failure is None or failure.code != failure_code:
            raise ArtifactError("terminal run event references an absent or contradictory failure")
        expected_failure_payload = {
            "failure_id": failure.failure_id,
            "failure_code": failure.code,
        }
        if artifact.status == RunStatus.CANCELLED:
            if (
                failure.code != "run_cancelled"
                or failure.origin != FailureOrigin.CANCELLATION
                or failure.retryable
                or failure.exception_type != "CancelledError"
                or failure.source_component != "runtime"
                or failure.attempt is not None
                or failure.model_attempt_id is not None
                or failure.tool_call_id is not None
                or failure.span_id != run_start.span_id
            ):
                raise ArtifactError("run cancellation failure taxonomy is inconsistent")
            if run_terminal.payload != {
                "active_workers_after_cleanup": 0,
                **expected_failure_payload,
            }:
                raise ArtifactError("run cancellation payload must prove zero active workers")
        elif run_terminal.payload != expected_failure_payload:
            raise ArtifactError("run failure payload contradicts its terminal failure")
        if run_terminal.source_component != failure.source_component:
            raise ArtifactError("terminal run event source contradicts its failure")
        if failure.model_attempt_id is not None:
            linked = next(
                (
                    item
                    for item in artifact.model_attempts
                    if item.attempt_id == failure.model_attempt_id
                ),
                None,
            )
            if linked is None:
                raise ArtifactError("terminal run failure references an absent model attempt")
            request_attempts = sorted(
                (item for item in artifact.model_attempts if item.request_id == linked.request_id),
                key=lambda item: item.attempt,
            )
            if (
                not request_attempts
                or linked != request_attempts[-1]
                or linked.failure != failure
                or run_terminal.span_id != linked.parent_span_id
                or run_terminal.parent_span_id != run_start.span_id
            ):
                raise ArtifactError("terminal run event references a nonterminal model failure")
        elif failure.span_id == run_start.span_id:
            if (
                failure.source_component != "runtime"
                or run_terminal.span_id != run_start.span_id
                or run_terminal.parent_span_id is not None
            ):
                raise ArtifactError("terminal runtime failure span context is invalid")
        elif (
            failure.source_component != "supervisor"
            or run_terminal.span_id != failure.span_id
            or run_terminal.parent_span_id != run_start.span_id
        ):
            raise ArtifactError("terminal run failure span context is invalid")

        terminal_transition = next(
            (item for item in transitions if item.phase == artifact.final_state.phase),
            None,
        )
        if (
            terminal_transition is not None
            and terminal_transition.source_component != run_terminal.source_component
        ):
            raise ArtifactError("terminal transition source contradicts the terminal run event")

    terminal_transitions = [
        item for item in transitions if item.phase == artifact.final_state.phase
    ]
    if len(terminal_transitions) != 1 or terminal_transitions[0].sequence >= run_terminal.sequence:
        raise ArtifactError("terminal run event must follow its accepted terminal transition")


def _terminal_event_for_request(
    artifact: RunArtifact,
    request: ModelRequestRecord,
    attempts_by_request: dict[str, list[ModelAttempt]],
) -> AgentEvent | None:
    attempts = sorted(
        attempts_by_request.get(request.request_id, []), key=lambda item: item.attempt
    )
    if not attempts:
        return None
    terminal_id = attempts[-1].attempt_id
    terminal_types = {"model_attempt_end", "model_attempt_failure", "model_attempt_timeout"}
    events = [
        item
        for item in artifact.events
        if item.span_id == terminal_id and item.event_type in terminal_types
    ]
    return events[0] if len(events) == 1 else None


def _reject_private_or_secret_data(payload: Any) -> None:
    encoded = json.dumps(payload, sort_keys=True)
    if _PRIVATE_PATH.search(encoded):
        raise ArtifactError("artifact contains an absolute private path")
    if _SECRET_VALUE.search(encoded):
        raise ArtifactError("artifact contains an obvious secret-like value")
    config = payload.get("run_config", {}) if isinstance(payload, dict) else {}
    if isinstance(config, dict):
        for key in config:
            lowered = str(key).lower()
            if any(marker in lowered for marker in ("secret", "password", "api_key", "credential")):
                raise ArtifactError("artifact configuration contains a secret-like field")


def write_artifact(artifact: RunArtifact, path: Path) -> Path:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(
                artifact.model_dump(mode="json"),
                handle,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except (OSError, TypeError, ValueError) as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise ArtifactError(f"could not atomically persist artifact: {error}") from error
    return destination


def read_artifact(path: Path) -> RunArtifact:
    try:
        raw = path.read_text(encoding="utf-8")
        artifact = RunArtifact.model_validate_json(raw)
    except (OSError, ValidationError, ValueError) as error:
        raise ArtifactError(f"could not read run artifact: {error}") from error
    validate_artifact(artifact)
    return artifact


def generated_schema() -> dict[str, Any]:
    return RunArtifact.model_json_schema()
