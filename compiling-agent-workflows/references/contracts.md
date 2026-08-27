# 编译器契约

权威字段定义位于：

- `contracts/system-definition.json`：跨仓职责和不变量；
- `schemas/business-requirement.schema.json`：结构化业务输入；
- `schemas/workflow-ir.schema.json`：稳定的可执行契约；
- `schemas/agent-profile.schema.json`：Agent 权限与预算；
- `schemas/loop-spec.schema.json`：持久循环安全约束。

每个软件包必须包含：

```text
workflow.ir.json
graph.json
registry.lock.json
runtime.policy.json
compile-report.json
agents/*.agent.json          存在 Agent 任务时
loops/*.loop.json            存在持久循环时
```

运行时证明还会产生：

```text
runtime/events/<run-id>.jsonl
runtime/checkpoints/<run-id>.json
```

自然语言到业务流程图的综合构建还会产生：

```text
business-requirement.json
interpretation-report.json
process.bpmn
workflow-overview.svg
business-view.json
package/graph.json
```

即使工作流不使用外部 Tool，也必须生成锁文件，因为锁文件还会固定能力目录和共享总定义版本。
