# followup-v1 审查证据

阶段：未知时钟候选完整性修正之后。相关测试和 `tests/data` 全套都已实跑。写本文件没有再跑测试，也没有改 pytest 源码。

## 范围

最终改过的文件：

- `quant-lab/src/quant_lab/data/followup.py`
- `quant-lab/src/quant_lab/data/FOLLOWUP_V1.md`
- `quant-lab/tests/data/test_followup.py`
- `quant-lab/tests/data/FOLLOWUP_EVIDENCE.md`（本文件）

`cx_batch.py` 这一轮没有改。没有改 `api.py`、`cx_v2.py`、`llm.py`、`extract.py`、`market/`。工作区里那些文件若有别的 diff，不是这次的。没有 commit。

这一轮只去掉 `plan_prompts_from_tables` 对未知 `event_time` / `available_at` 的 `continue`。v2 now 管理消息仍是候选，原文照导出。未知时序不调用 `visible_opens`，不猜时间，不挂 root：`context=[]`、`candidate_root_ids=[]`、`reply_text=None`。`_clocks_visible` 在任一时钟为 `None` 时本来就返回 `False`。past / conditional 仍不进候选。export 与 build 报告增加 `unknown_clock_candidates` 和 `unknown_clock_note`（`unknown chronology is not executable data`）。计数在抽样之前，按全部计划 prompt 算。

## 相关验证

工作目录 `quant-lab/`。解释器 `.venv-g1/bin/python`。没有设置跳过测试的环境变量，没有联网调用项目 LLM。stdout/stderr 和 `proc.returncode` 由 `subprocess.run(capture_output=True)` 写入下面两个文件，不是 shell `$?`。

```bash
.venv-g1/bin/python -m pytest tests/data/test_followup.py tests/data/test_cx_batch.py tests/data/test_cx_v2.py -q --tb=line
```

stdout/stderr：`/private/tmp/followup-related-final.log`。退出码：`/private/tmp/followup-related-final.exit`。

结果行：

```text
124 passed in 11.39s
```

退出码：0。

`test_followup.py` 是这 19 个测试。本轮只新增最后第二个，其余没有改名：

- `test_prompt_and_schema_state_the_rules`
- `test_fraction_half_percent_and_no_fill_mutants`
- `test_bad_number_partial_and_discontinuous_quote_reject_the_instruction`
- `test_to_entry_requires_the_words_and_rejects_entry_price`
- `test_target_must_be_candidate_and_null_is_not_guessed`
- `test_past_and_conditional_never_become_candidates`
- `test_window_twelve_reply_asof_channel_and_terminal_mutants`
- `test_replay_does_not_trust_a_stored_flag`
- `test_episode_ambiguity_and_decimal_fraction_mutants`
- `test_v2_prompt_hash_wire_and_schema_dispatch_stay_compatible`
- `test_export_fake_run_import_build_maps_decimal_and_misses`
- `test_mutant_cross_wire_uses_each_rows_candidates`
- `test_fraction_bounds_d12_and_field_ownership`
- `test_recorded_examples_cover_legal_actions`
- `test_terminal_payload_not_kind_and_supersession`
- `test_latest_visible_source_and_original_publish_window`
- `test_run_rejects_response_schema_that_does_not_match_input`
- `test_unknown_clock_now_messages_stay_candidates`
- `test_cli_subcommands_read_graph_and_write_silver`

`test_cx_batch.py` 与 `test_cx_v2.py` 的原断言没有改，一并通过。124 是 pytest 汇总，不是这三个文件的顶层 `def test_` 个数。全套跑在这次相关测试通过之后，中间没有再改核心源码。

## 全套 tests/data

已跑，而且只跑了这一次。工作目录 `/Users/balen/projects/trader-bot/quant-lab`。没有跳过测试的环境变量，没有联网，fixture 是仿写。同一 Python `subprocess.run` 包装器把 pytest 的 stdout+stderr 写入日志，把 `proc.returncode` 写入退出码文件。包装器打印的开始/结束时间不在 pytest 日志里。

```bash
.venv-g1/bin/python -m pytest tests/data -q --tb=line
```

stdout/stderr：`/private/tmp/followup-data-suite-final.log`。退出码：`/private/tmp/followup-data-suite-final.exit`。

开始 UTC：`2026-10-02T10:19:15Z`。结束 UTC：`2026-10-02T10:20:48Z`。

结果行：

```text
627 passed, 1 warning in 92.09s (0:01:32)
```

退出码：0。

那一条 warning 来自 `tests/data/test_tg_pull.py::test_usermethods_call_nested_resolve_and_forbidden_write`，`telethon/utils.py:1582`：`Using async sessions support is an experimental feature`。不是 followup。全套没有为 mutant 重复。全套没有失败，所以没有第二轮诊断修复，也没有偷偷再跑。

## 变异

规则：`mutant()` 必须真正构造出可调用对象，并且随后的行为断言失败（`AssertionError`），才算死亡。源码片段不唯一是 `ValueError`；编译或加载失败是 `RuntimeError`。这两种都不是死亡，包在 `pytest.raises(AssertionError)` 里也不会被当成通过。没有为了让 mutant 死而删掉运行时该留的异常防御。没有对每个 mutant 重跑全套。死亡证据是上面那一次相关测试；全套把同一批测试又包含了一次，没有按 mutant 拆开重跑。

计数（相关测试里实际构造的次数）：

