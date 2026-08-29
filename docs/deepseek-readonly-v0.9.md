# DeepSeek Harness 只读 Graph v0.9：全包签名与离线根信任

## 评审结论

v0.9 在 v0.8 的 Binding/Registry 双签名之外增加两层完整性边界：

- 对整个工作流软件包建立并签署统一 manifest；
- 使用独立离线根密钥签署运行端信任库。

DeepSeek Runner 现在按以下顺序验证，任何一步失败都不会创建 Harness 会话：

```text
固定的根公钥
  → 验证 trusted-publishers.json 根签名
  → 验证 Tool Binding 清单签名
  → 验证 registry.lock.json 签名
  → 验证 package.manifest.json 签名
  → 核对包内每个文件的路径、大小、摘要和文件集合
  → 核对签名 Binding 与真实宿主实现
  → Tool / Agent / Graph 执行
```

共享总定义仍保持 3.0.0，不因运行时信任能力升级而改变。

## 全包 Manifest

签名编译会在软件包中新增：

- `process.bpmn`：编译输入 BPMN 的包内副本；
- `package.manifest.json`：完整产物清单；
- `package.manifest.sig.json`：构建发布者的 Ed25519 分离签名。

清单覆盖：

- BPMN；
- Workflow IR；
- Agent Graph；
- 所有 Agent Profile；
- Runtime Policy；
- LoopSpec（存在时）；
- Registry lock 及其签名；
- Compile Report。

每项包含 POSIX 相对路径、SHA-256、字节大小和媒体类型。Manifest 与自身签名不列入清单，以避免循环摘要。

验证器比较“已声明路径集合”和“磁盘实际路径集合”，因此会拒绝：

- 文件内容修改；
- 已声明文件被删除；
- 未签名文件被注入；
- 重复路径；
- `../` 或绝对路径；
- 符号链接；
- Manifest Schema 或 package format 降级；
- BPMN/JSON 媒体类型不匹配。

公开 Schema：[`../schemas/package-manifest.schema.json`](../schemas/package-manifest.schema.json)。

## 离线根信任

信任库不再被直接相信。运行端必须配置：

- `trusted-publishers.json`；
- `trusted-publishers.sig.json`；
- `root-public-key.json`。

根私钥仅用于信任库发布或轮换，不参与日常工作流构建。仓库只保存根公钥和已签名的默认信任库；仓库默认根私钥未被提交。根公钥是部署时的信任锚，其替换必须通过独立运维流程完成。

公开 Schema：[`../schemas/root-public-key.schema.json`](../schemas/root-public-key.schema.json)。

## 从 v0.8 本机配置升级

你已有的构建私钥和本机信任库可以继续使用。只需创建本机离线根，并签署现有信任库：

```bash
cd ~/src/agent-workflow-factory
source .venv/bin/activate
git pull
python -m pip install -e '.[deepseek]'

python scripts/workflowctl.py keygen-root \
  --private-key ~/.config/agent-workflow-factory/trust-root.pem \
  --public-key ~/.config/agent-workflow-factory/trust-root-public.json

python scripts/workflowctl.py sign-artifact \
  ~/.config/agent-workflow-factory/trusted-publishers.json \
  --private-key ~/.config/agent-workflow-factory/trust-root.pem \
  --output ~/.config/agent-workflow-factory/trusted-publishers.sig.json \
  --publisher agent-workflow-factory-trust-root

python scripts/workflowctl.py verify-trust \
  --trust-store ~/.config/agent-workflow-factory/trusted-publishers.json \
  --signature ~/.config/agent-workflow-factory/trusted-publishers.sig.json \
  --root-public-key ~/.config/agent-workflow-factory/trust-root-public.json
```

根私钥与构建私钥都禁止提交 Git。生产环境应把根私钥离线保存，并限制为极少数信任库发布操作。

## 重新编译与强制校验

v0.8 包没有全包 manifest，必须重新编译：

```bash
python scripts/workflowctl.py compile \
  build/deepseek-multinode-live/process.bpmn \
  --business examples/deepseek-readonly-multinode/business-requirement.json \
  --catalog fixtures/catalog.snapshot.json \
  --output build/deepseek-multinode-live/package \
  --signing-key ~/.config/agent-workflow-factory/build-signing-key.pem
```

强制验证全部三层签名和根信任：

```bash
python scripts/workflowctl.py validate \
  build/deepseek-multinode-live/package \
  --trust-store ~/.config/agent-workflow-factory/trusted-publishers.json \
  --trust-store-signature ~/.config/agent-workflow-factory/trusted-publishers.sig.json \
  --trust-root-public-key ~/.config/agent-workflow-factory/trust-root-public.json \
  --require-trust-root \
  --require-registry-signature \
  --require-package-signature
```

真实执行：

```bash
python scripts/workflowctl.py run \
  build/deepseek-multinode-live/package \
  --adapter deepseek \
  --runtime-dir build/deepseek-multinode-live/runtime-v09 \
  --run-id run-deepseek-v09-001 \
  --facts examples/deepseek-readonly-multinode/initial-facts.json \
  --trust-store ~/.config/agent-workflow-factory/trusted-publishers.json \
  --trust-store-signature ~/.config/agent-workflow-factory/trusted-publishers.sig.json \
  --trust-root-public-key ~/.config/agent-workflow-factory/trust-root-public.json
```

## Signing Provider 接口

编译器不再依赖“私钥一定来自 PEM 文件”的假设。`SigningProvider` 只要求：

- 返回稳定的 `key_id`；
- 对输入字节执行签名。

当前 `FileEd25519SigningProvider` 是默认实现。云 KMS、HSM 或 Sigstore 驱动可实现相同接口并通过 `compile_package(signing_provider=...)` 注入，无需修改 Manifest 或验证契约。

v0.9 没有内置任何云厂商凭据或网络驱动，这属于安全边界设计完成、具体 Provider 待后续选择。

## 轨迹与测试

`artifact.signatures.accepted` 现在包含四项：

- 根签名的信任库；
- Tool Binding 清单；
- Registry lock；
- 完整软件包 Manifest。

自动化测试覆盖正常签署、文件修改、删除、注入、路径穿越、版本降级、信任库篡改、密钥吊销、多节点执行和恢复重放。

## v1.0 演进状态

PKCS#11 HSM Provider、运行轨迹与检查点签名、SQLite 事务事件存储、租约和保留策略已经在 v1.0 落地，参见 [`deepseek-readonly-v1.0.md`](deepseek-readonly-v1.0.md)。真实 DeepSeek SDK、真实 HSM 和灾难恢复演练仍通过环境开关在受控 Linux 环境中执行。
