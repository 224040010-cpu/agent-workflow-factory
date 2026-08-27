from __future__ import annotations

from pathlib import Path

from .business import render_bpmn
from .compiler import compile_package
from .diagram import render_graph_svg
from .natural_language import interpret_business_text
from .util import read_json, sha256_file, write_json
from .validator import validate_package


def build_from_business_text(
    source_path: Path,
    catalog_path: Path,
    definition_path: Path,
    output_dir: Path,
    workflow_id: str | None = None,
) -> dict:
    text = source_path.read_text(encoding="utf-8")
    requirement, interpretation = interpret_business_text(text, workflow_id=workflow_id)

    requirement_path = output_dir / "business-requirement.json"
    interpretation_path = output_dir / "interpretation-report.json"
    bpmn_path = output_dir / "process.bpmn"
    package_dir = output_dir / "package"
    svg_path = output_dir / "workflow-overview.svg"

    write_json(requirement_path, requirement)
    write_json(interpretation_path, interpretation)
    bpmn_path.parent.mkdir(parents=True, exist_ok=True)
    bpmn_path.write_bytes(render_bpmn(requirement))
    compile_report = compile_package(
        bpmn_path,
        requirement_path,
        catalog_path,
        definition_path,
        package_dir,
    )
    validation_errors = validate_package(package_dir)
    if validation_errors:
        raise ValueError("综合流程生成失败：" + "; ".join(validation_errors))

    graph = read_json(package_dir / "graph.json")
    workflow_ir = read_json(package_dir / "workflow.ir.json")
    render_graph_svg(graph, workflow_ir, svg_path)
    manifest = {
        "schema_version": "1.0.0",
        "workflow_id": requirement["workflow_id"],
        "name": requirement["name"],
        "intent": requirement["intent"],
        "status": "REVIEW_REQUIRED" if interpretation["review_required"] else "READY",
        "business_review": {
            "confidence": interpretation["confidence"],
            "warnings": interpretation["warnings"],
            "assumptions": interpretation["assumptions"],
        },
        "statistics": {
            "participants": len(requirement["participants"]),
            "nodes": len(graph["spec"]["nodes"]),
            "edges": len(graph["spec"]["edges"]),
            "agents": compile_report["generated_agents"],
            "loops": compile_report["generated_loops"],
        },
        "deliverables": [
            {
                "type": "business-requirement",
                "media_type": "application/json",
                "path": "business-requirement.json",
                "digest": sha256_file(requirement_path),
                "description": "自然语言解释后的结构化业务需求",
            },
            {
                "type": "business-view",
                "media_type": "image/svg+xml",
                "path": "workflow-overview.svg",
                "digest": sha256_file(svg_path),
                "description": "供业务人员直接查看的整体工作流程图",
            },
            {
                "type": "bpmn-source",
                "media_type": "application/bpmn+xml",
                "path": "process.bpmn",
                "digest": sha256_file(bpmn_path),
                "description": "包含 BPMN Diagram Interchange 坐标的 BPMN 2.0 文件",
            },
            {
                "type": "agent-graph",
                "media_type": "application/json",
                "path": "package/graph.json",
                "digest": sha256_file(package_dir / "graph.json"),
                "description": "供运行时使用的 Agent Graph",
            },
            {
                "type": "interpretation-report",
                "media_type": "application/json",
                "path": "interpretation-report.json",
                "digest": sha256_file(interpretation_path),
                "description": "自然语言推断、置信度和业务复核信息",
            },
        ],
    }
    write_json(output_dir / "business-view.json", manifest)
    return manifest
