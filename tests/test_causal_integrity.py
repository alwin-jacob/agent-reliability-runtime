from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from conftest import make_loaded, scripted_registry, success_script
from pydantic import ValidationError

from agent_runtime.artifacts import (
    attach_content_hash,
    build_accounting,
    configuration_fingerprint,
    read_artifact,
    semantic_fingerprint,
    validate_artifact,
)
from agent_runtime.domain import (
    AgentEvent,
    AttemptOutcome,
    FailureRecord,
    ModelRequestRecord,
    RunArtifact,
    RunStatus,
    RuntimePhase,
)
from agent_runtime.errors import ArtifactError
from agent_runtime.integrity import canonical_sha256
from agent_runtime.runtime import execute_loaded


async def _successful_artifact(tmp_path: Path) -> RunArtifact:
    return await execute_loaded(make_loaded(tmp_path), output_path=tmp_path / "run.json")


def _reseal(artifact: RunArtifact) -> RunArtifact:
    requests = [
        item.model_copy(update={"payload_sha256": canonical_sha256(item.payload)})
        for item in artifact.model_requests
    ]
    accounting = build_accounting(
        requests,
        artifact.model_attempts,
        artifact.tool_calls,
        artifact.tool_results,
    )
    artifact = artifact.model_copy(
        update={
            "model_requests": requests,
            "accounting": accounting,
            "configuration_fingerprint": configuration_fingerprint(
                artifact.content_digests, artifact.task, artifact.run_config
            ),
        }
    )
    semantic = semantic_fingerprint(
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
    return attach_content_hash(artifact.model_copy(update={"semantic_fingerprint": semantic}))


def _resequence(events: list[AgentEvent]) -> list[AgentEvent]:
    return [item.model_copy(update={"sequence": index}) for index, item in enumerate(events, 1)]


def _replace_event(
    artifact: RunArtifact, event_id: str, *, updates: dict[str, object]
) -> RunArtifact:
    events = [
        item.model_copy(update=updates) if item.event_id == event_id else item
        for item in artifact.events
    ]
    return artifact.model_copy(update={"events": events})


def _insert_before_persistence(artifact: RunArtifact, event: AgentEvent) -> RunArtifact:
    events = list(artifact.events)
    events.insert(-1, event.model_copy(update={"timestamp": events[-2].timestamp}))
    return artifact.model_copy(update={"events": _resequence(events)})


def _attempt_for_turn(artifact: RunArtifact, turn: str) -> tuple[int, Any]:
    return next(
        (index, item)
        for index, item in enumerate(artifact.model_attempts)
        if item.logical_turn_id == turn and item.outcome == AttemptOutcome.SUCCEEDED
    )


def _replace_nested_failure(
    artifact: RunArtifact,
    *,
    failure: FailureRecord,
    replacement: FailureRecord,
) -> list[FailureRecord]:
    return [
        replacement if item.failure_id == failure.failure_id else item for item in artifact.failures
    ]


def _move_event_before(
    artifact: RunArtifact, *, moving_event_id: str, target_event_id: str
) -> RunArtifact:
    events = list(artifact.events)
    moving = next(item for item in events if item.event_id == moving_event_id)
    events.remove(moving)
    target_index = next(
        index for index, item in enumerate(events) if item.event_id == target_event_id
    )
    events.insert(target_index, moving)
    events = [item.model_copy(update={"timestamp": artifact.started_at}) for item in events]
    return artifact.model_copy(update={"events": _resequence(events)})


def test_model_request_record_is_strict_and_requires_sha256_shape() -> None:
    with pytest.raises(ValidationError):
        ModelRequestRecord.model_validate(
            {
                "request_id": "request-1",
                "logical_turn_id": "turn-supervisor-plan",
                "source_component": "supervisor",
                "phase": "planning",
                "fixture_key": "supervisor_plan",
                "created_at": "2026-08-25T00:00:00Z",
                "payload": {},
                "payload_sha256": "bad",
                "unknown": True,
            }
        )


@pytest.mark.asyncio
async def test_resealed_model_request_payload_digest_mismatch_is_rejected(tmp_path: Path) -> None:
    artifact = await _successful_artifact(tmp_path)
    request = artifact.model_requests[0].model_copy(update={"payload_sha256": "0" * 64})
    mutated = attach_content_hash(
        artifact.model_copy(update={"model_requests": [request, *artifact.model_requests[1:]]})
    )

    with pytest.raises(ArtifactError, match="payload_sha256"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_model_request_payload_is_scanned_for_secret_like_values(tmp_path: Path) -> None:
    artifact = await _successful_artifact(tmp_path)
    request = artifact.model_requests[0].model_copy(
        update={"payload": {"authorization": "Bearer abcdefghijklmnopqrstuvwxyz"}}
    )
    mutated = _reseal(
        artifact.model_copy(update={"model_requests": [request, *artifact.model_requests[1:]]})
    )

    with pytest.raises(ArtifactError, match="secret-like"):
        validate_artifact(mutated)


@pytest.mark.parametrize("field", ["request_id", "logical_turn_id"])
@pytest.mark.asyncio
async def test_resealed_duplicate_model_request_identity_is_rejected(
    tmp_path: Path, field: str
) -> None:
    artifact = await _successful_artifact(tmp_path)
    requests = list(artifact.model_requests)
    requests[1] = requests[1].model_copy(update={field: getattr(requests[0], field)})
    mutated = _reseal(artifact.model_copy(update={"model_requests": requests}))

    with pytest.raises(ArtifactError, match="unique"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_model_attempt_referencing_absent_request_is_rejected(
    tmp_path: Path,
) -> None:
    artifact = await _successful_artifact(tmp_path)
    attempts = list(artifact.model_attempts)
    attempts[0] = attempts[0].model_copy(update={"request_id": "request-absent"})
    mutated = _reseal(artifact.model_copy(update={"model_attempts": attempts}))

    with pytest.raises(ArtifactError, match="absent durable request"):
        validate_artifact(mutated)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_component", "wrong-source"),
        ("phase", RuntimePhase.FINALIZING),
    ],
)
@pytest.mark.asyncio
async def test_resealed_model_attempt_request_context_mismatch_is_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    artifact = await _successful_artifact(tmp_path)
    attempts = list(artifact.model_attempts)
    attempts[0] = attempts[0].model_copy(update={field: value})
    mutated = _reseal(artifact.model_copy(update={"model_attempts": attempts}))

    with pytest.raises(ArtifactError, match="durable request"):
        validate_artifact(mutated)


@pytest.mark.parametrize(
    "turn",
    [
        "turn-supervisor-plan",
        "turn-order-worker",
        "turn-policy-worker",
        "turn-supervisor-finalize",
    ],
)
@pytest.mark.asyncio
async def test_resealed_success_missing_required_logical_turn_is_rejected(
    tmp_path: Path, turn: str
) -> None:
    artifact = await _successful_artifact(tmp_path)
    request_ids = {
        item.request_id for item in artifact.model_requests if item.logical_turn_id == turn
    }
    attempt_ids = {
        item.attempt_id for item in artifact.model_attempts if item.request_id in request_ids
    }
    requests = [item for item in artifact.model_requests if item.request_id not in request_ids]
    attempts = [item for item in artifact.model_attempts if item.request_id not in request_ids]
    events = [item for item in artifact.events if item.span_id not in attempt_ids]
    mutated = _reseal(
        artifact.model_copy(
            update={
                "model_requests": requests,
                "model_attempts": attempts,
                "events": _resequence(events),
            }
        )
    )

    with pytest.raises(ArtifactError):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_success_with_extra_unrelated_logical_turn_is_rejected(
    tmp_path: Path,
) -> None:
    artifact = await _successful_artifact(tmp_path)
    extra = artifact.model_requests[0].model_copy(
        update={
            "request_id": "request-extra",
            "logical_turn_id": "turn-unrelated",
            "fixture_key": "unrelated",
            "payload": {},
            "payload_sha256": canonical_sha256({}),
        }
    )
    mutated = _reseal(
        artifact.model_copy(update={"model_requests": [*artifact.model_requests, extra]})
    )

    with pytest.raises(ArtifactError, match="unrelated"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_success_without_any_model_attempts_is_rejected(tmp_path: Path) -> None:
    artifact = await _successful_artifact(tmp_path)
    events = [item for item in artifact.events if not item.event_type.startswith("model_attempt_")]
    mutated = _reseal(
        artifact.model_copy(update={"model_attempts": [], "events": _resequence(events)})
    )

    with pytest.raises(ArtifactError, match="terminal successful provider response"):
        validate_artifact(mutated)


@pytest.mark.parametrize(
    ("turn", "response_value", "message"),
    [
        (
            "turn-supervisor-plan",
            {
                "assignments": [
                    {
                        "worker_id": "order-worker",
                        "allowed_tools": ["lookup_order"],
                        "purpose": "different purpose",
                    },
                    {
                        "worker_id": "policy-worker",
                        "allowed_tools": ["lookup_return_policy"],
                        "purpose": "retrieve the applicable return policy",
                    },
                ]
            },
            "planner terminal response",
        ),
        (
            "turn-order-worker",
            {"tool_name": "lookup_order", "arguments": {"order_id": "ORD-OTHER"}},
            "worker terminal response",
        ),
        (
            "turn-policy-worker",
            {
                "tool_name": "lookup_return_policy",
                "arguments": {
                    "market": "CA",
                    "item_category": "household",
                    "purchase_channel": "online",
                },
            },
            "worker terminal response",
        ),
    ],
)
@pytest.mark.asyncio
async def test_resealed_terminal_response_action_mismatch_is_rejected(
    tmp_path: Path,
    turn: str,
    response_value: dict[str, object],
    message: str,
) -> None:
    artifact = await _successful_artifact(tmp_path)
    index, attempt = _attempt_for_turn(artifact, turn)
    assert attempt.response is not None
    attempts = list(artifact.model_attempts)
    attempts[index] = attempt.model_copy(
        update={
            "response": attempt.response.model_copy(
                update={"raw_json": json.dumps(response_value, separators=(",", ":"))}
            )
        }
    )
    mutated = _reseal(artifact.model_copy(update={"model_attempts": attempts}))

    with pytest.raises(ArtifactError, match=message):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_finalizer_response_decision_mismatch_is_rejected(tmp_path: Path) -> None:
    artifact = await _successful_artifact(tmp_path)
    index, attempt = _attempt_for_turn(artifact, "turn-supervisor-finalize")
    assert attempt.response is not None and artifact.final_decision is not None
    other = artifact.final_decision.model_copy(update={"reason": "different reason"})
    attempts = list(artifact.model_attempts)
    attempts[index] = attempt.model_copy(
        update={
            "response": attempt.response.model_copy(update={"raw_json": other.model_dump_json()})
        }
    )
    mutated = _reseal(artifact.model_copy(update={"model_attempts": attempts}))

    with pytest.raises(ArtifactError, match="finalizer terminal response"):
        validate_artifact(mutated)


@pytest.mark.parametrize("orphan_type", ["start", "terminal", "duplicate_terminal"])
@pytest.mark.asyncio
async def test_resealed_orphan_or_duplicate_model_attempt_event_is_rejected(
    tmp_path: Path, orphan_type: str
) -> None:
    artifact = await _successful_artifact(tmp_path)
    attempt = artifact.model_attempts[0]
    start = next(
        item
        for item in artifact.events
        if item.span_id == attempt.attempt_id and item.event_type == "model_attempt_start"
    )
    terminal = next(
        item
        for item in artifact.events
        if item.span_id == attempt.attempt_id and item.event_type == "model_attempt_end"
    )
    original = start if orphan_type == "start" else terminal
    span_id = attempt.attempt_id if orphan_type == "duplicate_terminal" else "orphan-model-span"
    extra = original.model_copy(update={"event_id": f"evt-{uuid4().hex}", "span_id": span_id})
    mutated = _reseal(_insert_before_persistence(artifact, extra))

    with pytest.raises(ArtifactError, match=r"orphan|exactly one"):
        validate_artifact(mutated)


@pytest.mark.parametrize("orphan_type", ["start", "terminal"])
@pytest.mark.asyncio
async def test_resealed_orphan_tool_attempt_event_is_rejected(
    tmp_path: Path, orphan_type: str
) -> None:
    artifact = await _successful_artifact(tmp_path)
    result = artifact.tool_results[0]
    event_type = "tool_attempt_start" if orphan_type == "start" else "tool_attempt_end"
    original = next(
        item
        for item in artifact.events
        if item.span_id == result.attempt_span_id and item.event_type == event_type
    )
    extra = original.model_copy(
        update={"event_id": f"evt-{uuid4().hex}", "span_id": "orphan-tool-span"}
    )
    mutated = _reseal(_insert_before_persistence(artifact, extra))

    with pytest.raises(ArtifactError, match="orphan tool-attempt"):
        validate_artifact(mutated)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("payload", {"attempt": 999}),
        ("source_component", "wrong-source"),
        ("phase", RuntimePhase.FINALIZING),
        ("parent_span_id", "wrong-parent"),
    ],
)
@pytest.mark.asyncio
async def test_resealed_model_attempt_event_context_mismatch_is_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    artifact = await _successful_artifact(tmp_path)
    attempt = artifact.model_attempts[0]
    event = next(
        item
        for item in artifact.events
        if item.span_id == attempt.attempt_id and item.event_type == "model_attempt_end"
    )
    mutated = _reseal(_replace_event(artifact, event.event_id, updates={field: value}))

    with pytest.raises(ArtifactError, match="model-attempt event"):
        validate_artifact(mutated)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("payload", {"attempt": 999}),
        ("source_component", "wrong-source"),
        ("phase", RuntimePhase.PLANNING),
        ("parent_span_id", "wrong-parent"),
    ],
)
@pytest.mark.asyncio
async def test_resealed_tool_attempt_event_context_mismatch_is_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    artifact = await _successful_artifact(tmp_path)
    result = artifact.tool_results[0]
    event = next(
        item
        for item in artifact.events
        if item.span_id == result.attempt_span_id and item.event_type == "tool_attempt_end"
    )
    mutated = _reseal(_replace_event(artifact, event.event_id, updates={field: value}))

    with pytest.raises(ArtifactError, match="tool-attempt event"):
        validate_artifact(mutated)


