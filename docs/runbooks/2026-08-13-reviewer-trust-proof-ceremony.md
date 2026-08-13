# 四账号不可变发布：reviewer trust proof 签署仪式

适用：`hk-deploy-20260803.sh` immutable 交付路径。capture 阶段强制校验
`$STAGING/reviewer-trust-proof.json`，并要求操作员以
`TRADER_RELEASE_REVIEWER_TRUST_SHA256` 环境变量 pin 该文件哈希。proof
必须绑定本次构建的 attestation 文件哈希，因此部署为两段式。

## 流程

### Run N（首跑，预期在 proof 门 fail-closed 停止）

```bash
cd /tmp/<release-staging>
bash hk-deploy-20260803.sh
# 预期 FATAL: pinned reviewer trust proof sha256 …（节点保持 HALTED/stopped）
# 此时 $STAGING 已有 immutable-build-attestation.json 与 derived-image.id
```

### 签署（reviewer 人工审批）

用与 capture 完全相同的代码路径预览 subject（不要手工复刻计算）：

```bash
python3 release_manifest.py capture \
  --preview-review-subject \
  --bundle-manifest "$STAGING/bundle-manifest.json" \
  --dependency-lock "$STAGING/uv.node.lock" \
  --patch-root "$STAGING" \
  --require-transition-runtime \
  --delivery-mode immutable_image \
  --image-digest "$(cat "$STAGING/derived-image.id")" \
  --node-config account-a=trader-v3-node-a=<run N 的 artifact 路径> \
  --node-config account-b=trader-v3-node-b=<…> \
  --node-config account-c=trader-v3-node-c=<…> \
  --node-config account-d=trader-v3-node-d=<…>
# 输出 PREVIEW review_subject_sha256=… build_attestation_sha256=… source_commit=…
```

reviewer 审批通过后写 proof（五字段 exact-set）：

```json
{
  "schema_version": "trader-v3-reviewer-trust-proof/v1",
  "reviewer": "<审查人>",
  "decision": "approved",
  "source_commit": "<PREVIEW source_commit>",
  "review_subject_sha256": "<PREVIEW review_subject_sha256>",
  "build_attestation_sha256": "<PREVIEW build_attestation_sha256>"
}
```

保存为 `$STAGING/reviewer-trust-proof.json`，然后：

```bash
export TRADER_RELEASE_REVIEWER_TRUST_SHA256="$(sha256sum "$STAGING/reviewer-trust-proof.json" | cut -d' ' -f1)"
```

### Run N+1（复用构建，走完全流程）

```bash
cd "$STAGING" && TRADER_RELEASE_REVIEWER_TRUST_SHA256="$TRADER_RELEASE_REVIEWER_TRUST_SHA256" \
  bash hk-deploy-20260803.sh
# builder 打印 REUSED immutable node image …（attestation 字节不变，proof pin 有效）
```

## 硬前提（违反任何一条都会退回全新构建 → proof 失配）

1. **Run N 与 Run N+1 之间禁止重新解包 staging**——attestation 不在
   SHA256SUMS/bundle 内，重新解包会抹掉它。
2. attested image 必须仍在 daemon（禁止 `docker image prune` 波及）。
3. `trader-bot/immutable-base:<hex>` alias 不得被改动。
4. bundle 任何文件（含 uv.node.lock、迁移文件）不得变化。

复用判定的安全校验与全新构建等价：attestation 字段 exact-set、
build subject 绑定全部 7 项输入、镜像自证 ID、base layer 前缀、alias
复验、daemon 实际 label 逐键匹配；任一存疑即回退全新构建（此时需重新
签署 proof）。

## 相关

- 复用逻辑：`scripts/build_immutable_node_image.py` `_reusable_attested_image`
- 校验器：`scripts/release_manifest.py` `validate_reviewer_trust_proof`
- watcher builder 未接入部署链，暂无复用需求（接入时需同步补齐）。
