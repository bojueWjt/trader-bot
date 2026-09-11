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
