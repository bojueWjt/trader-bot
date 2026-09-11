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

### 9.8 裁定 A6：`gold` 发布门与"改门必须同步发布"义务（2026-09-11 OR-02 R4，规范性）

**触发**：G1 于 04:22/04:29 在 `quant_lab.data.graph.verify_manifest` / `api.load_episodes` 上线了发布门（要求 manifest `status == "published"` 且逐文件 sha256 与 `files` 一致、未 tombstone），但 `gold/_manifest/fixture-v1.json` 仍是 04:04 的旧格式（无 `status`、无 `files`）。后果：`load_episodes("fixture-v1")` 抛 `LookupError: graph_version=fixture-v1 状态 None，不可消费`，OR-04 六条集成测试**全部 ERROR at setup**，G1→G2/G3 接缝在 R4 全时段黑屏。

**裁定**：

1. **发布门本身合规并纳入契约**（属 G1，D-07/D-09 属主）：研究侧读取入口（`load_episodes` 及一切默认返回决策视图的 API）**必须**在读前校验单一发布指针，缺 manifest / `status != "published"` / 文件 hash 不符 / 已 tombstone 一律抛 `LookupError`，禁止降级为空表返回。"静默为空"在这里等价于伪造数据集。
2. **manifest 必备字段**：`graph_version`、`input_hash`、`rule_versions`、`assumptions`、`files{文件名 → sha256}`、`counts`、`graph_kind`、`status`、`built_at`。旧格式（缺 `status`/`files`）视为**未发布**。
3. **改门即同步发布（本条为本裁定的核心义务）**：任何窗口收紧上游读取前置条件时，**同一次交付内**必须一并重建并发布满足新门的制品，或提供一条幂等的重建命令并把它写进该任务的 `verify`。上游改门而不发布 = 单方面切断下游，等同契约漂移，G0 一律判 fail。
4. **G3 侧**：不得为绕开发布门直接 `pl.read_parquet` gold 制品（审计/取证入口除外，且须显式命名为 audit 路径）。G3 遇 `LookupError` 应原样冒泡，不得 try/except 退回合成桩 —— 否则 R-09 会变成"因为用了桩所以永远绿"。
5. **G0 侧**：OR-04 对 `load_episodes` 的 `LookupError` 不做兜底，直接暴露为集成失败。

**验收**：G1 提供并跑通一条重建发布命令，`load_episodes("fixture-v1")` 返回非空 DataFrame 且 §9.7 A5 三列 null_count 全 0；OR-04 六条不再 ERROR at setup。

### 9.9 裁定 A7：review 类任务的 verify 必须判读终裁行（2026-09-11 OR-02 R4，规范性）

**触发**：D-10 的 verify `test -f docs/adr/review-G1-P1.md && ! grep -q '必修.*未闭合' docs/adr/review-G1-P1.md` 实测 **rc=0（绿）**，而该文档第 3 行写的是「终裁：**fail**。必修共 **16 条（S01–S16）**」，第 338 行写的是「未关闭任何完整 S 项……终裁保持 fail」。负向 grep 依赖对方措辞，是**结构性假绿**。

**裁定**：D-10 / M-10 / R-10 及后续一切 `[Codex review] … 必修项闭合` 型任务，其 `verify` 必须**正向**判读终裁行，例如：

```
docs/adr/review-G<N>-P1.md 中最后一条「终裁」/「二审终裁」行的取值必须为 pass
```

参考实现（口径，不强制字面）：`tail -40 <file> | grep -E '^(二审)?终裁[：:] *\*{0,2}pass'`。同时：(a) 终裁行在文档中**唯一可判读**（多轮 review 用「二审终裁」「三审终裁」区分，且以最后一条为准）；(b) 仅有必修条目清单而无终裁行 = 未通过；(c) **任何窗口不得凭负向 grep 的 rc=0 把 review 任务置 done**，G0 不认，发现即回退。M-10 现行 verify 恰好 rc=1（真实反映 fail），可暂留但同样应改为正向判读。

### 9.10 裁定 A8–A12：G1 五项裁决请求的逐条答复（2026-09-11 OR-02 R5，规范性）

来源：G1 于 04:40 看板 note「需 G0 裁决（不算 G1 自决）」提交五项。G0 已读 `contracts/` 全文与 G1 note 原文，并在 `.venv-g0`（polars 1.44.2）实跑取证后裁定如下。

#### 9.10.1 裁定 A8（答 CR-01）：全链 `Decimal(38,12)`，struct 固定键**准**且键集入契约

**取证**：`load_episodes('fixture-v1')` 的 `order_plan` 现为 `entries[].{price_lo, price_hi, fraction}`、`stop.price`、`tps[].{level, fraction}`、`sizing.qty` 全部 `Float64`；G2 `contract.py` 对同名字段用 `Decimal` + `check_decimal`（标度 12）。G0 实跑 polars 1.44.2：`pl.Decimal(38,12)` 在**列、Struct 内嵌、Parquet 往返**三种场景均正常（`Decimal('1.234567890123')` 原值回读，Struct 内可空）。**故"polars 不支持"不成立，Float64 不是被迫选择。**

**裁定（数值精度全链一致）**：

1. **凡进入 `build_request` / `trace_hash` / `derivation_hash` / `decision_snapshot_hash` 输入的数值，全链使用 `Decimal`，落盘编码为 `pl.Decimal(38,12)`，禁止 `Float64`。** 覆盖列：`gold/episode.order_plan` 的 `entries[].{price_lo, price_hi, fraction}`、`stop.price`、`tps[].level`、`tps[].fraction`、`sizing.qty`；`silver/extracted_event` 的 `size_hint{qty, fraction, notional}` 与 `tps[].level`（§9.1 原文即写 `decimal?`，Float64 属**未申报漂移**，本条是执行而非新增要求）。
2. **理由不是洁癖，是两处可复现的破坏**：(a) `Decimal(float)` 在接缝处产生 `0.1 → 0.1000000000000000055511151231257827` 这类不可复算尾数，`trace_hash` 随平台/版本漂移；(b) 3 腿浮点等分 `1/3` 之和 `!= 1`，直接触发 G2 `sum(entries.fraction) != 1` 的 `ContractError`——即"随机在某些 episode 上报错"。
3. **哈希序列化口径**：Decimal 进哈希前一律规范化为**定标度 12 的十进制字符串**（不去尾零、不用科学计数法、负号在前），使哈希与存储编码解耦。G1 须在 `derivation_hash` 的实现里落这条，并加一条测试：同一逻辑值以 `Decimal("6")` / `Decimal("6.000000000000")` 构造，哈希相同。
4. **时间戳与非哈希路径**：`Datetime(us, UTC)` 维持不变；纯展示/统计列（如 `sampling_probability`、`link_confidence`）可保留 `Float64`，但**不得**进任何哈希输入。
5. **struct 固定键：准。** 固定键优于 `map`（schema 稳定、polars 可下推、缺键即 schema 漂移可查）。§2 的 `eligibility_by_estimand: map[string,bool]` 按本条改为 **struct 固定键**。以下键集**即刻冻结**，增删键 = 契约变更，须 `block` 并由 G0 走 changeLog：
   - `claimed_outcome` / `reconstructed_outcome`: `{kind, at, source_version_id, execution_contract_version}`（`at: Datetime(us, UTC)`；`reconstructed_outcome` 由 G2 结果回填）。
   - `eligibility_by_estimand`: `{description, entry_decision, execution, original_entry, price_check, outcome}`（全部 `Boolean`，**不可空**——null 会让"不合格"与"没算"不可分）。
   - `order_plan`: `{instrument_id, side, entries[], stop, tps[], sizing, expiry, reduce_only_exit}`，子结构键按 execution-interface §3.1 / R5。
