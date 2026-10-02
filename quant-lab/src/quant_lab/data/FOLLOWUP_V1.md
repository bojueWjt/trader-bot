# followup-v1（离线管理指令）

当前规则版本为 `followup-v2`；输出 schema 仍是 `followup-v1`，不修改 v2 抽取契约。

从已构建图里挑 v2 `time_ref=now` 的管理消息，给每条 source 一次标注，再把校验过的指令落成 `followup_action.parquet`。
不调用 `api.build`，不改其他 pipeline 表，不把 context 里的数字当成证据。回放会重新校验原文，不信任存下来的通过标记。

候选：`extracted_event.checks` 里 `schema_version=2`，`op` 属于 reduce / take_profit / close / stop_move / cancel / add，且 `time_ref` 严格为 now。past / conditional 不进候选。同一 `source_version_id` 只导出一次，模型仍可输出多条指令。`event_time` 或 `available_at` 未知的 now 管理消息仍导出原文。可见性得到空 context、空 `candidate_root_ids`、`reply_text` 为空，不猜时间、不挂 root。`target_message_id=null` 且 `uncertain=true` 可以落审计表，`available_at` 可以为空。未知时序不是可执行数据。export 与 build 报告记 `unknown_clock_candidates`。

对话上下文 `conversation`：同频道、当前消息原始发布前最近 8 条不同 message id 的可见消息，任意类型，含无数字闲聊；每条是 `message_id`、`minutes_before`（距离当前发布时间的分钟数）、`text`（最多 300 字），按发布时间远到近。取当时可见最新版本，编辑不占第二个名额；异频道、未来发布/编辑、可用时间晚于当前消息、自身旧版本和同刻消息不出现。未知时序时为空。它与 `reply_text`、开仓摘要一样，只能用于指向，不能提供比例或价格证据。

开仓上下文 `context`：基础列表仍为同频道、21 天内（含边界）的 v2 now-open 保守候选，最多 12 条，近到远；可见性要求事件时间严格早于当前消息，`available_at` 不晚于当前消息。可见 `reply_to` 父文若是合格 open，仍在这 12 条里优先保留。再按当前原文、上述 8 条截断后的对话文本点名币种，追加每币种 60 天内（含边界）最近的一条 now-open，已在列表的 root 不重复添加，总上限 20。币种使用 `extract.canonical_symbol` 和现有别名表，包括以太/eth→ETH、大饼/饼→BTC；候选自己的中文名称（如仿写「小火箭」）也可匹配，不新增跨模块别名。追加部分按当前原文点名、前文从远到近的首次提及顺序；超限不扩展。旧开仓时间并列而不能唯一选出最近 root 时不追加。窗口外的 reply 自身不会扩展候选，只有点名规则可以扩展。

每条开仓给归一 `symbol`、`side`、`entry` / `stop` / `tps` 摘要、原始 `open_time`、`has_visible_terminal`。side 空时，仅同一 source 版本的可见开仓原文明确写出单一币种和单一方向才补齐；不借后来编辑、闲聊或其他币种的方向。缺少这种证据仍为空。

是否仍在场只看当前消息之前已经可见、且尚未失效的 episode_event。kind 本身不够。payload 必须是有效状态迁移：`action=move`，不是 invalid，并且 `to` 为 `[plan, claim]`（lifecycle 的写法）。plan 只能是 none / active / cancelled / expired，claim 只能是 unknown / claimed_open / claimed_closed。缺 plan、缺 claim，或任一端不是这些状态，都留下，不能把残缺 payload 当成终态。`to.claim=claimed_closed`，或 `to.plan` 为 cancelled/expired 且 claim 不是 claimed_open，才算根不再可能在场。cancel/expire 时 claim 仍是 claimed_open 的留下。desc、I、L、R、缺 payload、坏 payload 都留下。未来 supersede 还没生效时终态仍然算数；已经生效的 supersede 不再用来排除。不用 episode 的最终 `author_claim_state`。多个 episode 不能都证明已结束时，不排除。

上下文里的 open 是保守候选，不是已确认仓位。同一频道、同一 root 只要当时可见的版本里有过 v2 now-open，就保留该根；参数用截至当时最近的一条可见 now-open，不从后来的 past / chatter / 非 open 版本抄 open 字段。past 或 chatter 本身不新建 open，也不等于平仓。只有已验证的终态状态迁移，或原始发布时间落在对应 21/60 天窗外，才排除。未来版本既不覆盖参数，也不泄露。窗口和 open_time 按原始 `message_date`（没有则用当时可见事件里最早的 event_time），后来的编辑不刷新窗口。可见性仍看版本自己的 event_time / available_at。多个 episode 终态分歧时保留候选并标 `has_visible_terminal=true`，指向校验保持 uncertain。

## 指令

`action`：`close_all` | `reduce` | `move_stop` | `cancel_pending` | `add` | `none`。

