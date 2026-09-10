# 契约：feature-snapshot（G3 research provides）

> 状态：**v1 已冻结（2026-09-11 OR-01）**。来源：合并稿 D.1–D.5 / E。改签名先 `block`。
> **优先级**：§7「OR-01 定稿修订」为规范性增补，与 §1–§6 冲突时**以 §7 为准**。

## 1. AST（JSON，唯一入口；不接受 Python 字符串）
```json
{"op": "Div", "args": [
  {"op": "Mean", "args": [{"field": "close"}], "window": {"unit": "wallclock", "minutes": 300}},
  {"field": "close"}
]}
```
- 节点只有三类：`{"field": <registered_field>}`、`{"const": <finite number>}`、`{"op": <registered_op>, "args": [...], "window"?: {...}, "params"?: {...}}`。
- lint 硬门：未知 op/field/键、非有限常量、深度 > 4、节点 > 15、负 `Ref`、`center` 窗、跨资产算子 → 拒收（`ASTRejected`）。
- `canonical_hash = sha256(规范化 JSON)`，规范化含参数排序与常量归一，用于去重与账本。
- 本 DSL `Ref(x, 0)` = 当前闭合值（与 Qlib 不同，禁止直接复用同名算子）。

## 2. 算子登记表（`quant_lab.research.ops.REGISTRY`）
每个算子一条：`name, arity, input_units, output_unit, window_semantics ∈ {wallclock, rows, none}, lookback, lookahead=0, min_samples, nan_policy, ddof?, tie_rule?, init_rule?, version, backends ∈ {polars, polars_ta}`。
首批：`Add Sub Mul SafeDiv Abs LogPositive Gt Lt And Ref Delta Mean Std Min Max TSRank Corr EMA`。
契约测试（每算子必过）：截断重算、未来 NaN/极值/乱序不影响过去、过去缺值按 nan_policy 传播、墙钟缺 bar 不跨洞、≥20 边界例 + 100 随机序列 手算 reference，atol=1e-10 / rtol=1e-8，布尔完全一致。任一失败 → 算子隔离，依赖它的 AST 全部禁用。

## 3. feature_snapshot
```python
# quant_lab.research.features
def feature_snapshot(
    asts: list[dict], anchors: pl.DataFrame,   # anchors: episode_id, instrument_id, t_dec
    *, bars: pl.DataFrame, backend: str = "polars", cutoff: datetime | None = None,
) -> pl.DataFrame   # 列: episode_id, t_dec, f_<canonical_hash>..., validity_<hash>
```
对齐规则：特征时间戳 = bar `close_time`，取 `close_time <= t_dec` 的最后一根（G2 `last_closed_bar`），等号语义单测。缓存 key = `(canonical_hash, op_versions, market_manifest, cutoff, fold_id, preprocess_state)`。

## 4. EventEvaluator（合并稿 D.4）
```python
# quant_lab.research.evaluator
@dataclass(frozen=True)
class OpportunitySet:            # 收益前冻结
    episode_ids: list[str]; eligibility: pl.DataFrame; weights: pl.DataFrame  # 簇均权：每经济簇总权重 1
def evaluate(
    ast: dict, opp: OpportunitySet, *, features: pl.DataFrame, rule: Callable[[pl.DataFrame], pl.Series],  # take/skip
    execution: pl.DataFrame,      # G2 simulate_batch 输出（见 §7.3：不含 candidate 概念，G3 发两组 request 后自行配对）
    fold_id: str, attempt_id: str,
) -> EvalResult                    # theta, se, n_opportunities, n_clusters, coverage, nan_rate, per_fill_R, tail_loss, unclosed_rate
```
`θ = Σ w_i (R_cand,i − R_base,i) / Σ w_i`；候选 NaN → `skip(0)`，基线仍在分母；`nan_rate > 5%` 拒收；证据缺失/右删失不记 0 而是排除并计入损耗。

