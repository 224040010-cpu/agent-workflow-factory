from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, version
import inspect
import json
from pathlib import Path
import platform
import re
from typing import Any, Callable, Protocol

from .package_integrity import verify_package_manifest
from .reference_runtime import (
    ReferenceRuntime,
    RuntimeIntegrityPolicy,
    deep_merge,
    evaluate_expression,
    fact_value,
)
from .runtime_contracts import RuntimeCapabilities, negotiate
from .signing import SigningProvider, verify_artifact, verify_trust_store
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


class BudgetExceeded(RuntimeError):
    """Raised after the runtime has durably recorded a budget exhaustion."""


@dataclass(frozen=True)
class ToolBinding:
    endpoint: str
    implementation_id: str
    input_schema: dict
    output_schema: dict
    handler: ToolHandler
    reviewed_digest: str


@dataclass(frozen=True)
class HarnessUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        # DeepSeek reports reasoning tokens inside outputTokens. Cache token fields are
        # disjoint from inputTokens, so they are included in the governed total.
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }


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
class DeepSeekTrustPolicy:
    trust_store: Path
    trust_store_signature: Path
    trust_root_public_key: Path
    binding_manifest: Path
    binding_signature: Path
    binding_publisher: str = "agent-workflow-factory-adapter-maintainers"
    registry_publisher: str = "agent-workflow-factory-build"


@dataclass(frozen=True)
class ToolObservation:
    tool_name: str
    facts: dict
    evidence: list[dict]
    output_digest: str
    binding_digest: str


@dataclass(frozen=True)
class HarnessNodeResult:
    session_id: str
    finish_reason: str | None
    fact_updates: dict
    evidence: list[str]
    response_digest: str
    harness_event_types: list[str]
    tool_observation: ToolObservation
    usage: HarnessUsage


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


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ValueError(f"Unsupported binding schema type: {expected}")


def validate_binding_schema(value: Any, schema: dict, path: str = "$") -> None:
    """Validate the deliberately small JSON Schema subset used by v0.7 bindings."""

    expected = schema.get("type")
    if expected is not None and not _json_type_matches(value, expected):
        raise ValueError(f"Tool binding schema rejected {path}: expected {expected}")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"Tool binding schema rejected {path}: expected const value")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"Tool binding schema rejected {path}: value is not allowed")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        raise ValueError(f"Tool binding schema rejected {path}: string is too short")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"Tool binding schema rejected {path}: array is too short")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                validate_binding_schema(item, item_schema, f"{path}[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"Tool binding schema rejected {path}: missing {key}")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise ValueError(
                    f"Tool binding schema rejected {path}: unexpected {', '.join(extras)}"
                )
        for key, child_schema in properties.items():
            if key in value:
                validate_binding_schema(value[key], child_schema, f"{path}.{key}")


def binding_digest(
    endpoint: str,
    implementation_id: str,
    input_schema: dict,
    output_schema: dict,
    handler: ToolHandler,
) -> str:
    source = inspect.getsource(handler).replace("\r\n", "\n")
    material = {
        "endpoint": endpoint,
        "implementation_id": implementation_id,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "implementation_source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
    }
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


class ReadonlyToolHost:
    """Executes only explicit, schema-checked and digest-pinned host tool bindings."""

    def __init__(self, bindings: dict[str, ToolBinding] | None = None):
        self.bindings = bindings or builtin_readonly_tool_bindings()

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

    def preflight(self, descriptor: dict, request: dict) -> ToolBinding:
        self.validate_descriptor(descriptor)
        endpoint = descriptor.get("endpoint")
        binding = self.bindings.get(endpoint)
        if binding is None:
            raise ValueError(f"No read-only host binding for pinned endpoint: {endpoint}")
        actual = binding_digest(
            binding.endpoint,
            binding.implementation_id,
            binding.input_schema,
            binding.output_schema,
            binding.handler,
        )
        if actual != binding.reviewed_digest:
            raise ValueError(f"Tool binding implementation digest mismatch: {endpoint}")
        validate_binding_schema(request, binding.input_schema)
        return binding

    def invoke(self, descriptor: dict, request: dict, idempotency_key: str) -> ToolObservation:
        binding = self.preflight(descriptor, request)
        raw = binding.handler(
            copy.deepcopy(descriptor), copy.deepcopy(request), idempotency_key
        )
        validate_binding_schema(raw, binding.output_schema)
        material = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return ToolObservation(
            tool_name=descriptor["name"],
            facts=raw["facts"],
            evidence=raw["evidence"],
            output_digest=f"sha256:{hashlib.sha256(material.encode('utf-8')).hexdigest()}",
            binding_digest=binding.reviewed_digest,
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


_REQUEST_BASE = {
    "type": "object",
    "required": ["facts", "node_id"],
    "additionalProperties": False,
    "properties": {
        "node_id": {"type": "string", "minLength": 1},
        "facts": {
            "type": "object",
            "required": ["business"],
            "properties": {
                "business": {
                    "type": "object",
                    "required": ["description"],
                    "properties": {
                        "description": {"type": "string", "minLength": 1},
                    },
                }
            },
        },
    },
}

_EVIDENCE_ITEM = {
    "type": "object",
    "required": ["kind", "digest", "idempotency_key"],
    "properties": {
        "kind": {"type": "string", "minLength": 1},
        "digest": {"type": "string", "minLength": 1},
        "idempotency_key": {"type": "string", "minLength": 1},
    },
}

PARSE_INTENT_INPUT_SCHEMA = copy.deepcopy(_REQUEST_BASE)
PARSE_INTENT_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["facts", "evidence"],
    "additionalProperties": False,
    "properties": {
        "facts": {
            "type": "object",
            "required": ["intent"],
            "additionalProperties": False,
            "properties": {
                "intent": {
                    "type": "object",
                    "required": ["parsed"],
                    "additionalProperties": False,
                    "properties": {"parsed": {"type": "boolean", "const": True}},
                }
            },
        },
        "evidence": {"type": "array", "minItems": 1, "items": _EVIDENCE_ITEM},
    },
}

