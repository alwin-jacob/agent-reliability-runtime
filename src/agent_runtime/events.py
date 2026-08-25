"""Concurrency-safe structured event recording."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from agent_runtime.domain import AgentEvent, RuntimePhase
from agent_runtime.errors import sanitized_payload


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class EventRecorder:
    def __init__(self, *, now: Callable[[], datetime] = utc_now) -> None:
        self._now = now
        self._lock = asyncio.Lock()
        self._events: list[AgentEvent] = []

    async def record(
        self,
        event_type: str,
        *,
        source_component: str,
        phase: RuntimePhase,
        span_id: str,
        parent_span_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AgentEvent:
        safe_payload = sanitized_payload(payload or {})
        if not isinstance(safe_payload, dict):  # pragma: no cover - fixed input shape
            safe_payload = {"value": safe_payload}
        async with self._lock:
            event = AgentEvent(
                event_id=f"evt-{uuid4().hex}",
                sequence=len(self._events) + 1,
                timestamp=isoformat_utc(self._now()),
                event_type=event_type,
                source_component=source_component,
                phase=phase,
                span_id=span_id,
                parent_span_id=parent_span_id,
                payload=safe_payload,
            )
            self._events.append(event)
            return event

    async def snapshot(self) -> list[AgentEvent]:
        async with self._lock:
            return list(self._events)
