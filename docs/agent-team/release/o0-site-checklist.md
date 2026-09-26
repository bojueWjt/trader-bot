# O-0 jp-24 现场只读核对清单（wac-032 草案，wac-045 修订）

> **每一条执行前都需要用户授权（授权项 O0-A01）。只读核对也不例外。** 本文件里的命令一条都没有执行过；本地只做了语法检查与桩化泄露测试。
> 目的：在任何部署动作之前，把计划 §9 O-0 的 Caddy 现场清单和部署门禁需要的现场事实一次核清，并留下不含凭据的证据。
> 要求出处见 `o0-requirements.md`（R-xx）；前置见同文件 §1（P-xx）。

## 0. 怎么跑

1. 本地先生成 bundle（只在本机，见 `o0-runbook-deploy.md` §2 第 1 步），用于基线比对：`bash scripts/ops/o0/o0_package.sh --candidate <候选提交> --out <本机目录> --report-only`。
2. 用户授权 O0-A01 后，执行（脚本从 stdin 进入，**不在主机上写任何文件**，输出只落本机）：

   ```bash
   ssh -p 53222 root@100.89.58.40 'bash -s -- --execute --auth-id O0-A01 --expect-sync-sha256 <bundle/tools/sync_operator_risk_db.py 的 sha256>' \
     < scripts/ops/o0/o0_site_check.sh > o0-site-check-<UTC>.txt
   ```

   不加 `--execute` 时脚本只打印命令（`bash scripts/ops/o0/o0_site_check.sh` 即为本清单的命令全集）。
3. 本地分析（不连 jp-24）见 §3。
4. 输出卫生（默认拒绝，审查 wac-032 🔴-5）：env 文件只打印变量名；**Caddy 完整配置不离开主机**：S-03/S-04 只输出白名单骨架（匹配器、handler 顺序、rewrite、上游地址、头**名**、`{占位符}` 值，其余所有字符串一律 `<literal len=N>`，包括头匹配器的值、replace 操作、`static_response.body`、vars、basic auth 账号），"运行配置是否等于文件"在主机上比较，只带回 yes/no 与不同处的 JSON 路径（不带值）；S-02 的 Caddyfile 大纲只打印指令与分类后的参数；compose 的 environment 值除白名单外一律隐藏；其余自由文本再过一遍行过滤（bcrypt、Bearer、长随机串、URL 查询值、疑似密钥的 `KEY=VALUE`）。`scripts/ops/o0/tests/site_check_leak_test.sh` 用 38 个哨兵值实测 0 泄露：审查 wac-032 指出的三种形状、无法靠模式识别的短字面值、路径里的令牌（wac-032-r2 的四种形状，以及 wac-032-r3 §2.2 里被转义符、`.`、`-`、`+` 切碎的令牌和裸 base64），还有 S-00 新增的节点配置与节点 env 里的令牌。同时核对内嵌库与 `o0_tool.py` 的骨架、行规则、路径规则逐项一致（70 条语料），29 条合法路径原样保留。**残余风险**（启发式本身的边界，审查已判为规格内）：纯字母令牌（例如 `/probe/qctbhyapqzxwvuk`，或查询串里没有 `=` 的纯字母值）、短于 12 位的字母数字混合段、短于 16 位的纯数字段，仍会原样输出。
5. 本次核对还要确认几项参数（`o0-authorization-list.md` §二）：舰队节点 id 与节点心跳参数（S-00）、节点通道地址（S-06）、watcher 根目录与镜像名（S-05/S-07）、控制面 env 文件清单（S-10/S-11）、主机同步脚本版本（S-09）、构建用 Python（S-12）。

**发现不符时的统一原则**：只记录、不修；任何一条判为"阻断"时，O-0 的所有生产阶段都不开始，由 Planner 转用户决定。不因为核对结果去动服务、改配置或发 RESUME。

## 1. 远端只读项（`o0_site_check.sh` 的 S-00 … S-18）

