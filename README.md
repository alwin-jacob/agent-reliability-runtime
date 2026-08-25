# Agent Reliability Runtime

This repository makes the execution of a small agent system inspectable. A supervisor decomposes one retail return-eligibility request, two bounded concurrent workers retrieve authoritative fixture facts through typed read-only tools, and the supervisor produces a structured decision only when both results are present. The runtime preserves state transitions, every provider/tool attempt, failures, events, usage, cost, and provenance in a validated local artifact.

It owns agent execution, not generic evaluation. `llm-eval-reliability` remains a separate repository and was not changed for this milestone.

## Implemented today

Stage 0 and the deterministic Stage 1 vertical slice are implemented:

- Python 3.11+ package and `agent-runtime` CLI;
- low-level LangGraph `StateGraph` pinned to `langgraph==1.2.11`;
- supervisor planning, dynamic `Send` fan-out, one generic worker node, reducer accumulation, and supervisor finalization;
- exactly two Stage 1 workers with an explicit `asyncio.Semaphore` bound;
- strict Pydantic v2 package version `0.3.2` and durable artifact schema `0.3.0`, while
  unchanged task, config, and fixture schemas remain independently versioned `0.2.0`;
- one asynchronous scripted fixture provider that returns raw JSON;
- typed asynchronous `lookup_order` and `lookup_return_policy` fixture tools;
- shared per-attempt timeout, classified retry, exponential backoff, cancellation semantics,
  and validation of the effective model/tool retry policies preserved in each artifact;
- one pure semantic contract shared by live orchestration and artifact validation, binding
  worker calls, authoritative order/policy evidence, calendar-day derivation, and the final decision;
- durable hashed internal structured model requests, provider responses, response-to-action
  reconciliation, stable failure IDs, exact attempt-event evidence, and accepted partial state;
- logical-operation accounting, zero-cost fixture accounting, content/configuration/semantic
  fingerprints, atomic JSON persistence, JSON Schema, and cross-record validation;
- deterministic offline tests and a Python 3.11/3.12/3.13 CI configuration; and
- one checked-in successful artifact tied to a clean local implementation commit.

The fixture provider and tools do not use the network, filesystem at invocation time, credentials, paid resources, or external model processes.

## Deterministic quickstart

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

`run` prints one compact JSON object. A successful fixture run reports four logical model turns, four model attempts, two logical tool calls, two tool attempts, `external_model_calls=false`, and `cost_usd=0.0`. IDs, timestamps, durations, and observed concurrent event order vary between runs; the semantic fingerprint does not.

## Graph shape

```text
START
  -> supervisor_plan
       -> Send(order-worker)  ----\
       -> Send(policy-worker) ----+-> reducer(worker_results)
                                   -> supervisor_finalize
                                   -> END
```

The planning fixture must produce exactly `order-worker` (only `lookup_order`) and `policy-worker` (only `lookup_return_policy`). The order worker must request exactly the task order ID. The policy worker must request exactly the task market, category, and purchase channel. The finalizer independently requires delivered order evidence, exact task/order/policy context equality, the policy-required item condition, and an explicit task `as_of_date`. It derives whole calendar days as `as_of_date - delivery_date`; the inclusive result is eligible when `days_since_delivery <= return_window_days`, with canonical code `RETURN_ELIGIBLE`, otherwise `RETURN_INELIGIBLE`.

Live providers, registries, locks, semaphores, recorders, and file handles are injected through LangGraph runtime context and never stored in graph state.

## Failures, retries, and cancellation

One logical model turn or tool call can contain multiple attempts. Each attempt receives an independent timeout and its own event/record. Attempt origin, outcome, exception boundary, timeout code, and retryability are validated against the fixture runtime: model attempts contain only provider, cancellation, untyped-unexpected, or explicitly typed transient-infrastructure failures; nested tool attempts contain only execution/output, cancellation, untyped-unexpected, or explicitly typed transient-infrastructure failures. Only typed retryable provider/tool-execution failures, timeouts, and explicitly typed transient infrastructure failures can retry. Model-output, tool-policy, and tool-input failures occur outside nested attempts; tool-output failures are nonretryable. Backoff is exponential; Stage 1 fixes jitter to zero.

Ordinary worker failure becomes typed evidence and prevents a successful final answer. Every failure receives a stable `failure_id`; nested attempt/worker copies, top-level records, and failure events must form one coherent reference graph. `CancelledError` remains control flow. Small in-memory evidence commits are shielded just long enough to append their complete record/event/state unit; this includes run start plus initial planning transition, planning-lifecycle entry, finalization entry, and both classified and unexpected failed-terminal evidence. If outer cancellation arrives, the bounded commit finishes and the original cancellation is then re-raised. Cancellation after startup but before planning work persists an exact empty-work cancelled shape rather than fabricating a planning event or model request. Cancellation reaches active work, cleanup records coherent cancellation evidence and zero active workers, and a cancelled artifact preserves the already accepted plan and completed worker results. Once a terminal success or failure unit is committed, cancellation-safe assembly persists that terminal artifact; committed success never gains cancellation evidence, and the caller's cancellation remains control flow. If terminal-artifact persistence fails, a sanitized secondary note is attached and the original `CancelledError` remains externally observable. This is not durable checkpointing: a process crash can still lose every in-memory record.

