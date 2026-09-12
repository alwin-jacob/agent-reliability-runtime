# Runtime specification

## Purpose

This repository implements a deterministic Python 3.11+ runtime for small agent systems with explicit orchestration, typed tools, bounded concurrency, failure handling, cancellation semantics, accounting, and inspectable execution artifacts.

The reference workflow is a retail return-eligibility task. It provides a compact end-to-end system for exercising the runtime's orchestration and reliability contracts without depending on external services.

## Graph contract

The runtime uses a low-level LangGraph `StateGraph` with three execution stages:

1. `supervisor_plan` produces a validated `SupervisorPlan`.
2. A conditional edge fans out worker assignments using `langgraph.types.Send`.
3. `supervisor_finalize` consumes the validated worker results and produces the final decision.

Both assignments target the same generic worker node.

The reference workflow contains:

* `order-worker`, which may call only `lookup_order`;
* `policy-worker`, which may call only `lookup_return_policy`.

A reducer accumulates `WorkerResult` values from the concurrent workers before finalization.

Worker execution is bounded by an explicit run-scoped semaphore.

## Task semantics

The task contains:

* `order_id`;
* `as_of_date`;
* `item_condition`;
* `market`;
* `item_category`;
* `purchase_channel`.

The order worker must request the task's exact order ID.

The policy worker must request the task's exact market, category, and purchase-channel context.

The final decision requires:

* delivered order evidence;
* matching task, order, and policy context;
* matching item condition;
* a decision date on or after delivery;
* both required worker results.

Whole calendar days are derived from `as_of_date - delivery_date`.

The return window is inclusive:

```text
days_since_delivery <= return_window_days
```

The decision must use the canonical eligibility code, order ID, policy window, fees, and both worker IDs.

The same pure semantic validation functions are used during execution and when validating persisted artifacts.

## Provider boundary

`ModelProvider` is asynchronous and provider-independent.

The reference implementation uses a deterministic scripted fixture provider that returns raw versioned JSON.

Each logical model turn persists one structured `ModelRequestRecord` before its first provider attempt. The request contains:

* logical turn;
* source;
* runtime phase;
* fixture key;
* structured payload;
* UTC creation time;
* canonical payload SHA-256.

Retries reference the same logical request.

Provider responses are parsed after invocation into the applicable strict public model:

* `SupervisorPlan`;
* `WorkerToolRequest`;
* `FinalDecision`.

For the deterministic fixture provider, the persisted structured request is the complete provider input.

Provider-specific adapters may add a separate redacted wire-level record when their external API envelope contains additional transport metadata.

## Tool boundary

Tools expose typed definitions with strict Pydantic input and output schemas.

The reference runtime provides:

* `lookup_order`;
* `lookup_return_policy`.

Tool policy is checked before execution.

Every successful tool attempt is output-validated before its value can become an accepted worker result.

Accepted worker output must match the terminal successful tool output.

The fixture-backed tools operate on data loaded during runtime setup and execute as read-only in-memory operations.

## Invocation policy

Model and tool operations use a shared invocation model with:

* independent per-attempt timeouts;
* classified failures;
* retry policy;
* exponential backoff;
* attempt records;
* event records;
* cancellation propagation.

One logical model turn may contain multiple provider attempts.

One logical tool call may contain multiple execution attempts.

Retryable execution or infrastructure failures may retry when permitted by the effective configuration.

Validation, policy, malformed-output, cancellation, and unclassified failures remain distinct from retryable execution failures.

The graph itself is not wrapped in an internal whole-run retry loop.

## Runtime states

The successful state path is:

```text
initialized
    -> planning
    -> executing_workers
    -> finalizing
    -> succeeded
```

Active phases can transition to `failed` or `cancelled` through the runtime's validated transition rules.

A shared transition predicate is used by both execution and artifact validation.

## Failure model

Failures receive stable `failure_id` values.

Those identifiers connect:

* attempt-level failures;
* worker failures;
* top-level failure records;
* failure events;
* terminal runtime evidence.

Artifact validation rejects dangling, duplicate, contradictory, or causally inconsistent failure references.

Provider, execution, validation, timeout, cancellation, and unexpected failure boundaries remain separately represented so the artifact preserves where a failure occurred.

## Cancellation

`asyncio.CancelledError` remains control flow and is re-raised to the caller.

Cancellation-safe in-memory evidence units preserve coherent runtime state across boundaries such as:

* startup;
* planning entry;
* model and tool terminal evidence;
* accepted worker results;
* finalization entry;
* terminal failure recording;
* final success.