async def _failed_planner_artifact(tmp_path: Path) -> RunArtifact:
    script = success_script()
    script["responses"]["supervisor_plan"] = [{"kind": "permanent_failure", "code": "planner_down"}]
    return await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )


@pytest.mark.asyncio
async def test_resealed_duplicate_failure_id_is_rejected(tmp_path: Path) -> None:
    artifact = await _failed_planner_artifact(tmp_path)
    duplicate = artifact.failures[0].model_copy(update={"message": "contradictory copy"})
    mutated = _reseal(artifact.model_copy(update={"failures": [*artifact.failures, duplicate]}))

    with pytest.raises(ArtifactError, match=r"failure_id.*unique"):
        validate_artifact(mutated)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt", 2),
        ("model_attempt_id", "wrong-attempt"),
        ("tool_call_id", "wrong-call"),
        ("phase", RuntimePhase.FINALIZING),
        ("source_component", "wrong-source"),
        ("span_id", "wrong-span"),
    ],
)
@pytest.mark.asyncio
async def test_resealed_nested_model_failure_parent_mismatch_is_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    artifact = await _failed_planner_artifact(tmp_path)
    attempt = artifact.model_attempts[0]
    assert attempt.failure is not None
    replacement = attempt.failure.model_copy(update={field: value})
    attempts = [attempt.model_copy(update={"failure": replacement})]
    failures = _replace_nested_failure(artifact, failure=attempt.failure, replacement=replacement)
    mutated = _reseal(
        artifact.model_copy(update={"model_attempts": attempts, "failures": failures})
    )

    with pytest.raises(ArtifactError):
        validate_artifact(mutated)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt", 3),
        ("tool_call_id", "wrong-call"),
        ("model_attempt_id", "wrong-attempt"),
        ("phase", RuntimePhase.FINALIZING),
        ("source_component", "wrong-source"),
        ("span_id", "wrong-span"),
    ],
)
@pytest.mark.asyncio
async def test_resealed_nested_tool_failure_parent_mismatch_is_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    loaded = make_loaded(tmp_path)
    registry, _ = scripted_registry(order_transient_failures=5)
    artifact = await execute_loaded(
        replace(loaded, tools=registry), output_path=tmp_path / "failed.json"
    )
    index = next(
        index for index, item in enumerate(artifact.tool_results) if item.failure is not None
    )
    result = artifact.tool_results[index]
    assert result.failure is not None
    replacement = result.failure.model_copy(update={field: value})
    results = list(artifact.tool_results)
    results[index] = result.model_copy(update={"failure": replacement})
    failures = _replace_nested_failure(artifact, failure=result.failure, replacement=replacement)
    mutated = _reseal(artifact.model_copy(update={"tool_results": results, "failures": failures}))

    with pytest.raises(ArtifactError):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_nested_failure_absent_from_top_level_is_rejected(
    tmp_path: Path,
) -> None:
    script = success_script()
    success = script["responses"]["supervisor_plan"][0]
    script["responses"]["supervisor_plan"] = [
        {"kind": "transient_failure", "code": "planner_busy"},
        success,
    ]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "run.json"
    )
    nested = next(item.failure for item in artifact.model_attempts if item.failure is not None)
    failures = [item for item in artifact.failures if item.failure_id != nested.failure_id]
    mutated = _reseal(artifact.model_copy(update={"failures": failures}))

    with pytest.raises(ArtifactError, match="absent from the top-level"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_worker_failure_absent_from_top_level_is_rejected(tmp_path: Path) -> None:
    script = success_script()
    script["responses"]["worker:policy-worker"] = [
        {"kind": "permanent_failure", "code": "policy_down"}
    ]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )
    worker = next(item for item in artifact.final_state.worker_results if not item.succeeded)
    assert worker.failure is not None
    failures = [item for item in artifact.failures if item.failure_id != worker.failure.failure_id]
    mutated = _reseal(artifact.model_copy(update={"failures": failures}))

    with pytest.raises(ArtifactError, match="absent from the top-level"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_run_failure_event_dangling_reference_is_rejected(tmp_path: Path) -> None:
    artifact = await _failed_planner_artifact(tmp_path)
    event = next(item for item in artifact.events if item.event_type == "run_failure")
    payload = {"failure_id": "failure-absent", "failure_code": "absent_code"}
    mutated = _reseal(_replace_event(artifact, event.event_id, updates={"payload": payload}))

    with pytest.raises(ArtifactError, match="absent or contradictory failure"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_failed_worker_with_terminal_tool_success_is_rejected(
    tmp_path: Path,
) -> None:
    script = success_script()
    script["responses"]["supervisor_finalize"] = [
        {"kind": "permanent_failure", "code": "finalizer_down"}
    ]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )
    failure = artifact.failures[-1]
    workers = list(artifact.final_state.worker_results)
    index = next(index for index, item in enumerate(workers) if item.worker_id == "order-worker")
    workers[index] = workers[index].model_copy(
        update={"succeeded": False, "tool_name": None, "output": None, "failure": failure}
    )
    final_state = artifact.final_state.model_copy(update={"worker_results": workers})
    mutated = _reseal(artifact.model_copy(update={"final_state": final_state}))

    with pytest.raises(ArtifactError, match="contradicts terminal successful tool"):
        validate_artifact(mutated)


@pytest.mark.parametrize(
    "mutation",
    [
        "tool_before_model",
        "worker_before_tool",
        "execution_before_planning_end",
        "success_before_finalization_end",
    ],
)
@pytest.mark.asyncio
async def test_resealed_cross_record_causal_order_violation_is_rejected(
    tmp_path: Path, mutation: str
) -> None:
    artifact = await _successful_artifact(tmp_path)
    order_request = next(
        item for item in artifact.model_requests if item.logical_turn_id == "turn-order-worker"
    )
    order_attempt = next(
        item for item in artifact.model_attempts if item.request_id == order_request.request_id
    )
    order_result = next(item for item in artifact.tool_results if item.worker_id == "order-worker")
    by_type = {item.event_type: item for item in artifact.events}
    model_end = next(
        item
        for item in artifact.events
        if item.span_id == order_attempt.attempt_id and item.event_type == "model_attempt_end"
    )
    tool_start = next(
        item
        for item in artifact.events
        if item.span_id == order_result.attempt_span_id and item.event_type == "tool_attempt_start"
    )
    tool_end = next(
        item
        for item in artifact.events
        if item.span_id == order_result.attempt_span_id and item.event_type == "tool_attempt_end"
    )
    worker_end = next(
        item
        for item in artifact.events
        if item.event_type == "worker_end" and item.source_component == "order-worker"
    )
    if mutation == "tool_before_model":
        changed = _move_event_before(
            artifact, moving_event_id=tool_start.event_id, target_event_id=model_end.event_id
        )
    elif mutation == "worker_before_tool":
        changed = _move_event_before(
            artifact, moving_event_id=worker_end.event_id, target_event_id=tool_end.event_id
        )
    elif mutation == "execution_before_planning_end":
        execution = next(
            item
            for item in artifact.events
            if item.event_type == "state_transition"
            and item.phase == RuntimePhase.EXECUTING_WORKERS
        )
        changed = _move_event_before(
            artifact,
            moving_event_id=execution.event_id,
            target_event_id=by_type["supervisor_planning_end"].event_id,
        )
    else:
        changed = _move_event_before(
            artifact,
            moving_event_id=by_type["run_success"].event_id,
            target_event_id=by_type["supervisor_finalization_end"].event_id,
        )
    mutated = _reseal(changed)

    with pytest.raises(ArtifactError):
        validate_artifact(mutated)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_component", "ghost"),
        ("span_id", "ghost-span"),
        ("parent_span_id", "ghost-parent"),
    ],
)
@pytest.mark.asyncio
async def test_resealed_run_success_context_is_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    artifact = await _successful_artifact(tmp_path)
    event = next(item for item in artifact.events if item.event_type == "run_success")
    mutated = _reseal(_replace_event(artifact, event.event_id, updates={field: value}))

    with pytest.raises(ArtifactError, match="run_success context"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_model_retry_start_before_previous_terminal_is_rejected(
    tmp_path: Path,
) -> None:
    script = success_script()
    success = script["responses"]["supervisor_plan"][0]
    script["responses"]["supervisor_plan"] = [
        {"kind": "transient_failure", "code": "planner_busy"},
        success,
    ]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "run.json"
    )
    attempts = sorted(
        (
            item
            for item in artifact.model_attempts
            if item.logical_turn_id == "turn-supervisor-plan"
        ),
        key=lambda item: item.attempt,
    )
    first_terminal = next(
        item
        for item in artifact.events
        if item.span_id == attempts[0].attempt_id and item.event_type == "model_attempt_failure"
    )
    second_start = next(
        item
        for item in artifact.events
        if item.span_id == attempts[1].attempt_id and item.event_type == "model_attempt_start"
    )
    mutated = _reseal(
        _move_event_before(
            artifact,
            moving_event_id=second_start.event_id,
            target_event_id=first_terminal.event_id,
        )
    )

    with pytest.raises(ArtifactError, match="model retry attempt"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_tool_retry_start_before_previous_terminal_is_rejected(
    tmp_path: Path,
) -> None:
    loaded = make_loaded(tmp_path)
    registry, _ = scripted_registry(order_transient_failures=1)
    artifact = await execute_loaded(
        replace(loaded, tools=registry), output_path=tmp_path / "run.json"
    )
    attempts = sorted(
        (item for item in artifact.tool_results if item.worker_id == "order-worker"),
        key=lambda item: item.attempt,
    )
    first_terminal = next(
        item
        for item in artifact.events
        if item.span_id == attempts[0].attempt_span_id and item.event_type == "tool_attempt_failure"
    )
    second_start = next(
        item
        for item in artifact.events
        if item.span_id == attempts[1].attempt_span_id and item.event_type == "tool_attempt_start"
    )
    mutated = _reseal(
        _move_event_before(
            artifact,
            moving_event_id=second_start.event_id,
            target_event_id=first_terminal.event_id,
        )
    )

    with pytest.raises(ArtifactError, match="tool retry attempt"):
        validate_artifact(mutated)


@pytest.mark.parametrize(
    "event_type",
    [
        "run_start",
        "artifact_persistence_start",
        "supervisor_planning_start",
        "supervisor_planning_end",
        "worker_dispatch",
        "supervisor_finalization_start",
        "supervisor_finalization_end",
    ],
)
@pytest.mark.asyncio
async def test_resealed_wrong_context_lifecycle_duplicate_is_rejected(
    tmp_path: Path, event_type: str
) -> None:
    artifact = await _successful_artifact(tmp_path)
    original = next(item for item in artifact.events if item.event_type == event_type)
    duplicate = original.model_copy(
        update={
            "event_id": f"evt-{uuid4().hex}",
            "source_component": "ghost",
            "phase": RuntimePhase.CANCELLED,
            "span_id": "ghost-span",
            "parent_span_id": "ghost-parent",
        }
    )
    mutated = _reseal(_insert_before_persistence(artifact, duplicate))

    with pytest.raises(ArtifactError):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_state_transition_source_is_rejected(tmp_path: Path) -> None:
    artifact = await _successful_artifact(tmp_path)
    event = next(
        item
        for item in artifact.events
        if item.event_type == "state_transition" and item.phase == RuntimePhase.EXECUTING_WORKERS
    )
    mutated = _reseal(
        _replace_event(artifact, event.event_id, updates={"source_component": "ghost"})
    )

    with pytest.raises(ArtifactError, match="transition source"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_accepted_plan_requires_planning_end(tmp_path: Path) -> None:
    script = success_script()
    script["responses"]["worker:policy-worker"] = [
        {"kind": "permanent_failure", "code": "policy_down"}
    ]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )
    events = [item for item in artifact.events if item.event_type != "supervisor_planning_end"]
    mutated = _reseal(artifact.model_copy(update={"events": _resequence(events)}))

    with pytest.raises(ArtifactError, match="accepted plan requires"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_nonmonotonic_event_timestamp_is_rejected(tmp_path: Path) -> None:
    artifact = await _successful_artifact(tmp_path)
    persistence = artifact.events[-1].model_copy(update={"timestamp": "2000-01-01T00:00:00Z"})
    mutated = _reseal(artifact.model_copy(update={"events": [*artifact.events[:-1], persistence]}))

    with pytest.raises(ArtifactError, match=r"nondecreasing|run interval"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_unreferenced_top_level_failure_is_rejected(tmp_path: Path) -> None:
    artifact = await _failed_planner_artifact(tmp_path)
    extra = artifact.failures[0].model_copy(
        update={"failure_id": "failure-unreferenced", "message": "unreferenced copy"}
    )
    mutated = _reseal(artifact.model_copy(update={"failures": [*artifact.failures, extra]}))

    with pytest.raises(ArtifactError, match="unreferenced"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_unlinked_worker_failure_context_is_rejected(tmp_path: Path) -> None:
    script = success_script()
    script["responses"]["worker:policy-worker"] = [{"kind": "success", "raw_json": "{"}]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )
    worker_index = next(
        index
        for index, item in enumerate(artifact.final_state.worker_results)
        if item.worker_id == "policy-worker"
    )
    worker = artifact.final_state.worker_results[worker_index]
    assert worker.failure is not None
    replacement = worker.failure.model_copy(
        update={"source_component": "wrong-source", "span_id": "wrong-span"}
    )
    workers = list(artifact.final_state.worker_results)
    workers[worker_index] = worker.model_copy(update={"failure": replacement})
    failures = _replace_nested_failure(artifact, failure=worker.failure, replacement=replacement)
    final_state = artifact.final_state.model_copy(update={"worker_results": workers})
    mutated = _reseal(
        artifact.model_copy(update={"final_state": final_state, "failures": failures})
    )

    with pytest.raises(ArtifactError, match="causally linked"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_run_failure_context_is_rejected(tmp_path: Path) -> None:
    artifact = await _failed_planner_artifact(tmp_path)
    event = next(item for item in artifact.events if item.event_type == "run_failure")
    mutated = _reseal(
        _replace_event(
            artifact,
            event.event_id,
            updates={
                "source_component": "wrong-source",
                "span_id": "wrong-span",
                "parent_span_id": "wrong-parent",
            },
        )
    )

    with pytest.raises(ArtifactError, match="terminal run event"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_failed_worker_cannot_omit_model_request_chain(tmp_path: Path) -> None:
    script = success_script()
    script["responses"]["worker:policy-worker"] = [
        {"kind": "permanent_failure", "code": "policy_down"}
    ]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )
    request = next(
        item for item in artifact.model_requests if item.logical_turn_id == "turn-policy-worker"
    )
    attempt_ids = {
        item.attempt_id for item in artifact.model_attempts if item.request_id == request.request_id
    }
    requests = [item for item in artifact.model_requests if item.request_id != request.request_id]
    attempts = [item for item in artifact.model_attempts if item.request_id != request.request_id]
    events = [item for item in artifact.events if item.span_id not in attempt_ids]
    mutated = _reseal(
        artifact.model_copy(
            update={
                "model_requests": requests,
                "model_attempts": attempts,
                "events": _resequence(events),
            }
        )
    )

    with pytest.raises(ArtifactError, match="worker result requires"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_run_failure_cannot_reference_earlier_transient_failure(
    tmp_path: Path,
) -> None:
    script = success_script()
    script["responses"]["supervisor_plan"] = [
        {"kind": "transient_failure", "code": "planner_busy"},
        {"kind": "permanent_failure", "code": "planner_down"},
    ]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )
    earlier = next(item for item in artifact.failures if item.code == "planner_busy")
    event = next(item for item in artifact.events if item.event_type == "run_failure")
    mutated = _reseal(
        _replace_event(
            artifact,
            event.event_id,
            updates={
                "payload": {
                    "failure_id": earlier.failure_id,
                    "failure_code": earlier.code,
                }
            },
        )
    )

    with pytest.raises(ArtifactError, match="nonterminal model failure"):
        validate_artifact(mutated)


class _CancelAfterOrderCompletes:
    def __init__(self) -> None:
        self.order_completed = asyncio.Event()

    async def entered(self, worker_id: str, active: int) -> None:
        del worker_id, active

    async def exited(self, worker_id: str, active: int) -> None:
        del active
        if worker_id == "order-worker":
            self.order_completed.set()


async def _cancelled_after_order_artifact(tmp_path: Path) -> RunArtifact:
    script = success_script()
    script["responses"]["worker:policy-worker"] = [{"kind": "wait_for_cancellation"}]
    loaded = make_loaded(tmp_path, script=script)
    probe = _CancelAfterOrderCompletes()
    destination = tmp_path / "cancelled.json"
    task = asyncio.create_task(
        execute_loaded(loaded, output_path=destination, concurrency_probe=probe)
    )
    await asyncio.wait_for(probe.order_completed.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return read_artifact(destination)


@pytest.mark.asyncio
async def test_cancellation_preserves_accepted_plan_and_completed_worker(
    tmp_path: Path,
) -> None:
    artifact = await _cancelled_after_order_artifact(tmp_path)

    assert artifact.status == RunStatus.CANCELLED
    assert artifact.final_state.phase == RuntimePhase.CANCELLED
    assert artifact.final_state.plan is not None
    assert [item.worker_id for item in artifact.final_state.worker_results] == ["order-worker"]
    assert artifact.final_state.worker_results[0].succeeded is True
    assert artifact.final_decision is None
    assert {item.worker_id for item in artifact.tool_calls} == {"order-worker"}
    assert any(
        item.logical_turn_id == "turn-policy-worker"
        and item.failure is not None
        and item.failure.code == "invocation_cancelled"
        for item in artifact.model_attempts
    )


@pytest.mark.asyncio
async def test_resealed_cancellation_cleanup_payload_is_rejected(tmp_path: Path) -> None:
    artifact = await _cancelled_after_order_artifact(tmp_path)
    event = next(item for item in artifact.events if item.event_type == "run_cancellation")
    payload = dict(event.payload)
    payload["active_workers_after_cleanup"] = 99
    mutated = _reseal(_replace_event(artifact, event.event_id, updates={"payload": payload}))

    with pytest.raises(ArtifactError, match="cancellation payload"):
        validate_artifact(mutated)


@pytest.mark.parametrize("mutation", ["missing", "wrong_parent"])
@pytest.mark.asyncio
async def test_resealed_blocked_worker_request_requires_lifecycle_start(
    tmp_path: Path, mutation: str
) -> None:
    artifact = await _cancelled_after_order_artifact(tmp_path)
    start = next(
        item
        for item in artifact.events
        if item.event_type == "worker_start" and item.source_component == "policy-worker"
    )
    if mutation == "missing":
        events = [item for item in artifact.events if item.event_id != start.event_id]
        changed = artifact.model_copy(update={"events": _resequence(events)})
    else:
        changed = _replace_event(
            artifact,
            start.event_id,
            updates={"parent_span_id": "wrong-parent"},
        )
    mutated = _reseal(changed)

    with pytest.raises(ArtifactError, match=r"worker-start|worker lifecycle"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_one_loaded_input_constructs_fresh_fixture_provider_per_run(
    tmp_path: Path,
) -> None:
    script = success_script()
    success = script["responses"]["supervisor_plan"][0]
    script["responses"]["supervisor_plan"] = [
        {"kind": "transient_failure", "code": "planner_busy"},
        success,
    ]
    loaded = make_loaded(tmp_path, script=script)
    first = await execute_loaded(loaded, output_path=tmp_path / "first.json")
    second = await execute_loaded(loaded, output_path=tmp_path / "second.json")
    first_pattern = [
        item.outcome
        for item in first.model_attempts
        if item.logical_turn_id == "turn-supervisor-plan"
    ]
    second_pattern = [
        item.outcome
        for item in second.model_attempts
        if item.logical_turn_id == "turn-supervisor-plan"
    ]
    assert first.status == second.status == RunStatus.SUCCEEDED
    assert first_pattern == second_pattern == [AttemptOutcome.FAILED, AttemptOutcome.SUCCEEDED]
    assert first.semantic_fingerprint == second.semantic_fingerprint
    assert first.run_id != second.run_id
    assert {item.failure_id for item in first.failures}.isdisjoint(
        item.failure_id for item in second.failures
    )
