# G1 r4 残余闭合：实现者自验证据（供第五轮独立复审）

2026-09-11；当前 GPT-6 Codex 主会话执行，无 Grok / 子 Agent / 真实 provider。
这是实现与实测记录，不是第五轮独立复审 PASS。

## Changed files

开始时 data 源码与测试目录已未跟踪；以下按本会话实际内容修改记录，不把全仓库已有改动归入本任务。

```text
src/quant_lab/data/api.py
src/quant_lab/data/audit.py
src/quant_lab/data/dedup.py
src/quant_lab/data/extract.py
src/quant_lab/data/graph.py
src/quant_lab/data/lake.py
src/quant_lab/data/lifecycle.py
src/quant_lab/data/linker.py
src/quant_lab/data/llm.py
src/quant_lab/data/normalize.py
src/quant_lab/data/validate.py
tests/data/test_dedup.py
tests/data/test_extract.py
tests/data/test_lifecycle.py
tests/data/test_review_probes.py
tests/data/fixtures/draw_numbers.py
tests/data/fixtures/gen_ocr_recorded.py
tests/data/fixtures/llm_recorded/ocr_v1.json
tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_106@09-03-2024_10-00-00.png
tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_108@12-03-2024_08-00-00.png
tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_110@20-03-2024_11-00-00.png
tests/data/R5_CLOSURE_EVIDENCE.md
```

未编辑 contracts、taskList.json、docs/adr 或其它模块；未向共享 quant-lab/data、data/lake、data/lockbox 写入或删除。
仅临时湖、指定两目录内的源码/测试/合成夹具、临时验证日志发生本任务写入。
未启动真实 LLM/OCR/网络调用；无 services 导入。3 张 OCR 图现实际绘出数字旁的品种、方向、ENTRY/STOP 标签，已逐张目视复核。

版本：normalize/extract/validate/linker/lifecycle 升 v0.5，dedup 升 v0.3，parser 升 v0.5，OCR 录制夹具升 v3。
lifecycle.RULE_VERSION 在 gold schema/审核记录结构修改前已升为 tg-lifecycle-v0.5。
公共四个 API 签名保留，Reason 集合仍 29 项。

## 验证命令与最终输出

cwd 为 quant-lab。所有命令使用 bash `-o pipefail`，环境：

```bash
export QUANT_LAB_DATA_ROOT=$(mktemp -d)
export PYTHONDONTWRITEBYTECODE=1
export PYTEST_ADDOPTS='-p no:cacheprovider'
```

命令 P（下表所有探针及新增闭合断言均已实际运行）：

```bash
.venv-g1/bin/python -m pytest tests/data/test_review_probes.py -q -s
# 87 passed in 21.99s; exit=0
```

最终全套：

```bash
.venv-g1/bin/python -m pytest tests/data -q
# 222 passed in 47.10s; exit=0
```

初始将 r4 §9 原始源码逐语句载入测试，9 项全部失败；修复后相同源码保留，T01 的预期 LookupError 转为负向观察。
r2 §9 / r3 §10 原探针与 C3 补充仍全部执行；仅原夹具 ID 用 ANCHORS 映射，业务输入与期望没有缩减。

## 每项闭合证据

下表测试名省略 `test_` 前缀，均由 P 覆盖；只有验收断言通过的性质在此记为实现者闭合。

