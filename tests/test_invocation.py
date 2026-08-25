from __future__ import annotations

import asyncio

import pytest

from agent_runtime.domain import AttemptOutcome, FailureOrigin, RetryPolicy, RuntimePhase
from agent_runtime.errors import InvocationFailed, ProviderError
from agent_runtime.invocation import AttemptObservation, invoke_with_policy


def policy(*, attempts: int = 2, timeout: float = 0.05) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=attempts,
        timeout_seconds=timeout,
        initial_backoff_seconds=0.0,
        max_backoff_seconds=0.0,
        jitter_ratio=0.0,
    )


@pytest.mark.asyncio
async def test_transient_typed_failure_retries() -> None:
    calls = 0
    observed: list[AttemptObservation[str]] = []

    async def operation(attempt: int) -> str:
        nonlocal calls
        calls += 1
        if attempt == 1:
            raise ProviderError("transient", code="transient", retryable=True)
        return "ok"

    async def observer(value: AttemptObservation[str]) -> None:
        observed.append(value)

    result = await invoke_with_policy(
        operation,
        policy(),
        phase=RuntimePhase.PLANNING,
        source_component="test",
        timeout_code="timeout",
        timeout_origin=FailureOrigin.MODEL_PROVIDER,
        observer=observer,
    )
    assert result == "ok"
    assert calls == 2
    assert [item.outcome for item in observed] == [
        AttemptOutcome.FAILED,
        AttemptOutcome.SUCCEEDED,
    ]


@pytest.mark.asyncio
async def test_permanent_failure_is_not_retried() -> None:
    observed: list[AttemptObservation[str]] = []

    async def operation(_: int) -> str:
        raise ProviderError("permanent", code="permanent", retryable=False)

    async def observer(value: AttemptObservation[str]) -> None:
        observed.append(value)

    with pytest.raises(InvocationFailed):
        await invoke_with_policy(
            operation,
            policy(attempts=3),
            phase=RuntimePhase.PLANNING,
            source_component="test",
            timeout_code="timeout",
            timeout_origin=FailureOrigin.MODEL_PROVIDER,
            observer=observer,
        )
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_each_attempt_has_independent_timeout() -> None:
    observed: list[AttemptObservation[str]] = []

    async def operation(attempt: int) -> str:
        if attempt == 1:
            await asyncio.sleep(1)
        return "ok"

    async def observer(value: AttemptObservation[str]) -> None:
        observed.append(value)

    assert (
        await invoke_with_policy(
            operation,
            policy(timeout=0.01),
            phase=RuntimePhase.PLANNING,
            source_component="test",
            timeout_code="timeout",
            timeout_origin=FailureOrigin.MODEL_PROVIDER,
            observer=observer,
        )
        == "ok"
    )
    assert [item.outcome for item in observed] == [
        AttemptOutcome.TIMED_OUT,
        AttemptOutcome.SUCCEEDED,
    ]


@pytest.mark.asyncio
async def test_cancellation_propagates_after_attempt_observation() -> None:
    observed: list[AttemptObservation[str]] = []

    async def operation(_: int) -> str:
        await asyncio.Event().wait()
        return "unreachable"

    async def observer(value: AttemptObservation[str]) -> None:
        observed.append(value)

    task = asyncio.create_task(
        invoke_with_policy(
            operation,
            policy(),
            phase=RuntimePhase.EXECUTING_WORKERS,
            source_component="test",
            timeout_code="timeout",
            timeout_origin=FailureOrigin.MODEL_PROVIDER,
            observer=observer,
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(observed) == 1
    assert observed[0].failure is not None
    assert observed[0].failure.origin == FailureOrigin.CANCELLATION
