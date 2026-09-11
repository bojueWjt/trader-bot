# G2 四审问题实现报告

2026-09-11；实现者：GPT-6。仅记录实现处置与实跑证据，不代写独立复核结论。
`review-G2-P1.md` 的终裁与判定表未修改。本报告的“已闭合”指对应反例的实现验收，最终判定仍由下一轮独立复核负责。

四审 11 条 open / partial-P2：**已闭合 3（S05/S16/S17），留 P2 8**。留 P2 不等于本次未修：S04 的暴露残余、S06 的未知冲突、S10 的报告与缺结果门禁、S11 的异常 CLI / 矩阵 / 跨进程重放、S13 的事件精度已补实现或测试；其余验收边界逐条列出。

## 逐项处置

下表新测试均在 `tests/market/test_review_fixes.py`，除非注明既有文件。全量实跑包含表内所有测试；新增文件单跑 **49 passed in 3.41s**。

| ID | 处置（理由与归属） | 回归测试 | 一句实跑摘要 |
|---|---|---|---|
| S02 | 留 P2：真实结算时点 U03、变周期完整性与引擎余额证明依赖 G0 结算口径及 G2 P2；不把 adapter funding 账本当引擎余额。`P2_UNSUPPORTED` 和 report JSON 显式 unsupported，loader notes 明确仅 archive calc_time。 | 既有 `test_funding.py`、`test_c2_loader_missing_funding_and_unknown_rules`；新增 `test_p2_report_exposes_unsupported_capabilities` | 全量 232 passed；report 输出 S02 unsupported，引擎余额核对 not_run。 |
| S04 | 留 P2：本次修复 S16 对应的 hold 暴露窗；仍不宣称 B 完整账户/资金事件因果均已证明，B 原生命令排空与资金处理的独立证明归 G2 P2/U03（与 S02/S10 同一边界）。 | 既有 `test_c1_s04_hold_end_precedes_funding_and_b_truncates`、`test_s04_*`；新增 `test_s16_exposure_uses_observation_boundary` | 四种截止回归全过；原 hold 暴露反例由 0 改为手算 .4R。 |
| S05 | 已闭合：保留既有 OHLC loader 语义；gap_flag 缺列/null 起点拒收，OHLC 任一经济字段 null 体检隔离；尖刺计算的未知输入不再标清洁。主键未知显式拒绝，无法生成可信 gap/conflict 结论。 | 既有 `test_s05_null_ohlc_valid_fails_closed`；新增 `test_s05_partition_unknown_ohlc_is_quarantined`（5 参数）、`test_s05_loader_unknown_quality`（4 参数）、`test_s05_spike_unknown_gap_is_not_clean` | 首次反例 7 项失败，修复后通过；既有四审 2 项启动基线通过，隔离反演 S05 再现失败。 |
| S06 | 留 P2：未知经济值的重复行现已隔离；bronze 深度重放、多来源版本化 manifest 属 G1 湖/G2 P2，不扩造契约。入湖 manifest 新增 unsupported_capabilities；报告输出同一边界。 | 新增 `test_s06_vision_unknown_duplicate_values_quarantined`；既有 `test_s06_conflicting_keys_quarantined_and_bronze_retained`、`test_s14_revised_source_supersedes_old_silver_days` | 新反例先失败后通过，未知重复行 days=[] 且 quarantine_n>0；全量通过。 |
| S07 | 留 P2：管理命令流 E13/C01、完整预留生命周期需 G0 C01/G2 P2；没有新增命令字段绕过契约。请求传 management_commands 实际被 extra_forbidden 拒绝，报告显式 unsupported。 | 新增 `test_s07_management_stream_rejected_at_request_boundary`；既有 `test_s07_*`、`test_c3_s07_equity_based_affordability` | 请求边界拒绝与既有静态账户回归均通过。 |
| S10 | 留 P2：报告“余额已核”已纠正为“引擎余额核对 not_run”；缺 A/B 结果均 NOT_RUN；冻结分类严格性保留。冻结差异集合仍不能代替独立逐事件经济证明，后者归 G2 P2/独立复核，不在实现报告自证。 | 新增 `test_s10_missing_a_cannot_match`、`test_s10_report_balance_evidence`；既有 `test_s10_*` | 两条新反例先失败后通过；A/B 正常报告未知码为 0，缺 A 不再 MATCH。 |
| S11 | 留 P2：本窗口的 CLI 异常出口、22 例完整成本×路径×内核矩阵、B 跨进程确定性均补齐；真实账户/真实分区仍依赖 S02 与离线门禁，E13 随 C01，归 G2 P2/G0。 | 新增 `test_s11_report_cli_exception_returns_failure`、`test_s11_cost_path_matrix`、`test_s11_all_episodes_cost_path_invariants`、`test_s11_b_replay_across_processes`；既有 CLI 门禁 | B 异常原先在性能重跑抛错，现在 exit1 且 NOT_RUN=22；22×2×3×2 组合不变量/重放通过，两独立进程 22 个 B hash 一致。 |
| S12 | 留 P2：版本化湖快照解析属 G1/G0 身份契约；loader 仅 active silver，notes 明示，显式 snapshot_id 请求抛 ContractError(unsupported)，不静默加载当前版本。供应链完整身份仍归 P2。 | 新增 `test_s12_snapshot_request_explicitly_unsupported`；既有 `test_s12_*`、`test_c3_s12_b_rejects_stale_policy_and_registry_pins_hash`、`test_replay.py` | snapshot_id 在读湖前拒绝；既有哈希/版本登记/重放测试通过。 |
| S13 | 留 P2：新增 CanonicalEvent Decimal(38,12) 校验，并在 check_invariants 再验事件精度，阻断 model_copy/归约对象把细小金额量化隐藏。共享容量、当前可成交价、预留余额的独立逐价点审计仍非现有结果层不变量能力；归 G2 P2，预留生命周期依赖 G0 C01。 | 新增 `test_s13_event_decimal_precision_rejected`、`test_s13_event_precision_checked_by_invariants`；既有 `test_c1_s13_closed_causality_precision_and_step`、`test_s13_event_level_invariants` | 两个精度反例均先失败后通过；既有终态因果、step、非法数量回归未退化。 |
| S16 | 已闭合：观察截止取 horizon（等号前）、真实平仓、真实删失（等号前）、首次开仓+hold（等号前）最早者，独立于最后订单事件；成本均价只用当时已发生的入场。 | 新增 `test_s16_exposure_uses_observation_boundary`（hold/horizon/closed/funding_censor）、`test_s16_horizon_equal_mark_does_not_extend_exposure`；既有 `test_s16_b_exposure_respects_hold_cutoff` | hold/horizon/censor 原 MFE=0 反例先失败后 .4R；平仓 qty1×价差5/risk5=1R；horizon 等号 mark1000 被排除；截止后 mark 变更不改事件与暴露。 |
| S17 | 已闭合（既有 G2 修复保留）：显式 fractions 逐值 Decimal 校验及等于 plan/policy 推导结果原样保留；不重复改写该实现。 | 既有 `test_s17_explicit_fractions_cannot_bypass_policy_or_precision`（round4） | 启动基线 2 passed；仅在 /tmp 隔离副本撤掉既有校验后该测试失败，正式代码专项再跑 7 passed（含 S17）。 |

