# Evaluation integration

The agent runtime and `llm-eval-reliability` have complementary responsibilities.

This repository owns agent execution: orchestration, model and tool attempts, retries, failures, accounting, provenance, and runtime artifacts.

`llm-eval-reliability` owns evaluation: candidate execution interfaces, scoring, aggregation, regression analysis, and evaluation artifacts.

Keeping those artifact types separate allows the runtime to preserve detailed execution evidence while the evaluation layer remains independent of a particular agent implementation.

## Candidate adapter

Integration can use the existing asynchronous `Candidate.generate()` interface.

A runtime-backed candidate should place the final structured decision in `CandidateResponse.output`.

`CandidateResponse.metadata` should contain a portable runtime-artifact reference with:

* `schema_version`;
* `run_id`;
* `status`;
* artifact content SHA-256;
* semantic fingerprint;
* task fingerprint;
* configuration fingerprint;
* accounting summary;
* `external_model_calls`;
* a relative artifact locator when one is portable.

This keeps the evaluation response compact while preserving a path back to the complete execution record.

## Retry ownership

The runtime owns retries for individual model and tool operations.

Each logical model turn or tool call may contain multiple attempts governed by the effective runtime configuration. Those attempts are persisted and accounted for inside the runtime artifact.

For a runtime-backed evaluation candidate, the evaluation engine's outer candidate `max_attempts` should normally default to `1`.

That avoids wrapping a complete internally retried agent run in a second retry loop and keeps turn, attempt, cost, and failure accounting interpretable.

## Runtime request evidence

Artifact schema `0.3.0` persists the structured request used by the deterministic fixture provider together with:

* its canonical payload digest;
* provider attempts and responses;
* accepted tool calls and results;
* the final structured decision;
* accounting;
* provenance.

For the fixture provider, the persisted structured request is the complete provider input.

A hosted-provider adapter can preserve the same internal request boundary while adding a separate redacted provider-wire record when the external API envelope contains additional transport or provider-specific information.

Keeping those representations separate avoids coupling the runtime's semantic request model to a particular provider protocol.

## Artifact references

Evaluation artifacts should reference runtime artifacts rather than duplicate them.

A portable reference should be sufficient to:

1. identify the runtime artifact;
2. verify its content digest;
3. identify its semantic and configuration fingerprints;
4. inspect execution status and accounting;
5. resolve the artifact within the evaluation output when a portable relative path is available.

The ownership relationship is:

evaluation artifact
→ score or regression result
→ candidate output
→ runtime artifact reference
→ orchestration evidence, requests and responses, tool execution, failures, accounting, and provenance

## Replay extension

The persisted request-response-action chain provides the information needed to design deterministic replay interfaces.

A replay adapter can define:

* which recorded request representation is replayed;
* provider-wire redaction and reconstruction rules;
* compatibility across runtime and provider versions;
* handling of volatile provider metadata;
* comparison between recorded and replayed outputs.

For deterministic fixture execution, the persisted structured request already captures the complete provider input.

For hosted providers, a provider-specific adapter can extend the provenance model without changing the runtime's core request semantics.

## Sampling and evaluation

Each runtime execution constructs a fresh provider instance, so individual runs remain isolated from one another.

Repeated execution, aggregation, confidence estimation, regression thresholds, and other sampling policy belong naturally in the evaluation layer rather than inside one agent run.

That separation keeps the runtime responsible for **one inspectable execution** and the evaluation system responsible for **reasoning across executions**.
