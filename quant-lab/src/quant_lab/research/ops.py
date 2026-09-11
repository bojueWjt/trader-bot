"""算子登记表 REGISTRY（契约 feature-snapshot §2）。

每个算子一条 OpSpec：name, arity, input_units, output_unit, window_semantics ∈ {wallclock, rows, none}, lookback,
lookahead=0, min_samples, nan_policy, ddof?, tie_rule?, init_rule?, version, backends。
R-03 先落登记表元数据（lint 依赖它做 op/arity/window/单位检查）；R-04 挂后端实现与契约测试。

单位代数（lint 用）：字段单位 price / volume / count；const 为 scalar（与任意单位相容）；
same 表示各参数单位必须一致并原样输出；bool 只能进 And。本 DSL Ref(x, 0) = 当前闭合值。
"""
from __future__ import annotations

from dataclasses import dataclass, field

FIELD_UNITS: dict[str, str] = {
    "open": "price", "high": "price", "low": "price", "close": "price",
    "volume": "volume", "quote_volume": "volume", "taker_buy_volume": "volume", "taker_buy_quote_volume": "volume",
    "trades": "count",
}
#: 跨资产算子：契约 §1 硬门拒收（单品种窗口隔离，不开放 center / 跨资产 rank）。
CROSS_ASSET_OPS = frozenset({"CSRank", "CSMean", "CSZScore", "CSDemean", "CSScale"})


@dataclass(frozen=True)
class OpSpec:
    name: str
    arity: int
    input_units: tuple[str, ...]      # 每参数：same | any | bool | scalar
    output_unit: str                  # same | bool | ratio | log | rank | derived
    window_semantics: str             # 契约 §2 三值：wallclock | rows | none（主语义）
    lookback: int | None              # None = 由 window/params 决定
    lookahead: int = 0
    min_samples: int = 1
    nan_policy: str = "propagate"
    ddof: int | None = None
    tie_rule: str | None = None
    init_rule: str | None = None
    version: str = "0.1"
    backends: tuple[str, ...] = ("polars",)
    params: dict = field(default_factory=dict)   # 参数名 → {"type", "min"}
    commutative: bool = False
    contract_status: str = "active"              # active | quarantined（契约测试失败即隔离，lint 拒收）
    alt_windows: tuple[str, ...] = ()            # 额外接受的窗口单位（wallclock 主语义的算子也接受 rows），不扩契约枚举

    @property
    def windowed(self) -> bool:
        """是否接受 window 节点键；Ref/Delta 的回看由 params.lag 表达，不接受 window。"""
        return self.window_semantics != "none" and "lag" not in self.params


def _spec(name, arity, inp, out, *, win="none", lookback=0, params=None, commutative=False, alt=(), **kw) -> OpSpec:
    assert win in ("wallclock", "rows", "none")
    return OpSpec(name=name, arity=arity, input_units=tuple(inp), output_unit=out, window_semantics=win, alt_windows=tuple(alt),
                  lookback=lookback, params=params or {}, commutative=commutative, backends=("polars", "polars_ta"), **kw)


_ROWS_OR_WALL = "wallclock"   # 主语义 wallclock；alt=("rows",) 表示也接受 rows.count
REGISTRY: dict[str, OpSpec] = {s.name: s for s in [
    _spec("Add", 2, ("same", "same"), "same", commutative=True),
    _spec("Sub", 2, ("same", "same"), "same"),
    _spec("Mul", 2, ("any", "any"), "derived", commutative=True),
    _spec("SafeDiv", 2, ("any", "any"), "derived", nan_policy="propagate; 分母 0 → NaN"),
    _spec("Abs", 1, ("any",), "same"),
    _spec("LogPositive", 1, ("any",), "log", nan_policy="propagate; x <= 0 → NaN"),
    _spec("Gt", 2, ("same", "same"), "bool", nan_policy="任一 NaN → null"),
    _spec("Lt", 2, ("same", "same"), "bool", nan_policy="任一 NaN → null"),
    _spec("And", 2, ("bool", "bool"), "bool", commutative=True, nan_policy="null 参与 → null（三值逻辑）"),
    _spec("Ref", 1, ("any",), "same", win="rows", lookback=None, params={"lag": {"type": "int", "min": 0}},
          nan_policy="lag 槽位缺失或无效 → invalid（不跨缺失槽位找更早有效值）"),
    _spec("Delta", 1, ("any",), "same", win="rows", lookback=None, params={"lag": {"type": "int", "min": 1}},
          nan_policy="lag 槽位缺失或无效 → invalid（不跨缺失槽位找更早有效值）"),
    _spec("Mean", 1, ("any",), "same", win=_ROWS_OR_WALL, alt=("rows",), lookback=None, nan_policy="窗内任一 NaN → NaN；样本 < min_samples → null"),
    _spec("Std", 1, ("any",), "same", win=_ROWS_OR_WALL, alt=("rows",), lookback=None, ddof=1, min_samples=2,
          nan_policy="窗内任一 NaN → NaN；样本 < 2 → null"),
    _spec("Min", 1, ("any",), "same", win=_ROWS_OR_WALL, alt=("rows",), lookback=None, nan_policy="窗内任一 NaN → NaN"),
    _spec("Max", 1, ("any",), "same", win=_ROWS_OR_WALL, alt=("rows",), lookback=None, nan_policy="窗内任一 NaN → NaN"),
    _spec("TSRank", 1, ("any",), "rank", win=_ROWS_OR_WALL, alt=("rows",), lookback=None, tie_rule="average",
          nan_policy="窗内任一 NaN → NaN；rank ∈ (0,1]，当前值在窗内的平均秩 / 窗样本数"),
    _spec("Corr", 2, ("any", "any"), "ratio", win=_ROWS_OR_WALL, alt=("rows",), lookback=None, ddof=1, min_samples=3, commutative=True,
          nan_policy="窗内任一 NaN → NaN；零方差 → NaN"),
    _spec("EMA", 1, ("any",), "same", win="rows", lookback=None, init_rule="前 n 个连续有效槽位的均值（SMA）为种子，adjust=False；缺值/缺 bar 重置 warm-up",
          nan_policy="invalid → 输出 invalid 并重置 warm-up"),
]}

FIRST_BATCH = tuple(REGISTRY)
assert len(FIRST_BATCH) == 18   # 契约 §2 明列 18 个（ADR C02 称 19 系误数）

#: 契约测试失败的算子进隔离；lint 拒收其 AST（ADR §2.1 "算子状态"）。运行期集合，进程内有效。
QUARANTINE: set[str] = set()


def quarantine(op: str, reason: str = "") -> None:
    if op not in REGISTRY:
        raise KeyError(op)
    QUARANTINE.add(op)


def release(op: str) -> None:
    QUARANTINE.discard(op)


def is_quarantined(op: str) -> bool:
    return op in QUARANTINE or REGISTRY[op].contract_status == "quarantined"


def registry_digest() -> str:
    """登记表内容摘要（名称、版本、语义字段），进语义身份与缓存 key。"""
    import hashlib, json
    payload = [(s.name, s.arity, s.window_semantics, s.min_samples, s.ddof, s.version, sorted(s.params)) for s in REGISTRY.values()]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

__all__ = ["CROSS_ASSET_OPS", "FIELD_UNITS", "FIRST_BATCH", "OpSpec", "QUARANTINE", "REGISTRY", "is_quarantined", "quarantine", "registry_digest", "release"]
