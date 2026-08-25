# Claim and evidence matrix

Only locally observed Stage 1 behavior is marked implemented. A workflow file is CI configuration, not evidence that remote CI passed.

| Claim target | Current status | Exact evidence | Missing evidence | Stage |
|---|---|---|---|---|
| Low-level LangGraph orchestration | Implemented locally | `build_graph()` uses `StateGraph`, `START`, `END`; 115-test Python 3.13 suite | Remote CI execution | Stage 1 |
| Supervisor/worker structure | Implemented locally | `supervisor_plan_node`, dynamic `Send`, generic `worker_node`, reducer, `supervisor_finalize_node`; exact-decision integration test | General task coverage | Stage 1 |
| Two-worker bounded concurrency | Implemented locally | Deterministic two-entry barrier and separate max-active bound tests in `tests/test_concurrency.py` | Multi-process/distributed evidence | Stage 1 |
| Typed in-process tool/function calling | Implemented locally | Strict retail schemas, allowed-tool enforcement, input/output tests in `tests/test_tools.py` | Network/MCP/sandbox tools | Stage 1 |
| Deterministic fixture model provider | Implemented locally | Raw JSON fixture provider tests cover success/failures/delay/output/cancellation | Real-provider evidence | Stage 1 |
| Retries/timeouts and typed failures | Implemented locally | Shared invocation tests plus model/tool integration failure tests | Real infrastructure behavior | Stage 1 |
| Evidence-bound retail semantics | Implemented locally | `semantics.py` is shared by live finalization and artifact validation; context/date/condition/inside/outside/adversarial tests | General-domain decision semantics | Stage 1 |
| Cancellation semantics | Implemented locally | Active-worker cancellation, failed-persistence preservation, and phase-publication consistency tests | Process termination recovery | Stage 1 |
| Structured local event trace | Implemented locally | Lock-assigned sequence test; validator enforces lifecycle/attempt/transition evidence; model attempts/responses persist | Exact `ModelRequest` payloads or request digests; external trace backend | Stage 1 |
| Turn/attempt/tool accounting | Implemented locally | Accounting is derived and independently reconciled by validator/tests | Provider-measured usage from a real provider | Stage 1 |
| Zero-cost fixture accounting | Implemented locally | Checked artifact: external calls false, cost USD 0.0, null token counts | Billing reconciliation for real providers | Stage 1 |
| Versioned run artifact | Implemented locally | Current schema 0.2.0 plus historical 0.1.0; generated schema sync, atomicity, resealed-adversarial, semantic, timestamp, identity, and checked-artifact tests | Migration policy for later schemas | Stage 1 |
| Hash-based modification detection | Implemented with explicit limit | Content/configuration/semantic recomputation tests | No signature or writer authenticity; later release controls | Stage 1 |
| CI configuration and local test evidence | Implemented locally | `.github/workflows/ci.yml`; Python 3.13 Ruff, format, strict mypy, 115 pytest tests, schema/example commands pass | No Python 3.11/3.12 or remote CI execution evidence in this corrective milestone | Stage 1 |
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
| Repeated-sampling reliability | Not implemented | One run per invocation; no repeated sampler; `LoadedInputs` provider is stateful and one-execution | Per-run provider construction, sampling design, and repeated-run evidence | Later |
| Exact trajectory replay | Not implemented | Attempts and provider responses persist, but exact model-request payloads do not | Decide and implement request persistence or request digests | Later |
| Judge calibration and inter-rater agreement | Not implemented | No judge, annotation, or rater pipeline | Audited labels, raters, calibration analysis | Later |
| Regression-threshold derivation | Not implemented | No statistical threshold policy in runtime | Baseline data and justified derivation | Later |

## Local evidence snapshot

On 2026-08-25, authoritative Python 3.13.15 local verification reported Ruff clean, Ruff format clean, strict mypy clean, 115 pytest tests passing, synchronized schema 0.2.0, successful deterministic CLI run, valid generated/checked artifacts, and equal semantic reproduction. Python 3.11/3.12 and remote CI were not run in this corrective milestone. Hashes are modification detectors, not signatures.
