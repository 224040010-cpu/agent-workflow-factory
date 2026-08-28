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


BUILD = ROOT / "build/deepseek-readonly-multinode"


class ContractHarnessClient:
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


def execute(package: Path, facts: Path, runtime: Path, run_id: str) -> dict:
    return DeepSeekReadonlyRunner(
        package,
        runtime,
        DeepSeekReadonlyAdapter(client=ContractHarnessClient()),
    ).run(read_json(facts), run_id=run_id)


def main() -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    package = BUILD / "package"
    bpmn = BUILD / "process.bpmn"
    example = ROOT / "examples/deepseek-readonly-multinode"
    business = example / "business-requirement.json"
    generate_bpmn(business, bpmn)
    compile_report = compile_package(
        bpmn,
        business,
        ROOT / "fixtures/catalog.snapshot.json",
        ROOT / "contracts/system-definition.json",
        package,
    )
    errors = validate_package(package)
    if errors:
        raise RuntimeError(f"Multinode package validation failed: {errors}")
    ready = execute(
        package,
        example / "initial-facts.json",
        BUILD / "runtime-ready",
        "run-multinode-ready",
    )
    ambiguous = execute(
        package,
        example / "ambiguous-facts.json",
        BUILD / "runtime-ambiguous",
        "run-multinode-ambiguous",
    )
    result = {
        "result": "PASS"
        if ready["result"] == ambiguous["result"] == "PASS"
        else "FAIL",
        "generated_agents": compile_report["generated_agents"],
        "resolved_tools": compile_report["resolved_tools"],
        "ready_path": ready,
        "ambiguous_path": ambiguous,
    }
    if result["result"] != "PASS":
        raise RuntimeError(f"DeepSeek multinode contract MVP failed: {result}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
