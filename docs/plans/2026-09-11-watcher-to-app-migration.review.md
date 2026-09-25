# watcher-to-app-migration 落地前 adversarial review

审查日期：2026-09-11。对象：`docs/plans/2026-09-11-watcher-to-app-migration.md` v0。

**总结论：修改后开工。共 P0 阻断 2 项、P1 必修 11 项、P2 建议 2 项；保留 watcher 配置存储的方向成立，但鉴权、缓存失效、切换基线和价格告警契约不能按 v0 直接实施。**

## 审查边界与证据口径

- 本次仅静态读取代码、文档；未运行测试、未访问 jp-24、未操作交易或发送通知。唯一新增文件为本 review。
- 已完整读取 `server.js`、`lib/trading-api.js`、`price-monitor.js`、watcher 下全部 8 个 `__tests__/*.js`；读取 `public/index.html` 的导航、表单、数据调用和计算逻辑；核对 `read_api.py` 的 DB 路径解析、两类 addon 读取、品种风险及其开仓调用链。下文引用的是当前工作区行号。
- §1.2 的 basicauth、卷权限、operator-query 用户、副本与无同步事实按派发者给定事实采信，不重复现场核验（`docs/plans/2026-09-11-watcher-to-app-migration.md:34`、`:35`、`:36`）。文件大小差本身不能量化三张配置表差异；切换仍需逐行比对。
- app 的现状仅以设计文档为输入；未将 `alert-personal` 的导航、安全存储或 attention 集成描述当成已核验代码事实。文档同时引用不同 worktree/分支，实施任务应固定同一 app 基线（`docs/plans/2026-09-11-watcher-to-app-migration.md:4`、`:39`）。
- 以下为设计缺口及可复现的代码契约冲突；涉及新接口的泄露、绕过等均描述触发条件，不声称未实现的接口已发生事故。P0 必须在开放入口或切换前消除，P1 必须补入对应 Phase 的设计与验收。

## 一、按严重度分级的问题清单

### P0-01：无限期沿用旧配置与 fail-closed 相互矛盾

**证据：** §2.2 和 T0-3 要求过期拉取失败后沿用旧值，仅标 stale；§4.2 却要求 watcher 不可达时拒绝（`docs/plans/2026-09-11-watcher-to-app-migration.md:67`、`:98`、`:104`）。当前开仓先调用账号/频道 addon（`services/control-plane/api/read_api.py:7567`），其中启用状态、映射冲突分别会导致拒绝（`:6012`、`:6058`、`:6176`）。feeder 直接读取真库路由（`scripts/hermes_signal_feeder.py:307`、`:349`）。

**影响：** 缓存中账号启用且 addon 较高时，用户在 watcher 禁用账号、降低 addon 或改绑频道，随后快照服务不可达，控制面可能无限期继续使用旧风险配置；标记 stale 本身不构成拒绝。feeder 新映射与控制面旧映射还会造成 409。反过来，控制面冷启动没有缓存时，走该 operator API 的所有 `open_position` 会因缺配置拒绝；不能据此声称所有交易链或减仓都必然被阻断。

**修法：** 明确刷新周期与授权有效期是两个参数，指定 `fresh_until`、`max_age`、首次失败、过期失败、无效快照、401 的状态机。安全默认建议超过 60s 未成功验证便拒绝依赖快照的开仓；如需重启宽限，必须明确有上限的旧配置授权窗口及禁用操作的传播延迟。展示读取可以继续返回 stale，执行读取必须强制检查有效期。预热每个 operator-query worker、后台刷新/单飞、请求超时及退避；单次订单从同一不可变快照取路由、addon、risk，不能在函数之间跨版本。新增版本与年龄到 dry_run 证据。恢复时主动刷新，不能以“按需”保证无请求情况下的 60s 自动恢复。

### P0-02：以可缺省入口头决定是否鉴权，未形成封闭信任边界

**证据：** v0 只在 `/api/` 且 `X-Watcher-Entry: mobile` 时校验 bearer，T0-1 明确要求非 `/w` 不校验；内网快照调用却没有定义该入口头（`docs/plans/2026-09-11-watcher-to-app-migration.md:51`、`:59`、`:102`）。当前路由本身无鉴权，静态媒体先于 API 注册（`bridge/services/telegram-watcher/server.js:68`、`:70`、`:863`）。容器内部监听所有地址、宿主只映射 loopback（`bridge/docker-compose.yml:157`、`:178`）。

**影响：** 缺头/错误头就成为“浏览器免 token”，内网快照的 token 可能只是被发送而从不被检查；可访问容器服务的客户端也不必经 basicauth。公网 `/w` 若遗漏注入或路由落入另一 handle，也会失去进程内鉴权。**浏览器路径伪造 `mobile` 头本身不会绕过仍然执行的 Caddy basicauth，通常只会增加 bearer 校验；不能将它误报成已证明的公网漏洞。** 真正缺口是服务端把“未证明来自可信浏览器入口”当成免鉴权。

**修法：** bearer 的身份、权限和轮换由 watcher 校验；Caddy 负责精确入口路由及浏览器 basicauth。移动和内网快照请求应无条件要求有效 token；浏览器豁免必须来自隔离监听入口或可验证的代理凭证，并由 Caddy 清除所有外来入口/actor 头后重新注入，不能只凭 `mobile/browser` 字符串或 loopback IP。快照配置独立只读服务身份，不能复用 app 写权限 token。缺失 token 环境配置应使相应入口拒绝或启动失败。鉴权置于受保护静态文件和 handler 之前；覆盖直连无头、伪造头、重复头、旧 token 撤销、未知入口、代理注入失效的负向测试。

### P1-01：快照版本算法依赖不存在的字段，且没有定义脱敏与一致性契约

