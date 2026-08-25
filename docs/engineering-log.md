# Engineering log

## 2026-08-23 — Stage 0 and deterministic Stage 1 decisions

Low-level LangGraph `StateGraph` was selected because the milestone needs inspectable node/edge semantics, dynamic `Send` fan-out, one generic worker node, explicit reducer state, and an injected runtime context. A prebuilt agent abstraction would hide the exact graph and tool-policy boundaries this repository is intended to test.

Checkpointing and human-in-the-loop behavior were excluded from Stage 1 because durable resume and human interrupts change state compatibility, persistence, security, and operational semantics. Graph state is kept serialization-ready now, but adding empty checkpointer or `HumanDecision` code would imply unsupported behavior.

Runtime retries are distinct from evaluation retries. A single agent run internally controls each model/tool attempt, timeout, classification, and backoff. A future `llm-eval-reliability` adapter should therefore default the evaluation engine's outer candidate attempts to one; wrapping a whole internally retried run in another retry loop would distort accounting and failure attribution.

Full artifacts are not expected to be byte-identical. Run/event/attempt/tool identifiers, UTC timestamps, measured durations, source commit/dirty state, and real concurrent observation order are intentionally truthful and volatile. Semantic reproducibility is tested by hashing stable inputs and normalized outcomes, executing twice, comparing the semantic fingerprints, and comparing a stable artifact projection after removing only documented volatile fields.

The concurrency test does not rely on elapsed time. Both workers enter an injected active-section barrier and neither is released until both are present; a sequential worker implementation cannot satisfy it. A separate one-slot test checks the observed maximum never exceeds the configuration.

Failure and negative evidence are first-class. Tests demonstrate permanent failures do not retry, transient failures do, each attempt has an independent timeout, malformed/schema-invalid model output does not retry, tool policies and schemas fail closed, cancellation propagates to active work, accounting reconciles, invalid lifecycle evidence is rejected, and atomic replacement failure leaves no partial final.

The independent review found eight actionable integrity/contract issues: validation ordering for tool references, required lifecycle-event validation, synthetic token labeling, ignored jitter configuration, contradictory attempt evidence, unconstrained fixture digests, incomplete stable-projection comparison, and incomplete unexpected CLI normalization. All were accepted, fixed by the implementation agent, covered by regression tests, and independently re-reviewed as passing. The reviewer made no changes.

Features deliberately not implemented include checkpoint/resume, human interrupts/approval, MCP, sandbox/Docker, network/filesystem execution tools, RAG, Langfuse or configured LangSmith observability, FastAPI, real providers/fallback, benchmarks and submitted percentages, repeated sampling, annotations/raters/calibration, derived regression thresholds, significance claims, and changes to `llm-eval-reliability`.

Limitations: evidence remains in memory until the atomic final write; execution is one local process; fixtures prove runtime mechanics rather than model quality; remote CI has not run; local Python 3.14 is outside the configured 3.11-3.13 matrix; and public-release licensing remains unresolved.
