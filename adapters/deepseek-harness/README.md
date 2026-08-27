# DeepSeek Harness adapter boundary

This experimental adapter maps the provider-neutral package to DeepSeek Harness plugins and presets. It intentionally contains no copied Harness core and does not expose DeepSeek-specific types to Workflow IR.

Before implementing a deployment binding:

1. pin one tested DeepSeek Harness version;
2. map Agent Profiles to provider/preset configuration;
3. map resolved Registry assets to Skill/Tool plugins;
4. map LoopSpec to loop and scheduler plugins;
5. map Harness session events into the common append-only trajectory schema;
6. run capability negotiation and reject unsupported required features;
7. add adapter contract tests for start, resume, interrupt, loop stop and human approval.

DeepSeek Harness is a developer preview. Compatibility changes belong inside this directory and must not change Workflow IR or the shared system definition without a separate architecture decision.
