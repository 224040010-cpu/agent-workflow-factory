# DeepSeek Harness Desktop 与新版 SDK 升级方案

更新日期：2026-10-03

实施状态：v1.2.2 兼容迁移代码已落地，Windows 官方运行时与本地假模型合同测试通过；真实 API、WSL 与目标 HSM 尚待新版验收。具体迁移和实测发现见 [v1.2.2 指南](v1.2.2-sdk-migration.md)。下文“当前实现与差距”保留的是升级前基线，Desktop Plugin 仍属后续阶段。

## 结论

Agent Workflow Factory 不应把 DeepSeek Harness Desktop 当作新的生产执行 API，也不应通过自动点击桌面界面运行工作流。

推荐将官方 Harness 的三种形态分开使用：

- Python SDK 或 Headless：继续承担 Factory 的确定性、可测试、可审计执行；
- Desktop：承担业务演示、工作区浏览、插件安装、人工复核和运行材料查看；
- Factory：继续拥有 BPMN、Agent Graph、能力锁定、签名信任、运行状态和审计证据的权威定义。

本仓库当前最大的升级阻塞不是 Desktop UI，而是 Python SDK 启动契约已经变化。应先交付 `v1.2.2` SDK 兼容迁移，再在 `v1.3` 建设 Desktop Review Bridge。

## 官方版本现状

截至 2026-10-03：

- DeepSeek Harness 官方仓库仍标记为 Developer Preview，并明确提示可能出现不兼容变更；
- 最新官方预发布为 `v0.2.1-alpha.1`，最近候选版本为 `v0.2.0-rc.2`；
- PyPI 上 `deepseek-harness-sdk` 当前版本为 `0.1.5rc1`；
- 官方 Desktop 是仓库内的 Electron 应用，不应与 GitHub 上多个社区 Desktop Fork 混淆；
- Desktop 在签名资源中携带自己的生产运行时，并拥有保留的 `$DSH_HOME/profiles/desktop`；
- Desktop 与 CLI 可以共享产品数据，但执行包、插件启用状态和锁文件相互独立；公开 CLI 不能直接管理 Desktop Profile；
- Windows 和 macOS Desktop 可以通过菜单安装随应用携带的 `dsh` 命令，用于插件管理，无需用户另外安装 Node 或 pnpm；
- 新版 Python SDK 已支持 Linux x64/arm64、macOS arm64 和 Windows x64。

官方资料：

