# Agent Workflow Factory

Compile governed business workflows into deployable Agent packages.

```text
business requirement
  → BPMN 2.0
  → Workflow IR
  → fact-routed Agent Graph
  → Agent / Loop / Policy manifests
  → Harness Adapter
  → append-only trajectory
```

This repository is the workflow compiler and runtime plane. Canonical Skill and Tool definitions remain in `skill-registory`; this repository only resolves approved assets from an immutable Capability Catalog and pins them in `registry.lock.json`.

The two repositories carry byte-identical copies of [`contracts/system-definition.json`](contracts/system-definition.json). `skill-registory` publishes the canonical definition; this repository verifies its mirror and refuses to package against a Catalog produced from another definition version.

## Implemented v3 vertical slice

- Structured business-language contract.
- Deterministic business definition → BPMN 2.0 generation.
- BPMN subset parser with lanes, tasks, gateways, sequence conditions and loop annotations.
- Workflow IR and fact-routed Graph generation.
- Catalog resolution for approved/restricted Skill and Tool versions.
- Deterministic `registry.lock.json` with snapshot and asset digests.
- Agent Profile generation from explicit responsibility annotations.
- LoopSpec generation with checker, finite budget, stop and escalation contracts.
- Package validation and tests.
- Provider-neutral Runtime Adapter contract.

Not yet implemented: model-backed free-form language interpretation, production scheduler/session store, DeepSeek Harness API binding, compensation transactions and a UI.

## Quick review

Use Python 3.11 or later. The v3 compiler uses only the standard library.

```bash
python scripts/workflowctl.py verify-definition

python scripts/workflowctl.py generate-bpmn \
  examples/financial-event-monitor/business-requirement.json \
  --output build/financial-event-monitor/process.bpmn

python scripts/workflowctl.py compile \
  build/financial-event-monitor/process.bpmn \
  --business examples/financial-event-monitor/business-requirement.json \
  --catalog fixtures/catalog.snapshot.json \
  --output build/financial-event-monitor/package

python scripts/workflowctl.py validate \
  build/financial-event-monitor/package
```

Or run the full example:

```bash
python scripts/run_example.py
python -m unittest discover -s tests -v
```

## Repository ownership

Owned here:

- business-language contracts;
- BPMN generation and parsing;
- Workflow IR and Graph contracts;
- Agent/Loop/Policy compilation;
- Runtime adapters and capability negotiation;
- trajectory, resume, replay and evaluation contracts.

Not owned here:

- canonical Skill/Tool specifications;
- asset approval or retirement;
- Registry status mutation;
- runtime discovery from a moving Git branch.

See [`docs/architecture.md`](docs/architecture.md) and the shared system definition for review details.
