# 十一审修复回执（部分完成）

S40/S41、B20/B21 已完成并验证。**B19 未实现，整体目标未闭合。**

## 未闭合原因与待裁定

规格 `docs/adr/design-G2-force-close-net-r.md` §2 签名不接受 `risk_budget`、`cost_scenario`，但 §3 公式必须使用两者；现有 `ExecutionResult` 不包含这两项。不得猜测风险预算或静默固定 base。已请求允许增加两个必填关键字参数，等待裁定。

`ExecutionResult.gross_pnl` 类型允许 None，A/B 实际结果路径均保留真实已实现损益。应明确缺失时拒绝估值，还是获准由真实事件重建；不能视为零。未改模型、未实现 C06、未生成估值事件。

§4/§6 的真实 G3 调用登记、消费哨兵和未来价突变需要 G3 消费方。目前 research 源码没有 force_close_net_R/net_R_forced 调用，且本任务禁止写 research/。未虚构登记或以测试消费者代替真实 G3 证明；B19 七条突变尚未执行。

## Changed files（相对 quant-lab）

| 文件 | 修改 |
|---|---|
| src/quant_lab/market/partition_check.py | 相邻缺口改为 grid_points_between；合法网格筛选、首尾计数和 funding 容差不变 |
| src/quant_lab/market/single_source.py | 函数内直接 Add/Sub 定义使用检查；固定每函数调用次数；P7 覆盖 Assign/AnnAssign/AugAssign/NamedExpr/IfExp/直接字段返回；P3/P5 补 AugAssign |
| src/quant_lab/market/policy_hashes.json | B20 解除占位、B21 声明追溯；五个哈希字符串未改 |
| tests/market/test_single_source.py | 新增调用次数门、6 个 TTL 节点、3 个拆行定义、2 个增量二元表达测试；原测试不改 |
| tests/market/test_review_p1_round2.py | 旧占位期断言按 B20/B21 改为定稿与声明追溯断言 |
| tests/market/p11_mutation_evidence.py | 可复跑逐条突变工具；finally 恢复原始字节，然后 SHA256 比对，绝不以返回码判断还原 |
| tests/market/p11_mutation_results.json | 23 组注入/叠加/恢复原始 pytest 输出、退出码和完整前后 SHA256 |
| tests/market/P11G-rerun.txt | 报告原命令及最终完整真实输出 |
| tests/market/P11TTL-rerun.txt | 报告原命令及最终完整真实输出 |
| tests/market/p11_hash_before.json | 修改前真实 resolve_policy 哈希基线 |
| tests/market/p11_hash_comparison.txt | 修改后逐版本 resolve_policy 对比输出 |
| tests/market/p11-receipt.md | 本回执 |

## 突变自证表

全部记录的源文件恢复均有 **完整 sha256_before == sha256_after**；随后指定测试 GREEN。原始失败堆栈与每条 hash 见 [JSON](p11_mutation_results.json)，具体变换见 [脚本](p11_mutation_evidence.py)。

| 注入 | 原缺陷结果 | A26 叠加 | SHA256 相等 | 还原 |
|---|---|---|---|---|
| P1_inline_latency:latency | RED | — | True | GREEN |
| P2_int_total_seconds:int-seconds | RED | — | True | GREEN |
| P3_duration_div:duration-div | RED | — | True | GREEN |
| P4_t_start_or_t_dec:start-or | RED | — | True | GREEN |
| P5_inline_entry_ttl:expiry-add | RED | — | True | GREEN |
| P6_inline_grid_comparison:middle-grid | RED | — | True | GREEN |
| P6_inline_grid_comparison:tail-grid | RED | — | True | GREEN |
| P7_inline_ttl_resolution:ttl-expression | RED | — | True | GREEN |
| P7_inline_ttl_resolution:ttl-branches | RED | — | True | GREEN |
| P7:annotated | GREEN | RED；A23 非行为证据、不可达防御 | True | GREEN |
| P7:augmented | GREEN | RED；A23 非行为证据、不可达防御 | True | GREEN |
| P7:named | GREEN | RED；A23 非行为证据、不可达防御 | True | GREEN |
| P7:return | RED | — | True | GREEN |
| P7:comprehension | GREEN | RED；A23 非行为证据、不可达防御 | True | GREEN |
| P7:return-field | RED | — | True | GREEN |
| P6:delta = o - prev | RED | — | True | GREEN |
| P6:delta: int = o - prev | RED | — | True | GREEN |
| P6:delta -= prev | RED | — | True | GREEN |
| P3_duration_div:AugAssign | RED | — | True | GREEN |
| P5_inline_entry_ttl:AugAssign | RED | — | True | GREEN |
| S40-count | RED | — | True | GREEN |
| B20-placeholder | RED | — | True | GREEN |
| B21-provenance | RED | — | True | GREEN |

