# Agent Workflow Factory

Agent Workflow Factory 是一个把业务人员的流程描述转换为可评审、可验证、可部署 Agent 工作流的软件工厂。

业务人员先用自然语言描述流程目标、参与角色、处理步骤和判断条件，系统随后生成结构化需求、BPMN 2.0 流程图、Agent Graph、Agent 配置、Tool 锁定清单与运行安全策略。通过评审和部署检查后，工作流可以交给 DeepSeek Harness 等 Agent Runtime 执行，并保留可恢复、可重放、可验证的运行证据。

```text
业务自然语言
  → 结构化需求与解释报告
  → BPMN 2.0 与整体流程图
  → Workflow IR 与 Agent Graph
  → Agent / Tool / Loop / Policy
  → 业务评审与运行能力检查
  → 受治理部署与 Agent 执行
  → 事件轨迹、检查点与审计重放
```

当前版本：`1.2.1`

## 项目解决什么问题

传统业务流程产品通常存在两个断层。

第一个断层发生在业务描述与技术实现之间：业务人员会描述“员工提交报销、经理审批、财务付款”，但开发团队还需要手工将其转换为 BPMN、服务调用、Agent 职责和异常路径。

第二个断层发生在“模型能回答”与“Agent 可以安全执行”之间：即使模型能够理解业务，也不能直接证明它使用了哪个 Tool、为什么获得该权限、是否修改外部系统、运行证据有没有被篡改，以及中断后能否安全恢复。

本项目通过确定性的编译、能力锁定、运行策略和可信轨迹连接这两个断层：

- 让业务流程首先变成可查看、可讨论的 BPMN；
- 让 BPMN 进一步变成真实 Agent Graph 和 Agent Profile；
- 只允许使用能力目录中已批准并固定版本的 Skill 和 Tool；
- 在调用模型前检查权限、签名、预算和运行能力；
- 将模型结论与可信 Tool Facts 对齐；
- 为暂停、恢复、重放、审计和防篡改保留证据。

## 面向哪些场景

### 业务流程设计与评审

适用于把受控中文业务描述快速转换为 BPMN，例如费用报销、材料审核、风险复核、客户服务、运营检查和知识处理流程。

业务人员可以查看整体流程图、参与者、判断分支、系统推断的假设和警告；技术人员可以继续查看 Graph、Agent、Tool 与 Policy。

### 只读 Agent 自动化

适用于读取资料、解析意图、检查歧义、汇总证据、生成判断但不修改业务系统的流程。当前 DeepSeek Harness 纵向链路已经支持真实 Agent 会话、固定只读 Tool、可信 Facts、预算、暂停恢复和轨迹重放。

### 金融与受监管场景的技术验证

适用于需要软件包签名、运行事件签名、离线根信任、HSM、事件保留和防篡改验证的原型与技术基线。

项目提供三档复杂度：

- `dev`：本地开发、演示和低风险原型；
- `team`：团队集成测试和受控内部业务；
- `regulated`：金融生产、强审计和长期保留场景。

### Agent 工作流基础设施研发

适用于研究业务语言、BPMN、Graph Engineering、Loop Engineering、Agent Harness、能力治理和可信运行如何组合成一条完整工程链路。

## 项目目标

### 业务目标

- 让业务人员使用业务语言描述流程，而不是先学习 Agent 配置；
- 为业务人员返回可以直接评审的 BPMN 和整体流程图；
- 清楚解释流程中有哪些 Agent、Tool、权限、假设和风险；
- 在真实执行前说明流程是 `READY` 还是 `BLOCKED`，以及原因是什么；
- 最终形成“描述—澄清—评审—发布—运行—反馈”的业务闭环。

### 工程目标

- 让相同输入和相同能力目录产生可复现的软件包；
- 用 `registry.lock.json` 固定 Skill 和 Tool 版本；
- 让模型输出服从 Tool 证据和完成条件；
- 支持暂停、恢复、有限循环、预算和终止条件；
- 支持 PEM 与 PKCS#11 Ed25519 签名；
- 支持 JSONL 与 SQLite 事件存储、运行租约和保留策略；
- 支持软件包、事件和检查点的验签与重放。

### 安全目标

- API Key、HSM PIN 和私钥内容不得进入项目定义；
- 高等级运行环境不能静默降级到低等级策略；
- 未批准、未固定或没有 Host Binding 的 Tool 不能执行；
- 被修改、缺失或额外注入文件的软件包不能进入运行；
- 被修改的事件或检查点不能通过重放验证。

