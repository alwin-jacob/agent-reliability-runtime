# Evaluation integration contract

Agent-runtime artifacts and `llm-eval-reliability` evaluation artifacts are different artifact types with different replay semantics. Stage 1 requires no change to `llm-eval-reliability`, and no cross-repository modification is authorized.

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

The exact current integration gap is that `llm-eval-reliability` has no stable top-level exported external runtime-artifact reference and no general JSON-configured Python scorer factory for trajectory-aware scoring. Its current top-level export remains only `__version__`, and its existing replay candidate replays evaluation `CandidateResponse` objects rather than arbitrary agent-runtime trajectories.

This gap does not block deterministic final-output evaluation: a trusted local `module:callable` candidate factory can invoke the runtime and return the final decision through the current candidate contract. Trajectory-aware scoring and portable cross-repository replay need a later, separately authorized contract change.
