# Evaluation integration contract

Agent-runtime artifacts and `llm-eval-reliability` evaluation artifacts are different artifact types with different replay semantics. Stage 1 requires no change to `llm-eval-reliability`, and no cross-repository modification is authorized.

Current runtime artifact schema 0.3.0 persists the exact internal structured request passed to the fixture provider, its canonical payload digest, provider attempts/responses, accepted calls/results, and final decision. This is causal evidence, not chain-of-thought capture and not a general replay interface. The fixture request is the complete fixture-provider input. A later real-provider adapter may create a separate provider-wire envelope that must have explicitly redacted provenance rather than being conflated with the internal request.

A future trusted local adapter can implement the existing asynchronous `Candidate.generate()` contract. `CandidateResponse.output` should contain the serialized final structured decision. `CandidateResponse.metadata` should contain a portable agent-run reference with:

- `schema_version`;
- `run_id`;
- `status`;
- artifact content SHA-256;
- semantic fingerprint;
- task and configuration fingerprints;
- accounting summary;
- `external_model_calls`; and
- a relative artifact locator only when it is portable.

The evaluation engine's outer candidate `max_attempts` should default to 1 for this adapter. The runtime already records and controls internal model/tool retries; retrying the whole candidate run by default would double-retry and misrepresent turn, attempt, cost, and failure evidence.

The exact current integration gap is an implemented adapter/replay contract: runtime artifacts do not yet have a replay command, real-provider wire provenance, or a portable evaluation-owned reference with defined compatibility semantics. No claim of general trajectory replay is made.

This gap does not block deterministic final-output evaluation: a future trusted local adapter can invoke the runtime and return the final decision through the candidate contract. Trajectory-aware scoring and portable cross-repository replay need a later, separately authorized contract change. Correct per-run provider isolation is implemented, but repeated-sampling orchestration, aggregation, and analysis are not.
