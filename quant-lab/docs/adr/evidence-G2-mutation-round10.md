# G2 自做的突变自证：十审修复（A24 §12.1 / G0 R45 口径）

> G0 R45：**契约强制新增的门，其闭合回执必须包含我自己做的突变结果（注入原缺陷 → 回归必须红）；缺这份证据的闭合声明不接受。由审查方代做突变而发现回归无效，等同该项未闭合。**
> 本文是 G2 **自己**跑的那一份，与修复方 Codex 的 45 次自证相互独立（不同注入点、不同工具、不同选择器）。
> 台：会话 scratchpad `mutate.py`。每例走「基线必须绿 → 注入 → **必须红** → 还原 → **sha256 必须一致** → 必须绿」。
> **还原一律用内容 sha256 比对，绝不用返回码**——文件被正确还原与文件被毁返回码可能相同，八审终裁行就是这么丢的。

## 1. 结果

| 门 | 我注入的生产缺陷 | 基线 | 注入后 | 还原后 | sha256 | 判定 |
|---|---|---|---|---|---|---|
| S29 离网行抵扣 | `present` 恢复成只按时间范围过滤 | 2 passed | **1 failed** | 2 passed | 一致 | ✓ 证成 |
| S29 非负不变量 | 去掉 `assert rep.missing >= 0` | 2 passed | 2 passed | 2 passed | 一致 | **冗余防御，见 §2** |
| S31 门未接线 | 删掉 `simulate_batch` 对真实冲突门的调用 | 2 passed | **1 failed** | 2 passed | 一致 | ✓ 证成 |
| S31 诊断丢版本名 | 冲突诊断里把 `policy_version` 换成 REDACTED | 2 passed | **1 failed** | 2 passed | 一致 | ✓ 证成 |
| S38 政策域放开负延迟 | 政策层不再拒绝负 `latency_s` | 1 passed | **1 failed** | 1 passed | 一致 | ✓ 证成 |
| S38 只查显式启动 | 解析后启动时刻校验改回只查显式字段 | 1 passed | **1 failed** | 1 passed | 一致 | ✓ 证成 |
| A24 绕过单一来源 | `build_request` 改回自写 latency 加法（S32 原形） | 14 passed | **1 failed** | 14 passed | 一致 | ✓ 证成 |
| A24 网格内联重写 | `vision.expected_rows` 改回自写整除（S33 原形） | 14 passed | **1 failed** | 14 passed | 一致 | ✓ 证成 |

七条证成。第二条不是缺陷，理由见下——但**我一开始按二值口径把它判成了"未证成"，这暴露了 A24 突变要求本身需要一条补充规则**。

## 2. "注入后仍绿"有两种含义，二值口径会制造假警报

`assert rep.missing >= 0` 删掉之后测试仍绿。按 A24 的字面二值口径，这条该判"回归无法失败"，与 S31/S39 同罪。**但它们不是一回事。** 我做了一次叠加突变来分辨：

| 注入内容 | 结果 | 说明 |
|---|---|---|
| 只注入 off-grid 缺陷 | **失败，且失败点就是该断言**：`> assert rep.missing >= 0, "present 必须是期望网格的子集"` | 断言确实在工作 |
| 同时注入 off-grid 缺陷 **且** 删掉该断言 | **仍然失败**（2 failed），由测试的显式期望值抓到 | 断言不是唯一检测者 |

结论：该断言是**冗余的第二道防线**。它守护的性质（`present ⊆ 期望网格`）**已被行为测试独立钉住**；断言本身在正确实现下**不可达**，所以没有任何行为测试能让它变红——这正是 §11 A23 已裁的那个形态（不可达缺陷无法用行为测试钉住）。

**因此建议给 A24 补一条判别规则**：突变后回归仍绿时，不得直接判"门无效"，须再做一次**叠加突变**——把该检查所守护的性质本身破坏掉，看回归是否变红。

