# Canonical project state

## Scope

This local repository owns agent runtime execution and runtime evidence. Generic evaluation remains in the separate read-only-for-this-action `llm-eval-reliability` repository.

## Current evidence

The implementation uses a low-level LangGraph supervisor/two-worker graph, package 0.3.2 and current artifact schema 0.3.0, unchanged independent task/config/fixture schemas 0.2.0, durable hashed internal model requests, deterministic raw-JSON fixture responses, two typed in-memory retail tools, one pure live/artifact semantic validator, exact fixture-runtime attempt taxonomy, lifecycle-bounded request and output-failure timing, strict partial-success output validation, fixture-response consistency checks, bounded concurrency, typed failure references, cancellation-consistent startup and in-memory evidence commits, accepted partial-state preservation, closed-vocabulary event reconciliation, per-run provider isolation, accounting, atomic persistence, CLI, generated JSON Schema, offline tests, and a configured Python 3.11-3.13 CI matrix.

The current checked successful artifact uses schema 0.3.0 and proves the exact four-request response-to-plan/call/decision chain plus semantic/configuration reproduction. Prior artifact schemas 0.1.0 and 0.2.0 remain unchanged historical evidence. Every successful partial tool output is schema-validated regardless of final status. Startup, planning entry, finalization entry, and failed terminalization are cancellation-consistent; cancellation after startup but before planning work persists no fabricated lifecycle/request evidence, and committed success remains a persisted success while the caller still observes cancellation. The internal structured request is not chain-of-thought; it is the complete fixture-provider input. A future real-provider adapter may require a separate redacted wire-envelope record.

Hashes detect modification but are not signatures. Source commit, lock digest, and other provenance require matching repository context before they can support authenticity.

The public repository is `alwin-jacob/agent-reliability-runtime` at <https://github.com/alwin-jacob/agent-reliability-runtime>. Its configured `origin` is `https://github.com/alwin-jacob/agent-reliability-runtime.git`. The initial private-staging source HEAD was `9027e2446857b158ac65ae4fee767e68c427e8e6`; governance and staging-state documentation commits followed. Private-stage CI runs `32924165501` and `33012384259` completed successfully on Python 3.11.16, 3.12.14, and 3.13.15, with 277 tests passing in each matrix job. GitHub detects the root license as MIT. The current `main` documentation commit is the public-release head after its own successful final CI gate. The checked artifact still records its historical clean implementation-source commit.

This release includes no tag, GitHub Release, package publication, deployment, profile change, pin change, or outreach.

## Status fields

- As-of date: 2026-08-26
- Repository: `alwin-jacob/agent-reliability-runtime`
- Visibility: public GitHub repository
- Public URL: <https://github.com/alwin-jacob/agent-reliability-runtime>
- Current stage: Stage 0 plus deterministic Stage 1 complete
- Stage 1: source-accepted and frozen
- Package: 0.3.2
- Artifact schema: 0.3.0
- Implemented features: low-level supervisor/worker LangGraph, bounded concurrency, durable request/response/action causality, fixture provider, typed fixture tools, exact attempt taxonomy and effective-policy validation, causal request/output-failure timing, partial successful-output validation, fixture-response semantics, coherent event/failure graphs, cancellation-consistent startup and in-memory evidence commits, accepted partial state, provider isolation, validated atomic schema 0.3.0 artifact, CLI, offline tests, and CI configuration
- Exact verification status: local Python 3.13.15 verification; private-stage GitHub Actions runs `32924165501` and `33012384259`; and the current release-head Actions run on Python 3.11, 3.12, and 3.13; every matrix job passed Ruff, format, strict mypy, all 277 tests, schema synchronization, deterministic generation, generated and checked artifact validation, and semantic verification
- Claim/evidence status: Stage 1 local implementation plus verified remote CI and a publicly inspectable repository; all later-stage, real-provider, deployment, and measurement claims remain unsupported
- Blockers: a separately authorized real-provider adapter with redacted wire provenance and an exact replay command before real-provider trajectory replay; sampling design/analysis before repeated-sampling claims
- Paid-resource use: none
- Git remote: `origin` configured and synchronized
- Remote CI: verified by private-stage runs `32924165501` and `33012384259` plus the current release-head Actions run
- Last verified local commit: current evidence `HEAD`; the checked artifact continues to record its historical clean implementation-source commit
- Public release gate: completed only after final release-head CI and visibility verification
- Employer-visible evidence: public Stage 1 repository
- Remaining risks: fixture-only behavior, no real-provider wire provenance, no replay command, no repeated sampler, no checkpoint/crash recovery, no process resume, no human approval/interrupt path, and no deployment
- Stage 2: unimplemented and unauthorized; checkpoint/resume and human-interrupt design remain separate, while the exact next blocker for real-provider trajectory evidence is an explicitly authorized adapter contract defining redacted wire provenance and replay semantics
