# Stage 0 and Stage 1 specification

## Mission

Build a local-only Python 3.11+ agent runtime that owns execution reliability rather than generic evaluation. Stage 0 establishes repository, package, tooling, safety, documentation, and evidence foundations. Stage 1 implements exactly one deterministic retail return-eligibility vertical slice.

## Required Stage 1 behavior

The runtime uses a low-level LangGraph `StateGraph`: a supervisor planning node dynamically fans out two assignments with `Send`, a generic worker node runs once per assignment under explicit bounded concurrency, a reducer accumulates typed worker results, and a supervisor finalization node requires both results. The workers are `order-worker`, allowed only `lookup_order`, and `policy-worker`, allowed only `lookup_return_policy`.

The fixture model boundary is asynchronous and returns raw versioned JSON. Model and tool calls share one invocation policy with independent per-attempt timeouts, classified retries, exponential backoff, deterministic zero jitter in fixtures, attempt evidence, and cancellation propagation. Malformed/schema-invalid model output and tool policy/schema failures are not retried.

Strict Pydantic v2 models forbid extra fields. Runtime phases are `initialized -> planning -> executing_workers -> finalizing -> succeeded`, with typed failure and cancellation transitions. Events receive concurrency-safe, strictly increasing sequence numbers. Accounting distinguishes logical model turns from model attempts and logical tool calls from tool attempts.

Version `0.1.0` artifacts contain task/config/state/decision, complete traces and attempts, failures, accounting, nonidentifying provenance, source digests, a stable semantic fingerprint, and a content hash over canonical JSON with the content hash omitted. Writes are atomic and validation rejects structural, semantic, accounting, integrity, private-path, and secret-like violations.

## Boundaries

Stage 1 has no paid or real model provider, provider fallback, network or filesystem execution tool, checkpointing, process resume, human decision/interrupt, MCP, container sandbox, RAG, vector storage, Langfuse/LangSmith project observability, API server, benchmark suite, repeated sampling, calibration study, or cross-repository change. Planned features remain documentation only. Public-release licensing is unresolved and no LICENSE is created in this stage.

## Acceptance evidence

Offline deterministic tests must cover domain strictness, transitions, successful and failed orchestration, actual bounded overlap, retries/timeouts, malformed output, policy/schema failures, cancellation, traces, accounting, artifact integrity and atomicity, CLI exit behavior, reproducible semantic fingerprints, schema synchronization, and the checked-in successful example. Ruff, Ruff formatting, strict mypy, pytest, schema synchronization, CLI run/validation, semantic example verification, Git diff checks, and empty remote/status checks are required before final reporting.
