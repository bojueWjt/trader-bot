## 十一审判定表

审查日期：2026-09-11。独立审查方：Codex 主控；只出具报告，不承担修复。
十一审判 fail，阻断项为 S40、S41：A24 对已有拆行网格表达和普通带注解 TTL 赋值存在可实跑的漏检。
十审的 S29、S31、S38、S39 原缺陷及指定回归均闭合；不把机制漏口冒称为当前交易结果已经错误。
S32、S33、S35、S37 的具名生产落点已归一，指定原形突变能够被检查识别。
修复方 45 个突变定义经本轮独立内存注入，覆盖 36 个 pytest 参数化节点，全部目标节点 RED→GREEN。
上述数量由本轮 pytest call 阶段结果计算，未使用 mutation_evidence.json 的红绿结果作为证明。
所有 closed 仅覆盖表中的具体命题；不是对整个缺陷族的无限外推。

| ID | 状态 | 本轮亲跑命令与真实输出摘要 | 阻断 P1 |
|---|---|---|---|
| S29 离网覆盖、首缺与计数 | closed | P10N 原命令：混合组 expected=3/missing=0，纯离网组 expected=3/missing=3，均 quarantined；M 的 S29-first-gap/range-only/truncated-count 共4节点红后绿；P10G 375+25组 PASS | 否；其余网格表达的机制遗漏见 S40 |
| S31 B17 真门、输入和诊断 | closed | M 的 S31-remove-output-gate、S31-sanitized-copy、S31-redacted-version，各2 failed→2 passed；真实 result_row 注入，版本串与三个完整 hash 均由测试断言 | 否 |
| S38 负延迟与显式/省略 | closed | P10C 原命令在政策层 ContractError，exit1；M 两项各1 failed→1 passed；P11B 对 latency=0/60、窗口−1/0/+1µs 两表达一致 | 否 |
| S39 loader 返回值生效 | closed | M 的 discard-return、inline-count、wrong-helper-value 各1 failed→1 passed；控制湖下 expected+7 会影响覆盖判定 | 否 |
| S32 build_request 第二处 latency | closed | P10I 仅余 derived_t_start 内加法；M S32-build-start-bypass 两个门都红后绿；P11B build+7 ACCEPT，启动与 horizon 同移7秒 | 否 |
| S33 vision 网格整除 | closed | P10I 不再打印 vision total_seconds；M S33-wrong-expected 的12个日/月×interval节点全红后绿；A/AB分类不变 | 否 |
| S35 TTL解析及入场到期 | closed | P11I 实际3处 resolve_entry_ttl_s、3处 entry_expiry_at 登记相等；M before/after/builder/A-timeline/A-entries/B 全红后绿 | 否；P7语法覆盖缺口另列 S41 |
| S37 A中尾网格 | closed | P10I 不再打印旧比较；M S37-middle、S37-tail 各1 failed→1 passed；A 22/0 | 否；同函数只绕过一处的拆行形态见 S40 |
| A24 调用方集合双向门 | closed | M real-caller-missing/extra 均红；停用 missing/extra 分支，相应机制回归均红；恢复均绿 | 否；只证明函数级集合，未证明每个 Call 位置 |
| A24 P1–P7 逐模式可失败 | closed | M 9个表达分别插入真实生产文本均红；9次分别停用真实 _hit 路径，对应回归均红；恢复均绿 | 否；只证明列出的9种表达 |
| A24 例外位置真实性 | closed | P11I 实跑7个命中：5个单一来源定义处、2个 foreign，violations=[]；foreign-freeze 突变1 failed→1 passed | 否；证据与语义边界逐条见下文 |
| S34 funding固定8h边界 | partial-P2 | P10I 仍打印 hour//8、8h步进、60s容差；P11B +1/+2/+3ms与60s接受、60s+1µs隔离；AB S02 unsupported | 否；保留既有裁定，不要求改为精确bar网格 |
| S36 持仓截止重复 | partial-P2 | P10I 仍定位 A 424、B 530/560；T 相关 hold/exposure 回归通过，AB分类不变 | 否；既有 S04/S16范围 |
| S02/S04/S06/S07/S10/S11/S12/S13 | partial-P2 | T 293 passed；AB 输出4个具名 unsupported，P11B直接打印同一字典；其余项目边界见下文 | 否；不重新升级历史P2 |
| S40 P6漏掉已有拆行中段网格表达 | open | P11I 当前 partition_check:258–259 自写 delta>step，lint HITS=[]；P11G 将 A中段恢复同形表达，S 14 passed→14 passed，SHA256_EQUAL True | 是；A24门对树里已有同族表达不完整 |
| S41 P7忽略 AnnAssign | open | P11TTL 向生产文本加入带 int 注解的原 TTL 选择式，S 14 passed→14 passed，SHA256_EQUAL True | 是；普通类型注解即可使已规定的TTL禁令失效 |
| S29生产非负断言的性质分类 | closed | 附加突变：单删断言2 passed；off-grid+删非负断言2 failed于另一个生产不变量；再删缺口一致性断言2 failed于测试第25行；还原2 passed | 否；认可冗余防御分类，修正证据中的失败归因 |

### 审查边界、运行方式与证据身份

命令工作目录均为 quant-lab。
全程设置 PYTHONDONTWRITEBYTECODE=1；产品导入设 PYTHONPATH=src。
所有 pytest 调用都包含 -p no:cacheprovider；未生成仓库 pytest cache 或新字节码。
唯一持久写入为本报告；pytest 的临时湖与机制注入样例位于系统临时目录。
没有连接交易所私有 API，没有读取 services/nautilus-node 配置，没有 import services 模块。
没有恢复其他 Codex 会话，没有调用修复代理，没有进行任何生产操作。
工程规则已读取 AGENTS.md 和 /Users/balen/.codex/GROK.md。
本任务为独立终审，本轮证伪与裁判均由主控执行。

修复方 mutation_round10.py 的 main 会 write_text 生产源码并 write_bytes 还原。
本轮没有运行该 main；通过 runpy.run_path 只读取 CASES 定义。
注入器同时替换 SourceFileLoader.get_code 和 Path.read_text，前者让真实产品模块执行突变，后者让 AST 门看到同一突变。
Pydantic 模型在新进程首次加载时编译，未把旧 validator 缓存当成突变成功。
每例突变、每例基线各起独立子进程，收集 pytest 节点 call 阶段 failed/passed。
没有把 collection error、fixture TypeError 或单独的 exit1 算作 RED。
本轮45例的全部目标节点均是 call/failed，恢复后均为 call/passed。
Path.write_text 对政策登记表的写入转到内存；其他 src/tests 写入被包装器拒绝。
Path.read_bytes 仍读取真实磁盘，用于每个子进程前后的 SHA256 比较。
hash/版本身份读取真实磁盘不是突变行为证据；本轮变红依据均是行为/AST断言。
完整源码与测试集合的最终摘要见后文。
所有原始源码文件、测试文件、22个 episode 文件在本审开始与结束之间字节一致。
该事实只证明本轮没有改夹具，不冒称从未提供的十审前快照证明修复方未改夹具。

### 指定验证 T / S / A / AB

| 验证 | 实际执行 | 真实输出 |
|---|---|---|
| T | pytest tests/market -q -p no:cacheprovider；政策登记表内存保护包装 | 293 passed in 15.27s；exit0；SOURCE_TEST_SHA256_EQUAL True |
| S | .venv-g2/bin/python -m pytest tests/market/test_single_source.py -q -v -p no:cacheprovider | 14 passed in 1.23s；exit0 |
| A | .venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A | 22个 replay=True；kernel=A passed=22 failed=0；exit0 |
| AB | .venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1 | exit0；MATCH12/B_COMMAND_LATENCY3/GAP_PRICE1/SAME_TS_PRIORITY4/GTD_BOUNDARY1/B_LIQUIDITY_MODEL1 |
| AB解释门 | 上一命令实际 codes | UNEXPLAINED=0，A_GOLD_FAIL=0，NOT_RUN=0；合计22 |

T 不是裸跑：test_s30_window_lower_bound_uses_derived_start_not_t_dec 仍会临时 write_text 真实 policy_hashes.json。
为遵守只写报告的边界，上述写入用内存读写替身承接；没有改测试断言或政策校验逻辑。
AB 实测 median_ms：A=8.624166992376558、B=36.70199999760371。
单次计时不作为性能稳定性结论。
M-03 真实网络下载未做：本轮没有下载或改湖，历史 G0 公开归档证据仅作为既有裁定依据。
taskList 看板 verify 命令未做：用户指定本轮验证为 T/S/A/AB，本轮不宣称另行跑过看板。
这些未做事项不是本轮判 fail 的新增理由；阻断来自实跑的 S40/S41。

### 十审原命令：抽取、原样复跑及失效解释

从历史十审相应命令段第一对反引号抽取原字符串，shlex.split 后交给 subprocess.run。
命令内容未改写；环境统一继承禁字节码与 PYTHONPATH。
抽取定位最终使用 P10M包含/P10N包含/P10C复现/P10D复现等命令段标题。
初次宽匹配错误地落到前部叙述，误取 S38最小命令；该探索输出未计入以下证据，随后已按精确标题重跑全部9条。
P10M 原命令引用已删除的旧B16测试名，提前 AttributeError；没有发生它后续的 first_gap/删门突变。
P10N/P10D/P10L 直接调用新B17测试时缺 monkeypatch/later_version，TypeError 不计为红。
P10C 在负延迟政策构造层即抛错；这是政策域修复的证据，后续零/正延迟由 P11B 独立补跑。
P10D 的 loader旧哨兵仍 GREEN：该旧测试已删除 loader 部分，仅保留 A首点测试，不能拿它判新 loader 回归无效。
新 loader 回归的三个生产突变均在 M 中独立红后绿。
P10D 的 sanitized 实际批量仍输出2行2hash且不抛错，这是注入后的故意缺陷；未突变产品的 B17 回归已红绿证成。
各原命令的真实 stdout/stderr 逐条保留在后面的证据附录，包括非零退出。
P10I/P10S 是静态定位证据，不把表达位置本身当作经济数值错误。
P10G 的375组 helper独立枚举与25组对齐网格子集实际全过。
P10F 可空/缺省字段六输入枚举已复跑；其完整输出附后。

### S40 必修：清单声称禁中尾自写比较，当前树仍存在同形表达

位置：src/quant_lab/market/partition_check.py:258–259；single_source.py:_Lint.visit_Compare。
现有生产表达是 delta = (df[key] - prev)，然后 gap_before = (delta > step).fill_null(False)。
它仍独立判定相邻 bar 是否跨过缺口；并未向 grid_points_between 委派这个布尔结果。
输入被 legal 过滤后，该表达在当前受支持数据上与网格判断等价；本轮不宣称当前 missing 算错。
问题在于 P6 的说明“中尾网格不得用相邻时间差/步长比较重写”与当前树不一致，且没有登记此位置为例外。
visit_Compare 只在比较操作数本身为 Add/Sub 时命中；中间变量使它遗漏。
这不是任意别名或混淆：恰是当前生产文件已经使用的常规拆行写法，也符合项目鼓励单步变量的风格。

进一步将 KernelA._first_bar_gap 中段的 grid_points_between 调用改成同形两行。
尾段仍调用该 helper，所以函数级 ALLOWED_CALLERS 集合完全不变。
P6 同时漏检拆行比较，两道门合跑14 passed；还原14 passed。
这准确复现“登记函数仍有其他调用，某一个使用点已经绕过”的缺口。
函数级登记集合在其字面定义下是真实的，但不足以声称所有表达位置已受登记保护。
本项阻断 A24 的机制验收，不按任意复杂语义等价要求扩张扫描器。
验收应覆盖树里这个已知拆行形态，并明确每个实际表达位置或有证据的差异规则。
不得仅新增一个字符串样例而继续保留当前生产遗漏。
不得把 funding 的独立容差规则一起强制精确归一。

P11G最小只读复现命令：
`.venv-g2/bin/python -c 'exec("import pathlib,hashlib,sys,json,pytest\nfrom unittest.mock import patch\nitem=json.loads(sys.argv[1])\np=pathlib.Path(\"src/quant_lab/market\")/item[\"file\"]\nraw=p.read_bytes();source=raw.decode();original=pathlib.Path.read_text\nif item[\"old\"] is None:\n mutated=source+item[\"new\"]\nelse:\n assert source.count(item[\"old\"])==1\n mutated=source.replace(item[\"old\"],item[\"new\"])\ndef read(path,*a,**kw):\n if path.resolve()==p.resolve():\n  return mutated\n return original(path,*a,**kw)\nwith patch.object(pathlib.Path,\"read_text\",read):\n rc=pytest.main([\"tests/market/test_single_source.py\",\"-q\",\"-p\",\"no:cacheprovider\"])\nprint(\"INJECTED\",rc)\nrc=pytest.main([\"tests/market/test_single_source.py\",\"-q\",\"-p\",\"no:cacheprovider\"])\nprint(\"RESTORED\",rc,\"SHA256_EQUAL\",hashlib.sha256(raw).digest()==hashlib.sha256(p.read_bytes()).digest())\n")' '{"name":"S40-middle-split-bypass","file":"kernel_a.py","old":"if grid_points_between(prev + iv, o, interval_s) > 0:","new":"delta = o - prev\n            if delta > iv:","tests":["tests/market/test_single_source.py"]}'`

真实输出摘要：注入14 passed in 1.20s / INJECTED 0；恢复14 passed in 1.20s / RESTORED 0 SHA256_EQUAL True。
命令仅替换 Path.read_text 返回值以驱动真实 A24 测试，不写源文件。
另向 vision.py 内存文本新增同形拆行函数，S同样14 passed；说明不仅是现有调用方登记粒度问题。

### S41 必修：相同 TTL 选择式加类型注解即逃过 P7

位置：single_source.py:_Lint.visit_Assign；缺少对 ast.AnnAssign 的同类值检查。
P7 声称 TTL计划/政策选择必须经过 resolve_entry_ttl_s。
把样例 ttl = plan.expiry.entry_ttl_s if ... else policy.entry_ttl_s 改为 ttl: int = 同一个选择式。
字段名、选择条件、fallback 和经济语义完全未变，只增加普通类型注解。
该样例放入 vision.py 的内存源码，S仍14 passed，恢复14 passed，SHA256一致。
新增函数不调用 helper，调用方收集无法“发现一个从来没有调用过的调用方”。
因此负向禁令是这种新增表达的唯一保护，而它恰好未访问 AnnAssign。
这不是要求解决别名分析或混淆语义；AST提供独立标准赋值节点，条件值沿用已登记测试样例。
当前三个具名TTL解析点确实已归一，本项不撤销 S35 的具体落点结论。
本项阻断 A24 的强制机制验收；应对普通 Assign/AnnAssign 的同一值模式补足有效门及各自注入自证。

P11TTL最小只读复现命令：
`.venv-g2/bin/python -c 'exec("import pathlib,hashlib,sys,json,pytest\nfrom unittest.mock import patch\nitem=json.loads(sys.argv[1])\np=pathlib.Path(\"src/quant_lab/market\")/item[\"file\"]\nraw=p.read_bytes();source=raw.decode();original=pathlib.Path.read_text\nif item[\"old\"] is None:\n mutated=source+item[\"new\"]\nelse:\n assert source.count(item[\"old\"])==1\n mutated=source.replace(item[\"old\"],item[\"new\"])\ndef read(path,*a,**kw):\n if path.resolve()==p.resolve():\n  return mutated\n return original(path,*a,**kw)\nwith patch.object(pathlib.Path,\"read_text\",read):\n rc=pytest.main([\"tests/market/test_single_source.py\",\"-q\",\"-p\",\"no:cacheprovider\"])\nprint(\"INJECTED\",rc)\nrc=pytest.main([\"tests/market/test_single_source.py\",\"-q\",\"-p\",\"no:cacheprovider\"])\nprint(\"RESTORED\",rc,\"SHA256_EQUAL\",hashlib.sha256(raw).digest()==hashlib.sha256(p.read_bytes()).digest())\n")' '{"name":"S41-annotated-ttl","file":"vision.py","old":null,"new":"\ndef _audit_ttl(plan, policy):\n    ttl: int = plan.expiry.entry_ttl_s if plan.expiry.entry_ttl_s is not None else policy.entry_ttl_s\n    return ttl\n","tests":["tests/market/test_single_source.py"]}'`

真实输出摘要：注入14 passed in 1.16s / INJECTED 0；恢复14 passed in 1.13s / RESTORED 0 SHA256_EQUAL True。
没有要求“任何别名、任意改写都必然能识别”；本项只要求相同选择表达式的正常带注解赋值被覆盖。

### A24 的有效部分与例外证据逐项核对

ALLOWED_CALLERS 的7个键，本轮实际函数位置数分别为3、2、3、2、3、4、1。
对应 derived_t_start、derived_window_s、entry_expiry_at、first_grid_point、resolve_entry_ttl_s、grid_points_between、check_policy_hash_consistency。
P11I 实际调用集合与清单相等，diff=[]。
真实移除 vision.expected_rows 的唯一网格调用会红；新增另一真实调用方也会红。
停用 diff_call_sites 的 missing 或 extra 分支，相应机制自测会红。
此结论不等于每个函数内的每一个 Call 都被独立登记，S40 已实跑限定其能力。

| 清单条目 | 本轮实跑证据 | 判读 |
|---|---|---|
| P1 home derived_t_start | P11I contract:262 timedelta(seconds=policy.latency_s) | 正是该单一来源定义；M停用P1后回归红 |
| P3 home _us | P11I contract:236 timedelta除1µs；P10G375组含负epoch与亚秒边界全过 | 精确整数微秒尺度，不是秒截断 |
| P5 home derived_window_s | P11I contract:271 TTL+持仓/研究段 | 观察窗长度定义，区别于入场到期时间 |
| P5 home entry_expiry_at | P11I contract:284 start+TTL | 入场到期单一来源定义 |
| P7 home resolve_entry_ttl_s | P11I contract:276 计划TTL读取；M三个绕过点均红 | 作者值优先、None才政策兜底 |
| P2 empty homes | P11I全树无此命中；M int-seconds 插入与停用均红 | 未声明截秒豁免 |
| P4 empty homes | P11I全树无此命中；M start-or 插入与停用均红 | 未声明显式/省略回退豁免 |
| P6 empty homes | P11I visitor无命中，但另枚举发现 partition delta>step | 清单宣称的禁止范围不实；S40 |
| FOREIGN P5 research/api.py:build_inputs_from_synthetic | P11I真实命中line616：pl.col("t_dec")+pl.duration(seconds=pl.col("entry_ttl_s")) | 冻结外窗位置确实存在；是所有权边界，不证明这段已归一或其语义正确 |
| FOREIGN P3 research/maxt.py:calendar_blocks | P11I真实命中line104：(max(v)-min(v)).total_seconds()/86400 | G3日历块长度用途；不是G2 bar覆盖计数，冻结位置证据属实 |

FOREIGN_HITS 的精确集合测试实跑通过；把所有 research 命中从真实 lint_tree 输出删掉，该测试会红。
外窗冻结注释有所有权说明，未附独立历史内容hash或语义验收链接；本轮通过实际 AST命中补足位置证据。
不得把“2个位置存在”扩大成“2段外窗代码所有行为均正确”。
本轮未修改 research，也未据外窗残余另开P1。
五个 home 的证据来自本轮真实 AST位置与代码表达，不是无理由地许可其他定义。

### funding 例外：不同规则，未被错误归一

docs/adr/note-G2-archive-timestamp-grid.md 的公开归档记录已阅读。
它记载 BTCUSDT 2024-01：klines/markPriceKlines各44640行离网0；fundingRate93行中15行偏移1/2/3ms。
这些真实网络观测是该文的历史证据，本轮未重新下载，不冒称本轮亲跑89280行。
本轮亲跑的是合成但真实 check_funding 入口的容差边界。
0、1000、2000、3000、60000000µs偏移：rows=2/status=ok/codes=[]。
60000001µs偏移：status=quarantined/codes=['FUNDING_SCHEDULE_GAP']。
行内周期从8h变为4h且偏移3ms：check_funding 返回ok。
生产 check_funding 仍比较 abs(gap-timedelta(hours=ih[i])) > timedelta(seconds=60)。
旧浮点小时换算改成直接 timedelta，没有变为 first_grid_point(t)==t。
loader仍保留8h步进和60秒覆盖容差；这也是 S34/S02 的固定周期边界。
P11I funding没有P1–P7命中，也没有伪造一个“已经精确对齐”的例外条目。
因此确认没有把实际funding结算毫秒抖动按bar脏行规则隔离。
当前代码未在该容差注释直接链接归档说明，属于证据导航改善项，不构成本轮方向错误或新增P1。
在未来加强P6/P3时，应保留这里的差异语义及具名依据。

### 冗余非负断言：独立叠加突变的裁判

单删 assert rep.missing >= 0，两个新离网测试仍绿。
仅把 present 改成范围过滤，两个测试均红；mixed组在非负生产断言处失败。
再叠加删除非负断言，两组仍红，但当前树的实际拦截者是“缺口清单与 missing 必须一致”生产断言。
故 G2 自述“这两项叠加后由测试显式期望值抓到”不符合本轮当前源码的实际失败顺序。
本轮第三步同时删除这两条生产不变量后，两个测试明确在 test_review_p1_round10.py:25 变红。
mixed组 assert -1 == 0；all-off-grid组 assert 0 == 3。
还原后2 passed；每步SHA256一致。
因此最终分类认可：它是冗余的防御断言，不是 S31/S39 那种原缺陷注入后回归仍无法失败。
准确说法是“正确实现下断言的失败分支不可达”，不是“断言语句本身不可达”。
只删一条冗余检查不要求测试必红；必须检查被保护性质在禁用相关保护后仍由独立行为断言锁住。
这项原则不豁免契约唯一门：S31必须在删真门时红，已在本轮两组参数上证成。
本轮证据支持G0采用该区分，并建议保留真实失败归因，不能把生产不变量误记成测试断言。

### 45个生产源码突变：本轮独立结果

M 为下节内存执行器。它读取 CASES 的 old/new/选择器，不读取修复方红绿结果。
表内每例均亲跑突变和原源码基线；所有 SHA256_before/after 相等。
“红节点数”按参数化 nodeid 计，不按函数定义数计。
每例绿色运行中同一选择器的全部节点均 passed，无 skip/xfail/error。
完整执行器会输出每个 nodeid、call阶段状态、失败消息与 sha_equal。
可传表中名称单独复跑；不传参数依次重跑全部45例。

| 序号 | M选择名称 | 红节点数/rc | 绿节点数/rc | SHA256 |
|---|---|---|---|---|
| 1 | S29-first-gap | 1/1 | 1/0 | 一致 |
| 2 | S29-range-only | 2/1 | 2/0 | 一致 |
| 3 | S29-truncated-count | 1/1 | 1/0 | 一致 |
| 4 | S31-remove-output-gate | 2/1 | 2/0 | 一致 |
| 5 | S31-sanitized-copy | 2/1 | 2/0 | 一致 |
| 6 | S31-redacted-version | 2/1 | 2/0 | 一致 |
| 7 | S38-remove-domain | 1/1 | 1/0 | 一致 |
| 8 | S38-explicit-only | 1/1 | 1/0 | 一致 |
| 9 | S39-discard-return | 1/1 | 1/0 | 一致 |
| 10 | S39-inline-count | 1/1 | 1/0 | 一致 |
| 11 | S39-wrong-helper-value | 1/1 | 1/0 | 一致 |
| 12 | S33-wrong-expected | 12/1 | 12/0 | 一致 |
| 13 | modified-existing-first-point-sentinel | 1/1 | 1/0 | 一致 |
| 14 | A24-real-caller-missing | 1/1 | 1/0 | 一致 |
| 15 | A24-real-caller-extra | 1/1 | 1/0 | 一致 |
| 16 | A24-gate-missing-disabled | 1/1 | 1/0 | 一致 |
| 17 | A24-gate-extra-disabled | 1/1 | 1/0 | 一致 |
| 18 | A24-real-inline-latency | 1/1 | 1/0 | 一致 |
| 19 | A24-disable-pattern-latency | 1/1 | 1/0 | 一致 |
| 20 | A24-real-inline-int-seconds | 1/1 | 1/0 | 一致 |
| 21 | A24-disable-pattern-int-seconds | 1/1 | 1/0 | 一致 |
| 22 | A24-real-inline-duration-div | 1/1 | 1/0 | 一致 |
| 23 | A24-disable-pattern-duration-div | 1/1 | 1/0 | 一致 |
| 24 | A24-real-inline-start-or | 1/1 | 1/0 | 一致 |
| 25 | A24-disable-pattern-start-or | 1/1 | 1/0 | 一致 |
| 26 | A24-real-inline-expiry-add | 1/1 | 1/0 | 一致 |
| 27 | A24-disable-pattern-expiry-add | 1/1 | 1/0 | 一致 |
| 28 | A24-real-inline-middle-grid | 1/1 | 1/0 | 一致 |
| 29 | A24-disable-pattern-middle-grid | 1/1 | 1/0 | 一致 |
| 30 | A24-real-inline-tail-grid | 1/1 | 1/0 | 一致 |
| 31 | A24-disable-pattern-tail-grid | 1/1 | 1/0 | 一致 |
| 32 | A24-real-inline-ttl-expression | 1/1 | 1/0 | 一致 |
| 33 | A24-disable-pattern-ttl-expression | 1/1 | 1/0 | 一致 |
| 34 | A24-real-inline-ttl-branches | 1/1 | 1/0 | 一致 |
| 35 | A24-disable-pattern-ttl-branches | 1/1 | 1/0 | 一致 |
| 36 | A24-foreign-freeze | 1/1 | 1/0 | 一致 |
| 37 | S32-build-start-bypass | 2/1 | 2/0 | 一致 |
| 38 | S35-ttl-before | 1/1 | 1/0 | 一致 |
| 39 | S35-ttl-after | 1/1 | 1/0 | 一致 |
| 40 | S35-ttl-builder | 1/1 | 1/0 | 一致 |
| 41 | S35-expiry-A-timeline | 2/1 | 2/0 | 一致 |
| 42 | S35-expiry-A-entries | 2/1 | 2/0 | 一致 |
| 43 | S35-expiry-B | 2/1 | 2/0 | 一致 |
| 44 | S37-middle | 1/1 | 1/0 | 一致 |
| 45 | S37-tail | 1/1 | 1/0 | 一致 |

### 36条新增/修改测试的逐条杀伤对应

下面逐节点列出本轮真正令它失败的一项生产突变。
每个节点随后以同一选择器、原源码独立进程恢复为 passed。
参数化测试不以“其中一个失败”代替其余参数自证。
没有把单纯注入测试自身的 assert 当成生产突变证据。

