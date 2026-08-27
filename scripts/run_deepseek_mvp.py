#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.business import generate_bpmn  # noqa: E402
from workflow_factory.compiler import compile_package  # noqa: E402
from workflow_factory.deepseek_harness import (  # noqa: E402
    DeepSeekReadonlyAdapter,
    DeepSeekReadonlyRunner,
)
from workflow_factory.util import read_json  # noqa: E402
from workflow_factory.validator import validate_package  # noqa: E402


BUILD = ROOT / "build/deepseek-readonly"


class ContractHarnessClient:
    """Deterministic SDK-shaped client used only for local/CI acceptance."""

    def run(self, input: str, *, session_id: str | None = None):
        payload = json.loads(input)
        observation = payload["trusted_tool_observation"]
        return SimpleNamespace(
            session_id=session_id or "contract-session",
            final_response=json.dumps(
                {
                    "status": "completed",
                    "facts": observation["facts"],
                    "evidence": [item["kind"] for item in observation["evidence"]],
                },
                ensure_ascii=False,
            ),
            finish_reason="completed",
            events=[{"type": "assistant/message"}, {"type": "turn/end"}],
        )

    def close(self) -> None:
        return None


def main() -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    package = BUILD / "package"
    bpmn = BUILD / "process.bpmn"
    business = ROOT / "examples/deepseek-readonly/business-requirement.json"
    generate_bpmn(business, bpmn)
    compile_package(
        bpmn,
        business,
        ROOT / "fixtures/catalog.snapshot.json",
        ROOT / "contracts/system-definition.json",
        package,
    )
    errors = validate_package(package)
    if errors:
        raise RuntimeError(f"DeepSeek MVP package validation failed: {errors}")
    report = DeepSeekReadonlyRunner(
        package,
        BUILD / "runtime",
        DeepSeekReadonlyAdapter(client=ContractHarnessClient()),
    ).run(
        read_json(ROOT / "examples/deepseek-readonly/initial-facts.json"),
        run_id="run-deepseek-contract-mvp",
    )
    if report["result"] != "PASS":
        raise RuntimeError(f"DeepSeek contract MVP failed: {report}")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