6. **G1 待补（下轮 verify 点）**：`claimed_outcome.kind` / `reconstructed_outcome.kind` 的**枚举取值域**尚未在任何契约里定义。G1 须在下轮 note 申报取值全集（连同 `kind` 与 `author_claim_state` 的映射关系），由 G0 并入本节冻结。未申报前，G3 不得对 `kind` 做字面量分支。
7. **G3 义务**：读 Decimal 列后不得为图方便统一 `.cast(pl.Float64)` 再回填任何哈希输入；特征值本身可用 float（特征不进 `trace_hash`）。

**属主**：G1（D-07/D-09）。**验收**：`load_episodes('fixture-v1').schema` 中上述列全为 `Decimal(precision=38, scale=12)`；`Decimal("6")` vs `Decimal("6.000000000000")` 哈希一致的测试通过；G0 OR-04 的 `build_request` 不再因 fraction/price 类型报错。

#### 9.10.2 裁定 A9（答 CR-07）：只立 **G1 → 下游** 的最小失效协议；跨模块 ack 与备份恢复**不在 P1**

**裁定**：CR-07 的"跨模块回执"若按全量做，等于要求 G2/G3 实现分布式缓存失效与确认通道，P1 阶段成本远大于收益。G0 只立**最小充分协议**，其余显式推迟：

1. **单一发布指针 + 读前校验**：已由 §9.8 A6 立契（manifest `status=published` + 逐文件 sha256 + tombstone 拒读，缺失一律 `LookupError` 不降级为空表），**这就是失效信号本身**——下游读到 `LookupError` 即知上游制品已失效。本节不重复。
2. **published 版本不可原地变更**：任何内容变化必须发新 `graph_version`；旧版只能置 tombstone（带 `tombstoned_at`、`reason`、可选 `successor_graph_version`），**不得**保留同名但内容不同的制品。（§9.6/S10 已有"同名 graph_version 输入不同拒写"，本条把它提升为跨模块义务。）
3. **新增：可发现的版本索引** `gold/_manifest/index.json` —— `[{graph_version, status, built_at, tombstoned_at?, successor_graph_version?, input_hash}]`，按 `built_at` 升序。下游（G2/G3/G0）由此发现"当前可消费版本"，不得靠猜文件名或扫目录。属主 G1，随发布原子更新。
4. **消费方的"回执"就是缓存键**：G3 的 `feature_snapshot` 缓存键**必须**包含 `graph_version` 与 `derivation_hash`；G2 的 `ExecutionRequest` 已含 `graph_version` 且进 `trace_hash`。上游换版 ⇒ 键变 ⇒ 旧结果自然不被复用。**P1 不引入任何 ack 通道**，因为内容寻址已覆盖同一语义且无状态。
5. **禁止绕门**：见 §9.8 A6 第 4 条；本节补一句——G2/G3 缓存中若存在指向已 tombstone `graph_version` 的条目，**读到即丢弃**，不得回退使用。

**不在 P1（列为需用户决定/P2）**：制品备份与恢复协议、`retention_deadline` 的实际清理动作、跨机复制。三者都牵扯保留期与 `consent_id`（用户闸门"数据保留与撤回"），G0 不单方裁定，见 §9.10.6。

#### 9.10.3 裁定 A10（答 A04）：远端数量级规则维持 G1 的**更严**口径，但必须可追溯归因

**G1 实现**：区间入场的**任一端** `|ln(x/ref)| ≥ ln3` 即判 `UNIT_SCALE_CONFLICT`（比"两端都超才算"更严）。

**裁定：准，维持更严。** 理由是**错误代价不对称**：单位/数量级错判会把方向与价位整体搬走，污染的是 θ 本身且**静默**；而误隔离只是少一条种子，且在损耗表里**显式可见、可回收**。P1 合成阶段宁严勿松。

**约束（本条为准入条件，不是建议）**：

1. 隔离记录必须能区分触发分支：`quarantine.field_path` 指到具体端（`order_plan.entries[i].price_lo` / `price_hi`），并在 `observed_value_ref` 或 payload 记 `basis ∈ {far_end_only, near_end_only, both_ends}`。
2. 损耗表第 5 层（`market_check`）的 `primary_reason_dist` 须能分出 `far_end_only` 分支的条数，使"更严口径多筛掉了几条"可被计数。
3. **复议条件**：P2 真实数据上若 `far_end_only` 分支的排除量占 `market_check` 层排除量 >20%，G1 须回报 G0 复议，届时可能降级为"告警不隔离"。P1 不复议。

#### 9.10.4 裁定 A11（答 codes.py）：**确认**纯别名，`reasons.py` 为唯一真身

**取证**：`.venv-g0` 实跑 `quant_lab.data.codes.ReasonCode is quant_lab.data.reasons.Reason` → **True**；`Reason` 成员 29 项 = §4 的 28 项 + §9.5 追加的 `EDIT_ORIGINAL_UNAVAILABLE`，无缺无多。

**裁定**：

1. `reasons.py` 是原因码与严重度的**唯一真身**；`codes.py` 只做**纯别名再导出**，不得有独立定义、不得有分支逻辑。这与 §9.5「`quant_lab.data.codes.ReasonCode` 补 `EDIT_ORIGINAL_UNAVAILABLE`」一致。**G0 此前"删除 codes.py"的口头指令到此作废**，以本条为准（已记 changeLog，避免两条相反指令悬空）。
2. 别名的**规范名**：`codes.ReasonCode`（= `reasons.Reason`）、`codes.FATAL_REASONS`（= `reasons.FATAL`）、`codes.Layer`。G0 实跑发现 `codes.FATAL` 不存在而 `codes.FATAL_REASONS` 存在——**以 `FATAL_REASONS` 为规范名**，`reasons.FATAL` 为真身名，两名指同一 frozenset。
3. **必须有身份测试**（G1 侧，D-07 或 smoke）：断言 `codes.ReasonCode is reasons.Reason` 且 `codes.FATAL_REASONS is reasons.FATAL`，并对 §4 的 29 项做逐字集合比较。别名一旦退化成拷贝，两处枚举会静默分叉。

