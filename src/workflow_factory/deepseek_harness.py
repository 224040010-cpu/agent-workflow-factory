from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import re
from typing import Any, Callable, Protocol

from .reference_runtime import ReferenceRuntime, deep_merge, evaluate_expression, fact_value
from .runtime_contracts import RuntimeCapabilities, negotiate
from .util import read_json


READONLY_SIDE_EFFECTS = {"none", "read"}
PINNED_SDK_VERSION = "0.1.1rc1"
READONLY_CORDIS_DIGEST = (
    "8c1187e946b3308e94cc255997353b63c84346091239e62e3957696efdc5367a"
)
SECRET_PATTERN = re.compile(r"(?i)(?:bearer\s+)?sk-[A-Za-z0-9_-]{8,}")


class HarnessClient(Protocol):
    def run(self, input: str, *, session_id: str | None = None) -> Any: ...
    def close(self) -> None: ...


ToolHandler = Callable[[dict, dict, str], dict]


@dataclass(frozen=True)
class DeepSeekHarnessSettings:
    provider: str = "deepseek-official"
    model: str = "deepseek-v4-flash"
    max_tokens: int | None = None
    cwd: Path | None = None
    session_root: Path | None = None
    cordis: Path | None = None
    base_url: str | None = None
    api_key: str | None = None


@dataclass(frozen=True)
class ToolObservation:
    tool_name: str
    facts: dict
    evidence: list[dict]
    output_digest: str


@dataclass(frozen=True)
class HarnessNodeResult:
    session_id: str
    finish_reason: str | None
    fact_updates: dict
    evidence: list[str]
    response_digest: str
    harness_event_types: list[str]
    tool_observation: ToolObservation


class OfficialDeepSeekHarnessClient:
    """Small optional-dependency boundary around the official synchronous SDK."""

    def __init__(self, settings: DeepSeekHarnessSettings):
        if platform.system() not in {"Linux", "Darwin"}:
            raise RuntimeError(
                "The official bundled DeepSeek Harness Python runtime supports Linux/macOS, "
                "not native Windows; run the live adapter in WSL2/Linux"
            )
        try:
            installed_version = version("deepseek-harness-sdk")
        except PackageNotFoundError as exc:
            raise RuntimeError(
                "DeepSeek Harness SDK is not installed; install the pinned optional dependency "
                "on a supported Linux/macOS host"
            ) from exc
        if installed_version != PINNED_SDK_VERSION:
            raise RuntimeError(
                f"Unsupported DeepSeek Harness SDK {installed_version}; "
                f"expected {PINNED_SDK_VERSION}"
            )
        if settings.cordis is None or not settings.cordis.is_file():
            raise RuntimeError("The pinned read-only Cordis composition is required")
        actual_cordis_digest = hashlib.sha256(settings.cordis.read_bytes()).hexdigest()
        if actual_cordis_digest != READONLY_CORDIS_DIGEST:
            raise RuntimeError(
                "Cordis composition differs from the reviewed read-only configuration"
            )
        if settings.cwd is not None:
            settings.cwd.mkdir(parents=True, exist_ok=True)
        if settings.session_root is not None:
            settings.session_root.mkdir(parents=True, exist_ok=True)
        try:
            from deepseek_harness import DeepSeekHarness
        except ImportError as exc:
            raise RuntimeError(
                "DeepSeek Harness SDK distribution does not expose deepseek_harness"
            ) from exc

        kwargs: dict[str, Any] = {
            "provider": settings.provider,
            "model": settings.model,
        }
        for key, value in {
            "max_tokens": settings.max_tokens,
            "cwd": settings.cwd,
            "session_root": settings.session_root,
            "cordis": settings.cordis,
            "base_url": settings.base_url,
            "api_key": settings.api_key,
        }.items():
            if value is not None:
                kwargs[key] = str(value) if isinstance(value, Path) else value
        self._harness = DeepSeekHarness(**kwargs)

    def run(self, input: str, *, session_id: str | None = None) -> Any:
        return self._harness.run(input, session_id=session_id)

    def close(self) -> None:
        self._harness.close()


