# A28 真实树突变回执

每阶段新 Python 进程，导入路径断言指向真实被写盘文件；只运行差分测试。

| 注入 | 差分 | 基线/注入/还原 exit | 还原 SHA256 相等 |
|---|---|---|---|
| partition-count | tests/market/test_single_source.py::test_differential_partition_grid | 0/1/0 | True |
| partition-first | tests/market/test_single_source.py::test_differential_partition_grid | 0/1/0 | True |
| partition-middle | tests/market/test_single_source.py::test_differential_partition_grid | 0/1/0 | True |
| vision-count | tests/market/test_single_source.py::test_differential_vision_count | 0/1/0 | True |
| kernel-first | tests/market/test_single_source.py::test_differential_kernel_grid | 0/1/0 | True |
| kernel-middle | tests/market/test_single_source.py::test_differential_kernel_grid | 0/1/0 | True |
| kernel-tail | tests/market/test_single_source.py::test_differential_kernel_grid | 0/1/0 | True |
| lake-count | tests/market/test_single_source.py::test_differential_lake_grid | 0/1/0 | True |
| lake-start | tests/market/test_single_source.py::test_differential_start | 0/1/0 | True |
| start-validator | tests/market/test_single_source.py::test_differential_start | 0/1/0 | True |
| start-resolver | tests/market/test_single_source.py::test_differential_start | 0/1/0 | True |
| start-builder | tests/market/test_single_source.py::test_differential_start | 0/1/0 | True |
| start-lower-bound | tests/market/test_single_source.py::test_differential_start | 0/1/0 | True |
| window-validator | tests/market/test_single_source.py::test_differential_window | 0/1/0 | True |
| window-builder | tests/market/test_single_source.py::test_differential_window | 0/1/0 | True |
| window-derived-validator | tests/market/test_single_source.py::test_differential_window | 0/1/0 | True |
| ttl-before | tests/market/test_single_source.py::test_differential_ttl | 0/1/0 | True |
| ttl-validator | tests/market/test_single_source.py::test_differential_ttl | 0/1/0 | True |
| ttl-builder | tests/market/test_single_source.py::test_differential_ttl | 0/1/0 | True |
| expiry-timeline | tests/market/test_single_source.py::test_differential_expiry_timeline | 0/1/0 | True |
| expiry-timeline-bound | tests/market/test_single_source.py::test_differential_expiry_timeline | 0/1/0 | True |
| expiry-orders | tests/market/test_single_source.py::test_differential_expiry_orders | 0/1/0 | True |
| expiry-b | tests/market/test_single_source.py::test_differential_expiry_b | 0/1/0 | True |
| expiry-b-bound | tests/market/test_single_source.py::test_differential_expiry_b | 0/1/0 | True |

## partition-count — src/quant_lab/market/partition_check.py

真实替换：
```python
exp_n = grid_points_between(cal_from, cal_to, sec)
# →
exp_n = max(0, int((cal_to - cal_from).total_seconds()) // sec)
```

### baseline（exit 0）

SHA256 `1b0ddff88d5a8716781cd89479261d99931b186208190fdbe0af1bcbda867374`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/partition_check.py
.                                                                        [100%]
1 passed in 2.66s
```

### mutant（exit 1）

SHA256 `9ce967bc18a2cae92142e68b3767d6cefd3321e76c8382657b16b33ac0dd5fba`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/partition_check.py
F                                                                        [100%]
=================================== FAILURES ===================================
_______________________ test_differential_partition_grid _______________________
tests/market/test_single_source.py:218: in test_differential_partition_grid
    out, quarantines, report = pc.check_bars(
src/quant_lab/market/partition_check.py:253: in check_bars
    assert rep.missing >= 0, "present 必须是期望网格的子集"
           ^^^^^^^^^^^^^^^^
E   AssertionError: present 必须是期望网格的子集
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_partition_grid
1 failed in 1.22s
```

### restored（exit 0）

SHA256 `1b0ddff88d5a8716781cd89479261d99931b186208190fdbe0af1bcbda867374`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/partition_check.py
.                                                                        [100%]
1 passed in 3.32s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## partition-first — src/quant_lab/market/partition_check.py

真实替换：
```python
first_grid_point(t, sec) == t
# →
int(t.timestamp()) % sec == 0
```

### baseline（exit 0）

SHA256 `1b0ddff88d5a8716781cd89479261d99931b186208190fdbe0af1bcbda867374`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/partition_check.py
.                                                                        [100%]
1 passed in 2.80s
```

### mutant（exit 1）

SHA256 `8c612fd9703614f287dd01afc9fa2977cc5d53dcd914dc06c74133707c395bcc`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/partition_check.py
F                                                                        [100%]
=================================== FAILURES ===================================
_______________________ test_differential_partition_grid _______________________
tests/market/test_single_source.py:218: in test_differential_partition_grid
    out, quarantines, report = pc.check_bars(
src/quant_lab/market/partition_check.py:284: in check_bars
    assert sum(g["n"] for g in rep.gaps) == rep.missing, "缺口清单与 missing 必须一致"
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E   AssertionError: 缺口清单与 missing 必须一致
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_partition_grid
1 failed in 0.11s
```