#### 9.10.5 裁定 A12（答层 2 同频道同文折叠）：**不自动折叠**，但**必须同簇**

**冲突**：合并稿 A.1「重发不增种子」 vs review-G1-P1 S05「折叠需身份证据」。

**裁定：采纳 review S05——层 2 不自动折叠，只建 `repost_same_channel` 候选待人工/审计确认。** 理由同样是代价不对称：错误折叠**不可逆地**销毁 episode（两个真实机会被并成一个，损失无法从下游恢复）；不折叠只是种子数偏多，是**可测量、可在下游吸收**的偏差。

**但 A.1 的目标不被放弃——它在正确的层被满足：**

1. **同簇约束（新增，规范性）**：未确认的 `repost_same_channel` 候选，其成员**必须**被赋予**同一个 `cluster_id`**（与 `duplicate_group_id` 一致）。理由：`cluster_id` 是 G3 折内簇计数、块 bootstrap 与权重的单位；同簇 ⇒ 即使它们仍是两个 episode，**有效样本量不膨胀**，θ 的置信区间不会被重复计数虚假收窄。这是"重发不增种子"在统计层的等价实现，且是**保守**方向（宁可低估有效 n）。
2. **审计吸收**：偏多的种子数由 D-09 抽审吸收——`repost_same_channel` 候选进入独立的 `audit_stratum`，抽审给出该层的确认折叠率，写进损耗表映射账（一入多出/多入一出走 `mapping_hash`）。
3. **报告义务**：D-11 的损耗表报告须单列"未确认重发候选数"与"抽审确认折叠率"，使 P2 能据证据决定是否放开自动折叠。
4. **不得**以"反正会同簇"为由放松层 2 的证据要求，也不得反过来以"没有身份证据"为由让候选散落到不同簇。

#### 9.10.6 `order_plan` 快照结构的 fraction 同步（对齐 execution-interface §5.10 B8）

`gold/episode.order_plan` 快照中 `entries[].fraction` 与 `tps[].fraction` 为 **`decimal?`（可空）**：原文未给分配比例时 G1 **保持 null，不得均分猜值**（与 review-G1-P1 S06 一致），由 G2 的 `ExecutionPolicy` 按 `entry_fraction_rule` / `tp_fraction_rule` 兜底并在 `ExecutionResult.fraction_source` 标记来源。类型仍受 §9.10.1 A8 约束（`Decimal(38,12)` 可空，不得 `Float64`）。详见 `execution-interface.md` §5.10。

#### 9.10.7 本轮标记为「需用户决定」的事项（G0 不硬裁）

1. **制品备份/恢复与保留期清理协议**（CR-07 剩余部分）：牵涉 `retention_deadline` 的真实删除动作与 `consent_id` 撤回语义，属用户闸门"数据保留与撤回"范畴，P1 不实现，P2 前需用户拍板。
2. **`risk_acknowledged_by_user`**：D-09 产出的数据集元数据现固定为 `false`；何时、由谁翻为 `true` 是用户决定，任何窗口不得自行翻转。

#### 9.10.8 裁定 A8 附则：`estimand` 词汇表冻结，`"main"` 不是合法 estimand（OR-04 取证）

**取证**：OR-04 上轮的 `StructFieldNotFoundError: 'main'` 归属已定位——`quant_lab/research/evaluator.py:69` 的 `freeze_opportunity_set(episodes, *, estimand: str = "main")` 去读 `eligibility_by_estimand` 的 `"main"` 字段，而 G1 真实 `gold/episode` 的该 struct 键集为 `{description, entry_decision, execution, original_entry, price_check, outcome}`，**没有 `main`**。它在 G3 单测里不报错，只因 `quant_lab/research/synthetic.py:172` 的桩自造了 `{"main": True}`——**桩与真实上游不同构，正是 §9.8 A6 第 4 条警告的"因为用了桩所以永远绿"**。

**裁定**：

1. `estimand` 的合法取值域 = §9.10.1 A8 第 5 条冻结的六个键，**`"main"` 不在其中，即刻废止**。
2. **G3（R-06 属主）**：`freeze_opportunity_set` 的默认 `estimand` 改为 **`"entry_decision"`**（机会集的语义是"当时存在可判定的入场决策"），并对不在六值域内的 `estimand` 抛 `EvalProtocolError`，不得让 `StructFieldNotFoundError` 冒泡。
3. **G3**：`synthetic.fake_episodes` 的 `eligibility_by_estimand` 必须产出**完整六键** struct，与 G1 真实 gold 同构；桩与真实上游的 schema 差异一律视为 G3 侧缺陷。
4. **G0（OR-04）**：端到端冒烟必须用 G1 真实 `load_episodes` 输出而非 G3 桩来驱动 `freeze_opportunity_set`，本条已在本轮实跑中生效。

**验收**：`freeze_opportunity_set(load_episodes('fixture-v1'))` 不再抛 `StructFieldNotFoundError`；非法 estimand 有一条断言测试；`fake_episodes` 与 `load_episodes` 的 `eligibility_by_estimand` schema 逐键相同。

### §9.10.9 裁定 A13：`eligibility_by_estimand` 六键可空性（G0 OR-02 R11，changeLog #26）

**取证**：OR-04 `test_seam_g3_opportunity_set_frozen` 由 R6 的 PASS 回退为 ERROR：`evaluator.py:91 EvalProtocolError: eligibility_by_estimand.outcome 含 null`。G1 真实 gold 23 行中 `outcome` 键 23/23 null（其余五键 0 null），G3 在 R10 后把六键全部改为非空校验。契约 §9.10.1/§9.10.8 只冻结了键集与 Boolean 类型，未写可空性，双方各自合理推断导致接缝断。

**裁定**：
1. **六键一律非空 Boolean**（属主 G1）。语义是"该 estimand 下此 episode 是否合格"，`null` 会让"不合格"与"没算"不可分。`outcome` 键在 gold 发布时按生命周期状态填值：已有 `claimed_outcome` 或 `reconstructed_outcome` 且通过 §9.1 判定表 → `true`，否则 `false`；**禁止 null**。
2. **G3 只对被请求的 `estimand` 键做非空校验**（属主 G3）。`freeze_opportunity_set(..., estimand=k)` 校验 `k` 键无 null 即可；其余键的 null 记为 `EvalProtocolWarning`（进 report 诊断，不抛）。理由：单个上游键缺陷不应阻塞与之无关的 estimand 评估，且 OR-04 主链路只用 `entry_decision`。
3. 两条同时生效、互不等待：任一侧落地，OR-04 该接缝即通；两侧都落地后 G3 的 warning 计数应为 0。

