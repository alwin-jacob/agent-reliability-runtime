"""Low-level LangGraph supervisor/worker orchestration."""

from __future__ import annotations

import asyncio
import json
import operator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Protocol, TypeVar, cast
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError
from typing_extensions import TypedDict

from agent_runtime.domain import (
    AttemptOutcome,
    FailureOrigin,
    FailureRecord,
    FinalDecision,
    ModelAttempt,
    ModelRequest,
    ModelResponse,
    RunConfig,
    RuntimePhase,
    SupervisorPlan,
    TaskSpec,
    ToolCall,
    ToolResult,
    WorkerAssignment,
    WorkerResult,
    is_valid_transition,
)
from agent_runtime.errors import (
    ClassifiedError,
    InvocationFailed,
    ModelOutputError,
    OrchestrationError,
    failure_from_error,
)
from agent_runtime.events import EventRecorder, isoformat_utc, utc_now
from agent_runtime.invocation import AttemptObservation, invoke_with_policy
from agent_runtime.providers.base import ModelProvider
from agent_runtime.tools.registry import ToolRegistry


class WorkerConcurrencyProbe(Protocol):
    async def entered(self, worker_id: str, active: int) -> None: ...

    async def exited(self, worker_id: str, active: int) -> None: ...


class _WorkerToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    tool_name: str
    arguments: dict[str, JsonValue]


class GraphState(TypedDict, total=False):
    task: TaskSpec
    phase: RuntimePhase
    plan: SupervisorPlan | None
    assignment: WorkerAssignment
    worker_results: Annotated[list[WorkerResult], operator.add]
    final_decision: FinalDecision | None


def validate_transition(current: RuntimePhase, target: RuntimePhase) -> None:
    if not is_valid_transition(current, target):
        raise OrchestrationError(
            f"invalid runtime phase transition {current.value} -> {target.value}",
            code="invalid_state_transition",
        )


class PhaseManager:
    def __init__(self, recorder: EventRecorder, run_span_id: str) -> None:
        self._current = RuntimePhase.INITIALIZED
        self._recorder = recorder
        self._run_span_id = run_span_id
        self._lock = asyncio.Lock()

    @property
    def current(self) -> RuntimePhase:
        return self._current

    async def transition(self, target: RuntimePhase, *, source_component: str) -> None:
        async with self._lock:
            previous = self._current
            validate_transition(previous, target)
            self._current = target
            await self._recorder.record(
                "state_transition",
                source_component=source_component,
                phase=target,
                span_id=self._run_span_id,
                payload={"from": previous.value, "to": target.value},
            )


