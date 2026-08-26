# Repository operating rules

## Scope and ownership

This repository owns local agent-runtime execution: deterministic orchestration, worker execution, typed tools, runtime reliability controls, event traces, accounting, and agent-runtime artifacts. It does not own generic evaluation.

`llm-eval-reliability` is a separate repository. Do not modify it from this repository. Any future integration must use an explicit adapter contract and separate runtime and evaluation artifacts.

The currently implemented target is Stage 0 repository foundation plus the deterministic Stage 1 supervisor/two-worker vertical slice. Stage 2 features must remain documented as planned until implemented and verified.

The package version is `0.3.2`. The current durable artifact schema remains `0.3.0`, and task, config, model-fixture, order-fixture, and policy-fixture schemas remain `0.2.0`. This closure validates exact attempt taxonomy and causal lifecycle time, and makes startup/planning entry cancellation-consistent without changing the artifact shape or adding durable checkpointing.

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

## Current authorized repository state

```text
Repository:
alwin-jacob/agent-reliability-runtime

Remote:
origin

Remote URL:
https://github.com/alwin-jacob/agent-reliability-runtime.git

Current visibility:
private

Default branch:
main
```

The repository is currently private. Public visibility is not implied by the presence of a remote. Stage 1 is source-accepted and frozen. Remote CI evidence and current staging facts belong in project-state and claim/evidence documentation. This governance amendment does not authorize a push.

For this repository, `git remote -v` must show exactly the authorized `origin` URL. Local-only tasks must not push merely because the remote exists. A push requires explicit authorization in the active task.

## Security and external-action rules

- GitHub and other external actions are prohibited by default.
- An external operation is allowed only when Alwin explicitly authorizes the exact repository, exact operation, exact branch, and applicable visibility boundary.
- Authorization for one operation does not authorize a later or broader operation.
- Normal fast-forward pushes to the authorized private `origin/main` are allowed only when the current task explicitly authorizes that push.
- The remote must never be changed, removed, renamed, or repointed without separate explicit authorization.
- Public visibility always requires a distinct explicit authorization. Authorization to create, stage, push, or inspect a private repository does not authorize publication.
- Before any authorized push, the tree must be clean except for the explicitly authorized committed work; local `main` and `origin/main` must have the expected relationship; the repository must have the expected visibility; and the changed-file scope must match the authorization.
- After any authorized push, local, tracking, API, and remote SHAs must be reconciled; CI must be inspected when the task requires it; and visibility must be rechecked.
- Force-pushes, history rewriting, rebasing published history, branch deletion, tags, releases, issues, pull requests, deployments, package publication, profile edits, pin changes, and outreach remain prohibited unless each is separately and explicitly authorized.
- Operations on any other repository remain prohibited unless separately authorized.
- No task may silently expand into Stage 2 or a later capability.
- Do not use a paid model API, provider CLI model call, cloud service, GPU, or external runtime without explicit user authorization. Stage 1 uses only checked-in deterministic fixtures.
- Never commit secrets, credential material, environment-variable values, usernames, hostnames, absolute home-directory paths, canonical context, resume PDFs, `ResumeProjects_Submitted.md`, loan or immigration files, or other private project documents.
- Tools implemented in Stage 1 must be fixture-backed, read-only, network-free, filesystem-free at invocation time, side-effect-free, and idempotent.
- Dependency installation may use the network only for pinned open-source packages. Runtime verification and tests must not use the network.
- Never modify `llm-eval-reliability` as part of work in this repository.

## Evidence and claim rules

- Preserve typed failures, every durable model request, every model/tool attempt, state transitions, event sequence, accounting, provenance, and content integrity in versioned artifacts.
- Enforce effective retry policy, deterministic response-to-failure causality, fixture-response semantics, strict validation of every successful partial tool output, timestamp/event/span consistency, and cancellation-consistent in-memory evidence units.
- Treat unkeyed hashes as modification detectors, not signatures. Source commit, lock digest, and other provenance require matching repository context before making authenticity claims.
- Do not report unsupported measurements or resume claims. In particular, do not claim benchmark percentages, repeated-sampling reliability, judge calibration, annotator consensus, Cohen's kappa, significance, deployment, or unimplemented infrastructure.
- README and claim/evidence documentation must clearly separate what is implemented now from what is planned. Planned work must not appear as an empty module, working feature, or completed checklist item.
- Do not mark checkpointing, process resume, human approval/interrupts, MCP, sandboxing/Docker, network tools, RAG, Langfuse/LangSmith observability, real-model providers, provider fallback, or benchmark programs as implemented.
- Before each local commit, the relevant tests and generated artifacts must pass and the staged diff must be inspected. Use the existing Git identity; never invent or change identity configuration.
