# quant-lab 正式集成报告（OR-06）

报告日期：2026-09-24。证据读取基线：`be3cc5d2b73e212194a7f71ce41fb46e537967ba`。结论：**满足本报告 §9 的全部标准，可以完成 P1 研究层集成拼接**；研究层输出只作描述，任何 θ 声明均须另行授权。

本报告依据 [任务账本](taskList.json) 的 `integration.milestones.P1_主链路_合成数据`（下称 P1 记录）、[集成审查](docs/adr/review-G0-integration.md)、[执行身份能力证据](docs/adr/capability-G3-execution-identity.md)、[R-10 范围与接受裁决](docs/adr/ruling-G0-R10-scope-and-acceptance.md)及[空模型报告](docs/adr/report-G3-null-model.md)定稿。两份草稿的范围、门限、判定标准与缺口已并入本文；其中过时状态按现行证据更正。本次是文档整合与核验，不声称新执行了完整套件、逐条 gate 或 Monte Carlo。

## 1. 交付范围

交付 Telegram 消息到交易假设统计评估的**研究层合成数据主链路**：G1 产出消息与 episode，G2 提供行情及规范执行事件，G3 冻结机会集、构建特征、配对评估并记入尝试账本，G0 核对接缝及端到端冒烟。P1 不依赖真实聊天导出，也不授权生产使用；边界是不 import `services/*`、不使用生产凭据、不改生产。

| 窗口 | 模块 | P1 交付职责 |
|---|---|---|
| G1 | `data` | 消息归一、去重、抽取、规范化与行情校验、链接和双轨状态机、标注与分级放行工具；损耗表与隔离区 |
| G2 | `market` | Binance Vision 行情湖、分区体检、三时钟 as-of、`order_plan` 到规范执行事件的永续执行内核 |
| G3 | `research` | JSON AST 与算子前视契约、feature_snapshot、事件级配对评估、walk-forward、尝试账本、共同日历块 max-t 与空模型验收 |
| G0 | `contracts`、`tests/integration` | 契约与接缝仲裁、跨模块冒烟、集成审查及证据归档 |

现行 P1 gate 为 revision 2：D-03–D-07、D-09；M-03–M-09；R-03–R-09 逐项通过，OR-04 合成 θ 非空，各窗口审查必修闭合或有经用户授权且列明开放项与适用范围的替代验收，R-11 达到依赖内容身份标准。D-08 已获授权移出 P1，列入 P2；D-08、D-11、M-11 均为 gated 未启动。其余用户闸门的未批准出口见 §6。

## 2. 各模块状态与证据

### 2.1 提交、环境和输入绑定

**`be3cc5d` 上记录的实跑来源是 `6c073ae` 的隔离导出**。P1 记录 `evidence.sourceCommit` 为 `6c073ae`，`evidence.ranAt` 为 `2026-09-24T04:18:41Z`。两提交在 `quant-lab/` 范围的差异仅为 `taskList.json`，源码、测试及被引用审查/能力制品未变；因此以下是 `be3cc5d` 所承载的同源码证据，不是另一次在 `be3cc5d` 上新跑的结果。账本只提供该 `ranAt` 时间点，本文不补造逐命令起止时间。

环境取自 `evidence.environment`；包清单摘要方法均为 `importlib.metadata` 的 `name==version` 排序后 SHA256。它记录本次环境，不替代 R-11 的实际文件字节身份。

| 环境 | Python | 包数量 | 包版本清单 SHA256 |
|---|---|---:|---|
| `.venv-g0` | 3.12.13 | 104 | `e1535ccd28dcbe65f608ea982825f9f81b148c6addf7b1a5f1ae18dae6e7ac89` |
| `.venv-g1` | 3.12.13 | 75 | `d351a3afb0fcec95c2c3a1fb738b8ae9d8ad8660fc363819b583a485c0bf7c40` |
| `.venv-g2` | 3.12.13 | 35 | `1334e0eda60eabfe749106782e927329ad5dc605b8a243316a8d498ced48e779` |
| `.venv-g3` | 3.12.13 | 45 | `af3e470ff1f39746f252bb881557f2f1856075b8543da92477bf579c3e9cc1b7` |

输入取自 `evidence.inputManifest`：`quant-lab/data` 全树共 **6704 个文件**，按路径及字节计算的 SHA256 为 `449a984454700df7a2b9004a896ec5b2998250807fd7d422c660f3d55a849563`。逐条 gate 使用隔离数据根；报告不把该输入清单当作真实聊天数据授权。

### 2.2 四个套件

下表通过数逐项取自 `evidence.suites`，属于上述隔离对跑；不使用草稿或审查早期轮次的旧计数替代。

