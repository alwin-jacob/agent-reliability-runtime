from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_runtime.domain import RetryPolicy, RuntimePhase, TaskSpec
from agent_runtime.errors import OrchestrationError
from agent_runtime.orchestration import validate_transition


def test_strict_model_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(
            {
                "schema_version": "0.1.0",
                "task_id": "t",
                "customer_request": "request",
                "order_id": "ORD-1",
                "unknown": True,
            }
        )


def test_strict_model_rejects_scalar_coercion() -> None:
    with pytest.raises(ValidationError):
        RetryPolicy.model_validate(
            {
                "max_attempts": "2",
                "timeout_seconds": 1.0,
                "initial_backoff_seconds": 0.0,
                "max_backoff_seconds": 0.0,
                "jitter_ratio": 0.0,
            }
        )


def test_stage_one_retry_policy_rejects_nonzero_jitter() -> None:
    with pytest.raises(ValidationError):
        RetryPolicy.model_validate(
            {
                "max_attempts": 2,
                "timeout_seconds": 1.0,
                "initial_backoff_seconds": 0.0,
                "max_backoff_seconds": 0.0,
                "jitter_ratio": 0.1,
            }
        )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RuntimePhase.INITIALIZED, RuntimePhase.PLANNING),
        (RuntimePhase.PLANNING, RuntimePhase.EXECUTING_WORKERS),
        (RuntimePhase.EXECUTING_WORKERS, RuntimePhase.FINALIZING),
        (RuntimePhase.FINALIZING, RuntimePhase.SUCCEEDED),
        (RuntimePhase.PLANNING, RuntimePhase.FAILED),
        (RuntimePhase.EXECUTING_WORKERS, RuntimePhase.CANCELLED),
    ],
)
def test_valid_transitions(current: RuntimePhase, target: RuntimePhase) -> None:
    validate_transition(current, target)


def test_invalid_transition_raises_typed_error() -> None:
    with pytest.raises(OrchestrationError, match="invalid runtime phase transition") as caught:
        validate_transition(RuntimePhase.PLANNING, RuntimePhase.SUCCEEDED)
    assert caught.value.code == "invalid_state_transition"
