#!/usr/bin/env bash
# verify_hk_deployment.sh — hk trader-v3 部署验证门（只读、幂等、无需 root）
#
# 背景：2026-07 发生过"补丁放进 container-patches/ 但从未被挂载进容器"事故
# （projection_actor.py，靠 /proc/PID/mountinfo 才发现）。此脚本把
# "改的文件真的在跑" 变成机械化检查，每次部署后必须跑一次。
#
# 检查内容（全部在 hk 上只读执行）：
#   1. 期望清单里每个远端路径的 sha256 与期望值一致；
#   2. container-patches 文件必须在清单声明容器目标路径，并以精确的
#      source + destination 对出现在两个 nautilus node 进程的
#      /proc/PID/mountinfo 中。
#
# 用法：
#   scripts/verify_hk_deployment.sh manifest.txt
#   cat manifest.txt | scripts/verify_hk_deployment.sh
#   scripts/verify_hk_deployment.sh - < manifest.txt
#
# 清单格式（每行一条，# 开头为注释，空行忽略）：
#   宿主直跑文件：
#     <sha256> <远端绝对路径>
#     或 <远端绝对路径> <sha256>
#   container-patches 文件：
#     <sha256> <挂载源绝对路径> <容器目标绝对路径>
#     或 <挂载源绝对路径> <sha256> <容器目标绝对路径>
#
# 本地生成清单示例（路径需替换成 hk 上的目标路径）：
#   hash=$(sha256sum container-patches/projection_actor.py | awk '{print $1}')
#   echo "$hash /srv/trader-v3/container-patches/projection_actor.py /app/projection/actor.py"
#
# 环境变量：
#   HK_SSH       ssh 目标，默认 balen@149.104.30.223
#                （hk Tailscale 100.104.27.123 常年 relay/不可达，公网 IP 更稳）
#   HK_SSH_OPTS  额外 ssh 选项，如 "-J balen@home-mini" 或 ControlPath 复用
#
# 退出码：0 全部通过；1 存在差异（缺文件/哈希不符/未挂载）；2 用法或连接错误。
set -u -o pipefail

HK_SSH="${HK_SSH:-balen@149.104.30.223}"
HK_SSH_OPTS="${HK_SSH_OPTS:-}"
PATCH_DIR="/srv/trader-v3/container-patches"

usage() { sed -n '2,30p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'; }

manifest_input="${1:--}"
if [ "$manifest_input" = "-h" ] || [ "$manifest_input" = "--help" ]; then
  usage; exit 2
fi
if [ "$manifest_input" != "-" ] && [ ! -r "$manifest_input" ]; then
  echo "ERROR: 清单文件不可读: $manifest_input" >&2; exit 2
fi