| 测试节点（相对 tests/market） | 本轮杀死它的生产突变 | 突变/恢复 |
|---|---|---|
| test_review_p1_round10.py::test_s29_subsecond_calendar_start_has_no_false_first_gap | S29-first-gap | failed / passed |
| test_review_p1_round10.py::test_s29_off_grid_never_covers_calendar[mixed-off-grid] | S29-range-only | failed / passed |
| test_review_p1_round10.py::test_s29_off_grid_never_covers_calendar[all-off-grid] | S29-range-only | failed / passed |
| test_review_p1_round10.py::test_s29_subsecond_end_includes_last_grid_point | S29-truncated-count | failed / passed |
| test_outcome_kind.py::test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes[first-version] | S31-remove-output-gate | failed / passed |
| test_outcome_kind.py::test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes[later-version] | S31-remove-output-gate | failed / passed |
| test_review_p1_round10.py::test_s38_policy_rejects_negative_latency | S38-remove-domain | failed / passed |
| test_review_p1_round10.py::test_s38_resolved_start_checked_for_both_spellings | S38-explicit-only | failed / passed |
| test_review_p1_round10.py::test_s39_loader_grid_return_controls_coverage | S39-discard-return | failed / passed |
| test_review_p1_round10.py::test_s33_aligned_calendar_equivalence[2024-01-01-1-1m] | S33-wrong-expected | failed / passed |
| test_review_p1_round10.py::test_s33_aligned_calendar_equivalence[2024-01-01-1-15m] | S33-wrong-expected | failed / passed |
| test_review_p1_round10.py::test_s33_aligned_calendar_equivalence[2024-02-29-1-1m] | S33-wrong-expected | failed / passed |
| test_review_p1_round10.py::test_s33_aligned_calendar_equivalence[2024-02-29-1-15m] | S33-wrong-expected | failed / passed |
| test_review_p1_round10.py::test_s33_aligned_calendar_equivalence[2024-01-31-1m] | S33-wrong-expected | failed / passed |
| test_review_p1_round10.py::test_s33_aligned_calendar_equivalence[2024-01-31-15m] | S33-wrong-expected | failed / passed |
| test_review_p1_round10.py::test_s33_aligned_calendar_equivalence[2024-02-29-1m] | S33-wrong-expected | failed / passed |
| test_review_p1_round10.py::test_s33_aligned_calendar_equivalence[2024-02-29-15m] | S33-wrong-expected | failed / passed |
| test_review_p1_round10.py::test_s33_aligned_calendar_equivalence[2023-02-28-1m] | S33-wrong-expected | failed / passed |
| test_review_p1_round10.py::test_s33_aligned_calendar_equivalence[2023-02-28-15m] | S33-wrong-expected | failed / passed |
| test_review_p1_round10.py::test_s33_aligned_calendar_equivalence[2024-12-31-1m] | S33-wrong-expected | failed / passed |
| test_review_p1_round10.py::test_s33_aligned_calendar_equivalence[2024-12-31-15m] | S33-wrong-expected | failed / passed |
| test_constants_effective.py::test_grid_math_single_source_and_exact_to_microsecond | modified-existing-first-point-sentinel | failed / passed |
| test_single_source.py::test_single_source_call_sites_match_registry | A24-real-caller-missing | failed / passed |
| test_single_source.py::test_registry_gate_fails_when_mutated[missing] | A24-gate-missing-disabled | failed / passed |
| test_single_source.py::test_registry_gate_fails_when_mutated[extra] | A24-gate-extra-disabled | failed / passed |
| test_single_source.py::test_no_inline_reexpression_of_single_sources_anywhere_in_src | A24-real-inline-latency | failed / passed |
| test_single_source.py::test_lint_gate_fails_on_injected_violation[latency] | A24-disable-pattern-latency | failed / passed |
| test_single_source.py::test_lint_gate_fails_on_injected_violation[int-seconds] | A24-disable-pattern-int-seconds | failed / passed |
| test_single_source.py::test_lint_gate_fails_on_injected_violation[duration-div] | A24-disable-pattern-duration-div | failed / passed |
| test_single_source.py::test_lint_gate_fails_on_injected_violation[start-or] | A24-disable-pattern-start-or | failed / passed |
| test_single_source.py::test_lint_gate_fails_on_injected_violation[expiry-add] | A24-disable-pattern-expiry-add | failed / passed |
| test_single_source.py::test_lint_gate_fails_on_injected_violation[middle-grid] | A24-disable-pattern-middle-grid | failed / passed |
| test_single_source.py::test_lint_gate_fails_on_injected_violation[tail-grid] | A24-disable-pattern-tail-grid | failed / passed |
| test_single_source.py::test_lint_gate_fails_on_injected_violation[ttl-expression] | A24-disable-pattern-ttl-expression | failed / passed |
| test_single_source.py::test_lint_gate_fails_on_injected_violation[ttl-branches] | A24-disable-pattern-ttl-branches | failed / passed |
| test_single_source.py::test_foreign_hits_registry_is_exact | A24-foreign-freeze | failed / passed |

### M：可复跑内存生产突变执行器

此代码块是本轮实际执行器，增加了可选名称筛选；不写 src/tests。
执行方式：读取本报告具名块并在内存 exec。
默认逐例运行45个定义；参数可选择任一表内名称。
SourceFileLoader 与 Path.read_text 同时覆盖，同一突变对产品导入和树级AST门均可见。
运行时失败栈的磁盘源码行展示可能与内存行号错位；判读使用实际异常消息、call阶段状态和测试断言。
附加 S40/S41 的最小命令不依赖该工具或修复方定义，已在上文直接给出。

<!-- P11-MUTATION-RUNNER -->
```python
import runpy,subprocess,sys,json,os
cases=runpy.run_path("tests/market/mutation_round10.py")["CASES"]
child="\nimport os,sys,pathlib,hashlib,json,importlib.machinery\nfrom unittest.mock import patch\nos.environ[\"PYTHONDONTWRITEBYTECODE\"]=\"1\"\nsys.dont_write_bytecode=True\nroot=pathlib.Path.cwd()\npaths=list((root/\"src/quant_lab/market\").rglob(\"*\"))+list((root/\"tests/market\").rglob(\"*\"))\nbefore={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}\nrd=pathlib.Path.read_text\nwr=pathlib.Path.write_text\ngc=importlib.machinery.SourceFileLoader.get_code\noverlay={}\ndef read(p,*a,**kw):\n    key=str(p.resolve())\n    if key in overlay:\n        return overlay[key]\n    return rd(p,*a,**kw)\ndef write(p,s,*a,**kw):\n    key=str(p.resolve())\n    if key.endswith(\"policy-hashes.json\") or \"policy\" in p.name and str(root/\"src\") in key:\n        overlay[key]=s\n        return len(s)\n    if str(root/\"src\") in key or str(root/\"tests\") in key:\n        raise RuntimeError(\"review prohibits write \"+key)\n    return wr(p,s,*a,**kw)\ndef code(loader,name):\n    key=str(pathlib.Path(loader.path).resolve())\n    if key in overlay:\n        return compile(overlay[key],loader.path,\"exec\")\n    return gc(loader,name)\n\nitem=json.loads(sys.argv[1])\nif item:\n    target=root/\"src/quant_lab/market\"/item[\"file\"]\n    source=rd(target)\n    if item[\"old\"] is None:\n        changed=source+item[\"new\"]\n    else:\n        assert source.count(item[\"old\"])==1,(item[\"name\"],source.count(item[\"old\"]))\n        changed=source.replace(item[\"old\"],item[\"new\"])\n    overlay[str(target.resolve())]=changed\nimport pytest\nclass Results:\n    def __init__(self):\n        self.rows=[]\n    def pytest_runtest_logreport(self,report):\n        if report.when==\"call\" or report.failed:\n            self.rows.append([report.nodeid,report.when,report.outcome,str(report.longrepr)[-1700:] if report.failed else \"\"])\nplugin=Results()\nwith patch.object(pathlib.Path,\"read_text\",read),patch.object(pathlib.Path,\"write_text\",write),patch.object(importlib.machinery.SourceFileLoader,\"get_code\",code):\n    rc=pytest.main(json.loads(sys.argv[2])+[\"-q\",\"-p\",\"no:cacheprovider\"],plugins=[plugin])\nafter={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}\nprint(\"AUDIT_RESULT=\"+json.dumps(dict(rc=rc,rows=plugin.rows,sha_equal=before==after),ensure_ascii=False))\n"
for item in cases:
 if len(sys.argv)>1 and item["name"] not in sys.argv[1:]:
  continue
 outputs=[]
 for mutation in (item,{}):
  p=subprocess.run([sys.executable,"-c",child,json.dumps(mutation),json.dumps(item["tests"])],capture_output=True,text=True,env=dict(os.environ,PYTHONDONTWRITEBYTECODE="1",PYTHONPATH="src"))
  marker="AUDIT_RESULT="
  if marker not in p.stdout:
   print(json.dumps(dict(name=item["name"],error=p.stdout+p.stderr)),flush=True)
   raise SystemExit(2)
  outputs.append(json.loads(p.stdout.split(marker)[-1]))
 print(json.dumps(dict(name=item["name"],tests=item["tests"],red=outputs[0],green=outputs[1]),ensure_ascii=False),flush=True)

```

M全部复跑命令：`.venv-g2/bin/python -c 'import pathlib; f=chr(96)*3; exec(pathlib.Path("docs/adr/review-G2-P1.md").read_text().split("<!-- P11-MUTATION-RUNNER -->",1)[1].split(f+"python\n",1)[1].split("\n"+f,1)[0])'`
M单项示例命令：`.venv-g2/bin/python -c 'import pathlib; f=chr(96)*3; exec(pathlib.Path("docs/adr/review-G2-P1.md").read_text().split("<!-- P11-MUTATION-RUNNER -->",1)[1].split(f+"python\n",1)[1].split("\n"+f,1)[0])' S31-sanitized-copy`

### P11I：树、调用点、例外的实跑命令与输出

`.venv-g2/bin/python -c 'exec("import ast,pathlib,json\nfrom quant_lab.market import single_source as g\nactual=g.collect_call_sites(g.SRC,set(g.ALLOWED_CALLERS))\nprint(\"registry\",json.dumps({k:sorted(v) for k,v in actual.items()},sort_keys=True))\nprint(\"diff\",g.diff_call_sites(g.ALLOWED_CALLERS,actual))\nhits=g.lint_tree(g.SRC)\nprint(\"HITS\",json.dumps(hits,ensure_ascii=False))\nprint(\"violations\",g.violations(hits))\nfor snippet in [\"def f():\\n    delta = o - prev\\n    return delta > step\\n\",\"def f():\\n    ttl: int = plan.expiry.entry_ttl_s if plan.expiry.entry_ttl_s is not None else policy.entry_ttl_s\\n\"]:\n lint=g._Lint(\"market/probe.py\",snippet);lint.visit(ast.parse(snippet));print(\"INJECT\",repr(snippet),\"HITS\",lint.hits)\np=pathlib.Path(\"src/quant_lab/market/partition_check.py\")\nfor i,l in enumerate(p.read_text().splitlines(),1):\n if \"delta =\" in l or \"delta > step\" in l:\n  print(\"ACTUAL\",i,l.strip())\n")'`

```text
registry {"check_policy_hash_consistency": ["market/execution.py:simulate_batch"], "derived_t_start": ["market/contract.py:ExecutionRequest._chk", "market/contract.py:ExecutionRequest.resolved_t_start", "market/contract.py:build_request"], "derived_window_s": ["market/contract.py:ExecutionRequest._chk", "market/contract.py:build_request"], "entry_expiry_at": ["market/kernel_a.py:KernelA.submit_entries", "market/kernel_a.py:KernelA.timeline", "market/nautilus_adapter.py:_simulate_b.PlanShell._submit_entries"], "first_grid_point": ["market/kernel_a.py:KernelA._first_bar_gap", "market/partition_check.py:check_bars"], "grid_points_between": ["market/execution.py:load_market_from_lake.bars", "market/kernel_a.py:KernelA._first_bar_gap", "market/partition_check.py:check_bars", "market/vision.py:expected_rows"], "resolve_entry_ttl_s": ["market/contract.py:ExecutionRequest._chk", "market/contract.py:ExecutionRequest._resolve_ttl", "market/contract.py:build_request"]}
diff []
HITS [["P3_duration_div", 236, "market/contract.py:_us", "(t - EPOCH) // dt.timedelta(microseconds=1)"], ["P1_inline_latency", 262, "market/contract.py:derived_t_start", "dt.timedelta(seconds=policy.latency_s)"], ["P5_inline_entry_ttl", 271, "market/contract.py:derived_window_s", "entry_ttl_s + (hold if hold is not None else policy.research_horizon_s)"], ["P7_inline_ttl_resolution", 276, "market/contract.py:resolve_entry_ttl_s", "ttl = plan.expiry.entry_ttl_s"], ["P5_inline_entry_ttl", 284, "market/contract.py:entry_expiry_at", "t_start + dt.timedelta(seconds=entry_ttl_s)"], ["P5_inline_entry_ttl", 616, "research/api.py:build_inputs_from_synthetic", "pl.col(\"t_dec\") + pl.duration(seconds=pl.col(\"entry_ttl_s\"))"], ["P3_duration_div", 104, "research/maxt.py:calendar_blocks", "(max(v) - min(v)).total_seconds() / 86400"]]
violations []
INJECT 'def f():\n    delta = o - prev\n    return delta > step\n' HITS []
INJECT 'def f():\n    ttl: int = plan.expiry.entry_ttl_s if plan.expiry.entry_ttl_s is not None else policy.entry_ttl_s\n' HITS []
ACTUAL 258 delta = (df[key] - prev)
ACTUAL 259 gap_before = (delta > step).fill_null(False)
```

### P11B：零/正延迟、构造委派、funding边界实跑

`.venv-g2/bin/python -c 'exec("import datetime as dt,polars as pl\nfrom unittest.mock import patch\nfrom quant_lab.market import contract as c,partition_check as pc\nfrom tests.market.test_partition_check import JAN,INST\nfrom tests.market.test_review_p1 import e03\nq,m=e03()\nfor latency in (0,60):\n p=c.ExecutionPolicy.model_validate({**c.resolve_policy(q.policy_version).model_dump(),\"version\":\"audit-positive\",\"latency_s\":latency})\n with patch.dict(c.POLICIES,{p.version:p}),patch.object(c,\"load_policy_registry\",return_value={p.version:p.content_hash}):\n  for us in (-1,0,1):\n   outcomes=[]\n   for explicit in (None,q.t_dec+dt.timedelta(seconds=latency)):\n    try:\n     r=c.ExecutionRequest.model_validate({**q.model_dump(),\"policy_version\":p.version,\"policy_hash\":p.content_hash,\"t_start\":explicit,\"horizon_end\":q.t_dec+dt.timedelta(seconds=latency,microseconds=us),\"horizon_source\":\"caller\"})\n     outcomes.append(\"ACCEPT\")\n    except c.ContractError: outcomes.append(\"REJECT\")\n   print(\"latency\",latency,\"offset_us\",us,outcomes)\n   assert outcomes==([\"ACCEPT\"]*2 if us>0 else [\"REJECT\"]*2)\nwith patch.object(c,\"derived_t_start\",side_effect=lambda t,p:t+dt.timedelta(seconds=7)):\n r=c.build_request(q.model_dump(),policy_version=q.policy_version,policy_hash=q.policy_hash,risk_budget=q.risk_budget,market_manifest=q.market_manifest)\n print(\"build+7\",\"ACCEPT\",r.horizon_end,r.resolved_t_start(c.resolve_policy(r.policy_version)))\nfor micros in (0,1000,2000,3000,60000000,60000001):\n df=pl.DataFrame({\"calc_time\":[JAN,JAN+dt.timedelta(hours=8,microseconds=micros)],\"funding_interval_hours\":[8,8],\"funding_rate\":[0.,0.]})\n out,qs,rep=pc.check_funding(df,pid=\"p\",inst=INST,period=\"2024-01\")\n print(\"funding jitter_us\",micros,\"rows\",out.height,\"status\",rep.status,\"codes\",[x[\"reason_code\"] for x in qs])\n assert bool(qs)==(micros>60000000)\ndf=pl.DataFrame({\"calc_time\":[JAN,JAN+dt.timedelta(hours=4,microseconds=3000)],\"funding_interval_hours\":[8,4],\"funding_rate\":[0.,0.]})\nprint(\"funding-variable\",pc.check_funding(df,pid=\"p\",inst=INST,period=\"2024-01\")[2].status)\nprint(\"P2_UNSUPPORTED\",c.P2_UNSUPPORTED)\n")'`

```text
latency 0 offset_us -1 ['REJECT', 'REJECT']
latency 0 offset_us 0 ['REJECT', 'REJECT']
latency 0 offset_us 1 ['ACCEPT', 'ACCEPT']
latency 60 offset_us -1 ['REJECT', 'REJECT']
latency 60 offset_us 0 ['REJECT', 'REJECT']
latency 60 offset_us 1 ['ACCEPT', 'ACCEPT']
build+7 ACCEPT 2024-01-06 02:00:07+00:00 2024-01-01 01:00:07+00:00
funding jitter_us 0 rows 2 status ok codes []
funding jitter_us 1000 rows 2 status ok codes []
funding jitter_us 2000 rows 2 status ok codes []
funding jitter_us 3000 rows 2 status ok codes []
funding jitter_us 60000000 rows 2 status ok codes []
funding jitter_us 60000001 rows 2 status quarantined codes ['FUNDING_SCHEDULE_GAP']
funding-variable ok
P2_UNSUPPORTED {'S02': {'status': 'unsupported', 'capability': 'real_settlement_time_U03_and_engine_balance', 'owner': 'G0 settlement contract + G2 P2'}, 'S06': {'status': 'unsupported', 'capability': 'bronze_depth_replay_and_multi_source_versions', 'owner': 'G1 lake + G2 P2'}, 'S07': {'status': 'unsupported', 'capability': 'management_commands_E13_C01_and_reservation_lifecycle', 'owner': 'G0 C01 + G2 P2'}, 'S12': {'status': 'unsupported', 'capability': 'versioned_lake_snapshot_resolution', 'owner': 'G1 lake + G0 identity contract'}}
```

### 四个缺陷族的本轮覆盖与剩余范围

族A：对照7个调用登记键、7个禁令族和两类例外，实际调用函数集合等于登记。
已归一的S32/S33/S35/S37没有在原落点残留十审原表达。
清单对应的全树“表达”范围并不完整：partition中段拆行公式既存在又未登记豁免，S40为直接反例。
P7已覆盖原分支赋值与三元选择式，但忽略同一值的AnnAssign，S41为直接反例。
本轮不接受把这两项缩写成“任意混淆表达无法证明”的笼统免责。

族B：P10G375组独立枚举保留微秒，P10I移除vision整秒整除后12种日/月回归变异可红。
P10S全market AST枚举的当前时间表达完整输出附后。
funding直接timedelta比较已用60秒两侧边界验证，不把funding时间抖动当bar精确网格。
本轮没有证成新的当前秒截断数值错误。

族C：P10F枚举可缺省/可空字段的 omit/None/False/0/空串/空列表；没有把合法显式字段的拒绝算作默默改写。
P10N恢复旧观察窗下界及请求启动自写加法均使既有测试红，恢复绿。
P11B对零/正latency的显式与省略逐值一致；负latency在政策层拒绝。
TTL解析仍使用None分支；原三处统一到helper，S35各绕过点由M独立证伪。
本轮没有证明新的falsy-or请求分叉。

族D：45例对36个节点的call阶段失败、原源码恢复成功已逐条登记。
A24原9个表达模式并非“从不命中”：分别注入及分别停用真实实现均能使测试失败。
但样例红绿不能替代对已知语法族的完整覆盖，S40/S41注入时两道门都绿。
S29非负断言按叠加突变归为冗余防御，不误报为无效检查。

### 历史 partial-P2 支持声明的核对

P2_UNSUPPORTED 实际只有S02/S06/S07/S12四个键，不是十条历史状态的机器编码全集。
AB和P11B均亲自打印这个字典，内容一致，没有隐去这四项unsupported。
S02为真实settlement/U03与引擎余额：checker支持行内周期不等于loader能处理任意真实结算时间。
S34固定8h覆盖推断保留，因此S02/S34不升级也不宣称已闭合。
S06为bronze深度重放与多来源版本：本轮合成湖/active silver路径不构成这些能力的验证。
S07为管理命令E13/C01及reservation生命周期：22个episode未新增该全套能力，仍按既有unsupported。
S12为版本化湖快照解析：实际字典仍明确unsupported，不用policy哈希测试代替历史湖版本能力。
S04/S36/S16的持仓截止与B暴露定型债务由原状态和实际A/B分类承载，没有凭四键字典声称其全闭合。
S10未知差异不能洗白、S11报告/场景矩阵、S13规范事件精度相关既有回归均包含在T的293通过内。
这些项目沿用历史partial-P2，不把T全绿当作P2真实数据/完整定型验收。
本轮未做外窗研究实现的语义验收，不宣称FOREIGN_HITS内代码已通过G3终审。

### GOAL-2 §7 P1 DoD判读与未做事项

M-03..M-09：本轮用户指定的本地T/S/A/AB均实际通过；M-03历史真实网络证据沿用G0既有裁定。
本轮未重新执行真实下载及看板verify，原因是本轮只读范围及指定验证边界；不另构成新增否决。
候选A ≥10最小episode与不变量：本轮22/0且22个replay=True，达到该本地数量与重放要求。
A/B报告：实际报告摘要含22例分类，六类计数逐项等于用户给出的基线；B定型仍在P2。
Codex P1 review必修闭合：未满足。S40/S41证明A24强制机制仍有具体可复现的漏检。
因此本轮不能判pass；无需把历史P2项重新升级才能得出fail。

“AST门只覆盖已知语法族，不能证明任意别名或混淆表达的语义等价”作为一般边界是诚实的。
单纯存在这个理论边界不阻断P1，本轮也不要求完备的语义等价证明。
但当前漏检包括树里已有的普通拆行，以及原选择式加一个类型注解；二者有直接实跑反例。
把这些具体已知输入遗漏藏在一般边界描述后面不足以通过A24。
剩余风险还包括：函数级集合无法计数同函数内各个Call、外窗冻结只有位置证据、测试仍有真实登记表写入习惯。
本轮对登记表采用内存保护；未修改这一测试行为，未据此另开阻断项。
本轮没有声明对实时交易、真实账户、全品种资金费或生产服务做过验证。

### 十审9条原命令的真实输出附录

历史正文中的原命令逐字保留，可从相应P10标题下直接取出。
本附录为本轮shlex.split + subprocess.run的实际输出，不是十审旧输出。
stderr非空时单列；空stdout明确记为“空输出”。

#### P10M：exit 1

stdout：

```text
空输出
```

stderr：

```text
Traceback (most recent call last):
  File "<string>", line 1, in <module>
  File "<string>", line 17, in <module>
AttributeError: module 'tests.market.test_outcome_kind' has no attribute 'test_b16_pair_key_carries_policy_hash_and_batch_rejects_split_brain'
```

#### P10N：exit 0

stdout：

```text
offgrid (0, 1, 60000000, 120000000) expected 3 missing 0 gaps [] flags [False, False, False] status quarantined
offgrid (1, 60000001, 120000001) expected 3 missing 3 gaps [{'from': '2024-01-01T00:00:00+00:00', 'to': '2024-01-01T00:03:00+00:00', 'n': 3}] flags [] status quarantined
B17 sanitized dynamic RED TypeError test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes() missing 2 required positional arguments: 'monkeypatch' and 'later_versi
S30 old lower bound RED Failed DID NOT RAISE ContractError
S30 restored GREEN
start delegate bypass RED Failed DID NOT RAISE ContractError
start restored GREEN
```

#### P10C：exit 1

stdout：

```text
空输出
```

stderr：

```text
Traceback (most recent call last):
  File "<string>", line 1, in <module>
  File "<string>", line 7, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/.venv-g2/lib/python3.12/site-packages/pydantic/main.py", line 732, in model_validate
    return cls.__pydantic_validator__.validate_python(
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py", line 329, in _latency_domain
    raise ContractError("latency_s 必须非负：启动时刻不能早于决策时刻")
quant_lab.market.contract.ContractError: latency_s 必须非负：启动时刻不能早于决策时刻
```

#### P10D：exit 0

stdout：

```text
loader discard return GREEN
loader restored GREEN
sanitized gate regression RED TypeError test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes() missing 2 required positional a
sanitized gate actual 2 2 NO ContractError
gate restored RED TypeError test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes() missing 2 required positional a
```

#### P10I：exit 0

stdout：

```text
S32 contract.py:262 return t_dec + dt.timedelta(seconds=policy.latency_s)
S34 execution.py:236 g = a.replace(minute=0, second=0, microsecond=0)
S34 execution.py:237 g = g.replace(hour=(g.hour // 8) * 8)
S34 execution.py:239 while g <= b:
S34 execution.py:240 if g >= a and not any(abs((h - g).total_seconds()) <= 60 for h in have):
S34 execution.py:243 g += dt.timedelta(hours=8)
S35 contract.py:276 ttl = plan.expiry.entry_ttl_s
S35 contract.py:479 exp_ttl = resolve_entry_ttl_s(self.order_plan, pol)
S36A kernel_a.py:424 self.hold_end = ts + dt.timedelta(seconds=self.plan.expiry.max_holding_s)
S36B nautilus_adapter.py:530 hold_end = min(opens) + dt.timedelta(seconds=plan.expiry.max_holding_s)
S36B nautilus_adapter.py:556 cutoff = min(cutoff, st["close_at"])
S36B nautilus_adapter.py:558 cutoff = min(cutoff, st["censor_at"] - dt.timedelta(microseconds=1))
S36B nautilus_adapter.py:560 cutoff = min(cutoff, st["open_at"] + dt.timedelta(seconds=plan.expiry.max_holding_s) - dt.timedelta(microseconds=1))
S37 kernel_a.py:243 first_expected = first_grid_point(self.t_start, bars[0].interval_s)
TTL-deadline-A kernel_a.py:222 deadline = entry_expiry_at(self.t_start, self.req.entry_ttl_s)
TTL-deadline-A kernel_a.py:319 deadline = entry_expiry_at(self.t_start, self.req.entry_ttl_s)
TTL-deadline-B nautilus_adapter.py:226 deadline = entry_expiry_at(t_start, req.entry_ttl_s)
```

#### P10L：exit 0

stdout：

