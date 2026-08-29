from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.business import generate_bpmn  # noqa: E402
from workflow_factory.compiler import compile_package  # noqa: E402
from workflow_factory.reference_runtime import (  # noqa: E402
    ReferenceRuntime,
    RuntimeIntegrityPolicy,
    SqliteEventStore,
)
from workflow_factory.signing import (  # noqa: E402
    FileEd25519SigningProvider,
    generate_root_key,
    generate_signing_key,
    sign_artifact,
)
from workflow_factory.util import read_json, write_json  # noqa: E402


class ReferenceRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package = self.root / "package"
        self.runtime_dir = self.root / "runtime"
        business = ROOT / "examples/governed-workflow-build/business-requirement.json"
        bpmn = self.root / "process.bpmn"
        generate_bpmn(business, bpmn)
        compile_package(
            bpmn,
            business,
            ROOT / "fixtures/catalog.snapshot.json",
            ROOT / "contracts/system-definition.json",
            self.package,
        )
        self.runtime = ReferenceRuntime(self.package, self.runtime_dir)
        self.runtime_private_key = self.root / "runtime-key.pem"
        self.runtime_trust = self.root / "runtime-trust.json"
        generate_signing_key(
            self.runtime_private_key,
            self.runtime_trust,
            "agent-workflow-factory-runtime",
        )

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

    def signed_runtime(self) -> ReferenceRuntime:
        return ReferenceRuntime(
            self.package,
            self.root / "signed-runtime",
            RuntimeIntegrityPolicy(
                signing_provider=FileEd25519SigningProvider(self.runtime_private_key),
                trust_store=self.runtime_trust,
                require_signatures=True,
            ),
        )

    def test_signed_events_and_checkpoint_replay(self) -> None:
        runtime = self.signed_runtime()
        run_id = "run-signed"
        runtime.start(run_id=run_id)
        runtime.route(run_id)
        events = runtime.events.read(run_id)
        self.assertTrue(events)
        self.assertTrue(all("signature" in event for event in events))
        self.assertTrue(runtime.checkpoint_signature_path(run_id).is_file())
        self.assertEqual(runtime.replay(run_id)["result"], "PASS")

    def test_recomputed_hash_chain_cannot_forge_signed_events(self) -> None:
        runtime = self.signed_runtime()
        run_id = "run-forged-chain"
        runtime.start(run_id=run_id)
        runtime.route(run_id)
        events = runtime.events.read(run_id)
        events[0]["payload"]["workflow_id"] = "forged"
        previous_hash = None
        for event in events:
            event["prev_hash"] = previous_hash
            material = {
                key: value
                for key, value in event.items()
                if key not in {"event_hash", "signature"}
            }
            canonical = json.dumps(
                material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            event["event_hash"] = (
                "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            )
            previous_hash = event["event_hash"]
        runtime.events.path(run_id).write_text(
            "\n".join(json.dumps(item, sort_keys=True) for item in events) + "\n",
            encoding="utf-8",
        )
        report = runtime.replay(run_id)
        self.assertEqual(report["result"], "FAIL")
        self.assertTrue(any("signature invalid" in item for item in report["errors"]))

    def test_tampered_signed_checkpoint_is_rejected(self) -> None:
        runtime = self.signed_runtime()
        run_id = "run-checkpoint-tampered"
        runtime.start(run_id=run_id)
        state = read_json(runtime.checkpoint_path(run_id))
        state["facts"]["forged"] = True
        write_json(runtime.checkpoint_path(run_id), state)
        with self.assertRaisesRegex(ValueError, "digest does not match"):
            runtime.load_state(run_id)

    def test_signed_checkpoint_rollback_is_rejected(self) -> None:
        runtime = self.signed_runtime()
        run_id = "run-checkpoint-rollback"
        runtime.start(run_id=run_id)
        old_checkpoint = self.root / "old-checkpoint.json"
        old_signature = self.root / "old-checkpoint.sig.json"
        shutil.copy2(runtime.checkpoint_path(run_id), old_checkpoint)
        shutil.copy2(runtime.checkpoint_signature_path(run_id), old_signature)

        runtime.route(run_id)
        shutil.copy2(old_checkpoint, runtime.checkpoint_path(run_id))
        shutil.copy2(old_signature, runtime.checkpoint_signature_path(run_id))

        with self.assertRaisesRegex(ValueError, "latest state checkpoint"):
            runtime.load_state(run_id)

    def test_signed_uncheckpointed_state_event_is_rejected(self) -> None:
        runtime = self.signed_runtime()
        run_id = "run-uncheckpointed-event"
        runtime.start(run_id=run_id)
        runtime.events.append(
            run_id,
            "node.completed",
            {"node_id": "parse-requirement", "facts": {"forged": True}},
        )

        with self.assertRaisesRegex(ValueError, "uncheckpointed state events"):
            runtime.load_state(run_id)

    def test_required_runtime_signatures_need_a_signer(self) -> None:
        runtime = ReferenceRuntime(
            self.package,
            self.root / "missing-signer-runtime",
            RuntimeIntegrityPolicy(
                trust_store=self.runtime_trust,
                require_signatures=True,
            ),
        )
        with self.assertRaisesRegex(ValueError, "requires a runtime signing provider"):
            runtime.start(run_id="run-missing-signer")
        self.assertFalse(runtime.events.path("run-missing-signer").exists())

    def test_run_id_path_traversal_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "run_id must contain"):
            self.runtime.start(run_id="../outside")

    def test_sqlite_store_enforces_lease_and_retention(self) -> None:
        root = self.root / "sqlite-store"
        checkpoint_root = root / "checkpoints"
        checkpoint_root.mkdir(parents=True)
        first = SqliteEventStore(
            root,
            lease_owner="worker-a",
            lease_ttl_seconds=30,
            retention_days=1,
            checkpoint_root=checkpoint_root,
        )
        second = SqliteEventStore(
            root,
            lease_owner="worker-b",
            lease_ttl_seconds=30,
            retention_days=1,
        )
        first.acquire_lease("run-leased")
        with self.assertRaisesRegex(ValueError, "held by another owner"):
            second.acquire_lease("run-leased")
        first.append("run-leased", "run.started", {"workflow_id": "test"})
        self.assertEqual(first.verify("run-leased"), [])
        first.mark_terminal("run-leased")
        write_json(checkpoint_root / "run-leased.json", {"state": "terminal"})
        write_json(checkpoint_root / "run-leased.sig.json", {"signature": "test"})
        first.release_lease("run-leased")
        self.assertEqual(first.purge_expired(now=time.time() + 2 * 86400), 1)
        self.assertEqual(first.read("run-leased"), [])
        self.assertFalse((checkpoint_root / "run-leased.json").exists())
        self.assertFalse((checkpoint_root / "run-leased.sig.json").exists())

    def test_reference_runtime_runs_on_signed_sqlite_store(self) -> None:
        runtime = ReferenceRuntime(
            self.package,
            self.root / "sqlite-runtime",
            RuntimeIntegrityPolicy(
                signing_provider=FileEd25519SigningProvider(self.runtime_private_key),
                trust_store=self.runtime_trust,
                require_signatures=True,
            ),
            event_store_backend="sqlite",
            lease_owner="runtime-worker",
        )
        run_id = "run-signed-sqlite"
        runtime.start(run_id=run_id)
        runtime.route(run_id)
        self.assertEqual(runtime.replay(run_id)["result"], "PASS")
        self.assertTrue(all("signature" in event for event in runtime.events.read(run_id)))

    def test_runtime_rejects_trust_store_without_valid_root_signature(self) -> None:
        root_private = self.root / "root-key.pem"
        root_public = self.root / "root-public.json"
        trust_signature = self.root / "runtime-trust.sig.json"
        generate_root_key(root_private, root_public)
        sign_artifact(
            self.runtime_trust,
            root_private,
            trust_signature,
            "agent-workflow-factory-trust-root",
        )
        trust = read_json(self.runtime_trust)
        trust["keys"][0]["status"] = "revoked"
        write_json(self.runtime_trust, trust)
        with self.assertRaisesRegex(ValueError, "digest does not match"):
            ReferenceRuntime(
                self.package,
                self.root / "rooted-runtime",
                RuntimeIntegrityPolicy(
                    trust_store=self.runtime_trust,
                    trust_store_signature=trust_signature,
                    trust_root_public_key=root_public,
                    require_signatures=True,
                    require_rooted_trust=True,
                ),
            )


if __name__ == "__main__":
    unittest.main()
