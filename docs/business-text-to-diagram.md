# 自然语言生成整体工作流程图

该功能把业务人员提交的中文业务描述转换为结构化业务需求、BPMN 2.0、可执行 Agent Graph 和面向业务人员的整体流程图。

## 输入格式

当前参考实现使用可审计的中文业务描述格式，支持流程名称、流程目标、参与者、顺序步骤和一层条件分支。示例：

```text
流程名称：员工费用报销
流程目标：让员工提交的费用报销经过经理审核后，由财务系统完成付款和归档。
参与者：员工（人工）、部门经理（人工）、财务系统（系统）
流程步骤：
1. 员工：提交报销申请
2. 部门经理：审核报销申请
3. 若审核通过，则财务系统：执行报销付款，否则员工：补充报销材料
4. 财务系统：归档报销记录
5. 流程结束
```

参与者类型支持 `人工`、`系统`、`Agent/智能体` 和 `外部`。未声明的参与者会被推断并写入警告。条件分支会映射为布尔事实，例如 `facts.decisions.decision-001 == true`，该假设必须由业务人员确认。

当前规则解析器不假装理解任意自由文本。无法可靠识别的复杂并行、跨层嵌套分支、异常补偿和循环应先补充为明确步骤，或由后续模型解释器生成候选结构后进入同一复核流程。

## 生成命令

```bash
python scripts/workflowctl.py build-from-text \
  examples/expense-reimbursement/business-description.txt \
  --workflow-id expense-reimbursement \
  --output build/expense-reimbursement
```

也可以运行仓库内置示例：

```bash
python scripts/run_text_example.py
```

## 返回内容

```text
business-requirement.json   自然语言解释后的结构化业务需求
interpretation-report.json  置信度、警告、假设和业务复核标记
process.bpmn                含 BPMN Diagram Interchange 坐标的 BPMN 2.0 文件
workflow-overview.svg       供业务人员直接查看的整体工作流程图
business-view.json          返回产物、摘要、统计和 SHA-256 摘要
package/graph.json          供运行时使用的 Agent Graph
package/...                 Agent、Loop、Policy 和 Registry 锁文件
```

`business-view.json` 是调用方的稳定返回入口。业务界面应优先展示 `workflow-overview.svg`，同时提供 `process.bpmn` 下载入口，并展示 `interpretation-report.json` 中需要确认的假设。

## 一致性规则

- SVG 和 BPMN DI 使用同一套确定性布局计算。
- SVG 从最终编译的 Graph 和工作流 IR 生成，而不是直接根据自然语言绘制。
- `.bpmn`、`.svg` 和 `graph.json` 都在返回清单中记录 SHA-256 摘要。
- 自然语言解释结果始终标记为 `REVIEW_REQUIRED`，未经业务确认不能直接部署。
- 图形是工作流工厂的派生展示产物，不改变双仓共享总定义或 Registry 治理边界。
