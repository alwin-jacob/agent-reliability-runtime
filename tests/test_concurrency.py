from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from conftest import BarrierProbe, TrackingProbe, make_loaded

from agent_runtime.domain import RunStatus
from agent_runtime.runtime import execute_loaded


@pytest.mark.asyncio
async def test_two_workers_overlap_at_deterministic_barrier(tmp_path: Path) -> None:
    probe = BarrierProbe()
    artifact = await asyncio.wait_for(
        execute_loaded(
            make_loaded(tmp_path),
            output_path=tmp_path / "run.json",
            concurrency_probe=probe,
        ),
        timeout=2,
    )
    assert artifact.status == RunStatus.SUCCEEDED
    assert probe.entered_workers == {"order-worker", "policy-worker"}
    assert probe.exited_workers == probe.entered_workers
    assert probe.max_active == 2


@pytest.mark.asyncio
async def test_active_workers_never_exceed_configured_bound(tmp_path: Path) -> None:
    probe = TrackingProbe()
    loaded = make_loaded(tmp_path, config_changes={"max_worker_concurrency": 1})
    artifact = await execute_loaded(
        loaded,
        output_path=tmp_path / "run.json",
        concurrency_probe=probe,
    )
    assert artifact.status == RunStatus.SUCCEEDED
    assert probe.max_active == 1
