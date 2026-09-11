# S45 / A36 / A37.2 验收记录

仅修改允许目录。S44 不在范围；本批未使用、修改 tree_guard.py。工作树原有改动保留。生产行为保持不变。

## 本次 changed files

- `src/quant_lab/market/contract.py`：在 CENSOR_PRIORITY 旁明确低代价偏向及统计效率/结论污染依据（仅新增三行注释）。
- `tests/market/test_single_source.py`：分区生成器新增整组 −1µs 与窗口内单点 −1/0/+1µs，独立 helper 处理短窗口。
- `tests/market/test_boundary_discrimination.py`：四类独立输入组、共用真实请求/分区消费路径；五证据原因 × 两种顺序的 A37 回归。共新增 14 个测试。
- `tests/market/verify_s45_a36.py`：真实写盘、进程隔离自证、SHA256 还原、同批对照和 A26 叠加取证。
- `tests/market/s45-a36-mutation-evidence.json`：完整突变、PID、SHA256、逐测试结果和三次运行输出。
- `tests/market/s45-a36-mutation-run.log`：突变器前台实跑原始输出。
- `tests/market/s45-a36-verification.log`：逐条指定命令与真实归档命令原始输出及退出码。
- `tests/market/s45-a36-verification.md`：本记录。

## A29 / A33 自证

注入发生在 tests/market 下独立副本的真实生产 .py 文件。每个子进程在 pytest 启动前和收集完成后检查模块 __file__；另核父进程传入的源文件 SHA256。收集到的 15 个测试必须来自副本。禁用字节码和 pytest 缓存/外部插件自动加载，运行前后核完整副本文件哈希表不变。每例 baseline / injected / restored 使用新进程。还原成功由完整文件哈希表相等判定，返回码仅记录测试结果。

同一批首例 expected_rows + 1 为已知必红对照：四类输入组与原分区差分均 RED。

| 突变 | 基线 PID / exit | 注入 PID / exit | 还原 PID / exit | 内容 SHA256 还原 |
|---|---|---|---|---|
| known_red_control | 39720 / 0 | 39880 / 1 | 39917 / 0 | MATCH |
| S45_time_forward_tolerance | 39937 / 0 | 39975 / 1 | 39983 / 0 | MATCH |
| A36_numeric_float_validation | 39996 / 0 | 40013 / 1 | 40040 / 0 | MATCH |
| A36_empty_falsy_fallback | 40064 / 0 | 40081 / 1 | 40096 / 0 | MATCH |
| A36_equivalence_explicit_start | 40140 / 0 | 40153 / 1 | 40178 / 0 | MATCH |
| A37_label_before_evidence | 40221 / 0 | 40251 / 1 | 40274 / 0 | MATCH |

| 突变 | 基线 = 还原 SHA256 | 注入 SHA256 |
|---|---|---|
| known_red_control | `1b0ddff88d5a8716781cd89479261d99931b186208190fdbe0af1bcbda867374` | `cbb6a69508839bb9aaf38c5a19c89bbf769b9a375283e92e47de914c4cd68082` |
| S45_time_forward_tolerance | `1b0ddff88d5a8716781cd89479261d99931b186208190fdbe0af1bcbda867374` | `045ea1ba667b337c66a23ed5b90e0f84caa35ac2b28eaf7741b8c7b50eea851d` |
| A36_numeric_float_validation | `8ccced9abb38f402e51a9b2dc46e7013625d76525a3764f4d35c572217e02be7` | `907c2f824c6108bad6acb9a60e4836c84f65976f24d53e7037e978022285ae28` |
| A36_empty_falsy_fallback | `8ccced9abb38f402e51a9b2dc46e7013625d76525a3764f4d35c572217e02be7` | `6db2b67c4ab21277c1bc4b475d3f4b4e352cbd9c1ba53f81f6df4ef378d2185c` |
| A36_equivalence_explicit_start | `8ccced9abb38f402e51a9b2dc46e7013625d76525a3764f4d35c572217e02be7` | `4521f426ed7397b125b16d0711f55cf686dcd6ff222748ac2a9467c00660af61` |
| A37_label_before_evidence | `8ccced9abb38f402e51a9b2dc46e7013625d76525a3764f4d35c572217e02be7` | `f7d820ffac60c4ddb981edb6c7eb40e87cf606de05c2e3718a8049758a0225fc` |

## A36 判别性矩阵

