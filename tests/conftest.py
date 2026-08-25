from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from agent_runtime.domain import FinalDecision, ToolDefinition
from agent_runtime.errors import ToolExecutionError
from agent_runtime.runtime import LoadedInputs, load_inputs
from agent_runtime.tools.base import RuntimeTool
from agent_runtime.tools.registry import ToolRegistry
from agent_runtime.tools.retail import (
    LookupOrderInput,
    LookupOrderOutput,
    LookupOrderTool,
    LookupReturnPolicyInput,
    LookupReturnPolicyOutput,
    LookupReturnPolicyTool,
)

EXPECTED_DECISION = FinalDecision(
    eligible=True,
    decision_code="RETURN_ELIGIBLE",
    reason=(
        "The unopened delivered household item is within the 30-day US online return "
        "window and has no fee."
    ),
    next_action="Start the online return for ORD-1001 and send the item back unopened.",
    order_id="ORD-1001",
    as_of_date="2026-08-25",
    days_since_delivery=15,
    policy_window_days=30,
    applicable_fees="none",
    evidence_worker_ids=["order-worker", "policy-worker"],
)


def success_script() -> dict[str, Any]:
    return {
        "schema_version": "0.2.0",
        "provider_id": "test-fixture",
        "responses": {
            "supervisor_plan": [
                {
                    "kind": "success",
                    "raw_json": json.dumps(
                        {
                            "assignments": [
                                {
                                    "worker_id": "order-worker",
                                    "allowed_tools": ["lookup_order"],
                                    "purpose": "retrieve authoritative order facts",
                                },
                                {
                                    "worker_id": "policy-worker",
                                    "allowed_tools": ["lookup_return_policy"],
                                    "purpose": "retrieve the applicable return policy",
                                },
                            ]
                        },
                        separators=(",", ":"),
                    ),
                }
            ],
            "worker:order-worker": [
                {
                    "kind": "success",
                    "delay_seconds": 0.001,
                    "raw_json": json.dumps(
                        {"tool_name": "lookup_order", "arguments": {"order_id": "ORD-1001"}},
                        separators=(",", ":"),
                    ),
                }
            ],
            "worker:policy-worker": [
                {
                    "kind": "success",
                    "delay_seconds": 0.001,
                    "raw_json": json.dumps(
                        {
                            "tool_name": "lookup_return_policy",
                            "arguments": {
                                "market": "US",
                                "item_category": "household",
                                "purchase_channel": "online",
                            },
                        },
                        separators=(",", ":"),
                    ),
                }
            ],
            "supervisor_finalize": [
                {
                    "kind": "success",
                    "raw_json": EXPECTED_DECISION.model_dump_json(),
                }
            ],
        },
    }


def make_loaded(
    root: Path,
    *,
    script: dict[str, Any] | None = None,
    config_changes: Mapping[str, Any] | None = None,
    task_changes: Mapping[str, Any] | None = None,
    order_changes: Mapping[str, Any] | None = None,
    policy_changes: Mapping[str, Any] | None = None,
) -> LoadedInputs:
    root.mkdir(parents=True, exist_ok=True)
    task = {
        "schema_version": "0.2.0",
        "task_id": "retail-return-test",
        "customer_request": "Can the unopened item be returned?",
        "order_id": "ORD-1001",
        "as_of_date": "2026-08-25",
        "item_condition": "unopened",
        "market": "US",
        "item_category": "household",
        "purchase_channel": "online",
    }
    if task_changes:
        task.update(task_changes)
    config: dict[str, Any] = {
        "schema_version": "0.2.0",
        "provider": "fixture",
        "model_fixture": "model.json",
        "order_fixture": "orders.json",
        "policy_fixture": "policies.json",
        "max_worker_concurrency": 2,
        "model_retry": {
            "max_attempts": 2,
            "timeout_seconds": 0.1,
            "initial_backoff_seconds": 0.0,
            "max_backoff_seconds": 0.0,
            "jitter_ratio": 0.0,
        },
        "tool_retry": {
            "max_attempts": 2,
            "timeout_seconds": 0.1,
            "initial_backoff_seconds": 0.0,
            "max_backoff_seconds": 0.0,
            "jitter_ratio": 0.0,
        },
    }
    if config_changes:
        config.update(config_changes)
    orders: dict[str, Any] = {
        "schema_version": "0.2.0",
        "delay_seconds": 0.001,
        "orders": [
            {
                "order_id": "ORD-1001",
                "item_category": "household",
                "delivery_status": "delivered",
                "delivery_date": "2026-08-10",
                "purchase_channel": "online",
                "market": "US",
            }
        ],
    }
    if order_changes:
        cast(list[dict[str, Any]], orders["orders"])[0].update(order_changes)
    policies: dict[str, Any] = {
        "schema_version": "0.2.0",
        "delay_seconds": 0.001,
        "policies": [
            {
                "market": "US",
                "item_category": "household",
                "purchase_channel": "online",
                "return_window_days": 30,
                "item_condition_requirement": "unopened",
                "applicable_fees": "none",
            }
        ],
    }
    if policy_changes:
        cast(list[dict[str, Any]], policies["policies"])[0].update(policy_changes)
    _write_json(root / "task.json", task)
    _write_json(root / "config.json", config)
    _write_json(root / "model.json", script or success_script())
    _write_json(root / "orders.json", orders)
    _write_json(root / "policies.json", policies)
    (root / "uv.lock").write_text("test lock\n", encoding="utf-8")
    return load_inputs(Path("task.json"), Path("config.json"), root=root)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


