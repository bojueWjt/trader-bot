"""AST JSON schema / lint 硬门 / 规范化 / canonical_hash（契约 feature-snapshot §1；ADR-G3 §2；合并稿 D.2）。

唯一入口是 JSON 对象树；不接受 Python 字符串表达式，不执行任何外来代码（无 eval / compile / from_string）。
文本传输层用 parse_json_text：拒绝重复对象键、NaN/Infinity 扩展、尾随内容、非对象根、> 64 KiB。

节点三类（不混用、不附加 metadata）：
  {"field": <registered_field>}
  {"const": <finite number>}
  {"op": <registered_op>, "args": [...], "window"?: {"unit": "wallclock", "minutes": int>0} | {"unit": "rows", "count": int>0},
   "params"?: {"lag": int>=0}}

lint 硬门（任一命中 → ASTRejected(code, detail, path)）：
  NOT_A_DICT / EMPTY / UNKNOWN_KEY / UNKNOWN_OP / OP_QUARANTINED / CROSS_ASSET / UNKNOWN_FIELD / CONST_TYPE / NON_FINITE_CONST /
  DEPTH（根深度 1、叶节点计数，深度 > 4）/ WIDTH（节点 > 15，重复子树按每次出现计）/ ARITY / PARAM / NEGATIVE_REF /
  WINDOW_MISSING / WINDOW_FORBIDDEN / WINDOW_CENTER / WINDOW_UNIT / WINDOW_SIZE / WINDOW_NOT_REGISTERED / UNIT_MISMATCH

规范化 canonicalization_version = g3-json-c14n-v1（ADR §2）：只消除**表示差异**，不做代数重写——
不排序可交换算子参数、不做 Lt→Gt、不消 Ref(lag=0)、不折叠常量。键按码点排序；数值按**精确十进制**规范：
文本层 parse_json_text 用 Decimal 解码（不经 binary64），1 / 1.0 / 1e0 → 1，-0 → 0，去无效前导/尾随零、展开指数、
最短无指数十进制；数值 token > 128 字符或 |指数| > 308 拒收（WIDTH）；转 Float64 非零下溢为 0 或溢出 → NON_FINITE_CONST 拒收，
不静默改变语义。dict 入口的 Python float 用 repr（最短 round-trip）进入同一 Decimal 规范化。输出 UTF-8 无空白。
canonical_hash = sha256(canonical_json_bytes)。
算子版本不进 hash：语义身份 = (canonical_hash, canonicalization_version, registry_digest, op_versions)。
本 DSL Ref(x, 0) = 当前闭合值。
"""
from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field

from quant_lab.research.ops import CROSS_ASSET_OPS, FIELD_UNITS, REGISTRY, OpSpec, is_quarantined, registry_digest

CANONICALIZATION_VERSION = "g3-json-c14n-v1"
MAX_DEPTH = 4
MAX_NODES = 15
MAX_TEXT_BYTES = 64 * 1024
MAX_NUM_TOKEN = 128
MAX_EXPONENT = 308
WINDOW_UNITS = {"wallclock": "minutes", "rows": "count"}
_NODE_KEYS = {"field", "const", "op", "args", "window", "params"}


class ASTRejected(ValueError):
    def __init__(self, code: str, detail: str = "", path: str = "$"):
        self.code, self.detail, self.path = code, detail, path
        super().__init__(f"{code} at {path}: {detail}" if detail else f"{code} at {path}")


@dataclass(frozen=True)
class LintReport:
    depth: int
    n_nodes: int
    unit: str
    fields: tuple[str, ...]
    ops: tuple[str, ...]
    windows: tuple[tuple[str, int], ...]
    lookback_rows: int                # rows 窗与 lag 累计的最大计划槽位回看
    lookback_minutes: int             # wallclock 窗累计的最大回看分钟（两者相加换算槽位后为总地平线）
    op_versions: dict = field(default_factory=dict)
    canonicalization_version: str = CANONICALIZATION_VERSION
    registry_digest: str = ""


# ---------------------------------------------------------------- 文本传输层
def _reject_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ASTRejected("UNKNOWN_KEY", f"重复对象键 {k!r}")
        d[k] = v
    return d


def _reject_constant(name):
    raise ASTRejected("NON_FINITE_CONST", name)


def _parse_number_token(tok: str):
    """数值 token → 精确 Decimal（整数保持 int）；token 长度与指数绝对值受限（资源门）。"""
    if len(tok) > MAX_NUM_TOKEN:
        raise ASTRejected("WIDTH", f"数值 token {len(tok)} 字符 > {MAX_NUM_TOKEN}")
    try:
        d = Decimal(tok)
    except InvalidOperation:
        raise ASTRejected("CONST_TYPE", tok) from None
    if abs(d.adjusted()) > MAX_EXPONENT:
        raise ASTRejected("WIDTH", f"指数 |{d.adjusted()}| > {MAX_EXPONENT}")
    return d


