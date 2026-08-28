# DeepSeek Harness 多节点只读 Graph MVP

## 目标

该阶段把单节点纵向切片升级为两个真实 Agent、两个固定版本只读 Tool 和一个可信事实网关：

```text
业务描述
  → intent-parser-agent
  → parse-business-intent@1.0.0
  → facts.intent.parsed=true
  → ambiguity-review-agent
  → detect-description-ambiguity@1.0.0
  → facts.analysis.ambiguous
  → BPMN 排他网关
      ├─ false → ready
      └─ true  → needs-clarification
```

模型仍然不能直接控制路由。每个 Agent 只能复核其节点对应宿主 Tool 的证据，模型 facts 必须与 Tool facts 完全一致，随后参考运行时才计算 BPMN/Graph 条件。

## 第二个只读 Tool

`detect-description-ambiguity@1.0.0` 来自 `registry.lock.json`，必须满足：

- `status=approved`；
- `side_effects=read`；
- `requires_approval=false`；
- `idempotent=true`；
- 具有固定 SHA-256 摘要。

当前宿主绑定实现 `ambiguity-markers/v1` 确定性规则，检查“尽快、适当、必要时、酌情、相关人员、视情况、若干”等模糊表述，并返回：

```json
{
  "analysis": {
    "ambiguity_checked": true,
    "ambiguous": true,
    "ambiguity_terms": ["尽快", "适当"]
  }
}
```

这是 MVP 规则，不等同于完整自然语言歧义理解；后续可以把规则引擎替换为独立 Tool 服务，但 Tool 名称、版本、Schema、风险与摘要仍由 Registry 管理。

## 分支语义

工作流只允许以下事实表达式决定终点：

```text
facts.analysis.ambiguous == false → ready
facts.analysis.ambiguous == true  → needs-clarification
```

模型文本、置信度描述或自由格式结论都不会直接参与选择边。

## 契约验收

```bash
python scripts/run_deepseek_multinode_mvp.py
```

脚本同时执行两条路径：

- 清晰描述：2 个 Agent、2 个 Tool、2 个动作完成，路由到 `ready`；
- 模糊描述：识别模糊词并路由到 `needs-clarification`。

两条路径都必须返回 `result=PASS`、`replay=PASS`。v0.7 起轨迹还包含逐阶段预算事件，因此不再把固定事件条数作为兼容契约。

组件测试还验证：

- 两个 Agent Profile 和两个 Tool lock 均被生成；
- 第二个 Agent 中断时，第一个节点不会重复执行；
- 恢复后复用第二个节点原有会话 ID；
- 两条分支均由宿主可信事实选择；
- 最终轨迹可以完整重放。

## 真实 Harness 验收

在 Linux/WSL2、SDK 与 `DEEPSEEK_API_KEY` 已配置的环境中运行：

```bash
DSH_MULTINODE_LIVE_TEST=1 \
python -m unittest discover -s tests -p 'test_deepseek_harness.py' -v
```

该开关会额外发起两个真实模型回合，因此不会在普通 CI 或单节点冒烟中自动开启。

也可手工执行：

```bash
python scripts/workflowctl.py generate-bpmn \
  examples/deepseek-readonly-multinode/business-requirement.json \
  --output build/deepseek-multinode-live/process.bpmn

python scripts/workflowctl.py compile \
  build/deepseek-multinode-live/process.bpmn \
  --business examples/deepseek-readonly-multinode/business-requirement.json \
  --catalog fixtures/catalog.snapshot.json \
  --output build/deepseek-multinode-live/package

python scripts/workflowctl.py run \
  build/deepseek-multinode-live/package \
  --adapter deepseek \
  --runtime-dir build/deepseek-multinode-live/runtime \
  --run-id run-deepseek-multinode-live-001 \
  --facts examples/deepseek-readonly-multinode/initial-facts.json
```

## 下一阶段边界

Tool 输入/输出 JSON Schema、预算计量和宿主实现摘要已在 v0.7 完成；Binding 与 Registry lock 的 Ed25519 发布者签名已在 v0.8 完成，详见 [`deepseek-readonly-v0.8.md`](deepseek-readonly-v0.8.md)。下一步应接入 KMS/HSM、整个软件包签名，并继续保持人工审批和定时循环关闭，直到对应 Provider 与生产事件存储完成。
