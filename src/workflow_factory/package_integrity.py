from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from .signing import (
    FileEd25519SigningProvider,
    SigningProvider,
    artifact_digest,
    sign_artifact_with_provider,
    verify_artifact,
)
from .util import read_json, write_json


MANIFEST_NAME = "package.manifest.json"
SIGNATURE_NAME = "package.manifest.sig.json"
EXCLUDED = {MANIFEST_NAME, SIGNATURE_NAME}


def _media_type(path: Path) -> str:
    if path.suffix == ".json":
        return "application/json"
    if path.suffix == ".bpmn":
        return "application/xml"
    raise ValueError(f"Unsupported package artifact type: {path.name}")


def _package_files(package_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in package_dir.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Package must not contain symbolic links: {path}")
        if path.is_file() and path.relative_to(package_dir).as_posix() not in EXCLUDED:
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(package_dir).as_posix())


def build_package_manifest(package_dir: Path) -> dict:
    workflow = read_json(package_dir / "workflow.ir.json")
    artifacts = []
    for path in _package_files(package_dir):
        relative = path.relative_to(package_dir).as_posix()
        artifacts.append(
            {
                "path": relative,
                "digest": artifact_digest(path),
                "size": path.stat().st_size,
                "media_type": _media_type(path),
            }
        )
    return {
        "schema_version": "1.0.0",
        "package_format": "agent-workflow-factory/v0.9",
        "workflow_id": workflow["metadata"]["id"],
        "workflow_version": workflow["metadata"]["version"],
        "artifacts": artifacts,
    }


def write_signed_package_manifest(
    package_dir: Path,
    private_key_path: Path | None = None,
    publisher: str = "agent-workflow-factory-build",
    signing_provider: SigningProvider | None = None,
) -> dict:
    if (private_key_path is None) == (signing_provider is None):
        raise ValueError("Provide exactly one package signing key or provider")
    provider = signing_provider or FileEd25519SigningProvider(private_key_path)
    manifest_path = package_dir / MANIFEST_NAME
    write_json(manifest_path, build_package_manifest(package_dir))
    sign_artifact_with_provider(
        manifest_path,
        provider,
        package_dir / SIGNATURE_NAME,
        publisher,
    )
    return read_json(manifest_path)


def _safe_manifest_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        raise ValueError("Package manifest artifact path must be non-empty text")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != raw:
        raise ValueError(f"Package manifest contains unsafe path: {raw}")
    if raw in EXCLUDED:
        raise ValueError(f"Package manifest must not include itself: {raw}")
    return raw


def verify_package_manifest(
    package_dir: Path,
    trust_store_path: Path,
    expected_publisher: str = "agent-workflow-factory-build",
) -> dict:
    manifest_path = package_dir / MANIFEST_NAME
    signature_report = verify_artifact(
        manifest_path,
        package_dir / SIGNATURE_NAME,
        trust_store_path,
        expected_publisher,
    )
    manifest = read_json(manifest_path)
    if manifest.get("schema_version") != "1.0.0":
        raise ValueError("Unsupported package manifest schema_version")
    if manifest.get("package_format") != "agent-workflow-factory/v0.9":
        raise ValueError("Unsupported package format")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("Package manifest artifacts must be an array")
    declared: dict[str, dict] = {}
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {
            "path", "digest", "size", "media_type"
        }:
            raise ValueError("Package manifest artifact has an invalid shape")
        relative = _safe_manifest_path(item.get("path"))
        if relative in declared:
            raise ValueError(f"Package manifest contains duplicate path: {relative}")
        if item.get("media_type") != _media_type(PurePosixPath(relative)):
            raise ValueError(f"Unsupported package artifact media type: {relative}")
        declared[relative] = item
    actual = {
        path.relative_to(package_dir).as_posix(): path
        for path in _package_files(package_dir)
    }
    missing = sorted(set(declared) - set(actual))
    injected = sorted(set(actual) - set(declared))
    if missing:
        raise ValueError("Package files are missing: " + ", ".join(missing))
    if injected:
        raise ValueError("Package contains unsigned files: " + ", ".join(injected))
    for relative, item in declared.items():
        path = actual[relative]
        if item.get("digest") != artifact_digest(path):
            raise ValueError(f"Package artifact digest mismatch: {relative}")
        if item.get("size") != path.stat().st_size:
            raise ValueError(f"Package artifact size mismatch: {relative}")
    workflow = read_json(package_dir / "workflow.ir.json")
    if manifest.get("workflow_id") != workflow["metadata"]["id"]:
        raise ValueError("Package manifest workflow_id mismatch")
    if manifest.get("workflow_version") != workflow["metadata"]["version"]:
        raise ValueError("Package manifest workflow_version mismatch")
    material = "\n".join(
        f"{path}:{declared[path]['digest']}" for path in sorted(declared)
    ).encode("utf-8")
    return {
        "result": "PASS",
        "artifact": MANIFEST_NAME,
        "digest": signature_report["digest"],
        "publisher": expected_publisher,
        "key_id": signature_report["key_id"],
        "key_status": signature_report["key_status"],
        "files": len(declared),
        "contents_digest": "sha256:" + hashlib.sha256(material).hexdigest(),
    }
