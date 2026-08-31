from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import platform
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .complexity_profiles import (
    resolve_runtime_options,
    validate_runtime_material,
)
from .deepseek_harness import (
    DeepSeekHarnessSettings,
    DeepSeekReadonlyAdapter,
    DeepSeekReadonlyRunner,
    DeepSeekTrustPolicy,
)
from .project import (
    WorkflowProject,
    create_project,
    load_project,
    review_project,
    test_project,
)
from .signing import (
    FileEd25519SigningProvider,
    Pkcs11Ed25519SigningProvider,
    SigningProvider,
    verify_artifact,
    verify_trust_store,
)
from .util import read_json
from .validator import validate_package


DEPLOYMENT_SCHEMA_VERSION = "1.0.0"
DEPLOYMENT_FIELDS = {
    "$schema",
    "schema_version",
    "deployment_id",
    "runtime_dir",
    "cordis",
    "artifact_trust",
    "build_signer",
    "runtime_signer",
    "api_key_env",
    "max_tokens",
    "base_url",
}
TRUST_FIELDS = {
    "trust_store",
    "trust_store_signature",
    "trust_root_public_key",
    "binding_manifest",
    "binding_signature",
}
SIGNER_FIELDS = {
    "kind",
    "private_key_path",
    "module",
    "token_label",
    "key_label",
    "key_id",
    "object_id",
    "pin_env",
}
FORBIDDEN_SECRET_FIELDS = {
    "api_key",
    "password",
    "pin",
    "private_key",
    "secret",
    "token",
}


@dataclass(frozen=True)
class ArtifactTrust:
    trust_store: Path
    trust_store_signature: Path
    trust_root_public_key: Path
    binding_manifest: Path
    binding_signature: Path


