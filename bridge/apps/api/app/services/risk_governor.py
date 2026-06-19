from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


RISK_STATES = {
    "normal",
    "warning",
    "blocked_new_entries",
    "kill_switch_enabled",
    "live_readonly",
}

REDUCING_ACTIONS = {"close", "partial_close", "move_stoploss", "reduce"}


@dataclass(frozen=True)
class RiskPolicy:
    max_single_trade_risk_pct: float = 1.0
    max_total_open_risk_pct: float = 5.0
    max_daily_realized_loss_pct: float = 3.0
    max_symbol_exposure_pct: float = 15.0
    max_correlated_group_exposure_pct: float = 35.0
    default_leverage: int = 3
    max_leverage: int = 5
    min_liquidation_buffer_pct: float = 3.0
    signal_max_age_minutes: int = 240
    allow_live_without_stop_loss: bool = False
    allow_live_without_take_profit: bool = False
    dry_run_allow_without_take_profit: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def default_risk_policy() -> RiskPolicy:
    return RiskPolicy()


def _parse_datetime(value) -> datetime | bool:
    if isinstance(value, datetime):
        parsed = value
    elif not value:
        return False
    elif isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    else:
        return False
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class JsonRiskStateStore:
    def __init__(self, path):
        self.path = path

    def load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return {}

    def save(self, state: dict) -> None:
        parent = Path(self.path).parent
        parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)

    def update(self, updates: dict) -> None:
        state = self.load()
        state.update(updates)
        self.save(state)


class PairLockStore:
    def __init__(self, state_store: JsonRiskStateStore | None = None):
        self.state_store = state_store
        self._locks: list[dict] = []
        if self.state_store:
            state = self.state_store.load()
            locks = state.get("pair_locks", [])
            for lock in locks:
                loaded = dict(lock)
                loaded["expires_at"] = _parse_datetime(loaded.get("expires_at"))
                loaded["released_at"] = _parse_datetime(loaded.get("released_at"))
                self._locks.append(loaded)

    def create(self, pair: str, reason: str, actor_id: str, expires_at) -> dict:
        lock = {
            "lock_id": str(uuid4()),
            "pair": pair,
            "reason": reason,
            "actor_id": actor_id,
            "created_at": _now_utc().isoformat(),
            "expires_at": _parse_datetime(expires_at),
            "released_at": False,
        }
        self._locks.append(lock)
        self._persist()
        return self._serialize_lock(lock)

    def release(self, lock_id: str) -> bool:
        for lock in self._locks:
            if lock["lock_id"] == lock_id:
                lock["released_at"] = _now_utc()
                self._persist()
                return True
        return False

    def active_for_pair(self, pair: str) -> list[dict]:
        active = []
        for lock in self._locks:
            if lock["pair"] != pair:
                continue
            if lock["released_at"]:
                continue
            expires_at = lock["expires_at"]
            if expires_at and expires_at <= _now_utc():
                continue
            active.append(self._serialize_lock(lock))
        return active

    def list_active(self) -> list[dict]:
        active = []
        for lock in self._locks:
            if lock["released_at"]:
                continue
            expires_at = lock["expires_at"]
            if expires_at and expires_at <= _now_utc():
                continue
            active.append(self._serialize_lock(lock))
        return active

    def _serialize_lock(self, lock: dict) -> dict:
        serialized = dict(lock)
        expires_at = serialized["expires_at"]
        released_at = serialized["released_at"]
        if isinstance(expires_at, datetime):
            serialized["expires_at"] = expires_at.isoformat()
        if isinstance(released_at, datetime):
            serialized["released_at"] = released_at.isoformat()
        return serialized

    def _persist(self) -> None:
        if not self.state_store:
            return
        locks = []
        for lock in self._locks:
            locks.append(self._serialize_lock(lock))
        self.state_store.update({"pair_locks": locks})


