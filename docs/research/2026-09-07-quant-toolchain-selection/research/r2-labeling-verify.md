# 第 2 轮关键验证报告（抓取日 2026-09-07，共 9 次工具调用）

## 1. 官方价格页

### 1a. Anthropic（claude.com/pricing，anthropic.com/pricing 已 301 跳转）
**结论**：Sonnet 5 标准价 $2 in / $10 out（$/1M）；页面明示 "Save 50% with batch processing"，即 Batch 价 $1 / $5。Haiku 4.5 为 $1 / $5。
**证据**：[Claude Pricing, https://claude.com/pricing, 抓取 2026-09-07] 可信度：高（一手官方页）。
**图片计费**：主价格页未列图片 token 公式；官方 docs 惯例为 tokens ≈ (宽×高)/750（未在本轮抓取核验，标记为待查）。
**反面证据**：无；但注意 anthropic.com/pricing 已不是有效入口，文档引用应改为 claude.com/pricing。

### 1b. OpenAI（openai.com/api/pricing）
**结论**：**抓取失败（HTTP 403，反爬）**，本轮无法给出一手价格。
**证据**：无。可信度：不适用。
**反面证据/替代**：沿用第 1 轮"Batch 50% 折扣"结论，但中档模型具体单价与图片计费规则本轮未核验；建议用浏览器人工打开或 `platform.openai.com/docs/pricing` 二次抓取。

### 1c. Google Gemini（ai.google.dev/gemini-api/docs/pricing）
**结论**：Flash 级现行主力 Gemini 3.8/3.7/3.6 Flash：$0.75 in / $3.75 out（促销价至 2026-12-31，之后 $1.50 / $7.50）；Gemini 3.5 Flash $1.50 / $9.00；3.5 Flash-Lite $0.30 / $2.50；旧代 2.5 Flash $0.30 / $2.50。所有型号 Batch 一律"50% off standard rates"。
**图片计费**：按 token 计入输入价，PDF 按图片费率；参考值 3.1 Flash Image ≈560 tok/图，2.5 Flash Image ≈1,290 tok/1024² 图。
**证据**：[Gemini API Pricing, https://ai.google.dev/gemini-api/docs/pricing, 抓取 2026-09-07] 可信度：高。
**反面证据**：Flash 3.x 的促销价有到期日，预算需按 2027 年翻倍价做压力测试。

## 2. Label Studio

**结论**：
- 最新版 **1.23.0，2026-03-13**（PyPI）；节奏约每季一版（1.22.0 2025-12-19、1.21.0 2025-09-30），Python ≥3.10，Apache 2.0。项目活跃。
- 可变个数止盈档位：官方 `<Repeater>` 标签可按数据数组动态重复一组控件（每档一个 `<Number>`/`<TextArea>`），文档 https://labelstud.io/tags/repeater ；退路是 `<TextArea>` 允许多值（`maxSubmissions` 不设限）或 `<Number>` 多实例。本轮未单独抓取该页（预算），可信度：中（基于既有文档知识，URL 稳定）。
- predictions vs annotation diff：社区版可导入 `predictions` 预填，导出 JSON 中同时含 `predictions[]` 与 `annotations[]`（含 `completed_by`、`created_at`、`lead_time`），diff 需自己离线算；内置的 agreement/对比可视化属企业版。
**证据**：[label-studio · PyPI, https://pypi.org/project/label-studio/, 抓取 2026-09-07] 可信度：高。
**反面证据**：Repeater 与 diff 导出细节未在本轮实抓（搜索预算已用完），落地前需实际跑一个 3 档/5 档止盈样例验证。

## 3. Argilla

**结论**：**基本停更**。GitHub `develop` 分支最近一次提交为 **2025-08-05**（"Update README.md"），此前 5 月有 "Update project status"，最后一次功能性 PR 在 2025-03-10。截至抓取日已 **13 个月无功能提交**，近 90 天零 commit、零 release。
**证据**：[argilla-io/argilla commits, https://github.com/argilla-io/argilla/commits/develop, 抓取 2026-09-07] 可信度：高。
**反面证据**：未找到 HF 官方"归档"声明，但 README 连续更新"project status"是典型维护模式收尾信号。

## 4. 截图价格标签 OCR 基准

**结论**：**未找到**针对"交易图截图上价格标签/水平线数字"的专门基准。搜索到的是通用文档 OCR 榜（GLM-OCR 94.62、PaddleOCR-VL 94.50、Gemini 3.1 Pro ≈90.3 于 OmniDocBench）与图表理解基准 CharXiv（柱/折线/散点图问答，非 K 线价格读数）。
**证据**：[Best LLM for OCR (2026), https://ofox.ai/blog/best-ai-model-for-ocr-2026/, 2026]；[OCR Benchmarks & Real-World Documents (July 2026), https://www.extend.ai/resources/ocr-benchmarks-real-world-documents]；[LLM OCR vs Traditional OCR, https://parsli.co/blog/llm-ocr-vs-traditional-ocr] 可信度：中低（均为厂商博客，非同行评审）。
**未找到（搜索词）**：`LLM vision OCR chart annotation price levels screenshot accuracy benchmark 2026 GPT-5 Gemini reading numbers from trading charts`（两条指定搜索词已合并为一次执行）。

## 5. OpenTimestamps Python 客户端

**结论**：`opentimestamps-client` 最新 **0.7.2，2024-12-31**，Python 3，状态 "4 - Beta"，无弃用声明；仍可用，但 20 个月未更新，README 明示日历 REST 协议未来可能不兼容变更。
**证据**：[opentimestamps-client · PyPI, https://pypi.org/project/opentimestamps-client/, 抓取 2026-09-07] 可信度：高。
**反面证据**：更新停滞属低风险（协议稳定），但生产使用应锁定版本并保留 `.ots` 原文件以便换客户端重验。

## 对选型的含义

1. **Argilla 出局**：13 个月无功能提交，Label Studio 1.23.0（2026-03）季度节奏活跃，人工确认工具维持 Label Studio 社区版首选，Streamlit 备胎保留。
2. **成本结论仍成立但有到期风险**：Sonnet 5 Batch $1/$5、Gemini 3.x Flash Batch $0.375/$1.875，千条级仍在 $0.1–5 区间；Gemini Flash 促销价 2026-12-31 到期后翻倍，预算按 2027 价核算。OpenAI 单价本轮未核验（403），需人工补抓。
3. **截图 OCR 精度无外部基准可依**：必须自建 50–100 张带真值的截图小样，用 Label Studio predictions 预填 + 人工纠错，把纠错率本身作为验收指标。
4. **锁箱方案可用但要防依赖腐化**：OpenTimestamps 客户端 0.7.2 冻结版本、`.ots` 文件与 JSONL+sha256 一并归档；Label Studio 的 Repeater/多值导出与 diff 需在立项前做一次 3–5 档止盈的实跑验证。