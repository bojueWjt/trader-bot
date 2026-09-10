# 契约：feature-snapshot（G3 research provides）

> 状态：v0 草案，G0 在 OR-01 定稿。来源：合并稿 D.1–D.5 / E。

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
    execution: pl.DataFrame,      # G2 simulate_batch 输出，含 baseline 与 candidate 同机会
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
