from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.business import validate_business_requirement  # noqa: E402
from workflow_factory.natural_language import interpret_business_text  # noqa: E402


class NaturalLanguageInterpreterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (
            ROOT / "examples/expense-reimbursement/business-description.txt"
        ).read_text(encoding="utf-8")

    def test_interprets_chinese_business_description_and_branch(self) -> None:
        requirement, report = interpret_business_text(
            self.text,
            workflow_id="expense-reimbursement",
        )
        self.assertEqual(validate_business_requirement(requirement), [])
        self.assertEqual(requirement["name"], "员工费用报销")
        self.assertEqual(len(requirement["participants"]), 3)
        self.assertEqual(report["recognized"], {"participants": 3, "actions": 5, "decisions": 1})
        decision = next(step for step in requirement["steps"] if step["id"] == "decision-001")
        self.assertEqual(decision["name"], "审核通过")
        routes = [
            item for item in requirement["transitions"] if item["from"] == "decision-001"
        ]
        self.assertEqual(len(routes), 2)
        self.assertEqual(
            {item["condition"] for item in routes},
            {
                "facts.decisions.decision-001 == true",
                "facts.decisions.decision-001 == false",
            },
        )
        self.assertTrue(report["review_required"])
        self.assertEqual(len(report["assumptions"]), 1)

    def test_infers_undeclared_actor_and_reports_warning(self) -> None:
        text = """流程名称：客户回访
流程目标：完成客户回访记录。
流程步骤：
客服：联系客户
质检人员：检查回访记录
流程结束
"""
        requirement, report = interpret_business_text(text, workflow_id="customer-follow-up")
        self.assertEqual(len(requirement["participants"]), 2)
        self.assertEqual(len(report["warnings"]), 2)
        self.assertTrue(all("未在参与者清单中声明" in item for item in report["warnings"]))

    def test_rejects_description_without_steps(self) -> None:
        with self.assertRaisesRegex(ValueError, "未识别到流程步骤"):
            interpret_business_text("流程名称：空流程", workflow_id="empty-workflow")


if __name__ == "__main__":
    unittest.main()
