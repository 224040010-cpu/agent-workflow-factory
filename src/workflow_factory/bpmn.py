from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET

from . import __version__
from .business import BPMN_NS, WF_NS, qname
from .util import sha256_file


NODE_KINDS = {
    "startEvent": "start",
    "endEvent": "terminal",
    "serviceTask": "tool_task",
    "task": "agent_task",
    "userTask": "human_gate",
    "businessRuleTask": "rule_task",
    "scriptTask": "script_task",
    "manualTask": "manual_task",
    "exclusiveGateway": "choice",
    "parallelGateway": "parallel",
    "inclusiveGateway": "inclusive_choice",
    "eventBasedGateway": "event_choice",
    "callActivity": "subworkflow",
}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_json_attribute(element: ET.Element, name: str, default):
    raw = element.get(qname(WF_NS, name))
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid wf:{name} JSON on {element.get('id')}: {exc}") from exc


def parse_bpmn(path: Path) -> dict:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"Invalid BPMN XML: {exc}") from exc
    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        raise ValueError("BPMN document has no process")

    lane_membership: dict[str, dict] = {}
    participants: list[dict] = []
    for lane in process.findall(f".//{{{BPMN_NS}}}lane"):
        participant = {
            "id": lane.get("id"),
            "name": lane.get("name", lane.get("id", "")),
            "kind": lane.get(qname(WF_NS, "participantKind"), "unknown"),
            "agent_ref": lane.get(qname(WF_NS, "agentRef")),
        }
        participants.append(participant)
        for ref in lane.findall(f"{{{BPMN_NS}}}flowNodeRef"):
            if ref.text:
                lane_membership[ref.text] = participant

    nodes: list[dict] = []
    node_ids: set[str] = set()
    for element in list(process):
        tag = local_name(element.tag)
        if tag not in NODE_KINDS:
            continue
        node_id = element.get("id")
        if not node_id:
            raise ValueError(f"BPMN {tag} is missing id")
        if node_id in node_ids:
            raise ValueError(f"Duplicate BPMN node id: {node_id}")
        node_ids.add(node_id)
        participant = lane_membership.get(node_id, {})
        node = {
            "id": node_id,
            "kind": NODE_KINDS[tag],
            "name": element.get("name", node_id),
            "source_ref": node_id,
            "participant_ref": element.get(qname(WF_NS, "participantRef"))
            or participant.get("id"),
            "agent_ref": element.get(qname(WF_NS, "agentRef"))
            or participant.get("agent_ref"),
            "tool_ref": element.get(qname(WF_NS, "toolRef")),
            "risk_level": element.get(qname(WF_NS, "riskLevel"), "L1"),
            "completion_evidence": parse_json_attribute(
                element, "completionEvidence", []
            ),
        }
        nodes.append(node)

    edges: list[dict] = []
    for flow in process.findall(f"{{{BPMN_NS}}}sequenceFlow"):
        source = flow.get("sourceRef")
        target = flow.get("targetRef")
        if source not in node_ids or target not in node_ids:
            raise ValueError(
                f"Sequence flow {flow.get('id')} references an unknown node: {source} -> {target}"
            )
        condition = flow.find(f"{{{BPMN_NS}}}conditionExpression")
        edge = {
            "id": flow.get("id"),
            "from": source,
            "to": target,
        }
        if condition is not None and condition.text:
            edge["when"] = condition.text.strip()
            edge["condition_language"] = "fact-expression/v1"
        edges.append(edge)

    starts = [node["id"] for node in nodes if node["kind"] == "start"]
    terminals = [node["id"] for node in nodes if node["kind"] == "terminal"]
    if len(starts) != 1:
        raise ValueError(f"Exactly one start event is required; found {len(starts)}")
    if not terminals:
        raise ValueError("At least one end event is required")

    loop = parse_json_attribute(process, "loopSpec", None)
    return {
        "api_version": "workflow.skill-registry/v1alpha1",
        "kind": "Workflow",
        "metadata": {
            "id": process.get("id"),
            "version": process.get(qname(WF_NS, "version"), "0.0.0"),
            "source": {"type": "bpmn", "path": str(path.as_posix())},
            "source_digest": sha256_file(path),
            "compiler_version": __version__,
        },
        "spec": {
            "intent": process.get(qname(WF_NS, "intent"), process.get("name", "")),
            "entry": starts[0],
            "terminals": terminals,
            "participants": participants,
            "required_skills": parse_json_attribute(process, "requiredSkills", []),
            "nodes": nodes,
            "edges": edges,
            "loop": loop,
            "invariants": [
                "terminal_requires_verified_completion_evidence",
                "external_side_effect_requires_policy_allow",
                "persistent_loop_requires_finite_budget",
            ],
        },
    }
