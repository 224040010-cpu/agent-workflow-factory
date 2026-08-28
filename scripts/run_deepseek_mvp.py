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
from workflow_factory.signing import (  # noqa: E402
    generate_root_key,
    generate_signing_key,
    sign_artifact,
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
    BUILD.mkdir(parents=True)
    trust_store = BUILD / "trusted-publishers.json"
    shutil.copyfile(ROOT / "trust/trusted-publishers.json", trust_store)
    private_key = BUILD / "test-build-key.pem"
    generate_signing_key(
        private_key, trust_store, "agent-workflow-factory-build"
    )
    root_private = BUILD / "test-root-key.pem"
    root_public = BUILD / "test-root-public.json"
    trust_signature = BUILD / "trusted-publishers.sig.json"
    generate_root_key(root_private, root_public)
    sign_artifact(
        trust_store,
        root_private,
        trust_signature,
        "agent-workflow-factory-trust-root",
    )
    trust_policy = DeepSeekTrustPolicy(
        trust_store=trust_store,
        trust_store_signature=trust_signature,
        trust_root_public_key=root_public,
        binding_manifest=ROOT / "adapters/deepseek-harness/readonly-tool-bindings.json",
        binding_signature=(
            ROOT / "adapters/deepseek-harness/readonly-tool-bindings.sig.json"
        ),
    )
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
        signing_key_path=private_key,
    )
    errors = validate_package(
        package,
        trust_store=trust_store,
        require_registry_signature=True,
        require_package_signature=True,
        trust_store_signature=trust_signature,
        trust_root_public_key=root_public,
        require_trust_root=True,
    )
    if errors:
        raise RuntimeError(f"DeepSeek MVP package validation failed: {errors}")
    report = DeepSeekReadonlyRunner(
        package,
        BUILD / "runtime",
        DeepSeekReadonlyAdapter(client=ContractHarnessClient()),
        trust_policy,
    ).run(
        read_json(ROOT / "examples/deepseek-readonly/initial-facts.json"),
        run_id="run-deepseek-contract-mvp",
    )
    if report["result"] != "PASS":
        raise RuntimeError(f"DeepSeek contract MVP failed: {report}")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
