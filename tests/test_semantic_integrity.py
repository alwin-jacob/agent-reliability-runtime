from __future__ import annotations

import asyncio
from pathlib import Path
from typing import NoReturn

import pytest
from conftest import BarrierProbe, make_loaded, success_script

import agent_runtime.runtime as runtime_module
from agent_runtime.artifacts import (
    attach_content_hash,
    configuration_fingerprint,
    semantic_fingerprint,
    validate_artifact,
)
from agent_runtime.domain import RunArtifact
from agent_runtime.errors import ArtifactError
from agent_runtime.runtime import execute_loaded


def _reseal(artifact: RunArtifact) -> RunArtifact:
    artifact = artifact.model_copy(
        update={
            "configuration_fingerprint": configuration_fingerprint(
                artifact.content_digests,
                artifact.task,
                artifact.run_config,
            ),
            "semantic_fingerprint": semantic_fingerprint(
                digests=artifact.content_digests,
                final_decision=artifact.final_decision,
                events=artifact.events,
                model_attempts=artifact.model_attempts,
                tool_calls=artifact.tool_calls,
                tool_results=artifact.tool_results,
                failures=artifact.failures,
                accounting=artifact.accounting,
            ),
        }
    )
    return attach_content_hash(artifact)


async def _successful_artifact(tmp_path: Path) -> RunArtifact:
    return await execute_loaded(make_loaded(tmp_path), output_path=tmp_path / "run.json")


@pytest.mark.parametrize("field", ["market", "item_category", "purchase_channel"])
@pytest.mark.asyncio
async def test_resealed_order_policy_context_mismatch_is_rejected(
    tmp_path: Path,
    field: str,
) -> None:
    artifact = await _successful_artifact(tmp_path)
    policy_result_index = next(
        index
        for index, item in enumerate(artifact.tool_results)
        if item.worker_id == "policy-worker"
    )
    policy_worker_index = next(
        index
        for index, item in enumerate(artifact.final_state.worker_results)
        if item.worker_id == "policy-worker"
    )
    replacement = {
        "market": "CA",
        "item_category": "electronics",
        "purchase_channel": "store",
    }[field]
    tool_output = dict(artifact.tool_results[policy_result_index].output or {})
    tool_output[field] = replacement
    worker_output = dict(artifact.final_state.worker_results[policy_worker_index].output or {})
    worker_output[field] = replacement
    tool_results = list(artifact.tool_results)
    tool_results[policy_result_index] = tool_results[policy_result_index].model_copy(
        update={"output": tool_output}
    )
    worker_results = list(artifact.final_state.worker_results)
    worker_results[policy_worker_index] = worker_results[policy_worker_index].model_copy(
        update={"output": worker_output}
    )
    final_state = artifact.final_state.model_copy(update={"worker_results": worker_results})
    mutated = _reseal(
        artifact.model_copy(update={"tool_results": tool_results, "final_state": final_state})
    )

    with pytest.raises(ArtifactError):
        validate_artifact(mutated)