## 项目不是什么

本项目当前不是一个通用低代码平台，也不是一个已经完成的企业级流程管理系统。

它目前不提供正式的浏览器业务界面、组织级登录与 RBAC、人工任务收件箱、跨主机调度、云 KMS 原生驱动或通用外部系统连接器。它也不会在运行期间从不断变化的 Git 分支动态加载能力。

Skill 和 Tool 的权威定义、批准、限制和退役由独立的 `skill-registory` 仓库负责；本仓库只消费不可变的 Capability Catalog，并把解析结果固定到工作流软件包中。

两个仓库共同维护字节一致的 [`contracts/system-definition.json`](contracts/system-definition.json)。如果 Catalog 使用的总定义版本与本仓库不一致，编译会被拒绝。

## 当前阶段

项目当前处于“可信技术内核完成，业务应用控制面待建设”的阶段。

### 已完成

- 受控中文业务描述解析；
- 结构化业务需求、解释置信度、警告和假设；
- 带 BPMN DI 坐标的 BPMN 2.0 文件；
- 面向业务人员的整体 SVG 流程图；
- Workflow IR、基于可信 Facts 的 Agent Graph；
- Agent Profile、LoopSpec 和 Runtime Policy；
- Catalog 解析与 Registry Lock；
- DeepSeek Harness 单节点与多节点只读执行；
- Tool Binding Schema、实现摘要和输入输出校验；
- Agent、节点、Token 和 Tool 调用预算；
- 暂停恢复、检查点、哈希链和确定性重放；
- Registry Lock、Tool Binding 和完整软件包签名；
- 根签名信任库、密钥状态和吊销检查；
- PEM 与 PKCS#11 Ed25519 签名 Provider；
- 逐事件与检查点签名；
- SQLite 原子事件、运行租约与终止记录保留；
- `dev / team / regulated` 三档复杂度预设；
- `workflow.project.json` 项目入口；
- `create / review / test-run` 业务短命令；
- 独立 `workflow.deployment.json` 部署边界；
- `deploy-check / run-project` 真实部署入口。

2026 年 8 月 31 日已经在 WSL2 Ubuntu 24.04 参考环境完成真实 DeepSeek、SoftHSM PKCS#11、HSM 签名运行、无私钥重放、防篡改和全量回归验收。该结果证明参考技术链路可用，但不能替代目标机构的真实 HSM 认证、组织安全评审和生产验收。

### 尚未完成

- 面向业务人员的 Web 应用；
- BPMN 在线评审、评论和版本差异；
- 假设、歧义和业务批准工作流；
- Human Task Provider 与任务收件箱；
- 系统写操作、补偿事务和通用连接器；
- 登录、组织、租户与细粒度 RBAC；
- 运行监控、指标看板和业务反馈闭环；
- 跨主机 Worker、故障接管和分布式事件存储；
- 云 KMS、外部时间戳锚和企业 Secret Manager 集成。

## 后续开发计划

### v1.3：业务评审台

目标是让不懂 CLI 的业务人员在浏览器中完成一次流程创建和评审。

计划交付：

- 项目列表和行业流程模板；
- 自然语言创建向导和附件输入；
- BPMN 在线查看与节点详情；
- 警告、假设和歧义逐条确认；
- Agent、Tool、权限与完成证据解释；
- `READY / BLOCKED` 可视化测试结果；
- 评审版本、评论和确认记录。

第一条纵向切片将使用薄 API 调用现有编译器，不在前端重新实现 BPMN、Graph 或治理逻辑。

### v1.4：人机协同与发布

目标是让包含人工审批和系统任务的流程能够真实闭环。

计划交付：

- Human Task Provider；
- 任务收件箱和动态表单；
- 指派、转交、代理、催办和升级；
- 人工决定写入可信 Facts；
- 系统任务连接器与补偿策略；
- 业务批准、技术批准和环境晋级。

退出标准是费用报销示例能够完成员工提交、经理审批、系统付款和归档。

### v1.5：运营产品化

目标是让运营人员能够观察、处置并持续改进已经发布的工作流。

计划交付：

- 运行中心和 BPMN 节点状态；
- 事件时间线、暂停恢复和失败处置；
- 成功率、耗时、Token、成本和人工介入指标；
- OpenTelemetry、告警和审计导出；
- 流程版本对比、灰度和回滚；
- 业务反馈、评测集和回归验证。

### v2.0：企业控制面

目标是支持多个组织和团队安全复用工作流工厂。

计划交付：

