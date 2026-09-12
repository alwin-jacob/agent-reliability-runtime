# Architecture

## Overview

The runtime is organized around a small set of explicit boundaries:

```text
task + configuration + fixtures
              |
              v
       runtime construction
              |
              v
       LangGraph execution
              |
     +--------+--------+
     |                 |
     v                 v
model provider      typed tools
     |                 |
     +--------+--------+
              |
              v
       accepted state
              |
              v
       artifact assembly
              |
              v
      semantic validation
              |
              v
       atomic persistence
```

The design keeps orchestration, invocation policy, domain semantics, persistence, and provider/tool implementations separate so each can be inspected and tested independently.

## Module boundaries

### `domain.py`

Defines strict serialization-safe runtime values, including tasks, plans, worker assignments and results, decisions, attempts, events, failures, accounting records, configuration, and artifact structures.

Pydantic models provide the public validation boundary for structured runtime data.

### `versions.py`

Centralizes package, artifact, task, configuration, and fixture versions.

Keeping these versions explicit allows durable artifact formats to evolve independently from the Python package.

### `integrity.py`

Provides canonical JSON serialization and SHA-256 helpers used by request digests and artifact fingerprints.

### `providers/`

Defines the asynchronous model-provider boundary.

The reference implementation uses a scripted deterministic provider that returns raw JSON. Parsing and semantic validation occur outside the provider so transport/provider behavior remains separate from runtime meaning.

### `tools/`

Owns:

* tool definitions;
* tool-policy checks;
* strict input validation;
* strict output validation;
* deterministic in-memory reference data.

The reference workflow exposes `lookup_order` and `lookup_return_policy`.

### `invocation.py`

Owns model and tool attempt execution:

* per-attempt timeout;
* retry classification;
* retry limits;
* exponential backoff;
* cancellation propagation;
* attempt evidence.

This gives providers and tools one consistent reliability boundary.

### `semantics.py`

Contains LangGraph-independent domain rules.

The same pure semantic functions validate:

* supervisor plans;
* task-bound worker requests;
* accepted worker evidence;
* final decisions.

Sharing these functions between live execution and artifact validation prevents the persisted record from using a weaker interpretation than the running system.

### `orchestration.py`

Owns:

* LangGraph nodes and edges;
* worker fan-out;
* reducer behavior;
* runtime phase transitions;
* accepted-state recording;
* run-scoped service access.

### `artifacts.py`

Owns:

* artifact assembly;
* cross-record validation;
* content, configuration, and semantic fingerprints;
* JSON Schema generation;
* artifact persistence.

### `runtime.py`

Loads confined inputs, constructs fresh run-scoped services, executes the graph, snapshots accepted evidence, and coordinates final artifact construction.

### `cli.py`

Provides a compact command-line interface over runtime execution and artifact validation.

## Graph structure

The runtime uses a low-level LangGraph `StateGraph`.

```text
START
  |
  v
supervisor_plan
  |
  +--> Send(order-worker)  -------\
  |                                \
  +--> Send(policy-worker) ---------> worker-result reducer
                                      |
                                      v
                               supervisor_finalize
                                      |
                                      v
                                     END
```

There are three node definitions:

1. `supervisor_plan`
2. one generic `worker`
3. `supervisor_finalize`

The supervisor creates two `WorkerAssignment` values.

Both assignments are sent to the same generic worker node through `langgraph.types.Send`. Worker behavior comes from the assignment and its allowed tool rather than from separate worker implementations.

A reducer accumulates `WorkerResult` values before finalization.

## Supervisor planning

`supervisor_plan` invokes the provider and parses the returned JSON into a strict `SupervisorPlan`.

For the reference workflow, the accepted plan contains exactly:

* `order-worker`;
* `policy-worker`.

The order worker is bound to `lookup_order`.

The policy worker is bound to `lookup_return_policy`.

The semantic validator also checks that each planned request matches the task context.

Only a validated plan crosses the accepted-state boundary.

## Worker execution

Each worker receives a `WorkerAssignment` and resolves its allowed tool through the tool registry.

Worker execution follows this sequence:

```text
assignment
   |
   v
tool policy
   |
   v
input validation
   |
   v
task-context validation
   |
   v
logical tool call
   |
   v
attempt execution
   |
   v
output validation
   |
   v
WorkerResult
```

