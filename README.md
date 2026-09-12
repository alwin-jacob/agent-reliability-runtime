# Agent Reliability Runtime

A deterministic, failure-aware runtime for small agent systems built with LangGraph.

The repository focuses on making agent execution inspectable: supervisor planning, concurrent worker execution, typed tool calls, retries and timeouts, cancellation behavior, structured failures, accounting, provenance, and validated run artifacts.

The current implementation uses a retail return-eligibility workflow as a deterministic vertical slice. A supervisor decomposes the request, two bounded concurrent workers retrieve authoritative order and policy facts through typed read-only tools, and the supervisor produces a structured decision once both results are available.

## What it demonstrates

- low-level LangGraph orchestration with explicit graph structure;
- supervisor planning and dynamic `Send` fan-out;
- concurrent worker execution with an explicit concurrency bound;
- typed asynchronous tools with strict input and output validation;
- per-attempt timeout, retry, backoff, and failure classification;
- cancellation-safe evidence recording;
- deterministic response-to-action reconciliation;
- structured event, request, attempt, tool, failure, usage, and provenance records;
- semantic validation shared between live execution and persisted artifacts;
- atomic artifact persistence with content, configuration, and semantic fingerprints;
- reproducible offline execution and CI across Python 3.11, 3.12, and 3.13.

The runtime is deliberately structured so orchestration behavior and failure semantics can be inspected independently of a hosted model provider.

## Architecture

```text
START
  |
  v
supervisor_plan
  |
  +--> Send(order-worker)  -------\
  |                                \
  +--> Send(policy-worker) ---------> reducer(worker_results)
                                      |
                                      v
                               supervisor_finalize
                                      |
                                      v
                                     END
```

The graph uses one generic worker node. Worker behavior is determined by the planned worker specification and its allowed tool.

For the deterministic example:

- `order-worker` may call only `lookup_order`;
- `policy-worker` may call only `lookup_return_policy`;
- both workers execute under a shared concurrency bound;
- the reducer accumulates validated worker results;
- the finalizer independently validates the evidence before producing the decision.

Runtime dependencies such as providers, registries, semaphores, recorders, and file handles are injected through LangGraph runtime context rather than stored in graph state.

## Quickstart

```sh
uv sync --python 3.13 --frozen --extra dev

uv run agent-runtime run \
  --task examples/tasks/retail-return-v1.json \
  --config examples/configs/deterministic-v1.json \
  --output /tmp/retail-return-v3.run.json

uv run agent-runtime validate-artifact \
  /tmp/retail-return-v3.run.json

uv run python scripts/verify_example.py
```

A successful deterministic run produces a validated JSON artifact containing the complete runtime evidence for the execution.

## Supervisor and worker contract

The planning step produces exactly two worker assignments for the example workflow.

The order worker must request the task's order ID. The policy worker must request the task's market, item category, and purchase channel.

The finalizer requires:

- delivered-order evidence;
- exact task/order/policy context agreement;
- the policy-required item condition;
- an explicit task `as_of_date`;
- both required worker results.

Eligibility is derived from whole calendar days between delivery and `as_of_date`. The inclusive decision rule is:

```text
days_since_delivery <= return_window_days
```

The canonical decision code is `RETURN_ELIGIBLE` when the rule holds and `RETURN_INELIGIBLE` otherwise.

The same semantic contract is applied during execution and artifact validation.

## Reliability model

Each logical model turn or tool call may contain multiple attempts.

Every attempt receives:

- an independent timeout;
- an attempt record;
- an event record;
- an explicit outcome;
- failure classification when applicable.

Retry behavior is constrained by the effective model and tool policies recorded in the artifact.

Retryable execution failures can be retried with exponential backoff. Validation and policy failures are handled separately from transport or execution failures so malformed output is not treated as transient infrastructure failure.

Failures receive stable identifiers that connect nested attempt evidence, worker results, top-level failure records, and events.

## Cancellation behavior

`asyncio.CancelledError` remains control flow rather than being normalized into an ordinary runtime error.

Small in-memory evidence updates are committed atomically enough to preserve coherent state before cancellation propagates. This includes run startup, planning lifecycle entry, finalization lifecycle entry, and terminal failure evidence.

Cancellation cleanup:

- propagates to active work;
- records coherent cancellation evidence;
- preserves already accepted plan and worker results;
- leaves no active workers after cleanup;
- re-raises the original cancellation to the caller.

