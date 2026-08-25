# Architecture

## Boundaries

`domain.py` defines strict, frozen, serialization-safe runtime values. `providers/` owns the asynchronous raw-response model boundary. `tools/` owns definitions, policy checks, Pydantic input/output validation, and in-memory retail fixtures. `invocation.py` owns attempt timeout/retry/cancellation behavior. `orchestration.py` owns LangGraph nodes and run-scoped injected services. `artifacts.py` owns fingerprints, semantic invariants, JSON Schema, and atomic persistence. `runtime.py` resolves confined inputs, constructs services, executes the graph, and assembles artifacts. `cli.py` is a thin exit-code and compact-JSON interface.

Generic datasets, scoring, regression analysis, and evaluation artifacts belong to `llm-eval-reliability`, not this repository.

## Supervisor and worker graph

The compiled low-level `StateGraph` has three node definitions:

1. `supervisor_plan` invokes the raw fixture provider, parses a `SupervisorPlan`, enforces the exact two-worker Stage 1 contract, and transitions to `executing_workers`.
2. A conditional edge returns two `langgraph.types.Send` values. Both target the same generic `worker` node with a distinct `WorkerAssignment`. A reducer appends each `WorkerResult`.
3. `supervisor_finalize` requires successful `order-worker` and `policy-worker` evidence, invokes the final fixture decision, cross-checks its order/policy references, and transitions to `succeeded`.

Each worker acquires the explicit run-scoped semaphore before recording its active section. The overlap test injects a deterministic barrier: neither worker can proceed until both have entered. With a sequential implementation that test times out and fails. A separate configured-bound test observes a maximum of one active worker when the semaphore limit is one.

## Dependency injection and checkpoint readiness

`GraphState` contains only task, phase, plan, current assignment, accumulated results, and final decision values. Provider instances, tool registry, event recorder, phase/evidence locks, semaphore, concurrency probe, and other services live in `RuntimeContext`. This keeps graph data serialization-ready for a future checkpointer without claiming checkpointing or resume today.

## State transitions

The accepted success path is:

```text
initialized -> planning -> executing_workers -> finalizing -> succeeded
```

`planning`, `executing_workers`, or `finalizing` can transition to `failed`. Any active phase, including `initialized`, can transition to `cancelled`. A shared pure transition predicate drives orchestration and artifact validation; the orchestration surface raises typed `invalid_state_transition` errors. Artifact validation also proves that ordered transition events form one chain from initialized to the persisted final phase.

## Model and tool boundaries

The `ModelProvider` protocol is asynchronous and provider-independent. Stage 1 implements only a versioned scripted fixture provider. It returns `ModelResponse.raw_json`; parsing into `SupervisorPlan`, worker tool requests, or `FinalDecision` occurs after provider invocation so malformed and schema-invalid output paths are real. The provider can script success, transient failure then success, permanent failure, delay/timeout, malformed JSON, schema-invalid JSON, and cancellation. Any fixture token values must be labeled synthetic.

Tools publish typed definitions with generated input/output schemas and fixed metadata: no network, no filesystem, no side effects, and idempotent. Fixture files are read during setup into memory. Worker invocation first records the logical request, then checks existence and allowed-tool policy, validates input, executes through the common policy, and validates each attempt's output before accepting it.

## Retry ownership and accounting

One logical model turn is one runtime request for a model decision excluding retries; every provider try is a model attempt. One logical tool call is one requested tool operation; every execution try is a tool attempt. Attempts record start/end timestamps, duration, outcome, response/output or failure, and trace IDs. Accounting is derived from these records and validation derives it again.

The common invocation function applies an independent `asyncio.timeout` to every attempt. Typed retryable provider/tool failures, timeout, and explicitly typed transient infrastructure failures may retry. Exponential backoff is capped; fixture-stage jitter is constrained to zero. Model-output, policy, input/output validation, unknown-tool, cancellation, and unexpected unclassified failures do not retry. The graph itself is never retried.

A future evaluation adapter should configure the evaluation engine's outer candidate attempts to one by default, preventing a second retry loop around a complete agent run.

## Cancellation

`CancelledError` is observed for the active attempt, never converted to `InvocationFailed`, and re-raised. Worker `finally` blocks leave the active section and release semaphore capacity. The runtime records a typed cancellation failure and terminal transition, snapshots evidence after cleanup, atomically persists a cancelled artifact when the destination is writable, and re-raises cancellation. Persistence is shielded only for that cleanup write.

## Event ordering

The in-memory recorder assigns sequence numbers under an `asyncio.Lock`. Artifact order therefore reflects observation order: sequence values are unique, contiguous, and strictly increasing, but worker-event interleaving can differ between runs. Events cover run start, accepted transitions, planning, dispatch, worker lifecycle, every model/tool attempt, finalization, terminal outcome, and persistence start. A successful persistence-result event is emitted after writing and intentionally is not retroactively added to the already-hashed artifact.

## Artifact integrity and reproducibility

`content_sha256` is SHA-256 of canonical JSON with that field omitted. The configuration fingerprint binds embedded task/effective config plus task/config/fixture digests. The semantic fingerprint binds those digests, final decision, normalized provider/tool outcomes, sorted required event type/source pairs, failure codes, and accounting while excluding IDs, timestamps, durations, source commit/dirty state, and destination.

Full artifacts are expected to differ in volatile run/event/attempt/tool IDs, timestamps, durations, actual concurrent event order, source commit/dirty provenance, and destination-external context. Tests remove exactly those fields, normalize event order, and compare everything else across two runs. Semantic fingerprints must match.

Persistence uses a temporary file in the destination directory, JSON serialization, flush, `fsync`, close, and `os.replace`. Replacement failure removes the temporary and leaves no partial final file.

## Trade-offs and known limitations

- In-memory evidence plus one final write keeps Stage 1 inspectable but cannot survive a process crash.
- One generic worker shape and two fixture tools are enough to prove fan-out and policies but not general planning quality.
- Static fixtures make CI deterministic and cost-free but provide no evidence about real models or external provider reliability.
- Artifact validation is deliberately stricter than JSON Schema alone because cross-record, accounting, lifecycle, privacy, and fingerprint invariants are semantic.
- Pydantic models and runtime context preserve a checkpoint-ready separation, but adding a checkpointer requires a separate compatibility/resume design.
- Transitive LangGraph dependencies are unused unless explicitly wired; their presence is not project observability or a provider integration.