A successful worker result must correspond to the terminal successful output of its accepted logical tool call.

## Bounded concurrency

Workers execute under a run-scoped `asyncio.Semaphore`.

The concurrency tests avoid relying only on elapsed wall-clock time.

One deterministic test places both workers behind an injected barrier and requires both to enter the active region before either may continue. A sequential implementation cannot satisfy that contract.

A separate test configures a one-slot bound and verifies that observed active concurrency never exceeds one.

## Runtime context

Graph state contains serializable execution values such as:

* task;
* phase;
* accepted plan;
* current assignment;
* accumulated worker results;
* final decision.

Operational services are injected through `RuntimeContext`, including:

* provider;
* tool registry;
* event recorder;
* synchronization locks;
* semaphore;
* concurrency probe;
* accepted-state recorder.

This keeps resource-bearing runtime objects outside graph state and leaves state focused on portable execution values.

## Accepted-state recorder

A run-scoped accepted-state recorder tracks values that have passed their live validation boundary.

It records:

* the accepted plan;
* completed worker results;
* the accepted final decision.

If graph execution exits through cancellation or an unexpected exception, artifact assembly can use this snapshot rather than reverting to the initial graph input.

This is particularly important when one worker has already completed while another is interrupted.

## Runtime phases

The normal successful path is:

```text
initialized
    -> planning
    -> executing_workers
    -> finalizing
    -> succeeded
```

The transition model also supports `failed` and `cancelled` terminal states.

One pure transition predicate is shared by orchestration and artifact validation.

The event stream therefore records the same state-machine contract that validation later checks.

## Model-provider boundary

`ModelProvider` is asynchronous and provider-independent.

Before the first provider attempt for a logical turn, the runtime persists a `ModelRequestRecord` containing:

* logical turn;
* source;
* phase;
* fixture key;
* structured payload;
* creation timestamp;
* canonical payload SHA-256.

Retries reference that same logical request.

The provider returns `ModelResponse.raw_json`.

The runtime then parses that response into the expected public type:

* `SupervisorPlan`;
* `WorkerToolRequest`;
* `FinalDecision`.

For the deterministic fixture adapter, the structured request is the complete provider input.

A hosted-provider adapter can preserve this semantic request boundary while adding a separate redacted wire-level representation for provider-specific transport data.

## Per-run provider isolation

`LoadedInputs` retains immutable fixture construction data.

Each call to `execute_loaded()` constructs a fresh `FixtureModelProvider`.

This ensures that provider state such as scripted transient failures belongs to one execution rather than leaking into the next execution.

Independent runs can therefore reproduce the same logical attempt behavior and semantic result.

## Invocation and retry ownership

One logical model turn corresponds to one durable request.

Each provider try is a model attempt.

One accepted logical tool operation corresponds to one tool call.

Each execution try is a tool attempt.

Every attempt records information such as:

* source;
* phase;
* attempt number;
* start and completion time;
* monotonic duration;
* outcome;
* response or output;
* failure reference;
* trace context.

The common invocation layer applies an independent `asyncio.timeout` to each attempt.

Retryable execution failures can follow the configured retry policy and exponential backoff.

Validation and policy failures remain outside that retry category so malformed or semantically invalid output is not treated as transient infrastructure behavior.

The graph execution itself remains a single runtime execution; model and tool retry accounting stays inside that run.

## Cancellation model

`asyncio.CancelledError` remains caller-visible control flow.

The runtime groups small related in-memory evidence updates into bounded cancellation-safe units so records do not become internally inconsistent at important state boundaries.

Examples include:

* run startup and initial transition;
* planning lifecycle entry;
* model/tool terminal evidence;
* accepted-plan dispatch;
* accepted worker results;
* finalization entry;
* terminal failure evidence;
* final success.

If cancellation arrives while one of these small evidence units is being committed, the unit finishes coherently and the original cancellation is then propagated.

Worker cleanup releases semaphore capacity and leaves the active concurrency probe consistently updated.

Artifact assembly uses the latest accepted-state snapshot after cleanup.

## Event stream

The in-memory recorder assigns event sequence numbers under an `asyncio.Lock`.

Sequence numbers are therefore:

* unique;
* contiguous;
* strictly increasing.

Concurrent worker events may interleave differently between independent runs, so the recorded order reflects actual observation order rather than imposing an artificial deterministic worker schedule.

