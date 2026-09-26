# watcher-app-crew 任务看板

> 四个平台共用的唯一任务表。状态：pending / in_progress / review / testing / done / blocked。设计真相：`docs/plans/2026-09-11-watcher-to-app-migration.md`；协议：`docs/agent-team/watcher-app-crew-workflow-protocol.md`。
> 集成分支：trader-bot `integ/watcher-app-crew`（基线 `67b401a`，worktree `.worktrees/wac-integ`）；alert-personal `integ/watcher-app-crew`（基线 `0f7d26d`，worktree `.worktrees/wac-integ`）。

## 里程碑

| 里程碑 | 模块 | 状态 |
|--------|------|------|
| M0 契约冻结 | 路由真源 YAML、backend-api.md 新节 | done（WGW-1.0 → 1.0.1，4464ff5） |
| M1 watcher 地基 | W-0（拆为 W-0a 鉴权层 wac-009、W-0b 配置写路径 wac-011） | W-0a done（05e9013）；W-0b 第 3 次 FAIL，等待用户裁决 |
| M2 控制面快照 | C-0 | done（d5e269b + 校准 eaf333d，开关默认关）；打开开关只剩 O-0 数据基线 |
| M3 平板基础设施 | T-0 | done（代码合入 91f6c8e；真机与键盘避让待 Tester） |
| M4 网关 | C-1 | done（cb4c5c8）；O-0 门禁须跑 P2 断言测试并核对 phase_max=P2 |
| M5 对照与发行准备 | O-0（非生产部分 + Release 清单） | in_progress（Caddy 清单 done be76e92；三路对照工具与 Release 清单待派） |
| M6 app 接入 | A-0 | done（142162e + 加固 c59b666）；联调待 O-0 |
| M7 信号 Tab | A-1 | in_progress |
| M8 交易配置 | A-2 | pending |
| M9 价格提醒 | A-3 | pending |
| M10 存量页平板改造 | A-T | pending（开工先收紧守卫：见 wac-006 备注） |
| M11 生产切换 | O-0 生产部分（需用户授权） | pending（前置：生产库一致快照副本上先跑一次 W-0b 初始化并写回滚步骤；数据库初始化失败退出路径补 Telegram 告警；wac-011 第二层秘密拒绝合入前集成分支不得成为部署候选；compose 需传入六个 WATCHER_* 凭据） |

## 任务

