# cx batch v2（离线导出、校验与回放）

v1 parser、Grok provider 和旧录制夹具继续沿原路径运行，默认 `plan-source=parser`。
`cx_batch export` 默认导出 `cx.actions.v2`，提示词和单条回复上下文进入 record key，
旧 key 不会冒充新解析。导入的 v2 fixture 使用 `cx-batch-v2` 版本。

## 输出

模型输出 `{ "items": [{ "key": "...", "schema_version": 2, "actions": [...] }] }`。
每个输入 key 恰好一个 item；actions 可以为空。只有无法识别任何动作时才输出唯一
`op=undecidable`，缺参数不等于缺动作。

每个 action：

- `op`: open、add、reduce、take_profit、stop_loss_hit、stop_move、close、cancel、result、analysis、chatter（或整条 undecidable）。
- `time_ref`: now、past、conditional。回复明确再开才是 now open；挂单属于 now，等待额外确认的设想属于 conditional。
- `symbol_raw`、`side`（long/short）：未知为 null。
- `entry`: null 或 `{kind,price,lo,hi,levels}`。market_ref/limit 用 price；zone 用 lo/hi；ladder 用 levels。
- `levels`: `[{kind:market_ref|limit,price,fraction}]`，支持 CMP 与限价混合档。未给 CMP 数字时 price=null。
- `stop`: null 或 `{kind:price|condition,price,condition}`。condition 是本条逐字条件文字；其中价格另有 quote。
- `tps`: `[{kind:price|percent,value}]`。
- `field_issues`: `[{field,reason}]`。

所有数字都用 `{value:"十进制字符串",quote:"本条逐字连续片段"}`，避免数字与引用的平行数组错位。
percent 和 fraction 都以百分数表示，例如 10 表示 10%，50 表示 50%。不使用杠杆收益率代替价格涨跌幅。
未使用的 entry 键为 null/[]。示例只用仿写数据：

```json
{
  "op": "open", "time_ref": "now", "symbol_raw": "BTC", "side": "long",
  "entry": {"kind":"market_ref","price":{"value":"100","quote":"现价100"},"lo":null,"hi":null,"levels":[]},
  "stop": {"kind":"condition","price":{"value":"90","quote":"低于90"},"condition":"日线收盘低于90才止损"},
  "tps": [{"kind":"percent","value":{"value":"10","quote":"10%"}}],
  "field_issues": []
}
```

校验后的 `responses.jsonl` 是 `{key,response:{schema_version:2,actions,stats}}`；传输或包结构失败仍为 abstain。
校验器附加每个 action 的 `branch_index` 和原文数字 `spans`。引用必须逐字出现于当前消息；
单位换算仅接受明确的万/w/k/K，全角及空格规范化后用 Decimal 严格比较，无容差、不推断省略单位。
quote 必须覆盖完整数字及单位，不能引用 1000 内的 100、9万 内的 9，也不能从回复父帖取数字。
百分数不能冒充价格。字段拒绝后仅该数字为 null 或删除该 TP 项，其他字段和 action 保留；原因记录 `evidence_rejected:...`。
回放再次执行同样校验，缓存的 approval 或 spans 不能跳过它。

stats 分开给出 `model_uncertain`（消息数）、`field_evidence_failed`（字段数）、
`whole_message_discarded`（消息数）；成功空 actions 不算作废。

## G1

每个 action 一行 extracted_event，branch_index 和 extract_id 独立。`checks.action` 保留结构、引用、
时间指向及字段问题；不改 contracts 或 Parquet schema。

now open 映射为 entry_proposal；past/conditional open 映射为仅描述的 entry_claimed，
checks 中仍保留原始 open。它们可进入描述事件，不能生成新决策或改变决策状态。
reconciled 按同一 source 的币种/方向匹配 action，有币种时不依赖模型输出顺序；
币种缺失时回退 branch_index。成功的 LLM action 覆盖对应 parser action，不连带覆盖其他币种。
v1 录制继续按旧 source 规则选择。

百分比 TP 只有确定入场价与方向时换算；long 为 `entry*(1+p/100)`，short 为 `entry*(1-p/100)`。
区间或多个不同入场档不猜平均成交价，记录 `percent_requires_unambiguous_entry_and_side`。
条件止损当前无法用只支持 mark 价格触发的 order_plan 表达，保留原条件并记录
`condition_not_supported_by_order_plan`，不生成普通价格止损。上述映射问题令 execution=false。
缺失某个 ladder 价格时保留 action，记录 incomplete_ladder，不能用剩余档假冒完整订单。

## 命令（在 quant-lab/ 下）

所有批次、提示、原始尝试、录制和人工复核文件都放仓库外。`run` 是唯一调用真实 Codex 的命令，
以下真实解析由用户在允许联网的沙箱外执行；其他命令离线。

