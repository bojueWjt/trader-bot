#!/bin/bash
# hk root 窗口一条龙：事故恢复 + 第一/二批修复部署（2026-07-24）
# 在 hk 上以 root 执行：sudo bash /home/balen/deploy-20260724/hk-root-window-20260724.sh
# 每阶段结束暂停确认。物料目录 /home/balen/deploy-20260724（由 balen 预先 stage）。
set -euo pipefail
D="${DEPLOY_DIR:-/home/balen/deploy-20260724}"
T="${TRADER_ROOT:-/srv/trader-v3}"
CP=$T/container-patches
STAMP="${DEPLOY_STAMP:-20260724}"
MEMINFO_PATH="${MEMINFO_PATH:-/proc/meminfo}"
pause() {
  if [ "${DEPLOY_NONINTERACTIVE:-0}" = "1" ]; then
    return
  fi
  read -rp ">>> $1 —— 回车继续，Ctrl-C 中止 <<<"
}

echo "===== 预检：物料完整性（缺任何一个都不开工，避免半途而废）====="
MISSING=0
for f in container-patches/intent_execution_planner.py container-patches/contracts.py \
         container-patches/projection_actor.py container-patches/event_mapper.py \
         container-patches/exchange_cancel_adapter.py container-patches/node.py \
         container-patches/binance_execution.py \
         container-patches/intent_execution_strategy_MERGED.py \
         control-plane/read_api_MERGED.py control-plane/repository.py control-plane/migrate.py \
         control-plane/order_reducer.py control-plane/position_reducer.py \
         control-plane/exchange_state_recorder.py control-plane/order_state.v1.json \
         scripts/order_lifecycle_monitor.py scripts/hermes_signal_feeder.py \
         scripts/rebuild_positions_projection.py hk-gen-recreate-patched.py; do
  [ -f "$D/$f" ] || { echo "缺失: $D/$f"; MISSING=1; }
done
[ "$MISSING" -eq 0 ] || { echo "FATAL: 物料不齐，等 Claude 通知齐备后再跑"; exit 1; }
echo "物料齐备"

echo "===== 预检：内存余量（2026-07-24 事故根因：redis 3.7GB + 零 swap 把 8G 机器吃穿）====="
AVAIL_MB=$(awk '/MemAvailable/{print int($2/1024)}' "$MEMINFO_PATH")
echo "MemAvailable: ${AVAIL_MB}MB"
if [ "$AVAIL_MB" -lt 1500 ]; then
  echo "内存不足 1.5G，先做容量处置再部署："
  echo "  docker ps --format '{{.ID}} {{.Names}} {{.Image}}' | grep 70f3eba2  # 认领这个 3.7G 的 redis"
  echo "  # 若非关键服务：docker stop <name>；若关键：docker update --memory 1g <name> 并排查膨胀"
  echo "  # 无论如何加 swap 兜底：fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile"
  read -rp ">>> 处置完成后回车重测，或 Ctrl-C 中止 <<<"
  AVAIL_MB=$(awk '/MemAvailable/{print int($2/1024)}' "$MEMINFO_PATH")
  echo "MemAvailable: ${AVAIL_MB}MB"
  [ "$AVAIL_MB" -ge 1000 ] || { echo "FATAL: 仍不足 1G，不部署"; exit 1; }
fi

echo "===== 阶段0：事故证据抓取（节点自 02:51 UTC 僵死）====="
mkdir -p $T/incident-$STAMP
for n in trader-v3-node-a trader-v3-node-b; do
  docker logs --since 2026-07-24T00:00:00Z "$n" &> "$T/incident-$STAMP/$n.log" || true
  docker inspect "$n" > "$T/incident-$STAMP/$n.inspect.json" || true
done
ls -la $T/incident-$STAMP/
pause "证据已抓取（重启后 docker logs 即丢，此步不可跳过）"

echo "===== 阶段1：探明并验证容器内 binance execution 路径 ====="
if ! BINANCE_DST=$(timeout 15 docker exec trader-v3-node-a python -c \
  "import nautilus_trader.adapters.binance.execution as m; print(m.__file__)" 2>/dev/null); then
  echo "FATAL: BINANCE_EXEC_DST 探测失败，禁止继续部署"
  exit 1
fi
BINANCE_DST=$(printf '%s' "$BINANCE_DST" | tr -d '\r')
if [ -z "$BINANCE_DST" ]; then
  echo "FATAL: BINANCE_EXEC_DST 探测结果为空，禁止继续部署"
  exit 1
