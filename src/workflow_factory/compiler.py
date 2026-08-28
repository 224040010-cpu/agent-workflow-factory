from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .bpmn import parse_bpmn
from .catalog import resolve_catalog
from .signing import sign_artifact
from .util import read_json, risk_number, write_json


def build_graph(ir: dict) -> dict:
    nodes = []
    for node in ir["spec"]["nodes"]:
        action = {"kind": node["kind"]}
        if node.get("agent_ref"):
            action["agent_ref"] = node["agent_ref"]
        if node.get("tool_ref"):
            action["tool_ref"] = node["tool_ref"]
        nodes.append(
            {
                "id": node["id"],
                "name": node["name"],
                "action": action,
                "required_facts": [],
                "completion_evidence": node["completion_evidence"],
                "source_ref": node["source_ref"],
            }
        )
    edges = []
    for edge in ir["spec"]["edges"]:
        route = {"id": edge["id"], "from": edge["from"], "to": edge["to"]}
        if edge.get("when"):
            route["when"] = edge["when"]
            route["reason_code"] = f"CONDITION_{edge['id'].upper()}"
        else:
            route["reason_code"] = f"ROUTE_{edge['id'].upper()}"
        edges.append(route)
    return {
        "api_version": "graph.skill-registry/v1alpha1",
        "kind": "AgentGraph",
        "metadata": {
            "id": ir["metadata"]["id"],
            "version": ir["metadata"]["version"],
            "workflow_ir_digest": ir["metadata"]["source_digest"],
        },
        "spec": {
            "entry": ir["spec"]["entry"],
            "terminals": ir["spec"]["terminals"],
            "routing_basis": "trusted-facts-and-completion-evidence",
            "nodes": nodes,
            "edges": edges,
        },
    }


def build_agent_profiles(ir: dict, resolved_tools: list[dict]) -> list[dict]:
    tool_by_name = {item["name"]: item for item in resolved_tools}
    agent_nodes: dict[str, list[dict]] = defaultdict(list)
    for node in ir["spec"]["nodes"]:
        if node.get("agent_ref"):
            agent_nodes[node["agent_ref"]].append(node)
    loop = ir["spec"].get("loop")
    if loop and loop.get("checker_ref"):
        agent_nodes.setdefault(loop["checker_ref"], [])

    profiles: list[dict] = []
    for agent_ref, nodes in sorted(agent_nodes.items()):
        tools = sorted({node["tool_ref"] for node in nodes if node.get("tool_ref")})
        max_risk = max(
            [risk_number(node.get("risk_level", "L1")) for node in nodes]
            + [risk_number(tool_by_name[name]["risk_level"]) for name in tools]
            + [1]
        )
        purpose = "; ".join(node["name"] for node in nodes) or "Verify workflow evidence"
        profiles.append(
            {
                "api_version": "agent.skill-registry/v1alpha1",
                "kind": "AgentProfile",
                "metadata": {"id": agent_ref, "version": ir["metadata"]["version"]},
                "spec": {
                    "purpose": purpose,
                    "model_policy": {
                        "capability_class": "complex_reasoning",
                        "required_features": ["structured_output", "tool_calling"],
                    },
                    "skills": sorted(ir["spec"].get("required_skills", [])),
                    "tools": tools,
                    "context": {"strategy": "route_scoped", "include_full_graph": False},
                    "memory": {
                        "working": "workflow_state",
                        "durable_write": "checker_verified_only",
                    },
                    "permissions": {
                        "external_write": "deny" if max_risk < 3 else "approval_required",
                        "requires_human_approval": max_risk >= 3,
                        "effective_risk_level": f"L{max_risk}",
                    },
                    "budgets": {
                        "max_turns": 12,
                        "max_tokens": 60000,
                        "max_tool_calls": 30,
                    },
                    "output_policy": {
                        "structured": True,
                        "completion_evidence_required": True,
                    },
                },
            }
        )
    return profiles


