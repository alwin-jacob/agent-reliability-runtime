# Agent Reliability Runtime

This repository makes the execution of a small agent system inspectable. A supervisor decomposes one retail return-eligibility request, two bounded concurrent workers retrieve authoritative fixture facts through typed read-only tools, and the supervisor produces a structured decision only when both results are present. The runtime preserves state transitions, every provider/tool attempt, failures, events, usage, cost, and provenance in a validated local artifact.

It owns agent execution, not generic evaluation. `llm-eval-reliability` remains a separate repository and was not changed for this milestone.

## Implemented today

Stage 0 and the deterministic Stage 1 vertical slice are implemented:

- Python 3.11+ package and `agent-runtime` CLI;
- low-level LangGraph `StateGraph` pinned to `langgraph==1.2.11`;
- supervisor planning, dynamic `Send` fan-out, one generic worker node, reducer accumulation, and supervisor finalization;
- exactly two Stage 1 workers with an explicit `asyncio.Semaphore` bound;
- strict, versioned Pydantic v2 domain models and transition validation;
- one asynchronous scripted fixture provider that returns raw JSON;
- typed asynchronous `lookup_order` and `lookup_return_policy` fixture tools;
- shared per-attempt timeout, classified retry, exponential backoff, and cancellation semantics;
- monotonic structured events, attempt evidence, logical-operation accounting, zero-cost fixture accounting, content/configuration/semantic fingerprints, atomic JSON persistence, JSON Schema, and semantic validation;
- deterministic offline tests and a Python 3.11/3.12/3.13 CI configuration; and
- one checked-in successful artifact tied to a clean local implementation commit.

The fixture provider and tools do not use the network, filesystem at invocation time, credentials, paid resources, or external model processes.

## Deterministic quickstart

```sh
uv sync --frozen --extra dev

uv run agent-runtime run \
  --task examples/tasks/retail-return-v1.json \
  --config examples/configs/deterministic-v1.json \
  --output /tmp/retail-return-v1.run.json

uv run agent-runtime validate-artifact \
  /tmp/retail-return-v1.run.json

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

The planning fixture must produce exactly `order-worker` (only `lookup_order`) and `policy-worker` (only `lookup_return_policy`). Live providers, registries, locks, semaphores, recorders, and file handles are injected through LangGraph runtime context and never stored in graph state.

## Failures, retries, and cancellation

One logical model turn or tool call can contain multiple attempts. Each attempt receives an independent timeout and its own event/record. Only typed retryable provider/tool failures, timeouts, and explicitly typed transient infrastructure failures can retry. Malformed or schema-invalid model output, unknown/disallowed tools, tool input/output schema violations, policy failures, and cancellation do not retry. Backoff is exponential; Stage 1 fixes jitter to zero.

Ordinary worker failure becomes typed evidence and prevents a successful final answer. `CancelledError` remains control flow: cancellation reaches active work, cleanup records cancellation evidence and zero active workers, a cancelled artifact is written when possible, and cancellation is re-raised.

CLI exits are 0 for a successful run or valid artifact, 1 for configuration/artifact/setup/unexpected errors, 2 for argparse usage, 3 for a normalized failed run, and 130 for mapped keyboard interruption.

## Artifact validation

Schema `0.1.0` artifacts forbid unknown fields and contain the task, effective config, final state/decision, full event and attempt evidence, tool calls/results, failures, accounting, nonidentifying provenance, input digests, and three integrity fingerprints. `content_sha256` covers canonical JSON with that field omitted. Files are written through a destination-directory temporary file, flush, `fsync`, and `os.replace`.

Validation recomputes content/configuration/semantic fingerprints and rejects invalid schema versions, unknown fields, broken event/transition chains, missing required lifecycle or attempt events, inconsistent accounting/outcome evidence, invalid tool-result references, status/decision/failure contradictions, private paths, and obvious secret-like values. `scripts/check_schema_sync.py` ensures the checked schema equals Pydantic generation. `scripts/verify_example.py` validates the checked artifact, reruns the fixtures, checks the exact decision/invariants, and compares stable fingerprints.

## Evaluation integration

Runtime artifacts are not evaluation artifacts. A future local adapter can implement `Candidate.generate()` for `llm-eval-reliability`, put the final structured decision in `CandidateResponse.output`, and put a portable runtime-artifact reference in metadata. Its evaluation-engine outer candidate `max_attempts` should default to 1 because this runtime already owns internal model/tool retries. No evaluation-repository change is needed for deterministic final-output evaluation in Stage 1.

## Explicitly not implemented

This milestone does not implement a LangGraph checkpointer, process restart/resume, human decision or interrupt/approval, MCP, Docker or another sandbox, network/filesystem execution tools, RAG/embeddings/reranking/vector storage, Langfuse, LangSmith project observability, FastAPI, a real model provider, provider fallback, benchmark execution or submitted benchmark results, repeated-sampling reliability, judge calibration/annotator agreement, regression-threshold derivation, or significance claims. Transitive packages are not claims of configured features.

## Limitations and next stage

Runs are single-process and keep evidence in memory until one final atomic write, so a process crash loses in-progress work. There is no checkpointer or resume path. Fixture behavior proves orchestration and failure semantics, not real-provider quality or deployment reliability. Local Python is 3.14.3, outside the configured 3.11-3.13 CI matrix; an upstream LangChain compatibility warning appears on direct CLI startup under 3.14 but does not affect stdout or the passing local suite. Remote CI has not been run. Public-release licensing remains unresolved and no LICENSE is included.

The next planned stage is a separately scoped checkpoint/resume and human-interrupt design audit; it is not part of this implementation.
