"""Reviewed SDK composition and isolated-home checks (no model calls)."""
from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform

PINNED_SDK_VERSION = "0.1.5rc1"
READONLY_PROFILE = "sdk-minimal"
READONLY_PATCH_DIGEST = "4c3c4a207cab4ac1dea8bd625500ed763a556dae9446be0fa559719209f2427b"


def supported_platform() -> bool:
    system, machine = platform.system(), platform.machine().lower()
    return (system, machine) in {
        ("Windows", "amd64"), ("Windows", "x86_64"),
        ("Linux", "x86_64"), ("Linux", "amd64"),
        ("Linux", "aarch64"), ("Linux", "arm64"),
        ("Darwin", "arm64"),
    }


def sdk_environment() -> dict:
    errors = []
    installed = {}
    for package in ("deepseek-harness-sdk", "deepseek-harness-runtime-bin"):
        try:
            installed[package] = version(package)
        except PackageNotFoundError:
            installed[package] = None
        if installed[package] != PINNED_SDK_VERSION:
            errors.append(f"{package}: expected {PINNED_SDK_VERSION}, found {installed[package]}")
    if not supported_platform():
        errors.append("Unsupported SDK platform/architecture; use Windows x64, Linux x64/arm64 or macOS arm64")
    if platform.system() == "Darwin":
        major = platform.mac_ver()[0].split(".")[0]
        if not major.isdigit() or int(major) < 14:
            errors.append("SDK requires macOS 14 or newer")
    # These variables can inject arbitrary code before the reviewed profile boots.
    for key in ("NODE_OPTIONS", "NODE_PATH"):
        if os.environ.get(key):
            errors.append(f"Unset {key} for the isolated SDK runtime")
    return {"packages": installed, "platform": platform.system(),
            "architecture": platform.machine(), "errors": errors}


def validate_composition(dsh_home: Path | None, profile: str, patches: tuple[Path, ...]) -> dict:
    if profile != READONLY_PROFILE:
        raise ValueError("Only the reviewed sdk-minimal profile is supported; Desktop profiles are not allowed")
    if dsh_home is None or not dsh_home.is_absolute():
        raise ValueError("An explicit absolute, isolated dsh_home is required")
    home = dsh_home.resolve()
    if home.exists() and not home.is_dir():
        raise ValueError("dsh_home must be a directory")
    if home == (Path.home() / ".dsh").resolve():
        raise ValueError("dsh_home must not reuse the personal/Desktop Harness home")
    if len(patches) != 1 or not patches[0].is_file():
        raise ValueError("Exactly one reviewed read-only patch is required")
    digest = hashlib.sha256(patches[0].read_bytes()).hexdigest()
    if digest != READONLY_PATCH_DIGEST:
        raise ValueError("Patch differs from the reviewed read-only configuration")
    # Inspect text, never evaluate YAML !!js from ambient/user-controlled layers.
    for base in (home, home / "profiles" / profile):
        for name in (".env", ".env.local"):
            if (base / name).exists():
                raise ValueError(f"Isolated Harness home must not contain {name}")
        for name in ("cordis.patch.yml", "cordis.yml"):
            overlay = base / name
            if overlay.exists():
                meaningful = "".join(line.strip() for line in overlay.read_text(encoding="utf-8").splitlines()
                                     if line.strip() and not line.lstrip().startswith("#"))
                if meaningful not in {"", "[]"}:
                    raise ValueError("Unreviewed home/profile patch or root is not allowed")
    profiles = home / "profiles"
    if profiles.exists():
        for child in profiles.iterdir():
            if child.is_dir() and child.name not in {profile, "node_modules"}:
                raise ValueError("Harness home contains another profile; use an isolated SDK home")
    manifest = profiles / profile / "package.json"
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        expected = {"bundles": ["@deepseek-ai/dsh-sdk-minimal"], "patchReload": "startup"}
        if not isinstance(data, dict) or not isinstance(data.get("dsh"), dict):
            raise ValueError("Invalid SDK profile manifest")
        if data["dsh"].get("profile") != expected:
            raise ValueError("Unreviewed profile manifest/bundle order")
        if any(data.get(key) for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies", "scripts")):
            raise ValueError("External dependencies/scripts are not allowed in the SDK profile")
    return {"sdk_version": PINNED_SDK_VERSION, "runtime_version": PINNED_SDK_VERSION,
            "profile": profile, "patch_digests": ["sha256:" + digest],
            "platform": platform.system(), "architecture": platform.machine()}