### restored（exit 0）

SHA256 `1b0ddff88d5a8716781cd89479261d99931b186208190fdbe0af1bcbda867374`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/partition_check.py
.                                                                        [100%]
1 passed in 2.65s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## partition-middle — src/quant_lab/market/partition_check.py

真实替换：
```python
grid_points_between(prev[i] + step, df[key][i], sec) > 0
# →
grid_points_between(prev[i] + step, df[key][i], sec) >= 0
```

### baseline（exit 0）

SHA256 `1b0ddff88d5a8716781cd89479261d99931b186208190fdbe0af1bcbda867374`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/partition_check.py
.                                                                        [100%]
1 passed in 2.62s
```

### mutant（exit 1）

SHA256 `f62b408fc41ee6159e9a1c240e520ba7dc761bfe23177afb43ef991ee6b3aaae`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/partition_check.py
F                                                                        [100%]
=================================== FAILURES ===================================
_______________________ test_differential_partition_grid _______________________
tests/market/test_single_source.py:238: in test_differential_partition_grid
    assert out['gap_flag'].to_list() == flags
E   assert [False, True, True, True] == [False, False, False, False]
E     
E     At index 1 diff: True != False
E     Use -v to get more diff
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_partition_grid
1 failed in 1.36s
```

### restored（exit 0）

SHA256 `1b0ddff88d5a8716781cd89479261d99931b186208190fdbe0af1bcbda867374`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/partition_check.py
.                                                                        [100%]
1 passed in 2.55s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## vision-count — src/quant_lab/market/vision.py

真实替换：
```python
return grid_points_between(a, b, INTERVAL_SECONDS[interval])
# →
return int((b - a).total_seconds()) // INTERVAL_SECONDS[interval] + 1
```

### baseline（exit 0）

SHA256 `30f0dcdf25bd9e9869fe13d516fdd7a0d7646528e440cf5f85ff993147614cd8`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/vision.py
.                                                                        [100%]
1 passed in 0.04s
```

### mutant（exit 1）

SHA256 `7b958775ba0a60c66fc580244dad71d04d60dd887eccba4273d9cc57ae1b06ee`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/vision.py
F                                                                        [100%]
=================================== FAILURES ===================================
________________________ test_differential_vision_count ________________________
tests/market/test_single_source.py:258: in test_differential_vision_count
    assert vision.expected_rows(kind, interval, period) == c.grid_points_between(
E   AssertionError: assert 8353 == 8352
E    +  where 8353 = <function expected_rows at 0x10a54c720>('indexPriceKlines', '5m', '2024-02')
E    +    where <function expected_rows at 0x10a54c720> = vision.expected_rows
E    +  and   8352 = <function grid_points_between at 0x10a4d4f40>(datetime.datetime(2024, 2, 1, 0, 0, tzinfo=datetime.timezone.utc), datetime.datetime(2024, 3, 1, 0, 0, tzinfo=datetime.timezone.utc), 300)
E    +    where <function grid_points_between at 0x10a4d4f40> = c.grid_points_between
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_vision_count - A...
1 failed in 0.06s
```

### restored（exit 0）

SHA256 `30f0dcdf25bd9e9869fe13d516fdd7a0d7646528e440cf5f85ff993147614cd8`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/vision.py
.                                                                        [100%]
1 passed in 0.04s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## kernel-first — src/quant_lab/market/kernel_a.py

真实替换：
```python
first_expected = first_grid_point(self.t_start, bars[0].interval_s)
# →
first_expected = dt.datetime.fromtimestamp(-(-int(self.t_start.timestamp()) // interval_s) * interval_s, dt.UTC)
```

### baseline（exit 0）

SHA256 `d0582ac7a77ea000c3f901882e97802726000e087b1075ca1e5f204b9c169c82`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
.                                                                        [100%]
1 passed in 0.20s
```

### mutant（exit 1）

SHA256 `2e3018eea6bae286ecc99ca1f770c9552933bada76615b43a60e40528d384cef`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
F                                                                        [100%]
=================================== FAILURES ===================================
________________________ test_differential_kernel_grid _________________________
tests/market/test_single_source.py:281: in test_differential_kernel_grid
    assert kernel._first_bar_gap(bars, end) == want
E   AssertionError: assert datetime.datetime(2024, 1, 31, 23, 59, tzinfo=datetime.timezone.utc) == None
E    +  where datetime.datetime(2024, 1, 31, 23, 59, tzinfo=datetime.timezone.utc) = _first_bar_gap([Bar(open_time=datetime.datetime(2024, 2, 1, 0, 0, tzinfo=datetime.timezone.utc), o=Decimal('100'), h=Decimal('100'), ...zone.utc), o=Decimal('100'), h=Decimal('100'), l=Decimal('100'), c=Decimal('100'), volume=Decimal('0'), interval_s=60)], datetime.datetime(2024, 2, 1, 0, 3, 59, 999999, tzinfo=datetime.timezone.utc))
E    +    where _first_bar_gap = <quant_lab.market.kernel_a.KernelA object at 0x10893dac0>._first_bar_gap
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_kernel_grid - As...
1 failed in 0.24s
```