## 先失败、后通过的证据

实际未修版本上先创建测试，再改实现；未改既有断言、未删测试、未按内核输出回填 episode 期望。

- 首轮：`.venv-g2/bin/python -m pytest tests/market/test_review_fixes.py -q` → **13 failed, 3 passed in 3.11s**。覆盖 OHLC null、gap 未知、S16 三种截止、S10 两条、S13 事件入口精度。
- 第二轮选择 `spike_unknown or unknown_duplicate or cli_exception or cost_path` → **3 failed, 12 passed, 16 deselected in 2.12s**。失败项为未知 gap 尖刺标记、同 null 重复值、B 异常的 CLI 输出。
- 第三轮选择 `all_episodes or precision_checked` → **1 failed, 12 passed, 35 deselected in 2.36s**。失败项为不变量接收超精度事件 fee。
- 第四轮选择 `horizon_equal` → **1 failed**，同刻极端 mark 被计入 MFE；修复后该项通过。
- 完整新增文件单跑：**49 passed in 3.41s**（之后增强 funding_censor 用例以排除 hold 干扰，正式全量再次通过）；正式全量最终：**232 passed in 16.65s**。

S05/S17 在开始任务前已经修好，所以不伪称本次修改前指定 round4 会失败：启动实跑为 **2 passed in 2.47s**。为核实测试具有检错能力，复制 market 包到 `/tmp/quant-g2-legacy-repro/quant_lab/market`，只在隔离副本把 OHLC 分支还原为 `(~df["ohlc_valid"]).any()`、撤去标有 S17 的逐值精度/推导一致性校验块；原工作区实现从未回退。执行：

