from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from conftest import EXPECTED_DECISION, make_loaded, scripted_registry, success_script

from agent_runtime import runtime as runtime_module
from agent_runtime.artifacts import (
    attach_content_hash,
    build_accounting,
    configuration_fingerprint,
    read_artifact,
    semantic_fingerprint,
    validate_artifact,
    write_artifact,
)
from agent_runtime.domain import (
    AgentEvent,
    AttemptOutcome,
    FailureOrigin,
    FailureRecord,
    RunArtifact,
    RunStatus,
    RuntimePhase,
)
from agent_runtime.errors import ArtifactError
from agent_runtime.events import EventRecorder
from agent_runtime.integrity import canonical_sha256
from agent_runtime.orchestration import AcceptedStateRecorder
from agent_runtime.runtime import execute_loaded


def _reseal(artifact: RunArtifact) -> RunArtifact:
    """Recompute every derived integrity value after an adversarial mutation."""

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
                artifact.content_digests,
                artifact.task,
                artifact.run_config,
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


async def _success(tmp_path: Path) -> RunArtifact:
    return await execute_loaded(make_loaded(tmp_path), output_path=tmp_path / "success.json")


def _replace_failure(
    artifact: RunArtifact,
    failure: FailureRecord,
    replacement: FailureRecord,
) -> RunArtifact:
    attempts = [
        item.model_copy(update={"failure": replacement}) if item.failure == failure else item
        for item in artifact.model_attempts
    ]
    results = [
        item.model_copy(update={"failure": replacement}) if item.failure == failure else item
        for item in artifact.tool_results
    ]
    workers = [
        item.model_copy(update={"failure": replacement}) if item.failure == failure else item
        for item in artifact.final_state.worker_results
    ]
    failures = [replacement if item == failure else item for item in artifact.failures]
    events = []
    for event in artifact.events:
        if event.payload.get("failure_id") != failure.failure_id:
            events.append(event)
            continue
        payload = dict(event.payload)
        payload["failure_code"] = replacement.code
        events.append(event.model_copy(update={"payload": payload}))
    final_state = artifact.final_state.model_copy(update={"worker_results": workers})
    return artifact.model_copy(
        update={
            "model_attempts": attempts,
            "tool_results": results,
            "final_state": final_state,
            "failures": failures,
            "events": events,
        }
    )


def _replace_response_raw(artifact: RunArtifact, turn: str, raw_json: str) -> RunArtifact:
    attempts = []
    for attempt in artifact.model_attempts:
        if attempt.logical_turn_id != turn or attempt.response is None:
            attempts.append(attempt)
            continue
        attempts.append(
            attempt.model_copy(
                update={"response": attempt.response.model_copy(update={"raw_json": raw_json})}
            )
        )
    return artifact.model_copy(update={"model_attempts": attempts})


def _replace_attempt_terminal_event(
    artifact: RunArtifact,
    *,
    span_id: str,
    event_type: str,
    outcome: AttemptOutcome,
) -> RunArtifact:
    terminal_types = {
        "model_attempt_end",
        "model_attempt_failure",
        "model_attempt_timeout",
        "tool_attempt_end",
        "tool_attempt_failure",
        "tool_attempt_timeout",
    }
    events = []
    for event in artifact.events:
        if event.span_id != span_id or event.event_type not in terminal_types:
            events.append(event)
            continue
        payload = dict(event.payload)
        payload["outcome"] = outcome.value
        events.append(event.model_copy(update={"event_type": event_type, "payload": payload}))
    return artifact.model_copy(update={"events": events})


async def _model_retry_then_success_artifact(tmp_path: Path) -> RunArtifact:
    script = success_script()
    success = script["responses"]["supervisor_plan"][0]
    script["responses"]["supervisor_plan"] = [
        {"kind": "transient_failure", "code": "planner_busy"},
        success,
    ]
    return await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "model-retry.json"
    )


async def _model_timeout_then_success_artifact(tmp_path: Path) -> RunArtifact:
    script = success_script()
    success = script["responses"]["supervisor_plan"][0]
    script["responses"]["supervisor_plan"] = [
        {**success, "delay_seconds": 0.05},
        success,
    ]
    loaded = make_loaded(
        tmp_path,
        script=script,
        config_changes={
            "model_retry": {
                "max_attempts": 2,
                "timeout_seconds": 0.001,
                "initial_backoff_seconds": 0.0,
                "max_backoff_seconds": 0.0,
                "jitter_ratio": 0.0,
            }
        },
    )
    return await execute_loaded(loaded, output_path=tmp_path / "model-timeout.json")


async def _tool_retry_then_success_artifact(tmp_path: Path) -> RunArtifact:
    loaded = make_loaded(tmp_path)
    registry, _ = scripted_registry(order_transient_failures=1)
    return await execute_loaded(
        replace(loaded, tools=registry), output_path=tmp_path / "tool-retry.json"
    )


async def _tool_timeout_artifact(tmp_path: Path) -> RunArtifact:
    loaded = make_loaded(
        tmp_path,
        config_changes={
            "tool_retry": {
                "max_attempts": 2,
                "timeout_seconds": 0.001,
                "initial_backoff_seconds": 0.0,
                "max_backoff_seconds": 0.0,
                "jitter_ratio": 0.0,
            }
        },
    )
    registry, _ = scripted_registry(order_delay=0.05)
    return await execute_loaded(
        replace(loaded, tools=registry), output_path=tmp_path / "tool-timeout.json"
    )


@pytest.mark.parametrize(
    "origin",
    [FailureOrigin.TOOL_EXECUTION, FailureOrigin.MODEL_OUTPUT],
    ids=["case-01-tool-execution-origin", "case-02-model-output-origin"],
)
@pytest.mark.asyncio
async def test_model_attempt_rejects_non_provider_retry_origin(
    tmp_path: Path, origin: FailureOrigin
) -> None:
    artifact = await _model_retry_then_success_artifact(tmp_path)
    failure = next(item for item in artifact.failures if item.code == "planner_busy")
    forged = _replace_failure(artifact, failure, failure.model_copy(update={"origin": origin}))

    with pytest.raises(ArtifactError, match=r"model.*(taxonomy|provider|failure)"):
        validate_artifact(_reseal(forged))


@pytest.mark.asyncio
async def test_model_attempt_rejects_retryable_unexpected_exception_followed_by_success(
    tmp_path: Path,
) -> None:
    artifact = await _model_retry_then_success_artifact(tmp_path)
    failure = next(item for item in artifact.failures if item.code == "planner_busy")
    replacement = failure.model_copy(
        update={
            "code": "unexpected_invocation_error",
            "origin": FailureOrigin.UNEXPECTED_INTERNAL,
            "retryable": True,
            "exception_type": "RuntimeError",
        }
    )

    with pytest.raises(ArtifactError, match=r"model.*unexpected.*(taxonomy|retry)"):
        validate_artifact(_reseal(_replace_failure(artifact, failure, replacement)))


