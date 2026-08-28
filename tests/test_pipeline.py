from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.business import generate_bpmn, validate_business_requirement
from workflow_factory.compiler import compile_package
from workflow_factory.signing import generate_signing_key
from workflow_factory.util import read_json, write_json
from workflow_factory.validator import validate_package


class PipelineTest(unittest.TestCase):
    def test_example_compiles_and_validates(self) -> None:
        business = ROOT / "examples/governed-workflow-build/business-requirement.json"
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            bpmn = temp_path / "process.bpmn"
            package = temp_path / "package"
            generate_bpmn(business, bpmn)
            report = compile_package(
                bpmn,
                business,
                ROOT / "fixtures/catalog.snapshot.json",
                ROOT / "contracts/system-definition.json",
                package,
            )
            self.assertEqual(report["result"], "PASS")
            self.assertEqual(report["generated_agents"], 3)
            self.assertEqual(report["generated_loops"], 1)
            self.assertFalse(report["registry_lock_signed"])
            self.assertEqual(validate_package(package), [])
            lock = read_json(package / "registry.lock.json")
            self.assertEqual(lock["system_definition_version"], "3.0.0")
            self.assertEqual(len(lock["resolved_assets"]), 5)

    def test_signed_registry_lock_validates_and_tampering_fails(self) -> None:
        business = ROOT / "examples/deepseek-readonly/business-requirement.json"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            private_key = root / "build.pem"
            trust_store = root / "trust.json"
            generate_signing_key(
                private_key, trust_store, "agent-workflow-factory-build"
            )
            bpmn = root / "process.bpmn"
            package = root / "package"
            generate_bpmn(business, bpmn)
            report = compile_package(
                bpmn,
                business,
                ROOT / "fixtures/catalog.snapshot.json",
                ROOT / "contracts/system-definition.json",
                package,
                signing_key_path=private_key,
            )
            self.assertTrue(report["registry_lock_signed"])
            self.assertEqual(
                validate_package(
                    package,
                    trust_store=trust_store,
                    require_registry_signature=True,
                ),
                [],
            )
            lock = read_json(package / "registry.lock.json")
            lock["resolved_assets"][0]["version"] = "tampered"
            write_json(package / "registry.lock.json", lock)
            self.assertTrue(
                any(
                    "signature invalid" in error
                    for error in validate_package(
                        package,
                        trust_store=trust_store,
                        require_registry_signature=True,
                    )
                )
            )

    def test_agent_step_requires_explicit_responsibility(self) -> None:
        data = json.loads(
            (ROOT / "examples/governed-workflow-build/business-requirement.json").read_text(
                encoding="utf-8"
            )
        )
        for participant in data["participants"]:
            if participant["id"] == "lane-compiler":
                participant.pop("agent_ref", None)
        errors = validate_business_requirement(data)
        self.assertTrue(any("requires explicit agent_ref" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