AMBIGUITY_INPUT_SCHEMA = copy.deepcopy(_REQUEST_BASE)
AMBIGUITY_INPUT_SCHEMA["properties"]["facts"]["required"].append("intent")
AMBIGUITY_INPUT_SCHEMA["properties"]["facts"]["properties"]["intent"] = {
    "type": "object",
    "required": ["parsed"],
    "properties": {"parsed": {"type": "boolean", "const": True}},
}
AMBIGUITY_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["facts", "evidence"],
    "additionalProperties": False,
    "properties": {
        "facts": {
            "type": "object",
            "required": ["analysis"],
            "additionalProperties": False,
            "properties": {
                "analysis": {
                    "type": "object",
                    "required": ["ambiguity_checked", "ambiguous", "ambiguity_terms"],
                    "additionalProperties": False,
                    "properties": {
                        "ambiguity_checked": {"type": "boolean", "const": True},
                        "ambiguous": {"type": "boolean"},
                        "ambiguity_terms": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                }
            },
        },
        "evidence": {"type": "array", "minItems": 1, "items": _EVIDENCE_ITEM},
    },
}


def builtin_readonly_tool_bindings() -> dict[str, ToolBinding]:
    bindings = [
        ToolBinding(
            endpoint="bpmn-tools:parse_business_intent()",
            implementation_id="python:workflow_factory._parse_business_intent@v1",
            input_schema=PARSE_INTENT_INPUT_SCHEMA,
            output_schema=PARSE_INTENT_OUTPUT_SCHEMA,
            handler=_parse_business_intent,
            reviewed_digest=(
                "sha256:efc8a2efeb0c207bceeedf538dc50d7f3346123a08e48f8d84db46a86db06df5"
            ),
        ),
        ToolBinding(
            endpoint="bpmn-tools:detect_description_ambiguity()",
            implementation_id="python:workflow_factory._detect_description_ambiguity@v1",
            input_schema=AMBIGUITY_INPUT_SCHEMA,
            output_schema=AMBIGUITY_OUTPUT_SCHEMA,
            handler=_detect_description_ambiguity,
            reviewed_digest=(
                "sha256:0e9faa95a11ddb6d2c87e1a1dc4074da7d903754ffef2e4128ddbdad2b4d0e5d"
            ),
        ),
    ]
    return {binding.endpoint: binding for binding in bindings}


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


def _token_usage(raw: Any) -> HarnessUsage | None:
    if not isinstance(raw, dict):
        return None
    fields = {
        "input_tokens": raw.get("inputTokens", 0),
        "output_tokens": raw.get("outputTokens", 0),
        "cache_read_tokens": raw.get("cacheReadTokens", 0),
        "cache_write_tokens": raw.get("cacheWriteTokens", 0),
        "reasoning_tokens": raw.get("reasoningTokens", 0),
    }
    if not any(
        key in raw
        for key in (
            "inputTokens",
            "outputTokens",
            "cacheReadTokens",
            "cacheWriteTokens",
            "reasoningTokens",
        )
    ):
        return None
    if not all(isinstance(value, int) and value >= 0 for value in fields.values()):
        raise ValueError("Harness token usage must contain non-negative integers")
    return HarnessUsage(**fields)


