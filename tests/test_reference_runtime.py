from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.business import generate_bpmn  # noqa: E402
from workflow_factory.compiler import compile_package  # noqa: E402
from workflow_factory.reference_runtime import ReferenceRuntime  # noqa: E402


class ReferenceRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.package = root / "package"
        self.runtime_dir = root / "runtime"
        business = ROOT / "examples/governed-workflow-build/business-requirement.json"
        bpmn = root / "process.bpmn"
        generate_bpmn(business, bpmn)
        compile_package(
            bpmn,
            business,
            ROOT / "fixtures/catalog.snapshot.json",
            ROOT / "contracts/system-definition.json",
            self.package,
        )
        self.runtime = ReferenceRuntime(self.package, self.runtime_dir)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def complete_until_review(self, run_id: str) -> None:
        updates = [
            ("parse-requirement", {"intent": {"parsed": True}}),
            ("assemble-model", {"bpmn": {"model_exists": True}}),
            (
                "compile-graph",
                {"package": {"workflow_ir_exists": True, "registry_lock_exists": True}},
            ),
        ]
        for node_id, facts in updates:
            route = self.runtime.route(run_id)
            self.assertEqual(route.node_id, node_id)
            self.runtime.complete(run_id, node_id, facts)

    def test_successful_run_pause_resume_and_replay(self) -> None:
        run_id = "run-success"
        self.runtime.start(run_id=run_id)
        first = self.runtime.route(run_id)
        self.assertEqual(first.node_id, "parse-requirement")
        event_count = len(self.runtime.events.read(run_id))
        self.assertEqual(self.runtime.route(run_id), first)
        self.assertEqual(len(self.runtime.events.read(run_id)), event_count)

        self.runtime.pause(run_id, "operator check")
        self.assertEqual(self.runtime.route(run_id).status, "paused")
        self.runtime.resume(run_id)
        self.runtime.complete(run_id, "parse-requirement", {"intent": {"parsed": True}})
        for node_id, facts in [
            ("assemble-model", {"bpmn": {"model_exists": True}}),
            (
                "compile-graph",
                {"package": {"workflow_ir_exists": True, "registry_lock_exists": True}},
            ),
            ("validate-bpmn", {"review": {"completed": True, "passed": True}}),
        ]:
            self.assertEqual(self.runtime.route(run_id).node_id, node_id)
            self.runtime.complete(run_id, node_id, facts)
        self.assertEqual(self.runtime.route(run_id).status, "completed")
        self.assertEqual(self.runtime.replay(run_id)["result"], "PASS")

    def test_rejects_missing_evidence(self) -> None:
        run_id = "run-evidence"
        self.runtime.start(run_id=run_id)
        self.runtime.route(run_id)
        with self.assertRaisesRegex(ValueError, "Completion evidence failed"):
            self.runtime.complete(run_id, "parse-requirement", {})
        self.assertNotIn("intent", self.runtime.load_state(run_id)["facts"])

    def test_failed_review_enters_bounded_loop(self) -> None:
        run_id = "run-loop"
        self.runtime.start(run_id=run_id)
        self.complete_until_review(run_id)
        self.assertEqual(self.runtime.route(run_id).node_id, "validate-bpmn")
        self.runtime.complete(
            run_id,
            "validate-bpmn",
            {"review": {"completed": True, "passed": False}},
        )
        self.assertEqual(self.runtime.route(run_id).node_id, "human-review")
        self.runtime.complete(
            run_id,
            "human-review",
            {"approval": {"decision_recorded": True}},
        )
        self.assertEqual(self.runtime.route(run_id).node_id, "assemble-model")
        self.assertEqual(self.runtime.load_state(run_id)["loop_rounds"], 1)

    def test_tampered_trajectory_fails_replay(self) -> None:
        run_id = "run-tampered"
        self.runtime.start(run_id=run_id)
        path = self.runtime.events.path(run_id)
        events = self.runtime.events.read(run_id)
        events[0]["payload"]["workflow_id"] = "tampered"
        path.write_text(
            "\n".join(json.dumps(item, sort_keys=True) for item in events) + "\n",
            encoding="utf-8",
        )
        report = self.runtime.replay(run_id)
        self.assertEqual(report["result"], "FAIL")
        self.assertTrue(any("event hash mismatch" in item for item in report["errors"]))


if __name__ == "__main__":
    unittest.main()
