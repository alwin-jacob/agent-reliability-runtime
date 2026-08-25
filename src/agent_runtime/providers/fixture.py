"""Deterministic scripted provider returning raw JSON responses."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_runtime.domain import ModelRequest, ModelResponse, UsageRecord
from agent_runtime.errors import ConfigurationError, ProviderError


class _FixtureBehavior(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["success", "transient_failure", "permanent_failure", "wait_for_cancellation"]
    raw_json: str | None = None
    delay_seconds: float = Field(default=0.0, ge=0, le=300)
    code: str | None = None
    message: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    token_source: Literal["synthetic"] | None = None

    @model_validator(mode="after")
    def validate_synthetic_usage(self) -> _FixtureBehavior:
        has_tokens = self.input_tokens is not None or self.output_tokens is not None
        if has_tokens and self.token_source != "synthetic":
            raise ValueError("fixture token counts must be labeled synthetic")
        if not has_tokens and self.token_source is not None:
            raise ValueError("synthetic token_source requires at least one token count")
        return self


class _FixtureScript(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["0.2.0"]
    provider_id: str = Field(min_length=1)
    responses: dict[str, list[_FixtureBehavior]]


class FixtureModelProvider:
    """Consume one scripted behavior per request key and retain the last behavior."""

    def __init__(self, script: _FixtureScript) -> None:
        self._script = script
        self._counts: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self.cancellation_observed = False

    @classmethod
    def from_file(cls, path: Path) -> FixtureModelProvider:
        try:
            script = _FixtureScript.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ConfigurationError(
                f"could not load model fixture: {error}", code="model_fixture_invalid"
            ) from error
        return cls(script)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> FixtureModelProvider:
        try:
            return cls(_FixtureScript.model_validate(data))
        except ValueError as error:
            raise ConfigurationError(
                f"could not load model fixture: {error}", code="model_fixture_invalid"
            ) from error

    @property
    def identifier(self) -> str:
        return self._script.provider_id

    @property
    def external_model_calls(self) -> bool:
        return False

    async def generate(self, request: ModelRequest) -> ModelResponse:
        behavior = await self._next_behavior(request.fixture_key)
        if behavior.delay_seconds:
            await asyncio.sleep(behavior.delay_seconds)
        if behavior.kind == "wait_for_cancellation":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancellation_observed = True
                raise
        if behavior.kind == "transient_failure":
            raise ProviderError(
                behavior.message or "scripted transient provider failure",
                code=behavior.code or "fixture_provider_transient",
                retryable=True,
            )
        if behavior.kind == "permanent_failure":
            raise ProviderError(
                behavior.message or "scripted permanent provider failure",
                code=behavior.code or "fixture_provider_permanent",
                retryable=False,
            )
        if behavior.raw_json is None:
            raise ProviderError(
                "successful fixture behavior requires raw_json",
                code="fixture_response_missing",
                retryable=False,
            )
        return ModelResponse(
            response_id=f"response-{uuid4().hex}",
            raw_json=behavior.raw_json,
            model_id=self.identifier,
            provider="fixture",
            finish_reason="scripted",
            usage=UsageRecord(
                input_tokens=behavior.input_tokens,
                output_tokens=behavior.output_tokens,
                cost_usd=0.0,
                token_source=behavior.token_source,
            ),
            metadata={"fixture_key": request.fixture_key, "behavior": behavior.kind},
        )

    async def _next_behavior(self, key: str) -> _FixtureBehavior:
        async with self._lock:
            behaviors = self._script.responses.get(key)
            if not behaviors:
                raise ProviderError(
                    f"no scripted response for key {key!r}",
                    code="fixture_key_missing",
                    retryable=False,
                )
            index = self._counts.get(key, 0)
            self._counts[key] = index + 1
            return behaviors[min(index, len(behaviors) - 1)]