| 套件 | 通过数 | 证据键 |
|---|---:|---|
| `data` | **225 passed** | `evidence.suites.data` |
| `market` | **379 passed** | `evidence.suites.market` |
| `research` | **569 passed** | `evidence.suites.research` |
| `integration` | **100 passed** | `evidence.suites.integration` |

### 2.3 逐条 gate 的 rc

以下 **28 条**逐项抄自 `evidence.gates`；每行的实跑提交均为 `6c073ae`，读取与交付证据基线均为 `be3cc5d`。套件全绿不替代这些逐项返回码。

| gate | rc |
|---|---:|
| D-01 | 0 |
| D-03 | 0 |
| D-04 | 0 |
| D-05 | 0 |
| D-06 | 0 |
| D-07 | 0 |
| D-09 | 0 |
| D-10 | 0 |
| M-01 | 0 |
| M-03 | 0 |
| M-04 | 0 |
| M-05 | 0 |
| M-06 | 0 |
| M-07 | 0 |
| M-08 | 0 |
| M-09 | 0 |
| M-10 | 0 |
| R-03 | 0 |
| R-04 | 0 |
| R-05 | 0 |
| R-06 | 0 |
| R-07 | 0 |
| R-08 | 0 |
| R-09 | 0 |
| R-10 | 0 |
| R-11 | 0 |
| OR-01 | 0 |
| OR-04 | 0 |

OR-05 不在上述 28 条字典中：其 `rc=0` 另由 `evidence.integrationReview` 记录，对应集成审查 §13 六审封存 pass。[封存记录](docs/adr/review-G0-integration.verdict.json)明确 `round=6`、`verdict=pass`、`evidence_complete=true`，审查文件 SHA256 为 `1ca628dd61b4b35b72b5a13cda83a176bae9097de069a0098d7dfdf4246e808a`。

## 3. 接缝 S1–S9 核对

