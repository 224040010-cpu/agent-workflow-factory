from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any
import uuid

from .util import read_json, write_json


EXPRESSION = re.compile(
    r"^facts\.([A-Za-z0-9_.-]+)\s*(==|!=|>=|<=|>|<)\s*"
    r"(true|false|null|-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\")$"
)
BUDGET_LIMIT_KEYS = {
    "model_turns": "max_turns",
    "tokens": "max_tokens",
    "tool_calls": "max_tool_calls",
}


def deep_merge(target: dict, updates: dict) -> dict:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)
    return target


def fact_value(facts: dict, path: str) -> Any:
    current: Any = facts
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def literal_value(raw: str) -> Any:
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null":
        return None
    if raw.startswith(("'", '"')):
        return raw[1:-1]
    return float(raw) if "." in raw else int(raw)


def evaluate_expression(expression: str, facts: dict) -> bool:
    expression = expression.strip()
    if " or " in expression:
        return any(evaluate_expression(part, facts) for part in expression.split(" or "))
    if " and " in expression:
        return all(evaluate_expression(part, facts) for part in expression.split(" and "))
    match = EXPRESSION.match(expression)
    if not match:
        raise ValueError(f"Unsupported fact expression: {expression}")
    left = fact_value(facts, match.group(1))
    right = literal_value(match.group(3))
    operator = match.group(2)
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    if left is None:
        return False
    if operator == ">=":
        return left >= right
    if operator == "<=":
        return left <= right
    if operator == ">":
        return left > right
    return left < right


class JsonlEventStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.jsonl"

    def read(self, run_id: str) -> list[dict]:
        path = self.path(run_id)
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def append(self, run_id: str, event_type: str, payload: dict) -> dict:
        previous = self.read(run_id)
        prev_hash = previous[-1]["event_hash"] if previous else None
        event = {
            "run_id": run_id,
            "seq": len(previous) + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "prev_hash": prev_hash,
            "payload": payload,
        }
        canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        event["event_hash"] = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
        with self.path(run_id).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return event

    def verify(self, run_id: str) -> list[str]:
        errors: list[str] = []
        previous_hash = None
        for expected_seq, event in enumerate(self.read(run_id), start=1):
            if event.get("seq") != expected_seq:
                errors.append(f"event seq mismatch at {expected_seq}")
            if event.get("prev_hash") != previous_hash:
                errors.append(f"event prev_hash mismatch at {expected_seq}")
            supplied_hash = event.get("event_hash")
            material = {key: value for key, value in event.items() if key != "event_hash"}
            canonical = json.dumps(
                material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            actual_hash = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
            if supplied_hash != actual_hash:
                errors.append(f"event hash mismatch at {expected_seq}")
            previous_hash = supplied_hash
        return errors


@dataclass(frozen=True)
class Route:
    run_id: str
    node_id: str
    action: dict
    completion_evidence: list[str]
    status: str

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "node_id": self.node_id,
            "action": self.action,
            "completion_evidence": self.completion_evidence,
            "status": self.status,
        }


