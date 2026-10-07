# 交易系统排障

排查顺序按现象选择；操作仍受 AGENTS.md 授权与所有权边界约束。

## 排查链路(按层,自上而下)

### 0. 舰队一眼看
```bash
ssh -p 53222 root@100.89.58.40 "docker exec trader-v3-postgres psql -U postgres -d trader -Atc \"SELECT node_id||' '||status||' '||release_id::varchar(12)||' hb_age='||round(extract(epoch from now()-last_seen_at),1) FROM node_heartbeats ORDER BY node_id\"; for p in 8081 8082 8083 8084; do curl -s -o /dev/null -w \"\$p:%{http_code} \" -m 3 http://127.0.0.1:\$p/ready; done"
```
心跳 age 应 <5s;**心跳冻结(age 大)= 节点死了而不是"没变化"**,监控要按失活报警。

### 1. 信号链(零成交先查这里)
- 源头:Telegram 频道 → `trader-watcher-1` 容器(白名单在 `/srv/trader-secrets/telegram-watcher.config.json` 的 `watchGroups`;不在名单的频道逐条 `FILTERED OUT`)。
- 转发量:`docker logs trader-watcher-1 --since <date>` 过滤掉 FILTERED/raw-update/debug 行计数。
- feeder 处理量:`journalctl -u trader-v3-hermes-feeder --since <date> | grep -c 'Triggered job: signal'`。周末美股代币类频道(GOOGL/SPCX/SNDK 信号源)会静默,**0 信号 ≠ 故障**。
- hermes 决策器:systemd `hermes-gateway-trader`,`HERMES_HOME=/srv/hermes/profiles/trader`;cron 任务列表:
  `HERMES_HOME=/srv/hermes/profiles/trader /srv/hermes/hermes-agent/venv/bin/python -m hermes_cli.main --profile trader cron list`
  (只应有 daily/weekly 报表两个常驻任务;信号是 feeder 塞入的一次性 job。)hermes 的告警出口是 Telegram bot(用户手机),**巡检必须把告警内容也读一遍**。

### 2. 意向与执行漏斗
```sql
-- intent 漏斗(库 trader,容器 trader-v3-postgres,-U postgres)
SELECT created_at::date, status, count(*) FROM trade_intents WHERE created_at > now()-interval '4 days' GROUP BY 1,2;
-- 拒因(权威)
SELECT created_at, payload FROM audit_events WHERE event_type='intent_ack.rejected' ORDER BY created_at DESC LIMIT 10;
-- 机器人成交
SELECT count(*) FROM execution_events WHERE event_type IN ('OrderFilled','OrderPartiallyFilled') AND client_order_id ~ '^B[0-9a-f]{32}[0-9]{2}$';
```
常见拒因:`halted`(账户 HALTED);`live_entry_instrument_not_allowed`(品种不在风控表且无 `"*"` 默认上限);`intent_exchange_confirmation_required`(卡死旧 intent 重放)。approved 的 intent 若在 HALTED 期间批准,过 `valid_until` 即失效——**信号窗口与 ACTIVE 窗口必须重叠才有成交**。

### 3. 操作命令与审计(谁动了系统)
```sql
SELECT created_at, payload->>'operation', payload->'payload'->'scope'->>'account_id', payload->>'reason', payload->>'request_id'
FROM audit_events WHERE event_type='operator_command' ORDER BY created_at DESC LIMIT 20;
```
request_id 前缀识别操作者:`deploy-*`=部署流水线;`manual-*`/`user-chat-*`/`real-resume-*`=人工授权通道。**多操作员同时在场时先仲裁单通道,再动状态。**

### 4. 审计命令 API(执行 HALT/RESUME/CANCEL_ALL/REFRESH_EVIDENCE)
`POST http://172.30.1.1:8080/v1/commands`,Bearer=`RISK_ADMIN_TOKEN`(在 `/srv/trader-v3/secrets/control-plane/operator-query.env`)。必填 `type/confirm:true/request_id/reason/scope/target_nodes`(单账户单节点)。RESUME 的 scope 另需 `symbol`(仲裁锚点,不限品种)+ 完整 64 位 `release_id`。
RESUME 闸门顺序:机器人自有订单终态(**reduce-only 保护单已豁免**)→ margin 证据 30 秒窗(先发 REFRESH_EVIDENCE,2 秒间隔重试即可)→ 自有锚点品种仓位为零。

### 5. Redis / 租约
- 租约注册表:`trader-bot:redis-namespaces:active:leases`(hash,per-namespace JSON:owner/fencing_token/persistence_instance_id/refreshed_at_epoch)。
- 崩溃循环签名:`lease acquire failed closed: held` → 同身份新实例等旧租约老化(max_age=120s)后接管,令牌递增;`stale heartbeat writer`(exit 75)= 心跳围栏要求租约令牌严格大于库存值,随重启自愈。
- 死实例键 GC:`trader-v3-redis-dead-instance-janitor.timer`(白名单=lease 注册表存活 instance id;只删 `trader-TRADER-ACCOUNT-*:<死uuid>:*`)。手动清理前必 BGSAVE 备份到 `/srv/trader-v3/redis-janitor-backups/<ts>/`。

### 6. 事故(P0/P1)
```sql
SELECT incident_id, account_id, severity, status, opened_at, summary FROM production_incidents WHERE status='open';
```
关闭走节点身份审计端点 `POST /v1/nodes/{node}/incidents/resolve`(节点 token 在容器 `/run/secrets/control_plane_account_X_token`,围栏三元组取自 node_heartbeats 行)。部署停机窗会开出 per-node P1,滚动完成后自动关闭,残留才需人工。
