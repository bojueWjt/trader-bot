# 四账户 signal worker 切换与回滚

本清单对应仓库实现与 `infra/systemd/trader-v3-signal-worker@.service`、`trader-v3-hermes-feeder-signal-cutover.conf` 模板。模板尚未安装或启动；**代码落盘不等于线上服务已生效**。本地测试不构成线上容量、延迟或交易恢复证据。线上排障先读 `docs/agent-operations.md`，部署门禁必须在停旧进程前全部通过。**RESUME 只能依据用户明确指令执行，切换 worker 不授权开闸。**

## 启动边界

保留一个 Telegram watcher/ingress。四个 signal worker 不接 Telegram 更新、不持有 Telegram token；交互式全局 operator 保持独立，保留其人工授权入口。每个实例只有自己的账户 signal token，不读取全局 `RISK_ADMIN_TOKEN`。

| 实例 `%i` | profile / HERMES_HOME | 操作凭证变量 |
| --- | --- | --- |
| account-a | trader-account-a / `/srv/hermes/profiles/trader-account-a` | `SIGNAL_TOKEN_ACCOUNT_A` |
| account-b | trader-account-b / `/srv/hermes/profiles/trader-account-b` | `SIGNAL_TOKEN_ACCOUNT_B` |
| account-c | trader-account-c / `/srv/hermes/profiles/trader-account-c` | `SIGNAL_TOKEN_ACCOUNT_C` |
| account-d | trader-account-d / `/srv/hermes/profiles/trader-account-d` | `SIGNAL_TOKEN_ACCOUNT_D` |

为四个模板实例分别准备 `trader-signal-account-a` 至 `trader-signal-account-d` 的 OS 用户、对应 profile 目录及专属环境文件。模板不创建这些用户。每个 `/srv/trader-v3/secrets/signal-workers/account-X.env` 仅配置：

```text
DATABASE_URL=<该worker处理任务使用的PostgreSQL连接；按其实际读写表审核权限>
HERMES_API_URL=<该profile审核过的OpenAI-compatible接口base或chat/completions完整地址>
HERMES_API_KEY=<仅该profile的模型凭证>
HERMES_MODEL=<固定模型版本>
SIGNAL_OPERATOR_URL=<实际operator-query地址>/v1/operator/orders
SIGNAL_TOKEN_ACCOUNT_A=<account-a专属signal凭证；其他文件只放各自变量>
HERMES_MEDIA_ROOT=<该服务用户能读取的真实媒体根目录>
```

环境文件按实例设为仅运维账号可读，不加载包含全局管理员或 Telegram 凭证的共用 `.env`。CLI 会拒绝其他账户的非空 signal token，以及 `TELEGRAM_*TOKEN*` / `TG_*TOKEN*`。模板额外清除已知 Telegram token 与管理员 token。profile 目录不从交互 operator 复制 session、工具配置或凭证。

启动的是仓库 Python worker，它通过 `RealHermesClient` 请求模型，不调用或启动另一个 Hermes CLI。`HERMES_HOME` / `HERMES_PROFILE` 是每实例的配置归属标记；此客户端实际使用环境中的 `HERMES_API_URL/KEY/MODEL`，这些目录变量本身不会自动创建模型服务或工具沙盒。模型只接收文本、图片和账户过滤后的上下文，不授予宿主机执行、Docker socket、数据库文件或数据库凭证。

`ExecStart` 使用现有 ingress 同布局的 `/srv/trader-v3/.venv-cp/bin/python`，直接运行 `services/hermes-worker/worker.py`；worker 自行加入 control-plane API、DB 和 queue 导入路径，无需临时 `PYTHONPATH` 包装。实际 CLI 为：

```bash
/srv/trader-v3/.venv-cp/bin/python /srv/trader-v3/services/hermes-worker/worker.py --help
# 仅在完成下述切换审核后执行；这里不是dry-run：
/srv/trader-v3/.venv-cp/bin/python /srv/trader-v3/services/hermes-worker/worker.py \
  --mode signal --account-id account-a --once --projection-adapters \
  --media-root /实际媒体根目录 --worker-id hermes-signal
```

CLI 默认仍是 shadow；Python `process_one()` 的默认仍是 legacy。signal 模式必须显式指定账户和真实 projection adapters，拒绝 fixture snapshot。`--worker-id` 会附加账户。`--loop` 排空可领取任务后退出；遇到 `reconciling` 也退出，模板用 `Restart=always` / `RestartSec=5` 轮询，不紧密重试。`--once` 会真的处理任务，不能当作只读部署探针。

