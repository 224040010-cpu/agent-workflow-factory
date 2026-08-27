# 参考运行时

参考运行时用于证明编译后的软件包能够驱动真实且可恢复的执行。它有意保持小型化并与模型提供方解耦；生产级调度器和模型适配器可以替换它，同时保留相同的状态及事件语义。

## 执行边界

运行时只加载已经编译的软件包，在运行期间不会从持续变化的 Registry 分支发现能力。执行开始前，`registry.lock.json` 已固定能力目录及所有资产版本。

路由表达式使用受限的事实语言。操作数支持布尔值、`null`、数字和带引号的字符串；比较运算支持 `==`、`!=`、`>`、`>=`、`<` 和 `<=`，并可使用 `and` 或 `or` 连接。该语言不支持执行任意代码。

Adapter 或人工关卡通过提交结构化事实更新来完成动作节点。运行时合并候选事实并计算每一条 `completion_evidence` 表达式；任何表达式为假时都将拒绝节点完成。选择节点和终止节点由运行时自动处理，不能手动完成。

## 持久状态

每次状态变化都会产生：

1. 一条领域事件，例如 `node.completed` 或 `route.selected`；
2. 一条包含结果状态的 `state.checkpointed` 事件；
3. 一个位于 `runtime/checkpoints/<run-id>.json` 的逻辑检查点。

事件以追加方式写入 `runtime/events/<run-id>.jsonl`。每条事件都包含 `seq`、`prev_hash` 和 `event_hash`，共同形成 SHA-256 哈希链。重放会验证事件序号、前后链接、事件哈希，以及最后一份事件状态与检查点文件是否一致。

该机制可以发现意外修改或未授权篡改，但不能替代带签名的事件或生产级不可变存储。

## 状态机

```text
运行中 → 等待动作 → 运行中 → 已完成
  │          │
  ├─ 已暂停 ─┘（恢复后回到暂停前的准确状态）
  ├─ 等待事实
  └─ 已升级（循环预算耗尽）
```

当路由再次指向已经完成的节点时，运行时计为一轮循环。循环轮数超过 `max_rounds` 后停止路由，并生成 `loop.budget_exhausted` 事件，其中包含已配置的升级负责人。

## 命令示例

首先使用 `python scripts/run_example.py` 编译示例，然后执行完整的运行时演示：

```bash
python scripts/run_runtime_example.py
```

集成测试可以使用 `scripts/workflowctl.py` 提供的 `runtime-start`、`runtime-route`、`runtime-complete`、`runtime-pause`、`runtime-resume` 和 `runtime-replay` 命令。事实更新通过 JSON 文件提交，确保自由文本模型输出不会直接进入可信路由边界。
