# panel-and-app-upgrade-v1 集成报告

- 部署时间：2026-08-31 15:44Z（切换窗口 21 秒）
- 部署批次：`20260831T153547Z`
- 执行：G0（协调窗口）
- 设计真相：`docs/plans/2026-08-30-panel-and-app-upgrade-v1.md` v1.1

## 1. 交付内容

| 目标 | 来源 | 落点 | 状态 |
|---|---|---|---|
| 控制面读模型 | goal/backend-v1 `5a56f1f` | /srv/trader-v3/services/control-plane/api/（5 新模块 + read_api.py） | ✅ |
| 契约 schema | 同上 | /srv/trader-v3/packages/contracts/v1/（mirror/reconcile/outcomes） | ✅ 补齐 e2a37f6 缺口 |
| 面板 | goal/frontend-v1 `332b10a` | /srv/trader-v3/dashboard/dist（bundle `index-DK7MJT14.js`，与本地构建哈希一致） | ✅ |
| Caddy 路由 | 新增 | `/api/reports/*`、`/api/risk/*` → bridge:8000 | ✅ 公网 200 |
| bridge M2g | 同 backend 分支 | /srv/trader/apps/api/app/services/report_snapshot.py | ✅ 容器已重启 |
| app Trading 区 | goal/trading-v1 `edfd87c` | 手机 com.pudutech.attention（67MB） | ✅ 已装并运行 |

## 2. 部署前拦截的问题（预检价值）

1. **契约 schema 缺失**：宿主 `packages/contracts/v1/` 只有 2 个旧文件，三个新 schema 未部署即会重演 e2a37f6（派生 100% 失败）。已随部署补齐。
2. **Caddy 路由缺口**：面板需要 `/api/reports/*`、`/api/risk/*`，原配置只路由 `/api/auth/*`。已补并预先带 env 验证（禁 reload，走 restart）。
3. **沙箱失真两次误判**：`execution_domain`/`observability` 的"缺失"实为临时沙箱路径深度不对（生产代码用 `parents[2]`/`parents[3]` 自解析）。用对照实验证伪，并回退了误加的 `.pth`。教训：控制面预检沙箱必须复刻 `/srv/trader-v3` 的目录深度并软链 packages/nautilus-node。

## 3. 验证结果

- 三新端点内网冒烟：`/v1/mirror/positions` `/v1/outcomes` `/v1/reconcile` 全 200
- 角色可见性实测：operator-query（面板用）5 个新端点齐全；node-control/event-ingest 仅 outcomes（见遗留项 1）
- 控制面部署后 20 分钟：日志零 error/traceback；`projection_failures` 零未解决
- 面板公网 200，线上 bundle 与本地构建哈希一致；`/api/reports/*` 新路由 200
- app：装机后启动正常，崩溃缓冲仅有 08-30 的历史 rnscreens 记录（已修复项），无新崩溃

## 4. 舰队状态

| 账户 | 状态 |
|---|---|
| account-a / b / c / d | ✅ 全部 ACTIVE（account-a 经第 5 节回填后恢复） |

## 5. account-a 未恢复：根因与处置建议（待人工决策）

**结论：与本次部署无关，是既有数据问题被 HALT 窗口暴露。**

RESUME 闸门要求"机器人自有订单全终态"，非终态订单需豁免。豁免路径要求投影行与交易所快照 **shape 比对一致**。account-a 有 3 条投影"空壳行"：

| client_order_id | 品种 | 投影状态 | quantity/price/side/order_type | 交易所实况 |
|---|---|---|---|---|
| Bbb7c9f43…001 | CLUSDT | accepted | 全 NULL | SELL LIMIT 3.16 @ 94.91 在场 |
| Be094c35a…001 | CLUSDT | accepted | 全 NULL | SELL LIMIT 3.33 @ 89.91 在场 |
| Becd16aca…001 | BZUSDT | accepted | 全 NULL | SELL LIMIT 3.12 @ 96 在场 |

- 三者 intent 均 `approved` 且未过期、序号 01 合规、`payload` 仅含 instrument_id
- 逐单跑闸门逻辑确认：`shape_ok=True`、identity 正常，**唯独 shape 比对因 NULL 失败** → 无豁免 → 409
- 时间线：BZUSDT 行创建于 08-31 12:58（部署前 3 小时），故该阻塞自当时起已潜伏
- 交易所侧这三张是**真实在场的埋伏单**，不可撤