- SSO、组织、租户和项目级 RBAC；
- Secret Manager 与云 KMS；
- PostgreSQL 事件存储和多 Worker；
- 配额、成本中心和数据地域策略；
- Skill/Tool 目录检索与治理反馈；
- 开放 API、Webhook 和嵌入式组件。

## 快速开始

### 环境要求

- Python 3.11 或更高版本；
- Git；
- 基础离线编译需要 `cryptography`；
- 真实 DeepSeek Harness 运行建议使用 WSL2 Ubuntu、Linux 或 macOS；
- PKCS#11 HSM 测试需要 SoftHSM 或厂商 PKCS#11 Module。

### 1. 下载并安装

```bash
git clone https://github.com/224040010-cpu/agent-workflow-factory.git
cd agent-workflow-factory

python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

在 Windows PowerShell 中激活虚拟环境：

```powershell
.\.venv\Scripts\Activate.ps1
```

需要真实 DeepSeek Harness 时安装：

```bash
python -m pip install -e '.[deepseek]'
```

需要 PKCS#11 HSM 时安装：

```bash
python -m pip install -e '.[pkcs11]'
```

两者都需要时：

```bash
python -m pip install -e '.[deepseek,pkcs11]'
```

确认版本与命令：

```bash
python -c "import workflow_factory; print(workflow_factory.__version__)"
python scripts/workflowctl.py -h
```

### 2. 无外部调用预览

这是最安全、最适合首次体验的入口。它会在临时目录完成自然语言解释、BPMN 生成和 Graph 编译，但不写正式输出目录、不调用 DeepSeek，也不产生模型费用。

```bash
python scripts/workflowctl.py create \
  examples/readonly-intent-review/workflow.project.json \
  --dry-run
```

输出报告会展示业务流程、解释结果、BPMN/Graph 交付物、Agent、Tool、权限、Runtime Policy 与复杂度档位。

### 3. 生成正式项目

```bash
python scripts/workflowctl.py create \
  examples/readonly-intent-review/workflow.project.json
```

默认输出目录是：

```text
build/projects/readonly-intent-review/
```

主要交付物包括：

```text
business-requirement.json   结构化业务需求
interpretation-report.json  解释置信度、警告和假设
process.bpmn                BPMN 2.0 流程文件
workflow-overview.svg       面向业务人员的整体流程图
business-view.json          统一业务交付清单
package/graph.json          Agent Graph
package/agents/             Agent Profile
package/registry.lock.json  固定后的 Skill/Tool 版本
package/runtime.policy.json 运行策略
```

为避免混合新旧交付物，`create` 不会覆盖非空输出目录。如果目录已经存在，先执行 `review`；确实需要重新生成时，应先把旧目录归档到其他名称。

### 4. 复核生成结果

```bash
python scripts/workflowctl.py review \
  examples/readonly-intent-review/workflow.project.json
```

`review` 会检查业务交付清单、文件摘要和软件包结构。`PASS` 表示文件完整且结构合法，不代表业务负责人已经批准业务语义。

### 5. 执行离线合同测试

```bash
python scripts/workflowctl.py test-run \
  examples/readonly-intent-review/workflow.project.json
```

该命令不会调用真实模型。它会验证 Agent/Tool 绑定、Host Binding、Tool Facts、完成证据、Runtime Policy 和适配器能力。

只读意图复核示例应返回 `PASS / READY`。费用报销示例包含人工审批和系统任务，当前会如实返回 `BLOCKED`，并列出 `human_gate` 与 `script_task` 等缺失能力。

## 连接真实 DeepSeek Harness

真实运行需要独立部署配置。项目文件只保存非敏感的 `deployment_ref`；部署文件由平台管理员维护，只引用信任材料、签名器和凭据环境变量名称。

参考模板：[`examples/readonly-intent-review/workflow.deployment.example.json`](examples/readonly-intent-review/workflow.deployment.example.json)

不要直接使用模板中的占位路径。复制模板后，把运行目录、Cordis、信任库、Tool Binding 和签名器路径全部修改为当前机器上的真实绝对路径。不要把 API Key、HSM PIN 或私钥内容写入 JSON。

### 1. 注入凭据

DeepSeek API Key 默认从 `DEEPSEEK_API_KEY` 读取：

```bash
read -rsp "DeepSeek API Key: " DEEPSEEK_API_KEY
export DEEPSEEK_API_KEY
echo
```

PKCS#11 PIN 应通过部署文件中声明的环境变量注入，例如：

```bash
read -rsp "PKCS#11 PIN: " AWF_PKCS11_PIN
export AWF_PKCS11_PIN
echo
```

### 2. 生成签名软件包

```bash
python scripts/workflowctl.py create \
  examples/readonly-intent-review/workflow.project.json \
  --deployment-file ~/.config/agent-workflow-factory/readonly-intent-review.deployment.json
