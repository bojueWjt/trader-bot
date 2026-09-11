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
