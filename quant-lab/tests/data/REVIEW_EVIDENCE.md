# G1 P1 实现与自验记录（2026-09-11）

本文件是实现者自验记录，不是第四轮独立复审结论。当前：176 tests passed；42 条复审回归 passed；D-03…D-07、D-09 exit=0；D-08 exit=1。因此不能宣称完整验收通过。

## Changed files

本次编辑/新增以下源码、测试和夹具。开始时两个 data 目录已是未跟踪目录，git diff 无法区分其内部历史改动；此清单按本会话实际编辑列出。

```text
src/quant_lab/data/api.py
src/quant_lab/data/audit.py
src/quant_lab/data/extract.py
src/quant_lab/data/graph.py
src/quant_lab/data/lifecycle.py
src/quant_lab/data/linker.py
src/quant_lab/data/llm.py
src/quant_lab/data/market_stub.py
src/quant_lab/data/normalize.py
src/quant_lab/data/sources.py
src/quant_lab/data/validate.py
tests/data/test_dedup.py
tests/data/test_extract.py
tests/data/test_lifecycle.py
tests/data/test_linker.py
tests/data/test_normalize.py
tests/data/test_normalize_validate.py
tests/data/test_review_probes.py
tests/data/fixtures/gen_tdesktop_sample.py
tests/data/fixtures/gen_ocr_recorded.py
tests/data/fixtures/draw_numbers.py
tests/data/fixtures/fixture_matrix.py
tests/data/REVIEW_EVIDENCE.md
```

生成产物：`tests/data/fixtures/tdesktop_sample/{ANCHORS.json,README.md,SPEC.json}`，三个频道各自的 `result.json` / `observations.jsonl` / `photos/*.png`，`DeltaLive/live.jsonl`，以及 `tests/data/fixtures/llm_recorded/{extract_v1.json,ocr_v1.json}`。下附具体生成文件库存；export_manifest 为生成器重写的元数据，不声称其内容必然变化。`gen_llm_recorded.py` 只执行，未编辑。

版本：lifecycle `tg-lifecycle-v0.4`；normalize / extract / validate / linker 相应升至 v0.4，gold schema 变更不复用旧 lifecycle 版本。

## 逐项自验证据

下表除 T04 外全部对应实际执行的 P 命令；函数名前缀省略 `test_`，`*` 表示该前缀下已执行的多个用例。P 直接加载 r2 §9 / r3 §10 探针源码，只有夹具 message_id 按原 ANCHORS 语义转换。报告中 X 编号并不连续；执行报告实际包含的全部探针及 C3 补充，不虚构不存在的 X 编号。期望断言为手写。

```bash
# P：工作目录 quant-lab；QUANT_LAB_DATA_ROOT 指向 mktemp
set -o pipefail
.venv-g1/bin/python -m pytest tests/data/test_review_probes.py -q -s
# exit=0；42 passed in 1.60s
```

