#!/usr/bin/env python3
"""Patched gen_recreate for trader-v3 node containers (2026-07-24).

原版缺陷：extra 只显式挂 planner+contracts，其余靠继承旧容器 Mounts——
projection_actor.py 与 binance_execution.py 因此"部署了但从未挂载"，分别
造成投影幻影仓与孤儿止损（cancel intents died）。本版把 container-patches
下全部需生效补丁列为显式挂载，不再依赖继承。

用法（root）：
  python3 hk-gen-recreate-patched.py trader-v3-node-a [BINANCE_EXEC_DST]
BINANCE_EXEC_DST 为容器内 nautilus binance execution 模块路径，先用：
  docker exec trader-v3-node-a python -c \
    "import nautilus_trader.adapters.binance.execution as m; print(m.__file__)"
探明后传入；不传则跳过该挂载并警告。
"""
import json, subprocess, sys, shlex

name = sys.argv[1]                       # trader-v3-node-a
binance_dst = sys.argv[2] if len(sys.argv) > 2 else None
suffix = name.rsplit("-", 1)[-1]         # a / b
insp = json.loads(subprocess.check_output(["docker", "inspect", name]))[0]

image = insp["Config"]["Image"]
env = insp["Config"]["Env"]
cmd = insp["Config"]["Cmd"] or []
entry = insp["Config"]["Entrypoint"] or []
net = list(insp["NetworkSettings"]["Networks"].keys())[0]
restart = insp["HostConfig"]["RestartPolicy"]["Name"] or "unless-stopped"
mounts = insp["Mounts"]

CP = "/srv/trader-v3/container-patches"
lines = ["#!/bin/bash", "set -euo pipefail",
         f"docker rm -f {name} 2>/dev/null || true",
         f"mkdir -p /srv/trader-v3/node-state/{suffix}"]
run = ["docker", "run", "-d", "--name", name, f"--network={net}",
       f"--restart={restart}"]
for e in env:
    if e.startswith(("PATH=", "PYTHON", "LANG=", "GPG_KEY", "HOME=")):
        continue
    run += ["-e", e]

# 显式补丁挂载优先于继承的旧 Mounts（同 dst 时丢弃旧条目）
extra = [
    (f"/srv/trader-v3/node-state/{suffix}", "/state", "rw"),
    (f"{CP}/intent_execution_planner.py", "/app/strategy/intent_execution_planner.py", "ro"),
    (f"{CP}/contracts.py", "/app/execution_domain/contracts.py", "ro"),
    (f"{CP}/projection_actor.py", "/app/projection/actor.py", "ro"),
    (f"{CP}/event_mapper.py", "/app/projection/event_mapper.py", "ro"),
    (f"{CP}/intent_execution_strategy.py", "/app/strategy/intent_execution_strategy.py", "ro"),
    (f"{CP}/exchange_cancel_adapter.py", "/app/runtime/exchange_cancel_adapter.py", "ro"),
    (f"{CP}/node.py", "/app/app/node.py", "ro"),
]
if binance_dst:
    extra.append((f"{CP}/binance_execution.py", binance_dst, "ro"))
else:
    print("WARN: BINANCE_EXEC_DST 未提供，binance_execution.py 本轮仍不挂载！", file=sys.stderr)

explicit_dst = {d for _, d, _ in extra}
seen_dst = set()
for m in mounts:
    dst = m["Destination"]
    if dst in explicit_dst:
        continue  # 显式版本覆盖继承版本
    seen_dst.add(dst)
    mode = "ro" if not m.get("RW", True) else "rw"
    run += ["-v", f"{m['Source']}:{dst}:{mode}"]
import os
for src, dst, mode in extra:
    if not os.path.exists(src):
        print(f"FATAL: 挂载源不存在 {src}", file=sys.stderr)
        sys.exit(1)
    run += ["-v", f"{src}:{dst}:{mode}"]
run += ["-e", "NODE_STATE_DIR=/state"]
if entry:
    run += ["--entrypoint", entry[0]]
run += [image] + (entry[1:] if len(entry) > 1 else []) + cmd
lines.append(" ".join(shlex.quote(x) for x in run))
out = f"/srv/trader-v3/recreate-{name}.sh"
with open(out, "w") as fh:
    fh.write("\n".join(lines) + "\n")
os.chmod(out, 0o700)
print(f"WROTE {out}")
print("image:", image, "| net:", net, "| restart:", restart)
print("mount dsts:", sorted(seen_dst | explicit_dst))