class RuntimeServices:
    """Run-scoped mutable services injected outside serialization-ready graph state."""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        tools: ToolRegistry,
        recorder: EventRecorder,
        config: RunConfig,
        run_span_id: str,
        concurrency_probe: WorkerConcurrencyProbe | None = None,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.recorder = recorder
        self.config = config
        self.run_span_id = run_span_id
        self.phase = PhaseManager(recorder, run_span_id)
        self.worker_semaphore = asyncio.Semaphore(config.max_worker_concurrency)
        self.concurrency_probe = concurrency_probe
        self.active_workers = 0
        self.max_active_workers = 0
        self._activity_lock = asyncio.Lock()
        self._evidence_lock = asyncio.Lock()
        self._model_attempts: list[ModelAttempt] = []
        self._tool_calls: list[ToolCall] = []
        self._tool_results: list[ToolResult] = []
        self._failures: list[FailureRecord] = []

    async def worker_entered(self, worker_id: str) -> None:
        async with self._activity_lock:
            self.active_workers += 1
            self.max_active_workers = max(self.max_active_workers, self.active_workers)
            active = self.active_workers
        if self.concurrency_probe is not None:
            await self.concurrency_probe.entered(worker_id, active)

    async def worker_exited(self, worker_id: str) -> None:
        async with self._activity_lock:
            self.active_workers -= 1
            active = self.active_workers
        if self.concurrency_probe is not None:
            await self.concurrency_probe.exited(worker_id, active)

    async def add_failure(self, failure: FailureRecord) -> None:
        async with self._evidence_lock:
            self._failures.append(failure)

    async def add_tool_call(self, call: ToolCall) -> None:
        async with self._evidence_lock:
            self._tool_calls.append(call)

    async def evidence(
        self,
    ) -> tuple[list[ModelAttempt], list[ToolCall], list[ToolResult], list[FailureRecord]]:
        async with self._evidence_lock:
            return (
                list(self._model_attempts),
                list(self._tool_calls),
                list(self._tool_results),
                list(self._failures),
            )

    async def invoke_model(
        self,
        request: ModelRequest,
        *,
        phase: RuntimePhase,
        span_id: str,
        parent_span_id: str,
    ) -> ModelResponse:
        attempt_ids: dict[int, str] = {}
        last_failure: FailureRecord | None = None

        async def on_start(attempt: int, _: str) -> None:
            attempt_id = f"model-attempt-{uuid4().hex}"
            attempt_ids[attempt] = attempt_id
            await self.recorder.record(
                "model_attempt_start",
                source_component=request.source_component,
                phase=phase,
                span_id=attempt_id,
                parent_span_id=span_id,
                payload={"attempt": attempt, "logical_turn_id": request.logical_turn_id},
            )

        async def observe(observation: AttemptObservation[ModelResponse]) -> None:
            nonlocal last_failure
            attempt_id = attempt_ids[observation.attempt]
            failure = observation.failure
            if failure is not None:
                failure = failure.model_copy(
                    update={"model_attempt_id": attempt_id, "span_id": span_id}
                )
                last_failure = failure
            record = ModelAttempt(
                attempt_id=attempt_id,
                logical_turn_id=request.logical_turn_id,
                request_id=request.request_id,
                source_component=request.source_component,
                attempt=observation.attempt,
                outcome=observation.outcome,
                started_at=observation.started_at,
                completed_at=observation.completed_at,
                duration_ms=observation.duration_ms,
                response=observation.value,
                failure=failure,
            )
            async with self._evidence_lock:
                self._model_attempts.append(record)
            event_type = {
                AttemptOutcome.SUCCEEDED: "model_attempt_end",
                AttemptOutcome.FAILED: "model_attempt_failure",
                AttemptOutcome.TIMED_OUT: "model_attempt_timeout",
            }[observation.outcome]
            await self.recorder.record(
                event_type,
                source_component=request.source_component,
                phase=phase,
                span_id=attempt_id,
                parent_span_id=span_id,
                payload={
                    "attempt": observation.attempt,
                    "logical_turn_id": request.logical_turn_id,
                    "outcome": observation.outcome.value,
                    "failure_code": failure.code if failure else None,
                },
            )

        try:
            return await invoke_with_policy(
                lambda _: self.provider.generate(request),
                self.config.model_retry,
                phase=phase,
                source_component=request.source_component,
                timeout_code="model_attempt_timeout",
                timeout_origin=FailureOrigin.MODEL_PROVIDER,
                observer=observe,
                on_attempt_start=on_start,
            )
        except InvocationFailed as error:
            raise InvocationFailed(last_failure or error.failure) from error

    async def invoke_tool(
        self,
        call: ToolCall,
        operation: Any,
        *,
        phase: RuntimePhase,
        parent_span_id: str,
    ) -> dict[str, JsonValue]:
        attempt_spans: dict[int, str] = {}
        last_failure: FailureRecord | None = None

        async def on_start(attempt: int, _: str) -> None:
            attempt_span = f"tool-attempt-{uuid4().hex}"
            attempt_spans[attempt] = attempt_span
            await self.recorder.record(
                "tool_attempt_start",
                source_component=call.tool_name,
                phase=phase,
                span_id=attempt_span,
                parent_span_id=parent_span_id,
                payload={"attempt": attempt, "tool_call_id": call.call_id},
            )

        async def observe(observation: AttemptObservation[dict[str, JsonValue]]) -> None:
            nonlocal last_failure
            attempt_span = attempt_spans[observation.attempt]
            failure = observation.failure
            if failure is not None:
                failure = failure.model_copy(
                    update={"tool_call_id": call.call_id, "span_id": attempt_span}
                )
                last_failure = failure
            result = ToolResult(
                result_id=f"tool-result-{uuid4().hex}",
                attempt_span_id=attempt_span,
                tool_call_id=call.call_id,
                worker_id=call.worker_id,
                tool_name=call.tool_name,
                attempt=observation.attempt,
                outcome=observation.outcome,
                started_at=observation.started_at,
                completed_at=observation.completed_at,
                duration_ms=observation.duration_ms,
                output=observation.value,
                failure=failure,
            )
            async with self._evidence_lock:
                self._tool_results.append(result)
            event_type = {
                AttemptOutcome.SUCCEEDED: "tool_attempt_end",
                AttemptOutcome.FAILED: "tool_attempt_failure",
                AttemptOutcome.TIMED_OUT: "tool_attempt_timeout",
            }[observation.outcome]
            await self.recorder.record(
                event_type,
                source_component=call.tool_name,
                phase=phase,
                span_id=attempt_span,
                parent_span_id=parent_span_id,
                payload={
                    "attempt": observation.attempt,
                    "tool_call_id": call.call_id,
                    "outcome": observation.outcome.value,
                    "failure_code": failure.code if failure else None,
                },
            )

        try:
            return await invoke_with_policy(
                operation,
                self.config.tool_retry,
                phase=phase,
                source_component=call.tool_name,
                timeout_code="tool_attempt_timeout",
                timeout_origin=FailureOrigin.TOOL_EXECUTION,
                observer=observe,
                on_attempt_start=on_start,
            )
        except InvocationFailed as error:
            raise InvocationFailed(last_failure or error.failure) from error