CLI exits are 0 for a successful run or valid artifact, 1 for configuration/artifact/setup/unexpected errors, 2 for argparse usage, 3 for a normalized failed run, and 130 for mapped keyboard interruption.

## Artifact validation

Schema `0.3.0` artifacts forbid unknown fields and contain the task, effective config, accepted final state/decision, durable model requests, full event and attempt evidence, tool calls/results, failures, accounting, nonidentifying provenance, input digests, and three integrity fingerprints. `content_sha256` covers canonical JSON with that field omitted. Files are written through a destination-directory temporary file, flush, `fsync`, and `os.replace`. Artifact schemas `0.1.0` and `0.2.0` remain checked in unchanged as historical evidence; task, run-config, and fixture shapes remain schema `0.2.0`.

Validation recomputes request-payload digests and content/configuration/semantic fingerprints. It enforces the effective model and tool retry limits, the exact origin/outcome/exception/retry taxonomy at the fixture invocation boundaries, and deterministic response-to-failure causality. Malformed JSON, public-schema failure, and semantic-contract failure records must follow the successful provider response they classify and retain the request lifecycle context. Planner, worker, and finalizer requests cannot predate their respective lifecycle-start event and still cannot follow their first attempt start. Every successful tool attempt is parsed through its strict output schema even when the run ultimately failed or was cancelled, and accepted worker output must equal the terminal successful tool output. Successful fixture responses must retain zero cost, scripted finish reason, synthetic-or-absent token attribution, request-matching fixture metadata, and one consistent fixture model ID; these checks establish internal fixture-contract consistency, not provider authenticity.

It also rejects missing or extra successful turns; attempts without requests; terminal responses that contradict the accepted plan, tool calls, decision, or persisted deterministic failure; orphaned, duplicate, or contradictory attempt events; dangling failure references; inconsistent accounting; unknown event types; timestamps outside the inclusive run interval; lifecycle/attempt span collisions; broken lifecycle/transition chains; private paths; and obvious secret-like values. Event order is checked against operation records, while nonnegative `duration_ms` is intentionally measured on an independent monotonic clock and need not equal UTC wall-clock subtraction. The same pure plan, tool-request, and decision validators used live are applied to persisted evidence. `scripts/check_schema_sync.py` targets artifact schema 0.3.0, and `scripts/verify_example.py` reproduces the exact four-request causal contract and stable fingerprints.

The content and semantic hashes detect unsealed or accidental modification; they are not digital signatures. A writer with modification access can recompute them. Source commit, lock digest, and other recorded provenance require the corresponding repository context before they can support an authenticity claim. Semantic validation, provenance, repository history, and later release controls are separate evidence layers. No authenticity claim is made from recomputed hashes alone.

Each logical model turn persists one `ModelRequestRecord` before its first attempt. Its `payload_sha256` is SHA-256 over the canonical JSON bytes of the exact persisted payload, and retries reference the same request. This internal structured request is not chain-of-thought capture. For the fixture adapter it is the complete provider input. A later real-provider adapter may add a provider-wire envelope or request that requires separate, redacted provenance. `LoadedInputs` stores immutable fixture construction bytes and creates a fresh provider per execution; two isolated executions reproduce the same attempt pattern and semantic fingerprint. This is run isolation, not repeated-sampling analysis.

## Evaluation integration

Runtime artifacts are not evaluation artifacts. A future local adapter can implement `Candidate.generate()` for `llm-eval-reliability`, put the final structured decision in `CandidateResponse.output`, and put a portable runtime-artifact reference in metadata. Its evaluation-engine outer candidate `max_attempts` should default to 1 because this runtime already owns internal model/tool retries. No evaluation-repository change is needed for deterministic final-output evaluation in Stage 1.

## Explicitly not implemented

This milestone does not implement a LangGraph checkpointer, process restart/resume, human decision or interrupt/approval, MCP, Docker or another sandbox, network/filesystem execution tools, RAG/embeddings/reranking/vector storage, Langfuse, LangSmith project observability, FastAPI, a real model provider, provider fallback, benchmark execution or submitted benchmark results, repeated-sampling reliability, judge calibration/annotator agreement, regression-threshold derivation, or significance claims. Transitive packages are not claims of configured features.

## Limitations and next stage

Runs are single-process and keep evidence in memory until one final atomic write, so a process crash loses in-progress work. There is no checkpointer or resume path. Fixture behavior proves orchestration and failure semantics, not real-provider quality or deployment reliability. Authoritative local verification uses Python 3.13.15; the workflow also configures 3.11 and 3.12, but those versions and remote CI were not run in this corrective milestone. Public-release licensing remains unresolved and no LICENSE is included.

No general trajectory-replay claim is made because no adapter or replay command exists. The next exact blocker for real-provider trajectory evidence is a separately authorized adapter contract that distinguishes the internal structured request from any provider-wire envelope, defines redaction, and implements replay semantics. Repeated-sampling orchestration and analysis also remain unimplemented despite correct per-run provider isolation. Checkpoint/resume and human-interrupt design remain separately scoped Stage 2 work.
