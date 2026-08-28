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
    DeepSeekTrustPolicy,
)
from workflow_factory.signing import generate_signing_key  # noqa: E402
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


def execute(
    package: Path,
    facts: Path,
    runtime: Path,
    run_id: str,
    trust_policy: DeepSeekTrustPolicy,
) -> dict:
    return DeepSeekReadonlyRunner(
        package,
        runtime,
        DeepSeekReadonlyAdapter(client=ContractHarnessClient()),
        trust_policy,
    ).run(read_json(facts), run_id=run_id)


def main() -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)
    trust_store = BUILD / "trusted-publishers.json"
    shutil.copyfile(ROOT / "trust/trusted-publishers.json", trust_store)
    private_key = BUILD / "test-build-key.pem"
    generate_signing_key(
        private_key, trust_store, "agent-workflow-factory-build"
    )
    trust_policy = DeepSeekTrustPolicy(
        trust_store=trust_store,
        binding_manifest=ROOT / "adapters/deepseek-harness/readonly-tool-bindings.json",
        binding_signature=(
            ROOT / "adapters/deepseek-harness/readonly-tool-bindings.sig.json"
        ),
    )
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
        signing_key_path=private_key,
    )
    errors = validate_package(
        package, trust_store=trust_store, require_registry_signature=True
    )
    if errors:
        raise RuntimeError(f"Multinode package validation failed: {errors}")
    ready = execute(
        package,
        example / "initial-facts.json",
        BUILD / "runtime-ready",
        "run-multinode-ready",
        trust_policy,
    )
    ambiguous = execute(
        package,
        example / "ambiguous-facts.json",
        BUILD / "runtime-ambiguous",
        "run-multinode-ambiguous",
        trust_policy,
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