- 止盈 / 走了 / 落袋且无比例 → `close_all`，不带 fraction。有显式比例 → `reduce`，不是 close_all。
- 减仓 / 先减且无比例 → `reduce`，`fraction_pct=null`。不补比例。
- 「80%的人要止盈」的人群百分数不算仓位比例；止盈指令仍不带 fraction。校验拒绝把这种数字当减仓比例。
- 取消挂单 / 撤单 → `cancel_pending`，不带 fraction。加仓 → `add`。只有 add 和 reduce 可以带 fraction。
- 止损原文有成本 / 保本 / 入场价 → `move_stop`，`to_entry=true`，`price=null`。不能用 entry price 代替，也不能没有这些原词就写 `to_entry=true`。这句话只看本条 `evidence_quote` 和 `stop.quote`，不用整篇里其他指令的词，也不用其他候选的入场价。
  - 校验另认可真实频道里的同义说法（`TO_ENTRY_WORDS`，10-02 起；prompt 不变、录制 key 不变）：报本、入场点/位/区/水平、进场价/点/位、开仓价/点/位、盈亏平衡、收支平衡、无风险、BE/B/E/breakeven，以及「移至/移到/调整到/设在…入场（不含入场时）」。旧词表让 Titan/高卢人/Cash/峰哥约 400 条拉保本被拒。反向检查（数字止损其实是入场价）仍只用成本/保本/入场价。
  - 只说「止损上移 / 推防守 / 移动止损」而没有去处的仍拒（`move_stop_without_destination`），不猜移到哪。
- `stop.price` 或 `to_entry=true` 只属于 `move_stop`。
- 只有评论 → `none`。
- `一半` = 50，quote 必须含原文「一半」。明确 `%` 才走百分数，50 表示 50%。比例必须是有限、非负的数，quote 必须严格引用原文，不补比例。reduce 不能超过 100%。add 没有比例上界：原文写明 150% 或 200% 时按原文保存，不截成 100。任何动作的 `pct/100` 都必须能精确落进 Decimal(38,12)，否则整条拒绝。这条精度是校验和落盘规则，不写进给模型的 prompt。sNaN 一类信号转成该条拒绝，不让整批中断。
- 数字 quote 必须是当前 text 的连续片段，并复用 `cx_v2.exact_number`（百分数 `percent=True`）。坏证据、坏类型或 target 不在本条候选根里：整条指令丢弃并记下原因，不把坏字段改成 null 后继续执行。
- 指向校验先核对候选 ID、字段和当前数字证据，再按下列规则解析目标。当前 `evidence_quote` 的点名优先于整条原文，原文优先于最近一条明确点名的前文；不把更早的多个话题混成一个目标。没有当前点名且有可见候选回复目标时优先按回复。
- 明确点名币种（含别名）：只有一条候选可以确定；多条时，原文明确方向且方向吻合的只有一条可以确定。写明方向却有两条同名同向候选，仍 `target_message_id=null, uncertain=true`，不靠时间猜。缺失 side 不算方向吻合（币种本身唯一且没有相反方向证据时仍可定位）。
- 点名但未写方向：同名多条仅取开仓时间唯一最近的一条；时间缺失或并列仍 uncertain。候选 ID 不能用于打破时间平局。
- 「所有多单保本」「手里的都走一半」等全体范围可界定时，输出每个符合币种/方向条件的候选各一条指令；共用当前原文的数字证据，不补比例。重复模型条目不重复展开。对「之前那几笔」等无法界定的子集、前文同时点名多个币种且当前未消歧、存在终态分歧的集合保持 uncertain。
- 安全确定的规则可把模型 `target=null, uncertain=true` 的有效指令补到候选，并改为 `uncertain=false`；模型无证据指定的候选会清空并标 uncertain。候选外 ID 仍整条拒绝，不能通过确定性补选救回。无点名/明确全体范围/可见候选回复目标则不猜。`action=none` 不做目标补选。
- 未解析的 `target_message_id=null` 必须 `uncertain=true`，可以落审计表。
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
# 校验规则改了而模型输出不用重跑时：用 run/raw 里保存的原始输出按当前校验重算，不调用模型
"$PY" -m quant_lab.data.cx_batch revalidate --prompts "$B/prompts.jsonl" --run-dir "$B/run" --output-dir "$B/run-revalidated"
"$PY" -m quant_lab.data.followup build --build-dir "$LAKE" --graph-version "$GV" --llm-fixture "$B/recorded.json" --output "$B/followup_action.parquet"
```

`--lake-root` 与 `--build-dir` 二选一，语义同 `cx_batch`。`build` 省略 `--output` 时写 `silver/followup_action.parquet`，旁边有同名 `.report.json`（拒绝原因、缺录制、abstain、歧义计数）。缺录制记入 report，不发明指令。

录制 JSON 的 schema `version` 仍为 `followup-v1`，`items` 的 key 是 `record_key(system, user, schema)`。规则版本 `followup-v2` 同时出现在 system 和 user；新增上下文也参与 key，旧录制不会命中新 prompt，缺录制只记 missing。重新 export/run/import，不复用旧运行目录。落盘 `rule_version` 和 instruction ID 的版本盐也递增。`cx_batch import` 对 followup 仍标 schema 版本；v2 响应仍是 `cx-batch-v2`。

贯通改动仅在 `cx_batch.quote_response` 的 followup 分支透传候选上下文到校验器；v1/v2 和其他 schema 行为不变。没有完整上下文的直接校验调用仍保留旧的字段/证据校验契约；正式 batch 和 build 都会传完整上下文。

离线仿写测试：`tests/data/test_followup.py`、`tests/data/test_followup_context.py`。新文件覆盖接续指令、60 天补选、side 空时的原文证据、同名方向/最近时间/时间平局、范围展开与反例、候选外 ID、上下文证据隔离、规则录制隔离、batch→回放。参数化变异逐条验证上限、时序、别名、目标选择与范围规则；变异只运行这两个相关文件，最后用 `.venv-g1/bin/python -m pytest tests/data -q` 完整验收一次。