### restored（exit 0）

SHA256 `d0582ac7a77ea000c3f901882e97802726000e087b1075ca1e5f204b9c169c82`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
.                                                                        [100%]
1 passed in 0.20s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## kernel-middle — src/quant_lab/market/kernel_a.py

真实替换：
```python
if grid_points_between(prev + iv, o, interval_s) > 0:
# →
if grid_points_between(prev + iv, o, interval_s) >= 0:
```

### baseline（exit 0）

SHA256 `d0582ac7a77ea000c3f901882e97802726000e087b1075ca1e5f204b9c169c82`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
.                                                                        [100%]
1 passed in 0.19s
```

### mutant（exit 1）

SHA256 `42f34262d4c4cc6082b3a21c63357cd190ae67d9c03870ce9f9c764fb5d71b49`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
F                                                                        [100%]
=================================== FAILURES ===================================
________________________ test_differential_kernel_grid _________________________
tests/market/test_single_source.py:281: in test_differential_kernel_grid
    assert kernel._first_bar_gap(bars, end) == want
E   AssertionError: assert datetime.datetime(2024, 2, 1, 0, 0, tzinfo=datetime.timezone.utc) == None
E    +  where datetime.datetime(2024, 2, 1, 0, 0, tzinfo=datetime.timezone.utc) = _first_bar_gap([Bar(open_time=datetime.datetime(2024, 1, 31, 23, 59, tzinfo=datetime.timezone.utc), o=Decimal('100'), h=Decimal('100'...zone.utc), o=Decimal('100'), h=Decimal('100'), l=Decimal('100'), c=Decimal('100'), volume=Decimal('0'), interval_s=60)], datetime.datetime(2024, 2, 1, 0, 3, 58, 999999, tzinfo=datetime.timezone.utc))
E    +    where _first_bar_gap = <quant_lab.market.kernel_a.KernelA object at 0x107b6b530>._first_bar_gap
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_kernel_grid - As...
1 failed in 0.20s
```

### restored（exit 0）

SHA256 `d0582ac7a77ea000c3f901882e97802726000e087b1075ca1e5f204b9c169c82`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
.                                                                        [100%]
1 passed in 0.32s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## kernel-tail — src/quant_lab/market/kernel_a.py

真实替换：
```python
if grid_points_between(prev + iv, end, interval_s) > 0:
# →
if grid_points_between(prev + iv, end, interval_s) >= 0:
```

### baseline（exit 0）

SHA256 `d0582ac7a77ea000c3f901882e97802726000e087b1075ca1e5f204b9c169c82`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
.                                                                        [100%]
1 passed in 0.29s
```

### mutant（exit 1）

SHA256 `087e6399bc0a0f64aea96e45c56f19fbe596dc9f43c6d0acfd268e900902e51d`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
F                                                                        [100%]
=================================== FAILURES ===================================
________________________ test_differential_kernel_grid _________________________
tests/market/test_single_source.py:281: in test_differential_kernel_grid
    assert kernel._first_bar_gap(bars, end) == want
E   AssertionError: assert datetime.datetime(2024, 2, 1, 0, 4, tzinfo=datetime.timezone.utc) == None
E    +  where datetime.datetime(2024, 2, 1, 0, 4, tzinfo=datetime.timezone.utc) = _first_bar_gap([Bar(open_time=datetime.datetime(2024, 1, 31, 23, 59, tzinfo=datetime.timezone.utc), o=Decimal('100'), h=Decimal('100'...zone.utc), o=Decimal('100'), h=Decimal('100'), l=Decimal('100'), c=Decimal('100'), volume=Decimal('0'), interval_s=60)], datetime.datetime(2024, 2, 1, 0, 3, 58, 999999, tzinfo=datetime.timezone.utc))
E    +    where _first_bar_gap = <quant_lab.market.kernel_a.KernelA object at 0x107f175f0>._first_bar_gap
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_kernel_grid - As...
1 failed in 0.22s
```

### restored（exit 0）

SHA256 `d0582ac7a77ea000c3f901882e97802726000e087b1075ca1e5f204b9c169c82`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
.                                                                        [100%]
1 passed in 0.21s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## lake-count — src/quant_lab/market/execution.py

真实替换：
```python
expected = grid_points_between(a, min(b, dt.datetime.now(dt.UTC)), INTERVAL_SECONDS["1m"])
# →
expected = max(0, int((min(b, dt.datetime.now(dt.UTC)) - a).total_seconds()) // INTERVAL_SECONDS["1m"])
```

