# DeepSeek Harness 适配器边界

该实验性适配器负责把与提供方无关的软件包映射到 DeepSeek Harness。此目录不会复制 Harness 核心代码，也不会向工作流 IR 暴露 DeepSeek 专属类型。

## 已实现的只读 MVP

- SDK 与 Runtime 固定为 `0.1.5rc1`，对应源码标签 `dsh-v0.1.5-rc.1`；
- 调用官方 `DeepSeekHarness.run(input, session_id=...)` 接口；
- Agent Profile 映射为受限提示、预算和权限闸门；
- 只允许 lockfile 中 `read/none`、无需审批且幂等的 Tool；
- Registry Tool 由宿主执行，模型只能复核证据；
- Harness 会话摘要映射到本仓库的追加式哈希轨迹；
- 支持执行中断后的 Factory 检查点恢复；同进程复用会话，跨进程创建新会话并重传可信 Facts；
- 能力不足时在执行前拒绝。
- 支持两个 Agent、两个只读 Tool 和可信 facts 排他网关的多节点 Graph。

当前入口使用 `readonly.patch.yml` 覆盖官方 `sdk-minimal`，禁用模型侧 Bash/Pwsh 与终端插件，关闭会话日志上传和插件清单附加。模型没有可调用 Tool；业务 Tool 仍由 Factory 宿主执行。SDK 原生日志保留在隔离 Home，签名审计轨迹仍由 Factory 维护。

`readonly.cordis.yml` 仅保留作旧版历史参考，不再用于启动。升级和兼容性验收见 [v1.2.2 指南](../../docs/v1.2.2-sdk-migration.md)。原有单节点和多节点文档属于历史设计，涉及启动方式时以新指南为准。

Patch 不是操作系统沙箱。Home、SDK 安装目录和宿主 Tool 实现须由可信部署者维护；不能据此宣称限制了本机所有进程的文件或网络访问。Desktop、外部插件、文件编辑和 MCP 都不在本次只读组合内。

## 尚未开放

人工审批、定时循环、写操作 Tool、非幂等 Tool 和无证据的模型事实都不在本 MVP 范围内。

DeepSeek Harness 目前属于开发者预览功能。兼容性变化必须封装在本目录内；除非经过单独的架构决策，否则不得修改工作流 IR 或双仓共享总定义。