If a terminal success or failure has already been committed, artifact assembly preserves that terminal result while cancellation remains externally observable.

## Typed tools

The deterministic runtime includes two asynchronous read-only tools:

- `lookup_order`
- `lookup_return_policy`

Tool inputs and outputs use strict Pydantic models.

Successful outputs are validated even when the enclosing run later fails or is cancelled, and accepted worker output must match the terminal successful tool result.

The example tools are backed by checked-in deterministic fixture data, which keeps the runtime reproducible and allows failure behavior to be tested without external credentials or services.

## Run artifacts

The current durable run-artifact schema is `0.3.0`.

Artifacts include:

- task and effective configuration;
- accepted plan and state;
- final structured decision when available;
- structured model requests;
- provider responses;
- model and tool attempts;
- tool calls and results;
- event sequence;
- failures and failure references;
- accounting;
- provenance;
- input digests;
- integrity fingerprints.

Unknown fields are rejected.

Artifact validation cross-checks the records rather than validating each record independently. It verifies relationships such as:

- request → attempt → response;
- plan → worker call;
- tool call → successful tool output;
- worker result → accepted state;
- final response → final decision;
- failure record → attempt/event references.

This makes the artifact useful for inspecting the execution path rather than merely storing the final output.

## Integrity and reproducibility

Artifacts use three complementary fingerprints:

- content;
- configuration;
- semantics.

`content_sha256` covers canonical JSON with the hash field itself omitted.

The configuration fingerprint captures stable execution configuration, while the semantic fingerprint captures the normalized execution outcome independently of volatile identifiers and timestamps.

Files are persisted through a destination-directory temporary file followed by flush, `fsync`, and `os.replace`.

The fingerprints are intended for reproducibility and modification detection. Repository history and recorded provenance provide the surrounding source context.

## Event and lifecycle validation

The runtime maintains a closed event vocabulary and validates event relationships against durable operation records.

Validation covers:

- event identity and ordering;
- run interval boundaries;
- lifecycle start ordering;
- attempt spans;
- failure references;
- transition chains;
- accounting reconciliation;
- tool/result identity;
- accepted-state consistency.

Durations are measured on a monotonic clock and therefore do not need to equal UTC wall-clock subtraction.

## Deterministic provider

The included scripted provider returns raw JSON and supports deterministic success, failure, delay, malformed-output, and cancellation scenarios.

A successful example run records:

- four logical model turns;
- four model attempts;
- two logical tool calls;
- two tool attempts;
- zero external model calls;
- zero provider cost.

Volatile identifiers, timestamps, durations, and concurrent observation order may differ between executions while the semantic fingerprint remains stable.

## Verification

The repository includes:

- Ruff linting;
- Ruff formatting checks;
- strict mypy;
- 277 deterministic tests;
- generated-schema synchronization;
- deterministic example generation;
- generated and checked artifact validation;
- semantic reproduction checks.

GitHub Actions verifies the project across Python 3.11, 3.12, and 3.13.

A checked successful artifact is included in the repository so the runtime's persisted evidence format can be inspected directly.

## Repository layout

```text
src/
  agent_reliability_runtime/   runtime implementation

tests/                         deterministic and adversarial tests

examples/
  tasks/                       example tasks
  configs/                     runtime configurations
  fixtures/                    deterministic provider/tool data
  artifacts/                   checked execution evidence

schemas/                       versioned JSON Schemas

scripts/                       schema and artifact verification

docs/
  architecture.md              runtime architecture
  specification.md             behavioral contracts
  eval-integration-contract.md evaluation adapter boundary
```

## Evaluation integration

Runtime execution and evaluation remain separate concerns.

A local adapter can expose this runtime through the `Candidate.generate()` interface used by `llm-eval-reliability`, placing the final structured decision in `CandidateResponse.output` and a portable runtime-artifact reference in metadata.

Because this runtime already owns internal model and tool retry behavior, an evaluation layer can keep its outer candidate attempt count independent of the runtime's internal attempt accounting.

## Current scope

The current repository concentrates on deterministic orchestration, runtime reliability semantics, and inspectable execution artifacts.

The checked example uses a local fixture provider so execution remains reproducible without credentials, paid APIs, or external services. Provider adapters, persistent checkpoint/resume, and additional tool environments can be layered on the same runtime boundaries as separate extensions.

## License

MIT. See [`LICENSE`](LICENSE).