### baseline（exit 0）

SHA256 `9e5f278e994af1beb9736420cbc79c53322f43620056b6ea4e46a7567fba80a7`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/execution.py
.                                                                        [100%]
1 passed in 1.18s
```

### mutant（exit 1）

SHA256 `74441fbb9fef51b3c19ba1eb9babb0f1a97b48c075562eb043fdeec00fa3094f`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/execution.py
F                                                                        [100%]
=================================== FAILURES ===================================
_________________________ test_differential_lake_grid __________________________
tests/market/test_single_source.py:333: in test_differential_lake_grid
    assert market.bars_complete == (len(opens) == c.grid_points_between(start, end, 60))
E   AssertionError: assert False == (5 == 5)
E    +  where False = MarketView(manifest_id='fixture-E03', last=[], mark=[], bars_last=[Bar(open_time=datetime.datetime(2024, 1, 31, 23, 59... 'markPriceKlines 期望 4 根，实际 5', 'manifest missing fundingRate 2024-01', 'fundingRate 缺 2024-02-01T00:00:00+00:00 结算行']).bars_complete
E    +  and   5 = len([datetime.datetime(2024, 1, 31, 23, 59, tzinfo=datetime.timezone.utc), datetime.datetime(2024, 2, 1, 0, 0, tzinfo=date...ime(2024, 2, 1, 0, 2, tzinfo=datetime.timezone.utc), datetime.datetime(2024, 2, 1, 0, 3, tzinfo=datetime.timezone.utc)])
E    +  and   5 = <function grid_points_between at 0x10a64bd80>(datetime.datetime(2024, 1, 31, 23, 58, 59, 999999, tzinfo=datetime.timezone.utc), datetime.datetime(2024, 2, 1, 0, 3, 58, 999999, tzinfo=datetime.timezone.utc), 60)
E    +    where <function grid_points_between at 0x10a64bd80> = c.grid_points_between
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_lake_grid - Asse...
1 failed in 0.49s
```

### restored（exit 0）

SHA256 `9e5f278e994af1beb9736420cbc79c53322f43620056b6ea4e46a7567fba80a7`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/execution.py
.                                                                        [100%]
1 passed in 1.96s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## lake-start — src/quant_lab/market/execution.py

真实替换：
```python
a = req.resolved_t_start(_rp(req.policy_version)) - dt.timedelta(seconds=window_before_s)
# →
a = (req.t_start or req.t_dec) - dt.timedelta(seconds=window_before_s)
```

### baseline（exit 0）

SHA256 `9e5f278e994af1beb9736420cbc79c53322f43620056b6ea4e46a7567fba80a7`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/execution.py
.                                                                        [100%]
1 passed in 1.93s
```

### mutant（exit 1）

SHA256 `24cc5d8ea30b26387eb651e1affee9460ac925ae3bff406d735e03eb1595e2e5`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/execution.py
F                                                                        [100%]
=================================== FAILURES ===================================
___________________________ test_differential_start ____________________________
tests/market/test_single_source.py:375: in test_differential_start
    assert actual.bars_complete
E   AssertionError: assert False
E    +  where False = MarketView(manifest_id='a28', last=[], mark=[], bars_last=[Bar(open_time=datetime.datetime(2024, 2, 1, 0, 0, tzinfo=da... 'markPriceKlines 期望 4 根，实际 3', 'manifest missing fundingRate 2024-01', 'fundingRate 缺 2024-02-01T00:00:00+00:00 结算行']).bars_complete
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_start - Assertio...
1 failed in 0.58s
```

### restored（exit 0）

SHA256 `9e5f278e994af1beb9736420cbc79c53322f43620056b6ea4e46a7567fba80a7`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/execution.py
.                                                                        [100%]
1 passed in 2.06s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## start-validator — src/quant_lab/market/contract.py

真实替换：
```python
exp_t_start = derived_t_start(self.t_dec, pol)
# →
exp_t_start = (self.t_dec + dt.timedelta(seconds=pol.latency_s)).replace(microsecond=0)
```

### baseline（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 2.20s
```

### mutant（exit 1）

SHA256 `8ea17a005807e65aed3bd46f78f358ac4819eba895c3a754aa617c4605c8fbff`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
F                                                                        [100%]
=================================== FAILURES ===================================
___________________________ test_differential_start ____________________________
tests/market/test_single_source.py:361: in test_differential_start
    req = _build(_plan(), t_dec, policy)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
tests/market/test_single_source.py:170: in _build
    return c.build_request(row, policy_version=policy.version, policy_hash=policy.content_hash,
src/quant_lab/market/contract.py:540: in build_request
    return ExecutionRequest(
src/quant_lab/market/contract.py:464: in _chk
    raise ContractError("t_start 不能早于 t_dec（解析后的启动时刻）")
E   quant_lab.market.contract.ContractError: t_start 不能早于 t_dec（解析后的启动时刻）
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_start - quant_la...
1 failed in 0.35s
```

