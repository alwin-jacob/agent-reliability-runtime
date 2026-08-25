"""Runtime setup, graph execution, cancellation cleanup, and artifact assembly."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from agent_runtime.artifacts import (
    attach_content_hash,
    build_accounting,
    configuration_fingerprint,
    semantic_fingerprint,
    validate_artifact,
    write_artifact,
)
from agent_runtime.domain import (
    AgentState,
    ContentDigests,
    FailureOrigin,
    FailureRecord,
    RunArtifact,
    RunConfig,
    RunStatus,
    RuntimePhase,
    TaskSpec,
)
from agent_runtime.errors import (
    ConfigurationError,
    new_failure_id,
    sanitize_message,
    unexpected_failure,
)
from agent_runtime.events import EventRecorder, isoformat_utc, utc_now
from agent_runtime.integrity import sha256_bytes
from agent_runtime.invocation import complete_cancellation_safe
from agent_runtime.orchestration import (
    GraphState,
    RuntimeContext,
    RuntimeServices,
    WorkerConcurrencyProbe,
    build_graph,
)
from agent_runtime.provenance import collect_provenance
from agent_runtime.providers.fixture import FixtureModelProvider
from agent_runtime.tools.base import RuntimeTool
from agent_runtime.tools.registry import ToolRegistry
from agent_runtime.tools.retail import LookupOrderTool, LookupReturnPolicyTool


@dataclass(frozen=True)
class LoadedInputs:
    task: TaskSpec
    config: RunConfig
    digests: ContentDigests
    model_fixture_bytes: bytes
    tools: ToolRegistry
    root: Path


def load_inputs(task_path: Path, config_path: Path, *, root: Path) -> LoadedInputs:
    resolved_root = root.resolve()
    task_source = _resolve_within(resolved_root, task_path)
    config_source = _resolve_within(resolved_root, config_path)
    try:
        task_bytes = task_source.read_bytes()
        config_bytes = config_source.read_bytes()
        task = TaskSpec.model_validate_json(task_bytes)
        config = RunConfig.model_validate_json(config_bytes)
    except (OSError, ValueError) as error:
        raise ConfigurationError(f"could not load task/config: {error}") from error
    fixture_paths = {
        "model": _resolve_fixture_path(resolved_root, config.model_fixture),
        "orders": _resolve_fixture_path(resolved_root, config.order_fixture),
        "policies": _resolve_fixture_path(resolved_root, config.policy_fixture),
    }
    try:
        fixture_bytes = {name: path.read_bytes() for name, path in fixture_paths.items()}
    except OSError as error:
        raise ConfigurationError(f"could not read configured fixture: {error}") from error
    digests = ContentDigests(
        task_sha256=sha256_bytes(task_bytes),
        config_sha256=sha256_bytes(config_bytes),
        fixture_sha256={name: sha256_bytes(value) for name, value in sorted(fixture_bytes.items())},
    )
    # Validate construction data while loading, but do not retain this stateful instance.
    FixtureModelProvider.from_bytes(fixture_bytes["model"])
    tools = ToolRegistry(
        [
            cast(RuntimeTool, LookupOrderTool.from_file(fixture_paths["orders"])),
            cast(RuntimeTool, LookupReturnPolicyTool.from_file(fixture_paths["policies"])),
        ]
    )
    return LoadedInputs(
        task=task,
        config=config,
        digests=digests,
        model_fixture_bytes=fixture_bytes["model"],
        tools=tools,
        root=resolved_root,
    )


async def execute_loaded(
    loaded: LoadedInputs,
    *,
    output_path: Path,
    concurrency_probe: WorkerConcurrencyProbe | None = None,
) -> RunArtifact:
    run_id = f"run-{uuid4().hex}"
    run_span_id = f"span-{run_id}"
    started_at = isoformat_utc(utc_now())
    recorder = EventRecorder()
    provider = FixtureModelProvider.from_bytes(loaded.model_fixture_bytes)
    services = RuntimeServices(
        provider=provider,
        tools=loaded.tools,
        recorder=recorder,
        config=loaded.config,
        run_span_id=run_span_id,
        concurrency_probe=concurrency_probe,
    )
    context = RuntimeContext(
        provider=provider,
        tool_registry=loaded.tools,
        event_recorder=recorder,
        services=services,
    )
    await recorder.record(
        "run_start",
        source_component="runtime",
        phase=RuntimePhase.INITIALIZED,
        span_id=run_span_id,
        payload={"run_id": run_id, "task_id": loaded.task.task_id},
    )
    await services.phase.transition(RuntimePhase.PLANNING, source_component="runtime")
    graph_state: GraphState = {
        "task": loaded.task,
        "phase": RuntimePhase.PLANNING,
        "plan": None,
        "worker_results": [],
        "final_decision": None,
    }
    try:
        graph = build_graph()
        raw_result = await graph.ainvoke(graph_state, context=context)
        graph_state = cast(GraphState, raw_result)
    except asyncio.CancelledError as cancellation:
        await _persist_after_cancellation(
            cancellation=cancellation,
            loaded=loaded,
            services=services,
            state=graph_state,
            run_id=run_id,
            started_at=started_at,
            output_path=output_path,
        )
        raise
    except Exception as error:
        failure = unexpected_failure(
            error,
            phase=services.phase.current,
            source_component="runtime",
            timestamp=isoformat_utc(utc_now()),
            span_id=run_span_id,
        )

        async def commit_unexpected_failure() -> None:
            await services.add_failure(failure)
            if services.phase.current in {
                RuntimePhase.PLANNING,
                RuntimePhase.EXECUTING_WORKERS,
                RuntimePhase.FINALIZING,
            }:
                await services.phase.transition(RuntimePhase.FAILED, source_component="runtime")
            await recorder.record(
                "run_failure",
                source_component="runtime",
                phase=RuntimePhase.FAILED,
                span_id=run_span_id,
                payload={"failure_id": failure.failure_id, "failure_code": failure.code},
            )

        try:
            await complete_cancellation_safe(commit_unexpected_failure)
        except asyncio.CancelledError as cancellation:
            graph_state = {**graph_state, "phase": RuntimePhase.FAILED, "final_decision": None}
            await _persist_after_cancellation(
                cancellation=cancellation,
                loaded=loaded,
                services=services,
                state=graph_state,
                run_id=run_id,
                started_at=started_at,
                output_path=output_path,
            )
            raise
        graph_state = {**graph_state, "phase": RuntimePhase.FAILED, "final_decision": None}

    try:
        return await complete_cancellation_safe(
            lambda: _assemble_and_write(
                loaded=loaded,
                services=services,
                state=graph_state,
                run_id=run_id,
                started_at=started_at,
                output_path=output_path,
            )
        )
    except asyncio.CancelledError as cancellation:
        if services.phase.current == RuntimePhase.SUCCEEDED:
            cancellation.add_note(
                "run had already committed success before cancellation propagated"
            )
        raise


async def _persist_after_cancellation(
    *,
    cancellation: asyncio.CancelledError,
    loaded: LoadedInputs,
    services: RuntimeServices,
    state: GraphState,
    run_id: str,
    started_at: str,
    output_path: Path,
) -> None:
    try:
        if services.phase.current in {RuntimePhase.SUCCEEDED, RuntimePhase.FAILED}:
            await complete_cancellation_safe(
                lambda: _assemble_and_write(
                    loaded=loaded,
                    services=services,
                    state=state,
                    run_id=run_id,
                    started_at=started_at,
                    output_path=output_path,
                )
            )
            if services.phase.current == RuntimePhase.SUCCEEDED:
                cancellation.add_note(
                    "run had already committed success; the successful artifact was "
                    "persisted before cancellation propagated"
                )
        else:
            await complete_cancellation_safe(
                lambda: _persist_cancelled(
                    loaded=loaded,
                    services=services,
                    state=state,
                    run_id=run_id,
                    started_at=started_at,
                    output_path=output_path,
                )
            )
    except BaseException as persistence_error:
        cancellation.add_note(
            "cancelled-artifact persistence failed: "
            f"{type(persistence_error).__name__}: {sanitize_message(persistence_error)}"
        )


async def _persist_cancelled(
    *,
    loaded: LoadedInputs,
    services: RuntimeServices,
    state: GraphState,
    run_id: str,
    started_at: str,
    output_path: Path,
) -> None:
    failure = FailureRecord(
        failure_id=new_failure_id(),
        code="run_cancelled",
        origin=FailureOrigin.CANCELLATION,
        phase=services.phase.current,
        source_component="runtime",
        retryable=False,
        message="run was externally cancelled",
        exception_type="CancelledError",
        timestamp=isoformat_utc(utc_now()),
        span_id=services.run_span_id,
    )
    await services.add_failure(failure)
    if services.phase.current in {
        RuntimePhase.INITIALIZED,
        RuntimePhase.PLANNING,
        RuntimePhase.EXECUTING_WORKERS,
        RuntimePhase.FINALIZING,
    }:
        await services.phase.transition(RuntimePhase.CANCELLED, source_component="runtime")
    await services.recorder.record(
        "run_cancellation",
        source_component="runtime",
        phase=RuntimePhase.CANCELLED,
        span_id=services.run_span_id,
        payload={
            "active_workers_after_cleanup": services.active_workers,
            "failure_id": failure.failure_id,
            "failure_code": failure.code,
        },
    )
    cancelled_state = cast(
        GraphState,
        {**state, "phase": RuntimePhase.CANCELLED},
    )
    await _assemble_and_write(
        loaded=loaded,
        services=services,
        state=cancelled_state,
        run_id=run_id,
        started_at=started_at,
        output_path=output_path,
    )


async def _assemble_and_write(
    *,
    loaded: LoadedInputs,
    services: RuntimeServices,
    state: GraphState,
    run_id: str,
    started_at: str,
    output_path: Path,
) -> RunArtifact:
    artifact = await _assemble_artifact(
        loaded=loaded,
        services=services,
        state=state,
        run_id=run_id,
        started_at=started_at,
    )
    await asyncio.to_thread(write_artifact, artifact, output_path)
    return artifact


async def _assemble_artifact(
    *,
    loaded: LoadedInputs,
    services: RuntimeServices,
    state: GraphState,
    run_id: str,
    started_at: str,
) -> RunArtifact:
    await services.recorder.record(
        "artifact_persistence_start",
        source_component="artifact",
        phase=services.phase.current,
        span_id=services.run_span_id,
    )
    events = await services.recorder.snapshot()
    model_requests, model_attempts, tool_calls, tool_results, failures = await services.evidence()
    accounting = build_accounting(model_requests, model_attempts, tool_calls, tool_results)
    phase = services.phase.current
    status = {
        RuntimePhase.SUCCEEDED: RunStatus.SUCCEEDED,
        RuntimePhase.FAILED: RunStatus.FAILED,
        RuntimePhase.CANCELLED: RunStatus.CANCELLED,
    }.get(phase)
    if status is None:
        raise RuntimeError(f"cannot assemble artifact from active phase {phase.value}")
    accepted = await services.accepted_state.snapshot()
    final_decision = accepted.final_decision
    final_state = AgentState(
        phase=phase,
        task=loaded.task,
        plan=accepted.plan,
        worker_results=accepted.worker_results,
        final_decision=final_decision,
    )
    config_fingerprint = configuration_fingerprint(loaded.digests, loaded.task, loaded.config)
    semantic = semantic_fingerprint(
        digests=loaded.digests,
        final_decision=final_decision,
        events=events,
        model_requests=model_requests,
        model_attempts=model_attempts,
        tool_calls=tool_calls,
        tool_results=tool_results,
        failures=failures,
        accounting=accounting,
    )
    artifact = RunArtifact(
        run_id=run_id,
        status=status,
        started_at=started_at,
        completed_at=isoformat_utc(utc_now()),
        task=loaded.task,
        run_config=loaded.config,
        final_state=final_state,
        final_decision=final_decision,
        events=events,
        model_requests=model_requests,
        model_attempts=model_attempts,
        tool_calls=tool_calls,
        tool_results=tool_results,
        failures=failures,
        accounting=accounting,
        provenance=collect_provenance(loaded.root, loaded.digests),
        content_digests=loaded.digests,
        configuration_fingerprint=config_fingerprint,
        semantic_fingerprint=semantic,
        content_sha256="0" * 64,
    )
    artifact = attach_content_hash(artifact)
    validate_artifact(artifact)
    return artifact


def _resolve_within(root: Path, value: Path) -> Path:
    if value.is_absolute():
        resolved = value.resolve()
    else:
        resolved = (root / value).resolve()
    if not resolved.is_relative_to(root):
        raise ConfigurationError("configured path escapes the repository root", code="path_escape")
    return resolved


def _resolve_fixture_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        raise ConfigurationError(
            "fixture paths must be repository-relative", code="absolute_fixture_path"
        )
    return _resolve_within(root, path)
