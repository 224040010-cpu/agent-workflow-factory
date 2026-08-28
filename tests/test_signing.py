from __future__ import annotations

import base64
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.signing import (  # noqa: E402
    generate_root_key,
    generate_signing_key,
    sign_artifact,
    verify_artifact,
    verify_trust_store,
)
from workflow_factory.util import read_json, write_json  # noqa: E402


class ArtifactSigningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.artifact = self.root / "registry.lock.json"
        self.private_key = self.root / "publisher.pem"
        self.trust_store = self.root / "trust.json"
        self.signature = self.root / "registry.lock.sig.json"
        write_json(self.artifact, {"schema_version": "1.0.0", "assets": []})
        self.record = generate_signing_key(
            self.private_key,
            self.trust_store,
            "test-build-publisher",
        )
        sign_artifact(
            self.artifact,
            self.private_key,
            self.signature,
            "test-build-publisher",
            issued_at="2026-08-28T00:00:00+00:00",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_ed25519_signature_verifies(self) -> None:
        report = verify_artifact(
            self.artifact,
            self.signature,
            self.trust_store,
            "test-build-publisher",
        )
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["key_id"], self.record["key_id"])

    def test_artifact_tampering_is_rejected(self) -> None:
        write_json(self.artifact, {"schema_version": "1.0.0", "assets": ["tampered"]})
        with self.assertRaisesRegex(ValueError, "digest does not match"):
            verify_artifact(
                self.artifact,
                self.signature,
                self.trust_store,
                "test-build-publisher",
            )

    def test_signature_tampering_is_rejected(self) -> None:
        envelope = read_json(self.signature)
        raw = bytearray(base64.b64decode(envelope["signature"]))
        raw[0] ^= 1
        envelope["signature"] = base64.b64encode(raw).decode("ascii")
        write_json(self.signature, envelope)
        with self.assertRaisesRegex(ValueError, "signature verification failed"):
            verify_artifact(
                self.artifact,
                self.signature,
                self.trust_store,
                "test-build-publisher",
            )

    def test_revoked_key_is_rejected_but_retired_key_verifies(self) -> None:
        trust = read_json(self.trust_store)
        trust["keys"][0]["status"] = "retired"
        write_json(self.trust_store, trust)
        self.assertEqual(
            verify_artifact(
                self.artifact,
                self.signature,
                self.trust_store,
                "test-build-publisher",
            )["key_status"],
            "retired",
        )
        trust["keys"][0]["status"] = "revoked"
        write_json(self.trust_store, trust)
        with self.assertRaisesRegex(ValueError, "not usable: revoked"):
            verify_artifact(
                self.artifact,
                self.signature,
                self.trust_store,
                "test-build-publisher",
            )

    def test_unknown_key_and_wrong_publisher_are_rejected(self) -> None:
        write_json(self.trust_store, {"schema_version": "1.0.0", "keys": []})
        with self.assertRaisesRegex(ValueError, "not uniquely trusted"):
            verify_artifact(
                self.artifact,
                self.signature,
                self.trust_store,
                "test-build-publisher",
            )
        with self.assertRaisesRegex(ValueError, "required publisher"):
            verify_artifact(
                self.artifact,
                self.signature,
                ROOT / "trust/trusted-publishers.json",
                "different-publisher",
            )

    def test_key_generation_refuses_private_key_overwrite(self) -> None:
        with self.assertRaisesRegex(ValueError, "Refusing to overwrite"):
            generate_signing_key(
                self.private_key,
                self.trust_store,
                "test-build-publisher",
            )

    def test_signing_key_rotation_accepts_new_and_retired_keys(self) -> None:
        rotated_private_key = self.root / "publisher-rotated.pem"
        rotated_signature = self.root / "registry.lock.rotated.sig.json"
        rotated_record = generate_signing_key(
            rotated_private_key,
            self.trust_store,
            "test-build-publisher",
        )
        trust = read_json(self.trust_store)
        for record in trust["keys"]:
            if record["key_id"] == self.record["key_id"]:
                record["status"] = "retired"
        write_json(self.trust_store, trust)
        sign_artifact(
            self.artifact,
            rotated_private_key,
            rotated_signature,
            "test-build-publisher",
        )

        old_report = verify_artifact(
            self.artifact,
            self.signature,
            self.trust_store,
            "test-build-publisher",
        )
        new_report = verify_artifact(
            self.artifact,
            rotated_signature,
            self.trust_store,
            "test-build-publisher",
        )
        self.assertEqual(old_report["key_status"], "retired")
        self.assertEqual(new_report["key_status"], "active")
        self.assertEqual(new_report["key_id"], rotated_record["key_id"])

    def test_root_signature_protects_trust_store(self) -> None:
        root_private = self.root / "root.pem"
        root_public = self.root / "root-public.json"
        trust_signature = self.root / "trust.sig.json"
        generate_root_key(root_private, root_public)
        sign_artifact(
            self.trust_store,
            root_private,
            trust_signature,
            "agent-workflow-factory-trust-root",
        )
        self.assertEqual(
            verify_trust_store(
                self.trust_store, trust_signature, root_public
            )["key_status"],
            "root",
        )
        trust = read_json(self.trust_store)
        trust["keys"][0]["status"] = "revoked"
        write_json(self.trust_store, trust)
        with self.assertRaisesRegex(ValueError, "digest does not match"):
            verify_trust_store(self.trust_store, trust_signature, root_public)


if __name__ == "__main__":
    unittest.main()
