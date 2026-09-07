搜索预算已用满（1 次工具加载 + 7 次搜索 = 8 次），停止检索，直接交付。

# 交易信号 LLM 结构化抽取 + 人工确认工具 选型调研（第 1 轮广度）

抓取日期统一为 **2026-09-07**。官方价格页本轮未直接抓取（预算限制），价格数字来自当日搜索摘要/第三方聚合页，标注为「中」可信度；下一轮应对官方页各做一次 WebFetch 核对。

---

## 1. LLM 结构化抽取

### 1.1 三家厂商：结构化输出与 Batch 折扣

| 厂商 | JSON Schema / 结构化输出 | Batch 折扣 | 2026-09 价格口径（$/1M tokens，标准价 输入/输出） | 官方价格页（待核对） | 来源与可信度 |
|---|---|---|---|---|---|
| Anthropic Claude | 支持通过 `response_format` 传 JSON schema 的结构化输出（Sonnet 5 / Opus 5 / Fable 5.1 均标注支持） | **50%**，输入输出同降，24h 窗口 | Haiku 4.5 $1/$5；Sonnet 5 与 Sonnet 4.6 $3/$15；Opus 5 及 Opus 4.x $5/$25；Fable 5 $10/$50 | https://www.anthropic.com/pricing ；https://platform.claude.com/docs/about-claude/pricing | [Claude API Pricing (September 2026), https://benchlm.ai/anthropic/api-pricing, 2026-09-07]、[Anthropic API Pricing in 2026, https://www.finout.io/blog/anthropic-api-pricing, 2026-09-07]、[Claude Sonnet 5 (batch) OpenRouter, https://openrouter.ai/anthropic/claude-sonnet-5:batch, 2026-09-07] — 可信度 **中**（第三方转述，非官方页） |
| OpenAI | Structured Outputs / function calling 全系支持；Batch 端点可用 | **50%**，24h 窗口 | GPT-5（原版）$1.25/$10；GPT-5.6 Sol $5/$30、Terra $2/$12、Luna $0.20/$1.20；GPT-6 Astra（2026-09-03 发布）$10/$50；缓存输入按 10% 计 | https://openai.com/api/pricing/ | [OpenAI API Pricing (2026), https://www.morphllm.com/openai-api-pricing, 2026-09-07]、[OpenAI API pricing in 2026: every model from Astra to Luna, https://www.cloudzero.com/blog/openai-pricing/, 2026-09-07]、[OpenAI API Pricing 2026, https://pecollective.com/tools/openai-api-pricing/, 2026-09-07] — 可信度 **中** |
| Google Gemini | `responseSchema` 支持 JSON Schema（含 anyOf/$ref，Pydantic 直接可用，2.5 起保持键序）；**Batch 请求内可指定结构化输出**（有社区帖反映 Flash-Lite batch 输入文件方式曾有问题，需实测） | **50%** 平折，24h 窗口 | 本轮搜索摘要**未给出具体模型单价**（仅提到 3.5 Flash、3.1 Pro、2.5 Lite 存在）。记忆参考（**低**，需核对）：2.5 Flash $0.30/$2.50，2.5 Flash-Lite $0.10/$0.40，2.5 Pro $1.25/$10 | https://ai.google.dev/gemini-api/docs/pricing | [Batch API - Gemini API, https://ai.google.dev/gemini-api/docs/batch-api, 2026-09-07]（官方，**高**：50% 折扣与 batch 内结构化输出）、[Improving Structured Outputs in the Gemini API, https://blog.google/innovation-and-ai/technology/developers-tools/gemini-api-structured-outputs/, 2026-09-07]（官方，**高**）、[Structured output in gemini-2.5-flash-lite batch mode, https://discuss.ai.google.dev/t/structured-output-in-gemini-2-5-flash-lite-batch-mode-input-file/102297, 2026-09-07]（**中**）、[Gemini pricing in 2026, https://www.cloudzero.com/blog/gemini-pricing/, 2026-09-07]（**中**） |

注意：三家图片计费方式不同（Anthropic 约 (w×h)/750 token；OpenAI 按 tile；Gemini 2.5 按 258 token/tile），题设「图约 1500 token」是统一假设，实际手机截图 K 线在各家可能落在 800–2000 token 区间，下一轮应用各家 token 计数 API 实测一张典型截图。

### 1.2 开源约束库（维护状态）

本轮一次合并搜索命中质量差，多数只返回旧版本页面；以下版本号为**低可信度**，需下一轮逐个访问 PyPI。

