# A28 性质差分交付回执

续派 task-mtwjotp6-5a6doi；接受 G0 的 force_close_net_R 不适用裁定。新增 10 条性质差分，最终 332/36 全绿，24 项真实树突变均 0→1→0。生产 market 内容逐文件 SHA256 与任务开始快照完全相同。

## Changed files

| 文件（相对 tests/market） | 本轮用途 |
|---|---|
| test_single_source.py | 保留原 26 条测试，新增 10 条真实消费方性质差分、固定种子边界生成器、G0 换层与不制造证据的原则说明 |
| a28_mutations.py | 24 项真实写盘突变驱动；finally 写回，独立 Python 进程与模块 __file__ 断言；异常退出不冒称行为变红 |
| a28_mutation_results.json | 最终 24 项的三阶段完整输出、退出码、来源文件 SHA256、测试源码 SHA256、注入原文 |
| a28_mutation_receipt.md | 上述证据逐条可读回执，包含 72 次运行的真实输出和完整 SHA256 |
| a28_mutation_attempt1.json | 首次探索保留；TTL-before 收集期 exit 4 明确不是差分变红证据，未计入最终通过；修正输入装载后全部重跑 |
| a28_before.json | 本轮开始的 12 个 market 文件（含全部 policy_hashes.json）内容哈希与纳秒 mtime |
| a28_after.json | 四条验收完成后同一集合的哈希与纳秒 mtime；生产内容全相同，policy 元数据全相同 |
| a28_validation.json | 四条指定命令的完整 stdout/stderr 合并输出和真实退出码 |
| a28_receipt.md | 本回执：范围、边界映射、变红清单、验收、剩余边界 |

本轮没有修改交易夹具、政策登记、AB 解释或冻结分类。真实源码只在突变窗口短暂写入，并按字节还原；源码的 mtime 因真实写盘会变化，policy_hashes.json 的 mtime 没有变化。工作树其他会话已有/新增内容均保留，不列为本轮修改。

## 单一来源与四类生成边界

种子 2801–2810；每个生成器固定可复现。_times 强制 6 个锚点 × −1/0/+1µs，其中固定月末、闰日、年末，另外 3 个锚点随机生成。随机性补充覆盖，必经边界不依赖均匀抽中。

| 差分测试（test_differential_ 前缀） | 实际消费行为 / 单一来源预期 | 明确生成的边界 |
|---|---|---|
| partition_grid | check_bars 的 expected_rows、保留行、missing、gap_flag、缺口逐点集合、离网隔离；预期直接用 grid_points_between / first_grid_point | 起止微秒三点；生命周期与日历相交；首/中/尾/随机洞/全空；离网 +1µs；半开两侧；缺 open_time / 全 null 列 |
| vision_count | expected_rows 与 grid_points_between 逐值相等 | 随机日期、闰/平年二月、跨日跨月年末、全部 INTERVAL_SECONDS 与全部 BAR_TYPES；funding/metrics 未知计数 None |
| kernel_grid | _first_bar_gap 与单一来源网格减去实际 bar 集合后的首缺点一致 | 首尾 ±1µs、首/中/尾洞/随机洞/空列表；MarketView 字段未提供与 [] 都为空流，显式 None 拒绝 |
| lake_grid | 真实 parquet → load_market_from_lake 的完整性、所载时间点、Decimal 载荷 | 亚秒首点、端点排除、跨日/月、无行/缺首/缺尾；gap_flag/ohlc_valid/source_sha256 分别缺列与全 null；Decimal(38,12)、1e-8、1e4 第12位、26位整数与12位小数混合 |
| start | build_request、_chk、resolved_t_start、KernelA.t_start 与真实湖装载；直接用 derived_t_start | latency 0/1/60/随机正值和负域拒绝；起点微秒三点；ABSENT/None/显式同值/±1µs，与 horizon 起点±1µs 交叉；S20/S30/S38 同义请求两条分支 |
| window | build_request / _chk 与 derived_window_s；caller 与 policy 边界逐值核对 | max_holding 缺失/None/1/60/随机；推导窗、安全上限各±1µs；跨日/月；caller/policy 同一终点 |
| ttl | build_request、_resolve_ttl、_chk 与 resolve_entry_ttl_s | 计划缺失/None/1/60/显式等于policy/随机；解析记录 ABSENT/None/[]/0/预期±1；dict/OrderPlan 双形态；S19 省略与同值等价；显式空 fractions 不得修补；风险预算12位与13位小数域 |
| expiry_timeline | A.timeline 的实际 expiry moments 与 entry_expiry_at | 随机 TTL，亚秒开始，horizon=expiry±1µs；恰到期必须被插入 |
| expiry_orders | A.submit_entries 创建的真实 entry orders.deadline 与 entry_expiry_at | 同上；订单必须确实存在且未 rejected，避免空集合 all() 假通过 |
| expiry_b | 真实 Nautilus 引擎到期事件 ts/reason 与 entry_expiry_at | 随机 TTL，开始±1µs，终点相对到期±1µs，明确给到期价点；未到期走 horizon_end，已到期走 entry_ttl |

