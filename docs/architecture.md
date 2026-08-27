# Architecture

## Stable boundary

BPMN is the business design source. Workflow IR is the stable executable contract. Graph, Agent Profile, LoopSpec and Policy manifests are derived artifacts. Harness Adapters translate those artifacts but cannot change their semantics or weaken policy.

## Cross-repository flow

```text
skill-registory
  registry YAML + source specifications
      → admission/governance
      → immutable catalog.snapshot.json
                              |
                              v
agent-workflow-factory
  business requirement → BPMN → IR → resolve catalog
                                     → registry.lock.json
                                     → graph + agents + loops + policy
                                     → adapter package
```

Resolution happens during compile/package. Runtime nodes never query Registry `main` and never float to a newer asset version.

## Trusted facts

Graph routing uses facts produced by deterministic validators, state providers or human decisions. Model outputs are candidate values until an evidence checker commits them as facts.

## Agent boundaries

BPMN lanes are responsibility hints. This implementation generates an Agent Profile only from an explicit `agent_ref` annotation supplied by the business contract or an approved override. A future responsibility partitioner may propose annotations, but cannot deploy them without review.

## Loop boundaries

A persistent loop must define intent, trigger, checker, maximum rounds, token budget, stop conditions and escalation. A BPMN back edge without these fields is a local graph cycle, not a persistent LoopSpec.

## Runtime boundary

Adapters must announce capabilities such as durable sessions, append-only events, human gates, scheduled loops and sandbox restrictions. Packaging fails when a required capability is unavailable; adapters cannot silently ignore it.
