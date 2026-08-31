# Agent Workflow Factory 开发现状、总体路线与复杂度收敛

## 评估范围

本评估以 [`deepseek-readonly-v1.0.md`](deepseek-readonly-v1.0.md) 为验收基线，覆盖业务语言到 BPMN、Graph、Agent 配置、DeepSeek Harness、可信运行证据以及双仓职责边界。共享总定义继续保持 `3.0.0`；v1.1 只改变 Factory 的使用入口和运行策略，不改变 Skill/Tool、Workflow IR 或 Registry Lock 跨仓契约。

## v1.0 开发现状

### 已完成并具有自动化证据

- 自然语言生成结构化业务需求、解释报告、BPMN 2.0、Graph 和整体 SVG；
- 根据 BPMN 任务生成 Agent Profile、Tool Binding、LoopSpec 和 Runtime Policy；
- 从 `skill-registory` 快照解析已批准能力，并固定为 `registry.lock.json`；
- DeepSeek Harness 单节点和多节点只读执行、可信 Tool facts、暂停恢复和重放；
- 完整软件包 Manifest、Tool Binding、Registry Lock 和信任库根签名；
- PEM 与 PKCS#11 Ed25519 签名 Provider；
- 逐事件签名、检查点签名、轨迹头绑定、回滚与未检查点状态事件检测；
- SQLite 原子事件序号、Worker 租约、终止标记和保留期清理；
- 伪造哈希链、检查点篡改、路径穿越、密钥吊销和预算超限测试。

### 外部环境验收状态

2026-08-31 已在 WSL2 Ubuntu 24.04 参考环境完成：

- SoftHSM 的 PKCS#11 Ed25519 Live Test；
- DeepSeek 官方 SDK 的真实凭据单节点与多节点 Live Test；
- 真实 DeepSeek + SoftHSM 签名运行、无 PIN 重放和 SQLite 篡改拒绝；
- 同时启用 DeepSeek 与 PKCS#11 Live Test 的全量回归。

详细证据参见 [`v1.1-acceptance.md`](v1.1-acceptance.md)。以下生产环境事项仍待目标机构验收：

- 真实 HSM 厂商对 Ed25519 Object 属性、并发会话、故障恢复和密钥轮换的兼容性；
- 组织级审计、法律、隐私和数据保留策略确认。

SoftHSM 与真实 DeepSeek 的参考环境验收不能替代厂商 HSM 认证或组织合规审批。当前结论应表达为“v1.1 技术基线和参考环境验收通过，目标生产环境验收仍需执行”。

### 尚未完成的生产能力

- 跨主机调度、故障接管和分布式事件存储；
- 外部不可回滚时间戳、透明日志或远程审计锚；
- AWS KMS、Azure Key Vault、Google Cloud KMS 等原生驱动；
- 人工审批、定时循环和补偿事务的生产适配器；
- 面向业务人员的交互式流程评审与运行控制界面。

## 总体开发流程

每个版本统一经过以下阶段，避免“先堆功能、后补治理”。

```text
需求与风险分级
  → 契约影响评估
  → 最小纵向功能实现
  → 组件测试与攻击测试
  → 单节点/多节点综合测试
  → 外部环境验收
  → 复杂度收敛与文档
  → 版本门槛评审
```

### 阶段一：需求与风险分级

确认能力属于业务理解、编译、运行、治理还是运维；先确定使用者和风险级别，再决定是否需要签名、租约、HSM 或人工审批。

### 阶段二：契约影响评估

判断是否改变双仓总定义、Capability Catalog、Registry Lock、Workflow IR 或 Agent Profile。只有跨仓语义改变时才升级共享总定义；单仓实现优化不应制造不必要的同步成本。

### 阶段三：最小纵向实现

每次只交付一条可运行链路，例如“业务文本 → BPMN → Graph → Agent → Harness → 轨迹”，避免只完成孤立 Schema 或没有运行证据的抽象接口。

### 阶段四：分层测试

组件测试验证解析和边界；综合测试验证完整流程；攻击测试验证篡改和降级；Live Test 验证真实 SDK、凭据和 HSM。任何一层不能用另一层代替。

### 阶段五：复杂度收敛

新增能力必须映射到预设档位，并提供默认值、预检命令、可操作错误和最短示例。底层参数保留给平台管理员，普通用户只选择风险档位。

## 版本路线

### v1.0：可信单机运行基线

目标是证明编译产物和运行证据可验证。代码、自动化测试、参考环境 DeepSeek Live Test 和 SoftHSM PKCS#11 Live Test 已经完成；真实厂商 HSM 仍按目标部署环境单独验收。

### v1.1：复杂度收敛与配置防错

本版本实现：

