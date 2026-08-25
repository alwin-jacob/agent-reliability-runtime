from __future__ import annotations

import getpass
import json
import os
import socket
from pathlib import Path
from typing import Any, NoReturn

import pytest
from conftest import make_loaded

from agent_runtime.artifacts import (
    attach_content_hash,
    configuration_fingerprint,
    generated_schema,
    read_artifact,
    semantic_fingerprint,
    validate_artifact,
    write_artifact,
)
from agent_runtime.domain import AttemptOutcome, RunArtifact, RunStatus, RuntimePhase
from agent_runtime.errors import ArtifactError
from agent_runtime.runtime import execute_loaded


@pytest.mark.asyncio
async def test_artifact_round_trip_and_accounting_reconcile(tmp_path: Path) -> None:
    destination = tmp_path / "run.json"
    artifact = await execute_loaded(make_loaded(tmp_path), output_path=destination)
    observed = read_artifact(destination)
    assert observed == artifact
    assert observed.accounting.logical_model_turns == 4
    assert observed.accounting.model_attempts == 4
    assert observed.accounting.logical_tool_calls == 2
    assert observed.accounting.tool_attempts == 2
    assert observed.accounting.successful_model_attempts == sum(
        item.outcome == AttemptOutcome.SUCCEEDED for item in observed.model_attempts
    )
    assert observed.accounting.successful_tool_attempts == sum(
        item.outcome == AttemptOutcome.SUCCEEDED for item in observed.tool_results
    )
    assert observed.accounting.external_model_calls is False
    assert observed.accounting.cost_usd == 0.0
    assert observed.accounting.input_tokens is None
    assert observed.accounting.output_tokens is None


@pytest.mark.asyncio
async def test_tampered_content_hash_is_rejected(tmp_path: Path) -> None:
    destination = tmp_path / "run.json"
    await execute_loaded(make_loaded(tmp_path), output_path=destination)
    payload = json.loads(destination.read_text(encoding="utf-8"))
    payload["status"] = "failed"
    destination.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactError, match="content_sha256"):
        read_artifact(destination)


@pytest.mark.asyncio
async def test_wrong_schema_version_and_unknown_fields_are_rejected(tmp_path: Path) -> None:
    destination = tmp_path / "run.json"
    await execute_loaded(make_loaded(tmp_path), output_path=destination)
    payload = json.loads(destination.read_text(encoding="utf-8"))
    payload["schema_version"] = "9.9.9"
    payload["unknown"] = True
    destination.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactError, match="could not read"):
        read_artifact(destination)


@pytest.mark.asyncio
async def test_semantic_status_and_accounting_invariants_are_enforced(tmp_path: Path) -> None:
    artifact = await execute_loaded(make_loaded(tmp_path), output_path=tmp_path / "run.json")
    no_decision_state = artifact.final_state.model_copy(update={"final_decision": None})
    no_decision = attach_content_hash(
        artifact.model_copy(update={"final_decision": None, "final_state": no_decision_state})
    )
    with pytest.raises(ArtifactError, match="requires a final decision"):
        validate_artifact(no_decision)

    failed_state = artifact.final_state.model_copy(
        update={"phase": RuntimePhase.FAILED, "final_decision": None}
    )
    no_failure = attach_content_hash(
        artifact.model_copy(
            update={
                "status": RunStatus.FAILED,
                "final_decision": None,
                "final_state": failed_state,
                "failures": [],
            }
        )
    )
    with pytest.raises(ArtifactError, match="requires FailureRecord"):
        validate_artifact(no_failure)

    wrong_accounting = artifact.accounting.model_copy(
        update={"model_attempts": artifact.accounting.model_attempts + 1}
    )
    inconsistent = attach_content_hash(artifact.model_copy(update={"accounting": wrong_accounting}))
    with pytest.raises(ArtifactError, match="does not reconcile"):
        validate_artifact(inconsistent)

    unknown_call_result = artifact.tool_results[0].model_copy(
        update={"tool_call_id": "missing-call"}
    )
    unknown_call = attach_content_hash(
        artifact.model_copy(
            update={"tool_results": [unknown_call_result, *artifact.tool_results[1:]]}
        )
    )
    with pytest.raises(ArtifactError, match="unknown logical tool call"):
        validate_artifact(unknown_call)

    response_less = artifact.model_attempts[0].model_copy(update={"response": None})
    contradictory = attach_content_hash(
        artifact.model_copy(
            update={"model_attempts": [response_less, *artifact.model_attempts[1:]]}
        )
    )
    with pytest.raises(ArtifactError, match="contradictory evidence"):
        validate_artifact(contradictory)


@pytest.mark.asyncio
async def test_required_lifecycle_events_are_enforced(tmp_path: Path) -> None:
    artifact = await execute_loaded(make_loaded(tmp_path), output_path=tmp_path / "run.json")
    events = [item for item in artifact.events if item.event_type != "run_start"]
    events = [item.model_copy(update={"sequence": index}) for index, item in enumerate(events, 1)]
    semantic = semantic_fingerprint(
        digests=artifact.content_digests,
        final_decision=artifact.final_decision,
        events=events,
        model_requests=artifact.model_requests,
        model_attempts=artifact.model_attempts,
        tool_calls=artifact.tool_calls,
        tool_results=artifact.tool_results,
        failures=artifact.failures,
        accounting=artifact.accounting,
    )
    missing_start = attach_content_hash(
        artifact.model_copy(update={"events": events, "semantic_fingerprint": semantic})
    )
    with pytest.raises(ArtifactError, match="run_start"):
        validate_artifact(missing_start)


