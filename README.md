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

## 当前已实现的纵向能力

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
- DeepSeek v0.9 全包信任：BPMN/Graph/Agent/Policy/Lock 全清单签名、离线根签名和可插拔签名 Provider。
- DeepSeek v1.0 生产信任：PKCS#11 Ed25519 HSM Provider、逐事件签名、检查点签名、SQLite 原子事件存储、运行租约和终止记录保留策略。
- v1.1 复杂度收敛：`dev`、`team`、`regulated` 三档运行预设、配置预检、活动签名密钥检查和高等级防降级保护。
- v1.2 项目入口：使用一份 `workflow.project.json` 固化输入、输出、能力目录和运行档位，并通过 `create / review / test-run` 完成预览、生成、复核和能力预检。
- SQLite 连接在事务结束后显式释放，Windows 与 Linux 均可立即清理或归档运行数据库。

v1.1 技术基线已于 2026-08-31 完成真实 DeepSeek、SoftHSM PKCS#11、HSM 签名运行、无私钥重放、防篡改和全量回归验收。验收范围、证据和仍需目标机构确认的生产边界参见 [`docs/v1.1-acceptance.md`](docs/v1.1-acceptance.md)。

尚未实现：理解任意自由文本的模型解释器、跨主机生产调度器、DeepSeek 人工审批和定时循环绑定、非 PKCS#11 云 KMS 驱动、外部不可回滚时间戳锚、补偿事务和交互式可视化界面。

## 快速评审

需要 Python 3.11 或更高版本。基础 Ed25519 签名层使用 `cryptography`；连接 PKCS#11 HSM 时安装可选依赖：`python -m pip install -e '.[pkcs11]'`。

### v1.2 面向业务项目的最短入口

项目定义样例位于 [`examples/readonly-intent-review/workflow.project.json`](examples/readonly-intent-review/workflow.project.json)。其中只保存业务输入、输出目录、能力目录、模型名称和复杂度档位，不允许保存 API Key、PIN、私钥或密码。

第一步先预览。该命令在临时目录生成 BPMN、整体流程图、Agent、Tool 和安全策略，不写项目输出目录，也不调用 DeepSeek 或任何外部系统：

```bash
python scripts/workflowctl.py create \
  examples/readonly-intent-review/workflow.project.json \
  --dry-run
```

第二步正式生成并复核交付物：

```bash
python scripts/workflowctl.py create \
  examples/readonly-intent-review/workflow.project.json

python scripts/workflowctl.py review \
  examples/readonly-intent-review/workflow.project.json
```

第三步执行确定性合同测试。它验证软件包完整性、工具是否已固定、运行策略与 DeepSeek 只读适配器能力是否匹配，但不会产生真实模型费用：

```bash
python scripts/workflowctl.py test-run \
  examples/readonly-intent-review/workflow.project.json
```

`READY` 表示该 Graph 可以进入现有受治理 `run` 链路；`BLOCKED` 会列出缺失能力和不支持的节点类型。报销示例含人工与系统节点，因此使用当前 DeepSeek 只读适配器时会如实返回 `human_gate` 和 `script_task` 阻塞，而不会伪造运行成功。详细设计、输出字段和边界参见 [`docs/v1.2-project-entry.md`](docs/v1.2-project-entry.md)。

### 底层命令评审

```bash
python scripts/workflowctl.py profile-show
python scripts/workflowctl.py profile-check --profile dev

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

本地开发显式使用 `--profile dev`，无需运行签名材料；团队环境使用
`--profile team`，自动选择 SQLite、租约和签名运行；金融生产使用
`--profile regulated`，额外强制离线根信任和 365 天默认保留期。
DeepSeek `run` 为避免升级后静默降级，默认仍是 `regulated`。完整评估、版本路线和
预设参数参见 [`docs/development-roadmap-and-complexity.md`](docs/development-roadmap-and-complexity.md)。

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

自然语言输入格式和返回文件参见 [`docs/business-text-to-diagram.md`](docs/business-text-to-diagram.md)。架构和运行时评审细节参见 [`docs/architecture.md`](docs/architecture.md)、[`docs/reference-runtime.md`](docs/reference-runtime.md)、[`docs/development-roadmap-and-complexity.md`](docs/development-roadmap-and-complexity.md)、[`docs/v1.1-acceptance.md`](docs/v1.1-acceptance.md)、[`docs/v1.2-project-entry.md`](docs/v1.2-project-entry.md)、[`docs/deepseek-readonly-mvp.md`](docs/deepseek-readonly-mvp.md)、[`docs/deepseek-readonly-multinode.md`](docs/deepseek-readonly-multinode.md)、[`docs/deepseek-readonly-v0.7.md`](docs/deepseek-readonly-v0.7.md)、[`docs/deepseek-readonly-v0.8.md`](docs/deepseek-readonly-v0.8.md)、[`docs/deepseek-readonly-v0.9.md`](docs/deepseek-readonly-v0.9.md)、[`docs/deepseek-readonly-v1.0.md`](docs/deepseek-readonly-v1.0.md) 和双仓共享的总定义。