| ID | 核对什么（要求） | 命令要点 | 预期 | 不符时处置 |
|---|---|---|---|---|
| S-00 | 舰队基线：各节点状态、release、心跳年龄、`/ready`（R19、R83）；**节点 id 即舰队守卫的 `O0_FLEET_NODES`**。**节点心跳参数**（审查 wac-032-r2 🟡-7）：舰队守卫阈值的依据 | `docs/agent-operations.md` §0 的 SQL（NULL 打印为 `NULL`）+ 8081–8084 `/ready`；每个 `trader-v3-node-*` 容器：`NODE_CONFIG_PATH`，并在容器内用它自己的 python 只打印三个数：配置里的 `control_plane.heartbeat_timeout_seconds`、镜像代码 `runtime/control_plane_session.py` 的 `DEFAULT_HEARTBEAT_INTERVAL_SECONDS`、`app/node.py` 是否覆盖间隔（不打印配置、不打印 token，出错只打印异常类名）；再每 2 秒采样一次心跳年龄、共 6 次，按节点取最大值 | 每节点一行，`hb_age` < 5 秒；记下 ACTIVE/HALTED 原样与节点 id 全集。每个节点一行 `heartbeat_timeout_seconds=15 default_heartbeat_interval_seconds=2.0 node_py_overrides_interval=False`；`observed_max_hb_age` ≤ 约 2.5 秒 | 心跳冻结或节点崩溃：**阻断**，先按 `docs/agent-operations.md` 排障，不进入 O-0（用户若声明某节点本就停着，写入 `O0_FLEET_KNOWN_DOWN`）。HALTED 不是阻断，但部署后必须仍是 HALTED。**心跳参数与默认（2 秒 / 15 秒）不一致**、四个节点互相不一致、`node_py_overrides_interval=True`、实测最大年龄明显高于间隔、或读不到（`<unreadable:…>`、`NODE_HB_PARAMS_UNREADABLE`）：不是阻断，但在用户确认新的一组守卫参数之前，不开始任何部署阶段（`o0-runbook-deploy.md` §0；脚本对不一致的组合会以 `FLEET_PARAMS_INCONSISTENT` 拒绝执行） |
| S-01 | Caddy 版本、单元、EnvironmentFile 变量名（R71、R80） | `caddy version`；`systemctl show/cat caddy`；`v3.env` 只列变量名 | v2.x；`EnvironmentFile=/etc/caddy/v3.env`；变量名含 `WATCHER_BASIC_AUTH_HASH`，此时**不含** `WATCHER_BROWSER_PROXY_TOKEN` | 版本早于 path cleaning（caddyserver/caddy#4407 所在版本）：按 wac-026 🟡-1 第 2 点重核规范化行为，记为阻断直到复核完成。EnvironmentFile 不是 v3.env：更新阶段 C 脚本参数 |
| S-02 | Caddyfile 的 `import` 与 handler 书写顺序（R06） | 大纲：行号 + 指令 + 分类参数（路径、匹配器名、占位符、`host:port` 原样；其余 `<literal len=N>`；头指令只显示头名） | 能看到 jp-bot 站点块、`/m` 移动端四路径 handle（`uri strip_prefix /m` → `127.0.0.1:8183`）、面板 `/v1/*` 注入 system_observer 的块、watcher basicauth 块 | 有 `import`：adapt 会展开，以 S-03 骨架为准。书写顺序不代表生效顺序，以 L-A1 为准。面板注入若显示 `<literal len=N>` 而不是占位符：记录（明文在 admin API 可读，R44 同类），报用户 |
| S-03 | 磁盘上 Caddyfile 的 adapt 结果（带 env、无 shell 展开）的**白名单骨架**（R06–R12） | 内嵌 python 读 `v3.env` 后 `caddy adapt`，只输出骨架 | 夹在 `-----BEGIN O0 ADAPTED FILE SKELETON JSON-----` 与 END 之间；`X-Watcher-*` 注入值只能是 `{env.…}` 占位符 | `ADAPT_FAILED`：阻断（任何 Caddy 变更都不能做）。字面 `Authorization`（显示为 `<literal len=N>`）：记录，报用户 |
| S-04 | 运行中配置（admin API）与 S-03 在**主机上**比较（R06） | 内嵌 python：adapt 文件 + 读 `:2019/config/`，比较完整 JSON | `RUNNING_EQUALS_FILE=yes`；随后是运行配置的骨架 | `no`：列出不同处的 JSON 路径（不带值）。说明有人改了文件未重启，或反之：**阻断**，先查清谁改的（不采信转述） |
| S-05 | watcher 容器：镜像、运行用户（`.Config.User`，W-6 恢复库的属主依据）、标签、端口映射、重启次数、健康、env 变量名、DB 路径值、挂载（R03、R13） | `docker inspect`（env 只取名字，路径类变量取值） | `user=` 记下（空即镜像默认 root；与 S-08 `ls -l` 的库文件属主对照）；`PortBindings` = `{"9100/tcp":[{"HostIp":"127.0.0.1","HostPort":"9090"}]}`；三个 DB 路径变量都等于 `/data/watcher-trading.db`；env 名中没有任何控制面 token 名；`config_image` 记下（阶段 W 的 `--compose-image`） | 端口映射不是宿主 127.0.0.1:9090 → 容器 9100：阻断（计划 §9 O-0 要记录的事实不成立，网关上游默认值要改）。三个 DB 变量不一致：阻断（W-0b 的路径解析会直接抛错退出） |
| S-06 | 监听端口（R08、R11、R13）；**节点通道地址**（阶段 C 探针 r7 用） | `ss -ltnp`；`:8080` 的全部监听地址 | 9090 只在 127.0.0.1；8181/8182/8183 在 127.0.0.1；2019 只在 127.0.0.1；9100 不在宿主上监听；`:8080` 预期在 `172.30.1.1`（记下实际地址作 `--node-channel`） | 9090 或 9100 暴露在非回环地址：**阻断**（可绕过 Caddy 直连 watcher），报用户 |
| S-07 | watcher 源码树与 compose 的 sha（R84 同类，P-15）；构建上下文里有没有 `config.json`（R60） | 源码逐文件 `sha256sum`；compose sha 与 watcher 服务块（脱敏） | L-A4 比对全部等于 `67b401a`；`config.json absent from build context` | 源码漂移：阻断，先把线上文件取回本地做差异审阅，不覆盖未知代码。`config.json` 存在：阻断（镜像会烤进 Telegram 会话，任何演练容器都有双活风险） |
| S-08 | watcher 真库（只读打开）：表清单、三表行数、`config_revision` 是否存在、account_configs 列与 CHECK（R26、R57） | python `mode=ro` | 此时应无 `config_revision`/`config_audit`（W-0b 未上线）；三表行数记下 | 已有 `config_revision`：说明有人提前上线过 W-0b 代码，阻断并查审计 |
| S-09 | 副本与同步工具（R26、P-17） | stat 副本；timer；主机脚本的 sha256；**只有 sha 等于 `--expect-sync-sha256`（bundle 里已审版本）才**以 `--check` 运行（只读，退出 3 = 有差异） | 副本 `root:trader-v3-cp-operator-query 0640`；timer 未安装或未启用；`sync --check exit=0/3` | `SYNC_CHECK_SKIPPED`/`PENDING`：不执行未知代码，记为待核，切换时用 bundle 的副本。timer 已启用：记录并报 Planner。`SYNC_SCRIPT_ABSENT`：用 bundle 的副本（P-17） |
| S-10 | 三个控制面单元：状态、MainPID、启动时间、NRestarts、WorkingDirectory、User、EnvironmentFiles、ExecStart（worker 数）（R78、R79、R85）；**进程只持自己需要的凭据**（R03） | `systemctl show/cat`；node-control 与 event-ingest 的 EnvironmentFiles 是否含 `operator-query.env` | 三个单元同一 `WorkingDirectory`；记下 `--workers N`（切换 runbook 3.2.3 用）；两行 `ENVFILE_ISOLATION ok`；记下全部 EnvironmentFiles（`--catalog-env` 清单） | `ENVFILE_ISOLATION VIOLATION`：**阻断**阶段 O（O-2 之后另两个进程也会持有 gateway/snapshot 值），报用户。同一检查（更严：还查 `Environment=` 与另两个单元各自 env 文件里的 `WATCHER_*TOKEN` 名字，以及 operator-query 的 WorkingDirectory）已是阶段 O preflight 与 apply 的机读门禁（`o0_tool.py cp-isolation`，审查 wac-032-r2 🟡-6），这里只是提前发现。WorkingDirectory 不同于预期：更新 `--cp-root`；不共用目录：D-04 风险消失 |
| S-11 | operator-query env：只列变量名，外加三个 DB 别名的路径值与开关值（R25、R26） | 逐个 EnvironmentFile | 有 `RISK_ADMIN_TOKEN`/`VIEWER_TOKEN`/`REVIEWER_TOKEN`/`SYSTEM_OBSERVER_TOKEN` 等名字；`WATCHER_TRADING_DB` 等指向副本；**没有** `WATCHER_CONFIG_SNAPSHOT_ENABLED` 或其值为 0；没有 `WATCHER_*_TOKEN` | 开关已为 1：阻断并查审计（不应存在）。env 文件路径不是 `/srv/trader-v3/secrets/control-plane/operator-query.env`：更新阶段 O 参数 |
| S-12 | `.venv-cp`：Python ≥ 3.11、`httpx`、`tomllib` 可导入（R64、P-13；离线镜像构建器也用它） | `python -c import …; assert >= 3.11` | 打印版本，退出 0 | Python < 3.11：**阻断**阶段 O 与 W-2 构建 |
| S-13 | 控制面代码 sha：`api/*.py`、`api/generated/*.py`、`security/*.py`（R84、P-14） | `sha256sum` | L-A3 比对全部等于 `67b401a`；`api/generated/` 不存在 | 任何漂移：**阻断**阶段 O，把线上文件取回本地审阅（09-22 热挂载、09-24 发布都可能改过 read_api）；由 Planner 决定是重做基线还是先收编线上改动 |
| S-14 | staging 与磁盘（R76） | `ls -ld /srv/trader-staging`；`df -h`；备份目录大小 | `/srv/trader-staging` 存在；`/srv` 剩余 ≥ 5 GB（镜像构建 + 两份库副本） | 空间不足：先报用户处理磁盘（历史上备份只存不删），不在 `/tmp` 凑合 |
| S-15 | `db_manager.py` 的调用者（R27、R56、P-03） | cron、systemd、进程表 | 无 cron/systemd 引用，无进程 | 有调用者：阻断阶段 W（首次迁移拿锁超过 5 秒会让新 watcher 起不来），报用户 |
| S-16 | hermes-feeder 直读真库的方式（不受 O-0 影响，但要记录） | `systemctl show/cat trader-v3-hermes-feeder`（脱敏） | root 运行，直读卷里的 `watcher-trading.db` | 与预期不同：记录，报 Planner（D1 写入者清单要更新） |
| S-17 | 最近一小时错误基线（计数） | operator-query 日志、watcher `[db] Failed`、24 小时内退出次数、caddy error 计数 | 记下数值，作为部署后对照 | 基线本身就高：记录，部署后用"变化量"判断 |
| S-18 | 宿主内无凭据 HTTP 探针 | `127.0.0.1:9090/api/status`、`/healthz`、`127.0.0.1:8183/v1/accounts`、`/v1/watcher/status` | 旧 watcher 无鉴权：9090 两项为 200；8183 `/v1/accounts` 401；`/v1/watcher/status` 404（旧代码无此路由） | 9090 已经 401：说明线上已是 W-0a 之后的代码，与 S-07 对照查清来源，阻断 |

