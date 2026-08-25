"""Provider-independent asynchronous model protocol."""

from __future__ import annotations

from typing import Protocol

from agent_runtime.domain import ModelRequest, ModelResponse


class ModelProvider(Protocol):
    @property
    def identifier(self) -> str: ...

    @property
    def external_model_calls(self) -> bool: ...

    async def generate(self, request: ModelRequest) -> ModelResponse: ...