@pytest.mark.asyncio
async def test_reserved_model_timeout_code_cannot_have_failed_outcome(tmp_path: Path) -> None:
    artifact = await _model_retry_then_success_artifact(tmp_path)
    failure = next(item for item in artifact.failures if item.code == "planner_busy")
    replacement = failure.model_copy(
        update={"code": "model_attempt_timeout", "exception_type": "TimeoutError"}
    )

    with pytest.raises(ArtifactError, match=r"model.*timeout.*(outcome|taxonomy|reserved)"):
        validate_artifact(_reseal(_replace_failure(artifact, failure, replacement)))


@pytest.mark.asyncio
async def test_timed_out_model_attempt_requires_timeout_error_exception(tmp_path: Path) -> None:
    artifact = await _model_timeout_then_success_artifact(tmp_path)
    failure = next(
        item.failure for item in artifact.model_attempts if item.outcome == AttemptOutcome.TIMED_OUT
    )
    assert failure is not None
    replacement = failure.model_copy(update={"exception_type": "RuntimeError"})

    with pytest.raises(ArtifactError, match=r"model.*timeout.*taxonomy"):
        validate_artifact(_reseal(_replace_failure(artifact, failure, replacement)))


@pytest.mark.parametrize(
    "origin",
    [
        FailureOrigin.TOOL_POLICY,
        FailureOrigin.TOOL_INPUT,
        FailureOrigin.TOOL_EXECUTION,
        FailureOrigin.TOOL_OUTPUT,
        FailureOrigin.CONFIGURATION,
        FailureOrigin.ARTIFACT,
        FailureOrigin.ORCHESTRATION,
    ],
)
@pytest.mark.asyncio
async def test_model_attempt_rejects_non_provider_failure_origins(
    tmp_path: Path, origin: FailureOrigin
) -> None:
    artifact = await _model_retry_then_success_artifact(tmp_path)
    failure = next(item for item in artifact.failures if item.code == "planner_busy")
    forged = _replace_failure(artifact, failure, failure.model_copy(update={"origin": origin}))

    with pytest.raises(ArtifactError, match=r"model.*(taxonomy|provider|failure)"):
        validate_artifact(_reseal(forged))


@pytest.mark.parametrize(
    "origin",
    [FailureOrigin.MODEL_PROVIDER, FailureOrigin.MODEL_OUTPUT],
    ids=["case-07-model-provider-origin", "case-08-model-output-origin"],
)
@pytest.mark.asyncio
async def test_tool_attempt_rejects_model_origins(tmp_path: Path, origin: FailureOrigin) -> None:
    artifact = await _tool_retry_then_success_artifact(tmp_path)
    failure = next(item for item in artifact.failures if item.code == "tool_transient")
    forged = _replace_failure(artifact, failure, failure.model_copy(update={"origin": origin}))

    with pytest.raises(ArtifactError, match=r"tool.*(taxonomy|execution|output|failure)"):
        validate_artifact(_reseal(forged))


@pytest.mark.asyncio
async def test_tool_attempt_rejects_retryable_unexpected_exception_followed_by_success(
    tmp_path: Path,
) -> None:
    artifact = await _tool_retry_then_success_artifact(tmp_path)
    failure = next(item for item in artifact.failures if item.code == "tool_transient")
    replacement = failure.model_copy(
        update={
            "code": "unexpected_invocation_error",
            "origin": FailureOrigin.UNEXPECTED_INTERNAL,
            "retryable": True,
            "exception_type": "RuntimeError",
        }
    )

    with pytest.raises(ArtifactError, match=r"tool.*unexpected.*(taxonomy|retry)"):
        validate_artifact(_reseal(_replace_failure(artifact, failure, replacement)))


@pytest.mark.asyncio
async def test_reserved_tool_timeout_code_cannot_have_failed_outcome(tmp_path: Path) -> None:
    artifact = await _tool_retry_then_success_artifact(tmp_path)
    failure = next(item for item in artifact.failures if item.code == "tool_transient")
    replacement = failure.model_copy(
        update={"code": "tool_attempt_timeout", "exception_type": "TimeoutError"}
    )

    with pytest.raises(ArtifactError, match=r"tool.*timeout.*(outcome|taxonomy|reserved)"):
        validate_artifact(_reseal(_replace_failure(artifact, failure, replacement)))


@pytest.mark.asyncio
async def test_timed_out_tool_attempt_requires_timeout_error_exception(tmp_path: Path) -> None:
    artifact = await _tool_timeout_artifact(tmp_path)
    failure = next(
        item.failure for item in artifact.tool_results if item.outcome == AttemptOutcome.TIMED_OUT
    )
    assert failure is not None
    replacement = failure.model_copy(update={"exception_type": "RuntimeError"})

    with pytest.raises(ArtifactError, match=r"tool.*timeout.*taxonomy"):
        validate_artifact(_reseal(_replace_failure(artifact, failure, replacement)))


@pytest.mark.parametrize("origin", [FailureOrigin.TOOL_POLICY, FailureOrigin.TOOL_INPUT])
@pytest.mark.asyncio
async def test_tool_attempt_rejects_pre_invocation_origins(
    tmp_path: Path, origin: FailureOrigin
) -> None:
    artifact = await _tool_retry_then_success_artifact(tmp_path)
    failure = next(item for item in artifact.failures if item.code == "tool_transient")
    forged = _replace_failure(artifact, failure, failure.model_copy(update={"origin": origin}))

    with pytest.raises(ArtifactError, match=r"tool.*(taxonomy|execution|output|failure)"):
        validate_artifact(_reseal(forged))


@pytest.mark.asyncio
async def test_tool_output_failure_cannot_be_retryable_or_followed_by_another_attempt(
    tmp_path: Path,
) -> None:
    artifact = await _tool_retry_then_success_artifact(tmp_path)
    failure = next(item for item in artifact.failures if item.code == "tool_transient")
    replacement = failure.model_copy(
        update={
            "code": "tool_output_invalid",
            "origin": FailureOrigin.TOOL_OUTPUT,
            "exception_type": "ToolOutputError",
        }
    )

    with pytest.raises(ArtifactError, match=r"tool.*output.*(nonretryable|terminal|taxonomy)"):
        validate_artifact(_reseal(_replace_failure(artifact, failure, replacement)))


@pytest.mark.parametrize(
    ("raw_json", "failure_code"),
    [
        ("{", "malformed_model_json"),
        ('{"assignments":[]}', "model_output_schema_invalid"),
        (
            json.dumps(
                {
                    "assignments": [
                        {
                            "worker_id": "order-worker",
                            "allowed_tools": ["lookup_order"],
                            "purpose": "wrong",
                        },
                        {
                            "worker_id": "policy-worker",
                            "allowed_tools": ["lookup_return_policy"],
                            "purpose": "retrieve the applicable return policy",
                        },
                    ]
                }
            ),
            "supervisor_plan_contract_invalid",
        ),
    ],
    ids=["malformed-json", "pydantic-schema", "semantic-contract"],
)
@pytest.mark.asyncio
async def test_model_output_failure_cannot_predate_the_response_it_classifies(
    tmp_path: Path,
    raw_json: str,
    failure_code: str,
) -> None:
    script = success_script()
    script["responses"]["supervisor_plan"] = [{"kind": "success", "raw_json": raw_json}]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / f"{failure_code}.json"
    )
    terminal = next(
        item for item in artifact.model_attempts if item.logical_turn_id == "turn-supervisor-plan"
    )
    failure = next(item for item in artifact.failures if item.code == failure_code)
    assert artifact.started_at < terminal.completed_at
    replacement = failure.model_copy(update={"timestamp": artifact.started_at})

    with pytest.raises(ArtifactError, match=r"model-output failure.*provider.*completion"):
        validate_artifact(_reseal(_replace_failure(artifact, failure, replacement)))