- 变红 → 被删的是**冗余防御**，其守护性质另有行为证据，**合格**；按 A23 在注释里注明"非行为证据、不可达防御"。
- 仍绿 → 才是 S31/S39 那种**真正无法失败的检查**，判未闭合。

不加这条，A24 会对树里每一条防御性断言产生假警报，而假警报最终的下场是被人关掉——那就把 A24 反向变成了降低防御的力量。

## 3. 与修复方证据的关系

修复方 Codex 自报 45 次突变（`tests/market/mutation_evidence.md` / `.json`），覆盖 36 条测试。本文这 8 次是我**独立选点**跑的，其中 `S29 非负不变量` 这条修复方没有覆盖——它自报的表里没有针对该断言的注入。**这就是 R45 要求"由被修方自己再跑一遍"的价值：不同的人选不同的点。**

## 4. S31 的生产行为定论（回 G0 R45/R46）

在**未突变**的生产树上，经真实 `result_row` 注入同版本双哈希，`simulate_batch` 在 `strict` 两个取值下**都抛 `ContractError`**，消息含版本名与两个 hash 取值：

```
strict=True   ✓ 抛 ContractError
  同一 policy_version 对应多个 policy_hash（版本串与内容不一一对应）：
  fixture-zero-v1 → ['0000…0000', '12ca10ebb10ef27d10873584ee42a224c44efdf99e9b55c7656e8a838c58cd1d']
  含版本名 True   含真 hash True   含伪 hash True
strict=False  ✓ 同上
```

**定论：生产门有效，S31 是验证缺陷而非生产缺陷**，与审查方十审原文第 111 行"真实代码当前并没有 sanitized 替换……不混称为当前生产放行漏洞"一致，G0 已于 R46 按原文逐字核对后更正记录。

## 5. 独立复跑的验收命令（我亲跑，非引用）

```
PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python -m pytest tests/market -q -p no:cacheprovider   → 293 passed
PYTHONPATH=src .venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A → kernel=A passed=22 failed=0, rc=0
PYTHONPATH=src .venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1 → rc=0，MATCH 12 / B_COMMAND_LATENCY 3 / GAP_PRICE 1 / SAME_TS_PRIORITY 4 / GTD_BOUNDARY 1 / B_LIQUIDITY_MODEL 1，UNEXPLAINED 0
```

---

# 附：`force_close_net_R` 的 G2 自证（A29 合规，2026-09-11）

> A29：突变取证必须**真实写盘 + 子进程隔离**，基线/注入/还原三次各起新进程，还原以**内容 SHA256** 为准。
> 本表直接在真实树上写盘注入再还原，**不使用隔离副本**，因此不存在"副本继承祖先 `pythonpath=真实 src`、突变模块未被加载"那类路径问题。

## 1. 我自己跑的七条（选择器 `tests/market/test_force_close_net_r.py`）

| 我注入的生产缺陷 | 基线 | 注入后 | 还原后 | SHA256 | 判定 |
|---|---|---|---|---|---|
| 漏计余仓平仓费 | 17 passed | **5 failed, 12 passed** | 17 passed | 一致 | ✓ |
| 方向符号写反（`short` 判据改 `long`） | 17 passed | **5 failed, 12 passed** | 17 passed | 一致 | ✓ |
| 漏计资金费 | 17 passed | **5 failed, 12 passed** | 17 passed | 一致 | ✓ |
| 漏计已实现损益（只算余仓浮盈） | 17 passed | **5 failed, 12 passed** | 17 passed | 一致 | ✓ |
| 漏计累计费 | 17 passed | **5 failed, 12 passed** | 17 passed | 一致 | ✓ |
| `gross_pnl` 缺失改为兜底 0（§2.1 裁定二禁止形态的等价替代） | 17 passed | **1 failed, 16 passed** | 17 passed | 一致 | ✓ |
| 无余仓返回 0 而非 `None` | 17 passed | **2 failed, 15 passed** | 17 passed | 一致 | ✓ |

**7/7 证成。**

## 2. 一条与承包方证据的交叉核对（值得单记）