Decimal 属于请求与湖载荷的边界输入；这些整数时间单一来源本身不做 Decimal 估值。本轮未制造 force_close 的数值消费方，也未重写其公式。空流按 _first_bar_gap 已声明的无可定位 bar 行为返回 None，不虚构首缺点。

## 每条差分的注入—变红表

三阶段完整原始输出及完整 SHA256 逐条见 [a28_mutation_receipt.md](a28_mutation_receipt.md)，机器可读版本见 [a28_mutation_results.json](a28_mutation_results.json)。只运行表中性质差分节点，不依赖静态禁令/调用登记导致红灯。

| 注入 ID | 差分 | 基线 | 注入 | 还原 | 内容 SHA 相等 |
|---|---|---|---|---|---|
| partition-count | partition_grid | 1 passed in 2.66s / exit0 | 1 failed in 1.22s / exit1 | 1 passed in 3.32s / exit0 | 是 |
| partition-first | partition_grid | 1 passed in 2.80s / exit0 | 1 failed in 0.11s / exit1 | 1 passed in 2.65s / exit0 | 是 |
| partition-middle | partition_grid | 1 passed in 2.62s / exit0 | 1 failed in 1.36s / exit1 | 1 passed in 2.55s / exit0 | 是 |
| vision-count | vision_count | 1 passed in 0.04s / exit0 | 1 failed in 0.06s / exit1 | 1 passed in 0.04s / exit0 | 是 |
| kernel-first | kernel_grid | 1 passed in 0.20s / exit0 | 1 failed in 0.24s / exit1 | 1 passed in 0.20s / exit0 | 是 |
| kernel-middle | kernel_grid | 1 passed in 0.19s / exit0 | 1 failed in 0.20s / exit1 | 1 passed in 0.32s / exit0 | 是 |
| kernel-tail | kernel_grid | 1 passed in 0.29s / exit0 | 1 failed in 0.22s / exit1 | 1 passed in 0.21s / exit0 | 是 |
| lake-count | lake_grid | 1 passed in 1.18s / exit0 | 1 failed in 0.49s / exit1 | 1 passed in 1.96s / exit0 | 是 |
| lake-start | start | 1 passed in 1.93s / exit0 | 1 failed in 0.58s / exit1 | 1 passed in 2.06s / exit0 | 是 |
| start-validator | start | 1 passed in 2.20s / exit0 | 1 failed in 0.35s / exit1 | 1 passed in 1.74s / exit0 | 是 |
| start-resolver | start | 1 passed in 1.61s / exit0 | 1 failed in 0.20s / exit1 | 1 passed in 1.91s / exit0 | 是 |
| start-builder | start | 1 passed in 1.88s / exit0 | 1 failed in 0.21s / exit1 | 1 passed in 1.82s / exit0 | 是 |
| start-lower-bound | start | 1 passed in 1.89s / exit0 | 1 failed in 0.64s / exit1 | 1 passed in 2.17s / exit0 | 是 |
| window-validator | window | 1 passed in 0.30s / exit0 | 1 failed in 0.23s / exit1 | 1 passed in 0.32s / exit0 | 是 |
| window-builder | window | 1 passed in 0.32s / exit0 | 1 failed in 0.36s / exit1 | 1 passed in 0.30s / exit0 | 是 |
| window-derived-validator | window | 1 passed in 0.28s / exit0 | 1 failed in 0.24s / exit1 | 1 passed in 0.30s / exit0 | 是 |
| ttl-before | ttl | 1 passed in 0.27s / exit0 | 1 failed in 0.21s / exit1 | 1 passed in 0.19s / exit0 | 是 |
| ttl-validator | ttl | 1 passed in 0.18s / exit0 | 1 failed in 0.21s / exit1 | 1 passed in 0.20s / exit0 | 是 |
| ttl-builder | ttl | 1 passed in 0.18s / exit0 | 1 failed in 0.21s / exit1 | 1 passed in 0.21s / exit0 | 是 |
| expiry-timeline | expiry_timeline | 1 passed in 0.22s / exit0 | 1 failed in 0.27s / exit1 | 1 passed in 0.20s / exit0 | 是 |
| expiry-timeline-bound | expiry_timeline | 1 passed in 0.23s / exit0 | 1 failed in 0.19s / exit1 | 1 passed in 0.21s / exit0 | 是 |
| expiry-orders | expiry_orders | 1 passed in 0.19s / exit0 | 1 failed in 0.17s / exit1 | 1 passed in 0.18s / exit0 | 是 |
| expiry-b | expiry_b | 1 passed in 1.70s / exit0 | 1 failed in 1.12s / exit1 | 1 passed in 1.16s / exit0 | 是 |
| expiry-b-bound | expiry_b | 1 passed in 1.18s / exit0 | 1 failed in 1.13s / exit1 | 1 passed in 1.20s / exit0 | 是 |

