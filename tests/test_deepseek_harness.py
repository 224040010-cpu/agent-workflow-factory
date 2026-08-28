from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import unittest
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.business import generate_bpmn  # noqa: E402
from workflow_factory.compiler import compile_package  # noqa: E402
from workflow_factory.deepseek_harness import (  # noqa: E402
    BudgetExceeded,
    DeepSeekHarnessSettings,
    DeepSeekReadonlyAdapter,
    DeepSeekReadonlyRunner,
    READONLY_CORDIS_DIGEST,
    ReadonlyToolHost,
    binding_digest,
    builtin_readonly_tool_bindings,
    harness_usage,
)
from workflow_factory.reference_runtime import ReferenceRuntime  # noqa: E402
from workflow_factory.util import read_json, write_json  # noqa: E402


class FakeHarnessClient:
    def __init__(
        self,
        fail_once: bool = False,
        wrong_facts: bool = False,
        wrong_evidence: bool = False,
        fail_on_call: int | None = None,
        usage: dict | None = None,
    ):
        self.fail_on_call = 1 if fail_once else fail_on_call
        self.failed = False
        self.wrong_facts = wrong_facts
        self.wrong_evidence = wrong_evidence
        self.usage = usage
        self.calls: list[dict] = []
        self.closed = False

    def run(self, input: str, *, session_id: str | None = None):
        payload = json.loads(input)
        self.calls.append({"payload": payload, "session_id": session_id})
        if self.fail_on_call == len(self.calls) and not self.failed:
            self.failed = True
            raise RuntimeError("injected Harness interruption")
        facts = payload["trusted_tool_observation"]["facts"]
        if self.wrong_facts:
            facts = {"intent": {"parsed": False}}
        evidence = [item["kind"] for item in payload["trusted_tool_observation"]["evidence"]]
        if self.wrong_evidence:
            evidence = ["untrusted-evidence"]
        return SimpleNamespace(
            session_id=session_id or "fake-session",
            final_response=json.dumps(
                {"status": "completed", "facts": facts, "evidence": evidence},
                ensure_ascii=False,
            ),
            finish_reason="completed",
            events=[
                {
                    "type": "assistant/message",
                    "data": {"turn": 0, "step": 0, "usage": self.usage},
                },
                {"type": "turn/end"},
            ],
        )

    def close(self) -> None:
        self.closed = True


class ErrorHarnessClient:
    def run(self, input: str, *, session_id: str | None = None):
        del input
        return SimpleNamespace(
            session_id=session_id,
            final_response="",
            finish_reason="error",
            events=[
                {
                    "type": "turn/end",
                    "data": {
                        "reason": {
                            "kind": "error",
                            "error": {
                                "code": "AUTHENTICATION_FAILED",
                                "message": "invalid API key sk-super-secret-value",
                            },
                        }
                    },
                }
            ],
        )

    def close(self) -> None:
        return None


def malformed_parse_output(descriptor: dict, request: dict, idempotency_key: str) -> dict:
    del descriptor, request, idempotency_key
    return {"facts": {"intent": {"parsed": "yes"}}, "evidence": []}


class DeepSeekReadonlyHarnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package = self.root / "package"
        self.runtime_dir = self.root / "runtime"
        self.business = ROOT / "examples/deepseek-readonly/business-requirement.json"
        bpmn = self.root / "process.bpmn"
        generate_bpmn(self.business, bpmn)
        compile_package(
            bpmn,
            self.business,
            ROOT / "fixtures/catalog.snapshot.json",
            ROOT / "contracts/system-definition.json",
            self.package,
        )
        self.facts = read_json(ROOT / "examples/deepseek-readonly/initial-facts.json")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_readonly_end_to_end_reaches_terminal_and_replays(self) -> None:
        client = FakeHarnessClient()
        adapter = DeepSeekReadonlyAdapter(client=client)
        report = DeepSeekReadonlyRunner(self.package, self.runtime_dir, adapter).run(
            self.facts,
            run_id="run-e2e",
        )

        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["replay"], "PASS")
        self.assertEqual(len(client.calls), 1)
        runtime = ReferenceRuntime(self.package, self.runtime_dir)
        self.assertTrue(runtime.load_state("run-e2e")["facts"]["intent"]["parsed"])
        event_types = [item["type"] for item in runtime.events.read("run-e2e")]
        self.assertIn("tool.observation.accepted", event_types)
        self.assertIn("agent.turn.completed", event_types)
        self.assertIn("facts.verified", event_types)
        self.assertEqual(state := runtime.load_state("run-e2e"), runtime.replay("run-e2e")["state"])
        self.assertEqual(state["budget_usage"]["total"]["model_turns"], 1)
        self.assertEqual(state["budget_usage"]["total"]["tool_calls"], 1)

    def test_reviewed_cordis_digest_is_pinned(self) -> None:
        cordis = ROOT / "adapters/deepseek-harness/readonly.cordis.yml"
        self.assertEqual(hashlib.sha256(cordis.read_bytes()).hexdigest(), READONLY_CORDIS_DIGEST)

    def test_reviewed_binding_manifest_matches_runtime_bindings(self) -> None:
        manifest = read_json(
            ROOT / "adapters/deepseek-harness/readonly-tool-bindings.json"
        )
        runtime = builtin_readonly_tool_bindings()
        self.assertEqual(
            {item["endpoint"]: item["reviewed_digest"] for item in manifest["bindings"]},
            {endpoint: binding.reviewed_digest for endpoint, binding in runtime.items()},
        )
        for binding in runtime.values():
            self.assertEqual(
                binding.reviewed_digest,
                binding_digest(
                    binding.endpoint,
                    binding.implementation_id,
                    binding.input_schema,
                    binding.output_schema,
                    binding.handler,
                ),
            )

    def test_interrupted_turn_can_resume_with_same_session(self) -> None:
        client = FakeHarnessClient(fail_once=True)
        adapter = DeepSeekReadonlyAdapter(client=client)
        runner = DeepSeekReadonlyRunner(self.package, self.runtime_dir, adapter)
        with self.assertRaisesRegex(RuntimeError, "injected Harness interruption"):
            runner.run(self.facts, run_id="run-resume")
        self.assertEqual(runner.runtime.load_state("run-resume")["status"], "paused")

        report = runner.run(self.facts, run_id="run-resume")
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(client.calls[0]["session_id"], client.calls[1]["session_id"])
        self.assertEqual(runner.runtime.replay("run-resume")["result"], "PASS")

    def test_rejects_non_readonly_lockfile_tool_before_model(self) -> None:
        lock = read_json(self.package / "registry.lock.json")
        tool = next(item for item in lock["resolved_assets"] if item["type"] == "tool")
        tool["side_effects"] = "write"
        write_json(self.package / "registry.lock.json", lock)
        client = FakeHarnessClient()
        with self.assertRaisesRegex(ValueError, "Tool is not read-only"):
            DeepSeekReadonlyRunner(
                self.package,
                self.runtime_dir,
                DeepSeekReadonlyAdapter(client=client),
            ).run(self.facts, run_id="run-write-rejected")
        self.assertEqual(client.calls, [])

    def test_rejects_tool_input_schema_before_model(self) -> None:
        client = FakeHarnessClient()
        with self.assertRaisesRegex(ValueError, "schema rejected.*description"):
            DeepSeekReadonlyRunner(
                self.package,
                self.runtime_dir,
                DeepSeekReadonlyAdapter(client=client),
            ).run({"business": {}}, run_id="run-invalid-input")
        self.assertEqual(client.calls, [])

    def test_rejects_changed_binding_implementation_digest_before_model(self) -> None:
        bindings = builtin_readonly_tool_bindings()
        endpoint = "bpmn-tools:parse_business_intent()"
        bindings[endpoint] = replace(bindings[endpoint], reviewed_digest="sha256:" + "0" * 64)
        client = FakeHarnessClient()
        with self.assertRaisesRegex(ValueError, "implementation digest mismatch"):
            DeepSeekReadonlyRunner(
                self.package,
                self.runtime_dir,
                DeepSeekReadonlyAdapter(tool_host=ReadonlyToolHost(bindings), client=client),
            ).run(self.facts, run_id="run-binding-digest")
        self.assertEqual(client.calls, [])

    def test_rejects_tool_output_schema_before_model(self) -> None:
        bindings = builtin_readonly_tool_bindings()
        endpoint = "bpmn-tools:parse_business_intent()"
        original = bindings[endpoint]
        bindings[endpoint] = replace(
            original,
            handler=malformed_parse_output,
            reviewed_digest=binding_digest(
                original.endpoint,
                original.implementation_id,
                original.input_schema,
                original.output_schema,
                malformed_parse_output,
            ),
        )
        client = FakeHarnessClient()
        with self.assertRaisesRegex(ValueError, "schema rejected.*parsed"):
            DeepSeekReadonlyRunner(
                self.package,
                self.runtime_dir,
                DeepSeekReadonlyAdapter(tool_host=ReadonlyToolHost(bindings), client=client),
            ).run(self.facts, run_id="run-invalid-output")
        self.assertEqual(client.calls, [])

    def test_agent_turn_budget_exhausts_before_model_call(self) -> None:
        profile_path = next((self.package / "agents").glob("*.agent.json"))
        profile = read_json(profile_path)
        profile["spec"]["budgets"]["max_turns"] = 0
        write_json(profile_path, profile)
        client = FakeHarnessClient()
        runner = DeepSeekReadonlyRunner(
            self.package, self.runtime_dir, DeepSeekReadonlyAdapter(client=client)
        )
        with self.assertRaisesRegex(BudgetExceeded, "before execution"):
            runner.run(self.facts, run_id="run-turn-budget")
        self.assertEqual(client.calls, [])
        state = runner.runtime.load_state("run-turn-budget")
        self.assertEqual(state["status"], "escalated")
        self.assertEqual(state["budget_usage"]["total"]["model_turns"], 0)

    def test_actual_token_usage_is_recorded_then_escalated(self) -> None:
        profile_path = next((self.package / "agents").glob("*.agent.json"))
        profile = read_json(profile_path)
        profile["spec"]["budgets"]["max_tokens"] = 5
        write_json(profile_path, profile)
        client = FakeHarnessClient(
            usage={
                "inputTokens": 3,
                "outputTokens": 2,
                "cacheReadTokens": 2,
                "reasoningTokens": 1,
            }
        )
        runner = DeepSeekReadonlyRunner(
            self.package, self.runtime_dir, DeepSeekReadonlyAdapter(client=client)
        )
        with self.assertRaisesRegex(BudgetExceeded, "after provider usage"):
            runner.run(self.facts, run_id="run-token-budget")
        state = runner.runtime.load_state("run-token-budget")
        self.assertEqual(state["status"], "escalated")
        self.assertEqual(state["budget_usage"]["total"]["tokens"], 7)
        self.assertNotIn("intent", state["facts"])
        self.assertEqual(runner.runtime.replay("run-token-budget")["result"], "PASS")
        self.assertIn(
            "budget.exhausted",
            [event["type"] for event in runner.runtime.events.read("run-token-budget")],
        )

    def test_usage_chunk_overrides_message_and_reasoning_is_not_double_counted(self) -> None:
        usage = harness_usage(
            [
                {
                    "type": "assistant/message",
                    "data": {
                        "turn": 1,
                        "step": 2,
                        "usage": {"inputTokens": 100, "outputTokens": 100},
                    },
                },
                {
                    "type": "assistant/chunk",
                    "data": {
                        "turn": 1,
                        "step": 2,
                        "chunk": {
                            "type": "usage",
                            "usage": {
                                "inputTokens": 3,
                                "outputTokens": 5,
                                "cacheWriteTokens": 2,
                                "reasoningTokens": 4,
                            },
                        },
                    },
                },
            ]
        )
        self.assertEqual(usage.total_tokens, 10)
        self.assertEqual(usage.reasoning_tokens, 4)

    def test_rejects_model_facts_that_differ_from_tool_evidence(self) -> None:
        client = FakeHarnessClient(wrong_facts=True)
        with self.assertRaisesRegex(ValueError, "Model facts differ"):
            DeepSeekReadonlyRunner(
                self.package,
                self.runtime_dir,
                DeepSeekReadonlyAdapter(client=client),
            ).run(self.facts, run_id="run-untrusted-facts")

    def test_prompt_supplies_exact_required_response(self) -> None:
        client = FakeHarnessClient()
        DeepSeekReadonlyRunner(
            self.package,
            self.runtime_dir,
            DeepSeekReadonlyAdapter(client=client),
        ).run(self.facts, run_id="run-required-response")
        payload = client.calls[0]["payload"]
        self.assertEqual(
            payload["required_response"],
            {
                "status": "completed",
                "facts": {"intent": {"parsed": True}},
                "evidence": ["business-description-digest"],
            },
        )

    def test_rejects_model_evidence_that_differs_from_trusted_tool(self) -> None:
        with self.assertRaisesRegex(ValueError, "Model evidence differs"):
            DeepSeekReadonlyRunner(
                self.package,
                self.runtime_dir,
                DeepSeekReadonlyAdapter(client=FakeHarnessClient(wrong_evidence=True)),
            ).run(self.facts, run_id="run-untrusted-evidence")

    def test_surfaces_structured_harness_error_and_redacts_api_key(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "AUTHENTICATION_FAILED") as caught:
            DeepSeekReadonlyRunner(
                self.package,
                self.runtime_dir,
                DeepSeekReadonlyAdapter(client=ErrorHarnessClient()),
            ).run(self.facts, run_id="run-provider-error")
        self.assertNotIn("sk-super-secret-value", str(caught.exception))
        self.assertIn("[REDACTED_API_KEY]", str(caught.exception))

    def test_capability_negotiation_rejects_human_and_scheduled_loop(self) -> None:
        package = self.root / "governed-package"
        business = ROOT / "examples/governed-workflow-build/business-requirement.json"
        bpmn = self.root / "governed.bpmn"
        generate_bpmn(business, bpmn)
        compile_package(
            bpmn,
            business,
            ROOT / "fixtures/catalog.snapshot.json",
            ROOT / "contracts/system-definition.json",
            package,
        )
        with self.assertRaisesRegex(ValueError, "human_gate, scheduled_loops"):
            DeepSeekReadonlyRunner(
                package,
                self.root / "governed-runtime",
                DeepSeekReadonlyAdapter(client=FakeHarnessClient()),
            ).run(run_id="run-capabilities")


class DeepSeekReadonlyMultinodeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package = self.root / "package"
        self.runtime_dir = self.root / "runtime"
        self.business = ROOT / "examples/deepseek-readonly-multinode/business-requirement.json"
        bpmn = self.root / "process.bpmn"
        generate_bpmn(self.business, bpmn)
        report = compile_package(
            bpmn,
            self.business,
            ROOT / "fixtures/catalog.snapshot.json",
            ROOT / "contracts/system-definition.json",
            self.package,
        )
        self.assertEqual(report["generated_agents"], 2)
        self.assertEqual(report["resolved_tools"], 2)
        self.facts = read_json(
            ROOT / "examples/deepseek-readonly-multinode/initial-facts.json"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_two_agents_and_tools_route_clear_description_to_ready(self) -> None:
        client = FakeHarnessClient()
        report = DeepSeekReadonlyRunner(
            self.package,
            self.runtime_dir,
            DeepSeekReadonlyAdapter(client=client),
        ).run(self.facts, run_id="run-multinode-ready")

        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["completed_actions"], 2)
        self.assertEqual(len(client.calls), 2)
        runtime = ReferenceRuntime(self.package, self.runtime_dir)
        state = runtime.load_state("run-multinode-ready")
        self.assertEqual(state["current_node"], "ready")
        self.assertFalse(state["facts"]["analysis"]["ambiguous"])
        self.assertEqual(state["completed_nodes"], ["parse-intent", "check-ambiguity"])
        event_types = [item["type"] for item in runtime.events.read("run-multinode-ready")]
        self.assertEqual(event_types.count("tool.observation.accepted"), 2)
        self.assertEqual(event_types.count("agent.turn.completed"), 2)

    def test_trusted_ambiguity_fact_routes_to_clarification_terminal(self) -> None:
        facts = read_json(
            ROOT / "examples/deepseek-readonly-multinode/ambiguous-facts.json"
        )
        report = DeepSeekReadonlyRunner(
            self.package,
            self.runtime_dir,
            DeepSeekReadonlyAdapter(client=FakeHarnessClient()),
        ).run(facts, run_id="run-multinode-ambiguous")

        self.assertEqual(report["result"], "PASS")
        state = ReferenceRuntime(self.package, self.runtime_dir).load_state(
            "run-multinode-ambiguous"
        )
        self.assertEqual(state["current_node"], "needs-clarification")
        self.assertTrue(state["facts"]["analysis"]["ambiguous"])
        self.assertEqual(
            state["facts"]["analysis"]["ambiguity_terms"],
            ["尽快", "适当", "相关人员", "视情况"],
        )

    def test_second_agent_interruption_resumes_without_replaying_first_node(self) -> None:
        client = FakeHarnessClient(fail_on_call=2)
        runner = DeepSeekReadonlyRunner(
            self.package,
            self.runtime_dir,
            DeepSeekReadonlyAdapter(client=client),
        )
        with self.assertRaisesRegex(RuntimeError, "injected Harness interruption"):
            runner.run(self.facts, run_id="run-multinode-resume")
        paused = runner.runtime.load_state("run-multinode-resume")
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["completed_nodes"], ["parse-intent"])

        report = runner.run(self.facts, run_id="run-multinode-resume")
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(client.calls[1]["session_id"], client.calls[2]["session_id"])
        self.assertNotEqual(client.calls[0]["session_id"], client.calls[2]["session_id"])
        self.assertEqual(runner.runtime.replay("run-multinode-resume")["result"], "PASS")


