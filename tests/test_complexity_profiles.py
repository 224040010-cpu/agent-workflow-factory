from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.complexity_profiles import (  # noqa: E402
    get_profile,
    profiles_report,
    resolve_runtime_options,
    validate_runtime_material,
)
from workflow_factory.signing import (  # noqa: E402
    FileEd25519SigningProvider,
    generate_root_key,
    generate_signing_key,
    sign_artifact,
)
from workflow_factory.util import read_json, write_json  # noqa: E402


class ComplexityProfileTest(unittest.TestCase):
    def test_profiles_expose_three_clear_operating_levels(self) -> None:
        report = profiles_report()
        self.assertEqual(set(report), {"dev", "team", "regulated"})
        self.assertEqual(report["dev"]["event_store"], "jsonl")
        self.assertFalse(report["dev"]["require_runtime_signatures"])
        self.assertTrue(report["team"]["require_runtime_signatures"])
        self.assertTrue(report["regulated"]["require_runtime_trust_root"])

    def test_profile_defaults_can_be_strengthened(self) -> None:
        options = resolve_runtime_options(
            "dev",
            event_store="sqlite",
            require_runtime_signatures=True,
            retention_days=45,
        )
        self.assertEqual(options.event_store, "sqlite")
        self.assertTrue(options.require_runtime_signatures)
        self.assertEqual(options.retention_days, 45)

    def test_team_and_regulated_cannot_downgrade_to_jsonl(self) -> None:
        for name in ("team", "regulated"):
            with self.subTest(profile=name):
                with self.assertRaisesRegex(ValueError, "cannot be downgraded"):
                    resolve_runtime_options(name, event_store="jsonl")

    def test_invalid_profile_and_non_positive_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown complexity profile"):
            get_profile("production")
        with self.assertRaisesRegex(ValueError, "TTL must be positive"):
            resolve_runtime_options("dev", lease_ttl_seconds=0)
        with self.assertRaisesRegex(ValueError, "retention days must be positive"):
            resolve_runtime_options("dev", retention_days=0)

    def test_team_requires_registered_active_signer_and_trust_store(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            private_key = root / "runtime.pem"
            trust_store = root / "trust.json"
            generate_signing_key(
                private_key,
                trust_store,
                "agent-workflow-factory-runtime",
            )
            provider = FileEd25519SigningProvider(private_key)
            options = resolve_runtime_options("team")
            validate_runtime_material(
                options,
                signing_provider=provider,
                trust_store=trust_store,
                trust_store_signature=None,
                trust_root_public_key=None,
                mutating=True,
            )

            trust = read_json(trust_store)
            trust["keys"][0]["status"] = "retired"
            write_json(trust_store, trust)
            with self.assertRaisesRegex(ValueError, "active signing key"):
                validate_runtime_material(
                    options,
                    signing_provider=provider,
                    trust_store=trust_store,
                    trust_store_signature=None,
                    trust_root_public_key=None,
                    mutating=True,
                )

    def test_regulated_requires_complete_rooted_trust_material(self) -> None:
        options = resolve_runtime_options("regulated")
        with self.assertRaisesRegex(ValueError, "requires runtime trust store"):
            validate_runtime_material(
                options,
                signing_provider=None,
                trust_store=None,
                trust_store_signature=None,
                trust_root_public_key=None,
                mutating=False,
            )

    def test_cli_shows_and_checks_dev_profile_without_security_arguments(self) -> None:
        show = subprocess.run(
            [sys.executable, str(ROOT / "scripts/workflowctl.py"), "profile-show", "dev"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertEqual(json.loads(show.stdout)["dev"]["event_store"], "jsonl")

        check = subprocess.run(
            [sys.executable, str(ROOT / "scripts/workflowctl.py"), "profile-check"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual(json.loads(check.stdout)["result"], "PASS")

    def test_cli_team_profile_fails_fast_with_actionable_message(self) -> None:
        check = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/workflowctl.py"),
                "profile-check",
                "--profile",
                "team",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(check.returncode, 1)
        self.assertIn("requires --runtime-trust-store", check.stderr)

    def test_cli_regulated_profile_verifies_complete_rooted_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_key = root / "runtime.pem"
            trust_store = root / "trust.json"
            root_private = root / "root.pem"
            root_public = root / "root-public.json"
            trust_signature = root / "trust.sig.json"
            generate_signing_key(
                runtime_key,
                trust_store,
                "agent-workflow-factory-runtime",
            )
            generate_root_key(root_private, root_public)
            sign_artifact(
                trust_store,
                root_private,
                trust_signature,
                "agent-workflow-factory-trust-root",
            )
            check = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/workflowctl.py"),
                    "profile-check",
                    "--profile",
                    "regulated",
                    "--runtime-signing-key",
                    str(runtime_key),
                    "--runtime-trust-store",
                    str(trust_store),
                    "--runtime-trust-store-signature",
                    str(trust_signature),
                    "--runtime-trust-root-public-key",
                    str(root_public),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(check.returncode, 0, check.stderr)
            report = json.loads(check.stdout)
            self.assertEqual(report["result"], "PASS")
            self.assertEqual(report["runtime_profile"]["profile"], "regulated")
            self.assertEqual(report["runtime_profile"]["retention_days"], 365)


if __name__ == "__main__":
    unittest.main()