@dataclass(frozen=True)
class RuntimeContext:
    provider: ModelProvider
    tool_registry: ToolRegistry
    event_recorder: EventRecorder
    services: RuntimeServices


def build_graph() -> CompiledStateGraph[GraphState, RuntimeContext, GraphState, GraphState]:
    builder = StateGraph(GraphState, context_schema=RuntimeContext)
    builder.add_node("supervisor_plan", supervisor_plan_node)
    builder.add_node("worker", worker_node)
    builder.add_node("supervisor_finalize", supervisor_finalize_node)
    builder.add_edge(START, "supervisor_plan")
    builder.add_conditional_edges("supervisor_plan", dispatch_workers)
    builder.add_edge("worker", "supervisor_finalize")
    builder.add_edge("supervisor_finalize", END)
    return cast(
        CompiledStateGraph[GraphState, RuntimeContext, GraphState, GraphState],
        builder.compile(),
    )


async def supervisor_plan_node(state: GraphState, runtime: Runtime[RuntimeContext]) -> GraphState:
    services = runtime.context.services
    span_id = f"span-plan-{uuid4().hex}"
    await services.recorder.record(
        "supervisor_planning_start",
        source_component="supervisor",
        phase=RuntimePhase.PLANNING,
        span_id=span_id,
        parent_span_id=services.run_span_id,
    )
    request = ModelRequest(
        request_id=f"request-{uuid4().hex}",
        logical_turn_id="turn-supervisor-plan",
        source_component="supervisor",
        fixture_key="supervisor_plan",
        payload={"task": state["task"].model_dump(mode="json")},
    )
    try:
        response = await services.invoke_model(
            request,
            phase=RuntimePhase.PLANNING,
            span_id=span_id,
            parent_span_id=services.run_span_id,
        )
        plan = _parse_model_output(response.raw_json, SupervisorPlan)
        _validate_stage_one_plan(plan)
    except InvocationFailed as error:
        await _fail_phase(services, error.failure, span_id=span_id)
        return {"phase": RuntimePhase.FAILED, "worker_results": []}
    except ClassifiedError as error:
        failure = _normalize_error(
            error,
            phase=RuntimePhase.PLANNING,
            source_component="supervisor",
            span_id=span_id,
        )
        await _fail_phase(services, failure, span_id=span_id)
        return {"phase": RuntimePhase.FAILED, "worker_results": []}
    await services.recorder.record(
        "supervisor_planning_end",
        source_component="supervisor",
        phase=RuntimePhase.PLANNING,
        span_id=span_id,
        parent_span_id=services.run_span_id,
        payload={"assignment_count": len(plan.assignments)},
    )
    await services.phase.transition(RuntimePhase.EXECUTING_WORKERS, source_component="supervisor")
    for assignment in plan.assignments:
        await services.recorder.record(
            "worker_dispatch",
            source_component="supervisor",
            phase=RuntimePhase.EXECUTING_WORKERS,
            span_id=span_id,
            parent_span_id=services.run_span_id,
            payload={"worker_id": assignment.worker_id},
        )
    return {
        "phase": RuntimePhase.EXECUTING_WORKERS,
        "plan": plan,
        "worker_results": [],
    }


def dispatch_workers(state: GraphState) -> list[Send] | str:
    if state["phase"] == RuntimePhase.FAILED:
        return END
    plan = state.get("plan")
    if plan is None:
        raise OrchestrationError("planning completed without a plan", code="plan_missing")
    return [
        Send(
            "worker",
            {
                "task": state["task"],
                "phase": RuntimePhase.EXECUTING_WORKERS,
                "assignment": assignment,
            },
        )
        for assignment in plan.assignments
    ]


