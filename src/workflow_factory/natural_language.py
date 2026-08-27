from __future__ import annotations

import hashlib
import re
from typing import Any

from .business import validate_business_requirement


FIELD_ALIASES = {
    "name": ("流程名称", "业务流程名称", "名称"),
    "intent": ("流程目标", "业务目标", "目标", "目的"),
    "participants": ("参与者", "参与角色", "角色"),
    "steps": ("流程步骤", "处理步骤", "步骤", "流程"),
}

KIND_ALIASES = {
    "人工": "human",
    "人员": "human",
    "用户": "human",
    "系统": "system",
    "平台": "system",
    "agent": "agent",
    "智能体": "agent",
    "外部": "external",
}


def _extract_field(text: str, aliases: tuple[str, ...]) -> str | None:
    label = "|".join(re.escape(item) for item in aliases)
    match = re.search(rf"(?m)^\s*(?:{label})\s*[：:]\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else None


def _strip_step_prefix(value: str) -> str:
    return re.sub(r"^\s*(?:第?[一二三四五六七八九十百\d]+[步、.．)：:]?|[-*])\s*", "", value).strip()


def _split_step_text(value: str) -> list[str]:
    value = value.replace("\r", "\n")
    parts = re.split(r"\n+|\s*(?:→|->)\s*|[；;]+|(?:，|,)\s*(?:然后|接着|随后|之后|最后)", value)
    return [item for item in (_strip_step_prefix(part) for part in parts) if item]


def _step_section(text: str) -> list[str]:
    lines = text.replace("\r", "").split("\n")
    start = None
    values: list[str] = []
    aliases = FIELD_ALIASES["steps"]
    for index, line in enumerate(lines):
        match = re.match(
            rf"^\s*(?:{'|'.join(re.escape(item) for item in aliases)})\s*[：:]\s*(.*)$",
            line,
        )
        if match:
            start = index
            if match.group(1).strip():
                values.append(match.group(1).strip())
            break
    if start is None:
        return []
    for line in lines[start + 1 :]:
        if re.match(r"^\s*(?:流程名称|业务流程名称|名称|流程目标|业务目标|目标|目的|参与者|参与角色|角色)\s*[：:]", line):
            break
        if line.strip():
            values.append(line.strip())
    return _split_step_text("\n".join(values))


def _participant_kind(name: str, declared: str | None = None) -> str:
    candidate = (declared or "").strip().lower()
    for label, kind in KIND_ALIASES.items():
        if label in candidate:
            return kind
    if any(word in name.lower() for word in ("系统", "平台", "服务")):
        return "system"
    if "agent" in name.lower() or "智能体" in name:
        return "agent"
    return "human"


def _parse_participants(raw: str | None) -> list[dict]:
    if not raw:
        return []
    participants = []
    for item in re.split(r"[、,，;；]+", raw):
        item = item.strip()
        if not item:
            continue
        match = re.match(r"^(.*?)(?:[（(]([^）)]+)[）)])?$", item)
        if not match:
            continue
        name = match.group(1).strip()
        kind = _participant_kind(name, match.group(2))
        participant = {
            "id": f"lane-{len(participants) + 1:03d}",
            "name": name,
            "kind": kind,
        }
        if kind == "agent":
            participant["agent_ref"] = f"business-agent-{len(participants) + 1:03d}"
        participants.append(participant)
    return participants


def _is_terminal(value: str) -> bool:
    cleaned = value.strip("。.!！ ")
    return cleaned in {"结束", "完成", "流程结束", "流程完成", "业务完成"}


def _parse_actor_action(value: str, actor_names: list[str]) -> tuple[str | None, str]:
    value = re.sub(r"^(?:首先|然后|接着|随后|之后|最后)\s*", "", value.strip())
    if "：" in value or ":" in value:
        actor, action = re.split(r"[：:]", value, maxsplit=1)
        return actor.strip(), action.strip("。.!！ ")
    delegated = re.match(r"^由(.+?)(?:负责|执行|进行)(.+)$", value)
    if delegated:
        return delegated.group(1).strip(), delegated.group(2).strip("。.!！ ")
    for actor in sorted(actor_names, key=len, reverse=True):
        if value.startswith(actor):
            action = re.sub(r"^(?:负责|执行|进行)", "", value[len(actor) :]).strip()
            return actor, action.strip("。.!！ ")
    return None, value.strip("。.!！ ")


def _branch_parts(value: str) -> tuple[str, str, str] | None:
    match = re.match(
        r"^(?:如果|若)(.+?)(?:，|,)?(?:则|那么)(.+?)(?:，|,)(?:否则|不然|未通过时)(.+)$",
        value.strip("。.!！ "),
    )
    if not match:
        return None
    return tuple(part.strip() for part in match.groups())


def interpret_business_text(
    text: str,
    workflow_id: str | None = None,
    version: str = "1.0.0",
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not text.strip():
        raise ValueError("自然语言业务描述不能为空")
    source_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    workflow_id = workflow_id or f"workflow-{source_digest[:10]}"
    if not re.fullmatch(r"[a-z][a-z0-9-]+", workflow_id):
        raise ValueError("workflow_id 必须使用小写字母、数字和连字符")

    name = _extract_field(text, FIELD_ALIASES["name"])
    intent = _extract_field(text, FIELD_ALIASES["intent"])
    participants = _parse_participants(_extract_field(text, FIELD_ALIASES["participants"]))
    clauses = _step_section(text)
    if not clauses:
        raise ValueError("未识别到流程步骤；请使用“流程步骤：”或“流程：”描述处理顺序")

    warnings: list[str] = []
    assumptions: list[str] = []
    if not name:
        name = "业务工作流程"
        warnings.append("未提供流程名称，已使用默认名称")
    if not intent:
        intent = f"按照业务描述完成{name}。"
        warnings.append("未提供流程目标，已根据流程名称生成目标")

    participant_by_name = {item["name"]: item for item in participants}

    def ensure_participant(actor: str | None) -> dict:
        actor = actor or (participants[0]["name"] if participants else "业务人员")
        if actor not in participant_by_name:
            kind = _participant_kind(actor)
            participant = {
                "id": f"lane-{len(participants) + 1:03d}",
                "name": actor,
                "kind": kind,
            }
            if kind == "agent":
                participant["agent_ref"] = f"business-agent-{len(participants) + 1:03d}"
            participants.append(participant)
            participant_by_name[actor] = participant
            warnings.append(f"参与者“{actor}”未在参与者清单中声明，已推断为{kind}")
        return participant_by_name[actor]

    steps: list[dict] = []
    transitions: list[dict] = []
    previous: str | None = None
    action_counter = 0
    decision_counter = 0

    def add_action(actor: str | None, action: str) -> str:
        nonlocal action_counter
        if not action:
            raise ValueError("流程动作不能为空")
        participant = ensure_participant(actor)
        action_counter += 1
        step_id = f"step-{action_counter:03d}"
        kind = {
            "human": "human",
            "agent": "agent",
            "system": "script",
            "external": "manual",
        }[participant["kind"]]
        step = {
            "id": step_id,
            "name": action,
            "kind": kind,
            "participant": participant["id"],
            "completion_evidence": [f"facts.completed.{step_id} == true"],
            "risk_level": "L1",
        }
        if kind == "agent":
            step["agent_ref"] = participant["agent_ref"]
        steps.append(step)
        return step_id

    for clause in clauses:
        if _is_terminal(clause):
            continue
        branch = _branch_parts(clause)
        if branch:
            if previous is None:
                raise ValueError("条件分支前必须至少有一个流程动作")
            condition, yes_text, no_text = branch
            decision_counter += 1
            gateway_id = f"decision-{decision_counter:03d}"
            gateway_participant = steps[-1]["participant"]
            steps.append(
                {
                    "id": gateway_id,
                    "name": condition,
                    "kind": "exclusive_gateway",
                    "participant": gateway_participant,
                    "completion_evidence": [],
                    "risk_level": "L0",
                }
            )
            transitions.append({"from": previous, "to": gateway_id})
            yes_actor, yes_action = _parse_actor_action(yes_text, list(participant_by_name))
            no_actor, no_action = _parse_actor_action(no_text, list(participant_by_name))
            yes_id = add_action(yes_actor, yes_action)
            no_id = add_action(no_actor, no_action)
            merge_id = f"merge-{decision_counter:03d}"
            steps.append(
                {
                    "id": merge_id,
                    "name": "分支汇合",
                    "kind": "exclusive_gateway",
                    "participant": gateway_participant,
                    "completion_evidence": [],
                    "risk_level": "L0",
                }
            )
            fact_path = f"facts.decisions.{gateway_id}"
            transitions.extend(
                [
                    {"from": gateway_id, "to": yes_id, "condition": f"{fact_path} == true"},
                    {
                        "from": gateway_id,
                        "to": no_id,
                        "condition": f"{fact_path} == false",
                        "default": True,
                    },
                    {"from": yes_id, "to": merge_id},
                    {"from": no_id, "to": merge_id},
                ]
            )
            previous = merge_id
            assumptions.append(f"条件“{condition}”映射为布尔事实 {fact_path}")
            continue

        actor, action = _parse_actor_action(clause, list(participant_by_name))
        step_id = add_action(actor, action)
        if previous:
            transitions.append({"from": previous, "to": step_id})
        previous = step_id

    if previous is None:
        raise ValueError("自然语言描述中没有可执行的业务动作")
    terminal_participant = steps[-1]["participant"]
    terminal_id = "completed"
    steps.append(
        {
            "id": terminal_id,
            "name": "流程完成",
            "kind": "end",
            "participant": terminal_participant,
            "completion_evidence": [],
            "risk_level": "L0",
        }
    )
    transitions.append({"from": previous, "to": terminal_id})

    requirement = {
        "$schema": "../../schemas/business-requirement.schema.json",
        "schema_version": "1.0.0",
        "workflow_id": workflow_id,
        "version": version,
        "name": name,
        "intent": intent,
        "entry": steps[0]["id"],
        "required_skills": [
            "converting-business-to-bpmn",
            "decomposing-business-process",
            "validating-bpmn-compliance",
        ],
        "participants": participants,
        "steps": steps,
        "transitions": transitions,
    }
    errors = validate_business_requirement(requirement)
    if errors:
        raise ValueError("自然语言解释结果不合法：" + "; ".join(errors))

    confidence = max(0.55, 0.98 - len(warnings) * 0.08 - len(assumptions) * 0.03)
    report = {
        "schema_version": "1.0.0",
        "interpreter": "controlled-chinese-business-language/v1",
        "source_digest": f"sha256:{source_digest}",
        "workflow_id": workflow_id,
        "confidence": round(confidence, 2),
        "review_required": True,
        "warnings": warnings,
        "assumptions": assumptions,
        "recognized": {
            "participants": len(participants),
            "actions": action_counter,
            "decisions": decision_counter,
        },
    }
    return requirement, report