## 2. 公网只读探针（从本机发，不带任何凭据；同属 O0-A01）

| ID | 命令（本机） | 预期 | 不符时处置 |
|---|---|---|---|
| L-P1 | `curl -s -o /dev/null -w '%{http_code}\n' https://jp-bot.balen.wang/m/v1/accounts` | 401（移动端不被注入凭据，R09） | 200：面板的 `system_observer` 注入覆盖了移动端路径，**阻断**并报用户（这是现存的越权读） |
| L-P2 | 同上，加 `-H 'Authorization: Bearer o0-probe-not-a-token-000000000000000000000000'` | 403 | 200：同上 |
| L-P3 | `…/m/v1/watcher/status` | 非 200，且不是控制面 JSON 200；当前预期 SPA HTML 200 或 404 | 若返回控制面的 404 JSON：说明 `/m` 下有兜底转发（R69），阶段 C 必须同时删除 |
| L-P4 | `…/m/v1/commands` <!-- o0-allow --> | SPA（08-31 记录"越界路径返回 SPA HTML，从未触达控制面"） | 返回控制面 JSON：`/m` 有兜底转发，同上 |
| L-P5 | `…/watcher/`、`…/api/status` | 401（basic auth 质询） | 200：浏览器入口没有 basic auth，**阻断** |
| L-P6 | `curl -s -o /dev/null -w '%{http_code} %{content_type}\n' https://jp-bot.balen.wang/media/<任一真实文件名>`（文件名从 S-08 之外取不到时用假名） | 401 或 SPA，**绝不能**是 200 + `image/*` | 无认证拿到图片：`/media` 公网绕过（R11），阻断 |