@unittest.skipUnless(
    os.environ.get("DSH_LIVE_TEST") == "1" and platform.system() in {"Linux", "Darwin"},
    "requires DSH_LIVE_TEST=1, credentials, SDK and an official supported platform",
)
class DeepSeekReadonlyHarnessLiveTest(unittest.TestCase):
    def test_official_sdk_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "package"
            bpmn = root / "process.bpmn"
            business = ROOT / "examples/deepseek-readonly/business-requirement.json"
            generate_bpmn(business, bpmn)
            compile_package(
                bpmn,
                business,
                ROOT / "fixtures/catalog.snapshot.json",
                ROOT / "contracts/system-definition.json",
                package,
            )
            runtime_dir = root / "runtime"
            adapter = DeepSeekReadonlyAdapter(
                settings=DeepSeekHarnessSettings(
                    cwd=runtime_dir / "workspace",
                    session_root=runtime_dir / "harness-sessions",
                    cordis=ROOT / "adapters/deepseek-harness/readonly.cordis.yml",
                )
            )
            try:
                report = DeepSeekReadonlyRunner(package, runtime_dir, adapter).run(
                    read_json(ROOT / "examples/deepseek-readonly/initial-facts.json"),
                    run_id="run-live-smoke",
                )
            finally:
                adapter.close()
            self.assertEqual(report["result"], "PASS")


