# Repository operating rules

## Scope and ownership

This repository owns local agent-runtime execution: deterministic orchestration, worker execution, typed tools, runtime reliability controls, event traces, accounting, and agent-runtime artifacts. It does not own generic evaluation.

`llm-eval-reliability` is a separate repository. Do not modify it from this repository. Any future integration must use an explicit adapter contract and separate runtime and evaluation artifacts.

The currently implemented target is Stage 0 repository foundation plus the deterministic Stage 1 supervisor/two-worker vertical slice. Stage 2 features must remain documented as planned until implemented and verified.

## Required commands

Use the repository-local locked environment. Before a local commit, run the checks relevant to that commit; before reporting completion, run all of these commands:

```sh
uv lock --check
uv sync --python 3.13 --frozen --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest -q
uv run python scripts/check_schema_sync.py
rm -f /tmp/retail-return-v3.run.json
uv run agent-runtime run --task examples/tasks/retail-return-v1.json --config examples/configs/deterministic-v1.json --output /tmp/retail-return-v3.run.json
uv run agent-runtime validate-artifact /tmp/retail-return-v3.run.json
uv run python scripts/verify_example.py
git diff --check
git status --short
```

Tests, schema synchronization, deterministic example generation, artifact validation, and semantic example verification are required evidence. A workflow file proves configuration only; do not claim remote CI passed without remote evidence.

## Security and external-action rules

- Keep the repository and all work local-only. Never create or modify a GitHub repository, add a Git remote, push, publish, deploy, or open a pull request.
- Do not use a paid model API, provider CLI model call, cloud service, GPU, or external runtime without explicit user authorization. Stage 1 uses only checked-in deterministic fixtures.
- Never commit secrets, credential material, environment-variable values, usernames, hostnames, absolute home-directory paths, canonical context, resume PDFs, `ResumeProjects_Submitted.md`, loan or immigration files, or other private project documents.
- Tools implemented in Stage 1 must be fixture-backed, read-only, network-free, filesystem-free at invocation time, side-effect-free, and idempotent.
- Dependency installation may use the network only for pinned open-source packages. Runtime verification and tests must not use the network.
- Never modify `llm-eval-reliability` as part of work in this repository.

## Evidence and claim rules

- Preserve typed failures, every durable model request, every model/tool attempt, state transitions, event sequence, accounting, provenance, and content integrity in versioned artifacts.
- Do not report unsupported measurements or resume claims. In particular, do not claim benchmark percentages, repeated-sampling reliability, judge calibration, annotator consensus, Cohen's kappa, significance, deployment, or unimplemented infrastructure.
- README and claim/evidence documentation must clearly separate what is implemented now from what is planned. Planned work must not appear as an empty module, working feature, or completed checklist item.
- Do not mark checkpointing, process resume, human approval/interrupts, MCP, sandboxing/Docker, network tools, RAG, Langfuse/LangSmith observability, real-model providers, provider fallback, or benchmark programs as implemented.
- Before each local commit, the relevant tests and generated artifacts must pass and the staged diff must be inspected. Use the existing Git identity; never invent or change identity configuration.
- Never add a remote. Confirm `git remote -v` remains empty throughout the work and in final evidence.
