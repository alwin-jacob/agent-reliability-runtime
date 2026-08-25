from __future__ import annotations

from pathlib import Path

import pytest
from conftest import EXPECTED_DECISION, make_loaded, success_script

from agent_runtime.domain import AttemptOutcome, RunStatus
from agent_runtime.runtime import execute_loaded


@pytest.mark.asyncio
async def test_successful_retail_task_has_exact_decision(tmp_path: Path) -> None:
    artifact = await execute_loaded(make_loaded(tmp_path), output_path=tmp_path / "run.json")
    assert artifact.status == RunStatus.SUCCEEDED
    assert artifact.final_decision == EXPECTED_DECISION
    assert {item.worker_id for item in artifact.final_state.worker_results} == {
        "order-worker",
        "policy-worker",
    }


@pytest.mark.asyncio
async def test_both_worker_results_are_required(tmp_path: Path) -> None:
    script = success_script()
    script["responses"]["worker:policy-worker"] = [
        {"kind": "permanent_failure", "code": "policy_provider_down"}
    ]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )
    assert artifact.status == RunStatus.FAILED
    assert artifact.final_decision is None
    assert "required_worker_evidence_missing" in {item.code for item in artifact.failures}


@pytest.mark.asyncio
async def test_event_sequences_and_attempt_trace_are_complete(tmp_path: Path) -> None:
    artifact = await execute_loaded(make_loaded(tmp_path), output_path=tmp_path / "run.json")
    assert [item.sequence for item in artifact.events] == list(range(1, len(artifact.events) + 1))
    spans = {(item.span_id, item.event_type) for item in artifact.events}
    for attempt in artifact.model_attempts:
        assert (attempt.attempt_id, "model_attempt_start") in spans
        assert (attempt.attempt_id, "model_attempt_end") in spans
        assert attempt.outcome == AttemptOutcome.SUCCEEDED
    for result in artifact.tool_results:
        assert (result.attempt_span_id, "tool_attempt_start") in spans
        assert (result.attempt_span_id, "tool_attempt_end") in spans
