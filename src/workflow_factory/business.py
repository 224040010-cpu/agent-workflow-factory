from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET

from .util import read_json


BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
WF_NS = "urn:skill-registry:workflow:v1"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

ET.register_namespace("bpmn", BPMN_NS)
ET.register_namespace("wf", WF_NS)
ET.register_namespace("xsi", XSI_NS)


def qname(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


STEP_TAGS = {
    "service": "serviceTask",
    "agent": "task",
    "human": "userTask",
    "rule": "businessRuleTask",
    "script": "scriptTask",
    "manual": "manualTask",
    "exclusive_gateway": "exclusiveGateway",
    "parallel_gateway": "parallelGateway",
    "end": "endEvent",
}


def validate_business_requirement(data: dict) -> list[str]:
    errors: list[str] = []
    required = {
        "schema_version",
        "workflow_id",
        "version",
        "name",
        "intent",
        "entry",
        "participants",
        "steps",
        "transitions",
    }
    missing = sorted(required - set(data))
    if missing:
        errors.append(f"missing fields: {', '.join(missing)}")
        return errors
    if data["schema_version"] != "1.0.0":
        errors.append("schema_version must be 1.0.0")

    participants = {item.get("id"): item for item in data.get("participants", [])}
    if None in participants or len(participants) != len(data.get("participants", [])):
        errors.append("participant ids must be present and unique")

    steps = {item.get("id"): item for item in data.get("steps", [])}
    if None in steps or len(steps) != len(data.get("steps", [])):
        errors.append("step ids must be present and unique")
    if data.get("entry") not in steps:
        errors.append("entry must reference a step id")

    for step_id, step in steps.items():
        kind = step.get("kind")
        if kind not in STEP_TAGS:
            errors.append(f"step {step_id} has unsupported kind: {kind}")
        if step.get("participant") not in participants:
            errors.append(f"step {step_id} references unknown participant")
        if kind == "service" and not step.get("tool_ref"):
            errors.append(f"service step {step_id} requires tool_ref")
        participant = participants.get(step.get("participant"), {})
        if kind == "agent" and not (step.get("agent_ref") or participant.get("agent_ref")):
            errors.append(f"agent step {step_id} requires explicit agent_ref")
        if kind not in {"exclusive_gateway", "parallel_gateway", "end"} and not step.get(
            "completion_evidence"
        ):
            errors.append(f"step {step_id} requires completion_evidence")

    for index, transition in enumerate(data.get("transitions", []), start=1):
        if transition.get("from") not in steps:
            errors.append(f"transition {index} has unknown source")
        if transition.get("to") not in steps:
            errors.append(f"transition {index} has unknown target")

    loop = data.get("loop")
    if loop:
        for field in (
            "intent",
            "trigger",
            "checker_ref",
            "max_rounds",
            "max_tokens_total",
            "stop_conditions",
            "escalation",
        ):
            if not loop.get(field):
                errors.append(f"loop requires {field}")
        if not isinstance(loop.get("max_rounds"), int) or loop.get("max_rounds", 0) < 1:
            errors.append("loop.max_rounds must be a positive integer")
    return errors


def render_bpmn(data: dict) -> bytes:
    errors = validate_business_requirement(data)
    if errors:
        raise ValueError("Invalid business requirement: " + "; ".join(errors))

    definitions = ET.Element(
        qname(BPMN_NS, "definitions"),
        {
            "id": f"Definitions_{data['workflow_id']}",
            "targetNamespace": f"urn:workflow:{data['workflow_id']}",
        },
    )
    process_attributes = {
        "id": data["workflow_id"],
        "name": data["name"],
        "isExecutable": "true",
        qname(WF_NS, "version"): data["version"],
        qname(WF_NS, "intent"): data["intent"],
        qname(WF_NS, "requiredSkills"): json.dumps(
            data.get("required_skills", []), ensure_ascii=False
        ),
    }
    if data.get("loop"):
        process_attributes[qname(WF_NS, "loopSpec")] = json.dumps(
            data["loop"], ensure_ascii=False, sort_keys=True
        )
    process = ET.SubElement(definitions, qname(BPMN_NS, "process"), process_attributes)

    participants = {item["id"]: item for item in data["participants"]}
    lane_set = ET.SubElement(process, qname(BPMN_NS, "laneSet"), {"id": "LaneSet_main"})
    for participant in data["participants"]:
        lane_attrs = {
            "id": participant["id"],
            "name": participant["name"],
            qname(WF_NS, "participantKind"): participant["kind"],
        }
        if participant.get("agent_ref"):
            lane_attrs[qname(WF_NS, "agentRef")] = participant["agent_ref"]
        lane = ET.SubElement(lane_set, qname(BPMN_NS, "lane"), lane_attrs)
        for step in data["steps"]:
            if step["participant"] == participant["id"]:
                ET.SubElement(lane, qname(BPMN_NS, "flowNodeRef")).text = step["id"]

    ET.SubElement(process, qname(BPMN_NS, "startEvent"), {"id": "start", "name": "Start"})
    step_elements: dict[str, ET.Element] = {}
    for step in data["steps"]:
        participant = participants[step["participant"]]
        attrs = {
            "id": step["id"],
            "name": step["name"],
            qname(WF_NS, "participantRef"): step["participant"],
            qname(WF_NS, "completionEvidence"): json.dumps(
                step.get("completion_evidence", []), ensure_ascii=False
            ),
            qname(WF_NS, "riskLevel"): step.get("risk_level", "L1"),
        }
        agent_ref = step.get("agent_ref") or participant.get("agent_ref")
        if agent_ref:
            attrs[qname(WF_NS, "agentRef")] = agent_ref
        if step.get("tool_ref"):
            attrs[qname(WF_NS, "toolRef")] = step["tool_ref"]
        step_elements[step["id"]] = ET.SubElement(
            process, qname(BPMN_NS, STEP_TAGS[step["kind"]]), attrs
        )

    flows = [{"from": "start", "to": data["entry"]}, *data["transitions"]]
    for index, transition in enumerate(flows, start=1):
        flow_id = f"Flow_{index:03d}"
        attrs = {
            "id": flow_id,
            "sourceRef": transition["from"],
            "targetRef": transition["to"],
        }
        flow = ET.SubElement(process, qname(BPMN_NS, "sequenceFlow"), attrs)
        if transition.get("condition"):
            condition = ET.SubElement(flow, qname(BPMN_NS, "conditionExpression"))
            condition.text = transition["condition"]
        if transition.get("default") and transition["from"] in step_elements:
            step_elements[transition["from"]].set("default", flow_id)

    ET.indent(definitions, space="  ")
    return ET.tostring(definitions, encoding="utf-8", xml_declaration=True)


def generate_bpmn(input_path: Path, output_path: Path) -> None:
    data = read_json(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(render_bpmn(data))
