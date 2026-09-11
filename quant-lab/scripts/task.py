#!/usr/bin/env python3
"""taskList.json 安全读写 CLI —— 所有窗口通过它更新看板。

设计要点：
- 目录锁 (os.mkdir 原子) + 临时文件替换 (os.replace 原子)，多窗口并发写不丢数据。
- 每个窗口只应改自己的 modules.<module>；contracts/integration 仅 G0（orchestrator）可写。
- 纯标准库，任何窗口 `python scripts/task.py ...` 即用。
"""
import argparse, json, os, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARD = os.path.join(ROOT, "taskList.json")
LOCK = os.path.join(ROOT, "taskList.lock")
def _modules():
    """模块名从看板动态读取,新项目改 taskList.json 即可,无需改本文件。"""
    try:
        with open(BOARD, encoding="utf-8") as f:
            return sorted(json.load(f).get("modules", {}).keys())
    except Exception:
        return []


MODULES = None  # 延迟加载,见 main()
STALE = 60  # 秒：超过则视为死锁并抢占


def _acquire(timeout=15):
    start = time.time()
    while True:
        try:
            os.mkdir(LOCK)
            return
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(LOCK) > STALE:
                    os.rmdir(LOCK)
                    continue
            except FileNotFoundError:
                continue
            if time.time() - start > timeout:
                sys.exit("!! 获取看板锁超时，稍后重试（或删除 taskList.lock/）")
            time.sleep(0.2)


def _release():
    try:
        os.rmdir(LOCK)
    except FileNotFoundError:
        pass


def _load():
    with open(BOARD, encoding="utf-8") as f:
        return json.load(f)


def _save(d):
    d["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    fd, tmp = tempfile.mkstemp(dir=ROOT, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BOARD)


def _rmw(fn):
    _acquire()
    try:
        d = _load()
        fn(d)
        d["modules"] and None
        _save(d)
    finally:
        _release()


def _touch(mod, d):
    d["modules"][mod]["lastUpdate"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def cmd_show(a):
    d = _load()
    if a.module:
        print(json.dumps(d["modules"][a.module], ensure_ascii=False, indent=2))
        return
    print(f"# {d['project']}  phase={d['phase']}  updated={d['updatedAt']}")
    for m, v in d["modules"].items():
        done = sum(t["status"] == "done" for t in v["tasks"])
        tot = len(v["tasks"])
        bl = " ⛔" + ";".join(v["blockers"]) if v["blockers"] else ""
        rv = f" review={v['review']['verdict']}" if v.get("review") else ""
        print(f"  {v['owner']:>3} {m:<12} {v['status']:<12} {v['progress']*100:>4.0f}%  tasks {done}/{tot}{rv}{bl}")
    ig = d["integration"]
    print(f"  --- integration readyForStitch={ig['readyForStitch']} blockers={len(ig['blockers'])}")


def cmd_report(a):
    def fn(d):
        m = d["modules"][a.module]
        if a.status:
            m["status"] = a.status
        if a.progress is not None:
            m["progress"] = a.progress
        if a.note:
            m.setdefault("notes", []).append({"t": time.strftime("%H:%M"), "n": a.note})
        _touch(a.module, d)
    _rmw(fn)
    print(f"ok: {a.module} 已上报")


def _set_task(d, mod, tid, status):
    for t in d["modules"][mod]["tasks"]:
        if t["id"] == tid:
            t["status"] = status
            return True
    sys.exit(f"!! 未找到任务 {tid}（在 {mod} 下）")


def cmd_claim(a):
    _rmw(lambda d: (_set_task(d, a.module, a.task, "doing"), _touch(a.module, d)))
    print(f"ok: 认领 {a.task}")


def cmd_done(a):
    def fn(d):
        _set_task(d, a.module, a.task, "done")
        ts = d["modules"][a.module]["tasks"]
        d["modules"][a.module]["progress"] = round(sum(t["status"] == "done" for t in ts) / len(ts), 2)
        _touch(a.module, d)
    _rmw(fn)
    print(f"ok: 完成 {a.task}")


def cmd_set_verify(a):
    """改自己模块某任务的 verify 命令（走同一目录锁 + 原子替换；禁止手改 JSON）。

    用途：把负向 grep（`! grep -q '未闭合'`，报告变了就假绿）改成正向精确判读（契约 §9.9 A7）。
    """
    def fn(d):
        for t in d["modules"][a.module]["tasks"]:
            if t["id"] == a.task:
                t.setdefault("verifyHistory", []).append({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                                          "old": t.get("verify", ""), "why": a.why or ""})
                t["verify"] = a.cmd
                _touch(a.module, d)
                return
        sys.exit(f"!! 未找到任务 {a.task}（在 {a.module} 下）")
    _rmw(fn)
    print(f"ok: {a.module} {a.task} verify 已更新")


def cmd_block(a):
    def fn(d):
        d["modules"][a.module]["blockers"].append(a.on)
        d["modules"][a.module]["status"] = "blocked"
        _touch(a.module, d)
    _rmw(fn)
    print(f"ok: {a.module} 已标记阻塞")


def cmd_unblock(a):
    def fn(d):
        d["modules"][a.module]["blockers"] = []
        if d["modules"][a.module]["status"] == "blocked":
            d["modules"][a.module]["status"] = "in_progress"
        _touch(a.module, d)
    _rmw(fn)
    print(f"ok: {a.module} 解除阻塞")


def cmd_review(a):  # 仅 orchestrator(G0) 使用
    def fn(d):
        d["modules"][a.module]["review"] = {
            "verdict": a.verdict, "note": a.note or "",
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        d["integration"]["reviews"].append(
            {"at": time.strftime("%H:%M"), "module": a.module, "verdict": a.verdict, "note": a.note or ""})
    _rmw(fn)
    print(f"ok: 评审 {a.module} = {a.verdict}")


def main():
    global MODULES
    MODULES = _modules() or None
    p = argparse.ArgumentParser(description="taskList.json 看板 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show"); s.add_argument("module", nargs="?", choices=MODULES); s.set_defaults(f=cmd_show)
    s = sub.add_parser("report"); s.add_argument("module", choices=MODULES); s.add_argument("--status"); s.add_argument("--progress", type=float); s.add_argument("--note"); s.set_defaults(f=cmd_report)
    s = sub.add_parser("claim"); s.add_argument("module", choices=MODULES); s.add_argument("task"); s.set_defaults(f=cmd_claim)
    s = sub.add_parser("done"); s.add_argument("module", choices=MODULES); s.add_argument("task"); s.set_defaults(f=cmd_done)
    s = sub.add_parser("set-verify"); s.add_argument("module", choices=MODULES); s.add_argument("task"); s.add_argument("--cmd", required=True); s.add_argument("--why"); s.set_defaults(f=cmd_set_verify)
    s = sub.add_parser("block"); s.add_argument("module", choices=MODULES); s.add_argument("--on", required=True); s.set_defaults(f=cmd_block)
    s = sub.add_parser("unblock"); s.add_argument("module", choices=MODULES); s.set_defaults(f=cmd_unblock)
    s = sub.add_parser("review"); s.add_argument("module", choices=MODULES); s.add_argument("--verdict", required=True, choices=["pass", "issues", "fail"]); s.add_argument("--note"); s.set_defaults(f=cmd_review)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
