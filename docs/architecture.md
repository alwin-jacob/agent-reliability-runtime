# Architecture

## Boundaries

`domain.py` defines strict, frozen, serialization-safe runtime values. `integrity.py` owns neutral canonical JSON and SHA-256 helpers; `versions.py` separates package, artifact, task, config, and fixture versions. `providers/` owns the asynchronous raw-response model boundary. `tools/` owns definitions, policy checks, Pydantic input/output validation, and in-memory retail fixtures. `invocation.py` owns attempt timeout/retry/cancellation behavior. `semantics.py` owns pure Stage 1 plan, task-bound tool-request, worker-evidence, and final-decision rules without importing LangGraph. `orchestration.py` owns LangGraph nodes, the accepted-state recorder, and run-scoped injected services. `artifacts.py` owns fingerprints, causal cross-record validation, JSON Schema, and atomic persistence while calling the same pure semantics. `runtime.py` resolves confined inputs, constructs fresh per-run services, executes the graph, and assembles artifacts. `cli.py` is a thin exit-code and compact-JSON interface.

Generic datasets, scoring, regression analysis, and evaluation artifacts belong to `llm-eval-reliability`, not this repository.

## Supervisor and worker graph

The compiled low-level `StateGraph` has three node definitions:

1. `supervisor_plan` invokes the raw fixture provider, parses a `SupervisorPlan`, enforces the exact two-worker Stage 1 contract, and transitions to `executing_workers`.
2. A conditional edge returns two `langgraph.types.Send` values. Both target the same generic `worker` node with a distinct `WorkerAssignment`. A reducer appends each `WorkerResult`.
3. `supervisor_finalize` requires successful `order-worker` and `policy-worker` evidence, invokes the final fixture decision, and applies the pure semantic validator before transitioning to `succeeded`.

The task carries `as_of_date`, `item_condition`, `market`, `item_category`, and `purchase_channel`. Each worker call must exactly match the applicable task fields. Finalization independently validates delivered order status, task/order/policy context equality, condition equality, and authoritative fees/window evidence. It calculates whole calendar days with standard-library dates, rejects a decision date before delivery, treats the window as inclusive, and requires the canonical eligible/ineligible code and exactly both worker IDs.

Each worker acquires the explicit run-scoped semaphore before recording its active section. The overlap test injects a deterministic barrier: neither worker can proceed until both have entered. With a sequential implementation that test times out and fails. A separate configured-bound test observes a maximum of one active worker when the semaphore limit is one.

## Dependency injection and checkpoint readiness

`GraphState` contains only task, phase, plan, current assignment, accumulated results, and final decision values. Provider instances, tool registry, event recorder, phase/evidence locks, semaphore, concurrency probe, accepted-state recorder, and other services live in `RuntimeContext`. The recorder is run-scoped and updated only after a plan, worker result, or final decision passes its live checks. Related record, event, and accepted-state mutations are grouped into narrowly bounded cancellation-deferring commits: an outer `CancelledError` waits for the shielded in-memory unit to finish and is then re-raised. If `ainvoke()` cannot return because of cancellation or an unexpected exception, artifact assembly uses that accepted snapshot instead of stale initial graph input. This is not a checkpointer and does not provide crash recovery or resume.

## State transitions

The accepted success path is:

```text
initialized -> planning -> executing_workers -> finalizing -> succeeded
```

`planning`, `executing_workers`, or `finalizing` can transition to `failed`. Any active phase, including `initialized`, can transition to `cancelled`. A shared pure transition predicate drives orchestration and artifact validation; the orchestration surface raises typed `invalid_state_transition` errors. Artifact validation also proves that ordered transition events form one chain from initialized to the persisted final phase.

## Model and tool boundaries

The `ModelProvider` protocol is asynchronous and provider-independent. Stage 1 implements only a versioned scripted fixture provider. Before invocation, the runtime persists the exact frozen `ModelRequestRecord` passed to the provider, including logical turn, source, phase, fixture key, full structured payload, UTC creation time, and canonical payload SHA-256. Retries reuse that same request. The provider returns `ModelResponse.raw_json`; parsing into `SupervisorPlan`, public `WorkerToolRequest`, or `FinalDecision` occurs after invocation. For the fixture adapter, the internal request record is the complete provider input. It is not chain-of-thought. A real adapter may have an additional provider-wire request/envelope that needs separate redacted provenance.

`LoadedInputs` stores immutable fixture bytes rather than a consumed provider. Every `execute_loaded()` constructs a fresh `FixtureModelProvider`, so reusing one loaded configuration reproduces a transient-failure-then-success script and the same semantic fingerprint. No sampler or repeated-sampling analysis is implemented.

Tools publish typed definitions with generated input/output schemas and fixed metadata: no network, no filesystem, no side effects, and idempotent. Fixture files are read during setup into memory. Worker invocation parses the public strict tool-request model, resolves tool policy/input, applies the exact task-bound semantic contract, and only then persists the accepted logical call. It executes through the common policy and validates every attempt output before accepting a worker result.

## Retry ownership and accounting