**处置：用户裁决选 A（精确回填），已执行并恢复。**

- 语义以两条能正常豁免的 BTCUSDT 行为模板确定：`side` 列存 position side（`short`），`payload.side` 存订单方向（`SELL`）——二者不同义，直接映射交易所 `side` 会写错
- 事务内先建备份表 `orders_projection_backfill_bak_20260831T153547Z`（3 行原始状态），再逐单 UPDATE，带三重守卫（点名 client_order_id + `status='accepted'` + `quantity IS NULL`），可重复执行
- 结果：3 条各更新 1 行，值与交易所快照一致；account-a 残留空壳行归零；未波及其他行
- 复检：闸门对全部 5 单 `durable豁免=True`；RESUME accepted → **ACTIVE confirmed**
- 回滚：备份表保留原始 NULL 状态

## 6. 遗留项

1. **`/v1/outcomes` 角色外泄**：挂载在 role 过滤之后，三个 role app 都可见（mirror/trace/incidents 正确地仅 operator-query）。无功能影响，一行可修（改为与 mirror 同样挂 all_role_app）。
2. **账实核对演练未执行**：改SL→改TP→50%→100%平仓 + 幂等重发三端一致，待安排。
3. **app 侧 Trading 区未接线验证**：APK 已装，但真实 baseUrl/token 配置与端到端写操作演练待做。
4. F-10 的 7 条 accepted 残余风险见 `docs/agent-team/upgrade-crew-roster.md`。
5. **⚠️ 上线即命中的真实风险（非部署引入）**：`/v1/mirror/positions` 报出 **account-a CLUSDT SHORT 11.71 完全裸奔**（SL=0、TP=0，开仓均价 85.33、标记 85.57、浮亏约 -2.79 USDT）。同账户另两仓（BTCUSDT SHORT、SPCXUSDT LONG）保护正常。这正是 M2c「无保护告警」的设计目标场景，系统上线首分钟即命中。**处置需人工决定（挂保护单属金融交易操作，不由自动化代行）**，可用本次交付的面板/app「改止损」功能执行。
6. **对账差异**：`/v1/reconcile` 报 11 条漏记单（交易所有、投影无，多为 algo 条件单），零幽灵单。属既有投影覆盖不全，建议后续在低流量窗用 `scripts/rebuild_orders_projection.py` 处理。

## 7. 移动端公网入口（2026-08-31 后续，用户指示"直接开放公网"）

原设计 D4 是「仅 Tailscale 可达」，用户改为公网直连。实测手机端 Tailscale 未连接（只有 WiFi 与另一 VPN 的 tun0），故走公网。

**实现**：jp-bot.balen.wang 新增 `/m` 前缀路由 → strip → 127.0.0.1:8183，**不注入 token**（app 自带 operator token 透传）。

```caddyfile
@mobile_api path /m/v1/accounts /m/v1/mirror/positions /m/v1/operator/orders /m/v1/operator/orders/*
handle @mobile_api {
    uri strip_prefix /m
    reverse_proxy 127.0.0.1:8183 { flush_interval -1 }
}
```

**收敛措施**：只放行 app 实际调用的 4 个路径（不是整个 `/v1/*`）；面板原有 `/v1/*` 只读路由（注入 SYSTEM_OBSERVER_TOKEN）保持不变。

**验证**（外部客户端实测）：`/m/v1/accounts` 带 operator token→200、无 token→401；越界路径 `/m/v1/commands`→返回 SPA HTML，**从未触达控制面**；面板不受影响。本次 caddy restart **未触发舰队 HALT**（四节点全程 ACTIVE）。

**⚠️ 安全后果（用户已知情选择）**：
1. `POST /v1/operator/orders`（下单/改止损/平仓）现在从公网任意位置可达，唯一门槛是 bearer token
2. 该 token 就是 `RISK_ADMIN_TOKEN`——与 `v3_trade.py` 等全部 operator 工具共用。控制面的 token→角色映射是「一角色一环境变量」，**无法给 app 单独发可独立吊销的 token**；一旦泄露需轮换所有消费方
3. 无速率限制、无 IP 白名单、无 mTLS
4. 后续若要收紧：给控制面加「同角色多 token」支持（需改代码+部署），或加 IP 白名单/mTLS

