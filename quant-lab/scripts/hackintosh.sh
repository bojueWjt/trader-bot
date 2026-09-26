#!/bin/bash
# 访问家中黑苹果研究机（数据与重放都在那台机器上）。经 home-mini 跳板、只用公钥（BatchMode），不接受任何密码。
#
#   scripts/hackintosh.sh run '<shell 命令>'           在黑苹果上执行
#   scripts/hackintosh.sh ql <模块> [参数...]           在黑苹果的 quant-lab 里运行 python -m <模块>（已设好数据根）
#                                                      模块以 quant_lab.market / quant_lab.research 开头时用 .venv-g2，其余用 .venv-g1
#   scripts/hackintosh.sh get <远端路径> <本地路径>      取回文件
#   scripts/hackintosh.sh put <本地路径> <远端路径>      上传文件
#   scripts/hackintosh.sh sync                         把本仓库 quant-lab 的代码同步到黑苹果（不含 data、venv）
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
  get)
    [ $# -eq 2 ] || { echo "用法: $0 get <远端> <本地>" >&2; exit 2; }
    scp -q -o BatchMode=yes -J "$JUMP" "$HOST:$1" "$2" ;;
  put)
    [ $# -eq 2 ] || { echo "用法: $0 put <本地> <远端>" >&2; exit 2; }
    scp -q -o BatchMode=yes -J "$JUMP" "$1" "$HOST:$2" ;;
  sync)
    rsync -a --delete -e "ssh ${SSH_OPTS[*]}" --exclude '.venv-g*' --exclude '/data/' --exclude '__pycache__' \
      --exclude '.pytest_cache' --exclude 'taskList.lock' \
      "$REPO/src" "$REPO/requirements" "$REPO/pyproject.toml" "$REPO/scripts" "$REPO/contracts" "$HOST:quant-lab/" ;;
  *)
    sed -n '2,12p' "$0"; exit 2 ;;
esac