### restored（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 1.74s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## start-resolver — src/quant_lab/market/contract.py

真实替换：
```python
return self.t_start if self.t_start is not None else derived_t_start(self.t_dec, policy)
# →
return self.t_start if self.t_start is not None else (self.t_dec + dt.timedelta(seconds=policy.latency_s)).replace(microsecond=0)
```

### baseline（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 1.61s
```

### mutant（exit 1）

SHA256 `c40f524bee263789741033c27ea28de36f18c8dee6a4111394d53d2ee4aac406`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
F                                                                        [100%]
=================================== FAILURES ===================================
___________________________ test_differential_start ____________________________
tests/market/test_single_source.py:362: in test_differential_start
    assert req.resolved_t_start(policy) == start
E   AssertionError: assert datetime.datetime(2024, 1, 31, 23, 58, 59, tzinfo=datetime.timezone.utc) == datetime.datetime(2024, 1, 31, 23, 58, 59, 999999, tzinfo=datetime.timezone.utc)
E    +  where datetime.datetime(2024, 1, 31, 23, 58, 59, tzinfo=datetime.timezone.utc) = resolved_t_start(ExecutionPolicy(version='a28-input', latency_s=0, ladder_steps=2, costs={'base': CostSpec(maker_fee=Decimal('0'), take...zon_s=432000, entry_ttl_s=86400, entry_fraction_rule='equal', tp_fraction_rule='equal', tp_total_fraction=Decimal('1')))
E    +    where resolved_t_start = ExecutionRequest(episode_id='E03', graph_version='gv-fixture', decision_snapshot_hash='dsh-E03', t_dec=datetime.dateti...e='one_way', entry_ttl_s=86400, entry_fractions=(Decimal('1'),), tp_fractions=(Decimal('1'),), horizon_source='policy').resolved_t_start
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_start - Assertio...
1 failed in 0.20s
```

### restored（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 1.91s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## start-builder — src/quant_lab/market/contract.py

真实替换：
```python
horizon_end = derived_t_start(t_dec, policy) + dt.timedelta(seconds=derived_window_s(plan, policy, ttl))
# →
horizon_end = (t_dec + dt.timedelta(seconds=policy.latency_s)).replace(microsecond=0) + dt.timedelta(seconds=derived_window_s(plan, policy, ttl))
```

### baseline（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 1.88s
```

### mutant（exit 1）

SHA256 `81646fe0a454a8618f21b60a00544669d01d6fb2d48096711338b4f2b6a4df49`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
F                                                                        [100%]
=================================== FAILURES ===================================
___________________________ test_differential_start ____________________________
tests/market/test_single_source.py:361: in test_differential_start
    req = _build(_plan(), t_dec, policy)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
tests/market/test_single_source.py:170: in _build
    return c.build_request(row, policy_version=policy.version, policy_hash=policy.content_hash,
src/quant_lab/market/contract.py:540: in build_request
    return ExecutionRequest(
src/quant_lab/market/contract.py:493: in _chk
    raise ContractError(f"horizon_source=policy 但 horizon_end 与推导值不一致：窗口 {window} != {dt.timedelta(seconds=derived_s)}"
E   quant_lab.market.contract.ContractError: horizon_source=policy 但 horizon_end 与推导值不一致：窗口 5 days, 23:59:59.000001 != 6 days, 0:00:00（自选观察窗请显式标 horizon_source='caller'，它会进 trace_hash 并由 G3 记入尝试账本）
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_start - quant_la...
1 failed in 0.21s
```

### restored（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 1.82s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## start-lower-bound — src/quant_lab/market/contract.py

真实替换：
```python
if self.horizon_end <= exp_t_start:
# →
if self.horizon_end < exp_t_start:
```

### baseline（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 1.89s
```

### mutant（exit 1）

SHA256 `768120656a9322045e611c190679803e23f525f06ebe881ec6f7071ca3adce8a`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
F                                                                        [100%]
=================================== FAILURES ===================================
___________________________ test_differential_start ____________________________
tests/market/test_single_source.py:384: in test_differential_start
    assert _accepts(data) == (valid_spelling and data['horizon_end'] > start)
E   AssertionError: assert True == ((True and datetime.datetime(2024, 1, 31, 23, 58, 59, 999999, tzinfo=datetime.timezone.utc) > datetime.datetime(2024, 1, 31, 23, 58, 59, 999999, tzinfo=datetime.timezone.utc)))
E    +  where True = _accepts({'episode_id': 'E03', 'graph_version': 'gv-fixture', 'decision_snapshot_hash': 'dsh-E03', 't_dec': datetime.datetime(2024, 1, 31, 23, 58, 59, 999999, tzinfo=datetime.timezone.utc), ...})
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_start - Assertio...
1 failed in 0.64s
```

### restored（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 2.17s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## window-validator — src/quant_lab/market/contract.py

真实替换：
```python
window = self.horizon_end - exp_t_start
# →
window = dt.timedelta(seconds=int((self.horizon_end - exp_t_start).total_seconds()))
```