| 项 | 已执行的回归用例/命令 | 关键输出或通过的断言 |
|---|---|---|
| S01 | r01_x01_public_all_columns | R01 / X01-correction / X01-original：全列 diff=[]；未来 correction 不污染决策视图。 |
| S02 | r02_h1_no_retroactive_entry | 10:21 不可见，available_at=10:25；H1 无 t_dec/order_plan。 |
| S03 | r03_full_album_closure；s03_* | 晚到相册成员使 t_dec 从 03-04 推至 03-05；白灰黑 DFS 拒环、未知依赖关闭决策；字段/规则/price_check 依赖与 sequence 实证分别断言。 |
| S04 | r04_x04_unknown_market_evidence | future/missing_available/null mark 均无可用价格；无样本校准拒用；无时钟列测试保持通过。 |
| S05 | r05_identity_and_branch_guards；s05_* | 跨币及同币明确独立单各生成两个根；Titan 阶梯单一个分支；唯一 quote 强边、多源弱边；plan_ref 作者隔离。 |
| S06 | r06_t02_no_price_or_fraction_guessing | 其他币锚不触发换算，entry=6/stop=5/tp=7；无依据报价 entry=null；显式 TP fraction=0.25。 |
| S07 | r07_x07_quarantine_inheritance | 四类上游 quarantine 均 execution=false；冲突 severity=fatal。 |
| S08 | r08_x08_schema_and_source_clocks；decimal_physical_types_and_q12 | 原因码仍 29；source_clock_mismatches=0；all-clock=[]；价格 Decimal(38,12)，金额哈希 q12；公共签名保留。 |
| S09 | r09_observation_horizon_and_delete；s09_* | delete_notice 进入事件与 episode 删除证据；correction 定位并 supersede、replay_required、禁止全部估计用途；无法定位 unresolved；reopen 前驱与迁移落盘、重跑幂等。 |
| S10 | r10_c3_hash_all_used_inputs | 只改 MV available_at：input_hash_equal=false、snapshot_equal=false；修改同版本 TP fraction 改 hash，未来事件不改变决策。 |
| S11 | r11_x11_t01_manifest_and_stale | 缺 manifest、缺/空 files 均 LookupError；实际图版本 stale 后 episode/events 均 0 行。 |
| S12 | r12_physical_denominators | L3 canonical MV 输入集覆盖；L4 上层251/MAP251，L5 上层153/MAP153，L6 上层153/MAP153，unaccounted 均0；服务/非信号 excluded。 |
| S13 | r13_x13_c3_recorded_protocol；s13_* | 未来 sent_context=[] 时 unresolved；第21个未发送候选 unresolved；sent_chars=7153；逐字段 OCR 冲突、真实数字像素与 bbox、模糊配对拒答均断言通过。 |
| S14 | r14_c3_no_partial_label_pass；s14_formal_history_and_full_audit | 缺/半标签 insufficient；追加 signoffs/acceptance_records；历史 attempt=1/2，第三次拒绝；小批仅全审记录 pass；producer/reviewer 交集拒绝。 |
| S15 | r15_fixture_spec；s15_source_matrix_and_cross_attributes | 216 unique sources、248 MV；三频道各 ID 1..72；每频道≥8编辑 source、其中≥2有两次编辑；H0/H1/H2/V/U、2024/2025/2026；seed=20260911；逐类配额手写断言。 |
| S16 | r16_media_path_guard | absolute_path / parent_escape / symlink_escape 均 exists=false、sha256=null。 |
| T01 | r11_x11_t01_manifest_and_stale | 19 个必要元数据/值级 schema 反例全部 LookupError。 |
| T02 | r06_t02_no_price_or_fraction_guessing | 同版本 fraction unknown→0.25 改 snapshot；未来更新 snapshot_equal=true、plan_equal=true。 |
| T03 | t03_u01_size_and_bbox_matrix；s13_fixture_pixels_and_unreadable_pair | 负坐标/短 bbox/NaN/Inf/倒置/越界均丢弃；3组可读图均256×80，recorded/actual一致，bbox覆盖绘制数字。 |
| T04 | D-07 原 verify（见下） | 临时根构建 exit=0；33 passed；公共视图56行，instrument_id/t_dec/cluster_id 全非空；仅原命令自身清理该 mktemp。 |
| U01 | t03_u01_size_and_bbox_matrix | size=[8]、[bad,8]、[8,NaN] 均 status=unreadable、numbers=[]。 |

原始逐条 probe 输出：`/tmp/g1-verification/review-probes.log`。没有把探针脚本“运行无异常”当成业务通过；回归测试对输出加了验收断言。

## Verification

所有湖构建使用临时 QUANT_LAB_DATA_ROOT；设置 `PYTHONDONTWRITEBYTECODE=1`、`PYTEST_ADDOPTS=-p no:cacheprovider`。D-03 原命令指定的 `/tmp/ql-norm` 保持不改，故其中 MV 行数可能累积，不能当成新鲜批次计数。

```bash
cd quant-lab
set -o pipefail
export QUANT_LAB_DATA_ROOT=$(mktemp -d)
.venv-g1/bin/python -m pytest tests/data -q
# exit=0；176 passed in 6.47s
```

### D-03

从 taskList 读取并原样执行，未改 verify 语义：

```bash
.venv-g1/bin/python -m pytest tests/data/test_normalize.py -q && .venv-g1/bin/python -m quant_lab.data.normalize --fixture tests/data/fixtures/tdesktop_sample --out /tmp/ql-norm && .venv-g1/bin/python -c "import polars as pl;d=pl.read_parquet('/tmp/ql-norm/message_version.parquet');assert d.height>0;print(d.height)"
```

```text
exit=0
18 passed in 0.62s
```

### D-04

从 taskList 读取并原样执行，未改 verify 语义：

```bash
.venv-g1/bin/python -m pytest tests/data/test_dedup.py -q
```

```text
exit=0
11 passed in 0.37s
```

### D-05

从 taskList 读取并原样执行，未改 verify 语义：

```bash
.venv-g1/bin/python -m pytest tests/data/test_extract.py -q && .venv-g1/bin/python -m quant_lab.data.extract --bench ../eval/v3_trader_signal_bench --report /tmp/ql-extract.json && .venv-g1/bin/python -c "import json;r=json.load(open('/tmp/ql-extract.json'));assert r['n_items']>=30 and r['parser_recall']>0;print(r)"
```

