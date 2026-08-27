# DeepSeek Harness 适配器边界

该实验性适配器负责把与提供方无关的软件包映射为 DeepSeek Harness 的插件和预设。此目录不会复制 Harness 核心代码，也不会向工作流 IR 暴露 DeepSeek 专属类型。

实现部署绑定前，需要：

1. 固定一个已经测试的 DeepSeek Harness 版本；
2. 将 Agent Profile 映射为提供方和预设配置；
3. 将 Registry 中已解析的资产映射为 Skill 和 Tool 插件；
4. 将 LoopSpec 映射为循环和调度器插件；
5. 将 Harness 会话事件映射为通用的仅追加轨迹 Schema；
6. 执行能力协商，并拒绝缺少必需能力的部署；
7. 为启动、恢复、中断、循环停止和人工审批增加适配器契约测试。

DeepSeek Harness 目前属于开发者预览功能。兼容性变化必须封装在本目录内；除非经过单独的架构决策，否则不得修改工作流 IR 或双仓共享总定义。