承包方在 `tests/market/force_close_harness_audit.json` 里记录：注入"漏计余仓平仓费"后测试**仍绿**，并自行诊断为

> 隔离副本继承祖先 `pyproject.toml` `pythonpath=真实 src`，未执行突变模块。属验证器路径错误……**此记录不是通过证据**。

**该诊断成立**：同一条缺陷我在真实树上注入，得到 **5 failed**。所以那次绿是取证器没把突变模块喂进去，**不是测试无效**。承包方没有把它当作通过证据，这个处理是对的——**一条自己不确定的绿，被如实记成"不是证据"，比记成"通过"有用得多**。

## 3. B19 三条硬约束的独立行为验证

夹具 `E15a.json`（`outcome_kind=right_censored`，`filled_qty=1`），调用前后对比：

| 约束 | 结果 |
|---|---|
| 不产生 `canonical_events` | 事件对象同一 `True`，条数 10 → 10 ✓ |
| 不改 `fill_status` | `filled` → `filled` ✓ |
| 不改 `outcome_kind` | `right_censored` → **仍 `right_censored`** ✓ |
| 不改 `censor_reason` | `LABEL_RIGHT_CENSORED` → 不变 ✓ |
| 不覆写 `net_R` | `net_R` 仍为 `None`，`net_R_forced` 另行给出 ✓ |

溯源与诊断字段齐备：`estimand=forced_close`、`mark` / `mark_at` / `mark_source`、`residual_qty` / `close_fee`；返回体 frozen（改写抛 `FrozenInstanceError`）。

## 4. 独立复核的验收命令（测量时刻 14:55:26，关键路径 180s 内无源码写入）

```
pytest tests/market -q                    → 322 passed
pytest tests/market/test_single_source.py → 26 passed
execution replay --kernel A               → kernel=A passed=22 failed=0
nautilus_adapter report --reps 1          → MATCH 12 / B_COMMAND_LATENCY 3 / GAP_PRICE 1
                                            / SAME_TS_PRIORITY 4 / GTD_BOUNDARY 1
                                            / B_LIQUIDITY_MODEL 1，UNEXPLAINED 0
```

## 5. 本轮**未**完成的（A19：写明未做及原因，不留悬空）

- **A28 性质差分测试未开始**。承包方在完成 `force_close_net_R` 后如实报告本项未动，未两件各做一半。已单独派 `task-mtwjotp6-5a6doi`。
- **G3 接入未完成**，跨窗口调用方登记为**空集**。按裁定不得预登记尚不存在的调用方——一份声称存在而实际不存在的条目会让「登记 == 实际调用方」因错误的原因通过。函数与测试模块 docstring 已各写明：夹具只证明函数行为与 A24 机制对本函数生效，**不证明 G3 调用而非自算**。

---

# 附二：A28 三道机制互补性的**实证**（G2 自跑，A29 合规，2026-09-11）

> G0 A28 声明三道机制"各有不可替代之处"：禁令捕获**重复**、登记捕获**绕过**、差分捕获**偏离**，且**差分不取代禁令**。
> 这条此前是设计主张。本节把它变成实测结果。

## 1. 同样四条注入，分别只跑差分、与跑全部三道

方法：A29（真实写盘 + 子进程隔离，基线/注入/还原三次各起新进程，还原以内容 SHA256 为准）。
**只跑差分时用 `-k differential` 排除 lint 与登记测试**——否则变红可能来自语法门，就证不到语义层。

| 我注入的「当前等价但独立」实现 | 只跑差分（10 条） | 跑全部三道（36 条） |
|---|---|---|
| `vision.expected_rows` 改回整秒整除（**S33 原形**） | **未抓到** | 抓到（3 failed） |
| `vision.expected_rows` 改成闭区间计数（多 1） | **抓到**（1 failed） | 抓到（1 failed） |
| `partition_check` 中段改回相邻时间差比较（**S40 原形**） | **未抓到** | 抓到（2 failed） |
| `kernel_a` 中段改回 `o - prev > iv`（**S37 原形**） | **未抓到** | 抓到（2 failed） |

