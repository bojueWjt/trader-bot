# OR-05 P1 合成端到端对抗审查

审查日期：2026-09-24（Asia/Manila）；取证跨越 2026-09-23 UTC 晚间。
证据完整性：完成
范围：当前工作树的 P1 合成接缝、契约、看板验收命令。必修共 7 条。
本报告没有修代码、修改看板、提交、联网取行情、读取生产配置、连接生产服务或执行交易操作。
仓库唯一写入文件为本报告；获准测试先复制 data 至 mktemp 目录，账本使用测试临时路径。
Grok MCP 被 approval=never 拒绝，companion CLI 写自身 state.json 遭 EPERM；按 AGENTS.md 双路径失败降级规则，由 Codex 完成执行和报告。

## 1. 证据范围与基线

已核对 contracts/*.md、三份 ADR-G*.md、三份 review-G*-P1.md 的判定与相关证据、全部 ruling-G0-*.md、根 AGENTS.md、GOAL-0-orchestrator.md 和 2026-09-11 合并计划。
G3 长报告按任务要求读取末尾终裁及第 9–15 轮标题和末轮判定表；历史十五审为 fail，不把当前裁决路径误写成审查方 pass。
G2 当前十四审为 pass；G1 分类裁决与原审查结论分开理解。
看板 P1 的 done=false，openItems 有 P1-OPEN-1 至 P1-OPEN-7。本报告不重复其中授权、依赖身份、pycache、提交归属及从明确提交重跑的问题。
paths.py 的实际文件字节摘要、传递依赖，以及 nullmodel.py 新 pyc 前缀和逐 job 回执绑定是当前在途修复；本次没有运行研究层测试，不为这些修复签发完成证明。
ruling-G0-R10-scope-and-acceptance.md 的后续授权说明与看板状态存在更新时间差；属于已有 openItems 的协调范围，不另列缺陷。
I02–I05 是普通数据或参数输入即可触发的问题，不依赖向 MC worker 注入代码，不能归入 R-10 已声明的任意进程内代码篡改边界。
I06 的内存替换仅用于检验测试断言强度，不是对 MC 执行身份的指控。

只读取证时 HEAD 为 `f0e80552fe67ce2e7b6808962bd904bf3322150e`；这不是本次被审工作树的不可变快照承诺。
src 下 Python 文件跟踪率 48/48；tests 为 66/68。
未跟踪的两份文件：`tests/research/test_dependency_content_identity.py`、`tests/research/test_execution_identity_binding.py`。保留原样，不据此另报 P1-OPEN-6。
研究层正式 MC 在另一进程运行；未运行 tests/research，也未终止或干预该进程。

关键取证文件 SHA256（2026-09-23 19:09 UTC 读取）：

| 文件 | SHA256 |
|---|---|
| src/quant_lab/market/asof.py | 91e35abd6dd83199af7b47fa253b2d2121fea15bc428b50e7724e3321a2ec090 |
| src/quant_lab/market/vision.py | 30f0dcdf25bd9e9869fe13d516fdd7a0d7646528e440cf5f85ff993147614cd8 |
| src/quant_lab/research/features.py | 65f36acacec9b7656dc572ee2490ef7dafc3f2782a43706560dbaae88cd07b46 |
| src/quant_lab/research/evaluator.py | d2c73127ad42b91d83be4362b3ce56c730044bd80813688ea7d4b8dc377aa9fd |
| tests/integration/test_e2e_synthetic.py | 7b0a51b0d66d8dad463e138366dd7c44c083a229217de9c873f56aca5aec3ba3 |

### 1.1 获准套件实跑

以下每个命令分别使用新临时目录；没有复用真实湖作输出目录：

```sh
cd /Users/balen/projects/trader-bot/quant-lab
T=$(mktemp -d)
cp -R data "$T/data"
export QUANT_LAB_DATA_ROOT="$T/data"
export PYTHONDONTWRITEBYTECODE=1
export PYTEST_ADDOPTS='-p no:cacheprovider'
.venv-g0/bin/python -m pytest tests/integration -q
# 下一套重新执行上面的 mktemp / cp / export，再运行：
# .venv-g1/bin/python -m pytest tests/data -q
# .venv-g2/bin/python -m pytest tests/market -q
```

实际输出与退出码：

```text
integration: 8 passed in 2.89s                         rc=0
data:        225 passed in 86.25s (0:01:26)             rc=0
market:      358 passed in 56.29s                      rc=0
```

按 A46 同报门的余量：当前执行 80 行，成交 78，对下限 40；走完结局 65，对下限 20。
这证明现有非退化门通过，不证明回归保护完整，也不替代下面的反例。
integration 的 quarantine 检查仍读硬编码的仓库 data 路径（I01）；这是只读越过副本边界，未据此声称真实湖被写入。
没有执行看板中单独的 CLI 构建、下载、研究测试或正式报告 verify；下一节逐条核对属于命令静态审查，另有 I07 的只读门探针。

## 2. 必修

### I01：G2 默认路径忽略全局隔离数据根

- 位置：`src/quant_lab/market/vision.py:109–114`，`tests/integration/test_e2e_synthetic.py:218,234`。
- 问题：G1/G3 响应 QUANT_LAB_DATA_ROOT，G2 LakePaths.default 只读 QUANT_LAB_MARKET_LAKE，默认仍指向工作树 data/lake/market。按全局隔离契约启动的默认 fetch/loader 会指向真实湖，隔离运行和并行窗口无法依赖同一个数据根。
- 契约：research-schema §9.1 数据根修订、feature-snapshot §7.5 和 contracts/README 分区约定。integration 的 quarantine glob 同样绕过隔离根。
- 复现命令（quant-lab/；只解析路径，不创建 sentinel，不下载）：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g0/bin/python -B - <<'PY'
import os
from unittest.mock import patch
from quant_lab.data.lake import Layout
from quant_lab.market.vision import LakePaths
from quant_lab.research.paths import data_root
with patch.dict(os.environ, {'QUANT_LAB_DATA_ROOT':'/tmp/or05-read-only-sentinel'}):
    os.environ.pop('QUANT_LAB_MARKET_LAKE',None)
    print('G1=',Layout.from_root(None).bronze_dir)
    print('G2=',LakePaths.default().root.resolve())
    print('G3=',data_root())
    print('G2_inside_root=',LakePaths.default().root.resolve().is_relative_to(data_root()))
PY
```

实际输出，rc=0：

```text
G1= /tmp/or05-read-only-sentinel/lake/telegram/bronze
G2= /Users/balen/projects/trader-bot/quant-lab/data/lake/market
G3= /private/tmp/or05-read-only-sentinel
G2_inside_root= False
```

- 改法：默认 G2 路径从统一 data_root 派生；明确专用 override 的优先级，隔离运行不得无提示回落真实湖。quarantine 检查改走 Layout/API。
- 验收：只设置 QUANT_LAB_DATA_ROOT 时，G1/G2/G3 的读写产物均在副本；使用相互冲突的根内外 sentinel 内容证明实际消费者读了副本，而不只断言字符串路径。

### I02：G2 标记价入口消费尚未 available 的 bar

- 位置：`src/quant_lab/market/asof.py:140–153,169–192`。
- 问题：last_closed_bar 仅检查 close_time+latency，mark_bar_at/mark_price_at 沿用结果；即便输入明确带未来 available_at，仍返回该 bar 的价格。与 asof_join 和 G3 的可见性判断分叉。
- 契约：合并计划 C.1 要求事件结束且已经可知；ADR-G2 的时钟语义。H0 收盘等号的特许不等于忽略已提供的实际晚到证据。
- 复现命令：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g0/bin/python -B - <<'PY'
from datetime import UTC,datetime,timedelta
import polars as pl
from quant_lab.market.asof import asof_join,last_closed_bar,mark_price_at
inst='BTCUSDT-PERP.BINANCE-UM'; t=datetime(2024,1,1,0,2,tzinfo=UTC)
a=pl.DataFrame({'episode_id':['e'],'instrument_id':[inst],'t_dec':[t]})
b=pl.DataFrame({'instrument_id':[inst]*2,'interval':['1m']*2,
 'close_time':[t-timedelta(minutes=1),t],
 'available_at':[t-timedelta(minutes=1),t+timedelta(minutes=10)],'close':[100.,999.]})
print('I02 strict_asof=',asof_join(a,b,by=['instrument_id'])['close'][0],
 'last_closed=',last_closed_bar(b,at=t,instrument_id=inst,interval='1m')['close'][0],
 'mark_price=',mark_price_at(b,t,inst))
PY
```

实际输出，rc=0：

```text
I02 strict_asof= 100.0 last_closed= 999.0 mark_price= (Decimal('999.0'), None)
```

- 改法：公开 bar/mark 入口共同校验闭合时钟和可用时钟；实际 available_at 优先，缺失仅按明确声明的 H0 假设处理。保留契约批准的等号语义，不能机械改成全部严格小于。
- 验收：同一输入分别经 asof_join、last_closed_bar、mark_bar_at、mark_price_at，对晚到与未知时间均不暴露 999；补 H0 等号、顺序已知/未知及 120s 陈旧边界对照。
- 范围限定：G1 FrameMarks 已有自身 available_at 过滤；本条指仍可独立调用的 G2 公共 API，不把已修的 G1 包装器重复报错。

### I03：特征回看依赖的未知可用时刻被聚合忽略

- 位置：`src/quant_lab/research/features.py:172–182,195–203`。
- 问题：rolling_max 使用 min_samples=1，忽略窗口内的 null；检查聚合最大值非空不能证明每个必要依赖可知。Ref(close,1) 可直接取出 available_at=null 的 999 并标 validity=True。
- 契约：合并计划 C.1 的依赖闭包、feature-snapshot 的缺失语义、ADR-G3 的可见性规则。
- 复现命令：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g0/bin/python -B - <<'PY'
from datetime import UTC,datetime,timedelta
import polars as pl
from quant_lab.research.features import feature_snapshot
from quant_lab.research.ast import canonical_hash
inst='BTCUSDT-PERP.BINANCE-UM'; t=datetime(2024,1,1,0,2,tzinfo=UTC)
a=pl.DataFrame({'episode_id':['e'],'instrument_id':[inst],'t_dec':[t]})
b=pl.DataFrame({'instrument_id':[inst]*3,'interval':['1m']*3,
 'close_time':[t-timedelta(minutes=2),t-timedelta(minutes=1),t],
 'available_at':[t-timedelta(minutes=2),None,t],'close':[100.,999.,100.]})
ast={'op':'Ref','args':[{'field':'close'}],'params':{'lag':1}}
h=canonical_hash(ast); r=feature_snapshot([ast],a,bars=b)
print('I03 unknown_dependency_value=',r['f_'+h][0],'valid=',r['validity_'+h][0])
PY
```

实际输出，rc=0：

```text
I03 unknown_dependency_value= 999.0 valid= True
```

- 改法：按算子的必要依赖传播“时间未知”掩码，与最大 available_at 分开检查；EMA 的累计依赖同样需要未知传播，不能仅把 rolling_max 换成另一个忽略 null 的聚合。
- 验收：Ref、有限窗口、EMA 分别注入一个必要依赖时刻未知，结果应 invalid 且值为空；相同依赖改为按时到达恢复有效，改为晚到仍拒绝，非必要依赖不应无故使结果失效。

### I04：负 latency 将特征查询推进到决策之后

- 位置：`src/quant_lab/research/features.py:117–136,151–168,193–194`；G2 `last_closed_bar` 参数同样没有负值守卫。
- 问题：ctx 和便捷参数只做冲突检查，没有 latency 非负域校验；t_query=t_dec-latency 可变为未来时刻。没有 available_at 列的合成 H0 行情因此直接前视。
- 复现命令（此条的实证范围为 G3 公开入口）：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g0/bin/python -B - <<'PY'
from datetime import UTC,datetime,timedelta
import polars as pl
from quant_lab.research.features import feature_snapshot
from quant_lab.research.ast import canonical_hash
inst='BTCUSDT-PERP.BINANCE-UM'; t=datetime(2024,1,1,0,2,tzinfo=UTC)
a=pl.DataFrame({'episode_id':['e'],'instrument_id':[inst],'t_dec':[t]})
b=pl.DataFrame({'instrument_id':[inst]*2,'interval':['1m']*2,
 'close_time':[t,t+timedelta(minutes=1)],'close':[100.,999.]})
ast={'field':'close'}; h=canonical_hash(ast)
for secs in [0,-60]:
    r=feature_snapshot([ast],a,bars=b,latency=timedelta(seconds=secs))
    print('I04 latency_s=',secs,'value=',r['f_'+h][0],'valid=',r['validity_'+h][0])
PY
```

实际输出，rc=0：

```text
I04 latency_s= 0 value= 100.0 valid= True
I04 latency_s= -60 value= 999.0 valid= True
```

- 改法：在统一上下文解析时拒绝负 latency，并在公开 G2 入口同样守域；max_staleness 应按各接口明确允许域校验，不通过负 tolerance 或未来查询隐式解释坏参数。
- 验收：便捷参数和 SnapshotContext 两条路径对 -1µs/-60s 均明确拒绝，0 和正延迟遵守既有等号约定；不能只修 ExecutionPolicy 而留下直接 API。

### I05：冻结机会集与执行结果没有绑定同一图和决策快照

- 位置：`src/quant_lab/research/evaluator.py:59–81,124–137,168–172` 及 pair_arms/evaluate。
- 问题：OpportunitySet 仅保存 ID、资格与权重，丢失 graph_version、decision_snapshot_hash 等来源身份。pair_arms 核对两臂彼此一致，却无法核对它们是否来自冻结机会集；两臂同时取自错误版本仍被接受。
- 影响：同 episode_id 的旧版结果或重放结果可混入新决策机会集。此处已证身份错配，不外推为已在磁盘实施 tombstone 后读到真实旧缓存。
- 复现命令（只读现有合成湖，在内存中换两列；不传 ledger）：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g0/bin/python -B - <<'PY'
import runpy
import polars as pl
from quant_lab.research.ast import canonical_hash
from quant_lab.research.evaluator import evaluate
m=runpy.run_path('tests/integration/test_e2e_synthetic.py')
ep=m['episodes'].__wrapped__(); ex=m['execution'].__wrapped__(ep)
opp=m['opportunity'].__wrapped__(ep); ids=opp.episode_ids
ast={'field':'close'}; h=canonical_hash(ast)
f=ep.filter(pl.col('episode_id').is_in(ids)).select('episode_id','t_dec').with_columns(
    pl.lit(1.).alias('f_'+h),pl.lit(True).alias('validity_'+h))
x=ex.filter(pl.col('episode_id').is_in(ids))
wrong=x.with_columns(pl.lit('wrong-graph').alias('graph_version'),
                     pl.lit('wrong-snapshot').alias('decision_snapshot_hash'))
for label,xx in [('baseline',x),('foreign_graph',wrong)]:
    r=evaluate(ast,opp,features=f,rule=lambda frame:pl.Series([True]*frame.height),
               execution=xx,fold_id='probe',attempt_id='read-only')
    print('I05',label,'status=',r.status,'theta=',r.theta,'n_evaluated=',r.n_evaluated)
PY
```

实际输出，rc=0：

```text
I05 baseline status= ok theta= 0.0 n_evaluated= 67
I05 foreign_graph status= ok theta= 0.0 n_evaluated= 67
```

- 改法：机会集冻结逐 episode 的 graph_version、decision_snapshot_hash、t_dec 等规范身份并纳入 digest；两臂与该身份逐行比较，而非只比较两臂。alias 先解析为不可变版本，特征来源同时绑定。
- 验收：只改图、只改快照、两臂同时错配、旧 alias 重指向均拒收；同一合法输入重放仍通过。账本已预留时，拒绝必须留下正确失败终态。

### I06：名为“尊重 t_dec”的集成断言接受全无效伪造结果

- 位置：`tests/integration/test_e2e_synthetic.py:145–160`；配对测试 `135–142`，quarantine 检查 `213–238`。
- 问题：时钟测试只检查行数与 validity 列名，没有一个数值、有效性或未来扰动断言；全 invalid 且填任意大值也通过。配对测试先求交集，再以交集为期望，失去独立核对完整机会集的能力。
- 复现命令（仅进程内 patch，退出自动恢复；没有修改源码或另跑 pytest 选择器）：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g0/bin/python -B - <<'PY'
import runpy
from unittest.mock import patch
import polars as pl
from quant_lab.research.ast import canonical_hash
m=runpy.run_path('tests/integration/test_e2e_synthetic.py')
ep=m['episodes'].__wrapped__(); ex=m['execution'].__wrapped__(ep)
opp=m['opportunity'].__wrapped__(ep)
def forged_snapshot(asts,anchors,**kwargs):
    h=canonical_hash(asts[0])
    return anchors.select('episode_id','t_dec').with_columns(
        pl.lit(999999.).alias('f_'+h),pl.lit(False).alias('validity_'+h))
with patch('quant_lab.research.features.feature_snapshot',forged_snapshot):
    m['test_seam_g3_feature_snapshot_respects_t_dec'](ep)
    print('I06 feature_clock_assertion_accepts_all_invalid_fabrication=True')
sub=ex.head(1)
m['test_seam_g2_build_request_is_sole_entrypoint'](sub)
m['test_e2e_sample_is_not_degenerate'](sub)
m['test_seam_g3_pair_arms_consumes_g2_output'](sub,opp)
print('I06 execution_rows=',ex.height,'reduced_rows=',sub.height,'three_seam_checks=PASS')
PY
```

实际输出，rc=0：

```text
I06 feature_clock_assertion_accepts_all_invalid_fabrication=True
I06 execution_rows= 80 reduced_rows= 1 three_seam_checks=PASS
```

- 证据限定：证明上述四个测试函数的断言缺口，不声称缩到一行后整套 8 项仍通过。主 evaluate 使用完整 opp，能独立发现缺臂；不能把这一保护抹去。A46 已明示非退化门不是回归门，本条也不以已接受的阈值余量另报缺陷。
- 其他静态缺口：episodes 直接读已发布 fixture-v1，不从当前代码重建；行情 resolver 按计划 TP 生成路径，不经 G2 行情湖/as-of；loss 用 latest，未绑定本批；quarantine 的 height>=0 恒真，只有列名的空表可过。
- 改法：至少用一个已知有效和一个已知不可见 anchor 逐值验证时间边界，再扰动未来数据要求过去不变；完整机会集先核集合相等；固定包含隔离样本的夹具逐 ID 核 Q/MAP/LOSS，而非要求任何运行的隔离区都非空。
- 验收：I02–I04 反例必须由相应集成时钟测试识别；伪造返回、漏单、已知隔离对象消失各有单独失败证据；从空临时湖重建的产物身份可追到本次输入和代码。后续真实文件突变验收须另在授权隔离副本执行，本次不冒称已完成该种取证。

### I07：OR-05 没有终裁也放行，OR-01 丢弃上游失败码

- 位置：`taskList.json` 的 modules.orchestrator.tasks 中 OR-01、OR-05 的 verify。
- 问题：OR-05 只用文件存在和一条负向措辞检查，完全不读取终裁。OR-01 用分号分隔冻结检查与文件存在性检查，上游失败不影响最终成功。
- 复现命令（OR-05 在本文件仅写入标题、尚无终裁时实跑；命令从看板读取，未手抄或修改）：

```sh
python3 -B - <<'PY'
import json,pathlib,subprocess
x=json.load(open('taskList.json'))
ts={t['id']:t for t in x['modules']['orchestrator']['tasks']}
p=pathlib.Path('docs/adr/review-G0-integration.md')
r=subprocess.run(['/bin/sh','-c',ts['OR-05']['verify']],capture_output=True,text=True)
print('OR05 terminal_count=',sum(s.startswith('终裁：') for s in p.read_text().splitlines()),'rc=',r.returncode)
r=subprocess.run(['/bin/sh','-c','python() { return 1; }; '+ts['OR-01']['verify']],capture_output=True,text=True)
print('OR01 forced_show_failure=1 rc=',r.returncode)
PY
```

实际输出，rc=0：

```text
OR05 terminal_count= 0 rc= 0
OR01 forced_show_failure=1 rc= 0
```

OR-01 的 shell 函数只模拟展示程序失败，不改 Python 可执行文件或仓库。交付后重跑上面命令，OR-05 应看到本报告一条 fail 终裁，却仍 rc=0；前面的 0 条终裁记录是落盘过程中的实际反例。
完整报告落盘后已再次实跑原 verify，实际输出摘录：`OR05_FINAL report_verdict=fail rc=0`；五个发现关联源码/测试文件的 SHA256 与上表相同，变化清单为 `[]`。
- 改法：OR-05 正向解析唯一终裁和证据完整性；OR-01 读取结构化 frozen 真值并保留每一段退出码，不以有该词或有文件代替冻结。
- 验收：从 taskList.json 原文执行门，对无终裁、fail、insufficient、历史 pass 加最终 fail、上游命令失败分别非零；唯一完整 pass 和冻结=true 才通过。不得依靠审查者使用某种措辞来触发失败。

## 3. 应改

### A01：回滚、热缓存与无缓存路径需要真正接到同一撤权源

G1 verify_manifest 会核文件 hash、发布状态和 tombstone；data 套件覆盖 tombstone 后拒读及旧 Parquet 保留。
G3 cache=True 在读前、热读后、发布前、返回前调用 consumable，静态上存在有效的拒读屏障，不能笼统说“缓存完全没有撤权检查”。
但 callback 由调用者提供；cache=False 绕过 ctx.check_identity，OR-04 没有使用真实 G1 撤权源驱动 G3 热缓存的试验。
应在获准隔离副本中执行：生成快照→tombstone 同图→再次热读及无缓存计算→两者拒绝；再做合法新图重放。
本次不写 tombstone、不造磁盘缓存，没有“实际旧缓存被消费”的证据，因此这一端到端撤权项不列必修；已复现的图身份问题单列 I05。

### A02：共享元数据的并发发布需有独立故障试验

G1 telegram、G2 market、G3 features/lockbox 的常规叶目录按属主分区；quarantine 亦有各流路径。不同目录不能自动证明元数据不会竞争。
graph.py 中 alias/index/tombstone 的读改写与固定临时文件名，需证明同一分区并发发布、发布与撤权交错不会丢更新；G2 同月分区也需并发幂等验收。
本次未启动并发写入，没有 lost-update 的动态证据；只列应改。I01 是已复现的隔离根失效，应先修。

### A03：将弱 verify 换成内容和运行身份判据

下表逐条列出全部 39 条 verify。标为套件覆盖仅表示该测试文件随获准全套运行，不表示该条 verify 的 CLI 和 shell 尾部已实跑。
文档位置锚应按 A47 修正；纯关键词、负向 grep、文件存在或行数门不应承担业务完成判定。
对 M-08、R-05 等本次未做制品改写反例，按证据等级保留在应改，不仅凭静态判断增加必修计数。

| verify | 本次核对与缺口 |
|---|---|
| D-01 | 仅静态；`--co -q` 后匹配 `collected [1-9]` 是旧格式依赖，且管道未保留生产者退出码；import 不证明有测试。 |
| D-02 | 只数 ADR 行数并找 invalid_transition/tombstone，不能证明状态机或撤权实现。 |
| D-03 | normalize 测试随 data 套件通过；CLI 未跑。固定 /tmp/ql-norm 有跨运行混用风险，height>0 不绑定本次输入。 |
| D-04 | dedup 测试随 data 套件通过；直接测试门，无额外文件存在性尾门。 |
| D-05 | extract 测试随 data 套件通过；bench CLI 未跑。固定 /tmp/ql-extract.json，n_items>=30 与 recall>0 不等于质量门通过。 |
| D-06 | normalize_validate 测试随 data 套件通过；不外推为全部跨模块时钟路径安全。 |
| D-07 | 现行命令确有独立 mktemp、隔离根和本轮 build；不再重复旧的真实湖覆盖问题。linker/lifecycle 随套件通过；CLI 未跑。>=5 行和三列非空只是最低形状门。 |
| D-08 | todo，真实数据 PoC，现已移出 P1；未执行，不以其未实现重复阻断本次。 |
| D-09 | audit 测试随套件通过；OC CLI 未跑。两个概率用正则 OR，任一行即可满足，管道可能掩盖失败；应两方案各自数值核验。 |
| D-10 | 第一支仍用 tail -40 位置锚和缺行尾约束的 pass 匹配；第二支正向分类行加负向撤回词。A47 只修了第二支位置锚，不能据此说整条无位置依赖。 |
| D-11 | todo/P2；latest 加 layer=5 正数 grep，未绑定批次，也不覆盖现有第 6 层。 |
| M-01 | 同 D-01 的旧 collection 文案与管道风险；import 包只验证可导入。 |
| M-02 | ADR >=200 行和两个名字，不是内核行为证据。 |
| M-03 | vision 测试随 market 套件通过；fetch 未执行。默认根有 I01；尾部硬编码 data，取排序首个 manifest，行数>40000 不能绑定下载结果与检查状态。 |
| M-04 | partition_check 随套件通过；有内容断言，不只是文件存在。 |
| M-05 | asof 测试随套件通过，但未挡住 I02 的 mark 公共入口晚到输入。 |
| M-06 | contract 测试随套件通过；另数 fixture 文件>=10，只证明数量，不证明列举的所有枚举可达。 |
| M-07 | kernel_a 测试随套件通过；replay CLI 未跑。passed=10..99 的位置文案范围会误拒 >=100，管道未保留生产者码；应结构化核计数和身份。 |
| M-08 | 仅 test -f 加 episode.*diff；一行表头可满足，未检查 episode 集、差分值、容差或通过结论。 |
| M-09 | execution_api 随套件通过；其成功不替代 I05 的冻结机会集来源比较。 |
| M-10 | 正向完整性行、负向关键词、grep 后 tail -1 终裁；比 OR-05 强，但仍取最后匹配文本，可能读到代码块，完整性行不要求唯一。 |
| M-11 | todo/P2；coverage 只 grep instruments 正数，不能证明所有种子品种、时间范围和合格覆盖。 |
| R-01 | 未跑；collection 正则方向已修正，仍依赖管道文本，import 不证明收集退出成功。 |
| R-02 | 未跑；ADR 行数+canonical_hash/max-t 关键词只验文档形状。 |
| R-03 | AST 测试入口；本次研究测试不运行，不借历史绿灯下结论。 |
| R-04 | 算子测试入口加 REGISTRY 长度>=18；冻结表实际列 19 个名字，应核完整集合，数量不能识别同数替换。未跑。 |
| R-05 | 用 rindex 取最后 JSON 代码块，属位置依赖；all(polars.values()) 对空字典真，没有先核必需算子集合。其余 TA 数量与性能数值门存在，不误写成完全只查文件。 |
| R-06 | features/evaluator 测试入口；本次未跑。I03–I05 由独立只读探针复现。 |
| R-07 | protocol 测试后查持久账本 height>0 且无 reserved；旧账本或全失败终态也可满足尾门，应绑定本轮 completed 与配置。未跑。 |
| R-08 | 测试加 verify_report_text，有算术、身份与反例门；MC 正在重生成，按用户指令不执行，不把预期陈旧当新缺陷。 |
| R-09 | CLI 后核有限 theta、completed、无 reserved、非全 duplicate、描述性输出，明显强于存在性检查；固定 /tmp/ql-proto 仍应绑定本次运行。未跑。 |
| R-10 | 当前为能力文档精确声明行、负向关键词、research 全套；已不是历史报告 tail -1。锚定行首行尾是内容锚，不应误报成 A47 的位置锚。授权与在途身份修复沿 openItems。 |
| R-11 | 文档关键词、清单中任一值长度64、research 全套；“有一个摘要”不足以证明覆盖集与实际字节。实现修复已在途，沿 P1-OPEN-3/4，不重复新增。 |
| OR-01 | 文件存在性尾门覆盖前段失败；I07 实测。frozen 词出现也不等于 frozen=true。 |
| OR-02 | 只有 task.py show 展示动作，没有任何完成条件断言；doing 状态不因此变成错误的已完成声明。 |
| OR-03 | 只 grep blockers 字段，非“闸门已批准/阻塞已解除”；未执行外部动作。 |
| OR-04 | integration 套件实跑通过；只读已有 gold 与自制行情，时钟断言缺口见 I06。不能替代 fresh-build 全链验收。 |
| OR-05 | 文件存在+负向措辞；I07 证明无终裁也返回成功。 |
| OR-06 | 仅 test -f 加 readyForStitch 词，false 或正文提及也满足；todo，未据此宣称任务已被错误完成。 |

verifyHistory 全量读取共 11 条：D-10 三条、M-10 三条、R-08 两条、R-10 三条。本次新增 0 条，未修改任何 verify。
M-10 的三次收紧已知方向成立；D-10 的分类析取和 R-10 的能力验收属已有裁决范围，不把“中性扩展”标签当充分证明，也不重审 openItems 授权轴。
这些历史记录不修复表内当前命令的实际缺口；尤其 OR-05 仍保留 M-10 历史上已淘汰的负向判读形状。

## 4. 契约、偏差与安全边界核对

| 关注面 | 本次得到的证据与限度 |
|---|---|
| 三时钟与等号 | G1 decision_visible 严格可见性、G2 asof_join 严格小于/有顺序等号、H0 bar 闭合等号需要分开解释；I02–I04 证明实际晚到/未知和参数域仍有漏洞。没有把所有 <= 一律判错。 |
| EP→Execution 列与类型 | 实读 EP=80 行，eligibility 六键均 Boolean 且各 null_count=0；order_plan.entries/stop/tps/sizing 的经济量为 Decimal(38,12)，与修订后的契约一致。 |
| 执行输出枚举 | 本合成批 net_R 为 Decimal(38,12)，fill_status={filled,none,partial}；outcome_kind={rejected,right_censored,tp_hit}。这些是合法子集，不把一个批次未出现全部七终态误报为枚举漂移；全枚举靠模块夹具核验。 |
| 特征 schema | 列形状为 episode_id/t_dec/f_hash/validity_hash；形状符合不代表时钟有效，I03/I04 给出具体反例。机会集来源身份语义漂移见 I05。 |
| 删帖样本路径 | data 测试含 delete_notice 解析/状态允许、episode deleted_after_observation_any 标记，未把该标记回写成原始 MV 的既有事实；对应测试本轮通过。决策视图遮蔽事后删除标记有防前视意义，不能要求把未来删除信息带回 t_dec。 |
| 隔离保留 | lifecycle 测试核隔离继承、MAP/LOSS 守恒及 tombstone 后旧文件保留；这强于 OR-04 的可读性断言。当前 OR-04 没有把已知删除/隔离 cohort 从 raw 逐 ID 追到最终分母，端到端覆盖仍需补。 |
| 不填零 | evaluator 对 censor_reason 非空但 net_R 非空、无删失却 net_R 缺失/非有限主动拒绝；合法未成交与候选 skip 的 0 不能和证据缺失混为一谈。I03 是把未知可用性当有效，不是收益 fill_null(0)。 |
| 回滚重放 | G1 发布/撤权屏障有实现和本轮 data 测试证据；G3 热缓存有 callback 复查；真实 G1 撤权→G3 热缓存的跨层连通试验未跑，见 A01。 |
| 夹具泄漏 | resolver 由计划 TP 决定合成价格，用于 smoke 可解释；all-true rule 加同政策只能检验接缝，不能证明因子有效性、行情独立性或实际收益。没有发现足以新增必修的运行期读取金标路径。 |
| 凭据边界 | 对 src/quant_lab 的 import、环境入口与路径调用点静态扫描，未见 services import 或生产配置读取路径；LLM 默认禁网/录制路径。没有读取 .env、密钥、生产数据库或 SSH。阴性扫描只支持被扫描范围，不声称任意外部插件零风险。 |
| 运维铁律 | 未 RESUME、未开交易、未操作订单、未撤销用户手工单、未停节点、未部署；本任务不需要生产服务状态核验，也未把其他会话的转述当成本轮测试输出。 |

## 5. 可选

1. 给集成报告附机器可读摘要，包含实际图版本、输入 manifest、环境、命令退出码、样本数及门余量；审查正文负责解释边界。
2. 将跨模块时间边界做成共同的少量参数化 golden：等号、+1µs、未知、晚到和不同周期闭合，减少各层各写一套却含义不同。
3. 按轮次归档超长评审证据，当前文件保留逐项状态、具名证据链接及唯一终裁，降低位置锚和旧结论误读风险。

## 6. 交付验收

必修计数为 7：I01 数据根、I02 标记价晚到、I03 未知依赖时钟、I04 负延迟、I05 图身份、I06 集成断言、I07 看板门。
三套获准测试通过与这七条反例并存；终裁由可复现缺陷决定，不由通过用例总数决定。
应改项未被包装为已复现缺陷；研究层全量回归和正式 MC 报告验收仍由后续既定流程执行。
格式验收命令：`wc -l docs/adr/review-G0-integration.md`；`grep -c '^终裁：' docs/adr/review-G0-integration.md`。

终裁：fail

## 7. 二审（2026-09-24，提交 b2a30eb）：必修闭合核验

二审证据完整性：完成。结论：I01、I02、I03、I04、I06 闭合；I05、I07 未闭合。新增独立编号 0 条；两项修复不完整沿用原编号，不重复计数。终裁为 fail。

基线为 `b2a30eb3dbde8ad6e4303bb61770803b7204ab03`；已审 `git show bf1fe55 b2a30eb` 的源码、测试及门控变化。开始时 quant-lab 无工作树改动，根仓库其他目录已有用户改动；这些改动不属于本审查。MCP 委派被 approval=never 拒绝，companion CLI 因自身 state.json 写入 EPERM 无法启动，按项目规则降级由 Codex 执行。

唯一仓库写入是本节起的末尾追加；§1–§6 和一审终裁按字节保留。不运行 R-09 verify，不运行正式 MC 生成，不写真实 data、taskList 或源码。测试中的新湖、账本、缓存、门控夹具及进程内突变全部隔离；PYTHONDONTWRITEBYTECODE=1，pytest 缓存关闭。

本轮原始取证目录：`/var/folders/js/d8w4bf351gvdk84fn820t3nw0000gn/T/or05-second-nk782h7t`。`I01.sh`–`I07.sh` 是从一审原文直接抽取的命令，配套 `.log`/`.rc` 保存真实输出与退出码；`*-baseline.log`/`*-mutant.log`、`or05_mutator.py` 保存独立突变证据；`original-review.md` 保存追加前完整字节。该目录是本机临时证据，关键结论及输出在本文内留存，不把临时目录当长期制品。

全套命令均在 quant-lab/ 执行，每套先单独运行：

```sh
T=$(mktemp -d)
cp -R data "$T/data"
export QUANT_LAB_DATA_ROOT="$T/data"
export PYTHONDONTWRITEBYTECODE=1
export PYTEST_ADDOPTS='-p no:cacheprovider'
```

| 命令 | 本次最终完整实跑输出 | 退出码 |
|---|---|---|

| `.venv-g0/bin/python -m pytest tests/integration -q` | `42 passed in 2.99s` | 0 |

| `.venv-g1/bin/python -m pytest tests/data -q` | `225 passed in 36.29s` | 0 |

| `.venv-g2/bin/python -m pytest tests/market -q` | `379 passed in 22.85s` | 0 |

| `.venv-g3/bin/python -m pytest tests/research -q -p no:cacheprovider` | `565 passed in 262.77s (0:04:22)` | 0 |

共 1211 项通过，无 skip。通过总数不替代下面的反例。最初同样隔离的一轮也全部通过（42 / 225 / 379 / 565）；最终表使用逐字 shell 初始化命令的复核回执。

独立突变运行方式：`PYTHONPATH=<取证目录>:<quant-lab>/src OR05_MUTANT=Ixx .venv-gN/bin/python -m pytest <下列节点> -q -p or05_mutator`，其余隔离环境如上；基线清空 OR05_MUTANT。插件只在该测试进程内替换一个守卫/函数。I03–I05 第一版临时插件误导入 g3 venv 未安装的 httpx，rc=3；修正为按需导入后，均得到下列业务断言失败 rc=1。初版工具错误日志也保留，但不算突变证据。

### 7.1 I01：闭合

原样执行 §2 I01 的完整 sh 代码块（自动抽取，未改一字）；进程退出码 `0`。实际输出：

```text
G1= /tmp/or05-read-only-sentinel/lake/telegram/bronze
G2= /private/tmp/or05-read-only-sentinel/lake/market
G3= /private/tmp/or05-read-only-sentinel
G2_inside_root= True
```

`LakePaths.default()` 明确专用 override 优先，其次全局数据根，最后项目绝对路径；`load_market_from_lake` 的缺省入口也走该解析。集成 quarantine 改用本轮 Layout/API。并非只比较路径字符串：`tests/market/test_review_or05_i01_i02_i04.py::test_i01_data_root_only_loader_uses_copy_not_cwd` 从根内 100 / 根外 999 两份冲突行情消费价格；`::test_i01_data_root_only_fetch_writes_copy_fingerprint_unchanged` 用录制 client 写副本并核外部指纹不变，均随 market 套件通过。override、无环境变量、G1/G3 路径一致性另有同文件回归。

该文件两个主测试自带切回 cwd 默认根、切回旧 loader 的内存突变，复用原断言并要求 AssertionError。本轮独立突变再次证实：

独立突变选中 `tests/market/test_review_or05_i01_i02_i04.py::test_i01_data_root_only_loader_uses_copy_not_cwd`；将默认湖强制改为 cwd 下 data/lake/market，实际读到外部 999，价格断言失败。基线与突变的实际 pytest 摘要如下（退出码依次为 0、1；完整失败栈见 `I01-mutant.log`）：

```text
baseline: 1 passed in 0.32s
mutant: 1 failed in 0.10s
```

### 7.2 I02：闭合

原样执行 §2 I02 的完整 sh 代码块（自动抽取，未改一字）；进程退出码 `0`。实际输出：

```text
I02 strict_asof= 100.0 last_closed= 100.0 mark_price= (Decimal('100.0'), None)
```

`last_closed_bar` 同时校验 close_time 与显式 available_at；未知行拒绝、晚到行拒绝，mark 两入口共享此实现。无 available_at 列才采用 H0 latency，保留收盘等号；有实际到达时不再叠加假设延迟。

同文件 `test_i02_late_available_at_not_exposed_on_four_entries`、`test_i02_null_available_at_not_exposed_on_four_entries` 覆盖 asof_join / last_closed_bar / mark_bar_at / mark_price_at；`test_i02_h0_equality_without_available_at_column`、`test_i02_explicit_equality_without_sequence_rejected`、`test_i02_explicit_equality_with_asof_sequence_only`、`test_i02_sequence_null_equal_reverse_reject_equality` 及 `test_i02_staleness_exact_120_and_plus_1us` 均通过。测试内部删除到达守卫、翻转顺序及陈旧边界的突变均必须抛断言；集成 `test_seam_g2_late_bar_clock` 和 `test_mutation_late_bar_ignores_arrival` 也通过。

独立突变选中 `tests/market/test_review_or05_i01_i02_i04.py::test_i02_late_available_at_not_exposed_on_four_entries`；在 last_closed_bar 入口删除 available_at 列，恢复仅依赖收盘时钟的旧行为。基线与突变的实际 pytest 摘要如下（退出码依次为 0、1；完整失败栈见 `I02-mutant.log`）：

```text
baseline: 1 passed in 0.27s
mutant: 1 failed in 0.05s
```

### 7.3 I03：闭合

原样执行 §2 I03 的完整 sh 代码块（自动抽取，未改一字）；进程退出码 `0`。实际输出：

```text
I03 unknown_dependency_value= None valid= False
```

`_dependency_availability` 沿 AST 传播必要槽位的 unknown 与最大到达时刻；Ref 只移位其必要槽，窗口聚合与 EMA 累计分开处理，EMA 数值重置后才清除旧依赖。旧缓存通过 AVAILABILITY_VERSION=2 隔离。

`tests/research/test_feature_availability_or05.py::test_required_arrival_unknown_ontime_late_and_unneeded`、`::test_nested_and_branch_dependencies`、`::test_ema_reset_discards_only_pre_reset_dependencies`、`::test_ref_missing_intermediate_slot_is_not_a_dependency`、`::test_cache_availability_semantics_version_prevents_legacy_hit` 均随 research 通过。`::test_mutation_removing_dependency_guard_is_killed` 对 Ref / EMA / rolling 的 unknown、late 六个对照复用正向行为断言，必须捕获 AssertionError。集成的 `test_seam_g3_unknown_dependency_clock` 与反向突变也通过。

独立突变选中 `tests/research/test_feature_availability_or05.py::test_i03_review_ref_999_reproduction`；把所有 __avail_unknown 强制设为 False，原 999/null 反例重新穿透。基线与突变的实际 pytest 摘要如下（退出码依次为 0、1；完整失败栈见 `I03-mutant.log`）：

```text
baseline: 1 passed in 0.04s
mutant: 1 failed in 0.08s
```

### 7.4 I04：闭合

原样执行 §2 I04 的完整 sh 代码块（自动抽取，未改一字）；进程退出码 `1`。实际输出：

```text
I04 latency_s= 0 value= 100.0 valid= True
Traceback (most recent call last):
  File "<stdin>", line 11, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/features.py", line 228, in feature_snapshot
    ctx = _resolve_ctx(ctx, cutoff, latency, max_staleness, step)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/features.py", line 132, in _resolve_ctx
    _check_time_parameters(latency, max_staleness)
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/features.py", line 122, in _check_time_parameters
    raise SnapshotInvalid("latency 必须为非负 timedelta")
quant_lab.research.features.SnapshotInvalid: latency 必须为非负 timedelta
```

rc=1 是明确的 SnapshotInvalid 拒绝，发生在负 latency 分支；0 秒仍有效。`_resolve_ctx` 校验便捷参数和 SnapshotContext，G2 公共入口也有负值守卫。research `test_i04_review_negative_latency_rejected` 覆盖两入口 × -1µs/-60s；`test_nonnegative_latency_preserves_equality` 保留 0/正延迟；`test_max_staleness_invalid_domain_rejected` 及严格正边界通过。market 的 `test_i04_public_entries_reject_negative_latency_keep_h0_equality`、`test_i04_missing_guard_did_not_raise_per_entry` 与 integration `test_seam_negative_latency_clock` 通过。

对应内存突变删除研究层参数守卫、删除 G2 latency 守卫，分别使同一拒收断言出现 DID NOT RAISE；并非只改 ExecutionPolicy。

独立突变选中 `tests/research/test_feature_availability_or05.py::test_i04_review_negative_latency_rejected`；将 _check_time_parameters 换为空函数，两入口 × 两个负值全部失败。基线与突变的实际 pytest 摘要如下（退出码依次为 0、1；完整失败栈见 `I04-mutant.log`）：

```text
baseline: 4 passed in 0.03s
mutant: 4 failed in 0.08s
```

### 7.5 I05：未闭合

原样执行 §2 I05 的完整 sh 代码块（自动抽取，未改一字）；进程退出码 `1`。实际输出：

```text
Traceback (most recent call last):
  File "<stdin>", line 6, in <module>
TypeError: episodes() missing 1 required positional argument: 'fresh_lake'
```

旧命令因 `episodes` 新增 fresh_lake 参数而提前退出，尚未执行身份伪造；不能把此 TypeError 当作拒绝外来图的证据。补跑 `or05-adapted.py`：先驱动 fresh_lake 的生成器夹具，再将其传给 episodes；其余 I05 的 baseline/foreign_graph 逻辑保持不变。真实输出（同一次补跑也包含下节 I06）：

```text

I06 fresh build: 1.224s; root=/var/folders/js/d8w4bf351gvdk84fn820t3nw0000gn/T/or05-i06-i77cgsq4/data; batch=tg-de7710d283f0
I05 baseline status= ok theta= 0.0 n_evaluated= 45
I05 foreign_graph EvalProtocolError baseline 臂 的 graph_version 与冻结机会集不一致（56 行，例 ['027317e07d034bff84f16a2a181ea508', '0e7575d4c223ff00a9f6b60633623515', '0f0c1d2b210d6ea5d3240f4eef1ef301']）：结果不来自本机会集所冻结的图版本 / 决策快照，不得评估
I06 forged REJECTED AssertionError 时钟特征值不符
I06 test_seam_g2_build_request_is_sole_entrypoint REJECTED 执行结果与完整机会集不相等
I06 test_e2e_sample_is_not_degenerate ACCEPTED reduced_rows=1
I06 test_seam_g3_pair_arms_consumes_g2_output REJECTED 执行结果与完整机会集不相等
```

已修部分：冻结的 provenance 含逐 episode graph_version / decision_snapshot_hash / t_dec，并纳入 digest；evaluate 把它传入 pair_arms，逐臂对照。`tests/research/test_opportunity_provenance.py::test_single_identity_mismatch_is_refused`、`::test_two_arms_consistently_wrong_are_refused`、`::test_unresolved_alias_does_not_match_the_frozen_version`、`::test_provenance_is_part_of_the_frozen_digest`、`::test_refusal_leaves_a_failed_terminal_state_in_the_ledger` 均通过。alias 测试只证明未解析字符串不能冒充固定版本，不冒称已做真实磁盘 alias 重指向验收。

已有 `test_mutation_without_provenance_check_the_forgery_is_paired` 通过省略 provenance 证明臂间比较单独不足；本轮另把 evaluate 实际经过的 `_check_provenance` 删除，核对原拒收测试会红：

独立突变选中 `tests/research/test_opportunity_provenance.py::test_two_arms_consistently_wrong_are_refused`；将 _check_provenance 换为空函数，两臂同时来自错误图仍被接受，pytest 报 DID NOT RAISE EvalProtocolError。基线与突变的实际 pytest 摘要如下（退出码依次为 0、1；完整失败栈见 `I05-mutant.log`）：

```text
baseline: 1 passed in 0.09s
mutant: 1 failed in 0.12s
```

残留是 §2 I05 已要求的“特征来源同时绑定”，不是从 A01 提升一个新范围：`features.py::feature_snapshot` 返回时丢弃 anchors 上的 graph_version / decision_snapshot_hash；`evaluator.py::_evaluate` 只检查 features **已有**的来源列。于是同 episode_id、同 t_dec 的另一图特征经过正常公开 API 后，只留下 t_dec，仍可用于本机会集。

补充复现（quant-lab/、副本环境、g3 venv；未改源码、不传 ledger）：

```python
import polars as pl
from quant_lab.research.features import feature_snapshot, SnapshotContext
from tests.research.test_opportunity_provenance import _setup, _eval
from tests.research.test_evaluator import AST
_, opp, ex, _ = _setup()
n = len(opp.episode_ids)
anchors = opp.provenance.with_columns(
    pl.lit('BTCUSDT-PERP.BINANCE-UM').alias('instrument_id'),
    pl.lit('foreign-graph').alias('graph_version'),
    pl.lit('foreign-snapshot').alias('decision_snapshot_hash'))
bars = pl.DataFrame({'instrument_id':['BTCUSDT-PERP.BINANCE-UM']*n,
    'interval':['1m']*n, 'close_time':opp.provenance['t_dec'], 'close':[999.]*n})
f = feature_snapshot([AST], anchors, bars=bars,
                     ctx=SnapshotContext(graph_version='foreign-graph'))
print(f.columns)
r = _eval(opp, ex, f)
print(r.status, r.theta, r.n_evaluated)
# 对照：仅补回同一快照本来携带的外来图列，便明确拒绝。
_eval(opp, ex, f.with_columns(pl.lit('foreign-graph').alias('graph_version')))
```

实际输出摘自 `I05-extra.log`：

```text
foreign_snapshot_columns= ['episode_id', 't_dec', 'f_2fbe45dd48a486255e2e7fe5b278bc4819e8df728ee897445c9e087e8dd21b29', 'validity_2fbe45dd48a486255e2e7fe5b278bc4819e8df728ee897445c9e087e8dd21b29']
foreign_snapshot status= ok theta= 0.0 n_evaluated= 4
same_snapshot_with_provenance= EvalProtocolError features 的 graph_version 与冻结机会集不一致（4 行，例 ['a', 'b', 'c']）：结果不来自本机会集所冻结的图版本 / 决策快照，不得评估
```

现有 `test_features_carrying_foreign_identity_are_refused` 仅覆盖人工附带 graph_version 的 frame，没有覆盖正常 feature_snapshot 丢列后的路径。修复需使快照来源成为不可省略、可核对的身份，并增加以上公开路径的回归与突变对照。另测 ns/ms 时间戳在字符串比较下被拒绝；该探针改变了规范时间精度，未据此另列新增缺陷。

### 7.6 I06：闭合

原样执行 §2 I06 的完整 sh 代码块（自动抽取，未改一字）；进程退出码 `1`。实际输出：

```text
Traceback (most recent call last):
  File "<stdin>", line 6, in <module>
TypeError: episodes() missing 1 required positional argument: 'fresh_lake'
```

同 I05，旧脚本的 rc=1 仅是夹具签名变化。`or05-adapted.py` 补齐 fresh_lake，时钟测试改按现签名传 convenience/0；上节输出证明伪造全 invalid/999999 被拒，漏一整批仅留一行也分别被 build_request 接缝及完整机会集配对断言拒绝。`test_e2e_sample_is_not_degenerate` 单独仍接受一行；它本来只是比例非退化门，完整集合守卫另有独立断言，不将其包装为单独的完整性门。

本轮 integration 从空临时湖调用当前 G1 api.build，`test_seam_g1_fresh_build_identity` 核本轮图、批次、输入 hash 及当前源码/输入文件 hash。`test_e2e_loss_table_and_quarantine_readable` 固定逐 ID 核 Q/MAP/LOSS；不是非空/height>=0 断言。已通过的反向控制包括：

- `test_mutation_feature_all_invalid`、`test_mutation_feature_future_changes_past`：复用逐值时钟断言。
- `test_mutation_unknown_dependency_assumed_known`、`test_mutation_late_bar_ignores_arrival`、`test_mutation_feature_accepts_negative_latency`、`test_mutation_market_accepts_negative_latency`：I02–I04 反例进入集成时钟门。
- `test_mutation_execution_missing_episode`：两个独立完整集合检查都必须失败。
- `test_mutation_quarantine_known_object_disappears`、`test_mutation_mapping_known_object_disappears`、`test_mutation_loss_fabricated`：已知隔离对象、映射及本批计数/分层/身份缺失必须失败。
- `test_mutation_published_episodes_wrong_identity`：图或批次换掉必须失败。

以上函数均位于 `tests/integration/test_e2e_synthetic.py`；42 项全套含参数化控制。额外独立突变：

独立突变选中 `tests/integration/test_e2e_synthetic.py::test_seam_g3_feature_snapshot_respects_t_dec`；用 999999/False 伪造 feature_snapshot，保留正确 ID/时间/列名，六个时钟参数组合均被原断言杀死。基线与突变的实际 pytest 摘要如下（退出码依次为 0、1；完整失败栈见 `I06-mutant.log`）：

```text
baseline: 6 passed in 0.28s
mutant: 6 failed in 0.14s
```

### 7.7 I07：未闭合

原样执行 §2 I07 的完整 sh 代码块（自动抽取，未改一字）；进程退出码 `0`。实际输出：

```text
OR05 terminal_count= 1 rc= 1
OR01 forced_show_failure=1 rc= 0
```

OR-05 已能拒绝当前一审 fail。旧 OR-01 探针仍注入 `python()`，修复后实际调用的是 `python3`，所以该 rc=0 不是上游失败被吞的证据。必须用当前调用名复测。

补测在临时 `gates/` 建目录、复制 contracts 与 taskList；取 `tasks[id]['verify']` 原文执行，仅替换临时报告正文/临时 frozen 值。OR-01 的实际失败注入是 `python3() { return 1; }; ` 加原 verify。报告输入：none 为仅标题；fail/insufficient/bare_pass 只有对应终裁；complete_pass 加“证据完整性：完成”；incomplete_pass 加“证据完整性：未完成”；codeblock_pass 为真实 fail 后附一个 text fenced block，其中出现 pass 终裁行。全部实际输出：

```text
OR05 none: rc=1; verdicts []
OR05 fail: rc=1; verdicts ['fail']
OR05 insufficient: rc=1; verdicts ['insufficient']
OR05 pass_then_fail: rc=1; verdicts ['pass', 'fail']
OR05 complete_pass: rc=0; verdicts ['pass']
OR05 incomplete_pass: rc=0; verdicts ['pass']
OR05 bare_pass: rc=0; verdicts ['pass']
OR05 codeblock_pass: rc=0; verdicts ['fail', 'pass']
OR01 frozen=True: rc=0
OR01 frozen=False: rc=1
OR01 frozen='true': rc=1
OR01 python3 forced failure: rc=1
OR05 old-gate mutant on fail: rc=0; expected nonzero assertion=FAIL
OR01 semicolon mutant frozen=False: rc=0; expected nonzero assertion=FAIL
```

OR-01 的 true/false/字符串及 python3 失败传播已闭合。OR-05 的无终裁、fail、insufficient、历史 pass 加末轮 fail 已闭合；但原 §2 验收要求的证据完整性仍没有被检查，且 regex 把代码块示例当作正式终裁。`bare_pass`、`incomplete_pass`、`codeblock_pass` 的 rc=0 是当前门的可复现误放行，属本条修复不完整，沿 I07 计数。

本轮另将门控矩阵落成临时 pytest `test_or05_gates.py`，以真正的退出码断言复核：

```text
baseline: 2 passed in 0.73s; rc=0
mutant: 2 failed in 0.43s; rc=1
remaining: 3 failed in 0.14s; rc=1
```

`::test_rejects_failure[OR-01/OR-05]` 基线通过；切旧门/分号后两个断言确实失败。`::test_requires_complete_formal_pass` 的三个当前代码反例都失败，分别对应 bare_pass / incomplete_pass / codeblock_pass。

仓库没有针对 OR-01/OR-05 verify 的持久回归测试；本轮的临时门控矩阵就是直接执行原文的回归证据。末两行是独立突变对照：恢复旧 OR-05 门后 fail 输入变 rc=0；OR-01 将 && 改回分号后 frozen=False 变 rc=0；两者都违反同一个“应非零”断言。当前正向门还需绑定本轮证据完成状态并区分正文终裁与代码示例；允许历史轮次存在，不要求删掉一审终裁。

### 7.8 修复增量与二审计数

未另立新编号：0 条。I05 的特征血缘省略、I07 的完整性/终裁解析缺口均是原修复范围内的残留，不能以套件全绿宣布闭合。bf1fe55 的依赖内容身份、worker 启动及 job 绑定增量随本次 565 项 research 回归通过；此处不把单元回归冒称正式 MC 重跑或已执行 R-09 verify。与本轮修复无关的草稿接缝文案偏差归 §9 A04，应改，不升级。

## 8. 接缝实跑证据索引

下表节点均来自本次四套完整执行，函数名直接对应仓库；不是仅 collect、历史回执或文件存在性。PASS 仅覆盖所列断言，G1/G2/G3 单侧测试与跨层消费明确分开。没有整条完全缺乏测试的接缝；S7 的 G3 θ 消费尚不存在，消费差分不适用，不以估值单测冒充。

| 接缝 | 本次实际覆盖的测试（文件::函数） | 本次结果与证据边界 |
|---|---|---|
| S1 G1→G3 决策视图 | `tests/data/test_lifecycle.py::test_api_views_and_manifest_barrier`；`tests/data/test_lifecycle.py::test_decision_view_invariant_to_future_events_full_row`；`tests/integration/test_e2e_synthetic.py::test_seam_g1_decision_view`；`tests/integration/test_e2e_synthetic.py::test_e2e_theta_ledger_loss_quarantine` | PASS，data 225 / integration 42。G1 门含非空 t_dec、资格、致命码与 manifest；集成从本轮新湖消费到冻结机会集及 evaluate。 |
| S2 G1→G3 决策边 | `tests/data/test_lifecycle.py::test_decision_events_only_before_t_dec`；`tests/data/test_lifecycle.py::test_api_views_and_manifest_barrier` | PASS，data 225。实际调用 load_episode_events，join t_dec 后断言严格 `<`；这是 G1 导出 API 证据，当前集成没有单独把边表输入 G3 的端到端断言。 |
| S3 G1→* LOSS / quarantine | `tests/data/test_lifecycle.py::test_loss_six_layers_conserved`；`tests/integration/test_e2e_synthetic.py::test_e2e_loss_table_and_quarantine_readable`；`tests/integration/test_e2e_synthetic.py::test_mutation_quarantine_known_object_disappears`；`tests/integration/test_e2e_synthetic.py::test_mutation_mapping_known_object_disappears`；`tests/integration/test_e2e_synthetic.py::test_mutation_loss_fabricated` | PASS，data 225 / integration 42。六层守恒、本批身份与固定隔离 cohort 的逐 ID 断言及突变均执行。 |
| S4 G3→G2 build_request | `tests/market/test_contract.py::test_build_request_from_episode_row`；`tests/market/test_contract.py::test_b8_fractions_nullable_policy_split_and_hashed`；`tests/integration/test_e2e_synthetic.py::test_seam_g2_build_request_is_sole_entrypoint` | PASS，market 379 / integration 42。null fraction 保留作者未知语义，执行分配由具名 policy 解析并入 hash，不能将此说成拒绝所有政策分配。 |
| S5 G3→G2 simulate_batch | `tests/market/test_execution_api.py::test_simulate_requires_market_and_checks_manifest`；`tests/market/test_execution_api.py::test_simulate_batch_schema_and_pairing`；`tests/market/test_execution_api.py::test_simulate_batch_non_strict_keeps_error_rows`；`tests/market/test_outcome_kind.py::test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes`；`tests/market/test_execution_api.py::test_g3_evaluate_consumes_frozen_contract_output` | PASS，market 379。包含 A/B 入口、strict 抛错及真实批量 hash 冲突门、G3 消费。当前契约是每个 policy_version 恰好对应一个 hash，非整批只能一个 hash；草稿缩写应修正。 |
| S6 G2→G3 outcome.kind | `tests/market/test_outcome_kind.py::test_every_result_maps_to_exactly_one_of_seven_values`；`tests/market/test_outcome_kind.py::test_six_values_reachable_and_filled_closed_unreachable_in_v0`；`tests/market/test_outcome_kind.py::test_batch_exposes_both_columns`；`tests/market/test_execution_api.py::test_g3_evaluate_consumes_frozen_contract_output` | PASS，market 379。契约 §9.10.11 七值包含 unfilled_expired，不包含 cancelled；草稿“已并入 cancelled”与现行契约相反，不能用这些 PASS 证明该句话。v0 六值真实可达，第七值有政策腿构造对照。 |
| S7 G2→G3 force_close_net_R | `tests/market/test_force_close_net_r.py::test_accounting_and_read_only`；`tests/market/test_force_close_net_r.py::test_fixture_asof_and_sentinel`；`tests/market/test_force_close_net_r.py::test_force_close_empty_registration` | PASS，market 379。核估值、返回值哨兵、事件/原对象不变，以及扫描到的实际调用方集合和登记集合均为空。G3 θ 消费差分：不适用（当前无消费方）。 |
| S8 G3 calendar_blocks.n_nonempty | `tests/research/test_maxt.py::test_hand_computed_theta_and_jackknife_se`；`tests/research/test_maxt.py::test_B20_sufficiency_floor_pairs_block_len_and_pins_cross_and_span`；`tests/research/test_maxt.py::test_B20_max_t_panel_uses_declared_floor`；`tests/research/test_review_p1_closure.py::test_s11_censored_blocks_do_not_count_as_nonempty` | PASS，research 565。手算非空块、19/20 下限、删失块不充数、block_len 与门限成对。 |
| S9 全局 Decimal(38,12) | `tests/data/test_review_probes.py::test_a18_verbatim_source_string_reaches_gold`；`tests/data/test_lifecycle.py::test_a8_decimal_columns_and_hash_normalization`；`tests/market/test_execution_api.py::test_simulate_batch_schema_and_pairing`；`tests/market/test_contract.py::test_b8_fractions_nullable_policy_split_and_hashed` | PASS，data 225 / market 379。原始高精度数字逐字到 gold、物理 Decimal 类型、执行输出 Decimal 与 1/3 分配余量；这些是具名路径证据，不是任意未来插件转换均无损的证明。 |

## 9. 应改转已知缺口

A01–A03 维持应改；下列是建议债主及可执行的触发事件，不表示已派发或已接受风险。A04 是本次新增的应改，代码与契约偏差的来源早于 bf1fe55/b2a30eb。

| 编号 | 已知缺口 | 建议债主 | 触发事件与应补证据 |
|---|---|---|---|
| A01 | G1 撤权源→G3 热缓存 / cache=False 尚缺同源端到端验收；单个 callback 测试不能替代跨层撤权 | G0 接缝负责；G1 提供 manifest/tombstone；G3 接入统一拒读 | 首次把真实 G1 图撤权接入 G3 缓存，或启用会复用图的研究任务前：新图生成快照→同图 tombstone→热读和无缓存重算均拒绝→合法新图可重放。I05 的特征身份残留另行处理，不混入此项。 |
| A02 | alias/index/tombstone 及同月行情分区的并发发布/撤权缺故障试验 | G1 负责图元数据；G2 负责行情分区；G0 负责联合调度 | 首次允许同一分区两个 writer，或发布与撤权可交错前：并发幂等、kill/retry、读改写冲突、无丢更新、撤权胜出证据。当前没有动态 lost-update 结论。 |
| A03 | 其余 verify 的关键词/位置/数量门尚未绑定内容、退出码和本轮身份 | G0 总负责；G1/G2/G3 分别偿还 D/M/R 任务门 | 每条弱 verify 下次被用于 done/里程碑签字或修改命令时：正反夹具、上游失败传播、制品内容和本轮输入/code/attempt 身份绑定；M-08、R-05 等仍按一审分类保留。OR-01/OR-05 的范围专属 I07，不借此降级。 |
| A04 | 集成草稿 S5“整批 hash 唯一”、S6“unfilled_expired 并入 cancelled”与现行 B16/B17、A15 不一致 | G0 文档债主；G2 核对导出枚举与批量门；G3 核对消费解释 | OR-06 草稿定稿/复制进最终接缝表前：将 S5 改成按 policy_version 唯一 hash；S6 使用契约七值并链接本次测试。只改草稿措辞即可，不将既有契约改去迎合草稿。 |

交付验收：追加前原文逐字节相等；quant-lab 范围的 `git diff --stat -- quant-lab` 仅显示本文件。全仓不带路径的 diff --stat 还包含任务开始前已有的用户改动，因此无法字面只剩本文件；保留它们并对照开始时工作树快照核验，本次新增变更集合仅本文件。格式核验输出为 `['fail', 'fail']`，共两行正式终裁。未添加第三行，也未更改一审终裁。

终裁：fail


## 10. 三审（2026-09-24，提交 4529c11）

核验对象为 `4529c1134c883128867a9cacd52e31bd43c4b80b`，工作树 HEAD 与之相同，取证前 quant-lab 无工作树差异。只复核 §7 判为未闭合的 I05、I07，以及 `git show 4529c11` 的修复增量。I01–I04、I06 沿用二审闭合结论；本次未发现增量破坏它们的证据，不另行重审。

执行前均在 quant-lab/ 使用以下隔离前缀；测试关闭字节码写入与 pytest 缓存。未运行 R-09 verify、正式 MC 生成或任何生产操作。唯一仓库写入为本章节末尾追加。

```sh
T=$(mktemp -d); cp -R data "$T/data"; export QUANT_LAB_DATA_ROOT="$T/data"
export PYTHONDONTWRITEBYTECODE=1
```

证据临时目录：`/var/folders/js/d8w4bf351gvdk84fn820t3nw0000gn/T/tmp.LXkplMCVll`。Grok MCP 被当前不可审批策略拒绝，companion CLI 随后因状态目录写权限报 EPERM，按项目降级约定由 Codex 执行本轮取证及追加。

### 10.1 I05：闭合

`feature_snapshot` 按 anchors 行携带 `graph_version`、`decision_snapshot_hash`，并保留 `t_dec`；evaluate 要求三个来源列齐全，并逐 episode 与冻结 provenance 比较。三列逐个删除或改值的六个探针均明确拒绝，合法快照回归通过。

从 §7.5 的“补充复现”自动抽取原 Python 代码块，保存为临时 `I05-verbatim.py`，内容未改；使用 `.venv-g3/bin/python -B -u <临时脚本>`、`PYTHONPATH=<quant-lab>:<quant-lab>/src` 执行。退出码 **1**，完整实际输出如下（错误已到特征身份守卫，不是夹具签名或导入失败；在第一次 `_eval` 处终止，后面的人工补列对照未执行）：

```text
['episode_id', 't_dec', 'graph_version', 'decision_snapshot_hash', 'f_2fbe45dd48a486255e2e7fe5b278bc4819e8df728ee897445c9e087e8dd21b29', 'validity_2fbe45dd48a486255e2e7fe5b278bc4819e8df728ee897445c9e087e8dd21b29']
Traceback (most recent call last):
  File "/var/folders/js/d8w4bf351gvdk84fn820t3nw0000gn/T/tmp.LXkplMCVll/I05-verbatim.py", line 16, in <module>
    r = _eval(opp, ex, f)
        ^^^^^^^^^^^^^^^^^
  File "/Users/balen/projects/trader-bot/quant-lab/tests/research/test_opportunity_provenance.py", line 31, in _eval
    return evaluate(AST, opp, features=feats, rule=take_all, execution=ex, fold_id="f0", attempt_id=kw.pop("attempt_id", "a0"), **kw)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/evaluator.py", line 294, in evaluate
    res = _evaluate(ast, opp, features=features, rule=rule, execution=execution, fold_id=fold_id, attempt_id=attempt_id,
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/evaluator.py", line 345, in _evaluate
    _check_provenance(pl.DataFrame({"episode_id": ids}).join(features.select("episode_id", *PROVENANCE_KEYS), on="episode_id", how="left"),
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/evaluator.py", line 104, in _check_provenance
    raise EvalProtocolError(f"{label} 的 {k} 与冻结机会集不一致（{bad.height} 行，例 {bad['episode_id'].head(3).to_list()}）："
quant_lab.research.evaluator.EvalProtocolError: features 的 graph_version 与冻结机会集不一致（4 行，例 ['a', 'b', 'c']）：结果不来自本机会集所冻结的图版本 / 决策快照，不得评估
```

三个独立拒收维度的补充实际输出（脚本 rc=0，逐例捕获预期 EvalProtocolError）：

```text
missing graph_version: EvalProtocolError features 缺来源身份列 ['graph_version']：无法核对特征是否来自冻结机会集的图版本 / 决策快照，anchors 须带这些列再做 feature_snapshot
mismatch graph_version: EvalProtocolError features 的 graph_version 与冻结机会集不一致（4 行，例 ['a', 'b', 'c']）：结果不来自本机会集所冻结的图版本 / 决策快照，不得评估
missing decision_snapshot_hash: EvalProtocolError features 缺来源身份列 ['decision_snapshot_hash']：无法核对特征是否来自冻结机会集的图版本 / 决策快照，anchors 须带这些列再做 feature_snapshot
mismatch decision_snapshot_hash: EvalProtocolError features 的 decision_snapshot_hash 与冻结机会集不一致（4 行，例 ['a', 'b', 'c']）：结果不来自本机会集所冻结的图版本 / 决策快照，不得评估
missing t_dec: EvalProtocolError features 缺来源身份列 ['t_dec']：无法核对特征是否来自冻结机会集的图版本 / 决策快照，anchors 须带这些列再做 feature_snapshot
mismatch t_dec: EvalProtocolError features 的 t_dec 与冻结机会集不一致（4 行，例 ['a', 'b', 'c']）：结果不来自本机会集所冻结的图版本 / 决策快照，不得评估
```

常驻回归与突变：`tests/research/test_opportunity_provenance.py` 的 `test_public_snapshot_carries_the_anchor_identity_and_matching_features_pass`、`test_public_snapshot_from_a_foreign_graph_is_refused`、`test_features_without_identity_columns_are_refused`、`test_mutation_optional_feature_identity_would_accept_the_foreign_snapshot` 全部通过。最后一项确实把特征身份检查退回只看 t_dec，外来快照去掉两列后得到 status=ok，证明旧可选身份检查能够漏收。

另做独立进程内突变：从 `git show 4529c11^:quant-lab/src/quant_lab/research/{features,evaluator}.py` 读取两份父提交实现到临时目录，通过临时 pytest 插件只在测试进程中恢复它们，仓库源码未改。对前三个新增回归使用相同选择器：

```text
baseline: 3 passed in 0.29s; rc=0
mutant:   3 failed in 0.83s; rc=1
```

突变失败分别为输出身份列缺失，以及两项拒收断言 DID NOT RAISE EvalProtocolError；不是环境错误。完整记录为 `I05-baseline.log`、`I05-mutant.log`。本条补充复现、合法对照、缺列/错配拒收及突变证据齐全。

### 10.2 I07：未闭合

OR-05 看板 verify 确为 `python3 scripts/review_gate.py docs/adr/review-G0-integration.md`。在临时 gates/ 复制 scripts、contracts、taskList，以看板原文命令运行，只替换临时报告和 frozen 值；§7.7 的八类 OR-05 输入及 OR-01 四类输入全部得到预期退出码。输入按该节描述原样复建：none 仅标题，fail/insufficient/bare_pass 仅终裁，pass_then_fail 为历史 pass 后最终 fail，complete/incomplete_pass 加对应完整性行，codeblock_pass 为正文 fail 后 text 代码块中的 pass。输入全文保存于 `gate-matrix-inputs.json`。

全部实际退出码与输出（末三例为本轮新增边界探针）：

```text
OR05 none: rc=1; 没有正式终裁行
OR05 fail: rc=1; 最后一轮终裁为 fail
OR05 insufficient: rc=1; 最后一轮终裁为 insufficient
OR05 pass_then_fail: rc=1; 最后一轮终裁为 fail
OR05 complete_pass: rc=0; 最后一轮终裁 pass，证据完整性完成（共 1 轮）
OR05 incomplete_pass: rc=1; 最后一轮的证据完整性标记为 ['未完成']，须恰为一条「完成」
OR05 bare_pass: rc=1; 最后一轮的证据完整性标记为 []，须恰为一条「完成」
OR05 codeblock_pass: rc=1; 最后一轮终裁为 fail
OR01 frozen=True: rc=0;
OR01 frozen=False: rc=1;
OR01 frozen='true': rc=1;
OR01 python3 forced failure: rc=1;
OR05 old-gate mutant on fail: rc=0;
OR01 semicolon mutant frozen=False: rc=0; usage: task.py [-h]
               {show,report,claim,done,set-verify,block,unblock,review} ...
task.py: error: argument cmd: invalid choice: 'scripts/task.py' (choose from show, report, claim, done, set-verify, block, unblock, review)
OR05 four_backticks: rc=0; 最后一轮终裁 pass，证据完整性完成（共 2 轮）
OR05 trailing_text: rc=0; 最后一轮终裁 pass，证据完整性完成（共 2 轮）
OR05 four_tildes: rc=0; 最后一轮终裁 pass，证据完整性完成（共 2 轮）
```

其中旧 OR-01 突变沿用了常驻测试的 python 包装，因重复传入 scripts/task.py 产生 argparse 错误；该次 rc=0 只能直接证明旧门吞上游失败。为避免把它误称为成功读取 frozen=False，另用正确的参数转发 `python() { python3 "$@"; }; ` 加旧命令复测，实际输出：

```text
OR01 corrected Python alias, old gate frozen=False: rc=0; stdout=''; stderr=''
```

常驻覆盖核对：`tests/integration/test_board_gates.py::test_or05_gate_matrix` 覆盖 §7.7 八类输入的判据，另外覆盖重复完整性行、未闭合围栏、跨轮借用完整性、历史 fail 后合法 pass、代码示例后合法 pass；OR-01 的 frozen 真/假/字符串/None 及 python3 上游失败另有参数化测试。旧 OR-05 的 fail/bare_pass/codeblock_pass 和旧 OR-01 的突变也已执行。该文件共 23 项通过，与 provenance 18 项合跑为 `41 passed in 0.87s`、rc=0。

覆盖是行为分类上的覆盖，不声称文本逐字相同：常驻 none 多正文，fail/insufficient/pass_then_fail/codeblock_pass 加了完整性行；本轮另复测了 §7.7 所述原输入。常驻用例未覆盖下列围栏长度及结束行规则。

**三审需修发现（沿 I07）：新门仍会将代码块内的示例终裁当正文。** 位置 `scripts/review_gate.py:25–31`。开始围栏只保留 `s[:3]`，结束只看 `startswith(fence)`：四反引号块内的三反引号行被错误视为结束；四波浪号同理；三反引号后带非空尾随文本也被错误视为结束。后面的示例证据完成/pass 被当成第二轮，真实正文终裁仍为 fail，却 rc=0。

以下最小复现可独立运行（在 quant-lab/，沿用本节隔离环境；只写临时文件，不修改真实报告）：

```python
import pathlib, subprocess, tempfile
cases = {
    'four_backticks': ['终裁：fail', '````text', '```', '证据完整性：完成', '终裁：pass', '````'],
    'trailing_text': ['终裁：fail', '```text', '```not-a-closing-fence', '证据完整性：完成', '终裁：pass', '```'],
    'four_tildes': ['终裁：fail', '~~~~text', '~~~', '证据完整性：完成', '终裁：pass', '~~~~'],
}
with tempfile.TemporaryDirectory() as tmp:
    for name, lines in cases.items():
        path = pathlib.Path(tmp) / (name + '.md')
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        r = subprocess.run(['python3', 'scripts/review_gate.py', str(path)], capture_output=True, text=True)
        print(name, 'rc=', r.returncode, r.stdout.strip())
```

三例实际均 rc=0，输出均为“最后一轮终裁 pass，证据完整性完成（共 2 轮）”；预期均非零。这里仅有一轮正文 fail，所谓第二轮全部在代码块内。三个输入全文分别保存在 `four_backticks.md`、`trailing_text.md`、`four_tildes.md`。这是原 I07“仅正文终裁”修复不完整，不因旧矩阵通过而闭合，也不把它拆成三个编号。

改法及验收：围栏状态须保留字符与完整长度；关闭围栏必须同字符、长度不少于起始围栏，后面只能有允许的空白。将上述三例加入常驻矩阵，原 fail 必须被保留；同时保留三字符合法关闭、历史多轮及完整 pass 的正向用例。

### 10.3 增量、计数与交付验证

逐项检查 4529c11 的生产实现、测试夹具、看板及报告增量。三审需修发现 **1 条**，归入原 I07 的围栏解析残留；新增独立编号 **0**。I05 已闭合，I01–I04、I06 继承二审；因此 I01–I07 尚未全部闭合，终裁为 fail。没有发现其他可复现的运行行为回归。

应改 A05：`test_mutation_the_old_or01_gate_swallowed_the_failure` 的 python 包装应改为正确转发参数，并可核 stderr，避免把 argparse 失败当作读到 frozen=False 的证据。本轮修正包装后的独立复测仍 rc=0，现行 OR-01 对四类输入均正确，因此这是突变证据归因的改进，不另计阻断项。原 A01–A04 保留既有分类。

null-model 报告 JSON 的 13 条 results 与父提交逐字段比对，仅 wall_s 改变；统计结果未变，摘要与回执身份随代码更新。本轮不冒称重跑了正式 MC。

四套全量测试（均带 `-B -m pytest ... -q -p no:cacheprovider`）：

| 环境 / 套件 | 实际输出摘要 | rc |
|---|---|---|
| `.venv-g3` / `tests/research` | `569 passed in 242.77s (0:04:02)` | 0 |
| `.venv-g2` / `tests/market` | `379 passed in 22.30s` | 0 |
| `.venv-g1` / `tests/data` | `225 passed in 35.45s` | 0 |
| `.venv-g0` / `tests/integration` | `65 passed in 3.81s` | 0 |

全量绿灯与 I07 三个反例并存；终裁由可复现反例决定。原报告字节作为追加前基线保存，交付时核对新增章节前缀与之完全相同；量化目录仅本文件新增 diff，仓库其他既有改动保留。

追加后在 quant-lab/ 执行 `python3 scripts/review_gate.py docs/adr/review-G0-integration.md`，实际 rc=1，完整输出如下：

```text
最后一轮终裁为 fail
```

证据完整性：完成
终裁：fail
