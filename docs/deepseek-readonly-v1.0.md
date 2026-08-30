# DeepSeek Harness 只读 Graph v1.0：HSM 签名与可信运行证据

> v1.0 是可信运行技术基线。v1.1 已用 `dev`、`team`、`regulated` 预设收敛配置入口，参见 [`development-roadmap-and-complexity.md`](development-roadmap-and-complexity.md)。下文保留完整底层参数，供平台与安全评审使用。

## 评审结论

v1.0 将 v0.9 的“可信工作流软件包”扩展为“可信构建 + 可信运行”：

- 提供可连接 SoftHSM、企业 HSM 和云 HSM PKCS#11 模块的 Ed25519 Signing Provider；
- 为每个运行事件附加发布者签名，防止攻击者重算哈希链后伪造轨迹；
- 为每个检查点生成分离签名，并绑定已签名事件链中的轨迹头；
- 提供 SQLite 事务事件存储，支持原子序号分配、运行租约和终止记录保留策略；
- DeepSeek Runner 在模型调用前强制要求独立运行签名 Provider。

共享总定义仍保持 `3.0.0`，因为这些变化属于 Factory 的运行信任实现，没有改变双仓 Skill/Tool 或 Workflow IR 契约。

## v1.0 信任链

```text
离线根公钥
  → 验证发布者信任库
  → 验证 Tool Binding 清单
  → 验证 Registry Lock
  → 验证完整工作流软件包 Manifest
  → 创建签名运行
      → 每个事件：哈希链 + Ed25519 发布者签名
      → 每个检查点：状态文件 + Ed25519 分离签名
  → 检查点 trajectory_head 必须等于已验证事件链中最新的 state.checkpointed 事件
  → 恢复 / 重放 / 审计
```

哈希链可以发现普通内容修改，但能够写入全部文件的攻击者也可以重新计算所有哈希。v1.0 的事件签名由信任库中的独立运行密钥验证，因此攻击者在没有私钥或 HSM 权限时无法生成一条新的可信轨迹。

## PKCS#11 Ed25519 Provider

`Pkcs11Ed25519SigningProvider` 使用 `python-pkcs11` 调用厂商 PKCS#11 模块：

- 通过模块路径、Token Label、Key Label 和可选 Object ID 定位私钥；
- 使用 `CKM_EDDSA`/`Mechanism.EDDSA` 对规范化签名声明签名；
- 私钥材料不会进入 Python 进程，也不会写入工作流软件包；
- PIN 只从环境变量读取，默认变量名为 `AWF_PKCS11_PIN`；
- 签名信封中的 Key ID 必须对应信任库中已登记的 Ed25519 公钥。

安装可选依赖：

```bash
python -m pip install -e '.[pkcs11]'
```

SoftHSM 是本地验证 PKCS#11 集成的推荐兼容环境；生产环境可以替换为企业 HSM 或提供 PKCS#11 模块的云 HSM。Token 初始化、Ed25519 Key Pair 生成和公钥导出命令取决于具体模块，应以设备文档为准。

## 登记 HSM 公钥

HSM 生成密钥后，只导出 Ed25519 公钥，并转换为 PEM SubjectPublicKeyInfo 格式。不要导出私钥。

```bash
python scripts/workflowctl.py register-key \
  --public-key ~/.config/agent-workflow-factory/runtime-hsm-public.pem \
  --trust-store ~/.config/agent-workflow-factory/trusted-publishers.json \
  --publisher agent-workflow-factory-runtime
```

命令会计算 `sha256:<public-key>` Key ID 并把公钥加入信任库。信任库发生变化后，必须用离线根重新签署：

```bash
python scripts/workflowctl.py sign-artifact \
  ~/.config/agent-workflow-factory/trusted-publishers.json \
  --private-key ~/.config/agent-workflow-factory/trust-root.pem \
  --output ~/.config/agent-workflow-factory/trusted-publishers.sig.json \
  --publisher agent-workflow-factory-trust-root
```