| ID | 标签 | 仓库 | 文件范围 | 验收（计划原文出处） | 验证命令 | 依赖 | 负责 | 状态 | 备注 |
|----|------|------|---------|---------------------|---------|------|------|------|------|
| wac-001 | [Executor] 契约冻结：路由真源与 backend-api.md 网关/快照节 | trader-bot | `contracts/watcher-gateway-routes.yaml`、`contracts/backend-api.md` | §2.1–§2.3、§3、附录 E | YAML 解析与自检 | — | watcher-app-crew-architect（Opus 子代理） | done | bd27b32 → 集成 90e96d0；WGW-1.0 |
| wac-002 | [Reviewer] 契约冻结 | trader-bot | — | 协议红线 5 | — | wac-001 | watcher-app-crew-reviewer（Opus 子代理） | done | PASS，0 🔴，11 🟡 → 裁定 R1–R9 与勘误 wac-007；报告 reviews/wac-001.md |
| wac-003 | [Executor] T-0 平板基础设施 | alert-personal | `apps/attention-android/src/ui/`、`src/navigation/RootTabs.tsx`、单列页外层包装（Setup/Pairing/Request/Settings/TradingSetup/AntdPrototype）、新增测试 | §7A.1（除账户/仓位/信号/配置页）、§7A.3 T7A-1/2/3/5 | typecheck:android、lint:android、test:android | —（与契约无关，提前并行） | watcher-app-crew-app-executor（Codex gpt-6-sol medium） | done | c9fc751 → 集成 91f6c8e |
| wac-004 | [Reviewer] T-0 平板基础设施 | alert-personal | — | 协议红线 3、7、8 | 同上 | wac-003 | watcher-app-crew-reviewer（Opus 子代理） | done | PASS，0 🔴，5 🟡；报告 `docs/agent-team/reviews/wac-003.md` |
| wac-005 | [Executor] T-0 审查建议修补：守卫补漏、档位切换保持当前 Tab、宽屏安全区断言、测试卫生 | alert-personal | `apps/attention-android/__tests__/**`，必要时 `src/ui/useLayoutClass.ts` | wac-003 审查 🟡1/2/4/5 与 nit | typecheck/lint/test:android | wac-004 | watcher-app-crew-app-executor（Codex gpt-6-sol medium） | done | 20074e2 → 集成 1022c25 |
| wac-006 | [Reviewer] T-0 审查建议修补 | alert-personal | — | 协议红线 7、8 | 同上 | wac-005 | watcher-app-crew-reviewer（Opus 子代理） | done | PASS，0 🔴；新 🟡3 条并入 A-T：除 useLayoutClass 外禁 `Dimensions` 名字、BottomSheet 禁读宽度、守卫扫 .js |
| wac-007 | [Executor] 契约勘误 WGW-1.0.1（R1–R9、🟡6/8/9/10/11、W-0 接口） | trader-bot | `contracts/backend-api.md`、`contracts/watcher-gateway-routes.yaml` | reviews/wac-001.md、Planner 裁定 R1–R9 | YAML 解析与自检 | wac-002 | watcher-app-crew-architect（Opus 子代理） | done | 5c3aa7c → 集成 4464ff5；WGW-1.0.1 |
| wac-008 | [Reviewer] 契约勘误 | trader-bot | — | 协议红线 5 | — | wac-007 | watcher-app-crew-reviewer（Opus 子代理） | done | PASS，0 🔴，5 🟡 → 裁定 R10、R11，wac-015 扩范围并排在 C-1 生成器之后；报告 reviews/wac-007.md |
| wac-009 | [Executor] W-0a watcher 鉴权层（三身份、actor 头、healthz、受保护媒体、status 扩展） | trader-bot | `server.js`、`lib/auth.js`、`lib/generated/`、媒体与 status 新模块、`__tests__/`、`bridge/docker-compose.yml` healthcheck | §2.1、§4.3 T0-1、§5.1；契约 §9.2/9.3/9.8/9.9 | watcher test | wac-002 | watcher-app-crew-backend-executor（Codex gpt-6-sol high） | done | 00648fe → 集成 05e9013 |
| wac-010 | [Reviewer] W-0a | trader-bot | — | 协议红线 2、3、7 | watcher test | wac-009 | watcher-app-crew-reviewer（Opus 子代理） | done | PASS，0 🔴；真实 HTTP 93 项全过；变异 22 个（漏：常量时间比较、never_allowed、鉴权前注册 handler 缺结构性测试）；报告 reviews/wac-009.md |
| wac-011 | [Executor] W-0b 配置写路径与快照（revision、audit、条件写、幂等、busy、秘密第二层、config-snapshot、站点 JS） | trader-bot | `lib/trading-api.js`、`lib/config-store.js`、`public/index.html`、`__tests__/` | §2.2、§2.3、§4.3 T0-2、§6.1；契约 §9.10/9.12 | watcher test | wac-002 | watcher-app-crew-backend-executor（Codex gpt-6-sol high） | blocked | FAIL 第 1 次（2 🔴：R7 未落实、站点全局 revision）；修复见 wac-018 |
| wac-012 | [Reviewer] W-0b | trader-bot | — | 协议红线 2、6、7 | watcher test | wac-011 | watcher-app-crew-reviewer（Opus 子代理） | done | FAIL，2 🔴 9 🟡；端到端 7 例与临时合并 91/91 通过；漏变异：幂等查重移出事务、快照去排序；报告 reviews/wac-011.md |
| wac-013 | [Executor] C-0 控制面快照 reader 与缓存状态机 | trader-bot | `services/control-plane/` 新模块与 reader 调用点、`tests/control-plane/` | §2.2、§4.3 T0-3；契约 §9.11；R1 | control-plane pytest | wac-002 | watcher-app-crew-backend-executor（Codex gpt-6-sol high） | done | 664f2b4 → 集成 d5e269b；全量 797 passed / 5 预存失败 |
| wac-014 | [Reviewer] C-0 | trader-bot | — | 协议红线 1、6、7 | control-plane pytest | wac-013 | watcher-app-crew-reviewer（Opus 子代理） | done | PASS，0 🔴 6 🟡（开关打开前必修）；变异 21 抓 14；报告 reviews/wac-013.md |
| wac-015 | [Executor] W-0 集成补齐：req.watcherRoute（E-12）、凭据 ≥32 字节且互异（E-02）、生成文件名对齐 E-09、停用仓库内 `bridge/services/telegram-watcher/skills/crypto-trader/scripts/db_manager.py` 的直写命令（D1）、按 WGW-1.0.1 校准；W-0a 补 query/body 校验（64 KiB、GET 带 body 拒绝、未知键与类型）；watcher 改用 C-1 生成的路由产物；W-0b 去掉自带秘密正则、operation 取 write.operation；E-02 env 值不 trim、可打印 ASCII、Basic 大小写；wac-010 补充：E-02 格式 `^[\x21-\x7E]{32,}$`、空白 PREVIOUS 启动失败、strip 字符集对齐 Python；E-12 watcherAuth 用 defineProperty 冻结；E-05 JS 恒加 u 标志并与 YAML 比对 secretKeyPattern；E-06 methods "*"；W6/W7 query 与 body 校验（64 KiB 413）归属本任务；补常量时间比较、never_allowed、中间件接线的结构性测试；status 区分"库不可读"与"从未入库"；截流日志加 bytes_sent；wac-012 补充：groups/disconnect/reconnect 写入口的幂等审计与站点 client_ref；gateway 请求体类型严格校验（字符串布尔与数字、数组比例、小写品种名）；切换到 phase_max=P2 生成物后，W-0a 手写路由表中网关放行的 P3 价格提醒删除须消失（R13） | trader-bot | watcher 鉴权与 handler 接口 | 契约 §9.16 E-02/E-12 | watcher test | wac-010、wac-012、wac-017（C-1 生成器合入） | watcher-app-crew-backend-executor（Codex） | pending | 等 W-0a、W-0b 都合入后开工 |
| wac-016 | [Executor] C-1 网关与路由生成器（scripts/contracts 生成器与校验、JS 与 Python 生成物、/v1/watcher/* async 网关） | trader-bot | `scripts/contracts/`、`services/control-plane/api/` 新模块与 `generated/`、`bridge/services/telegram-watcher/lib/generated/gateway-routes.js`、read_api 注册点、`tests/control-plane/` | 计划 §2.1、§3、§4.3 T0-1b/T0-4；契约 §9.3–§9.9、§9.14、§9.16；R3/R4/R5/R8/R9/R10/R11 | control-plane pytest、生成器校验 | wac-008 | watcher-app-crew-backend-executor（Codex gpt-6-sol high） | done | a301072 + 修复 5e54023 → 集成 cb4c5c8 |
| wac-017 | [Reviewer] C-1 | trader-bot | — | 协议红线 2–5、7 | 同上 | wac-016 | watcher-app-crew-reviewer（Opus 子代理） | done | FAIL，4 🔴 9 🟡；全量 782/6（多 1 回归）；变异 21 抓 15；报告 reviews/wac-016.md |
| wac-018 | [Executor] W-0b 修复：🔴1 R7 跨行规则仅在相关字段提交时执行；🔴2 站点按资源保存 revision；补漏变异测试（跨进程幂等、快照排序）；恢复被削弱断言；CHECK 违反 400；未知异常记日志；DB 初始化失败非零退出 | trader-bot | 同 wac-011（在 `.worktrees/wac-011` 分支 auto/wac-011 上继续） | reviews/wac-011.md 🔴1/🔴2 与指定 🟡 | watcher test | wac-012 | watcher-app-crew-backend-executor（Codex gpt-6-sol high） | blocked | 7edcf6e；复审 FAIL 第 2 轮（上轮 2 🔴 已修；新 🔴：DB 初始化失败时 server.js 成僵尸进程）；修复见 wac-020 |
| wac-019 | [Reviewer] W-0b 修复复审 | trader-bot | — | 协议红线 6、7 | watcher test | wac-018 | watcher-app-crew-reviewer（Opus 子代理） | done | FAIL，1 🔴 4 🟡；Gap Analysis：根因为上轮测量误判，建议再修一轮（最后一轮）；报告 reviews/wac-011-r2.md |
| wac-020 | [Executor] W-0b 第 3 轮修复：DB 初始化失败显式 process.exit(1)，测试改为隔离环境真实启动 server.js；补 🟡1–4 测试 | trader-bot | `lib/trading-api.js`、`__tests__/` | reviews/wac-011-r2.md 🔴-1、🟡1–4 | watcher test | wac-019 | watcher-app-crew-backend-executor（Codex gpt-6-sol high） | blocked | 59e5a59；产品代码已修对；FAIL 第 3 次：启动测试缺 W-0a 的三个假 token，合入集成分支后变红 |
| wac-021 | [Reviewer] W-0b 第 3 轮复审 | trader-bot | — | 协议红线 6、7 | watcher test | wac-020 | watcher-app-crew-reviewer（Opus 子代理） | done | FAIL 第 3 次，1 🔴（测试环境缺三个假 token，源于任务书遗漏）；退出并重启的取舍可接受；推荐方案 A 第 4 轮小修；**等待用户裁决**；报告 reviews/wac-011-r3.md |
| wac-022 | [Executor] C-0 校准（开关打开前置）：403 锁为 unauthorized；非 JSON 与超 1 MiB 不锁、上限 4 MiB；R12 channel-route 归开仓依赖；E-14 拒绝原因与旧 reader 等价；补 7 个漏变异测试 | trader-bot | `services/control-plane/api/watcher_config_snapshot.py`、read_api 相关调用点、`tests/control-plane/` | reviews/wac-013.md 🟡1–5；契约 §9.11、§9.16 E-01/E-14；R1、R12 | control-plane pytest | wac-014 | watcher-app-crew-backend-executor（Codex gpt-6-sol high） | done | 94a2911 → 集成 eaf333d |
| wac-023 | [Reviewer] C-0 校准 | trader-bot | — | 协议红线 1、6、7 | control-plane pytest | wac-022 | watcher-app-crew-reviewer（Opus 子代理） | done | PASS，0 🔴 3 🟡；合并后全量 855/5；变异 43 抓 41；控制面代码满足开关前置，剩 O-0 数据基线；报告 reviews/wac-022.md |
| wac-024 | [Executor] C-1 修复：🔴1 中间件只装一次且可重复调用；🔴2 R13 phase_max=P2 重新生成；🔴3 媒体错误响应长度；🔴4 补 5 组安全验收测试；🟡：双 Cache-Control、超长整数 500、G5 停用告警日志、S-10 读 YAML secret_fields、JS 摘要自检测试、路由集合断言改到 operator-query app | trader-bot | 同 wac-016（`.worktrees/wac-016`，先合入最新集成分支） | reviews/wac-016.md 第 10 节 | control-plane pytest、生成器校验、watcher test | wac-017 | watcher-app-crew-backend-executor（Codex gpt-6-sol high） | done | 5e54023 → 集成 cb4c5c8 |
| wac-025 | [Reviewer] C-1 修复复审 | trader-bot | — | 协议红线 2–5、7 | 同上 | wac-024 | watcher-app-crew-reviewer（Opus 子代理） | done | PASS，0 🔴；全量 815/5；生成物 P2 59 行；变异 34 抓 33（1 等价）；报告 reviews/wac-016-r2.md |
| wac-026 | [Executor] 契约 §9.14 第三份生成物：Caddy 路径清单（O-0 用） | trader-bot | `scripts/contracts/` 与清单产物 | 契约 §9.14 | 生成器校验 | wac-025 | watcher-app-crew-backend-executor（Codex） | done | 745bc70 → 集成 be76e92 |
| wac-027 | [Executor] A-0 watcherApi 服务层与设置页只读采集状态行 | alert-personal | `apps/attention-android/src/services/`、`src/screens/SettingsScreen.tsx`、`__tests__/` | 计划 §2.1 app 侧、§4.3 T0-5；契约 §9.5/§9.6 结构码 | typecheck/lint/test:android | wac-025 | watcher-app-crew-app-executor（Codex gpt-6-sol medium） | done | 354c5aa → 集成 142162e |
| wac-028 | [Reviewer] A-0 | alert-personal | — | 协议红线 2、7、8 | 同上 | wac-027 | watcher-app-crew-reviewer（Opus 子代理） | done | PASS，0 🔴 7 🟡；33/273；报告 reviews/wac-027.md；🟡 由 wac-029 处理 |
| wac-029 | [Executor] A-0 加固：R14 isConnectionInvalid、非 JSON 503 归类、同 client_ref 规则多形状测试、禁写真实拦截断言、设置页无 TextInput 与出错渲染、token 不入错误对象、media() 客户端超时；补 wac-028 列出的存活变异测试 | alert-personal | `src/services/watcherApi.ts`、`src/screens/SettingsScreen.tsx`、`__tests__/` | reviews/wac-027.md 🟡1–7；R14 | typecheck/lint/test:android | wac-028 | watcher-app-crew-app-executor（Codex gpt-6-sol medium） | done | 1ea3e75、5b141e5 → 集成 c59b666 |
| wac-030 | [Reviewer] A-0 加固 | alert-personal | — | 协议红线 2、7 | 同上 | wac-029 | watcher-app-crew-reviewer（Opus 子代理） | done | PASS，0 🔴；33/301；变异上轮 10 个全抓，新 23 抓 20；A-1 媒体约束 5 条；报告 reviews/wac-029.md |
| wac-031 | [Reviewer] Caddy 路径清单 | trader-bot | — | 协议红线 5、7 | 生成器校验 | wac-026 | watcher-app-crew-reviewer（Opus 子代理） | done | PASS，0 🔴；全量 821/5；变异 18 抓 17（1 等价）；Caddy `*` 为前缀匹配会跨段，O-0 用锚定 path_regexp 并列入 WGW-1.0.2；报告 reviews/wac-026.md |
| wac-032 | [Release] O-0 发行准备草案：jp-24 现场核对清单、部署/回滚/凭据轮换/快照切换 runbook、待用户授权清单（只写文档与脚本草案，不连生产） | trader-bot | `docs/agent-team/release/`、`scripts/` 下脚本草案 | 计划 §4.1、§9 O-0；各审查报告中的 O-0 事项 | bash -n、dry-run | wac-031 | watcher-app-crew-release-steward（Opus 子代理） | in_progress | |
| wac-033 | [Executor] C-0 测试补强（低优先级）：4 MiB 上限精确值断言、禁用加冲突的判定顺序、真实提交后持久化证据回归 | trader-bot | `tests/control-plane/` | reviews/wac-022.md 🟡A–C | control-plane pytest | wac-023 | watcher-app-crew-backend-executor（Codex gpt-6-sol medium） | in_progress | 打开快照开关前完成即可 |
| wac-034 | [Executor] A-1 信号 Tab：状态条三态、消息流（DB 读模型、分页、UTC）、媒体（取消、body 时限、并发 2、去重、占位）、简报、站点订单只读、断开/重连状态机；compact 与 expanded 双栏；补 wac-030 🟡N14/N15/N19 | alert-personal | `src/screens/` 新屏幕、`src/navigation/RootTabs.tsx`、`src/services/watcherApi.ts`、`__tests__/` | 计划 §5、§7A.1 信号 Tab、T1-1..T1-5、T7A-6；reviews/wac-029.md 的 A-1 约束 | typecheck/lint/test:android | wac-030 | watcher-app-crew-app-executor（Codex gpt-6-sol medium） | in_progress | 不依赖 W-0b |
| wac-035 | [Reviewer] A-1 信号 Tab | alert-personal | — | 协议红线 2、3、7、8 | 同上 | wac-034 | watcher-app-crew-reviewer（Opus 子代理） | pending | |
