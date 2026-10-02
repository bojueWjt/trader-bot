# followup-v1（离线管理指令）

从已构建图里挑 v2 `time_ref=now` 的管理消息，给每条 source 一次标注，再把校验过的指令落成 `followup_action.parquet`。
不调用 `api.build`，不改其他 pipeline 表，不把 context 里的数字当成证据。回放会重新校验原文，不信任存下来的通过标记。

候选：`extracted_event.checks` 里 `schema_version=2`，`op` 属于 reduce / take_profit / close / stop_move / cancel / add，且 `time_ref` 严格为 now。past / conditional 不进候选。同一 `source_version_id` 只导出一次，模型仍可输出多条指令。`event_time` 或 `available_at` 未知的 now 管理消息仍导出原文。可见性得到空 context、空 `candidate_root_ids`、`reply_text` 为空，不猜时间、不挂 root。`target_message_id=null` 且 `uncertain=true` 可以落审计表，`available_at` 可以为空。未知时序不是可执行数据。export 与 build 报告记 `unknown_clock_candidates`。

上下文：同频道、发布前 21 天内（含正好 21 天）、事件时间严格早于当前消息、且 `available_at` 不晚于当前消息的 v2 now-open 保守候选（不是已确认仓位）。最多 12 条，近到远。未来编辑和未来 open 不出现。当前 `reply_to` 的可见最新父文单独给；若该父文是 21 天内的合格 open，即使排不进最近 12 也保留，总数仍不超过 12。时间窗外的 reply 只作上下文，不能当 target。

是否仍在场只看当前消息之前已经可见、且尚未失效的 episode_event。kind 本身不够。payload 必须是有效状态迁移：`action=move`，不是 invalid，并且 `to` 为 `[plan, claim]`（lifecycle 的写法）。plan 只能是 none / active / cancelled / expired，claim 只能是 unknown / claimed_open / claimed_closed。缺 plan、缺 claim，或任一端不是这些状态，都留下，不能把残缺 payload 当成终态。`to.claim=claimed_closed`，或 `to.plan` 为 cancelled/expired 且 claim 不是 claimed_open，才算根不再可能在场。cancel/expire 时 claim 仍是 claimed_open 的留下。desc、I、L、R、缺 payload、坏 payload 都留下。未来 supersede 还没生效时终态仍然算数；已经生效的 supersede 不再用来排除。不用 episode 的最终 `author_claim_state`。多个 episode 不能都证明已结束时，不排除。

上下文里的 open 是保守候选，不是已确认仓位。同一频道、同一 root 只要当时可见的版本里有过 v2 now-open，就保留该根；参数用截至当时最近的一条可见 now-open，不从后来的 past / chatter / 非 open 版本抄 open 字段。past 或 chatter 本身不新建 open，也不等于平仓。只有已验证的终态状态迁移，或原始发布时间落在 21 天窗外，才排除。未来版本既不覆盖参数，也不泄露。21 天和 open_time 按原始 `message_date`（没有则用当时可见事件里最早的 event_time），后来的编辑不把窗口刷新成新开仓。可见性仍看该版本自己的 event_time / available_at。

## 指令

`action`：`close_all` | `reduce` | `move_stop` | `cancel_pending` | `add` | `none`。

- 止盈 / 走了 / 落袋且无比例 → `close_all`，不带 fraction。有显式比例 → `reduce`，不是 close_all。
- 减仓 / 先减且无比例 → `reduce`，`fraction_pct=null`。不补比例。
- 取消挂单 / 撤单 → `cancel_pending`，不带 fraction。加仓 → `add`。只有 add 和 reduce 可以带 fraction。
- 止损原文有成本 / 保本 / 入场价 → `move_stop`，`to_entry=true`，`price=null`。不能用 entry price 代替，也不能没有这些原词就写 `to_entry=true`。这句话只看本条 `evidence_quote` 和 `stop.quote`，不用整篇里其他指令的词，也不用其他候选的入场价。
- `stop.price` 或 `to_entry=true` 只属于 `move_stop`。
- 只有评论 → `none`。
- `一半` = 50，quote 必须含原文「一半」。明确 `%` 才走百分数，50 表示 50%。比例必须是有限、非负的数，quote 必须严格引用原文，不补比例。reduce 不能超过 100%。add 没有比例上界：原文写明 150% 或 200% 时按原文保存，不截成 100。任何动作的 `pct/100` 都必须能精确落进 Decimal(38,12)，否则整条拒绝。这条精度是校验和落盘规则，不写进给模型的 prompt。sNaN 一类信号转成该条拒绝，不让整批中断。
- 数字 quote 必须是当前 text 的连续片段，并复用 `cx_v2.exact_number`（百分数 `percent=True`）。坏证据、坏类型或 target 不在本条候选根里：整条指令丢弃并记下原因，不把坏字段改成 null 后继续执行。
- `target_message_id=null` 时 `uncertain` 必须为 true，可以落盘，留给以后的 kernel，不在这里猜目标。
- `target_symbol` 与该 root 候选里已经写明的 symbol，都先用 `extract.canonical_symbol` 规范后再比。`#BTC/USDT`、比特币、大饼和 BTC 不算冲突。规范后代码不同（例如 BTC 与 ETH）则整条拒绝。候选上没有已知 symbol 时不猜测、不因此拒绝。不从消息正文用正则推断动作。

`fraction` = `fraction_pct / 100`（Decimal）。没有比例就是 null。`target_message_id` 按同 channel + `root_message_id` 对 episode。没有对上时 `episode_id` 为空，`episode_ambiguity=episode_not_found`，`uncertain=true`。多于一个根 episode 时也不挑，`episode_ambiguity=ambiguous_root_episode`，`uncertain=true`。

runner 按输入行的 schema 核验响应包络。followup 输入收到 v2 响应时记 `response_schema_mismatch`，不把另一种 schema 存进去。v1/v2 自己的成功路径不变。

`build` 会把录制里已有的 `stats.reject_reasons` 记为 `prior_validation`（审计，不是批准），再独立重验当前指令。同一 index+reason 不重复计数。`rejected` 只算这次重验新发现的，`prior_rejected` 只算录制里带过来的。

## 命令

在 `quant-lab/` 下。`run` 仍走原来的 batch runner；followup 文件不能和 v1/v2 混在同一个 prompts 文件里。

```bash
PY=.venv-g1/bin/python
"$PY" -m quant_lab.data.followup export --build-dir "$LAKE" --graph-version "$GV" --channel -100 --sample 100 --seed 0 --output "$B/prompts.jsonl"
"$PY" -m quant_lab.data.cx_batch run --prompts "$B/prompts.jsonl" --output-dir "$B/run"
"$PY" -m quant_lab.data.followup import --responses "$B/run/responses.jsonl" --output "$B/recorded.json"
"$PY" -m quant_lab.data.followup build --build-dir "$LAKE" --graph-version "$GV" --llm-fixture "$B/recorded.json" --output "$B/followup_action.parquet"
```

`--lake-root` 与 `--build-dir` 二选一，语义同 `cx_batch`。`build` 省略 `--output` 时写 `silver/followup_action.parquet`，旁边有同名 `.report.json`（拒绝原因、缺录制、abstain、歧义计数）。缺录制记入 report，不发明指令。

录制 JSON 的 `version` 为 `followup-v1`，`items` 的 key 是 `record_key(system, user, schema)`。`cx_batch import` 见到 followup 响应也会标这个版本；v2 响应仍是 `cx-batch-v2`。