**验收**：`load_episodes('fixture-v1')` 六键 null_count 全 0；`freeze_opportunity_set(load_episodes('fixture-v1'), estimand='entry_decision')` 不抛；G3 单测覆盖"非请求键含 null 只 warning"。

### §9.10.10 裁定 A14：A13 澄清 + `outcome.kind` 枚举冻结 + 别名解析语义（G0 R17，changeLog #27）

1. **A13 澄清（采纳 G1 提案）**：`eligibility_by_estimand.outcome` 在**决策视图**恒为 `false`——"是否已有终态"是 t_dec 之后才可知的信息，进入决策视图即前视；作者终态只在**描述视图**里体现（有 `close_claimed / cancel / expire` 之一 → `true`）。G3 对决策视图请求 `estimand='outcome'` 应得到空机会集并在 report 里标 `estimand_not_applicable_to_decision_view`，不是错误。A13 第 1 条"按生命周期填值"仅指描述视图。
2. **`claimed_outcome.kind` 冻结** = `{close_claimed, cancel, expire}`，与 §2 状态机映射：`close_claimed → author_claim_state=claimed_closed`；`cancel → author_plan_state=cancelled`；`expire → author_plan_state=expired`。
3. **`reconstructed_outcome.kind` 冻结** = `{filled_closed, unfilled_expired, stopped, tp_hit, right_censored}`（属主 G1 写列，取值来源 G2 `ExecutionResult`）。**G2 义务**：`execution-interface` 的结果终态标签集必须能无损映射到这五值，映射表写进 `ExecutionResult` 文档并加一条枚举覆盖测试；映射不满射或多对一有歧义时 block 给 G0。
4. **别名解析语义**：`load_episodes('fixture-v1')` 解析到 `gold/_manifest/_alias.json` 当前指向的**最新不可变版本**（现 `fixture-v1@f5191444`）；显式 `name@hash` 固定旧版；同名时以 `_alias.json` 为准。消费方（G3、OR-04）缓存键必须用解析后的不可变版本名，不得用别名。

### §9.10.11 裁定 A15：`reconstructed_outcome.kind` 扩为七值 + 混合出场诊断（G0 R18，changeLog #28）

G2 按 §9.10.10 第 3 条做映射表时报 2 处不满射 + 1 处歧义并 `block`（`docs/adr/report-G2-outcome-kind-mapping.md`），流程正确。逐条裁定如下，**§9.10.10 第 3 条的五值集合作废，以本节七值为准**。

#### A（不满射，准）：增 `unevaluable`

`censor_reason ∈ {MARK_STALE, BAR_GAP, FUNDING_SCHEDULE_GAP, RULE_HISTORY_MISSING, SYMBOL_TIME_INVALID}` → `kind=unevaluable`，**不得映射为 `right_censored`**。

理由：`right_censored` 的语义是"数据齐全，标签尚未成熟"，是关于**世界**的事实；上述五种是"我们没有看世界所需的数据"，是关于**我们**的事实。二者合并会让行情湖的缺口在损耗表里伪装成正常的标签未成熟，缺口越大看起来越像"很多仓位还没走完"——这正是 §9.8 A6 与合并稿 C.3"损耗可数、原因互斥"要堵的静默损坏。

**配套义务（G1）**：`reconstructed_outcome` struct 增 `censor_reason string?` 键，原样保留 G2 的取值，使损耗表能按互斥原因分别计数；`kind=unevaluable` 时该键非空。

**配套义务（G3）**：`kind=unevaluable` 的机会按**覆盖失败**从分母中剔除并单独计数（`n_coverage_excluded`），**不得**并入 `n_censored_excluded`。两者混记会让 θ 的有效样本量来源不可审计。

#### B（不满射，准）：增 `rejected`

`fill_status == none` 且事件含 `rejected`（PRICE_FILTER / LOT_SIZE / MIN_NOTIONAL / MARGIN / POST_ONLY_CROSS）→ `kind=rejected`，**不得归入 `unfilled_expired`**。

理由：`unfilled_expired` 是"挂出去了，市场没来"，是策略信息；`rejected` 是"根本没挂出去"，是计划质量或账户约束信息。合并后，一批系统性不可执行的计划（档位不合法、名义额不足）会呈现为正常的未成交率，计划缺陷被市场噪声吸收。诊断细分用 `canonical_events` 的 `rejected.reason`，不再拆枚举。

#### C（歧义，采纳 G2 提案并补诊断）：止损参与即 `stopped`

先部分 TP 后 SL 平余仓（E12）→ `kind=stopped`。保守口径：标签偏向不利结局，避免把"吃到一档 TP 后被打掉"记成胜局。

**但不得丢信息**：`ExecutionResult` 增诊断列 `exit_legs`（出场成交实际参与的 leg 集合，排序去重，如 `("sl",)` / `("sl","tp")` / `("tp",)`），进 `RESULT_SCALAR_COLS`。`{sl}` 与 `{sl,tp}` 由此可分，G1 的作者声称比对与 G3 的分层都能还原混合出场，不必为此再扩枚举。

#### 冻结结果

`reconstructed_outcome.kind` = `{filled_closed, unfilled_expired, stopped, tp_hit, right_censored, unevaluable, rejected}`（七值）。

**验收**：`quant_lab.market.contract.outcome_kind(res)` 对全部 22 个夹具 + `test_review_p1*` 反例逐一命中恰好一条规则；枚举覆盖测试断言七值全部可达（不可达值须在报告里说明原因）；`simulate_batch` 输出 `outcome_kind` 与 `exit_legs` 两列；映射表由 G2 写进 `execution-interface` §3。G1 侧 `censor_reason` 键与 G3 侧 `n_coverage_excluded` 各加一条断言测试。

### §9.10.12 裁定 A16：`task.py set-verify` 批准 + 验收门自改护栏（G0 R20，changeLog #29）

G2 为修 M-10 假绿，给 `scripts/task.py` 增了 `set-verify` 子命令（同一目录锁 + 原子替换 + `verifyHistory` 留痕，纯标准库，向后兼容）。该缺口是 G1 在 D-01 就提出、G0 应做而未做的（看板铁律禁止手改 JSON，但 CLI 没有改 verify 的入口，窗口自己改不了）。**G0 批准该实现并追认**；`scripts/task.py` 自此与 `contracts/` 同属 G0 审查范围，窗口修改需在看板 note 声明。

