# 契约：research-schema（G1 data provides）

> 状态：**v1 已冻结（2026-09-11 OR-01）**。字段来源：合并稿 B.2 / C.1 / C.3。改字段先 `block`，由 G0 仲裁并写 `taskList.json.contracts.changeLog`。
> **优先级**：§9「OR-01 定稿修订」为规范性增补，与 §1–§7 的 v0 表冲突时**以 §9 为准**。

## 1. 三时钟（所有表必带）

| 列 | 类型 | 语义 |
|---|---|---|
| `event_time` | timestamp[us, UTC] | 源声称事件发生时刻（发布/编辑各属不同版本；作者追述另存 `asserted_event_time`） |
| `available_at` | timestamp[us, UTC] | 该版本及其必要媒体最早可证明可知的时刻。V 级 = 实收时刻；H0 级 = 发布时刻 + 冻结延迟（`temporal_assumptions` 记录） |
| `ingested_at` | timestamp[us, UTC] | 写入研究湖的时刻 |

决策时钟 `t_dec = max(全部依赖 available_at) + 冻结处理延迟`。as-of 比较 `(available_at, sequence)`，顺序未知取严格 `<`。

## 2. 表与主键（Parquet，`data/lake/telegram/`）

### bronze/message_version（不可变，只追加）
| 列 | 类型 | 说明 |
|---|---|---|
| `source_id` | struct{peer_id:int64, message_id:int64} | 稳定源身份 |
| `source_version_id` | string | `sha256(source_id, content_hash, media_hashes, version_evidence)` |
| `version_no` | int32 | 同 source_id 内按 available_at 排序 |
| `content_hash` | string | 原文 sha256 |
| `text` | string | 原文（bronze 允许；silver 起只留 span） |
| `media_hashes` | list[string] | 图片 sha256 |
| `media_uris` | list[string] | 相对 `data/lake/telegram/bronze/media/` |
| `reply_to_message_id` | int64? | |
| `forward_from` | struct? | |
| `grouped_id` | int64? | 相册 |
| `message_date` / `last_edit_at` | timestamp? | Telegram 字段原值 |
| `first_seen_at` / `snapshot_at` | timestamp? | 实收/快照时刻，历史导出为空 |
| `time_grade` | enum{V,H0,H1,H2,U} | 合并稿 A.1 |
| `survival_scope` | enum{historical_survivor, watched_cohort, unknown} | |
| `deleted_after_observation` | bool? | 仅 V |
| `raw_hash` / `raw_uri` | string | 原始导出文件 |
| 三时钟 | | |

### silver/extracted_event
| 列 | 类型 | 说明 |
|---|---|---|
| `extract_id` | string | |
| `source_version_id` | string | FK |
| `kind` | enum{entry_proposal, amend, cancel, expire, entry_claimed, add, reduce, stop_move, tp_ladder, close_claimed, correction, delete_notice, analysis, result_post, chatter, undecidable} | |
| `symbol_raw` / `instrument_id` | string / string? | 规范化后 `BTCUSDT-PERP.BINANCE-UM`（见 §9 裁定 A1；旧写法 `BINANCE-PERP:BTCUSDT` 作废）；映射按 `available_at` 当时市场身份 |
| `side` | enum{long, short}? | |
| `entry` / `stop` / `tps` | struct{lo,hi,kind}? / decimal? / list[struct{level, fraction?}] | 区间入场记上下端；档位按方向排序 |
| `size_hint` | struct? | 数量/比例/杠杆，无依据不补 |
| `spans` | list[struct{field, start, end, source: text|ocr}] | 每个数字必须能定位 |
| `bboxes` | list[struct{field, media_hash, x0,y0,x1,y1}] | 图片来源 |
| `extractor` | struct{name, version, model?} | parser 与 LLM 分别一行 |
| `confidence_bucket` | enum{high, mid, low} | 只作抽样层 |
| `reason_codes` | list[string] | 见 §4 |
| 三时钟 | | 继承版本 |

### gold/episode 与 gold/episode_event（合并稿 B.2）
| 列 | 类型 | 说明 |
|---|---|---|
| `episode_id` | string | 新图版本重新分配；旧版 tombstone |
| `graph_version` | string | 不可变派生版本 |
| `predecessor_ids` / `successor_ids` | list[string] | merge/split 迁移 |
| `channel_id` / `trader_id` / `instrument_id` / `side` | | |
| `entry_branch_id` / `duplicate_group_id` / `cluster_id` | string | |
| `author_plan_state` | enum{none, active, cancelled, expired} | 作者轨 P |
| `author_claim_state` | enum{unknown, claimed_open, claimed_closed} | 作者轨 C |
| `entry_observed` / `exit_observed` / `left_truncated` / `right_censored` | bool | |
| `censor_at` / `censor_reason` | | |
| `time_grade_min` | enum | 所有依赖中最弱一级 |
| `decision_eligible_at` | timestamp? | 决策图闭包完成时刻 |
| `claimed_outcome` / `reconstructed_outcome` | struct? | 后者由 G2 执行结果回填，带 `execution_contract_version` |
| `eligibility_by_estimand` | map[string,bool] | |
| `audit_stratum` / `sampling_probability` / `label_status` / `signoffs` | | label_status ∈ {human_verified, auto_accepted_under_audit, unresolved, rejected} |
| `derivation_hash` | string | 同输入同规则重放一致 |