async def worker_node(state: GraphState, runtime: Runtime[RuntimeContext]) -> GraphState:
    services = runtime.context.services
    assignment = state["assignment"]
    worker_id = assignment.worker_id
    span_id = f"span-worker-{worker_id}-{uuid4().hex}"
    async with services.worker_semaphore:
        entered = False
        try:
            await services.worker_entered(worker_id)
            entered = True
            await services.recorder.record(
                "worker_start",
                source_component=worker_id,
                phase=RuntimePhase.EXECUTING_WORKERS,
                span_id=span_id,
                parent_span_id=services.run_span_id,
                payload={"purpose": assignment.purpose},
            )
            result = await _execute_worker(state["task"], assignment, services, span_id)
            event_type = "worker_end" if result.succeeded else "worker_failure"
            await services.recorder.record(
                event_type,
                source_component=worker_id,
                phase=RuntimePhase.EXECUTING_WORKERS,
                span_id=span_id,
                parent_span_id=services.run_span_id,
                payload={
                    "succeeded": result.succeeded,
                    "failure_code": result.failure.code if result.failure else None,
                },
            )
            return {"worker_results": [result]}
        finally:
            if entered:
                await services.worker_exited(worker_id)


async def _execute_worker(
    task: TaskSpec,
    assignment: WorkerAssignment,
    services: RuntimeServices,
    span_id: str,
) -> WorkerResult:
    request = ModelRequest(
        request_id=f"request-{uuid4().hex}",
        logical_turn_id=f"turn-{assignment.worker_id}",
        source_component=assignment.worker_id,
        fixture_key=f"worker:{assignment.worker_id}",
        payload={
            "task": task.model_dump(mode="json"),
            "assignment": assignment.model_dump(mode="json"),
        },
    )
    try:
        response = await services.invoke_model(
            request,
            phase=RuntimePhase.EXECUTING_WORKERS,
            span_id=span_id,
            parent_span_id=services.run_span_id,
        )
        decision = _parse_model_output(response.raw_json, _WorkerToolRequest)
        call = ToolCall(
            call_id=f"tool-call-{uuid4().hex}",
            worker_id=assignment.worker_id,
            tool_name=decision.tool_name,
            input=decision.arguments,
            span_id=span_id,
        )
        await services.add_tool_call(call)
        tool, validated_input = services.tools.resolve_call(
            allowed_tools=assignment.allowed_tools,
            call=call,
        )

        async def execute(_: int) -> dict[str, JsonValue]:
            raw_output = await tool.execute(validated_input)
            return services.tools.validate_output(tool, raw_output)

        output = await services.invoke_tool(
            call,
            execute,
            phase=RuntimePhase.EXECUTING_WORKERS,
            parent_span_id=span_id,
        )
        return WorkerResult(
            worker_id=assignment.worker_id,
            succeeded=True,
            tool_name=decision.tool_name,
            output=output,
        )
    except InvocationFailed as error:
        await services.add_failure(error.failure)
        return WorkerResult(
            worker_id=assignment.worker_id,
            succeeded=False,
            failure=error.failure,
        )
    except ClassifiedError as error:
        failure = _normalize_error(
            error,
            phase=RuntimePhase.EXECUTING_WORKERS,
            source_component=assignment.worker_id,
            span_id=span_id,
            tool_call_id=call.call_id if "call" in locals() else None,
        )
        await services.add_failure(failure)
        return WorkerResult(
            worker_id=assignment.worker_id,
            succeeded=False,
            failure=failure,
        )