# ---- 解析清单：输出 "sha256<TAB>path<TAB>mount_destination" 行 ----
parse_manifest() {
  local line f1 f2 f3 rest sha path destination
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    [ -z "${line//[[:space:]]/}" ] && continue
    read -r f1 f2 f3 rest <<<"$line"
    if [ -n "${rest:-}" ]; then
      echo "ERROR: 清单行字段数超过 3: $line" >&2; return 2
    fi
    if [[ "$f1" =~ ^[0-9a-fA-F]{64}$ ]] && [[ "$f2" == /* ]]; then
      sha="$(tr 'A-F' 'a-f' <<<"$f1")"
      path="$f2"
    elif [[ "$f2" =~ ^[0-9a-fA-F]{64}$ ]] && [[ "$f1" == /* ]]; then
      sha="$(tr 'A-F' 'a-f' <<<"$f2")"
      path="$f1"
    else
      echo "ERROR: 无法解析清单行（需要 64 位 sha256 + 绝对路径）: $line" >&2
      return 2
    fi
    destination="${f3:-}"
    if [[ "$path" == "$PATCH_DIR"/* ]] && [ -z "$destination" ]; then
      echo "ERROR: container-patches 清单项必须声明容器目标路径: $line" >&2
      return 2
    fi
    if [ -n "$destination" ] && [[ "$destination" != /* ]]; then
      echo "ERROR: 容器目标路径必须是绝对路径: $line" >&2
      return 2
    fi
    printf '%s\t%s\t%s\n' "$sha" "$path" "$destination"
  done
}

if [ "$manifest_input" = "-" ]; then
  entries="$(parse_manifest)" || exit 2
else
  entries="$(parse_manifest <"$manifest_input")" || exit 2
fi
if [ -z "$entries" ]; then
  echo "ERROR: 清单为空" >&2; exit 2
fi

# ---- 生成远端只读检查脚本，一次 ssh 完成全部核对 ----
# 远端输出协议（TSV）：
#   HASH <path> <actual_sha|MISSING|UNREADABLE>
#   MOUNT <pid> <src> <dst>          （node 进程 mountinfo 中的全部挂载对）
#   PIDS <pid...>                    （发现的 node 进程）
remote_script='
export LC_ALL=C
while IFS=$'"'"'\t'"'"' read -r want path destination; do
  if [ ! -e "$path" ]; then echo -e "HASH\t$path\tMISSING"
  elif [ ! -r "$path" ]; then echo -e "HASH\t$path\tUNREADABLE"
  else echo -e "HASH\t$path\t$(sha256sum "$path" | cut -d" " -f1)"
  fi
done <<< "$MANIFEST"
pids=""
for pid in $(pgrep -f "app.run_node" | sort -n); do
  # 真 node 容器进程一定有 /cfg.json bind-mount；据此排除 pgrep 自匹配和瞬时进程
  grep -q " /cfg.json " "/proc/$pid/mountinfo" 2>/dev/null && pids="$pids$pid "
done
echo -e "PIDS\t$pids"
for pid in $pids; do
  awk -v pid="$pid" '"'"'{print "MOUNT\t" pid "\t" $4 "\t" $5}'"'"' \
    "/proc/$pid/mountinfo" 2>/dev/null
done
'

# shellcheck disable=SC2086
remote_out="$(printf '%s\n' "$entries" \
  | ssh $HK_SSH_OPTS -o ConnectTimeout=60 -o BatchMode=yes "$HK_SSH" \
      "MANIFEST=\$(cat); $remote_script" 2>/dev/null)"
if [ -z "$remote_out" ]; then
  echo "ERROR: ssh 到 $HK_SSH 失败或远端无输出（hk 负载高时 banner 可能超时，可重试或加 HK_SSH_OPTS 走跳板）" >&2
  exit 2
fi

# ---- 本地比对 ----
fail=0
node_pids="$(awk -F'\t' '$1=="PIDS"{print $2}' <<<"$remote_out")"
pid_count=$(wc -w <<<"$node_pids" | tr -d ' ')
echo "== hk 部署验证门 $(date -u +%Y-%m-%dT%H:%M:%SZ) =="
echo "node 进程: ${node_pids:-无} (${pid_count} 个)"
if [ "$pid_count" -ne 2 ]; then
  echo "FAIL: 期望恰好 2 个 nautilus node 进程 (app.run_node)，实际 $pid_count 个"
  fail=1
fi

while IFS=$'\t' read -r want path expected_dst; do
  actual="$(awk -F'\t' -v p="$path" '$1=="HASH" && $2==p {print $3; exit}' <<<"$remote_out")"
  if [ -z "$actual" ]; then
    echo "FAIL: $path — 远端未返回结果"; fail=1; continue
  fi
  case "$actual" in
    MISSING)    echo "FAIL: $path — 远端文件不存在"; fail=1; continue ;;
    UNREADABLE) echo "FAIL: $path — 远端文件不可读（无法核对）"; fail=1; continue ;;
  esac
  if [ "$actual" != "$want" ]; then
    echo "FAIL: $path — sha256 不符"
    echo "      期望 $want"
    echo "      实际 $actual"
    fail=1
    continue
  fi
  echo "OK:   $path — sha256 匹配"

  # 声明了容器目标的文件：source + destination 必须逐节点精确唯一。
  if [ -n "$expected_dst" ]; then
    for pid in $node_pids; do
      exact_count="$(awk -F'\t' -v pid="$pid" -v src="$path" -v dst="$expected_dst" \
        '$1=="MOUNT" && $2==pid && $3==src && $4==dst {count++} END{print count+0}' \
        <<<"$remote_out")"
      source_count="$(awk -F'\t' -v pid="$pid" -v src="$path" \
        '$1=="MOUNT" && $2==pid && $3==src {count++} END{print count+0}' \
        <<<"$remote_out")"
      destination_count="$(awk -F'\t' -v pid="$pid" -v dst="$expected_dst" \
        '$1=="MOUNT" && $2==pid && $4==dst {count++} END{print count+0}' \
        <<<"$remote_out")"
      if [ "$exact_count" -ne 1 ] || [ "$source_count" -ne 1 ] || [ "$destination_count" -ne 1 ]; then
        actual_destinations="$(awk -F'\t' -v pid="$pid" -v src="$path" \
          '$1=="MOUNT" && $2==pid && $3==src {print $4}' <<<"$remote_out" \
          | paste -sd, -)"
        echo "FAIL: $path — pid=${pid} 挂载对不匹配，期望目标 $expected_dst"
        echo "      exact=$exact_count source=$source_count destination=$destination_count"
        echo "      该源实际目标: ${actual_destinations:-无}"
        fail=1
      else
        echo "OK:   $path — pid=${pid} 精确挂载 -> ${expected_dst}"
      fi
    done
  fi
done <<<"$entries"

echo
if [ "$fail" -ne 0 ]; then
  echo "结果: FAIL — 存在差异，部署未通过验证门。修复后重跑本脚本。"
  exit 1
fi
echo "结果: PASS — 清单内全部文件哈希匹配，声明的 source + destination 挂载对均已在两个 node 进程生效。"
exit 0
