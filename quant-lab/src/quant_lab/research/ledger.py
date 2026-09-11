"""尝试账本（契约 feature-snapshot §5；ADR-G3 §8）。路径经 QUANT_LAB_DATA_ROOT 解析：
  <data_root>/lockbox/ledger-events/<seq>-<attempt_id>-<status>.json   提交事件（临时文件 + 原子改名，只追加）
  <data_root>/lockbox/ledger.parquet                                    当前投影（由事件重建，原子替换）

规则：每次评估**前**先 reserve（lint / 收益访问之前落盘），失败/非法/重复/超时/中断都留终态；投影坏了只重建，不删事件；
单写者（目录锁）；预算先原子预留后派发（唯一配置额度超限 → budget_exhausted 且不派发）；重复配置记 duplicate 不消失、
不重复耗额度；invalidated 另追加事件，保留原终态。不落原始敏感正文或外来代码。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import socket
import re
import resource
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from quant_lab.research import paths

LEDGER_COLUMNS = ("attempt_id", "origin", "parent_id", "canonical_hash", "params", "fold_id", "visible_cutoff",
                  "code_version", "model_version", "data_manifest", "seed", "cost", "objective", "status")
EXTRA_COLUMNS = ("event_seq", "run_id", "scope", "stage", "caller_horizon_end", "raw_input_hash", "rule_hash", "market_manifest", "graph_version",
                 "backend_version", "reason_code", "created_at", "finished_at", "duplicate_of", "invalidated_by", "config_id", "owner_pid", "owner_host", "protocol_hash", "opportunity_set_hash", "policy_version", "policy_hash", "result_hash", "recompute_of")
ORIGINS = ("human", "llm", "enumeration", "gp")
STAGES = ("search", "selection", "outer", "final", "null_validation")
TERMINAL = frozenset({"completed", "rejected", "duplicate", "failed", "timeout", "budget_exhausted", "interrupted", "insufficient"})
LIVE = frozenset({"reserved", "running"})


class LedgerError(RuntimeError):
    pass


class LedgerBudgetExhausted(LedgerError):
    pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="microseconds")


def _json(o) -> str:
    return json.dumps(o, sort_keys=True, ensure_ascii=False, default=str)


def config_id(canonical_hash: str | None, params: dict | None, objective: str, rule_hash: str | None = None,
              caller_horizon_end=None) -> str:
    """唯一配置身份（跨折/来源共享额度）：AST + 参数 + 目标 + 规则 + **调用方自选的观察窗**。

    execution-interface §5.13 B11：`horizon_source == "caller"` 时 `horizon_end` 是分叉路径自由度（换个观察窗重跑到结果好看），
    必须进配置身份，使"试多个窗口"消耗尝试预算、在账本里可见、被 max-t 与分档惩罚；`horizon_source == "policy"` 的推导窗
    由 `policy_hash` 覆盖，**不入**（传 None）。纳入本身不加重预算：同值仍识别为 duplicate，只在取值不同时把尝试分开。
    """
    h = caller_horizon_end.isoformat() if hasattr(caller_horizon_end, "isoformat") else caller_horizon_end
    payload = [canonical_hash, params or {}, objective, rule_hash]
    if h is not None:
        payload.append({"caller_horizon_end": h})
    return hashlib.sha256(_json(payload).encode()).hexdigest()[:24]


@dataclass
class Ledger:
    root: Path = field(default_factory=paths.lockbox_dir)
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    code_version: str = "g3-research-v0"
    budget_configs: int | None = None          # 唯一合法配置上限（None = 不限），按 scope 计
    scope: str = "default"                     # 协议范围（run_protocol 传 protocol_hash）：重复判定与预算只在同 scope 内

    def __post_init__(self):
        self.lineage = {}
        self._timers = {}
        self.root = Path(self.root)
        self.events_dir = self.root / "ledger-events"
        self.projection = self.root / "ledger.parquet"
        self._seq = self._max_seq() + 1

    # ------------------------------------------------------------ 事件写入
    def _max_seq(self) -> int:
        if not self.events_dir.exists():
            return 0
        seqs = []
        for p in self.events_dir.glob("*.json"):
            try:
                seqs.append(int(p.name.split("-", 1)[0]))
            except ValueError:
                raise LedgerError(f"事件文件名非法（fail closed）: {p.name}") from None
        return max(seqs, default=0)

    def _lock(self):
        lock = self.root / ".ledger.lock"
        paths.ensure_dir(self.root)
        for _ in range(3000):
            try:
                os.mkdir(lock)
                (lock / "owner").write_text(str(os.getpid()))
                return lock
            except FileExistsError:
                try:
                    st = lock.stat()
                    owner = int((lock / "owner").read_text()) if (lock / "owner").exists() else None
                except (FileNotFoundError, ValueError):
                    time.sleep(0.01); continue
                dead = owner is not None and not _pid_alive(owner)
                if time.time() - st.st_mtime > 60 and (dead or owner is None):
                    try:
                        (lock / "owner").unlink(missing_ok=True); os.rmdir(lock)
                    except OSError:
                        pass
                    continue
                time.sleep(0.01)
        raise LedgerError("账本锁超时")

    @staticmethod
    def _unlock(lock: Path) -> None:
        try:
            (lock / "owner").unlink(missing_ok=True)
        except OSError:
            pass
        os.rmdir(lock)

    def _commit_locked(self, event: dict) -> dict:
        """须在持锁下调用：写事件（唯一临时文件 + fsync + 原子改名 + 目录 fsync），然后推进投影。"""
        paths.ensure_dir(self.events_dir)
        self._seq = max(self._seq, self._max_seq() + 1)
        event = {"event_seq": self._seq, "event_at": _now(), **event}
        name = f"{self._seq:012d}-{event['attempt_id']}-{event['status']}.json"
        tmp = self.events_dir / f".{name}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(_json(event))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.events_dir / name)
        try:
            dfd = os.open(self.events_dir, os.O_RDONLY)
            os.fsync(dfd); os.close(dfd)
        except OSError:
            pass
        self._seq += 1
        self._rebuild_locked()
        return event

    def _commit(self, event: dict) -> dict:
        lock = self._lock()
        try:
            return self._commit_locked(event)
        finally:
            self._unlock(lock)

    # ------------------------------------------------------------ 公共 API
    def reserve(self, *, origin: str, canonical_hash: str | None, params: dict | None, fold_id: str, visible_cutoff, objective: str,
                data_manifest: str, seed: int, stage: str = "search", parent_id: str | None = None, model_version: str | None = None,
                raw_input_hash: str | None = None, rule_hash: str | None = None, market_manifest: str | None = None,
                graph_version: str | None = None, backend_version: str | None = None, cost: dict | None = None,
                recompute_of: str | None = None, budget_exempt: bool = False, caller_horizon_end=None) -> str:
        """评估前预留：写 reserved 事件并返回 attempt_id。重复配置（同 config_id + fold + stage + cutoff 已 completed）→ 直接记 duplicate；
        recompute_of=<原 attempt_id> 时显式重算并关联原尝试（计计算调用，不耗配置额度）；
        唯一配置额度超限 → 记 budget_exhausted 并抛 LedgerBudgetExhausted（不派发）。读/判/写在同一锁内。"""
        if origin not in ORIGINS:
            raise LedgerError(f"origin 须为 {ORIGINS}")
        if stage not in STAGES:
            raise LedgerError(f"stage 须为 {STAGES}")
        cid = config_id(canonical_hash, params, objective, rule_hash, caller_horizon_end)   # B11：caller 观察窗进配置身份
        attempt_id = uuid.uuid4().hex
        base = {
            "attempt_id": attempt_id, "run_id": self.run_id, "owner_pid": str(os.getpid()), "owner_host": socket.gethostname(),
            **self.lineage, "origin": origin, "parent_id": parent_id, "canonical_hash": canonical_hash,
            "params": _json(params or {}), "caller_horizon_end": (caller_horizon_end.isoformat() if hasattr(caller_horizon_end, "isoformat") else caller_horizon_end), "fold_id": fold_id, "visible_cutoff": visible_cutoff.isoformat() if hasattr(visible_cutoff, "isoformat") else visible_cutoff,
            "code_version": hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))).hexdigest(), "model_version": model_version, "data_manifest": data_manifest, "seed": int(seed),
            "cost": _json(cost or {"wall_seconds": 0.0, "cpu_seconds": 0.0, "peak_bytes": 0, "api_cost": 0.0}), "objective": objective, "stage": stage, "raw_input_hash": raw_input_hash, "rule_hash": rule_hash,
            "market_manifest": market_manifest, "graph_version": graph_version, "backend_version": backend_version, "scope": self.scope,
            "config_id": cid, "created_at": _now(), "finished_at": None, "reason_code": None, "duplicate_of": None, "invalidated_by": None,
        }
        self._timers[attempt_id] = (time.perf_counter(), time.process_time())
        lock = self._lock()          # 读投影 → 去重/预算判定 → 提交 在同一跨进程锁内（原子预留）
        try:
            proj = self._rebuild_locked()
            if proj.height:
                proj = proj.filter(pl.col("scope").fill_null("default") == self.scope)
            dup = None
            if proj.height:
                same = proj.filter((pl.col("config_id") == cid) & (pl.col("fold_id") == fold_id) & (pl.col("stage") == stage)
                                   & (pl.col("visible_cutoff") == base["visible_cutoff"]) & (pl.col("status") == "completed"))
                if same.height:
                    dup = same["attempt_id"][0]
            if dup is not None and recompute_of is None:
                self._commit_locked({**base, "status": "duplicate", "duplicate_of": dup, "finished_at": _now(), "reason_code": "DUPLICATE_CONFIG"})
                return attempt_id
            if recompute_of is not None:
                original = self._row(recompute_of)
                if original["status"] == "duplicate":
                    original = self._row(original["duplicate_of"])
                if any(original.get(k) != base.get(k) for k in ("config_id", "fold_id", "stage", "visible_cutoff", "scope")):
                    raise LedgerError("recompute_of 与原配置/窗口/协议不一致")
                recompute_of = original["attempt_id"]
                base["recompute_of"] = recompute_of
                base["parent_id"] = recompute_of          # 重复配置的显式重算：新尝试关联原尝试，计计算调用，不耗配置额度
                base["reason_code"] = "RECOMPUTE_OF_DUPLICATE"
            if self.budget_configs is not None and not budget_exempt:
                used = set(proj.filter(~pl.col("status").is_in(["duplicate", "budget_exhausted"]) & ~pl.col("stage").is_in(["final"])
                                       & (pl.col("objective") != "feature_snapshot"))["config_id"].to_list()) if proj.height else set()
                if cid not in used and len(used) >= self.budget_configs:
                    self._commit_locked({**base, "status": "budget_exhausted", "finished_at": _now(), "reason_code": "BUDGET_CONFIGS"})
                    raise LedgerBudgetExhausted(f"唯一配置额度 {self.budget_configs} 已耗尽")
            self._commit_locked({**base, "status": "reserved"})
            return attempt_id
        finally:
            self._unlock(lock)

    def mark(self, attempt_id: str, status: str, *, reason: str | None = None, objective: float | None = None, cost: dict | None = None, result_hash: str | None = None, canonical_hash: str | None = None) -> None:
        """状态转移：读当前态 → 校验 → 提交，全部在同一跨进程锁内（CAS 语义，终态不可被并发改写）。"""
        if status not in TERMINAL | LIVE:
            raise LedgerError(f"未知状态 {status}")
        lock = self._lock()
        try:
            row = self._row(attempt_id)
            if row["status"] in TERMINAL:
                raise LedgerError(f"attempt {attempt_id} 已终态 {row['status']}，不能改写为 {status}（失效请用 invalidate）")
            ev = {**row, "status": status, "reason_code": reason if reason is not None else row.get("reason_code")}
            if canonical_hash is not None:
                ev["canonical_hash"] = canonical_hash
            if objective is not None:
                ev["objective_value"] = objective
            if cost is not None:
                ev["cost"] = _json(cost)
            if status == "reserved":
                raise LedgerError("非法状态转移：不能重新 reserved")
            if status in TERMINAL:
                ev["finished_at"] = _now()
                started = self._timers.pop(attempt_id, None)
                wall = max(0.0, (dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(row["created_at"])).total_seconds())
                cpu = 0.0
                if started is not None:
                    wall = time.perf_counter() - started[0]
                    cpu = time.process_time() - started[1]
                peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                if sys.platform != "darwin":
                    peak *= 1024
                ev["cost"] = _json({**(cost or {}), "wall_seconds": wall, "cpu_seconds": cpu,
                                    "peak_bytes": peak, "api_cost": 0.0})
                if status == "completed":
                    ev["result_hash"] = result_hash or hashlib.sha256(_json({"objective": row["objective"], "value": objective}).encode()).hexdigest()
            self._commit_locked(ev)
        finally:
            self._unlock(lock)

    def status_of(self, attempt_id: str) -> str:
        return self._row(attempt_id)["status"]

    def invalidate(self, attempt_id: str, by: str) -> None:
        """图版本撤销等：追加 invalidated 事件，保留原终态。"""
        lock = self._lock()
        try:
            row = self._row(attempt_id)
            self._commit_locked({**row, "status": row["status"], "invalidated_by": by, "event_kind": "invalidated"})
        finally:
            self._unlock(lock)

    def recover(self, *, run_id: str | None = None, max_age_s: float = 3600.0) -> int:
        """恢复：只把**确认失活**的未闭合 attempt 标 interrupted——指定 run_id 的全部 live 行，或（未指定时）
        同主机 run 所有者 pid 已失活的 live 行；无所有权证据不恢复。max_age_s 仅保留接口兼容。"""
        proj = self.read()
        n = 0
        if not proj.height:
            return 0
        live = proj.filter(pl.col("status").is_in(list(LIVE)))
        for r in live.iter_rows(named=True):
            if run_id is not None:
                if r.get("run_id") != run_id:
                    continue
            else:
                if r.get("owner_host") != socket.gethostname() or not r.get("owner_pid") or not r.get("run_id"):
                    continue
                if _pid_alive(int(r["owner_pid"])):
                    continue
            try:
                self.mark(r["attempt_id"], "interrupted", reason="RECOVERED_STALE" if run_id is None else "RECOVERED_RUN")
                n += 1
            except LedgerError:
                pass
        return n

    # ------------------------------------------------------------ 投影
    def _events(self) -> list[dict]:
        if not self.events_dir.exists():
            return []
        out = []
        seqs = set()
        previous = {}
        for p in sorted(self.events_dir.glob("*.json")):
            try:
                with open(p, encoding="utf-8") as f:
                    ev = json.load(f)
                if not isinstance(ev, dict) or "attempt_id" not in ev or "status" not in ev or "event_seq" not in ev:
                    raise LedgerError(f"事件缺必需键: {p.name}")
                seq, aid, status = ev["event_seq"], ev["attempt_id"], ev["status"]
                if type(seq) is not int or seq <= 0 or p.name != f"{seq:012d}-{aid}-{status}.json" or seq in seqs:
                    raise LedgerError(f"事件序号/文件名不一致或重复（fail closed）: {p.name}")
                if not re.fullmatch(r"[0-9a-f]{32}", aid) or status not in LIVE | TERMINAL:
                    raise LedgerError(f"事件身份/状态非法: {p.name}")
                prior = previous.get(aid)
                if prior is None:
                    valid = status in ("reserved", "duplicate", "budget_exhausted") and not ev.get("event_kind")
                elif ev.get("event_kind") == "invalidated":
                    valid = status == prior["status"] and bool(ev.get("invalidated_by"))
                else:
                    valid = prior["status"] in LIVE and status != "reserved" and not ev.get("event_kind")
                if prior is not None and any(ev.get(k) != prior.get(k) for k in ("run_id", "owner_pid", "owner_host", "config_id")):
                    valid = False
                if not valid:
                    raise LedgerError(f"非法状态转移或所有权变更: {p.name}")
                seqs.add(seq)
                previous[aid] = ev
                out.append(ev)
            except LedgerError:
                raise
            except Exception as e:
                raise LedgerError(f"账本事件损坏（fail closed，不跳过）: {p.name}: {e}") from None
        return out

    def _row(self, attempt_id: str) -> dict:
        evs = [e for e in self._events() if e["attempt_id"] == attempt_id]
        if not evs:
            raise LedgerError(f"未知 attempt {attempt_id}")
        return {k: v for k, v in evs[-1].items() if k not in ("event_seq", "event_at", "event_kind")}

    def rebuild_projection(self) -> pl.DataFrame:
        lock = self._lock()
        try:
            return self._rebuild_locked()
        finally:
            self._unlock(lock)

    def _rebuild_locked(self) -> pl.DataFrame:
        evs = self._events()
        latest: dict[str, dict] = {}
        for e in evs:
            latest[e["attempt_id"]] = {**latest.get(e["attempt_id"], {}), **e}
        cols = list(LEDGER_COLUMNS) + list(EXTRA_COLUMNS) + ["objective_value"]
        rows = [{c: r.get(c) for c in cols} for r in latest.values()]
        schema = {c: pl.Utf8 for c in cols}
        schema.update({"seed": pl.Int64, "event_seq": pl.Int64, "objective_value": pl.Float64})
        df = pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
        watermark = max((e["event_seq"] for e in evs), default=0)
        df = df.with_columns(pl.lit(watermark, dtype=pl.Int64).alias("__watermark"))
        paths.ensure_dir(self.root)
        tmp = self.root / f".ledger.parquet.{uuid.uuid4().hex}.tmp"
        df.write_parquet(tmp)
        os.replace(tmp, self.projection)
        return df.drop("__watermark")

    def read(self) -> pl.DataFrame:
        """读当前投影；投影缺失/损坏/schema 落后/事件水位领先 → 从事件重放重建（不漏账）。"""
        self._events()  # 即使投影水位相同，也不能隐藏事件篡改。
        if not self.projection.exists():
            return self.rebuild_projection()
        try:
            df = pl.read_parquet(self.projection)
        except Exception:
            return self.rebuild_projection()
        want = set(LEDGER_COLUMNS) | set(EXTRA_COLUMNS) | {"objective_value", "__watermark"}
        if not want <= set(df.columns):
            return self.rebuild_projection()
        wm = int(df["__watermark"][0]) if df.height else 0
        if self._max_seq() != wm:
            return self.rebuild_projection()
        return df.drop("__watermark")


__all__ = ["EXTRA_COLUMNS", "LEDGER_COLUMNS", "LIVE", "ORIGINS", "STAGES", "TERMINAL", "Ledger", "LedgerBudgetExhausted", "LedgerError", "MemoryLedger", "config_id"]


def _main(argv=None):  # pragma: no cover - CLI
    """`python -m quant_lab.research.ledger --seed-synthetic`：向当前 QUANT_LAB_DATA_ROOT 的账本写入 3 条合成尝试（R-07 冒烟用，非研究记录）。"""
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-synthetic", action="store_true")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args(argv)
    led = Ledger()
    if a.seed_synthetic:
        t0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
        for i, st in enumerate(("completed", "failed", "rejected")):
            aid = led.reserve(origin="enumeration", canonical_hash=f"synthetic-seed-{i}", params={"i": i}, fold_id="wf000", visible_cutoff=t0,
                              objective="theta", data_manifest="synthetic", seed=i, stage="search", graph_version="gv-synth-0001")
            led.mark(aid, st, reason="SYNTHETIC_SEED", objective=0.0 if st == "completed" else None)
        print(f"seeded 3 synthetic attempts → {led.projection}")
    if a.show or not a.seed_synthetic:
        print(led.read())


if __name__ == "__main__":  # pragma: no cover
    _main()


class MemoryLedger(Ledger):
    """内存事件存储；预留、预算、终态、成本与磁盘账本共用同一实现。"""

    def __init__(self, budget_configs: int | None = None):
        self.run_id = uuid.uuid4().hex[:12]
        self.scope = "default"
        self.budget_configs = budget_configs
        self.code_version = "g3-research-v0"
        self.lineage = {}
        self._timers = {}
        self.rows = {}
        self.events = []

    def _lock(self):
        return False

    @staticmethod
    def _unlock(lock):
        pass

    def _commit_locked(self, event):
        event = {**event, "event_seq": len(self.events) + 1, "event_at": _now()}
        self.events.append(event)
        self.rows[event["attempt_id"]] = event
        return event

    def _events(self):
        return self.events

    def _rebuild_locked(self):
        return self.read()

    def read(self):
        if not self.rows:
            return pl.DataFrame()
        return pl.DataFrame(list(self.rows.values()), infer_schema_length=None)

    @property
    def n_attempts(self):
        return len(self.rows)
