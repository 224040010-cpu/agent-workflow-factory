"""Pinned SDK migration tests; optional runtime tests use localhost, never a paid model."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from sdk_test_support import local_model_endpoint

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.deepseek_harness import DeepSeekHarnessSettings, OfficialDeepSeekHarnessClient, harness_usage
from workflow_factory.harness_config import (
    PINNED_SDK_VERSION, READONLY_PATCH_DIGEST, sdk_environment, supported_platform, validate_composition,
)

PATCH = ROOT / "adapters/deepseek-harness/readonly.patch.yml"


class HarnessCompositionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"

    def validate(self):
        return validate_composition(self.home, "sdk-minimal", (PATCH,))

    def test_reviewed_patch_and_provenance(self):
        self.assertEqual(hashlib.sha256(PATCH.read_bytes()).hexdigest(), READONLY_PATCH_DIGEST)
        self.assertEqual(self.validate()["sdk_version"], PINNED_SDK_VERSION)
        self.assertFalse(self.home.exists())  # preflight is read-only

    def test_rejects_personal_home_wrong_profile_and_missing_patch(self):
        for home, profile, patches in (
            (Path.home() / ".dsh", "sdk-minimal", (PATCH,)),
            (self.home, "desktop", (PATCH,)),
            (self.home, "sdk", (PATCH,)),
            (Path("relative"), "sdk-minimal", (PATCH,)),
            (self.home, "sdk-minimal", ()),
            (self.home, "sdk-minimal", (PATCH, PATCH)),
        ):
            with self.subTest(profile=profile, home=home, count=len(patches)):
                with self.assertRaises(ValueError):
                    validate_composition(home, profile, patches)

    def test_rejects_changed_patch(self):
        changed = Path(self.temp.name) / "changed.yml"
        changed.write_bytes(PATCH.read_bytes() + b"\n# modified\n")
        with self.assertRaisesRegex(ValueError, "differs"):
            validate_composition(self.home, "sdk-minimal", (changed,))

    def test_rejects_ambient_config_without_evaluating_js(self):
        for relative, content in (
            (".env", "SECRET=not-loaded"),
            ("profiles/sdk-minimal/.env.local", "SECRET=not-loaded"),
            ("cordis.patch.yml", "- id: persistent-bash\n  disabled: false\n"),
            ("profiles/sdk-minimal/cordis.yml", "- name: external-plugin"),
            ("profiles/sdk-minimal/cordis.patch.yml", "!!js throw new Error('not executed')"),
        ):
            with self.subTest(relative=relative):
                path = self.home / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.validate()
                path.unlink()

    def test_accepts_generated_manifest_but_rejects_bundle_or_dependency_changes(self):
        manifest = self.home / "profiles/sdk-minimal/package.json"
        manifest.parent.mkdir(parents=True)
        data = {"name": "dsh-profile-sdk-minimal", "private": True, "dependencies": {},
                "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-sdk-minimal"], "patchReload": "startup"}}}
        manifest.write_text(json.dumps(data), encoding="utf-8")
        self.validate()
        data["dependencies"] = {"external-plugin": "*"}
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "dependencies"):
            self.validate()
        data["dependencies"] = {}
        data["dsh"]["profile"]["bundles"].append("external-bundle")
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "bundle"):
            self.validate()

    def test_rejects_desktop_profile_in_home(self):
        (self.home / "profiles/desktop").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "another profile"):
            self.validate()

    def test_detects_runtime_version_mismatch_and_node_injection(self):
        with patch("workflow_factory.harness_config.version", side_effect=[PINNED_SDK_VERSION, "0.0.0"]), \
                patch.dict(os.environ, {"NODE_OPTIONS": "--inspect"}):
            errors = sdk_environment()["errors"]
        self.assertTrue(any("runtime-bin" in error for error in errors))
        self.assertTrue(any("NODE_OPTIONS" in error for error in errors))

    def test_windows_x64_supported_but_windows_arm64_rejected(self):
        with patch("workflow_factory.harness_config.platform.system", return_value="Windows"):
            with patch("workflow_factory.harness_config.platform.machine", return_value="AMD64"):
                self.assertTrue(supported_platform())
            with patch("workflow_factory.harness_config.platform.machine", return_value="ARM64"):
                self.assertFalse(supported_platform())

    def test_nested_message_usage_and_old_shape(self):
        for data in ({"message": {"usage": {"inputTokens": 5, "outputTokens": 3}}},
                     {"message": {}, "usage": {"inputTokens": 5, "outputTokens": 3}}):
            self.assertEqual(harness_usage([{"type": "assistant/message", "data": data}]).total_tokens, 8)

    def test_wrapper_uses_new_contract_and_rechecks_composition_before_call(self):
        calls = []
        options = {}

        class FakeSdk:
            def __init__(self, **kwargs):
                options.update(kwargs)

            def run(self, input, *, session_id=None):
                calls.append(session_id)
                return SimpleNamespace(session_id=session_id)

            def close(self):
                pass

        with patch.dict(sys.modules, {"deepseek_harness": SimpleNamespace(DeepSeekHarness=FakeSdk)}), \
                patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-memory-only-test",
                                        "DEEPSEEK_BASE_URL": "https://example.invalid/v1"}), \
                patch("workflow_factory.deepseek_harness.sdk_environment", return_value={"errors": []}):
            client = OfficialDeepSeekHarnessClient(DeepSeekHarnessSettings(dsh_home=self.home, patches=(PATCH,)))
            self.assertEqual(options["profile"], "sdk-minimal")
            self.assertEqual(options["patches"], (str(PATCH),))
            self.assertNotIn("cordis", options)
            self.assertNotIn("session_root", options)
            self.assertEqual(options["env"]["DSH_TELEMETRY_MODE"], "DISABLED")
            self.assertEqual(options["api_key"], "sk-memory-only-test")
            self.assertEqual(options["base_url"], "https://example.invalid/v1")
            client.run("complete facts", session_id="logical")
            client.run("complete facts", session_id="logical")
            self.assertEqual(calls[0], calls[1])
            client.close()
            client.run("complete facts", session_id="logical")
            self.assertNotEqual(calls[0], calls[2])
            (self.home / "cordis.patch.yml").write_text("- id: tools\n  disabled: true", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unreviewed"):
                client.run("must not be sent", session_id="logical")
            self.assertEqual(len(calls), 3)
            client.close()


@unittest.skipUnless(os.environ.get("AWF_SDK_RUNTIME_TEST") == "1", "requires AWF_SDK_RUNTIME_TEST=1 and pinned SDK/runtime")
class OfficialSdkRuntimeTest(unittest.TestCase):
    def test_resolved_profile_disables_shells_and_uploads(self):
        from deepseek_harness_runtime import resolve_bundled_launch_args
        with tempfile.TemporaryDirectory() as temporary:
            env = os.environ.copy()
            env.update(DSH_HOME=temporary, DSH_TELEMETRY_MODE="DISABLED")
            result = subprocess.run([*resolve_bundled_launch_args(), "--profile", "sdk-minimal",
                                     "--patch", str(PATCH), "--dump-config"], env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr)
            entries = {match.group(1): match.group(2) for match in re.finditer(
                r"(?ms)^- id: ([^\r\n]+)\r?\n(.*?)(?=^- id: |\Z)", result.stdout)}
            for name in ("persistent-bash", "persistent-pwsh", "terminal-bash", "terminal-pwsh"):
                self.assertIn("disabled: true", entries[name])
            for name in ("session-log-deepseek", "plugin-package-inventory-deepseek"):
                self.assertIn("enabled: false", entries[name])
            self.assertIn("maxTokensAsSuccess: false", entries["sdk-jsonrpc-server"])
            self.assertNotIn("mcp", entries)
            validate_composition(Path(temporary), "sdk-minimal", (PATCH,))

    def test_official_sdk_no_tools_in_process_followup_and_new_process_session(self):
        # Runs the real SDK + binary; only the model endpoint is a local deterministic stub.
        answer = '{"status":"completed","facts":{},"evidence":["local-test"]}'
        with local_model_endpoint() as (base_url, requests):
            with tempfile.TemporaryDirectory() as temporary:
                settings = DeepSeekHarnessSettings(
                    cwd=Path(temporary) / "workspace", dsh_home=Path(temporary) / "home", patches=(PATCH,),
                    api_key="sk-local-test-no-real-secret", base_url=base_url,
                )
                session_ids = []
                for prompt in ("Return the requested JSON.", "Return complete state in a new process."):
                    client = OfficialDeepSeekHarnessClient(settings)
                    try:
                        result = client.run(prompt, session_id="awf-sdk-contract")
                        self.assertEqual(result.finish_reason, "completed", result.events)
                        self.assertEqual(json.loads(result.final_response), json.loads(answer))
                        self.assertGreater(harness_usage(result.events).total_tokens, 0)
                        session_ids.append(result.session_id)
                        followup = client.run("Repeat JSON.", session_id="awf-sdk-contract")
                        self.assertEqual(followup.session_id, result.session_id)
                        self.assertEqual(followup.finish_reason, "completed")
                    finally:
                        client.close()
                self.assertNotEqual(session_ids[0], session_ids[1])
                self.assertGreaterEqual(len(requests), 4)
                self.assertTrue(all(not request.get("tools") for request in requests))
                self.assertTrue(any(len(request.get("messages", [])) >= 4 for request in requests))


if __name__ == "__main__":
    unittest.main()