四组均先 probe 正常请求和分区，再只改变所属维度。时间组覆盖首/中/尾 −1/0/+1µs、半开区间两端；数值组覆盖 Decimal(38,12) 整数位/标度上限、12 位小数、同一 parquet 混合 1e-8 与 1e4；空组覆盖省略、None、[]、缺列、全 null 列，拒绝显式空分配但允许省略推导；等价组覆盖省略与显式写同一启动时刻/分配。

| 真实突变 | 时间 | 数值 | 空/缺失 | 等价对 | A26 叠加对照 PID / 四组 |
|---|---|---|---|---|---|
| S45_time_forward_tolerance | RED | GREEN | GREEN | GREEN | 39982 / 全 RED |
| A36_numeric_float_validation | GREEN | RED | GREEN | GREEN | 40027 / 全 RED |
| A36_empty_falsy_fallback | GREEN | GREEN | RED | GREEN | 40095 / 全 RED |
| A36_equivalence_explicit_start | GREEN | GREEN | GREEN | RED | 40172 / 全 RED |

四条注入分别是：精确网格身份放宽为前向 1µs 容差；risk_budget 校验先转二进制 float；显式空分配被 falsy 默认值吞掉；显式 t_start 分支额外偏移 1µs。全部作用在可达真实消费代码，无别名替身/收集期替换/语法禁令。
他类 GREEN 在采信前均先叠加 expected_rows + 1，四次均令四组全 RED。此处 GREEN 表示该组输入没有触发该边界退化，不是检查无法失败。

## S45 原样突变 RED

```python
first_grid_point(t, sec) == t
# 注入为
(first_grid_point(t, sec) - t <= dt.timedelta(microseconds=1))
```

同一 helper 与调用次数不变。`test_differential_partition_grid` 与 `test_boundary_time` 均 failed；数值/空/等价组仍 passed。

真实 DataFrame 0 / 59999999 / 120000000 µs 已在 test_boundary_time 中执行：基线断言 expected=3 / missing=1 / quarantine=2、保留两行。注入失败输出显示 59.999999s 被错误保留。没有新增字符串写法禁令。

## A37.2

代码明确偏向 unevaluable：误排真实事实仅损失统计效率；把无知当成中性事实会污染 θ 分母与结论。优先级行为未修改。
回归直接调用 KernelA.censor_now 实际优先级消费方，再构造实际 result 并检查 coverage 与 outcome_kind；五类证据、两种到达顺序的期望是固定 unevaluable，不从 CENSOR_PRIORITY 推导。LABEL_RIGHT_CENSORED 提到最前时 10/10 RED。此为优先级门回归，不声称覆盖 B 的全部事件调度。

## 前台验证真实输出

完整逐条命令与输出在 `s45-a36-verification.log`；全部 exit 0。

```text
358 passed in 23.56s
48 passed in 7.46s
kernel=A passed=22 failed=0
```

| AB 分类 | 基线 | 实跑 |
|---|---:|---:|
| MATCH | 12 | 12 |
| B_COMMAND_LATENCY | 3 | 3 |
| GAP_PRICE | 1 | 1 |
| SAME_TS_PRIORITY | 4 | 4 |
| GTD_BOUNDARY | 1 | 1 |
| B_LIQUIDITY_MODEL | 1 | 1 |
| UNEXPLAINED | 0 | 0（输出无此键） |

真实归档调用既有 `verify_s43_archive.py`（本次未修改），读本地 data/lake/market，逐日 31 行明细见日志：

```text
ARCHIVE bars_last: rows=44640 off_grid=0 bars_complete=True (31/31 windows)
ARCHIVE bars_mark: rows=44640 off_grid=0 bars_complete=True (31/31 windows)
ARCHIVE fundingRate: rows=93 jitter_rows=15 retained=True funding_complete=True
```

## Remaining risks

- 判别矩阵只证明上述四组明确输入的判别性，不宣称所有可能边界输入均被穷尽。
- 归档证据限本地 BTCUSDT 2024-01；未扩展其他品种/月。
- AB 报告原有 unsupported S02/S06/S07/S12 保持原样；S44 排除在本任务之外。
- 首次新增数值测试误将 Decimal 列输入浮点分区体检而失败，随后改为现有 Decimal parquet 装载路径；未改生产分区 dtype 行为。
- 无新增规格缺口；无提交/部署/交易状态操作。
