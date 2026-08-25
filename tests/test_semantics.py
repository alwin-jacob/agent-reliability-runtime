from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import EXPECTED_DECISION, make_loaded, scripted_registry, success_script
from pydantic import ValidationError

from agent_runtime.domain import FinalDecision, RunStatus, TaskSpec
from agent_runtime.runtime import execute_loaded


def _responses(script: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return cast(dict[str, list[dict[str, Any]]], script["responses"])


def _set_final_decision(script: dict[str, Any], decision: FinalDecision) -> None:
    _responses(script)["supervisor_finalize"] = [
        {"kind": "success", "raw_json": decision.model_dump_json()}
    ]


@pytest.mark.parametrize(
    ("source", "field", "value", "failure_code"),
    [
        ("order", "market", "CA", "order_context_mismatch"),
        ("order", "item_category", "electronics", "order_context_mismatch"),
        ("order", "purchase_channel", "store", "order_context_mismatch"),
        ("policy", "market", "CA", "policy_context_mismatch"),
        ("policy", "item_category", "electronics", "policy_context_mismatch"),
        ("policy", "purchase_channel", "store", "policy_context_mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_authoritative_context_mismatch_fails_closed(
    tmp_path: Path,
    source: str,
    field: str,
    value: str,
    failure_code: str,
) -> None:
    loaded = make_loaded(tmp_path)
    order_output = {
        "order_id": "ORD-1001",
        "item_category": "household",
        "delivery_status": "delivered",
        "delivery_date": "2026-08-10",
        "purchase_channel": "online",
        "market": "US",
    }
    policy_output = {
        "market": "US",
        "item_category": "household",
        "purchase_channel": "online",
        "return_window_days": 30,
        "item_condition_requirement": "unopened",
        "applicable_fees": "none",
    }
    (order_output if source == "order" else policy_output)[field] = value
    registry, _ = scripted_registry(order_output=order_output, policy_output=policy_output)

    artifact = await execute_loaded(
        replace(loaded, tools=registry), output_path=tmp_path / "failed.json"
    )

    assert artifact.status == RunStatus.FAILED
    assert failure_code in {item.code for item in artifact.failures}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("market", "CA"),
        ("item_category", "electronics"),
        ("purchase_channel", "store"),
    ],
)
@pytest.mark.asyncio
async def test_policy_worker_arguments_must_equal_typed_task_context(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    script = success_script()
    arguments = {
        "market": "US",
        "item_category": "household",
        "purchase_channel": "online",
    }
    arguments[field] = value
    _responses(script)["worker:policy-worker"] = [
        {
            "kind": "success",
            "raw_json": json.dumps(
                {"tool_name": "lookup_return_policy", "arguments": arguments},
                separators=(",", ":"),
            ),
        }
    ]

    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )

    assert artifact.status == RunStatus.FAILED
    assert "worker_tool_request_contract_invalid" in {item.code for item in artifact.failures}


@pytest.mark.asyncio
async def test_order_worker_order_id_must_equal_task(tmp_path: Path) -> None:
    script = success_script()
    _responses(script)["worker:order-worker"] = [
        {
            "kind": "success",
            "raw_json": ('{"tool_name":"lookup_order","arguments":{"order_id":"ORD-OTHER"}}'),
        }
    ]

    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )

    assert artifact.status == RunStatus.FAILED
    assert "worker_tool_request_contract_invalid" in {item.code for item in artifact.failures}


@pytest.mark.parametrize("as_of_date", [None, "2026-8-25", "2026-02-30"])
def test_task_rejects_missing_or_invalid_as_of_date(as_of_date: str | None) -> None:
    payload: dict[str, object] = {
        "schema_version": "0.2.0",
        "task_id": "task",
        "customer_request": "Can this be returned?",
        "order_id": "ORD-1001",
        "item_condition": "unopened",
        "market": "US",
        "item_category": "household",
        "purchase_channel": "online",
    }
    if as_of_date is not None:
        payload["as_of_date"] = as_of_date
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(payload)


@pytest.mark.asyncio
async def test_decision_date_before_delivery_fails_closed(tmp_path: Path) -> None:
    artifact = await execute_loaded(
        make_loaded(tmp_path, task_changes={"as_of_date": "2026-08-09"}),
        output_path=tmp_path / "failed.json",
    )
    assert artifact.status == RunStatus.FAILED
    assert "decision_date_before_delivery" in {item.code for item in artifact.failures}


@pytest.mark.asyncio
async def test_inside_window_is_eligible(tmp_path: Path) -> None:
    artifact = await execute_loaded(make_loaded(tmp_path), output_path=tmp_path / "run.json")
    assert artifact.status == RunStatus.SUCCEEDED
    assert artifact.final_decision == EXPECTED_DECISION


@pytest.mark.asyncio
async def test_outside_window_is_canonically_ineligible(tmp_path: Path) -> None:
    script = success_script()
    decision = EXPECTED_DECISION.model_copy(
        update={
            "eligible": False,
            "decision_code": "RETURN_INELIGIBLE",
            "as_of_date": "2026-09-20",
            "days_since_delivery": 41,
            "reason": "The return is outside the authoritative 30-day window.",
            "next_action": "Do not initiate a standard return.",
        }
    )
    _set_final_decision(script, decision)

    artifact = await execute_loaded(
        make_loaded(
            tmp_path,
            script=script,
            task_changes={"as_of_date": "2026-09-20"},
        ),
        output_path=tmp_path / "run.json",
    )

    assert artifact.status == RunStatus.SUCCEEDED
    assert artifact.final_decision == decision


@pytest.mark.asyncio
async def test_item_condition_mismatch_fails_closed(tmp_path: Path) -> None:
    artifact = await execute_loaded(
        make_loaded(tmp_path, task_changes={"item_condition": "opened"}),
        output_path=tmp_path / "failed.json",
    )
    assert artifact.status == RunStatus.FAILED
    assert "item_condition_requirement_mismatch" in {item.code for item in artifact.failures}


@pytest.mark.asyncio
async def test_order_must_be_delivered_for_a_successful_decision(tmp_path: Path) -> None:
    loaded = make_loaded(tmp_path)
    registry, _ = scripted_registry(
        order_output={
            "order_id": "ORD-1001",
            "item_category": "household",
            "delivery_status": "in_transit",
            "delivery_date": "2026-08-10",
            "purchase_channel": "online",
            "market": "US",
        }
    )
    artifact = await execute_loaded(
        replace(loaded, tools=registry), output_path=tmp_path / "failed.json"
    )
    assert artifact.status == RunStatus.FAILED
    assert "order_not_delivered" in {item.code for item in artifact.failures}


@pytest.mark.parametrize(
    ("updates", "failure_code"),
    [
        ({"eligible": False}, "final_decision_eligibility_invalid"),
        ({"decision_code": "RETURN_INELIGIBLE"}, "final_decision_code_invalid"),
    ],
)
@pytest.mark.asyncio
async def test_incorrect_model_generated_decision_fails_closed(
    tmp_path: Path,
    updates: dict[str, object],
    failure_code: str,
) -> None:
    script = success_script()
    _set_final_decision(script, EXPECTED_DECISION.model_copy(update=updates))

    artifact = await execute_loaded(
        make_loaded(tmp_path, script=script), output_path=tmp_path / "failed.json"
    )

    assert artifact.status == RunStatus.FAILED
    assert failure_code in {item.code for item in artifact.failures}
