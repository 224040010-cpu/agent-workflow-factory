# Coordinated release

The shared definition is one logical contract with a canonical Registry copy and a workflow-factory mirror. Use this order when it changes:

1. Change `skill-registory/contracts/system-definition.json` and increment `definition_version`.
2. Refresh its checksum and merge the Registry change.
3. Synchronize the factory mirror with `scripts/sync_system_definition.py`.
4. Refresh or ingest a Catalog snapshot produced from the same definition version.
5. Merge the factory change after byte-for-byte canonical comparison passes.
6. Recompile affected workflows and review lockfile diffs.

Ordinary Skill/Tool additions do not require a system-definition change. They produce a new Catalog snapshot; each workflow chooses when to update its lockfile.

If either repository detects a definition mismatch, Catalog publication or workflow packaging is blocked. Runtime continues to use already-pinned packages and is not affected by Registry availability.