**证据：** `version = 三表 updated_at 最大值 + 行数哈希`（`docs/plans/2026-09-11-watcher-to-app-migration.md:66`）。三张配置表均无 `updated_at`（`bridge/services/telegram-watcher/lib/trading-api.js:55`、`:76`、`:83`），写入也不更新时间（`:620`、`:753`、`:815`）。账号表含 Binance 密钥（`:57`），现有账号 GET 专门脱敏（`:371`、`:1002`）；新 snapshot 的 `accounts[]` 没有字段清单。

**影响：** 原样查询会失败；仅退化为行数无法发现同一行改值。即使补秒级时间戳，同秒多次修改及删旧行再插新行仍需防碰撞。裸 `SELECT *` 会新增密钥传播到控制面/app 的路径。跨表或跨版本读取可组成不曾存在的路由与风险组合。

**修法：** 定义 `schema_version` 与内容版本；在同一 SQLite 读事务中读取白名单字段，按稳定主键排序，对规范化、类型校验后的完整配置内容求摘要；或采用所有配置写事务共同递增的 revision。`generated_at` 单独表示生成时间，不作为变更版本。账号快照仅含身份、层级、执行账号、启用状态和所需风险字段，完全排除 key/secret/session。校验重复执行账号、悬空路由、非法父账号、负数/非有限数、缺失字段；有效空表与不可读表应明确区分。覆盖同秒更新、删除、等行数替换、无变更重读、并发写、损坏响应、无凭据输出。

### P1-02：已漂移副本不能作为“删前一致”的正确性基线，回滚也不只改 env

**证据：** 给定副本已漂移且无同步（`docs/plans/2026-09-11-watcher-to-app-migration.md:36`）；验收只选同一账户/品种比较删除前后（`:97`）；回滚写为保留备份并回滚 env（`:168`）。旧读取路径实际有三个 env 别名并在模块加载时解析（`services/control-plane/api/read_api.py:5813`、`:5840`）。

**影响：** 新读取正确使用真库仍可能不等于旧副本；反而错误沿用副本可能通过验收。只测 `source_channel=operator` 不会覆盖频道路由。新代码若已删除 SQLite reader，恢复 env 对 HTTP reader 无效；恢复陈旧备份会重引漂移。

**修法：** 按第三节 Q6 的“三路对照”先核准数据差异，再比较读取实现；覆盖四账户、全部路由、全部配置品种及异常状态。切换前预热、保留旧代码/读取开关及可用回退包；记录代码、env、服务重启、缓存清空的完整回滚步骤。回退数据应是回退时经校验的真库一致快照，或明确冻结配置编辑的窗口，不能默认使用 09-06 文件。只有影子对照通过、生产新 reader 生效、失败恢复验证完成后才移除活跃副本。

### P1-03：风险比例范围、主账号规则和旧 UI 测试不能直接“沿用”

**证据：** T2-1 要求 `(0,1]`、主账号唯一（`docs/plans/2026-09-11-watcher-to-app-migration.md:143`）；watcher 风险 API 接受任意非负有限数，包括 0 和大于 1（`bridge/services/telegram-watcher/lib/trading-api.js:810`、`:1049`）；控制面只采用 `0 < ratio <= 0.1`，其余及读取异常回退 env 默认值（`services/control-plane/api/read_api.py:6970`）。账号只对 `account_id`、`execution_account_id` 唯一，允许多个 main（`bridge/services/telegram-watcher/lib/trading-api.js:55`、`:199`、`:1146`）。旧 UI 测试断言乘法公式（`bridge/services/telegram-watcher/__tests__/account-config-ui.test.js:9`、`:20`），页面已用加法，旧公式仅留在注释中（`bridge/services/telegram-watcher/public/index.html:769`、`:1443`）。

**影响：** app 保存 0.2 可成功，但控制面采用默认 0.01；保存 0 也不是停止开仓。缺表/断网回退默认值并非文档所说“维持现状拒绝”。把“主账号唯一”理解为全局仅一个会破坏已有多 main 数据。正则测试可能匹配注释而绿，无法证明加权额计算正确。

**修法：** 明确区分品种 risk、账号 default risk、固定 addon 与仅为兼容保留的 multiplier；统一服务端与两端 UI 规则。建议品种按现控制面支持的 `(0,0.1]` 设计，越界明确拒绝；无该品种记录的默认策略单独定义，数据源失败不得伪装成正常缺省。若要支持更大比例必须另行明确风险策略变更，不能当迁移顺手放宽。主账号规则写成“ID 唯一、每个子账号恰有一个合法主账号”。新测试执行加法计算，覆盖 `addon = target - initial`、0、负数、空字符串、布尔、边界值；默认风险修改是否实际用于 V3 也必须在 UI 如实说明，不能承诺所有字段都改变名义额（`services/control-plane/api/read_api.py:7023`、`:7071`）。

### P1-04：`signal_operations` 不是现成的 HTTP 幂等账本，busy 验收没有可控时序

**证据：** v0 承诺同 `client_ref` 返回同结果并写入该表（`docs/plans/2026-09-11-watcher-to-app-migration.md:132`），表目前只有 `UNIQUE(signal_id, operation_type)`，无请求摘要/HTTP 结果/actor/source（`bridge/services/telegram-watcher/lib/trading-api.js:114`）。配置写入与返回之间没有幂等事务（`:457`、`:620`、`:753`）；DB 连接仅显式设置 WAL/FK（`:45`），异常统一返回 500（`:1205`）。群组写的是 JSON 文件（`bridge/services/telegram-watcher/server.js:227`、`:804`），价格 API 用 `priceMonitor.getDb()`（`bridge/services/telegram-watcher/lib/trading-api.js:959`）。现有 Python 也使用信号操作表（`bridge/services/telegram-watcher/skills/crypto-trader/scripts/db_manager.py:579`、`:601`）。