- [DeepSeek Harness Releases](https://github.com/deepseek-ai/deepseek-harness/releases)
- [官方架构说明](https://deepseek-harness.github.io/deepseek-harness/en/reference/)
- [Python SDK 指南](https://deepseek-harness.github.io/deepseek-harness/en/guide/python-sdk)
- [Desktop 实现说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md)
- [SDK Minimal Profile](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/sdk-minimal/README.md)

## 对 Factory 有价值的新能力

### 1. Desktop 可以成为业务评审入口

Desktop 已具备工作区、文件预览、Markdown 本地图片预览、插件管理、会话历史和运行过程展示。Factory 可以把 BPMN、SVG、Agent/Tool 摘要、READY/BLOCKED 结果和事件时间线导出到一个只读评审工作区，让业务人员在 Desktop 中查看，而无需直接阅读构建目录和 JSON。

### 2. 插件体系可以承载 Factory Review Plugin

官方的“一切皆插件”架构允许 Factory 提供独立的 `dsh-plugin` Bundle。插件只负责读取已签名的软件包与评审材料，不重新实现编译器，也不直接修改生产工作流。安装和启用仍由 Desktop 自己的插件管理器完成。

### 3. Auto Review 和人工审批可用于交互辅助

Desktop 的 Auto Review 与人工继续机制可以帮助用户理解工具操作风险，但它是 Harness 工具操作审批，不等于 BPMN 业务审批。Factory 的业务 Review Gate 必须继续生成独立、可签名、可审计的决定记录。

### 4. Headless 和 SDK 提供更清晰的自动化边界

新版 Harness 统一采用具名 Profile 启动。Python SDK 通过 JSON-RPC 驱动 `dsh --profile sdk` 或 `sdk-minimal`；Headless 适合一次性任务和 JSON 事件输出。Factory 应保留 Python SDK 作为主适配器，同时把 Headless 作为诊断和兼容性测试入口，而不是并行维护第二套生产语义。

## 当前实现与新版契约的差距

### SDK 版本已经落后

当前 `pyproject.toml` 与运行时代码固定 `deepseek-harness-sdk==0.1.1rc1`。新版为 `0.1.5rc1`，仍属于预发布版本，因此升级后仍应精确锁定，不建议使用宽松范围。

### 启动参数已经不兼容

当前代码传入：

- `session_root`；
- `cordis` 完整配置。

新版公开配置改为：

- `dsh_home`；
- `profile`；
- `patches`；
- 可选 `runtime_cwd`、超时和环境覆盖。

官方已经删除调用方直接提供完整 Cordis Tree 的私有启动方式，不提供兼容回退。因此当前真实 Live Test 在升级 SDK 后会在客户端初始化阶段失败。

### 运行平台判断已经过时

当前代码明确拒绝原生 Windows，仅允许 Linux/macOS。新版 SDK 已提供 Windows x64 Runtime Wheel，应将平台检测改成“操作系统、架构、Runtime Wheel 与目标 Profile 联合检查”，并新增 Windows Contract/Live Test。

### 只读组合需要重新建模为 Profile Patch

当前 `readonly.cordis.yml` 是一棵完整组合树。新版应基于官方 `sdk-minimal` Profile 使用受审查的 Overlay Patch，并至少完成以下限制：

- 禁用 `persistent-bash` 和 `persistent-pwsh`，防止模型获得 Shell；
- 禁用 `session-log-deepseek`，避免额外上传 Factory 的会话事件、路径和工具结果；
- 禁用 `plugin-package-inventory-deepseek`，避免向模型请求附加无关插件清单；
- 保留 JSON-RPC Server、DeepSeek Provider、严格系统提示与 JSONL Session；
- 对 Patch 内容、顺序、SDK 版本和 Profile 名称共同计算并验证部署摘要。

只隐藏工具描述不构成安全边界，必须在最终解析后的 Profile 中确认不存在模型可调用的 Shell、编辑器、文件系统或未批准 MCP Tool。

### Desktop Profile 不能被 Factory 直接接管

Factory 不能写入 `$DSH_HOME/profiles/desktop`，不能依赖 Electron 私有 Host 端口，也不能把 Desktop Lockfile 当作 Registry Lock。Desktop 自动更新也不能触发 Factory 生产运行时的静默升级。

## 推荐目标架构

```text
业务自然语言
    ↓
Agent Workflow Factory
    ├─ BPMN / SVG / Agent Graph / Registry Lock
    ├─ 签名软件包 / Runtime Policy
    └─ Desktop Review Export
             ↓
DeepSeek Harness Desktop + AWF Review Plugin
    ├─ 查看流程图、Agent、Tool、风险与事件
    ├─ 发起 review/test-run 只读检查
    └─ 提交人工评审决定
             ↓ 明确的签名接口
Agent Workflow Factory Review Gate
             ↓
Python SDK（sdk-minimal + reviewed patches）
             ↓
可信事件、检查点、重放与审计
```

Desktop 是展示与交互面，Factory Runtime 才是工作流状态与证据的权威来源。

## 分阶段开发计划

### v1.2.2：新版 SDK 兼容迁移

目标：不改变现有业务语义和信任模型，把真实执行迁移到新版官方 SDK。

交付内容：

1. 将可选依赖升级并精确固定到 `deepseek-harness-sdk==0.1.5rc1`；
2. 将 `DeepSeekHarnessSettings` 的 `session_root/cordis` 替换为 `dsh_home/profile/patches`；
3. 默认使用隔离的 `dsh_home` 与 `sdk-minimal`；
4. 把 `readonly.cordis.yml` 改造为只读 Overlay Patch；
5. 更新 Deployment Schema，同时保留对旧 `cordis` 字段的显式迁移错误提示；
6. 取消 Linux/macOS 硬编码，增加 Windows x64 预检；
7. 记录 SDK、Runtime、Profile、Patch 摘要和平台信息到运行起始事件；
8. 使用官方新版事件样本增加 Final Response、Finish Reason、Usage 和错误脱敏合同测试；
9. 分别执行 Windows、WSL 单节点、多节点、恢复、重放和篡改拒绝测试。

退出标准：相同业务输入在新版 SDK 上仍生成相同可信 Facts；模型没有 Shell/文件写入能力；未授权 Patch 或版本变化在模型调用前被拒绝；Windows 与 WSL 至少各有一条真实 Live Test 通过。

### v1.3：Desktop Review Bridge

目标：让业务人员通过 Desktop 查看和评审 Factory 产物，但不改变生产执行权威边界。

交付内容：

1. 新增 `workflowctl desktop-export <project>`；
2. 生成只读评审工作区，包括项目摘要、BPMN、SVG、Agent/Tool 清单、策略解释、READY/BLOCKED 和事件时间线；
3. 提供 `AWF Review Plugin`，只读取经过摘要校验的评审材料；
4. 允许在 Desktop 中触发 `review` 和 `test-run`，但禁止直接绕过 Deployment Preflight 启动生产运行；
5. 将人工意见输出为独立 `review-decision.json`，由 Factory 校验身份、版本和摘要后写入审计链；
6. 对 Desktop 未安装、插件未启用和版本不兼容提供清晰降级路径，CLI 仍可独立使用。

退出标准：业务人员可以在 Desktop 完成“查看流程图—理解 Agent/Tool—查看阻断原因—提交评审意见”，且 Desktop 关闭、升级或损坏不会影响已签名工作流的执行与重放。

### v1.4：受治理的人机协同

目标：在 Desktop 或后续 Web 控制面中处理真实 Human Gate。

交付内容：

- Human Task Provider 与任务收件箱；
- 业务审批、工具操作审批和生产发布审批三类决定分离；
- 审批身份、流程版本、节点、决定、时间与证据摘要签名；
- 超时、转交、代理、撤回和升级；
- Desktop 作为可选交互客户端，Factory API 作为权威写入边界。

## 不建议采用的方案

- 不通过 UI 自动化点击 Desktop 来运行生产工作流；
- 不直接读取或修改 Desktop 私有 Profile 和 Lockfile；
- 不让 Desktop 自动更新隐式改变 Factory 的 SDK 版本；
- 不复用同一个 `DSH_HOME` 同时承载 Desktop 与受治理 SDK 运行；
- 不把 Harness Auto Review 当作业务审批或合规签字；
- 不直接启用 `sdk-minimal` 默认 Shell，因为其默认策略为 `danger-full-access`；
- 不默认上传完整 Harness Session Log；Factory 已拥有独立、签名的事件轨迹。

## 建议的下一步

本轮只实现 `v1.2.2`。Windows 官方运行时合同测试已执行，Windows/Linux CI 矩阵已配置但尚未在云端运行；下一步是在用户 WSL 环境完成新版真实 API 验收。该兼容层通过目标环境验收后，再开始 Desktop 导出、Plugin 与人工评审交互，避免同时引入 SDK 破坏性升级和新 UI 两类变量。
