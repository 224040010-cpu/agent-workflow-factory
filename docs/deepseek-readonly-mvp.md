# DeepSeek Harness 只读端到端执行 MVP

## 评审结论

该 MVP 已把“编译后的 Graph”连接到 DeepSeek Harness 官方 Python SDK 的同步会话接口，完成以下闭环：

```text
已编译软件包
  → 运行时能力协商
  → Graph 选出动作节点
  → registry.lock.json 定位固定版本 Tool
  → 宿主只读 Tool 执行并生成可信证据
  → DeepSeek Harness Agent 复核证据并返回严格 JSON
  → facts 一致性与 completion_evidence 双重验证
  → Graph 继续路由
  → 终态 + 哈希链轨迹回放 PASS
```

它是可运行的只读纵向切片，不是生产级调度器。真实模型调用通过当前可安装并已固定的 `deepseek-harness-sdk==0.1.1rc1` 完成；不具备 SDK、凭证或官方支持平台时，使用与官方 `run(input, session_id=...)` 返回结构一致的契约替身进行自动化测试。

## 信任边界

模型不能直接写入可信 facts。宿主首先根据 `registry.lock.json` 选择 Tool，并依次验证：

1. 资产类型必须是 `tool`，且摘要已固定；
2. `side_effects` 只能是 `read` 或 `none`；
3. `requires_approval` 必须为 `false`；
4. `idempotent` 必须为 `true`，以支持中断重试；
5. Agent Profile 必须声明 `external_write=deny` 且绑定该 Tool；
6. DeepSeek 输出必须是仅包含 `status`、`facts`、`evidence` 的 JSON 对象；
7. 模型返回的 facts 必须与宿主 Tool 事实完全一致；
8. 合并后必须满足节点全部 `completion_evidence`。

任何一项失败都会在完成节点前中断并暂停运行。恢复时复用相同的 Harness `session_id` 和幂等键。

## 为什么不让模型直接调用 Tool

官方最小示例为模型配置了 Bash 和编辑器，并使用 `danger-full-access`。这不符合只读 MVP 的目标。本仓库的 `readonly.cordis.yml` 不加载 Bash、编辑器、文件系统、Skill 或其他模型侧 Tool。Registry Tool 在宿主侧执行，模型只负责证据复核。

适配器启动时会同时校验 SDK 精确版本和这份 Cordis 文件的 SHA-256；修改组合或换用其他版本必须重新评审，不能以“仍然只读”为前提继续运行。

这样可证明真实 Harness Agent、真实模型回合、Graph 路由、Registry 固定资产和可信事实边界已经连通，同时不会把开发者预览阶段的插件权限直接扩展到业务环境。

## 能力协商

当前适配器声明支持：

- 持久会话；
- 仅追加事件映射；
- 受控网络边界（模型只访问指定提供方，模型侧无通用网络工具）；
- 只读、固定版本、幂等的宿主 Tool。

当前明确拒绝：

- 人工审批节点；
- 定时循环；
- 写操作或需要审批的 Tool；
- 非幂等 Tool；
- 无 Agent Profile 或无 Tool 的动作节点；
- 模型自行生成且缺少 Tool 证据的事实。

因此，含人工关卡或调度循环的软件包会在启动任何 Agent 前被拒绝，不会静默降级。

## 本地契约验收

Windows、Linux 和 macOS 均可运行不联网的契约验收：

```bash
python scripts/run_deepseek_mvp.py
python -m unittest discover -s tests -v
```

验收脚本会重新生成 BPMN、编译示例、执行一个固定版本的 `parse-business-intent` 只读 Tool、调用 SDK 形状一致的 Agent 客户端、完成 Graph，并验证事件哈希链。

## 真实 DeepSeek Harness 冒烟

官方打包的 Python runtime 当前支持 Linux x64/arm64 和 macOS arm64，不支持原生 Windows。Windows 开发机应在 WSL2/Linux 中执行：

```bash
python -m pip install -e '.[deepseek]'
export DEEPSEEK_API_KEY='...'

python scripts/workflowctl.py generate-bpmn \
  examples/deepseek-readonly/business-requirement.json \
  --output build/deepseek-live/process.bpmn

python scripts/workflowctl.py compile \
  build/deepseek-live/process.bpmn \
  --business examples/deepseek-readonly/business-requirement.json \
  --catalog fixtures/catalog.snapshot.json \
  --output build/deepseek-live/package

python scripts/workflowctl.py run \
  build/deepseek-live/package \
  --adapter deepseek \
  --runtime-dir build/deepseek-live/runtime \
  --facts examples/deepseek-readonly/initial-facts.json
```

也可设置 `DSH_LIVE_TEST=1` 后运行测试套件，启用真实 SDK 冒烟。API Key 只从环境读取，不进入命令行、工作流软件包或轨迹事件。

## 已验证场景

- 只读 Tool → Harness Agent → 可信 facts → Graph 终态；
- 模型 facts 与 Tool facts 不一致时拒绝；
- lockfile 中 Tool 被改为写操作时，在模型调用前拒绝；
- Harness 回合中断后运行暂停，再以同一会话恢复；
- 人工关卡和定时循环能力缺失时启动失败；
- 最终轨迹哈希链与检查点回放通过；
- 运行时仅加载软件包内 lockfile，不访问 Registry 主分支。

## 下一阶段

MVP 后续应按顺序推进：

1. 为更多 Registry Tool 建立独立、可签名的宿主绑定包；
2. 将 Tool 输入/输出升级为按 Tool Schema 校验，而不是仅校验通用证据信封；
3. 增加 Linux CI 的真实 SDK 夜间冒烟与兼容性矩阵；
4. 实现人工审批 Provider 后再开放 `human_gate`；
5. 实现带租约的生产事件存储和调度器后再开放 looping engineering；
6. DeepSeek Harness 退出开发者预览后重新评审版本固定策略。

## 官方依据

- [DeepSeek Harness 官方仓库](https://github.com/deepseek-ai/deepseek-harness)
- [官方 Python SDK 指南](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/python-sdk.md)
- [官方 Python SDK API](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/src/deepseek_harness/api.py)
- [官方 SDK 发布包](https://pypi.org/project/deepseek-harness-sdk/)
- [官方最小 Cordis 组合](https://github.com/deepseek-ai/deepseek-harness/blob/master/examples/jsonrpc-agent/minimal.cordis.yml)
