# force_close_net_R 交付回执

任务一已完成；任务二 A28 未开始。G3 接入未完成，两份跨窗口调用登记均为空集。

测试消费方夹具只证明函数五项记账、三条硬约束与 A24 登记/禁令生效，不证明 G3 调用而非自算；G3 接入时仍须以哨兵证明 θ 随返回值改变。未来/未闭合 mark 的 RED 也是夹具侧证据。

## Changed files

- `src/quant_lab/market/contract.py`：增加只读估值函数、冻结返回体、显式输入与导出，gross_pnl 缺失报错，不重建损益。
- `src/quant_lab/market/single_source.py`：两份空登记及 P8 余仓估值禁令，仅允许属主定义处。
- `tests/market/test_force_close_net_r.py`：17 个新增测试实例，覆盖记账、不变量、缺失值、消费方夹具、两条删失路径及结构/语法门。
- `tests/market/test_outcome_kind.py`：S30 probe 政策改写临时副本，monkeypatch 隔离内存登记与路径，保留行为断言，增加真实文件 mtime 断言。
- `tests/market/force_close_mutations.py`：可复跑隔离突变执行器，实际加载路径断言，逐条恢复内容 SHA256 与 GREEN。
- `tests/market/force_close_before.json`：任务开始时相关文件内容哈希和 mtime。
- `tests/market/force_close_harness_audit.json`：首次执行器路径错误及 A26 叠加仍绿的无效证据，保留原因与还原哈希。
- `tests/market/force_close_mutation_results.json`：34 项有效自证，含替换内容、目标测试、RED/GREEN 完整输出及还原前后 SHA256。
- `tests/market/force_close_validation.json`：四条命令、完整输出、退出码、mtime、基线对照与最终源码哈希。
- `tests/market/force_close_receipt.md`：本回执。

## 突变自证表

所有有效注入均在 tests/market 下的隔离副本中执行；不写原始生产文件。每行还原以内容 SHA256 相等判定，返回码只用于 RED/GREEN。

| 注入 | RED | 还原内容 SHA256 | GREEN |
|---|---|---|---|
| M01 omit close_fee | exit 1 | 相等 | exit 0 |
| M02 reverse sign | exit 1 | 相等 | exit 0 |
| M03 future mark (fixture only) | exit 1 | 相等 | exit 0 |
| M04 omit funding | exit 1 | 相等 | exit 0 |
| M05 omit realized | exit 1 | 相等 | exit 0 |
| M06 omit accumulated fees | exit 1 | 相等 | exit 0 |
| M07 write net_R | exit 1 | 相等 | exit 0 |
| M08 append event | exit 1 | 相等 | exit 0 |
| M09 change fill_status | exit 1 | 相等 | exit 0 |
| M10 change censor | exit 1 | 相等 | exit 0 |
| M11 full filled qty as residual | exit 1 | 相等 | exit 0 |
| M12 omit multiplier | exit 1 | 相等 | exit 0 |
| M13 wrong fee scenario | exit 1 | 相等 | exit 0 |
| M14 zero instead of None | exit 1 | 相等 | exit 0 |
| M15 rebuild missing gross | exit 1 | 相等 | exit 0 |
| M16 missing entry silently supplied | exit 1 | 相等 | exit 0 |
| M17 bypass fixture consumption | exit 1 | 相等 | exit 0 |
| M18 disable hold censor | exit 1 | 相等 | exit 0 |
| M19 change horizon censor | exit 1 | 相等 | exit 0 |
| M20 fake G3 caller registration | exit 1 | 相等 | exit 0 |
| M21 fake G3 count registration | exit 1 | 相等 | exit 0 |
| M22 new actual caller | exit 1 | 相等 | exit 0 |
| M23 suppress registry extra | exit 1 | 相等 | exit 0 |
| M23 suppress registry bypass | exit 1 | 相等 | exit 0 |
| M24 suppress use counts | exit 1 | 相等 | exit 0 |
| M25 suppress inline ban | exit 1 | 相等 | exit 0 |
| M26 allow inline outside owner | exit 1 | 相等 | exit 0 |
| M27 S30 original missing latency | exit 1 | 相等 | exit 0 |
| M28 S30 real file write (isolated replica) | exit 1 | 相等 | exit 0 |
| M29 inline bypass one registered use | exit 1 | 相等 | exit 0 |
| M30 inline bypass all registered uses | exit 1 | 相等 | exit 0 |
| M31 inline expression real tree gate | exit 1 | 相等 | exit 0 |
| M32 unclosed mark (fixture only) | exit 1 | 相等 | exit 0 |
| M33 mutable valuation | exit 1 | 相等 | exit 0 |