class ReadonlyToolHost:
    """Executes only explicit, pinned and retry-safe host tool bindings."""

    def __init__(self, handlers: dict[str, ToolHandler] | None = None):
        self.handlers = handlers or builtin_readonly_tool_handlers()

    @staticmethod
    def validate_descriptor(descriptor: dict) -> None:
        if descriptor.get("type") != "tool":
            raise ValueError("Runtime asset is not a tool")
        if descriptor.get("side_effects") not in READONLY_SIDE_EFFECTS:
            raise ValueError(f"Tool is not read-only: {descriptor.get('name')}")
        if descriptor.get("requires_approval") is not False:
            raise ValueError(f"Tool requires approval: {descriptor.get('name')}")
        if descriptor.get("idempotent") is not True:
            raise ValueError(f"Tool is not retry-safe: {descriptor.get('name')}")
        if not str(descriptor.get("digest", "")).startswith("sha256:"):
            raise ValueError(f"Tool has no pinned digest: {descriptor.get('name')}")

    def invoke(self, descriptor: dict, request: dict, idempotency_key: str) -> ToolObservation:
        self.validate_descriptor(descriptor)
        endpoint = descriptor.get("endpoint")
        handler = self.handlers.get(endpoint)
        if handler is None:
            raise ValueError(f"No read-only host binding for pinned endpoint: {endpoint}")
        raw = handler(copy.deepcopy(descriptor), copy.deepcopy(request), idempotency_key)
        if set(raw) != {"facts", "evidence"}:
            raise ValueError("Tool binding must return exactly facts and evidence")
        if not isinstance(raw["facts"], dict) or not isinstance(raw["evidence"], list):
            raise ValueError("Tool binding returned invalid evidence envelope")
        material = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return ToolObservation(
            tool_name=descriptor["name"],
            facts=raw["facts"],
            evidence=raw["evidence"],
            output_digest=f"sha256:{hashlib.sha256(material.encode('utf-8')).hexdigest()}",
        )