Cancellation cleanup propagates to active work, releases concurrency resources, snapshots accepted state, records the appropriate terminal evidence, and then re-raises the original cancellation.

If success or failure has already been committed, terminal artifact assembly preserves that terminal state.

## Accepted state

A run-scoped recorder tracks validated state that has crossed an acceptance boundary:

* accepted plan;
* completed worker results;
* final decision.

This lets artifact assembly use the latest validated execution state even when graph execution exits through cancellation or an unexpected exception.

## Event model

Events receive strictly increasing sequence numbers under a concurrency-safe recorder.

The event model covers:

* run start;
* state transitions;
* planning lifecycle;
* worker dispatch and lifecycle;
* model attempts;
* tool attempts;
* finalization;
* failure and cancellation;
* terminal outcome;
* persistence lifecycle.

Artifact validation checks event ordering against the corresponding durable operation records.

Durations use a monotonic clock and therefore do not need to equal UTC wall-clock timestamp subtraction.

## Accounting

Accounting distinguishes:

* logical model turns;
* model attempts;
* logical tool calls;
* tool attempts.

The runtime derives accounting from the recorded operations, and artifact validation independently recomputes it.

This makes retry behavior visible without conflating retries with new logical work.

## Artifact contract

Package version `0.3.2` emits durable run-artifact schema `0.3.0`.

Historical artifact schemas `0.1.0` and `0.2.0` remain available alongside the current schema.

Artifacts contain:

* task;
* effective configuration;
* accepted state;
* final decision when available;
* structured model requests;
* provider responses;
* model attempts;
* tool calls and attempts;
* tool results;
* events;
* failures;
* accounting;
* provenance;
* input digests;
* integrity fingerprints.

Strict models reject unknown fields.

## Cross-record validation

Artifact validation checks relationships across the complete execution record.

Examples include:

```text
request -> attempt -> response
plan -> worker assignment
worker assignment -> tool call
tool call -> successful output
successful output -> worker result
worker result -> accepted state
final response -> final decision
failure -> attempt/event references
```

Validation also checks:

* contiguous attempt numbering;
* effective retry limits;
* request and attempt timing;
* event and lifecycle ordering;
* successful tool-result identity;
* accepted-state consistency;
* deterministic response-to-failure causality;
* fixture-response consistency;
* accounting reconciliation.

## Integrity fingerprints

The artifact contains content, configuration, and semantic fingerprints.

`content_sha256` is computed over canonical JSON with the hash field omitted.

The configuration fingerprint binds the stable task, effective configuration, and relevant input digests.

The semantic fingerprint captures stable execution causality and normalized outcomes while excluding volatile identifiers, timestamps, durations, and concurrent observation order.

These fingerprints support modification detection and reproducibility checks. Recorded repository provenance supplies the corresponding source context.

## Persistence

Artifacts are written atomically using:

1. a temporary file in the destination directory;
2. JSON serialization;
3. flush;
4. `fsync`;
5. close;
6. `os.replace`.

A failed replacement removes the temporary file rather than leaving a partial final artifact.

Runtime evidence remains in memory until the final artifact write, so this persistence model is intentionally different from incremental checkpointing.

## Reproducibility

`LoadedInputs` retains immutable fixture construction data and creates a fresh provider instance for each execution.

Independent deterministic executions can therefore reproduce the same logical attempt pattern and semantic fingerprint while retaining truthful volatile fields such as:

* run IDs;
* event IDs;
* timestamps;
* durations;
* concurrent event ordering.

Tests compare stable execution projections after removing only the documented volatile fields.

## Verification

The repository verifies the runtime with:

* Ruff linting;
* Ruff formatting checks;
* strict mypy;
* deterministic pytest coverage;
* schema synchronization;
* CLI execution;
* generated artifact validation;
* checked artifact validation;
* semantic reproduction checks.

The test suite includes success paths, adversarial artifacts, bounded concurrency, retries, timeouts, malformed provider output, policy violations, schema failures, cancellation, accounting, integrity validation, atomic persistence, and CLI behavior.

GitHub Actions runs the verification matrix across Python 3.11, 3.12, and 3.13.

## Extension boundaries

The runtime separates graph state from injected runtime services so additional providers, tools, persistence mechanisms, and evaluation adapters can be introduced without changing the core orchestration model.

The current executable reference path uses deterministic fixtures and one-process execution. Persistent checkpoint/resume and external provider adapters can be added as separate extensions with their own compatibility and provenance contracts.