### baseline（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 0.30s
```

### mutant（exit 1）

SHA256 `14ea2d9571c48ecc28fe8322a7487b968265b06ba529fa11bee8e9835bb8a5c9`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
F                                                                        [100%]
=================================== FAILURES ===================================
___________________________ test_differential_window ___________________________
tests/market/test_single_source.py:410: in test_differential_window
    assert _accepts(data) == want
E   AssertionError: assert True == False
E    +  where True = _accepts({'episode_id': 'E03', 'graph_version': 'gv-fixture', 'decision_snapshot_hash': 'dsh-E03', 't_dec': datetime.datetime(2024, 1, 31, 23, 58, 59, 999999, tzinfo=datetime.timezone.utc), ...})
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_window - Asserti...
1 failed in 0.23s
```

### restored（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 0.32s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## window-builder — src/quant_lab/market/contract.py

真实替换：
```python
horizon_end = derived_t_start(t_dec, policy) + dt.timedelta(seconds=derived_window_s(plan, policy, ttl))
# →
horizon_end = derived_t_start(t_dec, policy) + dt.timedelta(seconds=ttl + (plan.expiry.max_holding_s or policy.max_horizon_s))
```

### baseline（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 0.32s
```

### mutant（exit 1）

SHA256 `92333ef2358c55d985b8bd8264524a3f2758f3db8d40d07718536cd6dc1755e7`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
F                                                                        [100%]
=================================== FAILURES ===================================
___________________________ test_differential_window ___________________________
tests/market/test_single_source.py:399: in test_differential_window
    req = _build(plan, t_dec, policy)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^
tests/market/test_single_source.py:170: in _build
    return c.build_request(row, policy_version=policy.version, policy_hash=policy.content_hash,
src/quant_lab/market/contract.py:540: in build_request
    return ExecutionRequest(
src/quant_lab/market/contract.py:489: in _chk
    raise ContractError(f"观察窗 {window} 超过 policy {self.policy_version} 的安全上限 "
E   quant_lab.market.contract.ContractError: 观察窗 4 days, 15:07:40 超过 policy a28-input 的安全上限 max_horizon_s=400000s（亚秒偏移同样超限）
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_window - quant_l...
1 failed in 0.36s
```

### restored（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 0.30s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## window-derived-validator — src/quant_lab/market/contract.py

真实替换：
```python
derived_s = derived_window_s(self.order_plan, pol, exp_ttl)
# →
derived_s = exp_ttl + (self.order_plan.expiry.max_holding_s or pol.max_horizon_s)
```

### baseline（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 0.28s
```

### mutant（exit 1）

SHA256 `98c2f9718e5072b8c3ab9e23befdf09942c6b52d734069a82df5d4ee2df158f6`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
F                                                                        [100%]
=================================== FAILURES ===================================
___________________________ test_differential_window ___________________________
tests/market/test_single_source.py:399: in test_differential_window
    req = _build(plan, t_dec, policy)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^
tests/market/test_single_source.py:170: in _build
    return c.build_request(row, policy_version=policy.version, policy_hash=policy.content_hash,
src/quant_lab/market/contract.py:540: in build_request
    return ExecutionRequest(
src/quant_lab/market/contract.py:493: in _chk
    raise ContractError(f"horizon_source=policy 但 horizon_end 与推导值不一致：窗口 {window} != {dt.timedelta(seconds=derived_s)}"
E   quant_lab.market.contract.ContractError: horizon_source=policy 但 horizon_end 与推导值不一致：窗口 0:03:03 != 4 days, 15:07:40（自选观察窗请显式标 horizon_source='caller'，它会进 trace_hash 并由 G3 记入尝试账本）
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_window - quant_l...
1 failed in 0.24s
```

### restored（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 0.30s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## ttl-before — src/quant_lab/market/contract.py

真实替换：
```python
"entry_ttl_s": resolve_entry_ttl_s(plan, pol)
# →
"entry_ttl_s": pol.entry_ttl_s
```

### baseline（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 0.27s
```

### mutant（exit 1）

SHA256 `1312d1bef00a4d96f05405da8cf45a5285de16747a5ffd259cab18f30ba6d779`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
F                                                                        [100%]
=================================== FAILURES ===================================
____________________________ test_differential_ttl _____________________________
tests/market/test_single_source.py:435: in test_differential_ttl
    assert _accepts(data) == want
E   AssertionError: assert False == True
E    +  where False = _accepts({'episode_id': 'E03', 'graph_version': 'gv-fixture', 'decision_snapshot_hash': 'dsh-E03', 't_dec': datetime.datetime(2024, 1, 31, 23, 58, 59, 999999, tzinfo=datetime.timezone.utc), ...})
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_ttl - AssertionE...
1 failed in 0.21s
```

### restored（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 0.19s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## ttl-validator — src/quant_lab/market/contract.py