@pytest.mark.parametrize(
    ("turn", "lifecycle_event"),
    [
        ("turn-supervisor-plan", "supervisor_planning_start"),
        ("turn-order-worker", "worker_start"),
        ("turn-policy-worker", "worker_start"),
        ("turn-supervisor-finalize", "supervisor_finalization_start"),
    ],
    ids=[
        "case-15-planner",
        "case-16-order-worker",
        "case-16-policy-worker",
        "case-17-finalizer",
    ],
)
@pytest.mark.asyncio
async def test_model_request_cannot_predate_its_lifecycle_start(
    tmp_path: Path,
    turn: str,
    lifecycle_event: str,
) -> None:
    artifact = await _success(tmp_path)
    requests = list(artifact.model_requests)
    index = next(index for index, item in enumerate(requests) if item.logical_turn_id == turn)
    lifecycle = next(
        item
        for item in artifact.events
        if item.event_type == lifecycle_event
        and (
            lifecycle_event != "worker_start"
            or item.source_component == requests[index].source_component
        )
    )
    run_start = next(item for item in artifact.events if item.event_type == "run_start")
    assert run_start.timestamp < lifecycle.timestamp
    requests[index] = requests[index].model_copy(update={"created_at": run_start.timestamp})

    with pytest.raises(ArtifactError, match=r"model request.*lifecycle.*start"):
        validate_artifact(_reseal(artifact.model_copy(update={"model_requests": requests})))


@pytest.mark.asyncio
async def test_success_path_retains_exact_stage_one_attempt_shape(tmp_path: Path) -> None:
    artifact = await _success(tmp_path)

    assert artifact.status == RunStatus.SUCCEEDED
    assert len(artifact.model_requests) == 4
    assert len(artifact.model_attempts) == 4
    assert all(item.outcome == AttemptOutcome.SUCCEEDED for item in artifact.model_attempts)
    assert len(artifact.tool_results) == 2
    assert all(item.outcome == AttemptOutcome.SUCCEEDED for item in artifact.tool_results)
    assert artifact.failures == []
    assert artifact.accounting.cost_usd == 0.0
    assert artifact.final_decision == EXPECTED_DECISION


