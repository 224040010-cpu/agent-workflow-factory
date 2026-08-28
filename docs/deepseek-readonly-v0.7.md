# DeepSeek Harness 只读 Graph v0.7：Tool Binding 与预算治理

## 评审目标

v0.7 不扩大 DeepSeek Harness 的权限边界，仍只执行固定版本、只读、幂等且无需审批的 Tool。本版本补齐两类生产前必须具备的治理能力：

- 宿主 Tool Binding 的输入/输出 JSON Schema 与实现摘要；
- Agent Profile 中模型回合、Token、Tool 调用预算的运行时强制执行。

双仓共享的 `contracts/system-definition.json` 保持 3.0.0 和原始摘要不变。上述能力属于工作流工厂的 Provider Adapter 与 Runtime 实现，不回写 Registry 的权威资产定义。

## 执行链路

```text
registry.lock Tool 描述
  → 只读/幂等/审批/资产摘要校验
  → 宿主 Binding 实现摘要校验
  → Tool 输入 Schema 校验
  → Tool 调用预算预检并记账
  → 执行确定性宿主 Tool
  → Tool 输出 Schema 校验
  → 模型回合预算预检并记账
  → DeepSeek Harness 复核可信观察
  → 记录官方 usage 事件中的 Token
  → Token 超限检查
  → facts/evidence 完全一致校验
  → 完成节点并按可信 facts 路由
```

任何一步失败，后续步骤都不会执行。尤其是：

- 输入 Schema、Binding 摘要或 Tool 调用预算不合格时，不调用 Tool 和模型；
- Tool 输出 Schema 不合格时，不调用模型；
- Token 用量超限时，真实消费仍会入账，但模型 facts 不会提交到工作流状态；
- 预算耗尽写入 `budget.exhausted`，运行状态变为 `escalated`，不能被普通自动恢复绕过。

## Tool Binding 契约

运行时 Binding 包含：

- Registry endpoint；
- 宿主实现标识；
- 输入 JSON Schema；
- 输出 JSON Schema；
- Python handler；
- 经过评审的实现摘要。

实现摘要的材料是 endpoint、实现标识、输入 Schema、输出 Schema 和 handler 源码 SHA-256 的规范 JSON。已评审值保存在 [`../adapters/deepseek-harness/readonly-tool-bindings.json`](../adapters/deepseek-harness/readonly-tool-bindings.json)，运行时会重新计算并比较。

这能发现实现、Schema 或 endpoint 的意外漂移，但不是发布者签名。若要防御攻击者同时篡改代码和摘要清单，下一阶段仍需引入签名产物、受保护发布流程和验证公钥。

当前两个 Binding：

- `parse-business-intent@1.0.0`：要求 `facts.business.description` 为非空字符串；只允许输出 `facts.intent.parsed=true` 与证据数组。
- `detect-description-ambiguity@1.0.0`：额外要求 `facts.intent.parsed=true`；只允许输出完整的歧义分析事实与证据数组。

## 预算模型

预算上限来自每个编译后 Agent Profile 的 `spec.budgets`：

- `max_turns`：该 Agent 发起的 Harness 模型回合尝试数；
- `max_tokens`：该 Agent 的累计受治理 Token；
- `max_tool_calls`：该 Agent 的宿主 Tool 调用尝试数。

检查点中的 `budget_usage` 同时保留全运行、按 Agent、按节点三个视角。每次消费都会生成 `budget.consumed` 事件并立即写检查点，因此进程中断后不会丢失已经发生的尝试。

Token 读取遵循 DeepSeek Harness 官方事件契约：优先使用同一 turn/step 的 `assistant/chunk` usage，缺失时回退到 `assistant/message.usage`。计量公式为：

```text
governed_tokens = inputTokens + outputTokens + cacheReadTokens + cacheWriteTokens
```

`reasoningTokens` 已包含在 `outputTokens` 中，只单独记录供审计，不重复相加。参考：[Session 类型](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/session/src/types.ts)、[LLM Streaming](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/llm-streaming.md)、[Session 子系统](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/session.md)。

## 轨迹事件

v0.7 新增或扩展的关键事件：

- `budget.consumed`：阶段、增量、Agent 累计、上限和超限结果；
- `budget.exhausted`：超限字段、尝试增量和 `action=escalate`；
- `tool.observation.accepted`：同时记录 Tool 输出摘要和 Binding 实现摘要；
- `agent.turn.completed`：记录 Harness 事件类型和五类 Token 用量。

所有事件继续写入原有 SHA-256 哈希链，预算状态随 `state.checkpointed` 参与重放。

## 组件与综合验收

Windows 本地契约测试：

```powershell
python scripts/run_deepseek_mvp.py
python scripts/run_deepseek_multinode_mvp.py
python -m unittest discover -s tests -v
```

WSL2 真实双节点验收：

```bash
cd ~/src/agent-workflow-factory
source .venv/bin/activate
git pull
DSH_MULTINODE_LIVE_TEST=1 python -m unittest discover -s tests -p 'test_deepseek_harness.py' -v
```

验收重点：

- 正常双节点路径完成并可重放；
- Binding 清单与运行时重新计算摘要一致；
- 非法输入、非法输出、实现漂移均在模型调用前失败；
- Tool、模型回合和 Token 都写入检查点；
- 缓存 Token 被计入，reasoning Token 不重复计数；
- 超限进入 `escalated`，可信 facts 不提交。

## v0.8 完成情况

- Binding 清单和 Registry lock 的 Ed25519 发布者签名与验证公钥已完成，详见 [`deepseek-readonly-v0.8.md`](deepseek-readonly-v0.8.md)；
- 将预算上限下推为 Provider 的单回合输出限制，减少事后 Token 超限；
- 增加人工审批 Provider Binding，保持定时循环仍默认关闭；
- 将 JSONL 事件存储替换为具备并发控制和不可篡改保留策略的生产存储。