fi
case "$BINANCE_DST" in
  /*)
    ;;
  *)
    echo "FATAL: BINANCE_EXEC_DST 不是绝对路径: $BINANCE_DST"
    exit 1
    ;;
esac
if ! timeout 15 docker exec trader-v3-node-a test -f "$BINANCE_DST"; then
  echo "FATAL: BINANCE_EXEC_DST 在容器内不存在: $BINANCE_DST"
  exit 1
fi
echo "BINANCE_EXEC_DST=$BINANCE_DST"
if ! timeout 15 docker exec trader-v3-node-a test -f /app/app/node.py; then
  echo "FATAL: node.py 容器目标路径不存在: /app/app/node.py"
  exit 1
fi
echo "node.py 目标路径 OK"
pause "路径探测完成"

echo "===== 阶段2：备份 ====="
for f in $T/scripts/order_lifecycle_monitor.py $T/scripts/hermes_signal_feeder.py \
         $T/services/control-plane/api/read_api.py $T/services/control-plane/db/repository.py \
         $T/gen_recreate.py $CP/intent_execution_planner.py $CP/contracts.py \
         $CP/projection_actor.py $CP/event_mapper.py $CP/binance_execution.py \
         $CP/intent_execution_strategy.py; do
  [ -f "$f" ] && cp -a "$f" "$f.bak-$STAMP"
done
echo 备份完成
pause "备份完成"

echo "===== 阶段3：落文件 ====="
# 3a 容器补丁（重建时经显式挂载生效）
install -m 644 $D/container-patches/intent_execution_planner.py $CP/intent_execution_planner.py
install -m 644 $D/container-patches/contracts.py                $CP/contracts.py
install -m 644 $D/container-patches/projection_actor.py         $CP/projection_actor.py
install -m 644 $D/container-patches/event_mapper.py             $CP/event_mapper.py
install -m 644 $D/container-patches/exchange_cancel_adapter.py  $CP/exchange_cancel_adapter.py
install -m 644 $D/container-patches/node.py                     $CP/node.py
install -m 644 $D/container-patches/binance_execution.py        $CP/binance_execution.py
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
python3 $T/gen_recreate_patched.py trader-v3-node-a "$BINANCE_DST"
python3 $T/gen_recreate_patched.py trader-v3-node-b "$BINANCE_DST"
verify_generated_mounts() {
  local recreate_script="$1"
  local suffix="$2"
  python3 - "$recreate_script" "$T" "$suffix" "$BINANCE_DST" <<'PY'
import shlex
import sys

script_path, trader_root, suffix, binance_dst = sys.argv[1:]
patch_dir = f"{trader_root}/container-patches"
expected = [
    (f"{trader_root}/node-state/{suffix}", "/state", "rw"),
    (
        f"{patch_dir}/intent_execution_planner.py",
        "/app/strategy/intent_execution_planner.py",
        "ro",
    ),
    (f"{patch_dir}/contracts.py", "/app/execution_domain/contracts.py", "ro"),
    (f"{patch_dir}/projection_actor.py", "/app/projection/actor.py", "ro"),
    (f"{patch_dir}/event_mapper.py", "/app/projection/event_mapper.py", "ro"),
    (
        f"{patch_dir}/intent_execution_strategy.py",
        "/app/strategy/intent_execution_strategy.py",
        "ro",
    ),
    (
        f"{patch_dir}/exchange_cancel_adapter.py",
        "/app/runtime/exchange_cancel_adapter.py",
        "ro",
    ),
    (f"{patch_dir}/node.py", "/app/app/node.py", "ro"),
    (f"{patch_dir}/binance_execution.py", binance_dst, "ro"),
]

with open(script_path, encoding="utf-8") as handle:
    command_lines = [
        line.strip()
        for line in handle
        if line.strip().startswith("docker run ")
    ]
if len(command_lines) != 1:
    raise SystemExit(f"FATAL: {script_path} 缺少唯一 docker run 命令")

tokens = shlex.split(command_lines[0])
mounts = []
for index, token in enumerate(tokens):
    if token != "-v":
        continue
    source, destination, mode = tokens[index + 1].rsplit(":", 2)
    mounts.append((source, destination, mode))

for expected_mount in expected:
    source, destination, mode = expected_mount
    exact_count = mounts.count(expected_mount)
    source_count = sum(1 for mount in mounts if mount[0] == source)
    destination_count = sum(1 for mount in mounts if mount[1] == destination)
    if exact_count != 1 or source_count != 1 or destination_count != 1:
        raise SystemExit(
            "FATAL: 挂载对校验失败 "
            f"{source} -> {destination} ({mode}); "
            f"exact={exact_count} source={source_count} destination={destination_count}"
        )
    print(f"OK: {source} -> {destination} ({mode})")
PY
}
echo "--- 校验生成脚本的 mount source + destination ---"
verify_generated_mounts "$T/recreate-trader-v3-node-a.sh" a
verify_generated_mounts "$T/recreate-trader-v3-node-b.sh" b
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