def harness_usage(events: list[Any]) -> HarnessUsage:
    """Aggregate official usage chunks, falling back to assistant messages per step."""

    message_usage: dict[tuple[Any, Any], HarnessUsage] = {}
    chunk_usage: dict[tuple[Any, Any], HarnessUsage] = {}
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        key = (data.get("turn", 0), data.get("step", index))
        if event.get("type") == "assistant/message":
            usage = _token_usage(data.get("usage"))
            if usage is not None:
                message_usage[key] = usage
        elif event.get("type") == "assistant/chunk":
            chunk = data.get("chunk", data)
            if isinstance(chunk, dict) and chunk.get("type") == "usage":
                usage = _token_usage(chunk.get("usage", chunk))
                if usage is not None:
                    chunk_usage[key] = usage
    selected = dict(message_usage)
    selected.update(chunk_usage)
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
    }
    for usage in selected.values():
        values = usage.as_dict()
        for key in totals:
            totals[key] += values[key]
    return HarnessUsage(**totals)


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

    def preflight(self, route: dict, state: dict, profile: dict, descriptor: dict) -> None:
        self._validate_profile(profile, descriptor["name"])
        self.tool_host.preflight(
            descriptor,
            {"facts": state["facts"], "node_id": route["node_id"]},
        )

    def observe(
        self,
        route: dict,
        state: dict,
        profile: dict,
        descriptor: dict,
        idempotency_key: str,
    ) -> ToolObservation:
        self.preflight(route, state, profile, descriptor)
        return self.tool_host.invoke(
            descriptor,
            {"facts": state["facts"], "node_id": route["node_id"]},
            idempotency_key,
        )

    def execute(
        self,
        route: dict,
        state: dict,
        profile: dict,
        descriptor: dict,
        idempotency_key: str,
        observation: ToolObservation | None = None,
    ) -> HarnessNodeResult:
        self.preflight(route, state, profile, descriptor)
        observation = observation or self.observe(
            route, state, profile, descriptor, idempotency_key
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
        usage = harness_usage(events)
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
            usage=usage,
        )