One logical model turn is one durable model request excluding retries; every provider try is a model attempt. One logical tool call is one accepted tool operation; every execution try is a tool attempt. Attempts record timestamps, duration, outcome, response/output or failure, phase, source, and parent trace context. Nonnegative `duration_ms` comes from a monotonic measurement independent of UTC wall-clock timestamps and is not required to equal their subtraction. Accounting counts logical model turns from request records and derives attempts from attempt records; validation derives it again.

The common invocation function applies an independent `asyncio.timeout` to every attempt. Typed retryable provider/tool failures, timeout, and explicitly typed transient infrastructure failures may retry. Exponential backoff is capped; fixture-stage jitter is constrained to zero. Model-output, policy, input/output validation, unknown-tool, cancellation, and unexpected unclassified failures do not retry. The graph itself is never retried. Artifact validation independently groups attempts by durable request or call and enforces the applicable effective `RunConfig` maximum, contiguous numbering, terminal success, retryability, exact timeout/cancellation taxonomy, and valid early-stop behavior.

A future evaluation adapter should configure the evaluation engine's outer candidate attempts to one by default, preventing a second retry loop around a complete agent run.

## Cancellation

`CancelledError` is observed for the active attempt, never converted to `InvocationFailed`, and re-raised. Worker `finally` blocks leave the active section and release semaphore capacity, including cancellation inside the optional concurrency probe. Model/tool terminal evidence, accepted-plan dispatch, worker acceptance, finalization entry, classified/unexpected failed terminalization, and final success are cancellation-consistent in-memory units. The runtime records a typed cancellation failure and terminal transition for an active phase, snapshots evidence after cleanup, and attempts to atomically persist a cancelled artifact. A persistence failure becomes a sanitized note on the original cancellation and never replaces it. If success or failure was already committed, cancellation-safe terminal assembly persists that terminal artifact; committed success does not gain `run_cancellation`, and the caller's cancellation is then re-raised. `PhaseManager` records the transition event before publishing its new in-memory phase. No checkpointer is involved.

## Event ordering

The in-memory recorder assigns sequence numbers under an `asyncio.Lock`. Artifact order therefore reflects observation order: sequence values are unique, contiguous, and strictly increasing, but worker-event interleaving can differ between runs. Artifact schema 0.3.0 has one closed event vocabulary covering run start, accepted transitions, planning, dispatch, worker lifecycle, every model/tool attempt, finalization, terminal outcome, and persistence start; unknown event types are rejected. Validation requires nondecreasing event timestamps, all durable timestamps within the inclusive run interval, operation start/terminal ordering consistent with attempt records, and distinct run, lifecycle, model-attempt, and tool-attempt span identities. Matching lifecycle start/end events intentionally reuse their one lifecycle span.

## Artifact integrity and reproducibility

`content_sha256` is SHA-256 of canonical JSON with that field omitted. The configuration fingerprint binds embedded task/effective config plus task/config/fixture digests. The semantic fingerprint binds stable request causality (turn, source, phase, fixture key, payload digest), normalized provider responses/attempt outcomes, downstream tool/final outcomes, event types/sources, failure codes, and accounting while excluding volatile IDs, spans, timestamps, durations, source commit/dirty state, and destination. These unkeyed hashes detect modification; they are not signatures, and a writer with modification access can recompute them. Source commit, lock digest, and other provenance require matching repository context before supporting authenticity.

Artifact validation separately proves the exact successful four-turn contract, request/attempt identity, canonical request digests, effective retry policy, deterministic response-to-failure causality, terminal response equality to accepted plan/calls/decision, exact attempt-event cardinality and context, coherent failure references, accepted worker/tool reconciliation, fixture-response consistency, lifecycle order, UTC timestamps, and the same pure semantics used live. Every successful order or policy attempt is strictly revalidated regardless of the final run status, and its accepted worker output must equal the terminal output. Successful fixture responses require scripted finish, zero cost, synthetic-or-absent token attribution, matching fixture metadata, and a consistent model ID; exactly the model, orders, and policies fixture digests are required. These rules check internal consistency rather than authenticity. Successful artifacts reject missing and unrelated turns. Failed/cancelled artifacts allow only legitimately reached turns and cannot invent deterministic output failures contradicted by their stored responses.

Full artifacts are expected to differ in volatile run/event/attempt/tool IDs, timestamps, durations, actual concurrent event order, source commit/dirty provenance, and destination-external context. Tests remove exactly those fields, normalize event order, and compare everything else across two runs. Semantic fingerprints must match.

Persistence uses a temporary file in the destination directory, JSON serialization, flush, `fsync`, close, and `os.replace`. Replacement failure removes the temporary and leaves no partial final file.

## Trade-offs and known limitations

- In-memory evidence plus one final write keeps Stage 1 inspectable but cannot survive a process crash.
- One generic worker shape and two fixture tools are enough to prove fan-out and policies but not general planning quality.
- Static fixtures make CI deterministic and cost-free but provide no evidence about real models or external provider reliability.
- Artifact validation is deliberately stricter than JSON Schema alone because cross-record, accounting, lifecycle, privacy, and fingerprint invariants are semantic.
- Pydantic models and runtime context preserve a checkpoint-ready separation, but adding a checkpointer requires a separate compatibility/resume design.
- Transitive LangGraph dependencies are unused unless explicitly wired; their presence is not project observability or a provider integration.