```sh
.venv-g2/bin/python -m pytest tests/market/test_review_p1_round4.py -q -o pythonpath=/tmp/quant-g2-legacy-repro
```

实际 **2 failed in 1.76s**，分别命中未知 OHLC 放行及伪造 fractions 未拒绝。此为四审缺陷的隔离反演证据，不冒充历史 checkout。本报告保留准备方法；正式源码与正式命令随后全绿。

## 实现边界与冻结

- `execution.py`：未知 gap 质量会设置 bars_quality_ok=False；已知 gap 仍保留现有按时间洞删失语义。保留 S05 原有 OHLC 校验。
- `partition_check.py`：OHLC 表达式 null→False，非有限 volume 不合格；未知时间键显式拒绝；尖刺输入未知不再生成清洁标记，合法首收益/缺口后的统计样本不足仍维持原来的 score 缺失语义。
- `vision.py`：同键同 null 经济值不能作为精确相同副本进入 active silver；所有候选进入现有 quarantine。主键未知直接拒绝，避免无法构造冲突身份。
- `contract.py`：CanonicalEvent 与事件不变量都校验 Decimal；四项跨窗口能力以可机读 P2_UNSUPPORTED 暴露。未改 contracts/*。
- `nautilus_adapter.py`：B v0.3 的暴露窗口与逐时均价修复；异常结果不再进入性能重跑；报告判定同时考虑 NOT_RUN/A_GOLD_FAIL/空集；失败时性能明确 not_run。默认 report 仅输出 JSON 摘要，**显式 --out 才写 Markdown**，避免本次指定命令改写授权外的旧 A/B 报告。
- `ab_explanations.json` 按任务约束4的明确授权更新。v0.3 重冻前逐个运行22例并校验不变量，完整 diff_keys / allowed_scalar_diffs / frozen_b_scalars 重新核算；**既有22例的差异集合与标量恰好没有变化**，只有 kernel_b_version 变为 v0.3。再核验 22/22。没有新增解释码、没有接受集合外差异；EXC/缺结果和冻结集合相等门禁保留。Episode JSON 未改。
- 矩阵分两层：12组简单 bars 请求的手算数量/毛利/净额；22例×2成本×3路径×2内核的派生请求不变量与确定性。派生矩阵不宣称拥有新增独立经济金标，也不按其输出生成金标。
- 不联网；既有 fetch 单测只走 MockTransport 合成包。未升级 .venv-g2，未调用收费模型、未接触生产、未新增 services import。真实分区测试及其 skipif 未修改。

## 四条正式验收命令

cwd：`/Users/balen/projects/trader-bot/quant-lab`。四条命令真实 exit code 均为 **0**。

### 市场测试

```sh
.venv-g2/bin/python -m pytest tests/market -q
```

```text
........................................................................ [ 31%]
........................................................................ [ 62%]
........................................................................ [ 93%]
................                                                         [100%]
232 passed in 16.65s
```

### 三/四审专项

```sh
.venv-g2/bin/python -m pytest tests/market/test_review_p1_round4.py tests/market/test_review_p1_round3.py -q -v
```

```text
============================= test session starts ==============================
platform darwin -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0
rootdir: /Users/balen/projects/trader-bot/quant-lab
configfile: pyproject.toml
plugins: anyio-4.15.1
collected 7 items

tests/market/test_review_p1_round4.py ..                                 [ 28%]
tests/market/test_review_p1_round3.py .....                              [100%]

============================== 7 passed in 3.59s ===============================
```

### A 金标回放

```sh
.venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A
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

### A/B 报告

```sh
.venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1
```

```text
{"codes": {"MATCH": 12, "B_COMMAND_LATENCY": 3, "GAP_PRICE": 1, "SAME_TS_PRIORITY": 4, "GTD_BOUNDARY": 1, "B_LIQUIDITY_MODEL": 1}, "median_ms": {"A": 9.06974999816157, "B": 56.247875007102266}, "unsupported": {"S02": {"status": "unsupported", "capability": "real_settlement_time_U03_and_engine_balance", "owner": "G0 settlement contract + G2 P2"}, "S06": {"status": "unsupported", "capability": "bronze_depth_replay_and_multi_source_versions", "owner": "G1 lake + G2 P2"}, "S07": {"status": "unsupported", "capability": "management_commands_E13_C01_and_reservation_lifecycle", "owner": "G0 C01 + G2 P2"}, "S12": {"status": "unsupported", "capability": "versioned_lake_snapshot_resolution", "owner": "G1 lake + G0 identity contract"}}}
```

A/B 分布合计22：MATCH12、B_COMMAND_LATENCY3、GAP_PRICE1、SAME_TS_PRIORITY4、GTD_BOUNDARY1、B_LIQUIDITY_MODEL1；UNEXPLAINED/A_GOLD_FAIL/NOT_RUN 均0。一次性能测量仅记录，不作门槛。

## Changed files 与复核身份

本任务改动清单（不包括工作区已有其它窗口改动）：

- `src/quant_lab/market/contract.py`，SHA256 `408c9c45e8fbad15e45a0b9f9de2ff15936c33c00b52ddb0252d2397d997d1c0`
- `src/quant_lab/market/execution.py`，SHA256 `090559fbd35415fad63f382e8912b56d621928dbada78eb683da25e360f99316`
- `src/quant_lab/market/partition_check.py`，SHA256 `c43b0cf5b3d432b4eb3aeacb254473f00a45452fc5d7ad54a12ba85af4355451`
- `src/quant_lab/market/vision.py`，SHA256 `c8dd4f8520f5854edbaeeb282a958280b7b6906321f56d06de951543f32e1f6d`
- `src/quant_lab/market/nautilus_adapter.py`，SHA256 `dac286eee891f8a48c01fa535956d1a532209f6a0f01ee48c0d49a4ce692db9c`
- `src/quant_lab/market/ab_explanations.json`，SHA256 `2d9da013340add3431f96b3cec4a246fe4cbf546679fb4f8755685f10a281a20`
- `tests/market/test_review_fixes.py`，SHA256 `91d46063cd6836d8c3aebe37f84caaa018a92d4412794f8d57dd76122f36ca8c`
- `docs/adr/report-G2-review-fixes.md`（本次唯一新增文档）。

剩余风险：表中8条仍留P2；B仍是失败情景有明确解释的候选 spike，不能用本次全绿宣称完成定型、真实余额或真实湖验收。独立逐事件经济证明、当前规则/共享容量/预留审计不得用冻结标量或不变量全绿替代。未修改独立复核的终裁文档与看板。