class ReferenceRuntime:
    AUTO_KINDS = {"start", "choice"}

    def __init__(self, package_dir: Path, runtime_dir: Path):
        self.package_dir = package_dir
        self.runtime_dir = runtime_dir
        self.graph = read_json(package_dir / "graph.json")
        self.workflow = read_json(package_dir / "workflow.ir.json")
        self.lock = read_json(package_dir / "registry.lock.json")
        self.policy = read_json(package_dir / "runtime.policy.json")
        self.events = JsonlEventStore(runtime_dir / "events")
        self.checkpoints = runtime_dir / "checkpoints"
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        self.nodes = {node["id"]: node for node in self.graph["spec"]["nodes"]}
        self.edges: dict[str, list[dict]] = {}
        for edge in self.graph["spec"]["edges"]:
            self.edges.setdefault(edge["from"], []).append(edge)

    def checkpoint_path(self, run_id: str) -> Path:
        return self.checkpoints / f"{run_id}.json"

    def load_state(self, run_id: str) -> dict:
        path = self.checkpoint_path(run_id)
        if not path.is_file():
            raise ValueError(f"Unknown run: {run_id}")
        return read_json(path)

    def persist(self, state: dict, event_type: str, payload: dict | None = None) -> None:
        state["state_version"] += 1
        self.events.append(state["run_id"], event_type, payload or {})
        self.events.append(state["run_id"], "state.checkpointed", {"state": state})
        write_json(self.checkpoint_path(state["run_id"]), state)

    def start(self, initial_facts: dict | None = None, run_id: str | None = None) -> dict:
        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        if self.checkpoint_path(run_id).exists():
            raise ValueError(f"Run already exists: {run_id}")
        state = {
            "run_id": run_id,
            "workflow_id": self.workflow["metadata"]["id"],
            "workflow_version": self.workflow["metadata"]["version"],
            "catalog_digest": self.lock["catalog_digest"],
            "status": "running",
            "current_node": self.graph["spec"]["entry"],
            "facts": copy.deepcopy(initial_facts or {}),
            "completed_nodes": [],
            "loop_rounds": 0,
            "budget_usage": {
                "total": {"model_turns": 0, "tokens": 0, "tool_calls": 0},
                "by_agent": {},
                "by_node": {},
            },
            "paused_from_status": None,
            "state_version": 0,
        }
        self.persist(state, "run.started", {"workflow_id": state["workflow_id"]})
        return state

    @staticmethod
    def budget_excess(
        state: dict,
        agent_id: str,
        delta: dict[str, int],
        limits: dict[str, int],
    ) -> list[str]:
        current = state["budget_usage"]["by_agent"].get(
            agent_id, {"model_turns": 0, "tokens": 0, "tool_calls": 0}
        )
        exceeded: list[str] = []
        for usage_key, limit_key in BUDGET_LIMIT_KEYS.items():
            amount = delta.get(usage_key, 0)
            limit = limits.get(limit_key)
            if not isinstance(amount, int) or amount < 0:
                raise ValueError(f"Invalid budget delta: {usage_key}")
            if not isinstance(limit, int) or limit < 0:
                raise ValueError(f"Invalid Agent budget: {limit_key}")
            if current.get(usage_key, 0) + amount > limit:
                exceeded.append(limit_key)
        return exceeded

    def record_budget(
        self,
        run_id: str,
        node_id: str,
        agent_id: str,
        delta: dict[str, int],
        limits: dict[str, int],
        phase: str,
    ) -> tuple[dict, list[str]]:
        state = self.load_state(run_id)
        exceeded = self.budget_excess(state, agent_id, delta, limits)
        usage = state["budget_usage"]
        empty = {"model_turns": 0, "tokens": 0, "tool_calls": 0}
        agent_usage = usage["by_agent"].setdefault(agent_id, copy.deepcopy(empty))
        node_usage = usage["by_node"].setdefault(node_id, copy.deepcopy(empty))
        for key in empty:
            amount = delta.get(key, 0)
            usage["total"][key] += amount
            agent_usage[key] += amount
            node_usage[key] += amount
        self.persist(
            state,
            "budget.consumed",
            {
                "node_id": node_id,
                "agent_id": agent_id,
                "phase": phase,
                "delta": delta,
                "agent_usage": copy.deepcopy(agent_usage),
                "limits": limits,
                "exceeded": exceeded,
            },
        )
        return state, exceeded

    def exhaust_budget(
        self,
        run_id: str,
        node_id: str,
        agent_id: str,
        attempted_delta: dict[str, int],
        limits: dict[str, int],
        exceeded: list[str],
    ) -> dict:
        state = self.load_state(run_id)
        if state["status"] not in {"completed", "cancelled"}:
            state["status"] = "escalated"
        self.persist(
            state,
            "budget.exhausted",
            {
                "node_id": node_id,
                "agent_id": agent_id,
                "attempted_delta": attempted_delta,
                "limits": limits,
                "exceeded": exceeded,
                "action": "escalate",
            },
        )
        return state

    def select_edge(self, state: dict, node_id: str) -> dict | None:
        candidates = self.edges.get(node_id, [])
        unconditional = []
        for edge in candidates:
            if edge.get("when"):
                if evaluate_expression(edge["when"], state["facts"]):
                    return edge
            else:
                unconditional.append(edge)
        if len(unconditional) == 1:
            return unconditional[0]
        if len(unconditional) > 1:
            raise ValueError(f"Ambiguous unconditional routes from {node_id}")
        return None

    def advance(self, state: dict, edge: dict) -> bool:
        target = edge["to"]
        if target in state["completed_nodes"]:
            state["loop_rounds"] += 1
            loop = self.workflow["spec"].get("loop") or {}
            maximum = loop.get("max_rounds")
            if maximum is not None and state["loop_rounds"] > maximum:
                state["status"] = "escalated"
                self.persist(
                    state,
                    "loop.budget_exhausted",
                    {"max_rounds": maximum, "escalation": loop.get("escalation")},
                )
                return False
        state["current_node"] = target
        self.persist(
            state,
            "route.selected",
            {"edge_id": edge["id"], "from": edge["from"], "to": target},
        )
        return True

    def route(self, run_id: str) -> Route:
        state = self.load_state(run_id)
        if state["status"] == "paused":
            return Route(run_id, state["current_node"], {}, [], "paused")
        if state["status"] in {"completed", "escalated", "cancelled"}:
            return Route(run_id, state["current_node"], {}, [], state["status"])

        while True:
            node = self.nodes[state["current_node"]]
            kind = node["action"]["kind"]
            if state["current_node"] in self.graph["spec"]["terminals"]:
                state["status"] = "completed"
                self.persist(state, "run.completed")
                return Route(run_id, state["current_node"], {}, [], "completed")
            if kind not in self.AUTO_KINDS:
                if state["status"] == "waiting_action":
                    return Route(
                        run_id,
                        node["id"],
                        node["action"],
                        node.get("completion_evidence", []),
                        "waiting_action",
                    )
                state["status"] = "waiting_action"
                self.persist(state, "node.ready", {"node_id": node["id"], "action": node["action"]})
                return Route(
                    run_id,
                    node["id"],
                    node["action"],
                    node.get("completion_evidence", []),
                    "waiting_action",
                )
            edge = self.select_edge(state, node["id"])
            if edge is None:
                state["status"] = "waiting_facts"
                self.persist(state, "route.waiting_facts", {"node_id": node["id"]})
                return Route(run_id, node["id"], {}, [], "waiting_facts")
            if not self.advance(state, edge):
                return Route(run_id, state["current_node"], {}, [], state["status"])

    def complete(self, run_id: str, node_id: str, fact_updates: dict) -> dict:
        state = self.load_state(run_id)
        if state["status"] != "waiting_action":
            raise ValueError(f"Run is not waiting for completion: {state['status']}")
        if state["current_node"] != node_id:
            raise ValueError(f"Current node is {state['current_node']}, not {node_id}")
        node = self.nodes[node_id]
        if node["action"]["kind"] in self.AUTO_KINDS:
            raise ValueError(f"Automatic node cannot be completed manually: {node_id}")
        candidate_facts = deep_merge(copy.deepcopy(state["facts"]), fact_updates)
        failed = [
            evidence
            for evidence in node.get("completion_evidence", [])
            if not evaluate_expression(evidence, candidate_facts)
        ]
        if failed:
            self.persist(state, "node.evidence_rejected", {"node_id": node_id, "failed": failed})
            raise ValueError(f"Completion evidence failed: {', '.join(failed)}")
        state["facts"] = candidate_facts
        if node_id not in state["completed_nodes"]:
            state["completed_nodes"].append(node_id)
        state["status"] = "running"
        self.persist(state, "node.completed", {"node_id": node_id, "fact_updates": fact_updates})
        edge = self.select_edge(state, node_id)
        if edge is not None:
            self.advance(state, edge)
        return state

    def pause(self, run_id: str, reason: str) -> dict:
        state = self.load_state(run_id)
        if state["status"] in {"completed", "escalated", "cancelled"}:
            raise ValueError(f"Terminal run cannot be paused: {state['status']}")
        if state["status"] == "paused":
            raise ValueError("Run is already paused")
        state["paused_from_status"] = state["status"]
        state["status"] = "paused"
        self.persist(state, "run.paused", {"reason": reason})
        return state

    def resume(self, run_id: str) -> dict:
        state = self.load_state(run_id)
        if state["status"] != "paused":
            raise ValueError(f"Only paused runs can resume; got {state['status']}")
        state["status"] = state.get("paused_from_status") or "running"
        state["paused_from_status"] = None
        self.persist(state, "run.resumed")
        return state

    def replay(self, run_id: str) -> dict:
        errors = self.events.verify(run_id)
        events = self.events.read(run_id)
        checkpoints = [
            event["payload"]["state"]
            for event in events
            if event["type"] == "state.checkpointed"
        ]
        if not checkpoints:
            errors.append("trajectory contains no state checkpoint")
            replayed = None
        else:
            replayed = checkpoints[-1]
            current = self.load_state(run_id)
            if replayed != current:
                errors.append("replayed state differs from checkpoint file")
        return {
            "run_id": run_id,
            "result": "PASS" if not errors else "FAIL",
            "events": len(events),
            "errors": errors,
            "state": replayed,
        }