`NoNewPrivileges` 和独立 OS 用户约束进程权限；`PrivateTmp` 意味着共享媒体不能依赖另一服务的 `/tmp`，应先核对实际 `object_key` 和媒体挂载。模板没有盲目屏蔽网络或只读整个文件系统。若额外配置执行沙盒，它只约束执行环境；队列安全仍依靠账户、不可变 purpose、lease/claim 校验和 operator 单事务交接，节点单 writer 围栏仍是另一层。

## 单一 feeder 的未来消息入口

默认 `SIGNAL_EXECUTION_ACCOUNTS` 为空，未切换账户继续旧 writer + shadow。以下变量属于**现有单一 feeder**，不能放进四个 worker 的环境文件：

| 路径 | 实际作用 |
| --- | --- |
| 旧执行路径（默认） | 未登记切换的账户继续使用原有 writer；普通 `POST /telegram/raw` 未指定 purpose 时持久化 shadow task，并保留旧执行 outbox。 |
| shadow worker | 只领取 shadow task 做观察，不通过 signal operator 交接下单；启动它不替换旧 writer。旧 shadow task 永远不能提升为 signal。 |
| signal worker（显式启用） | feeder 将满足账户切换双边界的未来消息提交到 `POST /telegram/raw/signal`；只创建 signal task，不创建旧执行 outbox，由对应账户 worker 交接 operator。 |

```text
SIGNAL_EXECUTION_ACCOUNTS=account-a
SIGNAL_EXECUTION_CUTOVER_RECEIVED_AT_ACCOUNT_A=<审核确认的ISO接收时间，必须含Z或时区偏移>
SIGNAL_EXECUTION_STATE_PATH=/srv/trader-v3/state/signal-cutover.json
```

账户列表允许 `account-a,account-b` 等不重复名称；每个启用项都必须有自己的 `SIGNAL_EXECUTION_CUTOVER_RECEIVED_AT_ACCOUNT_X`。上面的时间占位符不能直接运行，不提供可误用的历史默认时间。`SIGNAL_EXECUTION_STATE_PATH` 可省略，真实默认是 `/srv/trader-v3/scripts/.hermes_feeder_cursor.signal-cutover.json`。

现有 `infra/systemd/trader-v3-hermes-feeder.service` 保留单个 feeder 进程。可在审核后将新增 `.conf` 作为该服务的 drop-in，使其读取 `/srv/trader-v3/secrets/signal-cutover.env`；环境文件缺失时不启用新账户。应用配置还需要 daemon reload 和重启对应 feeder；不在此清单自动执行。**先部署并确认新版 ingress 生效，再启用 feeder 的账户切换配置**，不能只更新 worker 或只加 feeder 环境变量。

HTTP 模式沿用 feeder 的 `INGRESS_URL`（base URL，默认 `http://127.0.0.1:8087`）及 `INGRESS_API_TOKEN`。signal 消息实际提交到独立的 `POST /telegram/raw/signal`；该入口强制 signal purpose，拒绝 shadow 请求，原子持久化 raw + signal task，**不创建可执行的 legacy outbox**。正确鉴权的新路径请求到旧版 ingress 会返回 **404，且不写入 raw/task/outbox**；feeder 将其视为服务错误，不推进该消息的持久化游标，不重试旧 `/telegram/raw`，也不降级旧 writer。这里没有能力 probe；不要用真实消息 POST 当作只读检查。若配置了 `INGRESS_DATABASE_URL`，feeder 会绕过 HTTP、直接调用本地 `ingest_signal_telegram_update`，因此必须核对同机 ingress 模块也已更新，不能以远端路由版本代替检查。

首次启用某账户时，feeder 同时持久化该账户的 `received_at` 与 watcher 当前 `MAX(id)` 为 `watcher_high_water_id`。只有 watcher `id` **严格高于**这个值、且服务端 `received_at` 不早于配置时间的新行，才进入 signal pending；两条边界须同时满足。历史行只保留 `signal_cutover_history` 的 skipped 审计；已存在 shadow 身份的冲突会拒绝，不能提升为 signal。首次切换发现相关或无法证明账户归属的 active/uncertain 旧 cron delivery 时会中止，先核对清理在途结果，不能跳过检查。

guard 文件持久化后，重启会沿用原高水位；相同账户不得修改 cutoff。文件损坏或不可读会拒绝运行而非退回 legacy。该文件和账户任务账一并备份，**不得删除、重建或换到空的 STATE 路径绕过冻结**。从 `SIGNAL_EXECUTION_ACCOUNTS` 移除一个已登记账户，只会让其后续消息记录 `signal_execution_disabled`、保持冻结，**不会恢复旧 writer**；重新加入也不能改变原 cutoff。冻结期间的消息不会因重新加入而自动补跑。