```text
exit=0
19 passed in 0.37s
```

### D-06

从 taskList 读取并原样执行，未改 verify 语义：

```bash
.venv-g1/bin/python -m pytest tests/data/test_normalize_validate.py -q
```

```text
exit=0
9 passed in 0.42s
```

### D-07

从 taskList 读取并原样执行，未改 verify 语义：

```bash
tmp=$(mktemp -d) && QUANT_LAB_DATA_ROOT=$tmp .venv-g1/bin/python -m quant_lab.data.api --build --fixture tests/data/fixtures/tdesktop_sample --graph-version fixture-v1 --llm-fixture tests/data/fixtures/llm_recorded/extract_v1.json --ocr-fixture tests/data/fixtures/llm_recorded/ocr_v1.json > /dev/null && .venv-g1/bin/python -m pytest tests/data/test_linker.py tests/data/test_lifecycle.py -q && QUANT_LAB_DATA_ROOT=$tmp .venv-g1/bin/python -c "from quant_lab.data.api import load_episodes;d=load_episodes('fixture-v1');assert d.height>=5;assert all(d[c].null_count()==0 for c in ('instrument_id','t_dec','cluster_id'));print(d.select('episode_id','author_plan_state','author_claim_state','cluster_id'))" && rm -rf "$tmp"
```

```text
exit=0
33 passed in 3.58s
shape: (56, 4)
```

### D-08

从 taskList 读取并原样执行，未改 verify 语义：

```bash
.venv-g1/bin/python -m quant_lab.data.harvest --report /tmp/ql-1a.json && .venv-g1/bin/python -c "import json;r=json.load(open('/tmp/ql-1a.json'));assert r['n_messages']>0 and 'edit_ratio' in r;print(r)"
```

```text
exit=1
NotImplementedError: harvest 待实现（PoC 1a/1b）；args={'report': '/tmp/ql-1a.json', 'export_dir': None, 'allow_network': False}
```

### D-09

从 taskList 读取并原样执行，未改 verify 语义：

```bash
.venv-g1/bin/python -m pytest tests/data/test_audit.py -q && .venv-g1/bin/python -m quant_lab.data.audit --oc 200 3 --oc 400 0 | grep -E '0\.5%.*98\.1|0\.5%.*13\.4'
```

```text
exit=0
9 passed in 0.55s
p=0.5%  200/3 accept=98.1319%  400/0 accept=13.4658%
```

单独指定的 bench 命令已由 D-05 执行：

```bash
.venv-g1/bin/python -m quant_lab.data.extract --bench ../eval/v3_trader_signal_bench --report /tmp/ql-extract.json
```

结果：`n_items=30`，`n_actionable=16`，`parser_recall=1.0`（≥0.9）；`parser_strict_recall=0.75`，不宣称字段全正确。报告和逐命令日志在 `/tmp/g1-verification/`，bench 明细在 `/tmp/ql-extract.json`。

## Remaining risks / 未闭合验收

1. **D-08 未通过**：现有 harvest 是 skeleton，原 verify 未提供 export_dir，直接抛 NotImplementedError。当前没有用户授权的真实研究账号导出输入；本次又禁止真实网络。未用合成夹具冒充 PoC 1a 真实结果，也未修改 verify/任务状态。完整验收的“D-03…D-09 全 exit=0”仍不满足。

2. **第四轮独立复审未运行**：这里只证明实现者的离线自测和报告探针断言通过；不存在独立 reviewer 的 PASS/signoff。新增审计表的测试 reviewer ID 仅用于临时测试记录。

3. **工作树整体不是仅两目录 dirty**：开始时已有范围外改动；运行期间基线记录中的18个范围外文件又发生变化（见 `/tmp/g1-verification/boundary.json`）。本会话没有写这些文件，也没有清理/回滚他人工作。`git status --short quant-lab` 的39行实测结果在 `/tmp/g1-verification/git-status.txt`；不能声称该全树检查满足“只出现两目录”。

4. 未运行真实 LLM/OCR/网络调用、真实湖发布或生产接入；所有模型行为为 RecordedClient/RecordedOcr。临时测试不能替代真实 PoC 数据质量验收。历史规则版本可用时间使用显式历史冻结假设（规则节点非空版本），并在依赖闭包携带规则版本；真实规则注册/冻结时间治理仍需独立复审确认。

5. 最终命令执行前后，`src/quant_lab/data` 与 `tests/data` 内容 SHA-256 比较无变化（排除 __pycache__）；证据在 `/tmp/g1-verification/stability.json`。本证据文档于测试后写入，不影响 Python/夹具内容。未写入或删除共享 quant-lab/data、data/lake、data/lockbox。

