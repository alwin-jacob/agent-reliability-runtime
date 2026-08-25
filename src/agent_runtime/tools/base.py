"""Common typed tool protocol."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from agent_runtime.domain import ToolDefinition


class RuntimeTool(Protocol):
    definition: ToolDefinition
    input_model: type[BaseModel]
    output_model: type[BaseModel]

    async def execute(self, value: BaseModel) -> dict[str, Any]: ...
