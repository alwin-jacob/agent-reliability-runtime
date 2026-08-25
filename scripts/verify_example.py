"""Reproduce and semantically verify the checked-in successful example."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from agent_runtime.artifacts import read_artifact
from agent_runtime.domain import FinalDecision, RunStatus
from agent_runtime.runtime import execute_loaded, load_inputs

EXPECTED = FinalDecision(
    eligible=True,
    decision_code="RETURN_ELIGIBLE",
    reason=(
        "The unopened delivered household item is within the 30-day US online return "
        "window and has no fee."
    ),
    next_action="Start the online return for ORD-1001 and send the item back unopened.",
    order_id="ORD-1001",
    policy_window_days=30,
    evidence_worker_ids=["order-worker", "policy-worker"],
)


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
    if checked.final_decision != EXPECTED or reproduced.final_decision != EXPECTED:
        raise RuntimeError("example final decision does not match expected test evidence")
    if checked.semantic_fingerprint != reproduced.semantic_fingerprint:
        raise RuntimeError("reproduced semantic fingerprint differs from checked artifact")
    if checked.configuration_fingerprint != reproduced.configuration_fingerprint:
        raise RuntimeError("reproduced configuration fingerprint differs from checked artifact")
    if [event.sequence for event in checked.events] != list(range(1, len(checked.events) + 1)):
        raise RuntimeError("checked event sequence invariant failed")
    if checked.failures:
        raise RuntimeError("successful checked artifact unexpectedly contains failures")
    return {
        "valid": True,
        "semantic_fingerprint": checked.semantic_fingerprint,
        "configuration_fingerprint": checked.configuration_fingerprint,
        "checked_run_id": checked.run_id,
        "reproduced_run_id": reproduced.run_id,
        "event_count": len(checked.events),
    }


def main() -> int:
    print(json.dumps(asyncio.run(verify()), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