P7 的注解/增量/海象/推导式节点单删访问入口后，另一个表达式检查仍拦截；叠加停用 P7 性质检查后指定回归均失败。因此按 A26 归为冗余防御，没有将仍绿直接判通过。

## 审查方原命令复跑输出

直接从只读 review-G2-P1.md 提取 P11G/P11TTL 原命令执行，仅增加 PYTHONDONTWRITEBYTECODE=1 环境；未改报告、未改原命令注入文本。

```text
P11G
2 failed, 24 passed in 1.81s
INJECTED 1
26 passed in 1.74s
RESTORED 0 SHA256_EQUAL True

P11TTL
1 failed, 25 passed in 2.11s
INJECTED 1
26 passed in 1.54s
RESTORED 0 SHA256_EQUAL True
```

P11G 两个失败分别是树级 P6 和调用次数登记，尾段调用保留；P11TTL 失败是树级 P7。完整命令和堆栈见上述两个 rerun.txt。

## 五个版本 content_hash 前后实跑对比

```text
fixture-zero-v1 BEFORE 12ca10ebb10ef27d10873584ee42a224c44efdf99e9b55c7656e8a838c58cd1d AFTER 12ca10ebb10ef27d10873584ee42a224c44efdf99e9b55c7656e8a838c58cd1d EQUAL True
fixture-tick-v1 BEFORE 79a818107c42ffc1aec9af8ca4beb672231adfaf4b514c8eb0b63664fbb0238f AFTER 79a818107c42ffc1aec9af8ca4beb672231adfaf4b514c8eb0b63664fbb0238f EQUAL True
fixture-halftp-v1 BEFORE b5b9db9aaec5d88e1574d028a0807cdd34cff3d5141d93f2390edd69ccf338c9 AFTER b5b9db9aaec5d88e1574d028a0807cdd34cff3d5141d93f2390edd69ccf338c9 EQUAL True
fixture-wallet99-v1 BEFORE 982b510678f507dfec7269216e68ba24479f161624cdfebf48bc0b28d417f41f AFTER 982b510678f507dfec7269216e68ba24479f161624cdfebf48bc0b28d417f41f EQUAL True
base-v1 BEFORE 12f83216444dff123f7cc6e9b4f4752449b8a678555079f033cbb9980f07d5db AFTER 12f83216444dff123f7cc6e9b4f4752449b8a678555079f033cbb9980f07d5db EQUAL True
```

## 四条验收命令真实输出摘要

全部在 quant-lab 目录逐条前台执行，使用 .venv-g2/bin/python，PYTHONDONTWRITEBYTECODE=1；pytest 禁用 cacheprovider。

```text
PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python -m pytest tests/market -q -p no:cacheprovider
305 passed in 19.22s
exit 0

PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python -m pytest tests/market/test_single_source.py -q -p no:cacheprovider -v
collected 26 items
26 passed in 2.17s
exit 0

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A
kernel=A passed=22 failed=0
exit 0

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1
{"codes": {"MATCH": 12, "B_COMMAND_LATENCY": 3, "GAP_PRICE": 1, "SAME_TS_PRIORITY": 4, "GTD_BOUNDARY": 1, "B_LIQUIDITY_MODEL": 1}, "median_ms": {"A": 13.125041004968807, "B": 58.524915992165916}}
exit 0
```

report 的 unsupported 原样仍为 S02/S06/S07/S12（非本轮范围）；codes 无 UNEXPLAINED 项，按零计为 0，六个分类逐项与指定基线相同。

额外行为校验：test_partition_check.py + test_review_p1_round10.py = 32 passed in 1.43s。全量初次 1 failed/304 passed，仅因旧测试断言占位字段存在；按 B20 修正该断言后 305 全绿。没有调整交易夹具或分类基线。

## Remaining risks

- B19 的接口缺项与 G3 消费边界待裁定；该函数、A24 新禁令及七条自证未交付，不能宣告整体完成。
- def-use 只处理同函数中直接 Add/Sub 定义与已枚举比较，不进行跨函数别名/复杂控制流或任意语义等价推导。
- 调用次数是静态使用次数：能够发现本次同函数删一个使用点，不声称防御“同时删除一个调用并在别处补一个无用调用”等对抗改写。
- 分区中段逐行调用 helper 取代向量时间差；本次行为回归通过，未做大规模性能基准。
- 所有修改仅在授权 market 源码/测试目录；未改禁止文件，未连接私有交易 API，未读取 services/nautilus-node 配置。