def parse_json_text(text: str | bytes, *, max_bytes: int = MAX_TEXT_BYTES) -> dict:
    """文本 → dict：拒绝重复键、NaN/Infinity、尾随内容、非对象根、超 64 KiB；数值以 Decimal 精确解码（不经 binary64）。"""
    raw = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    if len(raw) > max_bytes:
        raise ASTRejected("WIDTH", f"传输体 {len(raw)} B > {max_bytes} B")
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_dupes, parse_constant=_reject_constant,
                         parse_float=_parse_number_token, parse_int=_parse_number_token)
    except ASTRejected:
        raise
    except (ValueError, UnicodeDecodeError) as e:
        raise ASTRejected("NOT_A_DICT", f"JSON 解析失败: {e}") from None
    if not isinstance(obj, dict):
        raise ASTRejected("NOT_A_DICT", f"根为 {type(obj).__name__}")
    return obj


# ---------------------------------------------------------------- 单位代数
_SCALAR = "scalar"


def _unify(a: str, b: str, path: str) -> str:
    if a == _SCALAR:
        return b
    if b == _SCALAR:
        return a
    if a != b:
        raise ASTRejected("UNIT_MISMATCH", f"{a} vs {b}", path)
    return a


def _output_unit(spec: OpSpec, arg_units: list[str], path: str) -> str:
    for i, (rule, u) in enumerate(zip(spec.input_units, arg_units)):
        if rule == "bool" and u != "bool":
            raise ASTRejected("UNIT_MISMATCH", f"{spec.name} 参数 {i} 需要 bool，得到 {u}", path)
        if rule in ("same", "any") and u == "bool":
            raise ASTRejected("UNIT_MISMATCH", f"{spec.name} 参数 {i} 不接受 bool", path)
    if spec.output_unit == "same":
        u = arg_units[0]
        for v in arg_units[1:]:
            u = _unify(u, v, path)
        return u
    if spec.output_unit == "bool":
        if spec.arity == 2 and spec.input_units[0] == "same":
            _unify(arg_units[0], arg_units[1], path)
        return "bool"
    if spec.output_unit == "derived":
        a, b = arg_units
        if spec.name == "SafeDiv" and a == b and a != _SCALAR:
            return "ratio"
        if a == _SCALAR:
            return b
        if b == _SCALAR:
            return a
        return "derived"
    return spec.output_unit  # log / ratio / rank


# ---------------------------------------------------------------- lint
def _to_decimal(v, path: str) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
        raise ASTRejected("CONST_TYPE", repr(v), path)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ASTRejected("NON_FINITE_CONST", repr(v), path)
        return Decimal(repr(v))          # 最短 round-trip 十进制
    d = Decimal(v)
    if not d.is_finite():
        raise ASTRejected("NON_FINITE_CONST", repr(v), path)
    return d


def _check_const(v, path: str) -> float:
    d = _to_decimal(v, path)
    f = float(d)
    if not math.isfinite(f):
        raise ASTRejected("NON_FINITE_CONST", f"Float64 溢出: {v!r}", path)
    if f == 0.0 and d != 0:
        raise ASTRejected("NON_FINITE_CONST", f"Float64 非零下溢为 0: {v!r}", path)
    return f


def _check_window(spec: OpSpec, node: dict, path: str, allowed) -> tuple[str, int] | None:
    win = node.get("window")
    if not spec.windowed:
        if win is not None:
            raise ASTRejected("WINDOW_FORBIDDEN", spec.name, path)
        return None
    if win is None:
        raise ASTRejected("WINDOW_MISSING", spec.name, path)
    if not isinstance(win, dict):
        raise ASTRejected("WINDOW_UNIT", "window 必须是对象", path)
    if "center" in win:
        raise ASTRejected("WINDOW_CENTER", "不开放 center 窗", path)
    unit = win.get("unit")
    if unit not in WINDOW_UNITS:
        raise ASTRejected("WINDOW_UNIT", repr(unit), path)
    size_key = WINDOW_UNITS[unit]
    extra = set(win) - {"unit", size_key}
    if extra:
        raise ASTRejected("UNKNOWN_KEY", f"window 键 {sorted(extra)}", path)
    accepted = (spec.window_semantics, *spec.alt_windows)
    if unit not in accepted:
        raise ASTRejected("WINDOW_UNIT", f"{spec.name} 只接受 {accepted} 窗", path)
    size = win.get(size_key)
    if isinstance(size, Decimal) and size == size.to_integral_value():
        size = int(size)
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ASTRejected("WINDOW_SIZE", f"{size_key}={size!r}", path)
    if unit == "rows" and size < spec.min_samples:
        raise ASTRejected("WINDOW_SIZE", f"{spec.name} rows 窗 {size} < min_samples {spec.min_samples}", path)
    if allowed is not None and (unit, size) not in allowed:
        raise ASTRejected("WINDOW_NOT_REGISTERED", f"({unit}, {size})", path)
    return unit, size