## 生成夹具文件库存

```text

tests/data/fixtures/tdesktop_sample/ANCHORS.json

tests/data/fixtures/tdesktop_sample/AlphaSignals/export_manifest.json

tests/data/fixtures/tdesktop_sample/AlphaSignals/observations.jsonl

tests/data/fixtures/tdesktop_sample/AlphaSignals/photos/matrix_45.png

tests/data/fixtures/tdesktop_sample/AlphaSignals/photos/matrix_46.png

tests/data/fixtures/tdesktop_sample/AlphaSignals/photos/matrix_50.png

tests/data/fixtures/tdesktop_sample/AlphaSignals/photos/matrix_52.png

tests/data/fixtures/tdesktop_sample/AlphaSignals/photos/matrix_54.png

tests/data/fixtures/tdesktop_sample/AlphaSignals/photos/matrix_55.png

tests/data/fixtures/tdesktop_sample/AlphaSignals/photos/matrix_57.png

tests/data/fixtures/tdesktop_sample/AlphaSignals/photos/matrix_58.png

tests/data/fixtures/tdesktop_sample/AlphaSignals/photos/matrix_59.png

tests/data/fixtures/tdesktop_sample/AlphaSignals/photos/matrix_60.png

tests/data/fixtures/tdesktop_sample/AlphaSignals/photos/matrix_61.png

tests/data/fixtures/tdesktop_sample/AlphaSignals/photos/matrix_62.png

tests/data/fixtures/tdesktop_sample/AlphaSignals/result.json

tests/data/fixtures/tdesktop_sample/BetaTrades/export_manifest.json

tests/data/fixtures/tdesktop_sample/BetaTrades/observations.jsonl

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/matrix_64.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/matrix_65.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/matrix_66.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/matrix_67.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/matrix_68.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/matrix_69.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_102@05-03-2024_14-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_103@05-03-2024_14-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_104@05-03-2024_14-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_106@09-03-2024_10-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_107@09-03-2024_10-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_108@12-03-2024_08-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_110@20-03-2024_11-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_113@01-04-2024_18-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_116@07-04-2024_10-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_119@13-04-2024_13-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_122@19-04-2024_18-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_126@01-06-2024_10-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_130@13-06-2024_10-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_134@25-06-2024_10-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_138@15-08-2024_10-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_139@15-08-2024_10-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_153@10-01-2025_10-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_63@01-04-2026_00-00-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_64@01-04-2026_00-02-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/photos/photo_65@01-04-2026_00-04-00.png

tests/data/fixtures/tdesktop_sample/BetaTrades/result.json

tests/data/fixtures/tdesktop_sample/DeltaLive/live.jsonl

tests/data/fixtures/tdesktop_sample/GammaRelay/export_manifest.json

tests/data/fixtures/tdesktop_sample/GammaRelay/observations.jsonl

tests/data/fixtures/tdesktop_sample/GammaRelay/photos/matrix_62.png

tests/data/fixtures/tdesktop_sample/GammaRelay/photos/matrix_63.png

tests/data/fixtures/tdesktop_sample/GammaRelay/photos/matrix_64.png

tests/data/fixtures/tdesktop_sample/GammaRelay/photos/matrix_65.png

tests/data/fixtures/tdesktop_sample/GammaRelay/photos/matrix_66.png

tests/data/fixtures/tdesktop_sample/GammaRelay/photos/matrix_67.png

tests/data/fixtures/tdesktop_sample/GammaRelay/photos/matrix_68.png

tests/data/fixtures/tdesktop_sample/GammaRelay/photos/matrix_69.png

tests/data/fixtures/tdesktop_sample/GammaRelay/photos/matrix_70.png

tests/data/fixtures/tdesktop_sample/GammaRelay/photos/matrix_71.png

tests/data/fixtures/tdesktop_sample/GammaRelay/photos/matrix_72.png

tests/data/fixtures/tdesktop_sample/GammaRelay/photos/photo_63@01-04-2026_00-00-00.png

tests/data/fixtures/tdesktop_sample/GammaRelay/photos/photo_64@01-04-2026_00-02-00.png

tests/data/fixtures/tdesktop_sample/GammaRelay/photos/photo_65@01-04-2026_00-04-00.png

tests/data/fixtures/tdesktop_sample/GammaRelay/result.json

tests/data/fixtures/tdesktop_sample/README.md

tests/data/fixtures/tdesktop_sample/SPEC.json

tests/data/fixtures/llm_recorded/extract_v1.json

tests/data/fixtures/llm_recorded/ocr_v1.json

```
