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
from workflow_factory.complexity_profiles import (  # noqa: E402
    PROFILES,
    profiles_report,
    resolve_runtime_options,
    validate_runtime_material,
)
from workflow_factory.deepseek_harness import (  # noqa: E402
    DeepSeekHarnessSettings,
    DeepSeekReadonlyAdapter,
    DeepSeekReadonlyRunner,
    DeepSeekTrustPolicy,
)
from workflow_factory.deployment import (  # noqa: E402
    check_deployment,
    create_project_for_deployment,
    run_project,
)
from workflow_factory.reference_runtime import (  # noqa: E402
    ReferenceRuntime,
    RuntimeIntegrityPolicy,
)
from workflow_factory.project import (  # noqa: E402
    create_project,
    review_project,
    test_project,
)
from workflow_factory.signing import (  # noqa: E402
    FileEd25519SigningProvider,
    Pkcs11Ed25519SigningProvider,
    SigningProvider,
    generate_root_key,
    generate_signing_key,
    register_signing_public_key,
    sign_artifact,
    verify_artifact,
    verify_trust_store,
)
from workflow_factory.text_pipeline import build_from_business_text  # noqa: E402
from workflow_factory.util import read_json  # noqa: E402
from workflow_factory.validator import validate_package  # noqa: E402


def verify_definition(definition: Path, checksum: Path) -> None:
    data = read_json(definition)
    expected = checksum.read_text(encoding="ascii").split()[0]
    actual = hashlib.sha256(definition.read_bytes()).hexdigest()
    if expected != actual:
        raise ValueError(f"system-definition checksum mismatch: {expected} != {actual}")
    print(f"Verified system definition {data['definition_version']} ({actual[:12]}...)")


def add_pkcs11_arguments(parser: argparse.ArgumentParser, prefix: str = "") -> None:
    option = f"{prefix}-" if prefix else ""
    dest = f"{prefix.replace('-', '_')}_" if prefix else ""
    parser.add_argument(f"--{option}pkcs11-module", dest=f"{dest}pkcs11_module")
    parser.add_argument(f"--{option}pkcs11-token-label", dest=f"{dest}pkcs11_token_label")
    parser.add_argument(f"--{option}pkcs11-key-label", dest=f"{dest}pkcs11_key_label")
    parser.add_argument(f"--{option}pkcs11-key-id", dest=f"{dest}pkcs11_key_id")
    parser.add_argument(f"--{option}pkcs11-object-id", dest=f"{dest}pkcs11_object_id")
    parser.add_argument(
        f"--{option}pkcs11-pin-env",
        dest=f"{dest}pkcs11_pin_env",
        default="AWF_PKCS11_PIN",
    )


def signing_provider_from_args(
    args: argparse.Namespace,
    prefix: str = "",
) -> SigningProvider | None:
    stem = f"{prefix}_" if prefix else ""
    key_path = getattr(args, f"{stem}signing_key", None)
    module = getattr(args, f"{stem}pkcs11_module", None)
    fields = {
        "module": module,
        "token label": getattr(args, f"{stem}pkcs11_token_label", None),
        "key label": getattr(args, f"{stem}pkcs11_key_label", None),
        "key id": getattr(args, f"{stem}pkcs11_key_id", None),
    }
    if key_path is not None and any(fields.values()):
        raise ValueError("PEM signing key and PKCS#11 signer are mutually exclusive")
    if key_path is not None:
        return FileEd25519SigningProvider(key_path)
    if not any(fields.values()):
        return None
    missing = [name for name, value in fields.items() if not value]
    if missing:
        raise ValueError("Incomplete PKCS#11 signer configuration: " + ", ".join(missing))
    object_id = getattr(args, f"{stem}pkcs11_object_id", None)
    try:
        object_id_bytes = bytes.fromhex(object_id) if object_id else None
    except ValueError as exc:
        raise ValueError("PKCS#11 object id must be hexadecimal") from exc
    return Pkcs11Ed25519SigningProvider(
        module,
        fields["token label"],
        fields["key label"],
        fields["key id"],
        getattr(args, f"{stem}pkcs11_pin_env"),
        object_id_bytes,
    )