def _check_params(spec: OpSpec, node: dict, path: str) -> dict:
    params = node.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ASTRejected("PARAM", "params 必须是对象", path)
    extra = set(params) - set(spec.params)
    if extra:
        raise ASTRejected("UNKNOWN_KEY", f"params 键 {sorted(extra)}", path)
    missing = set(spec.params) - set(params)
    if missing:
        raise ASTRejected("PARAM", f"缺 {sorted(missing)}", path)
    out = {}
    for k, rule in spec.params.items():
        v = params[k]
        if isinstance(v, Decimal) and v == v.to_integral_value():
            v = int(v)
        if rule["type"] == "int" and (isinstance(v, bool) or not isinstance(v, int)):
            raise ASTRejected("PARAM", f"{k}={v!r} 需为 int", path)
        lo = rule.get("min", 0)
        if v < lo:
            code = "NEGATIVE_REF" if spec.name == "Ref" and v < 0 else "PARAM"
            raise ASTRejected(code, f"{k}={v} < {lo}", path)
        out[k] = v
    return out


class _Walk:
    def __init__(self, allowed_windows):
        self.n = 0
        self.fields: set[str] = set()
        self.ops: set[str] = set()
        self.windows: set[tuple[str, int]] = set()
        self.allowed = None if allowed_windows is None else {tuple(w) for w in allowed_windows}

    def visit(self, node, path: str) -> tuple[int, str, int, int]:
        """返回 (depth, unit, lookback_rows, lookback_minutes)；深度：叶 = 1。"""
        self.n += 1
        if self.n > MAX_NODES:
            raise ASTRejected("WIDTH", f"节点 > {MAX_NODES}", path)
        if not isinstance(node, dict):
            raise ASTRejected("NOT_A_DICT", type(node).__name__, path)
        keys = set(node)
        if not keys:
            raise ASTRejected("EMPTY", "", path)
        unknown = keys - _NODE_KEYS
        if unknown:
            raise ASTRejected("UNKNOWN_KEY", str(sorted(unknown)), path)
        if "field" in keys:
            if keys != {"field"}:
                raise ASTRejected("UNKNOWN_KEY", "field 节点只允许 field 键", path)
            f = node["field"]
            if not isinstance(f, str) or f not in FIELD_UNITS:
                raise ASTRejected("UNKNOWN_FIELD", repr(f), path)
            self.fields.add(f)
            return 1, FIELD_UNITS[f], 0, 0
        if "const" in keys:
            if keys != {"const"}:
                raise ASTRejected("UNKNOWN_KEY", "const 节点只允许 const 键", path)
            _check_const(node["const"], path)
            return 1, _SCALAR, 0, 0
        if "op" not in keys:
            raise ASTRejected("UNKNOWN_KEY", "节点须为 field / const / op 之一", path)
        name = node["op"]
        if not isinstance(name, str):
            raise ASTRejected("UNKNOWN_OP", repr(name), path)
        if name in CROSS_ASSET_OPS:
            raise ASTRejected("CROSS_ASSET", name, path)
        spec = REGISTRY.get(name)
        if spec is None:
            raise ASTRejected("UNKNOWN_OP", name, path)
        if is_quarantined(name):
            raise ASTRejected("OP_QUARANTINED", name, path)
        args = node.get("args")
        if not isinstance(args, list):
            raise ASTRejected("ARITY", "args 须为列表", path)
        if len(args) != spec.arity:
            raise ASTRejected("ARITY", f"{name} 需要 {spec.arity} 个参数，得到 {len(args)}", path)
        params = _check_params(spec, node, path)
        win = _check_window(spec, node, path, self.allowed)
        self.ops.add(name)
        depths, units, lb_rows, lb_min = [], [], [], []
        for i, a in enumerate(args):
            d, u, r, m = self.visit(a, f"{path}.args[{i}]")
            depths.append(d); units.append(u); lb_rows.append(r); lb_min.append(m)
        depth = 1 + max(depths)
        if depth > MAX_DEPTH:
            raise ASTRejected("DEPTH", f"深度 {depth} > {MAX_DEPTH}", path)
        unit = _output_unit(spec, units, path)
        own_rows, own_min = 0, 0
        if win is not None:
            self.windows.add(win)
            if win[0] == "rows":
                own_rows = win[1] - 1
            else:
                own_min = win[1]
        if "lag" in params:
            own_rows = params["lag"]
        return depth, unit, max(lb_rows) + own_rows, max(lb_min) + own_min