@pytest.mark.asyncio
async def test_resealed_nonretryable_model_attempt_cannot_be_followed_by_success(
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
    failure = next(item for item in artifact.failures if item.code == "planner_busy")
    forged = _replace_failure(
        artifact,
        failure,
        failure.model_copy(update={"retryable": False}),
    )

    with pytest.raises(ArtifactError, match=r"nonretryable|retryable"):
        validate_artifact(_reseal(forged))


@pytest.mark.asyncio
async def test_resealed_nonretryable_tool_attempt_cannot_be_followed_by_retry(
    tmp_path: Path,
) -> None:
    loaded = make_loaded(tmp_path)
    registry, _ = scripted_registry(order_transient_failures=1)
    artifact = await execute_loaded(
        replace(loaded, tools=registry), output_path=tmp_path / "run.json"
    )
    failure = next(item for item in artifact.failures if item.code == "tool_transient")
    forged = _replace_failure(
        artifact,
        failure,
        failure.model_copy(update={"retryable": False}),
    )

    with pytest.raises(ArtifactError, match=r"nonretryable|retryable"):
        validate_artifact(_reseal(forged))


@pytest.mark.parametrize("kind", ["model", "tool"])
@pytest.mark.asyncio
async def test_resealed_attempts_cannot_exceed_effective_retry_maximum(
    tmp_path: Path,
    kind: str,
) -> None:
    if kind == "model":
        script = success_script()
        success = script["responses"]["supervisor_plan"][0]
        script["responses"]["supervisor_plan"] = [
            {"kind": "transient_failure", "code": "planner_busy"},
            success,
        ]
        artifact = await execute_loaded(
            make_loaded(tmp_path, script=script), output_path=tmp_path / "run.json"
        )
        policy = artifact.run_config.model_retry.model_copy(update={"max_attempts": 1})
        config = artifact.run_config.model_copy(update={"model_retry": policy})
    else:
        loaded = make_loaded(tmp_path)
        registry, _ = scripted_registry(order_transient_failures=1)
        artifact = await execute_loaded(
            replace(loaded, tools=registry), output_path=tmp_path / "run.json"
        )
        policy = artifact.run_config.tool_retry.model_copy(update={"max_attempts": 1})
        config = artifact.run_config.model_copy(update={"tool_retry": policy})

    with pytest.raises(ArtifactError, match=r"max_attempts|maximum"):
        validate_artifact(_reseal(artifact.model_copy(update={"run_config": config})))


@pytest.mark.asyncio
async def test_resealed_timeout_requires_exact_runtime_taxonomy(tmp_path: Path) -> None:
    script = success_script()
    script["responses"]["supervisor_plan"] = [
        {"kind": "success", "delay_seconds": 0.1, "raw_json": "{}"}
    ]
    loaded = make_loaded(
        tmp_path,
        script=script,
        config_changes={
            "model_retry": {
                "max_attempts": 1,
                "timeout_seconds": 0.001,
                "initial_backoff_seconds": 0.0,
                "max_backoff_seconds": 0.0,
                "jitter_ratio": 0.0,
            }
        },
    )
    artifact = await execute_loaded(loaded, output_path=tmp_path / "timeout.json")
    failure = artifact.model_attempts[0].failure
    assert failure is not None
    forged = _replace_failure(
        artifact,
        failure,
        failure.model_copy(
            update={
                "code": "wrong_timeout",
                "origin": FailureOrigin.ORCHESTRATION,
                "retryable": False,
            }
        ),
    )

    with pytest.raises(ArtifactError, match=r"timeout"):
        validate_artifact(_reseal(forged))


class _EnteredProbe:
    def __init__(self) -> None:
        self.entered_event = asyncio.Event()

    async def entered(self, worker_id: str, active: int) -> None:
        del worker_id, active
        self.entered_event.set()

    async def exited(self, worker_id: str, active: int) -> None:
        del worker_id, active


async def _cancelled_worker_artifact(tmp_path: Path) -> RunArtifact:
    script = success_script()
    script["responses"]["worker:order-worker"] = [{"kind": "wait_for_cancellation"}]
    script["responses"]["worker:policy-worker"] = [{"kind": "wait_for_cancellation"}]
    probe = _EnteredProbe()
    destination = tmp_path / "cancelled.json"
    task = asyncio.create_task(
        execute_loaded(
            make_loaded(tmp_path, script=script),
            output_path=destination,
            concurrency_probe=probe,
        )
    )
    await asyncio.wait_for(probe.entered_event.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return read_artifact(destination)


@pytest.mark.parametrize("mutation", ["code", "origin", "retryable", "outcome", "code_and_origin"])
@pytest.mark.asyncio
async def test_resealed_invocation_cancellation_requires_exact_taxonomy_and_outcome(
    tmp_path: Path,
    mutation: str,
) -> None:
    artifact = await _cancelled_worker_artifact(tmp_path)
    index, attempt = next(
        (index, item)
        for index, item in enumerate(artifact.model_attempts)
        if item.failure is not None and item.failure.code == "invocation_cancelled"
    )
    failure = attempt.failure
    assert failure is not None
    failure_updates: dict[str, Any] = {
        "code": ("wrong_cancellation" if mutation in {"code", "code_and_origin"} else failure.code),
        "origin": (
            FailureOrigin.MODEL_PROVIDER
            if mutation in {"origin", "code_and_origin"}
            else failure.origin
        ),
        "retryable": True if mutation == "retryable" else failure.retryable,
    }
    replacement = failure.model_copy(update=failure_updates)
    forged = _replace_failure(artifact, failure, replacement)
    if mutation == "outcome":
        attempts = list(forged.model_attempts)
        attempts[index] = attempts[index].model_copy(update={"outcome": AttemptOutcome.TIMED_OUT})
        forged = forged.model_copy(update={"model_attempts": attempts})
        forged = _replace_attempt_terminal_event(
            forged,
            span_id=attempt.attempt_id,
            event_type="model_attempt_timeout",
            outcome=AttemptOutcome.TIMED_OUT,
        )

    with pytest.raises(ArtifactError, match=r"cancellation|cancelled"):
        validate_artifact(_reseal(forged))


@pytest.mark.asyncio
async def test_resealed_nonterminal_failed_attempt_requires_retryable_failure(
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
    failure = next(item for item in artifact.failures if item.code == "planner_busy")
    forged = _replace_failure(
        artifact,
        failure,
        failure.model_copy(update={"retryable": False}),
    )

    with pytest.raises(ArtifactError, match=r"nonterminal|retryable"):
        validate_artifact(_reseal(forged))


@pytest.mark.asyncio
async def test_resealed_failed_run_cannot_stop_early_on_retryable_failure(
    tmp_path: Path,
) -> None:
    script = success_script()
    script["responses"]["supervisor_plan"] = [{"kind": "permanent_failure", "code": "planner_down"}]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )
    failure = artifact.model_attempts[0].failure
    assert failure is not None
    forged = _replace_failure(
        artifact,
        failure,
        failure.model_copy(update={"retryable": True}),
    )

    with pytest.raises(ArtifactError, match=r"configured maximum|retryable"):
        validate_artifact(_reseal(forged))


async def _failed_output_artifact(tmp_path: Path, stage: str, raw_json: str) -> RunArtifact:
    script = success_script()
    key = {
        "planner": "supervisor_plan",
        "worker": "worker:order-worker",
        "finalizer": "supervisor_finalize",
    }[stage]
    script["responses"][key] = [{"kind": "success", "raw_json": raw_json}]
    return await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / f"{stage}.json"
    )


@pytest.mark.asyncio
async def test_failed_planning_cannot_retain_valid_plan_response_and_claim_malformed_json(
    tmp_path: Path,
) -> None:
    artifact = await _failed_output_artifact(tmp_path, "planner", "{")
    valid = str(success_script()["responses"]["supervisor_plan"][0]["raw_json"])

    with pytest.raises(ArtifactError, match=r"contradict|accepted plan|unexpected_internal"):
        validate_artifact(_reseal(_replace_response_raw(artifact, "turn-supervisor-plan", valid)))


@pytest.mark.asyncio
async def test_valid_planner_response_may_precede_a_linked_unexpected_acceptance_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_acceptance(plan: Any) -> None:
        del plan
        raise RuntimeError("deterministic acceptance fault")

    monkeypatch.setattr("agent_runtime.orchestration.validate_stage_one_plan", fail_acceptance)
    artifact = await execute_loaded(make_loaded(tmp_path), output_path=tmp_path / "unexpected.json")

    assert artifact.status == RunStatus.FAILED
    assert artifact.final_state.plan is None
    failure = next(item for item in artifact.failures if item.code == "unexpected_internal_error")
    assert failure.origin == FailureOrigin.UNEXPECTED_INTERNAL
    validate_artifact(artifact)


@pytest.mark.asyncio
async def test_failed_worker_cannot_retain_valid_task_bound_request_without_tool_call(
    tmp_path: Path,
) -> None:
    artifact = await _failed_output_artifact(tmp_path, "worker", "{")
    valid = json.dumps(
        {"tool_name": "lookup_order", "arguments": {"order_id": artifact.task.order_id}},
        separators=(",", ":"),
    )

    with pytest.raises(ArtifactError, match=r"contradict|tool call|unexpected_internal"):
        validate_artifact(_reseal(_replace_response_raw(artifact, "turn-order-worker", valid)))


@pytest.mark.asyncio
async def test_failed_finalization_cannot_retain_valid_decision_and_claim_parse_failure(
    tmp_path: Path,
) -> None:
    artifact = await _failed_output_artifact(tmp_path, "finalizer", "{")

    with pytest.raises(ArtifactError, match=r"contradict|final decision|unexpected_internal"):
        validate_artifact(
            _reseal(
                _replace_response_raw(
                    artifact,
                    "turn-supervisor-finalize",
                    EXPECTED_DECISION.model_dump_json(),
                )
            )
        )


@pytest.mark.parametrize("mutation", ["code", "origin"])
@pytest.mark.asyncio
async def test_invalid_terminal_json_requires_failure_code_matching_parse_result(
    tmp_path: Path,
    mutation: str,
) -> None:
    artifact = await _failed_output_artifact(tmp_path, "planner", "{")
    failure = next(item for item in artifact.failures if item.code == "malformed_model_json")
    forged = _replace_failure(
        artifact,
        failure,
        failure.model_copy(
            update={
                "code": ("model_output_schema_invalid" if mutation == "code" else failure.code),
                "origin": (FailureOrigin.ORCHESTRATION if mutation == "origin" else failure.origin),
            }
        ),
    )

    with pytest.raises(ArtifactError, match=r"malformed_model_json|parse result|causally linked"):
        validate_artifact(_reseal(forged))


@pytest.mark.parametrize("stage", ["planner", "worker", "finalizer"])
@pytest.mark.asyncio
async def test_semantically_invalid_terminal_output_requires_matching_failure_code(
    tmp_path: Path,
    stage: str,
) -> None:
    raw = {
        "planner": json.dumps(
            {
                "assignments": [
                    {
                        "worker_id": "order-worker",
                        "allowed_tools": ["lookup_order"],
                        "purpose": "wrong",
                    },
                    {
                        "worker_id": "policy-worker",
                        "allowed_tools": ["lookup_return_policy"],
                        "purpose": "retrieve the applicable return policy",
                    },
                ]
            }
        ),
        "worker": json.dumps({"tool_name": "lookup_order", "arguments": {"order_id": "ORD-WRONG"}}),
        "finalizer": EXPECTED_DECISION.model_copy(
            update={"days_since_delivery": 14}
        ).model_dump_json(),
    }[stage]
    artifact = await _failed_output_artifact(tmp_path, stage, raw)
    failure = next(
        item
        for item in artifact.failures
        if item.origin == FailureOrigin.MODEL_OUTPUT and item.code != "malformed_model_json"
    )
    forged = _replace_failure(
        artifact,
        failure,
        failure.model_copy(update={"code": "unrelated_model_output_failure"}),
    )

    with pytest.raises(ArtifactError, match=r"failure code|contract|semantic"):
        validate_artifact(_reseal(forged))


@pytest.mark.parametrize(
    ("worker_id", "failed_fixture_key", "malformed_output"),
    [
        (
            "order-worker",
            "worker:policy-worker",
            {
                "order_id": "ORD-1001",
                "item_category": "household",
                "delivery_status": "delivered",
                "purchase_channel": "online",
                "market": "US",
            },
        ),
        (
            "policy-worker",
            "worker:order-worker",
            {
                "market": "US",
                "item_category": "household",
                "purchase_channel": "online",
                "return_window_days": "thirty",
                "item_condition_requirement": "unopened",
                "applicable_fees": "none",
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_failed_artifact_revalidates_every_partial_successful_tool_output(
    tmp_path: Path,
    worker_id: str,
    failed_fixture_key: str,
    malformed_output: dict[str, Any],
) -> None:
    script = success_script()
    script["responses"][failed_fixture_key] = [
        {"kind": "permanent_failure", "code": "other_worker_failed"}
    ]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )
    results = [
        item.model_copy(update={"output": malformed_output})
        if item.worker_id == worker_id and item.outcome == AttemptOutcome.SUCCEEDED
        else item
        for item in artifact.tool_results
    ]
    workers = [
        item.model_copy(update={"output": malformed_output})
        if item.worker_id == worker_id and item.succeeded
        else item
        for item in artifact.final_state.worker_results
    ]
    final_state = artifact.final_state.model_copy(update={"worker_results": workers})
    forged = artifact.model_copy(update={"tool_results": results, "final_state": final_state})

    with pytest.raises(ArtifactError, match=r"tool output|Lookup|output is invalid"):
        validate_artifact(_reseal(forged))


@pytest.mark.parametrize(
    ("case", "updates"),
    [
        ("nonzero_cost", {"usage": {"cost_usd": 1.25}}),
        (
            "provider_measured_tokens",
            {
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "token_source": "provider_measured",
                }
            },
        ),
        ("wrong_fixture_key", {"metadata": {"fixture_key": "wrong", "behavior": "success"}}),
        (
            "wrong_behavior",
            {"metadata": {"fixture_key": "supervisor_plan", "behavior": "transient_failure"}},
        ),
        ("wrong_finish_reason", {"finish_reason": "stop"}),
    ],
)
@pytest.mark.asyncio
async def test_successful_fixture_response_contract_is_enforced(
    tmp_path: Path,
    case: str,
    updates: dict[str, Any],
) -> None:
    del case
    artifact = await _success(tmp_path)
    attempts = list(artifact.model_attempts)
    index = next(
        index
        for index, item in enumerate(attempts)
        if item.logical_turn_id == "turn-supervisor-plan"
    )
    attempt = attempts[index]
    assert attempt.response is not None
    response = attempt.response
    response_updates = dict(updates)
    usage_updates = response_updates.pop("usage", None)
    if usage_updates is not None:
        response_updates["usage"] = response.usage.model_copy(update=usage_updates)
    attempts[index] = attempt.model_copy(
        update={"response": response.model_copy(update=response_updates)}
    )

    with pytest.raises(ArtifactError, match=r"fixture|cost|token|finish|metadata|behavior"):
        validate_artifact(_reseal(artifact.model_copy(update={"model_attempts": attempts})))


@pytest.mark.asyncio
async def test_successful_fixture_responses_require_one_consistent_model_id(tmp_path: Path) -> None:
    artifact = await _success(tmp_path)
    attempts = list(artifact.model_attempts)
    attempt = attempts[0]
    assert attempt.response is not None
    attempts[0] = attempt.model_copy(
        update={"response": attempt.response.model_copy(update={"model_id": "other-fixture"})}
    )

    with pytest.raises(ArtifactError, match=r"model ID|model_id|consistent"):
        validate_artifact(_reseal(artifact.model_copy(update={"model_attempts": attempts})))


@pytest.mark.parametrize("record", ["request", "model_attempt", "tool_attempt", "failure"])
@pytest.mark.asyncio
async def test_all_durable_record_timestamps_must_fall_within_run_interval(
    tmp_path: Path,
    record: str,
) -> None:
    if record == "failure":
        script = success_script()
        script["responses"]["supervisor_plan"] = [
            {"kind": "permanent_failure", "code": "planner_down"}
        ]
        artifact = await execute_loaded(
            make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
        )
        failure = artifact.model_attempts[0].failure
        assert failure is not None
        forged = _replace_failure(
            artifact,
            failure,
            failure.model_copy(update={"timestamp": "2000-01-01T00:00:00Z"}),
        )
    else:
        artifact = await _success(tmp_path)
        if record == "request":
            requests = list(artifact.model_requests)
            requests[0] = requests[0].model_copy(update={"created_at": "2000-01-01T00:00:00Z"})
            forged = artifact.model_copy(update={"model_requests": requests})
        elif record == "model_attempt":
            attempts = list(artifact.model_attempts)
            attempts[0] = attempts[0].model_copy(
                update={
                    "started_at": "2000-01-01T00:00:00Z",
                    "completed_at": "2000-01-01T00:00:00Z",
                }
            )
            requests = list(artifact.model_requests)
            request_index = next(
                index
                for index, request in enumerate(requests)
                if request.request_id == attempts[0].request_id
            )
            requests[request_index] = requests[request_index].model_copy(
                update={"created_at": "2000-01-01T00:00:00Z"}
            )
            forged = artifact.model_copy(
                update={"model_attempts": attempts, "model_requests": requests}
            )
        else:
            results = list(artifact.tool_results)
            results[0] = results[0].model_copy(
                update={
                    "started_at": "2000-01-01T00:00:00Z",
                    "completed_at": "2000-01-01T00:00:00Z",
                }
            )
            forged = artifact.model_copy(update={"tool_results": results})

    with pytest.raises(ArtifactError, match=r"run interval"):
        validate_artifact(_reseal(forged))


@pytest.mark.asyncio
async def test_failure_timestamp_must_equal_containing_attempt_completion(tmp_path: Path) -> None:
    script = success_script()
    script["responses"]["supervisor_plan"] = [{"kind": "permanent_failure", "code": "planner_down"}]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )
    failure = artifact.model_attempts[0].failure
    assert failure is not None
    forged = _replace_failure(
        artifact,
        failure,
        failure.model_copy(update={"timestamp": artifact.started_at}),
    )

    with pytest.raises(ArtifactError, match=r"completion timestamp"):
        validate_artifact(_reseal(forged))


@pytest.mark.parametrize("kind", ["model", "tool"])
@pytest.mark.asyncio
async def test_attempt_events_cannot_temporally_precede_attempt_records(
    tmp_path: Path,
    kind: str,
) -> None:
    artifact = await _success(tmp_path)
    if kind == "model":
        attempts = list(artifact.model_attempts)
        attempts[0] = attempts[0].model_copy(
            update={"started_at": artifact.completed_at, "completed_at": artifact.completed_at}
        )
        forged = artifact.model_copy(update={"model_attempts": attempts})
    else:
        results = list(artifact.tool_results)
        results[0] = results[0].model_copy(
            update={"started_at": artifact.completed_at, "completed_at": artifact.completed_at}
        )
        forged = artifact.model_copy(update={"tool_results": results})

    with pytest.raises(ArtifactError, match=r"event.*timestamp|tempor"):
        validate_artifact(_reseal(forged))


def _resequence(events: list[AgentEvent]) -> list[AgentEvent]:
    return [event.model_copy(update={"sequence": index}) for index, event in enumerate(events, 1)]


@pytest.mark.asyncio
async def test_unknown_event_type_is_rejected(tmp_path: Path) -> None:
    artifact = await _success(tmp_path)
    unknown = artifact.events[-2].model_copy(
        update={
            "event_id": f"evt-{uuid4().hex}",
            "event_type": "unknown_stage_one_event",
            "payload": {},
        }
    )
    events = list(artifact.events)
    events.insert(-1, unknown)

    with pytest.raises(ArtifactError, match=r"event type|vocabulary|unknown"):
        validate_artifact(_reseal(artifact.model_copy(update={"events": _resequence(events)})))


@pytest.mark.asyncio
async def test_attempt_span_cannot_collide_with_run_span(tmp_path: Path) -> None:
    artifact = await _success(tmp_path)
    run_span = artifact.events[0].span_id
    attempts = list(artifact.model_attempts)
    index = next(
        index
        for index, item in enumerate(attempts)
        if item.logical_turn_id == "turn-supervisor-plan"
    )
    old_span = attempts[index].attempt_id
    attempts[index] = attempts[index].model_copy(update={"attempt_id": run_span})
    events = [
        event.model_copy(update={"span_id": run_span}) if event.span_id == old_span else event
        for event in artifact.events
    ]
    forged = artifact.model_copy(update={"model_attempts": attempts, "events": events})

    with pytest.raises(ArtifactError, match=r"span|collision|distinct"):
        validate_artifact(_reseal(forged))


@pytest.mark.asyncio
async def test_fixture_digest_keys_are_exact(tmp_path: Path) -> None:
    artifact = await _success(tmp_path)
    fixture_sha = dict(artifact.content_digests.fixture_sha256)
    fixture_sha["extra"] = "0" * 64
    digests = artifact.content_digests.model_copy(update={"fixture_sha256": fixture_sha})
    provenance = artifact.provenance.model_copy(update={"fixture_sha256": fixture_sha})
    forged = artifact.model_copy(update={"content_digests": digests, "provenance": provenance})

    with pytest.raises(ArtifactError, match=r"fixture.*keys|model.*orders.*policies"):
        validate_artifact(_reseal(forged))


class _CancellationGate:
    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def pause_once(self) -> None:
        if self.reached.is_set():
            return
        self.reached.set()
        await self.release.wait()


async def _cancel_and_read(
    task: asyncio.Task[RunArtifact],
    gate: _CancellationGate,
    destination: Path,
) -> tuple[RunArtifact, asyncio.CancelledError]:
    await asyncio.wait_for(gate.reached.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    gate.release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, timeout=2)
    artifact = read_artifact(destination)
    validate_artifact(artifact)
    return artifact, caught.value


def _assert_startup_cancellation_shape(artifact: RunArtifact, *, planning_start_count: int) -> None:
    assert artifact.status == RunStatus.CANCELLED
    assert len([item for item in artifact.events if item.event_type == "run_start"]) == 1
    planning_transitions = [
        item
        for item in artifact.events
        if item.event_type == "state_transition" and item.phase == RuntimePhase.PLANNING
    ]
    assert len(planning_transitions) == 1
    assert (
        len([item for item in artifact.events if item.event_type == "supervisor_planning_start"])
        == planning_start_count
    )
    assert artifact.model_requests == []
    assert artifact.model_attempts == []
    assert artifact.tool_calls == []
    assert artifact.tool_results == []
    assert artifact.final_state.plan is None
    assert artifact.final_state.worker_results == []
    assert artifact.final_decision is None
    assert any(item.code == "run_cancelled" for item in artifact.failures)
    assert len([item for item in artifact.events if item.event_type == "run_cancellation"]) == 1


@pytest.mark.parametrize(
    "cancel_point",
    ["run_start", "planning_transition"],
    ids=["case-18-run-start-commit", "case-19-planning-transition-commit"],
)
@pytest.mark.asyncio
async def test_startup_commit_cancellation_persists_truthful_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_point: str,
) -> None:
    gate = _CancellationGate()
    original = EventRecorder.record

    async def blocked_record(
        self: EventRecorder,
        event_type: str,
        **kwargs: Any,
    ) -> AgentEvent:
        is_target = event_type == "run_start" or (
            event_type == "state_transition" and kwargs.get("phase") == RuntimePhase.PLANNING
        )
        expected = (
            event_type == "run_start"
            if cancel_point == "run_start"
            else (event_type == "state_transition" and kwargs.get("phase") == RuntimePhase.PLANNING)
        )
        if is_target and expected:
            await gate.pause_once()
        return await original(self, event_type, **kwargs)

    monkeypatch.setattr(EventRecorder, "record", blocked_record)
    destination = tmp_path / f"cancelled-{cancel_point}.json"
    task = asyncio.create_task(execute_loaded(make_loaded(tmp_path), output_path=destination))
    artifact, _ = await _cancel_and_read(task, gate, destination)

    _assert_startup_cancellation_shape(artifact, planning_start_count=0)


@pytest.mark.asyncio
async def test_cancellation_after_startup_before_graph_planning_persists_truthful_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _CancellationGate()

    class BlockedBeforePlanningGraph:
        async def ainvoke(self, state: Any, *, context: Any) -> Any:
            del context
            await gate.pause_once()
            return state

    monkeypatch.setattr(runtime_module, "build_graph", BlockedBeforePlanningGraph)
    destination = tmp_path / "cancelled-before-planning.json"
    task = asyncio.create_task(execute_loaded(make_loaded(tmp_path), output_path=destination))
    artifact, _ = await _cancel_and_read(task, gate, destination)

    _assert_startup_cancellation_shape(artifact, planning_start_count=0)


@pytest.mark.asyncio
async def test_cancellation_during_planning_start_commit_persists_truthful_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _CancellationGate()
    original = EventRecorder.record

    async def blocked_record(
        self: EventRecorder,
        event_type: str,
        **kwargs: Any,
    ) -> AgentEvent:
        if event_type == "supervisor_planning_start":
            await gate.pause_once()
        return await original(self, event_type, **kwargs)

    monkeypatch.setattr(EventRecorder, "record", blocked_record)
    destination = tmp_path / "cancelled-planning-start.json"
    task = asyncio.create_task(execute_loaded(make_loaded(tmp_path), output_path=destination))
    artifact, _ = await _cancel_and_read(task, gate, destination)

    _assert_startup_cancellation_shape(artifact, planning_start_count=1)


@pytest.mark.asyncio
async def test_cancellation_after_model_attempt_record_before_terminal_event_is_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _CancellationGate()
    original = EventRecorder.record

    async def blocked_record(
        self: EventRecorder,
        event_type: str,
        **kwargs: Any,
    ) -> AgentEvent:
        if event_type == "model_attempt_end":
            await gate.pause_once()
        return await original(self, event_type, **kwargs)

    monkeypatch.setattr(EventRecorder, "record", blocked_record)
    destination = tmp_path / "cancelled.json"
    task = asyncio.create_task(execute_loaded(make_loaded(tmp_path), output_path=destination))
    artifact, _ = await _cancel_and_read(task, gate, destination)

    assert artifact.status == RunStatus.CANCELLED
    attempt = artifact.model_attempts[0]
    assert attempt.outcome == AttemptOutcome.SUCCEEDED
    assert any(
        event.span_id == attempt.attempt_id and event.event_type == "model_attempt_end"
        for event in artifact.events
    )


@pytest.mark.asyncio
async def test_cancellation_after_tool_result_record_before_terminal_event_is_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _CancellationGate()
    original = EventRecorder.record

    async def blocked_record(
        self: EventRecorder,
        event_type: str,
        **kwargs: Any,
    ) -> AgentEvent:
        if event_type == "tool_attempt_end":
            await gate.pause_once()
        return await original(self, event_type, **kwargs)

    monkeypatch.setattr(EventRecorder, "record", blocked_record)
    destination = tmp_path / "cancelled.json"
    task = asyncio.create_task(execute_loaded(make_loaded(tmp_path), output_path=destination))
    artifact, _ = await _cancel_and_read(task, gate, destination)

    assert artifact.status == RunStatus.CANCELLED
    successful = [
        result for result in artifact.tool_results if result.outcome == AttemptOutcome.SUCCEEDED
    ]
    assert successful
    assert all(
        any(
            event.span_id == result.attempt_span_id and event.event_type == "tool_attempt_end"
            for event in artifact.events
        )
        for result in successful
    )


@pytest.mark.asyncio
async def test_cancelled_run_may_stop_during_backoff_after_retryable_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = success_script()
    success = script["responses"]["supervisor_plan"][0]
    script["responses"]["supervisor_plan"] = [
        {"kind": "transient_failure", "code": "planner_busy"},
        success,
    ]
    loaded = make_loaded(
        tmp_path,
        script=script,
        config_changes={
            "model_retry": {
                "max_attempts": 2,
                "timeout_seconds": 0.1,
                "initial_backoff_seconds": 1.0,
                "max_backoff_seconds": 1.0,
                "jitter_ratio": 0.0,
            }
        },
    )
    backoff_started = asyncio.Event()
    original_sleep = asyncio.sleep

    async def observed_sleep(delay: float, result: Any = None) -> Any:
        if delay == 1.0:
            backoff_started.set()
        return await original_sleep(delay, result)

    monkeypatch.setattr(asyncio, "sleep", observed_sleep)
    destination = tmp_path / "cancelled.json"
    task = asyncio.create_task(execute_loaded(loaded, output_path=destination))
    await asyncio.wait_for(backoff_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    artifact = read_artifact(destination)

    assert artifact.status == RunStatus.CANCELLED
    planner_attempts = [
        attempt
        for attempt in artifact.model_attempts
        if attempt.logical_turn_id == "turn-supervisor-plan"
    ]
    assert len(planner_attempts) == 1
    assert planner_attempts[0].failure is not None
    assert planner_attempts[0].failure.retryable is True


@pytest.mark.asyncio
async def test_cancellation_during_classified_failure_terminalization_persists_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = success_script()
    script["responses"]["supervisor_plan"] = [{"kind": "permanent_failure", "code": "planner_down"}]
    gate = _CancellationGate()
    original = EventRecorder.record

    async def blocked_record(
        self: EventRecorder,
        event_type: str,
        **kwargs: Any,
    ) -> AgentEvent:
        if event_type == "run_failure":
            await gate.pause_once()
        return await original(self, event_type, **kwargs)

    monkeypatch.setattr(EventRecorder, "record", blocked_record)
    destination = tmp_path / "failed.json"
    task = asyncio.create_task(
        execute_loaded(make_loaded(tmp_path, script=script), output_path=destination)
    )
    artifact, _ = await _cancel_and_read(task, gate, destination)

    assert artifact.status == RunStatus.FAILED
    assert any(failure.code == "planner_down" for failure in artifact.failures)
    assert len([event for event in artifact.events if event.event_type == "run_failure"]) == 1


@pytest.mark.asyncio
async def test_cancellation_during_unexpected_failure_terminalization_persists_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_acceptance(plan: Any) -> None:
        del plan
        raise RuntimeError("deterministic acceptance fault")

    gate = _CancellationGate()
    original = EventRecorder.record

    async def blocked_record(
        self: EventRecorder,
        event_type: str,
        **kwargs: Any,
    ) -> AgentEvent:
        if event_type == "run_failure":
            await gate.pause_once()
        return await original(self, event_type, **kwargs)

    monkeypatch.setattr("agent_runtime.orchestration.validate_stage_one_plan", fail_acceptance)
    monkeypatch.setattr(EventRecorder, "record", blocked_record)
    destination = tmp_path / "failed.json"
    task = asyncio.create_task(execute_loaded(make_loaded(tmp_path), output_path=destination))
    artifact, _ = await _cancel_and_read(task, gate, destination)

    assert artifact.status == RunStatus.FAILED
    assert any(failure.code == "unexpected_internal_error" for failure in artifact.failures)
    assert len([event for event in artifact.events if event.event_type == "run_failure"]) == 1


@pytest.mark.asyncio
async def test_cancellation_during_finalization_entry_commits_lifecycle_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _CancellationGate()
    original = EventRecorder.record

    async def blocked_record(
        self: EventRecorder,
        event_type: str,
        **kwargs: Any,
    ) -> AgentEvent:
        if event_type == "supervisor_finalization_start":
            await gate.pause_once()
        return await original(self, event_type, **kwargs)

    monkeypatch.setattr(EventRecorder, "record", blocked_record)
    destination = tmp_path / "cancelled.json"
    task = asyncio.create_task(execute_loaded(make_loaded(tmp_path), output_path=destination))
    artifact, _ = await _cancel_and_read(task, gate, destination)

    assert artifact.status == RunStatus.CANCELLED
    finalizing = [
        event
        for event in artifact.events
        if event.event_type == "state_transition" and event.phase == RuntimePhase.FINALIZING
    ]
    final_starts = [
        event for event in artifact.events if event.event_type == "supervisor_finalization_start"
    ]
    assert len(finalizing) == 1
    assert len(final_starts) == 1


@pytest.mark.asyncio
async def test_cancellation_after_planning_end_before_plan_acceptance_is_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _CancellationGate()
    original = AcceptedStateRecorder.accept_plan

    async def blocked_accept_plan(self: AcceptedStateRecorder, plan: Any) -> None:
        await gate.pause_once()
        await original(self, plan)

    monkeypatch.setattr(AcceptedStateRecorder, "accept_plan", blocked_accept_plan)
    destination = tmp_path / "cancelled.json"
    task = asyncio.create_task(execute_loaded(make_loaded(tmp_path), output_path=destination))
    artifact, _ = await _cancel_and_read(task, gate, destination)

    assert artifact.status == RunStatus.CANCELLED
    assert artifact.final_state.plan is not None
    assert len([event for event in artifact.events if event.event_type == "worker_dispatch"]) == 2


@pytest.mark.asyncio
async def test_cancellation_after_worker_terminal_before_result_acceptance_is_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _CancellationGate()
    original = AcceptedStateRecorder.accept_worker_result

    async def blocked_accept_worker(self: AcceptedStateRecorder, result: Any) -> None:
        await gate.pause_once()
        await original(self, result)

    monkeypatch.setattr(AcceptedStateRecorder, "accept_worker_result", blocked_accept_worker)
    destination = tmp_path / "cancelled.json"
    task = asyncio.create_task(execute_loaded(make_loaded(tmp_path), output_path=destination))
    artifact, _ = await _cancel_and_read(task, gate, destination)

    assert artifact.status == RunStatus.CANCELLED
    assert artifact.final_state.worker_results
    accepted_ids = {result.worker_id for result in artifact.final_state.worker_results}
    terminal_ids = {
        event.source_component
        for event in artifact.events
        if event.event_type in {"worker_end", "worker_failure"}
    }
    assert terminal_ids == accepted_ids


@pytest.mark.asyncio
async def test_cancellation_after_finalization_end_before_decision_acceptance_commits_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _CancellationGate()
    original = AcceptedStateRecorder.accept_final_decision

    async def blocked_accept_decision(self: AcceptedStateRecorder, decision: Any) -> None:
        await gate.pause_once()
        await original(self, decision)

    monkeypatch.setattr(AcceptedStateRecorder, "accept_final_decision", blocked_accept_decision)
    destination = tmp_path / "success.json"
    task = asyncio.create_task(execute_loaded(make_loaded(tmp_path), output_path=destination))
    artifact, cancellation = await _cancel_and_read(task, gate, destination)

    assert artifact.status == RunStatus.SUCCEEDED
    assert artifact.final_decision == EXPECTED_DECISION
    assert not [event for event in artifact.events if event.event_type == "run_cancellation"]
    assert any("committed success" in note for note in getattr(cancellation, "__notes__", []))


@pytest.mark.asyncio
async def test_cancellation_after_success_transition_before_run_success_commits_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _CancellationGate()
    original = EventRecorder.record

    async def blocked_record(
        self: EventRecorder,
        event_type: str,
        **kwargs: Any,
    ) -> AgentEvent:
        if event_type == "run_success":
            await gate.pause_once()
        return await original(self, event_type, **kwargs)

    monkeypatch.setattr(EventRecorder, "record", blocked_record)
    destination = tmp_path / "success.json"
    task = asyncio.create_task(execute_loaded(make_loaded(tmp_path), output_path=destination))
    artifact, cancellation = await _cancel_and_read(task, gate, destination)

    assert artifact.status == RunStatus.SUCCEEDED
    assert len([event for event in artifact.events if event.event_type == "run_success"]) == 1
    assert not [event for event in artifact.events if event.event_type == "run_cancellation"]
    assert any("committed success" in note for note in getattr(cancellation, "__notes__", []))


@pytest.mark.asyncio
async def test_cancellation_during_successful_artifact_assembly_persists_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _CancellationGate()
    original = runtime_module._assemble_artifact

    async def blocked_assembly(**kwargs: Any) -> RunArtifact:
        services = kwargs["services"]
        if services.phase.current == RuntimePhase.SUCCEEDED:
            await gate.pause_once()
        return await original(**kwargs)

    monkeypatch.setattr(runtime_module, "_assemble_artifact", blocked_assembly)
    destination = tmp_path / "success.json"
    task = asyncio.create_task(execute_loaded(make_loaded(tmp_path), output_path=destination))
    artifact, cancellation = await _cancel_and_read(task, gate, destination)

    assert artifact.status == RunStatus.SUCCEEDED
    assert artifact.final_decision == EXPECTED_DECISION
    assert any("committed success" in note for note in getattr(cancellation, "__notes__", []))


@pytest.mark.asyncio
async def test_cancellation_during_successful_artifact_write_persists_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_started = threading.Event()
    release_write = threading.Event()
    original = write_artifact

    def blocked_write(artifact: RunArtifact, path: Path) -> Path:
        write_started.set()
        if not release_write.wait(timeout=2):
            raise RuntimeError("test did not release artifact write")
        return original(artifact, path)

    monkeypatch.setattr(runtime_module, "write_artifact", blocked_write)
    destination = tmp_path / "success.json"
    task = asyncio.create_task(execute_loaded(make_loaded(tmp_path), output_path=destination))
    started = await asyncio.to_thread(write_started.wait, 2)
    assert started is True
    task.cancel()
    await asyncio.sleep(0)
    release_write.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, timeout=2)
    artifact = read_artifact(destination)

    assert artifact.status == RunStatus.SUCCEEDED
    assert artifact.final_decision == EXPECTED_DECISION
    assert any("committed success" in note for note in getattr(caught.value, "__notes__", []))


class _CancellingEnteredProbe:
    def __init__(self) -> None:
        self.entered_event = asyncio.Event()

    async def entered(self, worker_id: str, active: int) -> None:
        del worker_id, active
        self.entered_event.set()
        await asyncio.Event().wait()

    async def exited(self, worker_id: str, active: int) -> None:
        del worker_id, active


@pytest.mark.asyncio
async def test_cancellation_inside_concurrency_probe_restores_active_worker_count(
    tmp_path: Path,
) -> None:
    probe = _CancellingEnteredProbe()
    destination = tmp_path / "cancelled.json"
    task = asyncio.create_task(
        execute_loaded(
            make_loaded(tmp_path),
            output_path=destination,
            concurrency_probe=probe,
        )
    )
    await asyncio.wait_for(probe.entered_event.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    artifact = read_artifact(destination)

    cancellation = next(
        event for event in artifact.events if event.event_type == "run_cancellation"
    )
    assert cancellation.payload["active_workers_after_cleanup"] == 0