```

如果此前已经生成未签名输出，应先归档旧输出目录。正式生成会检查 Build Signer 的活动登记状态，并在完成后验证 Registry Lock 和完整软件包签名。

### 3. 部署预检

```bash
python scripts/workflowctl.py deploy-check \
  examples/readonly-intent-review/workflow.project.json \
  --deployment-file ~/.config/agent-workflow-factory/readonly-intent-review.deployment.json \
  --live
```

预检会检查软件包、根信任、Tool Binding、复杂度档位、官方 SDK、操作系统、API Key 和 PKCS#11 PIN。报告只显示环境变量名称和是否存在，不输出凭据值。

### 4. 启动真实运行

```bash
python scripts/workflowctl.py run-project \
  examples/readonly-intent-review/workflow.project.json \
  --deployment-file ~/.config/agent-workflow-factory/readonly-intent-review.deployment.json \
  --run-id run-readonly-intent-review
```

不提供 `--facts` 时，系统会把项目业务文本放入 `facts.business.description`。也可以通过 `--facts /path/to/facts.json` 提供初始可信事实。相同 `run-id` 会按照已有检查点恢复，不会重新执行已经完成的节点。

## 验证与测试

运行全部不需要真实凭据的自动化测试：

```bash
python -m unittest discover -s tests -v
```

查看三档复杂度预设：

```bash
python scripts/workflowctl.py profile-show
python scripts/workflowctl.py profile-check --profile dev
```

运行参考示例：

```bash
python scripts/run_example.py
python scripts/run_text_example.py
python scripts/run_runtime_example.py
python scripts/run_deepseek_mvp.py
python scripts/run_deepseek_multinode_mvp.py
```

真实 DeepSeek、PKCS#11 和联合 Live Test 需要显式环境开关与本地凭据，具体步骤参见相关设计与验收文档。

## 仓库职责边界

本仓库负责：

- 业务语言契约和解释结果；
- BPMN 生成、解析与业务流程图；
- Workflow IR 和 Agent Graph；
- Agent、Loop 和 Runtime Policy 编译；
- Capability Catalog 消费与 Registry Lock；
- Runtime Adapter 与能力协商；
- 项目配置、部署预检和真实运行入口；
- 事件、检查点、恢复、重放和审计证据。

`skill-registory` 负责：

- Skill 和 Tool 的权威规范；
- 资产状态、版本、批准、限制和退役；
- Catalog 发布与治理规则；
- Skill/Tool 质量验证和治理反馈。

运行期间不会直接读取持续变化的 Registry 分支，而是使用经过审核的 Catalog Snapshot 和 `registry.lock.json`。

## 文档索引

- [架构设计](docs/architecture.md)
- [自然语言到 BPMN 与业务流程图](docs/business-text-to-diagram.md)
- [参考运行时](docs/reference-runtime.md)
- [总体开发路线与复杂度收敛](docs/development-roadmap-and-complexity.md)
- [v1.1 技术基线验收](docs/v1.1-acceptance.md)
- [v1.2 项目入口](docs/v1.2-project-entry.md)
- [v1.2.1 部署入口](docs/v1.2.1-deployment-entry.md)
- [DeepSeek 只读 MVP](docs/deepseek-readonly-mvp.md)
- [DeepSeek 多节点工作流](docs/deepseek-readonly-multinode.md)
- [DeepSeek v0.7 Tool Binding 与预算](docs/deepseek-readonly-v0.7.md)
- [DeepSeek v0.8 签名信任](docs/deepseek-readonly-v0.8.md)
- [DeepSeek v0.9 完整软件包信任](docs/deepseek-readonly-v0.9.md)
- [DeepSeek v1.0 生产信任运行时](docs/deepseek-readonly-v1.0.md)

## 安全提醒

- 不要把 API Key、PIN、密码、Token 或私钥内容提交到仓库；
- 不要为了让测试通过而关闭签名、信任根或能力检查；
- 不要直接删除有审计价值的旧输出和运行目录，应先归档；
- SoftHSM 适合开发和参考验收，不能替代真实 HSM 厂商认证；
- `PASS` 只代表对应检查通过，不自动代表业务批准、组织合规或生产准入。