**回滚**：`/srv/trader-v3/backups/Caddyfile.pre-mobile-<TS>` → restart caddy。

## 8. 回滚

备份：`/srv/trader-v3/backups/predeploy-20260831T153547Z/`（read_api.py、contracts/v1、dashboard dist、report_snapshot.py、Caddyfile）
回滚 = 还原上述文件 → restart 三控制面服务 + caddy → `resume_race.py` 全舰队。

## 9. 2026-09-02 追加部署：fail-closed HALT 告警 + 审计（方案 A）

- 来源：`goal/backend-halt-alert` `59e152a`（基于 backend-v1 `5a56f1f`）；改动仅 `api/read_api.py`、`order_management/alerts.py`；525 tests green
- 部署批次 `20260902T090720Z`；生产解释器 py_compile 通过；重启 09:10:24Z，就绪 2s，90s 内零 error
- 备份：`/srv/trader-v3/backups/predeploy-halt-alert-20260902T090720Z/`
- **部署后发现并修复**：node-control 的 DB 角色缺 `outbox_events` 写权限（首批 3 次 HALTED→ACTIVE 跃迁被正确检测、正确隔离，但 INSERT 被拒）。已 `GRANT SELECT, INSERT ON outbox_events, audit_events TO trader_v3_node_control`，以该角色实测 has_table_privilege 全 true。端到端证明待下一次真实跃迁。
- 舰队：b/c/d ACTIVE；**a HALTED，被 RESUME 闸门 409**——`Bbb7c9f4…001` CLUSDT LIMIT 投影 price 94.91 vs 交易所 95.98（改单未更新投影 price）。修复 SQL 已 staged（备份表 + 三重守卫，干跑命中 1 行），**属 D9 记账表写入，待用户裁决**。
- 本轮 HALT 根因（非部署引入）：22:39 UTC 四节点 `RUNTIME FENCE` 心跳 5s 超时自杀重启；node-control 同期 GET 正常、心跳 POST 连续 7s 为零 → 心跳处理函数被阻塞（非进程/网络/OOM/PG 日志异常）。守卫 B 在 09-02 06:53 第二次自动升级中验证有效（控制面/caddy/docker 均未被重启）。
- **告警"通知到人"一环的事实（部署后查明）**：`OutboxNotificationSink` 写的 `notification.alert/recovery` 在生产**从未有消费者**（outbox 全表历史零条，`AlertEngine` 除测试外无人构造）；attention-service 只收手机自身的 capability-receipts，**无任何服务端喂送**；唯一活着的通道是 `trade-event-notifier` → Telegram，且 SQL 硬过滤 `intent_ack.*`。故 A 的 `node.halted/node.resumed` 审计行虽会落库，**默认不会通知任何人**。补丁 `goal/notifier-node-halt` `a5a05ab`（白名单 + `node` 消息类 + 渲染/去重，15 tests green）已 staged 于 `/srv/trader-staging/trade_event_notifier.node-halt-20260902T092308Z.py`，**未安装未重启，待裁决**。
- **2026-09-02 09:27Z 用户裁决执行**：以交易所实时快照（13.69 @ 95.98，executed 0）同步投影行 `Bbb7c9f4…001`（守卫：点名 + `status='accepted'`），备份表 `orders_projection_pricefix_bak_20260902T092721Z`（1 行）。随后 `resume_race account-a` → ACTIVE confirmed；四节点全 ACTIVE。
- **A 端到端首次证明**：该次 HALTED→ACTIVE 跃迁在心跳路径零吞错；`audit_events` 落 `node.resumed`（observed_at 09:27:28Z，previous HALTED → new ACTIVE）；`outbox_events` 落**历史首条** `notification.recovery`（pending，暂无消费者——Telegram notifier 补丁未安装，待裁决）。

## 10. 2026-09-05 待裁决：镜像录制器补 leverage（app 收益率为 `—` 的根因）