- `dev`、`team`、`regulated` 三档运行预设；
- `profile-show` 查看预设；
- `profile-check` 在启动模型前检查配置；
- `team` 和 `regulated` 禁止降级到 JSONL；
- 签名运行必须使用信任库中为指定 Publisher 登记的活动密钥；
- DeepSeek 显式 Dev 模式允许无运行签名，但仍验证受根保护的软件包；
- DeepSeek 默认保持 `regulated`，避免版本升级导致静默安全降级。

v1.1 的退出门槛是预设组件测试、CLI 预检测试、Dev/Regulated DeepSeek 回归、全量测试和文档验收全部通过。上述门槛已于 2026-08-31 完成，详细记录参见 [`v1.1-acceptance.md`](v1.1-acceptance.md)。

### v1.2：项目配置与一键业务入口

第一阶段已经实现可版本化的 `workflow.project.json`，把目录、Catalog、Provider、模型和档位固化为项目配置；已经增加面向业务流程的 `create / review / test-run` 短命令；`--dry-run` 会在临时目录展示将生成的 BPMN、整体 SVG、Agent、Tool 与安全策略，不调用模型或外部系统，也不污染正式输出目录。

`review` 会验证业务交付清单、文件摘要和编译软件包；`test-run` 当前是确定性合同测试，验证软件包与 DeepSeek 只读适配器的能力匹配。它不会把“不支持人工审批或系统任务”包装成运行成功。真实 Provider 调用仍由已有的受治理 `run` 命令承担，后续增量再把项目级安全材料引用与该入口连接。

第一阶段自动化门槛包括配置安全、无副作用预览、Agent/Tool 绑定、交付物篡改拒绝、可运行与阻塞分支，以及 Windows/WSL 全量回归。完整设计参见 [`v1.2-project-entry.md`](v1.2-project-entry.md)。最终退出门槛仍是业务用户完成一次流程生成和测试运行时，不需要理解 PKCS#11、事件库和信任链参数。

### v1.3：生产适配与可观测性

计划增加原生云 KMS Provider、OpenTelemetry 指标与 Trace、PostgreSQL 事件存储接口、Worker 心跳和故障接管协议。SQLite 继续作为 Team 单机模式，不被包装成分布式方案。

退出门槛是多 Worker 故障注入、密钥轮换、恢复点目标和审计导出测试通过。

### v2.0：业务评审控制面

计划提供可视化需求澄清、BPMN 差异评审、Agent 权限解释、人工审批、运行监控和治理反馈回流。界面只是既有确定性契约的控制面，不在浏览器中重新实现编译逻辑。

退出门槛是业务、平台、安全和审计四种角色能够在权限边界内完成各自任务。

## v1.1 三档复杂度模型

### Dev

面向本地开发、演示和低风险原型。默认使用 JSONL、单进程运行、30 天保留，不强制运行签名或根信任。选择 SQLite或签名可以增强 Dev，但不会自动获得 Team 合规声明。

```bash
python scripts/workflowctl.py profile-check --profile dev
```

### Team

面向团队集成测试和受控内部业务。默认使用 SQLite、Worker 租约、90 天保留，并强制运行事件与检查点签名。必须提供运行签名 Provider 和包含其活动公钥的信任库，但不强制运行证据使用离线根。

```bash
python scripts/workflowctl.py profile-check \
  --profile team \
  --runtime-signing-key ~/.config/agent-workflow-factory/runtime.pem \
  --runtime-trust-store ~/.config/agent-workflow-factory/trust.json
```

### Regulated

面向金融生产、强审计和多租户环境。默认使用 SQLite、Worker 租约、365 天保留、运行签名和离线根信任。生产签名建议使用 PKCS#11 HSM；PEM 保留用于集成测试和迁移验证。

```bash
python scripts/workflowctl.py profile-check \
  --profile regulated \
  --runtime-pkcs11-module /path/to/vendor-pkcs11.so \
  --runtime-pkcs11-token-label awf-runtime \
  --runtime-pkcs11-key-label workflow-runtime-key \
  --runtime-pkcs11-key-id sha256:<key-id> \
  --runtime-trust-store ~/.config/agent-workflow-factory/trust.json \
  --runtime-trust-store-signature ~/.config/agent-workflow-factory/trust.sig.json \
  --runtime-trust-root-public-key ~/.config/agent-workflow-factory/root-public.json
```

## 收敛后的使用边界

业务人员只需要描述需求、查看解释结果、评审 BPMN 和确认流程。开发人员选择档位并运行短命令。平台管理员才需要配置数据库、Provider 和 Worker。安全管理员负责根信任、密钥状态和保留策略。

内部仍保留 BPMN、Graph、Agent、Loop、Harness、事件、签名和租约，因为它们解决不同风险；外部入口收敛为“选择档位、预检、运行、重放”。复杂度被封装和分配给正确角色，而不是被删除或转嫁给业务人员。