## 5. 研究协议对象
- `walk_forward(anchors, *, scheme ∈ {expanding, rolling}, min_train_clusters, embargo, label_maturity)` → 折列表；每折输出 train/test 索引、purge 原因、embargo 区间。
- `PurgedKFold(t1)` 参考实现（枚举区间交集）只作稳定性对照，不作上线后表现声明。
- 尝试账本 `data/lockbox/ledger.parquet`：`attempt_id, origin ∈ {human, llm, enumeration, gp}, parent_id, canonical_hash, params, fold_id, visible_cutoff, code_version, model_version, data_manifest, seed, cost, objective, status`。每次评估前写入，失败/非法/重复也留终态。
- `max_t_bootstrap(thetas, ses, *, block_len_days, B=2000, seed)` → `p_adj`, 下界；共同日历块索引；`< 20` 非空块 → insufficient。
- 空模型：训练窗残差整块重采样（保留块内相关），逐 episode 洗牌禁止；FPR/功效验收报告 `docs/adr/report-G3-null-model.md`。
- 分档报告：`K = min_fold N_cluster_fold`，`DEFF`，档位与 `p ≤ min(档硬顶, floor(K/20))`。

## 6. G3 对外签名
```python
# quant_lab.research.api
def run_protocol(config_path: str) -> ProtocolReport   # 冻结配置 → 折、账本、θ/CI、档位、报告文件
def enumerate_grammar(depth: int = 2, *, ops: list[str], windows: list[int], fields: list[str], cap: int) -> list[dict]
```

---

## 7. OR-01 定稿修订（G0 裁定，2026-09-11，规范性）

G3 窗口本轮未启动，未提交修订；以下三条是 G0 在合并 G1（D-01）与 G2（M-01）修订时发现的、**直接改变 G3 桩签名**的跨契约裁定。G3 起步时按本节写桩。

### 7.1 `anchors.t_dec` 来自 `gold/episode` 的实列，不自行推导

见 `research-schema.md` §9.3。`t_dec` 是 G1 落的实列（`= max(闭包内依赖 available_at) + processing_delay_s`）。`decision_eligible_at` 是诊断列，**禁止**当 `t_dec` 用。

### 7.2 `instrument_id` 格式 = `BTCUSDT-PERP.BINANCE-UM`

见 `execution-interface.md` §5.2（Nautilus 实测证据）。旧写法 `BINANCE-PERP:BTCUSDT` 作废，合成夹具一并改。

### 7.3 `evaluate` 的 `execution` 入参：G3 自己配对 baseline/candidate

`simulate_batch` 输出**不含** baseline/candidate 概念（`execution-interface.md` §5.7 裁定 B6）。G3 发两组 `ExecutionRequest`（共享 `episode_id, graph_version`，差异在 `policy_version`/规则），按 `episode_id` 配对成两臂。列名与配对键见 G2 的 `docs/adr/report-G2-contract-revision-M01.md` §3.3 / R9。

### 7.4 `net_R` 可空，与 NaN/0/排除的判定表

| 情形 | 产出 | 谁负责 |
|---|---|---|
| 右删失 / 证据缺失（`censor_reason` 非空） | `net_R = null`，**排除出分母并计入损耗**，不记 0 | G2 出 null，G3 排除 |
| 未成交（`fill_status = none`，无删失） | `net_R = 0` | G2 |
| 规则判 skip | `net_R = 0`，仍在分母 | G3（G2 不产生 skip） |
| 候选特征 NaN | 按 skip 处理（记 0），基线仍在分母；`nan_rate > 5%` 拒收 | G3 |

**"删失记 0"是幸存者偏差的主要泄漏口，OR-05 的 adversarial review 会重点查这一条。**

### 7.5 湖路径统一走 `QUANT_LAB_DATA_ROOT`

`data/lockbox/ledger.parquet` 等路径必须经 `QUANT_LAB_DATA_ROOT`（默认 `<repo>/quant-lab/data`）解析，不得写死相对路径；G0 的 OR-04 冒烟会把它指向 tmp 目录。
