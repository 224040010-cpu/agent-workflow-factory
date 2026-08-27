from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .util import read_json, sha256_file


ELIGIBLE_STATUSES = {"approved", "restricted"}


@dataclass(frozen=True)
class ResolvedCatalog:
    snapshot_digest: str
    system_definition_version: str
    system_definition_digest: str
    skills: list[dict]
    tools: list[dict]

    def lockfile(self) -> dict:
        assets = [
            {"type": "skill", **asset} for asset in self.skills
        ] + [{"type": "tool", **asset} for asset in self.tools]
        assets.sort(key=lambda item: (item["type"], item["name"]))
        return {
            "schema_version": "1.0.0",
            "catalog_digest": self.snapshot_digest,
            "system_definition_version": self.system_definition_version,
            "system_definition_digest": self.system_definition_digest,
            "resolved_assets": assets,
        }


def _asset_index(items: list[dict], asset_type: str) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for item in items:
        name = item.get("name")
        if not name:
            raise ValueError(f"Catalog {asset_type} descriptor has no name")
        if name in index:
            raise ValueError(f"Catalog contains duplicate {asset_type}: {name}")
        for field in ("version", "status", "risk_level", "digest"):
            if not item.get(field):
                raise ValueError(f"Catalog {asset_type} {name} is missing {field}")
        index[name] = item
    return index


def _pin(item: dict) -> dict:
    keys = (
        "name",
        "version",
        "status",
        "risk_level",
        "digest",
        "endpoint",
        "side_effects",
        "requires_approval",
        "idempotent",
        "bundle_scope",
    )
    return {key: item[key] for key in keys if key in item}


def resolve_catalog(
    catalog_path: Path,
    required_skills: list[str],
    required_tools: list[str],
    expected_definition_version: str,
) -> ResolvedCatalog:
    catalog = read_json(catalog_path)
    if catalog.get("schema_version") != "2.0.0":
        raise ValueError("Capability Catalog schema_version must be 2.0.0")
    source = catalog.get("source", {})
    actual_version = source.get("system_definition_version")
    if actual_version != expected_definition_version:
        raise ValueError(
            "Catalog system-definition mismatch: "
            f"expected {expected_definition_version}, got {actual_version}"
        )

    skills = _asset_index(catalog.get("skills", []), "skill")
    tools = _asset_index(catalog.get("tools", []), "tool")

    def select(index: dict[str, dict], names: list[str], asset_type: str) -> list[dict]:
        selected: list[dict] = []
        for name in sorted(set(names)):
            item = index.get(name)
            if item is None:
                raise ValueError(f"Required {asset_type} is absent from Catalog: {name}")
            if item["status"] not in ELIGIBLE_STATUSES:
                raise ValueError(
                    f"Required {asset_type} {name} has ineligible status {item['status']}"
                )
            selected.append(_pin(item))
        return selected

    return ResolvedCatalog(
        snapshot_digest=sha256_file(catalog_path),
        system_definition_version=actual_version,
        system_definition_digest=source.get("system_definition_digest", ""),
        skills=select(skills, required_skills, "skill"),
        tools=select(tools, required_tools, "tool"),
    )
