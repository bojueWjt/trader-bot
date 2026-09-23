# capability-G3-execution-identity：执行身份的能力边界与证据

范围决定：ruling-G0-R10-scope-and-acceptance.md

能力表：完成

> 本文件是 **R-10 的验收证据**，不是一轮审查。审查轮次见 `review-G3-P1.md`（43016 行，第 9–15 轮的工程反例**全部保持 open**，见下 §2「已知可漏检」栏）。

## 1. 基线身份

本文件的全部陈述**只绑定到下面这一份制品**。换制品即需重新出证。

| 项 | 值 |
|---|---|
| 制品身份（冻结源码 + 依赖清单） | `ed39a8fe00c1952c6f3e60feae1c0dd42de2c2abb8f13e9b7e359f7de55880e8` |
| 冻结源码摘要 | `f213c44670f1ad4d741d6e6bc3c93f9217737ccd83667c1203b383c548f072a3` |
| 依赖清单（版本 + **按实际文件字节的内容哈希**） | 见下表 |
| 配置哈希（规范 JSON：world + pipeline + B/alpha/L/delta/pi，取自报告 meta） | `4035193384091d61c4343e1c72bcc05fd095b1125b00175e56009d722c98c741` |
| 报告文件哈希 | `3ee9b3d879bbe319c800dd2dc851cec1db1ed2bd0f6535aef9c84bb09b0b42f8` |
| worker 回执数 / 逐 job 绑定回执数 / 结果行数 | 13 / 13 / 13 |
| worker 制品身份集合 | `['ed39a8fe00c1952c6f3e60feae1c0dd42de2c2abb8f13e9b7e359f7de55880e8']` |
| 父进程以本次新建的空 pyc 前缀启动 | `True` |
| 冻结流水线 block_len_days | `None` |

### 1.1 依赖内容身份（R-11）

**只记版本号不够**：版本串相同而包内容不同，可经**换安装源、重打包 wheel、直改 `site-packages`、跨平台轮子**四条路径发生，而这四条**都不需要向 worker 进程注入任何代码**——按 §2 的判据属**事故类**，必须挡住。
内容身份按**已安装文件的实际字节**计算：逐条读取每个分发的 **`RECORD`**，对其中带 sha256 的条目重读文件、按 wheel 规范重算摘要。缺失、不可读或与 `RECORD` 不符一律**具名拒绝**；`<dist>.content` 是全部 (相对路径, 实际摘要) 排序后的聚合摘要。不带哈希的条目（`RECORD` 自身、安装时生成的 pyc）不参与，陈旧 pyc 由执行侧的新建 pyc 前缀关闭（§2.1）。
覆盖范围是六个声明根依赖，加上它们在当前环境下生效的非 extra 依赖（递归展开）。例如 polars 的编译运行时 `polars-runtime-32` 由此进入清单；生效却未安装的依赖具名拒绝。每次调用都重读文件，不按 mtime、size 缓存，本机单次约 0.5 秒。
判别性证明见 `tests/research/test_dependency_content_identity.py`：等长改写文件字节、`RECORD` 与 mtime 都不变时拒绝，并附「只哈希 RECORD 文本」的突变对照；实际加载的编译扩展模块所属分发必须都在清单里，并附「只留六根」的突变对照。