@pytest.mark.parametrize(
    "updates",
    [
        {"eligible": False},
        {"decision_code": "RETURN_INELIGIBLE"},
    ],
)
@pytest.mark.asyncio
async def test_resealed_incorrect_final_decision_is_rejected(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    artifact = await _successful_artifact(tmp_path)
    assert artifact.final_decision is not None
    decision = artifact.final_decision.model_copy(update=updates)
    final_state = artifact.final_state.model_copy(update={"final_decision": decision})
    mutated = _reseal(
        artifact.model_copy(update={"final_decision": decision, "final_state": final_state})
    )

    with pytest.raises(ArtifactError):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_success_requires_explicit_durable_decision_date(tmp_path: Path) -> None:
    artifact = await _successful_artifact(tmp_path)
    assert hasattr(artifact.task, "as_of_date")
    assert artifact.final_decision is not None
    assert hasattr(artifact.final_decision, "as_of_date")


@pytest.mark.asyncio
async def test_resealed_top_level_final_state_task_mismatch_is_rejected(tmp_path: Path) -> None:
    artifact = await _successful_artifact(tmp_path)
    other_task = artifact.final_state.task.model_copy(update={"order_id": "ORD-OTHER"})
    final_state = artifact.final_state.model_copy(update={"task": other_task})
    mutated = _reseal(artifact.model_copy(update={"final_state": final_state}))

    with pytest.raises(ArtifactError):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_top_level_final_state_decision_mismatch_is_rejected(tmp_path: Path) -> None:
    artifact = await _successful_artifact(tmp_path)
    assert artifact.final_state.final_decision is not None
    other_decision = artifact.final_state.final_decision.model_copy(
        update={"reason": "contradictory final-state reason"}
    )
    final_state = artifact.final_state.model_copy(update={"final_decision": other_decision})
    mutated = _reseal(artifact.model_copy(update={"final_state": final_state}))

    with pytest.raises(ArtifactError):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_worker_output_tool_evidence_mismatch_is_rejected(tmp_path: Path) -> None:
    artifact = await _successful_artifact(tmp_path)
    worker_index = next(
        index
        for index, item in enumerate(artifact.final_state.worker_results)
        if item.worker_id == "policy-worker"
    )
    worker = artifact.final_state.worker_results[worker_index]
    output = dict(worker.output or {})
    output["applicable_fees"] = "restocking fee"
    workers = list(artifact.final_state.worker_results)
    workers[worker_index] = worker.model_copy(update={"output": output})
    final_state = artifact.final_state.model_copy(update={"worker_results": workers})
    mutated = _reseal(artifact.model_copy(update={"final_state": final_state}))

    with pytest.raises(ArtifactError):
        validate_artifact(mutated)


@pytest.mark.parametrize("mutation", ["output", "call"])
@pytest.mark.asyncio
async def test_resealed_failed_run_still_reconciles_its_successful_worker(
    tmp_path: Path,
    mutation: str,
) -> None:
    script = success_script()
    script["responses"]["worker:policy-worker"] = [
        {"kind": "permanent_failure", "code": "scripted_failure"}
    ]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )
    if mutation == "output":
        workers = list(artifact.final_state.worker_results)
        worker_index = next(index for index, item in enumerate(workers) if item.succeeded)
        worker = workers[worker_index]
        output = dict(worker.output or {})
        output["market"] = "CA"
        workers[worker_index] = worker.model_copy(update={"output": output})
        final_state = artifact.final_state.model_copy(update={"worker_results": workers})
        artifact = artifact.model_copy(update={"final_state": final_state})
    else:
        calls = list(artifact.tool_calls)
        call_index = next(
            index for index, item in enumerate(calls) if item.worker_id == "order-worker"
        )
        calls[call_index] = calls[call_index].model_copy(
            update={"input": {"order_id": "ORD-OTHER"}}
        )
        artifact = artifact.model_copy(update={"tool_calls": calls})
    mutated = _reseal(artifact)

    with pytest.raises(ArtifactError):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_failed_run_cannot_contain_a_final_decision(tmp_path: Path) -> None:
    script = success_script()
    script["responses"]["worker:policy-worker"] = [
        {"kind": "permanent_failure", "code": "scripted_failure"}
    ]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )
    successful_decision = (await _successful_artifact(tmp_path / "success")).final_decision
    assert successful_decision is not None
    final_state = artifact.final_state.model_copy(update={"final_decision": successful_decision})
    mutated = _reseal(
        artifact.model_copy(
            update={"final_decision": successful_decision, "final_state": final_state}
        )
    )

    with pytest.raises(ArtifactError, match="must not contain a final decision"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_tool_result_tool_call_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    artifact = await _successful_artifact(tmp_path)
    call_by_id = {item.call_id: item for item in artifact.tool_calls}
    original = artifact.tool_results[0]
    call = call_by_id[original.tool_call_id]
    for update in (
        {"worker_id": "policy-worker" if call.worker_id == "order-worker" else "order-worker"},
        {
            "tool_name": (
                "lookup_return_policy" if call.tool_name == "lookup_order" else "lookup_order"
            )
        },
    ):
        result = original.model_copy(update=update)
        mutated = _reseal(
            artifact.model_copy(update={"tool_results": [result, *artifact.tool_results[1:]]})
        )

        with pytest.raises(ArtifactError):
            validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_duplicate_durable_identifier_is_rejected(tmp_path: Path) -> None:
    artifact = await _successful_artifact(tmp_path)
    duplicate = artifact.events[1].model_copy(update={"event_id": artifact.events[0].event_id})
    mutated = _reseal(
        artifact.model_copy(
            update={"events": [artifact.events[0], duplicate, *artifact.events[2:]]}
        )
    )

    with pytest.raises(ArtifactError):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_invalid_non_utc_timestamp_is_rejected(tmp_path: Path) -> None:
    artifact = await _successful_artifact(tmp_path)
    invalid = artifact.events[0].model_copy(update={"timestamp": "2026-08-25T12:00:00+01:00"})
    mutated = _reseal(artifact.model_copy(update={"events": [invalid, *artifact.events[1:]]}))

    with pytest.raises(ArtifactError):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_invalid_stage_one_plan_is_rejected(tmp_path: Path) -> None:
    artifact = await _successful_artifact(tmp_path)
    assert artifact.final_state.plan is not None
    assignments = list(artifact.final_state.plan.assignments)
    assignments[0] = assignments[0].model_copy(update={"purpose": "changed purpose"})
    plan = artifact.final_state.plan.model_copy(update={"assignments": assignments})
    final_state = artifact.final_state.model_copy(update={"plan": plan})
    mutated = _reseal(artifact.model_copy(update={"final_state": final_state}))

    with pytest.raises(ArtifactError, match="assignment contract"):
        validate_artifact(mutated)


@pytest.mark.parametrize("mutation", ["missing", "duplicate"])
@pytest.mark.asyncio
async def test_resealed_worker_result_cardinality_is_rejected(
    tmp_path: Path,
    mutation: str,
) -> None:
    artifact = await _successful_artifact(tmp_path)
    workers = list(artifact.final_state.worker_results)
    if mutation == "missing":
        workers = workers[:1]
    else:
        workers[1] = workers[1].model_copy(update={"worker_id": workers[0].worker_id})
    final_state = artifact.final_state.model_copy(update={"worker_results": workers})
    mutated = _reseal(artifact.model_copy(update={"final_state": final_state}))

    with pytest.raises(ArtifactError, match=r"exactly one result|worker IDs must be unique"):
        validate_artifact(mutated)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("order_id", "ORD-OTHER"),
        ("as_of_date", "2026-08-24"),
        ("days_since_delivery", 14),
        ("policy_window_days", 29),
        ("applicable_fees", "restocking fee"),
        ("evidence_worker_ids", ["order-worker", "other-worker"]),
    ],
)
@pytest.mark.asyncio
async def test_resealed_incorrect_structured_decision_field_is_rejected(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    artifact = await _successful_artifact(tmp_path)
    assert artifact.final_decision is not None
    decision = artifact.final_decision.model_copy(update={field: value})
    final_state = artifact.final_state.model_copy(update={"final_decision": decision})
    mutated = _reseal(
        artifact.model_copy(update={"final_decision": decision, "final_state": final_state})
    )

    with pytest.raises(ArtifactError):
        validate_artifact(mutated)


@pytest.mark.parametrize(
    "identifier",
    [
        "event_id",
        "model_attempt_id",
        "tool_call_id",
        "tool_result_id",
        "tool_attempt_span_id",
        "model_response_id",
    ],
)
@pytest.mark.asyncio
async def test_resealed_duplicate_durable_identifiers_are_rejected(
    tmp_path: Path,
    identifier: str,
) -> None:
    artifact = await _successful_artifact(tmp_path)
    if identifier == "event_id":
        events = list(artifact.events)
        events[1] = events[1].model_copy(update={"event_id": events[0].event_id})
        artifact = artifact.model_copy(update={"events": events})
    elif identifier == "model_attempt_id":
        model_attempts = list(artifact.model_attempts)
        model_attempts[1] = model_attempts[1].model_copy(
            update={"attempt_id": model_attempts[0].attempt_id}
        )
        artifact = artifact.model_copy(update={"model_attempts": model_attempts})
    elif identifier == "tool_call_id":
        calls = list(artifact.tool_calls)
        old_call_id = calls[1].call_id
        calls[1] = calls[1].model_copy(update={"call_id": calls[0].call_id})
        results = [
            item.model_copy(update={"tool_call_id": calls[0].call_id})
            if item.tool_call_id == old_call_id
            else item
            for item in artifact.tool_results
        ]
        artifact = artifact.model_copy(update={"tool_calls": calls, "tool_results": results})
    elif identifier == "tool_result_id":
        tool_results = list(artifact.tool_results)
        tool_results[1] = tool_results[1].model_copy(
            update={"result_id": tool_results[0].result_id}
        )
        artifact = artifact.model_copy(update={"tool_results": tool_results})
    elif identifier == "tool_attempt_span_id":
        tool_results = list(artifact.tool_results)
        tool_results[1] = tool_results[1].model_copy(
            update={"attempt_span_id": tool_results[0].attempt_span_id}
        )
        artifact = artifact.model_copy(update={"tool_results": tool_results})
    else:
        model_attempts = list(artifact.model_attempts)
        first_response = model_attempts[0].response
        second_response = model_attempts[1].response
        assert first_response is not None and second_response is not None
        model_attempts[1] = model_attempts[1].model_copy(
            update={
                "response": second_response.model_copy(
                    update={"response_id": first_response.response_id}
                )
            }
        )
        artifact = artifact.model_copy(update={"model_attempts": model_attempts})
    mutated = _reseal(artifact)

    with pytest.raises(ArtifactError, match="unique"):
        validate_artifact(mutated)


@pytest.mark.parametrize("attempt_kind", ["model", "tool"])
@pytest.mark.asyncio
async def test_resealed_noncontiguous_attempt_number_is_rejected(
    tmp_path: Path,
    attempt_kind: str,
) -> None:
    artifact = await _successful_artifact(tmp_path)
    if attempt_kind == "model":
        model_attempts = list(artifact.model_attempts)
        model_attempts[0] = model_attempts[0].model_copy(update={"attempt": 2})
        artifact = artifact.model_copy(update={"model_attempts": model_attempts})
    else:
        tool_results = list(artifact.tool_results)
        tool_results[0] = tool_results[0].model_copy(update={"attempt": 2})
        artifact = artifact.model_copy(update={"tool_results": tool_results})
    mutated = _reseal(artifact)

    with pytest.raises(ArtifactError, match="contiguous"):
        validate_artifact(mutated)


@pytest.mark.parametrize("record", ["run", "event", "model", "tool"])
@pytest.mark.asyncio
async def test_resealed_non_utc_durable_timestamp_is_rejected(
    tmp_path: Path,
    record: str,
) -> None:
    artifact = await _successful_artifact(tmp_path)
    invalid = "2026-08-25T12:00:00+01:00"
    if record == "run":
        artifact = artifact.model_copy(update={"started_at": invalid})
    elif record == "event":
        events = list(artifact.events)
        events[0] = events[0].model_copy(update={"timestamp": invalid})
        artifact = artifact.model_copy(update={"events": events})
    elif record == "model":
        model_attempts = list(artifact.model_attempts)
        model_attempts[0] = model_attempts[0].model_copy(update={"started_at": invalid})
        artifact = artifact.model_copy(update={"model_attempts": model_attempts})
    else:
        tool_results = list(artifact.tool_results)
        tool_results[0] = tool_results[0].model_copy(update={"started_at": invalid})
        artifact = artifact.model_copy(update={"tool_results": tool_results})
    mutated = _reseal(artifact)

    with pytest.raises(ArtifactError, match="UTC timestamp ending in Z"):
        validate_artifact(mutated)


@pytest.mark.parametrize("record", ["run", "model", "tool"])
@pytest.mark.asyncio
async def test_resealed_reversed_durable_interval_is_rejected(
    tmp_path: Path,
    record: str,
) -> None:
    artifact = await _successful_artifact(tmp_path)
    later = "2026-08-25T12:00:01Z"
    earlier = "2026-08-25T12:00:00Z"
    if record == "run":
        artifact = artifact.model_copy(update={"started_at": later, "completed_at": earlier})
    elif record == "model":
        model_attempts = list(artifact.model_attempts)
        model_attempts[0] = model_attempts[0].model_copy(
            update={"started_at": later, "completed_at": earlier}
        )
        artifact = artifact.model_copy(update={"model_attempts": model_attempts})
    else:
        tool_results = list(artifact.tool_results)
        tool_results[0] = tool_results[0].model_copy(
            update={"started_at": later, "completed_at": earlier}
        )
        artifact = artifact.model_copy(update={"tool_results": tool_results})
    mutated = _reseal(artifact)

    with pytest.raises(ArtifactError, match="must not be after"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_resealed_invalid_nested_failure_timestamp_is_rejected(tmp_path: Path) -> None:
    script = success_script()
    script["responses"]["worker:order-worker"] = [
        {"kind": "permanent_failure", "code": "scripted_failure"}
    ]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )
    attempt_index = next(
        index for index, item in enumerate(artifact.model_attempts) if item.failure is not None
    )
    attempt = artifact.model_attempts[attempt_index]
    assert attempt.failure is not None
    invalid_failure = attempt.failure.model_copy(update={"timestamp": "not-a-timestamp"})
    attempts = list(artifact.model_attempts)
    attempts[attempt_index] = attempt.model_copy(update={"failure": invalid_failure})
    mutated = _reseal(artifact.model_copy(update={"model_attempts": attempts}))

    with pytest.raises(ArtifactError, match="UTC timestamp ending in Z"):
        validate_artifact(mutated)

    worker_index = next(
        index
        for index, item in enumerate(artifact.final_state.worker_results)
        if item.failure is not None
    )
    workers = list(artifact.final_state.worker_results)
    worker_failure = workers[worker_index].failure
    assert worker_failure is not None
    workers[worker_index] = workers[worker_index].model_copy(
        update={"failure": worker_failure.model_copy(update={"timestamp": "not-a-timestamp"})}
    )
    final_state = artifact.final_state.model_copy(update={"worker_results": workers})
    mutated = _reseal(artifact.model_copy(update={"final_state": final_state}))

    with pytest.raises(ArtifactError, match="UTC timestamp ending in Z"):
        validate_artifact(mutated)


@pytest.mark.asyncio
async def test_cancelled_error_survives_cancelled_artifact_persistence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = success_script()
    waiting = [{"kind": "wait_for_cancellation"}]
    script["responses"]["worker:order-worker"] = waiting
    script["responses"]["worker:policy-worker"] = waiting
    loaded = make_loaded(tmp_path, script=script)
    probe = BarrierProbe()

    def fail_write(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise ArtifactError("scripted cancellation persistence failure")

    monkeypatch.setattr(runtime_module, "write_artifact", fail_write)
    task = asyncio.create_task(
        execute_loaded(
            loaded,
            output_path=tmp_path / "cancelled.json",
            concurrency_probe=probe,
        )
    )
    await probe.all_entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert any(
        "cancelled-artifact persistence failed" in note
        for note in getattr(caught.value, "__notes__", [])
    )