The event vocabulary covers:

* run start;
* phase transitions;
* planning;
* worker dispatch;
* worker lifecycle;
* model attempts;
* tool attempts;
* finalization;
* failures;
* cancellation;
* terminal outcome;
* persistence lifecycle.

Artifact validation reconciles these events against the corresponding durable operation records.

## Time model

UTC timestamps represent wall-clock event time.

Attempt durations use an independent monotonic clock.

Because those clocks serve different purposes, `duration_ms` is required to be nonnegative but is not derived by subtracting UTC timestamps.

This avoids introducing clock-adjustment artifacts into execution-duration evidence.

## Failure-reference graph

Failures receive stable `failure_id` values.

The same identifier connects related evidence across:

* model or tool attempts;
* worker results;
* top-level failure records;
* events;
* terminal state.

Validation checks that those references form a coherent graph and rejects dangling, duplicate, or contradictory relationships.

## Accounting

Accounting distinguishes logical work from physical attempts.

For model activity:

```text
logical model turn -> one ModelRequestRecord
                    -> one or more model attempts
```

For tools:

```text
logical tool call -> one accepted tool operation
                  -> one or more tool attempts
```

The runtime derives its accounting from operation records.

Artifact validation independently derives the same values and requires the persisted accounting summary to match.

## Artifact validation

Artifact schema `0.3.0` uses strict models and a closed event vocabulary.

Validation operates across the full execution record rather than validating records independently.

Important relationships include:

```text
request
  -> attempt
      -> provider response
          -> accepted plan / tool request / decision

worker assignment
  -> logical tool call
      -> tool attempts
          -> successful tool output
              -> WorkerResult

failure
  -> attempt / worker / event references
```

Validation also checks:

* request and attempt identity;
* contiguous attempt numbering;
* effective retry limits;
* operation timing;
* lifecycle ordering;
* transition chains;
* tool-result identity;
* accepted-state consistency;
* failure references;
* accounting reconciliation;
* task-bound semantic correctness.

The same pure semantic functions used during execution are reapplied to persisted evidence.

## Integrity fingerprints

Artifacts contain three complementary fingerprints.

### Content fingerprint

`content_sha256` hashes canonical JSON with that field omitted.

### Configuration fingerprint

Binds the stable task, effective configuration, and input digests used to construct the run.

### Semantic fingerprint

Binds stable request causality and normalized execution outcomes while excluding intentionally volatile fields such as:

* generated IDs;
* timestamps;
* measured durations;
* concurrent event interleaving;
* destination-specific context.

This allows two independent deterministic executions to retain truthful runtime-specific details while still producing the same normalized semantic identity.

## Reproducibility

Full artifacts are expected to differ where real execution produces volatile values.

Tests therefore compare a documented stable projection rather than forcing byte-for-byte identity.

Independent deterministic executions must preserve the expected logical attempt pattern and semantic fingerprint.

## Persistence

Final artifact persistence uses:

1. a temporary file in the destination directory;
2. serialization;
3. flush;
4. `fsync`;
5. close;
6. `os.replace`.

If replacement fails, the temporary file is removed so a partial final artifact is not left behind.

The current persistence model collects execution evidence in memory and performs one final atomic artifact write.

## Evaluation boundary

Runtime execution and evaluation are intentionally separate layers.

The runtime owns one execution and its evidence.

An evaluation adapter can consume the final structured output and reference the runtime artifact while leaving scoring, aggregation, regression analysis, and repeated-run policy to the evaluation system.

This prevents evaluation-level retries from being confused with the model and tool retries already represented inside a runtime execution.

## Design trade-offs

The current architecture optimizes for deterministic inspection of orchestration and failure behavior:

* explicit low-level graph structure makes node and edge behavior visible;
* strict typed boundaries make malformed state fail early;
* one generic worker keeps fan-out behavior uniform;
* deterministic fixtures make reliability tests reproducible and inexpensive;
* cross-record validation makes the persisted execution path inspectable rather than treating the artifact as a collection of unrelated records;
* run-scoped dependency injection keeps operational services separate from serializable graph state;
* atomic final persistence keeps the durable artifact internally coherent.

The same boundaries provide extension points for hosted providers, additional tool families, alternative persistence mechanisms, and evaluation adapters.
