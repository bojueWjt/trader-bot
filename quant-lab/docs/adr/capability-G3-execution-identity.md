# capability-G3-execution-identity：执行身份的能力边界与证据

范围决定：ruling-G0-R10-scope-and-acceptance.md

能力表：完成

> 本文件是 **R-10 的验收证据**，不是一轮审查。审查轮次见 `review-G3-P1.md`（43016 行，第 9–15 轮的工程反例**全部保持 open**，见下 §2「已知可漏检」栏）。

## 1. 基线身份

本文件的全部陈述**只绑定到下面这一份制品**。换制品即需重新出证。

| 项 | 值 |
|---|---|
| 制品身份（冻结源码 + 声明依赖） | `4055bdc516258d827eb5e5eaac21d42b038f07fa15cddfb04d383753613d0abd` |
| 冻结源码摘要 | `11ce7efcc1579173f480a7522dda6a57b09a9c6c32d3785c78f6dd2ea1982c40` |
| 依赖清单 | `{"python": "3.12.13", "polars": "1.44.2", "numpy": "2.5.3", "polars-ta": "0.5.17", "arch": "8.0.0", "scipy": "1.18.1", "pyarrow": "25.0.1"}` |
| 配置哈希（world + pipeline + B/alpha/L/delta/pi） | `5aa0933376c070fdc6aefacf89dbc5a7f06e76832791f0074e6ec45e19595697` |
| 报告文件哈希 | `bff829c9e4e632a58ae8687f87e9bfb87f31583efa95623064a9ffd9e88e47d1` |
| worker 回执数 / 结果行数 | 13 / 13 |
| worker 制品身份集合 | `['4055bdc516258d827eb5e5eaac21d42b038f07fa15cddfb04d383753613d0abd']` |
| 冻结流水线 block_len_days | `None` |

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
| 陈旧 `__pycache__` / 普通导入顺序导致的混版 | 每个 job 回传制品身份（冻结源码摘要 + 声明依赖清单），父进程逐份核对，任何不等或缺回执即拒绝落盘 | `test_proof2_worker_artifact_identity_must_equal_the_frozen_one`；突变 `test_proof2_mutation_without_the_check_it_would_publish` 证明去掉该门坏回执就放行 |
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
| 依赖包内部行为随版本变化 | 依赖清单只固定**版本号**，不校验包内容哈希；更换安装源或重打包不会被察觉 |
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

残余风险已由范围负责人正式接受，见 `ruling-G0-R10-scope-and-acceptance.md` §7。摘要：

| 项 | 内容 |
|---|---|
| P2 负责人 | G3 |
| 触发条件 | 任何 θ 声明进入资本配置或生产决策之前 |
| 完成判据 | 在独立配置的机器上由冻结制品重跑，按**结论层**比对（阳性计数与 Clopper–Pearson 区间落在同一判定侧），非逐位相等 |
| 不可用作 | 本接受书**不**授权把「机器检查通过」表述为「计算发生过」 |

## 6. 变更纪律

改动研究层源码或声明依赖即改变 §1 的制品身份，本文件的全部陈述随之失效，须重跑正式全量 MC 并重出本文件。
判读器或排版更新可重判既有原始结果，但**不得**为旧结果改盖生成身份。

