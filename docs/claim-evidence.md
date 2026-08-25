# Claim and evidence matrix

Only locally observed Stage 1 behavior is marked implemented. A workflow file is CI configuration, not evidence that remote CI passed.

| Claim target | Current status | Exact evidence | Missing evidence | Stage |
|---|---|---|---|---|
| Low-level LangGraph orchestration | Implemented locally | `build_graph()` uses `StateGraph`, `START`, `END`; deterministic Python 3.13 suite | Remote CI execution | Stage 1 |
| Supervisor/worker structure | Implemented locally | `supervisor_plan_node`, dynamic `Send`, generic `worker_node`, reducer, `supervisor_finalize_node`; exact-decision integration test | General task coverage | Stage 1 |
| Two-worker bounded concurrency | Implemented locally | Deterministic two-entry barrier and separate max-active bound tests in `tests/test_concurrency.py` | Multi-process/distributed evidence | Stage 1 |
| Typed in-process tool/function calling | Implemented locally | Strict retail schemas, allowed-tool enforcement, input/output tests in `tests/test_tools.py` | Network/MCP/sandbox tools | Stage 1 |
| Deterministic fixture model provider | Implemented locally | Raw JSON fixture provider tests cover success/failures/delay/output/cancellation | Real-provider evidence | Stage 1 |
| Retries/timeouts and typed failures | Implemented locally | Shared invocation tests plus resealed artifacts enforcing the effective model/tool maxima, retryability, exact timeout/cancellation taxonomy, and early-stop rules | Real infrastructure behavior | Stage 1 |
| Evidence-bound retail semantics | Implemented locally | `semantics.py` is shared by live finalization and artifact validation; context/date/condition/inside/outside/adversarial tests | General-domain decision semantics | Stage 1 |
| Cancellation semantics | Implemented locally | Boundary tests cover shielded record/event/state commits, finalization entry, classified/unexpected failure terminalization, probe bookkeeping, active-phase cancelled persistence, terminal assembly/writing, and committed terminal status while caller cancellation is re-raised | Process termination recovery | Stage 1 |
| Durable request/response/action causality | Implemented locally | Four exact `ModelRequestRecord` values, canonical payload digests, deterministic parse/schema/semantic failure reconciliation, accepted plan/call/decision equality, and fully resealed adversarial tests | Real-provider wire-envelope provenance | Stage 1 |
| Partial successful tool evidence | Implemented locally | Every successful order/policy attempt is strictly parsed for succeeded, failed, and cancelled runs; accepted worker output must equal its terminal output | Non-fixture tool families | Stage 1 |
| Fixture-response contract | Implemented locally | Successful responses require fixture provider, scripted finish, zero cost, synthetic-or-absent tokens, matching fixture metadata, one model ID, and the exact three digest keys | Provider authenticity or real billing evidence | Stage 1 |
| Structured local event trace | Implemented locally | Closed schema-0.3.0 vocabulary; lock-assigned sequence; inclusive run interval, attempt/event ordering, failure timestamp, identity/context, and lifecycle/attempt span-collision tests | External trace backend | Stage 1 |
| Failure-reference graph | Implemented locally | Stable failure IDs; nested/top-level/event reconciliation; transient retention, dangling-reference, and failed-worker contradiction tests | Cross-process failure storage | Stage 1 |
| Turn/attempt/tool accounting | Implemented locally | Accounting is derived and independently reconciled by validator/tests | Provider-measured usage from a real provider | Stage 1 |
| Zero-cost fixture accounting | Implemented locally | Checked artifact: external calls false, cost USD 0.0, null token counts | Billing reconciliation for real providers | Stage 1 |
| Versioned run artifact | Implemented locally | Package 0.3.1; current artifact schema 0.3.0 plus unchanged historical 0.1.0/0.2.0; independent unchanged task/config/fixture 0.2.0 schemas; generated sync and adversarial tests | Migration policy for later schemas | Stage 1 |
| Hash-based modification detection | Implemented with explicit limit | Content/configuration/semantic recomputation tests | No signature or writer authenticity; source commit, lock digest, and provenance require repository context; later release controls | Stage 1 |
| Per-run fixture-provider isolation | Implemented locally | One `LoadedInputs` executed twice reproduces transient-failure/success attempts and semantic fingerprint | No sampler or repeated-sampling analysis | Stage 1 |
| CI configuration and local test evidence | Implemented locally | `.github/workflows/ci.yml`; Python 3.13 Ruff, format, strict mypy, pytest, schema/example commands pass | No Python 3.11/3.12 or remote CI execution evidence in this corrective milestone | Stage 1 |
| Checkpointing | Not implemented | No checkpointer construction or checkpoint storage | Design, implementation, crash tests | Stage 2 |
| Process resume | Not implemented | Project state explicitly excludes resume | Checkpoint compatibility and recovery evidence | Stage 2 |
| Human interrupts/approval | Not implemented | No `HumanDecision` model or interrupt node | Interaction/state policy and tests | Stage 2 |
| MCP | Not implemented | No MCP client/server/tool module | Protocol/security implementation and tests | Later |
| Sandbox or Docker | Not implemented | No container/sandbox configuration | Threat model and isolation evidence | Later |
| RAG | Not implemented | No retrieval, embedding, reranking, or vector component | Data/index/evaluation design | Later |
| Langfuse or project LangSmith observability | Not implemented | Only the local artifact/event stream is configured | Backend integration and privacy review | Later |
| Real-model execution | Not implemented | Only provider value `fixture` is accepted | Authorized provider adapter and cost/reliability evidence | Later |
| Retail benchmark suite | Not implemented | One synthetic task only | Versioned benchmark, execution, scoring | Later |
| Submitted benchmark percentages | Not implemented | No benchmark run artifact or analysis | Reproducible measurements | Later |
| Repeated-sampling reliability | Not implemented | Per-run provider isolation exists, but there is no sampler, aggregation, or repeated-sampling analysis | Sampling design and repeated-run analysis evidence | Later |
| Exact trajectory replay | Not implemented | Requests/responses/actions persist and reconcile, but no replay adapter or command exists | Adapter/replay semantics and real-provider wire provenance | Later |
| Judge calibration and inter-rater agreement | Not implemented | No judge, annotation, or rater pipeline | Audited labels, raters, calibration analysis | Later |
| Regression-threshold derivation | Not implemented | No statistical threshold policy in runtime | Baseline data and justified derivation | Later |

## Local evidence snapshot

On 2026-08-25, authoritative Python 3.13.15 local verification reported Ruff clean, Ruff format clean, strict mypy clean, the deterministic pytest suite passing, synchronized artifact schema 0.3.0, successful deterministic CLI run, valid generated/checked artifacts, exact causal reconciliation, and equal semantic reproduction. Python 3.11/3.12 and remote CI were not run in this milestone. Hashes are modification detectors, not signatures.
