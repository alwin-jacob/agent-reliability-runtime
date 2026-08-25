# Stage 0 and Stage 1 specification

## Mission

Build a local-only Python 3.11+ agent runtime that owns execution reliability rather than generic evaluation. Stage 0 establishes repository, package, tooling, safety, documentation, and evidence foundations. Stage 1 implements exactly one deterministic retail return-eligibility vertical slice.

## Required Stage 1 behavior

The runtime uses a low-level LangGraph `StateGraph`: a supervisor planning node dynamically fans out two assignments with `Send`, a generic worker node runs once per assignment under explicit bounded concurrency, a reducer accumulates typed worker results, and a supervisor finalization node requires both results. The workers are `order-worker`, allowed only `lookup_order`, and `policy-worker`, allowed only `lookup_return_policy`. Their requests must exactly match the typed task order ID or task market/category/channel context.

The task requires an exact valid-calendar `as_of_date`, item condition, market, item category, and purchase channel. One LangGraph-independent semantic module validates the exact plan, worker evidence, and final decision both during execution and during artifact replay. A successful decision requires delivered authoritative order evidence; equal task/order/policy context; the required item condition; a decision date on or after delivery; deterministic whole calendar days; inclusive policy-window derivation; matching eligibility, canonical code, window, fees, and order ID; and exactly both worker IDs. Structured factual fields are validated; free-form reason and next-action prose are retained without word-level interpretation.

The fixture model boundary is asynchronous and returns raw versioned JSON. Model and tool calls share one invocation policy with independent per-attempt timeouts, classified retries, exponential backoff, deterministic zero jitter in fixtures, attempt evidence, and cancellation propagation. Malformed/schema-invalid model output and tool policy/schema failures are not retried.

Strict Pydantic v2 models forbid extra fields. Runtime phases are `initialized -> planning -> executing_workers -> finalizing -> succeeded`, with typed failure and cancellation transitions. Events receive concurrency-safe, strictly increasing sequence numbers. Accounting distinguishes logical model turns from model attempts and logical tool calls from tool attempts.

Version `0.2.0` artifacts contain task/config/state/decision, persisted events, model-attempt/provider-response evidence, tool-call/result evidence, failures, accounting, nonidentifying provenance, source digests, a stable semantic fingerprint, and a content hash over canonical JSON with the content hash omitted. Version `0.1.0` remains historical schema evidence. Writes are atomic and validation rejects structural, cross-record semantic, accounting, identity, timestamp, integrity, private-path, and secret-like violations. Content and semantic hashes detect unsealed or accidental modification but are not signatures.

Model attempts and provider responses are persisted. Exact model-request payload replay is not implemented; request persistence or request digests are required before trajectory replay or real-provider evaluation. A loaded fixture provider is stateful and intended for one execution, so per-run construction is required before repeated sampling.

## Boundaries

Stage 1 has no paid or real model provider, provider fallback, network or filesystem execution tool, checkpointing, process resume, human decision/interrupt, MCP, container sandbox, RAG, vector storage, Langfuse/LangSmith project observability, API server, benchmark suite, repeated sampling, calibration study, or cross-repository change. Planned features remain documentation only. Public-release licensing is unresolved and no LICENSE is created in this stage.

## Acceptance evidence

Offline deterministic tests must cover domain strictness, transitions, successful and failed orchestration, actual bounded overlap, retries/timeouts, malformed output, policy/schema failures, cancellation, traces, accounting, artifact integrity and atomicity, CLI exit behavior, reproducible semantic fingerprints, schema synchronization, and the checked-in successful example. Ruff, Ruff formatting, strict mypy, pytest, schema synchronization, CLI run/validation, semantic example verification, Git diff checks, and empty remote/status checks are required before final reporting.