def lint(ast: dict, *, allowed_windows=None) -> LintReport:
    """硬门检查；通过返回 LintReport，否则抛 ASTRejected（code 见模块 docstring）。"""
    w = _Walk(allowed_windows)
    depth, unit, lb_rows, lb_min = w.visit(ast, "$")
    if "const" in ast:
        raise ASTRejected("EMPTY", "常量根无信息", "$")
    return LintReport(
        depth=depth, n_nodes=w.n, unit=unit, fields=tuple(sorted(w.fields)), ops=tuple(sorted(w.ops)),
        windows=tuple(sorted(w.windows)), lookback_rows=lb_rows, lookback_minutes=lb_min,
        op_versions={o: REGISTRY[o].version for o in sorted(w.ops)}, registry_digest=registry_digest(),
    )


# ---------------------------------------------------------------- 规范化
class _Num(float):
    """携带精确十进制字符串的数值：json.dumps 时按 canonical 串输出（不经 binary64 重排）。"""
    __slots__ = ("text",)

    def __new__(cls, text: str):
        o = super().__new__(cls, float(Decimal(text)))
        o.text = text
        return o

    def __repr__(self):
        return self.text


def canonical_number_text(v) -> str:
    """精确十进制规范串：1/1.0/1e0 → "1"；-0 → "0"；去无效零、展开指数、最短无指数十进制。"""
    d = _to_decimal(v, "$")
    if d == 0:
        return "0"
    # 无损展开：直接用 as_tuple 的数字/指数，手工去尾零（不用 normalize()，它受调用方 Decimal 上下文精度/舍入影响）
    sign_bit, digs, exp = d.as_tuple()
    digits = "".join(str(x) for x in digs).lstrip("0") or "0"
    while len(digits) > 1 and digits.endswith("0"):
        digits = digits[:-1]; exp += 1
    sign = "-" if sign_bit else ""
    if exp >= 0:
        return sign + digits + "0" * exp
    if len(digits) > -exp:
        return sign + digits[:exp] + "." + digits[exp:]
    return sign + "0." + "0" * (-exp - len(digits)) + digits


def _norm_number(v):
    return _Num(canonical_number_text(v))


def _canon(node: dict) -> dict:
    if "field" in node:
        return {"field": node["field"]}
    if "const" in node:
        return {"const": _norm_number(node["const"])}
    out = {"args": [_canon(a) for a in node["args"]], "op": node["op"]}
    win = node.get("window")
    if win is not None:
        out["window"] = {k: (int(win[k]) if isinstance(win[k], (int, Decimal)) and not isinstance(win[k], bool) else win[k]) for k in sorted(win)}
    params = node.get("params")
    if params:
        out["params"] = {k: int(params[k]) for k in sorted(params)}
    return out


def _dumps(obj) -> str:
    """自写规范 JSON 序列化：键按码点排序、无空白、字符串标准转义、数值用精确十进制规范串（json.dumps 会忽略 float 子类 repr）。"""
    if isinstance(obj, dict):
        return "{" + ",".join(json.dumps(str(k), ensure_ascii=True) + ":" + _dumps(obj[k]) for k in sorted(obj)) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(_dumps(x) for x in obj) + "]"
    if isinstance(obj, _Num):
        return obj.text
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, (float, Decimal)):
        return canonical_number_text(obj)
    if obj is None:
        return "null"
    return json.dumps(str(obj), ensure_ascii=True)


def canonicalize(ast: dict, *, allowed_windows=None) -> dict:
    """lint 通过后返回规范化 AST（新对象，不改入参）。"""
    lint(ast, allowed_windows=allowed_windows)
    return _canon(ast)


def canonical_json(ast: dict, *, allowed_windows=None) -> str:
    return _dumps(canonicalize(ast, allowed_windows=allowed_windows))


def canonical_hash(ast: dict, *, allowed_windows=None) -> str:
    """sha256(规范化 JSON UTF-8 字节)，64 位小写十六进制。"""
    return hashlib.sha256(canonical_json(ast, allowed_windows=allowed_windows).encode("utf-8")).hexdigest()


def is_equivalent(a: dict, b: dict) -> bool:
    return canonical_hash(a) == canonical_hash(b)


__all__ = ["ASTRejected", "CANONICALIZATION_VERSION", "LintReport", "MAX_DEPTH", "MAX_EXPONENT", "MAX_NODES", "MAX_NUM_TOKEN", "MAX_TEXT_BYTES", "canonical_number_text",
           "canonical_hash", "canonical_json", "canonicalize", "is_equivalent", "lint", "parse_json_text"]
