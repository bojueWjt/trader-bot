#!/bin/bash
# hk root 窗口一条龙：事故恢复 + 第一/二批修复部署（2026-07-24）
# 在 hk 上以 root 执行：sudo bash /home/balen/deploy-20260724/hk-root-window-20260724.sh
# 每阶段结束暂停确认。物料目录 /home/balen/deploy-20260724（由 balen 预先 stage）。
set -euo pipefail
D=/home/balen/deploy-20260724
T=/srv/trader-v3
CP=$T/container-patches
STAMP=20260724
pause() { read -rp ">>> $1 —— 回车继续，Ctrl-C 中止 <<<"; }

echo "===== 阶段0：事故证据抓取（节点自 02:51 UTC 僵死）====="
mkdir -p $T/incident-$STAMP
for n in trader-v3-node-a trader-v3-node-b; do
  docker logs --since 2026-07-24T00:00:00Z "$n" &> "$T/incident-$STAMP/$n.log" || true
  docker inspect "$n" > "$T/incident-$STAMP/$n.inspect.json" || true
done
ls -la $T/incident-$STAMP/
pause "证据已抓取（重启后 docker logs 即丢，此步不可跳过）"

echo "===== 阶段1：探明容器内 binance execution 路径（容器僵死 exec 可能超时，15s 放弃则留空）====="
BINANCE_DST=$(timeout 15 docker exec trader-v3-node-a python -c \
  "import nautilus_trader.adapters.binance.execution as m; print(m.__file__)" 2>/dev/null || true)
echo "BINANCE_EXEC_DST=${BINANCE_DST:-<未探明，将不挂载，重建后再补>}"
timeout 15 docker exec trader-v3-node-a test -f /app/app/node.py && echo "node.py 目标路径 OK" || echo "WARN: /app/app/node.py 待重建后核实"
pause "路径探测完成"

echo "===== 阶段2：备份 ====="
for f in $T/scripts/order_lifecycle_monitor.py $T/scripts/hermes_signal_feeder.py \
         $T/services/control-plane/api/read_api.py $T/services/control-plane/db/repository.py \
         $T/gen_recreate.py $CP/intent_execution_planner.py $CP/event_mapper.py; do
  [ -f "$f" ] && cp -a "$f" "$f.bak-$STAMP"
done
echo 备份完成
pause "备份完成"

echo "===== 阶段3：落文件 ====="
# 3a 容器补丁（重建时经显式挂载生效）
install -m 644 $D/container-patches/intent_execution_planner.py $CP/intent_execution_planner.py
install -m 644 $D/container-patches/event_mapper.py             $CP/event_mapper.py
install -m 644 $D/container-patches/exchange_cancel_adapter.py  $CP/exchange_cancel_adapter.py
install -m 644 $D/container-patches/node.py                     $CP/node.py
install -m 644 $D/container-patches/intent_execution_strategy_MERGED.py $CP/intent_execution_strategy.py
# 3b 控制面（宿主直跑）
install -m 644 $D/control-plane/read_api_MERGED.py $T/services/control-plane/api/read_api.py
install -m 644 $D/control-plane/repository.py      $T/services/control-plane/db/repository.py
install -m 644 $D/control-plane/migrate.py         $T/services/control-plane/db/migrate.py
mkdir -p $T/services/control-plane/order_management
install -m 644 $D/control-plane/order_reducer.py    $T/services/control-plane/order_management/order_reducer.py
install -m 644 $D/control-plane/position_reducer.py $T/services/control-plane/order_management/position_reducer.py
install -m 644 $D/control-plane/exchange_state_recorder.py $T/services/control-plane/tools/exchange_state_recorder.py
mkdir -p $T/packages/contracts/v1
install -m 644 $D/control-plane/order_state.v1.json $T/packages/contracts/v1/order_state.v1.json
# 3c 监控/feeder/重建脚本
install -m 644 $D/scripts/order_lifecycle_monitor.py $T/scripts/order_lifecycle_monitor.py
install -m 644 $D/scripts/hermes_signal_feeder.py    $T/scripts/hermes_signal_feeder.py
install -m 755 $D/scripts/rebuild_positions_projection.py $T/scripts/rebuild_positions_projection.py
install -m 755 $D/hk-gen-recreate-patched.py $T/gen_recreate_patched.py
echo 文件落位完成
pause "文件已落位（尚未生效）"

echo "===== 阶段4：重启监控/feeder/控制面/recorder ====="
systemctl restart trader-v3-lifecycle-monitor trader-v3-hermes-feeder
systemctl restart trader-v3-controlplane trader-v3-exchange-state
sleep 3
systemctl --no-pager --lines=0 status trader-v3-lifecycle-monitor trader-v3-hermes-feeder trader-v3-controlplane trader-v3-exchange-state | grep -E "●|Active:"
curl -s -m5 127.0.0.1:8080/healthz || echo "（控制面健康端点按实际路径核对）"
pause "宿主侧服务已重启"

echo "===== 阶段5：重建两个节点容器（显式全量挂载）====="
python3 $T/gen_recreate_patched.py trader-v3-node-a ${BINANCE_DST:-}
python3 $T/gen_recreate_patched.py trader-v3-node-b ${BINANCE_DST:-}
echo "--- 审查生成的 recreate 脚本挂载行 ---"
grep -o "\-v [^ ]*" $T/recreate-trader-v3-node-a.sh | sort
pause "确认挂载清单无误后执行重建"
bash $T/recreate-trader-v3-node-a.sh
bash $T/recreate-trader-v3-node-b.sh
sleep 10
docker ps --format '{{.Names}} {{.Status}}' | grep trader-v3-node
echo "预期：节点以 HALTED 起动；两条悬置 INJUSDT open intent 应被 denied:trading_not_active 拒绝——去 audit_events 核实后再考虑 RESUME"
pause "容器已重建"

echo "===== 阶段6：验证提示（切回 balen 执行）====="
cat <<'EOF'
su - balen 后执行：
1. bash /home/balen/deploy-20260724/verify.sh   # 验证门（sha256+mountinfo）
2. curl -s 127.0.0.1:8081/ready; curl -s 127.0.0.1:8082/ready   # 应秒回，trading_state=HALTED
3. 核对心跳新鲜（node_heartbeats payload ts 更新中）
4. F2b 投影重建 dry-run：
   DATABASE_URL=<从 root 处安全注入> /srv/trader-v3/.venv-cp/bin/python /srv/trader-v3/scripts/rebuild_positions_projection.py
   审差异（预期清 ETH×2/JTO 幽灵仓）→ 低流量时段 --apply
5. RESUME（人工确认 INJ intents 已被拒、无其他悬置动作后）
6. F1 真单闭环：对 JTO 孤儿止损（algoId 1000002464583088）下 cancel intent，确认交易所侧消失
EOF
echo "===== root 窗口脚本完毕 ====="
