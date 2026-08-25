# Canonical project state

## Scope

This local repository owns agent runtime execution and runtime evidence. Generic evaluation remains in the separate read-only-for-this-action `llm-eval-reliability` repository.

## Current evidence

The implementation uses a low-level LangGraph supervisor/two-worker graph, strict Pydantic v2 schema 0.2.0 models, explicit task decision context, one deterministic raw-JSON fixture provider, two typed in-memory retail tools, one pure live/replay semantic validator, a shared invocation policy, bounded worker concurrency, typed failures, cancellation preservation, structured events, exact accounting, cross-record artifact validation, atomic persistence, validation CLI, generated JSON Schema, offline tests, and a configured Python 3.11-3.13 CI matrix.

The current checked successful artifact uses schema 0.2.0 and validates/reproduces its exact final decision and semantic/configuration fingerprints. The prior schema 0.1.0 remains historical evidence. A later independent read-only source audit, distinct from the earlier implementation-agent review, confirmed the semantic and artifact-integrity defects corrected in this milestone; the audit agent changed no files.

Public-release licensing is unresolved; no LICENSE is present. Authoritative local verification ran on Python 3.13.15. Python 3.11/3.12 and remote CI did not run in this corrective milestone.

## Status fields

- As-of date: 2026-08-25
- Current stage: Stage 0 foundation and semantically hardened deterministic Stage 1 vertical slice complete
- Implemented features: low-level supervisor/worker LangGraph, two-worker bounded concurrency, typed decision context and evidence binding, fixture provider, typed fixture tools, classified retry/timeout/cancellation, events/accounting, validated atomic schema 0.2.0 artifact, CLI, offline tests, and CI configuration
- Exact local test status: 115 passed on Python 3.13.15; Ruff check passed; Ruff format check passed; strict mypy passed; schema sync and deterministic example verification passed
- Claim/evidence status: only Stage 1 local-runtime claims in `docs/claim-evidence.md` are implemented; remote CI and all later-stage/measurement claims remain unsupported
- Blockers: exact request persistence or request digests before trajectory replay/real-provider evaluation; per-run provider construction before repeated sampling
- Paid-resource use: none
- Visibility: local-only
- Git remote status: none
- Last verified local commit: current evidence `HEAD`; checked artifact records its clean implementation source commit
- Unresolved risks: no exact model-request replay, stateful one-execution loaded provider, no crash recovery/checkpointer, no process resume, one-process in-memory evidence, fixture-only behavior, no Python 3.11/3.12 or remote CI execution evidence, and unresolved public-release license
- Next stage: first decide durable model-request payloads versus request digests for replay/evaluation and per-run provider construction for sampling; checkpoint/resume and human-interrupt design remain separate Stage 2 scope
