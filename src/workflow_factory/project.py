from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import tempfile
from typing import Any

from .complexity_profiles import profiles_report
from .deepseek_harness import DeepSeekReadonlyAdapter
from .runtime_contracts import negotiate
from .text_pipeline import build_from_business_text
from .util import read_json, sha256_file
from .validator import validate_package


PROJECT_SCHEMA_VERSION = "1.0.0"
PROJECT_FIELDS = {
    "$schema",
    "schema_version",
    "project_id",
    "source",
    "output",
    "catalog",
    "definition",
    "runtime",
}
RUNTIME_FIELDS = {"profile", "adapter", "provider", "model"}
SECRET_FIELDS = {
    "api_key",
    "apikey",
    "password",
    "pin",
    "private_key",
    "secret",
    "token",
}
SUPPORTED_ACTIONS = {"agent_task", "tool_task"}


@dataclass(frozen=True)
class ProjectRuntime:
    profile: str = "dev"
    adapter: str = "deepseek"
    provider: str = "deepseek-official"
    model: str = "deepseek-v4-flash"


@dataclass(frozen=True)
class WorkflowProject:
    config_path: Path
    schema_version: str
    project_id: str
    source: Path
    output: Path
    catalog: Path
    definition: Path
    runtime: ProjectRuntime

    def public_summary(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "source": str(self.source),
            "output": str(self.output),
            "catalog": str(self.catalog),
            "definition": str(self.definition),
            "runtime": asdict(self.runtime),
        }