`episode_event`：`event_id, episode_id, graph_version, kind, event_seq, extract_id, source_version_id, supersedes_event_id, link_method ∈ {reply, quote, plan_ref, window, llm}, link_confidence, edge_available_at, payload(json)` + 三时钟。

**决策图 vs 描述图**：描述图含全部边；决策图只含 `edge_available_at ≤ t_dec` 的边与依赖闭包。G3 只读决策图。

## 3. 状态机（作者轨，合并稿 B.1 表）
允许转移见合并稿 B.1；未列组合写 `invalid_transition` 并保留事件不用于决策。模拟轨（proposed→accepted→working→partial/filled→closed）属 G2 执行合同，两轨只用显式映射关联；"到了 TP1" 不创造成交。

## 4. 原因码（enum，冻结后只增不改）
`RAW_HASH_MISMATCH, SCHEMA_DRIFT, TIME_UNIT_INVALID, VERSION_TIME_UNKNOWN, MEDIA_MISSING, OCR_UNREADABLE, DUPLICATE_EXACT, COPY_LINK_AMBIGUOUS, NOT_SIGNAL, INTENT_AMBIGUOUS, SYMBOL_TIME_INVALID, UNIT_SCALE_CONFLICT, TEXT_IMAGE_CONFLICT, ENTRY_LINK_AMBIGUOUS, PARENT_MISSING, LIFECYCLE_INVALID, DEPENDENCY_NOT_AVAILABLE, BAR_GAP, KEY_DUPLICATE_OR_ORDER, OHLC_INVALID, PRICE_SPIKE_FLAG, FUNDING_SCHEDULE_GAP, RULE_HISTORY_MISSING, MARK_STALE, ENTRY_MARK_DEVIATION, VOL_HISTORY_SHORT, LABEL_RIGHT_CENSORED, CONSENT_REVOKED`
严重度：`UNIT_SCALE_CONFLICT, ENTRY_LINK_AMBIGUOUS, SYMBOL_TIME_INVALID` 及任何方向/品种/归属错 = 致命；其余一般。

## 5. quarantine 表（`data/quarantine/<flow>.parquet`）
字段组按合并稿 C.3：身份（`quarantine_id, batch_id, object_kind, object_id, object_version, partition_id`，唯一键 `(object_id, object_version, rule_version, reason_code)`）、来源与时钟、检测（`reason_code, all_reason_codes, severity, field_path, observed_value_ref, expected_contract, rule_version, schema_hash`）、处置（`status ∈ {open, reviewed, released, superseded, revoked}` …）、血缘（`dependency_refs, affected_manifest_ids, consent_id, retention_deadline`）。同输入同规则重跑不得产生重复隔离记录。

## 6. 损耗表（每层一行，`data/lake/telegram/_loss/<batch>.parquet`）
`batch_id, flow, layer, stratum(channel, year | partition), input_unit, input_n, output_unit, output_n, n_ok, n_review, n_quarantine, n_dup_ref, primary_reason_dist(json), cum_excluded_ids, cum_excluded_weight, rule_version, schema_hash, mapping_hash`。一入多出/多入一出走映射账。

## 7. G1 对外函数签名（Python）
```python
# quant_lab.data.api
def load_episodes(graph_version: str, *, decision_graph: bool = True) -> pl.DataFrame  # gold/episode 决策图视图
def load_episode_events(graph_version: str) -> pl.DataFrame
def loss_table(batch_id: str) -> pl.DataFrame
def quarantine(flow: str, *, status: str | None = None) -> pl.DataFrame
```

---

## 9. OR-01 定稿修订（G0 裁定，2026-09-11，规范性，冲突时优先于 §1–§7）

来源：G1 在 D-01 看板 note 提交的字段修订（7 组）+ G0 交叉核对 `execution-interface.md` / `feature-snapshot.md` 后的裁定。

### 9.1 全数采纳（G1 提案，G0 核对与三份契约无冲突）