@pytest.mark.asyncio
async def test_fixture_digest_values_must_be_sha256(tmp_path: Path) -> None:
    artifact = await execute_loaded(make_loaded(tmp_path), output_path=tmp_path / "run.json")
    bad_digests = artifact.content_digests.model_copy(
        update={"fixture_sha256": {"model": "not-a-sha"}}
    )
    invalid = attach_content_hash(artifact.model_copy(update={"content_digests": bad_digests}))
    with pytest.raises(ArtifactError, match="JSON Schema"):
        validate_artifact(invalid)


@pytest.mark.asyncio
async def test_atomic_write_failure_leaves_no_partial_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.json"
    artifact = await execute_loaded(make_loaded(tmp_path), output_path=source)
    destination = tmp_path / "atomic.json"

    def fail_replace(source_path: object, destination_path: object) -> NoReturn:
        del source_path, destination_path
        raise OSError("scripted replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(ArtifactError, match="atomically persist"):
        write_artifact(artifact, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".atomic.json.*.tmp"))


@pytest.mark.asyncio
async def test_two_runs_share_semantic_fingerprint_but_not_full_artifact(tmp_path: Path) -> None:
    loaded_one = make_loaded(tmp_path / "one")
    loaded_two = make_loaded(tmp_path / "two")
    first = await execute_loaded(loaded_one, output_path=tmp_path / "first.json")
    second = await execute_loaded(loaded_two, output_path=tmp_path / "second.json")
    assert first.semantic_fingerprint == second.semantic_fingerprint
    assert first.configuration_fingerprint == second.configuration_fingerprint
    assert first.run_id != second.run_id
    assert first.content_sha256 != second.content_sha256
    assert first.final_decision == second.final_decision
    assert first.accounting == second.accounting
    assert _stable_projection(first) == _stable_projection(second)


@pytest.mark.asyncio
async def test_private_path_and_secret_like_value_are_rejected(tmp_path: Path) -> None:
    artifact = await execute_loaded(make_loaded(tmp_path), output_path=tmp_path / "run.json")
    private_config = artifact.run_config.model_copy(
        update={"model_fixture": "/Users/private-person/model.json"}
    )
    private_artifact = artifact.model_copy(
        update={
            "run_config": private_config,
            "configuration_fingerprint": configuration_fingerprint(
                artifact.content_digests, artifact.task, private_config
            ),
        }
    )
    private_artifact = attach_content_hash(private_artifact)
    with pytest.raises(ArtifactError, match="private path"):
        validate_artifact(private_artifact)

    secret_event = artifact.events[0].model_copy(
        update={"payload": {"authorization": "Bearer abcdefghijklmnopqrstuvwxyz"}}
    )
    secret_artifact = artifact.model_copy(update={"events": [secret_event, *artifact.events[1:]]})
    secret_artifact = attach_content_hash(secret_artifact)
    with pytest.raises(ArtifactError):
        validate_artifact(secret_artifact)


@pytest.mark.asyncio
async def test_artifact_omits_identifying_and_external_configuration(tmp_path: Path) -> None:
    artifact = await execute_loaded(make_loaded(tmp_path), output_path=tmp_path / "run.json")
    encoded = json.dumps(artifact.model_dump(mode="json"), sort_keys=True)
    assert getpass.getuser() not in encoded
    assert socket.gethostname() not in encoded
    assert os.environ.get("HOME", "<no-home-value>") not in encoded
    assert "remote_url" not in encoded
    assert "https://" not in encoded


def test_generated_schema_matches_checked_schema() -> None:
    checked = json.loads(Path("schemas/run-artifact-v0.3.0.json").read_text(encoding="utf-8"))
    assert checked == generated_schema()


def test_success_status_enum_is_stable() -> None:
    assert RunStatus.SUCCEEDED.value == "succeeded"


def _stable_projection(artifact: RunArtifact) -> dict[str, Any]:
    """Remove exactly the documented volatile identity/timing/order fields."""

    payload = artifact.model_dump(mode="json")
    for key in ("run_id", "started_at", "completed_at", "content_sha256"):
        payload.pop(key)
    provenance = payload["provenance"]
    provenance.pop("source_commit")
    provenance.pop("git_dirty")
    payload["final_state"]["worker_results"].sort(key=lambda item: item["worker_id"])

    requests = payload["model_requests"]
    for request in requests:
        request.pop("request_id")
        request.pop("created_at")
    requests.sort(key=lambda item: item["logical_turn_id"])

    events = payload["events"]
    for event in events:
        for key in ("event_id", "sequence", "timestamp", "span_id", "parent_span_id"):
            event.pop(key)
        if event["event_type"] == "run_start":
            event["payload"].pop("run_id")
        event["payload"].pop("tool_call_id", None)
        event["payload"].pop("request_id", None)
    events.sort(key=lambda item: json.dumps(item, sort_keys=True))

    attempts = payload["model_attempts"]
    for attempt in attempts:
        for key in (
            "attempt_id",
            "request_id",
            "parent_span_id",
            "started_at",
            "completed_at",
            "duration_ms",
        ):
            attempt.pop(key)
        if attempt["response"] is not None:
            attempt["response"].pop("response_id")
    attempts.sort(key=lambda item: (item["logical_turn_id"], item["attempt"]))

    calls = payload["tool_calls"]
    for call in calls:
        call.pop("call_id")
        call.pop("span_id")
    calls.sort(key=lambda item: (item["worker_id"], item["tool_name"]))

    results = payload["tool_results"]
    for result in results:
        for key in (
            "result_id",
            "attempt_span_id",
            "tool_call_id",
            "parent_span_id",
            "started_at",
            "completed_at",
            "duration_ms",
        ):
            result.pop(key)
    results.sort(key=lambda item: (item["worker_id"], item["tool_name"], item["attempt"]))
    return payload