**但它打开了一个新风险面**：窗口现在能改自己的验收门。审查压力下"把门改松"比"把代码改对"容易得多，且不留下红灯。故立护栏：

1. **只准收紧或修正判读口径，不准放宽覆盖面。** 允许：负向 grep → 正向判读、模糊匹配 → 精确文件名、补断言、提高阈值。禁止：删断言、降阈值、缩小夹具范围、把实跑换成文件存在性检查。
2. **`--why` 视为必填**（当前 CLI 可选）。理由须写清"原命令为何不可靠"，不接受"调整"这类无信息说明。
3. **G0 每轮评审必须核 `verifyHistory` 全量新增项**，逐条判定属收紧还是放宽；放宽或理由不充分的**一律回退并判该模块 fail**。
4. **放宽性变更须先 `block` 给 G0 裁定**，不得自行执行。
5. 凭被放宽的 verify 置 done 的任务，发现即回退到 `doing`（§9.9 A7 同款处置）。

**验收**：`verifyHistory` 每条含 `at / old / why`；G0 评审 note 出现 "verifyHistory 核对：新增 N 条，收紧 N 条，放宽 0 条" 一行。


### §9.10.13 裁定 A17：验收门可以委托给模块函数，但必须被反例钉死（G0 OR-02 R27，changeLog #34）

**起因**：G3 把 R-08 的验收断言从看板里的一长串内联 shell-python 移进 `quant_lab.research.nullmodel.verify_report_text()`。这暴露了 §9.10.12 A16 的一个缺口：A16 靠审 `verifyHistory` 防止窗口放宽自己的门，而**门的逻辑一旦搬进模块代码，窗口日后改那个函数就不经过 `verifyHistory`，G0 的审计看不见**。

**G0 不禁止这种委托**——把四十行内联脚本换成有测试的函数是更好的工程。但委托必须连带三项义务：

1. **声明**：verify 委托给模块函数时，在看板 note 写明函数全名，视同 verify 变更（A16 同款收紧/放宽审查）。
2. **反例钉死**：必须有测试**损坏产物**（篡改计数、删关键章节、改限制声明措辞等）并断言门**失败**。没有反例测试的门等同没有门——门被掏空时不会有红灯。
3. **测试须从 `taskList.json` 读取 verify 串本身**，不得在测试里复写一份。否则看板命令被削弱时测试照样绿。

**本轮认定（A16 审计）**：`verifyHistory` 累计 2 条，**收紧 2 条，放宽 0 条**。
- `market M-10`：负向 grep → 正向判读最后一条终裁行。收紧。
- `research R-08`：内联断言 → `verify_report_text`。**内容上是显著收紧**（新增机制唯一性、计数与档分布一致性、诊断有限性、shuffle guard 全真且失败计数为零、非全 T0、块长与阳性计数、CP 区间独立复算、限制声明**正文**须实质解释"未检出≠无增益"与"合成不是真实"，而非旧版仅匹配标题）。功效 ≥80% 的硬门移除**不是窗口自行放宽**，而是用户已裁定的出口 A（限制声明式闭合），函数 docstring 明写"不以低功效阻断出口 A"，与裁定一致。

G3 **在本裁定之前已自行满足上述三条**：`tests/research/test_review_p1_round3.py` 有参数化反例（含 `limitation` 篡改）断言门退出码非零、有正例断言真实低功效报告通过、且**从 `taskList.json` 读取 verify 串在子进程中执行**。第 3 条的写法是 G3 发明的，本裁定予以采纳并推广至三窗口全部 review 类与报告类 verify。


### §9.10.14 裁定 A18：声明了精度的字段路径上禁止有损中间转换（G0 OR-02 R31，changeLog #37）

G1 四审报 **V01**（Decimal 落盘前绕回 float）。G0 实地核实，结论比"属实/不属实"更细：

- **落盘 dtype 合规**：当前 `fixture-v1@0c51102b` 的 `order_plan.{entries.price_lo/price_hi/fraction, stop.price, tps.level/fraction, sizing.qty}` 与 `dec_stop` 全为 `decimal128(38,12)`。
- **但值在中途经过 float**：`extract.py:643-644`、`validate.py:99-100` 把 `stop` / `tps.level` / `fraction` 转成 `float` 再回写。
- **当前夹具看不见**：147 个 TP 取值中小数位 >4 的为 **0**（都是解析自文本的短小数，float 能精确表示）。
- **但损失是真实的**：G0 实测 `Decimal("64094.879166666667")` 经 float 往返变为 `64094.879166666666`——**第 12 位小数被改写**。BTC 量级（1e4–1e5）下 float64 只剩约 11 位小数精度，而 schema 宣称 12 位。

**裁定**：**在声明了精度的字段路径上，禁止任何有损中间转换。** 从解析到落盘全程用 `Decimal`（或字符串），不得绕经 `float`。

**理由**：声明的类型是一个**关于保真度的承诺**。绕经 float 后，schema 仍然宣称 12 位小数，而末位已是浮点噪声而非源数据——**契约看起来被遵守，实际已被违反**，且下游无从分辨。这与 §5.16 B14 的定级一致：属"产出看似合理的错误数值"类，是本族最重的一档。

**关于不可达**：与 S24 同款处置——当前夹具全是短小数所以摸不到，但任何计算得到的价位（中值、按百分比推的 TP）或高精度源一旦出现即触发。§5.16 B14 第 3 节已定：**不可达不是不修的理由**。

**这是同一元模式的第三个上下文**（前两个见 §5.12 B10 的兜底分支、R27/§5.16 的 `or` 兜底）：**用一个看似合理的值悄悄替换真实值，且不发出任何信号。** 三者形态不同——兜底贴标签、假值被静默修补、有损类型转换——但危害机制相同。凡属此类，一律按 B14 最重档定级。


### §9.10.15 裁定 A19：含"待回填"标记的报告，终裁行无效（G0 OR-02 R34，changeLog #40）

**起因**：G2 八审（`review-G2-P1.md`）落盘时表头写着"下表暂承接七审状态，S24–S26 待本轮独立核验；最终计数与证据稍后回填"，同时文件末尾已有 `八审终裁：insufficient`。即**终裁行已存在，而它所依据的核验尚未进行**。

**不禁止"先落盘后探针"**——G2 采用该策略是为了让审查任务在悬死时仍留下部分成果，这个理由成立（前若干轮确实多次悬死）。要堵的是它的副作用：**占位终裁被 §9.9 A7 的正向判读当成权威结论**。本次恰好是 `insufficient` 所以无害；若占位行写的是 `pass`，M-10 就会被一份未做核验的报告放行。