| 项 | 已执行用例 / 命令 | 关键输出 / 通过的断言 |
|---|---|---|
| V01 / S08 | P: r4_original_counterexamples[V01], v01_noninteger_roundtrip_and_diagnostic_hash, decimal_physical_types_and_q12；gates.log | 原始/规范化值均 60000.123456789123，类型 Decimal；EX/CP/EP Parquet 往返价格、止损、TP 末位均保持123；区间中点精确；修改 delta_lo 不改语义输入哈希；六用途键、29原因码、四API签名保持。 |
| V02 / S05 | P: r4_original_counterexamples[V02], v02_default_copy_family_and_future_invariance；D-07: a9_index_and_a12_cluster_sharing | 默认视图两根都为 dg-96b3e0d1274dfce4；一根→两根→三根，旧行所有列（含 hash/stratum）相等。按各根 t_dec 重建当时可见复制图，使用稳定家族锚；真实公共默认API的非空同文组共享簇，不折叠。 |
| V03 / S14 | P: s14_formal_history_and_full_audit, v03_formal_evidence_rejection, r4_original_counterexamples[V03] | 正式记录绑定已校验 manifest 的 graph_version/input_hash/batch；样本必须属于被审 EP，有可重算ID哈希及逐样本完整字段标签/严重度/独立审核者。首次 fail/attempt1 → 有修复文件引用和内容哈希的新样本 pass/attempt2 → 换计划名仍 insufficient/attempt3；中途改名、无修复、空样本、伪造样本、错哈希、部分标签、错误数不符均拒；小批全审需覆盖实物全集。r4 无 sample_ids 的三次尝试全 insufficient，0份正式记录。 |
| V04 / S09（reopen） | P: r4_original_counterexamples[V04], v04_reopen_guards, s09_reopen_is_persisted_without_staling_predecessor | 同币同作者、旧根已终态的 reply 产生唯一前驱及 reopen migration；跨币、跨作者、两者皆跨分别无 predecessor、migration_reason=null，始终保留独立新根；旧根不被置 stale。 |
| S03 | P: r4_original_counterexamples[S03], s03_recursive_parent_album_edge_and_root, r03_full_album_closure | 父相册晚一天：A*=2024-06-11 09:01Z；refs 含 media:image:image-hash；实际子边推迟到 T+1d、所属根 t_dec=T+1d+1s，事件 refs 含父媒体。DFS 的缺节点/空时钟/循环拒收、用途区分与原晚父链仍通过。 |
| S09（撤回） | P: s09_assertion_withdrawal（两种文本×有/无reply）, s09_missing_assertion_is_unresolved, s09_unlocatable_correction_is_unresolved | “撤回上一条断言”/“上一条作废”解析 correction；按指向的源事件定位后 superseded=true，supersedes_event_id 非空、replay_required=true、描述六用途全false；找不到对象则 unresolved+LIFECYCLE_INVALID。原未来correction不改旧默认决策视图的测试仍通过。 |
| S10 | P: r4_original_counterexamples[S10], s10_edit_only_version_evidence_and_loss, r10_c3_hash_all_used_inputs；D-03 | same_id=false，different_event_time=true，rows=2，quarantine=0；两行均 edit_date，L1两输入/两映射，合法观察无需虚构Q。R10a edit-only 的正确期望已按 r4 §6/§7改为2。版本期限/MV时钟输入变更改hash，同输入发布幂等测试继续通过。 |
| S11 / T01 | P: r4_original_counterexamples[T01], t01_real_parquet_counts_are_verified（3计数×2 API）, r11_x11_t01_manifest_and_stale；D-07 | 虚假全0counts→LookupError；分别错episodes/events/decision_roots均被两API拒读。实读两份Parquet核对行数；原19个元数据缺键/空值/缺文件、别名、stale与tombstone测试保留。 |
| S12 | P: r4_original_counterexamples[S12], s12_all_six_physical_input_sets_and_cumulative_exits, r12_physical_denominators；D-04 | mv=248，L2 mapped=248，missing=0；5个service显式excluded/NOT_SIGNAL。L1物理观察数=251，L1输出=MV全集；L2..L6实物输入集−MAP输入集全部空。review/quarantine/dup_ref及excluded关系按输入身份并集累计，逐层逐stratum等于LOSS累计排除数，计数恒等式全部通过。 |
| S13 / T03 | P: r4_original_counterexamples[T03/S13], t03_u01_size_and_bbox_matrix, s13_*；D-05 | 缺/非法bbox数字不进入计划：kind=undecidable，entry/stop=null，reason=OCR_UNREADABLE。同数值跨品种/方向不能match；数量、名义金额、仓位比例、期限逐字段验证；完全同数值但无bbox也不match。10%/1小时的有效原始数字bbox分别支撑0.1/3600，正例通过。3张录制图实际包含计划标签，非法size与像素证据测试保留。 |
| S15 / r4 §7 | P + D-03/D-05/D-07 | edit-only=2；T03断言最终用途拒收而非仅空bbox；A12读默认公共视图并要求至少一个实测同文配对，空集不能通过。原216 source/三频道72ID/五时间级/多年份/CE1–CE5矩阵继续通过。 |
| S01/S02/S04/S06/S07/S16（已closed回归） | P: r01_x01_public_all_columns, r02_h1_no_retroactive_entry, r04_x04_unknown_market_evidence, r06_t02_no_price_or_fraction_guessing, r07_x07_quarantine_inheritance, r16_media_path_guard | R01/X01全列diff=[]；H1在10:21不可见；未来行情/校准拒用；非价格单位不误传播且未知分配不猜值；上游拒因保留；路径逃逸在hash前拒绝。 |
| T02/T04/U01（已closed回归） | P: r06_t02_no_price_or_fraction_guessing, t03_u01_size_and_bbox_matrix；D-07 | 显式fraction保留、未来fraction不改旧快照；D-07只删除自身mktemp目录；非法OCR尺寸有界拒答。 |

A12报告实跑：单独在新 mktemp 根 build 后执行 `api.main(['--loss','latest'])`，输出 `unconfirmed_repost_candidates=12, n_reviewed=0, status=not_run, confirmation_rate=null`。
默认视图56行，manifest实物counts为 episodes=75/events=124/decision_roots=61。decision_roots统计有t_dec的根，公共API还应用用途过滤，所以61与56不是不一致。
此临时构建版本为 fixture-v1@63866609；不把它当共享湖更新。

