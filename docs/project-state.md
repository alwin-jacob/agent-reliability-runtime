# Canonical project state

## Scope

This local repository owns agent runtime execution and runtime evidence. Generic evaluation remains in the separate read-only-for-this-action `llm-eval-reliability` repository.

## Current evidence

The implementation uses a low-level LangGraph supervisor/two-worker graph, strict Pydantic v2 models, one deterministic raw-JSON fixture provider, two typed in-memory retail tools, a shared invocation policy, bounded worker concurrency, typed failures, cancellation propagation, structured events, exact accounting, versioned/hashed artifacts, atomic persistence, validation CLI, generated JSON Schema, offline tests, and a configured Python 3.11-3.13 CI matrix.

The checked successful artifact was generated from a clean tree at implementation commit `ceedafaf1a6309f6b7004c1f4aeba09d87a7b324`. It validates and reproduces the exact final decision and semantic/configuration fingerprints. The independent review's eight findings were fixed, regression-tested, and independently verified. The reviewer changed no files.

Public-release licensing is unresolved; no LICENSE is present. Remote CI has not run. Direct CLI startup on local Python 3.14 emits an upstream compatibility warning to stderr; the supported/configured matrix is 3.11-3.13.

## Status fields

- As-of date: 2026-08-25
- Current stage: Stage 0 foundation and deterministic Stage 1 vertical slice complete
- Implemented features: low-level supervisor/worker LangGraph, two-worker bounded concurrency, fixture provider, typed fixture tools, classified retry/timeout/cancellation, events/accounting, validated atomic artifact, CLI, offline tests, and CI configuration
- Exact local test status: 53 passed; Ruff check passed; Ruff format check passed; strict mypy passed; schema sync and deterministic example verification passed
- Claim/evidence status: only Stage 1 local-runtime claims in `docs/claim-evidence.md` are implemented; remote CI and all later-stage/measurement claims remain unsupported
- Blockers: none for Stage 1 completion
- Paid-resource use: none
- Visibility: local-only
- Git remote status: none
- Last verified local commit: current evidence `HEAD`; clean artifact provenance commit `ceedafaf1a6309f6b7004c1f4aeba09d87a7b324`
- Unresolved risks: no crash recovery/checkpointer, no process resume, one-process in-memory evidence, fixture-only behavior, local Python outside CI matrix, no remote CI evidence, and unresolved public-release license
- Next stage: separately scope checkpoint/resume compatibility and human-interrupt design; no next implementation task is issued here