- 根因：`exchange_state_recorder.py` 从 `/fapi/v2/positionRisk` 组装仓位时未复制 `leverage`，镜像里没有该字段，`v1_mirror.py` 回落为 `"0"`，app 无法推导保证金回报率。
- 补丁：`goal/backend-leverage` `b0b6143`（一个字段，契约 §2 已声明，`v1_mirror` 原本就透传）+ 夹具修复 `4be7d80` + `.live-mirror` 同步 `77477c5`；控制面+战报套件 **527 passed**。
- 部署方式：仅替换 `services/control-plane/tools/exchange_state_recorder.py` 并 `systemctl restart trader-v3-exchange-state.service`（独立单元，**不触碰心跳路径，不会 HALT 舰队**）。staged：`/srv/trader-staging/exchange_state_recorder.leverage-20260905T065630Z.py`（sha 8ea37165…，生产解释器 py_compile 通过）。
- 属 `exchange_state_mirror` 既有写入器的加字段改动（D9 相邻），**待用户裁决**。
- 本机测试环境教训：空 `LANG/LC_*` 会让 initdb 报 invalid locale、postmaster 报 multithreaded；`--no-locale` 单独用又会把库建成 SQL_ASCII 使含 `§` 的迁移失败。夹具已改为 `--no-locale -E UTF8` + 强制 `LC_ALL=C`。
- **2026-09-05 07:21Z 用户按推荐执行**：录制器 leverage 补丁 + Telegram HALT notifier 补丁同批部署。备份 `/srv/trader-v3/backups/predeploy-recorder-notifier-20260905T072118Z/`；两单元 active、零错误；一个录制周期后镜像与 `/v1/mirror/positions` 均出现真实杠杆（account-a TAOUSDT 10 / CLUSDT 20 / BTCUSDT 100 / BZUSDT 20；account-b TAOUSDT 10；account-c BTCUSDT 5）；四节点全程 ACTIVE（两单元均不在心跳路径）。app 无需改动即可显示收益率。

## 11. 2026-09-05 app 真机截图验收 + 总权益波形图（进行中）

- 首次拿到真机截图（QA 包 `-PattentionAllowScreenshots=true`，已换回安全版并验证）。卡片视图合格；**发现三处测试未捕获的真机缺陷**：列表品种列截断为 `T…`；平仓抽屉未贴底（悬于屏幕中央）；抽屉预览块与确认按钮不可见。已派前端执行者修复。
- 总账户金额 30 分钟波形图：契约 §8 已冻结（`account_equity_samples` 非记账新表 + `/v1/accounts?history_hours=` 附加字段，不新增端点、不改 Caddy）。后端/前端并行开发中。部署需：一次迁移（新表）+ 录制器重启（安全）+ **仅 operator-query 重启**（不碰 node-control 心跳路径，预期不 HALT 舰队）。无历史回填，曲线自部署起累积。
- **权益历史部署清单（已核实，待执行）**：
  1. 迁移运行器只按版本号比对已应用集合（不校验旧文件哈希）；生产 `schema_migrations` 头 0018，磁盘迁移目录仅到 0014（0015–0018 文件缺失但记录哈希与仓库逐一吻合）→ 部署时把 `db/migrations/0015…0019` 全部复制到 `/srv/trader-v3/db/migrations/`
  2. 命令：`cd /srv/trader-v3 && .venv-cp/bin/python services/control-plane/db/migrate.py up --database-env-file /srv/trader-v3/.env.v3`（`.env.v3` 为 `postgres` 超级用户，含且仅含一条 DATABASE_URL）
  3. **权限**：录制器单元走 `.env.v3`（postgres）无需授权；`/v1/accounts` 挂在 `trader_v3_operator_query` / `trader_v3_node_control` / `trader_v3_event_ingest` 三角色下，**0019 迁移必须包含 `GRANT SELECT ON account_equity_samples` 给三者**（复现 09-02 outbox 权限教训）
  4. 文件：`services/control-plane/api/read_api.py`（+ 新模块若有）、`tools/exchange_state_recorder.py`（同步仓库 `.live-mirror`）、`packages/contracts/v1` 新 schema
  5. 重启：`trader-v3-exchange-state.service`（安全）+ **仅** `trader-v3-controlplane-operator-query`（无单元绑定；不碰 node-control 心跳路径）；node-control/event-ingest 留待下一个控制面窗口
  6. 生产 `db/migrate.py` 比仓库旧 232 行（缺 verify-frozen 围栏函数），`up` 语义相同，本次不更新