**影响：** 先改配置后写账本，崩溃后会重做；先占位后改配置，崩溃后可能虚假成功。同 ref 不同请求若无摘要会返回错误旧结果；直接套 signal namespace 会与现有记录混淆。SQLite 事务不能原子涵盖 JSON 文件或 Telegram 连接副作用。固定持锁 200ms 不必然报 busy：若 timeout 更长，应等待后成功。

**修法：** 配置使用独立幂等表，或明确保留旧 schema 兼容且隔离命名空间：键绑定可信 actor、操作类型与 client_ref，存规范化请求摘要、状态码、安全响应、变更版本。相同键不同摘要返回 409；查重、校验、业务写、revision、审计和成功结果在同一事务提交，事务外仅发送响应；响应丢失后原 ref 重试返回同结果。对 crash-before-commit、commit-before-response、并发同 ref、重启后重放、DELETE 重放逐一验收。明确保留期与服务端生成浏览器 request id 的规则。群组/连接操作单独定义可重入及恢复语义，不承诺跨存储“恰好一次”。busy_timeout 必须明确值，分别测短于 timeout 的锁等待成功、超过 timeout 的可重试 503/明确错误；使用独立进程持锁，避免同步 DB 调用堵住同进程解锁定时器。审计不得落凭据明文。

### P1-05：摘要确认后仍采用无条件后写覆盖，确认内容可能已失真

**证据：** 设计要求展示变更前后值，却又接受 app/站点同时编辑后写覆盖（`docs/plans/2026-09-11-watcher-to-app-migration.md:131`、`:138`）。PUT 从请求覆盖字段后直接写入，不带版本条件（`bridge/services/telegram-watcher/lib/trading-api.js:500`、`:620`），站点提交包含完整风险及启用字段（`bridge/services/telegram-watcher/public/index.html:1513`）。

**影响：** app 在“启用、addon=6000”的旧表单上确认另一字段，期间站点已禁用或降低 addon；app 随后的全表单提交可能恢复旧值。回读最终值和保留两条审计只能发现覆盖，不能保证用户确认了实际变化。

**修法：** 对风险、启用、执行账号和路由至少采用 `expected_version`/行 revision 的条件写；过期返回 409 并展示最新差异后重新确认。浏览器同样发送版本，服务端不能给缺版本请求开放覆盖通道。如坚持后写覆盖，应明确这是产品接受的风险并展示所有实际覆盖项；本 review 推荐修改。测试“两端读 v1、站点先写 v2、app 持 v1 提交被拒、重新确认后成功”，并结合幂等规定先识别已成功重放，不能把重放误判为版本冲突。

### P1-06：`/w/api/*` 不是白名单，媒体路径与鉴权链缺失

**证据：** 路由示例匹配整个 `/w/api/*`，但正文要求阻断登录/config（`docs/plans/2026-09-11-watcher-to-app-migration.md:60`）；功能表又需 `/media/*`（`:75`）与 app 读取 snapshot（`:134`）。媒体由独立静态路由提供（`bridge/services/telegram-watcher/server.js:70`），文件名是时间戳加消息 ID（`:251`），不是访问凭证。

**影响：** 若照示例实现，将连同登录接口一起反代；若只实现 `/w/api`，图片全部不通。若无鉴权直接开放 `/media`，知道/猜到 URL 即可下载私有频道附件；“缩略图”也不代表现接口做了缩略，实际下载的是原文件。

**修法：** 用第三节 Q3 的“方法 + 规范化路径 + 阶段”矩阵替代星号表，默认拒绝未知路径；登录/config 永不进入 mobile router。定义 `/w/media/:filename`，在 static 之前执行 bearer 校验，或签发短期资源级 URL；禁止把长期 token 放 query。只服务合法文件名及允许媒体类型，防止目录穿越，定义消息过期/取消关注后的可读范围、私有缓存和 token 清除后的设备缓存策略。app 图片请求需实际携带认证，跨域跳转不得转发 token；缺图不能阻塞消息文本。拒绝未授权 GET/HEAD/Range，以及路径编码、重复斜杠等绕过尝试。

### P1-07：Phase 1 的状态、消息对照与重连成功标准缺少真实响应契约

**证据：** `/api/status` 只有 configured/loggedIn/connected/watchGroups，无最近消息时间或 stale（`bridge/services/telegram-watcher/server.js:583`）；`lastUpdateAt` 会因空轮询成功刷新，表示活性（`:497`），已有测试刻意覆盖空轮询刷新（`bridge/services/telegram-watcher/__tests__/watcher-resilience.test.js:292`）。站点读 `/api/messages` 的内存/JSON ring（`bridge/services/telegram-watcher/public/index.html:1137`、`bridge/services/telegram-watcher/server.js:835`），app 计划读 SQLite 查询（`bridge/services/telegram-watcher/lib/trading-api.js:903`）。SQLite `id` 是本地自增、`msg_id` 才是 Telegram ID，时间为入库时间（`:281`、`:313`）；站点 ID/时间来自 Telegram（`bridge/services/telegram-watcher/server.js:404`）。重连 await 整个 `startListening()`，缺凭据直接 return、内部异常被吞后仍可能返回 HTTP 200（`:298`、`:563`、`:854`）。

**影响：** 299s/300s 测试没有定义依据字段，静默频道可能被误报故障；两端直接比较 `id` 永远不是同一身份空间。ring 与 DB 的时间窗口、去重和保存失败路径不同，也不能要求无条件 50 条集合一致。重连可能超过 3s 或返回 `ok:true, connected:false`，收到 200 不等于连接成功。

**修法：** 定义 `observed_at`、连接/监听状态、最近成功 Telegram 活动时间、最近已收录消息时间；区分连接失活、读取失败与仅无新消息，空频道无消息不当故障。对照统一为 `(channel_id,msg_id)`，限定相同频道/窗口/数量/截至水位；本地列表可用 DB id，但必须说明只在同一数据集内稳定。明确 UTC 解析、并列时间稳定排序、最多 500 条截断及是否需要历史翻页；网站网页预览数据比 DB 丰富，缩略图只覆盖有 `media_filename` 的行。重连定义 pending/成功/失败/需站点登录，分别设置请求与完成期限；3s 可要求显示回读或等待状态，不能要求 Telegram 必须完成重连。延迟、超时、无会话、重复点击、明确断开后 watchdog 不自启都应测试。

