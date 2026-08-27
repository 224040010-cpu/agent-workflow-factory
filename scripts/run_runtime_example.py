#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.reference_runtime import ReferenceRuntime  # noqa: E402


PACKAGE = ROOT / "build/governed-workflow-build/package"
RUNTIME = ROOT / "build/governed-workflow-build/runtime"


def main() -> None:
    if RUNTIME.exists():
        shutil.rmtree(RUNTIME)
    runtime = ReferenceRuntime(PACKAGE, RUNTIME)
    run_id = "run-reference-example"
    runtime.start(run_id=run_id)

    steps = [
        ("parse-requirement", {"intent": {"parsed": True}}),
        ("assemble-model", {"bpmn": {"model_exists": True}}),
        (
            "compile-graph",
            {"package": {"workflow_ir_exists": True, "registry_lock_exists": True}},
        ),
        ("validate-bpmn", {"review": {"completed": True, "passed": True}}),
    ]
    for expected_node, facts in steps:
        route = runtime.route(run_id)
        if route.node_id != expected_node or route.status != "waiting_action":
            raise RuntimeError(f"Unexpected route: {route.as_dict()}")
        runtime.complete(run_id, expected_node, facts)

    terminal = runtime.route(run_id)
    if terminal.status != "completed":
        raise RuntimeError(f"Run did not complete: {terminal.as_dict()}")
    report = runtime.replay(run_id)
    if report["result"] != "PASS":
        raise RuntimeError(f"Replay failed: {report['errors']}")
    print(
        f"Reference runtime completed and replayed {run_id} "
        f"({report['events']} events, hash chain verified)"
    )


if __name__ == "__main__":
    main()