## 使用 HSM 编译签名软件包

先把 PIN 写入当前 Shell 环境。不要把 PIN 放进命令参数、Git、日志或 BPMN：

```bash
export AWF_PKCS11_PIN='<从安全凭据系统注入>'
```

使用 HSM 构建密钥编译：

```bash
python scripts/workflowctl.py compile \
  build/deepseek-live/process.bpmn \
  --business examples/deepseek-readonly/business-requirement.json \
  --catalog fixtures/catalog.snapshot.json \
  --output build/deepseek-live/package \
  --pkcs11-module /path/to/vendor-pkcs11.so \
  --pkcs11-token-label awf-build \
  --pkcs11-key-label workflow-build-key \
  --pkcs11-key-id sha256:<构建公钥摘要> \
  --pkcs11-object-id 01
```

PEM `--signing-key` 与 PKCS#11 参数互斥。PKCS#11 参数不完整、PIN 缺失、签名长度错误或依赖未安装时都会立即失败。

## 从 v0.9 升级运行配置

v1.0 要求构建密钥与运行密钥分离。已有本地环境可以生成一个运行密钥，并重新签署信任库：

```bash
python scripts/workflowctl.py keygen \
  --private-key ~/.config/agent-workflow-factory/runtime-signing-key.pem \
  --trust-store ~/.config/agent-workflow-factory/trusted-publishers.json \
  --publisher agent-workflow-factory-runtime

python scripts/workflowctl.py sign-artifact \
  ~/.config/agent-workflow-factory/trusted-publishers.json \
  --private-key ~/.config/agent-workflow-factory/trust-root.pem \
  --output ~/.config/agent-workflow-factory/trusted-publishers.sig.json \
  --publisher agent-workflow-factory-trust-root
```

旧 v0.9 软件包不必仅因运行签名升级而重新编译，但新的 v1.0 DeepSeek 运行目录必须使用签名事件和签名检查点。不要把旧的无签名运行目录直接标记为 v1.0 合规。

## DeepSeek + SQLite 生产模式

使用本地 PEM 运行签名密钥：

```bash
python scripts/workflowctl.py run \
  build/deepseek-live/package \
  --adapter deepseek \
  --runtime-dir build/deepseek-live/runtime-v10 \
  --run-id run-deepseek-v10-001 \
  --facts examples/deepseek-readonly/initial-facts.json \
  --trust-store ~/.config/agent-workflow-factory/trusted-publishers.json \
  --trust-store-signature ~/.config/agent-workflow-factory/trusted-publishers.sig.json \
  --trust-root-public-key ~/.config/agent-workflow-factory/trust-root-public.json \
  --runtime-signing-key ~/.config/agent-workflow-factory/runtime-signing-key.pem \
  --event-store sqlite \
  --lease-owner workflow-worker-01 \
  --lease-ttl-seconds 30 \
  --retention-days 90
```

使用 HSM 运行密钥时，把 `--runtime-signing-key` 替换为：

```bash
--runtime-pkcs11-module /path/to/vendor-pkcs11.so \
--runtime-pkcs11-token-label awf-runtime \
--runtime-pkcs11-key-label workflow-runtime-key \
--runtime-pkcs11-key-id sha256:<运行公钥摘要> \
--runtime-pkcs11-object-id 02
```

## 验证和重放

离线验证一个已经签署的运行不需要私钥，只需要受离线根保护的信任库：

```bash
python scripts/workflowctl.py runtime-replay \
  build/deepseek-live/package \
  run-deepseek-v10-001 \
  --runtime-dir build/deepseek-live/runtime-v10 \
  --runtime-trust-store ~/.config/agent-workflow-factory/trusted-publishers.json \
  --runtime-trust-store-signature ~/.config/agent-workflow-factory/trusted-publishers.sig.json \
  --runtime-trust-root-public-key ~/.config/agent-workflow-factory/trust-root-public.json \
  --runtime-publisher agent-workflow-factory-runtime \
  --require-runtime-signatures \
  --require-runtime-trust-root \
  --event-store sqlite
```

