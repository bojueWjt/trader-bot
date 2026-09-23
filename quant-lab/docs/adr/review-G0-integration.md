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