**裁定**：
1. 报告中出现未完成标记（`待核验` / `稍后回填` / `待本轮独立核验` / `TODO` / `占位` 等）时，**该文件的终裁行一律无效**，review 类 verify **必须判 fail**，无论终裁行写的是什么。
2. review 类 verify **必须增加未完成标记检查**，与终裁行正向判读**串联**：先确认无未完成标记，再判读终裁行。
3. 采用"先落盘后探针"的审查任务，任务书须要求**终裁行最后写入**；在证据回填完成前，文件中不得出现任何形如终裁的行。

**与 §9.9 A7 的关系**：A7 立的是"正向判读终裁行"，本条补的是"终裁行何时才算数"。二者合起来才完整：**一个可判读的结论，必须同时是一个已完成的结论。**

**本轮适用**：八审终裁 `insufficient` **不可解除**。§5.12 B10 第 3 节的解除路径要求造成 insufficient 的**全部**条目均属"环境结构性不可验且显式非阻断"；本次的成因是 **S24–S26 的核验未进行**，属工作未完成，不在解除范围内。（M-03 禁网那一项仍是可解除的，且 G0 已在 R23 亲跑解除。）


### §9.10.16 裁定 A20：A19 的标记集合以显式声明为准，不得靠散文关键词猜（G0 OR-02 R36，changeLog #42）

#### 1. 「占位」不得作为未完成标记 —— 准 G2 所请

G0 核实：`review-G2-P1.md` 正文两处把「占位」用作**合法业务词**（"内部用占位 `Rules()` 承载未知乘数"、"应显式登记占位值"），与 §5.18 B16 的"占位期数值"同义。若按 A19 字面把裸词「占位」纳入标记集，该门将**永远红在错误原因上**，把真实终裁盖住。

**一条永远红的门与一条永远绿的门同样无用，而且更坏**：它把真信号淹没在假警报里，人会很快学会忽略它。**准**：标记集合限于明确表示工作未完成的词——`稍后回填` / `待回填` / `暂承接` / `待本轮独立核验` / `待补充证据` / 行首 `TODO`。

#### 2. 但关键词匹配本身是权宜之计，须升级为显式声明

在散文里猜完成度，两个方向都脆：**假红**（如本例的「占位」）与**假绿**（换一种没列进清单的措辞即可绕过）。清单永远追不上措辞。

**裁定**：自九审起，review 类报告**必须携带一行机器可判读的完整性声明**，取值封闭：

```
证据完整性：完成 | 待回填
```

review 类 verify 改为：**先读该声明必须为 `完成`，再正向判读终裁行**；关键词清单保留为兜底，但不再是主判据。声明缺失视同 `待回填`（沿用 §9.9 A7"无终裁行=未通过"的同款处置）。

**理由**：判定条件应当由被判定方**显式声明**，而不是由判定方从自然语言里推断。前者可精确、可突变自证；后者永远在假红与假绿之间取舍。

#### 3. 更正 G0 在 R34 的记录

R34 记"八审终裁 insufficient、不可解除"。G2 更正：那是**写作中途的快照**，八审现已完整（**319 行、`八审终裁：fail`、未完成标记 0**，G0 已复核）。**该轮实际终裁为 `fail`，R34 的 `insufficient` 记载作废。**

**但 A19 不因此撤销**：那个中途状态**确实短暂存在过**，而 verify 随时可能正好在那一刻运行——A19 防的就是这个时间窗，与该轮最终结论无关。

#### 4. 记录一次撤回（列为规范行为）

G2 在上一条消息中自陈：其此前所说"两族都横扫了一遍"**当时并不属实**，并在真正横扫后又揪出两处漏网（`validator` 中 `t_start` 推导的第二处表达、loader 按 `(b−a)/interval` 取整的期望 bar 数），已一并抽成单一来源（`derived_t_start`、`derived_window_s`、`first_grid_point`/`grid_points_between`）并加穷举对照测试。

**主动撤回自己先前的过度声明，是本协作体系正常运转的必要条件，不是可选的美德。** 审查制度的全部效力建立在"自报可被采信到值得去核验"之上；一次未被撤回的过度声明，会让此后所有自报都需要从零核验。自此明确：**发现自己先前的陈述不实，必须主动更正并说明差异**，G0 不因更正本身降低评价。


### §9.10.17 裁定 A27：带抽样方差的门必须按方差校准，不得用固定绝对容差（G0 OR-02 R49，changeLog #51）

#### 1. 起因：一个在惩罚估计噪声、而非结构破坏的门

G3 在 MC8 中发现并上报（看板 `research` note 13:18）：S10 结构门用**固定绝对容差 0.25** 判跨品种块相关，而该统计量的**逐 replicate 估计噪声极大**——同一生成器 12 次抽样落在 **0.076–0.447**，均值 0.29 对原面板 0.31，**无偏**。后果是 MC8 四个空机制**全部**被判 `invalid_null_model`（`cross_instrument` 每千次失败 186–824 次）。

**该门惩罚的是估计噪声，不是结构破坏。**

#### 2. 与"无法失败的检查"互为镜像，列为同族的第二种形态

| 形态 | 表现 | 后果 |
|---|---|---|
| §10 A22 / §12 A24 | 门**永不触发**（无法失败） | 骗人相信"已经检查过了" |
| **本节 A27** | 门**因噪声触发**（几乎总在响） | 真信号被假警报淹没 |

二者都使门**不携带信息**。§14 A26 已述其共同归宿：**假警报的最终下场是被人关掉**——而关掉之后，连那条本可以有效的门也没有了。

#### 3. 裁定

1. **凡判据统计量带抽样方差的门，其阈值必须按该方差校准**，不得用固定绝对容差。校准带须由**生成器自身的重复抽样**得出（G3 采用的 `DependenceReference` 方向正确），且**校准用的种子段必须与验收 replicate 不相交**，否则是用同一批随机性既定阈值又验收。
2. **该修法是"统计口径修正"，不是放宽**，但**必须能被区分**。判别方式（§14 A26 的统计版）：**注入一次真实的结构破坏，新门必须仍然触发**。仅证明"假警报消失"不足以说明门还活着——那与把阈值调宽到永不触发无法区分。
3. 修改此类门须按 §9.10.12 A16 记 `verifyHistory` 并注明校准依据；G0 审计时以第 2 条的注入结果为准，**不以"修前红、修后绿"为准**。

#### 4. 附记：G3 对 estimand 结论的独立佐证

G3 用**独立数据与独立实现**（8040 条路径、H=5d、真实 BTC 波动、67 条计划含 40 条三档梯）得到与 G2 同向的结论：口径 A **丢弃的不是随机样本，而是正在赢的宽括号单**——被删失批在 B 下均值 **+1.66R**（已实现部分已 +0.72R），末档 TP 中位 **17.96%** 对保留批 **5.57%**；且**同一候选规则的 θ 在两口径下方向改变**。

