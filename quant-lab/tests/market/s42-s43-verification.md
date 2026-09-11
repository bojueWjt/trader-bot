# S42 / S43 验收取证（2026-09-11）

S43 已修复：loader 只将窗口内、精确对齐的唯一 open_time 计入覆盖；离网行保留原始时间戳和 quality_notes，并令 bars_complete=False、bars_quality_ok=False。KernelA 的覆盖游标只由匹配期望点的 bar 推进。显式 MarketView 的 bars_complete 声明不能代替 KernelA 的检查。

S42 已修复：两个既有生成器都加入窗口内 bar 的 −1µs / 0 / +1µs；新增 12 个公共路径用例分别覆盖 last / mark 和两个 parquet 流。没有增加禁止 replace 的语法规则；调用登记仅反映实际调用位置和次数。

## Changed files

- src/quant_lab/market/contract.py：说明 Bar 保留原始时间证据、MarketView 的来源声明不代替消费检查，funding 不适用 bar 网格。
- src/quant_lab/market/execution.py：合法唯一网格点计数、离网质量证据。
- src/quant_lab/market/kernel_a.py：逐点匹配网格身份，修复提前 1µs 的假覆盖。
- src/quant_lab/market/single_source.py：同步 first_grid_point 调用登记及调用次数；没有新增语法禁令。
- tests/market/test_single_source.py：扩充两条差分输入域及 12 个公共路径参数用例。
- tests/market/verify_s42_s43.py：真实写盘、新进程、路径自证、同批对照、SHA256 还原的可重跑取证器。
- tests/market/verify_s43_archive.py：原样复现和逐行真实归档 loader 检查。
- tests/market/s42-s43-mutation-evidence.json：最终批次九次子进程的完整原始输出、退出码、注入表达和 SHA256。
- tests/market/s42-s43-verification.md：本报告及四条验收命令、真实归档的完整输出。

## S43 原样复现前后

输入：01:00Z 开始，offsets=[0,59999999,120000000] 微秒，interval_s=60；真实 Bar / MarketView 与临时 parquet，没有 model_construct 或生产函数 patch。

修改前实跑输出：

```text
S43 BEFORE explicit FIRST_GAP= None censor_reason= LABEL_RIGHT_CENSORED bars_ok= True
S43 BEFORE loader bars_complete= True bars_quality_ok= True
```

修改后见下方 archive 命令输出：FIRST_GAP=01:01:00Z、BAR_GAP、bars_ok=False；loader 两项标志均 False，并记录原始 01:00:59.999999 和实际 2 个合法点。

## A29 / A33 突变自证

运行命令：`PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python tests/market/verify_s42_s43.py`，exit 0。

同批三组注入，每组 baseline / injected / restored 都另起进程。复制源码与测试到 tests/market 下的临时树，使用独立 pytest.ini，清除继承的 PYTHON* / PYTEST* 配置，禁用插件自动加载和字节码；启动和 collection 后均断言六个被测模块 __file__ 精确等于预期临时树路径，全部测试 item 路径也必须位于该树。临时树已删除。还原由源码内容 SHA256 相等证明，退出码仅报告测试结果。

| 注入 | baseline | injected | restored | PID（三次） |
|---|---|---|---|---|
| known_red_control | 48 passed in 5.83s / exit 0 | 7 failed, 41 passed in 5.08s / exit 1 | 48 passed in 5.02s / exit 0 | 1340 / 1396 / 1438 |
| S42_original_seconds_truncation | 48 passed in 5.06s / exit 0 | 3 failed, 45 passed in 4.97s / exit 1 | 48 passed in 5.00s / exit 0 | 1486 / 1537 / 1599 |
| loader_seconds_truncation | 48 passed in 5.23s / exit 0 | 5 failed, 43 passed in 4.37s / exit 1 | 48 passed in 4.89s / exit 0 | 1631 / 1722 / 1758 |

| 注入 | baseline SHA256 | injected SHA256 | restored SHA256 |
|---|---|---|---|
| known_red_control | `f6cce0bb9b66ceb3189122e409cc204bfd65f6535235872435e1c626081e782e` | `4e9db5bc371c152032fbb9192666265d5742dace94ec22a1dfb2263d3a06c766` | `f6cce0bb9b66ceb3189122e409cc204bfd65f6535235872435e1c626081e782e` |
| S42_original_seconds_truncation | `f6cce0bb9b66ceb3189122e409cc204bfd65f6535235872435e1c626081e782e` | `7b89a662c108d1d7d1c0f46531b3bd182672842f82984f1fcef605422375c06b` | `f6cce0bb9b66ceb3189122e409cc204bfd65f6535235872435e1c626081e782e` |
| loader_seconds_truncation | `8afa5b640f603b2da02966a77390974c31d0d220461aff4c24377481c6c5797e` | `cd7a326db5d8d853dd9449d3d1eb3be1c3d4141efc49fa9f13538adf8456417c` | `8afa5b640f603b2da02966a77390974c31d0d220461aff4c24377481c6c5797e` |

