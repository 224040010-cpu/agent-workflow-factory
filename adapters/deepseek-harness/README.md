# DeepSeek Harness 适配器边界

该实验性适配器负责把与提供方无关的软件包映射到 DeepSeek Harness。此目录不会复制 Harness 核心代码，也不会向工作流 IR 暴露 DeepSeek 专属类型。

## 已实现的只读 MVP

- 目标版本固定为 `v0.1.1-rc.1` / `deepseek-harness-sdk==0.1.1rc1`；
- 调用官方 `DeepSeekHarness.run(input, session_id=...)` 接口；
- Agent Profile 映射为受限提示、预算和权限闸门；
- 只允许 lockfile 中 `read/none`、无需审批且幂等的 Tool；
- Registry Tool 由宿主执行，模型只能复核证据；
- Harness 会话摘要映射到本仓库的追加式哈希轨迹；
- 支持执行中断后的检查点恢复和相同会话复用；
- 能力不足时在执行前拒绝。

`readonly.cordis.yml` 特意不加载 Bash、编辑器、文件系统、Skill 或其他模型侧 Tool。完整设计、运行命令与测试结论见 [`../../docs/deepseek-readonly-mvp.md`](../../docs/deepseek-readonly-mvp.md)。

## 尚未开放

人工审批、定时循环、写操作 Tool、非幂等 Tool 和无证据的模型事实都不在本 MVP 范围内。

DeepSeek Harness 目前属于开发者预览功能。兼容性变化必须封装在本目录内；除非经过单独的架构决策，否则不得修改工作流 IR 或双仓共享总定义。