### P1-08：账号 CRUD 隐含 Binance 凭据录入，D4 的凭据边界只处理了 Telegram

**证据：** D4 理由为凭据操作留站点（`docs/plans/2026-09-11-watcher-to-app-migration.md:13`），Phase 2 仍迁入全部账号 CRUD（`:80`、`:130`）。POST 必须有 Binance key/secret（`bridge/services/telegram-watcher/lib/trading-api.js:386`、`:406`）；PUT 接受非空凭据替换（`:581`）；GET 返回掩码（`:1002`），空凭据保留原值已有测试（`bridge/services/telegram-watcher/__tests__/trading-api.test.js:556`）。

**影响：** D4 并未禁止 Binance 凭据进入 app，但当前设计没明确选择，账号“增”无法仅靠风险表单完成。若 app 把掩码回填 PUT，会把掩码写成新密钥。snapshot/幂等日志若序列化全部请求还会扩大凭据留存。watcher token 虽不等于 RISK_ADMIN，仍可改风控/停采集，其泄露影响不能只描述为“只影响 watcher”（设计 `:167`）。

**修法：** 建议账号创建、密钥轮换继续在站点；app 先编辑非秘密配置并删除相应移动创建白名单。若产品坚持完整 CRUD，应明确 Binance 凭据一次性录入、禁止持久化/日志/摘要回显、提交后清空，以及凭据更新权限；非凭据编辑完全不发送 key/secret，服务端拒绝掩码占位值。snapshot、审计、错误响应、客户端调试日志都需秘密哨兵测试。账号启用不是交易 RESUME，文案与请求路径要明确区分。

### P1-09：Phase 3 假定存在的价格推送链路不存在，触发删除标准与代码相反

**证据：** §7.1 写 Telegram 现有推送、attention 展示，§7.2 要求触发后删除（`docs/plans/2026-09-11-watcher-to-app-migration.md:151`、`:155`）。实际 `markTriggered()` 是 UPDATE；`triggerTrader()` 仅 console.log 后返回 false；主循环先标记再调用它（`bridge/services/telegram-watcher/price-monitor.js:93`、`:193`、`:246`）。Telegram 发送在 watcher 连通性告警函数内，并未接价格循环（`bridge/services/telegram-watcher/server.js:94`、`:139`、`:168`）。既有 price test 只证明不执行外部下单进程（`bridge/services/telegram-watcher/__tests__/price-monitor.test.js:81`）。

**影响：** 按 v0 只做 app CRUD，价格会触发但没有投递；列表中消失（过滤未触发）也不能证明记录被删除或消息送达。把健康告警通道误认为价格告警通道，会交付一个静默失效的提醒功能。

**修法：** Phase 3 必须二选一写清：缩小为“记录并展示触发状态”，移除推送承诺；或增加明确的价格事件发布与 attention/Telegram 接入任务、事件 ID、投递确认/重试/去重和所有者。保留 triggered 与 delivered 的区分，不能先永久标记触发再因发送失败丢事件。验收检查 DB 状态、事件落点、投递结果和 app 可见性；不以删除作为成功证据。新增通知不得恢复已移除的直接交易执行路径。

### P1-10：价格提醒的账户/仓位身份与价源环境未建模

**证据：** 设计将提醒绑定 V3 仓位并按订单清理（`docs/plans/2026-09-11-watcher-to-app-migration.md:151`、`:156`、`:160`）；现有 `order_id INTEGER` 指向站点 `active_orders.id` 的注释约定，表无账户、仓位、环境字段及实际 FK（`bridge/services/telegram-watcher/price-monitor.js:37`、`:51`）。按订单删除只比较 order_id（`bridge/services/telegram-watcher/lib/trading-api.js:988`）。价格环境启动时取账号表第一行并对全部告警共用（`bridge/services/telegram-watcher/price-monitor.js:24`、`:275`）；POST 只检查三个字段是否 truthy（`bridge/services/telegram-watcher/lib/trading-api.js:955`）。

**影响：** 把 V3 position/order ID 直接塞进旧字段，会出现孤立判断错误、跨账户误清理；多环境时全部使用首账号价源。非法 direction 会永不触发，负价可造成立即触发。只设置 canonical DB env 时 price-monitor 仍读旧变量/默认路径（`bridge/services/telegram-watcher/price-monitor.js:13`）；当前 compose 显式设置相同别名，不能把该潜在差异说成已发生的生产故障（`bridge/docker-compose.yml:136`）。

**修法：** 定义带来源命名空间的 `account_id + position_id/order_id + symbol + side + environment` 关联；旧站点订单另存来源与 ID。所有列表/删除均按该身份限定，不能按 symbol 推断归属。仓位读取 stale/失败不得判断“已关闭”并清理。明确市场环境与价源选择、首次加载/热更新行为、正有限 target_price、direction 枚举及 symbol/note 限制；允许用户手动仓位显式创建提醒，但禁止自动为无关联手动单制造清理/骚扰。统一 price DB 路径解析并验明 PRICE_MONITOR_ENABLED=false、表不存在、价源失败的错误状态。

### P1-11：部署验收默认四节点 ACTIVE，Phase 任务覆盖和依赖也不闭合

**证据：** Phase 0 要 Caddy restart 后四节点 ACTIVE（`docs/plans/2026-09-11-watcher-to-app-migration.md:61`、`:99`）；项目明确 RESUME 只能由用户指令触发、部署门禁须先完成（`AGENTS.md:7`、`:9`）。映射表把订单放 P1、§7 标题又放 Phase 3（设计 `:77`、`:148`）；群组选择映射 P2，但 §6 没有群组设计/测试（`:79`、`:129`）；A-2 一致性验收依赖 C-0，拆分表仅列 A-0/W-0（`:134`、`:183`）。

