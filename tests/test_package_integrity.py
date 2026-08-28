from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.business import generate_bpmn  # noqa: E402
from workflow_factory.compiler import compile_package  # noqa: E402
from workflow_factory.package_integrity import verify_package_manifest  # noqa: E402
from workflow_factory.signing import (  # noqa: E402
    FileEd25519SigningProvider,
    generate_signing_key,
    sign_artifact,
)
from workflow_factory.util import read_json, write_json  # noqa: E402


class PackageIntegrityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package = self.root / "package"
        self.private_key = self.root / "build.pem"
        self.trust_store = self.root / "trust.json"
        generate_signing_key(
            self.private_key,
            self.trust_store,
            "agent-workflow-factory-build",
        )
        self.business = ROOT / "examples/deepseek-readonly/business-requirement.json"
        self.bpmn = self.root / "process.bpmn"
        generate_bpmn(self.business, self.bpmn)
        compile_package(
            self.bpmn,
            self.business,
            ROOT / "fixtures/catalog.snapshot.json",
            ROOT / "contracts/system-definition.json",
            self.package,
            signing_key_path=self.private_key,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def resign_manifest(self) -> None:
        sign_artifact(
            self.package / "package.manifest.json",
            self.private_key,
            self.package / "package.manifest.sig.json",
            "agent-workflow-factory-build",
        )

    def test_signed_manifest_covers_every_package_file(self) -> None:
        report = verify_package_manifest(self.package, self.trust_store)
        self.assertEqual(report["result"], "PASS")
        self.assertGreaterEqual(report["files"], 8)
        self.assertEqual(
            (self.package / "process.bpmn").read_bytes(), self.bpmn.read_bytes()
        )

    def test_compiler_accepts_pluggable_signing_provider(self) -> None:
        delegate = FileEd25519SigningProvider(self.private_key)

        class CountingProvider:
            def __init__(self) -> None:
                self.calls = 0

            @property
            def key_id(self) -> str:
                return delegate.key_id

            def sign(self, payload: bytes) -> bytes:
                self.calls += 1
                return delegate.sign(payload)

        provider = CountingProvider()
        package = self.root / "provider-package"
        compile_package(
            self.bpmn,
            self.business,
            ROOT / "fixtures/catalog.snapshot.json",
            ROOT / "contracts/system-definition.json",
            package,
            signing_provider=provider,
        )
        self.assertEqual(provider.calls, 2)
        self.assertEqual(verify_package_manifest(package, self.trust_store)["result"], "PASS")

    def test_modified_file_is_rejected(self) -> None:
        graph = read_json(self.package / "graph.json")
        graph["metadata"]["version"] = "tampered"
        write_json(self.package / "graph.json", graph)
        with self.assertRaisesRegex(ValueError, "artifact digest mismatch: graph.json"):
            verify_package_manifest(self.package, self.trust_store)

    def test_deleted_and_injected_files_are_rejected(self) -> None:
        (self.package / "runtime.policy.json").unlink()
        with self.assertRaisesRegex(ValueError, "files are missing"):
            verify_package_manifest(self.package, self.trust_store)

        write_json(self.package / "runtime.policy.json", {"restored": False})
        write_json(self.package / "injected.json", {"unexpected": True})
        with self.assertRaisesRegex(ValueError, "unsigned files: injected.json"):
            verify_package_manifest(self.package, self.trust_store)

    def test_signed_version_downgrade_is_rejected(self) -> None:
        manifest = read_json(self.package / "package.manifest.json")
        manifest["schema_version"] = "0.9.0"
        write_json(self.package / "package.manifest.json", manifest)
        self.resign_manifest()
        with self.assertRaisesRegex(ValueError, "Unsupported package manifest schema_version"):
            verify_package_manifest(self.package, self.trust_store)

    def test_signed_path_traversal_is_rejected(self) -> None:
        manifest = read_json(self.package / "package.manifest.json")
        manifest["artifacts"][0]["path"] = "../outside.json"
        write_json(self.package / "package.manifest.json", manifest)
        self.resign_manifest()
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            verify_package_manifest(self.package, self.trust_store)


if __name__ == "__main__":
    unittest.main()
