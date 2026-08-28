# DeepSeek Harness 只读 Graph v0.8：发布者签名与信任策略

## 评审结论

v0.8 将 v0.7 的“内容摘要”升级为“发布者可验证签名”。DeepSeek Runner 在调用宿主 Tool 或模型之前，必须同时验证：

- `readonly-tool-bindings.json` 由 Adapter 维护者签署；
- 工作流包中的 `registry.lock.json` 由构建发布者签署；
- 两个签名使用的公钥均存在于运行端信任库，且状态不是 `revoked`；
- 已签署 Binding endpoint/实现摘要与实际运行时代码完全一致。

共享总定义 `contracts/system-definition.json` 仍保持 3.0.0；签名属于工作流工厂的构建、发布和运行信任层。

## 加密与信封契约

签名算法使用 Ed25519。实现采用 `cryptography` 官方 API 的 `Ed25519PrivateKey.sign` 和 `Ed25519PublicKey.verify`。Ed25519 公钥是 32 字节，签名是 64 字节。

分离签名文件只保存签名声明和签名值，不复制原始产物：

```json
{
  "statement": {
    "schema_version": "1.0.0",
    "subject": {
      "name": "registry.lock.json",
      "digest": "sha256:<文件字节摘要>",
      "media_type": "application/json"
    },
    "publisher": "agent-workflow-factory-build",
    "key_id": "sha256:<公钥摘要>",
    "algorithm": "Ed25519",
    "canonicalization": "AWF-CANONICAL-JSON-v1",
    "issued_at": "<UTC 时间>"
  },
  "signature": "<Base64 Ed25519 signature>"
}
```

签名覆盖按键排序、无多余空白、UTF-8 编码的声明。目标文件按原始字节计算 SHA-256，所以重新格式化 JSON 也会使签名失效。

公开契约：

- [`../schemas/artifact-signature.schema.json`](../schemas/artifact-signature.schema.json)
- [`../schemas/trust-store.schema.json`](../schemas/trust-store.schema.json)
- [`../trust/trusted-publishers.json`](../trust/trusted-publishers.json)

## 信任库与密钥状态

每条公钥包含 `key_id`、发布者、算法、状态和 Base64 原始公钥。

- `active`：可验证现有签名，也可作为当前发布密钥；
- `retired`：仍允许验证历史签名，不应用于新发布；
- `revoked`：所有签名立即拒绝。

运行时要求签名声明的发布者与信任库中的公钥所有者一致。未知 key ID、重复 key ID、错误发布者、错误算法、损坏公钥或吊销密钥都会失败。

仓库内 Binding 签名由一次性离线评审密钥生成，私钥从未提交到仓库。未来修改 Binding 清单时，必须添加新公钥、使用新私钥重签，并按轮换策略将旧密钥标为 `retired`；如果旧密钥存在泄露风险，应标为 `revoked`。

## 本地配置

安装 v0.8 依赖：

```bash
cd ~/src/agent-workflow-factory
source .venv/bin/activate
git pull
python -m pip install -e '.[deepseek]'
```

在仓库外准备本机信任库和构建私钥：

```bash
mkdir -p ~/.config/agent-workflow-factory
cp trust/trusted-publishers.json \
  ~/.config/agent-workflow-factory/trusted-publishers.json

python scripts/workflowctl.py keygen \
  --private-key ~/.config/agent-workflow-factory/build-signing-key.pem \
  --trust-store ~/.config/agent-workflow-factory/trusted-publishers.json \
  --publisher agent-workflow-factory-build
```

`keygen` 拒绝覆盖已有私钥，并在支持的系统上设置为仅当前用户可读写。当前 MVP 使用未加密 PKCS#8 文件，因此必须依赖操作系统权限或外部密钥管理系统保护；禁止提交到 Git。

## 签署、校验与运行

重新编译并签署 `registry.lock.json`：

```bash
python scripts/workflowctl.py compile \
  build/deepseek-multinode-live/process.bpmn \
  --business examples/deepseek-readonly-multinode/business-requirement.json \
  --catalog fixtures/catalog.snapshot.json \
  --output build/deepseek-multinode-live/package \
  --signing-key ~/.config/agent-workflow-factory/build-signing-key.pem
```

强制验证签名：

```bash
python scripts/workflowctl.py validate \
  build/deepseek-multinode-live/package \
  --trust-store ~/.config/agent-workflow-factory/trusted-publishers.json \
  --require-registry-signature
```

执行真实双节点 Graph：

```bash
python scripts/workflowctl.py run \
  build/deepseek-multinode-live/package \
  --adapter deepseek \
  --runtime-dir build/deepseek-multinode-live/runtime-v08 \
  --run-id run-deepseek-v08-001 \
  --facts examples/deepseek-readonly-multinode/initial-facts.json \
  --trust-store ~/.config/agent-workflow-factory/trusted-publishers.json
```

真实测试会在临时目录生成测试专用构建密钥，不会覆盖你的本机密钥：

```bash
DSH_MULTINODE_LIVE_TEST=1 \
python -m unittest discover -s tests -p 'test_deepseek_harness.py' -v
```

## 失败语义与轨迹

签名校验发生在能力协商、Tool 调用、预算消费和 Harness 会话之前。失败时不创建运行检查点，也不调用模型。

校验成功后，新运行会写入 `artifact.signatures.accepted`，其中包含产物摘要、发布者、key ID 与密钥状态。该事件进入原有哈希链，但运行时仍会在每次恢复前重新验证磁盘上的签名产物。

## 已验证场景

- Binding 清单签名和 Registry lock 签名正常通过；
- 目标内容或签名任一字节被篡改时拒绝；
- 缺失信任策略、未知 key ID、错误发布者时拒绝；
- `retired` 密钥可验证历史签名；
- `revoked` 密钥立即拒绝；
- 已签署清单与运行时 Binding 不一致时拒绝；
- 两节点 ready/模糊分支及中断恢复仍可重放。

## v0.9 完成情况

- 信任库独立根签名、完整工作流包清单签名和 KMS/HSM Provider 接口已完成，详见 [`deepseek-readonly-v0.9.md`](deepseek-readonly-v0.9.md)；
- 具体云 KMS/HSM 驱动仍待选择；
- 在 Linux 定时 CI 中加入真实 DeepSeek SDK 与密钥轮换演练。

## 官方依据

- [Ed25519 signing](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ed25519/)
- [Key serialization](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/serialization/)