## taskList 原 verify：逐条退出码

以下命令从看板读取后原样执行；未改看板或降低阈值。

### D-03

```bash
.venv-g1/bin/python -m pytest tests/data/test_normalize.py -q && .venv-g1/bin/python -m quant_lab.data.normalize --fixture tests/data/fixtures/tdesktop_sample --out /tmp/ql-norm && .venv-g1/bin/python -c "import polars as pl;d=pl.read_parquet('/tmp/ql-norm/message_version.parquet');assert d.height>0;print(d.height)"
```

```text
exit=0
19 passed in 0.49s
```

### D-04

```bash
.venv-g1/bin/python -m pytest tests/data/test_dedup.py -q
```

```text
exit=0
11 passed in 0.70s
```

### D-05

```bash
.venv-g1/bin/python -m pytest tests/data/test_extract.py -q && .venv-g1/bin/python -m quant_lab.data.extract --bench ../eval/v3_trader_signal_bench --report /tmp/ql-extract.json && .venv-g1/bin/python -c "import json;r=json.load(open('/tmp/ql-extract.json'));assert r['n_items']>=30 and r['parser_recall']>0;print(r)"
```

```text
exit=0
19 passed in 0.66s
```

### D-06

```bash
.venv-g1/bin/python -m pytest tests/data/test_normalize_validate.py -q
```

```text
exit=0
9 passed in 0.70s
```

### D-07

```bash
tmp=$(mktemp -d) && QUANT_LAB_DATA_ROOT=$tmp .venv-g1/bin/python -m quant_lab.data.api --build --fixture tests/data/fixtures/tdesktop_sample --graph-version fixture-v1 --llm-fixture tests/data/fixtures/llm_recorded/extract_v1.json --ocr-fixture tests/data/fixtures/llm_recorded/ocr_v1.json > /dev/null && .venv-g1/bin/python -m pytest tests/data/test_linker.py tests/data/test_lifecycle.py -q && QUANT_LAB_DATA_ROOT=$tmp .venv-g1/bin/python -c "from quant_lab.data.api import load_episodes;d=load_episodes('fixture-v1');assert d.height>=5;assert all(d[c].null_count()==0 for c in ('instrument_id','t_dec','cluster_id'));print(d.select('episode_id','author_plan_state','author_claim_state','cluster_id'))" && rm -rf "$tmp"
```

```text
exit=0
33 passed in 25.44s
shape: (56, 4)
```

### D-09

```bash
.venv-g1/bin/python -m pytest tests/data/test_audit.py -q && .venv-g1/bin/python -m quant_lab.data.audit --oc 200 3 --oc 400 0 | grep -E '0\.5%.*98\.1|0\.5%.*13\.4'
```

```text
exit=0
9 passed in 2.21s
p=0.5%  200/3 accept=98.1319%  400/0 accept=13.4658%
```

D-03 原命令复用固定 /tmp/ql-norm；其累计行数不作新鲜批次证据。独立测试与另一次全新 mktemp 构建提供新鲜输入验证。
D-07 的 rm 目标仅为原命令自己的 mktemp；未清理共享湖或既有版本。

另显式执行 `assert report['n_items'] >= 30 and report['parser_recall'] >= 0.9`（读取本轮D-05写出的 /tmp/ql-extract.json）：exit=0，n_items=30，n_actionable=16，parser_recall=1.0，parser_strict_recall=0.75。
D-09 的两个 OC 值均有原单元测试的独立数值断言，不只依赖 OR grep。

## 证据位置与剩余风险

- 完整逐条原始输出：`/tmp/g1-r5-verification/{pytest,probes,D-03,D-04,D-05,D-06,D-07,D-09,gates,fresh-build}.log`；准确命令与退出码在 `results.json`。
- 最终整轮验证前后99个源码/测试/夹具文件摘要相同，`stability.json` 为 `changed=[]`。本证据文档写于验证完成之后；旧 REVIEW_EVIDENCE.md 保留，未覆盖历史记录。
- 第五轮独立复审尚未执行，没有独立 PASS/signoff。本报告仅证明列明的离线反例和验收断言。
- parser_recall=1.0 是30条bench上的粗动作召回；strict_recall=0.75，不能冒称字段全正确或真实数据质量已通过。
- 每根按截止重算复制图、API读前实读Parquet核对计数增加计算/I/O；本轮合成全套47.10s，未验证大型真实批次性能。
- 正式审核 schema 已增加制品身份、样本标签与修复证据。旧不完整正式记录不在本次临时测试中迁移；没有触碰共享湖，也不声称旧记录已兼容升级。
- 未执行真实批次验收、真实PoC、真实provider、共享湖发布或下游接入。D-08不在用户本轮指定验收清单内，保持原gated边界。