- **`bronze/message_version` 增列**：`channel_id int64`（=peer_id，分区键）、`message_type enum{message, service}`、`sequence int64?`（同 `available_at` 内的顺序证据，未知为 null；供 as-of `le_with_sequence` 用）、`version_evidence enum{export_snapshot, edit_date, live_receive, album_infer}`、`temporal_assumptions json`、`text_entities json`、`media_kinds list[string]`、`asserted_event_time timestamp?`。后两项补上了 §1 正文提到却无列的字段，接受。
- **`silver/extracted_event` 增列**：`channel_id`、`version_no`；`size_hint` 定死为 `struct{qty decimal?, fraction decimal?, leverage int?, notional decimal?}`；`tps[]` 增 `kind enum{price, pct}`。
- **`gold/episode` 增列**：`t_dec timestamp?`、`processing_delay_s int`、`order_plan struct`、`decision_snapshot_hash string`、`temporal_assumptions json`、`is_tombstone bool` + `tombstoned_at timestamp?` + `migration_reason string?`；`execution_state` **不入** `gold/episode`（属 G2 模拟轨，用 `episode_id` 外联）。
- **`episode_event` 增 `channel_id`**；明确 `available_at` = 所引 `source_version` 的 `available_at`，`edge_available_at` = 两端点与证据的 `max`。
- **§6 损耗表**：`layer` 为 `int 1..5` + `layer_name enum{normalize, dedup, extract, canonicalize, market_check}`，`stratum` 为 json 字符串 `{channel_id, year}`。
- **§4 增原因码 `EDIT_ORIGINAL_UNAVAILABLE`**（H1 级：仅最终编辑版可见，原始入场被隔离），严重度=一般，与 `VERSION_TIME_UNKNOWN` 区分。原因码总数 28 → 29。**注意：`quant_lab.data.codes.ReasonCode` 需同步加这一个，G0 下轮会实跑比对 29/29。**
- **`QUANT_LAB_DATA_ROOT` 环境变量**：采纳并**扩大到全仓**——默认 `<repo>/quant-lab/data`，`quant_lab.data`、`quant_lab.market`、`quant_lab.research` **全部**通过它解析湖根目录（G2 的 `data/lake/market/` 与 G3 的 `data/lockbox/` 同样适用），函数签名不变。G0 的 OR-04 合成冒烟会把它指向 tmp 目录，任何模块把湖路径写死为相对 `data/` 都会让冒烟失败。

### 9.2 裁定 A1：`instrument_id` 格式 = `BTCUSDT-PERP.BINANCE-UM`

G1 原契约写 `BINANCE-PERP:BTCUSDT`，G2（M-01 修订 §4.1）提议 `BTCUSDT-PERP.BINANCE-UM`。**采纳 G2**，理由是实测证据而非偏好：

```
.venv-g0/bin/python -c "from nautilus_trader.model.identifiers import InstrumentId; InstrumentId.from_str('BINANCE-PERP:BTCUSDT')"
→ ValueError: Error parsing `InstrumentId` from 'BINANCE-PERP:BTCUSDT': missing '.' separator between symbol and venue components
→ 'BTCUSDT-PERP.BINANCE-UM' 解析成功（symbol=BTCUSDT-PERP, venue=BINANCE-UM），nautilus_trader 1.227.0
```

M-08 要求候选 B（Nautilus `SimulationModule`）与候选 A 逐 episode 对拍；若契约用 Nautilus 无法解析的 id，就必须在**正被对拍的那条接缝上**插一层有损转换，A/B 差异将无法归因。此外 `-UM` 显式区分 USDT 本位与币本位，`BINANCE-PERP:` 做不到。三个窗口一律用新格式；G2 提到的互转 helper 只允许出现在读取 Binance Vision 原始文件名的边界处。

### 9.3 裁定 A2：`t_dec` 是 `gold/episode` 的实列（§8 第 1 项闭合）

`t_dec` 由 G1 计算并落成实列，**不由下游各自推导**。定义：`t_dec = max(决策图闭包内全部依赖的 available_at) + processing_delay_s`，`processing_delay_s` 同表存列（默认 0，非 0 必须在 `temporal_assumptions` 记录依据）。`decision_eligible_at` 保留为"闭包完成时刻"的诊断列，**不得**被下游当作 `t_dec` 使用。G3 的 `anchors`（`episode_id, instrument_id, t_dec`）直接取本列。

### 9.4 裁定 A3：Episode → ExecutionRequest 的构造属主（§8 第 2 项闭合）

三段式，各司其职，不得越界：

1. **G1** 在 `gold/episode` 落 `order_plan`（结构 = `execution-interface.md` §3.1 冻结版）、`t_dec`、`decision_snapshot_hash`。G1 只负责把"作者计划"翻译成结构化计划，**不决定钱**。
2. **G2** 提供 `quant_lab.market.contract.build_request(episode_row, *, policy_version, policy_hash, risk_budget, cost_scenario, path_scenario, market_manifest, seed) -> ExecutionRequest`，负责补齐执行侧字段与校验。
3. **调用方**（G3 的研究协议 / G0 的冒烟）提供 `risk_budget`、`policy_version`、场景与 `seed`。`risk_budget` 入场前固定，移动 SL 不重置（合并稿 D.4）。

