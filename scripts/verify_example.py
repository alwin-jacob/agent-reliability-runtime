"""Reproduce and semantically verify the checked-in successful example."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from agent_runtime.artifacts import read_artifact
from agent_runtime.domain import (
    AttemptOutcome,
    FinalDecision,
    RunArtifact,
    RunStatus,
    SupervisorPlan,
    WorkerToolRequest,
)
from agent_runtime.integrity import canonical_sha256
from agent_runtime.runtime import execute_loaded, load_inputs
from agent_runtime.semantics import CANONICAL_EVIDENCE_WORKER_IDS
from agent_runtime.versions import ARTIFACT_SCHEMA_VERSION, PACKAGE_VERSION

EXPECTED = FinalDecision(
    eligible=True,
    decision_code="RETURN_ELIGIBLE",
    reason=(
        "The unopened delivered household item is within the 30-day US online return "
        "window and has no fee."
    ),
    next_action="Start the online return for ORD-1001 and send the item back unopened.",
    order_id="ORD-1001",
    as_of_date="2026-08-25",
    days_since_delivery=15,
    policy_window_days=30,
    applicable_fees="none",
    evidence_worker_ids=["order-worker", "policy-worker"],
)
EXPECTED_TURNS = {
    "turn-supervisor-plan",
    "turn-order-worker",
    "turn-policy-worker",
    "turn-supervisor-finalize",
}


async def verify() -> dict[str, object]:
    root = Path.cwd().resolve()
    checked = read_artifact(Path("examples/artifacts/retail-return-v1.run.json"))
    loaded = load_inputs(
        Path("examples/tasks/retail-return-v1.json"),
        Path("examples/configs/deterministic-v1.json"),
        root=root,
    )
    with tempfile.TemporaryDirectory(prefix="agent-runtime-example-") as directory:
        reproduced = await execute_loaded(
            loaded,
            output_path=Path(directory) / "reproduced.run.json",
        )
    if checked.status != RunStatus.SUCCEEDED or reproduced.status != RunStatus.SUCCEEDED:
        raise RuntimeError("both checked and reproduced artifacts must succeed")
    if checked.schema_version != ARTIFACT_SCHEMA_VERSION or ARTIFACT_SCHEMA_VERSION != "0.3.0":
        raise RuntimeError("checked artifact does not use current schema 0.3.0")
    if checked.provenance.package_version != PACKAGE_VERSION:
        raise RuntimeError("checked artifact package provenance is not current")
    if checked.provenance.source_commit is None or checked.provenance.git_dirty is not False:
        raise RuntimeError("checked artifact must identify a clean implementation commit")
    if checked.final_decision != EXPECTED or reproduced.final_decision != EXPECTED:
        raise RuntimeError("example final decision does not match expected test evidence")
    _verify_causal_contract(checked)
    _verify_causal_contract(reproduced)
    if checked.semantic_fingerprint != reproduced.semantic_fingerprint:
        raise RuntimeError("reproduced semantic fingerprint differs from checked artifact")
    if checked.configuration_fingerprint != reproduced.configuration_fingerprint:
        raise RuntimeError("reproduced configuration fingerprint differs from checked artifact")
    if [event.sequence for event in checked.events] != list(range(1, len(checked.events) + 1)):
        raise RuntimeError("checked event sequence invariant failed")
    if checked.failures:
        raise RuntimeError("successful checked artifact unexpectedly contains failures")
    if checked.accounting.model_attempts != 4 or checked.accounting.tool_attempts != 2:
        raise RuntimeError("checked attempt accounting is not the exact fixture success pattern")
    return {
        "valid": True,
        "artifact_schema": checked.schema_version,
        "request_count": len(checked.model_requests),
        "logical_turns": sorted(item.logical_turn_id for item in checked.model_requests),
        "model_attempt_count": len(checked.model_attempts),
        "tool_attempt_count": len(checked.tool_results),
        "final_decision": checked.final_decision.model_dump(mode="json"),
        "semantic_fingerprint": checked.semantic_fingerprint,
        "configuration_fingerprint": checked.configuration_fingerprint,
        "content_sha256": checked.content_sha256,
        "checked_run_id": checked.run_id,
        "reproduced_run_id": reproduced.run_id,
        "event_count": len(checked.events),
        "source_commit": checked.provenance.source_commit,
        "git_dirty": checked.provenance.git_dirty,
    }


def _verify_causal_contract(artifact: RunArtifact) -> None:
    requests = {item.logical_turn_id: item for item in artifact.model_requests}
    if len(artifact.model_requests) != 4 or set(requests) != EXPECTED_TURNS:
        raise RuntimeError("successful artifact does not contain exactly four required requests")
    if any(item.payload_sha256 != canonical_sha256(item.payload) for item in requests.values()):
        raise RuntimeError("model request payload digest does not reproduce")
    if requests["turn-supervisor-plan"].payload != artifact.task.model_dump(mode="json"):
        raise RuntimeError("planner request payload does not equal the task")
    plan = artifact.final_state.plan
    if plan is None:
        raise RuntimeError("successful artifact has no accepted plan")
    assignments = {item.worker_id: item for item in plan.assignments}
    workers = {item.worker_id: item for item in artifact.final_state.worker_results}
    for worker_id in CANONICAL_EVIDENCE_WORKER_IDS:
        payload = requests[f"turn-{worker_id}"].payload
        if payload != {
            "task": artifact.task.model_dump(mode="json"),
            "assignment": assignments[worker_id].model_dump(mode="json"),
        }:
            raise RuntimeError(f"{worker_id} request payload does not reproduce")
    finalizer_payload = {
        "task": artifact.task.model_dump(mode="json"),
        "worker_results": [
            workers[worker_id].model_dump(mode="json")
            for worker_id in CANONICAL_EVIDENCE_WORKER_IDS
        ],
    }
    if requests["turn-supervisor-finalize"].payload != finalizer_payload:
        raise RuntimeError("finalizer request payload does not use canonical worker order")

    terminal_raw: dict[str, str] = {}
    for turn, request in requests.items():
        attempts = sorted(
            (item for item in artifact.model_attempts if item.request_id == request.request_id),
            key=lambda item: item.attempt,
        )
        if not attempts or attempts[-1].outcome != AttemptOutcome.SUCCEEDED:
            raise RuntimeError(f"{turn} has no terminal successful provider response")
        if [item.attempt for item in attempts] != list(range(1, len(attempts) + 1)):
            raise RuntimeError(f"{turn} attempts are not contiguous")
        response = attempts[-1].response
        if response is None:
            raise RuntimeError(f"{turn} terminal success has no response")
        terminal_raw[turn] = response.raw_json

    parsed_plan = SupervisorPlan.model_validate_json(terminal_raw["turn-supervisor-plan"])
    if parsed_plan != plan:
        raise RuntimeError("planner response does not equal accepted plan")
    calls = {item.worker_id: item for item in artifact.tool_calls}
    for worker_id in CANONICAL_EVIDENCE_WORKER_IDS:
        parsed = WorkerToolRequest.model_validate_json(terminal_raw[f"turn-{worker_id}"])
        call = calls[worker_id]
        if parsed.tool_name != call.tool_name or parsed.arguments != call.input:
            raise RuntimeError(f"{worker_id} response does not equal accepted tool call")
    parsed_decision = FinalDecision.model_validate_json(terminal_raw["turn-supervisor-finalize"])
    if parsed_decision != artifact.final_decision:
        raise RuntimeError("finalizer response does not equal accepted final decision")


def main() -> int:
    print(json.dumps(asyncio.run(verify()), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