def add_runtime_integrity_arguments(
    parser: argparse.ArgumentParser,
    include_signer: bool = True,
    default_profile: str = "dev",
) -> None:
    parser.add_argument(
        "--profile",
        choices=list(PROFILES),
        default=default_profile,
        help="复杂度预设：dev、team 或 regulated",
    )
    if include_signer:
        parser.add_argument("--runtime-signing-key", type=Path)
        add_pkcs11_arguments(parser, "runtime")
    parser.add_argument("--runtime-trust-store", type=Path)
    parser.add_argument("--runtime-trust-store-signature", type=Path)
    parser.add_argument("--runtime-trust-root-public-key", type=Path)
    parser.add_argument(
        "--runtime-publisher", default="agent-workflow-factory-runtime"
    )
    parser.add_argument("--require-runtime-signatures", action="store_true")
    parser.add_argument("--require-runtime-trust-root", action="store_true")
    parser.add_argument("--event-store", choices=["jsonl", "sqlite"])
    parser.add_argument("--lease-owner")
    parser.add_argument("--lease-ttl-seconds", type=int)
    parser.add_argument("--retention-days", type=int)


def reference_runtime_from_args(args: argparse.Namespace) -> ReferenceRuntime:
    provider = signing_provider_from_args(args, "runtime")
    options = resolve_runtime_options(
        args.profile,
        event_store=args.event_store,
        require_runtime_signatures=args.require_runtime_signatures,
        require_runtime_trust_root=args.require_runtime_trust_root,
        lease_owner=args.lease_owner,
        lease_ttl_seconds=args.lease_ttl_seconds,
        retention_days=args.retention_days,
    )
    validate_runtime_material(
        options,
        signing_provider=provider,
        trust_store=args.runtime_trust_store,
        trust_store_signature=args.runtime_trust_store_signature,
        trust_root_public_key=args.runtime_trust_root_public_key,
        mutating=args.command not in {"runtime-replay", "runtime-purge"},
        publisher=args.runtime_publisher,
    )
    return ReferenceRuntime(
        args.package,
        args.runtime_dir,
        RuntimeIntegrityPolicy(
            signing_provider=provider,
            trust_store=args.runtime_trust_store,
            trust_store_signature=args.runtime_trust_store_signature,
            trust_root_public_key=args.runtime_trust_root_public_key,
            publisher=args.runtime_publisher,
            require_signatures=options.require_runtime_signatures,
            require_rooted_trust=options.require_runtime_trust_root,
        ),
        event_store_backend=options.event_store,
        lease_owner=options.lease_owner,
        lease_ttl_seconds=options.lease_ttl_seconds,
        retention_days=options.retention_days,
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="workflowctl")
    subparsers = parser.add_subparsers(dest="command", required=True)

    profile_show = subparsers.add_parser("profile-show")
    profile_show.add_argument("profile", nargs="?", choices=list(PROFILES))

    profile_check = subparsers.add_parser("profile-check")
    add_runtime_integrity_arguments(profile_check)
    profile_check.add_argument(
        "--read-only",
        action="store_true",
        help="只检查重放/审计材料，不要求运行签名私钥",
    )

    create = subparsers.add_parser(
        "create", help="从 workflow.project.json 生成完整工作流交付物"
    )
    create.add_argument("project", type=Path)
    create.add_argument(
        "--deployment-file",
        type=Path,
        help="平台管理员提供的部署配置；正式生成时对软件包签名",
    )
    create.add_argument(
        "--dry-run",
        action="store_true",
        help="只生成临时预览，不写项目输出目录，也不调用外部模型",
    )

    review = subparsers.add_parser(
        "review", help="复核业务视图、BPMN、Agent Graph 和交付物完整性"
    )
    review.add_argument("project", type=Path)

    test_run = subparsers.add_parser(
        "test-run", help="执行确定性合同测试和运行能力预检"
    )
    test_run.add_argument("project", type=Path)
    test_run.add_argument(
        "--dry-run",
        action="store_true",
        help="在临时目录生成并预检，不写项目输出目录，不调用外部模型",
    )

    deploy_check = subparsers.add_parser(
        "deploy-check", help="校验项目软件包、部署信任材料与真实运行环境"
    )
    deploy_check.add_argument("project", type=Path)
    deploy_check.add_argument("--deployment-file", type=Path, required=True)
    deploy_check.add_argument(
        "--live",
        action="store_true",
        help="同时检查 DeepSeek 凭据、官方 SDK 和操作系统支持",
    )

    project_run = subparsers.add_parser(
        "run-project", help="使用项目引用的部署配置执行真实受治理工作流"
    )
    project_run.add_argument("project", type=Path)
    project_run.add_argument("--deployment-file", type=Path, required=True)
    project_run.add_argument("--run-id")
    project_run.add_argument("--facts", type=Path)

    verify = subparsers.add_parser("verify-definition")
    verify.add_argument(
        "--definition", type=Path, default=ROOT / "contracts/system-definition.json"
    )
    verify.add_argument(
        "--checksum", type=Path, default=ROOT / "contracts/system-definition.sha256"
    )

    generate = subparsers.add_parser("generate-bpmn")
    generate.add_argument("business", type=Path)
    generate.add_argument("--output", type=Path, required=True)

    text_build = subparsers.add_parser("build-from-text")
    text_build.add_argument("source", type=Path)
    text_build.add_argument("--workflow-id")
    text_build.add_argument("--catalog", type=Path, default=ROOT / "fixtures/catalog.snapshot.json")
    text_build.add_argument(
        "--definition", type=Path, default=ROOT / "contracts/system-definition.json"
    )
    text_build.add_argument("--output", type=Path, required=True)
    text_build.add_argument("--signing-key", type=Path)
    add_pkcs11_arguments(text_build)
    text_build.add_argument(
        "--signing-publisher", default="agent-workflow-factory-build"
    )

    compile_command = subparsers.add_parser("compile")
    compile_command.add_argument("bpmn", type=Path)
    compile_command.add_argument("--business", type=Path, required=True)
    compile_command.add_argument("--catalog", type=Path, required=True)
    compile_command.add_argument(
        "--definition", type=Path, default=ROOT / "contracts/system-definition.json"
    )
    compile_command.add_argument("--output", type=Path, required=True)
    compile_command.add_argument("--signing-key", type=Path)
    add_pkcs11_arguments(compile_command)
    compile_command.add_argument(
        "--signing-publisher", default="agent-workflow-factory-build"
    )

    validate = subparsers.add_parser("validate")
    validate.add_argument("package", type=Path)
    validate.add_argument("--trust-store", type=Path)
    validate.add_argument("--require-registry-signature", action="store_true")
    validate.add_argument("--require-package-signature", action="store_true")
    validate.add_argument("--trust-store-signature", type=Path)
    validate.add_argument("--trust-root-public-key", type=Path)
    validate.add_argument("--require-trust-root", action="store_true")

    keygen = subparsers.add_parser("keygen")
    keygen.add_argument("--private-key", type=Path, required=True)
    keygen.add_argument("--trust-store", type=Path, required=True)
    keygen.add_argument("--publisher", required=True)

    register_key = subparsers.add_parser("register-key")
    register_key.add_argument("--public-key", type=Path, required=True)
    register_key.add_argument("--trust-store", type=Path, required=True)
    register_key.add_argument("--publisher", required=True)
    register_key.add_argument(
        "--status", choices=["active", "retired", "revoked"], default="active"
    )

    root_keygen = subparsers.add_parser("keygen-root")
    root_keygen.add_argument("--private-key", type=Path, required=True)
    root_keygen.add_argument("--public-key", type=Path, required=True)

    sign = subparsers.add_parser("sign-artifact")
    sign.add_argument("artifact", type=Path)
    sign.add_argument("--private-key", type=Path, required=True)
    sign.add_argument("--output", type=Path, required=True)
    sign.add_argument("--publisher", required=True)

    verify_signature = subparsers.add_parser("verify-artifact")
    verify_signature.add_argument("artifact", type=Path)
    verify_signature.add_argument("--signature", type=Path, required=True)
    verify_signature.add_argument("--trust-store", type=Path, required=True)
    verify_signature.add_argument("--publisher", required=True)

    verify_trust = subparsers.add_parser("verify-trust")
    verify_trust.add_argument("--trust-store", type=Path, required=True)
    verify_trust.add_argument("--signature", type=Path, required=True)
    verify_trust.add_argument("--root-public-key", type=Path, required=True)

    runtime_start = subparsers.add_parser("runtime-start")
    runtime_start.add_argument("package", type=Path)
    runtime_start.add_argument("--runtime-dir", type=Path, required=True)
    runtime_start.add_argument("--run-id")
    runtime_start.add_argument("--facts", type=Path)
    add_runtime_integrity_arguments(runtime_start)

    runtime_route = subparsers.add_parser("runtime-route")
    runtime_route.add_argument("package", type=Path)
    runtime_route.add_argument("run_id")
    runtime_route.add_argument("--runtime-dir", type=Path, required=True)
    add_runtime_integrity_arguments(runtime_route)

    runtime_complete = subparsers.add_parser("runtime-complete")
    runtime_complete.add_argument("package", type=Path)
    runtime_complete.add_argument("run_id")
    runtime_complete.add_argument("node_id")
    runtime_complete.add_argument("--runtime-dir", type=Path, required=True)
    runtime_complete.add_argument("--facts", type=Path, required=True)
    add_runtime_integrity_arguments(runtime_complete)

    runtime_pause = subparsers.add_parser("runtime-pause")
    runtime_pause.add_argument("package", type=Path)
    runtime_pause.add_argument("run_id")
    runtime_pause.add_argument("--runtime-dir", type=Path, required=True)
    runtime_pause.add_argument("--reason", required=True)
    add_runtime_integrity_arguments(runtime_pause)

    runtime_resume = subparsers.add_parser("runtime-resume")
    runtime_resume.add_argument("package", type=Path)
    runtime_resume.add_argument("run_id")
    runtime_resume.add_argument("--runtime-dir", type=Path, required=True)
    add_runtime_integrity_arguments(runtime_resume)

    runtime_replay = subparsers.add_parser("runtime-replay")
    runtime_replay.add_argument("package", type=Path)
    runtime_replay.add_argument("run_id")
    runtime_replay.add_argument("--runtime-dir", type=Path, required=True)
    add_runtime_integrity_arguments(runtime_replay, include_signer=False)

    runtime_purge = subparsers.add_parser("runtime-purge")
    runtime_purge.add_argument("package", type=Path)
    runtime_purge.add_argument("--runtime-dir", type=Path, required=True)
    add_runtime_integrity_arguments(runtime_purge, include_signer=False)

    run_command = subparsers.add_parser("run")
    run_command.add_argument("package", type=Path)
    run_command.add_argument("--adapter", choices=["deepseek"], required=True)
    run_command.add_argument("--runtime-dir", type=Path, required=True)
    run_command.add_argument("--run-id", default="run-deepseek-readonly")
    run_command.add_argument("--facts", type=Path)
    run_command.add_argument("--provider", default="deepseek-official")
    run_command.add_argument("--model", default="deepseek-v4-flash")
    run_command.add_argument("--max-tokens", type=int)
    run_command.add_argument("--base-url")
    run_command.add_argument(
        "--cordis",
        type=Path,
        default=ROOT / "adapters/deepseek-harness/readonly.cordis.yml",
    )
    run_command.add_argument(
        "--trust-store", type=Path, default=ROOT / "trust/trusted-publishers.json"
    )
    run_command.add_argument(
        "--trust-store-signature",
        type=Path,
        default=ROOT / "trust/trusted-publishers.sig.json",
    )
    run_command.add_argument(
        "--trust-root-public-key",
        type=Path,
        default=ROOT / "trust/root-public-key.json",
    )
    run_command.add_argument(
        "--binding-manifest",
        type=Path,
        default=ROOT / "adapters/deepseek-harness/readonly-tool-bindings.json",
    )
    run_command.add_argument(
        "--binding-signature",
        type=Path,
        default=ROOT / "adapters/deepseek-harness/readonly-tool-bindings.sig.json",
    )
    run_command.add_argument("--runtime-signing-key", type=Path)
    add_pkcs11_arguments(run_command, "runtime")
    run_command.add_argument(
        "--runtime-publisher", default="agent-workflow-factory-runtime"
    )
    run_command.add_argument(
        "--profile", choices=list(PROFILES), default="regulated"
    )
    run_command.add_argument("--event-store", choices=["jsonl", "sqlite"])
    run_command.add_argument("--lease-owner")
    run_command.add_argument("--lease-ttl-seconds", type=int)
    run_command.add_argument("--retention-days", type=int)

    args = parser.parse_args()
    try:
        if args.command == "profile-show":
            report = profiles_report()
            if args.profile:
                report = {args.profile: report[args.profile]}
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.command == "profile-check":
            provider = signing_provider_from_args(args, "runtime")
            options = resolve_runtime_options(
                args.profile,
                event_store=args.event_store,
                require_runtime_signatures=args.require_runtime_signatures,
                require_runtime_trust_root=args.require_runtime_trust_root,
                lease_owner=args.lease_owner,
                lease_ttl_seconds=args.lease_ttl_seconds,
                retention_days=args.retention_days,
            )
            validate_runtime_material(
                options,
                signing_provider=provider,
                trust_store=args.runtime_trust_store,
                trust_store_signature=args.runtime_trust_store_signature,
                trust_root_public_key=args.runtime_trust_root_public_key,
                mutating=not args.read_only,
                publisher=args.runtime_publisher,
            )
            if options.require_runtime_trust_root:
                verify_trust_store(
                    args.runtime_trust_store,
                    args.runtime_trust_store_signature,
                    args.runtime_trust_root_public_key,
                )
            print(
                json.dumps(
                    {"result": "PASS", "runtime_profile": options.as_dict()},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "create":
            report = (
                create_project_for_deployment(
                    args.project,
                    args.deployment_file,
                    dry_run=args.dry_run,
                )
                if args.deployment_file
                else create_project(args.project, dry_run=args.dry_run)
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.command == "review":
            report = review_project(args.project)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if report["result"] != "PASS":
                return 1
        elif args.command == "test-run":
            report = test_project(args.project, dry_run=args.dry_run)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if report["result"] != "PASS":
                return 1
        elif args.command == "deploy-check":
            report = check_deployment(
                args.project,
                args.deployment_file,
                require_live_environment=args.live,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if report["result"] != "PASS":
                return 1
        elif args.command == "run-project":
            report = run_project(
                args.project,
                args.deployment_file,
                run_id=args.run_id,
                initial_facts=read_json(args.facts) if args.facts else None,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if report["result"] != "PASS":
                return 1
        elif args.command == "verify-definition":
            verify_definition(args.definition, args.checksum)
        elif args.command == "generate-bpmn":
            generate_bpmn(args.business, args.output)
            print(f"BPMN → {args.output}")
        elif args.command == "build-from-text":
            manifest = build_from_business_text(
                args.source,
                args.catalog,
                args.definition,
                args.output,
                workflow_id=args.workflow_id,
                signing_publisher=args.signing_publisher,
                signing_provider=signing_provider_from_args(args),
            )
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
        elif args.command == "compile":
            report = compile_package(
                args.bpmn,
                args.business,
                args.catalog,
                args.definition,
                args.output,
                signing_publisher=args.signing_publisher,
                signing_provider=signing_provider_from_args(args),
            )
            print(
                f"Package → {args.output} "
                f"({report['generated_agents']} agents, {report['resolved_tools']} tools)"
            )
        elif args.command == "validate":
            errors = validate_package(
                args.package,
                trust_store=args.trust_store,
                require_registry_signature=args.require_registry_signature,
                require_package_signature=args.require_package_signature,
                trust_store_signature=args.trust_store_signature,
                trust_root_public_key=args.trust_root_public_key,
                require_trust_root=args.require_trust_root,
            )
            if errors:
                for error in errors:
                    print(f"ERROR: {error}", file=sys.stderr)
                return 1
            print(f"Package verified: {args.package}")
        elif args.command == "keygen":
            record = generate_signing_key(
                args.private_key, args.trust_store, args.publisher
            )
            print(json.dumps(record, ensure_ascii=False, indent=2))
        elif args.command == "register-key":
            record = register_signing_public_key(
                args.public_key,
                args.trust_store,
                args.publisher,
                args.status,
            )
            print(json.dumps(record, ensure_ascii=False, indent=2))
        elif args.command == "keygen-root":
            record = generate_root_key(args.private_key, args.public_key)
            print(json.dumps(record, ensure_ascii=False, indent=2))
        elif args.command == "sign-artifact":
            envelope = sign_artifact(
                args.artifact,
                args.private_key,
                args.output,
                args.publisher,
            )
            print(json.dumps(envelope["statement"], ensure_ascii=False, indent=2))
        elif args.command == "verify-artifact":
            report = verify_artifact(
                args.artifact,
                args.signature,
                args.trust_store,
                args.publisher,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.command == "verify-trust":
            report = verify_trust_store(
                args.trust_store,
                args.signature,
                args.root_public_key,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.command == "runtime-start":
            runtime = reference_runtime_from_args(args)
            facts = read_json(args.facts) if args.facts else {}
            state = runtime.start(facts, args.run_id)
            print(json.dumps(state, ensure_ascii=False, indent=2))
        elif args.command == "runtime-route":
            runtime = reference_runtime_from_args(args)
            print(json.dumps(runtime.route(args.run_id).as_dict(), ensure_ascii=False, indent=2))
        elif args.command == "runtime-complete":
            runtime = reference_runtime_from_args(args)
            state = runtime.complete(args.run_id, args.node_id, read_json(args.facts))
            print(json.dumps(state, ensure_ascii=False, indent=2))
        elif args.command == "runtime-pause":
            runtime = reference_runtime_from_args(args)
            print(json.dumps(runtime.pause(args.run_id, args.reason), ensure_ascii=False, indent=2))
        elif args.command == "runtime-resume":
            runtime = reference_runtime_from_args(args)
            print(json.dumps(runtime.resume(args.run_id), ensure_ascii=False, indent=2))
        elif args.command == "runtime-replay":
            runtime = reference_runtime_from_args(args)
            report = runtime.replay(args.run_id)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if report["result"] != "PASS":
                return 1
        elif args.command == "runtime-purge":
            runtime = reference_runtime_from_args(args)
            count = runtime.purge_expired_runs()
            print(f"Purged terminal runtime records: {count}")
        elif args.command == "run":
            runtime_provider = signing_provider_from_args(args, "runtime")
            runtime_options = resolve_runtime_options(
                args.profile,
                event_store=args.event_store,
                lease_owner=args.lease_owner,
                lease_ttl_seconds=args.lease_ttl_seconds,
                retention_days=args.retention_days,
            )
            validate_runtime_material(
                runtime_options,
                signing_provider=runtime_provider,
                trust_store=args.trust_store,
                trust_store_signature=args.trust_store_signature,
                trust_root_public_key=args.trust_root_public_key,
                mutating=True,
                publisher=args.runtime_publisher,
            )
            settings = DeepSeekHarnessSettings(
                provider=args.provider,
                model=args.model,
                max_tokens=args.max_tokens,
                cwd=args.runtime_dir / "harness-workspace",
                session_root=args.runtime_dir / "harness-sessions",
                cordis=args.cordis,
                base_url=args.base_url,
            )
            adapter = DeepSeekReadonlyAdapter(settings=settings)
            try:
                report = DeepSeekReadonlyRunner(
                    args.package,
                    args.runtime_dir,
                    adapter,
                    DeepSeekTrustPolicy(
                        trust_store=args.trust_store,
                        trust_store_signature=args.trust_store_signature,
                        trust_root_public_key=args.trust_root_public_key,
                        binding_manifest=args.binding_manifest,
                        binding_signature=args.binding_signature,
                    ),
                    runtime_signing_provider=runtime_provider,
                    runtime_publisher=args.runtime_publisher,
                    event_store_backend=runtime_options.event_store,
                    lease_owner=runtime_options.lease_owner,
                    lease_ttl_seconds=runtime_options.lease_ttl_seconds,
                    retention_days=runtime_options.retention_days,
                    require_runtime_signatures=(
                        runtime_options.require_runtime_signatures
                    ),
                    require_runtime_rooted_trust=(
                        runtime_options.require_runtime_trust_root
                    ),
                ).run(
                    read_json(args.facts) if args.facts else {},
                    run_id=args.run_id,
                )
            finally:
                adapter.close()
            report["complexity_profile"] = runtime_options.profile.name
            report["runtime_profile"] = runtime_options.as_dict()
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if report["result"] != "PASS":
                return 1
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
