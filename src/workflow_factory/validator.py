from __future__ import annotations

from pathlib import Path

from .util import read_json


ACTION_KINDS_WITH_EVIDENCE = {
    "tool_task",
    "agent_task",
    "human_gate",
    "rule_task",
    "script_task",
    "manual_task",
    "subworkflow",
}


def validate_ir(ir: dict) -> list[str]:
    errors: list[str] = []
    if ir.get("api_version") != "workflow.skill-registry/v1alpha1":
        errors.append("unsupported Workflow IR api_version")
    spec = ir.get("spec", {})
    nodes = {node.get("id"): node for node in spec.get("nodes", [])}
    if None in nodes or len(nodes) != len(spec.get("nodes", [])):
        errors.append("node ids must be present and unique")
    if spec.get("entry") not in nodes:
        errors.append("entry node is missing")
    terminals = set(spec.get("terminals", []))
    if not terminals or not terminals.issubset(nodes):
        errors.append("one or more terminal nodes are missing")

    adjacency: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    for edge in spec.get("edges", []):
        source, target = edge.get("from"), edge.get("to")
        if source not in nodes or target not in nodes:
            errors.append(f"edge {edge.get('id')} references unknown node")
            continue
        adjacency[source].add(target)

    reachable: set[str] = set()
    stack = [spec.get("entry")]
    while stack:
        current = stack.pop()
        if current in reachable or current not in adjacency:
            continue
        reachable.add(current)
        stack.extend(adjacency[current] - reachable)
    unreachable = sorted(set(nodes) - reachable)
    if unreachable:
        errors.append(f"unreachable nodes: {', '.join(unreachable)}")
    if terminals and not terminals.intersection(reachable):
        errors.append("no terminal is reachable from entry")

    for node in nodes.values():
        if node.get("kind") in ACTION_KINDS_WITH_EVIDENCE and not node.get(
            "completion_evidence"
        ):
            errors.append(f"node {node.get('id')} requires completion evidence")
        if node.get("kind") == "agent_task" and not node.get("agent_ref"):
            errors.append(f"agent task {node.get('id')} requires explicit agent_ref")
        if node.get("kind") == "tool_task" and not node.get("tool_ref"):
            errors.append(f"tool task {node.get('id')} requires tool_ref")

    loop = spec.get("loop")
    if loop:
        for field in (
            "intent",
            "checker_ref",
            "max_rounds",
            "max_tokens_total",
            "stop_conditions",
            "escalation",
        ):
            if not loop.get(field):
                errors.append(f"persistent loop requires {field}")
    return errors


def validate_package(package_dir: Path) -> list[str]:
    errors: list[str] = []
    required = (
        "workflow.ir.json",
        "graph.json",
        "registry.lock.json",
        "runtime.policy.json",
        "compile-report.json",
    )
    for name in required:
        if not (package_dir / name).is_file():
            errors.append(f"missing package artifact: {name}")
    if errors:
        return errors

    ir = read_json(package_dir / "workflow.ir.json")
    errors.extend(validate_ir(ir))
    lock = read_json(package_dir / "registry.lock.json")
    if not lock.get("catalog_digest"):
        errors.append("registry.lock.json is missing catalog_digest")
    if not lock.get("system_definition_version"):
        errors.append("registry.lock.json is missing system_definition_version")
    for asset in lock.get("resolved_assets", []):
        for field in ("type", "name", "version", "status", "risk_level", "digest"):
            if not asset.get(field):
                errors.append(f"locked asset {asset.get('name')} is missing {field}")
        if asset.get("status") not in {"approved", "restricted"}:
            errors.append(f"locked asset {asset.get('name')} has ineligible status")

    loop = ir.get("spec", {}).get("loop")
    if loop and not list((package_dir / "loops").glob("*.loop.json")):
        errors.append("Workflow IR has a loop but package contains no LoopSpec")
    return errors
