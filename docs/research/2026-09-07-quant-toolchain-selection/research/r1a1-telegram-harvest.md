# Telegram 频道全量历史抓取工具候选与封号风险（第 1 轮广度收集）

> 预算内 12 次调用已用完（1 次工具加载 + 11 次搜索），未做二次页面抓取；下列"版本/日期"以搜索摘要为准，标注可信度。截止 2026-09-07。

## 1. 抓取工具候选表

### 1.1 总览

| 候选 | 定位 | 最新版本与日期 | 维护状态 | 按 channel+msg id 精确取 | date / edit_date / reply_to / 媒体 | 批量拉历史限流口径 | 会话隔离与只读用法 | 官方文档 | 疑虑 |
|---|---|---|---|---|---|---|---|---|---|
| **Telethon**（Python） | MTProto 用户端库，最成熟的 Python 方案 | **1.44.0**，文档 PDF 标注 2026-06-15 [Telethon Documentation Release 1.44.0, https://media.readthedocs.org/pdf/telethon/stable/telethon.pdf, 2026-06-15]（高） | **GitHub 仓库 2026-02-21 归档，迁移至 Codeberg** `https://codeberg.org/Lonami/Telethon`，仍在发版 [Releases · LonamiWebs/Telethon, https://github.com/LonamiWebs/Telethon/releases]（高） | 是：`client.get_messages(channel, ids=[...])` / `iter_messages(min_id, max_id, offset_id)`；底层 `messages.getHistory` [GetHistoryRequest, https://tl.telethon.dev/methods/messages/get_history.html]（高） | `Message` 对象带 `date`、`edit_date`、`reply_to`、`media`（MTProto 原生字段），可 `download_media` | `iter_messages` 有 `wait_time` 参数；`getHistory` 单页上限 100 条 [Issue #174/#290, https://github.com/LonamiWebs/Telethon/issues/174]（高）；FloodWait 典型值未见 2025 后官方口径（见下文） | SQLite `.session` 文件，**同一 session 被两处使用会 `database is locked` / `AuthKeyDuplicatedError`**，官方 FAQ 要求"一处一 session" [FAQ, https://docs.telethon.dev/en/stable/quick-references/faq.html]（高） | https://docs.telethon.dev/en/stable/modules/client.html | 主仓库迁移到 Codeberg，PyPI 仍正常；v2 alpha 与 v1 API 不兼容，选 v1 |
| **Pyrogram 原版** | 已停止维护 | — | **不再维护** [Kurigram README, https://github.com/KurimuzonAkuma/kurigram]（高） | — | — | — | — | https://docs.pyrogram.org/ | 不应选 |
| **Kurigram**（Pyrogram 分叉） | 声称 drop-in 替换，跟进 Gifts/Stories/Topics/Business | 版本号未取到（搜索词 `Kurigram PyPI version 2026`）；PyPI 有页 [Kurigram · PyPI, https://pypi.org/project/Kurigram/]（中） | 活跃 [GitHub kurigram, https://github.com/KurimuzonAkuma/kurigram]（中） | `get_messages(chat_id, message_ids=[...])`、`get_chat_history(offset_id, limit)`（承袭 Pyrogram，本轮未逐字核实）（中） | `Message.date`、`edit_date`、`reply_to_message`、`download_media` 承袭 Pyrogram（中） | 承袭 Pyrogram：`FloodWait` 异常需自处理，默认 `sleep_threshold=10s` 内自动睡（中，来自记忆，未核实） | 自有 `.session` SQLite 格式，与 Telethon **不通用**；同样禁止并发共享 | https://kurigram.icu/ 、https://kurigram.live/ | 个人维护，两个官网域名并存；PyPI 包名大小写 `Kurigram` |
| **pyrofork**（Pyrogram 分叉） | Mayuri-Chan 维护，加 Quote Reply / Topics / Mongo session | **最新发布 2025-12-10** [pyrofork · PyPI, https://pypi.org/project/pyrofork/]（高） | 活跃（截至 2025-12） | 同上 | 同上 | 同上 | 支持 MongoDB session 存储（利于与生产会话物理隔离） | https://pyrofork.mayuri.my.id | 仍是个人维护；与 Kurigram 二选一即可 |
| **GramJS**（`telegram` npm，生产在用 2.26） | TS/JS MTProto 客户端 | **2.26.22**，"一年前发布" [telegram - npm, https://www.npmjs.com/package/telegram]（高） | **GitHub 仓库 2026-07-14 归档**，npm 页标注"已归档、不再维护，开发转移到 teleproto" [Releases · gram-js/gramjs, https://github.com/gram-js/gramjs/releases]（高） | `client.getMessages(entity, {ids})` / `iterMessages({minId,maxId,offsetId})` [messages.GetMessages, https://gram.js.org/tl/messages/GetMessages]（高） | 原生 MTProto `Api.Message`：`date`、`editDate`、`replyTo`、`media`；`downloadMedia` | `getMessages` 的 `waitTime` 默认仅在 >3000 条时为 1s；`floodSleepThreshold` 默认 60s 内自动睡 [Handling Errors, https://painor.gitbook.io/gramjs/getting-started/handling-errors]（高） | `StringSession`/`StoreSession`，**不可与生产 watcher 共用同一 session 串**（同 auth key 多处登录会触发 `AUTH_KEY_DUPLICATED` 使生产掉线） | https://gram.js.org/ | **生产依赖已进入归档状态**，是独立于本课题的风险；抓取任务用它的唯一好处是团队熟悉 |
| **teleproto**（GramJS 活跃分叉） | 2025 从 GramJS 分叉，"换包名即可迁移" | v1.225.3（ref 站点标题）[teleproto - v1.225.3, https://ref.teleproto.dev/]（中） | 活跃 [GitHub sanyok12345/teleproto, https://github.com/sanyok12345/teleproto]（中） | 同 GramJS | 同 GramJS | 同 GramJS | 同 GramJS | https://ref.teleproto.dev/ 、https://www.npmjs.com/package/teleproto | 单人维护（sanyok12345）；版本号跳到 1.225.x 与 GramJS 2.26 不对应，需确认兼容 |
| **tdl**（Go CLI, iyear） | 面向"下载/导出"的命令行工具 | 版本号未取到（搜索词 `iyear/tdl releases 2026`），文档页 2024-06/11 更新 [Introduction, https://docs.iyear.me/tdl/]（中） | 活跃（推断，中） | `tdl chat export -c CHAT` 支持按 **id 区间 / 时间区间 / 关键字** 过滤 [tdl chat export, https://docs.iyear.me/tdl/more/cli/tdl_chat_export/]（高） | **默认只导出含媒体的消息**，可加 `--all`（文档："exports all messages containing media"）[Export Messages, https://docs.iyear.me/tdl/guide/tools/export-messages/]（高）；JSON 字段是否含 `edit_date`/`reply_to` **未核实** | 内置限速/重试（gotd 底层），具体 FloodWait 口径未见文档 | 独立 namespace 登录（`tdl login -n xxx`），天然与生产隔离；支持 Telegram Desktop tdata 导入 | https://docs.iyear.me/tdl/ | 定位是"下载器"，编辑版本、reply 链等元数据保真度需验证；不适合作为主抓取器，适合补媒体 |
| **Telegram Desktop 官方"导出聊天记录"** | 官方 GUI 导出，HTML/JSON | 随 TDesktop 版本走（官方 schema：https://core.telegram.org/import-export）[Telegram Data Export Schema, https://core.telegram.org/import-export]（高） | 官方持续维护 | **否**：只能整聊天导出，可按日期范围，不能按 id 精确取 | `result.json` 每条含 `id`、`date`、`date_unixtime`、`edited`/`edited_unixtime`、`reply_to_message_id`、`text_entities`、媒体相对路径（社区解析器验证 `reply_to_message_id` 保留但父消息可为 null）[telegram-chat-parser, https://github.com/innerdvations/telegram-chat-parser]（中）；**只保留最终编辑版本，不含编辑历史**（推断，中） | 官方导出速度由服务端控制，大频道可能耗时数小时；**导出请求本身走账号，首次导出触发 24h 等待**（记忆，未核实，低） | 用哪个账号登录 TDesktop 就是哪个账号；不需要 API_ID | https://telegram.org/blog/export-and-more | 无 id 精确重取、无编辑历史、不可脚本化增量；但作为**对照基准（ground truth）**很好 |
| **TDLib 直接绑定**（pytdbot / aiotdlib） | 官方 C++ 客户端库，Python 异步绑定 | 版本未取到（搜索词 `pytdbot pypi 2026`、`aiotdlib release 2026`）[Pytdbot · PyPI, https://pypi.org/project/Pytdbot/]（中） | 官方 tdlib 活跃；绑定为第三方 | `getChatHistory(chat_id, from_message_id, offset, limit)`、`getMessage(chat_id, message_id)` [TDLib getChatHistory, https://core.telegram.org/tdlib/docs/classtd_1_1td__api_1_1get_chat_history.html]（高） | `message.date`、`edit_date`、`reply_to`、`content`；媒体需 `downloadFile` | **返回条数由 TDLib 自行决定**（"for optimal performance the number of returned messages is chosen by TDLib"），需循环直到空（高）；TDLib 内部处理 FloodWait，透明 | TDLib 自带本地数据库目录，天然隔离；本地缓存是"客户端语义"，`only_local` 可离线读 | https://core.telegram.org/tdlib | 部署最重（需编译/预编译 tdjson）；只是为了拉 5 个频道显得过重；但**历史一致性最接近官方客户端** |

### 1.2 限流口径（跨库通用，MTProto 层）

| 事实 | 来源 | 可信度 |
|---|---|---|
| `messages.getHistory` 单请求上限 100 条；库层分页（Telethon `iter_messages`、GramJS `iterMessages`、Pyrogram `get_chat_history`）自动翻页 | [Issue #174, https://github.com/LonamiWebs/Telethon/issues/174] | 高 |
| Telethon `iter_messages(wait_time=...)`：当 limit 很大时库默认插入等待；文档明说"Telegram 会通过 FloodWaitError 让你减速" | [TelegramClient docs, https://docs.telethon.dev/en/stable/modules/client.html] | 高 |
| GramJS `getMessages` 的 `waitTime` 仅在 >3000 条时默认 1s；`floodSleepThreshold` 默认 60s | [Handling Errors, https://painor.gitbook.io/gramjs/getting-started/handling-errors] | 高 |
| 多频道频繁调 `GetHistoryRequest` 会触发 FloodWait | [Issue #494, https://github.com/LonamiWebs/Telethon/issues/494] | 中（老 issue） |
| **2025 后 FloodWait 典型秒数官方口径：未找到公开信息**（搜索词：`telethon iter_messages FloodWait channel history 2026`、`FLOOD_WAIT getHistory seconds 2025`）。社区经验值（记忆，低）：读历史一般为 0–30s 级，比 `ResolveUsername`/`GetParticipants` 宽松得多 | — | 低 |

### 1.3 会话隔离（本项目硬约束）

| 事实 | 来源 | 可信度 |
|---|---|---|
| Telethon FAQ：`database is locked` = 同一 session 被两个进程/客户端使用；解决方案是"两个客户端就用两个 session"，同账号多处用需各自登录 | [FAQ, https://docs.telethon.dev/en/stable/quick-references/faq.html] 、[Issue #637, https://github.com/LonamiWebs/Telethon/issues/637] | 高 |
| 同一 auth key 在两处并发会导致 `AuthKeyDuplicatedError`，**另一端会被踢下线**——直接威胁生产 watcher | [Telethon FAQ 同上] | 高 |
| 不同库 session 格式互不通用（Telethon SQLite / Pyrogram SQLite / GramJS StringSession / TDLib db 目录），不存在"顺手复用生产 session"的路径 | 各库文档 | 高 |

## 2. 封号与合规

### 2.1 官方条款

| 条款要点 | 来源 | 可信度 |
|---|---|---|
| Telegram API ToS：所有第三方客户端必须遵守 Telegram ToS；不得骚扰/垃圾信息；未见**明文禁止**用户账号读取自己已加入频道的历史 | [Telegram API Terms of Service, https://core.telegram.org/api/terms] | 高 |
| Bot API 条款与本课题无关（Bot 无法读频道历史，只能收实时更新）——决定了必须用用户账号 | [Telegram Bot Platform Developer ToS, https://telegram.org/tos/bot-developers] | 高 |
| 2025-05 Telegram 引入账号"freeze"机制（细节未取到；搜索词 `telegram account frozen May 2025 spam freeze`） | [Grokipedia: Appealing Telegram Account Restrictions, https://grokipedia.com/page/Appealing_Telegram_Account_Restrictions] | 低 |
| 限制/申诉入口为 `@SpamBot` | 同上 | 中 |

### 2.2 2025–2026 封禁案例与风险分层

| 案例 / 结论 | 来源 | 可信度 |
|---|---|---|
| 风险分层：**频道消息与群组历史抓取 = 低风险**，"新闻聚合/分析/监控类工具用 Telethon 大规模抓频道历史多年无账号后果"；**成员列表抓取 = 高风险**，重度使用几天内触发限制 | [Telegram Scraper: What Works and What Gets You Banned, https://clura.ai/blog/telegram-scraper]（商业博客，2025） | 中 |
| Telethon Issue #3955 "My account is banned"、#4150 "Auto Ban when using"：**新注册的虚拟号 + Telethon 登录后不到一天被封**；共同特征是虚拟号/新号，而非读历史本身 | [Issue #3955, https://github.com/LonamiWebs/Telethon/issues/3955]、[Issue #4150, https://github.com/LonamiWebs/Telethon/issues/4150] | 中 |
| 新账号限制常因"号码此前被用过"或"虚拟号码"触发；限制内容是发消息给非联系人/进群发言，**不是读取** | [Radist.Online: Telegram restrictions/Number bans, https://docs.radist.online/en/our-products/integrations/telegram+kommo.com/telegram-restrictions-number-bans/] | 中 |
| **未找到**2025–2026 因"纯只读拉取频道历史"被封的公开案例（搜索词：`telegram userbot ban scraping channel history telethon 2025`、`banned reading channel history telethon 2026`） | — | — |

### 2.3 独立只读账号 vs 复用主账号

| 维度 | 复用主账号（生产 watcher 同一账号，不同 session） | 独立只读账号 |
|---|---|---|
| 生产连带风险 | 同账号被限制/冻结则 watcher 一起失效；若误用同一 session 直接 `AUTH_KEY_DUPLICATED` 踢线 | 完全隔离 |
| 封号概率 | 老账号、有真实使用历史，本身最抗风控 | 新号 + 虚拟号是 Issue #3955/#4150 的共同特征，最易触发 |
| 频道可见性 | 已在 5 个频道内，私有频道也能读 | 需重新加入；私有频道需邀请链接；新号短时间加多个频道是风控信号 |
| 推荐 | 若必须用主账号：**新建独立 session 登录**（会在设备列表多一个"设备"），限速保守 | 若用独立号：**实体 SIM、非虚拟号**，warm-up 后再抓 |

### 2.4 新账号 warm-up 建议（社区经验，可信度低–中，无官方口径）

搜索词：`telegram new account warm up before scraping 2025`——**未找到 2025 后系统性公开信息**，以下为综合社区惯例（低）：
1. 用实体 SIM 注册，在官方客户端（手机 App）完成注册与首日使用，设置头像/用户名，2FA。
2. 前 3–7 天只做人类操作：手动加入目标频道（一天 1–2 个）、正常浏览。
3. 首次 API 登录用官方 App 确认设备登录提示；首次抓取只拉 1 个频道、`wait_time` ≥ 1s、分批不超过几千条/小时。
4. 避免：`GetParticipants`、`ResolveUsername` 高频、批量加入频道、代理 IP 与注册 IP 地理差异过大。

## 3. 对本项目的初步含义

1. **主抓取器选 Telethon 1.44（Python）**：字段最全（`date`/`edit_date`/`reply_to`/媒体）、按 id 精确重取、分页与 FloodWait 处理成熟；仓库迁到 Codeberg 但仍在发版。Kurigram/pyrofork 为备选。用 **独立 `.session` 文件 + 独立 API_ID**，绝不触碰生产 watcher 的 GramJS session。
2. **生产依赖 GramJS `telegram@2.26` 已归档（2026-07-14）**，活跃分叉为 teleproto——这是独立于本课题的技术债，建议另立任务评估迁移；抓取任务不要再叠加在 GramJS 上。
3. **"编辑版本"无法通过历史 API 回溯**：MTProto `getHistory`/TDesktop 导出都只给最终版本 + `edit_date`。要拿编辑序列只能靠实时监听 `UpdateEditMessage` 落盘（生产 watcher 或抓取器常驻）。研究样本应明确标注"仅最终版"。
4. **封号风险主要在账号属性而非读取行为**：只读频道历史属低风险类；风险集中在"新号/虚拟号"。若用主账号，新开独立 session、保守限速（≥1s/页）；若用独立号，实体 SIM + ≥1 周 warm-up。TDesktop 官方导出可作为一次性 ground truth 校验抓取完整性。

**未核实项清单**（下一轮可补）：Kurigram/tdl/pytdbot 精确版本号与发布日期；tdl JSON 是否含 `edit_date`/`reply_to`；2025-05 "freeze" 机制细节；FloodWait 典型秒数的 2025 后数据。