以下测试名称、结果和覆盖边界直接取自[集成审查 §8](docs/adr/review-G0-integration.md#8-接缝实跑证据索引)。表中的 data 225、market 379、research 565、integration 42 是**二审实跑时的套件计数**，不是 §2 最终隔离对跑的计数，也不是每条接缝各自的用例数。每行 PASS 只覆盖列出的断言；单侧证据不扩写成跨层端到端证明。

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

据此纠正原草稿：S5 是**每个 `policy_version` 恰好对应一个 hash**，并非整个批次只能一个 hash；S6 的七值包含 `unfilled_expired`、不包含 `cancelled`，不能把执行事件与结局标签混淆。这完成 A04 的文档修正，未修改契约。S4 的 null fraction 保留作者未知语义，但允许具名 policy 分配并入 hash。S7 的 `force_close_net_R` 是估值而非事件，**当前 G3 无消费方**，θ 消费差分不适用。

## 4. 冒烟输出与判别力

OR-04 在 §2 的隔离对跑中 `rc=0`，其所在 integration 套件为 **100 passed**。审查 §8 记录的 `test_e2e_theta_ledger_loss_quarantine` 从新建湖消费到冻结机会集及 evaluate，检查 θ 非空、尝试账本、损耗和隔离；`test_e2e_loss_table_and_quarantine_readable` 及 quarantine/mapping/loss 突变核对本批对象身份与完整性。时钟断言的 I06 已在二审闭合，不能再用一审的弱断言描述当前覆盖。

按原报告草稿、已知缺口草稿与审查 §1.1 的实测值，冒烟样本门的判别力如下。这些是已记录的冒烟测量值，不声称本次定稿重新测量了成交分布。

| 门 | 实际值 | 下限 | 余量 |
|---|---:|---:|---:|
| 成交样本 | 78 / 80 | 40 | 1.95× |
| 走完结局（`tp_hit` / `stopped` / `filled_closed`） | 65 / 80 | 20 | 3.25× |

**这是非退化门，不是回归门**：结局数从 65 降到 21 仍可全绿。套件通过不能解释为已锁定成交或结局基线；基线与容差带的债务见 §7。

研究层判别力取自[能力证据 §3](docs/adr/capability-G3-execution-identity.md#3-统计证据绑定到-1-基线)与[空模型报告 §2–§3.2](docs/adr/report-G3-null-model.md)。下表均按“有搜索 replicate”的条件最坏界计算，不拿 T0 的无搜索结果稀释风险。

| 类型 / 机制 | 阳性或检出 x | 有搜索 n | 最坏界 95% CP | 阈值与结果 |
|---|---:|---:|---|---|
| null / common_shock | 2 | 842 | [0.07%, 1.04%] | 上界 ≤ 7%，pass |
| null / cluster_heavy_tail | 6 | 974 | [0.42%, 1.75%] | 上界 ≤ 7%，pass |
| null / nonuniform_density | 8 | 725 | [1.59%, 4.06%] | 上界 ≤ 7%，pass |
| null / circular_shift | 4 | 993 | [0.11%, 1.03%] | 上界 ≤ 7%，pass |
| power / common_shock | 327 | 768 | [39.05%, 46.16%] | 下界 ≥ 80%，**fail** |

四空机制的条件最坏界上界最大 **4.06%**，阈值为 **7%**。空模型报告另有全体 replicate 最坏界上界 **2.95%**，分母不同，不用它替代条件验收。δ=0.2R 的条件功效点估计 **42.58%**、下界 **39.05%**，未达到 **80%**；全体 replicate 最坏界功效 **32.70%**、区间 **[29.80%, 35.71%]**。按用户裁定出口 A，以“功效报告 + 限制声明”收口，功效 fail 原样保留：未检出不能解释成无增益；真实研究的样本与噪声相当时须限制声明，扩样本另验；合成结果不代表真实频道功效。本文全部统计数值只描述，不作 θ 优势声明。

## 5. 里程碑判定

### 5.1 三条腿如何收口

| 腿 | 现行收口路径 | 证据性质与残余 |
|---|---|---|
| G1 / D-10 | 用户在 G1 会话经 `AskUserQuestion` 预授权替代路径；[G0 分类闭合裁决](docs/adr/ruling-G0-D10-classification-closure.md)经 verify 析取第二支通过，D-10 rc=0 | **裁决收口，非审查方 pass**。原审查及 r3/r4/r5 终裁均 fail；7 项 S08/S13/S14/V01/W01/W02/W03 保留。分类证据为三格突变证明、安全越权枚举覆盖、幸存偏差读侧无门列缺口，不能表述成五类全部有同等强度证明。 |
| G2 / M-10 | `review-G2-P1.md` 十四审终裁 pass；M-10 rc=0 | **审查方自行给出 pass**。10 项 partial-P2 与 `P2_UNSUPPORTED` 四键边界保留；M-11 gated 未启动。 |
| G3 / R-10 + R-11 | G0 范围重定裁决与具名能力证据；[裁决 §13](docs/adr/ruling-G0-R10-scope-and-acceptance.md#13-用户追认2026-09-24委托决定)记录用户会话 `0f23fe43` 代为追认并接受限定风险；R-10、R-11 均 rc=0 | **范围裁决加用户委托决定，非审查方 pass**。原十五轮审查均 fail，43016 行审查保留；第 9–15 轮 15 条对抗反例仍 open。残余风险接受不是用户逐条审阅，也不以 G0 原先的自行接受为依据。 |

G3 授权链须连读裁决 §12 与 §13：§12 撤销 §7 自行“正式接受”的效力并指出技术缺口；§13 根据用户“你自己决策就行”“你收尾吧。我只管用”“困难的工作你直接处理”的委托，由该会话代为决定范围及风险接受，限定只用于内部研究。授权只关闭授权轴，不能代替实现及测试。

### 5.2 历史撤回与重新成立

P1 记录 `declarationHistory` 保存 2026-09-12 的旧宣告及撤回。撤回原因包括：宣告用未提交工作树，交付 HEAD 上 R-08/R-10 递归不终止、R-11 ImportError，且依赖实际内容、运行时覆盖、pyc/逐 job 绑定及授权轴均未闭合。旧宣告的 **403 passed** 或之后工作树的 **407 passed** 不作为当前通过数。

当前 P1 记录为 `done=true`、`doneAt=2026-09-24`；撤回时七项已在 `withdrawalItems` 逐项记为 closed，关闭时间均为 `2026-09-24T04:18:41Z`：

| 撤回项 | 关闭依据 | 关闭轴 |
|---|---|---|
| P1-OPEN-1 | 裁决 §13 第 1 条，由用户委托的会话代为追认 R-10 范围重定 | 授权 |
| P1-OPEN-2 | 裁决 §13 第 2 条，代为接受 15 条对抗反例残余风险，仅限内部研究；外部用途前完成 P2 | 授权 |
| P1-OPEN-3 | `bf1fe55`：逐条重读 RECORD 所列文件的实际字节，缺失、不可读或不符具名拒绝；`test_dependency_content_identity.py` 含只哈希 RECORD 文本的突变对照 | 技术 |
| P1-OPEN-4 | `bf1fe55`：递归纳入当前环境生效的非 extra 依赖，共 22 个分发，含 `polars-runtime-32`；核实际加载扩展所属分发，附只留六根的突变对照 | 技术 |
| P1-OPEN-5 | `bf1fe55`：CLI 父进程和 worker 使用本次新建空 pyc 前缀；配置、输入、seed、前缀与结果逐 job 绑定；能力证据 §2.1 有威胁实证和突变 | 技术 |
| P1-OPEN-6 | `f0e8055`：实际运行研究代码按 origin `b8ee9bb` 原字节进入交付分支；交易侧整体分支对齐不属于 P1 | 提交归属 |
| P1-OPEN-7 | 从 `6c073ae` 隔离导出逐项验证；rc、环境和输入清单见 §2 及 P1 evidence | 可复现验收记录 |

[能力证据 §1](docs/adr/capability-G3-execution-identity.md#1-基线身份)绑定制品身份 `ed39a8fe00c1952c6f3e60feae1c0dd42de2c2abb8f13e9b7e359f7de55880e8`、冻结源码摘要 `f213c44670f1ad4d741d6e6bc3c93f9217737ccd83667c1203b383c548f072a3`、报告文件哈希 `3ee9b3d879bbe319c800dd2dc851cec1db1ed2bd0f6535aef9c84bb09b0b42f8`；worker 回执 / 逐 job 绑定回执 / 结果行数为 **13 / 13 / 13**，父进程新 pyc 前缀标志为 True。依赖覆盖六个声明根及其生效传递依赖，不能把环境包版本清单或 RECORD 文本摘要当作该证明。

### 5.3 OR-05 必修闭合

| 必修项 | 当前结论 | 集成审查证据 |
|---|---|---|
| I01 隔离根失效 | 闭合 | §7.1 二审 |
| I02 标记价消费晚到 bar | 闭合 | §7.2 二审 |
| I03 未知依赖可用时刻被忽略 | 闭合 | §7.3 二审 |
| I04 负 latency 前视 | 闭合 | §7.4 二审 |
| I05 冻结机会集、图与决策快照未绑定 | 闭合 | §10.1 三审；不把二审的未闭合状态沿用到最终结论 |
| I06 t_dec 断言接受全无效结果 | 闭合 | §7.6 二审 |
| I07 审查 gate 错误放行 / 上游 rc 丢失 | 闭合 | §13 六审；改用结构化封存协议，三至五审的 Markdown 解析根因不再适用 |

六审实际有 **35 个独立进程用例**：合法 pass 返回 0，**34 个拒绝用例全部返回 1**；当轮 integration **79 passed、rc=0**。六审新增必修 **0**、新增应改 **1**，历史应改 **4** 保持分类。其最终封存 pass 与 §2 后续 integration 100 passed 是不同时间的证据，不能混算。

## 6. 用户决策闸门状态

以下逐项来自 `taskList.json → integration.blockers`，六项均 pending；P1 的内部合成范围与以下出口相容，未批准不等于被本报告批准。

| 闸门 | 状态 | 影响任务 | 决策内容与未拍板出口 |
|---|---|---|---|
| G-ACCOUNT-SCOPE | pending | D-08、D-11 | 独立研究账号与频道范围（TDesktop 导出授权；单频道先行→五频道）。未批：仅合成夹具；禁止用生产 watcher 会话/凭据代替独立研究账号 |
| G-RETENTION-CLOUD | pending | D-05、D-08、D-11 | 云上传范围与数据保留/撤回合同（合并稿 H.1，逐对象批准，默认不上传）。未批：真实聊天记录不得进入 data/ |
| G-OC-200-3 | pending | D-09、D-11 | 一般错误 200/3 放行门与约 14.72% RQL 误收风险接受（或另预注册 (n,c)）；致命零容忍不变。未批：放行工具可做，真实批次放行不得执行 |
| G-BUDGET-LABOR | pending | D-05、D-09、D-11 | 真值人力与预算：主审核/独立复核、≤80 人时、API ≤300 美元、45 工程日为规划量。未批：不发起付费 API 批量调用与人工标注排产 |
| G-STAT-CLAIM | pending | R-08、R-09、OR-06 | 统计声明与风险容忍：冻结 V 最终前瞻窗口、尾损/回撤上限、成本压力档（κ/τ 只作场景）。未批：只描述不声明 |
| G-LICENSE-SEARCH | pending | R-05、R-09 | 许可与延后搜索：AlphaGen 代码复用需许可证据、DEAP 交付另核；GP/E 档位再议。未批：只做封顶枚举，不引入受限代码 |

`G-STAT-CLAIM` pending 与 readyForStitch 可以同时成立，前提是**研究层输出只作描述，任何 θ 声明都另行授权**。本报告不批准真实批次放行、付费排产、真实聊天接入、受限代码复用或资本配置。

## 7. 已知缺口表

本节合并已知缺口草稿，并以 §5 的新证据替换其中过时的 G3 六审未收口状态。旧 R6-G/R6-H/R6-L 不再作为现行未闭合必修重抄：空模型报告说明有限样本可实现性上界及计账规则，能力文档说明事故类身份防护；现行 G3 残余是 §8 所列对抗反例与以下未验证边界。

表中“债主”是偿还或维持限制的责任归属。原来源未明确责任人时，本文按模块职责列示；这不表示另行派发完成，也不表示用户接受了所有缺口。方法边界在触发时重申限制，不能承诺靠测试消除。A01–A03 仍为应改；A04 已在本报告修正。

### 7.1 方法边界

| 编号 | 缺口及现行边界 | 债主 | 触发事件与处置 | 来源 |
|---|---|---|---|---|
| 方法-计算来源 | 哈希与内部自洽不能证明计算发生过；全字段协调且数学上可能的伪造报告仍可通过 | G3；G0 核对声明 | 任何以机器检查作为计算来源保证的表述或对外用途前：删除该推论，按 §8 独立重跑并声明其边界 | 原缺口草稿方法边界；A39 |
| 方法-事故划界 | A39 对抗边界不能豁免普通导入缓存、陈旧 pyc、fork 混版等事故；当前支持域的防护见能力 §2.1 | G3 | 新增加载方式、运行依赖或发现无需进程内注入的反例时：按事故类修复并补判别性突变，不转入对抗免责 | 能力 §2.1；原缺口草稿 |
| 方法-可实现性 | 数学上不可能的统计值不在 A39 边界；仅用 `s²≤n/(n−1)(1−m²)` 必要界不足以替代有限 n 精确界 | G3 | 修改诊断统计量、样本分母或判读器时：核精确可实现性及逐对缺测总账，保留反例；不得用来源边界放行超界值 | 空模型报告 §4（R6-G / R7-G） |
| 方法-磁盘与执行 | 磁盘摘要不等于执行字节码摘要；执行侧依赖 spawn、空 pyc 前缀及逐 job 绑定，不证明任意驻留状态 | G3 | 源码、依赖或启动协议变动时：重跑正式 MC 并重出能力证据，分别声明摘要含义 | 能力 §2.1、§6 |
| 方法-独立复采样 | P1 未执行独立机器重采样对比；已公开 world/pipeline 与 `seed0×100000+i` | G3 | 研究输出用于内部研究以外任何用途之前：独立配置机器重跑冻结制品，按阳性计数与 CP 区间是否同判定侧比较 | 能力 §5；R-10 裁决 §13 |
| 方法-覆盖穷尽性 | 禁令、登记、差分互补但非穷尽；历史差分单独 1/4、三道合计 4/4，不能证明未测边界类 | G0 协调；G1/G2/G3 各自模块 | 新接缝、边界类或禁令进入验收时：逐类给出判别性突变；无突变的类按未覆盖声明 | 原缺口草稿；A32 / A36 |

### 7.2 范围与统计口径

| 编号 | 缺口及现行边界 | 债主 | 触发事件与处置 | 来源 |
|---|---|---|---|---|
| 范围-估值消费 | G3 尚未消费 `force_close_net_R`，调用点与登记均空，消费差分不适用 | G3 | 首次接入之前：登记真实调用并以哨兵证明 θ 随返回值改变 | 审查 §8 S7；原缺口草稿 |
| 范围-冒烟基线 | 非退化门：成交 78/80 对 40、余量 1.95×；结局 65/80 对 20、余量 3.25×；降到 21 仍过门 | G0 | P1 收口后：用实测值建立基线并评估容差带；引用早期 integration 8 passed 时必须同报本行数值 | 原缺口草稿；A46 |
| 范围-真实归档 | 真实归档只验 BTCUSDT 2024-01；bar 类 44640×2 行离网 0；funding 93 行离网 15（16.1%） | G2 | P2 扩品种或扩月前：补对应分区体检与时间网格证据 | 原缺口草稿 |
| 范围-M11 | M-11 gated 未启动；market 0.91 是按指令保留的边界 | G2 | D-11 种子清单可用且获准开展扩量后：执行种子全集覆盖与校准验收 | 原缺口草稿；任务账本 |
| 范围-D08 | D-08 gated 未启动，真实数据 PoC 已移出 P1 | G1；用户掌握授权闸门 | G-ACCOUNT-SCOPE、G-RETENTION-CLOUD 均批准后：接入获准研究导出并执行 PoC | P1 gateHistory；blockers |
| 范围-D11 | D-11 gated 未启动，五频道扩量不属于本次合成验收 | G1；用户掌握授权闸门 | 1a 过门且账号、保留、批放行及预算相关闸门批准后：执行扩量、损耗与批验收 | 原缺口草稿；任务账本 |
| 范围-读侧守恒 | `loss_table` / `quarantine` 直读 parquet，无读侧平衡恒等校验；S12 仅保证构建期，半写或手改可原样返回，属于事故面 | G1 | 读侧接入恒等校验或发布期封存时：补半写、陈旧与破坏恒等式的拒读证据 | D-10 裁决 §7.4；原缺口草稿 |
| 范围-路径逃逸 | 7 类逃逸向量均在取 hash 前拒绝，属于枚举覆盖，非任意路径的类级证明 | G1 | 新增路径入口、平台路径语义或逃逸向量时：扩充负例与拒绝位置证据；保持枚举措辞 | D-10 裁决 §7.3；原缺口草稿 |
| 统计-estimand | 部分出场主口径 B 为观察终点强平，A 为强制敏感性；G3 未消费估值函数的边界仍独立保留 | G3；G2 提供估值接缝 | G-STAT-CLAIM 决策及任何获准 θ 声明前：确认最终口径并明确所用 estimand、同步出敏感性 | 原缺口草稿 |
| 统计-研究窗 | `research_horizon_s` 为 5 天；3d/5d/7d 删失为 15.3%/6.0%/3.4%；每折须 Q×5 天，Q=20 时 100 天，2024-01 单月仅 5 块 | G2 + G3 | 基于 5d 作获准 θ 声明前：核每折日历长度和有效块数；样本不足即限制为描述，不靠改口径掩盖数据量 | 原缺口草稿 |
| 统计-功效 | δ=0.2R 下功效不足，327/768、条件下界 39.05% 低于 80%；按出口 A 限制声明收口而非达到原指标 | G3 | 任何获准 θ 声明或扩样本研究前：附空模型报告 §3.2 限制并按实测规模/噪声重估功效 | 能力 §3；空模型报告 §3.2 |
| 统计-最小样本 | 唯一口径 `maxt.calendar_blocks.n_nonempty`；与 `block_len` 成对，`block_len ≥ horizon + embargo`，同时钉 cross 与 max_span；最终具体下限尚未批准 | G2 + G3 提案；用户决策 | G-STAT-CLAIM 决策或 P2 真实评估前：提交成对数值提案并获得确认；此前不得作统计声明 | 原缺口草稿；审查 §8 S8 |

### 7.3 各窗口残余

G1 的下列 7 个编号是裁决保留的审查残余集合，不等于这 7 条原反例在当前版本都仍可复现。尤其 S08/V01/W02 的录制解码路径已由 `parse_float=Decimal` 的突变证明收口（去掉即静默改量 `-3E-12`）；这不把原独立审查 fail 改写为 pass。各行的统一来源是原缺口草稿与 [D-10 裁决 §6–§7](docs/adr/ruling-G0-D10-classification-closure.md)。

| 窗口 / 编号 | 保留事项 | 债主 | 触发事件与处置 |
|---|---|---|---|
| G1 / S08 | 经济量从录制输入到交付路径的可复算性覆盖边界 | G1 | 扩展录制格式或经济量路径前：核真实解码入口的逐字往返与突变证据 |
| G1 / S13 | 离线 OCR / LLM 证据协议的完整覆盖 | G1 | 接入真实媒体或新证据字段前：补合法证据正例、拒答边界及精度验证 |
| G1 / S14 | 正式标注及放行记录的语义覆盖 | G1 | 获准执行真实批次放行前：核标注内容而非仅字段存在，并保留拒收负例 |
| G1 / V01 | 全链经济量精度约束，不能由局部 parser 测试外推 | G1 | 增加转换路径前：逐路径核 Decimal 声明精度和输入输出保真 |
| G1 / W01 | 合法数量类证据的离线抽取路径覆盖 | G1 | 修改 LLM 数量证据协议或启用真实抽取前：补合法数量证据正例与数值类型兼容验证 |
| G1 / W02 | JSON 数值录制解码残余的独立审查记录保留 | G1 | 修改录制解码实现前：保持实际入口 parse_float=Decimal，并证明去掉即变红 |
| G1 / W03 | 关键字段齐全但值空的正式审核语义验证 | G1 | 获准真实抽审或全审前：提供关键标签空值不能签字 pass 的负例 |

G2 以下 **10 项 partial-P2** 取自原缺口草稿和任务账本 G2 十四审收口记录；边界保持，`P2_UNSUPPORTED` **四键**并未因 pass 消失。细项语义参见[市场审查](docs/adr/review-G2-P1.md)，下列触发条件不等于额外放行。

| 窗口 / 编号 | 保留事项 | 债主 | 触发事件与处置 |
|---|---|---|---|
| G2 / S02 | funding 变周期完整性、真实账务与 B 引擎余额 | G2 | 扩大资金费支持域或把 B 用于账务证明前：补真实结算与独立余额证据 |
| G2 / S04 | 持仓截止及 TP 余量的 B 账户/资金事件证明 | G2 | B 定型或扩大持仓边界支持前：补独立经济与截止证据 |
| G2 / S06 | bronze 深度重放、多来源版本化 | G2 | 接入修订源或多来源湖前：验证深度重放及版本化清单 |
| G2 / S07 | 管理流和完整预留生命周期 | G2 | 启用管理流或完整账户模型前：补队列、预留释放与生命周期验收 |
| G2 / S10 | A/B 独立逐事件经济证明，B 未定型 | G2 | 用 B 作为独立经济参照前：补余额与逐事件证明，不以差异解释表代替 |
| G2 / S11 | 完整成本×路径矩阵及 B 跨进程/账户验证 | G2 | 扩大成本/路径场景或 B 支持域前：执行完整矩阵与独立重放 |
| G2 / S12 | 历史不可变登记、版本化湖与供应链身份 | G2 | 迁移 policy 或交付跨版本重放前：保存历史登记并验证不可变映射和输入身份 |
| G2 / S13 | B 因果、容量与预留独立重放 | G2 | B 定型或引入容量/预留约束前：补事件因果和精度重放 |
| G2 / S34 | funding 独立网格与变周期支持边界 | G2 | 支持新结算周期前：统一网格语义并补真实网格证据 |
| G2 / S36 | 多处持仓截止计算及 B 定型边界 | G2 | 修改截止口径或 B 定型前：核各实现一致性及独立截止重放 |

| 窗口 / 范围 | 保留事项 | 债主 | 触发事件与处置 |
|---|---|---|---|
| G3 / 对抗身份 | 第 9–15 轮 15 条反例逐条保持 open，编号及载体见 §8；不是闭合或已豁免 | G3；用户掌握使用边界决定 | 任一输出用于内部研究以外用途之前：完成独立机器重跑的 P2 项；若引入能注入 worker 的对抗方，须重新定义防护和验收，不依赖本报告放行 |
| G3 / 隐式状态 | 文件系统、环境变量、网络读取影响未穷举验证 | G3 | 新批准代码或运行环境进入协议前：核声明输入及隐式状态依赖，补验证并保留未覆盖声明 |
| G3 / schema | 完整配置 schema 留 P2；P1 只覆盖当前协议实际字段 | G3 | 新配置字段或 P2 协议进入运行前：扩展纯数据 schema、拒绝域及判别性测试 |
| G3 / 跨机 | 跨机器数值复现未验证，身份摘要只在同次运行内比较 | G3 | 内部研究以外用途前：按 §8 的 P2 完成标准做结论层比较 |

### 7.4 集成审查应改项

A01–A04 来自审查 §9；六审覆盖建议来自 §13.4。A01–A03 不因 P1 恢复或本报告定稿而自动偿还，建议债主仍不等于已派发。

| 编号 / 状态 | 已知缺口 | 债主 | 触发事件与应补证据 |
|---|---|---|---|
| A01 / 应改 | G1 撤权源到 G3 热缓存及 cache=False 缺同源端到端验收；callback 测试不能代替 | G0 接缝负责；G1 提供 manifest/tombstone；G3 接入统一拒读 | 首次把真实 G1 图撤权接入 G3 缓存，或启用复用图的研究任务前：新图快照→同图 tombstone→热读及无缓存重算均拒绝→合法新图可重放；不与已闭合 I05 混同 |
| A02 / 应改 | alias/index/tombstone 和同月行情分区并发发布/撤权缺故障试验；没有动态 lost-update 结论 | G1 图元数据；G2 行情分区；G0 联合调度 | 首次允许同分区两个 writer 或发布与撤权交错前：证明并发幂等、kill/retry、读改写冲突无丢更新、撤权胜出 |
| A03 / 应改 | 其余 verify 的关键词/位置/数量门未充分绑定内容、退出码和本轮身份；包括 M-08、R-05 等 | G0 总负责；G1/G2/G3 分别偿还 D/M/R 门 | 每条弱 verify 下次用于 done/里程碑签字或修改命令时：补正反夹具、失败传播、制品内容与 input/code/attempt 身份绑定。本文如实保留该债，不以 28 条 rc=0 证明全部弱门已增强；OR-01/OR-05 另属已闭合 I07 |
| A04 / 本次文档修正已完成 | 草稿 S5 整批唯一 hash、S6 合并 cancelled 的表述错误 | G0 文档债主；G2 核对导出枚举与批量门；G3 核对消费解释 | 触发事件为本次 OR-06 定稿：§3 已按 policy_version 唯一 hash 及七值含 unfilled_expired 更正，并列实跑测试；未来复制接缝表时按同证据复核，不修改契约迎合旧稿 |
| 六审覆盖建议 / 应改 | 临时边界矩阵尚有组合未进入常驻参数化测试；当前实现均正确拒绝，非必修 | G0，review gate 与集成测试维护方 | 下次修改封存协议或 gate 实现前：补 review/round/verdict 缺失、round 布尔/浮点、各字段 null/数组、证据数字 0、JSON 顶层数组/null 等常驻组合 |

## 8. 遗留风险

P1 **不声称**以下四件事，不能因 readyForStitch 为 true 而扩大解释：

1. **已证明计算发生过**。哈希、回执和内部自洽不构成不可伪造的计算来源证明；不得把机器检查通过改写成计算确实发生。
2. **功效达标**。327/768、条件下界 39.05% 仍低于 80%；原指标 fail，靠用户裁定出口 A 的限制声明收口。
3. **已具备回归保护**。OR-04 成交 78/80 对门限 40、结局 65/80 对 20，是非退化门；实际值与容差基线尚有 §7 债务。
4. **跨机器数值可复现**。能力文档将旧宣告中的“跨机身份必然不等”更正为“可能不等”：平台轮子不同可导致不同，同轮子也可能相同；身份不同不自动表示污染。身份相等只在同次运行内比较，跨机器按结论层比较，不要求浮点逐位一致。

以下 **15 条**来自[能力文档 §2.2](docs/adr/capability-G3-execution-identity.md)。共同复现前提是**必须在 worker 进程内执行注入代码**，如绑定 globals、改注册表、改类属性或默认参数。它们全部保持 open，只退出当前 P1 发布判据，**不是已闭合，也不是按 A39 已豁免**。这项风险接受来自裁决 §13 的**用户委托决定**，不是 G0 自行接受，更不是用户逐条审阅。

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

上述适用范围不能扩展到普通数据或参数输入即可触发的 I02–I05，也不能豁免无需进程内代码注入的事故。磁盘摘要、纯数据配置、spawn、空 pyc 前缀及逐 job 绑定各有明确作用，均不声称穷尽任意外部 callable 的行为依赖。文件系统/环境变量/网络隐式读取、完整配置 schema、跨机器复现仍列为未验证；合成世界结构不等于真实频道数据，T3 未运行，max-t 是依赖假设下的近似而非有限样本保证。

**P2 责任人为 G3；触发事件是研究输出用于内部研究以外的任何用途之前**，其中包含任何 θ 声明进入资本配置或生产决策。完成判据是在独立配置机器上重跑冻结制品，按阳性计数与 Clopper–Pearson 区间落在同一判定侧比较，非逐位一致。这不会自动批准 G-STAT-CLAIM，也不会赋予系统在 worker 可被注入环境中的来源保证。

能力文档只绑定其具名基线。研究源码或声明依赖变化时，须重跑正式全量 MC 并重出能力文档；判读器或排版变化可以重判既有原始结果，但不能给旧结果改盖生成身份。

## 9. readyForStitch 判定

逐条沿用原报告草稿 §6 的标准（编号 0–5）；按当前证据判定，不沿用草稿写作时的未闭合状态。“三条腿全绿”在此按标准本身包含的授权替代验收解释，不能简写成三份独立审查全部 pass。

| 标准 | 判定 | 证据及边界 |
|---|---|---|
| 0. R-11 闭合：依赖内容哈希并入制品身份 | **满足** | §5.2 的 P1-OPEN-3/4/5 已分别技术闭合；实际文件字节、22 个分发含 polars-runtime-32、空 pyc 前缀及逐 job 绑定具备判别性证据；R-11 rc=0。不是仅哈希 RECORD 文本。 |
| 1. P1 三条腿全绿：审查 pass 或经用户授权的 G0 裁决 | **满足** | G1 预授权分类裁决；G2 十四审独立 pass；G3 范围裁决经 §13 用户委托决定追认，技术轴另由实现、测试及明确提交重跑关闭；P1 当前 done=true。G1、G3 均非审查方 pass，15 条反例仍 open。 |
| 2. OR-05 对抗式集成 review 无未闭合必修项 | **满足** | I01–I04/I06 二审闭合，I05 三审闭合，I07 六审闭合；六审封存 pass、证据完整，OR-05 rc=0。A01–A03 及六审覆盖建议保留应改，不虚构已偿还。 |
| 3. S1–S9 均有实跑证据且 S7 无消费方仍成立 | **满足** | §3 逐条列出审查 §8 的实际测试名、PASS 与边界；S7 的估值、哨兵、空登记/使用点测试成立，G3 θ 消费差分仍不适用。S2 单侧与 S9 具名路径限制明确。 |
| 4. 已知缺口逐条有债主和触发事件，无未填项 | **满足** | §7 完整并入方法、范围、统计、各窗口残余与 A01–A04；每行给出责任方及触发处置，G3 15 条共享债主/触发在 §7.3 并于 §8 逐项列名；A04 本次已纠正。 |
| 5. G-STAT-CLAIM 未批时只描述不声明，任何 θ 声明另行授权 | **满足** | §4、§6、§8 限定 descriptive_only；闸门仍 pending，本报告未授权任何 θ 声明。readyForStitch 只回答所列 P1 研究层集成标准，不表示允许生产、资本配置或其他内部研究外用途。 |

没有不满足的标准。该结论保留本报告全部证据边界、已知缺口及用户闸门，不替代后续用途授权。

readyForStitch: true
