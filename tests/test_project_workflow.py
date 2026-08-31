from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.project import (  # noqa: E402
    create_project,
    load_project,
    review_project,
    test_project,
)
from workflow_factory.util import write_json  # noqa: E402


class WorkflowProjectTest(unittest.TestCase):
    def _write_project(
        self,
        root: Path,
        *,
        source: Path | None = None,
        output: Path | None = None,
        extra: dict | None = None,
    ) -> Path:
        project = {
            "schema_version": "1.0.0",
            "project_id": "readonly-intent-review",
            "source": str(
                source
                or ROOT / "examples/readonly-intent-review/business-description.txt"
            ),
            "output": str(output or root / "generated"),
            "catalog": str(ROOT / "fixtures/catalog.snapshot.json"),
            "definition": str(ROOT / "contracts/system-definition.json"),
            "runtime": {
                "profile": "dev",
                "adapter": "deepseek",
                "provider": "deepseek-official",
                "model": "deepseek-v4-flash",
            },
        }
        if extra:
            project.update(extra)
        path = root / "workflow.project.json"
        write_json(path, project)
        return path

    def test_loads_project_and_resolves_relative_paths(self) -> None:
        project = load_project(
            ROOT / "examples/readonly-intent-review/workflow.project.json"
        )
        self.assertEqual(project.project_id, "readonly-intent-review")
        self.assertTrue(project.source.is_absolute())
        self.assertEqual(project.runtime.profile, "dev")

    def test_rejects_unknown_and_secret_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unknown = self._write_project(root, extra={"unexpected": True})
            with self.assertRaisesRegex(ValueError, "unknown fields"):
                load_project(unknown)

            secret = self._write_project(root, extra={"api_key": "must-not-live-here"})
            with self.assertRaisesRegex(ValueError, "must not contain secret field"):
                load_project(secret)

    def test_create_dry_run_shows_agent_tool_and_policy_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "must-not-exist"
            project_path = self._write_project(root, output=output)
            report = create_project(project_path, dry_run=True)

            self.assertEqual(report["result"], "PASS")
            self.assertFalse(report["external_calls"])
            self.assertFalse(report["writes_to_project_output"])
            self.assertFalse(output.exists())
            self.assertEqual(
                report["preview"]["agents"][0]["tools"],
                ["parse-business-intent"],
            )
            self.assertEqual(
                report["preview"]["tools"][0]["status"], "approved"
            )
            self.assertIn("runtime_requirements", report["preview"]["runtime_policy"])

    def test_create_review_and_integrity_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "generated"
            project_path = self._write_project(root, output=output)

            created = create_project(project_path)
            self.assertEqual(created["result"], "PASS")
            self.assertTrue((output / "process.bpmn").is_file())
            self.assertTrue((output / "workflow-overview.svg").is_file())
            self.assertEqual(review_project(project_path)["result"], "PASS")

            (output / "process.bpmn").write_text("tampered", encoding="utf-8")
            review = review_project(project_path)
            self.assertEqual(review["result"], "FAIL")
            self.assertIn(
                "deliverable digest mismatch: process.bpmn", review["errors"]
            )

            (output / "package/graph.json").unlink()
            missing_package = review_project(project_path)
            self.assertEqual(missing_package["result"], "FAIL")
            self.assertTrue(
                any(
                    "missing package artifact: graph.json" in error
                    for error in missing_package["errors"]
                )
            )

    def test_test_run_distinguishes_ready_and_unsupported_workflows(self) -> None:
        ready = test_project(
            ROOT / "examples/readonly-intent-review/workflow.project.json",
            dry_run=True,
        )
        self.assertEqual(ready["result"], "PASS")
        self.assertEqual(ready["runtime_readiness"]["status"], "READY")

        blocked = test_project(
            ROOT / "examples/expense-reimbursement/workflow.project.json",
            dry_run=True,
        )
        self.assertEqual(blocked["result"], "BLOCKED")
        self.assertIn(
            "human_gate", blocked["runtime_readiness"]["missing_capabilities"]
        )
        self.assertIn(
            "script_task", blocked["runtime_readiness"]["unsupported_action_kinds"]
        )

    def test_unbound_agent_is_not_reported_as_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "business.txt"
            source.write_text(
                """流程名称：未绑定 Agent 测试
流程目标：确认 Agent 没有被自动授予未知工具。
参与者：复核 Agent（智能体）
流程步骤：
1. 复核 Agent：执行未治理动作
2. 流程结束
""",
                encoding="utf-8",
            )
            project_path = self._write_project(root, source=source)
            report = test_project(project_path, dry_run=True)
            self.assertEqual(report["result"], "BLOCKED")
            self.assertEqual(
                report["runtime_readiness"]["unbound_execution_nodes"],
                ["step-001"],
            )

    def test_cli_short_commands_emit_machine_readable_reports(self) -> None:
        command = [
            sys.executable,
            str(ROOT / "scripts/workflowctl.py"),
            "test-run",
            str(ROOT / "examples/readonly-intent-review/workflow.project.json"),
            "--dry-run",
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["mode"], "deterministic-contract-test")


if __name__ == "__main__":
    unittest.main()
