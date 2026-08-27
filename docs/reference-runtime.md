# Reference Runtime

The Reference Runtime proves that a compiled package can drive a real, resumable execution. It is deliberately small and provider-neutral; production schedulers and model adapters can replace it while preserving the same state and event semantics.

## Execution boundary

The runtime loads only the compiled package. It never discovers a moving Registry branch during a run. `registry.lock.json` pins the Capability Catalog and assets before execution begins.

Routing expressions use a restricted fact language. Supported operands are booleans, null, numbers and quoted strings; supported comparisons are `==`, `!=`, `>`, `>=`, `<` and `<=`, joined by `and` or `or`. Arbitrary code execution is not supported.

An Adapter or human gate completes an action node by submitting structured fact updates. The runtime merges those facts, evaluates every `completion_evidence` expression and rejects the completion if any expression is false. Choice and terminal nodes are automatic and cannot be completed manually.

## Durable state

Every mutation produces:

1. A domain event such as `node.completed` or `route.selected`.
2. A `state.checkpointed` event containing the resulting state.
3. A logical checkpoint at `runtime/checkpoints/<run-id>.json`.

Events are appended to `runtime/events/<run-id>.jsonl`. Each event carries `seq`, `prev_hash` and `event_hash`, forming a SHA-256 hash chain. Replay verifies sequence, linkage, event hashes and equality between the latest recorded state and the checkpoint file.

This detects accidental or unauthorized mutation; it is not a substitute for signed events or an immutable production store.

## State machine

```text
running → waiting_action → running → completed
   │             │
   ├─ paused ────┘ (resume restores the exact pre-pause status)
   ├─ waiting_facts
   └─ escalated (loop budget exhausted)
```

When a route targets a previously completed node, one loop round is counted. Crossing `max_rounds` stops routing and emits `loop.budget_exhausted` with the configured escalation owner.

## Command example

Compile the example first with `python scripts/run_example.py`, then run the complete executable demonstration with:

```bash
python scripts/run_runtime_example.py
```

For integration testing, use `runtime-start`, `runtime-route`, `runtime-complete`, `runtime-pause`, `runtime-resume` and `runtime-replay` from `scripts/workflowctl.py`. Fact updates are supplied as JSON files, keeping model prose outside the trusted routing boundary.
