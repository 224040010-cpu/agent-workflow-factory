from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.project import (  # noqa: E402
    create_project,
    load_project,
    review_project,
    test_project,
)
from workflow_factory.deepseek_harness import DeepSeekReadonlyAdapter  # noqa: E402
from workflow_factory.deployment import (  # noqa: E402
    check_deployment,
    create_project_for_deployment,
    load_deployment,
    run_project,
)
from workflow_factory.signing import (  # noqa: E402
    generate_root_key,
    generate_signing_key,
    sign_artifact,
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

    def test_tool_without_reviewed_host_binding_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "business.txt"
            source.write_text(
                """流程名称：实体提取测试
流程目标：验证只有 Catalog、没有 Host Binding 的工具不能执行。
参与者：流程 Agent（智能体）
流程步骤：
1. 流程 Agent：提取流程实体
2. 流程结束
""",
                encoding="utf-8",
            )
            project_path = self._write_project(root, source=source)
            report = test_project(project_path, dry_run=True)
            self.assertEqual(report["result"], "BLOCKED")
            self.assertEqual(
                report["runtime_readiness"]["unavailable_tool_bindings"],
                ["extract-process-entities"],
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


class ContractHarnessClient:
    def run(self, input: str, *, session_id: str | None = None):
        payload = json.loads(input)
        observation = payload["trusted_tool_observation"]
        return SimpleNamespace(
            session_id=session_id or "project-contract-session",
            final_response=json.dumps(
                {
                    "status": "completed",
                    "facts": observation["facts"],
                    "evidence": [
                        item["kind"] for item in observation["evidence"]
                    ],
                },
                ensure_ascii=False,
            ),
            finish_reason="completed",
            events=[{"type": "assistant/message"}, {"type": "turn/end"}],
        )

    def close(self) -> None:
        return None


class ProjectDeploymentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "generated"
        self.runtime = self.root / "runtime"
        self.project_path = self.root / "workflow.project.json"
        write_json(
            self.project_path,
            {
                "schema_version": "1.0.0",
                "project_id": "readonly-intent-review",
                "source": str(
                    ROOT / "examples/readonly-intent-review/business-description.txt"
                ),
                "output": str(self.output),
                "catalog": str(ROOT / "fixtures/catalog.snapshot.json"),
                "definition": str(ROOT / "contracts/system-definition.json"),
                "runtime": {
                    "profile": "dev",
                    "adapter": "deepseek",
                    "provider": "deepseek-official",
                    "model": "deepseek-v4-flash",
                    "deployment_ref": "local-deepseek-dev",
                },
            },
        )

        self.trust_store = self.root / "trusted-publishers.json"
        shutil.copyfile(ROOT / "trust/trusted-publishers.json", self.trust_store)
        self.build_key = self.root / "build-key.pem"
        self.runtime_key = self.root / "runtime-key.pem"
        generate_signing_key(
            self.build_key,
            self.trust_store,
            "agent-workflow-factory-build",
        )
        generate_signing_key(
            self.runtime_key,
            self.trust_store,
            "agent-workflow-factory-runtime",
        )
        root_private = self.root / "root-key.pem"
        self.root_public = self.root / "root-public.json"
        self.trust_signature = self.root / "trusted-publishers.sig.json"
        generate_root_key(root_private, self.root_public)
        sign_artifact(
            self.trust_store,
            root_private,
            self.trust_signature,
            "agent-workflow-factory-trust-root",
        )

        self.deployment_path = self.root / "workflow.deployment.json"
        self._write_deployment()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _deployment_data(self) -> dict:
        return {
            "schema_version": "1.0.0",
            "deployment_id": "local-deepseek-dev",
            "runtime_dir": str(self.runtime),
            "cordis": str(ROOT / "adapters/deepseek-harness/readonly.cordis.yml"),
            "artifact_trust": {
                "trust_store": str(self.trust_store),
                "trust_store_signature": str(self.trust_signature),
                "trust_root_public_key": str(self.root_public),
                "binding_manifest": str(
                    ROOT
                    / "adapters/deepseek-harness/readonly-tool-bindings.json"
                ),
                "binding_signature": str(
                    ROOT
                    / "adapters/deepseek-harness/readonly-tool-bindings.sig.json"
                ),
            },
            "build_signer": {
                "kind": "pem",
                "private_key_path": str(self.build_key),
            },
            "runtime_signer": {
                "kind": "pem",
                "private_key_path": str(self.runtime_key),
            },
            "api_key_env": "DEEPSEEK_API_KEY",
        }

    def _write_deployment(self, extra: dict | None = None) -> None:
        data = self._deployment_data()
        if extra:
            data.update(extra)
        write_json(self.deployment_path, data)

    def test_deployment_rejects_inline_secrets_and_mismatched_reference(self) -> None:
        self._write_deployment({"api_key": "must-not-be-here"})
        with self.assertRaisesRegex(ValueError, "inline secret field"):
            load_deployment(self.deployment_path, load_project(self.project_path))

        self._write_deployment(
            {"base_url": "https://api.example.test/v1?access_token=secret"}
        )
        with self.assertRaisesRegex(ValueError, "query must not contain credentials"):
            load_deployment(self.deployment_path, load_project(self.project_path))

        self._write_deployment({"deployment_id": "another-environment"})
        with self.assertRaisesRegex(ValueError, "does not match"):
            load_deployment(self.deployment_path, load_project(self.project_path))

    def test_deployment_dry_run_does_not_sign_or_write_output(self) -> None:
        report = create_project_for_deployment(
            self.project_path,
            self.deployment_path,
            dry_run=True,
        )
        self.assertEqual(report["result"], "PASS")
        self.assertFalse(report["package_signed"])
        self.assertTrue(report["deployment_signing_planned"])
        self.assertFalse(self.output.exists())

    def test_signed_create_preflight_and_contract_run(self) -> None:
        created = create_project_for_deployment(
            self.project_path,
            self.deployment_path,
        )
        self.assertTrue(created["package_signed"])
        self.assertTrue(
            (self.output / "package/registry.lock.sig.json").is_file()
        )
        self.assertTrue(
            (self.output / "package/package.manifest.sig.json").is_file()
        )

        preflight = check_deployment(self.project_path, self.deployment_path)
        self.assertEqual(preflight["result"], "PASS", preflight["errors"])
        self.assertTrue(preflight["package_signed"])

        cli = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/workflowctl.py"),
                "deploy-check",
                str(self.project_path),
                "--deployment-file",
                str(self.deployment_path),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)["result"], "PASS")

        adapter = DeepSeekReadonlyAdapter(client=ContractHarnessClient())
        report = run_project(
            self.project_path,
            self.deployment_path,
            run_id="run-project-contract",
            adapter=adapter,
        )
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["deployment_id"], "local-deepseek-dev")
        self.assertEqual(report["status"], "completed")

    def test_live_preflight_reports_missing_credential_without_leaking_value(self) -> None:
        create_project_for_deployment(self.project_path, self.deployment_path)
        previous = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            report = check_deployment(
                self.project_path,
                self.deployment_path,
                require_live_environment=True,
            )
        finally:
            if previous is not None:
                os.environ["DEEPSEEK_API_KEY"] = previous
        self.assertEqual(report["result"], "FAIL")
        self.assertFalse(report["credential"]["present"])
        self.assertTrue(
            any("DEEPSEEK_API_KEY" in error for error in report["errors"])
        )


if __name__ == "__main__":
    unittest.main()
