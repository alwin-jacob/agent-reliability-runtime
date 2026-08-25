"""Allowed-tool enforcement plus strict input/output validation."""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, JsonValue, ValidationError

from agent_runtime.domain import ToolCall
from agent_runtime.errors import ToolInputError, ToolOutputError, ToolPolicyError
from agent_runtime.tools.base import RuntimeTool


class ToolRegistry:
    def __init__(self, tools: list[RuntimeTool]) -> None:
        self._tools = {tool.definition.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("tool names must be unique")

    @property
    def names(self) -> set[str]:
        return set(self._tools)

    def resolve_call(
        self,
        *,
        allowed_tools: list[str],
        call: ToolCall,
    ) -> tuple[RuntimeTool, BaseModel]:
        tool = self._tools.get(call.tool_name)
        if tool is None:
            raise ToolPolicyError(f"unknown tool {call.tool_name!r}", code="unknown_tool")
        if call.tool_name not in allowed_tools:
            raise ToolPolicyError(
                f"worker {call.worker_id!r} is not allowed to use {call.tool_name!r}",
                code="tool_disallowed",
            )
        try:
            validated = tool.input_model.model_validate(call.input)
        except ValidationError as error:
            raise ToolInputError(f"invalid input for {call.tool_name}: {error}") from error
        return tool, validated

    def validate_output(self, tool: RuntimeTool, value: dict[str, Any]) -> dict[str, JsonValue]:
        try:
            validated = tool.output_model.model_validate(value)
        except ValidationError as error:
            raise ToolOutputError(f"invalid output from {tool.definition.name}: {error}") from error
        return cast(dict[str, JsonValue], validated.model_dump(mode="json"))