```text
test_lint_forbidden_patterns_do_not_reappear forbidden injected RED AssertionError
test_lint_forbidden_patterns_do_not_reappear restored GREEN
test_s27_s28_single_source_start_time_and_exact_grid forbidden injected RED AssertionError
test_s27_s28_single_source_start_time_and_exact_grid restored GREEN
B17 missing version diagnostic RED TypeError
B17 restored RED TypeError
```

#### P10G：exit 0

stdout：

```text
helper brute 375 PASS
partition aligned subsets 25 PASS
```

#### P10F：exit 0

stdout：

```text
ExecutionRequest.cost_scenario | omit='base'
ExecutionRequest.path_scenario | omit='primary'
ExecutionRequest.execution_contract_version | omit='g2-exec-v0'
ExecutionRequest.seed | omit=0; False=0; 0=0
ExecutionRequest.t_start | omit=None; None=None
ExecutionRequest.position_mode | omit='one_way'
ExecutionRequest.entry_ttl_s | omit=3600; None=3600
ExecutionRequest.entry_fractions | omit=(Decimal('1'),); None=(Decimal('1'),)
ExecutionRequest.tp_fractions | omit=(Decimal('1'),); None=(Decimal('1'),)
ExecutionRequest.horizon_source | all reject
OrderPlan.tps | omit=[]; emptylist=[]
OrderPlan.reduce_only_exit | omit=True
ExecutionPolicy.latency_s | omit=0; False=0; 0=0
ExecutionPolicy.ladder_steps | omit=2; False=0; 0=0
ExecutionPolicy.participation | omit=None; None=None; 0=Decimal('0')
ExecutionPolicy.wallet | omit=Decimal('1000'); 0=Decimal('0')
ExecutionPolicy.leverage | omit=Decimal('1'); 0=Decimal('0')
ExecutionPolicy.mark_max_staleness_s | omit=120; False=0; 0=0
ExecutionPolicy.settlement_quantum | omit=Decimal('1E-8'); 0=Decimal('0')
ExecutionPolicy.max_horizon_s | omit=1209600; False=0; 0=0
ExecutionPolicy.entry_ttl_s | omit=86400; False=0; 0=0
ExecutionPolicy.entry_fraction_rule | omit='equal'
ExecutionPolicy.tp_fraction_rule | omit='equal'
ExecutionPolicy.tp_total_fraction | omit=Decimal('1'); 0=Decimal('0')
Entry.fraction | omit=None; None=None
Entry.tif | omit='GTC'
Entry.post_only | omit=False; False=False; 0=False
Stop.trigger | omit='mark'
TakeProfit.fraction | omit=None; None=None; 0=Decimal('0')
Sizing.qty | omit=None; None=None; 0=Decimal('0')
Expiry.entry_ttl_s | omit=None; None=None
Expiry.max_holding_s | omit=None; None=None
CostSpec.maker_fee | omit=Decimal('0'); 0=Decimal('0')
CostSpec.taker_fee | omit=Decimal('0'); 0=Decimal('0')
CostSpec.slippage_ticks | omit=0; False=0; 0=0
CostSpec.slippage_bps | omit=Decimal('0'); 0=Decimal('0')
```

#### P10S：exit 0

stdout：

```text
src/quant_lab/market/asof.py:180 stale > max_staleness_s
src/quant_lab/market/contract.py:262 t_dec + dt.timedelta(seconds=policy.latency_s)
src/quant_lab/market/contract.py:271 entry_ttl_s + (hold if hold is not None else policy.research_horizon_s)
src/quant_lab/market/contract.py:277 ttl is not None
src/quant_lab/market/contract.py:284 t_start + dt.timedelta(seconds=entry_ttl_s)
src/quant_lab/market/contract.py:427 data.get('entry_ttl_s') is None and isinstance(plan, OrderPlan) and (pol is not None)
src/quant_lab/market/contract.py:427 data.get('entry_ttl_s') is None
src/quant_lab/market/contract.py:446 self.order_plan.expiry.entry_ttl_s is not None
src/quant_lab/market/contract.py:462 exp_t_start < self.t_dec
src/quant_lab/market/contract.py:464 self.horizon_end <= exp_t_start
src/quant_lab/market/contract.py:469 self.entry_ttl_s is None
src/quant_lab/market/contract.py:476 self.t_start is not None and self.t_start != exp_t_start
src/quant_lab/market/contract.py:476 self.t_start is not None
src/quant_lab/market/contract.py:476 self.t_start != exp_t_start
src/quant_lab/market/contract.py:480 self.entry_ttl_s != exp_ttl
src/quant_lab/market/contract.py:481 self.entry_ttl_source == 'plan'
src/quant_lab/market/contract.py:486 self.horizon_end - exp_t_start
src/quant_lab/market/contract.py:487 window > dt.timedelta(seconds=pol.max_horizon_s)
src/quant_lab/market/contract.py:491 self.horizon_source == 'policy' and window != dt.timedelta(seconds=derived_s)
src/quant_lab/market/contract.py:491 self.horizon_source == 'policy'
src/quant_lab/market/contract.py:515 self.t_start is not None
src/quant_lab/market/contract.py:536 horizon_end is not None
src/quant_lab/market/contract.py:537 horizon_end is None
src/quant_lab/market/contract.py:538 derived_t_start(t_dec, policy) + dt.timedelta(seconds=derived_window_s(plan, policy, ttl))
src/quant_lab/market/contract.py:778 e.ts in settle_keys
src/quant_lab/market/execution.py:153 req.resolved_t_start(_rp(req.policy_version)) - dt.timedelta(seconds=window_before_s)
src/quant_lab/market/execution.py:240 g >= a and (not any((abs((h - g).total_seconds()) <= 60 for h in have)))
src/quant_lab/market/execution.py:240 abs((h - g).total_seconds()) <= 60
src/quant_lab/market/kernel_a.py:202 b.open_time >= self.t_start
src/quant_lab/market/kernel_a.py:208 [p for p in m.last if self.t_start <= p.ts <= end] + [p for p in self._expanded(m.bars_last, self.policy.participation) if p.ts <= end]
src/quant_lab/market/kernel_a.py:208 self.t_start <= p.ts <= end
src/quant_lab/market/kernel_a.py:209 [p for p in m.mark if self.t_start <= p.ts <= end] + [p for p in self._expanded(m.bars_mark, None) if p.ts <= end]
src/quant_lab/market/kernel_a.py:209 self.t_start <= p.ts <= end
src/quant_lab/market/kernel_a.py:220 self.t_start <= f.calc_time <= end
src/quant_lab/market/kernel_a.py:223 deadline <= end
src/quant_lab/market/kernel_a.py:240 b.open_time >= self.t_start and b.open_time < end
src/quant_lab/market/kernel_a.py:240 b.open_time >= self.t_start
src/quant_lab/market/kernel_a.py:250 grid_points_between(prev + iv, o, interval_s) > 0
src/quant_lab/market/kernel_a.py:253 grid_points_between(prev + iv, end, interval_s) > 0
src/quant_lab/market/kernel_a.py:259 self.hold_end is not None and self.hold_end <= self.req.horizon_end and (self.hold_end not in self.moments)
src/quant_lab/market/kernel_a.py:259 self.hold_end is not None
src/quant_lab/market/kernel_a.py:259 self.hold_end <= self.req.horizon_end
src/quant_lab/market/kernel_a.py:259 self.hold_end not in self.moments
src/quant_lab/market/kernel_a.py:262 self.hold_end is not None and self.hold_end in self.moments
src/quant_lab/market/kernel_a.py:262 self.hold_end is not None
src/quant_lab/market/kernel_a.py:262 self.hold_end in self.moments
src/quant_lab/market/kernel_a.py:423 self.plan.expiry.max_holding_s is not None
src/quant_lab/market/kernel_a.py:424 ts + dt.timedelta(seconds=self.plan.expiry.max_holding_s)
src/quant_lab/market/kernel_a.py:569 ts in self.settled_keys
src/quant_lab/market/kernel_a.py:570 self.settled_keys[ts] != row.rate
src/quant_lab/market/kernel_a.py:574 mp is None or (ts - mp.ts).total_seconds() > self.policy.mark_max_staleness_s
src/quant_lab/market/kernel_a.py:574 (ts - mp.ts).total_seconds() > self.policy.mark_max_staleness_s
src/quant_lab/market/kernel_a.py:592 r.effective_from and self.t_start < r.effective_from or (r.effective_to and self.t_start >= r.effective_to)
src/quant_lab/market/kernel_a.py:592 r.effective_from and self.t_start < r.effective_from
src/quant_lab/market/kernel_a.py:592 r.effective_to and self.t_start >= r.effective_to
src/quant_lab/market/kernel_a.py:592 self.t_start < r.effective_from
src/quant_lab/market/kernel_a.py:592 self.t_start >= r.effective_to
src/quant_lab/market/kernel_a.py:596 not self.market.bars_complete and (self._first_bar_gap(self.market.bars_last, self.req.horizon_end) is None and self._first_bar_gap(self.market.bars_mark, self.req.horizon_end) is None)
src/quant_lab/market/kernel_a.py:596 self._first_bar_gap(self.market.bars_last, self.req.horizon_end) is None and self._first_bar_gap(self.market.bars_mark, self.req.horizon_end) is None
src/quant_lab/market/kernel_a.py:596 self._first_bar_gap(self.market.bars_last, self.req.horizon_end) is None
src/quant_lab/market/kernel_a.py:597 self._first_bar_gap(self.market.bars_mark, self.req.horizon_end) is None
src/quant_lab/market/kernel_a.py:617 (mo.hold_end or (self.hold_end is not None and ts >= self.hold_end)) and self.pos != 0
src/quant_lab/market/kernel_a.py:617 mo.hold_end or (self.hold_end is not None and ts >= self.hold_end)
src/quant_lab/market/kernel_a.py:617 self.hold_end is not None and ts >= self.hold_end
src/quant_lab/market/kernel_a.py:617 self.hold_end is not None
src/quant_lab/market/kernel_a.py:617 ts >= self.hold_end
src/quant_lab/market/kernel_a.py:620 mo.horizon and self.pos != 0
src/quant_lab/market/kernel_a.py:623 ts == self.t_start and (not self.orders)
src/quant_lab/market/kernel_a.py:623 ts == self.t_start
src/quant_lab/market/kernel_a.py:635 o.deadline is not None and o.deadline <= ts
src/quant_lab/market/kernel_a.py:635 o.deadline is not None
src/quant_lab/market/kernel_a.py:635 o.deadline <= ts
src/quant_lab/market/kernel_a.py:656 self.mark_ts is None or (ts - self.mark_ts).total_seconds() > self.policy.mark_max_staleness_s
src/quant_lab/market/kernel_a.py:656 (ts - self.mark_ts).total_seconds() > self.policy.mark_max_staleness_s
src/quant_lab/market/kernel_a.py:673 self.hold_end is not None and self.pos != 0
src/quant_lab/market/kernel_a.py:673 self.hold_end is not None
src/quant_lab/market/nautilus_adapter.py:146 list(market.last) + [p for b in market.bars_last if b.open_time >= t_start for p in expand_bar_b(b, req.path_scenario, plan.side)]
src/quant_lab/market/nautilus_adapter.py:146 b.open_time >= t_start
src/quant_lab/market/nautilus_adapter.py:148 list(market.mark) + [p for b in market.bars_mark if b.open_time >= t_start for p in expand_bar_b(b, req.path_scenario, plan.side)]
src/quant_lab/market/nautilus_adapter.py:149 b.open_time >= t_start
src/quant_lab/market/nautilus_adapter.py:150 b.open_time < t_start
src/quant_lab/market/nautilus_adapter.py:150 p.path_step == 'C' and p.ts <= t_start
src/quant_lab/market/nautilus_adapter.py:150 p.ts <= t_start
src/quant_lab/market/nautilus_adapter.py:153 market.funding and len({f.calc_time for f in market.funding}) != len({(f.calc_time, f.rate) for f in market.funding})
src/quant_lab/market/nautilus_adapter.py:153 len({f.calc_time for f in market.funding}) != len({(f.calc_time, f.rate) for f in market.funding})
src/quant_lab/market/nautilus_adapter.py:156 t_start <= p.ts <= end
src/quant_lab/market/nautilus_adapter.py:237 deadline <= end
src/quant_lab/market/nautilus_adapter.py:419 self.fi < len(self.funding_rows) and self.funding_rows[self.fi].calc_time <= ts
src/quant_lab/market/nautilus_adapter.py:419 self.funding_rows[self.fi].calc_time <= ts
src/quant_lab/market/nautilus_adapter.py:422 row.calc_time < t_start or row.calc_time in self.settled_at
src/quant_lab/market/nautilus_adapter.py:422 row.calc_time < t_start
src/quant_lab/market/nautilus_adapter.py:422 row.calc_time in self.settled_at
src/quant_lab/market/nautilus_adapter.py:425 m.ts <= row.calc_time and m.path_step in ('none', 'C')
src/quant_lab/market/nautilus_adapter.py:425 m.ts <= row.calc_time
src/quant_lab/market/nautilus_adapter.py:426 not mk or (row.calc_time - mk[-1].ts).total_seconds() > policy.mark_max_staleness_s
src/quant_lab/market/nautilus_adapter.py:426 (row.calc_time - mk[-1].ts).total_seconds() > policy.mark_max_staleness_s
src/quant_lab/market/nautilus_adapter.py:426 row.calc_time - mk[-1].ts
src/quant_lab/market/nautilus_adapter.py:442 st['pos'] != 0 and (st['mark_ts'] is None or (ts - st['mark_ts']).total_seconds() > policy.mark_max_staleness_s)
src/quant_lab/market/nautilus_adapter.py:442 st['mark_ts'] is None or (ts - st['mark_ts']).total_seconds() > policy.mark_max_staleness_s
src/quant_lab/market/nautilus_adapter.py:442 (ts - st['mark_ts']).total_seconds() > policy.mark_max_staleness_s
src/quant_lab/market/nautilus_adapter.py:450 self.fi < len(self.funding_rows) and self.funding_rows[self.fi].calc_time <= until and (st['pos'] != 0) and (not st['censor'])
src/quant_lab/market/nautilus_adapter.py:450 self.funding_rows[self.fi].calc_time <= until
src/quant_lab/market/nautilus_adapter.py:453 row.calc_time < t_start or row.calc_time in self.settled_at or row.calc_time < (st['open_at'] or t_start)
src/quant_lab/market/nautilus_adapter.py:453 row.calc_time < t_start
src/quant_lab/market/nautilus_adapter.py:453 row.calc_time in self.settled_at
src/quant_lab/market/nautilus_adapter.py:453 row.calc_time < (st['open_at'] or t_start)
src/quant_lab/market/nautilus_adapter.py:453 st['open_at'] or t_start
src/quant_lab/market/nautilus_adapter.py:456 m.ts <= row.calc_time and m.path_step in ('none', 'C')
src/quant_lab/market/nautilus_adapter.py:456 m.ts <= row.calc_time
src/quant_lab/market/nautilus_adapter.py:457 not mk or (row.calc_time - mk[-1].ts).total_seconds() > policy.mark_max_staleness_s
src/quant_lab/market/nautilus_adapter.py:457 (row.calc_time - mk[-1].ts).total_seconds() > policy.mark_max_staleness_s
src/quant_lab/market/nautilus_adapter.py:457 row.calc_time - mk[-1].ts
src/quant_lab/market/nautilus_adapter.py:499 rules.effective_from and t_start < rules.effective_from or (rules.effective_to and t_start >= rules.effective_to)
src/quant_lab/market/nautilus_adapter.py:499 rules.effective_from and t_start < rules.effective_from
src/quant_lab/market/nautilus_adapter.py:499 rules.effective_to and t_start >= rules.effective_to
src/quant_lab/market/nautilus_adapter.py:499 t_start < rules.effective_from
src/quant_lab/market/nautilus_adapter.py:499 t_start >= rules.effective_to
src/quant_lab/market/nautilus_adapter.py:527 plan.expiry.max_holding_s is not None
src/quant_lab/market/nautilus_adapter.py:530 min(opens) + dt.timedelta(seconds=plan.expiry.max_holding_s)
src/quant_lab/market/nautilus_adapter.py:531 e['ts'] < hold_end
src/quant_lab/market/nautilus_adapter.py:559 plan.expiry.max_holding_s is not None and st['open_at'] is not None
src/quant_lab/market/nautilus_adapter.py:559 plan.expiry.max_holding_s is not None
src/quant_lab/market/nautilus_adapter.py:560 st['open_at'] + dt.timedelta(seconds=plan.expiry.max_holding_s) - dt.timedelta(microseconds=1)
src/quant_lab/market/nautilus_adapter.py:560 st['open_at'] + dt.timedelta(seconds=plan.expiry.max_holding_s)
src/quant_lab/market/nautilus_adapter.py:561 {m.ts for m in marks if t_start <= m.ts <= cutoff} | {p.ts for p in lasts if p.ts <= cutoff}
src/quant_lab/market/nautilus_adapter.py:561 t_start <= m.ts <= cutoff
src/quant_lab/market/partition_check.py:246 cal_from <= t < cal_to and first_grid_point(t, sec) == t
src/quant_lab/market/partition_check.py:246 cal_from <= t < cal_to
src/quant_lab/market/partition_check.py:246 first_grid_point(t, sec) == t
src/quant_lab/market/partition_check.py:261 df.height and grid_points_between(cal_from, df[key][0], sec) > 0
src/quant_lab/market/partition_check.py:261 grid_points_between(cal_from, df[key][0], sec) > 0
src/quant_lab/market/partition_check.py:275 grid_points_between(df[key][-1] + step, cal_to, sec) > 0
src/quant_lab/market/partition_check.py:289 (pl.col('low') <= pl.min_horizontal('open', 'close')) & (pl.max_horizontal('open', 'close') <= pl.col('high')) & (pl.col('low') > 0) & pl.col('open').is_finite() & pl.col('high').is_finite() & pl.col('low').is_finite()
src/quant_lab/market/partition_check.py:289 (pl.col('low') <= pl.min_horizontal('open', 'close')) & (pl.max_horizontal('open', 'close') <= pl.col('high')) & (pl.col('low') > 0) & pl.col('open').is_finite() & pl.col('high').is_finite()
src/quant_lab/market/partition_check.py:289 (pl.col('low') <= pl.min_horizontal('open', 'close')) & (pl.max_horizontal('open', 'close') <= pl.col('high')) & (pl.col('low') > 0) & pl.col('open').is_finite()
src/quant_lab/market/partition_check.py:289 (pl.col('low') <= pl.min_horizontal('open', 'close')) & (pl.max_horizontal('open', 'close') <= pl.col('high')) & (pl.col('low') > 0)
src/quant_lab/market/partition_check.py:289 (pl.col('low') <= pl.min_horizontal('open', 'close')) & (pl.max_horizontal('open', 'close') <= pl.col('high'))
src/quant_lab/market/partition_check.py:289 pl.col('low') <= pl.min_horizontal('open', 'close')
src/quant_lab/market/partition_check.py:289 pl.max_horizontal('open', 'close') <= pl.col('high')
src/quant_lab/market/partition_check.py:333 df.height - df['calc_time'].n_unique()
src/quant_lab/market/single_source.py:177 't_start' in attrs and 't_dec' in attrs
src/quant_lab/market/single_source.py:177 't_start' in attrs
src/quant_lab/market/single_source.py:177 't_dec' in attrs
src/quant_lab/market/single_source.py:187 name == 'int' and node.args and ('total_seconds()' in self._seg(node.args[0]))
src/quant_lab/market/single_source.py:187 'total_seconds()' in self._seg(node.args[0])
src/quant_lab/market/single_source.py:207 isinstance(n, ast.Attribute) and n.attr == 'entry_ttl_s'
src/quant_lab/market/single_source.py:207 n.attr == 'entry_ttl_s'
src/quant_lab/market/single_source.py:214 isinstance(node.op, (ast.FloorDiv, ast.Div)) and ('total_seconds()' in seg or 'interval_s' in seg or 'timedelta' in seg)
src/quant_lab/market/single_source.py:215 'total_seconds()' in seg or 'interval_s' in seg or 'timedelta' in seg
src/quant_lab/market/single_source.py:215 'total_seconds()' in seg
src/quant_lab/market/single_source.py:218 isinstance(node.op, ast.Add) and 'entry_ttl_s' in seg
src/quant_lab/market/single_source.py:218 'entry_ttl_s' in seg
src/quant_lab/market/vision.py:262 (pl.col('low') <= pl.min_horizontal('open', 'close')) & (pl.max_horizontal('open', 'close') <= pl.col('high')) & (pl.col('low') > 0) & pl.col('open').is_finite() & pl.col('high').is_finite() & pl.col('low').is_finite()
src/quant_lab/market/vision.py:262 (pl.col('low') <= pl.min_horizontal('open', 'close')) & (pl.max_horizontal('open', 'close') <= pl.col('high')) & (pl.col('low') > 0) & pl.col('open').is_finite() & pl.col('high').is_finite()
src/quant_lab/market/vision.py:262 (pl.col('low') <= pl.min_horizontal('open', 'close')) & (pl.max_horizontal('open', 'close') <= pl.col('high')) & (pl.col('low') > 0) & pl.col('open').is_finite()
src/quant_lab/market/vision.py:262 (pl.col('low') <= pl.min_horizontal('open', 'close')) & (pl.max_horizontal('open', 'close') <= pl.col('high')) & (pl.col('low') > 0)
src/quant_lab/market/vision.py:262 (pl.col('low') <= pl.min_horizontal('open', 'close')) & (pl.max_horizontal('open', 'close') <= pl.col('high'))
src/quant_lab/market/vision.py:262 pl.col('low') <= pl.min_horizontal('open', 'close')
src/quant_lab/market/vision.py:262 pl.max_horizontal('open', 'close') <= pl.col('high')
```


### 补充原样复跑与B17双strict生产基线

S38/S39最小命令及P29/P30/P31最小命令也经shlex.split原样复跑。
P31在导入已删除旧测试名时即失败，不能据此声称执行了删门。
P29第三组实际expected=4正确，不能沿用历史括注中的3。

#### S38最小命令：exit 1

```text
stdout为空
Traceback (most recent call last):
  File "<string>", line 1, in <module>
  File "<string>", line 6, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/.venv-g2/lib/python3.12/site-packages/pydantic/main.py", line 732, in model_validate
    return cls.__pydantic_validator__.validate_python(
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/market/contract.py", line 329, in _latency_domain
    raise ContractError("latency_s 必须非负：启动时刻不能早于决策时刻")
quant_lab.market.contract.ContractError: latency_s 必须非负：启动时刻不能早于决策时刻
```

#### S39最小命令：exit 0

```text
discarded result: GREEN
restored: GREEN

```

#### P29最小：exit 0

```text
0 180000000 3 0 [False, False, False] []
1 180000000 2 0 [False, False] []
0 180000001 4 0 [False, False, False, False] []

```

#### P30最小：exit 0

```text
-1 False REJECT horizon_end 必须晚于 t_start（推导值 2024-01-01 01:01:00+00:00，latency_s=60）
-1 True REJECT horizon_end 必须晚于 t_start（推导值 2024-01-01 01:01:00+00:00，latency_s=60）
0 False REJECT horizon_end 必须晚于 t_start（推导值 2024-01-01 01:01:00+00:00，latency_s=60）
0 True REJECT horizon_end 必须晚于 t_start（推导值 2024-01-01 01:01:00+00:00，latency_s=60）
1 False ACCEPT 0:00:00.000001
1 True ACCEPT 0:00:00.000001

```

#### P31最小：exit 1

```text
stdout为空
Traceback (most recent call last):
  File "<string>", line 1, in <module>
  File "<string>", line 4, in <module>
ImportError: cannot import name 'test_b16_pair_key_carries_policy_hash_and_batch_rejects_split_brain' from 'tests.market.test_outcome_kind' (/Users/balen/projects/trader-bot/quant-lab/tests/market/test_outcome_kind.py)
```

独立B17双strict命令：`.venv-g2/bin/python -c 'exec("from unittest.mock import patch\nfrom tests.market.test_review_p1 import e03\nfrom quant_lab.market import contract as c,execution as x\nq,m=e03()\nq2=c.ExecutionRequest.model_validate({**q.model_dump(),\"episode_id\":\"audit-second\"})\noriginal=x.result_row\ndef inject(req,res):\n row=original(req,res)\n if req.episode_id==\"audit-second\":\n  row[\"policy_hash\"]=\"0\"*64\n return row\nfor strict in (True,False):\n with patch.object(x,\"result_row\",side_effect=inject):\n  try:\n   x.simulate_batch([q,q2],markets={m.manifest_id:m},strict=strict)\n  except c.ContractError as e:\n   print(\"strict\",strict,str(e))\n   assert q.policy_version in str(e) and q.policy_hash in str(e) and \"0\"*64 in str(e)\n  else:\n   raise AssertionError(\"gate did not reject\")\nprint(\"B17_BOTH_STRICT_PASS\")\n")'`

```text
strict True 同一 policy_version 对应多个 policy_hash（版本串与内容不一一对应）：fixture-zero-v1 → ['0000000000000000000000000000000000000000000000000000000000000000', '12ca10ebb10ef27d10873584ee42a224c44efdf99e9b55c7656e8a838c58cd1d']
strict False 同一 policy_version 对应多个 policy_hash（版本串与内容不一一对应）：fixture-zero-v1 → ['0000000000000000000000000000000000000000000000000000000000000000', '12ca10ebb10ef27d10873584ee42a224c44efdf99e9b55c7656e8a838c58cd1d']
B17_BOTH_STRICT_PASS
```

### 本轮文件与SHA256审计

集合摘要算法：按quant-lab相对路径排序，拼接“路径:sha256\\n”，再SHA256；包括检查时已存在的文件，不声称原子快照。

