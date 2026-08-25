"""Read-only in-memory retail fixture tools."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agent_runtime.domain import ToolDefinition
from agent_runtime.errors import ConfigurationError, ToolExecutionError


class _ToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LookupOrderInput(_ToolModel):
    order_id: str = Field(min_length=1)


class LookupOrderOutput(_ToolModel):
    order_id: str
    item_category: str
    delivery_status: Literal["delivered", "in_transit", "cancelled"]
    delivery_date: str
    purchase_channel: Literal["online", "store"]
    market: str


class LookupReturnPolicyInput(_ToolModel):
    market: str = Field(min_length=1)
    item_category: str = Field(min_length=1)
    purchase_channel: Literal["online", "store"]


class LookupReturnPolicyOutput(_ToolModel):
    market: str
    item_category: str
    purchase_channel: Literal["online", "store"]
    return_window_days: int = Field(ge=0)
    item_condition_requirement: str
    applicable_fees: str


class _OrderFixture(_ToolModel):
    schema_version: Literal["0.2.0"]
    delay_seconds: float = Field(default=0.0, ge=0, le=300)
    orders: list[LookupOrderOutput]


class _PolicyFixture(_ToolModel):
    schema_version: Literal["0.2.0"]
    delay_seconds: float = Field(default=0.0, ge=0, le=300)
    policies: list[LookupReturnPolicyOutput]


class LookupOrderTool:
    input_model = LookupOrderInput
    output_model = LookupOrderOutput
    definition = ToolDefinition(
        name="lookup_order",
        description="Retrieve authoritative order facts from an in-memory fixture.",
        input_schema=cast(dict[str, JsonValue], LookupOrderInput.model_json_schema()),
        output_schema=cast(dict[str, JsonValue], LookupOrderOutput.model_json_schema()),
    )

    def __init__(self, fixture: _OrderFixture) -> None:
        self._orders = {item.order_id: item for item in fixture.orders}
        self._delay_seconds = fixture.delay_seconds

    @classmethod
    def from_file(cls, path: Path) -> LookupOrderTool:
        try:
            fixture = _OrderFixture.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ConfigurationError(
                f"could not load order fixture: {error}", code="order_fixture_invalid"
            ) from error
        return cls(fixture)

    async def execute(self, value: BaseModel) -> dict[str, Any]:
        request = cast(LookupOrderInput, value)
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        order = self._orders.get(request.order_id)
        if order is None:
            raise ToolExecutionError(
                f"order {request.order_id!r} was not found",
                code="order_not_found",
                retryable=False,
            )
        return order.model_dump(mode="json")


class LookupReturnPolicyTool:
    input_model = LookupReturnPolicyInput
    output_model = LookupReturnPolicyOutput
    definition = ToolDefinition(
        name="lookup_return_policy",
        description="Retrieve an applicable return policy from an in-memory fixture.",
        input_schema=cast(dict[str, JsonValue], LookupReturnPolicyInput.model_json_schema()),
        output_schema=cast(dict[str, JsonValue], LookupReturnPolicyOutput.model_json_schema()),
    )

    def __init__(self, fixture: _PolicyFixture) -> None:
        self._policies = {
            (item.market, item.item_category, item.purchase_channel): item
            for item in fixture.policies
        }
        self._delay_seconds = fixture.delay_seconds

    @classmethod
    def from_file(cls, path: Path) -> LookupReturnPolicyTool:
        try:
            fixture = _PolicyFixture.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ConfigurationError(
                f"could not load policy fixture: {error}", code="policy_fixture_invalid"
            ) from error
        return cls(fixture)

    async def execute(self, value: BaseModel) -> dict[str, Any]:
        request = cast(LookupReturnPolicyInput, value)
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        key = (request.market, request.item_category, request.purchase_channel)
        policy = self._policies.get(key)
        if policy is None:
            raise ToolExecutionError(
                "no applicable return policy was found",
                code="return_policy_not_found",
                retryable=False,
            )
        return policy.model_dump(mode="json")