重放同时检查：

- 事件序号连续；
- `prev_hash` 哈希链连续；
- 事件内容与 `event_hash` 一致；
- 每个事件签名的发布者、Key ID、Key 状态和签名值有效；
- 检查点文件与分离签名一致；
- 检查点 `trajectory_head` 等于签名事件链中最新的 `state.checkpointed` 事件；
- 最新检查点后不得出现未同步到检查点的状态变更事件；
- 从最后一个 `state.checkpointed` 事件重建的状态与检查点一致。

## SQLite 租约与保留策略

SQLite 后端使用 WAL 和 `BEGIN IMMEDIATE` 写事务，为同一个 `run_id` 原子分配事件序号。配置 `--lease-owner` 后：

- 只有持有未过期租约的 Worker 可以追加事件；
- 同一个 Owner 的后续操作会刷新租约；
- 另一个 Owner 在租约到期前不能接管；
- Runtime 标记为 `completed`、`escalated` 或 `cancelled` 后才可进入保留期清理；
- 存在有效租约的运行不会被清理；
- 清理同时删除 SQLite 事件、租约、运行索引、检查点及其签名。

执行保留期清理：

```bash
python scripts/workflowctl.py runtime-purge \
  build/deepseek-live/package \
  --runtime-dir build/deepseek-live/runtime-v10 \
  --event-store sqlite \
  --retention-days 90
```

## PKCS#11 Live Test

仓库提供真实 Token 测试，但默认跳过。配置 SoftHSM 或真实 HSM 后设置：

```bash
export AWF_PKCS11_LIVE_TEST=1
export AWF_PKCS11_MODULE=/path/to/vendor-pkcs11.so
export AWF_PKCS11_TOKEN_LABEL=awf-runtime
export AWF_PKCS11_KEY_LABEL=workflow-runtime-key
export AWF_PKCS11_KEY_ID=sha256:<运行公钥摘要>
export AWF_PKCS11_PIN='<从安全凭据系统注入>'
export AWF_PKCS11_TRUST_STORE=~/.config/agent-workflow-factory/trusted-publishers.json

python -m unittest tests.test_pkcs11_live -v
```

该测试会让 Token 实际签署规范化 JSON，并使用信任库中的 Ed25519 公钥完成验证。

## 已覆盖的攻击场景

自动化测试覆盖：

- 缺少运行签名 Provider 时在模型调用前失败；
- 事件内容被修改；
- 攻击者修改事件后重新计算完整哈希链；
- 检查点内容被修改；
- 使用签名有效但已经过期的旧检查点回滚当前状态；
- 在检查点后注入签名有效但未同步到状态文件的状态事件；
- 事件签名缺失、Key ID 不受信任或密钥被吊销；
- `run_id` 路径穿越；
- SQLite 租约冲突；
- 终止记录在有效租约期间被错误清理；
- PEM 与 PKCS#11 配置冲突、PIN 缺失和错误签名长度。

## 明确限制

- SQLite 适合单机或共享块存储上的受控 Worker，不替代跨区域数据库和分布式共识；
- 删除整套 SQLite 数据库和检查点并回滚到一份更早的完整备份，仍需要外部不可回滚时间戳、透明日志或远程审计锚才能识别；
- 不同 HSM 对 Ed25519、Key Object 属性和并发会话的支持存在差异，必须运行 Live Test；
- 当前 Provider 覆盖 PKCS#11 HSM。AWS KMS、Azure Key Vault、Google Cloud KMS 等非 PKCS#11 原生 API 仍需单独驱动；
- 保留策略删除是显式运维动作，应先满足组织的审计、法律和数据保留要求。