真实替换：
```python
exp_ttl = resolve_entry_ttl_s(self.order_plan, pol)
# →
exp_ttl = pol.entry_ttl_s
```

### baseline（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 0.18s
```

### mutant（exit 1）

SHA256 `022b09ee012b3dec87b75ac8408d150f527774d9801d76a8e2a6f99ef84cc938`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
F                                                                        [100%]
=================================== FAILURES ===================================
____________________________ test_differential_ttl _____________________________
tests/market/test_single_source.py:426: in test_differential_ttl
    req = _build(plan, t_dec, policy)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^
tests/market/test_single_source.py:170: in _build
    return c.build_request(row, policy_version=policy.version, policy_hash=policy.content_hash,
src/quant_lab/market/contract.py:540: in build_request
    return ExecutionRequest(
src/quant_lab/market/contract.py:483: in _chk
    raise ContractError(f"entry_ttl_s 与{src}解析值不一致：{self.entry_ttl_s} != {exp_ttl}")
E   quant_lab.market.contract.ContractError: entry_ttl_s 与计划解析值不一致：1 != 321
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_ttl - quant_lab....
1 failed in 0.21s
```

### restored（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 0.20s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## ttl-builder — src/quant_lab/market/contract.py

真实替换：
```python
ttl = resolve_entry_ttl_s(plan, policy)
# →
ttl = policy.entry_ttl_s
```

### baseline（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 0.18s
```

### mutant（exit 1）

SHA256 `d6a3b1f6d83c1614da7be656ba99fd451e76477565a8d16c74f9435211fa1660`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
F                                                                        [100%]
=================================== FAILURES ===================================
____________________________ test_differential_ttl _____________________________
tests/market/test_single_source.py:426: in test_differential_ttl
    req = _build(plan, t_dec, policy)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^
tests/market/test_single_source.py:170: in _build
    return c.build_request(row, policy_version=policy.version, policy_hash=policy.content_hash,
src/quant_lab/market/contract.py:540: in build_request
    return ExecutionRequest(
src/quant_lab/market/contract.py:483: in _chk
    raise ContractError(f"entry_ttl_s 与{src}解析值不一致：{self.entry_ttl_s} != {exp_ttl}")
E   quant_lab.market.contract.ContractError: entry_ttl_s 与计划解析值不一致：321 != 1
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_ttl - quant_lab....
1 failed in 0.21s
```

### restored（exit 0）

SHA256 `f84428aa6f48fab639c3df562ff9a9e2996f21a7fc4e43ad49415c4ed3e60291`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py
.                                                                        [100%]
1 passed in 0.21s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## expiry-timeline — src/quant_lab/market/kernel_a.py

真实替换：
```python
deadline = entry_expiry_at(self.t_start, self.req.entry_ttl_s)
        if deadline <= end:
# →
deadline = (self.t_start + dt.timedelta(seconds=self.req.entry_ttl_s)).replace(microsecond=0)
        if deadline <= end:
```

### baseline（exit 0）

SHA256 `d0582ac7a77ea000c3f901882e97802726000e087b1075ca1e5f204b9c169c82`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
.                                                                        [100%]
1 passed in 0.22s
```

### mutant（exit 1）

SHA256 `dc5bf4c0875af9d850441b4c81a74dffa1f1be872c712cf3c3ec2b54014041fa`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
F                                                                        [100%]
=================================== FAILURES ===================================
______________________ test_differential_expiry_timeline _______________________
tests/market/test_single_source.py:467: in test_differential_expiry_timeline
    assert [moment.ts for moment in moments if moment.expiry] == expected
E   assert [datetime.dat...timezone.utc)] == []
E     
E     Left contains one more item: datetime.datetime(2024, 2, 1, 0, 1, tzinfo=datetime.timezone.utc)
E     Use -v to get more diff
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_expiry_timeline
1 failed in 0.27s
```

### restored（exit 0）

SHA256 `d0582ac7a77ea000c3f901882e97802726000e087b1075ca1e5f204b9c169c82`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
.                                                                        [100%]
1 passed in 0.20s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## expiry-timeline-bound — src/quant_lab/market/kernel_a.py

真实替换：
```python
if deadline <= end:
# →
if deadline < end:
```

### baseline（exit 0）

SHA256 `d0582ac7a77ea000c3f901882e97802726000e087b1075ca1e5f204b9c169c82`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
.                                                                        [100%]
1 passed in 0.23s
```

### mutant（exit 1）

SHA256 `1dcf30cd475fba869cef9f02ac0113858291cf055ca72163f6ad5b64c7081834`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
F                                                                        [100%]
=================================== FAILURES ===================================
______________________ test_differential_expiry_timeline _______________________
tests/market/test_single_source.py:467: in test_differential_expiry_timeline
    assert [moment.ts for moment in moments if moment.expiry] == expected
E   assert [] == [datetime.dat...timezone.utc)]
E     
E     Right contains one more item: datetime.datetime(2024, 2, 1, 0, 1, 0, 999999, tzinfo=datetime.timezone.utc)
E     Use -v to get more diff
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_expiry_timeline
1 failed in 0.19s
```

