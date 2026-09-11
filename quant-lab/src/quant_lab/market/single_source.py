"""A24 静态门：调用方集合严格登记、全树禁止已知内联重写。

由 tests/market/test_single_source.py 调用；检查实现位于生产树，以支持删除真实门的源码突变。
FOREIGN_HITS 仅冻结 G3 既有两处，不授予修改 research/ 的权限。
AST 禁令针对已知语法族，不声称能识别任意语义等价的混淆写法。
"""
from __future__ import annotations

import ast
from collections import Counter
import pathlib


SRC = pathlib.Path(__file__).resolve().parents[1]
MARKET = SRC / "market"


# ---------------------------------------------------------------------------
# 机制 1：单一来源调用点登记
# ---------------------------------------------------------------------------
#
# 键 = 单一来源函数名；值 = 允许（且**必须**）调用它的位置，格式 "<相对 src 的模块路径>:<限定名>"。
# 清单与实际集合必须**相等**：多一个、少一个都红。
ALLOWED_CALLERS: dict[str, set[str]] = {
    # G3 尚未接入：跨窗口实际调用集合为空，不预登记虚构调用方。
    "force_close_net_R": set(),
    # 启动时刻的唯一表达（S20/S27/S30 同族；build_request 曾自写 latency 加法，A24 后归一）
    "derived_t_start": {
        "market/contract.py:ExecutionRequest._chk",
        "market/contract.py:ExecutionRequest.resolved_t_start",
        "market/contract.py:build_request",
    },
    # 观察窗长度的唯一表达（S24/S25 同族）
    "derived_window_s": {
        "market/contract.py:ExecutionRequest._chk",
        "market/contract.py:build_request",
    },
    # 入场单到期时刻的唯一表达（A/B 两内核曾各写一份，A24 抽取）
    "entry_expiry_at": {
        "market/kernel_a.py:KernelA.timeline",
        "market/kernel_a.py:KernelA.submit_entries",
        "market/nautilus_adapter.py:_simulate_b.PlanShell._submit_entries",
    },
    # 网格首点 / 网格计数的唯一表达（S28/S29 同族；vision.expected_rows 曾自写整除，A24 后归一）
    "first_grid_point": {
        "market/kernel_a.py:KernelA._first_bar_gap",
        "market/partition_check.py:check_bars",
    },
    "resolve_entry_ttl_s": {
        "market/contract.py:ExecutionRequest._resolve_ttl",
        "market/contract.py:ExecutionRequest._chk",
        "market/contract.py:build_request",
    },
    "grid_points_between": {
        "market/kernel_a.py:KernelA._first_bar_gap",
        "market/execution.py:load_market_from_lake.bars",
        "market/partition_check.py:check_bars",
        "market/vision.py:expected_rows",
    },
    # B17 批量冲突门（S31）：必须由 simulate_batch 调用，绕过即红
    "check_policy_hash_consistency": {
        "market/execution.py:simulate_batch",
    },
}


def _call_name(fn):
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return False