```text
FILE_COUNT 91 ALL_EQUAL True
CHANGED []
src/quant_lab/market 22 67c3754c99e552a460aaa5567e2324768a7ecfb6adb6408847bd7701a6f89158
tests/market 69 42ea957cd0e202d6a7198e3cef662de90b15f6aa1bb1dde86d2496fb8d6c93f8
tests/market/fixtures/episodes 22 a21f4c6f89acb4744007a3309584c3dc1bb5b2d1d299eeb901c5defd1b3b0576
partition_check.py ad1250d8eca7361d780c64a6d3cbfedd066c860e18608f51b69172d41a527b2b
execution.py 9e5f278e994af1beb9736420cbc79c53322f43620056b6ea4e46a7567fba80a7
single_source.py e274fb5cd78fb301c5f157931bc80fb7742915166f4b6aa227e00aa66c140d3f
vision.py 30f0dcdf25bd9e9869fe13d516fdd7a0d7646528e440cf5f85ff993147614cd8
nautilus_adapter.py 580c782a26924ddbad1d576a4ca107b475e74d92a01a57e2fdd22fa68fee5650
__init__.py 70149b449a03075ef4218d03921491270426af8851fce59d942d8966fca8f80f
quarantine.py abdacaba0e7be23d1741a79f01ad0ca77132b68b1f7594121e3563426dfb0662
contract.py 318bd84a80829ac70394b614027d6b1665659d3ce93db477454b1d2a63e8b2cd
asof.py 91e35abd6dd83199af7b47fa253b2d2121fea15bc428b50e7724e3321a2ec090
kernel_a.py d0582ac7a77ea000c3f901882e97802726000e087b1075ca1e5f204b9c169c82
HISTORY_SHA256 732c7b3f4dedc149ece0b6cc6cea9cd2ddfce9d0a6fcca0156b1469e8d5e3c90
```

历史报告原始字节SHA256为上述HISTORY_SHA256。
十一审章节只加在前部；历史整个字节串保留，不改写旧轮正文或旧终裁。
文件最末在原四行之后追加本轮两行。
本轮全部结论的实跑依据、失效命令原因、未做事项及适用范围均已明列。

---

## 十审判定表

审查日期：2026-09-11。独立审查方：Codex 主控；本轮不承担修复。
本轮判 fail。九审三条中：S29 open、S30 closed（限定原反例）、S31 open。
阻断项为 S29、S31、S38、S39；重复公式另列 S32–S37，不把当前等价的公式重复冒称为已发生的经济错误。
表中 closed 仅覆盖明确写出的命题，不表示整个缺陷族已经消失。
历史各轮文字及九审结尾保留；本轮结论优先于历史状态。

| ID | 状态 | 亲跑命令与输出摘要 | 是否阻断 P1 |
|---|---|---|---|
| S29 分区网格及其回归 | open | 原 P29 exit0：3/0、2/0、4/0，全部 gap_flag=false；P10N 加一个离网点后 missing=-1；P10M 恢复旧 first_gap 后新测试 GREEN。 | 是；非负门槛及原缺陷注入回归未满足 |
| S30 观察窗下界 | closed | 原 P30 exit0：latency60，省略/显式均在 -1µs、0 REJECT，在 +1µs ACCEPT；P10C caller 三点一致；P10N 原下界突变 RED、恢复 GREEN。 | 否；同族第2处 build_request 仍在，单列 S32；负延迟另列 S38 |
| S31 B17 冲突门 | open | 原 P31 在旧 AST 匹配断言处退出，未删门；P10M 真删门：旧 B16 GREEN、新 B17 RED；P10D 改传入哈希后新 B17 GREEN、真实批量2行2hash不拒绝。 | 是；真实双哈希批量的注入回归仍不足 |
| S32 build_request 启动公式第二处 | open | P10I 打印 contract:262/522 两份 latency 加法；P10C 改唯一来源为+7秒，build_request 抛推导对账 ContractError。 | 否；当前公式等价，但单一来源目标尚未完成 |
| S33 vision 独立网格计数 | open | P10I 打印 vision:318 的 total_seconds 整除；T 中 interval_seconds 测试通过。 | 否；受支持整日/月、整除日周期内未证实数值错算 |
| S34 funding 8h 独立网格 | partial-P2 | P10I 打印 execution:236–243 的 hour//8、闭右界和60秒容差；AB 明示 S02 unsupported。 | 否；与既有 S02 同一变周期/真实结算边界，不重复升级 |
| S35 TTL 解析三份与到期三份 | open | P10I：contract:408/410、462/463、519；deadline 在 A:222/318、B:226；T 的 S19 通过。 | 否；当前 None 分支一致，尚未归一 |
| S36 持仓截止三份 | partial-P2 | P10I：A:423、B:530/560；T 的 hold/exposure 回归通过，AB仍保留 B 定型边界。 | 否；当前精确 timedelta，属于既有 S04/S16 及复用债务 |
| S37 A 中尾网格独立公式 | open | P10I：A:242 首点委派，但249–253仍自行比较 o-prev 和 prev+iv；P10G 对齐网格子集25组通过。 | 否；未把首点委派扩大成全族已归一 |
| S38 负 latency 使显式/省略分叉 | open | P10C：latency=-1，policy 正常 model_validate；隐式 ACCEPT 00:59:59，显式同值 REJECT「t_start 不能早于 t_dec」。 | 是；同语义请求不同验收且允许决策前启动 |
| S39 loader 哨兵仅证明调用 | open | P10D：保留 helper 调用但令 expected=0，test_grid_math 单测 GREEN；恢复 GREEN。 | 是；A23 正向委派未证明返回值实际影响覆盖门 |
| A23-A 首点委派断言 | closed | P10M 捕获旧函数绕开可替换 helper：RED AssertionError；恢复 GREEN。 | 否；同族其余调用逐个核对，loader残余单列 S39，中尾数学单列 S37 |
| A23-C 请求启动委派断言 | closed | P10N 模型内恢复自写 latency 加法：RED「DID NOT RAISE」；恢复 GREEN。 | 否；同族第2处构造函数未归一，单列 S32 |
| A23-L 两处负向 lint | closed | P10L 注入两个禁止字符串：两条均 RED AssertionError；恢复均 GREEN；docstring均标「非行为证据」。 | 否；不把源码字符串检查算作行为证明 |

## 十审独立证据与新增必修

### 执行边界与命令约定

所有命令在 quant-lab 目录执行。
所有 Python 子进程继承 PYTHONDONTWRITEBYTECODE=1；导入产品的探针另设 PYTHONPATH=src。
pytest 参数均含 -p no:cacheprovider。
未连接网络、交易所私有 API 或生产服务，未读取账户配置，未 import services 模块。
未调用其他模型或续接历史会话。
唯一持久修改是本报告；测试临时湖由 pytest 临时目录承载。
S30 测试会调用 POLICY_HASH_REGISTRY.write_text，裸跑指定 T/R 会临时改产品文件。
因此 T/R 通过内存 Path.read_text/write_text 替身执行同一 pytest 参数，仅政策登记表的读写转到内存。
这项适配没有删测试、修改断言或绕过产品 validator；登记表初值取真实文件，finally 的恢复也在内存执行。
不能将这两次结果描述成未经适配的裸命令实跑。
探针中的 AST/类重建/函数 patch 均仅存在于子进程；模型突变通过重建 pydantic 类保证验证器真正换成突变版本。

### 指定验证结果

| 名称 | 运行内容 | 真实结果 |
|---|---|---|
| T | pytest tests/market -q -p no:cacheprovider，内存登记表包装 | 260 passed in 17.26s，exit0 |
| R | pytest tests/market/test_review_p1_round4.py tests/market/test_outcome_kind.py tests/market/test_contract.py tests/market/test_partition_check.py -q -v -p no:cacheprovider，同包装 | collected 54；54 passed in 3.73s，exit0 |
| A | .venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A | passed=22 failed=0，22个 replay=True，exit0 |
| AB | .venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1 | MATCH12、B_COMMAND_LATENCY3、GAP_PRICE1、SAME_TS_PRIORITY4、GTD_BOUNDARY1、B_LIQUIDITY_MODEL1，exit0 |
| AB 未解释门 | 上述实际 codes 汇总22例 | UNEXPLAINED=0；未输出 NOT_RUN/A_GOLD_FAIL；未写 AB 报告文件 |

AB 单次 median_ms 为 A=12.615667001227848、B=83.15900000161491，仅记事实，不据一次计时定性能门槛。
旧 S01–S28 的测试都包含在 T 中；本轮没有逐条重新声明全部历史独立探针已运行。
历史 partial-P2 的支持边界保持；不能用全量单测绿替代本轮反例。
M-03 真实网络下载未做：本轮只读审查不下载或写湖，历史 G0 网络证据未冒充本轮实跑。
M-03..M-09 的看板脚本未做：本轮执行的是用户列明的本地验证，不改看板和外部状态。
B 单独 replay 未做：本轮已执行要求的 A 与 AB，没有额外宣称 B 金标全过。

### 原 P29/P30/P31 的原样复跑

原命令从原报告以 P29最小、P30最小、P31最小 开头的行取出第一对反引号内容。
用 shlex.split 后 subprocess.run(capture_output=True,text=True) 执行；没有改写原命令。
原报告 SHA256 为 7214421ee0502a46f911f8273cc261d0309d4b3a6b8a62fd4e1b1ce71a2d8fca。

P29 第一组：0 180000000 3 0 [False, False, False] []。
P29 第二组：1 180000000 2 0 [False, False] []。
P29 第三组：0 180000001 4 0 [False, False, False, False] []。
任务书第三组 expected=3 是口径笔误：半开区间 [0,180秒+1µs) 包含 0/60/120/180 四个点；实际4是正确答案。
P10G 独立逐点枚举375组 helper 输入全部 PASS；不是拿 helper 自己计算期望。
P10G 另跑三个原边界的全部非空对齐网格子集，共25组，missing与缺口总数逐一相等。

P30 的 -1µs、0，两种 t_start 表达均 REJECT，错误含推导启动时刻与 latency_s=60。
P30 的 +1µs，两种表达均 ACCEPT，窗口都为 0:00:00.000001。
P10C 独立经过 build_request 的 caller 路径，latency=0/60均重复得到 REJECT/REJECT/ACCEPT。
S30 原反例闭合；同族其余成员逐个核对后，第2处构造公式仍在 S32，负延迟域漏检仍在 S38。

P31 真实 strict=True/False 门都报 ContractError，输出含 fixture-zero-v1 和两个完整64位哈希，hashes_listed=True。
P31 随后打印 original PASS，在 assert len(remove)==1 处 AssertionError，rc=1。
原因是门已从旧 conflict 条件块改为具名函数调用，旧 AST 定位器找不到目标。
这个 rc=1 不是测试因删门变红；删门动作尚未发生。
P10M 用当前函数调用作精确替换目标，要求仅一处，再在内存重编译，完成有效删门。
有效删门结果：旧 B16 仍 GREEN，新 B17 因 called=[] 变 RED，恢复两条 GREEN。

### S29 必修：离网行数仍被当作网格覆盖；新测试未杀死原首行错误

位置：partition_check.py:244–245，present 仅按时间范围过滤，随后 n_unique 直接抵扣网格点数。
没有将 present 与期望网格取交集，也没有先拒绝离网时间。
P10N 保持每行 close_time=open_time+60秒，输入偏移为 0、1µs、60秒、120秒。
窗口为 [0,180秒)，实际输出 expected=3、missing=-1、gaps=[]、所有 gap_flag=false、status=gap。
另一组为 1µs、60秒+1µs、120秒+1µs：expected=3、missing=0、首 gap 的 n=1、status=ok。
后一组证明问题不只是负数显示：数量看似完整，却一个期望分钟点都没有，缺口清单和总数也自相矛盾。
这些是内存合成非法时间网格输入；结论是入口未正确识别它，不声称公开归档普遍存在这种输入。
验收应先明确离网行隔离/拒绝规则，再按合法网格计算 present，确保 missing恒非负且缺口与计数一致。
P10M 仅将 first_gap 改回 df首时间>cal_from，现有新 S29 测试仍 GREEN。
同测试对旧 exp_n 整秒截断能 RED，说明不是整个测试无效，而是原亚秒下界分支未覆盖。
应补原 P29 第二组，确认恢复旧 first_gap 时测试失败。
原三组输入及当前对齐子集正确，不足以判 S29 closed。

### S31 必修：门本身正确，但批量冲突注入及诊断断言仍有洞

位置：test_outcome_kind.py:287–317、345–359；execution.py:125具名门调用。
新增 B17 测试确实把 split DataFrame送入门本身，并检查两个hash取值。
新增哨兵用正常三行batch，只记录 df.height，未把双hash送入 simulate_batch 的真实输出路径。
旧 B16 仍只在测试内重复 group_by 数学，删门不红。
P10D 将真实门调用的输入改成同高度、hash全部为sanitized的表，输出df本身仍保留原值。
新 B17 测试 GREEN；同进程通过 result_row 注入真实冲突，simulate_batch返回2行2hash且 NO ContractError。
这说明测试不能区分“检查了正确输出”与“检查了另外一张同高度表”。
应在真实 result_row/批量输出边界注入双hash，断言正常门拒绝，并在删门后确认该条行为测试失败。
P10L 将诊断版本串替换为 REDACTED，新 B17 仍 GREEN：当前代码含版本，但新测试未断言版本名。
验收还应断言版本名及该版本全部冲突hash，至少覆盖两个版本之一冲突，不依赖首行所属版本。
真实代码当前并没有sanitized替换；这里是标准门禁突变证据，不混称为当前生产放行漏洞。

### S38 新必修：负延迟触发同族显式值分叉

位置：ExecutionPolicy.latency_s（contract.py:295）没有非负域约束；ExecutionRequest:439只对显式t_start检查不得早于t_dec。
P10C 用 ExecutionPolicy.model_validate 正常构造并内存登记 latency=-1政策，未用 model_copy绕过请求验证。
同一policy/hash，t_start=None被接受并推导为t_dec-1秒；显式给这个推导值却被拒绝。
caller的+1µs观察窗也能在该负启动点后被接受。
此为S20/S30族的另一个边界，不改变对正延迟S30原反例的闭合判断。
修复应在政策域拒绝负延迟，并使“不得早于决策”作用于推导值；合法零/正延迟两路径仍一致。
现有登记政策并未发现负延迟；这里证明公共模型可正常接受新政策的非法时间域，风险在新增政策入口。

### S39 新必修：loader委派测试只证明调用次数

位置：test_constants_effective.py:134–144。
哨兵向called追加参数并返回0，随后对/nonexistent-lake-for-delegation-probe调用，异常被捕获，最终仅assert called。
P10M 绕开helper调用会RED，所以调用存在确有检查。
但P10D 保留该调用、丢弃其返回值并固定expected=0，测试仍GREEN。
应使用受控完整内存湖，将网格helper返回值变化传递到bars_complete/quality_notes等可观察输出并断言。
同族A首点哨兵和请求validator哨兵已分别经原行为绕过突变证实会RED；不能把这两条结果移用给loader。

### 族 A：五条规则的全部已定位表达位置

1. 启动时刻规范公式在contract.derived_t_start:262。
2. validator:446调用derived_t_start，随后下界、对账和窗长均使用exp_t_start。
3. resolved_t_start:499对显式值返回记录，对省略值委派derived_t_start。
4. loader:153、A:93、B:127消费resolved_t_start，不再自己退回t_dec。
5. build_request:522仍自行把latency加进horizon_end：S32。
6. 观察窗长度规范公式仅derived_window_s:271，validator:474与build:522均调用。
7. window=horizon_end-exp_t_start在contract:470；上限:471、policy对账:475为精确timedelta比较。
8. build_request docstring:508仍把max_horizon_s写作研究段，与实际research_horizon_s不符；为S32附近文档残余，不作为独立经济故障。
9. 网格原语为contract.first_grid_point:239与grid_points_between:249，共用精确微秒时间尺度；两函数分别解决首点与计数。
10. partition:242/252/258/263/267均调用grid_points_between；并未直接导入first_grid_point，首点存在性通过计数判断。
11. partition:250仍以delta>step判断中间gap_flag；该前提依赖输入时间已对齐，离网漏洞见S29。
12. loader:214期望bar数调用grid_points_between；返回值生效回归不足见S39。
13. vision:318仍自行total_seconds整除：S33。
14. funding loader:236–243仍hour//8、加8h、闭右界并容差60秒：S34。
15. funding partition:328–330以行内interval_hours比较相邻差，容差60秒；与loader固定8h不同，归S34/S02。
16. A首点:242调用first_grid_point；中段和尾段:249–253独立距离公式，单列S37。
17. B质量门复用A的首缺定位逻辑，没有新增另一套网格计数。
18. TTL解析在before:408/410、after:462/463、build:519共三处，S35。
19. TTL到期在A:222、A:318、B:226各自计算start+ttl，亦归S35。
20. 持仓截止在A:423、B:530、B:560共三处，S36。
21. B:560减1µs用来表达截止前的暴露样本；A在截止时刻前先删失，当前测试未证明两者新分叉。
22. 派生观察窗使用max_holding或research_horizon作为长度，并不等于首次成交后的实际持仓截止，不能将两种规则混并。

S32/S33/S35/S37 为明确尚未归一的open清单，但本轮没有把纯重复自动升级成P1功能故障。
S34/S36与既有P2边界重叠，保持partial-P2。
上述每处均由P10I/P10S实际源码枚举定位；静态证据只证明表达存在，数值正确性以行为探针为限。

### 族 B：截断、整除与时长比较静态穷举

AST横扫9个market Python模块，所有FloorDiv及total_seconds调用均逐个读取。
contract:236为timedelta除1µs，保留Python datetime全部微秒；254/255是整数ceil/floor，P10G覆盖负epoch及边界。
vision:318是唯一带int的total_seconds网格路径，单列S33；没有其他int(total_seconds())隐藏位置。
execution:237的hour//8是固定funding网格，单列S34。
nautilus_adapter:51有两处//，分别纳秒分秒和余数纳秒降微秒；符合当前事件datetime微秒接口，不声称支持亚微秒。
asof:179的total_seconds服务staleness；判定在180用>阈值，没有int截断。
A:573/655的total_seconds判mark过旧，没有int截断。
B:426/442/457的total_seconds判mark过旧，没有int截断。
execution:240用abs(total_seconds)<=60做funding容差，没有先整秒截断。
partition:328 total_seconds/3600、330乘回3600再比较60秒，为浮点容差路径，未归一成精确timedelta。
A/B的horizon、deadline、hold_end比较已逐项定位在P10S附录；主体使用datetime/timedelta关系运算。
vision:215的int(float(v))用于外部时间戳；当前现代毫秒/微秒范围在2^53以内，不将超范围假设认定为当前反例。
vision:221的int(timestamp())仅处理严格秒格式字符串；带小数的字符串会解析失败，不是静默丢亚秒。
vision:254/273的int(float(...))用于trades与funding_interval_hours，不是观察窗长度。
B:_ns对非零microsecond分开计算整秒和微秒，未用int(total_seconds())。
价格/数量floor_step和分配ROUND_DOWN为契约明确量化，不算时间截断问题。
未做全年份/任意大时间戳/任意间隔域穷举；当前结论限于源码定位、指定实跑和P10G输入域。

### 族 C：可空与默认字段逐项枚举

P10F通过model_fields枚举ExecutionRequest/OrderPlan/ExecutionPolicy及其嵌套模型所有非必填或可空字段。
每字段用省略、None、False、0、空字符串、空列表六种输入，基底为E03/fixture-zero-v1，逐次正常model_validate。
下面逐行只列接受的输入及实际值；未列的输入均拒绝。不是对任意策略域的全排列证明。
ExecutionPolicy的research_horizon_s为必填非空字段，不在默认/可空表；T包含其必填回归。
Request的horizon_source省略会取policy，但E03实际是caller短窗，因此该基底下六种探针全部拒绝，符合对账规则。
Sizing.qty表用risk_budget模式，qty允许存在但此模式不取它；fixed_qty分支另有正数校验。
TakeProfit.fraction单模型的0可解析，OrderPlan总入口随后check_decimal拒绝；不能把子模型结果冒充整个请求接受。
政策数值多数没有范围validator；本轮确认能造成时间一致性错误的是S38，其他零值仅记录接受事实。
以下是P10F真实输出：

```text
ExecutionRequest.cost_scenario | omit='base'
ExecutionRequest.path_scenario | omit='primary'
ExecutionRequest.execution_contract_version | omit='g2-exec-v0'
ExecutionRequest.seed | omit=0; False=0; 0=0
ExecutionRequest.t_start | omit=None; None=None
ExecutionRequest.position_mode | omit='one_way'
ExecutionRequest.entry_ttl_s | omit=3600; None=3600
ExecutionRequest.entry_fractions | omit=(Decimal('1'),); None=(Decimal('1'),)
ExecutionRequest.tp_fractions | omit=(Decimal('1'),); None=(Decimal('1'),)
ExecutionRequest.horizon_source | all reject
OrderPlan.tps | omit=[]; emptylist=[]
OrderPlan.reduce_only_exit | omit=True
ExecutionPolicy.latency_s | omit=0; False=0; 0=0
ExecutionPolicy.ladder_steps | omit=2; False=0; 0=0
ExecutionPolicy.participation | omit=None; None=None; 0=Decimal('0')
ExecutionPolicy.wallet | omit=Decimal('1000'); 0=Decimal('0')
ExecutionPolicy.leverage | omit=Decimal('1'); 0=Decimal('0')
ExecutionPolicy.mark_max_staleness_s | omit=120; False=0; 0=0
ExecutionPolicy.settlement_quantum | omit=Decimal('1E-8'); 0=Decimal('0')
ExecutionPolicy.max_horizon_s | omit=1209600; False=0; 0=0
ExecutionPolicy.entry_ttl_s | omit=86400; False=0; 0=0
ExecutionPolicy.entry_fraction_rule | omit='equal'
ExecutionPolicy.tp_fraction_rule | omit='equal'
ExecutionPolicy.tp_total_fraction | omit=Decimal('1'); 0=Decimal('0')
Entry.fraction | omit=None; None=None
Entry.tif | omit='GTC'
Entry.post_only | omit=False; False=False; 0=False
Stop.trigger | omit='mark'
TakeProfit.fraction | omit=None; None=None; 0=Decimal('0')
Sizing.qty | omit=None; None=None; 0=Decimal('0')
Expiry.entry_ttl_s | omit=None; None=None
Expiry.max_holding_s | omit=None; None=None
CostSpec.maker_fee | omit=Decimal('0'); 0=Decimal('0')
CostSpec.taker_fee | omit=Decimal('0'); 0=Decimal('0')
CostSpec.slippage_ticks | omit=0; False=0; 0=0
CostSpec.slippage_bps | omit=Decimal('0'); 0=Decimal('0')
```

此轮未发现Request中的None被falsy-or改成另一个显式合法分配值。
t_start使用is not None区分；TTL before仅在None兜底，after逐值比较；fraction before同样保留空序列供后续拒绝。
OrderPlan.tps缺省空列表合法，reduce_only_exit固定True；Entry.tif和Stop.trigger受Literal约束。
参与率None与0在政策模型中区分，0不是无限容量；钱包/杠杆0不因or被改成默认值。
状态中open_at or t_start、eff_to or b的左项为datetime或None，没有合法falsy datetime。
输入可空字段的域漏检与“or改写”不同：S38虽没有or，仍因显式专用的先验检查造成分叉。

### 族 D：新门的突变矩阵

| 门/断言 | 注入内容 | 突变结果 | 恢复结果 | 结论 |
|---|---|---|---|---|
| S29计数 | exp_n恢复整秒整除 | RED AssertionError | GREEN | 此分支回归有效 |
| S29首行 | first_gap恢复首时间>cal_from | GREEN | GREEN | 原下界反例未钉住 |
| S30下界 | 恢复self.t_start or self.t_dec | RED Failed: DID NOT RAISE ContractError | GREEN | 原下界回归有效 |
| B17旧B16 | 删除具名门调用 | GREEN | GREEN | 旧断言仍是自证数学 |
| B17新测试 | 删除具名门调用 | RED called=[] | GREEN | 能证明调用存在 |
| B17新测试 | 向门传同高度但统一hash表 | GREEN，且实际冲突批量返回 | GREEN | 未证明检查真实批量输出 |
| B17诊断 | 去掉版本名 | GREEN | GREEN | 版本名要求未被回归钉住 |
| A首点委派 | 调用捕获的旧helper绕过可替换绑定 | RED moved==real | GREEN | 返回值行为已证明 |
| Request启动委派 | validator自行计算latency | RED DID NOT RAISE | GREEN | 原委派行为已证明 |
| loader委派 | 绕过可替换helper调用 | RED called为空 | GREEN | 仅调用存在检查有效 |
| loader委派 | 保留调用但丢弃返回值 | GREEN | GREEN | 返回值生效证据不完整 |
| multiplier lint | 源码字符串含禁止表达 | RED AssertionError | GREEN | 仅lint，不是行为证据 |
| t_start lint | 源码字符串含旧回退表达 | RED AssertionError | GREEN | 仅lint，不是行为证据 |

首次探索的sanitize突变复制模块globals，意外固定旧门绑定，使哨兵无法生效，得到伪RED。
该探索结果不计为验收；P10D改为动态转发到x.check_policy_hash_consistency，再跑得到上述真实GREEN。
每条RED都检查具体异常/断言位置，没有将子进程rc=1笼统当作成功。
测试内临时写真实登记表的做法未在本轮修复；后续普通裸跑仍有并发读写风险，应改进程内patch或独立临时登记文件。

### 新增P1必修的最小只读复现

S38最小命令：`.venv-g2/bin/python -c 'exec("import datetime as dt\nfrom unittest.mock import patch\nfrom quant_lab.market import contract as c\nfrom tests.market.test_review_p1 import e03\nq,_=e03()\np=c.ExecutionPolicy.model_validate({**c.resolve_policy(q.policy_version).model_dump(),\"version\":\"negative-latency-audit\",\"latency_s\":-1})\nwith patch.dict(c.POLICIES,{p.version:p}),patch.object(c,\"load_policy_registry\",return_value={p.version:p.content_hash}):\n    for s in (None,q.t_dec-dt.timedelta(seconds=1)):\n        try:\n            r=c.ExecutionRequest.model_validate({**q.model_dump(),\"policy_version\":p.version,\"policy_hash\":p.content_hash,\"t_start\":s})\n            print(s,\"ACCEPT\",r.resolved_t_start(p))\n        except c.ContractError as e:print(s,\"REJECT\",str(e))\n")'`。
实际输出：None ACCEPT 2024-01-01 00:59:59+00:00；显式同值REJECT t_start不能早于t_dec。

S39最小命令：`.venv-g2/bin/python -c 'exec("import inspect\nfrom unittest.mock import patch\nfrom quant_lab.market import execution as x\nfrom tests.market.test_constants_effective import test_grid_math_single_source_and_exact_to_microsecond as test\nold='"'"'expected = grid_points_between(a, min(b, dt.datetime.now(dt.UTC)), INTERVAL_SECONDS[\"1m\"])'"'"'\nsrc=inspect.getsource(x.load_market_from_lake)\nassert src.count(old)==1\nns=dict(x.__dict__)\nexec(compile(src.replace(old,'"'"'grid_points_between(a, min(b, dt.datetime.now(dt.UTC)), INTERVAL_SECONDS[\"1m\"]); expected = 0'"'"'),\"<memory>\",\"exec\"),ns)\nns[\"grid_points_between\"]=lambda *args:x.grid_points_between(*args)\nwith patch.object(x,\"load_market_from_lake\",ns[\"load_market_from_lake\"]):\n    test();print(\"discarded result: GREEN\")\ntest();print(\"restored: GREEN\")\n")'`。
实际输出：discarded result: GREEN；restored: GREEN。