| 依赖 | 版本 | 内容哈希（实际文件字节） |
|---|---|---|
| arch | `8.0.0` | `8bcb2647738c29b40e142dbd3fbb245d49dc658af645fe33804252915b55c980` |
| formulaic | `1.2.2` | `18803d565ad38acedc219dc4d7b68c0d274710d9f8cb9a0ffc115cf6962035f6` |
| interface-meta | `2.0.1` | `af6e019ff24494f18748790b47896a3e5056327650b946a5ef1b3a3de6c0d5b3` |
| llvmlite | `0.49.0` | `bb6f40cc23d3e86b3fba667305449890c6d41ec9ac2d41b8e06a902cd753ec93` |
| more-itertools | `11.1.0` | `04e7e4caaa0565715ecceca44220e20d07184c133dcd074f018765a26afdcb9f` |
| narwhals | `2.26.0` | `77e0cbe7078b335921eeb867042dc9f7e4e14cf1e4b14d00ebfcdda3031477cc` |
| numba | `0.67.0` | `980c3e0d7a8d852772addba4c25f0f107cee10f7b1dac2b331b328a0681cfcab` |
| numpy | `2.5.3` | `fbc110c748def3cd3aae29affba71a5d650d1addde9d06d050244694431939ed` |
| packaging | `26.3` | `5e2e488e37074de5477d271ef6d217f65ed97bca2642b4889d8304dfbe330f6f` |
| pandas | `3.0.5` | `17d5a0d472e220344355a1284ce79d59ca5f11423ede27817645c1f31acf05be` |
| patsy | `1.0.3` | `70a781837f16bb42e3ebed8f0d3378548c9d62095081ed23bda94086cd3a57f0` |
| polars | `1.44.2` | `05d2ed8397688828941cebe66fd6a2bb5352406e8617c0bdd518b1a4bb955b08` |
| polars-ols | `0.3.5` | `7c9284c1e240b8fc1a9fd1a829c449475d2e003f7d1f41a15bd070e21274e6e0` |
| polars-runtime-32 | `1.44.2` | `270d330552c48309047160bbf877a5f49494f354c30852ee49651d953d0aef22` |
| polars-ta | `0.5.17` | `3e800e015f2511108d4a310ce2842691ee1616996508095a8d04badd74ea644e` |
| pyarrow | `25.0.1` | `7a7b571f1bb0609691acfa8bc595e63294d2032ed6320667fd1241993d709a82` |
| python-dateutil | `2.9.0.post0` | `a60a650b5d274c66eb033dee1d7022c4e95ca520b77e3a8789b1140b7243dd01` |
| scipy | `1.18.1` | `20e7d6ab642f19198834168389a6ba4268e8bf5eacbb0d50738f6db585558f63` |
| six | `1.17.0` | `1355d5a0fc15010dcdc0db199af2554df141f46f86c07b7dece6d4fac8463a58` |
| statsmodels | `0.15.0` | `7b7e259b43f8c03a3dde845afb5e12d52796cc6c6e923e3d5c13703c2c989f49` |
| typing-extensions | `4.16.0` | `343c09d7046db9849c1aadcd77b7678fbf35e7f838c210aa50705d03a7370812` |
| wrapt | `2.4.0` | `317a9fb3b99f83f1304ff8af52969457e9b92385437ee4d65dc0f8fe08b2c58a` |
| （解释器） | `3.12.13` | — |

#### 比较的适用域（必读）

内容哈希并入制品身份后，**跨机器的制品身份可能不等**——平台轮子不同，内容就不同；相同的轮子也可能相等。**不等不自动意味着污染**：它的含义是「这可能是另一份制品」。因此——

> **worker 回执的身份相等性只在同一次运行内有意义**（整组 worker 由同一台机器 spawn 启动）。跨机器**不比较身份摘要**；§5 的 P2 完成判据本来就按**结论层**比对（阳性计数与 Clopper–Pearson 区间落在同一判定侧），不要求逐位相等。

身份摘要不但**覆盖集**要由被标识对象决定，**比较的适用域**也要一并声明——两者缺一都会让摘要被误用。

seed 分配（可供不生成本报告的一方独立重跑）：每机制 `seed0×100000+i`，各机制 seed0 与前五个 seed —

| kind | mechanism | seed0 | 前五个 seed | n |
|---|---|---:|---|---:|
| null | common_shock | 1 | `[100000, 100001, 100002, 100003, 100004]` | 1000 |
| null | cluster_heavy_tail | 2 | `[200000, 200001, 200002, 200003, 200004]` | 1000 |
| null | nonuniform_density | 3 | `[300000, 300001, 300002, 300003, 300004]` | 1000 |
| null | circular_shift | 4 | `[400000, 400001, 400002, 400003, 400004]` | 1000 |
| power | common_shock | 11 | `[1100000, 1100001, 1100002, 1100003, 1100004]` | 1000 |

## 2. 能力表（三分，均不为空）

### 2.1 已防住（事故类，结构性关闭）

判别一条反例属事故类还是对抗类，只问一句：**能不能在不向 worker 进程内注入代码的前提下复现**。

