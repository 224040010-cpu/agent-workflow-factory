#!/usr/bin/env python3
"""Verify the shared cross-repository system definition and optional peer mirror."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


REQUIRED_TOP_LEVEL = {
    "schema_version",
    "definition_version",
    "contract_id",
    "canonical_repository",
    "repositories",
    "asset_types",
    "primary_pipeline",
    "cross_repository_contracts",
    "execution_invariants",
    "synchronization",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_shape(data: dict) -> list[str]:
    errors: list[str] = []
    missing = sorted(REQUIRED_TOP_LEVEL - set(data))
    if missing:
        errors.append(f"missing top-level fields: {', '.join(missing)}")
    if data.get("contract_id") != "skill-registry-agent-workflow-system":
        errors.append("contract_id is not skill-registry-agent-workflow-system")
    if data.get("canonical_repository") != "skill-registory":
        errors.append("canonical_repository must be skill-registory")
    repositories = data.get("repositories", {})
    for name in ("skill-registory", "agent-workflow-factory"):
        if name not in repositories:
            errors.append(f"repositories.{name} is missing")
    contracts = data.get("cross_repository_contracts", {})
    for name in ("catalog_snapshot", "registry_lock", "governance_feedback"):
        if name not in contracts:
            errors.append(f"cross_repository_contracts.{name} is missing")
    if len(data.get("primary_pipeline", [])) < 7:
        errors.append("primary_pipeline must contain P0 through P6")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--definition", default="contracts/system-definition.json")
    parser.add_argument("--checksum", default="contracts/system-definition.sha256")
    parser.add_argument("--peer", help="Optional peer system-definition.json to compare byte-for-byte")
    parser.add_argument("--write-checksum", action="store_true")
    args = parser.parse_args()

    definition = Path(args.definition)
    checksum_file = Path(args.checksum)
    if not definition.is_file():
        print(f"ERROR: missing definition: {definition}", file=sys.stderr)
        return 2

    try:
        data = json.loads(definition.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: invalid definition: {exc}", file=sys.stderr)
        return 2

    errors = validate_shape(data)
    digest = sha256(definition)

    if args.write_checksum:
        checksum_file.write_text(f"{digest}  system-definition.json\n", encoding="ascii")
    elif not checksum_file.is_file():
        errors.append(f"checksum file is missing: {checksum_file}")
    else:
        expected = checksum_file.read_text(encoding="ascii").split()[0]
        if expected != digest:
            errors.append(f"checksum mismatch: expected {expected}, got {digest}")

    if args.peer:
        peer = Path(args.peer)
        if not peer.is_file():
            errors.append(f"peer definition is missing: {peer}")
        elif peer.read_bytes() != definition.read_bytes():
            errors.append(f"peer definition differs byte-for-byte: {peer}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(
        f"System definition {data['definition_version']} verified "
        f"({digest[:12]}..., {len(data['primary_pipeline'])} pipeline stages)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