**影响：** 当前 HALTED 的账户也会被误判成部署不合格，执行者可能为“过验收”擅自恢复。群组、站点订单和推送没有完整任务归属；配置编辑独立交付时无法满足控制面一致性验收。仅按“前后改风险后 dry_run 变化”在真库做实验还会影响后续真实开仓。

**修法：** 改成记录部署前每节点状态、审计和心跳，部署后保持授权状态且恢复服务健康；HALTED 保持 HALTED，绝不把自动 RESUME 列为部署步骤。所有门禁完成后才进入必要重启窗口，失败有明确回退；控制面更新必须重启生效。把破坏性 CRUD、持锁和断网实验放隔离环境；生产只做已授权的最小变更及只读/dry_run 对照。群组、订单、媒体、snapshot 服务身份和价格发布分别补 owner/Phase/依赖；群组在正式迁入前保持站点操作可用。说明“二段式”这里只是 UI 摘要确认，并非控制面订单的 dry_run 授权协议。

### P2-01：四 Tab 信息架构可用，但性能标准还不可重复测量

**证据：** 现状三 Tab、新增信号 Tab 分离高频消息和低频配置（`docs/plans/2026-09-11-watcher-to-app-migration.md:15`、`:41`）；“200 条无掉帧、≤10 屏”与仅测试 memo（`:116`、`:123`）没有机型、构建、图像大小、指标定义。

**影响：** memo 测试通过不能证明图片解码、网络和列表滚动稳定；“信号汇总”和“告警 Tab”容易重复承担提醒管理。

**修法：** 固定最低目标真机、release build、200 条含图数据、网络条件和 60 秒滚动脚本，记录掉帧率/长帧/内存并给出项目接受阈值；先定指标再验收。信号 Tab 展示采集消息/简报及提醒入口，告警 Tab 展示已投递事件，设置负责低频配置。

### P2-02：“唯一写入者”应是被落实的边界，不能由 HTTP 架构图自动成立

**证据：** §1.2 称仓库只有 read_api 读取配置（`docs/plans/2026-09-11-watcher-to-app-migration.md:37`），但 feeder 也读路由（`scripts/hermes_signal_feeder.py:351`）；watcher 附带 Python 管理器可直接改三表（`bridge/services/telegram-watcher/skills/crypto-trader/scripts/db_manager.py:364`、`:451`、`:468`）。

**影响：** 若该脚本仍用于运维写入，会绕过新审计/revision/幂等。仅凭文件存在不能断言生产当前多写，但它是必须界定的兼容入口。

**修法：** 补充读写者清单；feeder 保持只读，遗留脚本停用写命令或改走同一配置服务。break-glass 写入也应规定审计与版本更新。D1 描述为目标约束，并给出验证手段。

## 二、D1–D6 逐条裁决

| 决策 | 裁决 | 理由与修订条件 |
|---|---|---|
| D1 watcher 唯一真相/写入者 | **同意** | 本次迁移可复用已有 CRUD、校验及真库，避免扩大到 PG 迁库；落实唯一写入者清单及事务边界，不代表现有 HTTP 接口无需修改。证据：`bridge/services/telegram-watcher/lib/trading-api.js:332`、`:382`；直写旁路见 `bridge/services/telegram-watcher/skills/crypto-trader/scripts/db_manager.py:451`。 |
| D2 进程内 bearer + `/w` | **修改** | 采用 watcher 身份/权限校验与 Caddy 路由分工，但反对“缺 mobile 头即免鉴权”。浏览器保留 basicauth，服务快照只读身份独立；按方法白名单及受保护媒体设计。证据：设计 `docs/plans/2026-09-11-watcher-to-app-migration.md:59`、`:60`；静态注册 `bridge/services/telegram-watcher/server.js:70`。 |
| D3 HTTP snapshot 替代副本 | **修改** | 支持 HTTP 取代无人同步副本，但不接受无限旧值、无字段契约、先删后验和只回滚 env。要有预热、有限有效期、三路对照和可执行回滚。直读卷是可行备选，给定权限问题并不证明重建一定覆盖命名卷权限；需比较持久挂载/组权限维护成本，不以未经验证的断言排除它。证据：设计 `docs/plans/2026-09-11-watcher-to-app-migration.md:12`、`:36`、`:67`；命名卷 `bridge/docker-compose.yml:131`；旧路径解析 `services/control-plane/api/read_api.py:5813`。 |
| D4 Telegram 登录/2FA 留站点 | **同意** | 登录状态机直接处理口令、二维码和 session，低频凭据入口保留合理；app 显示“需站点登录”，断开/重连使用明确状态。另补 Binance 凭据范围决策，不能由 D4 代替。证据：`bridge/services/telegram-watcher/server.js:624`、`:681`、`:738`、`:794`；账号密钥要求 `bridge/services/telegram-watcher/lib/trading-api.js:406`。 |
| D5 站点保留 | **同意** | Telegram 登录和配置回退仍需要它；两端必须共享新鉴权适配、写审计和版本校验，保留站点不等于恢复旧副本。证据：`bridge/services/telegram-watcher/public/index.html:640`、`:714`；设计 `docs/plans/2026-09-11-watcher-to-app-migration.md:14`、`:138`。 |
| D6 第四个信号 Tab，配置进设置 | **同意** | 与现站点消息/配置分区一致，按文档的三 Tab 基线增加后为四个；价格入口绑定仓位合理，但先解决身份模型，重复汇总只保留必要入口。最终导航需在固定 app 基线上验收。证据：`bridge/services/telegram-watcher/public/index.html:584`、`:597`；设计 `docs/plans/2026-09-11-watcher-to-app-migration.md:41`、`:83`。 |

