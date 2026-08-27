from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.business import BPMN_NS  # noqa: E402
from workflow_factory.diagram import BPMNDI_NS, DC_NS, DI_NS, SVG_NS  # noqa: E402
from workflow_factory.text_pipeline import build_from_business_text  # noqa: E402
from workflow_factory.util import read_json  # noqa: E402
from workflow_factory.validator import validate_package  # noqa: E402


class BusinessTextPipelineIntegrationTest(unittest.TestCase):
    def test_text_to_bpmn_graph_and_business_svg(self) -> None:
        source = ROOT / "examples/expense-reimbursement/business-description.txt"
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "expense-reimbursement"
            manifest = build_from_business_text(
                source,
                ROOT / "fixtures/catalog.snapshot.json",
                ROOT / "contracts/system-definition.json",
                output,
                workflow_id="expense-reimbursement",
            )

            self.assertEqual(manifest["status"], "REVIEW_REQUIRED")
            self.assertEqual(manifest["statistics"]["participants"], 3)
            self.assertEqual(manifest["statistics"]["nodes"], 9)
            self.assertEqual(validate_package(output / "package"), [])
            self.assertEqual(
                {item["type"] for item in manifest["deliverables"]},
                {
                    "business-requirement",
                    "business-view",
                    "bpmn-source",
                    "agent-graph",
                    "interpretation-report",
                },
            )

            bpmn_root = ET.parse(output / "process.bpmn").getroot()
            process = bpmn_root.find(f".//{{{BPMN_NS}}}process")
            self.assertIsNotNone(process)
            self.assertIsNotNone(bpmn_root.find(f".//{{{BPMNDI_NS}}}BPMNDiagram"))
            self.assertGreater(len(bpmn_root.findall(f".//{{{BPMNDI_NS}}}BPMNShape")), 3)
            self.assertGreater(len(bpmn_root.findall(f".//{{{DI_NS}}}waypoint")), 3)
            self.assertGreater(len(bpmn_root.findall(f".//{{{DC_NS}}}Bounds")), 3)

            svg_root = ET.parse(output / "workflow-overview.svg").getroot()
            self.assertEqual(svg_root.tag, f"{{{SVG_NS}}}svg")
            svg_text = (output / "workflow-overview.svg").read_text(encoding="utf-8")
            self.assertIn("提交报销申请", svg_text)
            self.assertIn("部门经理", svg_text)
            self.assertIn("员工费用报销", svg_text)

            graph = read_json(output / "package/graph.json")
            self.assertEqual(graph["spec"]["entry"], "start")
            self.assertTrue(any(edge.get("when") for edge in graph["spec"]["edges"]))
            view = read_json(output / "business-view.json")
            self.assertEqual(view, manifest)

    def test_business_text_is_escaped_in_svg(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "business.txt"
            source.write_text(
                """流程名称：安全测试
流程目标：验证业务文本只作为数据处理。
参与者：业务人员（人工）
流程步骤：
业务人员：记录 <script>alert(1)</script> 文本
流程结束
""",
                encoding="utf-8",
            )
            output = root / "output"
            build_from_business_text(
                source,
                ROOT / "fixtures/catalog.snapshot.json",
                ROOT / "contracts/system-definition.json",
                output,
                workflow_id="safe-business-text",
            )
            svg_root = ET.parse(output / "workflow-overview.svg").getroot()
            self.assertEqual(svg_root.findall(f".//{{{SVG_NS}}}script"), [])
            self.assertIn("<script>", "".join(svg_root.itertext()))


if __name__ == "__main__":
    unittest.main()