class BarrierProbe:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._release = asyncio.Event()
        self.all_entered = asyncio.Event()
        self.entered_workers: set[str] = set()
        self.exited_workers: set[str] = set()
        self.max_active = 0

    async def entered(self, worker_id: str, active: int) -> None:
        async with self._lock:
            self.entered_workers.add(worker_id)
            self.max_active = max(self.max_active, active)
            if len(self.entered_workers) == 2:
                self._release.set()
                self.all_entered.set()
        await self._release.wait()

    async def exited(self, worker_id: str, active: int) -> None:
        del active
        self.exited_workers.add(worker_id)


class TrackingProbe:
    def __init__(self) -> None:
        self.max_active = 0

    async def entered(self, worker_id: str, active: int) -> None:
        del worker_id
        self.max_active = max(self.max_active, active)

    async def exited(self, worker_id: str, active: int) -> None:
        del worker_id, active


class ScriptedTool:
    def __init__(
        self,
        *,
        definition: ToolDefinition,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        output: dict[str, Any],
        transient_failures: int = 0,
        delay_seconds: float = 0.0,
    ) -> None:
        self.definition = definition
        self.input_model = input_model
        self.output_model = output_model
        self.output = output
        self.transient_failures = transient_failures
        self.delay_seconds = delay_seconds
        self.calls = 0
        self.cancelled = False

    async def execute(self, value: BaseModel) -> dict[str, Any]:
        del value
        self.calls += 1
        try:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.calls <= self.transient_failures:
            raise ToolExecutionError(
                "scripted transient tool failure",
                code="tool_transient",
                retryable=True,
            )
        return dict(self.output)


def scripted_registry(
    *,
    order_output: dict[str, Any] | None = None,
    policy_output: dict[str, Any] | None = None,
    order_transient_failures: int = 0,
    order_delay: float = 0.0,
) -> tuple[ToolRegistry, ScriptedTool]:
    order = ScriptedTool(
        definition=LookupOrderTool.definition,
        input_model=LookupOrderInput,
        output_model=LookupOrderOutput,
        output=order_output
        or {
            "order_id": "ORD-1001",
            "item_category": "household",
            "delivery_status": "delivered",
            "delivery_date": "2026-08-10",
            "purchase_channel": "online",
            "market": "US",
        },
        transient_failures=order_transient_failures,
        delay_seconds=order_delay,
    )
    policy = ScriptedTool(
        definition=LookupReturnPolicyTool.definition,
        input_model=LookupReturnPolicyInput,
        output_model=LookupReturnPolicyOutput,
        output=policy_output
        or {
            "market": "US",
            "item_category": "household",
            "purchase_channel": "online",
            "return_window_days": 30,
            "item_condition_requirement": "unopened",
            "applicable_fees": "none",
        },
    )
    return ToolRegistry([cast(RuntimeTool, order), cast(RuntimeTool, policy)]), order