class _CallSites(ast.NodeVisitor):
    def __init__(self, module: str, targets: set[str]) -> None:
        self.module, self.targets, self.stack = module, targets, []
        self.found: dict[str, set[str]] = {t: set() for t in targets}
        self.counts = {t: Counter() for t in targets}

    def _scoped(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def visit_Call(self, node):
        name = _call_name(node.func)
        if name in self.targets:
            where = f"{self.module}:{'.'.join(self.stack) or '<module>'}"
            self.found[name].add(where)
            self.counts[name][where] += 1
        self.generic_visit(node)


def collect_call_sites(root: pathlib.Path, targets: set[str]) -> dict[str, set[str]]:
    """真实的门：AST 遍历 root 下全部 .py，返回 targets 各自的实际调用方集合。

    定义处本身不算调用方（只有 Call 节点计入），因此 `derived_t_start` 的函数体不会自计。
    """
    out: dict[str, set[str]] = {t: set() for t in targets}
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        visitor = _CallSites(path.relative_to(root.parent).as_posix().removeprefix("quant_lab/"), targets)
        visitor.visit(ast.parse(text))
        for key, hits in visitor.found.items():
            out[key] |= hits
    return out


def collect_call_counts(root: pathlib.Path, targets: set[str]) -> dict[str, dict[str, int]]:
    """固定每个函数内的使用次数；同函数保留另一个调用不能掩盖删除。"""
    out = {t: Counter() for t in targets}
    for path in sorted(root.rglob("*.py")):
        visitor = _CallSites(path.relative_to(root.parent).as_posix().removeprefix("quant_lab/"), targets)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        for key, counts in visitor.counts.items():
            out[key].update(counts)
    return {key: dict(counts) for key, counts in out.items()}


def diff_call_sites(declared: dict[str, set[str]], actual: dict[str, set[str]]) -> list[str]:
    """返回人可读的偏差说明；空列表 = 登记与实际相等。"""
    problems: list[str] = []
    for fn, expect in declared.items():
        got = actual.get(fn, set())
        extra, missing = sorted(got - expect), sorted(expect - got)
        if extra:
            problems.append(f"{fn}：出现**未登记**的调用方 {extra}——新调用方必须登记，否则单一来源的使用面不可知")
        if missing:
            problems.append(
                f"{fn}：登记的调用方 {missing} **不再调用它**——这通常意味着该处改回了自己写一份"
                f"（S29 的形态），而不是调用方被正当删除；若确实是正当删除，请同步改 ALLOWED_CALLERS"
            )
    return problems


# ---------------------------------------------------------------------------
# 机制 2：树级负向模式禁令
# ---------------------------------------------------------------------------
#
# 每条禁令 = (代号, 说明, 允许出现的位置集合)。允许位置只含该单一来源**自身的定义处**。
# 非 market/ 的命中按窗口所有权冻结登记（G3 拥有 research/），新增同样会红，但由 G2 转报而非直接改。
FORBIDDEN_HOMES: dict[str, set[str]] = {
    "P8_inline_force_close": {"market/contract.py:force_close_net_R"},
    "P6_inline_grid_comparison": set(),
    "P7_inline_ttl_resolution": {"market/contract.py:resolve_entry_ttl_s"},
    "P1_inline_latency": {"market/contract.py:derived_t_start"},
    "P2_int_total_seconds": set(),
    "P3_duration_div": {"market/contract.py:_us"},
    "P4_t_start_or_t_dec": set(),
    "P5_inline_entry_ttl": {"market/contract.py:derived_window_s", "market/contract.py:entry_expiry_at"},
}
# 非 G2 所有权的既有命中（冻结；新增会红，由 G2 转报 G3 而不是直接改 research/）
FOREIGN_HITS: set[tuple[str, str]] = {
    ("P5_inline_entry_ttl", "research/api.py:build_inputs_from_synthetic"),
    ("P3_duration_div", "research/maxt.py:calendar_blocks"),
}
PATTERN_WHY = {
    "P8_inline_force_close": "余仓 mark 减 entry_avg_price 估值只能由 force_close_net_R 表达",
    "P6_inline_grid_comparison": "中尾网格不得用相邻时间差/步长比较重写",
    "P7_inline_ttl_resolution": "TTL 计划/政策选择必须经 resolve_entry_ttl_s",
    "P1_inline_latency": "启动时刻 = t_dec + latency_s 只能由 derived_t_start 表达（S20/S27/S30 族）",
    "P2_int_total_seconds": "int(总秒数) 会把亚秒静默截断，使 +1µs 绕过边界（S25/S28 族）",
    "P3_duration_div": "时长除法/整除是自己再实现一遍网格数学，必须走 grid_points_between/first_grid_point（S28/S29 族）",
    "P4_t_start_or_t_dec": "(x.t_start or x.t_dec) 让省略与显式同值走两条不同边界（S30）",
    "P5_inline_entry_ttl": "入场有效期加法必须走 entry_expiry_at / derived_window_s（S19/S24 族）",
}


class _Lint(ast.NodeVisitor):
    def __init__(self, module: str, text: str) -> None:
        self.module, self.text, self.stack = module, text, []
        self.hits: list[tuple[str, int, str, str]] = []
        self.local_defs = {}

    def _scoped(self, node):
        self.stack.append(node.name)
        previous = self.local_defs
        self.local_defs = {}
        self.generic_visit(node)
        self.local_defs = previous
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def _where(self) -> str:
        return f"{self.module}:{'.'.join(self.stack) or '<module>'}"

    def _seg(self, node) -> str:
        return ast.get_source_segment(self.text, node) or ""

    def _hit(self, pattern: str, node) -> None:
        self.hits.append((pattern, node.lineno, self._where(), self._seg(node)[:120]))

    def visit_BoolOp(self, node):
        if isinstance(node.op, ast.Or):
            attrs = [v.attr for v in node.values if isinstance(v, ast.Attribute)]
            if "t_start" in attrs and "t_dec" in attrs:
                self._hit("P4_t_start_or_t_dec", node)
        self.generic_visit(node)

    def visit_Call(self, node):
        name = _call_name(node.func)
        if name == "timedelta":
            for kw in node.keywords:
                if kw.arg == "seconds" and "latency_s" in self._seg(kw.value):
                    self._hit("P1_inline_latency", node)
        if name == "int" and node.args and "total_seconds()" in self._seg(node.args[0]):
            self._hit("P2_int_total_seconds", node)
        self.generic_visit(node)

    def visit_Compare(self, node):
        operands = [node.left, *node.comparators]
        step_names = {"iv", "step", "interval", "interval_s"}
        for value in operands:
            if isinstance(value, ast.Name):
                value = self.local_defs.get(value.id, value)
            if not isinstance(value, ast.BinOp) or not isinstance(value.op, (ast.Add, ast.Sub)):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if names & step_names:
                self._hit("P6_inline_grid_comparison", node)
                break
        self.generic_visit(node)

    def _assignment(self, node):
        value = node.value
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        else:
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                self.local_defs.pop(target.id, None)
                if isinstance(value, ast.BinOp) and isinstance(value.op, (ast.Add, ast.Sub)):
                    self.local_defs[target.id] = value
        self._ttl_value(node, value)
        self.generic_visit(node)

    visit_Assign = visit_AnnAssign = visit_NamedExpr = _assignment

    def _ttl_value(self, node, value):
        if value is None:
            return
        values = [value]
        if isinstance(value, ast.IfExp):
            values = [value.body, value.orelse]
        reads = [n for branch in values for n in ast.walk(branch)
                 if isinstance(n, ast.Attribute) and n.attr == "entry_ttl_s"]
        if reads and not isinstance(value, ast.Call):
            self._hit("P7_inline_ttl_resolution", node)

    def visit_AugAssign(self, node):
        self._ttl_value(node, node.value)
        self._binary_patterns(node)
        if isinstance(node.target, ast.Name):
            self.local_defs.pop(node.target.id, None)
            if isinstance(node.op, (ast.Add, ast.Sub)):
                self.local_defs[node.target.id] = ast.BinOp(left=node.target, op=node.op, right=node.value)
        self.generic_visit(node)

    def visit_Return(self, node):
        if isinstance(node.value, ast.Attribute):
            self._ttl_value(node, node.value)
        self.generic_visit(node)

    def visit_IfExp(self, node):
        self._ttl_value(node, node)
        self.generic_visit(node)

    def visit_BinOp(self, node):
        self._binary_patterns(node)
        self.generic_visit(node)

    def _binary_patterns(self, node):
        seg = self._seg(node)
        if isinstance(node.op, ast.Sub) and isinstance(node, ast.BinOp):
            left, right = node.left, node.right
            if (_call_name(left) == "mark" and isinstance(right, ast.Attribute)
                    and right.attr == "entry_avg_price"):
                self._hit("P8_inline_force_close", node)
        if isinstance(node.op, (ast.FloorDiv, ast.Div)) and (
            "total_seconds()" in seg or "interval_s" in seg or "timedelta" in seg
        ):
            self._hit("P3_duration_div", node)
        if isinstance(node.op, ast.Add) and "entry_ttl_s" in seg:
            self._hit("P5_inline_entry_ttl", node)


def lint_tree(root: pathlib.Path) -> list[tuple[str, int, str, str]]:
    """真实的门：返回 root 下全部 .py 的模式命中。"""
    out: list[tuple[str, int, str, str]] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        module = path.relative_to(root.parent).as_posix().removeprefix("quant_lab/")
        lint = _Lint(module, text)
        lint.visit(ast.parse(text))
        out.extend(lint.hits)
    return out


def violations(hits) -> list[tuple[str, int, str, str]]:
    """扣除各模式自身定义处与冻结的外窗命中后，剩下的即违规。"""
    bad = []
    for pattern, lineno, where, code in hits:
        if where in FORBIDDEN_HOMES.get(pattern, set()):
            continue
        if (pattern, where) in FOREIGN_HITS:
            continue
        bad.append((pattern, lineno, where, code))
    return bad

# A24 固定使用次数；由审查确认后更新，禁止运行时从树生成期望。
ALLOWED_CALL_COUNTS = {"force_close_net_R": {},  # G3 尚未接入；夹具不是实际调用方。
 'check_policy_hash_consistency': {'market/execution.py:simulate_batch': 1},
 'derived_t_start': {'market/contract.py:ExecutionRequest._chk': 1,
                     'market/contract.py:ExecutionRequest.resolved_t_start': 1,
                     'market/contract.py:build_request': 1},
 'derived_window_s': {'market/contract.py:ExecutionRequest._chk': 1,
                      'market/contract.py:build_request': 1},
 'entry_expiry_at': {'market/kernel_a.py:KernelA.submit_entries': 1,
                     'market/kernel_a.py:KernelA.timeline': 1,
                     'market/nautilus_adapter.py:_simulate_b.PlanShell._submit_entries': 1},
 'first_grid_point': {'market/kernel_a.py:KernelA._first_bar_gap': 3,
                      'market/partition_check.py:check_bars': 1},
 'grid_points_between': {'market/execution.py:load_market_from_lake.bars': 1,
                         'market/kernel_a.py:KernelA._first_bar_gap': 2,
                         'market/partition_check.py:check_bars': 7,
                         'market/vision.py:expected_rows': 1},
 'resolve_entry_ttl_s': {'market/contract.py:ExecutionRequest._chk': 1,
                         'market/contract.py:ExecutionRequest._resolve_ttl': 1,
                         'market/contract.py:build_request': 1}}
