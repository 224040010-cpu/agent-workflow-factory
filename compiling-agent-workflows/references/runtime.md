# Runtime proof checklist

Use the Reference Runtime when a review requires evidence beyond static compilation.

1. Start a run against one immutable compiled package.
2. Route until an action node is returned.
3. Execute the pinned Tool or Agent through an Adapter.
4. Convert verified Adapter output into structured fact updates.
5. Complete the node and require all completion evidence to pass.
6. Pause and resume at least once for durable-session workflows.
7. Exercise a rejection path for workflows with a LoopSpec.
8. Replay the trajectory and require `result: PASS`.

Never turn free-form model output directly into trusted facts. Validate shape, provenance and policy first. A failed replay, missing checkpoint or exhausted loop budget is a hard review failure, not a warning.