## 3. 本地分析（不连 jp-24）

| ID | 做法 | 预期 | 不符时处置 |
|---|---|---|---|
| L-A1 | 从抓取文件取出 S-03 的**骨架**：`sed -n '/^-----BEGIN O0 ADAPTED FILE SKELETON JSON-----$/,/^-----END O0 ADAPTED FILE SKELETON JSON-----$/p' o0-site-check-<UTC>.txt \| sed '1d;$d' > file.skeleton.json`；然后 `python3 scripts/ops/o0/o0_caddy_watcher_routes.py inventory --adapted file.skeleton.json` 与 `verify --adapted file.skeleton.json --before-deploy`（骨架保留了核对需要的全部结构，自测对骨架同样抓住 20 种变体） | inventory 列出 jp-bot 站点的路由顺序；移动端样本一次 strip、到 8183、不动 Authorization；浏览器样本到 9090 前都有 basic auth；任何 `/m/v1/watcher/*` 样本都不到达网关 | `UNCOMPARABLE`（`expression`、`header`、`remote_ip` 等匹配器）：人工逐条审阅，不能当作通过。任何 FAIL：记入阻断项 |
| L-A2 | 读 S-04 的 `RUNNING_EQUALS_FILE` | `yes` | 见 S-04 |
| L-A3 | 把 S-13 的 `sha256sum` 段存为 `cp.sums`，`python3 scripts/ops/o0/o0_tool.py sums-compare --manifest <bundle>/controlplane-context.baseline.sha256 --sums cp.sums --prefix <S-10 的 WorkingDirectory 的上一级>` | `SUMS_OK` | `SUMS_DRIFT`：见 S-13。`SUMS_UNCOMPARABLE`（0 行或前缀不对）：不算通过，修正前缀重跑 |
| L-A4 | S-07 的 watcher 段与 compose 行分别对 `watcher.baseline.sha256`、`compose.baseline.sha256` 做 `sums-compare`（前缀 `/srv/trader/services/telegram-watcher`、`/srv/trader`） | `SUMS_OK` | 见 S-07 |
| L-A5 | 汇总成一页现场事实：Caddy 版本、`/m` 块形态与 strip 次数、上游端口、面板注入块的匹配器、浏览器块、watcher 端口映射、三处 sha 结论、Python 版本、worker 数、副本差异摘要、阻断项清单 | — | 交 Planner 转用户；阻断项清零之前不申请 O0-A02 之后的授权 |

