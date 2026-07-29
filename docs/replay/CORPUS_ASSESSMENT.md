# 真实图文 corpus 评估（C-03 前置）

> 输入：第二个 cmux drop（`window-c-integration-acceptance-replay-hk`）里的真实 Telegram 语料 + HK 部署参考。
> 结论：**数据可用、但未达 C.4 全部硬指标**，需 operator 决策两处缺口后再正式建 C-03 corpus。

## 1. 现有语料事实（mechanical，已核对）

| 项 | 值 |
|---|---|
| 路径 | `inbound/.../hk-replay/fixtures/signals/telegram_watcher_80msg_50img/` |
| 消息数 | 80（恰好踩 C.4 下限，无余量） |
| 含图消息 | 50（图片 50 张，0 缺失）→ 满足「≥30 含图」 |
| 来源 | home-mini 上 500 条语料的子集 |
| 两个来源群（→ 双账户路由素材） | `-1002198013097` Titan 家族 40 条；`-1002228497993` Gauls 家族 40 条（40/40 均衡）|
| 时间 | 2026-02-19 → 2026-05-16，**严格单调有序** ✓ |
| 空文本消息 | 17 条（多为纯图信号）→ **多模态必测样本**（Hermes 必须读图） |
| 消息字段 | `id, chatId, chatTitle, sender, text, media{type,filename,path,mimeType,size}, date` |
| 缺失字段（需 C 在 manifest 阶段补） | `sha256`、`source_version/edit`、`reply_to`、canonical `account_id` 绑定 |

## 2. 对照 PLAN C.4 硬指标

| 要求 | 状态 |
|---|---|
| ≥80 条真实消息 | ✓ 80（无余量） |
| ≥30 条含真实图片 | ✓ 50 |
| 保留原始顺序/timestamp | ✓ |
| **≥20 新开仓** / **≥20 更新平仓** / **≥20 分析噪音** / **≥20 歧义冲突** | ⚠ **未验证 + 可行性存疑**：四类各 ≥20 合计 = 80 = 整个语料，80 条几乎不可能同时满足四个下限；且分类是语义判断，不能由我用正则臆断 |
| **必含 HYPE 未下单事件** | ❌ **缺失**：80 条里 0 条含 HYPE；附带的 6 条 superset zip 里也 0 条 |
| **必含 INJ 更新误开仓事件** | �了 候选 = 消息 `id 4669`（B01-01Gauls，2026-02-20，「虚拟交易更新…#INJ 和 #KITE…」），**待人工确认是否就是该事故** |

## 3. ⚠ 重大风险：附带回放脚本是被禁的旁路路径

`hk-replay/replay_corpus_summary.py` 与 DEPLOYMENT.md 里的 `scripts/replay_smoke.py` 都通过：

```
python -m freqtrade.signal_strategy.importer --stdin --approve-parsed --refresh-window
```

把消息**正则解析出 message_type/pair 后直接 `status=approved`**。这正是 PLAN 不可妥协约束 #3、放行条件 G1 明令禁止的 `watcher → parser/importer → approved` 旁路自动批准路径。

处置：
- **语料数据复用**；**回放机制弃用** —— 该脚本绝不进入 v3 验收/生产路径。
- C-04 v3 回放必须走：真实 watcher/ingress → Postgres raw → **真 Hermes** → Gateway → Nautilus sandbox → projection → Dashboard。
- C-10 安全审计必须确认 `--approve-parsed`/`--refresh-window`/`importer` 在 v3 生产路径已移除；命中即 `gate=blocked`。

## 4. 需 operator 决策（阻塞 C-03 正式开工）

1. **语料源**：是否扩到 home-mini 上的 500 条全集，以同时满足四类各 ≥20 + 余量？（当前 80 条只能当回归子集，不能当最终验收 corpus）
2. **HYPE 未下单事故**：原文/图片/上下文在哪？（home-mini telegram-watcher？需定位日期+群+消息）必须纳入 corpus。
3. **INJ 事故**：确认 `id 4669` 即「INJ 更新误开仓」事故，或指出真正那条。
4. **双账户绑定**：`Titan(-1002198013097)` 与 `Gauls(-1002228497993)` 各映射到哪个 Nautilus 账户/trader_id（待 A/B 账户配置落地后定）。

## 5. gold label 立场（不可伪造）

gold label 是衡量 Hermes 的真值，PLAN 要求**每条人工复核**。我可以产出**机械 manifest**（hash/计数/路由/顺序）和**人工标注工作表**，并可附 AI 草稿建议，但**语义真值必须由人复核签字**，我不会把草稿当最终 gold。`tests/replay/gold/gold-label.schema.json` 已定义其契约。

## 6. 下一步（A/B 完成前可做）

- 写**干净的（非 importer）** `build_corpus_manifest.py`：读 watcher fixture → 算 sha256 → 映射 canonical schema → 计数/校验 → 保序，输出 `corpus-manifest.json`（先对 80 条子集，脚本对 500 条复用）。
- 据 §4 决策定最终语料源后，再正式建 C-03 corpus + 人工 gold。