**差分单独：1/4。三道合计：4/4。** 四条的还原 SHA256 全部一致。

## 2. 那三条为什么差分抓不到——不是差分失效，是没有可抓的东西

直接对拍两式在**可达输入**上的取值：

| 对拍 | 分歧数 |
|---|---|
| `expected_rows` 两式 × 14 个可达 period（`period_bounds` 恒返回对齐边界） | **0** |
| 相邻缺口两式 × 对齐网格样本 | **0** |

三条注入在**消费方实际能收到的全部输入上与单一来源恒等**，所以**没有偏离可供差分发现**。差分报绿是**正确**的。

> ### ⚠ 更正（十二审 S42，2026-09-11）：上面这句**对 kernel 那一行是错的**，是我过度泛化
>
> 十二审独立复核后指出：**"kernel 全部可达输入的泛化不成立"**。
>
> 我当时只对拍了**对齐网格**上的取值，就把结论推广到"全部可达输入"。但 `kernel_a` 的可达输入域**比我假设的大**——公共 `simulate` 接受调用方显式构造的 `MarketView`，其 `Bar.open_time` **可以离网**。在离网输入上两式并不恒等。
>
> | 行 | 我原来的判断 | 更正后 |
> |---|---|---|
> | `vision.expected_rows` 整秒整除 | 可达输入上恒等 | **成立**（`period_bounds` 恒返回对齐边界；G0 独立核 126 组合分歧 0） |
> | `partition_check` 相邻时间差 | 可达输入上恒等 | **成立**（`legal` 过滤后行必在网格上） |
> | `kernel_a` 相邻时间差 | 可达输入上恒等 | **不成立**——离网 bar 可达，见 S42/S43 |
>
> **"三道互补"这个结论本身不受影响**——十二审逐条复现了四行的"差分单独 / 三道合计"结果并判定"与三道分工一致"。受影响的是**我为其中一行给出的理由**。
>
> **教训（与 S42 同一条）**：我把"差分报绿是正确的"这条辩护，建立在一个**我没有验证过的输入域假设**上。正确的顺序是先问"**消费方到底能收到什么**"，再问"生成器生成了什么"——我做反了。S42 的本质完全相同：生成器只喂对齐 bar，于是所有人（包括我）都默认对齐 bar 就是全部。

抓到它们的是**禁令**，因为它们的问题不是"算错了"，而是"同一条规则被独立写了第二遍"。**重复今天不致错，明天才致错**——S32/S33 当时正是"当前等价"，靠的是运气。

## 3. 结论：G0 那句"差分不取代禁令"现在有实测支撑

- **禁令（语法）**：对**重复**敏感，即使当前等价也报；也是**不可达区域**唯一有效的机制（B14 的 `multiplier` 位置行为测试跑不到）。
- **差分（语义）**：对**偏离**敏感，与写法无关；但**只在真有分歧的输入上有效**，对"当前等价的重复"必然沉默。
- **登记（结构）**：对**绕过**敏感，含已登记调用方内部单个使用点的绕过（S40 的深层形态）。

**三者不是冗余，是三个不同的谓词。** 任何一道单独使用都会漏掉另外两道负责的那类。

**因此本节也反证了一件事**：若将来有人以"差分已覆盖语义"为由删掉禁令，上表第 1/3/4 行会立刻全部变绿——**而那三条正是 S33/S40/S37 的原形**，即本项目已经实际发生过三次的缺陷。

## 4. 本轮其余独立复核（测量时刻 15:20:32，关键路径 180s 内无源码写入）

```
pytest tests/market -q                    → 332 passed
pytest tests/market/test_single_source.py → 36 passed
execution replay --kernel A               → kernel=A passed=22 failed=0
nautilus_adapter report --reps 1          → MATCH 12 / B_COMMAND_LATENCY 3 / GAP_PRICE 1
                                            / SAME_TS_PRIORITY 4 / GTD_BOUNDARY 1
                                            / B_LIQUIDITY_MODEL 1，UNEXPLAINED 0
```
