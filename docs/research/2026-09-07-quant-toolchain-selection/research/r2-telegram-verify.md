# 第 2 轮关键验证报告（截止 2026-09-07）

工具调用 8 次（含 1 次工具加载），按预算停止搜索。以下 6 项中，第 1、5、6 项拿到一手决定性证据；第 2、3、4 项只拿到部分证据，未证实处已明确标注。

---

## 1. Telethon 迁 Codeberg 后的发版节奏 / v1 维护模式

**结论**：迁移未打断发版。2026 年已发 4 个版本（1.43.0/1.43.1/1.43.2/1.44.0），最近一版 2026-06-15；2025 年发 6 版。**v1 已由作者明文宣布"大体处于维护模式"**（层更新 + bug 修复 + 少量小增补），不是弃维护。

**证据**（可信度：高，一手）
- [Telethon · PyPI release history, https://pypi.org/project/Telethon/#history, 抓取 2026-09-07]
  - 2026：1.44.0 (06-15)、1.43.2 (04-20)、1.43.1 (04-13)、1.43.0 (04-10)
  - 2025：1.42.0 (11-05)、1.41.2 (09-04)、1.41.1 (09-04)、1.41.0 (09-01)、1.40.0 (04-21)、1.39.0 (02-20)
  - 项目页 Source 链接已指向 https://codeberg.org/Lonami/Telethon
  - 项目描述原文："Telethon v1 is for the most part in maintenance mode. New layers are still updated when released, bug fixes are welcome, and some small additions may still be added from time to time."

**反面证据**：1.44.0 之后到 09-07 已近 3 个月无新版，是迁移以来最长间隔（此前平均 1.5–2 个月一版）；PyPI 页未提及 v2 时间表。未做 Codeberg 提交活跃度抓取（预算用尽）。

---

## 2. FloodWait 实测值 / `iter_messages` 的 `wait_time`

**结论**：**未找到 2025 年后带秒数与触发条件的实测帖**。只确认：（a）Telethon 官方文档明确 `flood_sleep_threshold` 以下的 FloodWait 会自动 sleep 重试（默认 60s）；（b）历史 issue 中 `GetHistoryRequest` 引发过长达 24h 的 FloodWait，但为早期案例，非 2025 后数据。`wait_time` 推荐值官方无给出。

**证据**（可信度：中）
- [TelegramClient — Telethon 1.44.0 documentation, https://docs.telethon.dev/en/stable/modules/client.html, 2026]：请求在 FloodWaitError 小于 `flood_sleep_threshold` 时自动重试；获取全部 dialogs 时可能耗时数分钟，因为 Telegram 会用 FloodWait 让库减速。
- [FloodWaitError on get_entity() · Issue #494, https://github.com/LonamiWebs/Telethon/issues/494, 2018]：搜索摘要提到 GetHistoryRequest 用户遇到过持续 24 小时的 FloodWait（旧案例，仅作上限参考）。
- 官方文档中 `iter_messages(wait_time=None)` 语义：默认对超过 3000 条的请求自动加 1 秒等待（此为文档已知行为，本轮未再单独抓取核对）。

**未找到**（搜索词：`Telethon iter_messages FloodWait seconds channel history wait_time 2025 GetHistoryRequest "FloodWaitError"`）：2025 后针对频道历史批量拉取的具体秒数/条数阈值实测；Pyrogram 系对照数据。搜索结果被 2018–2019 老 issue 与营销文占据。

---

## 3. tdl `chat export` JSON 字段 / 最新版本

**结论**：**官方文档页未给出 JSON 字段清单或示例**，无法从一手文档证实是否含 `edit_date` / `reply_to`。已证实的能力：`--all` 导出非媒体消息、`--with-content` 带正文、`--raw` 导出原始 MTProto 结构（raw 模式下必然包含 `edit_date`、`reply_to` 等 TL 字段，因为那就是 `Message` 对象本身）。最新版本号与日期**未获取**。

**证据**（可信度：中，一手但不完整）
- [tdl — Export messages, https://docs.iyear.me/tdl/guide/tools/export-messages/, 抓取 2026-09-07]：flags `--with-content`、`-T time/id/last`、`--all`（"Export all messages including non-media messages, which is useful for debugging/backup"）、`--raw`（"Export raw MTProto structure"）、`-f` 过滤表达式；过滤示例引用字段 `Views`、`Media.Name`、`Media.Size`。

**反面证据 / 未找到**：默认（非 raw）导出格式的字段集未在文档列出，据既有认知默认模式只有 `id/type/file/date/size/text/views` 一类精简字段、**不含 edit_date/reply_to**——此为推断，需用 `tdl chat export --all --with-content` 本地实跑一次核实（约 1 分钟）。版本号可 `gh release view --repo iyear/tdl` 补查。

---

## 4. Telegram Desktop 官方导出：24 小时等待 / JSON 字段

**结论**：JSON 已证实含 `date` 与 `date_unixtime`（还含 `id/type/from/from_id`）；`edited` 与 `reply_to_message_id` 本轮**未从官方 schema 页直接证实**（core.telegram.org/import-export 未抓取），仅有社区文描述"编辑后原文被覆盖、只保留最新版本"。24 小时等待有两层：新设备登录后首次导出可能触发 24h 安全等待；同一聊天重复导出存在约 24h/次的软限。

**证据**（可信度：中）
- [Chat Export Tool, Better Notifications and More, https://telegram.org/blog/export-and-more, 2018-08]：官方导出功能公告（Machine-readable JSON 选项来源）。
- ["Export chat history" outputs illegal JSON with custom emojis · Issue #24961, https://github.com/telegramdesktop/tdesktop/issues/24961]：官方仓库 issue，证明 JSON 输出存在格式缺陷（自定义 emoji 时非法 JSON），社区解析器需容错。
- [How to Export Telegram Chat History (And What You Can't Do Natively), https://www.tgchatmemory.com/blog/export-telegram-chat-history.html, 2025-26]：JSON 消息对象含 `id/type/date/date_unixtime/from/from_id`；编辑只保留最新文本；同一聊天重复导出触发"Please try again later"，约 1 次/24h。
- [MosaicChats 导出指南, https://www.mosaicchats.com/blog/how-to-export-telegram-chat]：新登录 Desktop 后需等待 24 小时安全期方可导出。

**反面证据 / 未找到**：`edited` / `reply_to_message_id` 字段名未经官方页核实（搜索词：`"date_unixtime" "edited" "reply_to_message_id"`，命中页面未逐字给出这两个键）。既有认知中 tdesktop 导出确实有 `edited` + `edited_unixtime` 与 `reply_to_message_id`，可靠度较高但本轮无一手引用。

---

## 5. Telegram 2025-05 起的账号 freeze 机制

**结论**：**已证实**：2025 年 5 月 Telegram 引入"冻结（Frozen）"机制取代直接删号，冻结账号进入只读态——不能发消息、不能加群/订阅频道、**不能接收新消息、不能重新登录或添加新设备**，可通过 @SpamBot 申诉，不申诉则删号。对只读 userbot 的关键影响：冻结后 **session 无法重新登录**，抓取账号一旦冻结即等于失去，且"不能收新消息"意味着连被动读取也被切断。

**证据**（可信度：高，官方翻译平台 + 社区一手）
- [ChatList.FreezeAccount — Telegram Translations（官方客户端字符串）, https://translations.telegram.org/en/macos/chat_list/ChatList.FreezeAccount]：客户端内置冻结提示文案，证明机制存在于官方客户端。
- [What To Do If Your Account is Frozen? — Telegram Info, https://tginfo.me/frozen-account-en/, 2025]："In May 2025, a new mechanism was introduced: Telegram can now freeze accounts instead of deleting them right away"；限制清单：不能发消息/加群/订阅频道/收新消息/改资料/重新登录或添加新设备。
- [Appealing Telegram Account Restrictions, https://grokipedia.com/page/Appealing_Telegram_Account_Restrictions]：申诉路径 @SpamBot → telegram.org/support 表单。

**反面证据 / 未找到**（搜索词：`"This account is frozen" userbot third-party client ban`）：无官方文档说明"使用 Telethon/第三方 MTProto 客户端"本身是冻结触发条件；社区（BlackHatWorld 帖）案例多与新号、虚拟号、批量操作相关，与已确认结论"风险在新号/虚拟号"一致。

---

## 6. GramJS 归档公告 / teleproto 现状

**结论**：GramJS 归档公告一手确认，原文推荐迁 teleproto。**teleproto 的 npm 版本/周下载/维护者数未获取**（npm 页面 403）。

**证据**（可信度：高，一手）
- [gram-js/gramjs — GitHub, https://github.com/gram-js/gramjs, 抓取 2026-09-07]
  - 归档横幅原文："This repository was archived by the owner on Jul 14, 2026. It is now read-only."
  - README 原文："⚠️ This project is archived and no longer maintained."
  - 迁移指引：`npm install teleproto`，称与 GramJS 高度兼容，附迁移指南；后继仓库 https://github.com/sanyok12345/teleproto。

**反面证据 / 未获取**：teleproto 由个人账号（sanyok12345）维护，单人维护风险待 npm 数据核实（可 `npm view teleproto version time maintainers` 一条命令补查，无需网页）。

---

## 对选型的含义

1. **Telethon 可作主抓取器，但按"维护模式依赖"对待**：发版仍在（2026 年 4 版），作者已声明 v1 维护模式；锁定 1.44.0、把 layer 更新当作唯一必要升级理由，不指望新特性。
2. **账号是最脆弱资产，不是库**：2025-05 冻结机制下，冻结 = 不能重登录 + 不能收消息，session 直接作废。抓取账号必须用有历史的实名主号或长期养成的号，并把"账号被冻结"作为一级告警与降级路径（回落到 tdesktop 官方导出或 tdl）。
3. **FloodWait 无可靠 2025 后实测数据，须自测**：先在目标频道做一次带日志的全量拉取（`wait_time=1`、`flood_sleep_threshold=120`），记录实际 FloodWait 秒数作为项目内基线，不要引用外部数字。
4. **备用导出通道的字段完整性需 30 分钟本地验证**：tdl 默认 JSON 疑似不含 `edit_date`/`reply_to`（用 `--raw` 才有）、tdesktop 导出 `edited`/`reply_to_message_id` 未经官方页核实——两者各实跑一次即可闭环；生产 watcher 的 GramJS 债务已坐实（归档 07-14），teleproto 单人维护数据待 `npm view` 补查。