## 三、对 §10 六个问题的直接回答

### Q1：D1/D3 的取舍与重启窗口

**可以保留 SQLite，由 watcher 提供 HTTP 读模型；不需要为 UI 迁移重做 PG 存储。** 现有三表和 CRUD 已成型，feeder 也只读同库（`bridge/services/telegram-watcher/lib/trading-api.js:55`、`:332`；`scripts/hermes_signal_feeder.py:307`）。HTTP 减少跨用户读凭据库的需求，但增加服务、token、网络和缓存有效期依赖；卷直读则须持久配置目录/DB/WAL 访问权限并控制凭据可读范围。不能说 HTTP 没有“副本”：内存缓存仍是副本，只是受控读模型。定时 rsync 仍需处理一致快照、延迟和失败，不能仅复制活动 SQLite 主文件便称解决（当前启用 WAL：`bridge/services/telegram-watcher/lib/trading-api.js:47`）。

**冷缓存且 watcher 不可达会拒绝经 operator API 的全部开仓；热缓存是否拒绝取决于明确的有效期。** 当前 addon 获取位于 `action == open_position` 分支，非开仓不应因本次缓存改造引入此依赖（`services/control-plane/api/read_api.py:7567`）。预热、短超时、单飞、主动刷新和先就绪再切换能减少拒绝窗；旧值最多可用多久必须落成参数及测试，建议按 P0-01 的 60s 安全默认。禁用/降低风险的传播时间也必须计入该窗口，而不是仅测试可用性。

### Q2：进程鉴权还是 Caddy；入口头能否绕过

**选择进程校验权限，Caddy 管理入口；入口头仅作标记，不作可信身份。** Caddy 固定注入 `mobile` 时，公网客户端不能靠省略该头绕过；浏览器伪造该头仍须先过 basicauth。问题是当前豁免规则给直连/注入遗漏开放无鉴权，且内网 snapshot 也落在豁免分支（设计 `docs/plans/2026-09-11-watcher-to-app-migration.md:51`、`:59`）。

需要独立只读快照 token、浏览器可信代理入口、移动 token 轮换/撤销和服务端 actor 绑定。双 token 轮换只定义两个有效值，并不自动具有“过期 token”的时钟语义；T2 的过期要明确指已撤销旧值或新增期限（设计 `docs/plans/2026-09-11-watcher-to-app-migration.md:59`、`:140`）。这是 P0-02 的关闭条件。

### Q3：路径白名单和媒体

建议以下为**显式端点矩阵**，所有未列方法/路径拒绝；动态 ID 必须编码并经过统一路径解析。HEAD 是否支持要显式决定，支持时与 GET 同权限。

| 阶段/身份 | 外部路径和方法 | 代码依据/差异 |
|---|---|---|
| P0 mobile | `GET /w/api/status` | `bridge/services/telegram-watcher/server.js:583` |
| P0 service | 内网 `GET /api/trading/config-snapshot`，只读 service token | 新增端点，须显式鉴权；设计 `docs/plans/2026-09-11-watcher-to-app-migration.md:66` |
| P1 mobile read | `GET /w/api/trading/messages`、`GET /w/api/trading/briefings` | `bridge/services/telegram-watcher/lib/trading-api.js:903`、`:885` |
| P1 mobile media | `GET /w/media/:filename`（按需 HEAD/Range，同认证） | 新增代理与保护；原静态服务 `bridge/services/telegram-watcher/server.js:70` |
| P1 mobile write | `POST /w/api/disconnect`、`POST /w/api/reconnect` | 此两项是写能力，应明确提前放行；`bridge/services/telegram-watcher/server.js:842`、`:854` |
| 订单定为 P1 或 P3 后放行 | `GET /w/api/trading/orders`、`GET /w/api/trading/orders/active` | 不是 `/orders*` 任意后缀；`bridge/services/telegram-watcher/lib/trading-api.js:844`、`:868` |
| P2 mobile groups | `GET /w/api/dialogs`、`POST /w/api/groups`，已选列表从 status 读 | 没有 GET groups；`bridge/services/telegram-watcher/server.js:804`、`:813`、`:589` |
| P2 mobile account | `GET /w/api/trading/accounts`、`PUT/DELETE /w/api/trading/accounts/:id`；POST 创建待凭据决策 | `bridge/services/telegram-watcher/lib/trading-api.js:344`、`:382`、`:496`、`:654` |
| P2 mobile config | `GET/POST /w/api/trading/channels`、`DELETE /w/api/trading/channels/:id`；`GET/POST /w/api/trading/risks`、`DELETE /w/api/trading/risks/:symbol` | 更新 channel/risk 用 POST upsert，没有 PUT；`bridge/services/telegram-watcher/lib/trading-api.js:735`、`:772`、`:803`、`:826` |
| P2 mobile version（如确需） | `GET /w/api/trading/config-snapshot` 或新增不含敏感配置的版本查询 | 原 §3 漏列、§6 却调用；建议写响应直接返回 version，测试侧查询控制面所用版本即可。设计 `docs/plans/2026-09-11-watcher-to-app-migration.md:134` |
| P3 mobile alerts | `GET/POST /w/api/price-alerts`、`DELETE /w/api/price-alerts/:id`、`DELETE /w/api/price-alerts/order/:orderId`、`GET /w/api/price-monitor/status` | 没有 PUT 告警；order 删除须完成身份改造后才放行。`bridge/services/telegram-watcher/lib/trading-api.js:927`、`:931`、`:952`、`:972`、`:988` |
| 永不 mobile 放行 | `/api/config`、所有 `/api/login/*`（含 QR/status/password）、站点静态入口；无需暴露 `/healthz` | 凭据/健康接口分别见 `bridge/services/telegram-watcher/server.js:573`、`:594`、`:607`、`:768` |

