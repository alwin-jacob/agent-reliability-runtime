# Canonical project state

## Scope

This local repository owns agent runtime execution and runtime evidence. Generic evaluation remains in the separate read-only-for-this-action `llm-eval-reliability` repository.

## Current evidence

The implementation uses a low-level LangGraph supervisor/two-worker graph, package and current artifact schema 0.3.0, unchanged independent task/config/fixture schemas 0.2.0, durable hashed internal model requests, deterministic raw-JSON fixture responses, two typed in-memory retail tools, one pure live/artifact semantic validator, bounded concurrency, typed failure references, accepted partial-state preservation, exact event reconciliation, per-run provider isolation, accounting, atomic persistence, CLI, generated JSON Schema, offline tests, and a configured Python 3.11-3.13 CI matrix.

The current checked successful artifact uses schema 0.3.0 and proves the exact four-request response-to-plan/call/decision chain plus semantic/configuration reproduction. Prior artifact schemas 0.1.0 and 0.2.0 remain unchanged historical evidence. The internal structured request is not chain-of-thought; it is the complete fixture-provider input. A future real-provider adapter may require a separate redacted wire-envelope record.

Public-release licensing is unresolved; no LICENSE is present. Authoritative local verification ran on Python 3.13.15. Python 3.11/3.12 and remote CI did not run in this corrective milestone.

## Status fields

- As-of date: 2026-08-25
- Current stage: Stage 0 foundation and causally closed deterministic Stage 1 vertical slice complete
- Implemented features: low-level supervisor/worker LangGraph, bounded concurrency, durable request/response/action causality, fixture provider, typed fixture tools, classified retry/timeout/cancellation, coherent event/failure graphs, accepted partial state, provider isolation, validated atomic schema 0.3.0 artifact, CLI, offline tests, and CI configuration
- Exact local test status: recorded by the final verification report for Python 3.13.15; Ruff, format, strict mypy, schema sync, CLI artifact validation, and deterministic example verification are required
- Claim/evidence status: only Stage 1 local-runtime claims in `docs/claim-evidence.md` are implemented; remote CI and all later-stage/measurement claims remain unsupported
- Blockers: a separately authorized real-provider adapter with redacted wire provenance and an exact replay command before real-provider trajectory replay; sampling design/analysis before repeated-sampling claims
- Paid-resource use: none
- Visibility: local-only
- Git remote status: none
- Last verified local commit: current evidence `HEAD`; checked artifact records its clean implementation source commit
- Unresolved risks: no replay adapter/command, no real-provider wire provenance, no repeated sampler, no crash recovery/checkpointer, no process resume, one-process in-memory evidence, fixture-only behavior, no Python 3.11/3.12 or remote CI execution evidence, and unresolved public-release license
- Next stage: do not infer a Stage 2 implementation from this milestone; checkpoint/resume and human-interrupt design remain separate, while any real-provider or replay adapter requires explicit authorization and provenance design
