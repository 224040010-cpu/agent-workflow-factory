# 架构说明

## 稳定边界

BPMN 是业务设计来源，工作流 IR 是稳定的可执行契约。Graph、Agent Profile、LoopSpec 和 Policy 清单都是派生产物。Harness Adapter 可以转换这些产物，但不能改变其语义或削弱策略。

## 跨仓流程

```text
skill-registory
  Registry YAML + 源规范
      → 准入与治理
      → 不可变 catalog.snapshot.json
                              |
                              v
agent-workflow-factory
  业务需求 → BPMN → IR → 解析能力目录
                         → registry.lock.json + signature
                         → package.manifest.json + signature
                         → graph + agents + loops + policy
                         → 适配器软件包
```

能力解析发生在编译和打包阶段。运行时节点不会查询 Registry 的 `main` 分支，也不会自动漂移到更新的资产版本。

## 可信事实

Graph 路由只使用由确定性校验器、状态提供方或人工决策产生的事实。模型输出首先是候选值，只有通过证据检查器确认后才能写入可信事实。

## 业务展示边界

自然语言解释先生成结构化业务需求和解释报告，再进入 BPMN 与 Graph 编译。面向业务人员的 SVG 必须从最终 Graph 和工作流 IR 派生，并与 `.bpmn` 中的 BPMN DI 共用确定性布局。展示层不能删减节点、分支或循环语义；所有自然语言推断在确认前都保持 `REVIEW_REQUIRED`。

## Agent 边界

BPMN 泳道只表示职责提示。本实现仅根据业务契约提供的明确 `agent_ref` 注解或已经批准的职责覆盖生成 Agent Profile。未来的职责划分器可以提出注解建议，但未经评审不能部署。

## 循环边界

持久循环必须定义意图、触发器、检查器、最大轮数、Token 预算、停止条件和升级策略。缺少这些字段的 BPMN 回边只是局部 Graph 环路，不能编译为持久化 LoopSpec。

## 运行时边界

适配器必须声明其能力，例如持久会话、仅追加事件、人工关卡、定时循环和沙箱限制。当适配器缺少必需能力时，打包必须失败；适配器不能静默忽略该能力。

v1.0 中，DeepSeek 执行还必须配置独立的运行签名 Provider。每个事件都在哈希链之外附加 Ed25519 发布者签名，每个检查点保存分离签名并引用已经签名的轨迹头。恢复和重放必须同时验证事件顺序、前序摘要、事件签名、检查点签名和轨迹头。

生产模式使用 SQLite 事件存储，通过写事务分配连续序号并原子追加事件；可选租约用于阻止多个 Worker 同时推进同一个运行。保留策略只能清理由 Runtime 标记为终止、已超过保留期且不存在有效租约的记录。JSONL 仅保留为本地开发和兼容后端。
