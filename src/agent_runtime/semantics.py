"""Pure Stage 1 plan, tool-request, evidence, and decision semantics."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import ValidationError

from agent_runtime.domain import (
    FinalDecision,
    SupervisorPlan,
    TaskSpec,
    WorkerResult,
)
from agent_runtime.tools.retail import LookupOrderOutput, LookupReturnPolicyOutput

ORDER_WORKER_ID = "order-worker"
POLICY_WORKER_ID = "policy-worker"
REQUIRED_WORKER_IDS = frozenset({ORDER_WORKER_ID, POLICY_WORKER_ID})
CANONICAL_EVIDENCE_WORKER_IDS = [ORDER_WORKER_ID, POLICY_WORKER_ID]
_CALENDAR_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PLAN_CONTRACT = {
    ORDER_WORKER_ID: (["lookup_order"], "retrieve authoritative order facts"),
    POLICY_WORKER_ID: (
        ["lookup_return_policy"],
        "retrieve the applicable return policy",
    ),
}


class SemanticValidationError(ValueError):
    """Stable pure-validation failure that callers map to their typed boundary."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DerivedDecision:
    eligible: bool
    decision_code: str
    days_since_delivery: int


def validate_stage_one_plan(plan: SupervisorPlan) -> None:
    observed = {item.worker_id: (item.allowed_tools, item.purpose) for item in plan.assignments}
    if observed != _PLAN_CONTRACT or len(plan.assignments) != 2:
        raise SemanticValidationError(
            "supervisor plan violated the Stage 1 assignment contract",
            code="supervisor_plan_contract_invalid",
        )


def expected_tool_request(task: TaskSpec, worker_id: str) -> tuple[str, dict[str, Any]]:
    if worker_id == ORDER_WORKER_ID:
        return "lookup_order", {"order_id": task.order_id}
    if worker_id == POLICY_WORKER_ID:
        return (
            "lookup_return_policy",
            {
                "market": task.market,
                "item_category": task.item_category,
                "purchase_channel": task.purchase_channel,
            },
        )
    raise SemanticValidationError(
        f"unknown Stage 1 worker {worker_id!r}",
        code="worker_identity_invalid",
    )


