#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.text_pipeline import build_from_business_text  # noqa: E402


def main() -> None:
    output = ROOT / "build/expense-reimbursement"
    manifest = build_from_business_text(
        ROOT / "examples/expense-reimbursement/business-description.txt",
        ROOT / "fixtures/catalog.snapshot.json",
        ROOT / "contracts/system-definition.json",
        output,
        workflow_id="expense-reimbursement",
    )
    print(f"整体工作流程图：{output / 'workflow-overview.svg'}")
    print(f"BPMN 2.0 文件：{output / 'process.bpmn'}")
    print(
        f"生成结果：{manifest['statistics']['participants']} 个参与者，"
        f"{manifest['statistics']['nodes']} 个节点，"
        f"{manifest['statistics']['edges']} 条连线"
    )


if __name__ == "__main__":
    main()
