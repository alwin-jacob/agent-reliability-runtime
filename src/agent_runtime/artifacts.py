"""Artifact fingerprints, semantic validation, and atomic persistence."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from agent_runtime.domain import (
    Accounting,
    AgentEvent,
    AttemptOutcome,
    ContentDigests,
    FailureRecord,
    FinalDecision,
    ModelAttempt,
    RunArtifact,
    RunConfig,
    RunStatus,
    RuntimePhase,
    TaskSpec,
    ToolCall,
    ToolResult,
    is_valid_transition,
)
from agent_runtime.errors import ArtifactError
from agent_runtime.provenance import sha256_bytes
from agent_runtime.semantics import (
    REQUIRED_WORKER_IDS,
    SemanticValidationError,
    validate_stage_one_plan,
    validate_successful_decision,
    validate_worker_tool_request,
)

_PRIVATE_PATH = re.compile(r"(?:/(?:Users|home)/[^/\s]+|[A-Za-z]:\\Users\\[^\\\s]+)")
_SECRET_VALUE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{12,}\b|\bAKIA[0-9A-Z]{16}\b|\bBearer\s+[A-Za-z0-9._~+/-]{12,})"
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


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
        logical_model_turns=len({item.logical_turn_id for item in model_attempts}),
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
    model_attempts: list[ModelAttempt],
    tool_calls: list[ToolCall],
    tool_results: list[ToolResult],
    failures: list[FailureRecord],
    accounting: Accounting,
) -> str:
    model_outcomes = []
    for item in sorted(model_attempts, key=lambda value: (value.logical_turn_id, value.attempt)):
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
    if artifact.task != artifact.final_state.task:
        raise ArtifactError("artifact task does not match final_state.task")
    if artifact.final_decision != artifact.final_state.final_decision:
        raise ArtifactError("artifact final_decision does not match final_state.final_decision")
    sequences = [event.sequence for event in artifact.events]
    if sequences != list(range(1, len(sequences) + 1)):
        raise ArtifactError("artifact event sequences must be unique and strictly increasing")
    _validate_unique_identifiers(artifact)
    _validate_timestamps(artifact)
    expected_accounting = build_accounting(
        artifact.model_attempts, artifact.tool_calls, artifact.tool_results
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
        if artifact.final_decision is not None:
            raise ArtifactError("failed or cancelled artifact must not contain a final decision")
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
    _validate_attempt_trace(artifact)
    _validate_worker_evidence(artifact)
    _validate_persisted_plan(artifact)
    if artifact.status == RunStatus.SUCCEEDED:
        _validate_success_records(artifact)
    _validate_required_events(artifact)
    expected_semantic = semantic_fingerprint(
        digests=artifact.content_digests,
        final_decision=artifact.final_decision,
        events=artifact.events,
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
    _reject_private_or_secret_data(payload)


def _validate_attempt_trace(artifact: RunArtifact) -> None:
    event_spans = {(item.span_id, item.event_type) for item in artifact.events}
    for attempt in artifact.model_attempts:
        if attempt.outcome == AttemptOutcome.SUCCEEDED:
            if attempt.response is None or attempt.failure is not None:
                raise ArtifactError("successful model attempt has contradictory evidence")
        elif attempt.response is not None or attempt.failure is None:
            raise ArtifactError("failed model attempt has contradictory evidence")
        if (attempt.attempt_id, "model_attempt_start") not in event_spans:
            raise ArtifactError("model attempt is missing start event evidence")
        terminal = {
            AttemptOutcome.SUCCEEDED: "model_attempt_end",
            AttemptOutcome.FAILED: "model_attempt_failure",
            AttemptOutcome.TIMED_OUT: "model_attempt_timeout",
        }[attempt.outcome]
        if (attempt.attempt_id, terminal) not in event_spans:
            raise ArtifactError("model attempt is missing terminal event evidence")
    call_by_id = {item.call_id: item for item in artifact.tool_calls}
    for result in artifact.tool_results:
        call = call_by_id.get(result.tool_call_id)
        if call is None:
            raise ArtifactError("tool result references an unknown logical tool call")
        if result.worker_id != call.worker_id or result.tool_name != call.tool_name:
            raise ArtifactError("tool result identity does not match its logical tool call")
        if result.outcome == AttemptOutcome.SUCCEEDED:
            if result.output is None or result.failure is not None:
                raise ArtifactError("successful tool attempt has contradictory evidence")
        elif result.output is not None or result.failure is None:
            raise ArtifactError("failed tool attempt has contradictory evidence")
        if (result.attempt_span_id, "tool_attempt_start") not in event_spans:
            raise ArtifactError("tool attempt is missing start event evidence")
        terminal = {
            AttemptOutcome.SUCCEEDED: "tool_attempt_end",
            AttemptOutcome.FAILED: "tool_attempt_failure",
            AttemptOutcome.TIMED_OUT: "tool_attempt_timeout",
        }[result.outcome]
        if (result.attempt_span_id, terminal) not in event_spans:
            raise ArtifactError("tool attempt is missing terminal event evidence")

    model_groups: dict[str, list[ModelAttempt]] = defaultdict(list)
    request_context: dict[str, tuple[str, str]] = {}
    turn_requests: dict[str, str] = {}
    for attempt in artifact.model_attempts:
        context = (attempt.logical_turn_id, attempt.source_component)
        previous_context = request_context.setdefault(attempt.request_id, context)
        if previous_context != context:
            raise ArtifactError("model request ID is reused across logical request contexts")
        previous_request = turn_requests.setdefault(attempt.logical_turn_id, attempt.request_id)
        if previous_request != attempt.request_id:
            raise ArtifactError("logical model turn references multiple request IDs")
        model_groups[attempt.request_id].append(attempt)
    for attempts in model_groups.values():
        numbers = sorted(item.attempt for item in attempts)
        if numbers != list(range(1, len(numbers) + 1)):
            raise ArtifactError("model attempt numbers must be contiguous from 1")
        ordered = sorted(attempts, key=lambda item: item.attempt)
        successes = [item for item in ordered if item.outcome == AttemptOutcome.SUCCEEDED]
        if len(successes) > 1 or (successes and ordered[-1] != successes[0]):
            raise ArtifactError("successful model attempt must be unique and terminal")

    tool_groups: dict[str, list[ToolResult]] = defaultdict(list)
    for result in artifact.tool_results:
        tool_groups[result.tool_call_id].append(result)
    for results in tool_groups.values():
        numbers = sorted(item.attempt for item in results)
        if numbers != list(range(1, len(numbers) + 1)):
            raise ArtifactError("tool attempt numbers must be contiguous from 1")


def _validate_persisted_plan(artifact: RunArtifact) -> None:
    plan = artifact.final_state.plan
    if plan is None:
        return
    try:
        validate_stage_one_plan(plan)
    except SemanticValidationError as error:
        raise ArtifactError(str(error), code=error.code) from error


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
        if not worker.succeeded:
            continue
        calls = calls_by_worker.get(worker.worker_id, [])
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


def _validate_unique_identifiers(artifact: RunArtifact) -> None:
    _require_unique((item.event_id for item in artifact.events), "event_id")
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


def _require_unique(values: Any, label: str) -> None:
    observed = list(values)
    if len(observed) != len(set(observed)):
        raise ArtifactError(f"artifact {label} values must be unique")


def _validate_timestamps(artifact: RunArtifact) -> None:
    started = _parse_utc_timestamp(artifact.started_at, "run started_at")
    completed = _parse_utc_timestamp(artifact.completed_at, "run completed_at")
    if started > completed:
        raise ArtifactError("run started_at must not be after completed_at")
    for event in artifact.events:
        _parse_utc_timestamp(event.timestamp, "event timestamp")
    for attempt in artifact.model_attempts:
        attempt_started = _parse_utc_timestamp(attempt.started_at, "model attempt started_at")
        attempt_completed = _parse_utc_timestamp(attempt.completed_at, "model attempt completed_at")
        if attempt_started > attempt_completed:
            raise ArtifactError("model attempt started_at must not be after completed_at")
        if attempt.failure is not None:
            _parse_utc_timestamp(attempt.failure.timestamp, "model attempt failure timestamp")
    for result in artifact.tool_results:
        result_started = _parse_utc_timestamp(result.started_at, "tool attempt started_at")
        result_completed = _parse_utc_timestamp(result.completed_at, "tool attempt completed_at")
        if result_started > result_completed:
            raise ArtifactError("tool attempt started_at must not be after completed_at")
        if result.failure is not None:
            _parse_utc_timestamp(result.failure.timestamp, "tool attempt failure timestamp")
    for failure in artifact.failures:
        _parse_utc_timestamp(failure.timestamp, "failure timestamp")
    for worker in artifact.final_state.worker_results:
        if worker.failure is not None:
            _parse_utc_timestamp(worker.failure.timestamp, "worker failure timestamp")


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

    starts = matching("run_start", "runtime", RuntimePhase.INITIALIZED)
    if len(starts) != 1 or starts[0].sequence != 1:
        raise ArtifactError("artifact requires exactly one initial runtime run_start event")
    if not matching("supervisor_planning_start", "supervisor", RuntimePhase.PLANNING):
        raise ArtifactError("artifact is missing supervisor planning start evidence")
    if not matching("artifact_persistence_start", "artifact", artifact.final_state.phase):
        raise ArtifactError("artifact is missing persistence-start evidence")

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
        current = next_phase
    if current != artifact.final_state.phase:
        raise ArtifactError("artifact transition chain does not reach the final state")

    plan = artifact.final_state.plan
    if plan is not None:
        dispatch_ids = {
            cast(str, event.payload.get("worker_id"))
            for event in matching("worker_dispatch", "supervisor", RuntimePhase.EXECUTING_WORKERS)
        }
        if dispatch_ids != {item.worker_id for item in plan.assignments}:
            raise ArtifactError("artifact worker-dispatch evidence does not match the plan")
    for result in artifact.final_state.worker_results:
        if not matching("worker_start", result.worker_id, RuntimePhase.EXECUTING_WORKERS):
            raise ArtifactError("artifact is missing worker-start evidence")
        terminal = "worker_end" if result.succeeded else "worker_failure"
        if not matching(terminal, result.worker_id, RuntimePhase.EXECUTING_WORKERS):
            raise ArtifactError("artifact is missing worker terminal evidence")

    if artifact.status == RunStatus.SUCCEEDED:
        required = (
            ("supervisor_planning_end", "supervisor", RuntimePhase.PLANNING),
            ("supervisor_finalization_start", "supervisor", RuntimePhase.FINALIZING),
            ("supervisor_finalization_end", "supervisor", RuntimePhase.FINALIZING),
            ("run_success", "runtime", RuntimePhase.SUCCEEDED),
        )
        if any(not matching(*item) for item in required):
            raise ArtifactError("successful artifact is missing required lifecycle evidence")
    elif artifact.status == RunStatus.FAILED:
        if not matching("run_failure", phase=RuntimePhase.FAILED):
            raise ArtifactError("failed artifact is missing run_failure evidence")
    elif not matching("run_cancellation", "runtime", RuntimePhase.CANCELLED):
        raise ArtifactError("cancelled artifact is missing run_cancellation evidence")


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