## 单账户切换检查

1. 在旧进程仍运行时完成迁移、代码与依赖门禁、专属凭证、真实媒体读取、模型/API 连通性和路由能力核验。确认 0020/0021 已生效，控制面新代码已重启生效。准备 profile 文件不等于 worker 已上线。
2. 冻结目标账户的**旧领取/旧触发入口**，保留原始 Telegram 持久化；记录切换时间与 source message/edit 边界。不要关闭唯一 ingress，也不要误停其他账户。先确认新版 ingress 的独立 `/telegram/raw/signal` 路由已生效，再启用 feeder 配置；普通 ingestion 默认仍入 shadow，signal feeder 必须走新路径，不能靠启动 worker 替代路由接线。
3. 查当前任务、run、已接受 intent、节点心跳和交易所真实机器人订单；不能只看其他会话的“已完成”。旧模型 in-flight 未结束前，不让同账户另一路抢写。`reconciling` 表示结果未知，先核对已绑定 intent 与真实订单，不得重跑 LLM 生成新请求。
4. 单账户设置上述 opt-in 与明确 receive cutoff，核对落盘 guard 的账户、UTC 时间和首次 watcher 高水位，再启用对应 worker 实例。只将双边界后的**未来消息**路由为 signal purpose。既有 shadow purpose 不可提升；不得扫描历史 raw/outbox 再造 signal 任务，不得删除 guard 或身份去重后补跑。
5. 核对新任务：`processing_purpose=signal`、正确账户、唯一当前 run/claim、原始消息与 decision trace；核对 operator intent 与下游 outbox 各一条。模拟“返回超时”时必须复用已登记 request；只有数据库证明当前 task/run 均过期且未绑定 operation，才记录 `claim_expired_without_operation` 并释放账户。
6. 核对交易所 ack/fill、方向、数量及机器人 clientOrderId；`dispatched` / run `succeeded` 仅表示 API 接受，**不是成交**。达到验收要求后，再逐账户重复；不以四进程都存在替代账户验收。

可用于查看边界的只读 SQL：

```sql
SELECT t.account_id, t.processing_purpose, t.status, t.task_id,
       t.current_processing_run_id, t.attempt, t.lease_expires_at,
       r.status AS run_status, r.lease_expires_at AS run_lease,
       t.operator_intent_id, t.disposition_reason
FROM signal_dispatch_tasks t
LEFT JOIN message_processing_runs r ON r.processing_run_id=t.current_processing_run_id
WHERE t.account_id='account-a'
ORDER BY t.created_at DESC LIMIT 50;
```

## 回滚

1. 从 feeder opt-in 移除目标账户并停止其 signal 领取，记录冻结边界；保留 guard 和 ingress 原始数据。移除配置只冻结，不切回 legacy。对 `leased/reconciling/dispatched` 逐笔核对原 request、审计、intent 和真实订单。停止进程不代表下单已取消。
2. 恢复经过验证且仍尊重冻结边界的代码；恢复旧 writer 必须另做 drain/路由审核，不能仅删环境变量、删 guard 或运行不识别 guard 的老 feeder。确认只有一个执行入口后，才另行接入未来消息。未决 request 保留原身份查询与核对，不借回滚重放历史，也不把已接受交易重新排队。
3. **不执行 0020/0021 down，不删 guard、任务、版本、审计、outbox 或订单账。** 已登记 request/context 与 accepted intent 绑定保持不可变。回滚代码/路由不授权 RESUME，也不授权撤销用户手动单。

## 当前能力与本地验收

自动适配支持参数齐全的 open/add、close、move_stop_loss；开仓与加仓不互换，开仓/加仓需明确止损供服务端 sizing，管理需明确同频道父引用与方向。当前 HermesDecisionV1 无部分平仓数量、逐档止盈数量或可验证成本价，因此 `partial_close`、`replace_take_profits`、`move_stop_to_entry` 自动路径明确拒绝；不因 operator API 单独支持某操作就宣称模型可自动执行。GTC TTL 目前只是告警/需核对证据，**不是自动撤单、自动接管或历史重放机制**。机器人订单匹配 `^B[0-9a-f]{32}[0-9]{2}$`；手动订单不撤销、不骚扰告警。

本地核验使用 `python services/hermes-worker/worker.py --help` 和 `tests/hermes/test_signal_worker_cli.py`；后者校验真实入口参数、缺配置拒绝、signal loop 退出、模板结构及 signal prompt metadata，不发送网络请求。本机无 systemd 时只报告结构检查，不声称经过 Linux unit 启动验证。完整离线交易链回归见 `tests/control-plane/api/test_signal_worker_e2e.py`，它不测线上性能。
