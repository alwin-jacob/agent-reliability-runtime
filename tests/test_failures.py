from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import BarrierProbe, make_loaded, success_script

from agent_runtime.artifacts import read_artifact
from agent_runtime.domain import AttemptOutcome, RunStatus
from agent_runtime.runtime import execute_loaded


def _responses(script: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return cast(dict[str, list[dict[str, Any]]], script["responses"])


@pytest.mark.asyncio
async def test_transient_model_failure_retries_then_run_succeeds(tmp_path: Path) -> None:
    script = success_script()
    original = _responses(script)["supervisor_plan"][0]
    _responses(script)["supervisor_plan"] = [
        {"kind": "transient_failure", "code": "provider_busy"},
        original,
    ]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "run.json"
    )
    attempts = [
        item for item in artifact.model_attempts if item.logical_turn_id == "turn-supervisor-plan"
    ]
    assert artifact.status == RunStatus.SUCCEEDED
    assert [item.outcome for item in attempts] == [
        AttemptOutcome.FAILED,
        AttemptOutcome.SUCCEEDED,
    ]


@pytest.mark.asyncio
async def test_permanent_model_failure_is_not_retried_and_artifact_is_written(
    tmp_path: Path,
) -> None:
    script = success_script()
    _responses(script)["supervisor_plan"] = [
        {"kind": "permanent_failure", "code": "provider_disabled"}
    ]
    destination = tmp_path / "failed.json"
    artifact = await execute_loaded(make_loaded(tmp_path, script=script), output_path=destination)
    assert artifact.status == RunStatus.FAILED
    assert len(artifact.model_attempts) == 1
    assert read_artifact(destination).status == RunStatus.FAILED


@pytest.mark.asyncio
async def test_model_timeout_is_normalized_and_bounded(tmp_path: Path) -> None:
    script = success_script()
    _responses(script)["supervisor_plan"] = [
        {"kind": "success", "delay_seconds": 1.0, "raw_json": "{}"}
    ]
    loaded = make_loaded(
        tmp_path,
        script=script,
        config_changes={
            "model_retry": {
                "max_attempts": 1,
                "timeout_seconds": 0.01,
                "initial_backoff_seconds": 0.0,
                "max_backoff_seconds": 0.0,
                "jitter_ratio": 0.0,
            }
        },
    )
    artifact = await asyncio.wait_for(
        execute_loaded(loaded, output_path=tmp_path / "timeout.json"), timeout=1
    )
    assert artifact.status == RunStatus.FAILED
    assert artifact.model_attempts[0].outcome == AttemptOutcome.TIMED_OUT
    assert artifact.model_attempts[0].failure is not None
    assert artifact.model_attempts[0].failure.code == "model_attempt_timeout"


@pytest.mark.parametrize(
    ("raw", "failure_code"),
    [
        ("{", "malformed_model_json"),
        ('{"assignments":[]}', "model_output_schema_invalid"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_model_output_is_not_retried(
    tmp_path: Path, raw: str, failure_code: str
) -> None:
    script = success_script()
    _responses(script)["supervisor_plan"] = [{"kind": "success", "raw_json": raw}]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "invalid.json"
    )
    assert artifact.status == RunStatus.FAILED
    assert len(artifact.model_attempts) == 1
    assert failure_code in {item.code for item in artifact.failures}


@pytest.mark.asyncio
async def test_external_cancellation_cancels_active_workers_and_persists_evidence(
    tmp_path: Path,
) -> None:
    script = success_script()
    waiting = [{"kind": "wait_for_cancellation"}]
    _responses(script)["worker:order-worker"] = waiting
    _responses(script)["worker:policy-worker"] = waiting
    loaded = make_loaded(tmp_path, script=script)
    probe = BarrierProbe()
    destination = tmp_path / "cancelled.json"
    task = asyncio.create_task(
        execute_loaded(loaded, output_path=destination, concurrency_probe=probe)
    )
    await probe.all_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    artifact = read_artifact(destination)
    assert artifact.status == RunStatus.CANCELLED
    assert {item.failure.code for item in artifact.model_attempts if item.failure is not None} == {
        "invocation_cancelled"
    }
    cancellation_events = [
        item for item in artifact.events if item.event_type == "run_cancellation"
    ]
    assert cancellation_events[0].payload["active_workers_after_cleanup"] == 0
