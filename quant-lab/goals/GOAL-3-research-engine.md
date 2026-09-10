# GOAL-3 · 研究协议与表达式引擎 (G3 research)

> 一句话目标：一套**只收 JSON AST、每个算子过前视契约**的表达式引擎，和一套**事件级配对评估 + walk-forward + 尝试账本 + max-t + 空模型验收 + 折内簇数分档**的研究协议；质量底线：算子契约测试全过、空模型假阳性率 CI 上界 ≤ 7%、功效 ≥ 80%、任何结论附试验数与区间。

## 0. 你是谁 / 边界
- 你负责：`src/quant_lab/research/`、`tests/research/`、`data/lockbox/`、`docs/adr/ADR-G3-*.md`、`docs/adr/report-G3-*.md`、`docs/adr/review-G3-*.md`。
- **不碰** `data/`、`market/`、`contracts/`。
- 上下游：你在最下游。按 `contracts/research-schema.md` 与 `contracts/execution-interface.md` 的签名**在 R-01 就写桩**（fake episodes / fake bars / fake execution），P1 全部在合成数据上完成，不等 G1/G2 真实产出。
- 路线依据：合并稿 D.1–D.5、E 节；Claude 验收 R03（FPR 按档分级）。

## 1. 技术栈
Python 3.12、`.venv-g3`（`requirements/g3.txt`）：polars、polars_ta 0.5.17（后端变体）、TA-Lib 0.7.1、arch 8.0、deap 1.4.4（只在达档后用）、scikit-learn。

## 2. 交付物
- `src/quant_lab/research/{ast,ops,backends/{polars,polars_ta},features,evaluator,protocol,ledger,maxt,nullmodel,tiers,grammar,api}.py`
- `tests/research/fixtures/protocol_synthetic.yaml`
- `docs/adr/ADR-G3-expression-and-stats-engine.md`（Codex）、`docs/adr/report-G3-backend-spike.md`、`docs/adr/report-G3-null-model.md`、`docs/adr/review-G3-P1.md`（Codex）

## 3. 接口契约（provides）
见 `contracts/feature-snapshot.md`：AST 规范与 lint 硬门、算子登记表与契约测试、`feature_snapshot`、`OpportunitySet` / `evaluate` 与 θ、walk-forward / 账本 / `max_t_bootstrap` / 空模型 / 分档、`run_protocol` / `enumerate_grammar`。

## 4. 任务清单（对应 taskList.json → modules.research）
| id | 交付 | 验收要点 |
|---|---|---|
| R-01 | 环境 + 骨架 + 桩 | tests 可 collect，polars_ta/deap/arch 可 import |
| R-02 | **[Codex ADR]** 表达式与统计引擎设计 | ≥200 行，含 canonical_hash 与 max-t |
| R-03 | AST schema + lint + 规范化哈希 | 拒收用例全过 |
| R-04 | 算子注册表 + 契约框架 + 18 算子 polars 后端 | 每算子四类契约测试 |
| R-05 | polars_ta 变体对拍 + spike 报告 | 冷/热耗时、峰值内存、契约通过率 |
| R-06 | feature_snapshot + EventEvaluator | as-of 等号单测、θ、NaN→skip |
| R-07 | walk-forward + PurgedKFold + 账本 | 性质测试；账本非空 |
| R-08 | max-t + 空模型 + FPR/功效报告 | 合成 T1 规模 1000 次；FPR CI 上界 ≤7%、功效 ≥80% |
| R-09 | 分档报告 + 枚举生成器 + run_protocol 端到端 | report.json 含 tier / n_attempts / theta |
| R-10 | **[Codex review]** P1 | 必修闭合 |

## 5. Codex 参与（你自己派）
- R-02 开工前派 ADR-G3（任务书 `docs/agent-team/quant-lab-codex-briefs.md` §1）；ADR 必须回答：AST 规范化与哈希、算子契约测试框架的 reference 生成法、两后端抽象与切换、feature_snapshot 缓存 key 与失效（对接 G1 的 graph_version tombstone）、EventEvaluator 簇均权与 NaN 规则、walk-forward 与 search/selection 内层拆分、账本 schema、max-t 的块索引与 SE 重估、空模型的整块残差重采样、FPR/功效验收按档分级（T3 档 200 次的替代方案）、分档 K/DEFF 计算。
- R-10 派 P1 review（§3）。禁止 `--resume-last`。

## 6. 协作协议
```bash
python scripts/task.py claim research R-03
python scripts/task.py report research --status in_progress --progress 0.5 --note "<进展>"
python scripts/task.py done research R-03
python scripts/task.py block research --on "契约: feature-snapshot §4 缺 weight 列"
```

## 7. DoD
- P1：R-03..R-09 verify 实跑通过；18 算子契约全过；空模型报告 FPR/功效达标；`run_protocol` 在合成配置上出 tier/θ/账本；Codex P1 review 必修闭合。
- P2：接入 G1 决策图与 G2 真实 ExecutionResult，分档按实测 K；DEAP 只在达档后接入（8b）。

## 8. 验证命令
```bash
.venv-g3/bin/python -m pytest tests/research -q
```

## 9. 铁律
不执行外来字符串（不调用 DEAP `compile`/`from_string`、Qlib `eval` 路径）；只读 G1 决策图；任何结论措辞按档位（E 节），不得写"已验证优势"；账本每次评估前写入，失败也留终态。零生产凭据；不 import `services/*`。