- **权益历史后端就绪（待部署裁决）**：`goal/backend-equity-history` `f7961a4`（采样 `89358d1` + 三角色 SELECT 授权 `f7961a4`），控制面+战报 **540 passed**（基线 527 + 13 新增：迁移 up/down、桶内去重、采样失败不影响镜像写入、无参响应键集不变、缺账户桶 `accounts_sampled`、非法参数 400）。staging：`/srv/trader-staging/equity-history-20260905T075054Z/`（read_api.py、exchange_state_recorder.py、migrations 0015–0019 up/down），生产解释器 py_compile 通过。部署按第 11 节清单。
- **0019 生产库回滚事务试跑通过**（2026-09-05 08:0xZ）：CREATE TABLE/INDEX/GRANT 全部执行成功——6 列、2 索引、SELECT 授予 operator_query / node_control / event_ingest（+postgres 所有者）；ROLLBACK 后表不存在，生产零副作用。部署只剩执行清单第 1–5 步。
- **2026-09-05 08:05–08:08Z 权益历史已上线**（用户裁决）。两处教训：① 生产 `db/migrate.py` 是旧版，**不认识 `--database-env-file`**，须以 python 包装脚本读 `.env.v3` 注入 `DATABASE_URL` 后调 `migrate.py up`；② 部署脚本 `set -e` 被 `| tail` 吞掉退出码，迁移失败后仍换了文件并重启——后果可控（无参路径不变、录制器采样有 SAVEPOINT 隔离），随后补跑迁移 `Applied 0019`。验证：首桶 08:00 四账户齐全，`/v1/accounts?history_hours=1` 返回总额序列（accounts_sampled=4），无参响应键集不变，非法参数 400，录制器采样失败日志归零，舰队全程 ACTIVE，node-control 未动。线上 read_api = leverage 血统 + 吸收的 addon 热补丁 + 权益历史（`goal/backend-equity-history` `d4b7989`，542 passed；addon 的测试对齐从 main `88f7a3d` 移植）。
- **app 侧 2026-09-05 收口**（分支 `auto/task-F-11-tabs`，HEAD `d8bd517`，204 tests）：`f27acf3` 原生 BottomSheet（抽屉贴底、确认固定）→ `dec0fa1` 总权益波形图 + 列表品种列修复 → `cba6a4f` 波形图移到交易页顶部 → `ac4a96d` 列表行加开仓/标记/收益率 + 单点圆标 → `9290bbb` 账户页字号间距收紧 → `d8bd517` 开仓/标记分行。真机截图逐项核对：抽屉贴底且确认可见（探针 dump 证实原地打开）、列表无截断、账户页首个采样点已显示。手机现为安全版（截屏不可用）。
- **仓位详情页（`PositionDetailScreen`）仍是旧样式**（裸字段名 `quantity_step`/`order_id`），从卡片 `›` 进入可见；其写流程被冻结，需单独安排样式重做。
- `bbd768c` 修复波形图涨跌为正时的 `++`（手动前缀 + `signed:true` 双重加号）；真机 dump 证实 `+3.89 / +0.02%`，205 tests。手机已装回安全版。
- **app 图标与名称**（2026-09-05）：用网关 `gpt-image-2` 生成 balen-bot 图标（源图 `apps/attention-android/design/balen-bot-icon.png`），自适应图标（前景 90% + 底色 `#011324`）+ 各密度 legacy 方/圆；`app_name` → `balen-bot`；aapt2 验证 APK 标签与图标。提交 `1461362`（amend）。桌面实拍需手机解锁。
- 图标配色按用户选择切换为 **D 橙**（`b9b6587`，源 `design/balen-bot-icon-active.png`）；网关 token 额度已耗尽（−$2.32），四套备选为本地色相重映射；桌面实拍待手机解锁。
- 波形图末端改为**实时账户合计**（空心圆标、右刻度"现在"），无采样时以当前余额为起点并注明；实时值不齐则按缺口处理。提交见 app 分支 HEAD，209 tests。