| 事故类型 | 关闭方式 | 判别性证明（各带突变自证） |
|---|---|---|
| fork 继承父进程模块对象 | 整组 worker 由全新解释器显式 `spawn` 启动 | `test_proof1_undeclared_state_does_not_cross_the_process_boundary`；突变 `test_proof1_mutation_fork_would_let_it_cross` 证明换成 fork 就会跨界 |
| 普通导入顺序 / 源码或依赖在运行前后被改导致的混版 | 每个 job 起跑与收尾各回传一次制品身份（冻结源码摘要 + 依赖清单），父进程逐份核对，任何不等或缺回执即拒绝落盘。**它查的是磁盘上的字节，不是执行的字节码**，陈旧 pyc 另见下一行 | `test_proof2_worker_artifact_identity_must_equal_the_frozen_one`；突变 `test_proof2_mutation_without_the_check_it_would_publish` 证明去掉该门坏回执就放行 |
| 陈旧 `__pycache__`（pyc 头的源码 mtime / size 与磁盘相符、字节码却是旧版） | 每次运行新建一个空的 pyc 前缀目录，整组 worker 在其下 spawn，读不到任何运行前就存在的 pyc；worker 在回执里报告自己实际的前缀，父进程逐 job 比对；CLI 父进程也以同样方式重启自己，报告记录 `parent_started_with_fresh_pycache_prefix`，判读侧要求为真 | 威胁实证 `test_mutation_without_a_fresh_prefix_the_stale_pyc_is_executed`（不设新前缀就执行陈旧字节码）；关闭 `test_a_fresh_prefix_executes_the_source_not_the_stale_pyc`、`test_workers_spawned_inside_the_context_run_under_the_fresh_prefix`、`test_cli_parent_reexecs_itself_under_a_fresh_prefix`；突变 `test_mutation_real_run_refuses_workers_that_did_not_get_the_fresh_prefix`（只建目录不交给 worker，正式入口拒绝落盘） |
| 结果与派发的 job 不对应（配置、输入、seed 或结果在过界后不一致） | 逐 job 绑定：父进程按派发内容、worker 按收到内容各算一份配置摘要、输入摘要、seed0 / n_rep，逐键比对；worker 跑完即对结果取摘要，父进程对收到的对象独立重算；绑定随报告落盘为 `job_receipts`，判读侧要求条数与结果行数相等 | `test_any_binding_mismatch_is_refused`、`test_result_changed_after_the_worker_hashed_it_is_refused`、`test_worker_binds_what_it_received_not_what_the_parent_meant`、`test_real_run_binds_every_job`；突变 `test_mutation_without_expected_binding_the_forgery_would_publish` |
| 活对象随配置跨进程带入 | 配置收窄为纯数据，worker 内重建；schema 外字段**拒绝而非忽略** | `test_proof3_undeclared_config_fields_are_refused_not_ignored` 与 `test_proof3_reaches_the_real_job_entry`；突变 `test_proof3_mutation_plain_construction_would_ignore_them` 证明绕开检查缺字段会被默认值悄悄补齐 |

### 2.2 已知可漏检（对抗类，**保持 open，不是闭合，也不是已豁免**）

以下均为第 9–15 轮审查给出的**可复现工程反例**。本系统**不声称防护**。
**共同复现前提：必须在 worker 进程内执行注入代码**（绑定驻留 globals、改注册表项、改类属性或默认参数绑定）。
对任意外部 Python callable，自动推导其完整行为依赖并判断状态差异是否影响未来行为，**不可判定**。

| 编号 | 载体 | 复现前提 |
|---|---|---|
| R9-H | 深度预算内静默编码相同：11 层嵌套配置里两份不同取值编码成同一串 | 需在 worker 进程内执行注入代码 |
| R10-H | 可调用实例的取值被限定名取代，实例状态不进身份 | 需在 worker 进程内执行注入代码 |
| R11-H1 | 私有槽名字改写后读不到、重名槽相互覆盖、继承来的 __call__ 不参与比较 | 需在 worker 进程内执行注入代码 |
| R11-H2 | 内置函数分派不可达、容器子类附加状态丢失、基本类型子类覆写 __repr__ 吞掉取值 | 需在 worker 进程内执行注入代码 |
| R11-H3 | 通用 repr 既不稳定也不无损；slice / numpy dtype / tzinfo 递归丢值 | 需在 worker 进程内执行注入代码 |
| R11-H4 | 类属性排除只按名字不按种类，同名 property 可借排除名单藏身 | 需在 worker 进程内执行注入代码 |
| R12-H2 | dataclass 非字段状态、deque.maxlen、method-wrapper 的 __self__、opaque C callable | 需在 worker 进程内执行注入代码 |
| R12-H3 | 同名不同偏移的固定时区、object 数组的 dtype metadata | 需在 worker 进程内执行注入代码 |
| R13-H1 | polars 类型本体（Int64 与 Float64 同为元类实例）、Field / Array / Struct 的实际取值 | 需在 worker 进程内执行注入代码 |
| R13-H2 | operator 取值器用 repr 编码，外部键 repr 可相同、集合参数随 hash 种子变 | 需在 worker 进程内执行注入代码 |
| R13-H3 | deque 子类的 maxlen、ctypes 回调的 C 层状态 | 需在 worker 进程内执行注入代码 |
| R13-H5 | 普通模块全局被当作可按名字代表全部状态的哨兵 | 需在 worker 进程内执行注入代码 |
| R14-Cat | polars Categorical 的 categories（name / namespace / physical 为方法而非属性） | 需在 worker 进程内执行注入代码 |
| R14-Ext | 外部 callable 读自己模块的全局或类属性，实例状态与字节码全同而行为不同 | 需在 worker 进程内执行注入代码 |
| R15-H1 | 依赖编码未接入函数值路径；co_names 不等于真正读取的全局；dataclass ClassVar 绕开 | 需在 worker 进程内执行注入代码 |

