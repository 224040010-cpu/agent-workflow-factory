#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.business import generate_bpmn  # noqa: E402
from workflow_factory.compiler import compile_package  # noqa: E402
from workflow_factory.reference_runtime import ReferenceRuntime  # noqa: E402
from workflow_factory.util import read_json  # noqa: E402
from workflow_factory.validator import validate_package  # noqa: E402


def verify_definition(definition: Path, checksum: Path) -> None:
    data = read_json(definition)
    expected = checksum.read_text(encoding="ascii").split()[0]
    actual = hashlib.sha256(definition.read_bytes()).hexdigest()
    if expected != actual:
        raise ValueError(f"system-definition checksum mismatch: {expected} != {actual}")
    print(f"Verified system definition {data['definition_version']} ({actual[:12]}...)")


def main() -> int:
    parser = argparse.ArgumentParser(prog="workflowctl")
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser("verify-definition")
    verify.add_argument("--definition", type=Path, default=ROOT / "contracts/system-definition.json")
    verify.add_argument("--checksum", type=Path, default=ROOT / "contracts/system-definition.sha256")

    generate = subparsers.add_parser("generate-bpmn")
    generate.add_argument("business", type=Path)
    generate.add_argument("--output", type=Path, required=True)

    compile_command = subparsers.add_parser("compile")
    compile_command.add_argument("bpmn", type=Path)
    compile_command.add_argument("--business", type=Path, required=True)
    compile_command.add_argument("--catalog", type=Path, required=True)
    compile_command.add_argument("--definition", type=Path, default=ROOT / "contracts/system-definition.json")
    compile_command.add_argument("--output", type=Path, required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("package", type=Path)

    runtime_start = subparsers.add_parser("runtime-start")
    runtime_start.add_argument("package", type=Path)
    runtime_start.add_argument("--runtime-dir", type=Path, required=True)
    runtime_start.add_argument("--run-id")
    runtime_start.add_argument("--facts", type=Path)

    runtime_route = subparsers.add_parser("runtime-route")
    runtime_route.add_argument("package", type=Path)
    runtime_route.add_argument("run_id")
    runtime_route.add_argument("--runtime-dir", type=Path, required=True)

    runtime_complete = subparsers.add_parser("runtime-complete")
    runtime_complete.add_argument("package", type=Path)
    runtime_complete.add_argument("run_id")
    runtime_complete.add_argument("node_id")
    runtime_complete.add_argument("--runtime-dir", type=Path, required=True)
    runtime_complete.add_argument("--facts", type=Path, required=True)

    runtime_pause = subparsers.add_parser("runtime-pause")
    runtime_pause.add_argument("package", type=Path)
    runtime_pause.add_argument("run_id")
    runtime_pause.add_argument("--runtime-dir", type=Path, required=True)
    runtime_pause.add_argument("--reason", required=True)

    runtime_resume = subparsers.add_parser("runtime-resume")
    runtime_resume.add_argument("package", type=Path)
    runtime_resume.add_argument("run_id")
    runtime_resume.add_argument("--runtime-dir", type=Path, required=True)

    runtime_replay = subparsers.add_parser("runtime-replay")
    runtime_replay.add_argument("package", type=Path)
    runtime_replay.add_argument("run_id")
    runtime_replay.add_argument("--runtime-dir", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "verify-definition":
            verify_definition(args.definition, args.checksum)
        elif args.command == "generate-bpmn":
            generate_bpmn(args.business, args.output)
            print(f"BPMN → {args.output}")
        elif args.command == "compile":
            report = compile_package(
                args.bpmn,
                args.business,
                args.catalog,
                args.definition,
                args.output,
            )
            print(
                f"Package → {args.output} "
                f"({report['generated_agents']} agents, {report['resolved_tools']} tools)"
            )
        elif args.command == "validate":
            errors = validate_package(args.package)
            if errors:
                for error in errors:
                    print(f"ERROR: {error}", file=sys.stderr)
                return 1
            print(f"Package verified: {args.package}")
        elif args.command == "runtime-start":
            runtime = ReferenceRuntime(args.package, args.runtime_dir)
            facts = read_json(args.facts) if args.facts else {}
            state = runtime.start(facts, args.run_id)
            print(json.dumps(state, ensure_ascii=False, indent=2))
        elif args.command == "runtime-route":
            runtime = ReferenceRuntime(args.package, args.runtime_dir)
            print(json.dumps(runtime.route(args.run_id).as_dict(), ensure_ascii=False, indent=2))
        elif args.command == "runtime-complete":
            runtime = ReferenceRuntime(args.package, args.runtime_dir)
            state = runtime.complete(args.run_id, args.node_id, read_json(args.facts))
            print(json.dumps(state, ensure_ascii=False, indent=2))
        elif args.command == "runtime-pause":
            runtime = ReferenceRuntime(args.package, args.runtime_dir)
            print(json.dumps(runtime.pause(args.run_id, args.reason), ensure_ascii=False, indent=2))
        elif args.command == "runtime-resume":
            runtime = ReferenceRuntime(args.package, args.runtime_dir)
            print(json.dumps(runtime.resume(args.run_id), ensure_ascii=False, indent=2))
        elif args.command == "runtime-replay":
            runtime = ReferenceRuntime(args.package, args.runtime_dir)
            report = runtime.replay(args.run_id)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if report["result"] != "PASS":
                return 1
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
