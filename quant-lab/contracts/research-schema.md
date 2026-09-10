# 契约：research-schema（G1 data provides）

> 状态：v0 草案，G0 在 OR-01 定稿后置 `frozen`。字段来源：合并稿 B.2 / C.1 / C.3。改字段先 `block`。

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
| `symbol_raw` / `instrument_id` | string / string? | 规范化后 `BINANCE-PERP:BTCUSDT`；映射按 `available_at` 当时市场身份 |
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