### 9.5 待 G1 回执（下轮 verify 点）

`quant_lab.data.codes.ReasonCode` 补 `EDIT_ORIGINAL_UNAVAILABLE`；`instrument_id` 全部改用 `BTCUSDT-PERP.BINANCE-UM`；`gold/episode` 落 `t_dec` 实列。G0 下轮实跑核对。

### 9.6 裁定 A4：G1 D-07 三处偏离的仲裁（OR-02 R2，2026-09-11，规范性）

G1 在 D-07 看板 note 中主动申报三处与 §9.1 的偏离。G0 已实跑核对产物（`data/lake/telegram/_loss/tg-3146396a4422.parquet` 六层齐全、`load_episodes('fixture-v1')` 49 列），逐项裁定如下，**均为增量修订，不推翻 v1 既有约束**：

1. **损耗表 `layer` 由 `int 1..5` 扩为 `int 1..6`**，`layer_name enum{normalize, dedup, extract, canonicalize, market_check, **link**}`。理由：链接/生命周期是真实发生损耗的一层（episode 归并会丢事件），压进 `market_check` 会让第 5 层的口径失真。**约束**：(a) 1..5 的层号与语义**不得重编号**，`link` 只能取 6；(b) `layer=5` 仍必须独立成行且 `output_n` 可 >0——D-11 的 verify（`layer=5.*output_n=[1-9]`）与 OR-04 的"损耗表 ≥5 层"断言据此保持有效；(c) `quant_lab.data.codes.Layer` 的 value（`1_normalize`…`6_link_lifecycle`）是内部键，落表的 `layer_name` 必须是本条枚举的短名，二者不得互串。

2. **版本链 amend 的 `link_method` 记 `plan_ref`，准；`LinkMethod` 枚举不新增 `same_source`。** 细分依据一律落 `payload.basis`（本例 `same_source_version_chain`）。理由：`link_method` 是给 G3 做强/弱边分档用的**粗粒度**枚举，冻结后只增不改的代价高于收益；细分基属于证据，归 payload。**约束**：任何消费方（G3 特征、G0 冒烟）不得把 `plan_ref` 直接等同于"跨消息计划引用"，需要区分时读 `payload.basis`。

3. **`episode_event` 增列 `plan_id string?`**，准（增量列，与 §9.1 的 `channel_id` 同性质）。

**未申报即视为漂移**：`gold/episode` 的 `migration_reason string?`（§9.1 明列）在 `load_episodes('fixture-v1')` 输出中缺失（只有 `is_tombstone`/`tombstoned_at`）。G1 须在 D-09 前补列或在看板 note 申报删列理由由 G0 裁定，不得静默省略。

### 9.7 裁定 A5：`cluster_id` 必填 + 决策视图准入 + 机会集自洽（2026-09-11 OR-02 R3，规范性）

**触发**：G0 OR-04 骨架实跑 `freeze_opportunity_set(load_episodes('fixture-v1'))`，得 `episode_ids=27` 而 `weights.height=0`、`n_clusters=0` —— 因为 `gold/episode.cluster_id` **27/27 全为 null**，簇计数 join 无一命中，机会集"非空但零权重"地静默通过。这正是"静默为空"的最坏形态：下游 θ 会在零权重上算出来而不报错。另发现决策视图有 1 行 `instrument_id = null`（与 review-G1-P1 S07 的隔离行准入问题同源）。

**裁定**：

1. **G1（D-07/D-09 属主）**：`gold/episode.cluster_id` 为**非空必填**列。合成夹具与真实产线一致：无法定簇（缺品种/缺 t_dec/隔离）的 episode 不得以 null 混入决策视图，应 `eligible=false` 且带明确 `reason`。单机会自成一簇是合法取值，null 不是。
2. **G1**：决策视图（`load_episodes` 默认返回）中 `instrument_id`、`t_dec`、`cluster_id` 三列非空；未达标的行只出现在描述视图/quarantine，不出现在决策视图。
3. **G3（R-06 属主）**：`freeze_opportunity_set` 必须在返回前断言 `len(episode_ids) == weights.height` 且 `weights["cluster_id"].null_count() == 0`，不成立即抛 `EvalProtocolError`，禁止返回自相矛盾的 `OpportunitySet`。`n_clusters` 不得在 `episode_ids` 非空时为 0。
4. **G0（OR-04）**：端到端冒烟对 `n_clusters > 0` 与 `weights.height == len(ids)` 双断言，作为回归闸。

**验收**：G1 侧 `load_episodes('fixture-v1')` 三列 null_count 全 0；G3 侧对 cluster_id 含 null 的输入有一条断言测试；OR-04 上述两个断言绿。