### 可直接复跑的只读命令

以下命令均为本轮实际执行脚本的单行封装，继承本节开头环境；不用另建脚本文件。
P10M包含删门、S29两种原缺陷及A/loader调用绕过：
`.venv-g2/bin/python -c 'exec("import ast,inspect,textwrap\nfrom unittest.mock import patch\nfrom tests.market import test_outcome_kind as t\nfrom tests.market import test_constants_effective as k\nfrom quant_lab.market import contract as c,execution as x,partition_check as pc,kernel_a as ka\ndef outcome(fn):\n    try:\n        fn()\n        return \"GREEN\"\n    except BaseException as e:\n        return \"RED \"+type(e).__name__+\" \"+str(e)[:160]\ndef mutant(fn,old,new):\n    src=textwrap.dedent(inspect.getsource(fn))\n    assert src.count(old)==1,(fn.__name__,src.count(old))\n    ns=dict(fn.__globals__);exec(compile(src.replace(old,new),\"<memory-mutant>\",\"exec\"),ns)\n    return ns[fn.__name__]\nold=t.test_b16_pair_key_carries_policy_hash_and_batch_rejects_split_brain\nnew=t.test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes\nfor label,replacement in [(\"delete\",\"pass\")]:\n    fn=mutant(x.simulate_batch,\"check_policy_hash_consistency(df)\",replacement)\n    with patch.object(x,\"simulate_batch\",fn):\n        print(\"B17\",label,\"legacy\",outcome(old),\"new\",outcome(new))\nprint(\"B17 restored\",outcome(old),outcome(new))\nfn=mutant(pc.check_bars,\"first_gap = bool(df.height and grid_points_between(cal_from, df[key][0], sec) > 0)\",\"first_gap = bool(df.height and df[key][0] > cal_from)\")\nwith patch.object(pc,\"check_bars\",fn):\n    print(\"S29 old-first-gap\",outcome(t.test_s29_partition_grid_uses_single_source_and_no_empty_gaps))\nfn=mutant(pc.check_bars,\"exp_n = grid_points_between(cal_from, cal_to, sec)\",\"exp_n = int((cal_to-cal_from).total_seconds() // sec)\")\nwith patch.object(pc,\"check_bars\",fn):\n    print(\"S29 old-count\",outcome(t.test_s29_partition_grid_uses_single_source_and_no_empty_gaps))\nprint(\"S29 restored\",outcome(t.test_s29_partition_grid_uses_single_source_and_no_empty_gaps))\n# bypass a delegation but preserve behavior through a captured binding\nfor label,fn,oldexpr,newexpr,owner,attr,test in [\n(\"A-grid\",ka.KernelA._first_bar_gap,\"first_grid_point(self.t_start, bars[0].interval_s)\",\"_captured(self.t_start, bars[0].interval_s)\",ka.KernelA,\"_first_bar_gap\",k.test_grid_math_single_source_and_exact_to_microsecond),\n(\"loader-grid\",x.load_market_from_lake,'"'"'grid_points_between(a, min(b, dt.datetime.now(dt.UTC)), INTERVAL_SECONDS[\"1m\"])'"'"','"'"'_captured(a, min(b, dt.datetime.now(dt.UTC)), INTERVAL_SECONDS[\"1m\"])'"'"',x,\"load_market_from_lake\",k.test_grid_math_single_source_and_exact_to_microsecond)]:\n    m=mutant(fn,oldexpr,newexpr)\n    m.__globals__[\"_captured\"]=c.first_grid_point if label==\"A-grid\" else c.grid_points_between\n    with patch.object(owner,attr,m):\n        print(label,\"bypass\",outcome(test))\n    print(label,\"restored\",outcome(test))\n")'`

P10N包含S29离网输入、S30原下界突变、请求启动委派突变；登记表为内存对象：
`.venv-g2/bin/python -c 'exec("import datetime as dt,inspect,textwrap\nfrom unittest.mock import patch\nfrom tests.market.test_partition_check import bars,rules,run,JAN\nfrom tests.market import test_outcome_kind as t,test_constants_effective as k\nfrom quant_lab.market import contract as c,execution as x\nimport polars as pl\nfor offsets in [(0,1,60000000,120000000),(1,60000001,120000001)]:\n    df=bars(len(offsets)).with_columns(pl.Series(\"open_time\",[JAN+dt.timedelta(microseconds=u) for u in offsets]),pl.Series(\"close_time\",[JAN+dt.timedelta(microseconds=u,seconds=60) for u in offsets]))\n    out,qs,r=run(df,rules(JAN,JAN+dt.timedelta(seconds=180)))\n    print(\"offgrid\",offsets,\"expected\",r.expected_rows,\"missing\",r.missing,\"gaps\",r.gaps,\"flags\",out[\"gap_flag\"].to_list(),\"status\",r.status)\ndef outcome(fn):\n    try:\n        fn();return \"GREEN\"\n    except BaseException as e:\n        return \"RED \"+type(e).__name__+\" \"+str(e)[:140]\nsrc=inspect.getsource(x.simulate_batch).replace(\"check_policy_hash_consistency(df)\",'"'"'check_policy_hash_consistency(df.with_columns(pl.lit(\"sanitized\").alias(\"policy_hash\")))'"'"')\nns=dict(x.__dict__);exec(compile(src,\"<memory-mutant>\",\"exec\"),ns)\nns[\"check_policy_hash_consistency\"]=lambda df:x.check_policy_hash_consistency(df)\nwith patch.object(x,\"simulate_batch\",ns[\"simulate_batch\"]):\n    print(\"B17 sanitized dynamic\",outcome(t.test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes))\n# rebuild the pydantic model in memory so validator substitutions actually execute\nsrc=inspect.getsource(c.ExecutionRequest).replace(\"if self.horizon_end <= exp_t_start:\",\"if self.horizon_end <= (self.t_start or self.t_dec):\")\nns=dict(c.__dict__);exec(compile(src,\"<memory-model-mutant>\",\"exec\"),ns)\nclass Registry:\n    def __init__(self): self.s=c.POLICY_HASH_REGISTRY.read_text()\n    def exists(self): return True\n    def read_text(self,**kw): return self.s\n    def write_text(self,s,**kw): self.s=s;return len(s)\nwith patch.object(c,\"POLICY_HASH_REGISTRY\",Registry()):\n    with patch.object(c,\"ExecutionRequest\",ns[\"ExecutionRequest\"]):\n        print(\"S30 old lower bound\",outcome(t.test_s30_window_lower_bound_uses_derived_start_not_t_dec))\n    print(\"S30 restored\",outcome(t.test_s30_window_lower_bound_uses_derived_start_not_t_dec))\nsrc=inspect.getsource(c.ExecutionRequest).replace(\"exp_t_start = derived_t_start(self.t_dec, pol)\",\"exp_t_start = self.t_dec + dt.timedelta(seconds=pol.latency_s)\")\nns=dict(c.__dict__);exec(compile(src,\"<memory-model-mutant>\",\"exec\"),ns)\nwith patch.object(c,\"ExecutionRequest\",ns[\"ExecutionRequest\"]):\n    print(\"start delegate bypass\",outcome(k.test_t_start_derivation_single_source))\nprint(\"start restored\",outcome(k.test_t_start_derivation_single_source))\n")'`

P10C复现S32、S38及build_request caller边界：
`.venv-g2/bin/python -c 'exec("import datetime as dt\nfrom unittest.mock import patch\nfrom tests.market.test_review_p1 import e03\nfrom quant_lab.market import contract as c\nq,m=e03()\nfor latency in (-1,0,60):\n    p=c.ExecutionPolicy.model_validate({**c.resolve_policy(q.policy_version).model_dump(),\"version\":\"audit-latency\",\"latency_s\":latency})\n    reg={**c.load_policy_registry(),p.version:p.content_hash}\n    with patch.dict(c.POLICIES,{p.version:p}),patch.object(c,\"load_policy_registry\",return_value=reg):\n        for explicit in (False,True):\n            try:\n                d={**q.model_dump(),\"policy_version\":p.version,\"policy_hash\":p.content_hash,\"t_start\":q.t_dec+dt.timedelta(seconds=latency) if explicit else None}\n                r=c.ExecutionRequest.model_validate(d)\n                print(\"latency\",latency,\"explicit\",explicit,\"ACCEPT\",r.resolved_t_start(p))\n            except Exception as e: print(\"latency\",latency,\"explicit\",explicit,\"REJECT\",type(e).__name__,str(e))\n        for us in (-1,0,1):\n            try:\n                r=c.build_request(q.model_dump(),policy_version=p.version,policy_hash=p.content_hash,risk_budget=q.risk_budget,market_manifest=q.market_manifest,horizon_end=q.t_dec+dt.timedelta(seconds=latency,microseconds=us))\n                print(\"build caller\",latency,us,\"ACCEPT\")\n            except Exception as e: print(\"build caller\",latency,us,\"REJECT\",type(e).__name__)\nwith patch.object(c,\"derived_t_start\",side_effect=lambda t,p:t+dt.timedelta(seconds=7)):\n    try:\n        c.build_request(q.model_dump(),policy_version=q.policy_version,policy_hash=q.policy_hash,risk_budget=q.risk_budget,market_manifest=q.market_manifest)\n        print(\"build sentinel ACCEPT\")\n    except Exception as e: print(\"build sentinel\",type(e).__name__,str(e))\n")'`

P10D复现S31真实冲突漏检突变、S39返回值丢弃突变：
`.venv-g2/bin/python -c 'exec("import inspect\nfrom unittest.mock import patch\nfrom quant_lab.market import execution as x,contract as c\nfrom tests.market import test_constants_effective as k,test_outcome_kind as t\nfrom tests.market.test_review_p1 import e03\ndef result(fn):\n    try: fn();return \"GREEN\"\n    except BaseException as e: return \"RED \"+type(e).__name__+\" \"+str(e)[:100]\nsrc=inspect.getsource(x.load_market_from_lake)\nold='"'"'expected = grid_points_between(a, min(b, dt.datetime.now(dt.UTC)), INTERVAL_SECONDS[\"1m\"])'"'"'\nnew='"'"'grid_points_between(a, min(b, dt.datetime.now(dt.UTC)), INTERVAL_SECONDS[\"1m\"]); expected = 0'"'"'\nassert src.count(old)==1\nns=dict(x.__dict__);exec(compile(src.replace(old,new),\"<discard-grid-result>\",\"exec\"),ns)\nns[\"grid_points_between\"]=lambda *args:x.grid_points_between(*args)\nwith patch.object(x,\"load_market_from_lake\",ns[\"load_market_from_lake\"]):\n    print(\"loader discard return\",result(k.test_grid_math_single_source_and_exact_to_microsecond))\nprint(\"loader restored\",result(k.test_grid_math_single_source_and_exact_to_microsecond))\nsrc=inspect.getsource(x.simulate_batch)\nold=\"check_policy_hash_consistency(df)\"\nns=dict(x.__dict__);exec(compile(src.replace(old,'"'"'check_policy_hash_consistency(df.with_columns(pl.lit(\"sanitized\").alias(\"policy_hash\")))'"'"'),\"<sanitize-gate-input>\",\"exec\"),ns)\nns[\"check_policy_hash_consistency\"]=lambda df:x.check_policy_hash_consistency(df)\nq,m=e03();q2=c.ExecutionRequest.model_validate({**q.model_dump(),\"episode_id\":\"merge\"})\noriginal=x.result_row\ndef inject(req,res):\n    row=original(req,res)\n    if req.episode_id==\"merge\": row[\"policy_hash\"]=\"0\"*64\n    return row\nwith patch.object(x,\"simulate_batch\",ns[\"simulate_batch\"]):\n    print(\"sanitized gate regression\",result(t.test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes))\n    ns[\"result_row\"]=inject\n    df=x.simulate_batch([q,q2],markets={m.manifest_id:m})\n    print(\"sanitized gate actual\",df.height,df[\"policy_hash\"].n_unique(),\"NO ContractError\")\nprint(\"gate restored\",result(t.test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes))\n")'`

P10I最小静态复现S32–S37及TTL deadline全部残余：
`.venv-g2/bin/python -c 'exec("import pathlib,re\npatterns={\"S32\":(\"contract.py\",r\"return t_dec|horizon_end = t_dec\"),\"S33\":(\"vision.py\",r\"total_seconds\"),\"S34\":(\"execution.py\",r\"g = |while g|g \\+=|abs\\(\\(h - g\"),\"S35\":(\"contract.py\",r\"plan_ttl =|exp_ttl =|ttl = plan.expiry\"),\"S36A\":(\"kernel_a.py\",r\"self.hold_end = ts\"),\"S36B\":(\"nautilus_adapter.py\",r\"hold_end =|cutoff = min\"),\"S37\":(\"kernel_a.py\",r\"first_expected =|o - prev|return prev \\+|prev \\+ iv <\"),\"TTL-deadline-A\":(\"kernel_a.py\",r\"deadline =\"),\"TTL-deadline-B\":(\"nautilus_adapter.py\",r\"deadline =\")}\nfor label,(file,pattern) in patterns.items():\n    for i,line in enumerate((pathlib.Path(\"src/quant_lab/market\")/file).read_text().splitlines(),1):\n        if re.search(pattern,line):print(label,file+\":\"+str(i),line.strip())\n")'`

P10L复现两条lint红绿及B17缺版本诊断漏检：
`.venv-g2/bin/python -c 'exec("import inspect\nfrom unittest.mock import patch\nfrom tests.market import test_outcome_kind as t\nfrom quant_lab.market import execution as x\ndef out(fn):\n    try:fn();return \"GREEN\"\n    except BaseException as e:return \"RED \"+type(e).__name__\nreal=inspect.getsource\nfor name,token in [(\"test_lint_forbidden_patterns_do_not_reappear\",'"'"'multiplier\"] or \"1\"'"'"'),(\"test_s27_s28_single_source_start_time_and_exact_grid\",\"(req.t_start or req.t_dec)\")]:\n    fn=getattr(t,name)\n    with patch.object(inspect,\"getsource\",side_effect=lambda obj:real(obj)+\"\\n# \"+token):\n        print(name,\"forbidden injected\",out(fn))\n    print(name,\"restored\",out(fn))\nsrc=real(x.check_policy_hash_consistency).replace(\"{r['"'"'policy_version'"'"']}\",\"REDACTED\")\nns=dict(x.__dict__);exec(compile(src,\"<missing-version-diagnostic>\",\"exec\"),ns)\nwith patch.object(x,\"check_policy_hash_consistency\",ns[\"check_policy_hash_consistency\"]):\n    print(\"B17 missing version diagnostic\",out(t.test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes))\nprint(\"B17 restored\",out(t.test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes))\n")'`

P10G独立网格数学与25组分区子集：
`.venv-g2/bin/python -c 'exec("import datetime as dt,itertools\nfrom quant_lab.market import contract as c\nfrom tests.market.test_partition_check import bars,rules,run,JAN\nimport polars as pl\ncount=0\nfor iv,base,au,bu in itertools.product((1,7,60,900,28800),(c.EPOCH-dt.timedelta(days=1),c.EPOCH,JAN),(-1,0,1,999999,60000001),(-1,0,1,999999,180000001)):\n    a=base+dt.timedelta(microseconds=au);b=base+dt.timedelta(microseconds=bu)\n    first=c.first_grid_point(a,iv)\n    points=[c.EPOCH+dt.timedelta(seconds=i*iv) for i in range(int((a-c.EPOCH).total_seconds()//iv)-2,int((b-c.EPOCH).total_seconds()//iv)+3)]\n    want=sum(a<=p<b for p in points)\n    assert c.grid_points_between(a,b,iv)==want\n    assert first>=a and first-a<dt.timedelta(seconds=iv) and (first-c.EPOCH)%dt.timedelta(seconds=iv)==dt.timedelta(0)\n    count+=1\nprint(\"helper brute\",count,\"PASS\")\nn=0\nfor au,bu in [(0,180000000),(1,180000000),(0,180000001)]:\n    a=JAN+dt.timedelta(microseconds=au);b=JAN+dt.timedelta(microseconds=bu)\n    expected=[i for i in range(4) if a<=JAN+dt.timedelta(seconds=i*60)<b]\n    for mask in range(1,1<<len(expected)):\n        keep=[i for j,i in enumerate(expected) if mask&(1<<j)]\n        df=bars(4).filter(pl.col(\"open_time\").is_in([JAN+dt.timedelta(seconds=i*60) for i in keep]))\n        out,qs,r=run(df,rules(a,b))\n        assert r.missing==len(expected)-len(keep),(au,bu,keep,r)\n        assert sum(g[\"n\"] for g in r.gaps)==r.missing,(au,bu,keep,r)\n        assert all(g[\"n\"]>0 for g in r.gaps)\n        n+=1\nprint(\"partition aligned subsets\",n,\"PASS\")\n")'`

P10F逐字段六输入枚举：
`.venv-g2/bin/python -c 'exec("from quant_lab.market import contract as c\nfrom tests.market.test_review_p1 import e03\nq,m=e03()\nmodels=[(c.ExecutionRequest,q.model_dump()),(c.OrderPlan,q.order_plan.model_dump()),(c.ExecutionPolicy,c.resolve_policy(q.policy_version).model_dump()),(c.Entry,q.order_plan.entries[0].model_dump()),(c.Stop,q.order_plan.stop.model_dump()),(c.TakeProfit,q.order_plan.tps[0].model_dump()),(c.Sizing,q.order_plan.sizing.model_dump()),(c.Expiry,q.order_plan.expiry.model_dump()),(c.CostSpec,c.CostSpec().model_dump())]\nfor cls,base in models:\n    for name,f in cls.model_fields.items():\n        if f.is_required() and \"None\" not in str(f.annotation):\n            continue\n        results=[]\n        for tag,val in [(\"omit\",None),(\"None\",None),(\"False\",False),(\"0\",0),(\"emptystr\",\"\"),(\"emptylist\",[])]:\n            d=dict(base)\n            if tag==\"omit\": d.pop(name,None)\n            else: d[name]=val\n            try:\n                z=cls.model_validate(d)\n                results.append(tag+\"=\"+repr(getattr(z,name)))\n            except Exception: pass\n        print(cls.__name__+\".\"+name+\" | \"+(\"; \".join(results) or \"all reject\"))\n")'`

T/R内存保护包装体如下，以python -后接相同pytest参数执行；不替换其他文件读写：
```python
import pathlib,sys,pytest
from unittest.mock import patch
target=pathlib.Path("src/quant_lab/market/policy_hashes.json").resolve()
rd=pathlib.Path.read_text; wr=pathlib.Path.write_text
mem=[rd(target)]
def read(p,*a,**kw):
    if p.resolve()==target:
        return mem[0]
    return rd(p,*a,**kw)
def write(p,data,*a,**kw):
    if p.resolve()==target:
        mem[0]=data
        return len(data)
    return wr(p,data,*a,**kw)
with patch.object(pathlib.Path,"read_text",read),patch.object(pathlib.Path,"write_text",write):
    sys.exit(pytest.main(sys.argv[1:]))
```

### P1 DoD判定与剩余风险

GOAL-2 §7的候选A≥10金标与不变量条件：本轮A22/22、T通过，满足本地证据部分。
A/B报告条件：本轮AB已实际出具22例分类，无未解释项；B定型继续留P2。
M-03..M-09全部verify：本轮没有重新运行真实网络M-03和独立看板脚本，不能新增“逐条全部重验”的声明。
Codex P1 review必修闭合条件：不满足，阻断S29/S31/S38/S39。
因此无论历史M-03状态如何，本轮都不能判pass。
剩余非阻断风险：S32/S33/S35/S37结构重复仍open；S34/S36与既有P2范围重叠。
未做产品修复、测试修复、真实湖全年体检及B资金账户独立定型：这些都超出本轮只写审查报告的范围。
本轮证据完整性指上述判定、反例和边界均有亲跑/源码枚举依据，不表示所有工程能力已完成。

### 源码时长表达审查附录

以下为P10S实际AST输出的时间相关行，保留定位，去除无关OHLC比较；长表达另经rg定位。
相同语句的父子AST节点会同时出现，不将它们误计为多份产品公式。
P10S原样复现命令：`.venv-g2/bin/python -c 'exec("import ast,pathlib,re\nfor p in sorted(pathlib.Path(\"src/quant_lab/market\").glob(\"*.py\")):\n    tree=ast.parse(p.read_text())\n    for node in sorted(ast.walk(tree),key=lambda n:getattr(n,\"lineno\",0)):\n        if isinstance(node,(ast.Compare,ast.BinOp,ast.BoolOp)):\n            expr=ast.unparse(node)\n            if re.search(r\"t_start|t_dec|horizon|ttl|holding|hold_end|deadline|staleness|grid|cal_from|cal_to|gap_h|total_seconds|calc_time\",expr) and len(expr)<240:\n                print(str(p)+\":\"+str(node.lineno)+\" \"+expr)\n")'`

