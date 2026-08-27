#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
CLI = ROOT / "scripts/workflowctl.py"
EXAMPLE = ROOT / "examples/governed-workflow-build"
BUILD = ROOT / "build/governed-workflow-build"


def run(*args: str) -> None:
    subprocess.run([PYTHON, str(CLI), *args], cwd=ROOT, check=True)


def main() -> None:
    run("verify-definition")
    run(
        "generate-bpmn",
        str(EXAMPLE / "business-requirement.json"),
        "--output",
        str(BUILD / "process.bpmn"),
    )
    run(
        "compile",
        str(BUILD / "process.bpmn"),
        "--business",
        str(EXAMPLE / "business-requirement.json"),
        "--catalog",
        str(ROOT / "fixtures/catalog.snapshot.json"),
        "--output",
        str(BUILD / "package"),
    )
    run("validate", str(BUILD / "package"))


if __name__ == "__main__":
    main()
