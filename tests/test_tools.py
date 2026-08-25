from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import make_loaded, scripted_registry, success_script

from agent_runtime.domain import AttemptOutcome, RunStatus
from agent_runtime.runtime import execute_loaded


def _responses(script: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return cast(dict[str, list[dict[str, Any]]], script["responses"])


@pytest.mark.parametrize(
    ("tool_name", "arguments", "code"),
    [
        ("does_not_exist", {}, "worker_tool_request_contract_invalid"),
        (
            "lookup_return_policy",
            {"market": "US", "item_category": "household", "purchase_channel": "online"},
            "worker_tool_request_contract_invalid",
        ),
        ("lookup_order", {"wrong": "value"}, "worker_tool_request_contract_invalid"),
    ],
)
@pytest.mark.asyncio
async def test_tool_request_policy_and_input_failures(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, Any],
    code: str,
) -> None:
    script = success_script()
    _responses(script)["worker:order-worker"] = [
        {
            "kind": "success",
            "raw_json": __import__("json").dumps(
                {"tool_name": tool_name, "arguments": arguments}, separators=(",", ":")
            ),
        }
    ]
    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )
    assert artifact.status == RunStatus.FAILED
    assert code in {item.code for item in artifact.failures}
    assert not [item for item in artifact.tool_results if item.worker_id == "order-worker"]


@pytest.mark.asyncio
async def test_tool_output_validation_is_not_retried(tmp_path: Path) -> None:
    loaded = make_loaded(tmp_path)
    registry, order = scripted_registry(order_output={"unexpected": "shape"})
    artifact = await execute_loaded(
        replace(loaded, tools=registry), output_path=tmp_path / "failed.json"
    )
    assert artifact.status == RunStatus.FAILED
    assert order.calls == 1
    order_results = [item for item in artifact.tool_results if item.worker_id == "order-worker"]
    assert len(order_results) == 1
    assert order_results[0].failure is not None
    assert order_results[0].failure.code == "tool_output_invalid"


@pytest.mark.asyncio
async def test_transient_tool_failure_retries_then_succeeds(tmp_path: Path) -> None:
    loaded = make_loaded(tmp_path)
    registry, order = scripted_registry(order_transient_failures=1)
    artifact = await execute_loaded(
        replace(loaded, tools=registry), output_path=tmp_path / "run.json"
    )
    order_results = [item for item in artifact.tool_results if item.worker_id == "order-worker"]
    assert artifact.status == RunStatus.SUCCEEDED
    assert order.calls == 2
    assert [item.outcome for item in order_results] == [
        AttemptOutcome.FAILED,
        AttemptOutcome.SUCCEEDED,
    ]


@pytest.mark.asyncio
async def test_tool_timeout_is_normalized(tmp_path: Path) -> None:
    loaded = make_loaded(
        tmp_path,
        config_changes={
            "tool_retry": {
                "max_attempts": 1,
                "timeout_seconds": 0.01,
                "initial_backoff_seconds": 0.0,
                "max_backoff_seconds": 0.0,
                "jitter_ratio": 0.0,
            }
        },
    )
    registry, order = scripted_registry(order_delay=1.0)
    artifact = await execute_loaded(
        replace(loaded, tools=registry), output_path=tmp_path / "timeout.json"
    )
    order_results = [item for item in artifact.tool_results if item.worker_id == "order-worker"]
    assert artifact.status == RunStatus.FAILED
    assert order.calls == 1
    assert order_results[0].outcome == AttemptOutcome.TIMED_OUT
    assert order_results[0].failure is not None
    assert order_results[0].failure.code == "tool_attempt_timeout"
