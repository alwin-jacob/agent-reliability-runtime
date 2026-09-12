# Evaluation integration

The agent runtime and `llm-eval-reliability` have complementary responsibilities.

This repository owns agent execution: orchestration, model and tool attempts, retries, failures, accounting, provenance, and the runtime artifact.

`llm-eval-reliability` owns evaluation: candidate execution interfaces, scoring, aggregation, regression analysis, and evaluation artifacts.

Keeping those artifact types separate allows the runtime to preserve detailed execution evidence while the evaluation layer remains independent of a particular agent implementation.

## Candidate adapter

Integration can use the existing asynchronous `Candidate.generate()` interface.

A runtime-backed candidate should place the final structured decision in:

```text
CandidateResponse.output