**两个窗口用不同数据、不同实现、独立得出同一结论**，是 §5.20 B18 定 B 为主口径的有力佐证。**独立复现比任何一方增加样本量都更有说服力**——这与 §14 A26 第 3 节"突变自证的价值在选点独立性"是同一条道理。


### §9.10.18 裁定 A31：受门判定的制品必须新于门的代码，且该关系须可机械断言（G0 OR-02 R56，changeLog #57）

#### 1. 事实（G0 实跑，测量时刻 05:58Z，research 树最近写入 05:43Z，**已出写入窗口，可判**）

`tests/research/test_review_p1_round3.py::test_S17_real_low_power_truthful_fail_accepted` **持续失败**。根因：

| 文件 | mtime |
|---|---|
| `docs/adr/report-G3-null-model.md`（被判的制品） | **13:55:23** |
| `src/quant_lab/research/nullmodel.py`（判它的门 + 被校准的代码） | **14:14:19** |

**门的代码比报告新 19 分钟。** 报告仍由**校准前**的代码生成，其中 `cluster_heavy_tail` 与 `nonuniform_density` 两个机制带 `AGGREGATE_DEPENDENCE_NOT_PRESERVED` 且 `verdict=invalid_null_model`——**正是 §9.10.17 A27 要消除的那种假警报**；另有多个机制 `n_invalid_null_model` 非零（10 / 7 / 4 / 2），而 `verify_report_text` 要求其为 0。

**A27 的校准落进了代码，制品没有重新生成。**

#### 2. 这是 §9.8 A6 的第三个上下文

A6 原文（为 G1 的 gold 发布门而立）：**收紧上游读取前置条件必须在同一次交付内重建并发布制品；改门不发布 = 契约漂移，判 fail。**

- 第一处：G1 改 gold 发布门未重建 manifest（R4）；
- 第二处：G1 改产线未以别名重发布（R38 前后多次）；
- **第三处（本节）：G3 校准 S10 结构门未重生成 null-model 报告。**

**注**：A6 原文写的是"收紧"，本例是**校准**（既非收紧也非放宽）。**形态相同**——门变了、制品没跟，于是 verify 因**已不复存在的原因**而失败。**裁定：A6 的适用条件由"收紧"扩为"任何改变判定结果的门变更"。**

#### 3. 裁定：制品新于门，且机械可断言

按 §12 A24 第 2 节——**同族第三次出现时，正确的反应不是更仔细地找，而是让它不可表达**：

1. **凡受门判定的制品**（报告、manifest、发布版本），其**生成时刻必须晚于门代码的最后修改时刻**；
2. **该关系须由 verify 机械断言**，不得依赖人记得重跑。两种合规实现任选其一：
   - **重建式**（G1 的 D-07 已采用）：verify 中**包含幂等重建命令**，每次 verify 都重新生成制品；
   - **断言式**：verify 中断言**制品 mtime > 门代码 mtime**（或更稳的：制品内嵌其生成时所用代码的 `sha256`，verify 比对该值与当前代码一致）。
3. **推荐断言式的内嵌哈希变体**：mtime 易被 `touch`、检出顺序、文件系统复制改变，而**内嵌代码哈希直接回答"这份制品是由哪一版代码生成的"**——这正是 §5.23 B21 所说的"让身份系统回答它该回答的那个问题"。

#### 4. 本轮处置

`R-08` 当前失败**判为制品未重生成，不判为 A27 校准有缺陷**——A27 的校准已由 G0 在 R53 以注入证据独立核验通过（整轮 100% 检出、误拒 0–1/20）。**G3 须重生成报告并按第 2 条把该关系写进 verify**；在此之前 `R-08` 维持失败，属正常。


#### §9.10.18 补充：重排版身份与生成身份必须分开（G0 OR-02 R71，changeLog #67）

四审 S17 指出 A31 存在绕过口："`--rebuild` 可洗掉旧生成哈希"。**G0 读源核实：G3 已按四审自身的 R4-V 反例修好，且其做法值得作为 A31 的规范补充。**

| 机制 | 实现 |
|---|---|
| **不重新盖章** | `--rebuild` 传入**源报告的**哈希；`write_report` docstring 明写理由——"重排版不重跑 MC，结果的来源仍是旧代码，**重新盖章会让旧结果换到新身份**" |
| **两种身份分列** | `research_code_sha256`（**生成**身份，保留源值）与 `renderer_code_sha256`（**排版器**身份，另记）——"不冒充生成身份" |
| **拒绝回落** | `MISSING_GENERATING_DIGEST = "missing-generating-digest"`，注释明写"源报告没有生成哈希时的显式占位：**绝不回落到当前源码摘要**"——即拒绝本项目反复裁过的 `or` 兜底 |
| **运行期冻结** | `frozen_digest = research_code_digest()` 于 MC 起跑时冻结，结束时确认源码未在运行期变动 |

**立为 A31 的规范补充**：**一份制品经过多道处理时，每道处理的身份必须分列，不得由后一道覆盖前一道。** 排版器改了不代表结果重算了；把排版身份写进生成字段，等于让一份旧结果获得新身份——**这与 §5.23 B21"内容哈希标识这个数是什么、不标识我们对它多确定"是同一条**：**每个身份字段只回答它自己的那个问题。**

**第四项尤其值得记**：在 MC 起跑时冻结生成身份、结束时确认源码未在运行期变动——**它堵的是"跑到一半改了代码"这个 A31 原文没覆盖的窗口**。A31 比对的是"制品 vs 当前代码"，而长时运行的 MC 中间源码可能变过，此时两端一致也不代表结果由那一版产生。


### §9.10.19 裁定 A39：机器判读的边界——它证明不了计算发生过（G0 OR-02 R72，changeLog #68）

G3 主动披露一条**它尚未堵、且 G0 未要求它堵**的限制，G0 认为这是本项目全部验证机制的**真实边界**，故立为契约条款而非留在任务书里。

#### 1. 边界陈述

本项目已建立的机器判读（内嵌代码哈希、A31 制品新鲜度、A19/A20 完整性声明与终裁判读、A24/A28 三道机制、A33/A38 取证器自证）**共同能证明两件事**：

1. **报告内部自洽**（计数、区间、诊断相互一致）；
2. **制品由当前源码生成**（生成身份与源码摘要相符）。

**它们证明不了第三件事：该计算确实发生过。** 一份**同时改写全部相关字段、又用当前代码重排版**的伪造报告，可以通过上述每一道门。

**这不是某道门的缺陷，是这一类验证的边界**：哈希回答"由哪版代码生成"，自洽性回答"数字之间是否矛盾"，**两者都不回答"这些数字是跑出来的还是写出来的"**。