```bash
PY=.venv-g1/bin/python
B=/private/tmp/cx-v2-new-blind
mkdir -p "$B"

# LAKE 是已有 G1 平铺构建目录；OLD 是旧样本 JSONL，需含 text。
# 支持多个 --exclude-prompts；按文本排除旧消息，频道间同文也排除。
"$PY" -m quant_lab.data.cx_batch export --build-dir "$LAKE" \
  --sample 300 --seed 927 --exclude-prompts "$OLD" --output "$B/prompts.jsonl"

# 用户在沙箱外执行；默认 20 条/约 12k 序列化字符，先达到哪个就切批。
# 单条超长消息独占一批，绝不截断原文或动作。
"$PY" -m quant_lab.data.cx_batch run --prompts "$B/prompts.jsonl" \
  --output-dir "$B/run" --batch-size 20 --max-chars 12000

"$PY" -m quant_lab.data.cx_batch import --responses "$B/run/responses.jsonl" \
  --output "$B/recorded.json"

# SOURCE 是原始 Telegram fixture/export 目录；复用现有构建参数。
"$PY" -m quant_lab.data.api --build --fixture "$SOURCE" --out "$BUILD" \
  --llm-fixture "$B/recorded.json" --plan-source llm --graph-version cx-v2-blind
# 将 llm 改成 reconciled 可按 action 对照；省略仍为 parser。

"$PY" -m quant_lab.data.cx_batch review-sample --build-dir "$BUILD" \
  --graph-version cx-v2-blind --sample 300 --seed 927 --output "$B/review.jsonl"
```

盲审必须先锁定新样本真值再看解析，不能把模型输出/旧 parser 标签当成真值。
review-sample 是供核对结果的文件，并非盲判输入。

## 旧 300 条本机回归

默认真值位置为用户指定 scratchpad 的 review-result.json。
prepare 通过其中的 source_path 读取 review-input.jsonl，只导出消息和上下文，不导出真值/旧预测。

```bash
R=/private/tmp/cx-v2-pilot-regression
mkdir -p "$R"
"$PY" -m quant_lab.data.cx_regression prepare --output "$R/prompts.jsonl"

# 用户在沙箱外重新解析；不得复用旧 run 目录或旧响应。
"$PY" -m quant_lab.data.cx_batch run --prompts "$R/prompts.jsonl" --output-dir "$R/run"

"$PY" -m quant_lab.data.cx_regression check --prompts "$R/prompts.jsonl" \
  --responses "$R/run/responses.jsonl" --output "$R/regression.json"
```

check 输出总体/逐频道分子分母、比例、95% Wilson 区间和每条字段判定。误判分母为预测开仓消息；
漏判和字段分母为全部真开仓消息，包括作废/漏判。字段独立于操作标签评分。
只沿用真值中预先标记的缺图不可核字段排除，缺失预测不减少分母。
证据作废率、全部作废率、模型不确定率、字段失败数/受影响消息比例分别报告。
另报告有效/原始操作标签一致率，以及可由文字确定动作的作废中不合理作废率；缺图影响操作的样本不计该可核分母。
门槛固定为误判/漏判/证据作废 ≤5%，五字段 ≥95%；未达标或分母为空返回退出码 1。

旧真值有部分人工语义（略高于、分段动态计划、省略单位的解释），不能机械比较。
这些项列为 pending，仍留在原分母，达标判断按未正确计算，绝不静默略去或标正确。
独立复核后可传 `--adjudications /private/tmp/.../adjudications.json`，格式为：
`{"item-id":{"entry":{"correct":false,"reason":"独立复核理由"}}}`。
不得用 adjudications 将作废或证据拒绝的内容改成正确。check 会验证新提示 key 与原始消息对应，
拒绝未知/重复响应 key，并再次严格校验新输出的所有数字证据。
报告固定 `blind_test=false`；在这 300 条上回归过线不能证明新样本已达标。

## 离线测试与突变对照

仓库测试只用仿写文本、临时真值和 fake executable，不依赖 scratchpad 或真实 Codex。

| 测试 | 断言 / 突变 |
|---|---|
| `test_units_exact_and_wrong_value_mutant` | 万/w/k/K、千分位、全角、空格严格换算；数值 +1 必拒绝；去掉 Decimal 相等比较的代码突变被断言捕获 |
| `test_evidence_does_not_accept_partial_units_or_percentage_as_price` | 部分数字、截断单位、负号、父帖引用、百分数冒充价格必须拒绝 |
| `test_field_failure_preserves_action_siblings_and_replay_revalidates` | 一个字段失败，兄弟字段/action 保留；回放篡改值再次拒绝，统计不转为整条作废 |
| `test_multiaction_time_refs_only_now_open_decision_and_default_parser` | 多 action 各自落行；三种 time_ref 仅 now open 生成决策；把所有 open 当 entry_proposal 并移除描述隔离的突变被捕获；默认 parser 行与原路径逐行相等 |
| `test_condition_and_percent_mapping_no_guessed_basis` | 百分比价格转换、完整条件保留、条件止损不可假冒 mark 止损，execution=false |
| `test_percent_short_mixed_ladder_fractions_and_ambiguous_basis` | short 的百分比换算、CMP+limit 档位与比例保留，多档未知均价不猜 TP |
| `test_reconciled_matches_actions_without_suppressing_other_symbol` | 分支序号不同仍匹配同币种；另一个币种 parser action 不被抹掉 |
| `test_export_fake_run_import_api_build_and_nonopening_mutant` | export→fake run→import→G1 全链；移除 action 覆盖会复活错误 parser 开仓，突变被捕获 |
| `test_message_denominators_include_discard_and_wrong_label_fields` | 漏判/作废仍计真开仓分母、错标签独立评字段、缺图仅排对应字段 |
| `test_missing_responses_are_not_dropped_and_unsupported_semantics_never_pass` | 缺响应不缩分母、待复核不算正确、消息 key/原文篡改被拒绝 |

在 quant-lab/ 下验证：

```bash
T=$(mktemp -d)
export QUANT_LAB_DATA_ROOT=$T
.venv-g1/bin/python -m pytest tests/data -q
.venv-g0/bin/python -m pytest tests/integration -q
```