媒体需要认证，而不是依赖 URL 难猜；采用 P1-06 的受保护文件接口。外部访问日志记录一个 403 不足以证明未到 watcher，应结合路由命中/upstream 字段或后端请求计数，验证拒绝确实发生在代理层（设计验收要求见 `docs/plans/2026-09-11-watcher-to-app-migration.md:96`）。

### Q4：幂等与 SQLite busy

**现有表不够，扩展事务语义后可复用，但独立幂等表更清楚。** `signal_operations` 的唯一键针对信号与操作，当前 Python 还使用它；加两列 source/actor 并不能提供相同 HTTP 响应、请求冲突检测或原子提交（`bridge/services/telegram-watcher/lib/trading-api.js:114`；`bridge/services/telegram-watcher/skills/crypto-trader/scripts/db_manager.py:601`）。详细键、事务和故障测试见 P1-04。

**busy 要定义超时与结果，不应规定任何 200ms 锁都失败。** 事务应短、不含网络 await；有限等待后可重试，UI 复用同 ref；审计失败则配置事务回滚。条件写保护解决不同 ref 的并发覆盖，幂等解决同 ref 重放，两者不能互相替代（现无条件 PUT：`bridge/services/telegram-watcher/lib/trading-api.js:620`；设计两种场景：`docs/plans/2026-09-11-watcher-to-app-migration.md:138`、`:145`）。

### Q5：信息架构与留站点功能

**同意第四个信号 Tab 和配置进设置。** 站点本来就将消息、Telegram 设置、交易配置分区（`bridge/services/telegram-watcher/public/index.html:584`），文档 app 基线已有三个 Tab（`docs/plans/2026-09-11-watcher-to-app-migration.md:41`）。价格提醒可以从仓位创建、在信号页管理，已投递事件在告警页查看，但身份关联必须按 P1-10 完成。

**Telegram API 凭据、电话/QR/2FA 登录和改绑继续留站点；建议 Binance 账号创建/密钥轮换本轮也留站点。** 前者直接保存 session，后者 POST 强制要求 key/secret（`bridge/services/telegram-watcher/server.js:738`；`bridge/services/telegram-watcher/lib/trading-api.js:406`）。app 保留状态、断开/重连、非秘密配置编辑。若保留全部账号 CRUD，则按 P1-08 明确新的秘密输入边界。站点订单是独立读模型，只作可标注来源/时间的参考，不能拿站点 INTEGER id 当作 V3 仓位身份（`bridge/services/telegram-watcher/lib/trading-api.js:88`、`:844`）。

### Q6：顺序与 Phase 0 前后对照方法

**总体顺序可以保留，但 Phase 0 应拆为“新增并验证—影子比较—切换—移除副本”，部署前置条件不是四节点变 ACTIVE。** 当前设计先删再作单账户比较且副本已漂移，无法判定差异原因（`docs/plans/2026-09-11-watcher-to-app-migration.md:36`、`:91`、`:97`）；禁止隐式 RESUME 的约束见 `AGENTS.md:7`。

可执行对照程序（本次未执行）：

1. **数据基线。** 部署执行者对旧副本和真库分别取得 SQLite 一致读取快照，导出三表非秘密字段、主键/行数/内容摘要；列出四账户、每个频道目标、启用/层级/addon、每个品种比例的差异及预期裁决。不能用 DB 文件大小或 HTTP 200 代替这一步。依据：给定漂移 `docs/plans/2026-09-11-watcher-to-app-migration.md:36`，所需字段/约束 `services/control-plane/api/read_api.py:5938`、`:6058`。
2. **读取逻辑三路对照。** 对 A=旧 reader+旧副本、B=旧 reader+真库一致快照、C=新 reader+同一真库 HTTP 快照分别计算。A/B 差异说明既有漂移；B/C 必须在成功值、错误码和拒因上等价，除本 review 要求明确修订的风险缺省/失败策略。重用现有账号校验矩阵，不降低父账号、重复执行账号、禁用、addon 非有限数等拒绝规则（`services/control-plane/api/read_api.py:5993`、`:6024`、`:6037`、`:6070`）。
3. **固定计算输入。** 隔离环境固定 real_equity/available_balance、限价 entry、stop_loss、caps、account_id、source_channel、client_ref 和 dry_run；不得比较两次实时行情/权益变化后的裸 notional。分别覆盖 operator 来源与真实频道来源、显式/自动金额、有效/缺失品种配置，并检查 `account_equity_basis`、`risk_sizing`、路由目标、version。现已有固定权益加法测试可作为方法基线（`tests/control-plane/api/test_operator_account_registry.py:547`），实际计算字段见 `services/control-plane/api/read_api.py:7032`、`:7098`。
4. **失败注入。** 冷启动 watcher 不通、热缓存 TTL 内/外、401、损坏 JSON/缺表字段、同秒改值、HTTP 超时、多个 worker、重启和回滚；对有缓存/无缓存分别断言，不再同时要求“旧值继续用”和“一断就拒”。对非开仓补不新增快照依赖的回归（`services/control-plane/api/read_api.py:7567`）。
5. **就绪与生产观察。** watcher 接口和 token 先就绪，operator-query 所有 worker 预热成功并记录版本，再切新 reader、重启对应服务。外部路由通过白名单和浏览器回归后，执行已授权的只读/dry_run 冒烟；副本移除在最后。生产 dry_run 按当次权益/价格归一化解释结果，不要求时变数值机械相同。版本、请求 ID、实际服务状态和审计/心跳均留证据；不发真实订单或 RESUME 以使验收通过（`AGENTS.md:7`、`:10`；设计 dry_run 目标 `docs/plans/2026-09-11-watcher-to-app-migration.md:97`）。

## 四、§4–§7 各 Phase 的验收/测试可执行性

