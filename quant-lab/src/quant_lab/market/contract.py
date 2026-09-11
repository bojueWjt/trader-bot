"""quant_lab.market.contract —— 执行合同 schema（契约 §3 + M-01 修订 R5–R10 + ADR-G2 §3/§7/§11，M-06）。

只做：pydantic 模型、单位/枚举校验、政策解析（版本→不可变内容+哈希）、规范 JSON 与 trace_hash、
结果不变量断言、夹具装载与结果 diff。**不做**撮合判定，不产生金标。

事件约定（候选 A/B 与夹具共同遵守，见 ADR-G2 §5.1 P0–P8）：
- t_start = t_dec + policy.latency_s；入场腿在 t_start 发 submitted/accepted(/working)。
- 首次 entry fill 后同 ts 建保护腿 sl-0（conditional，qty=仓位）与 tp-i（reduce-only limit，qty=floor_step(cum_entry×fraction)）；
  后续 entry fill → amended。
- SL 只按 mark 触发：stop_triggered(trigger_basis=mark, price=mark)，同 ts 取消未成交 entry 与 TP，成交在下一合法 last 价点
  （filled, leg=sl, trigger_basis=last）。TP 只按 last：tp_triggered(trigger_basis=last) → filled/partial_fill（价格取限价与 last 的更优者）。
- funding：kind=funding, leg=funding, trigger_basis=funding, price=结算 mark, qty=结算前有符号仓位, cash_delta=-qty×price×rate（C05 扩展列）。
- 仓位归零：剩余兄弟腿 cancelled → closed（每 bracket 一次）。未成交到期：expired → closed(reason=no_fill)。
- 观察窗结束仍有仓：无 closed，censor_reason=LABEL_RIGHT_CENSORED，net_pnl/net_R=null。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
import hashlib
import json
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant_lab.market import EXECUTION_CONTRACT_VERSION

Side = Literal["long", "short"]
EntryKind = Literal["limit", "market_ref", "ladder"]
TIF = Literal["GTC", "GTD", "IOC"]
CostScenario = Literal["base", "stress"]
PathScenario = Literal["primary", "adverse", "favorable"]
EventKind = Literal["submitted", "accepted", "rejected", "working", "partial_fill", "filled", "cancelled", "expired",
                    "stop_triggered", "tp_triggered", "funding", "closed", "amended"]
Leg = Literal["entry", "sl", "tp", "close", "funding"]
TriggerBasis = Literal["mark", "last", "funding", "expiry", "none"]
PathStep = Literal["O", "H", "L", "C", "none"]
FillStatus = Literal["none", "partial", "filled"]
# A15（research-schema §9.10.11）：reconstructed_outcome.kind 七值；映射表见 docs/adr/report-G2-outcome-kind-mapping.md
OUTCOME_KINDS = ("filled_closed", "unfilled_expired", "stopped", "tp_hit", "right_censored", "unevaluable", "rejected")
EVIDENCE_CENSORS = ("MARK_STALE", "BAR_GAP", "FUNDING_SCHEDULE_GAP", "RULE_HISTORY_MISSING", "SYMBOL_TIME_INVALID")
EXIT_LEG_ORDER = ("sl", "tp", "close")
CENSOR_REASONS = ("LABEL_RIGHT_CENSORED", "MARK_STALE", "BAR_GAP", "FUNDING_SCHEDULE_GAP", "RULE_HISTORY_MISSING", "SYMBOL_TIME_INVALID")
CENSOR_PRIORITY = ("SYMBOL_TIME_INVALID", "RULE_HISTORY_MISSING", "BAR_GAP", "MARK_STALE", "FUNDING_SCHEDULE_GAP", "LABEL_RIGHT_CENSORED")
TERMINAL_KINDS = ("filled", "cancelled", "expired", "rejected")
FILL_KINDS = ("filled", "partial_fill")
SETTLEMENT_QUANTUM = Decimal("0.00000001")
DF_DECIMAL = (38, 12)


# 可机读的支持边界；不得把当前合成/active-silver 路径宣称为下列能力。
P2_UNSUPPORTED = {
    "S02": {"status": "unsupported", "capability": "real_settlement_time_U03_and_engine_balance", "owner": "G0 settlement contract + G2 P2"},
    "S06": {"status": "unsupported", "capability": "bronze_depth_replay_and_multi_source_versions", "owner": "G1 lake + G2 P2"},
    "S07": {"status": "unsupported", "capability": "management_commands_E13_C01_and_reservation_lifecycle", "owner": "G0 C01 + G2 P2"},
    "S12": {"status": "unsupported", "capability": "versioned_lake_snapshot_resolution", "owner": "G1 lake + G0 identity contract"},
}


class ContractError(Exception):  # 不继承 ValueError：避免被 pydantic 包成 ValidationError
    pass


class ExecutionInvariantError(AssertionError):
    """运行时不变量失败：停止该批研究，不伪装成市场删失（ADR §7）。"""


def _utc(v: dt.datetime) -> dt.datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ContractError("时间必须 tz-aware UTC")
    return v.astimezone(dt.UTC)


def D(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def check_decimal(d: Decimal, name: str, *, positive: bool = True) -> None:
    """Decimal(38,12) 合同边界（S13）：有限、（正）、小数位 ≤ 12、总位数 ≤ 38。"""
    if not d.is_finite() or (positive and d <= 0):
        raise ContractError(f"{name} 必须有限{'且 > 0' if positive else ''}：{d}")
    tup = d.normalize().as_tuple()
    if tup.exponent < -12 or len(tup.digits) > 38 or (len(tup.digits) + max(0, tup.exponent)) > 26:   # 整数位 ≤ 38−12
        raise ContractError(f"{name} 超出 Decimal(38,12)：{d}")


def floor_step(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return qty
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def quantize_money(x: Decimal) -> Decimal:
    return x.quantize(SETTLEMENT_QUANTUM, rounding=ROUND_HALF_EVEN)


RATIO_QUANTUM = Decimal("0.000000000001")   # net_R / mae_R / mfe_R / 均价：12 位小数（DF Decimal(38,12)）


def quantize_ratio(x: Decimal) -> Decimal:
    return x.quantize(RATIO_QUANTUM, rounding=ROUND_HALF_EVEN)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# order_plan（M-01 §3.1）
# ---------------------------------------------------------------------------
class Entry(_Model):
    kind: EntryKind
    price_lo: Decimal
    price_hi: Decimal
    fraction: Decimal | None = None          # §5.10 B8：原文未给则 None（不得默认 1），由 policy 等分兜底
    tif: TIF = "GTC"
    post_only: bool = False

    @model_validator(mode="after")
    def _chk(self):
        if self.price_lo <= 0 or self.price_hi <= 0 or self.price_lo > self.price_hi:
            raise ContractError("entry 价格需 >0 且 price_lo <= price_hi")
        if self.kind == "limit" and self.price_lo != self.price_hi:
            raise ContractError("limit 要求 price_lo == price_hi（区间用 ladder）")
        if self.fraction is not None and self.fraction <= 0:
            raise ContractError("entry.fraction 必须 > 0")
        return self


class Stop(_Model):
    price: Decimal
    trigger: Literal["mark"] = "mark"


class TakeProfit(_Model):
    level: Decimal
    fraction: Decimal | None = None          # §5.10 B8：可空


class Sizing(_Model):
    mode: Literal["fixed_qty", "risk_budget"]
    qty: Decimal | None = None

    @model_validator(mode="after")
    def _chk(self):
        if self.mode == "fixed_qty":
            if self.qty is None:
                raise ContractError("fixed_qty 需要 qty")
            check_decimal(self.qty, "sizing.qty")
        return self


class Expiry(_Model):
    entry_ttl_s: int | None = Field(default=None, gt=0)      # §5.9 B5：原文未给则 None，由 policy.entry_ttl_s 兜底（G1 不得猜值）
    max_holding_s: int | None = Field(default=None, gt=0)


INSTRUMENT_ID_RE = r"^[A-Z0-9]+-PERP\.[A-Z]+-[A-Z]+$"   # 契约 §5.2 裁定 B1：BTCUSDT-PERP.BINANCE-UM


class OrderPlan(_Model):
    instrument_id: str = Field(pattern=INSTRUMENT_ID_RE)
    side: Side
    entries: list[Entry]
    stop: Stop
    tps: list[TakeProfit] = []
    sizing: Sizing
    expiry: Expiry
    reduce_only_exit: Literal[True] = True     # v0 只支持 reduce-only 出场（S13）

    @model_validator(mode="after")
    def _chk(self):
        if not self.entries:
            raise ContractError("entries 不能为空")
        for d in (self.stop.price, *(t.level for t in self.tps), *(e.price_lo for e in self.entries), *(e.price_hi for e in self.entries)):
            check_decimal(d, "price")
        # §5.10 B8 第 3 条：同一列表全有或全无
        ef = [e.fraction for e in self.entries]
        tf = [t.fraction for t in self.tps]
        if any(f is None for f in ef) and any(f is not None for f in ef):
            raise ContractError("entries[].fraction 必须全部给出或全部为 null（部分给出不得与 policy 兜底混合）")
        if any(f is None for f in tf) and any(f is not None for f in tf):
            raise ContractError("tps[].fraction 必须全部给出或全部为 null")
        if ef and ef[0] is not None:
            for f in ef:
                check_decimal(f, "entry.fraction")
            if sum(ef) != 1:
                raise ContractError("entry fractions 之和必须为 1")
        if tf and tf[0] is not None:
            for f in tf:
                check_decimal(f, "tp.fraction")
            if sum(tf) > 1:
                raise ContractError("tp fractions 之和必须 <= 1")
        lo = min(e.price_lo for e in self.entries)
        hi = max(e.price_hi for e in self.entries)
        if self.side == "long":
            if self.stop.price >= lo:
                raise ContractError("long 的 stop 必须低于最低入场价")
            if any(t.level <= hi for t in self.tps):
                raise ContractError("long 的 TP 必须高于最高入场价")
        else:
            if self.stop.price <= hi:
                raise ContractError("short 的 stop 必须高于最高入场价")
            if any(t.level >= lo for t in self.tps):
                raise ContractError("short 的 TP 必须低于最低入场价")
        return self

    @property
    def sign(self) -> int:
        return 1 if self.side == "long" else -1

    @property
    def entry_fractions_given(self) -> bool:
        return self.entries[0].fraction is not None

    @property
    def tp_fractions_given(self) -> bool:
        return bool(self.tps) and self.tps[0].fraction is not None


def equal_split(n: int, total: Decimal) -> tuple[Decimal, ...]:
    """§5.10 B8：Decimal 标度 12 ROUND_DOWN 等分，余量并入末腿使和恰为 total；禁止浮点。"""
    if n <= 0:
        return ()
    each = (total / Decimal(n)).quantize(RATIO_QUANTUM, rounding=ROUND_DOWN)
    parts = [each] * n
    parts[-1] = parts[-1] + (total - each * n)
    return tuple(parts)


EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def _us(t: dt.datetime) -> int:
    return (t - EPOCH) // dt.timedelta(microseconds=1)


def first_grid_point(at: dt.datetime, interval_s: int) -> dt.datetime:
    """[at, ∞) 内第一个落在 interval 网格上的时刻（含 at 自身，若 at 已在网格上）。

    **单一来源**的网格数学：精确到微秒，禁止整秒取模——t_dec 带亚秒时整秒取模会把"在网格上"误判成"不在"（S28 同族）。
    """
    iv_us = interval_s * 1_000_000
    rem = _us(at) % iv_us
    return at if rem == 0 else at + dt.timedelta(microseconds=iv_us - rem)


def grid_points_between(a: dt.datetime, b: dt.datetime, interval_s: int) -> int:
    """半开区间 [a, b) 内的网格点个数。与 first_grid_point 共用同一套精确微秒算术。"""
    if b <= a:
        return 0
    iv_us = interval_s * 1_000_000
    first_idx = -(-_us(a) // iv_us)          # ceil
    last_idx = (_us(b) - 1) // iv_us         # 最大满足 idx*iv < b
    return max(0, last_idx - first_idx + 1)


def derived_t_start(t_dec: dt.datetime, policy: "ExecutionPolicy") -> dt.datetime:
    """§R6：t_start = t_dec + policy.latency_s。**单一来源**——validator 与 resolved_t_start 共用，
    防止同一规则再出现第二处表达（S24/S27 同族）。"""
    return t_dec + dt.timedelta(seconds=policy.latency_s)


def derived_window_s(plan: "OrderPlan", policy: "ExecutionPolicy", entry_ttl_s: int) -> int:
    """§5.14 B12 推导观察窗 = 入场等待段(ttl) + 持仓段(max_holding 或 research_horizon_s)。

    **单一来源**：`build_request` 与 `ExecutionRequest` 校验共用本函数——S24 的成因正是同一公式有两处实现而只改了一处。
    """
    hold = plan.expiry.max_holding_s
    return entry_ttl_s + (hold if hold is not None else policy.research_horizon_s)


def resolve_entry_ttl_s(plan: "OrderPlan", policy: "ExecutionPolicy") -> int:
    """入场 TTL 的唯一解析：作者显式值优先，否则采用政策兜底。"""
    ttl = plan.expiry.entry_ttl_s
    if ttl is not None:
        return ttl
    return policy.entry_ttl_s


def entry_expiry_at(t_start: dt.datetime, entry_ttl_s: int) -> dt.datetime:
    """入场单到期时刻的唯一表达，A/B 共用契约时间边界。"""
    return t_start + dt.timedelta(seconds=entry_ttl_s)


def resolve_fractions(plan: "OrderPlan", policy: "ExecutionPolicy") -> tuple[tuple[Decimal, ...], tuple[Decimal, ...]]:
    ef = tuple(e.fraction for e in plan.entries) if plan.entry_fractions_given else equal_split(len(plan.entries), Decimal(1))
    if plan.tps:
        tf = tuple(t.fraction for t in plan.tps) if plan.tp_fractions_given else equal_split(len(plan.tps), policy.tp_total_fraction)
    else:
        tf = ()
    return ef, tf


# ---------------------------------------------------------------------------
# 政策（policy_version → 不可变内容 + 哈希）
# ---------------------------------------------------------------------------
class CostSpec(_Model):
    maker_fee: Decimal = Decimal(0)        # 比例
    taker_fee: Decimal = Decimal(0)
    slippage_ticks: int = 0                # market 成交：参考价 ± ticks×tick_size，再 ± bps
    slippage_bps: Decimal = Decimal(0)


class ExecutionPolicy(_Model):
    version: str
    latency_s: int = 0
    ladder_steps: int = 2
    costs: dict[str, CostSpec]
    participation: Decimal | None = None   # None = 容量无限；否则每价点容量 floor_step(participation×bar_volume/4)
    wallet: Decimal = Decimal(1000)
    leverage: Decimal = Decimal(1)
    mark_max_staleness_s: int = 120
    settlement_quantum: Decimal = SETTLEMENT_QUANTUM
    max_horizon_s: int = 14 * 86400        # §5.14 B12：**安全上限**（防无界观察、界定数据需求），不是研究观察窗
    research_horizon_s: int                # §5.14 B12：**研究标准观察窗**（必填，无默认值——默认值会让"未声明"静默变成一个数）
                                           # ⚠ 取值待定：见 docs/adr/report-G2-research-horizon-proposal.md，
                                           #    属 G-STAT-CLAIM 实质内容，P2 真实数据声明时随闸门确认
    entry_ttl_s: int = 24 * 3600           # §5.9（B7）：计划未给入场有效期时的兜底（进 content_hash）
    entry_fraction_rule: Literal["equal"] = "equal"     # §5.10 B8：单腿 [1]；n 腿等分（标度 12，余量末腿）
    tp_fraction_rule: Literal["equal"] = "equal"
    tp_total_fraction: Decimal = Decimal(1)

    @field_validator("latency_s")
    @classmethod
    def _latency_domain(cls, value):
        if value < 0:
            raise ContractError("latency_s 必须非负：启动时刻不能早于决策时刻")
        return value

    def cost(self, scenario: str) -> CostSpec:
        if scenario not in self.costs:
            raise ContractError(f"policy {self.version} 无 cost_scenario={scenario}")
        return self.costs[scenario]

    @property
    def content_hash(self) -> str:
        return sha256_canonical(self.model_dump(mode="json"))


POLICIES: dict[str, ExecutionPolicy] = {
    # 夹具专用：零费用、零滑点、容量由夹具价点给出（缺省无限）
    "fixture-zero-v1": ExecutionPolicy(version="fixture-zero-v1", research_horizon_s=5 * 86400, costs={"base": CostSpec(), "stress": CostSpec()}),
    # 夹具专用：taker 0.05%、market 滑点 1 tick（E17）
    "fixture-tick-v1": ExecutionPolicy(version="fixture-tick-v1", research_horizon_s=5 * 86400,
                                       costs={"base": CostSpec(taker_fee=Decimal("0.0005"), slippage_ticks=1),
                                              "stress": CostSpec(taker_fee=Decimal("0.0005"), slippage_ticks=1)}),
    # 夹具专用：只与 fixture-zero-v1 差 tp_total_fraction（B8 验收 (b)：policy_hash 与 trace_hash 均须不同）
    "fixture-halftp-v1": ExecutionPolicy(version="fixture-halftp-v1", research_horizon_s=5 * 86400, tp_total_fraction=Decimal("0.5"),
                                         costs={"base": CostSpec(), "stress": CostSpec()}),
    # 夹具专用：钱包 99（E14c 资金不足）
    "fixture-wallet99-v1": ExecutionPolicy(version="fixture-wallet99-v1", research_horizon_s=5 * 86400, wallet=Decimal(99),
                                           costs={"base": CostSpec(), "stress": CostSpec()}),
    # 研究基线（C02 待 G0 冻结；数值为 Binance USDT-M 常规档）
    "base-v1": ExecutionPolicy(version="base-v1", research_horizon_s=5 * 86400, latency_s=0, ladder_steps=3,
                               costs={"base": CostSpec(maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0005")),
                                      "stress": CostSpec(maker_fee=Decimal("0.0005"), taker_fee=Decimal("0.0005"),
                                                         slippage_ticks=1, slippage_bps=Decimal(2))},
                               participation=Decimal("0.1"), wallet=Decimal(100000), leverage=Decimal(10)),
}


POLICY_HASH_REGISTRY = Path(__file__).with_name("policy_hashes.json")   # S12：version→hash 跨修订登记（改内容必须改版本名并更新登记）


def _raw_policy_registry() -> dict:
    return json.loads(POLICY_HASH_REGISTRY.read_text(encoding="utf-8")) if POLICY_HASH_REGISTRY.exists() else {}


def load_policy_registry() -> dict[str, str]:
    """version → content_hash。下划线开头的键是元数据（占位状态等），不参与版本查找。"""
    return {k: v for k, v in _raw_policy_registry().items() if not k.startswith("_")}


def policy_registry_meta() -> dict:
    """B4 占位期豁免条件：登记表须显式标注占位状态与待定依据。"""
    return {k: v for k, v in _raw_policy_registry().items() if k.startswith("_")}


def resolve_policy(version: str) -> ExecutionPolicy:
    if version not in POLICIES:
        raise ContractError(f"未知 policy_version {version}")
    pol = POLICIES[version]
    reg = load_policy_registry()
    if version in reg and reg[version] != pol.content_hash:
        raise ContractError(f"policy_version={version} 内容哈希 {pol.content_hash[:12]} 与登记 {reg[version][:12]} 不符：改内容必须换版本名并更新 policy_hashes.json")
    if reg and version not in reg:
        raise ContractError(f"policy_version={version} 未登记于 policy_hashes.json")
    return pol


# ---------------------------------------------------------------------------
# ExecutionRequest（契约 §3 + R6）
# ---------------------------------------------------------------------------
class ExecutionRequest(_Model):
    episode_id: str
    graph_version: str
    decision_snapshot_hash: str
    t_dec: dt.datetime
    order_plan: OrderPlan
    policy_version: str
    policy_hash: str                        # 契约 §5.5 裁定 B4：policy 规范化内容 sha256，必须与注册表一致
    risk_budget: Decimal = Field(gt=0)
    cost_scenario: CostScenario = "base"
    path_scenario: PathScenario = "primary"
    market_manifest: str
    execution_contract_version: str = EXECUTION_CONTRACT_VERSION
    seed: int = 0
    t_start: dt.datetime | None = None      # None → t_dec + policy.latency_s
    horizon_end: dt.datetime
    position_mode: Literal["one_way"] = "one_way"
    entry_ttl_s: int | None = Field(default=None, gt=0)   # §5.9：解析后的入场有效期（显式字段，进 trace_hash）；None 时由验证器解析
    entry_fractions: tuple[Decimal, ...] | None = None    # §5.10 B8：解析后的入场分配（显式，进 trace_hash）
    tp_fractions: tuple[Decimal, ...] | None = None
    horizon_source: Literal["policy", "caller"] = "policy"   # §5.13 B11 诊断列：观察窗是推导值还是调用方自选

    @model_validator(mode="before")
    @classmethod
    def _resolve_ttl(cls, data):
        if not isinstance(data, dict):
            return data
        plan = data.get("order_plan")
        if isinstance(plan, dict):
            plan = OrderPlan.model_validate(plan)
        pol = POLICIES.get(data.get("policy_version"))
        if data.get("entry_ttl_s") is None and isinstance(plan, OrderPlan) and pol is not None:
            data = {**data, "entry_ttl_s": resolve_entry_ttl_s(plan, pol)}
        if isinstance(plan, OrderPlan) and pol is not None and (data.get("entry_fractions") is None or data.get("tp_fractions") is None):
            ef, tf = resolve_fractions(plan, pol)
            # S22：用 `is None` 而非 `or`——空元组/空列表是**显式值**，必须进入 §5.11 B9 的逐值对账并被拒，
            # 不得因假值被静默替换成推导结果（"静默修补显式值"与"显式值绕过推导"是同一枚硬币的两面）。
            data = {**data,
                    "entry_fractions": ef if data.get("entry_fractions") is None else data["entry_fractions"],
                    "tp_fractions": tf if data.get("tp_fractions") is None else data["tp_fractions"]}
        return data

    @property
    def fraction_source(self) -> Literal["plan", "policy"]:
        """§5.10 B8 第 5 条：entries 与 tps 都来自 plan ⇒ plan，否则 policy（保守）。"""
        p = self.order_plan
        return "plan" if p.entry_fractions_given and (not p.tps or p.tp_fractions_given) else "policy"

    @property
    def entry_ttl_source(self) -> Literal["plan", "policy"]:
        return "plan" if self.order_plan.expiry.entry_ttl_s is not None else "policy"

    @field_validator("t_dec", "horizon_end", "t_start")
    @classmethod
    def _tz(cls, v):
        return None if v is None else _utc(v)

    @model_validator(mode="after")
    def _chk(self):
        check_decimal(self.risk_budget, "risk_budget")
        if self.execution_contract_version != EXECUTION_CONTRACT_VERSION:
            raise ContractError(f"合同版本不符：{self.execution_contract_version} != {EXECUTION_CONTRACT_VERSION}")
        pol = resolve_policy(self.policy_version)
        # S30：观察窗下界此前用 (t_start or t_dec)，省略 t_start 时漏 latency——同一请求"省略"与"显式写同值"
        # 走两条不同边界，公共请求边界随表达方式分叉。统一用单一来源 derived_t_start。
        exp_t_start = derived_t_start(self.t_dec, pol)
        if exp_t_start < self.t_dec:
            raise ContractError("t_start 不能早于 t_dec（解析后的启动时刻）")
        if self.horizon_end <= exp_t_start:
            raise ContractError(f"horizon_end 必须晚于 t_start（推导值 {exp_t_start}，latency_s={pol.latency_s}）")
        expected = pol.content_hash
        if self.policy_hash != expected:
            raise ContractError(f"policy_hash 与 policy_version={self.policy_version} 的内容哈希不符（{self.policy_hash[:12]} != {expected[:12]}）")
        if self.entry_ttl_s is None:
            raise ContractError("entry_ttl_s 未能解析（计划与 policy 都未给）")
        # S19（与 S17 同型，契约判例 3）：显式 TTL 是**解析结果的记录**，不是独立输入——
        # 作者给值时须等于作者值，作者留空时须等于 policy 兜底值；否则同一 policy_hash 下可擅改入场期限，
        # 把标签从 tp_hit 翻成 unfilled_expired。
        # S20（B9 同族）：t_start 契约语义是"policy 决定"的解析记录，不是独立输入——
        # 否则同一 policy_hash 下可平移开始时刻，把标签从 tp_hit 翻成 unfilled_expired。要改开始时刻请改 policy.latency_s（进 content_hash）。
        if self.t_start is not None and self.t_start != exp_t_start:
            raise ContractError(f"t_start 与 policy {self.policy_version} 解析值不一致：{self.t_start} != {exp_t_start}"
                                f"（latency_s={pol.latency_s}；要平移开始时刻请改 policy.latency_s）")
        exp_ttl = resolve_entry_ttl_s(self.order_plan, pol)
        if self.entry_ttl_s != exp_ttl:
            src = "计划" if self.entry_ttl_source == "plan" else f"policy {self.policy_version}"
            raise ContractError(f"entry_ttl_s 与{src}解析值不一致：{self.entry_ttl_s} != {exp_ttl}")
        # §5.13 B11 / §5.14 B12：观察窗——推导路径受 B9 同款对账；调用方自选路径合法但必须留在 trace_hash 并标 caller；
        # 两条路径都受 max_horizon_s 安全上限封顶（上限不是默认值，二者职责分离）。
        # S25：不得用 int() 截断——+1µs 既能绕过逐值对账也能绕过安全封顶。一律按精确 timedelta 比较。
        window = self.horizon_end - exp_t_start
        if window > dt.timedelta(seconds=pol.max_horizon_s):
            raise ContractError(f"观察窗 {window} 超过 policy {self.policy_version} 的安全上限 "
                                f"max_horizon_s={pol.max_horizon_s}s（亚秒偏移同样超限）")
        derived_s = derived_window_s(self.order_plan, pol, exp_ttl)
        if self.horizon_source == "policy" and window != dt.timedelta(seconds=derived_s):
            raise ContractError(f"horizon_source=policy 但 horizon_end 与推导值不一致：窗口 {window} != {dt.timedelta(seconds=derived_s)}"
                                f"（自选观察窗请显式标 horizon_source='caller'，它会进 trace_hash 并由 G3 记入尝试账本）")
        if self.entry_fractions is None or self.tp_fractions is None:
            raise ContractError("entry_fractions / tp_fractions 未能解析")
        if len(self.entry_fractions) != len(self.order_plan.entries) or len(self.tp_fractions) != len(self.order_plan.tps):
            raise ContractError("entry_fractions / tp_fractions 长度与计划不符")
        # S17：显式字段只能是解析结果的记录，不得成为第三种分配来源；逐值过 Decimal(38,12)
        for f in self.entry_fractions:
            check_decimal(f, "entry_fractions[]")
        for f in self.tp_fractions:
            check_decimal(f, "tp_fractions[]")
        exp_ef, exp_tf = resolve_fractions(self.order_plan, pol)
        if tuple(self.entry_fractions) != tuple(exp_ef):
            raise ContractError(f"entry_fractions 与 plan/policy 推导不一致：{self.entry_fractions} != {exp_ef}")
        if tuple(self.tp_fractions) != tuple(exp_tf):
            raise ContractError(f"tp_fractions 与 plan/policy 推导不一致：{self.tp_fractions} != {exp_tf}")
        if sum(self.entry_fractions) != 1 or any(f <= 0 for f in self.entry_fractions):
            raise ContractError("entry_fractions 必须为正且和为 1")
        if self.tp_fractions and (sum(self.tp_fractions) > 1 or any(f <= 0 for f in self.tp_fractions)):
            raise ContractError("tp_fractions 必须为正且和 <= 1")
        return self

    def resolved_t_start(self, policy: ExecutionPolicy) -> dt.datetime:
        return self.t_start if self.t_start is not None else derived_t_start(self.t_dec, policy)


def build_request(
    episode_row: dict, *, policy_version: str, policy_hash: str, risk_budget: Decimal, cost_scenario: CostScenario = "base",
    path_scenario: PathScenario = "primary", market_manifest: str, seed: int = 0, horizon_end: dt.datetime | None = None,
) -> ExecutionRequest:
    """research-schema §9.4 裁定 A3：G1 episode 行（order_plan / t_dec / decision_snapshot_hash / episode_id / graph_version）
    + 调用方给的钱与场景 → ExecutionRequest。只补齐执行侧字段并校验，不重新解释原文。
    horizon_end 缺省 = t_dec + entry_ttl + (max_holding 或 policy.max_horizon_s)。"""
    required = ("episode_id", "graph_version", "decision_snapshot_hash", "t_dec", "order_plan")
    missing = [k for k in required if k not in episode_row or episode_row[k] is None]
    if missing:
        raise ContractError(f"episode_row 缺字段 {missing}")
    plan = episode_row["order_plan"]
    plan = plan if isinstance(plan, OrderPlan) else OrderPlan.model_validate(plan)
    t_dec = episode_row["t_dec"]
    if isinstance(t_dec, str):
        t_dec = dt.datetime.fromisoformat(t_dec.replace("Z", "+00:00"))
    policy = resolve_policy(policy_version)
    ttl = resolve_entry_ttl_s(plan, policy)
    horizon_source = "caller" if horizon_end is not None else "policy"
    if horizon_end is None:
        horizon_end = derived_t_start(t_dec, policy) + dt.timedelta(seconds=derived_window_s(plan, policy, ttl))
    return ExecutionRequest(
        episode_id=str(episode_row["episode_id"]), graph_version=str(episode_row["graph_version"]),
        decision_snapshot_hash=str(episode_row["decision_snapshot_hash"]), t_dec=t_dec, order_plan=plan,
        policy_version=policy_version, policy_hash=policy_hash, risk_budget=D(risk_budget), cost_scenario=cost_scenario,
        path_scenario=path_scenario, market_manifest=market_manifest, seed=seed, horizon_end=horizon_end, entry_ttl_s=ttl,
        horizon_source=horizon_source,
    )


# ---------------------------------------------------------------------------
# ExecutionResult（契约 §3 + R7/R8 + C05 cash_delta）
# ---------------------------------------------------------------------------
class CanonicalEvent(_Model):
    seq: int
    ts: dt.datetime
    kind: EventKind
    order_id: str
    leg: Leg
    trigger_basis: TriggerBasis = "none"
    price: Decimal | None = None
    qty: Decimal | None = None
    fee: Decimal | None = None
    reason: str | None = None
    bar_open_time: dt.datetime | None = None
    path_step: PathStep = "none"
    cash_delta: Decimal | None = None       # C05：funding 现金流（收入为正），其余事件 null

    @field_validator("price", "qty", "fee", "cash_delta")
    @classmethod
    def _decimal(cls, value):
        if value is not None:
            check_decimal(value, "event decimal", positive=False)
        return value

    @field_validator("ts", "bar_open_time")
    @classmethod
    def _tz(cls, v):
        return None if v is None else _utc(v)


class CoverageMask(_Model):
    mark_ok: bool
    funding_ok: bool
    rules_ok: bool
    bars_ok: bool
    liquidation_unmodeled: bool = True


class ExecutionResult(_Model):
    canonical_events: list[CanonicalEvent]
    fill_status: FillStatus
    filled_qty: Decimal
    fees: Decimal
    funding: Decimal
    slippage: Decimal
    gross_pnl: Decimal | None
    net_pnl: Decimal | None
    net_R: Decimal | None
    censor_reason: str | None
    coverage_mask: CoverageMask
    trace_hash: str
    kernel: Literal["A", "B"]
    kernel_version: str
    entry_avg_price: Decimal | None = None
    exit_avg_price: Decimal | None = None
    position_open_at: dt.datetime | None = None
    position_close_at: dt.datetime | None = None
    mae_R: Decimal | None = None
    mfe_R: Decimal | None = None
    entry_ttl_source: Literal["plan", "policy"] = "plan"   # §5.9 诊断列
    fraction_source: Literal["plan", "policy"] = "plan"    # §5.10 B8 诊断列
    horizon_source: Literal["policy", "caller"] = "policy" # §5.13 B11 诊断列

    @property
    def outcome_kind(self) -> str:
        """A15 七值（派生量，不进 model_dump / trace_hash：它是结果的读法，不是内核的输入）。"""
        return outcome_kind(self)

    @property
    def exit_legs(self) -> tuple[str, ...]:
        return exit_legs(self)

    @field_validator("censor_reason")
    @classmethod
    def _cr(cls, v):
        if v is not None and v not in CENSOR_REASONS:
            raise ContractError(f"未知 censor_reason {v}")
        return v


@dataclass(frozen=True)
class ForceCloseValuation:
    net_R_forced: Decimal
    mark: Decimal
    mark_at: dt.datetime
    mark_source: str
    residual_qty: Decimal
    close_fee: Decimal
    estimand: Literal["forced_close"] = field(default="forced_close", init=False)


def force_close_net_R(
    res: ExecutionResult,
    *,
    mark: Decimal,
    mark_at: dt.datetime,
    mark_source: str,
    policy: ExecutionPolicy,
    risk_budget: Decimal,
    cost_scenario: CostScenario,
    side: Side,
    multiplier: Decimal,
) -> ForceCloseValuation | None:
    """只读强平估值：mark 及其已闭合/as-of 证据由调用方提供，函数不自查行情。

    tests/market 消费方夹具只证明五项记账、三条硬约束及 A24 登记/禁令对本函数生效；
    不证明 G3 调用而非自算，G3 尚未接入，须在接入时以哨兵证明 θ 随返回值改变。
    """
    if res.gross_pnl is None:
        raise ContractError("内核未提供已实现损益 gross_pnl；禁止从事件重建")
    exited = sum((event.qty for event in res.canonical_events
                  if event.leg in EXIT_LEG_ORDER and event.kind in FILL_KINDS
                  and event.qty is not None), Decimal(0))
    residual = res.filled_qty - exited
    if residual == 0:
        return None
    if res.entry_avg_price is None:
        raise ContractError("余仓估值需要 entry_avg_price")
    sign = Decimal(1)
    if side == "short":
        sign = Decimal(-1)
    unrealized = (mark - res.entry_avg_price) * residual * sign * multiplier
    close_fee = mark * residual * policy.cost(cost_scenario).taker_fee * multiplier
    net_forced = res.gross_pnl + unrealized - res.fees - close_fee + res.funding
    return ForceCloseValuation(
        net_R_forced=quantize_ratio(net_forced / risk_budget),
        mark=mark, mark_at=mark_at, mark_source=mark_source,
        residual_qty=residual, close_fee=close_fee,
    )


def exit_legs(res: "ExecutionResult") -> tuple[str, ...]:
    """出场实际参与的 leg 集合（去重、按 sl<tp<close 排序）。A15 裁定 C：混合出场不丢信息，不扩枚举。"""
    legs = {e.leg for e in res.canonical_events if e.kind in FILL_KINDS and e.leg in EXIT_LEG_ORDER}
    return tuple(l for l in EXIT_LEG_ORDER if l in legs)


def outcome_kind(res: "ExecutionResult") -> str:
    """ExecutionResult → reconstructed_outcome.kind（A15 七值）。纯函数，不读行情；判定顺序见规格 §1。

    先回答"这条样本能不能评"（①②），再回答"它怎么结束"（③–⑦）：
    ① 数据齐全但标签未成熟 → right_censored；② 看世界所需的数据缺了 → unevaluable（禁映 right_censored）。
    ③ 根本没挂出去 → rejected（禁并入 unfilled_expired）；④ 挂了市场没来 → unfilled_expired。
    ⑤ 止损参与即 stopped（保守）；⑥ 出场全为 TP → tp_hit；⑦ 其余已平仓 → filled_closed（v0 无政策平仓腿，暂不可达）。
    """
    if res.censor_reason == "LABEL_RIGHT_CENSORED":
        return "right_censored"
    if res.censor_reason in EVIDENCE_CENSORS:
        return "unevaluable"
    kinds = {e.kind for e in res.canonical_events}
    if res.fill_status == "none":
        # 被拒（PRICE_FILTER/LOT_SIZE/MIN_NOTIONAL/MARGIN/POST_ONLY_CROSS）与"挂了没成交"必须分开计
        if "rejected" in kinds:
            return "rejected"
        # 规则④（B10）：订单以 expired 或 cancelled 终止——TTL 到期 / IOC·FOK 立即撤销 / 撤单。
        # 取枚举而非"非 rejected"兜底：将来冒出第三种终止形态时强制有人做一次决定，而不是被静默吸收。
        if kinds & {"expired", "cancelled"}:
            return "unfilled_expired"
        raise ContractError(
            f"outcome_kind 未命中契约 §3 任何规则（B10 禁止兜底贴值）；"
            f"fill_status={res.fill_status} censor={res.censor_reason} 实际事件集合={sorted(kinds)}")
    legs = exit_legs(res)
    if "closed" not in kinds:
        raise ContractError(
            f"outcome_kind 未命中契约 §3 任何规则（B10 禁止兜底贴值）；未删失且有成交却无 closed，"
            f"fill_status={res.fill_status} censor={res.censor_reason} 实际事件集合={sorted(kinds)}")
    if "sl" in legs:
        return "stopped"
    if legs and set(legs) == {"tp"}:
        return "tp_hit"
    if legs:
        return "filled_closed"                              # 规则⑦：含政策平仓腿（v0 不可达，C06 后转正例）
    raise ContractError(
        f"outcome_kind 未命中契约 §3 任何规则（B10 禁止兜底贴值）；已 closed 且有成交却无出场腿，"
        f"实际事件集合={sorted(kinds)}")


# 与 G3 配对的键（R9）
# §5.18 B16(3)：配对面必须带 policy_hash——否则同一版本串下的两套数值会被当作同一条臂，
# baseline 与 candidate 可能来自不同经济假设而无人察觉。G3 的 _ARM_KEYS 已含该字段。
PAIR_KEY = ("episode_id", "graph_version", "policy_version", "policy_hash", "cost_scenario", "path_scenario", "kernel")
REQUEST_ID_COLS = ("episode_id", "graph_version", "decision_snapshot_hash", "t_dec", "policy_version", "policy_hash", "cost_scenario",
                   "path_scenario", "market_manifest", "execution_contract_version", "seed", "entry_ttl_s", "entry_fractions", "tp_fractions")
RESULT_SCALAR_COLS = ("kernel", "kernel_version", "trace_hash", "fill_status", "filled_qty", "fees", "funding", "slippage",
                      "gross_pnl", "net_pnl", "net_R", "censor_reason", "entry_avg_price", "exit_avg_price",
                      "position_open_at", "position_close_at", "mae_R", "mfe_R", "entry_ttl_source", "fraction_source",
                      "outcome_kind", "exit_legs", "horizon_source")


# ---------------------------------------------------------------------------
# 规范 JSON 与 trace_hash（ADR §11）
# ---------------------------------------------------------------------------
def _canon(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return _canon(obj.model_dump())
    if isinstance(obj, dict):
        return {str(k): _canon(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [_canon(x) for x in obj]
    if isinstance(obj, Decimal):
        if obj.is_nan() or obj.is_infinite():
            raise ContractError("Decimal NaN/Infinity 不允许进入规范 JSON")
        s = format(obj.normalize(), "f")
        return "0" if s in ("-0", "0", "-0.0") else s
    if isinstance(obj, dt.datetime):
        return _utc(obj).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(obj, float):
        raise ContractError("float 不允许进入规范 JSON（用 Decimal）")
    return obj


def canonical_json(obj: Any) -> str:
    return json.dumps(_canon(obj), ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def sha256_canonical(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def request_canonical(req: ExecutionRequest, policy: ExecutionPolicy, *, t_start: dt.datetime, market_manifest_hash: str) -> dict:
    d = req.model_dump()
    d["t_start"] = t_start
    d["policy"] = {"version": policy.version, "content_hash": policy.content_hash, "content": policy.model_dump()}
    d["market_manifest_hash"] = market_manifest_hash
    return d


def trace_hash(*, kernel: str, kernel_version: str, req_canonical: dict, events: list[CanonicalEvent]) -> str:
    return sha256_canonical({
        "execution_contract_version": EXECUTION_CONTRACT_VERSION, "kernel": kernel, "kernel_version": kernel_version,
        "request_canonical": req_canonical, "canonical_events": [e.model_dump() for e in events],
    })


# ---------------------------------------------------------------------------
# 不变量（契约 §3 七条 + R 补充 + ADR I 表可在结果层验证者）
# ---------------------------------------------------------------------------
def check_invariants(req: ExecutionRequest, res: ExecutionResult, *, multiplier: Decimal = Decimal(1)) -> None:
    """每次 simulate 后断言；失败抛 ExecutionInvariantError。multiplier 参与全部金额复算（S08）。"""
    ev = res.canonical_events
    sign = req.order_plan.sign
    mult = multiplier
    # I14 seq 连续、ts 单调
    if [e.seq for e in ev] != list(range(len(ev))):
        raise ExecutionInvariantError("seq 必须从 0 连续")
    for a, b in zip(ev, ev[1:]):
        if b.ts < a.ts:
            raise ExecutionInvariantError(f"ts 非单调 @seq={b.seq}")
    # I13 触发基准
    for e in ev:
        for field in ("price", "qty", "fee", "cash_delta"):
            value = getattr(e, field)
            if value is not None:
                try:
                    check_decimal(value, f"event[{e.seq}].{field}", positive=False)
                except ContractError as exc:
                    raise ExecutionInvariantError(str(exc)) from exc
        if e.trigger_basis == "mark" and e.kind != "stop_triggered":
            raise ExecutionInvariantError(f"trigger_basis=mark 只允许 stop_triggered @seq={e.seq}")
        if e.kind == "tp_triggered" and e.trigger_basis != "last":
            raise ExecutionInvariantError(f"tp_triggered 必须 trigger_basis=last @seq={e.seq}")
        if e.kind == "funding" and (e.leg != "funding" or e.cash_delta is None or e.price is None or e.qty is None):
            raise ExecutionInvariantError(f"funding 事件缺 price/qty/cash_delta @seq={e.seq}")
        if e.kind in FILL_KINDS and (e.price is None or e.qty is None or e.qty <= 0):
            raise ExecutionInvariantError(f"fill 事件缺 price/qty @seq={e.seq}")
    # 终态唯一（GTD/IOC 终态唯一）；submitted 记录订单量；累计成交 ≤ 订单量
    order_qty: dict[str, Decimal] = {}
    cum: dict[str, Decimal] = {}
    terminal: dict[str, int] = {}
    settle_keys: set = set()
    for e in ev:
        if e.kind == "submitted":
            if e.order_id in order_qty:
                raise ExecutionInvariantError(f"订单 {e.order_id} 重复 submitted")
            order_qty[e.order_id] = e.qty if e.qty is not None else Decimal(0)
        if e.kind == "amended" and e.qty is not None:
            if e.order_id in terminal:
                raise ExecutionInvariantError(f"终态后 amended：{e.order_id}")
            order_qty[e.order_id] = e.qty
        if e.kind == "funding":
            if e.ts in settle_keys:
                raise ExecutionInvariantError(f"I11 结算键重复入账 @{e.ts}")
            settle_keys.add(e.ts)
        if e.kind in FILL_KINDS and e.leg != "funding":
            if e.order_id not in order_qty:
                raise ExecutionInvariantError(f"fill 前无 submitted：{e.order_id}")
            if e.order_id in terminal:
                raise ExecutionInvariantError(f"终态后 fill：{e.order_id} @seq={e.seq}")
            cum[e.order_id] = cum.get(e.order_id, Decimal(0)) + e.qty
            if cum[e.order_id] > order_qty[e.order_id]:
                raise ExecutionInvariantError(f"累计成交 > 订单量：{e.order_id}")
            if e.kind == "filled" and cum[e.order_id] != order_qty[e.order_id]:
                raise ExecutionInvariantError(f"filled 时累计量 != 订单量：{e.order_id}")
        if e.kind in TERMINAL_KINDS:
            terminal[e.order_id] = terminal.get(e.order_id, 0) + 1
            if terminal[e.order_id] > 1:
                raise ExecutionInvariantError(f"订单 {e.order_id} 多个终态")
    for oid, n in terminal.items():
        pass
    # 数量守恒 / reduce-only 不增仓 / 触 TP 前无 fill 则仓位为零
    pos = Decimal(0)
    entry_qty = Decimal(0)
    entry_cost = Decimal(0)
    exit_qty = Decimal(0)
    exit_value = Decimal(0)
    had_fill = False
    fees = Decimal(0)
    funding = Decimal(0)
    n_closed = 0
    for e in ev:
        if e.kind in FILL_KINDS and e.leg == "entry":
            pos += sign * e.qty
            entry_qty += e.qty
            entry_cost += e.qty * e.price
            had_fill = True
        elif e.kind in FILL_KINDS and e.leg in ("sl", "tp", "close"):
            if not had_fill:
                raise ExecutionInvariantError("无 entry fill 却有出场成交")
            new = pos - sign * e.qty
            if abs(new) > abs(pos) or (new != 0 and (new > 0) != (pos > 0)):
                raise ExecutionInvariantError(f"reduce-only 增仓/翻转 @seq={e.seq}")
            pos = new
            exit_qty += e.qty
            exit_value += e.qty * e.price
        elif e.kind in ("tp_triggered", "stop_triggered") and not had_fill:
            raise ExecutionInvariantError(f"无 entry fill 却触发 {e.kind} @seq={e.seq}")
        elif e.kind == "funding":
            funding += e.cash_delta
            if e.qty != pos:
                raise ExecutionInvariantError(f"funding qty 必须等于结算前仓位 @seq={e.seq}: {e.qty} != {pos}")
        elif e.kind == "closed":
            n_closed += 1
            if pos != 0:
                raise ExecutionInvariantError("closed 时仓位非零")
            # I17：closed 当刻（按事件顺序到此为止）所有已提交订单必须已终态
            seen_terminal = {x.order_id for x in ev[: e.seq] if x.kind in TERMINAL_KINDS}
            seen_submitted = {x.order_id for x in ev[: e.seq] if x.kind == "submitted"}
            open_orders = sorted(seen_submitted - seen_terminal)
            if open_orders:
                raise ExecutionInvariantError(f"closed 时兄弟腿未终态：{open_orders}")
            if any(x.kind in FILL_KINDS or x.kind in TERMINAL_KINDS for x in ev[e.seq + 1:] if x.leg != "funding"):
                raise ExecutionInvariantError("closed 之后仍有订单成交/终态事件")
        if e.fee is not None:
            fees += e.fee
    if n_closed > 1:
        raise ExecutionInvariantError("closed 多于一次")
    if res.filled_qty != entry_qty:
        raise ExecutionInvariantError(f"filled_qty {res.filled_qty} != Σentry fills {entry_qty}")
    if exit_qty > entry_qty:
        raise ExecutionInvariantError("出场量 > 入场量")
    if quantize_money(res.fees) != quantize_money(fees):
        raise ExecutionInvariantError(f"fees {res.fees} != Σfee {fees}")
    if quantize_money(res.funding) != quantize_money(funding):
        raise ExecutionInvariantError(f"funding {res.funding} != Σcash_delta {funding}")
    # fill_status
    if (entry_qty == 0) != (res.fill_status == "none"):
        raise ExecutionInvariantError("fill_status 与成交量不符")
    # censor / net_R
    if res.censor_reason is not None:
        if res.net_R is not None or res.net_pnl is not None:
            raise ExecutionInvariantError("censor_reason 非空时 net_pnl/net_R 必须为 null")
    else:
        if res.net_pnl is None or res.net_R is None or res.gross_pnl is None:
            raise ExecutionInvariantError("未删失时 gross/net/net_R 不能为 null")
        if n_closed == 0 and entry_qty > 0:
            raise ExecutionInvariantError("有成交且未删失则必须 closed")
        if quantize_money(res.net_pnl) != quantize_money(res.gross_pnl - res.fees + res.funding):
            raise ExecutionInvariantError("net_pnl != gross - fees + funding")
        if quantize_money(res.net_R) != quantize_money(res.net_pnl / req.risk_budget):
            raise ExecutionInvariantError("net_R != net_pnl / risk_budget")
        if entry_qty == 0 and (res.net_pnl != 0 or res.net_R != 0):
            raise ExecutionInvariantError("未成交且未删失 → net_pnl/net_R = 0")
        if entry_qty > 0 and pos == 0:
            avg_in = entry_cost / entry_qty
            gross = sum((e.price - avg_in) * e.qty * sign * mult for e in ev if e.kind in FILL_KINDS and e.leg in ("sl", "tp", "close"))
            if quantize_money(res.gross_pnl) != quantize_money(gross):
                raise ExecutionInvariantError(f"gross_pnl {res.gross_pnl} != 由事件复算 {gross}")
    if entry_qty > 0:
        if res.entry_avg_price is None or quantize_money(res.entry_avg_price) != quantize_money(entry_cost / entry_qty):
            raise ExecutionInvariantError("entry_avg_price 与事件不符")
        if exit_qty > 0 and (res.exit_avg_price is None or quantize_money(res.exit_avg_price) != quantize_money(exit_value / exit_qty)):
            raise ExecutionInvariantError("exit_avg_price 与事件不符")
        if (pos == 0) != (res.position_close_at is not None):
            raise ExecutionInvariantError("position_close_at 与仓位归零不符")
    else:
        if res.entry_avg_price is not None or res.position_open_at is not None:
            raise ExecutionInvariantError("未成交不得有 entry_avg/position_open_at")


# ---------------------------------------------------------------------------
# 夹具（tests/market/fixtures/episodes/*.json）与结果 diff
# ---------------------------------------------------------------------------
class PricePoint(_Model):
    ts: dt.datetime
    price: Decimal
    capacity: Decimal | None = None       # None = 无限
    bar_open_time: dt.datetime | None = None
    path_step: PathStep = "none"

    @field_validator("ts", "bar_open_time")
    @classmethod
    def _tz(cls, v):
        return None if v is None else _utc(v)


class Bar(_Model):
    open_time: dt.datetime
    o: Decimal
    h: Decimal
    l: Decimal
    c: Decimal
    volume: Decimal = Decimal(0)
    interval_s: int = 60

    @field_validator("open_time")
    @classmethod
    def _tz(cls, v):
        return _utc(v)


class FundingRow(_Model):
    calc_time: dt.datetime
    rate: Decimal
    interval_hours: int

    @field_validator("calc_time")
    @classmethod
    def _tz(cls, v):
        return _utc(v)


class Rules(_Model):
    tick_size: Decimal = Decimal(1)
    step_size: Decimal = Decimal(1)
    min_notional: Decimal = Decimal(0)
    multiplier: Decimal = Decimal(1)
    min_qty: Decimal = Decimal(0)
    max_qty: Decimal | None = None
    min_price: Decimal = Decimal(0)
    max_price: Decimal | None = None
    effective_from: dt.datetime | None = None
    effective_to: dt.datetime | None = None


class MarketView(_Model):
    """一次 simulate 的行情输入：显式价点（points）或 bars（按 path_scenario 展开）。"""
    manifest_id: str
    last: list[PricePoint] = []
    mark: list[PricePoint] = []
    bars_last: list[Bar] = []
    bars_mark: list[Bar] = []
    funding: list[FundingRow] = []
    funding_schedule_complete: bool = True   # 覆盖证据：结算表在观察窗内完整
    rules: Rules = Rules()
    rules_known: bool = True
    bars_complete: bool = True              # 网格完整（缺 bar 可定位 → 内核逐时 BAR_GAP）
    bars_quality_ok: bool = True            # S15：来源/体检/OHLC 质量失败（不可按时间洞处理，起点即删失）
    manifest_refs: list[dict] = []          # S12：来源分区摘要（partition_id/source_sha256/schema_hash/available_at_basis）
    quality_notes: list[str] = []           # 装载时发现的覆盖问题（不进哈希以外的经济结果，只作诊断）

    @property
    def manifest_hash(self) -> str:
        d = self.model_dump()
        d.pop("quality_notes", None)
        return sha256_canonical(d)


class ExpectedResult(_Model):
    """夹具期望：ExecutionResult 去掉内核身份字段（trace_hash/kernel/kernel_version）。"""
    canonical_events: list[CanonicalEvent]
    fill_status: FillStatus
    filled_qty: Decimal
    fees: Decimal
    funding: Decimal
    slippage: Decimal
    gross_pnl: Decimal | None
    net_pnl: Decimal | None
    net_R: Decimal | None
    censor_reason: str | None
    coverage_mask: CoverageMask
    entry_avg_price: Decimal | None = None
    exit_avg_price: Decimal | None = None
    position_open_at: dt.datetime | None = None
    position_close_at: dt.datetime | None = None
    mae_R: Decimal | None = None
    mfe_R: Decimal | None = None


class EpisodeFixture(_Model):
    id: str
    title: str
    derivation: str
    request: ExecutionRequest
    market: MarketView
    expected: ExpectedResult
    kernels: list[str] = ["A", "B"]


def load_fixture(path: str | Path) -> EpisodeFixture:
    return EpisodeFixture.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_fixtures(dir_: str | Path) -> list[EpisodeFixture]:
    return [load_fixture(p) for p in sorted(Path(dir_).glob("*.json"))]


def expected_as_result(exp: ExpectedResult, *, kernel: str = "A", kernel_version: str = "gold", entry_ttl_source: str = "plan",
                       fraction_source: str = "plan", horizon_source: str = "caller") -> ExecutionResult:
    return ExecutionResult(**exp.model_dump(), trace_hash="", kernel=kernel, kernel_version=kernel_version,
                           entry_ttl_source=entry_ttl_source, fraction_source=fraction_source, horizon_source=horizon_source)


_EV_FIELDS = ("ts", "kind", "order_id", "leg", "trigger_basis", "price", "qty", "fee", "reason", "bar_open_time", "path_step", "cash_delta")
_SCALARS = ("fill_status", "filled_qty", "fees", "funding", "slippage", "gross_pnl", "net_pnl", "net_R", "censor_reason",
            "entry_avg_price", "exit_avg_price", "position_open_at", "position_close_at", "mae_R", "mfe_R")


def _eq(a, b) -> bool:
    if isinstance(a, Decimal) and isinstance(b, Decimal):
        return a.compare(b) == 0
    return a == b


def diff_result(expected: ExpectedResult, actual: ExecutionResult | ExpectedResult, *, ignore_reason: bool = True,
                all_diffs: bool = False) -> list[str]:
    """业务字段逐项比较（剔除内核身份）；返回差异说明列表，空 = 一致。首个事件差异在前；all_diffs=True 枚举全部事件差异（S10）。
    ignore_reason 只对夹具比对放宽 reason 文本；A/B 报告用 ignore_reason=False。"""
    out: list[str] = []
    ea, eb = expected.canonical_events, actual.canonical_events
    for i, (x, y) in enumerate(zip(ea, eb)):
        for f in _EV_FIELDS:
            if f == "reason" and ignore_reason:
                continue
            if not _eq(getattr(x, f), getattr(y, f)):
                out.append(f"event[{i}].{f}: expected={getattr(x, f)!r} actual={getattr(y, f)!r} (kind exp={x.kind} act={y.kind})")
                if not all_diffs:
                    break
        if out and not all_diffs:
            break
    if len(ea) != len(eb) and (not out or all_diffs):
        out.append(f"events length: expected={len(ea)} actual={len(eb)}; next expected={ea[len(eb)].kind if len(eb) < len(ea) else None} next actual={eb[len(ea)].kind if len(ea) < len(eb) else None}")
    for f in _SCALARS:
        if not _eq(getattr(expected, f), getattr(actual, f)):
            out.append(f"{f}: expected={getattr(expected, f)!r} actual={getattr(actual, f)!r}")
    if expected.coverage_mask != actual.coverage_mask:
        out.append(f"coverage_mask: expected={expected.coverage_mask} actual={actual.coverage_mask}")
    return out


__all__ = [
    "ForceCloseValuation", "force_close_net_R",
    "Entry", "Stop", "TakeProfit", "Sizing", "Expiry", "OrderPlan", "CostSpec", "ExecutionPolicy", "POLICIES", "resolve_policy",
    "ExecutionRequest", "build_request", "resolve_fractions", "derived_window_s", "derived_t_start",
    "first_grid_point", "grid_points_between", "equal_split", "CanonicalEvent", "CoverageMask", "ExecutionResult", "PricePoint", "Bar", "FundingRow", "Rules",
    "MarketView", "ExpectedResult", "EpisodeFixture", "load_fixture", "load_fixtures", "expected_as_result", "diff_result",
    "canonical_json", "sha256_canonical", "request_canonical", "trace_hash", "check_invariants", "ExecutionInvariantError",
    "outcome_kind", "exit_legs", "OUTCOME_KINDS", "EVIDENCE_CENSORS", "EXIT_LEG_ORDER",
    "ContractError", "floor_step", "quantize_money", "D", "PAIR_KEY", "REQUEST_ID_COLS", "RESULT_SCALAR_COLS",
    "CENSOR_REASONS", "CENSOR_PRIORITY", "SETTLEMENT_QUANTUM",
]
