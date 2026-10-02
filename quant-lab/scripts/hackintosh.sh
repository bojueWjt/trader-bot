#!/bin/bash
# 访问家中黑苹果研究机（数据与重放都在那台机器上）。经 home-mini 跳板、只用公钥（BatchMode），不接受任何密码。
#
#   scripts/hackintosh.sh run '<shell 命令>'           在黑苹果上执行
#   scripts/hackintosh.sh ql <模块> [参数...]           在黑苹果的 quant-lab 里运行 python -m <模块>（已设好数据根）
#                                                      模块以 quant_lab.market / quant_lab.research 开头时用 .venv-g2，其余用 .venv-g1
#   scripts/hackintosh.sh get <远端路径> <本地路径>      取回文件
#   scripts/hackintosh.sh put <本地路径> <远端路径>      上传文件
#   scripts/hackintosh.sh sync                         把本仓库 quant-lab 的代码与测试同步到黑苹果（不含 data、venv）
#   scripts/hackintosh.sh test [pytest 参数...]         同步后在黑苹果跑测试（不占本机）；无参数跑 data+integration（g1）与 market（g2）全套
#   scripts/hackintosh.sh dash [远端配置] [本地端口] [远端端口]
#                                                      确保只读看板运行并开启 SSH 隧道；默认数据根/dashboard.json、8765、8765
#
# 黑苹果：数据根 /Volumes/G/quant-lab-data，代码 ~/quant-lab。真实聊天记录只在那台机器上，不要拉回本仓库。
set -euo pipefail
JUMP="balen@100.99.138.19"            # home-mini（Tailscale IP；裸主机名会被 Clash fake-ip 劫持）
HOST="balen@192.168.31.98"            # 黑苹果（本机直连会在 kex 阶段被断开，必须走跳板）
DATA_ROOT="/Volumes/G/quant-lab-data"
# LC_ALL=C：避免远端 locale 警告（不用进程替换过滤，Codex 沙箱不允许 /dev/fd）
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -J "$JUMP")
export LC_ALL=C LANG=C
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cmd="${1:-}"; shift || true
case "$cmd" in
  run)
    [ $# -ge 1 ] || { echo "用法: $0 run '<命令>'" >&2; exit 2; }
    ssh "${SSH_OPTS[@]}" "$HOST" "$*" ;;
  ql)
    [ $# -ge 1 ] || { echo "用法: $0 ql <模块> [参数...]" >&2; exit 2; }
    mod="$1"; shift
    venv=".venv-g1"; case "$mod" in quant_lab.market*|quant_lab.research*) venv=".venv-g2" ;; esac
    args=""; for a in "$@"; do args+=" $(printf '%q' "$a")"; done
    ssh "${SSH_OPTS[@]}" "$HOST" "cd ~/quant-lab && QUANT_LAB_DATA_ROOT=$DATA_ROOT $venv/bin/python -m $mod$args" ;;
  dash)
    [ $# -le 3 ] || { echo "用法: $0 dash [远端配置] [本地端口] [远端端口]" >&2; exit 2; }
    config="${1:-$DATA_ROOT/dashboard.json}"
    local_port="${2:-8765}"
    remote_port="${3:-8765}"
    for port in "$local_port" "$remote_port"; do
      [[ "$port" =~ ^[0-9]{1,5}$ ]] && (( 10#$port >= 1 && 10#$port <= 65535 )) || { echo "端口必须在 1–65535" >&2; exit 2; }
    done
    # All user-supplied arguments are shell-quoted, never interpolated into Python.
    remote_args="$(printf '%q ' "$config" "$remote_port" "$DATA_ROOT")"
    ssh "${SSH_OPTS[@]}" "$HOST" "bash -s -- $remote_args" <<'DASH_REMOTE'
set -euo pipefail
config="$1"; port="$2"; data_root="$3"
cd ~/quant-lab
[ -f "$config" ] || { echo "缺少看板配置: $config" >&2; exit 2; }
py=".venv-g2/bin/python"
log="$data_root/dashboard-$port.log"
# Probe service identity and configuration, not just an open TCP port.
health() {
  "$py" - "$config" "$port" <<'DASH_HEALTH'
import hashlib, json, pathlib, sys, urllib.error, urllib.request
path = pathlib.Path(sys.argv[1]).expanduser().resolve()
expected = hashlib.sha256(str(path).encode() + path.read_bytes()).hexdigest()
try:
    with urllib.request.urlopen('http://127.0.0.1:' + sys.argv[2] + '/api/health', timeout=2) as response:
        data = json.load(response)
except (OSError, ValueError):
    sys.exit(1)
if data.get('service') != 'quant-lab-dashboard' or data.get('config_id') != expected:
    print('端口上的服务或配置不匹配，请停止旧服务或选择另一远端端口。', file=sys.stderr)
    sys.exit(2)
DASH_HEALTH
}
status=0; health || status=$?
if [ "$status" -eq 2 ]; then exit 2; fi
if [ "$status" -ne 0 ]; then
  # Serialize competing starts without stopping an existing service.
  lock="$data_root/.dashboard-$port.start"
  mkdir "$lock" 2>/dev/null || { echo "另一启动正在进行（若遗留请检查后删除 $lock）" >&2; exit 1; }
  trap 'rmdir "$lock" 2>/dev/null || true' EXIT
  status=0; health || status=$?
  if [ "$status" -eq 2 ]; then exit 2; fi
  if [ "$status" -ne 0 ]; then
    nohup nice -n 19 "$py" -m quant_lab.viz --config "$config" --bind 127.0.0.1 --port "$port" >>"$log" 2>&1 </dev/null &
    pid=$!
    ready=false
    for attempt in {1..30}; do
      status=0; health || status=$?
      if [ "$status" -eq 0 ]; then ready=true; break; fi
      if [ "$status" -eq 2 ]; then exit 2; fi
      kill -0 "$pid" 2>/dev/null || { echo "看板启动失败，请检查 $log" >&2; exit 1; }
      sleep 1
    done
    [ "$ready" = true ] || { echo "看板未就绪，请检查 $log" >&2; exit 1; }
  fi
fi
echo "研究机看板已就绪；日志: $log"
DASH_REMOTE
    ssh "${SSH_OPTS[@]}" -o ExitOnForwardFailure=yes -f -N -L "127.0.0.1:$local_port:127.0.0.1:$remote_port" "$HOST"
    echo "http://127.0.0.1:$local_port" ;;
  get)
    [ $# -eq 2 ] || { echo "用法: $0 get <远端> <本地>" >&2; exit 2; }
    scp -q -o BatchMode=yes -J "$JUMP" "$HOST:$1" "$2" ;;
  put)
    [ $# -eq 2 ] || { echo "用法: $0 put <本地> <远端>" >&2; exit 2; }
    scp -q -o BatchMode=yes -J "$JUMP" "$1" "$HOST:$2" ;;
  sync)
    rsync -a --delete -e "ssh ${SSH_OPTS[*]}" --exclude '.venv-g*' --exclude '/data/' --exclude '__pycache__' \
      --exclude '.pytest_cache' --exclude 'taskList.lock' \
      "$REPO/src" "$REPO/requirements" "$REPO/pyproject.toml" "$REPO/scripts" "$REPO/contracts" "$REPO/tests" "$HOST:quant-lab/" ;;
  test)
    "$0" sync
    if [ $# -eq 0 ]; then
      ssh "${SSH_OPTS[@]}" "$HOST" "cd ~/quant-lab && .venv-g1/bin/python -m pytest tests/data tests/integration -q -p no:cacheprovider 2>&1 | tail -3; .venv-g2/bin/python -m pytest tests/market -q -p no:cacheprovider 2>&1 | tail -3"
    else
      venv=".venv-g1"; case "$1" in tests/market*|tests/research*) venv=".venv-g2" ;; esac
      args=""; for a in "$@"; do args+=" $(printf '%q' "$a")"; done
      ssh "${SSH_OPTS[@]}" "$HOST" "cd ~/quant-lab && $venv/bin/python -m pytest -p no:cacheprovider$args"
    fi ;;
  *)
    sed -n '2,14p' "$0"; exit 2 ;;
esac