@dataclass(frozen=True)
class SignerReference:
    kind: str
    private_key_path: Path | None = None
    module: str | None = None
    token_label: str | None = None
    key_label: str | None = None
    key_id: str | None = None
    object_id: bytes | None = None
    pin_env: str = "AWF_PKCS11_PIN"

    def provider(self) -> SigningProvider:
        if self.kind == "pem":
            if self.private_key_path is None:
                raise ValueError("PEM signer requires private_key_path")
            return FileEd25519SigningProvider(self.private_key_path)
        if self.kind == "pkcs11":
            required = {
                "module": self.module,
                "token_label": self.token_label,
                "key_label": self.key_label,
                "key_id": self.key_id,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(
                    "PKCS#11 signer is missing: " + ", ".join(missing)
                )
            return Pkcs11Ed25519SigningProvider(
                self.module,
                self.token_label,
                self.key_label,
                self.key_id,
                self.pin_env,
                self.object_id,
            )
        raise ValueError("Signer kind must be pem or pkcs11")


@dataclass(frozen=True)
class DeploymentConfig:
    config_path: Path
    schema_version: str
    deployment_id: str
    runtime_dir: Path
    cordis: Path
    artifact_trust: ArtifactTrust
    build_signer: SignerReference
    runtime_signer: SignerReference | None
    api_key_env: str
    max_tokens: int | None
    base_url: str | None

    def public_summary(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "deployment_id": self.deployment_id,
            "runtime_dir": str(self.runtime_dir),
            "cordis": str(self.cordis),
            "build_signer": self.build_signer.kind,
            "runtime_signer": (
                self.runtime_signer.kind if self.runtime_signer else None
            ),
            "api_key_env": self.api_key_env,
            "max_tokens": self.max_tokens,
            "base_url_configured": self.base_url is not None,
        }


def _unknown_fields(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {', '.join(unknown)}")


def _reject_inline_secrets(value: Any, path: str = "deployment") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in FORBIDDEN_SECRET_FIELDS or normalized.endswith(
                ("_api_key", "_password", "_pin", "_private_key", "_secret", "_token")
            ):
                raise ValueError(
                    f"Deployment configuration must not contain inline secret field: {path}.{key}"
                )
            _reject_inline_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_inline_secrets(item, f"{path}[{index}]")


def _required_string(value: dict[str, Any], field: str, label: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        raise ValueError(f"{label}.{field} must be a non-empty string")
    return result.strip()


def _resolve_path(base: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _load_signer(
    value: Any,
    base: Path,
    label: str,
    *,
    required: bool,
) -> SignerReference | None:
    if value is None and not required:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    _unknown_fields(value, SIGNER_FIELDS, label)
    kind = _required_string(value, "kind", label)
    if kind == "pem":
        private_key_path = _resolve_path(
            base, _required_string(value, "private_key_path", label)
        )
        if not private_key_path.is_file():
            raise ValueError(f"Missing PEM signer key: {private_key_path}")
        irrelevant = sorted(
            set(value) - {"kind", "private_key_path"}
        )
        if irrelevant:
            raise ValueError(
                f"{label} PEM signer contains PKCS#11 fields: {', '.join(irrelevant)}"
            )
        return SignerReference(kind=kind, private_key_path=private_key_path)
    if kind != "pkcs11":
        raise ValueError(f"{label}.kind must be pem or pkcs11")
    object_id = value.get("object_id")
    try:
        object_id_bytes = bytes.fromhex(object_id) if object_id else None
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}.object_id must be hexadecimal") from exc
    pin_env = value.get("pin_env", "AWF_PKCS11_PIN")
    if not isinstance(pin_env, str) or not pin_env.strip():
        raise ValueError(f"{label}.pin_env must be a non-empty string")
    return SignerReference(
        kind=kind,
        module=_required_string(value, "module", label),
        token_label=_required_string(value, "token_label", label),
        key_label=_required_string(value, "key_label", label),
        key_id=_required_string(value, "key_id", label),
        object_id=object_id_bytes,
        pin_env=pin_env,
    )


def load_deployment(
    config_path: Path,
    project: WorkflowProject | None = None,
) -> DeploymentConfig:
    config_path = config_path.resolve()
    if not config_path.is_file():
        raise ValueError(f"Deployment configuration does not exist: {config_path}")
    data = read_json(config_path)
    _reject_inline_secrets(data)
    _unknown_fields(data, DEPLOYMENT_FIELDS, "deployment")
    if data.get("schema_version") != DEPLOYMENT_SCHEMA_VERSION:
        raise ValueError(
            f"deployment.schema_version must be {DEPLOYMENT_SCHEMA_VERSION}"
        )
    deployment_id = _required_string(data, "deployment_id", "deployment")
    if project is not None:
        expected = project.runtime.deployment_ref
        if expected is None:
            raise ValueError("Project runtime.deployment_ref is required for deployment")
        if expected != deployment_id:
            raise ValueError(
                f"Project deployment_ref {expected} does not match {deployment_id}"
            )

    base = config_path.parent
    trust_data = data.get("artifact_trust")
    if not isinstance(trust_data, dict):
        raise ValueError("deployment.artifact_trust must be an object")
    _unknown_fields(trust_data, TRUST_FIELDS, "deployment.artifact_trust")
    trust_paths = {
        field: _resolve_path(
            base, _required_string(trust_data, field, "deployment.artifact_trust")
        )
        for field in TRUST_FIELDS
    }
    for label, path in trust_paths.items():
        if not path.is_file():
            raise ValueError(f"Missing deployment {label}: {path}")
    artifact_trust = ArtifactTrust(**trust_paths)

    runtime_dir = _resolve_path(
        base, _required_string(data, "runtime_dir", "deployment")
    )
    cordis = _resolve_path(base, _required_string(data, "cordis", "deployment"))
    if not cordis.is_file():
        raise ValueError(f"Missing deployment cordis: {cordis}")
    build_signer = _load_signer(
        data.get("build_signer"), base, "deployment.build_signer", required=True
    )
    runtime_signer = _load_signer(
        data.get("runtime_signer"),
        base,
        "deployment.runtime_signer",
        required=False,
    )
    api_key_env = data.get("api_key_env", "DEEPSEEK_API_KEY")
    if not isinstance(api_key_env, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", api_key_env
    ):
        raise ValueError("deployment.api_key_env must be an environment variable name")
    max_tokens = data.get("max_tokens")
    if max_tokens is not None and (
        not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1
    ):
        raise ValueError("deployment.max_tokens must be a positive integer")
    base_url = data.get("base_url")
    if base_url is not None and (
        not isinstance(base_url, str) or not base_url.strip()
    ):
        raise ValueError("deployment.base_url must be a non-empty string")
    if base_url:
        parsed_url = urlsplit(base_url)
        if parsed_url.username is not None or parsed_url.password is not None:
            raise ValueError("deployment.base_url must not contain inline credentials")
        query_keys = {
            key.strip().lower().replace("-", "_")
            for key, _ in parse_qsl(parsed_url.query, keep_blank_values=True)
        }
        if any(
            key in FORBIDDEN_SECRET_FIELDS
            or key.endswith(("_api_key", "_password", "_secret", "_token"))
            for key in query_keys
        ):
            raise ValueError("deployment.base_url query must not contain credentials")
    return DeploymentConfig(
        config_path=config_path,
        schema_version=DEPLOYMENT_SCHEMA_VERSION,
        deployment_id=deployment_id,
        runtime_dir=runtime_dir,
        cordis=cordis,
        artifact_trust=artifact_trust,
        build_signer=build_signer,
        runtime_signer=runtime_signer,
        api_key_env=api_key_env.strip(),
        max_tokens=max_tokens,
        base_url=base_url.strip() if base_url else None,
    )


def _trust_policy(deployment: DeploymentConfig) -> DeepSeekTrustPolicy:
    trust = deployment.artifact_trust
    return DeepSeekTrustPolicy(
        trust_store=trust.trust_store,
        trust_store_signature=trust.trust_store_signature,
        trust_root_public_key=trust.trust_root_public_key,
        binding_manifest=trust.binding_manifest,
        binding_signature=trust.binding_signature,
    )


def create_project_for_deployment(
    project_path: Path,
    deployment_path: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    project = load_project(project_path)
    deployment = load_deployment(deployment_path, project)
    trust = deployment.artifact_trust
    verify_trust_store(
        trust.trust_store,
        trust.trust_store_signature,
        trust.trust_root_public_key,
    )
    if (
        not dry_run
        and deployment.build_signer.kind == "pkcs11"
        and not os.environ.get(deployment.build_signer.pin_env, "").strip()
    ):
        raise ValueError(
            "Missing build PKCS#11 PIN environment variable: "
            + deployment.build_signer.pin_env
        )
    build_provider = None if dry_run else deployment.build_signer.provider()
    if build_provider is not None:
        trust_store = read_json(trust.trust_store)
        registered = [
            key
            for key in trust_store.get("keys", [])
            if key.get("key_id") == build_provider.key_id
            and key.get("publisher") == "agent-workflow-factory-build"
            and key.get("status") == "active"
        ]
        if not registered:
            raise ValueError(
                "Build signing key is not active for agent-workflow-factory-build"
            )
    report = create_project(
        project_path,
        dry_run=dry_run,
        signing_provider=build_provider,
    )
    if not dry_run:
        errors = validate_package(
            project.output / "package",
            trust_store=trust.trust_store,
            require_registry_signature=True,
            require_package_signature=True,
            trust_store_signature=trust.trust_store_signature,
            trust_root_public_key=trust.trust_root_public_key,
            require_trust_root=True,
        )
        if errors:
            raise ValueError(
                "Deployment package signature validation failed: " + "; ".join(errors)
            )
    report["deployment"] = deployment.public_summary()
    report["deployment_signing_planned"] = True
    return report


def check_deployment(
    project_path: Path,
    deployment_path: Path,
    *,
    require_live_environment: bool = False,
) -> dict[str, Any]:
    project = load_project(project_path)
    deployment = load_deployment(deployment_path, project)
    errors: list[str] = []

    review = review_project(project_path)
    if review["result"] != "PASS":
        errors.extend(review["errors"])
    contract = test_project(project_path)
    if contract["result"] != "PASS":
        errors.extend(contract["runtime_readiness"]["blockers"])

    trust = deployment.artifact_trust
    errors.extend(
        validate_package(
            project.output / "package",
            trust_store=trust.trust_store,
            require_registry_signature=True,
            require_package_signature=True,
            trust_store_signature=trust.trust_store_signature,
            trust_root_public_key=trust.trust_root_public_key,
            require_trust_root=True,
        )
    )
    try:
        verify_artifact(
            trust.binding_manifest,
            trust.binding_signature,
            trust.trust_store,
            "agent-workflow-factory-adapter-maintainers",
        )
    except (OSError, ValueError, KeyError) as exc:
        errors.append(f"Tool Binding signature invalid: {exc}")

    try:
        runtime_provider = (
            deployment.runtime_signer.provider()
            if deployment.runtime_signer is not None
            else None
        )
        runtime_options = resolve_runtime_options(project.runtime.profile)
        validate_runtime_material(
            runtime_options,
            signing_provider=runtime_provider,
            trust_store=trust.trust_store,
            trust_store_signature=trust.trust_store_signature,
            trust_root_public_key=trust.trust_root_public_key,
            mutating=True,
        )
    except (OSError, ValueError, KeyError) as exc:
        errors.append(str(exc))

    credential_present = bool(os.environ.get(deployment.api_key_env, "").strip())
    if require_live_environment and not credential_present:
        errors.append(f"Missing credential environment variable: {deployment.api_key_env}")
    if require_live_environment and platform.system() not in {"Linux", "Darwin"}:
        errors.append("Official DeepSeek Harness live execution requires Linux or Darwin")
    sdk_present = importlib.util.find_spec("deepseek_harness") is not None
    if require_live_environment and not sdk_present:
        errors.append("deepseek-harness-sdk is not installed")
    runtime_pin = (
        deployment.runtime_signer.pin_env
        if deployment.runtime_signer is not None
        and deployment.runtime_signer.kind == "pkcs11"
        else None
    )
    runtime_pin_present = (
        bool(os.environ.get(runtime_pin, "").strip()) if runtime_pin else None
    )
    if require_live_environment and runtime_pin and not runtime_pin_present:
        errors.append(f"Missing PKCS#11 PIN environment variable: {runtime_pin}")

    return {
        "result": "PASS" if not errors else "FAIL",
        "operation": "deploy-check",
        "live_environment_checked": require_live_environment,
        "project_id": project.project_id,
        "deployment": deployment.public_summary(),
        "package_signed": (
            (project.output / "package/registry.lock.sig.json").is_file()
            and (project.output / "package/package.manifest.sig.json").is_file()
        ),
        "credential": {
            "environment_variable": deployment.api_key_env,
            "present": credential_present,
        },
        "runtime_signer_pin": {
            "environment_variable": runtime_pin,
            "present": runtime_pin_present,
        },
        "official_sdk_present": sdk_present,
        "errors": errors,
    }


def run_project(
    project_path: Path,
    deployment_path: Path,
    *,
    run_id: str | None = None,
    initial_facts: dict[str, Any] | None = None,
    adapter: DeepSeekReadonlyAdapter | None = None,
) -> dict[str, Any]:
    project = load_project(project_path)
    deployment = load_deployment(deployment_path, project)
    live = adapter is None
    preflight = check_deployment(
        project_path,
        deployment_path,
        require_live_environment=live,
    )
    if preflight["result"] != "PASS":
        raise ValueError("Deployment preflight failed: " + "; ".join(preflight["errors"]))

    runtime_options = resolve_runtime_options(project.runtime.profile)
    runtime_provider = (
        deployment.runtime_signer.provider()
        if deployment.runtime_signer is not None
        else None
    )
    selected_adapter = adapter or DeepSeekReadonlyAdapter(
        settings=DeepSeekHarnessSettings(
            provider=project.runtime.provider,
            model=project.runtime.model,
            max_tokens=deployment.max_tokens,
            cwd=deployment.runtime_dir / "harness-workspace",
            session_root=deployment.runtime_dir / "harness-sessions",
            cordis=deployment.cordis,
            base_url=deployment.base_url,
            api_key=os.environ.get(deployment.api_key_env),
        )
    )
    facts = (
        initial_facts
        if initial_facts is not None
        else {
            "business": {
                "description": project.source.read_text(encoding="utf-8")
            }
        }
    )
    try:
        report = DeepSeekReadonlyRunner(
            project.output / "package",
            deployment.runtime_dir,
            selected_adapter,
            _trust_policy(deployment),
            runtime_provider,
            event_store_backend=runtime_options.event_store,
            lease_owner=runtime_options.lease_owner,
            lease_ttl_seconds=runtime_options.lease_ttl_seconds,
            retention_days=runtime_options.retention_days,
            require_runtime_signatures=runtime_options.require_runtime_signatures,
            require_runtime_rooted_trust=runtime_options.require_runtime_trust_root,
        ).run(
            facts,
            run_id=run_id or f"run-{project.project_id}",
        )
    finally:
        if adapter is None:
            selected_adapter.close()
    report["project_id"] = project.project_id
    report["deployment_id"] = deployment.deployment_id
    report["complexity_profile"] = project.runtime.profile
    return report
