# watcher-app-crew 任务看板

> 四个平台共用的唯一任务表。状态：pending / in_progress / review / testing / done / blocked。设计真相：`docs/plans/2026-09-11-watcher-to-app-migration.md`；协议：`docs/agent-team/watcher-app-crew-workflow-protocol.md`。
> 集成分支：trader-bot `integ/watcher-app-crew`（基线 `67b401a`，worktree `.worktrees/wac-integ`）；alert-personal `integ/watcher-app-crew`（基线 `0f7d26d`，worktree `.worktrees/wac-integ`）。

## 里程碑

| 里程碑 | 模块 | 状态 |
|--------|------|------|
| M0 契约冻结 | 路由真源 YAML、backend-api.md 新节 | done（WGW-1.0 → 1.0.1，4464ff5） |
| M1 watcher 地基 | W-0（拆为 W-0a 鉴权层 wac-009、W-0b 配置写路径 wac-011） | in_progress |
| M2 控制面快照 | C-0 | in_progress |
| M3 平板基础设施 | T-0 | done（代码合入 91f6c8e；真机与键盘避让待 Tester） |
| M4 网关 | C-1 | in_progress |
| M5 对照与发行准备 | O-0（非生产部分 + Release 清单） | pending |
| M6 app 接入 | A-0 | pending |
| M7 信号 Tab | A-1 | pending |
| M8 交易配置 | A-2 | pending |
| M9 价格提醒 | A-3 | pending |
| M10 存量页平板改造 | A-T | pending（开工先收紧守卫：见 wac-006 备注） |
| M11 生产切换 | O-0 生产部分（需用户授权） | pending（前置：wac-011 第二层秘密拒绝合入前集成分支不得成为部署候选；compose 需传入六个 WATCHER_* 凭据） |

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
| wac-013 | [Executor] C-0 控制面快照 reader 与缓存状态机 | trader-bot | `services/control-plane/` 新模块与 reader 调用点、`tests/control-plane/` | §2.2、§4.3 T0-3；契约 §9.11；R1 | control-plane pytest | wac-002 | watcher-app-crew-backend-executor（Codex gpt-6-sol high） | review | 664f2b4；专项 22 passed；全量因沙箱禁绑端口未跑，交 Reviewer 本机实跑 |
| wac-014 | [Reviewer] C-0 | trader-bot | — | 协议红线 1、6、7 | control-plane pytest | wac-013 | watcher-app-crew-reviewer（Opus 子代理） | in_progress | 2026-09-26 派发；验收 797 passed / 5 预存失败 |
| wac-015 | [Executor] W-0 集成补齐：req.watcherRoute（E-12）、凭据 ≥32 字节且互异（E-02）、生成文件名对齐 E-09、停用仓库内 `bridge/services/telegram-watcher/skills/crypto-trader/scripts/db_manager.py` 的直写命令（D1）、按 WGW-1.0.1 校准；W-0a 补 query/body 校验（64 KiB、GET 带 body 拒绝、未知键与类型）；watcher 改用 C-1 生成的路由产物；W-0b 去掉自带秘密正则、operation 取 write.operation；E-02 env 值不 trim、可打印 ASCII、Basic 大小写；wac-010 补充：E-02 格式 `^[\x21-\x7E]{32,}$`、空白 PREVIOUS 启动失败、strip 字符集对齐 Python；E-12 watcherAuth 用 defineProperty 冻结；E-05 JS 恒加 u 标志并与 YAML 比对 secretKeyPattern；E-06 methods "*"；W6/W7 query 与 body 校验（64 KiB 413）归属本任务；补常量时间比较、never_allowed、中间件接线的结构性测试；status 区分"库不可读"与"从未入库"；截流日志加 bytes_sent；wac-012 补充：groups/disconnect/reconnect 写入口的幂等审计与站点 client_ref；gateway 请求体类型严格校验（字符串布尔与数字、数组比例、小写品种名） | trader-bot | watcher 鉴权与 handler 接口 | 契约 §9.16 E-02/E-12 | watcher test | wac-010、wac-012、wac-017（C-1 生成器合入） | watcher-app-crew-backend-executor（Codex） | pending | 等 W-0a、W-0b 都合入后开工 |
| wac-016 | [Executor] C-1 网关与路由生成器（scripts/contracts 生成器与校验、JS 与 Python 生成物、/v1/watcher/* async 网关） | trader-bot | `scripts/contracts/`、`services/control-plane/api/` 新模块与 `generated/`、`bridge/services/telegram-watcher/lib/generated/gateway-routes.js`、read_api 注册点、`tests/control-plane/` | 计划 §2.1、§3、§4.3 T0-1b/T0-4；契约 §9.3–§9.9、§9.14、§9.16；R3/R4/R5/R8/R9/R10/R11 | control-plane pytest、生成器校验 | wac-008 | watcher-app-crew-backend-executor（Codex gpt-6-sol high） | in_progress | |
| wac-017 | [Reviewer] C-1 | trader-bot | — | 协议红线 2–5、7 | 同上 | wac-016 | watcher-app-crew-reviewer（Opus 子代理） | pending | |
| wac-018 | [Executor] W-0b 修复：🔴1 R7 跨行规则仅在相关字段提交时执行；🔴2 站点按资源保存 revision；补漏变异测试（跨进程幂等、快照排序）；恢复被削弱断言；CHECK 违反 400；未知异常记日志；DB 初始化失败非零退出 | trader-bot | 同 wac-011（在 `.worktrees/wac-011` 分支 auto/wac-011 上继续） | reviews/wac-011.md 🔴1/🔴2 与指定 🟡 | watcher test | wac-012 | watcher-app-crew-backend-executor（Codex gpt-6-sol high） | in_progress | |
| wac-019 | [Reviewer] W-0b 修复复审 | trader-bot | — | 协议红线 6、7 | watcher test | wac-018 | watcher-app-crew-reviewer（Opus 子代理） | pending | |