class DeepSeekReadonlyRunner:
    """Runs a compiled package end to end with only read-only, pinned tools."""

    def __init__(
        self,
        package_dir: Path,
        runtime_dir: Path,
        adapter: DeepSeekReadonlyAdapter,
        trust_policy: DeepSeekTrustPolicy | None = None,
        runtime_signing_provider: SigningProvider | None = None,
        runtime_publisher: str = "agent-workflow-factory-runtime",
        event_store_backend: str = "jsonl",
        lease_owner: str | None = None,
        lease_ttl_seconds: int = 30,
        retention_days: int = 90,
    ):
        self.adapter = adapter
        self.trust_policy = trust_policy
        self.runtime = ReferenceRuntime(
            package_dir,
            runtime_dir,
            RuntimeIntegrityPolicy(
                signing_provider=runtime_signing_provider,
                trust_store=trust_policy.trust_store if trust_policy is not None else None,
                trust_store_signature=(
                    trust_policy.trust_store_signature
                    if trust_policy is not None
                    else None
                ),
                trust_root_public_key=(
                    trust_policy.trust_root_public_key
                    if trust_policy is not None
                    else None
                ),
                publisher=runtime_publisher,
                require_signatures=True,
                require_rooted_trust=trust_policy is not None,
            ),
            event_store_backend=event_store_backend,
            lease_owner=lease_owner,
            lease_ttl_seconds=lease_ttl_seconds,
            retention_days=retention_days,
        )
        self.profiles = {
            path.stem.removesuffix(".agent"): read_json(path)
            for path in (package_dir / "agents").glob("*.agent.json")
        }
        self.tools = {
            item["name"]: item
            for item in self.runtime.lock["resolved_assets"]
            if item.get("type") == "tool"
        }

    def _verify_signatures(self) -> list[dict]:
        if self.trust_policy is None:
            raise ValueError("DeepSeek v1.0 requires a rooted artifact trust policy")
        policy = self.trust_policy
        trust_report = verify_trust_store(
            policy.trust_store,
            policy.trust_store_signature,
            policy.trust_root_public_key,
        )
        binding_report = verify_artifact(
            policy.binding_manifest,
            policy.binding_signature,
            policy.trust_store,
            policy.binding_publisher,
        )
        manifest = read_json(policy.binding_manifest)
        signed_bindings = {
            item["endpoint"]: item["reviewed_digest"]
            for item in manifest.get("bindings", [])
            if isinstance(item, dict)
        }
        runtime_bindings = {
            endpoint: binding.reviewed_digest
            for endpoint, binding in self.adapter.tool_host.bindings.items()
        }
        if signed_bindings != runtime_bindings:
            raise ValueError("Signed Tool Binding manifest differs from runtime bindings")
        registry_report = verify_artifact(
            self.runtime.package_dir / "registry.lock.json",
            self.runtime.package_dir / "registry.lock.sig.json",
            policy.trust_store,
            policy.registry_publisher,
        )
        package_report = verify_package_manifest(
            self.runtime.package_dir,
            policy.trust_store,
            policy.registry_publisher,
        )
        return [trust_report, binding_report, registry_report, package_report]

    def _verify_capabilities(self) -> None:
        required = self.runtime.policy["spec"]["runtime_requirements"]
        missing = negotiate(required, self.adapter.CAPABILITIES)
        if missing:
            raise ValueError(
                "DeepSeek read-only adapter lacks required capabilities: " + ", ".join(missing)
            )

    def _execution_context(self, route: dict, state: dict) -> tuple[str, dict, dict, str]:
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
        return agent_ref, self.profiles[agent_ref], self.tools[tool_ref], key

    def run(
        self,
        initial_facts: dict | None = None,
        run_id: str | None = None,
        max_nodes: int = 100,
    ) -> dict:
        signature_reports = self._verify_signatures()
        self._verify_capabilities()
        self.runtime.integrity.require_write_signer()
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
            self.runtime.events.append(
                run_id,
                "artifact.signatures.accepted",
                {"artifacts": signature_reports},
            )

        completed = 0
        try:
            while completed < max_nodes:
                route = self.runtime.route(run_id).as_dict()
                if route["status"] == "completed":
                    replay = self.runtime.replay(run_id)
                    final_state = self.runtime.load_state(run_id)
                    return {
                        "result": "PASS" if replay["result"] == "PASS" else "FAIL",
                        "adapter": "deepseek-harness",
                        "mode": "readonly",
                        "run_id": run_id,
                        "status": "completed",
                        "completed_actions": completed,
                        "replay": replay["result"],
                        "events": replay["events"],
                        "budget_usage": final_state["budget_usage"],
                    }
                if route["status"] != "waiting_action":
                    raise ValueError(f"Runtime cannot progress automatically: {route['status']}")
                state = self.runtime.load_state(run_id)
                agent_ref, profile, descriptor, key = self._execution_context(route, state)
                self.adapter.preflight(route, state, profile, descriptor)
                limits = profile["spec"]["budgets"]
                tool_delta = {"model_turns": 0, "tokens": 0, "tool_calls": 1}
                exceeded = self.runtime.budget_excess(state, agent_ref, tool_delta, limits)
                if exceeded:
                    self.runtime.exhaust_budget(
                        run_id, route["node_id"], agent_ref, tool_delta, limits, exceeded
                    )
                    raise BudgetExceeded(
                        "Agent budget exhausted before execution: " + ", ".join(exceeded)
                    )
                self.runtime.record_budget(
                    run_id,
                    route["node_id"],
                    agent_ref,
                    tool_delta,
                    limits,
                    "tool-attempt",
                )
                state = self.runtime.load_state(run_id)
                observation = self.adapter.observe(
                    route, state, profile, descriptor, key
                )
                turn_delta = {"model_turns": 1, "tokens": 0, "tool_calls": 0}
                exceeded = self.runtime.budget_excess(state, agent_ref, turn_delta, limits)
                if exceeded:
                    self.runtime.exhaust_budget(
                        run_id, route["node_id"], agent_ref, turn_delta, limits, exceeded
                    )
                    raise BudgetExceeded(
                        "Agent budget exhausted before execution: " + ", ".join(exceeded)
                    )
                self.runtime.record_budget(
                    run_id,
                    route["node_id"],
                    agent_ref,
                    turn_delta,
                    limits,
                    "model-turn-attempt",
                )
                state = self.runtime.load_state(run_id)
                result = self.adapter.execute(
                    route, state, profile, descriptor, key, observation
                )
                token_delta = {
                    "model_turns": 0,
                    "tokens": result.usage.total_tokens,
                    "tool_calls": 0,
                }
                _, exceeded = self.runtime.record_budget(
                    run_id,
                    route["node_id"],
                    agent_ref,
                    token_delta,
                    limits,
                    "provider-usage",
                )
                if exceeded:
                    self.runtime.exhaust_budget(
                        run_id, route["node_id"], agent_ref, token_delta, limits, exceeded
                    )
                    raise BudgetExceeded(
                        "Agent budget exhausted after provider usage: " + ", ".join(exceeded)
                    )
                self.runtime.events.append(
                    run_id,
                    "tool.observation.accepted",
                    {
                        "node_id": route["node_id"],
                        "tool": result.tool_observation.tool_name,
                        "output_digest": result.tool_observation.output_digest,
                        "binding_digest": result.tool_observation.binding_digest,
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
                        "usage": result.usage.as_dict(),
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
