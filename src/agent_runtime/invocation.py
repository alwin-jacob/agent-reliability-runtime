"""Reusable asynchronous retry, timeout, and cancellation policy."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar

from agent_runtime.domain import (
    AttemptOutcome,
    FailureOrigin,
    FailureRecord,
    RetryPolicy,
    RuntimePhase,
)
from agent_runtime.errors import (
    ClassifiedError,
    InvocationFailed,
    failure_from_error,
    sanitize_message,
)
from agent_runtime.events import isoformat_utc, utc_now

T = TypeVar("T")


@dataclass(frozen=True)
class AttemptObservation(Generic[T]):
    attempt: int
    outcome: AttemptOutcome
    started_at: str
    completed_at: str
    duration_ms: float
    value: T | None
    failure: FailureRecord | None


async def invoke_with_policy(
    operation: Callable[[int], Awaitable[T]],
    policy: RetryPolicy,
    *,
    phase: RuntimePhase,
    source_component: str,
    timeout_code: str,
    timeout_origin: FailureOrigin,
    observer: Callable[[AttemptObservation[T]], Awaitable[None]],
    on_attempt_start: Callable[[int, str], Awaitable[None]] | None = None,
    now: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.perf_counter,
) -> T:
    """Invoke one logical operation; cancellation is never normalized or retried."""

    for attempt in range(1, policy.max_attempts + 1):
        started_dt = now()
        started_at = isoformat_utc(started_dt)
        started = monotonic()
        if on_attempt_start is not None:
            await on_attempt_start(attempt, started_at)
        try:
            async with asyncio.timeout(policy.timeout_seconds):
                value = await operation(attempt)
        except asyncio.CancelledError as error:
            completed_at = isoformat_utc(now())
            failure = FailureRecord(
                code="invocation_cancelled",
                origin=FailureOrigin.CANCELLATION,
                phase=phase,
                source_component=source_component,
                retryable=False,
                attempt=attempt,
                message="invocation was cancelled",
                exception_type=type(error).__name__,
                timestamp=completed_at,
            )
            await observer(
                AttemptObservation[T](
                    attempt=attempt,
                    outcome=AttemptOutcome.FAILED,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_ms=_milliseconds(monotonic() - started),
                    value=None,
                    failure=failure,
                )
            )
            raise
        except TimeoutError as error:
            completed_at = isoformat_utc(now())
            failure = FailureRecord(
                code=timeout_code,
                origin=timeout_origin,
                phase=phase,
                source_component=source_component,
                retryable=True,
                attempt=attempt,
                message=f"attempt exceeded {policy.timeout_seconds:g}s timeout",
                exception_type=type(error).__name__,
                timestamp=completed_at,
            )
            observation = AttemptObservation[T](
                attempt=attempt,
                outcome=AttemptOutcome.TIMED_OUT,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=_milliseconds(monotonic() - started),
                value=None,
                failure=failure,
            )
            await observer(observation)
            if attempt >= policy.max_attempts:
                raise InvocationFailed(failure) from error
        except ClassifiedError as error:
            completed_at = isoformat_utc(now())
            failure = failure_from_error(
                error,
                phase=phase,
                source_component=source_component,
                timestamp=completed_at,
                attempt=attempt,
            )
            observation = AttemptObservation[T](
                attempt=attempt,
                outcome=AttemptOutcome.FAILED,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=_milliseconds(monotonic() - started),
                value=None,
                failure=failure,
            )
            await observer(observation)
            if not error.retryable or attempt >= policy.max_attempts:
                raise InvocationFailed(failure) from error
        except Exception as error:
            completed_at = isoformat_utc(now())
            failure = FailureRecord(
                code="unexpected_invocation_error",
                origin=FailureOrigin.UNEXPECTED_INTERNAL,
                phase=phase,
                source_component=source_component,
                retryable=False,
                attempt=attempt,
                message=sanitize_message(error),
                exception_type=type(error).__name__,
                timestamp=completed_at,
            )
            await observer(
                AttemptObservation[T](
                    attempt=attempt,
                    outcome=AttemptOutcome.FAILED,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_ms=_milliseconds(monotonic() - started),
                    value=None,
                    failure=failure,
                )
            )
            raise InvocationFailed(failure) from error
        else:
            completed_at = isoformat_utc(now())
            await observer(
                AttemptObservation[T](
                    attempt=attempt,
                    outcome=AttemptOutcome.SUCCEEDED,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_ms=_milliseconds(monotonic() - started),
                    value=value,
                    failure=None,
                )
            )
            return value

        delay = min(
            policy.max_backoff_seconds,
            policy.initial_backoff_seconds * (2 ** (attempt - 1)),
        )
        if delay > 0:
            await asyncio.sleep(delay)
    raise AssertionError("invocation loop exited without a result")  # pragma: no cover


def _milliseconds(seconds: float) -> float:
    return max(0.0, round(seconds * 1000, 3))