def _unknown_fields(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {', '.join(unknown)}")


def _reject_secrets(value: Any, path: str = "project") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in SECRET_FIELDS or normalized.endswith(
                ("_secret", "_api_key", "_password", "_pin", "_private_key", "_token")
            ):
                raise ValueError(
                    f"Project configuration must not contain secret field: {path}.{key}"
                )
            _reject_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secrets(item, f"{path}[{index}]")


def _required_string(value: dict[str, Any], field: str, label: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        raise ValueError(f"{label}.{field} must be a non-empty string")
    return result.strip()


def _resolve_path(base: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def load_project(config_path: Path) -> WorkflowProject:
    config_path = config_path.resolve()
    if not config_path.is_file():
        raise ValueError(f"Project configuration does not exist: {config_path}")
    data = read_json(config_path)
    _reject_secrets(data)
    _unknown_fields(data, PROJECT_FIELDS, "project")
    if data.get("schema_version") != PROJECT_SCHEMA_VERSION:
        raise ValueError(
            f"project.schema_version must be {PROJECT_SCHEMA_VERSION}"
        )
    project_id = _required_string(data, "project_id", "project")
    if not re.fullmatch(r"[a-z][a-z0-9-]+", project_id):
        raise ValueError("project.project_id must use lowercase letters, digits and hyphens")

    base = config_path.parent
    source = _resolve_path(base, _required_string(data, "source", "project"))
    output = _resolve_path(base, _required_string(data, "output", "project"))
    catalog = _resolve_path(base, _required_string(data, "catalog", "project"))
    definition = _resolve_path(base, _required_string(data, "definition", "project"))
    for label, path in (
        ("source", source),
        ("catalog", catalog),
        ("definition", definition),
    ):
        if not path.is_file():
            raise ValueError(f"Project {label} does not exist: {path}")

    runtime_data = data.get("runtime", {})
    if not isinstance(runtime_data, dict):
        raise ValueError("project.runtime must be an object")
    _unknown_fields(runtime_data, RUNTIME_FIELDS, "project.runtime")
    defaults = ProjectRuntime()
    runtime = ProjectRuntime(
        profile=runtime_data.get("profile", defaults.profile),
        adapter=runtime_data.get("adapter", defaults.adapter),
        provider=runtime_data.get("provider", defaults.provider),
        model=runtime_data.get("model", defaults.model),
    )
    for field, value in asdict(runtime).items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"project.runtime.{field} must be a non-empty string")
    if runtime.profile not in profiles_report():
        raise ValueError("project.runtime.profile must be dev, team or regulated")
    if runtime.adapter != "deepseek":
        raise ValueError("v1.2 currently supports only the deepseek adapter")

    return WorkflowProject(
        config_path=config_path,
        schema_version=PROJECT_SCHEMA_VERSION,
        project_id=project_id,
        source=source,
        output=output,
        catalog=catalog,
        definition=definition,
        runtime=runtime,
    )


def _safe_deliverable(output: Path, relative_path: str) -> Path:
    path = (output / relative_path).resolve()
    try:
        path.relative_to(output.resolve())
    except ValueError as exc:
        raise ValueError(f"Deliverable escapes project output: {relative_path}") from exc
    return path


def _package_preview(project: WorkflowProject, output: Path) -> dict[str, Any]:
    manifest = read_json(output / "business-view.json")
    package = output / "package"
    graph = read_json(package / "graph.json")
    policy = read_json(package / "runtime.policy.json")
    lock = read_json(package / "registry.lock.json")
    agents = []
    for path in sorted((package / "agents").glob("*.agent.json")):
        profile = read_json(path)
        spec = profile["spec"]
        agents.append(
            {
                "id": profile["metadata"]["id"],
                "purpose": spec["purpose"],
                "tools": spec["tools"],
                "permissions": spec["permissions"],
            }
        )
    tools = [
        {
            "name": asset["name"],
            "version": asset["version"],
            "risk_level": asset["risk_level"],
            "status": asset["status"],
        }
        for asset in lock.get("resolved_assets", [])
        if asset.get("type") == "tool"
    ]
    spec = graph["spec"]
    unbound_execution_nodes = [
        node["id"]
        for node in spec["nodes"]
        if node["action"]["kind"] in SUPPORTED_ACTIONS
        and (
            not node["action"].get("agent_ref")
            or not node["action"].get("tool_ref")
        )
    ]
    return {
        "project": project.public_summary(),
        "workflow": {
            "id": manifest["workflow_id"],
            "name": manifest["name"],
            "intent": manifest["intent"],
            "business_status": manifest["status"],
        },
        "business_review": manifest["business_review"],
        "deliverables": manifest["deliverables"],
        "graph": {
            "entry": spec["entry"],
            "terminals": spec["terminals"],
            "nodes": len(spec["nodes"]),
            "edges": len(spec["edges"]),
            "action_kinds": sorted(
                {node["action"]["kind"] for node in spec["nodes"]}
            ),
            "unbound_execution_nodes": unbound_execution_nodes,
        },
        "agents": agents,
        "tools": tools,
        "runtime_policy": policy["spec"],
        "complexity_profile": profiles_report()[project.runtime.profile],
    }


def create_project(config_path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    project = load_project(config_path)
    if dry_run:
        with tempfile.TemporaryDirectory(prefix="awf-project-preview-") as temporary:
            preview_output = Path(temporary) / project.project_id
            build_from_business_text(
                project.source,
                project.catalog,
                project.definition,
                preview_output,
                workflow_id=project.project_id,
            )
            preview = _package_preview(project, preview_output)
        return {
            "result": "PASS",
            "operation": "create",
            "dry_run": True,
            "external_calls": False,
            "writes_to_project_output": False,
            "preview": preview,
        }

    if project.output.is_file():
        raise ValueError(f"Project output is a file: {project.output}")
    if project.output.exists() and any(project.output.iterdir()):
        raise ValueError(
            f"Project output is not empty: {project.output}; choose a new output or archive it"
        )
    build_from_business_text(
        project.source,
        project.catalog,
        project.definition,
        project.output,
        workflow_id=project.project_id,
    )
    return {
        "result": "PASS",
        "operation": "create",
        "dry_run": False,
        "external_calls": False,
        "writes_to_project_output": True,
        "output": str(project.output),
        "preview": _package_preview(project, project.output),
    }


def review_project(config_path: Path) -> dict[str, Any]:
    project = load_project(config_path)
    output = project.output
    if not output.is_dir():
        raise ValueError(f"Project output does not exist; run create first: {output}")
    manifest_path = output / "business-view.json"
    if not manifest_path.is_file():
        raise ValueError(f"Missing business review manifest: {manifest_path}")
    manifest = read_json(manifest_path)
    integrity_errors: list[str] = []
    for deliverable in manifest.get("deliverables", []):
        relative_path = deliverable.get("path")
        if not isinstance(relative_path, str):
            integrity_errors.append("deliverable has no path")
            continue
        try:
            path = _safe_deliverable(output, relative_path)
        except ValueError as exc:
            integrity_errors.append(str(exc))
            continue
        if not path.is_file():
            integrity_errors.append(f"missing deliverable: {relative_path}")
        elif sha256_file(path) != deliverable.get("digest"):
            integrity_errors.append(f"deliverable digest mismatch: {relative_path}")
    package_errors = validate_package(output / "package")
    errors = [*integrity_errors, *package_errors]
    try:
        preview = _package_preview(project, output)
    except (OSError, ValueError, KeyError) as exc:
        errors.append(f"preview unavailable: {exc}")
        preview = {
            "project": project.public_summary(),
            "workflow": {
                "id": manifest.get("workflow_id"),
                "name": manifest.get("name"),
                "intent": manifest.get("intent"),
                "business_status": manifest.get("status"),
            },
            "business_review": manifest.get("business_review", {}),
            "deliverables": manifest.get("deliverables", []),
        }
    return {
        "result": "PASS" if not errors else "FAIL",
        "operation": "review",
        "review_decision": manifest.get("status", "REVIEW_REQUIRED"),
        "errors": errors,
        "preview": preview,
    }


def _runtime_readiness(preview: dict[str, Any]) -> dict[str, Any]:
    requirements = preview["runtime_policy"]["runtime_requirements"]
    missing_capabilities = negotiate(
        requirements, DeepSeekReadonlyAdapter.CAPABILITIES
    )
    unsupported_actions = sorted(
        set(preview["graph"]["action_kinds"])
        - SUPPORTED_ACTIONS
        - {"start", "terminal", "choice", "parallel"}
    )
    blockers = [
        f"DeepSeek 只读适配器缺少能力：{capability}"
        for capability in missing_capabilities
    ]
    blockers.extend(
        f"DeepSeek 只读适配器不能执行节点类型：{kind}"
        for kind in unsupported_actions
    )
    unbound_nodes = preview["graph"].get("unbound_execution_nodes", [])
    blockers.extend(
        f"节点缺少经过固定的 Agent 或 Tool 绑定：{node_id}"
        for node_id in unbound_nodes
    )
    return {
        "status": "READY" if not blockers else "BLOCKED",
        "adapter": "deepseek",
        "supported_action_kinds": sorted(SUPPORTED_ACTIONS),
        "missing_capabilities": missing_capabilities,
        "unsupported_action_kinds": unsupported_actions,
        "unbound_execution_nodes": unbound_nodes,
        "blockers": blockers,
    }


def test_project(config_path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        create_report = create_project(config_path, dry_run=True)
        preview = create_report["preview"]
        review_result = "PASS"
        errors: list[str] = []
    else:
        review = review_project(config_path)
        preview = review["preview"]
        review_result = review["result"]
        errors = review["errors"]
    readiness = (
        _runtime_readiness(preview)
        if review_result == "PASS"
        else {
            "status": "BLOCKED",
            "adapter": "deepseek",
            "supported_action_kinds": sorted(SUPPORTED_ACTIONS),
            "missing_capabilities": [],
            "unsupported_action_kinds": [],
            "unbound_execution_nodes": [],
            "blockers": ["项目复核未通过，不能进行运行能力预检"],
        }
    )
    successful = review_result == "PASS" and readiness["status"] == "READY"
    return {
        "result": "PASS" if successful else "BLOCKED",
        "operation": "test-run",
        "mode": "deterministic-contract-test",
        "dry_run": dry_run,
        "external_calls": False,
        "writes_to_project_output": False,
        "package_validation": review_result,
        "runtime_readiness": readiness,
        "errors": errors,
        "preview": preview,
    }