known_red_control 将 `if opened != expected` 改成 `if opened == expected`，已知性质破坏得到 RED。同批 S42 原样注入 `b.open_time.replace(microsecond=0)` 的三个失败全部来自差分行为测试，静态门仍通过；两个公共路径失败直接显示：

```text
E AssertionError: assert 'LABEL_RIGHT_CENSORED' == 'BAR_GAP'
FAILED test_differential_kernel_grid
FAILED test_differential_kernel_public_interior_bar[bars_last-1]
FAILED test_differential_kernel_public_interior_bar[bars_mark-1]
```

loader 对照把用于覆盖的 opened 截断到秒，得到 5 failed，其中 +1µs 的两个公共 loader 用例直接检出 bars_complete=True 的错误。−1µs 用例也检出时间证据被截断。所有注入均 RED，无仍绿项，A26 叠加调查未触发；没有把冗余防御结果充作行为证据。

## 四条验收命令（顺序前台实跑，退出码均 0）


```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python -m pytest tests/market -q -p no:cacheprovider
```

```text
........................................................................ [ 20%]
........................................................................ [ 41%]
........................................................................ [ 62%]
........................................................................ [ 83%]
........................................................                 [100%]
344 passed in 19.44s
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
collected 48 items

tests/market/test_single_source.py ..................................... [ 77%]
...........                                                              [100%]

============================== 48 passed in 5.64s ==============================
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
```

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1
```

```text
{"codes": {"MATCH": 12, "B_COMMAND_LATENCY": 3, "GAP_PRICE": 1, "SAME_TS_PRIORITY": 4, "GTD_BOUNDARY": 1, "B_LIQUIDITY_MODEL": 1}, "median_ms": {"A": 9.357791001093574, "B": 35.7985829905374}, "unsupported": {"S02": {"status": "unsupported", "capability": "real_settlement_time_U03_and_engine_balance", "owner": "G0 settlement contract + G2 P2"}, "S06": {"status": "unsupported", "capability": "bronze_depth_replay_and_multi_source_versions", "owner": "G1 lake + G2 P2"}, "S07": {"status": "unsupported", "capability": "management_commands_E13_C01_and_reservation_lifecycle", "owner": "G0 C01 + G2 P2"}, "S12": {"status": "unsupported", "capability": "versioned_lake_snapshot_resolution", "owner": "G1 lake + G0 identity contract"}}}
```

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv-g2/bin/python -m tests.market.verify_s43_archive
```

```text
S43 AFTER explicit FIRST_GAP=2024-01-01 01:01:00+00:00 censor_reason=BAR_GAP bars_ok=False
S43 AFTER loader bars_complete=False bars_quality_ok=False bars_ok=False
klines off-grid bar open_time=2024-01-01T01:00:59.999999+00:00 interval_s=60
klines 期望 3 根，实际 2 根合法唯一网格 bar（原始 3 行）
markPriceKlines off-grid bar open_time=2024-01-01T01:00:59.999999+00:00 interval_s=60
markPriceKlines 期望 3 根，实际 2 根合法唯一网格 bar（原始 3 行）
BTCUSDT 2024-01-01 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-02 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-03 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-04 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-05 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-06 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-07 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-08 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-09 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-10 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-11 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-12 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-13 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-14 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-15 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-16 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-17 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-18 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-19 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-20 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-21 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-22 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-23 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-24 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-25 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-26 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-27 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-28 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-29 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-30 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
BTCUSDT 2024-01-31 last=1440 mark=1440 bars_complete=True bars_quality_ok=True funding_complete=True
ARCHIVE bars_last: rows=44640 off_grid=0 bars_complete=True (31/31 windows)
ARCHIVE bars_mark: rows=44640 off_grid=0 bars_complete=True (31/31 windows)
ARCHIVE fundingRate: rows=93 jitter_rows=15 retained=True funding_complete=True
```

AB 逐项核对：MATCH 12、B_COMMAND_LATENCY 3、GAP_PRICE 1、SAME_TS_PRIORITY 4、GTD_BOUNDARY 1、B_LIQUIDITY_MODEL 1，与基线完全一致；无 UNEXPLAINED 项（0）。未修改夹具或分类基线。

## Remaining risks

- 真实归档验证范围是现有 BTCUSDT 2024-01；不将单月观察推广为所有归档保证。
- 归档按 31 个日窗口装载，避免超过既有 14 日请求上限及读取不存在的二月分区；每窗截至当日 23:59:59.999999，覆盖全部 1m bar 网格点。合计逐行验证 89,280 根 bar，并保留全部 93 行 funding。
- 沿用 loader 原有 S02（真实结算时刻）、S12（版本化快照）不支持项；本次不改变这些能力或 funding 容差。
- 工作区存在其他会话的 ADR、taskList 等改动，本次不修改、不还原它们。未连交易所私有 API、未读 services/nautilus-node 配置、未 import services。

