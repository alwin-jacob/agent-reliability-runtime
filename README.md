# Agent Reliability Runtime

This local-only repository is being built to make a small agent run inspectable: a supervisor plans work, bounded concurrent workers use typed read-only tools, and the supervisor produces a final structured decision. Reliability behavior belongs in the runtime rather than being hidden behind a model adapter.

The current foundation establishes the Python package, locked tooling, repository safety rules, and architecture boundary. The Stage 1 implementation adds a deterministic fixture provider, low-level LangGraph orchestration, event and attempt evidence, accounting, artifact integrity, a CLI, and offline tests.

Generic evaluation remains the responsibility of the separate `llm-eval-reliability` repository. This repository does not modify it, call a paid provider, or make benchmark, calibration, deployment, checkpointing, resume, human-in-the-loop, MCP, sandbox, RAG, or observability claims.

The completed quickstart and exact current-versus-planned status are maintained here only after their corresponding checks pass.
