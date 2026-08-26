# Canonical project state

## Scope

This local repository owns agent runtime execution and runtime evidence. Generic evaluation remains in the separate read-only-for-this-action `llm-eval-reliability` repository.

## Current evidence

The implementation uses a low-level LangGraph supervisor/two-worker graph, package 0.3.2 and current artifact schema 0.3.0, unchanged independent task/config/fixture schemas 0.2.0, durable hashed internal model requests, deterministic raw-JSON fixture responses, two typed in-memory retail tools, one pure live/artifact semantic validator, exact fixture-runtime attempt taxonomy, lifecycle-bounded request and output-failure timing, strict partial-success output validation, fixture-response consistency checks, bounded concurrency, typed failure references, cancellation-consistent startup and in-memory evidence commits, accepted partial-state preservation, closed-vocabulary event reconciliation, per-run provider isolation, accounting, atomic persistence, CLI, generated JSON Schema, offline tests, and a configured Python 3.11-3.13 CI matrix.

The current checked successful artifact uses schema 0.3.0 and proves the exact four-request response-to-plan/call/decision chain plus semantic/configuration reproduction. Prior artifact schemas 0.1.0 and 0.2.0 remain unchanged historical evidence. Every successful partial tool output is schema-validated regardless of final status. Startup, planning entry, finalization entry, and failed terminalization are cancellation-consistent; cancellation after startup but before planning work persists no fabricated lifecycle/request evidence, and committed success remains a persisted success while the caller still observes cancellation. The internal structured request is not chain-of-thought; it is the complete fixture-provider input. A future real-provider adapter may require a separate redacted wire-envelope record.

Hashes detect modification but are not signatures. Source commit, lock digest, and other provenance require matching repository context before they can support authenticity.

The repository is `alwin-jacob/agent-reliability-runtime`, with visibility restricted to private GitHub staging. Its configured `origin` is `https://github.com/alwin-jacob/agent-reliability-runtime.git`. The initial private-staging source HEAD was `9027e2446857b158ac65ae4fee767e68c427e8e6`, and initial private-staging CI run `32924165501` completed successfully. Python 3.11.16, 3.12.14, and 3.13.15 were verified, with 277 tests passing in each matrix job. GitHub detects the root license as MIT. Public visibility is not authorized and was not performed.

## Status fields

- As-of date: 2026-08-26
- Current stage: Stage 0 plus deterministic Stage 1 complete
- Stage 1 status: source-accepted and frozen
- Implemented features: low-level supervisor/worker LangGraph, bounded concurrency, durable request/response/action causality, fixture provider, typed fixture tools, exact attempt taxonomy and effective-policy validation, causal request/output-failure timing, partial successful-output validation, fixture-response semantics, coherent event/failure graphs, cancellation-consistent startup and in-memory evidence commits, accepted partial state, provider isolation, validated atomic schema 0.3.0 artifact, CLI, offline tests, and CI configuration
- Exact verification status: local Python 3.13.15 verification and private GitHub Actions run `32924165501` on Python 3.11.16, 3.12.14, and 3.13.15; every matrix job passed Ruff, format, strict mypy, all 277 tests, schema synchronization, deterministic generation, generated and checked artifact validation, and semantic verification
- Claim/evidence status: Stage 1 local implementation plus verified private remote CI; all later-stage, real-provider, deployment, and measurement claims remain unsupported
- Blockers: a separately authorized real-provider adapter with redacted wire provenance and an exact replay command before real-provider trajectory replay; sampling design/analysis before repeated-sampling claims
- Paid-resource use: none
- Visibility: private GitHub staging
- Git remote status: `origin` configured and synchronized at initial staging commit `9027e2446857b158ac65ae4fee767e68c427e8e6` before the governance and documentation descendants
- Remote CI: verified by initial private-staging run `32924165501`
- Last verified local commit: current evidence `HEAD`; the checked artifact continues to record its historical clean implementation-source commit
- Current release gate: public visibility remains separately unauthorized
- Unresolved risks: no replay adapter/command, no real-provider wire provenance, no repeated sampler, no crash recovery/checkpointer, no process resume, no human approval/interrupt path, one-process in-memory evidence, and fixture-only behavior
- Next stage: Stage 2 remains unimplemented and unauthorized; checkpoint/resume and human-interrupt design remain separate, while the exact next blocker for real-provider trajectory evidence is an explicitly authorized adapter contract defining redacted wire provenance and replay semantics