class RiskGovernor:
    def __init__(
        self,
        policy: RiskPolicy | None = None,
        pair_lock_store: PairLockStore | None = None,
        state_store: JsonRiskStateStore | None = None,
    ):
        if policy:
            self.policy = policy
        else:
            self.policy = default_risk_policy()
        self.state_store = state_store
        if pair_lock_store:
            self.pair_lock_store = pair_lock_store
        else:
            self.pair_lock_store = PairLockStore(state_store=state_store)
        self._risk_state = "normal"
        self._kill_switch = {
            "enabled": False,
            "enabled_at": False,
            "actor_id": False,
            "reason": False,
        }
        if self.state_store:
            state = self.state_store.load()
            risk_state = state.get("risk_state")
            if risk_state in RISK_STATES:
                self._risk_state = risk_state
            kill_switch = state.get("kill_switch")
            if isinstance(kill_switch, dict):
                self._kill_switch.update(kill_switch)
        self._signal_decisions: dict[str, dict] = {}

    def set_risk_state(self, risk_state: str) -> None:
        if risk_state not in RISK_STATES:
            raise ValueError(f"invalid risk state: {risk_state}")
        self._risk_state = risk_state
        self._persist_state()

    def get_risk_state(self) -> str:
        if self._kill_switch["enabled"]:
            return "kill_switch_enabled"
        return self._risk_state

    def enable_kill_switch(self, actor_id: str, reason: str) -> dict:
        self._kill_switch = {
            "enabled": True,
            "enabled_at": _now_utc().isoformat(),
            "actor_id": actor_id,
            "reason": reason,
        }
        self._risk_state = "kill_switch_enabled"
        self._persist_state()
        return dict(self._kill_switch)

    def disable_kill_switch(self) -> dict:
        self._kill_switch["enabled"] = False
        self._risk_state = "blocked_new_entries"
        self._persist_state()
        return dict(self._kill_switch)

    def overview(self) -> dict:
        return {
            "risk_state": self.get_risk_state(),
            "policy": self.policy.to_dict(),
            "kill_switch": dict(self._kill_switch),
            "pair_locks": self.pair_lock_store.list_active(),
        }

    def precheck(self, signal: dict, account: dict) -> dict:
        reason_codes: list[str] = []
        side = signal.get("side")
        action = signal.get("action", "entry")
        risk_state = self.get_risk_state()

        if action in REDUCING_ACTIONS:
            result = self._result("approved", risk_state, reason_codes, signal, account)
            self._remember_signal_decision(signal, account, result)
            return result

        if risk_state == "kill_switch_enabled":
            reason_codes.append("kill_switch_enabled")
        if risk_state in {"blocked_new_entries", "live_readonly"}:
            reason_codes.append("risk_state_blocks_new_entries")

        self._check_signal_age(signal, reason_codes)
        self._check_price_geometry(signal, reason_codes)
        self._check_leverage(signal, reason_codes)
        self._check_liquidation_buffer(signal, reason_codes)
        self._check_live_rules(signal, reason_codes)
        self._check_pair_lock(signal, reason_codes)
        self._check_account_risk(signal, account, reason_codes)

        if side not in {"long", "short"}:
            reason_codes.append("side_invalid")

        decision = "approved"
        next_state = risk_state
        if reason_codes:
            decision = "blocked"
        if "daily_loss_exceeded" in reason_codes:
            next_state = "blocked_new_entries"
            self._risk_state = "blocked_new_entries"
            self._persist_state()
        if self._kill_switch["enabled"]:
            next_state = "kill_switch_enabled"

        result = self._result(decision, next_state, reason_codes, signal, account)
        self._remember_signal_decision(signal, account, result)
        return result

    def explain(self, signal_id: str) -> dict:
        decision = self._signal_decisions.get(signal_id)
        if not decision:
            return {"status": "not_found"}
        result = decision["result"]
        if result["decision"] != "blocked":
            return {"status": "not_blocked"}
        reason_codes = result.get("reason_codes", [])
        rule_name = reason_codes[0] if reason_codes else "unknown"
        explanation = self._explain_rule(rule_name, decision["signal"], decision["account"], result)
        return {"status": "blocked", "rule_name": rule_name, **explanation}

    def _remember_signal_decision(self, signal: dict, account: dict, result: dict) -> None:
        signal_id = signal.get("signal_id")
        if not signal_id:
            return
        self._signal_decisions[str(signal_id)] = {
            "signal": dict(signal),
            "account": dict(account),
            "result": dict(result),
        }

    def _explain_rule(self, rule_name: str, signal: dict, account: dict, result: dict) -> dict:
        equity = account.get("equity", 0)
        trade_risk_pct = self._single_trade_risk_pct(signal, equity) if equity > 0 else 0.0
        pair = signal.get("pair")
        notional = signal.get("notional", 0)
        new_exposure_pct = notional / equity * 100 if equity > 0 and notional > 0 else 0.0
        symbol_exposure = account.get("symbol_exposure_pct", {})
        pair_groups = account.get("pair_groups", {})
        group = pair_groups.get(pair)
        group_exposure = account.get("correlated_group_exposure_pct", {})

        explanations = {
            "leverage_exceeded": self._numeric_explanation(
                signal.get("leverage", self.policy.default_leverage),
                self.policy.max_leverage,
            ),
            "liquidation_buffer_too_low": self._numeric_explanation(
                signal.get("liquidation_buffer_pct", 0),
                self.policy.min_liquidation_buffer_pct,
                minimum=True,
            ),
            "single_trade_risk_exceeded": self._numeric_explanation(
                trade_risk_pct,
                self.policy.max_single_trade_risk_pct,
            ),
            "total_open_risk_exceeded": self._numeric_explanation(
                account.get("open_risk_pct", 0) + trade_risk_pct,
                self.policy.max_total_open_risk_pct,
            ),
            "daily_loss_exceeded": self._numeric_explanation(
                account.get("daily_realized_loss_pct", 0),
                self.policy.max_daily_realized_loss_pct,
            ),
            "symbol_exposure_exceeded": self._numeric_explanation(
                symbol_exposure.get(pair, 0) + new_exposure_pct,
                self.policy.max_symbol_exposure_pct,
            ),
            "correlated_group_exposure_exceeded": self._numeric_explanation(
                group_exposure.get(group, 0) + new_exposure_pct,
                self.policy.max_correlated_group_exposure_pct,
            ),
            "signal_too_old": self._signal_age_explanation(signal),
            "kill_switch_enabled": {
                "current_value": self.get_risk_state(),
                "limit": "kill_switch_disabled",
                "gap_to_allow": 1,
            },
            "risk_state_blocks_new_entries": {
                "current_value": self.get_risk_state(),
                "limit": "normal_or_warning",
                "gap_to_allow": 1,
            },
            "pair_locked": {
                "current_value": len(self.pair_lock_store.active_for_pair(signal.get("pair"))),
                "limit": 0,
                "gap_to_allow": len(self.pair_lock_store.active_for_pair(signal.get("pair"))),
            },
            "price_geometry_invalid": {
                "current_value": self._price_geometry_value(signal),
                "limit": "valid_price_geometry",
                "gap_to_allow": 1,
            },
            "side_invalid": {
                "current_value": signal.get("side"),
                "limit": "long_or_short",
                "gap_to_allow": 1,
            },
            "equity_invalid": self._numeric_explanation(
                account.get("equity", 0),
                0,
                minimum=True,
            ),
            "live_stop_loss_required": {
                "current_value": bool(signal.get("stop_loss")),
                "limit": True,
                "gap_to_allow": 1,
            },
            "live_take_profit_requires_manual_approval": {
                "current_value": bool(signal.get("take_profits") or signal.get("manual_take_profit_approval", False)),
                "limit": True,
                "gap_to_allow": 1,
            },
        }
        return explanations.get(
            rule_name,
            {
                "current_value": result.get("decision"),
                "limit": "approved",
                "gap_to_allow": 1,
            },
        )

    def _numeric_explanation(self, current_value: float, limit: float, minimum: bool = False) -> dict:
        if minimum:
            gap = max(limit - current_value, 0)
        else:
            gap = max(current_value - limit, 0)
        return {
            "current_value": round(current_value, 4) if isinstance(current_value, float) else current_value,
            "limit": limit,
            "gap_to_allow": round(gap, 4) if isinstance(gap, float) else gap,
        }

    def _signal_age_explanation(self, signal: dict) -> dict:
        received_at = _parse_datetime(signal.get("received_at"))
        age_minutes = 0.0
        if received_at:
            age_minutes = (_now_utc() - received_at).total_seconds() / 60
        return self._numeric_explanation(age_minutes, self.policy.signal_max_age_minutes)

    def _price_geometry_value(self, signal: dict) -> dict:
        take_profits = signal.get("take_profits", [])
        first_take_profit = take_profits[0] if take_profits else None
        return {
            "side": signal.get("side"),
            "entry_price": signal.get("entry_price"),
            "stop_loss": signal.get("stop_loss"),
            "first_take_profit": first_take_profit,
        }

    def _persist_state(self) -> None:
        if not self.state_store:
            return
        self.state_store.update(
            {
                "risk_state": self._risk_state,
                "kill_switch": self._kill_switch,
            }
        )

    def _check_signal_age(self, signal: dict, reason_codes: list[str]) -> None:
        received_at = _parse_datetime(signal.get("received_at"))
        if not received_at:
            return
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        age = _now_utc() - received_at
        if age.total_seconds() > self.policy.signal_max_age_minutes * 60:
            reason_codes.append("signal_too_old")

    def _check_price_geometry(self, signal: dict, reason_codes: list[str]) -> None:
        side = signal.get("side")
        entry_price = signal.get("entry_price")
        stop_loss = signal.get("stop_loss")
        take_profits = signal.get("take_profits", [])
        if not entry_price or not stop_loss or not take_profits:
            return
        first_tp = take_profits[0]
        if side == "long":
            if stop_loss < entry_price and entry_price < first_tp:
                return
            reason_codes.append("price_geometry_invalid")
            return
        if side == "short":
            if first_tp < entry_price and entry_price < stop_loss:
                return
            reason_codes.append("price_geometry_invalid")

    def _check_leverage(self, signal: dict, reason_codes: list[str]) -> None:
        leverage = signal.get("leverage", self.policy.default_leverage)
        if leverage > self.policy.max_leverage:
            reason_codes.append("leverage_exceeded")

    def _check_liquidation_buffer(self, signal: dict, reason_codes: list[str]) -> None:
        buffer_pct = signal.get("liquidation_buffer_pct")
        if buffer_pct is False:
            return
        if buffer_pct is None:
            return
        if buffer_pct < self.policy.min_liquidation_buffer_pct:
            reason_codes.append("liquidation_buffer_too_low")

    def _check_live_rules(self, signal: dict, reason_codes: list[str]) -> None:
        run_mode = signal.get("run_mode", "dry_run")
        if run_mode != "live":
            return
        stop_loss = signal.get("stop_loss")
        if not stop_loss and not self.policy.allow_live_without_stop_loss:
            reason_codes.append("live_stop_loss_required")
        take_profits = signal.get("take_profits", [])
        manual_approval = signal.get("manual_take_profit_approval", False)
        if take_profits:
            return
        if self.policy.allow_live_without_take_profit:
            return
        if manual_approval:
            return
        reason_codes.append("live_take_profit_requires_manual_approval")

    def _check_pair_lock(self, signal: dict, reason_codes: list[str]) -> None:
        pair = signal.get("pair")
        if not pair:
            return
        locks = self.pair_lock_store.active_for_pair(pair)
        if locks:
            reason_codes.append("pair_locked")

    def _check_account_risk(self, signal: dict, account: dict, reason_codes: list[str]) -> None:
        equity = account.get("equity", 0)
        if equity <= 0:
            reason_codes.append("equity_invalid")
            return
        trade_risk_pct = self._single_trade_risk_pct(signal, equity)
        if trade_risk_pct > self.policy.max_single_trade_risk_pct:
            reason_codes.append("single_trade_risk_exceeded")

        open_risk_pct = account.get("open_risk_pct", 0)
        if open_risk_pct + trade_risk_pct > self.policy.max_total_open_risk_pct:
            reason_codes.append("total_open_risk_exceeded")

        daily_loss = account.get("daily_realized_loss_pct", 0)
        if daily_loss >= self.policy.max_daily_realized_loss_pct:
            reason_codes.append("daily_loss_exceeded")

        pair = signal.get("pair")
        notional = signal.get("notional", 0)
        new_exposure_pct = 0.0
        if notional > 0:
            new_exposure_pct = notional / equity * 100
        symbol_exposure = account.get("symbol_exposure_pct", {})
        if pair and symbol_exposure.get(pair, 0) + new_exposure_pct > self.policy.max_symbol_exposure_pct:
            reason_codes.append("symbol_exposure_exceeded")

        pair_groups = account.get("pair_groups", {})
        group = pair_groups.get(pair)
        group_exposure = account.get("correlated_group_exposure_pct", {})
        if group and group_exposure.get(group, 0) + new_exposure_pct > self.policy.max_correlated_group_exposure_pct:
            reason_codes.append("correlated_group_exposure_exceeded")

    def _single_trade_risk_pct(self, signal: dict, equity: float) -> float:
        entry_price = signal.get("entry_price")
        stop_loss = signal.get("stop_loss")
        notional = signal.get("notional", 0)
        if not entry_price or not stop_loss or notional <= 0:
            return 0.0
        price_risk = abs(entry_price - stop_loss) / entry_price
        risk_amount = notional * price_risk
        return risk_amount / equity * 100

    def _result(
        self,
        decision: str,
        risk_state: str,
        reason_codes: list[str],
        signal: dict,
        account: dict,
    ) -> dict:
        single_trade_risk = 0.0
        equity = account.get("equity", 0)
        if equity > 0:
            single_trade_risk = self._single_trade_risk_pct(signal, equity)
        return {
            "decision": decision,
            "risk_state": risk_state,
            "reason_codes": reason_codes,
            "single_trade_risk_usage_pct": round(single_trade_risk, 4),
            "total_open_risk_usage_pct": account.get("open_risk_pct", 0),
            "daily_loss_usage_pct": account.get("daily_realized_loss_pct", 0),
        }