## 4. 与计划 §9 O-0 Caddy 清单的逐项对应

| 计划 §9 O-0 原文 | 现场核对 | 部署后复核 |
|---|---|---|
| 确认 import 与 handler 顺序 | S-02、S-03、L-A1（按 adapt 结果模拟生效顺序） | 阶段 C preflight 对候选 adapt、verify 对运行中配置各做一次 |
| `/m` 只 strip 一次 | L-A1 移动端样本 | verify：每条 `/m/v1/watcher/*` 样本恰好一次；四条带参数路径为锚定、区分大小写的单段 `path_regexp`（用户裁决，WGW-1.0.2） |
| 上游仍是 8183 | L-A1、S-06 | verify：dial 恰为 `127.0.0.1:8183` |
| 移动端 `Authorization` 保留且不被面板注入的 `system_observer` 覆盖 | L-A1、L-P1、L-P2 | 阶段 C 探针 r1/r2；阶段 O 公网探针 |
| 浏览器 basicauth 后先清头再注入 | L-A1（部署前只查 basic auth） | verify 浏览器样本；阶段 W 用注入凭据 200 |
| `/media` 与其他公网入口没有绕过 | S-06、L-A1、L-P6 | verify 覆盖检查；阶段 C 探针 r6 |
| 按路由真源逐路径追加 | —（部署前不存在） | `render` 片段 + verify |
| 记录 watcher 端口映射为宿主 9090 对容器 9100 | S-05、S-06 | 阶段 W verify |
