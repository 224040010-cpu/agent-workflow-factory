# 业务流程图返回规则

当输入来自业务人员自然语言时：

1. 先生成 `interpretation-report.json`，保留所有推断、警告和条件事实映射。
2. 使用解释后的 `business-requirement.json` 生成 BPMN，不得直接从原始文本跳到图片。
3. 编译得到最终 Graph 后，再生成 `workflow-overview.svg`。
4. `.bpmn` 必须包含 BPMN Diagram Interchange 的 Shape、Bounds 和 Waypoint。
5. `business-view.json` 必须同时返回 SVG、BPMN、Graph 和解释报告，并记录摘要。
6. 自然语言推断未经业务人员确认时保持 `REVIEW_REQUIRED`，不得自动部署。

复杂并行、嵌套分支、异常补偿或循环超出规则解释器能力时，应返回复核警告，而不是静默简化流程语义。