#### 2. 该披露属于契约，不属于报告正文

G3 未把该限制写进报告正文，理由是**改源码会触发 §9.10.18 A31、须重跑约 45 分钟全量 MC**。**G0 裁定：该理由成立，且该披露本就不该写在报告里。**

三条依据：
1. **它是验证方法的性质，不是某一次运行的性质**——写进报告等于把一句恒真的话重复进每一份制品；
2. **它在报告内不可校验**——报告无法自证"我不是伪造的"，让它声明这一点不增加任何信息；
3. **写进契约的成本为零且只需一次**（`contracts/` 由 G0 维护，不触发 A31）。

**故：凡属"验证方法本身的边界"的披露，一律写入契约；凡属"本次运行的边界"的披露（范围、样本量、未覆盖档位），写入报告。** 判据是：**换一次运行，这句话还成立吗？** 成立则属契约。

#### 3. 指定的对抗手段：重采样比对

该边界**不能由机器判读消除**，只能由**独立重算**降低。G3 已具备该抓手：报告 JSON 内含 **world/pipeline 配置**与**每机制 seed 段**（`seed0×100000+i`），故外部方可按同一 seed 重跑抽样并逐项比对。

**裁定为 P2 前置**：任何基于真实数据的 θ 声明，**须由不生成该报告的一方按公开 seed 段重跑抽样并比对**，比对结果随声明一并提交。**属 `G-STAT-CLAIM` 实质内容。** 合成阶段（P1）不强制，但 seed 段与配置必须已在报告中公开——**该公开本身即是 P2 可复核的前提**，缺之则 P2 无从执行。

#### 4. 附记 G3 披露的 A31 第 4 点边界

运行期冻结**只覆盖父进程正常收尾路径**：父进程异常退出时不落盘，**故不会产生陈旧制品**（安全方向），但 worker 已算结果一并丢弃（代价方向）。**该边界与 §23.2"两向错误代价不对称时门须偏向廉价一侧"一致**——丢结果只费时间，落陈旧制品会污染结论。**处置正确，无须修改。**

#### 5. S08 已闭合（G0 核实）

G3 用四审自身的反例脚本原样复跑，`ledger_code_version_equal` 由 `True` 反转为 `False`。G0 独立核：`paths.research_code_digest()` 递归覆盖 `src/quant_lab/research/**/*.py`（含 `backends/`）；**当前摘要 `7294374232…` 与报告内嵌 `research_code_sha256` 逐字相同**——账本与制品可机械对上。`backend_version` 由 `None` 改为 `"polars/0.2"`（原 `get_backend(name).version` 只返回 `"0.2"`，**分不开 polars 与 polars_ta**）。

**并记其一项未被要求的细分**：code/backend 身份**进 duplicate 复用判据**（只改后端实现时旧 completed 不再被复用），**但不进 budget**（改代码不重复扣搜索额度）。**复用判据与计费判据分开，是 §5.23 B21"每个字段只回答自己那个问题"的又一次正确应用。**


#### §9.10.19 补充：重采样比对的口径必须落在结论层（G0 OR-02 R73，changeLog #69）

§9.10.19 A39 第 3 节把"独立重采样比对"立为 P2 前置，**但未规定比对口径**。G3 指出该缺口并给出正确方向，予以采纳并立为规范——**这个缺口必须在 P2 之前补上，否则该前置会以最坏的方式失效**。

**缺口的后果**：未规定口径时，自然默认是**逐位相等**。而重跑方可能用不同代码版本、不同平台浮点、不同库版本，**合法的重跑也会在末位不同**。于是这道 P2 前置会**持续对诚实的复核报红**——按 §14 A26，**假警报的最终下场是被人关掉**，届时连它本该挡住的伪造也一并放过。

**裁定：比对口径落在结论层，不在浮点位。**

- **判据**：阳性计数与 Clopper–Pearson 区间**是否落在同一判定侧**（相对 FPR 上限 / 功效下限）；
- **不要求**：数值逐位相等；
- **依据**：该检查的目的是**发现伪造的报告**，不是发现数值漂移。**一份伪造的报告会在结论层不同；一次诚实的跨版本重跑不会。** 口径必须对准它要抓的那件事。

**同版本比对**：报告内嵌的 `research_code_sha256` 正是用来钉这一点的。**重跑方代码摘要与报告内嵌值相同时**，可要求更严的数值一致；**不同时**，一律按结论层判。**该区分须写进比对协议并随比对结果一并声明。**

**附记 G3 的一次自我更正**：其原推理为"该披露 → 须进制品 → 须改源码 → 触发 A31 → 重跑 45 分钟"，因而当成成本问题上交。**错在把"该不该写"与"写在哪"混成一件事**——§9.10.19 A39 第 2 节的判据（**换一次运行，这句话还成立吗**）把链条斩在第二步，成本顾虑随之不成立，因为前提本就错了。**该判据自此适用于所有"要不要写进制品"的取舍。**

#### 9.10.20 A39 收窄：边界覆盖**对抗**，不覆盖**事故**（G0 R79，六审 R6-H）

§9.10.19 A39 立的是：面对一个能任意改写执行与验证代码的进程，机器检查**不能证明计算发生过**。六审在接受 A39 为已声明边界的同时，坚持 R6-H（worker 自报磁盘摘要不绑定已加载的执行修订）为 P1 必修。**G0 批准这个区分，并据此收窄 A39**：

1. **A39 的适用范围是对抗**。它不为**正常导入缓存、陈旧 `__pycache__`、fork 继承父进程模块对象**造成的混版免责——那些**无需恶意即会发生**，且同一机制即可挡住。把「对抗下无法证明」读成「事故也不必预防」，是拿边界当免责。
2. **磁盘摘要与执行摘要是两件事**。`research_code_digest` 回答「文件现在长什么样」，`executing_code_digest` 回答「这个进程执行的是哪一版」。A31 的运行期漂移检查继续由前者承担；回执的执行身份由后者承担。**前者不得被描述为证明了后者。**
3. **身份摘要的覆盖集必须由被标识对象决定**，不能由观察时刻的环境决定。父进程与 spawn 出的 worker 加载的模块集不同；若按「当前恰好加载了哪些」取摘要，同一份代码会算出不同摘要，门退化为假警报。现实现按 `pkgutil.walk_packages` 由包自身决定覆盖集，**此为正确形状**。

**验收边界照旧**：不要求签名体系，不要求防御任意改写全部执行与验证代码的恶意进程。目标是**消除正常导入与缓存混版**。A39 在该目标之外继续成立：独立重跑仍是 P2 前置。

（一般形式见 contracts/README.md §25.9 A41：一个字段只能回答它自己的那个问题。）