### restored（exit 0）

SHA256 `d0582ac7a77ea000c3f901882e97802726000e087b1075ca1e5f204b9c169c82`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
.                                                                        [100%]
1 passed in 0.21s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## expiry-orders — src/quant_lab/market/kernel_a.py

真实替换：
```python
deadline = entry_expiry_at(self.t_start, self.req.entry_ttl_s)
        for i, (e, p, q) in enumerate(legs):
# →
deadline = (self.t_start + dt.timedelta(seconds=self.req.entry_ttl_s)).replace(microsecond=0)
        for i, (e, p, q) in enumerate(legs):
```

### baseline（exit 0）

SHA256 `d0582ac7a77ea000c3f901882e97802726000e087b1075ca1e5f204b9c169c82`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
.                                                                        [100%]
1 passed in 0.19s
```

### mutant（exit 1）

SHA256 `46fe6c547e34396ebc73f793f75ff57ea677b5fdd5451898fb5c23dea9f511cb`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
F                                                                        [100%]
=================================== FAILURES ===================================
_______________________ test_differential_expiry_orders ________________________
tests/market/test_single_source.py:477: in test_differential_expiry_orders
    assert [order.deadline for order in orders] == [expiry] * len(orders)
E   assert [datetime.dat...timezone.utc)] == [datetime.dat...timezone.utc)]
E     
E     At index 0 diff: datetime.datetime(2024, 2, 1, 0, 1, tzinfo=datetime.timezone.utc) != datetime.datetime(2024, 2, 1, 0, 1, 0, 999999, tzinfo=datetime.timezone.utc)
E     Use -v to get more diff
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_expiry_orders - ...
1 failed in 0.17s
```

### restored（exit 0）

SHA256 `d0582ac7a77ea000c3f901882e97802726000e087b1075ca1e5f204b9c169c82`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/kernel_a.py
.                                                                        [100%]
1 passed in 0.18s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## expiry-b — src/quant_lab/market/nautilus_adapter.py

真实替换：
```python
deadline = entry_expiry_at(t_start, req.entry_ttl_s)
# →
deadline = (t_start + dt.timedelta(seconds=req.entry_ttl_s)).replace(microsecond=0)
```

### baseline（exit 0）

SHA256 `580c782a26924ddbad1d576a4ca107b475e74d92a01a57e2fdd22fa68fee5650`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/nautilus_adapter.py
.                                                                        [100%]
1 passed in 1.70s
```

### mutant（exit 1）

SHA256 `93508dd01932ee32e33966c8fb8d6220995c1c0e55245a7106def6830e4b8bd6`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/nautilus_adapter.py
F                                                                        [100%]
=================================== FAILURES ===================================
__________________________ test_differential_expiry_b __________________________
tests/market/test_single_source.py:502: in test_differential_expiry_b
    assert expired[0].reason == 'horizon_end'
E   AssertionError: assert 'entry_ttl' == 'horizon_end'
E     
E     - horizon_end
E     + entry_ttl
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_expiry_b - Asser...
1 failed in 1.12s
```

### restored（exit 0）

SHA256 `580c782a26924ddbad1d576a4ca107b475e74d92a01a57e2fdd22fa68fee5650`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/nautilus_adapter.py
.                                                                        [100%]
1 passed in 1.16s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。

## expiry-b-bound — src/quant_lab/market/nautilus_adapter.py

真实替换：
```python
TimeInForce.GTD if deadline <= end else TimeInForce.GTC
# →
TimeInForce.GTD if deadline < end else TimeInForce.GTC
```

### baseline（exit 0）

SHA256 `580c782a26924ddbad1d576a4ca107b475e74d92a01a57e2fdd22fa68fee5650`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/nautilus_adapter.py
.                                                                        [100%]
1 passed in 1.18s
```

### mutant（exit 1）

SHA256 `4074659c55f2818731864050c5c15d5cfda38b9137a541b1ae42e64111f32fe1`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/nautilus_adapter.py
F                                                                        [100%]
=================================== FAILURES ===================================
__________________________ test_differential_expiry_b __________________________
tests/market/test_single_source.py:500: in test_differential_expiry_b
    assert expired[0].reason == 'entry_ttl'
E   AssertionError: assert 'horizon_end' == 'entry_ttl'
E     
E     - entry_ttl
E     + horizon_end
=========================== short test summary info ============================
FAILED tests/market/test_single_source.py::test_differential_expiry_b - Asser...
1 failed in 1.13s
```

### restored（exit 0）

SHA256 `580c782a26924ddbad1d576a4ca107b475e74d92a01a57e2fdd22fa68fee5650`

```text
IMPORTED /Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/nautilus_adapter.py
.                                                                        [100%]
1 passed in 1.20s
```

内容还原相等：True；裁定：RED→内容还原一致→GREEN。
