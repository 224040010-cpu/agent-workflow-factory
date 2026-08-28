# Agent 工作流工厂

将受治理的业务工作流编译为可部署的 Agent 软件包。

```text
业务人员自然语言描述
  → 结构化业务需求 + 解释报告
  → BPMN 2.0
  → 工作流 IR
  → 基于事实路由的 Agent Graph
  → Agent / Loop / Policy 清单
  → Harness 适配器
  → 仅追加执行轨迹
```

本仓库负责工作流的编译与运行。Skill 和 Tool 的权威定义仍由 `skill-registory` 维护；本仓库只从不可变的能力目录（Capability Catalog）解析已批准的资产，并将解析结果固定在 `registry.lock.json` 中。

两个仓库各自保存一份字节完全一致的 [`contracts/system-definition.json`](contracts/system-definition.json)。`skill-registory` 发布权威定义，本仓库验证其镜像；如果能力目录使用了不同版本的总定义，本仓库将拒绝打包。

## v3 已实现的纵向能力

- 结构化业务语言契约。
- 将受控中文业务描述解释为结构化需求，并保留置信度、警告和假设。
- 将业务定义确定性地生成 BPMN 2.0。
- 解析包含泳道、任务、网关、顺序条件和循环注解的 BPMN 子集。
- 生成工作流 IR 和基于事实路由的 Graph。
- 按版本解析处于已批准或受限状态的 Skill 和 Tool。
- 生成包含目录摘要和资产摘要的确定性 `registry.lock.json`。
- 根据明确的职责注解生成 Agent Profile。
- 生成包含检查器、有限预算、停止条件和升级策略的 LoopSpec。
- 软件包校验与自动化测试。
- 与模型提供方无关的 Runtime Adapter 契约。
- 支持证据门控路由和有限循环的可执行参考运行时。
- 持久化暂停/恢复检查点，以及基于哈希链的轨迹重放。
- 为业务人员返回含 BPMN DI 的 `.bpmn` 文件、整体流程 SVG 和统一交付清单。
- DeepSeek Harness 只读端到端执行：固定 Tool、真实 Agent 会话、可信 facts、暂停恢复与轨迹回放。
- DeepSeek 多节点只读 Graph：两个 Agent、两个 Tool、可信 facts 网关与双终点路由。
- DeepSeek v0.7 治理：Tool Binding 输入/输出 Schema、实现摘要、按 Agent/节点预算记账与超限升级。
- DeepSeek v0.8 信任链：Ed25519 分离签名、公钥信任库、密钥轮换状态与执行前强制验签。

尚未实现：理解任意自由文本的模型解释器、生产级调度器与会话存储、DeepSeek 人工审批和定时循环绑定、KMS/HSM 私钥托管、整个软件包签名、带签名的事件存储、补偿事务和交互式可视化界面。

## 快速评审

需要 Python 3.11 或更高版本。v0.8 的签名层使用 `cryptography`，安装仓库后即可获得依赖：`python -m pip install -e .`。

```bash
python scripts/workflowctl.py verify-definition

python scripts/workflowctl.py build-from-text \
  examples/expense-reimbursement/business-description.txt \
  --workflow-id expense-reimbursement \
  --output build/expense-reimbursement

python scripts/workflowctl.py generate-bpmn \
  examples/financial-event-monitor/business-requirement.json \
  --output build/financial-event-monitor/process.bpmn

python scripts/workflowctl.py compile \
  build/financial-event-monitor/process.bpmn \
  --business examples/financial-event-monitor/business-requirement.json \
  --catalog fixtures/catalog.snapshot.json \
  --output build/financial-event-monitor/package

python scripts/workflowctl.py validate \
  build/financial-event-monitor/package
```

也可以直接运行完整示例和测试：

```bash
python scripts/run_example.py
python scripts/run_text_example.py
python scripts/run_runtime_example.py
python scripts/run_deepseek_mvp.py
python scripts/run_deepseek_multinode_mvp.py
python -m unittest discover -s tests -v
```

## 仓库职责边界

本仓库负责：

- 业务语言契约；
- BPMN 生成与解析；
- 工作流 IR 和 Graph 契约；
- Agent、Loop 和 Policy 编译；
- Runtime Adapter 和能力协商；
- 轨迹、恢复、重放和评估契约。

本仓库不负责：

- Skill 和 Tool 的权威规范；
- 资产批准或退役；
- Registry 状态变更；
- 运行期间从持续变化的 Git 分支动态发现能力。

自然语言输入格式和返回文件参见 [`docs/business-text-to-diagram.md`](docs/business-text-to-diagram.md)。架构和运行时评审细节参见 [`docs/architecture.md`](docs/architecture.md)、[`docs/reference-runtime.md`](docs/reference-runtime.md)、[`docs/deepseek-readonly-mvp.md`](docs/deepseek-readonly-mvp.md)、[`docs/deepseek-readonly-multinode.md`](docs/deepseek-readonly-multinode.md)、[`docs/deepseek-readonly-v0.7.md`](docs/deepseek-readonly-v0.7.md)、[`docs/deepseek-readonly-v0.8.md`](docs/deepseek-readonly-v0.8.md) 和双仓共享的总定义。