def build_loop_spec(ir: dict) -> dict | None:
    loop = ir["spec"].get("loop")
    if not loop:
        return None
    return {
        "api_version": "loop.skill-registry/v1alpha1",
        "kind": "Loop",
        "metadata": {
            "id": f"{ir['metadata']['id']}-loop",
            "version": ir["metadata"]["version"],
        },
        "spec": {
            "intent": loop["intent"],
            "trigger": loop["trigger"],
            "graph_ref": f"{ir['metadata']['id']}@{ir['metadata']['version']}",
            "checker_ref": loop["checker_ref"],
            "budgets": {
                "max_rounds": loop["max_rounds"],
                "max_tokens_total": loop["max_tokens_total"],
            },
            "stop_conditions": loop["stop_conditions"],
            "no_change_policy": loop.get(
                "no_change_policy",
                {"rounds_before_backoff": 5, "backoff": "increase_interval"},
            ),
            "escalation": loop["escalation"],
        },
    }


def compile_package(
    bpmn_path: Path,
    business_path: Path,
    catalog_path: Path,
    definition_path: Path,
    output_dir: Path,
    signing_key_path: Path | None = None,
    signing_publisher: str = "agent-workflow-factory-build",
) -> dict:
    business = read_json(business_path)
    definition = read_json(definition_path)
    ir = parse_bpmn(bpmn_path)
    if ir["metadata"]["id"] != business.get("workflow_id"):
        raise ValueError("BPMN process id differs from business workflow_id")
    if ir["metadata"]["version"] != business.get("version"):
        raise ValueError("BPMN workflow version differs from business version")

    required_tools = sorted(
        {node["tool_ref"] for node in ir["spec"]["nodes"] if node.get("tool_ref")}
    )
    resolved = resolve_catalog(
        catalog_path,
        required_skills=ir["spec"].get("required_skills", []),
        required_tools=required_tools,
        expected_definition_version=definition["definition_version"],
    )

    graph = build_graph(ir)
    profiles = build_agent_profiles(ir, resolved.tools)
    loop_spec = build_loop_spec(ir)
    has_human_gate = any(node["kind"] == "human_gate" for node in ir["spec"]["nodes"])
    max_risk = max(
        [risk_number(asset["risk_level"]) for asset in resolved.skills + resolved.tools] + [0]
    )
    policy = {
        "api_version": "policy.skill-registry/v1alpha1",
        "kind": "RuntimePolicy",
        "metadata": {"id": ir["metadata"]["id"], "version": ir["metadata"]["version"]},
        "spec": {
            "effective_risk_level": f"L{max_risk}",
            "human_approval_required": max_risk >= 3,
            "runtime_requirements": {
                "durable_sessions": "required" if loop_spec else "optional",
                "append_only_events": "required",
                "human_gate": "required" if has_human_gate else "optional",
                "scheduled_loops": "required" if loop_spec else "optional",
                "sandbox_network_allowlist": "required",
            },
        },
    }

    write_json(output_dir / "workflow.ir.json", ir)
    write_json(output_dir / "graph.json", graph)
    lock_path = output_dir / "registry.lock.json"
    write_json(lock_path, resolved.lockfile())
    if signing_key_path is not None:
        sign_artifact(
            lock_path,
            signing_key_path,
            output_dir / "registry.lock.sig.json",
            signing_publisher,
        )
    write_json(output_dir / "runtime.policy.json", policy)
    for profile in profiles:
        write_json(output_dir / "agents" / f"{profile['metadata']['id']}.agent.json", profile)
    if loop_spec:
        write_json(output_dir / "loops" / f"{loop_spec['metadata']['id']}.loop.json", loop_spec)

    report = {
        "result": "PASS",
        "workflow_id": ir["metadata"]["id"],
        "workflow_version": ir["metadata"]["version"],
        "system_definition_version": definition["definition_version"],
        "resolved_skills": len(resolved.skills),
        "resolved_tools": len(resolved.tools),
        "generated_agents": len(profiles),
        "generated_loops": 1 if loop_spec else 0,
        "registry_lock_signed": signing_key_path is not None,
        "warnings": [
            f"Node {node['id']} has no explicit Agent; host executes its action."
            for node in ir["spec"]["nodes"]
            if node["kind"] not in {"start", "terminal", "choice", "parallel"}
            and not node.get("agent_ref")
        ],
    }
    write_json(output_dir / "compile-report.json", report)
    return report