def _parse_business_intent(descriptor: dict, request: dict, idempotency_key: str) -> dict:
    del descriptor
    description = fact_value(request.get("facts", {}), "business.description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("parse-business-intent requires facts.business.description")
    normalized = " ".join(description.split())
    source_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return {
        "facts": {"intent": {"parsed": True}},
        "evidence": [
            {
                "kind": "business-description-digest",
                "digest": f"sha256:{source_digest}",
                "characters": len(normalized),
                "idempotency_key": idempotency_key,
            }
        ],
    }


def _detect_description_ambiguity(
    descriptor: dict,
    request: dict,
    idempotency_key: str,
) -> dict:
    del descriptor
    facts = request.get("facts", {})
    description = fact_value(facts, "business.description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("detect-description-ambiguity requires facts.business.description")
    if fact_value(facts, "intent.parsed") is not True:
        raise ValueError("detect-description-ambiguity requires facts.intent.parsed=true")
    markers = ("尽快", "适当", "必要时", "酌情", "相关人员", "视情况", "若干")
    found = [marker for marker in markers if marker in description]
    material = json.dumps(
        {"description": " ".join(description.split()), "markers": markers},
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "facts": {
            "analysis": {
                "ambiguity_checked": True,
                "ambiguous": bool(found),
                "ambiguity_terms": found,
            }
        },
        "evidence": [
            {
                "kind": "deterministic-ambiguity-scan",
                "digest": f"sha256:{hashlib.sha256(material.encode('utf-8')).hexdigest()}",
                "rule_version": "ambiguity-markers/v1",
                "matches": len(found),
                "idempotency_key": idempotency_key,
            }
        ],
    }


def builtin_readonly_tool_handlers() -> dict[str, ToolHandler]:
    return {
        "bpmn-tools:parse_business_intent()": _parse_business_intent,
        "bpmn-tools:detect_description_ambiguity()": _detect_description_ambiguity,
    }


def _strict_model_envelope(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Harness final response must be one JSON object without Markdown") from exc
    if not isinstance(data, dict) or set(data) != {"status", "facts", "evidence"}:
        raise ValueError("Harness response must contain exactly status, facts and evidence")
    if data["status"] != "completed":
        raise ValueError(f"Harness did not complete the node: {data['status']}")
    if not isinstance(data["facts"], dict):
        raise ValueError("Harness facts must be an object")
    if not isinstance(data["evidence"], list) or not all(
        isinstance(item, str) for item in data["evidence"]
    ):
        raise ValueError("Harness evidence must be an array of strings")
    return data


def _event_types(events: list[Any]) -> list[str]:
    values: list[str] = []
    for event in events:
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            values.append(event["type"])
    return values


def _harness_failure(events: list[Any], finish_reason: str | None) -> str:
    reason: Any = None
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("type") != "turn/end":
            continue
        data = event.get("data")
        if isinstance(data, dict):
            reason = data.get("reason")
        break
    detail = json.dumps(reason, ensure_ascii=False, sort_keys=True) if reason else "no detail"
    detail = SECRET_PATTERN.sub("[REDACTED_API_KEY]", detail)
    if len(detail) > 1200:
        detail = detail[:1200] + "..."
    return f"Harness turn ended with {finish_reason or 'unknown'}: {detail}"


class DeepSeekReadonlyAdapter:
    CAPABILITIES = RuntimeCapabilities(
        durable_sessions=True,
        append_only_events=True,
        human_gate=False,
        scheduled_loops=False,
        sandbox_network_allowlist=True,
    )

    def __init__(
        self,
        tool_host: ReadonlyToolHost | None = None,
        client: HarnessClient | None = None,
        settings: DeepSeekHarnessSettings | None = None,
    ):
        self.tool_host = tool_host or ReadonlyToolHost()
        self._client = client
        self.settings = settings or DeepSeekHarnessSettings()

    @property
    def client(self) -> HarnessClient:
        if self._client is None:
            self._client = OfficialDeepSeekHarnessClient(self.settings)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    @staticmethod
    def _validate_profile(profile: dict, tool_name: str) -> None:
        spec = profile.get("spec", {})
        permissions = spec.get("permissions", {})
        if permissions.get("external_write") != "deny":
            raise ValueError("Read-only adapter requires Agent external_write=deny")
        if permissions.get("requires_human_approval") is not False:
            raise ValueError("Read-only adapter cannot execute approval-gated Agent profiles")
        if tool_name not in spec.get("tools", []):
            raise ValueError(f"Agent profile is not bound to tool: {tool_name}")

    @staticmethod
    def _prompt(route: dict, profile: dict, state: dict, observation: ToolObservation) -> str:
        evidence_kinds = [
            item["kind"]
            for item in observation.evidence
            if isinstance(item, dict) and isinstance(item.get("kind"), str)
        ]
        payload = {
            "instruction": (
                "复核宿主工具证据，然后只返回 required_response 对象本身的 JSON 序列化；"
                "不要 Markdown、解释或新增字段，不要把 evidence 字符串改成对象。"
            ),
            "workflow": {
                "node_id": route["node_id"],
                "completion_evidence": route["completion_evidence"],
                "current_facts": state["facts"],
            },
            "agent_profile": {
                "id": profile["metadata"]["id"],
                "purpose": profile["spec"]["purpose"],
                "budgets": profile["spec"]["budgets"],
            },
            "trusted_tool_observation": {
                "tool_name": observation.tool_name,
                "facts": observation.facts,
                "evidence": observation.evidence,
                "output_digest": observation.output_digest,
            },
            "required_response": {
                "status": "completed",
                "facts": observation.facts,
                "evidence": evidence_kinds,
            },
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def execute(
        self,
        route: dict,
        state: dict,
        profile: dict,
        descriptor: dict,
        idempotency_key: str,
    ) -> HarnessNodeResult:
        self._validate_profile(profile, descriptor["name"])
        observation = self.tool_host.invoke(
            descriptor,
            {"facts": state["facts"], "node_id": route["node_id"]},
            idempotency_key,
        )
        session_id = f"{route['run_id']}--{route['node_id']}"
        result = self.client.run(
            self._prompt(route, profile, state, observation),
            session_id=session_id,
        )
        final_response = getattr(result, "final_response", None)
        if not isinstance(final_response, str):
            raise ValueError("Harness result has no final_response")
        events = getattr(result, "events", [])
        events = events if isinstance(events, list) else []
        finish_reason = getattr(result, "finish_reason", None)
        if finish_reason not in {None, "completed"}:
            raise RuntimeError(_harness_failure(events, finish_reason))
        if not final_response.strip():
            raise RuntimeError(_harness_failure(events, finish_reason))
        envelope = _strict_model_envelope(final_response)
        if envelope["facts"] != observation.facts:
            raise ValueError("Model facts differ from trusted tool facts")
        expected_evidence = [
            item["kind"]
            for item in observation.evidence
            if isinstance(item, dict) and isinstance(item.get("kind"), str)
        ]
        if envelope["evidence"] != expected_evidence:
            raise ValueError("Model evidence differs from trusted tool evidence kinds")
        candidate = deep_merge(copy.deepcopy(state["facts"]), envelope["facts"])
        failed = [
            item
            for item in route["completion_evidence"]
            if not evaluate_expression(item, candidate)
        ]
        if failed:
            raise ValueError(f"Trusted tool facts do not satisfy evidence: {', '.join(failed)}")
        return HarnessNodeResult(
            session_id=str(getattr(result, "session_id", session_id)),
            finish_reason=finish_reason,
            fact_updates=envelope["facts"],
            evidence=envelope["evidence"],
            response_digest=(
                "sha256:" + hashlib.sha256(final_response.encode("utf-8")).hexdigest()
            ),
            harness_event_types=_event_types(events),
            tool_observation=observation,
        )


class DeepSeekReadonlyRunner:
    """Runs a compiled package end to end with only read-only, pinned tools."""

    def __init__(self, package_dir: Path, runtime_dir: Path, adapter: DeepSeekReadonlyAdapter):
        self.runtime = ReferenceRuntime(package_dir, runtime_dir)
        self.adapter = adapter
        self.profiles = {
            path.stem.removesuffix(".agent"): read_json(path)
            for path in (package_dir / "agents").glob("*.agent.json")
        }
        self.tools = {
            item["name"]: item
            for item in self.runtime.lock["resolved_assets"]
            if item.get("type") == "tool"
        }

    def _verify_capabilities(self) -> None:
        required = self.runtime.policy["spec"]["runtime_requirements"]
        missing = negotiate(required, self.adapter.CAPABILITIES)
        if missing:
            raise ValueError(
                "DeepSeek read-only adapter lacks required capabilities: " + ", ".join(missing)
            )

    def _execute_route(self, route: dict, state: dict) -> HarnessNodeResult:
        action = route["action"]
        if action.get("kind") not in {"agent_task", "tool_task"}:
            raise ValueError(f"Read-only MVP cannot execute action kind: {action.get('kind')}")
        agent_ref = action.get("agent_ref")
        tool_ref = action.get("tool_ref")
        if not agent_ref or agent_ref not in self.profiles:
            raise ValueError(f"No compiled Agent profile for node: {route['node_id']}")
        if not tool_ref or tool_ref not in self.tools:
            raise ValueError(f"No pinned read-only tool for node: {route['node_id']}")
        key = f"{route['run_id']}:{route['node_id']}:{state['loop_rounds']}"
        return self.adapter.execute(
            route,
            state,
            self.profiles[agent_ref],
            self.tools[tool_ref],
            key,
        )

    def run(
        self,
        initial_facts: dict | None = None,
        run_id: str | None = None,
        max_nodes: int = 100,
    ) -> dict:
        self._verify_capabilities()
        run_id = run_id or "run-deepseek-readonly"
        checkpoint = self.runtime.checkpoint_path(run_id)
        if checkpoint.exists():
            state = self.runtime.load_state(run_id)
            if state["status"] == "paused":
                state = self.runtime.resume(run_id)
        else:
            state = self.runtime.start(initial_facts, run_id)
            self.runtime.events.append(
                run_id,
                "adapter.capabilities.accepted",
                {"adapter": "deepseek-harness", "mode": "readonly"},
            )

        completed = 0
        try:
            while completed < max_nodes:
                route = self.runtime.route(run_id).as_dict()
                if route["status"] == "completed":
                    replay = self.runtime.replay(run_id)
                    return {
                        "result": "PASS" if replay["result"] == "PASS" else "FAIL",
                        "adapter": "deepseek-harness",
                        "mode": "readonly",
                        "run_id": run_id,
                        "status": "completed",
                        "completed_actions": completed,
                        "replay": replay["result"],
                        "events": replay["events"],
                    }
                if route["status"] != "waiting_action":
                    raise ValueError(f"Runtime cannot progress automatically: {route['status']}")
                state = self.runtime.load_state(run_id)
                result = self._execute_route(route, state)
                self.runtime.events.append(
                    run_id,
                    "tool.observation.accepted",
                    {
                        "node_id": route["node_id"],
                        "tool": result.tool_observation.tool_name,
                        "output_digest": result.tool_observation.output_digest,
                    },
                )
                self.runtime.events.append(
                    run_id,
                    "agent.turn.completed",
                    {
                        "node_id": route["node_id"],
                        "session_id": result.session_id,
                        "finish_reason": result.finish_reason,
                        "response_digest": result.response_digest,
                        "harness_event_types": result.harness_event_types,
                    },
                )
                self.runtime.events.append(
                    run_id,
                    "facts.verified",
                    {
                        "node_id": route["node_id"],
                        "basis": "model-facts-equal-pinned-tool-facts",
                    },
                )
                self.runtime.complete(run_id, route["node_id"], result.fact_updates)
                completed += 1
            raise ValueError(f"Read-only run exceeded max_nodes={max_nodes}")
        except Exception as exc:
            self.runtime.events.append(
                run_id,
                "adapter.execution.interrupted",
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
            current = self.runtime.load_state(run_id)
            if current["status"] not in {"paused", "completed", "escalated", "cancelled"}:
                self.runtime.pause(run_id, f"adapter interruption: {type(exc).__name__}")
            raise