登记项为结构证据，禁令项为语法证据；不可达区域的禁令有效性不宣称为行为证据。M29/M30 直接把已登记使用点换成内联自算，分别触发次数/集合门；M31 单独注入内联表达式触发全树禁令。

首次隔离执行继承父目录 pytest 配置，加载了原始 src，M01 仍绿；按 A26 叠加直接抛错也仍绿，确认为验证器路径错误，不计入有效自证。已使用独立 pytest.ini 并断言加载路径后从头重跑。过程中一个中文参数选择器造成 exit 4，改用显式 ASCII 测试 ID 后重跑；exit 4 不计作 RED。最终 34 项均为真实测试失败 exit 1，而非收集/配置错误。

## policy_hashes.json 不变证明

```text
task_start mtime_ns:   1789105045832099622
suite_before mtime_ns: 1789105045832099622
suite_after mtime_ns:  1789105045832099622
SHA256: a89ef8a1f1155e530b0b78c4420e55a1e70413743ebefdef6b0b1b78cc94dd65
```

## 四条命令真实输出

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python -m pytest tests/market -q -p no:cacheprovider
```
exit=0
```text
........................................................................ [ 22%]
........................................................................ [ 44%]
........................................................................ [ 67%]
........................................................................ [ 89%]
..................................                                       [100%]
322 passed in 24.96s
```

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python -m pytest tests/market/test_single_source.py -q -p no:cacheprovider -v
```
exit=0
```text
============================= test session starts ==============================
platform darwin -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0
rootdir: /Users/balen/projects/trader-bot/quant-lab
configfile: pyproject.toml
plugins: anyio-4.15.1
collected 26 items

tests/market/test_single_source.py ..........................            [100%]

============================== 26 passed in 2.12s ==============================
```

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A
```
exit=0
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
exit=0
```text
{"codes": {"MATCH": 12, "B_COMMAND_LATENCY": 3, "GAP_PRICE": 1, "SAME_TS_PRIORITY": 4, "GTD_BOUNDARY": 1, "B_LIQUIDITY_MODEL": 1}, "median_ms": {"A": 15.44145800289698, "B": 50.52862499724142}, "unsupported": {"S02": {"status": "unsupported", "capability": "real_settlement_time_U03_and_engine_balance", "owner": "G0 settlement contract + G2 P2"}, "S06": {"status": "unsupported", "capability": "bronze_depth_replay_and_multi_source_versions", "owner": "G1 lake + G2 P2"}, "S07": {"status": "unsupported", "capability": "management_commands_E13_C01_and_reservation_lifecycle", "owner": "G0 C01 + G2 P2"}, "S12": {"status": "unsupported", "capability": "versioned_lake_snapshot_resolution", "owner": "G1 lake + G0 identity contract"}}}
```

AB 分类字典与指定六项基线严格相等；UNEXPLAINED 缺项按 0 计。未修改夹具或分类基线。

## Remaining risks

- G3 接入未完成，真实 θ 对返回值的哨兵消费证明留待接入。
- A28 性质差分测试未开始，无 A28 注入-变红证据；本回执的 34 项均属于任务一。
- mark 是否已闭合/as-of 由调用方负责，函数本身不查行情。
- 语法禁令仍只覆盖声明的 AST 写法，不宣称封闭，也不代替未来 A28。
