from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ComplexityProfile:
    name: str
    audience: str
    description: str
    event_store: str
    require_runtime_signatures: bool
    require_runtime_trust_root: bool
    default_lease_owner: str | None
    lease_ttl_seconds: int
    retention_days: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeProfileOptions:
    profile: ComplexityProfile
    event_store: str
    require_runtime_signatures: bool
    require_runtime_trust_root: bool
    lease_owner: str | None
    lease_ttl_seconds: int
    retention_days: int

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["profile"] = self.profile.name
        return result


PROFILES: dict[str, ComplexityProfile] = {
    "dev": ComplexityProfile(
        name="dev",
        audience="本地开发、演示和低风险原型",
        description="JSONL 单进程运行，不强制运行签名和根信任。",
        event_store="jsonl",
        require_runtime_signatures=False,
        require_runtime_trust_root=False,
        default_lease_owner=None,
        lease_ttl_seconds=30,
        retention_days=30,
    ),
    "team": ComplexityProfile(
        name="team",
        audience="团队集成测试和受控内部业务",
        description="SQLite、Worker 租约和运行签名，不强制离线根。",
        event_store="sqlite",
        require_runtime_signatures=True,
        require_runtime_trust_root=False,
        default_lease_owner="workflowctl-team",
        lease_ttl_seconds=30,
        retention_days=90,
    ),
    "regulated": ComplexityProfile(
        name="regulated",
        audience="金融生产、强审计和多租户环境",
        description="SQLite、租约、运行签名、离线根信任和长期保留。",
        event_store="sqlite",
        require_runtime_signatures=True,
        require_runtime_trust_root=True,
        default_lease_owner="workflowctl-regulated",
        lease_ttl_seconds=30,
        retention_days=365,
    ),
}


def get_profile(name: str) -> ComplexityProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown complexity profile: {name}; expected one of {', '.join(PROFILES)}"
        ) from exc


def resolve_runtime_options(
    profile_name: str,
    *,
    event_store: str | None = None,
    require_runtime_signatures: bool = False,
    require_runtime_trust_root: bool = False,
    lease_owner: str | None = None,
    lease_ttl_seconds: int | None = None,
    retention_days: int | None = None,
) -> RuntimeProfileOptions:
    profile = get_profile(profile_name)
    selected_store = event_store or profile.event_store
    if selected_store not in {"jsonl", "sqlite"}:
        raise ValueError(f"Unsupported event store: {selected_store}")
    if profile.event_store == "sqlite" and selected_store != "sqlite":
        raise ValueError(
            f"Profile {profile.name} requires the sqlite event store and cannot be downgraded"
        )
    ttl = (
        lease_ttl_seconds
        if lease_ttl_seconds is not None
        else profile.lease_ttl_seconds
    )
    retention = retention_days if retention_days is not None else profile.retention_days
    if ttl < 1:
        raise ValueError("Runtime lease TTL must be positive")
    if retention < 1:
        raise ValueError("Runtime retention days must be positive")
    if lease_owner is not None and not lease_owner.strip():
        raise ValueError("Runtime lease owner must not be empty")
    return RuntimeProfileOptions(
        profile=profile,
        event_store=selected_store,
        require_runtime_signatures=(
            profile.require_runtime_signatures or require_runtime_signatures
        ),
        require_runtime_trust_root=(
            profile.require_runtime_trust_root or require_runtime_trust_root
        ),
        lease_owner=(
            lease_owner
            if lease_owner is not None
            else profile.default_lease_owner
        ),
        lease_ttl_seconds=ttl,
        retention_days=retention,
    )


def validate_runtime_material(
    options: RuntimeProfileOptions,
    *,
    signing_provider: Any | None,
    trust_store: Path | None,
    trust_store_signature: Path | None,
    trust_root_public_key: Path | None,
    mutating: bool,
    publisher: str = "agent-workflow-factory-runtime",
) -> None:
    rooted = (trust_store, trust_store_signature, trust_root_public_key)
    if any(rooted) and not all(rooted):
        if options.require_runtime_trust_root:
            raise ValueError(
                f"Profile {options.profile.name} requires runtime trust store, "
                "trust-store signature and root public key"
            )
        if trust_store_signature is not None or trust_root_public_key is not None:
            raise ValueError(
                "Runtime rooted trust must provide store, signature and root public key together"
            )
    if options.require_runtime_trust_root and not all(rooted):
        raise ValueError(
            f"Profile {options.profile.name} requires runtime trust store, "
            "trust-store signature and root public key"
        )
    if options.require_runtime_signatures and trust_store is None:
        raise ValueError(
            f"Profile {options.profile.name} requires --runtime-trust-store"
        )
    if mutating and options.require_runtime_signatures and signing_provider is None:
        raise ValueError(
            f"Profile {options.profile.name} requires --runtime-signing-key "
            "or a runtime PKCS#11 signer"
        )
    if signing_provider is not None and trust_store is None:
        raise ValueError("A runtime signing provider requires --runtime-trust-store")
    for label, path in (
        ("runtime trust store", trust_store),
        ("runtime trust-store signature", trust_store_signature),
        ("runtime root public key", trust_root_public_key),
    ):
        if path is not None and not path.is_file():
            raise ValueError(f"Missing {label}: {path}")
    if mutating and signing_provider is not None and trust_store is not None:
        try:
            trust = json.loads(trust_store.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid runtime trust store: {exc}") from exc
        keys = trust.get("keys") if isinstance(trust, dict) else None
        if not isinstance(keys, list):
            raise ValueError("Runtime trust store has an invalid shape")
        matching = [
            item
            for item in keys
            if isinstance(item, dict)
            and item.get("key_id") == signing_provider.key_id
            and item.get("publisher") == publisher
        ]
        if not matching:
            raise ValueError(
                "Runtime signing key is not registered for the required publisher"
            )
        if matching[0].get("status") != "active":
            raise ValueError("New runtime evidence requires an active signing key")


def profiles_report() -> dict[str, dict[str, Any]]:
    return {name: profile.as_dict() for name, profile in PROFILES.items()}
