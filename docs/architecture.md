# Architecture

## Current boundary

The repository owns local agent execution. Domain values are serializable, strict data; providers and tools are asynchronous injected services; orchestration coordinates those services; artifact code validates and persists evidence. Generic scoring and evaluation artifacts remain outside this repository.

The Stage 1 graph is supervisor planning, dynamic fan-out to two concurrent invocations of one generic worker node, then supervisor finalization. Live providers, tool registries, recorders, concurrency controls, locks, and file handles stay outside graph state so the state is suitable for a later checkpointing design. Checkpointing itself is not implemented.

## Reliability ownership

The runtime owns retries and independent timeouts for each internal model/tool attempt. Cancellation is control flow and must propagate. A future evaluation adapter should therefore use one outer candidate attempt by default. Event order reflects actual observation under concurrency; full artifacts are not promised byte-identical. Stable task/config/fixture inputs and normalized outcomes form a semantic fingerprint instead.

Detailed implementation, trade-offs, transition rules, and limitations are added with the verified Stage 1 implementation.