共 15 条，逐条保持 open。它们**退出 P1 发布判据**（范围重定），**不得**记为闭合、**不得**记为「已按 A39 免责」。「开放且出范围」与「闭合」是两回事。

### 2.3 未验证

| 项 | 为何未验证 |
|---|---|
| 跨机器数值复现 | 同制品不保证跨平台浮点逐位一致；本文件只对本机基线出证。独立重跑属 P2，按结论层比对 |
| 文件系统 / 环境变量 / 网络读取对结果的影响 | 正式运行只接受声明输入，但未穷举验证被批准代码自身是否读取隐式状态 |
| 完整配置 schema（P2） | P1 只覆盖当前协议实际用到的字段 |

## 3. 统计证据（绑定到 §1 基线）

**不把「测试全绿」当作来源完整性证明。** 下面是实际值与判定阈值。

| kind | mechanism | 阳性 x | 有搜索 n | 最坏界 95% CP | 阈值 | 判定 |
|---|---|---:|---:|---|---|---|
| null | common_shock | 2 | 842 | [0.07%, 1.04%] | 上界 ≤ 7% | **pass** |
| null | cluster_heavy_tail | 6 | 974 | [0.42%, 1.75%] | 上界 ≤ 7% | **pass** |
| null | nonuniform_density | 8 | 725 | [1.59%, 4.06%] | 上界 ≤ 7% | **pass** |
| null | circular_shift | 4 | 993 | [0.11%, 1.03%] | 上界 ≤ 7% | **pass** |
| power | common_shock | 327 | 768 | [39.05%, 46.16%] | 下界 ≥ 80% | **fail** |

四个空机制的最坏界上界最大为 **4.06%**（阈值 7%），全部通过。功效 δ=0.2R 为 327/768，下界 39.05%，**未达 80%**，按用户裁定的出口 A 如实记 fail 并附 §3.2 限制声明。

这些数字**只描述本基线上的合成世界**，不构成任何研究优势声明；`claim_status = descriptive_only`，真实数据声明仍需 G-STAT-CLAIM。

## 4. 使用限制

| 用途 | 是否可据本文件放行 |
|---|---|
| 内部研究、方法迭代、在合成数据上比较协议变体 | 可 |
| 把「机器检查通过」表述为「计算发生过」 | **不可**。机器判读只能保证报告内部自洽且由本基线制品生成 |
| 任何 θ 声明进入资本配置或生产决策 | **不可**，须先完成 §5 的 P2 项 |
| 在存在对抗方（能在 worker 进程内执行代码）的环境中作为来源保证 | **不可**，见 §2.2 |

## 5. 残余风险接受

残余风险的接受人是用户。2026-09-24，用户把这项决定委托给用户会话 `0f23fe43`（原话「你自己决策就行」「你收尾吧」），该会话代为接受，理由与记录见 `ruling-G0-R10-scope-and-acceptance.md` §13。G0 在 §7 的自行接受已被 §12 撤销，不作依据。摘要：

| 项 | 内容 |
|---|---|
| P2 负责人 | G3 |
| 触发条件 | 研究层输出用于**内部研究以外的任何用途**时；具体地，任何 θ 声明进入资本配置或生产决策之前 |
| 完成判据 | 在独立配置的机器上由冻结制品重跑，按**结论层**比对（阳性计数与 Clopper–Pearson 区间落在同一判定侧），非逐位相等 |
| 不可用作 | 本接受书**不**授权把「机器检查通过」表述为「计算发生过」 |

## 6. 变更纪律

改动研究层源码或声明依赖即改变 §1 的制品身份，本文件的全部陈述随之失效，须重跑正式全量 MC 并重出本文件。
判读器或排版更新可重判既有原始结果，但**不得**为旧结果改盖生成身份。

