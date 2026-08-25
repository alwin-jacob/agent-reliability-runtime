from __future__ import annotations

import asyncio

import pytest

from agent_runtime.domain import ModelRequest
from agent_runtime.errors import ConfigurationError, ProviderError
from agent_runtime.providers.fixture import FixtureModelProvider


def request(key: str = "key") -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        logical_turn_id="turn-1",
        source_component="test",
        fixture_key=key,
        payload={},
    )


@pytest.mark.asyncio
async def test_fixture_provider_returns_raw_success() -> None:
    provider = FixtureModelProvider.from_dict(
        {
            "schema_version": "0.1.0",
            "provider_id": "fixture-test",
            "responses": {"key": [{"kind": "success", "raw_json": '{"ok":true}'}]},
        }
    )
    response = await provider.generate(request())
    assert response.raw_json == '{"ok":true}'
    assert response.provider == "fixture"
    assert provider.external_model_calls is False


@pytest.mark.asyncio
async def test_fixture_provider_transient_then_success() -> None:
    provider = FixtureModelProvider.from_dict(
        {
            "schema_version": "0.1.0",
            "provider_id": "fixture-test",
            "responses": {
                "key": [
                    {"kind": "transient_failure", "code": "transient"},
                    {"kind": "success", "raw_json": "{}"},
                ]
            },
        }
    )
    with pytest.raises(ProviderError) as caught:
        await provider.generate(request())
    assert caught.value.retryable is True
    assert (await provider.generate(request())).raw_json == "{}"


@pytest.mark.asyncio
async def test_fixture_provider_permanent_failure() -> None:
    provider = FixtureModelProvider.from_dict(
        {
            "schema_version": "0.1.0",
            "provider_id": "fixture-test",
            "responses": {"key": [{"kind": "permanent_failure", "code": "permanent"}]},
        }
    )
    with pytest.raises(ProviderError) as caught:
        await provider.generate(request())
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_fixture_provider_cancellation_is_observed() -> None:
    provider = FixtureModelProvider.from_dict(
        {
            "schema_version": "0.1.0",
            "provider_id": "fixture-test",
            "responses": {"key": [{"kind": "wait_for_cancellation"}]},
        }
    )
    task = asyncio.create_task(provider.generate(request()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.cancellation_observed is True


def test_fixture_token_counts_require_synthetic_label() -> None:
    with pytest.raises(ConfigurationError, match="labeled synthetic"):
        FixtureModelProvider.from_dict(
            {
                "schema_version": "0.1.0",
                "provider_id": "fixture-test",
                "responses": {
                    "key": [
                        {
                            "kind": "success",
                            "raw_json": "{}",
                            "input_tokens": 4,
                        }
                    ]
                },
            }
        )