async def supervisor_finalize_node(
    state: GraphState, runtime: Runtime[RuntimeContext]
) -> GraphState:
    services = runtime.context.services
    span_id = f"span-finalize-{uuid4().hex}"
    results = state.get("worker_results", [])
    by_id = {result.worker_id: result for result in results}
    required = {"order-worker", "policy-worker"}
    if set(by_id) != required or any(not by_id[item].succeeded for item in required):
        error = OrchestrationError(
            "both successful Stage 1 worker results are required",
            code="required_worker_evidence_missing",
        )
        failure = _normalize_error(
            error,
            phase=RuntimePhase.EXECUTING_WORKERS,
            source_component="supervisor",
            span_id=span_id,
        )
        await _fail_phase(services, failure, span_id=span_id)
        return {"phase": RuntimePhase.FAILED, "final_decision": None}

    await services.phase.transition(RuntimePhase.FINALIZING, source_component="supervisor")
    await services.recorder.record(
        "supervisor_finalization_start",
        source_component="supervisor",
        phase=RuntimePhase.FINALIZING,
        span_id=span_id,
        parent_span_id=services.run_span_id,
    )
    request = ModelRequest(
        request_id=f"request-{uuid4().hex}",
        logical_turn_id="turn-supervisor-finalize",
        source_component="supervisor",
        fixture_key="supervisor_finalize",
        payload={
            "task": state["task"].model_dump(mode="json"),
            "worker_results": [
                by_id[worker_id].model_dump(mode="json") for worker_id in sorted(required)
            ],
        },
    )
    try:
        response = await services.invoke_model(
            request,
            phase=RuntimePhase.FINALIZING,
            span_id=span_id,
            parent_span_id=services.run_span_id,
        )
        decision = _parse_model_output(response.raw_json, FinalDecision)
        _validate_final_decision(decision, state["task"], by_id)
    except InvocationFailed as error:
        await _fail_phase(services, error.failure, span_id=span_id)
        return {"phase": RuntimePhase.FAILED, "final_decision": None}
    except ClassifiedError as error:
        failure = _normalize_error(
            error,
            phase=RuntimePhase.FINALIZING,
            source_component="supervisor",
            span_id=span_id,
        )
        await _fail_phase(services, failure, span_id=span_id)
        return {"phase": RuntimePhase.FAILED, "final_decision": None}
    await services.recorder.record(
        "supervisor_finalization_end",
        source_component="supervisor",
        phase=RuntimePhase.FINALIZING,
        span_id=span_id,
        parent_span_id=services.run_span_id,
        payload={"decision_code": decision.decision_code},
    )
    await services.phase.transition(RuntimePhase.SUCCEEDED, source_component="supervisor")
    await services.recorder.record(
        "run_success",
        source_component="runtime",
        phase=RuntimePhase.SUCCEEDED,
        span_id=services.run_span_id,
        payload={"decision_code": decision.decision_code},
    )
    return {"phase": RuntimePhase.SUCCEEDED, "final_decision": decision}


async def _fail_phase(services: RuntimeServices, failure: FailureRecord, *, span_id: str) -> None:
    await services.add_failure(failure)
    if services.phase.current not in {RuntimePhase.FAILED, RuntimePhase.CANCELLED}:
        await services.phase.transition(
            RuntimePhase.FAILED, source_component=failure.source_component
        )
    await services.recorder.record(
        "run_failure",
        source_component=failure.source_component,
        phase=RuntimePhase.FAILED,
        span_id=span_id,
        parent_span_id=services.run_span_id,
        payload={"failure_code": failure.code},
    )


ModelT = TypeVar("ModelT", bound=BaseModel)


def _parse_model_output(raw: str, model: type[ModelT]) -> ModelT:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ModelOutputError(
            "fixture provider returned malformed JSON", code="malformed_model_json"
        ) from error
    try:
        return model.model_validate(value)
    except ValidationError as error:
        raise ModelOutputError(
            f"fixture provider output failed schema validation: {error}",
            code="model_output_schema_invalid",
        ) from error


def _validate_stage_one_plan(plan: SupervisorPlan) -> None:
    expected = {
        "order-worker": (
            ["lookup_order"],
            "retrieve authoritative order facts",
        ),
        "policy-worker": (
            ["lookup_return_policy"],
            "retrieve the applicable return policy",
        ),
    }
    observed = {item.worker_id: (item.allowed_tools, item.purpose) for item in plan.assignments}
    if observed != expected or len(plan.assignments) != 2:
        raise ModelOutputError(
            "supervisor plan violated the Stage 1 assignment contract",
            code="supervisor_plan_contract_invalid",
        )


def _validate_final_decision(
    decision: FinalDecision,
    task: TaskSpec,
    results: dict[str, WorkerResult],
) -> None:
    if set(decision.evidence_worker_ids) != {"order-worker", "policy-worker"}:
        raise ModelOutputError(
            "final decision must reference both worker IDs",
            code="final_decision_evidence_invalid",
        )
    if decision.order_id != task.order_id:
        raise ModelOutputError(
            "final decision order ID does not match the task",
            code="final_decision_order_invalid",
        )
    order_output = results["order-worker"].output or {}
    policy_output = results["policy-worker"].output or {}
    if order_output.get("order_id") != decision.order_id:
        raise ModelOutputError(
            "final decision does not match authoritative order evidence",
            code="final_decision_order_evidence_invalid",
        )
    if policy_output.get("return_window_days") != decision.policy_window_days:
        raise ModelOutputError(
            "final decision does not match authoritative policy evidence",
            code="final_decision_policy_evidence_invalid",
        )


def _normalize_error(
    error: ClassifiedError,
    *,
    phase: RuntimePhase,
    source_component: str,
    span_id: str,
    tool_call_id: str | None = None,
    now: datetime | None = None,
) -> FailureRecord:
    return failure_from_error(
        error,
        phase=phase,
        source_component=source_component,
        timestamp=isoformat_utc(now or utc_now()),
        tool_call_id=tool_call_id,
        span_id=span_id,
    )
