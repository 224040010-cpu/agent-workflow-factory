from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any
import uuid

from .signing import (
    SigningProvider,
    sign_artifact_with_provider,
    sign_json_value,
    verify_artifact,
    verify_json_value,
    verify_trust_store,
)
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
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
NON_STATE_AUDIT_EVENTS = {
    "adapter.capabilities.accepted",
    "artifact.signatures.accepted",
    "tool.observation.accepted",
    "agent.turn.completed",
    "facts.verified",
    "adapter.execution.interrupted",
}


@dataclass(frozen=True)
class RuntimeIntegrityPolicy:
    signing_provider: SigningProvider | None = None
    trust_store: Path | None = None
    trust_store_signature: Path | None = None
    trust_root_public_key: Path | None = None
    publisher: str = "agent-workflow-factory-runtime"
    require_signatures: bool = False
    require_rooted_trust: bool = False

    def require_write_signer(self) -> SigningProvider:
        if self.signing_provider is None:
            raise ValueError("Signed runtime mutation requires a runtime signing provider")
        trust_store = read_json(self.require_verifier())
        keys = trust_store.get("keys") if isinstance(trust_store, dict) else None
        if not isinstance(keys, list):
            raise ValueError("Runtime trust store has an invalid shape")
        matching = [
            item
            for item in keys
            if isinstance(item, dict)
            and item.get("key_id") == self.signing_provider.key_id
            and item.get("publisher") == self.publisher
        ]
        if not matching:
            raise ValueError(
                "Runtime signing key is not registered for the required publisher"
            )
        if matching[0].get("status") != "active":
            raise ValueError("New runtime evidence requires an active signing key")
        return self.signing_provider

    def require_verifier(self) -> Path:
        if self.trust_store is None:
            raise ValueError("Signed runtime verification requires a trust store")
        return self.trust_store


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
    def __init__(
        self,
        root: Path,
        integrity: RuntimeIntegrityPolicy | None = None,
    ):
        self.root = root
        self.integrity = integrity or RuntimeIntegrityPolicy()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("run_id must contain only letters, numbers, dot, underscore or dash")
        return self.root / f"{run_id}.jsonl"

    def read(self, run_id: str) -> list[dict]:
        path = self.path(run_id)
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def _build_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict,
        previous: list[dict],
    ) -> dict:
        provider = self.integrity.signing_provider
        if self.integrity.require_signatures or provider is not None:
            provider = self.integrity.require_write_signer()
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
        if provider is not None:
            event["signature"] = sign_json_value(
                event,
                f"{run_id}.{event['seq']}.event.json",
                provider,
                self.integrity.publisher,
            )
        return event

    def append(self, run_id: str, event_type: str, payload: dict) -> dict:
        previous = self.read(run_id)
        event = self._build_event(run_id, event_type, payload, previous)
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
            material.pop("signature", None)
            canonical = json.dumps(
                material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            actual_hash = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
            if supplied_hash != actual_hash:
                errors.append(f"event hash mismatch at {expected_seq}")
            signature = event.get("signature")
            if signature is None and self.integrity.require_signatures:
                errors.append(f"event signature missing at {expected_seq}")
            elif signature is not None:
                if self.integrity.trust_store is None:
                    errors.append(f"event signature trust store missing at {expected_seq}")
                else:
                    signed_value = {
                        key: value for key, value in event.items() if key != "signature"
                    }
                    try:
                        verify_json_value(
                            signed_value,
                            signature,
                            f"{run_id}.{expected_seq}.event.json",
                            self.integrity.trust_store,
                            self.integrity.publisher,
                        )
                    except (OSError, ValueError, KeyError) as exc:
                        errors.append(f"event signature invalid at {expected_seq}: {exc}")
            previous_hash = supplied_hash
        return errors


class SqliteEventStore(JsonlEventStore):
    """Transactional event store with optional leases and terminal-run retention."""

    def __init__(
        self,
        root: Path,
        integrity: RuntimeIntegrityPolicy | None = None,
        lease_owner: str | None = None,
        lease_ttl_seconds: int = 30,
        retention_days: int = 90,
        checkpoint_root: Path | None = None,
    ):
        if lease_ttl_seconds < 1:
            raise ValueError("lease_ttl_seconds must be positive")
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        super().__init__(root, integrity)
        self.database = root / "runtime-events.sqlite3"
        self.lease_owner = lease_owner
        self.lease_ttl_seconds = lease_ttl_seconds
        self.retention_days = retention_days
        self.checkpoint_root = checkpoint_root
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back the transaction, then release the file handle."""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (run_id, seq)
                );
                CREATE TABLE IF NOT EXISTS leases (
                    run_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    terminal INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                """
            )

    def path(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("run_id must contain only letters, numbers, dot, underscore or dash")
        return self.database

    def read(self, run_id: str) -> list[dict]:
        self.path(run_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT event_json FROM events WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _assert_lease(self, connection: sqlite3.Connection, run_id: str) -> None:
        if self.lease_owner is None:
            return
        row = connection.execute(
            "SELECT owner, expires_at FROM leases WHERE run_id = ?", (run_id,)
        ).fetchone()
        now = time.time()
        if row is None or row[0] != self.lease_owner or row[1] <= now:
            raise ValueError(f"No active runtime lease for {run_id}")

    def append(self, run_id: str, event_type: str, payload: dict) -> dict:
        self.path(run_id)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, run_id)
            rows = connection.execute(
                "SELECT event_json FROM events WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
            previous = [json.loads(row[0]) for row in rows]
            event = self._build_event(run_id, event_type, payload, previous)
            now = time.time()
            connection.execute(
                "INSERT INTO events(run_id, seq, event_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    run_id,
                    event["seq"],
                    json.dumps(event, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO runs(run_id, terminal, updated_at) VALUES (?, 0, ?)
                ON CONFLICT(run_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (run_id, now),
            )
        return event

    def acquire_lease(
        self,
        run_id: str,
        owner: str | None = None,
        ttl_seconds: int | None = None,
    ) -> float:
        self.path(run_id)
        owner = owner or self.lease_owner
        if owner is None or not owner.strip():
            raise ValueError("Runtime lease owner must not be empty")
        ttl = ttl_seconds or self.lease_ttl_seconds
        if ttl < 1:
            raise ValueError("Runtime lease TTL must be positive")
        now = time.time()
        expires_at = now + ttl
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner, expires_at FROM leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is not None and row[1] > now and row[0] != owner:
                raise ValueError(f"Runtime lease is held by another owner: {row[0]}")
            connection.execute(
                """
                INSERT INTO leases(run_id, owner, expires_at) VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    owner = excluded.owner,
                    expires_at = excluded.expires_at
                """,
                (run_id, owner, expires_at),
            )
        return expires_at

    def renew_lease(self, run_id: str, owner: str | None = None) -> float:
        self.path(run_id)
        owner = owner or self.lease_owner
        if owner is None:
            raise ValueError("Runtime lease owner must not be empty")
        now = time.time()
        expires_at = now + self.lease_ttl_seconds
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner, expires_at FROM leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or row[0] != owner or row[1] <= now:
                raise ValueError("Runtime lease cannot be renewed")
            connection.execute(
                "UPDATE leases SET expires_at = ? WHERE run_id = ?",
                (expires_at, run_id),
            )
        return expires_at

    def release_lease(self, run_id: str, owner: str | None = None) -> None:
        self.path(run_id)
        owner = owner or self.lease_owner
        if owner is None:
            raise ValueError("Runtime lease owner must not be empty")
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM leases WHERE run_id = ? AND owner = ?", (run_id, owner)
            )

    def mark_terminal(self, run_id: str) -> None:
        self.path(run_id)
        with self._connection() as connection:
            connection.execute(
                "UPDATE runs SET terminal = 1, updated_at = ? WHERE run_id = ?",
                (time.time(), run_id),
            )

    def purge_expired(self, now: float | None = None) -> int:
        now = now or time.time()
        cutoff = now - self.retention_days * 86400
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_ids = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT runs.run_id
                    FROM runs
                    LEFT JOIN leases ON leases.run_id = runs.run_id
                    WHERE runs.terminal = 1
                      AND runs.updated_at < ?
                      AND (leases.run_id IS NULL OR leases.expires_at <= ?)
                    """,
                    (cutoff, now),
                ).fetchall()
            ]
            for run_id in run_ids:
                connection.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
                connection.execute("DELETE FROM leases WHERE run_id = ?", (run_id,))
                connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
                if self.checkpoint_root is not None:
                    for suffix in (".json", ".sig.json"):
                        path = self.checkpoint_root / f"{run_id}{suffix}"
                        if path.is_file():
                            path.unlink()
        return len(run_ids)


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

    def __init__(
        self,
        package_dir: Path,
        runtime_dir: Path,
        integrity: RuntimeIntegrityPolicy | None = None,
        event_store_backend: str = "jsonl",
        lease_owner: str | None = None,
        lease_ttl_seconds: int = 30,
        retention_days: int = 90,
    ):
        self.package_dir = package_dir
        self.runtime_dir = runtime_dir
        self.integrity = integrity or RuntimeIntegrityPolicy()
        rooted_inputs = (
            self.integrity.trust_store,
            self.integrity.trust_store_signature,
            self.integrity.trust_root_public_key,
        )
        if self.integrity.require_rooted_trust and not all(rooted_inputs):
            raise ValueError(
                "Runtime root trust requires store, signature and root public key"
            )
        if all(rooted_inputs):
            verify_trust_store(
                self.integrity.trust_store,
                self.integrity.trust_store_signature,
                self.integrity.trust_root_public_key,
            )
        self.graph = read_json(package_dir / "graph.json")
        self.workflow = read_json(package_dir / "workflow.ir.json")
        self.lock = read_json(package_dir / "registry.lock.json")
        self.policy = read_json(package_dir / "runtime.policy.json")
        if event_store_backend == "jsonl":
            self.events: JsonlEventStore = JsonlEventStore(
                runtime_dir / "events", self.integrity
            )
        elif event_store_backend == "sqlite":
            self.events = SqliteEventStore(
                runtime_dir / "events",
                self.integrity,
                lease_owner=lease_owner,
                lease_ttl_seconds=lease_ttl_seconds,
                retention_days=retention_days,
                checkpoint_root=runtime_dir / "checkpoints",
            )
        else:
            raise ValueError(f"Unsupported event store backend: {event_store_backend}")
        self.checkpoints = runtime_dir / "checkpoints"
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        self.nodes = {node["id"]: node for node in self.graph["spec"]["nodes"]}
        self.edges: dict[str, list[dict]] = {}
        for edge in self.graph["spec"]["edges"]:
            self.edges.setdefault(edge["from"], []).append(edge)

    def checkpoint_path(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("run_id must contain only letters, numbers, dot, underscore or dash")
        return self.checkpoints / f"{run_id}.json"

    def checkpoint_signature_path(self, run_id: str) -> Path:
        return self.checkpoints / f"{run_id}.sig.json"

    def purge_expired_runs(self) -> int:
        if not isinstance(self.events, SqliteEventStore):
            raise ValueError("Retention purge requires the sqlite event store")
        return self.events.purge_expired()

    def load_state(self, run_id: str) -> dict:
        path = self.checkpoint_path(run_id)
        if not path.is_file():
            raise ValueError(f"Unknown run: {run_id}")
        signature_path = self.checkpoint_signature_path(run_id)
        if not signature_path.is_file() and self.integrity.require_signatures:
            raise ValueError("Runtime checkpoint signature is required")
        if signature_path.is_file():
            trust_store = self.integrity.require_verifier()
            verify_artifact(
                path,
                signature_path,
                trust_store,
                self.integrity.publisher,
            )
        errors = self.events.verify(run_id)
        if errors:
            raise ValueError("Runtime trajectory verification failed: " + "; ".join(errors))
        state = read_json(path)
        events = self.events.read(run_id)
        head = state.get("trajectory_head")
        checkpoint_events = [
            event for event in events if event.get("type") == "state.checkpointed"
        ]
        if not checkpoint_events:
            raise ValueError("Runtime trajectory contains no state checkpoint")
        latest_checkpoint = checkpoint_events[-1]
        if head != latest_checkpoint.get("event_hash"):
            raise ValueError("Runtime checkpoint does not reference latest state checkpoint")
        trailing = [
            event
            for event in events
            if event.get("seq", 0) > latest_checkpoint.get("seq", 0)
            and event.get("type") not in NON_STATE_AUDIT_EVENTS
        ]
        if trailing:
            raise ValueError("Runtime trajectory contains uncheckpointed state events")
        return state

    def persist(self, state: dict, event_type: str, payload: dict | None = None) -> None:
        if isinstance(self.events, SqliteEventStore) and self.events.lease_owner is not None:
            self.events.acquire_lease(state["run_id"])
        state["state_version"] += 1
        self.events.append(state["run_id"], event_type, payload or {})
        snapshot = copy.deepcopy(state)
        checkpoint_event = self.events.append(
            state["run_id"], "state.checkpointed", {"state": snapshot}
        )
        state["trajectory_head"] = checkpoint_event["event_hash"]
        checkpoint_path = self.checkpoint_path(state["run_id"])
        write_json(checkpoint_path, state)
        provider = self.integrity.signing_provider
        if self.integrity.require_signatures:
            provider = self.integrity.require_write_signer()
        if provider is not None:
            sign_artifact_with_provider(
                checkpoint_path,
                provider,
                self.checkpoint_signature_path(state["run_id"]),
                self.integrity.publisher,
            )
        if state.get("status") in {"completed", "escalated", "cancelled"} and isinstance(
            self.events, SqliteEventStore
        ):
            self.events.mark_terminal(state["run_id"])

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
            "trajectory_head": None,
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
        checkpoints = []
        for event in events:
            if event["type"] == "state.checkpointed":
                state = copy.deepcopy(event["payload"]["state"])
                state["trajectory_head"] = event["event_hash"]
                checkpoints.append(state)
        if not checkpoints:
            errors.append("trajectory contains no state checkpoint")
            replayed = None
        else:
            replayed = checkpoints[-1]
            try:
                current = self.load_state(run_id)
            except (OSError, ValueError, KeyError) as exc:
                current = None
                errors.append(f"checkpoint verification failed: {exc}")
            if current is not None and replayed != current:
                errors.append("replayed state differs from checkpoint file")
        return {
            "run_id": run_id,
            "result": "PASS" if not errors else "FAIL",
            "events": len(events),
            "errors": errors,
            "state": replayed,
        }