| 库 | 搜索能确认的信息 | 记忆中的近况（低，待核） | 疑虑 |
|---|---|---|---|
| Instructor | 搜索只命中 0.4.7（2024-01）旧页 [instructor · PyPI, https://pypi.org/project/instructor/0.4.7/, 2026-09-07] | 1.x 线持续活跃（2025 年发到 1.1x），Pydantic 驱动，三家厂商均有 provider，支持 retries + 校验 | 最薄的一层，与厂商原生结构化输出叠加时价值下降；Batch 支持需自己拼 |
| Outlines | 未找到 2026 版本信息（搜索词：`outlines pypi 2026`、`dottxt outlines release`） | 2025 年发 1.0 重构，主打本地模型的语法约束解码 | 对 API 模型价值低，本项目走云 Batch 时基本用不上 |
| BAML | `baml-lib` 0.42.0（2025-01-21）[baml-lib · PyPI, https://pypi.org/project/baml-lib/, 2026-09-07] — 注意这是旧/边缘包，主包是 `baml-py` [PyPI Download Stats baml-py, https://pypistats.org/packages/baml-py, 2026-09-07] | baml-py 0.2xx，2025 起快速迭代；自有 DSL + 容错 JSON 解析（SAP），图片输入原生支持 | 引入独立 DSL 与代码生成，单人项目学习成本；对比文 [BAML vs Instructor, https://www.glukhov.org/llm-performance/benchmarks/baml-vs-instruct-for-structured-output-llm-in-python/, 2026-09-07]（中） |
| LangExtract | Google 出品，2025-07-21 发布，PyPI 活跃 [langextract · PyPI, https://pypi.org/project/langextract/, 2026-09-07]；有第三方扩展 langextract-docling 1.6.1 说明生态在动 | 1.0.x，强调「抽取结果回溯到原文字符位置（grounding）」+ 可视化 | 偏文档抽取，默认绑定 Gemini；对多模态截图支持不明 |

### 1.3 截图价格标签的视觉抽取能力证据

| 发现 | 来源 | 可信度 |
|---|---|---|
| 反面证据：4 个前沿视觉模型、2 家厂商、40 条真实信号审计——方向判断 51%，形态命名 1/215 正确，Gemini 100% 偏多头；结论「截至 2026-04 无前沿 VLM 可用于生产级图表形态分析」 | [Stop Using Vision LLMs to Read Trading Charts, https://gist.github.com/roman-rr/c1cd675f7c35b68ae5ac281c30080166, 2026-09-07] | 中（个人 gist，但样本与方法透明） |
| 学术基准：VLM「读」K 线的多尺度基准，指出多数研究缺乏消融，无法区分模型靠图还是靠文本信号 | [Do VLMs Truly "Read" Candlesticks?, https://arxiv.org/html/2604.12659v1, 2026-09-07] | 中 |
| 正面：用 Gemini 2.5 Flash 全年 BTC 日线图自动抽取方向/形态/支撑阻力/信号的流水线（2026 会议论文） | [An Explainable Multimodal AI Framework for Bitcoin Candlestick Chart Analysis, https://dl.acm.org/doi/10.1145/3815970.3815982, 2026-09-07] | 中 |
| **关键缺口**：以上证据都是「看图判形态/预测方向」，**不是「读取图上标注的价格文字（入场/止损/止盈标签）」**。后者本质是 OCR + 空间关联，与形态识别是两个难度级别；本轮**未找到**针对「截图内价格标签 OCR 抽取准确率」的公开评测。搜索词建议下一轮：`vision LLM OCR price labels annotated chart screenshot benchmark`、`GPT-5 Gemini OCR numbers chart annotation accuracy`、`TradingView screenshot OCR LLM` | — | — |

### 1.4 每 1000 条消息成本估算（Batch 价，已含 50% 折扣）

假设：1000 条；输入 = 300 token 文本 ×1000 + 1500 token 图 ×400 = **0.9M 输入 token**；输出 0.2M。**未计** system prompt + schema（若每条 500 token，再加 0.5M 输入，约使输入侧成本 ×1.55）。

| 模型 | Batch 单价（输入/输出） | 输入费 | 输出费 | **每 1000 条** |
|---|---|---|---|---|
| OpenAI GPT-5.6 Luna | $0.10 / $0.60 | $0.09 | $0.12 | **≈ $0.21** |
| Gemini 2.5 Flash-Lite（记忆价，低） | $0.05 / $0.20 | $0.045 | $0.04 | ≈ $0.09 |
| Gemini 2.5 Flash（记忆价，低） | $0.15 / $1.25 | $0.135 | $0.25 | ≈ $0.39 |
| Claude Haiku 4.5 | $0.50 / $2.50 | $0.45 | $0.50 | **≈ $0.95** |
| OpenAI GPT-5（原版） | $0.625 / $5 | $0.56 | $1.00 | ≈ $1.56 |
| Gemini 2.5 Pro（记忆价，低） | $0.625 / $5 | $0.56 | $1.00 | ≈ $1.56 |
| OpenAI GPT-5.6 Terra | $1 / $6 | $0.90 | $1.20 | ≈ $2.10 |
| Claude Sonnet 5 / 4.6 | $1.50 / $7.50 | $1.35 | $1.50 | **≈ $2.85** |
| Claude Opus 5 | $2.50 / $12.50 | $2.25 | $2.50 | ≈ $4.75 |
| OpenAI GPT-5.6 Sol | $2.50 / $15 | $2.25 | $3.00 | ≈ $5.25 |

**区间结论：几千条全量跑一遍，小模型 $0.1–1 / 千条，中档 $1.5–3 / 千条，旗舰 $5 / 千条上下；即使 5000 条用 Sonnet 级也只是 ~$15，跑两遍（两个模型交叉）也在 $30 内。成本不是选型约束，抽取质量与图上价格 OCR 准确率才是。**

---

## 2. 人工确认工具对比

| 候选 | 最新版本/日期 | 许可证 | 预填 LLM 草稿仅确认 | 标注版本历史 / 导出格式 | 记录标注者与时间戳 | 单人本地部署成本 | 文档 URL | 疑虑 |
|---|---|---|---|---|---|---|---|---|
| **Label Studio** | 1.2x 线持续发布（具体号本轮未核，低） | Apache-2.0（社区版）；审计追踪等在 Enterprise | **是**：任务 JSON 带 `predictions` 数组（含 `model_version`、`result`），开启「Show predictions to annotators」后预测自动复制为新标注，人只改错 [Import pre-annotated data, https://labelstud.io/guide/predictions, 2026-09-07]（高） | 导出 JSON/JSON_MIN/CSV 等；SDK 导出可配置包含 drafts / predictions / **history** [Export Annotations, https://labelstud.io/guide/export, 2026-09-07]（高）；predictions 只读，便于对比「草稿 vs 人改」 | 是：annotation 含 `completed_by`、`created_at`、`updated_at`、`lead_time` [Understanding the Label Studio JSON format, https://labelstud.io/blog/understanding-the-label-studio-json-format/, 2026-09-07]（高） | 低：`pip install label-studio`，默认 SQLite，单进程 | https://labelstud.io/guide/ | 结构化表单（品种/方向/区间/多档止盈）要用 `<TextArea>`/`<Choices>`/`<Number>` 拼标签配置，多档止盈可变个数时表单表达笨；图片+文本双模态任务需自定义模板；社区版无审计日志 |
| **Argilla** | 2.x 线（2025 年到 2.8 左右，本轮未核，低）；2024-06 被 Hugging Face 收购 [argilla vs label studio…, https://aitaggers.com.au/blog/label-studio-vs-doccano-vs-prodigy-2026, 2026-09-07]（中） | Apache-2.0 | **是**：`suggestions` 机制专为「模型建议→人确认」设计，可带 score 与 agent 名 | 记录级 `responses` 含 status（submitted/draft/discarded）；导出为 HF Datasets / JSON（Python SDK） | 是：response 带 `user_id`、`inserted_at`、`updated_at` | **中高**：需 Elasticsearch/OpenSearch + PostgreSQL + Redis，docker compose 起 3-4 个容器，Mac 单机内存占用大 | https://docs.argilla.io/ | 建项目、上传、导出全靠 Python 脚本（对开发者友好，对非专业确认者无感知）[Choosing an Open-Source Annotation Tool in 2026, https://www.potatoannotator.com/blog/choosing-an-annotation-tool-2026, 2026-09-07]（中）；HF 收购后发布节奏与长期维护有不确定性（低，需核 GitHub 活跃度） |
| **doccano** | 1.8.x，2023 年后活跃度低（低，需核） | MIT | 部分：可导入带标签的 JSONL 作为初始标注，但设计目标是 NER/分类/序列到序列，不是「结构化表单确认」 | 无版本历史；导出 JSONL/CSV | 部分：导出可带标注者用户名（需勾选），时间戳不完整 | 低：pip / docker 单容器，SQLite | https://doccano.github.io/doccano/ | 任务类型不匹配（没有多字段表单），维护停滞；「Text-only NER/classification 用它足够」的定位不覆盖本需求 [同上 aitaggers, 2026-09-07]（中） |
| **Prodigy** | 商业闭源，持续更新（版本号本轮未核） | 付费，约 **$490/开发者席位一次性**，含 1 年更新 [aitaggers, https://aitaggers.com.au/blog/label-studio-vs-doccano-vs-prodigy-2026, 2026-09-07]（中） | **是**：`*.correct`/`*.manual` 类 recipe 就是「模型预填、人修正」范式；自定义 recipe 可做任意表单（HTML/blocks 界面） | SQLite 内置 DB，`db-out` 导出 JSONL；每条 answer 带 `_input_hash`/`_task_hash`，天然适合去重与版本比对 | 是：`_timestamp`、`_session_id`、`_annotator_id` | 低（本地单进程）但要花钱 | https://prodi.gy/docs | 闭源、付费、Python 版本绑定；code-first；对单人项目 $490 可接受但锁定 |
| **自建 Streamlit / Gradio 审核页** | Streamlit / Gradio 均活跃（版本本轮未核） | Apache-2.0 | **完全可控**：直接把 LLM JSON 渲染成表单，左图右表 | 自己写：每次确认 append 一条到 JSONL（含 record_id、version、prev_hash、annotator、ts），天然对接已定的 sha256 manifest 锁箱 | 自己写，一行代码 | 最低（单文件），但开发 1-2 天 | https://docs.streamlit.io/ ；https://www.gradio.app/docs | 无「标注流」管理（进度、跳过、回看、冲突）；多档止盈动态行、图片缩放、键盘快捷键都得自己实现；没有第三方来背「工具中立」的书 |

补充来源：[Label Studio vs Doccano vs Prodigy: Honest 2026 Comparison, https://aitaggers.com.au/blog/label-studio-vs-doccano-vs-prodigy-2026, 2026-09-07]（中）、[Open-Source Annotation Tools Compared, https://www.potatoannotator.com/docs/guides/annotation-tools-compared, 2026-09-07]（中）、[Comparing Open Source Data Annotation Tools: … LLM API Integration, https://ai.gopubby.com/comparing-open-source-data-annotation-tools-customised-model-and-llm-api-integration-for-e59a51efe056, 2026-09-07]（中）。

**未找到公开信息**：Label Studio / Argilla / doccano 2026 年精确版本号与发布日期（搜索词：`label-studio pypi release 2026`、`argilla github release 2026`、`doccano release 2026`）；Argilla 收购后维护状态的官方说明（搜索词：`argilla hugging face maintenance 2026`）。

---

## 对本项目的初步含义

1. **成本可忽略，质量是唯一变量**：几千条 × Batch 价即使旗舰模型也 <$50，应直接用两个不同厂商的中档模型（如 Sonnet 5 + GPT-5.6 Terra 或 Gemini Flash）各跑一遍取分歧作为人工重点核对项，而不是省钱选小模型。
2. **图上价格标签抽取没有现成证据**：已有评测都在说「VLM 看形态不可靠」，但本项目要的是「读图上标好的数字」（OCR 性质），需自己做 50 张截图的小基准；抽取 schema 里给每个价格字段加 `source: text|image` 与 `confidence`，让人工确认优先看 image 来源字段。
3. **确认工具首选 Label Studio 社区版**（predictions 预填 + 人改 + `completed_by`/时间戳/history 导出全部开箱即有，pip 单机部署），把导出 JSON 作为锁箱输入；Argilla 的 suggestions 机制更贴合但部署重、非专业用户无感知收益；doccano 任务类型不匹配可排除；Prodigy 作为付费备选。
4. **自建审核页保留为备胎而非首选**：若 Label Studio 标签配置表达不了「可变档数止盈 + 管理动作」，再退回 Streamlit 单文件（1-2 天），此时版本化与哈希链直接内嵌，反而与既定锁箱方案耦合最紧。

**待下一轮核对（优先级序）**：三家官方价格页各抓一次；Gemini 当前代际（3.x）单价；Instructor/BAML/LangExtract/Outlines 的 PyPI 最新版本与日期；Label Studio 与 Argilla 最新 release 与 GitHub 近 90 天提交数；一张真实截图在三家的 token 计数。