# Agent Reliability Runtime

This repository makes the execution of a small agent system inspectable. A supervisor decomposes one retail return-eligibility request, two bounded concurrent workers retrieve authoritative fixture facts through typed read-only tools, and the supervisor produces a structured decision only when both results are present. The runtime preserves state transitions, every provider/tool attempt, failures, events, usage, cost, and provenance in a validated local artifact.

It owns agent execution, not generic evaluation. `llm-eval-reliability` remains a separate repository and was not changed for this milestone.

## Implemented today

Stage 0 and the deterministic Stage 1 vertical slice are implemented:

- Python 3.11+ package and `agent-runtime` CLI;
- low-level LangGraph `StateGraph` pinned to `langgraph==1.2.11`;
- supervisor planning, dynamic `Send` fan-out, one generic worker node, reducer accumulation, and supervisor finalization;
- exactly two Stage 1 workers with an explicit `asyncio.Semaphore` bound;
- strict Pydantic v2 task/config/artifact schema `0.2.0`, including a fixed decision date,
  item condition, market, category, and purchase channel;
- one asynchronous scripted fixture provider that returns raw JSON;
- typed asynchronous `lookup_order` and `lookup_return_policy` fixture tools;
- shared per-attempt timeout, classified retry, exponential backoff, and cancellation semantics;
- one pure semantic contract shared by live orchestration and artifact validation, binding
  worker calls, authoritative order/policy evidence, calendar-day derivation, and the final decision;
- monotonic structured events, attempt evidence, logical-operation accounting, zero-cost fixture accounting, content/configuration/semantic fingerprints, atomic JSON persistence, JSON Schema, and cross-record validation;
- deterministic offline tests and a Python 3.11/3.12/3.13 CI configuration; and
- one checked-in successful artifact tied to a clean local implementation commit.

The fixture provider and tools do not use the network, filesystem at invocation time, credentials, paid resources, or external model processes.

## Deterministic quickstart

```sh
uv sync --python 3.13 --frozen --extra dev

uv run agent-runtime run \
  --task examples/tasks/retail-return-v1.json \
  --config examples/configs/deterministic-v1.json \
  --output /tmp/retail-return-v2.run.json

uv run agent-runtime validate-artifact \
  /tmp/retail-return-v2.run.json

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

One logical model turn or tool call can contain multiple attempts. Each attempt receives an independent timeout and its own event/record. Only typed retryable provider/tool failures, timeouts, and explicitly typed transient infrastructure failures can retry. Malformed or schema-invalid model output, unknown/disallowed tools, tool input/output schema violations, policy failures, and cancellation do not retry. Backoff is exponential; Stage 1 fixes jitter to zero.

Ordinary worker failure becomes typed evidence and prevents a successful final answer. `CancelledError` remains control flow: cancellation reaches active work, cleanup records cancellation evidence and zero active workers, a cancelled artifact is written when possible, and cancellation is re-raised. If cancelled-artifact persistence fails, a sanitized secondary note is attached and the original `CancelledError` still remains externally observable.

CLI exits are 0 for a successful run or valid artifact, 1 for configuration/artifact/setup/unexpected errors, 2 for argparse usage, 3 for a normalized failed run, and 130 for mapped keyboard interruption.

## Artifact validation

Schema `0.2.0` artifacts forbid unknown fields and contain the task, effective config, final state/decision, full event and attempt evidence, tool calls/results, failures, accounting, nonidentifying provenance, input digests, and three integrity fingerprints. `content_sha256` covers canonical JSON with that field omitted. Files are written through a destination-directory temporary file, flush, `fsync`, and `os.replace`. Schema `0.1.0` remains checked in only as historical schema evidence; the current quickstart and checked artifact use `0.2.0`.

Validation recomputes content/configuration/semantic fingerprints and rejects invalid schema versions, unknown fields, broken event/transition chains, missing required lifecycle or attempt events, inconsistent accounting/outcome evidence, cross-record task/decision/worker contradictions, invalid plan or tool identities, duplicate durable IDs, noncontiguous attempts, non-UTC or reversed timestamps, private paths, and obvious secret-like values. The same pure decision validator used live is applied to successful persisted evidence. `scripts/check_schema_sync.py` ensures the checked current schema equals Pydantic generation. `scripts/verify_example.py` validates the checked artifact, reruns the fixtures, checks the exact decision/invariants, and compares stable fingerprints.

The content and semantic hashes detect unsealed or accidental modification; they are not digital signatures. A writer with modification access can recompute them. Semantic validation, provenance, repository history, and later release controls are separate evidence layers. No authenticity claim is made from recomputed hashes alone.

Model attempts and provider responses are persisted. Exact `ModelRequest` payload persistence and replay are not implemented. Request persistence or request digests must be designed before trajectory replay or real-provider evaluation. `LoadedInputs` currently owns a stateful scripted fixture provider and is intended for one execution; per-run provider construction is required before repeated-sampling work.

## Evaluation integration

Runtime artifacts are not evaluation artifacts. A future local adapter can implement `Candidate.generate()` for `llm-eval-reliability`, put the final structured decision in `CandidateResponse.output`, and put a portable runtime-artifact reference in metadata. Its evaluation-engine outer candidate `max_attempts` should default to 1 because this runtime already owns internal model/tool retries. No evaluation-repository change is needed for deterministic final-output evaluation in Stage 1.

## Explicitly not implemented

This milestone does not implement a LangGraph checkpointer, process restart/resume, human decision or interrupt/approval, MCP, Docker or another sandbox, network/filesystem execution tools, RAG/embeddings/reranking/vector storage, Langfuse, LangSmith project observability, FastAPI, a real model provider, provider fallback, benchmark execution or submitted benchmark results, repeated-sampling reliability, judge calibration/annotator agreement, regression-threshold derivation, or significance claims. Transitive packages are not claims of configured features.

## Limitations and next stage

Runs are single-process and keep evidence in memory until one final atomic write, so a process crash loses in-progress work. There is no checkpointer or resume path. Fixture behavior proves orchestration and failure semantics, not real-provider quality or deployment reliability. Authoritative local verification uses Python 3.13.15; the workflow also configures 3.11 and 3.12, but those versions and remote CI were not run in this corrective milestone. Public-release licensing remains unresolved and no LICENSE is included.

The exact blocker before trajectory replay or real-provider evaluation is a decision and implementation for durable model-request payloads or request digests. Per-run provider construction is separately required before repeated sampling. Checkpoint/resume and human-interrupt design remain separately scoped Stage 2 work and are not part of this implementation.