- 函数 mutant 45 个。源码里直接调用 41 处，各执行 1 次。另有 `expect_dead` 里的 1 处调用实际跑了 4 次（`action!=move`、`invalid_transition`、`claim!=claimed_open`、`superseded_at<=moment`）。
- prompt 句子删除或反转 16 个（`PROMPT_MUTANTS`）。
- 合计 61。上面这次相关测试全部死亡。

本轮新增、并已死亡的 1 个：`plan_prompts_from_tables` 里未知时钟分支

```text
if unknown_clock:
    context = []
    roots = []
    reply_text = None
```

被换成 `if unknown_clock: continue`。`test_unknown_clock_now_messages_stay_candidates` 直接调用 `mutant()`，再用 `retained()` 断言来源集合仍是 `{known, unk-avail, unk-event}`。`continue` 丢掉未知时钟两条，断言变成 `AssertionError`。生成失败不会被算成死亡。

上一轮已经在死、这次仍死的：

- `visible_opens`：把「最近 now-open 的版本秩」改成「全部可见版本的版本秩」，后写的 past 会把根丢掉。`for event in opens` 改成 `for event in versions`，会把 past 版本的 open 字段抄进结果。
- `_symbol_conflict`：`canonical_symbol(symbol)` 改回 `strip().upper()`，`#BTC/USDT` 会被当成和 BTC 冲突。
- `_position_ended`：合法 plan/claim 的类型和状态检查改成恒假。缺 claim 的 `to.plan=cancelled`，以及 `to=['cancelled','garbage']`，会被当成终态。
- `_accept_percent`：`number > 100` 仍在，reduce 的 150% 把它杀死。add 不走这条上界。
- prompt：reduce 不能超过 100%、add 按原文超过 100% 且不要截成 100。删掉或反转这句会被契约断言杀死。这只证明原句还在 `RULES` 里，不证明模型会照做。

更早已经在死的函数 mutant 这次仍死：`time_ref`、21 天改成 22 和 20、12 条切片、reply 优先、双时钟、同频道、`return True`、最终 `claimed_closed`、一半、`percent=True`、坏比例改成 null、跳过 `exact_number`、先删空格、保本恒真、局部保本恒假、候选外 target、空 target 猜根、D12 不相等、sNaN 的 `InvalidOperation`、fraction 动作范围、stop 只属于 move_stop、空 target 不必 uncertain、关掉 symbol 冲突调用、episode 歧义取第一条、找不到不记原因、歧义不再抬 uncertain、float 除、任意批次都当 followup、共用第一条候选、`expected_schema` 置空。

## 旧断言与贯通

本轮没有改旧断言。`write_e2e_lake` 没动。`test_export_fake_run_import_build_maps_decimal_and_misses` 里的 `counts["exported"] == 4` 仍在。已知时钟的既有结果不靠改旧 hash 来迁就新行。

贯通是新增的 `test_unknown_clock_now_messages_stay_candidates`，仿写数据，`RecordedClient.from_file`：

- now 管理且 `available_at` 未知，或 `event_time` 未知，都留下。原文还在。用户 JSON 的 context、候选 root、reply 都空。
- `time_ref=past` 的 `past-fu` 不进候选，也不进 parquet。
- 只有 open+known 时，known 的 key 等于三条未知/过去兄弟一起导出时的 known key；两边 `candidate_root_ids` 都是 `[1]`。
- export 与 build 的 `unknown_clock_candidates == 2`，note 等于 `UNKNOWN_CLOCK_NOTE`。
- 录制指令：known 是 target 1 的 reduce；两条未知时钟是 `action=none`、`target=null`、`uncertain=true`。`unk-avail` 落盘 `available_at` 为空；known 的 `available_at` 非空且 target 为 1。

上一轮已经落地、本轮没有回退的预期：

- v1 now-open、v2 past 编辑不把根丢掉，参数留最近可见 now-open，不抄 past 字段。只有 past、没有既往 now-open，仍然不是目标。已验证的合法终态仍排除。未来版本不泄露。21 天外的原文，即使后来有 now-open 编辑，仍不是合法 target。
- add 原文 150% 合法，落盘 `fraction=Decimal('1.5')`；200% 落盘 `Decimal('2')`。reduce 的 150% 仍拒绝，原因仍是 `fraction_out_of_range`。
- `#BTC/USDT`、比特币、大饼对 BTC 候选不拒绝。BTC 对 ETH 仍拒绝。候选没有已知 symbol 时不猜。
- `to` 缺 claim、claim 为 garbage、或 `to` 不是成对状态，根留下。终态看合法 lifecycle payload，不看 kind。
- prompt 不要求「0 到 100」或 `Decimal(38,12)`。精度仍只在 validator 和 `FOLLOWUP_V1.md`。

## 剩余风险

- 未知时序行是审计行，不是可执行管理。`available_at=null` 不是交易时钟。下游不得把 `unknown_clock_candidates` 当成可下单数据。
- 这条修正只停止丢候选。它不发明时间，也不给未知时钟挂 root。可见性函数本身没有放宽。
- prompt mutant 只证明 `RULES` 里还有原句。它不证明模型会照做。
- 候选不是已确认持仓。保守 now-open 保留的是候选根，不是成交事实。
- telethon 那条 experimental async session warning 不属于本范围，没有修。
- 全套退出码 0 只覆盖写本文件之前的那一次。之后若再改源码，这个退出码不再适用。本文件的写入没有触发测试。
- 五个文件以外的并行脏 diff 没有纳入这次验收。