@unittest.skipUnless(
    os.environ.get("DSH_MULTINODE_LIVE_TEST") == "1"
    and platform.system() in {"Linux", "Darwin"},
    "requires DSH_MULTINODE_LIVE_TEST=1, credentials, SDK and a supported platform",
)
class DeepSeekReadonlyMultinodeLiveTest(unittest.TestCase):
    def test_official_sdk_runs_two_agent_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "package"
            bpmn = root / "process.bpmn"
            example = ROOT / "examples/deepseek-readonly-multinode"
            business = example / "business-requirement.json"
            generate_bpmn(business, bpmn)
            compile_package(
                bpmn,
                business,
                ROOT / "fixtures/catalog.snapshot.json",
                ROOT / "contracts/system-definition.json",
                package,
            )
            runtime_dir = root / "runtime"
            adapter = DeepSeekReadonlyAdapter(
                settings=DeepSeekHarnessSettings(
                    cwd=runtime_dir / "workspace",
                    session_root=runtime_dir / "harness-sessions",
                    cordis=ROOT / "adapters/deepseek-harness/readonly.cordis.yml",
                )
            )
            try:
                report = DeepSeekReadonlyRunner(package, runtime_dir, adapter).run(
                    read_json(example / "initial-facts.json"),
                    run_id="run-multinode-live",
                )
            finally:
                adapter.close()
            self.assertEqual(report["result"], "PASS")
            self.assertEqual(report["completed_actions"], 2)


if __name__ == "__main__":
    unittest.main()