def validate_worker_tool_request(
    task: TaskSpec,
    worker_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    expected_name, expected_arguments = expected_tool_request(task, worker_id)
    if tool_name != expected_name or arguments != expected_arguments:
        raise SemanticValidationError(
            f"{worker_id} tool request does not match the typed task context",
            code="worker_tool_request_contract_invalid",
        )


def validate_successful_decision(
    task: TaskSpec,
    worker_results: list[WorkerResult],
    decision: FinalDecision,
) -> DerivedDecision:
    if len(worker_results) != 2:
        raise SemanticValidationError(
            "success requires exactly two Stage 1 worker results",
            code="required_worker_evidence_invalid",
        )
    by_id = {item.worker_id: item for item in worker_results}
    if len(by_id) != 2 or set(by_id) != REQUIRED_WORKER_IDS:
        raise SemanticValidationError(
            "success requires exactly one result for each Stage 1 worker",
            code="required_worker_evidence_invalid",
        )
    if any(not item.succeeded for item in by_id.values()):
        raise SemanticValidationError(
            "success requires both Stage 1 workers to succeed",
            code="required_worker_evidence_invalid",
        )

    order = _parse_order_output(by_id[ORDER_WORKER_ID])
    policy = _parse_policy_output(by_id[POLICY_WORKER_ID])

    if order.order_id != task.order_id:
        raise SemanticValidationError(
            "authoritative order ID does not match the task",
            code="order_task_id_mismatch",
        )
    if order.delivery_status != "delivered":
        raise SemanticValidationError(
            "authoritative order is not delivered",
            code="order_not_delivered",
        )
    _require_context_match(
        "order",
        order.market,
        order.item_category,
        order.purchase_channel,
        task.market,
        task.item_category,
        task.purchase_channel,
    )
    _require_context_match(
        "policy",
        policy.market,
        policy.item_category,
        policy.purchase_channel,
        task.market,
        task.item_category,
        task.purchase_channel,
    )
    _require_context_match(
        "policy/order",
        policy.market,
        policy.item_category,
        policy.purchase_channel,
        order.market,
        order.item_category,
        order.purchase_channel,
    )
    if task.item_condition != policy.item_condition_requirement:
        raise SemanticValidationError(
            "task item condition does not satisfy the authoritative policy requirement",
            code="item_condition_requirement_mismatch",
        )

    as_of = parse_calendar_date(task.as_of_date, field="task.as_of_date")
    delivered = parse_calendar_date(order.delivery_date, field="order.delivery_date")
    if as_of < delivered:
        raise SemanticValidationError(
            "task decision date is before authoritative delivery date",
            code="decision_date_before_delivery",
        )
    days_since_delivery = (as_of - delivered).days
    eligible = days_since_delivery <= policy.return_window_days
    decision_code = "RETURN_ELIGIBLE" if eligible else "RETURN_INELIGIBLE"

    if decision.order_id != task.order_id:
        raise SemanticValidationError(
            "final decision order ID does not match the task",
            code="final_decision_order_invalid",
        )
    if decision.as_of_date != task.as_of_date:
        raise SemanticValidationError(
            "final decision date does not match the task decision date",
            code="final_decision_date_invalid",
        )
    if decision.days_since_delivery != days_since_delivery:
        raise SemanticValidationError(
            "final decision elapsed days do not match authoritative dates",
            code="final_decision_elapsed_days_invalid",
        )
    if decision.policy_window_days != policy.return_window_days:
        raise SemanticValidationError(
            "final decision return window does not match policy evidence",
            code="final_decision_policy_window_invalid",
        )
    if decision.applicable_fees != policy.applicable_fees:
        raise SemanticValidationError(
            "final decision fees do not match policy evidence",
            code="final_decision_fees_invalid",
        )
    if decision.eligible != eligible:
        raise SemanticValidationError(
            "final decision eligibility does not match the derived result",
            code="final_decision_eligibility_invalid",
        )
    if decision.decision_code != decision_code:
        raise SemanticValidationError(
            "final decision code does not match the derived result",
            code="final_decision_code_invalid",
        )
    if decision.evidence_worker_ids != CANONICAL_EVIDENCE_WORKER_IDS:
        raise SemanticValidationError(
            "final decision must reference exactly both Stage 1 worker IDs",
            code="final_decision_evidence_invalid",
        )
    return DerivedDecision(
        eligible=eligible,
        decision_code=decision_code,
        days_since_delivery=days_since_delivery,
    )


def parse_calendar_date(value: str, *, field: str) -> date:
    if _CALENDAR_DATE.fullmatch(value) is None:
        raise SemanticValidationError(
            f"{field} must use exact YYYY-MM-DD format",
            code="calendar_date_invalid",
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise SemanticValidationError(
            f"{field} is not a valid calendar date",
            code="calendar_date_invalid",
        ) from error
    if parsed.isoformat() != value:  # pragma: no cover - defensive exactness
        raise SemanticValidationError(
            f"{field} must use exact YYYY-MM-DD format",
            code="calendar_date_invalid",
        )
    return parsed


def _parse_order_output(result: WorkerResult) -> LookupOrderOutput:
    if result.tool_name != "lookup_order" or result.output is None:
        raise SemanticValidationError(
            "order worker does not contain successful lookup_order evidence",
            code="order_worker_evidence_invalid",
        )
    try:
        return LookupOrderOutput.model_validate(result.output)
    except ValidationError as error:
        raise SemanticValidationError(
            f"order worker output is invalid: {error}",
            code="order_worker_evidence_invalid",
        ) from error


def _parse_policy_output(result: WorkerResult) -> LookupReturnPolicyOutput:
    if result.tool_name != "lookup_return_policy" or result.output is None:
        raise SemanticValidationError(
            "policy worker does not contain successful lookup_return_policy evidence",
            code="policy_worker_evidence_invalid",
        )
    try:
        return LookupReturnPolicyOutput.model_validate(result.output)
    except ValidationError as error:
        raise SemanticValidationError(
            f"policy worker output is invalid: {error}",
            code="policy_worker_evidence_invalid",
        ) from error


def _require_context_match(
    source: str,
    market: str,
    category: str,
    channel: str,
    expected_market: str,
    expected_category: str,
    expected_channel: str,
) -> None:
    observed = (market, category, channel)
    expected = (expected_market, expected_category, expected_channel)
    if observed != expected:
        raise SemanticValidationError(
            f"{source} market/category/purchase-channel context does not match",
            code=f"{source.replace('/', '_')}_context_mismatch",
        )