| Phase | 现标准评估 | 必补可执行标准与退出条件 |
|---|---|---|
| **Phase 0 / §4** | **修改后可执行，当前不能通过门禁。** token 正反例、存储自检可以测；版本算法不存在、stale 语义冲突、漂移基线和 ACTIVE 前提不成立。证据：设计 `docs/plans/2026-09-11-watcher-to-app-migration.md:95`、`:97`、`:99`、`:103`、`:104`。 | T0-1 加未知/伪造入口、直连 snapshot、只读 service 权限、token 未配置/撤销及浏览器回归；T0-2 加无敏感字段、内容变化/删除/同秒写、三表事务与 schema 校验；T0-3 使用 fake clock 验证 59/60/61s 与选定 max_age、冷/热缓存、单飞、超时、401、重启；T0-4 遍历完整方法/路径矩阵并证明拒绝未触达 handler；T0-5 加 base URL 与 token 不匹配、轮换失败及不回退 fixtures。完成 Q6 三路对照、版本可观察、完整回退演练，才允许移除副本。 |
| **Phase 1 / §5** | **部分可执行。** 三态/确认框/解析器可测；最近消息时间缺字段，两端 ID 不同，“重连 3s 成功”“200 条无掉帧”未定义。证据：`bridge/services/telegram-watcher/server.js:583`、`:854`；`bridge/services/telegram-watcher/lib/trading-api.js:281`；设计 `docs/plans/2026-09-11-watcher-to-app-migration.md:111`、`:116`、`:117`。 | T1-1 用脱敏的真实响应 fixture，明确 ring/DB 身份转换和 UTC；T1-2 区分活性、静默、接口失败并测 299/300/301s；T1-3 除 memo 外按 P2-01 测真实滚动；T1-4 覆盖长重连、失败、无 session、取消、重复点击、断开后不自恢复；T1-5 保留守卫，但不能代替行为测试。加前后台停轮询、恢复刷新、频道筛选与乱序响应、媒体认证/404/大图/缓存测试。简报空/错误/长内容要验收；订单若归 P1，补过滤、状态、账户来源和截断标准。 |
| **Phase 2 / §6** | **修改后可执行。** 摘要取消不请求、回读可测；T2-1 与现代码不符，T2-3 仅连续两次调用不能验证崩溃一致性，200ms busy 断言不确定。证据：设计 `docs/plans/2026-09-11-watcher-to-app-migration.md:138`、`:139`、`:143`、`:145`；`bridge/services/telegram-watcher/__tests__/account-config-ui.test.js:20`。 | T2-1 统一风险边界、加法、唯一 ID、父子环境和禁用规则，凭据操作范围先定；T2-2 加过期版本冲突、真实前后差异、mask 不回写；T2-3 按 P1-04 测事务各 crash 点、跨进程并发、同键异 payload、响应丢失、重启和 DELETE 重放；独立进程注入短/长 busy 锁。T2-4 在隔离 watcher+控制面用固定输入检查 version 与新配置生效，不能只验数值“变化”。补群组保存/空选择/输入合法性、未保存取消、监听与路由两个列表独立、完整列表与增量选择策略；dialogs 仅取 100 个，不能因候选列表截断丢掉原已选群（`bridge/services/telegram-watcher/server.js:818`、`bridge/services/telegram-watcher/public/index.html:1130`）。账号删除补子账号/路由/活动单约束，浏览器和 app 审计均具可信 source/actor，测试数据不落真实生产配置。 |
| **Phase 3 / §7** | **当前推送与删除验收不可执行，需补设计。** 现循环只记录触发；T3-1/T3-2 不覆盖真实投递与价源。证据：`bridge/services/telegram-watcher/price-monitor.js:193`、`:246`；设计 `docs/plans/2026-09-11-watcher-to-app-migration.md:155`、`:159`。 | 先定“仅展示”或“新增事件投递”范围及 owner，再写验收。用隔离测试价源覆盖 above/below 等值与未达、非法值、行情缺失/超时、停用监控；触发记录只一次，投递失败可恢复且有结果，重启不丢待投递事件。覆盖四账户同 symbol、mainnet/testnet、V3 仓位与站点 order ID 冲突、仓位读取 stale 时不误判孤立、删除作用域。通知集成要提供实际 attention 接收/显示证据，不因 Telegram 健康告警已存在而跳过；保留“不直接执行交易”守卫。若订单归本 Phase，新增订单映射、过滤和读模型来源测试。 |

现有测试的可复用边界：账号 API 测试确有真实临时 SQLite+Express，适合扩展迁移/层级/CRUD（`bridge/services/telegram-watcher/__tests__/trading-api.test.js:68`、`:641`）；resilience 使用 VM 和假的 Express，`app.use()` 为空，**不能证明新鉴权中间件有效**（`bridge/services/telegram-watcher/__tests__/watcher-resilience.test.js:88`）；price-monitor 测试使用假 DB，不能证明触发持久化/通知投递（`bridge/services/telegram-watcher/__tests__/price-monitor.test.js:10`、`:81`）。`env-flags.test.js:6`、`telegram-proxy.test.js:89`、`signal-importer.test.js:53`、`watched-entry-routing.test.js:77`（均位于 `bridge/services/telegram-watcher/__tests__/`）应作为不改变采集/重连/直接执行边界的回归，不能替代迁移端到端验收。

## 五、收尾意见

建议先修订 D2/D3、冻结非秘密 snapshot 与风险参数契约、确定价格告警交付范围，再派发 W-0/C-0；其余功能沿现有分阶段迁移。该建议依据 P0-01/P0-02 及 §4/§7 的明确契约冲突（`docs/plans/2026-09-11-watcher-to-app-migration.md:59`、`:67`、`:98`、`:151`；`bridge/services/telegram-watcher/price-monitor.js:246`）。本次没有执行测试，以上不作任何测试通过或生产状态声明。

**一句话总结论：修改后开工——先关闭 2 项 P0 并将 11 项 P1 的修法和验收补入对应阶段，保留 watcher 真库与 HTTP 读模型的总体方向。**
