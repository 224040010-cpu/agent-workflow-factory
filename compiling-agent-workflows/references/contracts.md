# Compiler contracts

Read the canonical field definitions from:

- `contracts/system-definition.json` for cross-repository ownership and invariants.
- `schemas/business-requirement.schema.json` for structured business input.
- `schemas/workflow-ir.schema.json` for the stable executable contract.
- `schemas/agent-profile.schema.json` for Agent permissions and budgets.
- `schemas/loop-spec.schema.json` for persistent loop safety.

Every package must contain:

```text
workflow.ir.json
graph.json
registry.lock.json
runtime.policy.json
compile-report.json
agents/*.agent.json          when Agent tasks exist
loops/*.loop.json            when persistent loops exist
```

Runtime proof additionally produces:

```text
runtime/events/<run-id>.jsonl
runtime/checkpoints/<run-id>.json
```

The lockfile is mandatory even when a workflow has no external tools because it also pins the Catalog and shared system-definition version.