```text
src/quant_lab/market/asof.py:180 stale > max_staleness_s
src/quant_lab/market/contract.py:262 t_dec + dt.timedelta(seconds=policy.latency_s)
src/quant_lab/market/contract.py:271 entry_ttl_s + (hold if hold is not None else policy.research_horizon_s)
src/quant_lab/market/contract.py:407 data.get('entry_ttl_s') is None and isinstance(plan, OrderPlan)
src/quant_lab/market/contract.py:407 data.get('entry_ttl_s') is None
src/quant_lab/market/contract.py:409 plan_ttl is None and pol is not None
src/quant_lab/market/contract.py:409 plan_ttl is None
src/quant_lab/market/contract.py:429 self.order_plan.expiry.entry_ttl_s is not None
src/quant_lab/market/contract.py:439 self.t_start is not None and self.t_start < self.t_dec
src/quant_lab/market/contract.py:439 self.t_start is not None
src/quant_lab/market/contract.py:439 self.t_start < self.t_dec
src/quant_lab/market/contract.py:447 self.horizon_end <= exp_t_start
src/quant_lab/market/contract.py:452 self.entry_ttl_s is None
src/quant_lab/market/contract.py:459 self.t_start is not None and self.t_start != exp_t_start
src/quant_lab/market/contract.py:459 self.t_start is not None
src/quant_lab/market/contract.py:459 self.t_start != exp_t_start
src/quant_lab/market/contract.py:463 plan_ttl is not None
src/quant_lab/market/contract.py:464 self.entry_ttl_s != exp_ttl
src/quant_lab/market/contract.py:465 plan_ttl is not None
src/quant_lab/market/contract.py:470 self.horizon_end - exp_t_start
src/quant_lab/market/contract.py:471 window > dt.timedelta(seconds=pol.max_horizon_s)
src/quant_lab/market/contract.py:475 self.horizon_source == 'policy' and window != dt.timedelta(seconds=derived_s)
src/quant_lab/market/contract.py:475 self.horizon_source == 'policy'
src/quant_lab/market/contract.py:499 self.t_start is not None
src/quant_lab/market/contract.py:519 plan.expiry.entry_ttl_s is not None
src/quant_lab/market/contract.py:520 horizon_end is not None
src/quant_lab/market/contract.py:521 horizon_end is None
src/quant_lab/market/contract.py:522 t_dec + dt.timedelta(seconds=policy.latency_s + derived_window_s(plan, policy, ttl))
src/quant_lab/market/contract.py:522 policy.latency_s + derived_window_s(plan, policy, ttl)
src/quant_lab/market/contract.py:762 e.ts in settle_keys
src/quant_lab/market/execution.py:153 req.resolved_t_start(_rp(req.policy_version)) - dt.timedelta(seconds=window_before_s)
src/quant_lab/market/execution.py:240 g >= a and (not any((abs((h - g).total_seconds()) <= 60 for h in have)))
src/quant_lab/market/execution.py:240 abs((h - g).total_seconds()) <= 60
src/quant_lab/market/kernel_a.py:202 b.open_time >= self.t_start
src/quant_lab/market/kernel_a.py:208 [p for p in m.last if self.t_start <= p.ts <= end] + [p for p in self._expanded(m.bars_last, self.policy.participation) if p.ts <= end]
src/quant_lab/market/kernel_a.py:208 self.t_start <= p.ts <= end
src/quant_lab/market/kernel_a.py:209 [p for p in m.mark if self.t_start <= p.ts <= end] + [p for p in self._expanded(m.bars_mark, None) if p.ts <= end]
src/quant_lab/market/kernel_a.py:209 self.t_start <= p.ts <= end
src/quant_lab/market/kernel_a.py:220 self.t_start <= f.calc_time <= end
src/quant_lab/market/kernel_a.py:222 self.t_start + dt.timedelta(seconds=self.req.entry_ttl_s)
src/quant_lab/market/kernel_a.py:223 deadline <= end
src/quant_lab/market/kernel_a.py:239 b.open_time >= self.t_start and b.open_time < end
src/quant_lab/market/kernel_a.py:239 b.open_time >= self.t_start
src/quant_lab/market/kernel_a.py:258 self.hold_end is not None and self.hold_end <= self.req.horizon_end and (self.hold_end not in self.moments)
src/quant_lab/market/kernel_a.py:258 self.hold_end is not None
src/quant_lab/market/kernel_a.py:258 self.hold_end <= self.req.horizon_end
src/quant_lab/market/kernel_a.py:258 self.hold_end not in self.moments
src/quant_lab/market/kernel_a.py:261 self.hold_end is not None and self.hold_end in self.moments
src/quant_lab/market/kernel_a.py:261 self.hold_end is not None
src/quant_lab/market/kernel_a.py:261 self.hold_end in self.moments
src/quant_lab/market/kernel_a.py:318 self.t_start + dt.timedelta(seconds=self.req.entry_ttl_s)
src/quant_lab/market/kernel_a.py:422 self.plan.expiry.max_holding_s is not None
src/quant_lab/market/kernel_a.py:423 ts + dt.timedelta(seconds=self.plan.expiry.max_holding_s)
src/quant_lab/market/kernel_a.py:568 ts in self.settled_keys
src/quant_lab/market/kernel_a.py:569 self.settled_keys[ts] != row.rate
src/quant_lab/market/kernel_a.py:573 mp is None or (ts - mp.ts).total_seconds() > self.policy.mark_max_staleness_s
src/quant_lab/market/kernel_a.py:573 (ts - mp.ts).total_seconds() > self.policy.mark_max_staleness_s
src/quant_lab/market/kernel_a.py:591 r.effective_from and self.t_start < r.effective_from or (r.effective_to and self.t_start >= r.effective_to)
src/quant_lab/market/kernel_a.py:591 r.effective_from and self.t_start < r.effective_from
src/quant_lab/market/kernel_a.py:591 r.effective_to and self.t_start >= r.effective_to
src/quant_lab/market/kernel_a.py:591 self.t_start < r.effective_from
src/quant_lab/market/kernel_a.py:591 self.t_start >= r.effective_to
src/quant_lab/market/kernel_a.py:595 not self.market.bars_complete and (self._first_bar_gap(self.market.bars_last, self.req.horizon_end) is None and self._first_bar_gap(self.market.bars_mark, self.req.horizon_end) is None)
src/quant_lab/market/kernel_a.py:595 self._first_bar_gap(self.market.bars_last, self.req.horizon_end) is None and self._first_bar_gap(self.market.bars_mark, self.req.horizon_end) is None
src/quant_lab/market/kernel_a.py:595 self._first_bar_gap(self.market.bars_last, self.req.horizon_end) is None
src/quant_lab/market/kernel_a.py:596 self._first_bar_gap(self.market.bars_mark, self.req.horizon_end) is None
src/quant_lab/market/kernel_a.py:616 (mo.hold_end or (self.hold_end is not None and ts >= self.hold_end)) and self.pos != 0
src/quant_lab/market/kernel_a.py:616 mo.hold_end or (self.hold_end is not None and ts >= self.hold_end)
src/quant_lab/market/kernel_a.py:616 self.hold_end is not None and ts >= self.hold_end
src/quant_lab/market/kernel_a.py:616 self.hold_end is not None
src/quant_lab/market/kernel_a.py:616 ts >= self.hold_end
src/quant_lab/market/kernel_a.py:619 mo.horizon and self.pos != 0
src/quant_lab/market/kernel_a.py:622 ts == self.t_start and (not self.orders)
src/quant_lab/market/kernel_a.py:622 ts == self.t_start
src/quant_lab/market/kernel_a.py:634 o.deadline is not None and o.deadline <= ts
src/quant_lab/market/kernel_a.py:634 o.deadline is not None
src/quant_lab/market/kernel_a.py:634 o.deadline <= ts
src/quant_lab/market/kernel_a.py:655 self.mark_ts is None or (ts - self.mark_ts).total_seconds() > self.policy.mark_max_staleness_s
src/quant_lab/market/kernel_a.py:655 (ts - self.mark_ts).total_seconds() > self.policy.mark_max_staleness_s
src/quant_lab/market/kernel_a.py:672 self.hold_end is not None and self.pos != 0
src/quant_lab/market/kernel_a.py:672 self.hold_end is not None
src/quant_lab/market/nautilus_adapter.py:146 list(market.last) + [p for b in market.bars_last if b.open_time >= t_start for p in expand_bar_b(b, req.path_scenario, plan.side)]
src/quant_lab/market/nautilus_adapter.py:146 b.open_time >= t_start
src/quant_lab/market/nautilus_adapter.py:148 list(market.mark) + [p for b in market.bars_mark if b.open_time >= t_start for p in expand_bar_b(b, req.path_scenario, plan.side)]
src/quant_lab/market/nautilus_adapter.py:149 b.open_time >= t_start
src/quant_lab/market/nautilus_adapter.py:150 b.open_time < t_start
src/quant_lab/market/nautilus_adapter.py:150 p.path_step == 'C' and p.ts <= t_start
src/quant_lab/market/nautilus_adapter.py:150 p.ts <= t_start
src/quant_lab/market/nautilus_adapter.py:153 market.funding and len({f.calc_time for f in market.funding}) != len({(f.calc_time, f.rate) for f in market.funding})
src/quant_lab/market/nautilus_adapter.py:153 len({f.calc_time for f in market.funding}) != len({(f.calc_time, f.rate) for f in market.funding})
src/quant_lab/market/nautilus_adapter.py:156 t_start <= p.ts <= end
src/quant_lab/market/nautilus_adapter.py:226 t_start + dt.timedelta(seconds=req.entry_ttl_s)
src/quant_lab/market/nautilus_adapter.py:237 deadline <= end
src/quant_lab/market/nautilus_adapter.py:419 self.fi < len(self.funding_rows) and self.funding_rows[self.fi].calc_time <= ts
src/quant_lab/market/nautilus_adapter.py:419 self.funding_rows[self.fi].calc_time <= ts
src/quant_lab/market/nautilus_adapter.py:422 row.calc_time < t_start or row.calc_time in self.settled_at
src/quant_lab/market/nautilus_adapter.py:422 row.calc_time < t_start
src/quant_lab/market/nautilus_adapter.py:422 row.calc_time in self.settled_at
src/quant_lab/market/nautilus_adapter.py:425 m.ts <= row.calc_time and m.path_step in ('none', 'C')
src/quant_lab/market/nautilus_adapter.py:425 m.ts <= row.calc_time
src/quant_lab/market/nautilus_adapter.py:426 not mk or (row.calc_time - mk[-1].ts).total_seconds() > policy.mark_max_staleness_s
src/quant_lab/market/nautilus_adapter.py:426 (row.calc_time - mk[-1].ts).total_seconds() > policy.mark_max_staleness_s
src/quant_lab/market/nautilus_adapter.py:426 row.calc_time - mk[-1].ts
src/quant_lab/market/nautilus_adapter.py:442 st['pos'] != 0 and (st['mark_ts'] is None or (ts - st['mark_ts']).total_seconds() > policy.mark_max_staleness_s)
src/quant_lab/market/nautilus_adapter.py:442 st['mark_ts'] is None or (ts - st['mark_ts']).total_seconds() > policy.mark_max_staleness_s
src/quant_lab/market/nautilus_adapter.py:442 (ts - st['mark_ts']).total_seconds() > policy.mark_max_staleness_s
src/quant_lab/market/nautilus_adapter.py:450 self.fi < len(self.funding_rows) and self.funding_rows[self.fi].calc_time <= until and (st['pos'] != 0) and (not st['censor'])
src/quant_lab/market/nautilus_adapter.py:450 self.funding_rows[self.fi].calc_time <= until
src/quant_lab/market/nautilus_adapter.py:453 row.calc_time < t_start or row.calc_time in self.settled_at or row.calc_time < (st['open_at'] or t_start)
src/quant_lab/market/nautilus_adapter.py:453 row.calc_time < t_start
src/quant_lab/market/nautilus_adapter.py:453 row.calc_time in self.settled_at
src/quant_lab/market/nautilus_adapter.py:453 row.calc_time < (st['open_at'] or t_start)
src/quant_lab/market/nautilus_adapter.py:453 st['open_at'] or t_start
src/quant_lab/market/nautilus_adapter.py:456 m.ts <= row.calc_time and m.path_step in ('none', 'C')
src/quant_lab/market/nautilus_adapter.py:456 m.ts <= row.calc_time
src/quant_lab/market/nautilus_adapter.py:457 not mk or (row.calc_time - mk[-1].ts).total_seconds() > policy.mark_max_staleness_s
src/quant_lab/market/nautilus_adapter.py:457 (row.calc_time - mk[-1].ts).total_seconds() > policy.mark_max_staleness_s
src/quant_lab/market/nautilus_adapter.py:457 row.calc_time - mk[-1].ts
src/quant_lab/market/nautilus_adapter.py:499 rules.effective_from and t_start < rules.effective_from or (rules.effective_to and t_start >= rules.effective_to)
src/quant_lab/market/nautilus_adapter.py:499 rules.effective_from and t_start < rules.effective_from
src/quant_lab/market/nautilus_adapter.py:499 rules.effective_to and t_start >= rules.effective_to
src/quant_lab/market/nautilus_adapter.py:499 t_start < rules.effective_from
src/quant_lab/market/nautilus_adapter.py:499 t_start >= rules.effective_to
src/quant_lab/market/nautilus_adapter.py:527 plan.expiry.max_holding_s is not None
src/quant_lab/market/nautilus_adapter.py:530 min(opens) + dt.timedelta(seconds=plan.expiry.max_holding_s)
src/quant_lab/market/nautilus_adapter.py:531 e['ts'] < hold_end
src/quant_lab/market/nautilus_adapter.py:559 plan.expiry.max_holding_s is not None and st['open_at'] is not None
src/quant_lab/market/nautilus_adapter.py:559 plan.expiry.max_holding_s is not None
src/quant_lab/market/nautilus_adapter.py:560 st['open_at'] + dt.timedelta(seconds=plan.expiry.max_holding_s) - dt.timedelta(microseconds=1)
src/quant_lab/market/nautilus_adapter.py:560 st['open_at'] + dt.timedelta(seconds=plan.expiry.max_holding_s)
src/quant_lab/market/nautilus_adapter.py:561 {m.ts for m in marks if t_start <= m.ts <= cutoff} | {p.ts for p in lasts if p.ts <= cutoff}
src/quant_lab/market/nautilus_adapter.py:561 t_start <= m.ts <= cutoff
src/quant_lab/market/partition_check.py:244 (pl.col(key) >= cal_from) & (pl.col(key) < cal_to)
src/quant_lab/market/partition_check.py:244 pl.col(key) >= cal_from
src/quant_lab/market/partition_check.py:244 pl.col(key) < cal_to
src/quant_lab/market/partition_check.py:252 df.height and grid_points_between(cal_from, df[key][0], sec) > 0
src/quant_lab/market/partition_check.py:252 grid_points_between(cal_from, df[key][0], sec) > 0
src/quant_lab/market/partition_check.py:266 df[key][-1] + step < cal_to
src/quant_lab/market/partition_check.py:321 df.height - df['calc_time'].n_unique()
src/quant_lab/market/partition_check.py:328 (ct[i] - ct[i - 1]).total_seconds() / 3600
src/quant_lab/market/partition_check.py:330 abs(gap_h - ih[i]) * 3600 > 60
src/quant_lab/market/partition_check.py:330 abs(gap_h - ih[i]) * 3600
src/quant_lab/market/partition_check.py:330 gap_h - ih[i]
src/quant_lab/market/vision.py:318 (b - a).total_seconds() // INTERVAL_SECONDS[interval]
```

### 本轮文件完整性

审前审后逐文件SHA256对比：market顶层文件及tests/market递归文件共72个，差异0。
该集合不包含本报告；不是原子文件系统快照，不对其他会话的仓库改动作归属判断。
原报告339行整体作为连续字节块保留，原SHA256见上；仅在其前增加十审章节，并在原末尾追加两行十审结语。
最后四行按用户A21修正规则保持九审两行、十审两行顺序，中间没有新章节或空行。

# G2 P1 九审（终审）
## 九审判定表
2026-09-11，GPT-6独立九审。按表行计：**closed 21 / partial-P2 8 / open 3**（含G2-SC-01，其与S26为同一缺陷的两个编号）；S01–S28为closed20/partial-P2 8/open0，新增S29–S31阻断P1。历史轮次正文与判定保留，当前以本表为准。