全部 24 条注入均在测试执行阶段变红；没有注入仍绿，因此本次无需 A26 叠加。首次 TTL 收集错误不属于“仍绿”，也不计为变红；已保留该失败尝试并在修正后重新执行完整 24 条。

最终测试源码 SHA256：`98d3a28bb582cda79a50a241d7ec71a35e4222ef44a2cf1c15262c71004d8c32`（24 项取证记录与当前文件逐项相等）。

## 四条指定命令的真实输出

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python -m pytest tests/market -q -p no:cacheprovider
```

```text
........................................................................ [ 21%]
........................................................................ [ 43%]
........................................................................ [ 65%]
........................................................................ [ 86%]
............................................                             [100%]
332 passed in 27.61s
exit 0
```

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python -m pytest tests/market/test_single_source.py -q -p no:cacheprovider -v
```

```text
============================= test session starts ==============================
platform darwin -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0
rootdir: /Users/balen/projects/trader-bot/quant-lab
configfile: pyproject.toml
plugins: anyio-4.15.1
collected 36 items

tests/market/test_single_source.py ....................................  [100%]

============================== 36 passed in 8.75s ==============================
exit 0
```

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A
```

```text
E01    ok  replay=True 
E02    ok  replay=True 
E03    ok  replay=True 
E04a   ok  replay=True 
E04b   ok  replay=True 
E04c   ok  replay=True 
E05    ok  replay=True 
E06    ok  replay=True 
E07    ok  replay=True 
E08    ok  replay=True 
E09    ok  replay=True 
E10    ok  replay=True 
E11    ok  replay=True 
E12    ok  replay=True 
E14a   ok  replay=True 
E14b   ok  replay=True 
E14c   ok  replay=True 
E15a   ok  replay=True 
E15b   ok  replay=True 
E16    ok  replay=True 
E17    ok  replay=True 
E18    ok  replay=True 
kernel=A passed=22 failed=0
exit 0
```

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1
```

```text
{"codes": {"MATCH": 12, "B_COMMAND_LATENCY": 3, "GAP_PRICE": 1, "SAME_TS_PRIORITY": 4, "GTD_BOUNDARY": 1, "B_LIQUIDITY_MODEL": 1}, "median_ms": {"A": 12.322584007051773, "B": 47.530875002848916}, "unsupported": {"S02": {"status": "unsupported", "capability": "real_settlement_time_U03_and_engine_balance", "owner": "G0 settlement contract + G2 P2"}, "S06": {"status": "unsupported", "capability": "bronze_depth_replay_and_multi_source_versions", "owner": "G1 lake + G2 P2"}, "S07": {"status": "unsupported", "capability": "management_commands_E13_C01_and_reservation_lifecycle", "owner": "G0 C01 + G2 P2"}, "S12": {"status": "unsupported", "capability": "versioned_lake_snapshot_resolution", "owner": "G1 lake + G0 identity contract"}}}
exit 0
```

## 分类逐项保持

| 分类 | G0 基线 | 本轮 |
|---|---:|---:|
| MATCH | 12 | 12 |
| B_COMMAND_LATENCY | 3 | 3 |
| GAP_PRICE | 1 | 1 |
| SAME_TS_PRIORITY | 4 | 4 |
| GTD_BOUNDARY | 1 | 1 |
| B_LIQUIDITY_MODEL | 1 | 1 |
| UNEXPLAINED | 0 | 0（输出中无此类） |

## policy_hashes.json 内容与 mtime

`quant-lab/src/quant_lab/market/policy_hashes.json`

- BEFORE SHA256: `a89ef8a1f1155e530b0b78c4420e55a1e70413743ebefdef6b0b1b78cc94dd65`
- AFTER SHA256: `a89ef8a1f1155e530b0b78c4420e55a1e70413743ebefdef6b0b1b78cc94dd65`
- BEFORE mtime_ns: `1789105045832099622`
- AFTER mtime_ns: `1789105045832099622`

## Remaining risks / 边界

- 固定种子和有限生成器只能覆盖实际生成路径；语义层不宣称不可穿过，不能替代不可达位置的语法禁令或结构登记。
- B 的 native GTD 撮合顺序与 A 不同，既有 GTD_BOUNDARY 分类保持为1。本测试用不成交的挂单、明确到期价点观测到期消费；不是宣称稀疏价点下事件也会在无行情时刻发出。首次调试的无到期价点场景在下一 tick 才发到期，按已有时钟语义修正了新测试输入，未改任何冻结夹具或基线。
- force_close_net_R 无消费方，差分不适用且义务顺延：G3 接入时须同步增加实际 θ 消费差分与返回值哨兵。当前测试不作为 G3 θ 的证据。
- A/B report 中 S02/S06/S07/S12 unsupported 维持现状，未扩张本轮范围。
- 突变脚本必须独占相关源码写入窗口运行；失败使用 finally 真实还原与 SHA 校验，若遭进程强杀仍需人工根据原文件恢复。不得并行运行该脚本与其他源码编辑。