| ID | 状态 | 复现命令与输出摘要 | 是否阻断 P1 |
|---|---|---|---|
| S01 TP 当前可成交性 | closed | T:test_s01_tp_fill_requires_current_last_to_satisfy_limit 通过；A22/22，历史触及不授权回撤后成交。 | 否 |
| S02 funding 证据与账务 | partial-P2 | T:test_funding.py、test_c2_loader_missing_funding_and_unknown_rules 通过；AB仍声明U03/引擎余额unsupported。 | 否；变周期完整性与引擎账务留P2 |
| S03 分钟内部启动 | closed | T:test_s03_intra_bar_start_begins_at_next_bar_open、test_c3_s03_b_mark_bars_filtered_by_t_start 通过；P28五点通过。 | 否 |
| S04 持仓截止/TP 余量 | partial-P2 | T:test_c1_s04_hold_end_precedes_funding_and_b_truncates、test_s16_exposure_uses_observation_boundary 通过。 | 否；B账户/资金事件独立证明仍P2 |
| S05 覆盖/规则入口 | closed | T:test_s05_loader_unknown_quality、test_s05_partition_unknown_ohlc_is_quarantined 通过；缺列/null/false拒绝。 | 否；生命周期网格新反例单列S29 |
| S06 入湖冲突/bronze | partial-P2 | T:test_s06_conflicting_keys_quarantined_and_bronze_retained、test_s14_revised_source_supersedes_old_silver_days 通过。 | 否；深度重放/多来源版本化留P2 |
| S07 队列/post-only/账户 | partial-P2 | T:test_s07_post_only_cross_rejected_and_price_priority_and_margin_recheck、test_s07_management_stream_rejected_at_request_boundary 通过。 | 否；管理流/完整预留生命周期留P2 |
| S08 multiplier | closed | T:test_s08_multiplier_scales_pnl_fees_exposure、test_c1_s08_public_simulate_multiplier 通过。 | 否 |
| S09 as-of 等号/null/序号 | closed | T:test_s09_equal_candidate_null_kept、test_c1_s09_same_name_sequence_columns 通过；等号null不回退。 | 否 |
| S10 A/B 解释门禁 | partial-P2 | T:test_s10_classifier_exc_first_and_predicate_bound 通过；AB无未知码，余额证据仍not_run。 | 否；独立经济证明/B定型仍P2 |
| S11 测试/CLI 门禁 | partial-P2 | T 257 passed；R 44 passed；A22/22；B12/22；AB exit0，不能豁免P29–P31反例。 | 否；保持原P2边界 |
| S12 不可变输入/构建身份 | partial-P2 | T:test_s12_hash_sensitivity_and_stale_policy_hash_rejected、test_replay.py 通过；当前登记5/5匹配。 | 否；历史不可变登记/版本化湖仍P2 |
| S13 事件因果/精度/step | partial-P2 | T:test_c1_s13_closed_causality_precision_and_step、test_s13_event_precision_checked_by_invariants 通过。 | 否；独立容量/预留重放仍P2 |
| S14 源修订及逐行来源 | closed | T:test_s14_loader_unverifiable_source_rows_fail_closed、test_s14_revised_source_supersedes_old_silver_days 通过。 | 否 |
| S15 双流缺口/质量优先级 | closed | T:test_review_p1_round3.py 全5项通过；两流最早缺口、首尾缺和质量优先保持。 | 否；S29为分区体检新接缝 |
| S16 B hold 暴露窗口 | closed | T:test_s16_exposure_uses_observation_boundary、test_s16_horizon_equal_mark_does_not_extend_exposure 通过。 | 否；不扩大B定型验收 |
| S17 显式 fraction 一致性/精度 | closed | T:test_s17_explicit_fractions_cannot_bypass_policy_or_precision 通过；独立162组合18接受/144拒绝。 | 否 |
| S18 A15 无 expired 仍映到期 | closed | P18原样exit1/ContractError含完整事件集合；R:test_s18_all_non_match_branches_report_actual_event_set 通过。 | 否 |
| S19 显式 TTL 绕过 policy | closed | R:test_s19_explicit_ttl_cannot_bypass_policy_fallback 通过；独立16项6接受/10拒绝。 | 否 |
| S20 t_start 推导对账 | closed | R:test_request_validation 通过；显式启动偏移拒绝，推导值一致；观察窗下界另列S30。 | 否 |
| S21 删失优先级落实 | closed | R:test_s21_primary_censor_follows_contract_priority_not_check_order 通过；A/B均规则缺失优先、两coverage位false。 | 否 |
| S22 显式空 entry 分配被当缺省 | closed | P22原样exit1/ContractError长度不符；R:test_s22_explicit_empty_allocation_is_rejected_not_silently_filled 通过。 | 否 |
| S23 DF_DECIMAL 常量生效 | closed | R:test_constants_effective.py 8项通过；内存改DF_DECIMAL为(30,10)并reload，batch/event均随之变化。 | 否 |
| S24 B12 推导/必填未落实 | closed | P24原样输出432060.0/432060/True；合成政策无hold110、有hold160，缺research字段ValidationError。 | 否 |
| S25 horizon 亚秒绕过 | closed | P25原样exit1/ContractError；独立54次模型校验确认推导逐值对账与封顶精确。 | 否；下界另列S30 |
| S26 未知 multiplier | closed | P26采用P27内存表改multiplier=None/1：loader未知→A/B unevaluable/netNone；已知1→tp_hit/net5。 | 否；与G2-SC-01同一缺陷的审查编号 |
| S27 loader启动时间重复推导 | closed | P27原样exit0；省略/显式同一t_start均rules_known=True、bars_complete=True、tp_hit/net5。 | 否；仅闭合原loader反例 |
| S28 A首bar网格亚秒截断 | closed | P28原样exit0；0/1/500000/999999/1000000µs均无删失、tp_hit/net5、14事件。 | 否；同族遗漏另列S29 |
| G2-SC-01 未知乘数证据门 | closed | P26实调loader+simulate(A/B)通过；与S26交叉对应，不重复宣称两个独立修复。 | 否 |
| S29 分区体检网格仍重复且降精度 | open | P29：完整两bar却首行gap=True/n=0；右界+1µs时expected3/actual4/missing−1。 | 是；M-04同族漏网，完整输入会被删失 |
| S30 观察窗下界漏推导启动时刻 | open | P30：latency60且省略t_start，窗口−1µs/0均接受；显式同值均拒绝；A产net0/B报错。 | 是；公共请求边界随表达方式分叉 |
| S31 B17 冲突门回归及诊断不完整 | open | P31：真实门会拒双哈希但不列冲突哈希；内存删除该门后现有test_b16仍PASS。 | 是；B17注入式回归/冲突取值要求未落实 |
## 九审独立证据与新增必修
命令目录quant-lab；全程PYTHONDONTWRITEBYTECODE=1，探针/CLI另设PYTHONPATH=src，pytest加-p no:cacheprovider。T=`.venv-g2/bin/python -m pytest tests/market -q`：257 passed/15.17s/exit0；R=`.venv-g2/bin/python -m pytest tests/market/test_constants_effective.py tests/market/test_outcome_kind.py tests/market/test_contract.py -q -v`：44 passed/1.78s/exit0，无skip/xfail。A/B=`.venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A`（B换末参数）：22/0/exit0、12/10/exit1，双方22例replay=True；AB=`.venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1`：exit0，MATCH12/命令延迟3/跳空1/同刻优先4/GTD1/流动性1，未知码/NOT_RUN/A_GOLD_FAIL均0，不写AB文件。
P27/P28从下方八审正文抽取反引号内原命令，用shlex.split+subprocess.run原样复跑，结果见表。P18/P22/P24/P25同法复跑。P26只把P27内存合成表multiplier改None/1，并分别调用A/B：未知均unevaluable/netNone，已知均tp_hit/net5；文件读取替身不冒充真实湖或联网证据。小政策ttl60/research50/max200/latency60，hold=None/100分别110/160秒；两分支×推导/封顶policy/封顶caller×偏移{−1,0,1,2,123456,500000,999998,999999,1000000}µs=54次完整model_validate，全符独立期望；缺research字段实际ValidationError。
B16独立核验：5个登记政策research_horizon_s均432000，content_hash逐条匹配；PAIR_KEY含policy_hash且用于simulate_batch重复检测，G3既有_ARM_KEYS含hash。登记表_placeholder_fields与_note明示占位及14d→3d→5d依据，最终bump义务保留。实际批量门在strict=True/False都拒双哈希；不把任意pl.concat自动视为已验收，B17未闭合部分见S31。数值取舍和部分出场estimand依用户范围交G0，不据此否决。
新helper数学：first_grid_point为ceil(a_us/iv_us)，grid_points_between为max(0,floor((b_us−1)/iv_us)−ceil(a_us/iv_us)+1)，b≤a返回0；半开区间端点正确。独立逐点枚举6000对：interval={1,7,60,180,300,900,1800,3600,28800,86400}，负epoch/epoch/2024基点、UTC/+09、边界前后1µs/999999µs、空/反向区间，计数及首点均无差异。初次探针把epoch秒误当网格索引导致OverflowError；修正探针输入后完成上述穷举，不计作产品失败。kernel_a._first_bar_gap和loader确实调用helper；全族并未统一，见S29。
S29（P1）：partition_check.py:240、250、255–260仍用时长整除及首bar>cal_from，自行实现网格而未调用新helper。上市在00:00:00.000001、完整bar为00:01/00:02、下线00:03：expected2/missing0，却首bar gap=True并生成n=0的BAR_GAP；起点整齐、右界00:03:00.000001、完整四bar时expected3/missing−1。补链路用check_bars真实输出作内存loader输入（规则有效至00:04，bar60/120/180，请求窗1µs至180s），loader=False/quality=True，A/B均BAR_GAP/netNone/事件0。此为族B的第三处且沿用重复公式；不是helper公式本身错误。验收须日历首点、半开计数、首尾缺口共用精确网格，并钉住非负missing与完整网格无gap。
P29最小只读复现（输出依次为对照3/0/全false；2/0/[true,false]/n0；3/−1/全false）：`.venv-g2/bin/python -c 'exec("import datetime as dt\nfrom tests.market.test_partition_check import bars,rules,run,JAN\nfor au,bu,first,n in [(0,180000000,0,3),(1,180000000,60,2),(0,180000001,0,4)]:\n    a=JAN+dt.timedelta(microseconds=au);b=JAN+dt.timedelta(microseconds=bu)\n    out,qs,r=run(bars(n,start=JAN+dt.timedelta(seconds=first)),rules(a,b))\n    print(au,bu,r.expected_rows,r.missing,out[\"gap_flag\"].to_list(),r.gaps)\n")'`。
S30（P1）：contract.py:441仍用horizon_end<=(self.t_start or self.t_dec)，后续:466只查上限/推导对账，caller下界未对resolved_t_start检查。P30正常模型验证：latency60、省略t_start，horizon=T+60−1µs或T+60均接受；显式T+60均ContractError；+1µs两者皆合法。负窗进入A得到net0及唯一closed（早于启动），B报ValueError(start was > end)；不把A该异常事件序列宣称合法标签。build_request的caller路径同受此验证器影响。验收须对解析启动时刻要求window>0，并覆盖省略/显式与−1/0/+1µs。
P30最小只读复现（政策仅进程内登记，不用model_copy绕过请求校验）：`.venv-g2/bin/python -c 'exec("import datetime as dt\nfrom unittest.mock import patch\nfrom tests.market.test_review_p1 import e03\nfrom quant_lab.market import contract as c,execution as x\nq,m=e03();t=q.t_dec\np=c.ExecutionPolicy.model_validate({**c.resolve_policy(q.policy_version).model_dump(),\"version\":\"audit-latency\",\"latency_s\":60})\nreg=c.load_policy_registry();reg[p.version]=p.content_hash\nwith patch.dict(c.POLICIES,{p.version:p}),patch.object(c,\"load_policy_registry\",return_value=reg):\n    for us in (-1,0,1):\n        for explicit in (False,True):\n            start=None\n            if explicit:\n                start=t+dt.timedelta(seconds=60)\n            try:\n                r=c.ExecutionRequest.model_validate({**q.model_dump(),\"policy_version\":p.version,\"policy_hash\":p.content_hash,\"t_start\":start,\"horizon_source\":\"caller\",\"horizon_end\":t+dt.timedelta(seconds=60,microseconds=us)})\n                print(us,explicit,\"ACCEPT\",r.horizon_end-r.resolved_t_start(p))\n                if us==-1:\n                    for k in (\"A\",\"B\"):\n                        try:\n                            z=x.simulate(r,kernel=k,market=m);print(k,z.net_pnl,[e.kind for e in z.canonical_events])\n                        except Exception as e:\n                            print(k,type(e).__name__,str(e))\n            except c.ContractError as e:\n                print(us,explicit,\"REJECT\",str(e))\n")'`。
S31（P1）：execution.py:109–115真实批量断言存在，合成双哈希输出在strict两值下均ContractError，但错误仅列版本名，没有B17要求的冲突哈希取值。test_outcome_kind.py:279–295先跑正常batch，随后对本地split重复实现group_by，最后只assert conflict.height>0，从未将冲突输入送入真实门并断言抛错。P31仅内存AST删除simulate_batch的conflict块：原测试PASS→删除门仍PASS→恢复PASS；删除门后的注入输出确为2行/2hash。故新增门回归无法捕获它声称防止的退化，违反B17(1)(3)/(2)，不能用现有全绿收口。验收应对实际门注入双hash并断言ContractError含版本及两hash，删除门则回归必须红。
P31最小只读突变复现（只改子进程函数绑定/AST，源文件与测试文件未改）：`.venv-g2/bin/python -c 'exec("import ast,inspect\nfrom unittest.mock import patch\nfrom tests.market.test_review_p1 import e03\nfrom tests.market.test_outcome_kind import test_b16_pair_key_carries_policy_hash_and_batch_rejects_split_brain as test\nfrom quant_lab.market import contract as c,execution as x\nq,m=e03();q2=c.ExecutionRequest.model_validate({**q.model_dump(),\"episode_id\":\"merge\"})\nold=x.result_row\ndef inject(req,res):\n    row=old(req,res)\n    if req.episode_id==\"merge\":\n        row[\"policy_hash\"]=\"0\"*64\n    return row\nfor strict in (True,False):\n    with patch.object(x,\"result_row\",side_effect=inject):\n        try:\n            x.simulate_batch([q,q2],markets={m.manifest_id:m},strict=strict)\n        except c.ContractError as e:\n            print(\"gate\",strict,str(e),\"hashes_listed\",q.policy_hash in str(e) and \"0\"*64 in str(e))\ntest();print(\"original PASS\")\ntree=ast.parse(inspect.getsource(x.simulate_batch));f=tree.body[0]\nremove=[n for n in f.body if isinstance(n,ast.If) and any(isinstance(z,ast.Name) and z.id==\"conflict\" for z in ast.walk(n))]\nassert len(remove)==1\nf.body=[n for n in f.body if n not in remove]\nns=dict(x.__dict__);exec(compile(ast.fix_missing_locations(tree),\"<memory-B17-mutant>\",\"exec\"),ns)\nwith patch.object(x,\"simulate_batch\",ns[\"simulate_batch\"]):\n    test();print(\"deleted gate: existing test PASS\")\nns[\"result_row\"]=inject\ndf=ns[\"simulate_batch\"]([q,q2],markets={m.manifest_id:m})\nprint(\"mutant output\",df.height,df[\"policy_hash\"].n_unique())\ntest();print(\"restored PASS\")\n")'`。
其余同族：20个请求字段逐项核对，原始输入全进request_canonical；两来源×entry/tp各9输入（省略/None/[]/()/[1]/[.5]/False/0/空串）162项=18接受/144拒绝，TTL两来源×8项=6接受/10拒绝。4终止位×3出场腿位×3成交状态×7删失值=2688选择器组合，差异0、160未命中异常均含完整实际事件集合；七值仅在选择器合成域可达，真实22夹具仍六值，C06不可达保持，不用异常对象充当金标。
静态横扫全部9个market Python模块101处or及时间转换/常量使用：derived_t_start被validator/resolved_t_start共用，但build_request:520仍另写latency加法（当前等价），下界残余见S30；derived_window_s两调用点同源，TTL在before/after/build三处当前一致。vision.expected_rows整日/月且支持interval整除日，未证实亚秒错误；B _ns保留微秒，funding 8h网格仍独立且属于S02既有变周期限制。DF_DECIMAL在内存改(30,10)并reload后batch/event均变；其余阈值/优先级/量化常量有调用和R生效测试。BAR_COLUMNS/FUNDING_COLUMNS/_KEY是旧描述残余，B order_rank归S13；未将死表谎称为已实现规则。check_decimal仍硬写38/12，与当前固定契约一致。
偏差与DoD：S29会系统性排除完整样本，S30使同语义请求的可评估性依表达方式而变；未发现本轮新helper引入未来决策特征、幸存品种筛选或凭据入口。八项partial-P2（S02/S04/S06/S07/S10/S11/S12/S13）原回归未见退化、不升级。GOAL-2 §7：M-03按G0亲跑解除；M-04–M-09现有本地测试、A≥10金标/不变量、AB报告有实跑，但M-04独立网格反例及M-06请求边界、B17门禁仍未闭合，P1 DoD不满足；不以B既有10例差异或G0数值/部分出场裁定否决。
证据身份与范围：HEAD=081f1468d366efec9421a6f649808c1eb6c09d43；market顶层*.*、tests/market递归*.py、fixtures/episodes/*.json及execution-interface/GOAL-2共54文件，按仓库相对路径排序连接“路径:sha256\n”，审前审后SHA256同为4d41651e8866dff79ff3e77fe137cd7598646ed3de81d3ec12a225fbba2432de（非原子快照）。必读文档/全部market模块/指定测试及AGENTS/GROK已读；主控GPT-6独立审查，不联网、不调用其他/收费模型、不触生产；只编辑本文件，pytest临时产物由tmp_path管理。八审至一审历史正文284行逐字保留，SHA256=cf948f92e0ea6941c420ce18386f5381c762ab6941d753a9d753a4bea07c728d。
## 八审证据与新增必修
命令目录 quant-lab；均设 PYTHONDONTWRITEBYTECODE=1，探针/CLI 另设 PYTHONPATH=src；pytest 加 -p no:cacheprovider。T=`.venv-g2/bin/python -m pytest tests/market -q`：253 passed/13.65s/exit0；R=`.venv-g2/bin/python -m pytest tests/market/{test_outcome_kind,test_constants_effective,test_contract,test_review_fixes}.py -q -v`：89 passed/4.92s/exit0，无 skip/xfail。A/B replay 原命令：A22/0/exit0、B12/10/exit1，双方22例 replay=True；nautilus_adapter report --reps 1：exit0，MATCH12/命令延迟3/跳空1/同刻优先4/GTD1/流动性1，无 UNEXPLAINED/NOT_RUN/A_GOLD_FAIL，未写 AB 报告。
P24 原样从七审正文抽取命令执行：exit0，259260.0 259260 True；无 hold 窗口确为60+259200。另以进程内登记政策 ttl60/research50/max200 独立核算，无hold=110、有hold100=160，build_request 与正常 model_validate 一致；缺 research_horizon_s 实际抛 ValidationError(missing)，没有默认值（不把模型层 ValidationError 误述成 ContractError）。构造与对账均调用 contract.derived_window_s。
P25 原样执行：exit1/ContractError，14d+1µs 被安全上限拒绝；原命令第一处即退出，caller 由补探针单独验证。hold=None/100 × 推导policy/封顶policy/封顶caller × 偏移{−1,0,1,2,123456,500000,999998,999999,1000000}µs=54次完整模型校验，全部符合独立期望；policy推导只收0，caller封顶只收−1/0。再穷举1..999999µs的 timedelta 比较域，逐项满足不等于推导值且大于封顶；此为比较运算穷举，不冒称999999次完整模型验证。
P26：在 P27 的内存 loader 输入中，固定显式合法 t_start，仅将规则 multiplier 改 None/1/2；None 时实际 rules_known=False，公共 simulate(A/B) 均 RULE_HISTORY_MISSING/netNone；1 时均 rules_known=True/net5，2保持已知（经济量/保证金随乘数变化）。未知值虽内部用占位 Rules() 承载，执行在 rules_known 门前删失，不能冒充乘数1正常标签。现有 test_s26 仅查源码，本轮用实际调用补足行为证据。
S27（open，P1）：同侧启动时刻分叉。contract.py:406/456 按 t_dec+latency 解析，A/B 调用 resolved_t_start；execution.py:133 却用 `(req.t_start or req.t_dec)` 装载并选规则，遗漏 latency。正常构造的等价请求（latency60，t_start=None 或显式T+60，horizon=T+120），同一份T+60起生效规则及完整窗内bar，前者 rules/bars=False、unevaluable/netNone，后者 True/True、tp_hit/net5。不是版本化湖或B定型问题；应统一使用已解析启动时间再减 window_before_s，并验证两种表达的输入窗/结果一致。
P27 完整只读复现（所有文件读取替身仅在进程内提供合成表，不是实际湖/联网验证；patch 在 simulate 前退出）：`.venv-g2/bin/python -c 'exec("import datetime as dt,json\nfrom pathlib import Path\nfrom unittest.mock import patch\nimport polars as pl\nfrom tests.market.test_review_p1 import e03\nfrom quant_lab.market import contract as c,execution as x,partition_check as pc\nq,_=e03();t=q.t_dec\np=c.ExecutionPolicy.model_validate({**c.resolve_policy(q.policy_version).model_dump(),\"version\":\"audit-latency\",\"latency_s\":60})\nreg=c.load_policy_registry();reg[p.version]=p.content_hash\nbase={**q.model_dump(),\"policy_version\":p.version,\"policy_hash\":p.content_hash,\"t_start\":None,\"horizon_source\":\"caller\",\"horizon_end\":t+dt.timedelta(seconds=120)}\nrules=pl.DataFrame([{\"instrument_id\":q.order_plan.instrument_id,\"effective_from\":t+dt.timedelta(seconds=60),\"effective_to\":None,\"tick_size\":\"1\",\"step_size\":\"1\",\"min_notional\":\"0\",\"multiplier\":\"1\",\"funding_interval_hours\":8,\"status\":\"TRADING\",\"source\":\"audit-synthetic\"}],schema=pc.RULES_SCHEMA)\ndef read(path,*args,**kwargs):\n    if \"fundingRate\" in str(path):\n        return pl.DataFrame({\"calc_time\":[t],\"funding_rate\":[0.0],\"funding_interval_hours\":[8]})\n    return pl.DataFrame({\"open_time\":[t+dt.timedelta(seconds=60)],\"open\":[100.0],\"high\":[105.0],\"low\":[100.0],\"close\":[105.0],\"volume\":[100.0],\"gap_flag\":[False],\"ohlc_valid\":[True],\"source_sha256\":[\"audit\"]})\nwith patch.dict(c.POLICIES,{p.version:p}),patch.object(c,\"load_policy_registry\",return_value=reg):\n    for start in (None,t+dt.timedelta(seconds=60)):\n        r=c.ExecutionRequest.model_validate({**base,\"t_start\":start})\n        with patch.object(Path,\"exists\",return_value=True),patch.object(Path,\"read_text\",return_value=json.dumps({\"source_sha256\":\"audit\",\"check_status\":\"ok\"})),patch.object(pl,\"read_parquet\",side_effect=read),patch.object(pc,\"load_rules\",return_value=rules):\n            m=x.load_market_from_lake(r,lake_root=\"/audit-memory-only\")\n        out=x.simulate(r,market=m)\n        print(start,r.resolved_t_start(p),m.rules_known,m.bars_complete,out.outcome_kind,out.net_pnl)\n")'`。
S28（open，P1）：A 内部对“首根可用完整bar”有两种实现：kernel_a.py:196–198 的展开精确比较 open_time>=t_start；:240–242 的缺口计算先 int(timestamp)，误把整分钟+亚秒当作整分钟，first_expected=t_start。完整双流bars在T/T+60/T+120，horizon=T+180，t_dec=T+{1,500000,999999}µs 时均 BAR_GAP/unevaluable/netNone/事件0；T及T+1s对照均tp_hit/net5/事件14。应以完整时间精度求网格ceil并与展开规则一致；这是S03原整秒反例之外的新亚秒反例，不是S25请求封顶修复失败。
P28 完整原生复现（正常 model_validate + 公共 simulate；无 policy/mock 绕验证）：`.venv-g2/bin/python -c 'exec("import datetime as dt\nfrom tests.market.test_review_p1 import e03\nfrom quant_lab.market import contract as c,execution as x\nq,m=e03();t=q.t_dec\nfor us in (0,1,500000,999999,1000000):\n    r=c.ExecutionRequest.model_validate({**q.model_dump(),\"t_dec\":t+dt.timedelta(microseconds=us),\"t_start\":None,\"horizon_source\":\"caller\",\"horizon_end\":t+dt.timedelta(seconds=180)})\n    bars=[c.Bar(open_time=t+dt.timedelta(seconds=s),o=100,h=105,l=100,c=105,volume=100) for s in (0,60,120)]\n    market=c.MarketView.model_validate({**m.model_dump(),\"last\":[],\"mark\":[],\"bars_last\":bars,\"bars_mark\":bars,\"bars_complete\":True})\n    out=x.simulate(r,market=market)\n    print(us,out.censor_reason,out.outcome_kind,out.net_pnl,len(out.canonical_events))\n")'`。
同族独立穷举：两来源×entry/tp各9输入（省略/None/[]/()/[1]/[.5]/False/0/空串）162项，18接受/144拒绝；TTL两来源×8输入16项，6接受/10拒绝。4种终止事件存在位×3种出场腿存在位×3成交状态×7删失值=2688项，独立首匹配期望差异0，160异常全部回报完整实际事件集合；七值在选择器合成域可达，真实22夹具仍仅六值，C06不升级。
同侧重复清单：TTL 在 before解析/after对账/build_request 三处，当前 None 分支一致；t_start 在校验/resolved_t_start/build_request/loader 四处，实质分叉见S27；A deadline 在 timeline/submit_entries 两处，当前一致；首完整bar在展开/缺口两处，见S28；OHLC 在 vision._parse_bars 与 partition_check.check_bars 两处，后者额外检查volume有限及null，loader必须经check_status，不据预体检mask宣称通过；DF边界check_decimal仍硬编码38/12，当前与DF_DECIMAL一致，后续改标度须同步。B pre_process/flush 资金费重复（flush不调引擎余额），增量账本/_replay_ledger/暴露重放三份，以及hold截断/暴露cutoff两处，均记录为既有S02/S04/S10/S13 P2风险，未证实本轮退化。A/B间sizing/TP/费用/funding/路径独立实现符合ADR，不计缺陷。
常量/截断审查：AST列出全部9模块103处or及int/round调用并结合全文审阅；新问题S28独立于G2自查。DF_DECIMAL进程内改(30,10)后reload execution，batch.net_R/event.price均Decimal(30,10)；六条constants_effective实跑通过，SETTLEMENT_QUANTUM/RATIO_QUANTUM/EXIT_LEG_ORDER/SPIKE_K/SPIKE_MIN_SAMPLES/INTERVAL_SECONDS/CENSOR_PRIORITY可追溯。BAR_COLUMNS/FUNDING_COLUMNS/_KEY为描述残余，OUTCOME_KINDS为目录，B order_rank沿S13；未把它们误报为已生效规则。settlement_quantum政策字段与固定金额常量同值，未声称支持另选量化。
偏差/边界：S27按是否显式写同一启动时刻改变样本可评估性，S28按亚秒相位错误排除有效样本，均会污染下游样本组成；未发现本轮修复新增未来决策特征读取、幸存品种筛选或凭据/私有API入口。八项partial-P2（S02/S04/S06/S07/S10/S11/S12/S13）原回归未退化。研究窗曲线v2已读（3d15.3%、5d6.0%）；按用户范围，数值待裁及后增§5.18配对/估值要求不作为本审阻断。
DoD（GOAL-2 §7）：M-03依G0亲跑已解除，不联网重验；M-04–M-09本地测试、A≥10金标/不变量及AB报告条件满足。M-10必修闭合因S27/S28不满足，P1 fail；S24/S25/S26闭合不抵消独立新反例，不因B既有差异或3d/5d数值选择判失败。
证据身份：HEAD 7353904；以quant-lab相对路径排序，market顶层*.*、tests/market递归*.py、episode/*.json共52文件，连接“路径:sha256\n”后SHA256=aec245a94e9d7b0ca3a99c4512fcc9ef3c63096fec5f775d083eeb135b7bdfd5；探针前后摘要相同，非原子快照，不证明历史未变。当前5个policy登记hash逐条匹配，历史迁移限制仍S12。
审查边界：必读文件与全部market/*.py已读；主控GPT-6独立完成，无其他模型调用。仅编辑本文件，未改代码或测试；新增探针全在进程内，授权pytest的临时产物由tmp_path管理；未联网、未做生产操作。七审至一审正文及历史判定逐字保留，顶部表为八审当前结论。
## 七审证据与新增必修
命令目录 quant-lab；全部设 `PYTHONDONTWRITEBYTECODE=1`，CLI/探针另设 `PYTHONPATH=src`，pytest 加 `-p no:cacheprovider`。T=`.venv-g2/bin/python -m pytest tests/market -q`：251 passed/14.49s/exit0；R=`.venv-g2/bin/python -m pytest tests/market/{test_outcome_kind,test_constants_effective,test_review_fixes,test_review_p1_round4,test_review_p1_round3}.py -q -v`：75 passed/6.66s/exit0，无 skip/xfail。表内 T:/R: 指对应命令中的测试选择器。
A/B=`.venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A`（B 换末参数）：A 22/0/exit0，B 12/10/exit1，双方22例 replay=True；AB=`.venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1`：exit0，MATCH12/命令延迟3/跳空1/同刻优先4/GTD1/流动性1，UNEXPLAINED/NOT_RUN/A_GOLD_FAIL=0；未重写报告。
原样六审 P18/P22（命令完整保留于下方历史段）均 exit1：P18 实际事件集合为 accepted/cancelled/filled/submitted/tp_triggered/working；P22 为 entry_fractions / tp_fractions 长度与计划不符。空事件、无closed、closed无出场腿三类异常均逐条核验，不把异常对象充作真实金标。
独立同族穷举：4种终止事件存在位×3出场腿存在位×3成交状态×7删失值=2688组合；按契约独立分支求期望，差异0，160异常全部比对完整sorted事件集合；真实22夹具六值可达，filled_closed只在合成close腿可达，维持C06。两来源×entry/tp各9输入（省略/None/[]/()/[1]/[.5]/False/0/空串）=162项，18接受/144拒绝；TTL两来源×8输入=16项，6接受/10拒绝；无TP的显式空分配正常保留。
静态逐项核对 ExecutionRequest 实际20字段（六审“22字段”计数不沿用），21个非法身份/枚举输入全拒；原始输入全进request_canonical，推导字段核对TTL/fractions/t_start/horizon。AST枚举market全部105处or表达式并审阅：datetime/对象缺省、首拒因、P2来源边界与新S25分开；空分区missing现保留None。无运行读取的BAR_COLUMNS/FUNDING_COLUMNS/_KEY为旧描述/残余，OUTCOME_KINDS为测试目录，不是阈值或分派规则；B order_rank仍归原S13。
S23：execution.py:26 的 DEC=pl.Decimal(*DF_DECIMAL) 被BATCH_SCHEMA及EVENT_STRUCT复用。仅进程内将c.DF_DECIMAL改(30,10)再importlib.reload(execution)，net_R与event.price均Decimal(30,10)，直接运行test_df_decimal_drives_batch_schema通过；原文件断言用pl.Decimal(*c.DF_DECIMAL)，非硬编码38/12。SETTLEMENT_QUANTUM/RATIO_QUANTUM/EXIT_LEG_ORDER/SPIKE_K/SPIKE_MIN_SAMPLES/INTERVAL_SECONDS/CENSOR_PRIORITY均有调用点及生效断言；未把原P2排序残余宣称完成。
B11正向：合法caller与policy能输出对应诊断，普通整秒篡改与超限拒绝；仅改horizon_end而事件不变时公共simulate(A/B)两条trace_hash均变化。caller经build_request的显式horizon_end参数自动标注，直接模型需声明；亚秒例外见S25。G3 config_id记账属G3，本审未代验。
S24（P1，contract.py:263、418、471）：B12规定无hold时仍为t_start+ttl+research_horizon_s，实际两处都只用research_horizon_s；不是数值待定问题。内存登记合成policy（ttl60/research50/max200），build_request得50s，契约110s在policy路径反遭ContractError；同一行情A的50s为right_censored/netNone，110s caller为tp_hit/net5。另research_horizon_s被宣告必填，模型却允许省略并补14d；应显式登记占位值。现有两条horizon测试把漏TTL的式子写成期望，不能证明合规。
P24最小原生复现：`.venv-g2/bin/python -c 'from tests.market.test_review_p1 import e03; from quant_lab.market import contract as c; q,_=e03(); p=c.resolve_policy(q.policy_version); plan=q.order_plan.model_dump(); plan["expiry"]={"entry_ttl_s":60,"max_holding_s":None}; row={k:getattr(q,k) for k in ("episode_id","graph_version","decision_snapshot_hash","t_dec")}; row["order_plan"]=plan; r=c.build_request(row,policy_version=p.version,policy_hash=p.content_hash,risk_budget=q.risk_budget,market_manifest=q.market_manifest); print((r.horizon_end-r.resolved_t_start(p)).total_seconds(),60+p.research_horizon_s,c.ExecutionPolicy.model_fields["research_horizon_s"].is_required())'` → 1209600.0 1209660 False；该默认本应触发上限拒绝，不能靠漏TTL逃过。
S25（P1，contract.py:414–420）：int(total_seconds())先丢微秒再对账/封顶，原datetime却进内核。hold=None/3600两推导路径各测−1µs/0/+1µs/+999999µs/+1s，均拒/收/收/收/拒；caller上限同五点为收/收/收/收/拒。修复须精确比较datetime/timedelta，不得接受偏移后继续标policy；上下界与对账需用同一时间精度。
P25最小复现：`.venv-g2/bin/python -c 'import datetime as dt; from tests.market.test_review_p1 import e03; from quant_lab.market import contract as c; q,_=e03(); p=c.resolve_policy(q.policy_version); d=q.model_dump(); d.pop("horizon_source"); d["horizon_end"]=q.resolved_t_start(p)+dt.timedelta(seconds=p.max_horizon_s,microseconds=1); r=c.ExecutionRequest.model_validate(d); print(r.horizon_source,(r.horizon_end-r.resolved_t_start(p)).total_seconds()); d["horizon_source"]="caller"; print(c.ExecutionRequest.model_validate(d).horizon_source)'` → policy 1209600.000001 / caller，exit0；两次均应拒绝。
policy迁移核验：5条当前content_hash与policy_hashes.json逐条相等且唯一；删除新增research_horizon_s后重算的旧schema哈希5条全不同，携这些旧哈希的请求5条全拒。版本键仍为原5个v1名；只能确认schema重登记与拒旧哈希生效，不能凭“全换哈希”宣称完整B4历史迁移合规。源码/注册表untracked且无旧登记快照，历史一一对应证明不足，保留S12既有partial-P2；本轮未证明旧已有数值被改且旧请求获准执行，不另升级阻断。
偏差/凭据：S24能改变标签成熟性，筛掉删失样本时可改变研究样本组成；S25允许观察边界外行情进入，是新增窗口口径漏洞。未发现本轮新增决策前视读取、幸存品种筛选或凭据/私有API入口；公开客户端可配置hosts并非硬网络隔离。S02/S04/S06/S07/S10/S11/S12/S13八条partial-P2回归未见退化，保持原归属，不借新问题重新升级。
收敛判断：S18/S22及S23已闭合，新增缺陷集中于B11/B12观察窗接缝，未再发现同族分配/映射扩散；但“接缝集中”不等于机制闭合。审查中另读到§5.15 B13数值裁定；依用户指定范围，不把后增取值/真实括号曲线要求作为本轮新门槛，S24/S25在§5.14内已成立。
DoD（GOAL-2 §7）：M-03按用户指令及§5.12记G0亲跑已解除，不计未决；本轮M-04–M-09本地测试、A≥10金标与不变量、AB报告均有实跑证据。M-10“必修闭合”因S24/S25未满足，故P1不通过；研究窗数值待定及B定型留P2均不构成本轮否决理由。
证据身份：HEAD 3a57094；market顶层文件+tests/market递归Python+episode JSON共52文件，按仓库相对路径排序连接“路径:sha256\n”后SHA256=8b99c0c8203f94a3d37698fe4a42a26146b3143433e7b9b43828f1d5c9533096；两次文件摘要比对无变化（非原子快照，不能证明历史未变）。
边界：必读文档、全部market/*.py与指定三测试文件已读；只写本文件，未改代码/测试，探针扰动仅在子进程内，pytest临时产物由其tmp_path管理；不联网、不调用收费模型、不做生产操作。历史六审至一审段逐字保留，原footer仅替换为本轮唯一裁决。
## 六审证据与未闭合项
命令目录 quant-lab；均设 PYTHONDONTWRITEBYTECODE=1，CLI/探针另设 PYTHONPATH=src；pytest 加 -p no:cacheprovider。T=`.venv-g2/bin/python -m pytest tests/market -q`：242 passed/14.83s/exit0；R=`.venv-g2/bin/python -m pytest tests/market/{test_outcome_kind,test_review_fixes,test_review_p1_round4,test_review_p1_round3}.py -q -v`：66 passed/6.10s/exit0，无 skip/xfail。
A/B=`.venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A`（B 换末参数）：A 22/0/exit0，B 12/10/exit1，双方22例 replay=True；AB=`.venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1`：exit0，MATCH12/命令延迟3/跳空1/同刻优先4/GTD1/流动性1，未知码/NOT_RUN/A_GOLD_FAIL=0；未重写 AB 文档。
原 N 从下方五审代码块抽取 Python 原样执行：IOC 事件均 submitted/accepted/cancelled/closed，按 B10 枚举④命中；86400 正好等于 fixture-zero-v1 默认 TTL，不能为了“后者被拒”误拒合法值；1≠86400 被拒。独立 TTL 的 plan/policy 两来源×None/相等/偏移/0/1.5 共10项，正常域与对账一致。
S18 残余位置 contract.py:569–570：无 closed 的有成交结果抛错文字仅“未删失且有成交却无 closed：违反不变量，无法映射 outcome_kind”，缺实际事件集合；空事件与 closed 无出场腿两条异常分支均附集合。已逐分支确认不再静默兜底，但 B10 要求所有未命中路径附集合，不能以异常对象为由豁免其诊断要求。
P18 复现：`.venv-g2/bin/python -c 'from tests.market.test_review_p1 import e03; from quant_lab.market import contract as c,execution as x; q,m=e03(); r=x.simulate(q,market=m); d=r.model_dump(); d["canonical_events"]=[e for e in r.canonical_events if e.kind!="closed"]; print(c.ExecutionResult.model_validate(d).outcome_kind)'` → ContractError/exit1，错误未含事件集合。
S22 位置 contract.py:358：before 验证器仅在某组为 None 时启动，随后 `data.get("entry_fractions") or ef` 把显式 []/() 吞掉；tp_fractions 已给则同一空 entry 会被长度检查拒绝。此为显式记录的输入校验漏洞，未证明可改变经济分配；与 S17 的非空伪造比例/超精度反例分开，不宣称哈希碰撞。
P22 复现：`.venv-g2/bin/python -c 'from tests.market.test_review_p1 import e03; from quant_lab.market import contract as c; q,_=e03(); d=q.model_dump(); d.pop("tp_fractions"); d["entry_fractions"]=[]; print(c.ExecutionRequest.model_validate(d).entry_fractions)'` → (Decimal('1'),)/exit0；应 ContractError。None/省略可解析，显式空列表不可替换。
P20/P21 独立内联探针使用 e03()+model_validate：t_start=t_dec+timedelta(seconds=s)，s∈{0,−1,1,45}；MarketView 的 rules_known 与 bars_complete/bars_quality_ok 同步布尔穷举后公共 simulate(A/B)。输出见表；正向均保留。相应可持久复现命令为 T:test_contract.py::test_request_validation、R:test_outcome_kind.py::test_s21_primary_censor_follows_contract_priority_not_check_order。
同族穷举独立于 G2 §7：逐项核对 ExecutionRequest 全22字段；TTL、两分配、t_start 为推导记录（发现 S22），policy_hash/合同版本受校验；cost/path/position 六种非法枚举或身份值均拒绝。episode/graph/snapshot/manifest、t_dec、plan、预算、seed 为原始输入，request_canonical 包含全 model_dump，未发现新增未入哈希输入。
映射穷举：6种事件存在位×3 fill_status×7删失值共1344组合，另 closed位×8出场腿集合×2有成交状态×7删失值共224组合，分支预期差异0；包含不变量非法对象，只用于异常/选择器边界，不充当真实金标。22夹具六值可达，filled_closed 仅合成 close 腿可达，C06 的 v0 不可达裁定保留；无兜底贴值，诊断遗漏见 S18。
定义/使用核验：CENSOR_PRIORITY 在 A 的 censor_now 与 B 入口 _censor 真正读取；CENSOR_REASONS/EVIDENCE_CENSORS/终态/成交集合与序列化列均核过。OUTCOME_KINDS 是导出/测试目录，非运行分派表，不算失效功能；B 的 order_rank 仍只定义不使用，动态删失也仍有直接赋值，不能宣称全 B 定型，归 S13/S02/S04/S10 原 P2 边界，未见本轮退化。
horizon_end 不新增缺陷编号：M01 R6 将其定义为观察截止，build_request 明确提供可选覆盖；请求缺少市场数据末端，不能强制等于 plan/policy 唯一推导值。独立1/3600/172800秒截止均接受且进入 request_canonical；max_holding 的运行时更早截止由现有回归验证。调用方选择观察窗可能改变删失样本，需固定研究口径，不等于新前视漏洞。
偏差/凭据与 DoD：已读全部指定文档、market/*.py、两测试文件及 AGENTS/GROK；未发现这四项修复新增前视、幸存筛选或凭据入口。M-03 依用户指令与 B10 §3 记 G0 实跑已解除（44640行/44640键/ok/vision_CHECKSUM/rc0），本轮不联网、不以 MockTransport 冒充；M-04–M-09 本地测试/A金标/AB报告通过，八项 partial-P2 不升级；M-10 因 S18/S22 必修未闭合，P1 fail。
证据身份：HEAD c209d8a；market 顶层文件+tests/market递归Python+episode JSON共51文件，按“仓库相对路径:sha256\n”排序连接的 SHA256=5d9e70557d54edeaab0a7cdc5c54781fb3773a1442e21c6b2d7357ff5c1b5445。源码/测试多为 untracked，不能据空 diff 证明历史断言未变；只对本轮实际读取与实跑负责。本轮仅写本文件，未改代码/测试、未调用其他模型或生产服务。
## 五审历史证据（原文保留）
命令均在 quant-lab，环境 `PYTHONDONTWRITEBYTECODE=1`，CLI/内联探针另设 `PYTHONPATH=src`；pytest 已禁 cacheprovider。T=`.venv-g2/bin/python -m pytest tests/market -q`：238 passed/15.19s/exit0；R=`.venv-g2/bin/python -m pytest tests/market/{test_review_fixes,test_review_p1_round4,test_review_p1_round3,test_outcome_kind}.py -q -v`：62 passed/11.09s/exit0，无 skip/xfail。
A/B=`.venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A`（B 替换末参数）：A 22/0/exit0，B 12/10/exit1，双方22例 replay=True；AB=`.venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1`：exit0，MATCH12/命令延迟3/跳空1/同刻优先4/GTD1/流动性1，未知码/NOT_RUN/A_GOLD_FAIL=0；默认仅输出摘要，本轮未重写 AB 文档。
D/Q/X 从下方四审 bash 代码块抽取 Python 原样执行；Q 1 passed/0.35s，另加原记录指定 ohlc-null 变体1 passed/0.17s；X 在首个非法 .5 如预期中断，随后逐值捕获异常确认超精度也拒绝。旧四审表是历史证据，勿用旧结果覆盖本轮结果。
A15：两类删失优先且不互串、rejected 优先、E12 exit_legs=(sl,tp) 保留混合；属性不读行情。22×2内核×2成本×3路径=264项扩展实跑，六值计数 stopped76/tp_hit104/unfilled_expired6/right_censored18/rejected36/unevaluable24，无 close 成交、无 filled_closed；不因 C06 已声明不可达判失败。但 S18 证明夹具之外的合法 IOC 结果不命中规格任何一条，contract.py:545–547 兜底伪装到期；需补齐规范与实现，不能仅放宽测试。已成交无 closed 的异常对象会抛 ContractError，不能声称对任意 schema 对象全函数。
哈希边界：exit_legs 由已入 trace 的事件派生，outcome_kind 代码随 contract.py 进入 A/B build_id；无新增可独立传入的派生字段后门。trace 本来不认证所有结果标量（含 censor_reason），不能据其单独认证外部篡改结果；此为既有 S12 边界。S19 不涉及碰撞：两请求 trace 不同，但同政策下能擅改入场期限，contract.py:391–393 只核对作者非空 TTL；需对 null 时 policy 兜底同样逐值核对。
历史反放宽核验：`git ls-files quant-lab/tests/market` 与 `git log --all --oneline -- quant-lab/tests/market` 均空；测试/22个 JSON 均 untracked，故历史未删改断言/期望的证明为 insufficient，不用空 diff 冒充未变。当前逐条读22个 derivation 与 expected，手算量价/费用/funding/R一致（E12=14/20=.7，E17=(8−.105)/5=1.579），未发现按内核输出生成金标；新增矩阵仅不变量/确定性，不冒充独立金标。四审整体摘要不能恢复逐文件旧内容。
偏差/凭据：读完 market/*.py；质量修复未知即隔离、尖刺仍只标、S16 按当时已成交均价及真实截止重放，未发现新增前视/幸存筛选或私有 API/凭据读取。S18/S19 会污染终态标签/可成交样本。只运行本地/MockTransport 测试，无网络、收费模型调用或生产操作；实际只写本文件，探针湖由 pytest tmp_path 承载。
DoD（GOAL-2 §7）：M-04–M-09 本地测试、A≥10独立期望/不变量、AB差异报告通过；M-03 网络 verify 因禁网记 insufficient，按指令不阻断。原八条 partial-P2 不升级；M-10 新增 S18/S19 必修未闭合，因此 P1 fail。已读指定文档及规则；原文件只有四审表与三审历史摘要，本轮不臆造不存在的三审表。
证据身份：HEAD dc0ddd3；market 顶层文件+tests/market递归Python+episode JSON，共51文件，按仓库相对路径排序连接“路径:sha256\n”的 SHA256=`1fda03a02f3baf1d36aea1c6cfbef190ffab24402fa9d6f5ad5c6a62c638d5cf`，排除本文件，不声称原子快照。
N（新增问题只读复现；正常 model_validate + 公共 simulate，无 model_copy 绕验证）：
```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv-g2/bin/python - <<'PY'
from tests.market.test_review_p1 import e03,pt
from quant_lab.market import contract as c,execution as x
req,mk=e03()
p=req.order_plan.model_dump(); p['entries'][0]['tif']='IOC'
q=c.ExecutionRequest.model_validate({**req.model_dump(),'order_plan':p})
m=c.MarketView.model_validate({**mk.model_dump(),'last':[pt(0,101),pt(60,101)],'mark':[pt(0,100),pt(60,100)]})
for k in ('A','B'):
    r=x.simulate(q,kernel=k,market=m)
    print('S18',k,r.outcome_kind,[(e.kind,e.reason) for e in r.canonical_events])
p['entries'][0]['tif']='GTC'; p['expiry']['entry_ttl_s']=None
m=c.MarketView.model_validate({**mk.model_dump(),'last':[pt(0,101),pt(60,100),pt(90,105)],'mark':[pt(0,100),pt(60,100),pt(90,100)]})
for ttl in (86400,1):
    q=c.ExecutionRequest.model_validate({**req.model_dump(),'order_plan':p,'entry_ttl_s':ttl})
    r=x.simulate(q,market=m)
    print('S19',ttl,q.policy_hash[:12],r.fill_status,r.net_pnl,r.outcome_kind)
PY
```

## 四审终版判定表

2026-09-11，GPT-6 主控；不采信已取消任务 task-mtwa8727-xsmsdn 的中间结论。先写入待验证表，再完成本轮复核。
**S01–S16：closed 6 / partial-P2 9 / open 1；加新增 S17 后：closed 6 / partial-P2 9 / open 2。**
三审原有八项 partial-P2 均保留；第九项是 S16（原反例修复，但暴露窗口过度截短）。
S05 的 OHLC 未知值放行及 S17 的 B8 请求边界阻断 P1；S16 不阻断。

| ID | 状态 | 本轮复现命令与输出摘要 | 是否阻断 P1 |
|---|---|---|---|
| S01 TP 当前可成交性 | closed | T:test_s01_tp_fill_requires_current_last_to_satisfy_limit 通过，回撤后不虚构第二笔；A 22/22。 | 否 |
| S02 funding 证据与账务 | partial-P2 | T:test_funding.py、R:test_c2_loader_missing_funding_and_unknown_rules 与 B 闭合 mark/重复冲突回归通过。 | 否；变周期完整性、真实入账、B 引擎余额留 P2。 |
| S03 分钟内部启动 | closed | T:test_s03_intra_bar_start_begins_at_next_bar_open、R:test_c3_s03_b_mark_bars_filtered_by_t_start 通过。 | 否 |
| S04 持仓截止/TP 余量 | partial-P2 | R:test_c1_s04_hold_end_precedes_funding_and_b_truncates 通过；D 的 A/B 截止后 MFE 均 0，X 发现 B 截止前暴露漏算。 | 否；B 残余见 S16，原 P2 边界不升级。 |
| S05 覆盖/规则入口 | open | R 的 BREAK/未知规则拒绝通过；D/X 的缺口删失通过；Q+ 将第二行 ohlc_valid 改 null 后 bars_complete=True、net=5、censor=None。 | **是**；未知 OHLC 质量仍被放行，不能声称质量入口完整闭合。 |
| S06 入湖冲突/bronze | partial-P2 | T:test_s06_conflicting_keys_quarantined_and_bronze_retained、Q 真实修订均通过，active 消失、load_partition=0。 | 否；bronze 深度重放、多来源版本化清单留 P2。 |
| S07 队列/post-only/账户 | partial-P2 | T:test_s07_*、R:test_c3_s07_equity_based_affordability 通过，静态超额开仓反例未退化。 | 否；管理流/E13/C01、完整预留生命周期留 P2。 |
| S08 multiplier | closed | R:test_c1_s08_public_simulate_multiplier 与 T:test_s08_multiplier_scales_pnl_fees_exposure 通过。 | 否；不替代 C02 政策 gate。 |
| S09 as-of 等号/null/序号 | closed | R:test_c1_s09_same_name_sequence_columns 与 T:test_s09_equal_candidate_null_kept 通过，合法等号 null 不回退。 | 否 |
| S10 A/B 解释门禁 | partial-P2 | T:test_s10_* 通过；AB 为 MATCH12、命令延迟3、跳空1、同刻优先4、GTD1、流动性1，无未知码。 | 否；冻结差异键/标量不等于独立逐事件经济证明，B 不定型。 |
| S11 测试/CLI 门禁 | partial-P2 | T 181 passed、R 14 passed、A 22/22、AB exit0，G3 evaluate 接缝在 T 内通过，无 skip/xfail。 | 否；完整成本×路径矩阵、B 跨进程/账户等仍 P2；新增反例不被全绿豁免。 |
| S12 不可变输入/构建身份 | partial-P2 | T:test_s12_*、test_replay.py、R:test_c3_s12_b_rejects_stale_policy_and_registry_pins_hash 通过；B8 字段进哈希但解析一致性见 S17。 | 否；版本化湖和供应链身份仍 P2，新增请求缺陷单列 S17。 |
| S13 事件因果/精度/step | partial-P2 | R:test_c1_s13_closed_causality_precision_and_step 通过；原样 D 两超精度数量均 ContractError；新增 fraction 精度漏检见 S17。 | 否；原八项边界保留，B8 新增接缝单独阻断。 |
| S14 源修订及逐行来源 | closed | 原样 Q 与 Q+：修订通过；缺列/null/旧 SHA（含各自加 gap）均 BAR_GAP、net=None，组合场景事件数0。 | 否；本条 bar 来源反例闭合，不宣称 funding 来源或版本化湖全验收。 |
| S15 双流缺口/质量优先级 | closed | D 双流反例由 net5 改 BAR_GAP；X 反序、两种单流首/尾缺均删失，quality=False+内部洞事件数0。 | 否；内核 min/质量优先级闭合，loader 未知 OHLC 归 S05。 |
| S16 B hold 暴露窗口 | partial-P2 | D 中 B MFE180→0；X 截止后 mark 改动不影响 A/B 事件和暴露，但 T+5 mark102、hold10 得 A MFE=.4/B MFE=0。 | 否；最后订单事件不等于观察截止，B 定型前必修。 |

命令均在 quant-lab，设置 `PYTHONDONTWRITEBYTECODE=1`；pytest 配置禁用 cacheprovider，探针湖写 pytest tmp_path，未写测试/业务文件。
必读：根 AGENTS.md/GROK.md、三审全文、closure 末两节、round3、指定四模块、契约 §5.9–§5.10、GOAL-2 §7；本次按指定工作区审查，不委派模型。
除本文件，仅按授权重生成 report-G2-kernel-AB.md。M-03 网络 verify 本次 **insufficient，非阻断**，未用 MockTransport 冒充联网实跑。

| 代号 | 复现命令 | 本轮输出 |
|---|---|---|
| T | `.venv-g2/bin/python -m pytest tests/market -q` | 181 passed in 12.26s，exit0。 |
| R | `.venv-g2/bin/python -m pytest tests/market/test_review_p1_round3.py tests/market/test_review_p1_round2.py -q -v` | 14 passed in 4.62s，exit0；round3 实际5项、round2 9项，closure 的“6例/180”非本轮计数。 |
| A/B | `.venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A`（B 替换末参数） | A passed22/failed0/exit0；B passed12/failed10/exit1；全部22例 replay=True。 |
| AB | `.venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1` | exit0，22个 episode；A_GOLD_FAIL/NOT_RUN/UNEXPLAINED=0；单次性能不作门槛。 |

按 GOAL-2 §7：A≥10 episode、不变量及本地 M-03–M-09 测试与 A/B 报告门槛满足；B 10例差异不单独否决 P1。但 S05/S17 未闭合，故不满足“必修闭合”。
报告仍有“adjust_account，余额已核”与“引擎余额 not_run”矛盾，本审采用后者，保留 S10 的既有 P2 限制。

## 四审新增问题

### S17（open，P1）：B8 显式解析字段可绕过 policy，并丢失 Decimal 精度

位置：contract.py:340–342、377–389；execution.py:68–72。请求仅检查长度/和/正值，以及作者显式给值的一致性；作者留空时不核对 `resolve_fractions(plan, policy)`，显式 fraction 也未调用 `check_decimal`。
X17 通过正常 `ExecutionRequest.model_validate`（非 model_copy 绕验证）接受同 policy_hash 下 TP 分配1与.5，两者均标 policy；A 净利从10变删失。显式字段应记录解析结果，不能成为隐藏的第三种分配来源。
同接口接受 `.5000000000001`；simulate_batch 将其截成 `.500000000000`，但 trace_hash 基于原始值。不同 trace 是预期的哈希敏感性，问题是请求违反12位精度，批量输出不能忠实复原参与哈希的输入。
修复验收：两组解析字段逐值等于 plan/policy 推导结果；校验有限值与 Decimal(38,12)，不一致/超精度一律拒绝；正常 build_request、A/B 和 batch 往返保持一致。此为本轮 B8 新增公共接缝，不受旧 P2 gate 豁免。
B8 正向证据：T 的等分末腿补余量、来源两值、混合来源、部分给出拒绝、哈希敏感性均过；真实 gold 三个 episode__fixture-v1*.parquet 各36行，其中25行有 plan 且 entry fraction 空，25/25 build_request 成功，无错误；未把合成单测注释当作真实25行验收。
§5.9 TTL nullable 回归通过。尚未发现本轮引入未来特征读取或凭据入口；S05 未知质量放行会污染标签/样本，S16 漏算窗内暴露，S17 能改变标签删失状态。
静态扫描 market/tests 的 services/API_KEY/私有路径，仅见既有公开 Vision URL、MockTransport 和禁止 services 的测试；T:test_no_services_import 通过。未联网、未访问生产配置；公开客户端可配置 hosts/redirect，不能声称硬网络隔离。
S05 补充复现（沿原编号）：在下述 Q 的 variant 列表加入 `("ohlc-null",original.with_columns(pl.Series("ohlc_valid",[True,None])))`；本轮输出 True/net5/None/notes=[]；对照 False 为 False/netNone/BAR_GAP。loader 的 `(~df["ohlc_valid"]).any()` 跳过 null，修复须将缺列/null/false 均视为质量失败。
S16 补充复现及修复：X16 中唯一合法窗内新 mark 是 T+5=102；B 以最后事件 T 截断，丢失 .4R 暴露；应按真实平仓/删失/hold/horizon 截止重放，不能按最后订单事件。保持 partial-P2，不把 B 独有缺陷升级为 P1。

### D / Q 原样复跑命令与三审对照

D：exit0；两非法 qty 仍 ContractError；双流由三审 None/net5/entry+TP 改 BAR_GAP/netNone/仅entry；A/B 均 LABEL_RIGHT_CENSORED、MAE/MFE=0、最大事件 ts=T；三审 B MFE=180。
```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python - <<'PY'
import datetime as dt
from decimal import Decimal as D
from tests.market.test_review_p1 import FIX,e03,pt,T0
from quant_lab.market import contract as c,nautilus_adapter as nb
from quant_lab.market.kernel_a import simulate_a
f=FIX['E03']
for q in ('1e-13','1e27'):
    try:
        c.OrderPlan.model_validate({**f.request.order_plan.model_dump(),'sizing':{'mode':'fixed_qty','qty':q}})
        print('S13 qty',q,'ACCEPTED')
    except c.ContractError as e:
        print('S13 qty',q,type(e).__name__)
def bar(s,p=100):
    return c.Bar(open_time=T0+dt.timedelta(seconds=s),o=D(p),h=D(p),l=D(p),c=D(p))
req,mk=e03()
req=req.model_copy(update={'horizon_end':T0+dt.timedelta(seconds=360)})
mk=mk.model_copy(update={'last':[],'mark':[],
    'bars_last':[bar(0),bar(60),bar(120,105),bar(180),bar(300)],
    'bars_mark':[bar(0),bar(120),bar(180),bar(240),bar(300)],'bars_complete':False})
r=simulate_a(req,mk)
print('S05 two_stream_gaps',r.censor_reason,r.net_pnl,
      [(e.ts,e.leg,e.price) for e in r.canonical_events if e.kind in c.FILL_KINDS],r.coverage_mask)
req,mk=e03(plan_upd={'expiry':c.Expiry(entry_ttl_s=3600,max_holding_s=10)},
    market_upd={'last':[pt(0,100),pt(60,105)],'mark':[pt(0,100),pt(30,1000),pt(60,100)]})
for k,sim in [('A',simulate_a),('B',nb.simulate_b)]:
    r=sim(req,mk)
    print('S04 exposure',k,r.censor_reason,r.mae_R,r.mfe_R,max(e.ts for e in r.canonical_events))
PY
```

Q：exit0、1 passed in 0.23s；真实修订 PASS；baseline True/net5/None；旧 SHA False/netNone/BAR_GAP；null/缺列由三审 True/net5/None 改 False/netNone/BAR_GAP；旧 SHA+gap 由 net5 改 netNone/BAR_GAP。
```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python - <<'PY'
import datetime as dt
import json
import pytest
import polars as pl
from tests.market.test_review_p1 import e03,T0
from tests.market.test_review_p1_round2 import test_s14_revised_source_supersedes_old_silver_days
from quant_lab.market import vision as v, execution as x, partition_check as pc
from quant_lab.market.kernel_a import simulate_a

def test_third_source_rows(tmp_path):
    revised = tmp_path / "revision" / "lake" / "market"
    revised.mkdir(parents=True)
    test_s14_revised_source_supersedes_old_silver_days(revised)
    print("S14 real revision PASS: superseded exists, active absent, load_partition rows=0")
    lake = v.LakePaths(tmp_path / "loader" / "lake" / "market")
    req,_ = e03()
    req = req.model_copy(update={"horizon_end":T0+dt.timedelta(seconds=120)})
    def frame(seconds):
        return pl.DataFrame({"open_time":[T0+dt.timedelta(seconds=s) for s in seconds],
          "open":[100. if s==0 else 105. for s in seconds],
          "high":[100. if s==0 else 105. for s in seconds],
          "low":[100. if s==0 else 105. for s in seconds],
          "close":[100. if s==0 else 105. for s in seconds],
          "volume":[100.]*len(seconds), "ohlc_valid":[True]*len(seconds),
          "gap_flag":[False]*len(seconds), "source_sha256":["current"]*len(seconds)})
    files = {}
    for typ,interval in (("klines","1m"),("markPriceKlines","1m"),("fundingRate","8h")):
        v.atomic_write_json(lake.manifest(v.partition_id(typ,interval,"BTCUSDT","2024-01")),
          {"partition_id":v.partition_id(typ,interval,"BTCUSDT","2024-01"),"source_sha256":"current","check_status":"ok"})
        if typ != "fundingRate":
            files[typ] = lake.silver_dir(typ,interval,"BTCUSDT")/"date=2024-01-01"/"part.parquet"
            df=frame([0,60])
            if typ=="markPriceKlines":
                df=df.with_columns([pl.lit(100.).alias(k) for k in ("open","high","low","close")])
            v.atomic_write_parquet(files[typ],df)
    rules=pl.DataFrame([{"instrument_id":req.order_plan.instrument_id,"effective_from":T0-dt.timedelta(days=1),
       "effective_to":None,"tick_size":"1","step_size":"1","min_notional":"0","multiplier":"1",
       "funding_interval_hours":8,"status":"TRADING","source":"synthetic"}],schema=pc.RULES_SCHEMA)
    pc.write_rules(lake,rules)
    original=pl.read_parquet(files["klines"])
    for label,df in [("baseline",original),
      ("second-row-stale",original.with_columns(pl.Series("source_sha256",["current","old"]))),
      ("second-row-null",original.with_columns(pl.Series("source_sha256",["current",None]))),
      ("source-column-missing",original.drop("source_sha256"))]:
        v.atomic_write_parquet(files["klines"],df)
        mk=x.load_market_from_lake(req,lake_root=lake.root)
        r=simulate_a(req,mk)
        print("S14 loader",label,"bars_complete",mk.bars_complete,"net",r.net_pnl,"censor",r.censor_reason,"notes",mk.quality_notes)
        if label=="second-row-stale":
            assert not mk.bars_complete and r.censor_reason=="BAR_GAP"
    # Same detected stale source + later time gap: bad first row must never fill.
    req=req.model_copy(update={"horizon_end":T0+dt.timedelta(seconds=240)})
    for typ in ("klines","markPriceKlines"):
        df=frame([0,60,180])
        if typ=="markPriceKlines":
            df=df.with_columns([pl.lit(100.).alias(k) for k in ("open","high","low","close")])
        else:
            df=df.with_columns(pl.Series("source_sha256",["old","current","current"]))
        v.atomic_write_parquet(files[typ],df)
    mk=x.load_market_from_lake(req,lake_root=lake.root)
    r=simulate_a(req,mk)
    print("S14 mixed-stale-gap",mk.bars_complete,r.censor_reason,r.net_pnl,mk.quality_notes)

class ReviewPlugin:
    def pytest_collection_modifyitems(self,session,config,items):
        parent=items[0].parent
        items[:]=[pytest.Function.from_parent(parent,name="test_third_source_rows",callobj=test_third_source_rows)]

raise SystemExit(pytest.main(["tests/market/test_review_p1_round2.py","-q","-s"],plugins=[ReviewPlugin()]))
PY
```

Q+：在 Q 的 mixed-stale-gap 场景，依次将 klines 来源设为 `["old","current","current"]`、`["current",None,"current"]`、删除来源列，保留 [0,60,180] 网格；三种实跑均 bars_quality_ok=False/BAR_GAP/netNone/事件0，1 passed in 0.33s。
X（补充缺口、S16/S17）：以下为本轮实跑场景的紧凑复现，X16 截止后 mark1000→5 时 A/B 的 mae/mfe/events 均不变；trace_hash 含市场摘要，不要求其对未来行情变更不变。
```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python - <<'PY'
from decimal import Decimal as D
from tests.market.test_review_p1 import e03, pt
from tests.market.test_review_p1_round3 import bars_req
from quant_lab.market import contract as c, execution as x
from quant_lab.market.kernel_a import simulate_a
from quant_lab.market.nautilus_adapter import simulate_b
full=[0,60,120,180,240,300]
for label,l,m in [('mark-first',[0,60,120,180,300],[0,120,180,240,300]),('last-first',[0,120,180,240,300],[0,60,120,180,300]),('last-head',full[1:],full),('mark-head',full,full[1:]),('last-tail',[0,60],full),('mark-tail',full,[0,60])]:
    req,mk=bars_req(l,m,360)
    r=simulate_a(req,mk)
    print(label,r.censor_reason,r.net_pnl,r.fill_status)
req,mk=bars_req([0,60,180,240],full,360)
r=simulate_a(req,mk.model_copy(update={'bars_quality_ok':False}))
print('quality-gap',r.censor_reason,len(r.canonical_events))
req,mk=e03(plan_upd={'expiry':c.Expiry(entry_ttl_s=3600,max_holding_s=10)},market_upd={'last':[pt(0,100),pt(60,105)],'mark':[pt(0,100),pt(5,102),pt(30,1000),pt(60,100)]})
for k,sim in [('A',simulate_a),('B',simulate_b)]:
    r=sim(req,mk)
    r2=sim(req,mk.model_copy(update={'mark':[pt(0,100),pt(5,102),pt(30,5),pt(60,100)]}))
    print('X16',k,r.mae_R,r.mfe_R,r.mae_R==r2.mae_R and r.mfe_R==r2.mfe_R and r.canonical_events==r2.canonical_events)
req,mk=e03(plan_upd={'tps':[c.TakeProfit(level=D(105))],'sizing':c.Sizing(mode='fixed_qty',qty=D(2))})
for frac in [D(1),D('.5'),D('.5000000000001')]:
    q=c.ExecutionRequest.model_validate({**req.model_dump(),'tp_fractions':(frac,)})
    r=x.simulate(q,market=mk)
    df=x.simulate_batch([q],markets={mk.manifest_id:mk})
    print('X17',frac,q.policy_hash[:12],r.fraction_source,r.net_pnl,r.trace_hash[:12],df['tp_fractions'].to_list())
PY
```

证据身份：HEAD `4bc8ea3fb3fbb14c3b4c037bd5e59fbd6d57caac`；工作区51文件集合 SHA256 `417040ee529dc09e5162b016811a6261c38c1868aa4cf5f1489d5989070f1c68`。
集合算法：market 顶层文件、tests/market 递归 Python 与 episode JSON、接口/GOAL-2/closure，按路径排序连接 `路径:文件sha256\n` 后 SHA256；排除本文件及生成报告，不声称原子快照。
四模块 SHA256：A `0e900cd7c8b04ef131b77fdc4b3354ac1b8cf2d5926a317109c21e2368419d55`；execution `002343cb7504fb334ef4e2fb4e0fed9dcfae33d6c4baefad76ceaddc99307260`；contract `1dfa3f70124f55ab5dca8e54b8de2034ecbcc5e4290f2a4caf8e28eda0f0bdbd`；B `1ab356b40d144813cf3dc9e5a51dad21c96b22aeda3cd14e5cd91c997ee58f59`。

## 历史轮次

一审 fail（closed 0 / partial 0 / open 13）；二审 fail（closed 1 / partial 12 / open 1）；三审 fail（S01–S14：closed 4 / partial-P2 8 / open 2，另列 S15/P1、S16/P2）；正文以 docs/adr/review-G2-P1.rounds-1-2.md 快照与本文件旧版 git 历史为准，不再内嵌全文。

证据完整性：完成
九审终裁：fail
证据完整性：完成
十审终裁：fail
证据完整性：完成
十一审终裁：fail
