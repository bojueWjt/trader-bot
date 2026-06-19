from __future__ import annotations

import logging
import os
import re
import json
import sqlite3
from time import perf_counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from pandas import DataFrame, Timestamp
except ModuleNotFoundError:
    DataFrame = Any

    class Timestamp:
        pass

try:
    from freqtrade.persistence import Order, Trade
except ModuleNotFoundError:
    Order = Any
    Trade = Any

try:
    from freqtrade.strategy import IStrategy, stoploss_from_absolute
except ModuleNotFoundError:
    def stoploss_from_absolute(
        stop_rate: float,
        current_rate: float,
        is_short: bool = False,
        leverage: float = 1.0,
    ) -> float:
        if current_rate == 0:
            return 1
        stoploss = 1.0 - stop_rate / current_rate
        if is_short:
            stoploss = -stoploss
        return max(stoploss, 0.0) * leverage


    class IStrategy:
        INTERFACE_VERSION = 3

        def __init__(self, config: dict[str, Any]) -> None:
            self.config = config
            self.wallets = False

logger = logging.getLogger(__name__)

try:
    from freqtrade.signal_strategy import is_kill_switch_enabled as runtime_kill_switch_enabled
    from freqtrade.signal_strategy.domain import SignalStatus
    from freqtrade.signal_strategy.store import make_signal_store_from_url
    from freqtrade.signal_strategy.store import InMemorySignalStore as SignalStore
except ImportError:
    runtime_kill_switch_enabled = False
    make_signal_store_from_url = False
    SignalStatus = False
    SignalStore = False


class SignalStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "5m"
    can_short = True
    use_custom_stoploss = True
    position_adjustment_enable = True
    minimal_roi = {"0": 100}
    stoploss = -0.99
    startup_candle_count = 0
    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "limit",
        "stoploss_on_exchange": False,
    }
    order_time_in_force = {
        "entry": "GTC",
        "exit": "GTC",
    }

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        strategy_config = self._strategy_config()
        self.lookback_minutes = int(strategy_config.get("lookback_minutes", 240))
        self.default_risk_pct = float(strategy_config.get("risk_pct", 0.01))
        self.default_equity = float(strategy_config.get("equity", 0.0))
        self.risk_policy = self._load_risk_policy(strategy_config)
        self.max_entry_distance_pct = float(strategy_config.get("max_entry_distance_pct", 0.03))
        self.unfilled_timeout_seconds = int(strategy_config.get("unfilled_timeout_seconds", 300))
        self.adjust_entry_price_deviation_pct = float(
            strategy_config.get("adjust_entry_price_deviation_pct", self.max_entry_distance_pct)
        )
        self.default_leverage = float(strategy_config.get("default_leverage", 3.0))
        self.min_strategy_leverage = float(strategy_config.get("min_leverage", 2.0))
        self.max_strategy_leverage = float(strategy_config.get("max_leverage", 5.0))
        self.max_total_open_risk_pct = float(strategy_config.get("max_total_open_risk_pct", 100.0))
        self.max_symbol_exposure_pct = float(strategy_config.get("max_symbol_exposure_pct", 100.0))
        self.min_liquidation_buffer_pct = float(
            strategy_config.get("min_liquidation_buffer_pct", 0.0)
        )
        # Per-account channel routing: when account_id is set, this instance only
        # trades signals whose source channel is routed to it in channel_routing.
        # Empty account_id keeps the legacy behaviour (trade every channel) so
        # dry-run/back-test/unit tests are unaffected.
        self.account_id = str(strategy_config.get("account_id", "")).strip()
        self.routing_db_path = str(
            strategy_config.get("routing_db_path")
            or os.environ.get("TRADING_DB_PATH", "")
            or "/data/watcher-trading.db"
        )
        self.trade_unrouted = bool(strategy_config.get("trade_unrouted", False))
        self.signal_store = self._make_signal_store(strategy_config)
        self.signals_by_id: dict[str, dict[str, Any]] = {}
        self._entry_signals_by_pair: dict[str, list[dict[str, Any]]] = {}
        self._store_available = True
        self._dca_fills: set[tuple[Any, float]] = set()
        self._dca_fill_events: set[str] = set()
        self._tp_exits: set[tuple[Any, str, int]] = set()

    def _strategy_config(self) -> dict[str, Any]:
        raw_config = self.config.get("signal_strategy", {})
        if isinstance(raw_config, dict):
            return raw_config
        return {}

    def _load_risk_policy(self, strategy_config: dict[str, Any]) -> dict[str, Any]:
        policy: dict[str, Any] = {}
        policy_path = Path(__file__).resolve().parents[2] / "fixtures" / "risk" / "default_policy.json"
        try:
            with policy_path.open("r", encoding="utf-8") as handle:
                loaded_policy = json.load(handle)
            if isinstance(loaded_policy, dict):
                policy = loaded_policy
        except (OSError, json.JSONDecodeError):
            policy = {}

        configured_policy = strategy_config.get("risk_policy")
        if isinstance(configured_policy, dict):
            policy = self._deep_merge_dict(policy, configured_policy)

        configured_breakeven = strategy_config.get("breakeven_stoploss")
        if isinstance(configured_breakeven, dict):
            policy = self._deep_merge_dict(policy, {"breakeven_stoploss": configured_breakeven})
        return policy

    def _deep_merge_dict(self, base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        result = dict(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = self._deep_merge_dict(result[key], value)
                continue
            result[key] = value
        return result

    def _make_signal_store(self, strategy_config: dict[str, Any]) -> Any:
        configured_store = strategy_config.get("store")
        if configured_store:
            return configured_store
        store_url = strategy_config.get("store_url")
        if not store_url:
            store_url = self.config.get("HERMES_SIGNAL_STORE_URL")
        if not store_url:
            store_url = self.config.get("hermes_signal_store_url")
        if not store_url:
            store_url = os.environ.get("HERMES_SIGNAL_STORE_URL", "")
        if isinstance(store_url, str):
            store_url = os.path.expandvars(store_url)
        if store_url and make_signal_store_from_url:
            return make_signal_store_from_url(str(store_url))
        if SignalStore:
            risk_policy = strategy_config.get("risk_policy")
            if risk_policy:
                return SignalStore(risk_policy=risk_policy)
            return SignalStore()
        return False

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        if not self.signal_store:
            self._store_available = False
            self._entry_signals_by_pair = {}
            self._record_risk_event(
                "signal_store_unavailable",
                level="error",
                current_time=current_time,
            )
            return

        self._expire_stale_order_intents(current_time)
        self._monitor_open_entry_orders(current_time)

        start = perf_counter()
        try:
            signals = self._load_approved_signals(current_time)
        except TimeoutError as exc:
            logger.warning("Signal store timed out: %s", exc)
            elapsed = perf_counter() - start
            self._record_signal_store_failure(
                "signal_store_timeout",
                current_time=current_time,
                error=str(exc),
                elapsed=elapsed,
            )
            return
        except Exception as exc:
            logger.warning("Signal store unavailable: %s", exc)
            elapsed = perf_counter() - start
            event_type = "signal_store_unavailable"
            if elapsed > 2.0:
                event_type = "signal_store_timeout"
            self._record_signal_store_failure(
                event_type,
                current_time=current_time,
                error=str(exc),
                elapsed=elapsed,
            )
            return
        elapsed = perf_counter() - start
        if elapsed > 2.0:
            self._record_signal_store_failure(
                "signal_store_timeout",
                current_time=current_time,
                elapsed=elapsed,
            )
            return

        self._store_available = True
        self._entry_signals_by_pair = {}
        signals = self._filter_signals_for_account(signals)
        for raw_signal in signals:
            signal = self._normalize_signal(raw_signal)
            signal_id = self._signal_id(signal)
            if not signal_id:
                continue
            if self._string_value(signal, "status") == "expired":
                continue
            self.signals_by_id[signal_id] = signal
            if not self._is_entry_eligible(signal, current_time):
                continue
            pair = self._pair(signal)
            if not pair:
                continue
            pair_signals = self._entry_signals_by_pair.setdefault(pair, [])
            pair_signals.append(signal)

    def _record_signal_store_failure(
        self,
        event_type: str,
        current_time: datetime,
        elapsed: float,
        error: str = "",
    ) -> None:
        self._store_available = False
        self._entry_signals_by_pair = {}
        level = "error"
        if event_type == "signal_store_timeout":
            level = "warning"
        self._record_risk_event(
            event_type,
            level=level,
            current_time=current_time,
            elapsed=elapsed,
            error=error,
        )

    def _load_approved_signals(self, current_time: datetime) -> list[Any]:
        store = self.signal_store
        if hasattr(store, "get_approved_signals"):
            return store.get_approved_signals(
                since_minutes=self.lookback_minutes,
                current_time=current_time,
            )
        if hasattr(store, "load_approved"):
            return store.load_approved(
                since_minutes=self.lookback_minutes,
                current_time=current_time,
            )
        if hasattr(store, "list_approved"):
            return store.list_approved(
                since_minutes=self.lookback_minutes,
                current_time=current_time,
            )
        if hasattr(store, "_signals"):
            signals = []
            for signal in store._signals.values():
                signals.append(signal)
            return signals
        raise RuntimeError("signal store has no approved-signal loader")

    def _load_channel_routing(self) -> dict[str, str]:
        """Read channel_id -> target_account_id from the watcher trading DB.

        Read-only, re-read every loop so changes made in the watcher UI take
        effect live. On any failure return {} — with account_id set and
        trade_unrouted False this halts trading (safe) rather than misrouting
        real money to the wrong account.
        """
        path = self.routing_db_path
        if not path or not os.path.exists(path):
            logger.warning("channel routing db missing: %s", path)
            return {}
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error as exc:
            logger.warning("channel routing db open failed: %s", exc)
            return {}
        try:
            cursor = connection.execute(
                "SELECT channel_id, target_account_id FROM channel_routing"
            )
            routing: dict[str, str] = {}
            for channel_id, target_account_id in cursor.fetchall():
                if channel_id is None:
                    continue
                routing[str(channel_id)] = str(target_account_id or "").strip()
            return routing
        except sqlite3.Error as exc:
            logger.warning("channel routing read failed: %s", exc)
            return {}
        finally:
            connection.close()

    def _filter_signals_for_account(self, signals: list[Any]) -> list[Any]:
        """Keep only signals whose source channel is routed to this account."""
        if not self.account_id:
            return signals
        routing = self._load_channel_routing()
        kept: list[Any] = []
        skipped_unrouted = 0
        skipped_other = 0
        for signal in signals:
            channel_id = self._string_value(signal, "source_channel_id", "channel_id")
            target = routing.get(channel_id) if channel_id else None
            if not target:
                if self.trade_unrouted:
                    kept.append(signal)
                else:
                    skipped_unrouted += 1
                continue
            if target == self.account_id:
                kept.append(signal)
            else:
                skipped_other += 1
        if skipped_unrouted or skipped_other:
            logger.info(
                "channel routing %s: kept=%d skipped_unrouted=%d skipped_other=%d",
                self.account_id,
                len(kept),
                skipped_unrouted,
                skipped_other,
            )
        return kept

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if "exit_long" not in dataframe:
            dataframe["exit_long"] = 0
        if "exit_short" not in dataframe:
            dataframe["exit_short"] = 0
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        self._ensure_entry_columns(dataframe)
        if not self._store_available:
            return dataframe

        pair = self._normalize_pair(metadata.get("pair"))
        if not pair:
            return dataframe

        signals = self._entry_signals_by_pair.get(pair, [])
        if not signals:
            return dataframe

        blocked_reason = self._entry_block_reason()
        if blocked_reason:
            self._record_risk_event(
                "entry_blocked",
                level="warning",
                pair=pair,
                reason=blocked_reason,
            )
            return dataframe

        current_time = self._dataframe_current_time(dataframe)
        candle_delta = self._timeframe_delta()
        selected = self._select_latest_signal_per_candle(dataframe, signals, current_time, candle_delta)
        for row_index, signal in selected.items():
            side = self._side(signal)
            tag = self._entry_tag(signal)
            reserved = self._reserve_entry_signal(signal, pair, current_time)
            if not reserved:
                continue
            self._mark_signal_status(
                signal,
                "sent_to_freqtrade",
                "strategy",
                pair=pair,
                entry_tag=tag,
                current_time=current_time,
            )
            if side == "long":
                dataframe.at[row_index, "enter_long"] = 1
                dataframe.at[row_index, "enter_tag"] = tag
            if side == "short":
                dataframe.at[row_index, "enter_short"] = 1
                dataframe.at[row_index, "enter_tag"] = tag

        return dataframe

    def _ensure_entry_columns(self, dataframe: DataFrame) -> None:
        if "enter_long" not in dataframe:
            dataframe["enter_long"] = 0
        if "enter_short" not in dataframe:
            dataframe["enter_short"] = 0
        if "enter_tag" not in dataframe:
            dataframe["enter_tag"] = ""

    def _select_latest_signal_per_candle(
        self,
        dataframe: DataFrame,
        signals: list[dict[str, Any]],
        current_time: datetime,
        candle_delta: timedelta,
    ) -> dict[Any, dict[str, Any]]:
        selected: dict[Any, dict[str, Any]] = {}
        for signal in signals:
            if not self._is_entry_eligible(signal, current_time):
                continue
            approved_at = self._datetime_value(signal, "approved_at", "approved_time", "received_at")
            if not approved_at:
                continue
            row_index = self._row_index_for_time(dataframe, approved_at, candle_delta)
            if row_index is False:
                continue
            existing = selected.get(row_index)
            if not existing:
                selected[row_index] = signal
                continue
            existing_approved_at = self._datetime_value(
                existing,
                "approved_at",
                "approved_time",
                "received_at",
            )
            if not existing_approved_at:
                self._record_signal_conflict(signal, existing)
                selected[row_index] = signal
                continue
            if approved_at > existing_approved_at:
                self._record_signal_conflict(signal, existing)
                selected[row_index] = signal
                continue
            self._record_signal_conflict(existing, signal)
        return selected

    def _row_index_for_time(self, dataframe: DataFrame, target_time: datetime, candle_delta: timedelta) -> Any:
        if "date" not in dataframe:
            if dataframe.empty:
                return False
            return dataframe.index[-1]

        found_index = False
        found_date = False
        for row_index, row_date in dataframe["date"].items():
            candle_start = self._coerce_datetime(row_date)
            if not candle_start:
                continue
            candle_end = candle_start + candle_delta
            if candle_start <= target_time < candle_end:
                return row_index
            if candle_start <= target_time:
                found_index = row_index
                found_date = candle_start

        if found_index is False:
            return False
        if not found_date:
            return False
        if target_time < found_date + candle_delta:
            return found_index
        return False

    def custom_entry_price(
        self,
        pair: str,
        trade: Trade | None,
        current_time: datetime,
        proposed_rate: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float | bool:
        signal = self._signal_from_context(pair, entry_tag, trade)
        if not signal:
            return proposed_rate
        if proposed_rate <= 0:
            return False

        price, source = self._resolve_entry_price(signal, proposed_rate)

        distance = abs(price - proposed_rate) / proposed_rate
        if distance > self.max_entry_distance_pct:
            signal_id = self._signal_id(signal)
            self._record_risk_event(
                "entry_price_too_far",
                signal_id=signal_id,
                proposed_rate=proposed_rate,
                entry_price=price,
                entry_price_source=source,
                pair=pair,
            )
            return False
        return float(price)

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> bool:
        signal = self._signal_from_context(pair, entry_tag, None)
        if not signal:
            return True

        leverage = self._float_value(kwargs, "leverage")
        if leverage is False:
            leverage = self._signal_leverage(signal)

        reason = self._entry_reject_reason(signal, pair, side, amount, rate, leverage)
        if not reason:
            return True

        signal_id = self._signal_id(signal)
        self._reject_signal(
            signal,
            reason,
            actor="strategy",
            pair=self._normalize_pair(pair),
            entry_price=rate,
            side=side,
            current_time=current_time,
        )
        self._record_risk_event(
            reason,
            level="warning",
            signal_id=signal_id,
            pair=self._normalize_pair(pair),
            entry_price=rate,
            side=side,
        )
        self._record_audit_event(
            "trade_entry_rejected",
            signal_id,
            pair=self._normalize_pair(pair),
            reason=reason,
            current_time=current_time,
        )
        return False

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float | bool:
        signal = self._signal_from_context(pair, entry_tag, None)
        if not signal:
            return proposed_stake

        stop_loss = self._float_value(signal, "stop_loss", "sl")
        if stop_loss is False:
            self._record_risk_event(
                "stop_loss_missing",
                signal_id=self._signal_id(signal),
                pair=pair,
                entry_price=current_rate,
            )
            return False

        entry_price, entry_price_source = self._resolve_entry_price(signal, current_rate)
        stop_distance = abs(entry_price - stop_loss)
        signal_id = self._signal_id(signal)
        if stop_distance == 0:
            self._record_risk_event(
                "zero_stop_distance",
                signal_id=signal_id,
                pair=pair,
                entry_price=entry_price,
                entry_price_source=entry_price_source,
                stop_loss=stop_loss,
            )
            return False

        equity = self._equity()
        risk_pct = self._risk_pct(signal)
        risk_amount = equity * risk_pct
        quantity = risk_amount / stop_distance
        stake = quantity * entry_price / leverage
        self._record_risk_event(
            "stake_sized",
            level="info",
            signal_id=signal_id,
            pair=self._normalize_pair(pair),
            entry_price=entry_price,
            entry_price_source=entry_price_source,
            stop_distance=stop_distance,
            risk_amount=risk_amount,
            quantity=quantity,
        )
        return self._clamp_stake(stake, min_stake, max_stake)

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        signal = self._signal_from_context(pair, entry_tag, None)
        policy_max = self.max_strategy_leverage
        if signal:
            policy = self._dict_value(signal, "policy")
            value = self._float_value(policy, "max_leverage")
            if value is not False:
                policy_max = value
            else:
                leverage_plan = self._raw_value(signal, "leverage")
                plan_max = self._float_value(leverage_plan, "max")
                if plan_max is not False:
                    policy_max = plan_max

        leverage = self.default_leverage
        leverage = max(leverage, self.min_strategy_leverage)
        leverage = min(leverage, self.max_strategy_leverage)
        leverage = min(leverage, policy_max)
        leverage = min(leverage, max_leverage)
        return float(leverage)

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float | None:
        if not self._entry_block_reason():
            directive_stoploss = self._apply_stoploss_directive(pair, trade, current_rate, current_profit)
            if directive_stoploss is not False:
                return directive_stoploss

        signal = self._signal_from_context(pair, None, trade)
        if not signal:
            return self.stoploss

        stop_loss = self._float_value(signal, "stop_loss", "sl")
        if stop_loss is False:
            return self.stoploss

        leverage = self._trade_leverage(trade)
        is_short = self._trade_is_short(trade)
        stop_loss = self._breakeven_adjusted_stop_loss(
            pair,
            trade,
            signal,
            stop_loss,
            current_profit,
        )
        return stoploss_from_absolute(stop_loss, current_rate, is_short=is_short, leverage=leverage)

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> str | bool | None:
        close_directive = self._first_pending_directive(pair, "close")
        if close_directive:
            self._consume_directive(close_directive)
            return "directive_close"

        signal = self._signal_from_context(pair, None, trade)
        if not signal:
            return None

        is_short = self._trade_is_short(trade)
        schedule = self._take_profit_schedule(signal)
        for level_index, take_profit_level in enumerate(schedule, start=1):
            take_profit = self._float_value(take_profit_level, "price", "primary_price", "target")
            if take_profit is False:
                continue

            if is_short and current_rate <= take_profit:
                recorded = self._record_take_profit_event(
                    trade,
                    signal,
                    take_profit_level,
                    level_index,
                    current_time,
                    current_rate,
                )
                if recorded:
                    return "trade_exit_event/signal_take_profit"
                continue

            if not is_short and current_rate >= take_profit:
                recorded = self._record_take_profit_event(
                    trade,
                    signal,
                    take_profit_level,
                    level_index,
                    current_time,
                    current_rate,
                )
                if recorded:
                    return "trade_exit_event/signal_take_profit"
        return None

    def adjust_trade_position(
        self,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        min_stake: float | None,
        max_stake: float,
        current_entry_rate: float,
        current_exit_rate: float,
        current_entry_profit: float,
        current_exit_profit: float,
        **kwargs,
    ) -> float | None | tuple[float | None, str | None]:
        partial_directive = self._first_pending_directive(
            getattr(trade, "pair", ""),
            "partial_close",
        )
        if partial_directive:
            stake_amount = self._float_value(trade, "stake_amount")
            if stake_amount is not False:
                fraction = self._directive_fraction(partial_directive)
                self._consume_directive(partial_directive)
                return -(stake_amount * fraction)

        signal = self._signal_from_context(getattr(trade, "pair", ""), None, trade)
        if not signal:
            return None

        levels = self._list_value(signal, "dca", "dca_levels")
        if not levels:
            entry = self._entry(signal)
            dca_prices = self._list_value(entry, "dca_prices")
            levels = []
            for price in dca_prices:
                levels.append({"price": price})
        if not levels:
            return None

        signal_id = self._signal_id(signal)
        is_short = self._trade_is_short(trade)
        for index, level in enumerate(levels, start=1):
            price = self._float_value(level, "price", "primary_price")
            if price is False:
                continue
            dca_key = self._dca_key(trade, price)
            if dca_key in self._dca_fills:
                self._record_audit_event(
                    "dca_rejected",
                    signal_id,
                    pair=getattr(trade, "pair", ""),
                    reason="duplicate_dca_price",
                    trade_id=self._trade_id(trade),
                    dca_price=price,
                    current_rate=current_rate,
                    current_time=current_time,
                )
                continue
            if not self._dca_level_reached(current_rate, price, is_short):
                continue
            stake = self._dca_stake(signal, level, current_rate, min_stake, max_stake)
            if stake is False:
                self._record_audit_event(
                    "dca_rejected",
                    signal_id,
                    pair=getattr(trade, "pair", ""),
                    reason="dca_stake_rejected",
                    trade_id=self._trade_id(trade),
                    dca_price=price,
                    current_rate=current_rate,
                    current_time=current_time,
                )
                continue
            if isinstance(stake, str):
                self._record_audit_event(
                    "dca_rejected",
                    signal_id,
                    pair=getattr(trade, "pair", ""),
                    reason=stake,
                    trade_id=self._trade_id(trade),
                    dca_price=price,
                    current_rate=current_rate,
                    current_time=current_time,
                )
                continue
            risk_reason = self._dca_risk_reject_reason(signal, trade, level, stake, current_rate)
            if risk_reason:
                self._record_risk_event(
                    risk_reason,
                    level="warning",
                    signal_id=signal_id,
                    pair=getattr(trade, "pair", ""),
                    trade_id=self._trade_id(trade),
                    dca_price=price,
                    stake=stake,
                    current_rate=current_rate,
                    current_time=current_time,
                )
                self._record_audit_event(
                    "dca_rejected",
                    signal_id,
                    pair=getattr(trade, "pair", ""),
                    reason=risk_reason,
                    trade_id=self._trade_id(trade),
                    dca_price=price,
                    stake=stake,
                    current_rate=current_rate,
                    current_time=current_time,
                )
                return None
            self._dca_fills.add(dca_key)
            self._record_audit_event(
                "dca_triggered",
                signal_id,
                pair=getattr(trade, "pair", ""),
                trade_id=self._trade_id(trade),
                dca_price=price,
                stake=stake,
                current_rate=current_rate,
                current_time=current_time,
            )
            return (stake, f"sig:{signal_id}:dca:{index}")

        return None

    def _dca_key(self, trade: Trade, price: float) -> tuple[Any, float]:
        return (self._trade_id(trade), float(price))

    def _dca_level_reached(self, current_rate: float, price: float, is_short: bool) -> bool:
        if is_short:
            return current_rate >= price
        return current_rate <= price

    def _dca_stake(
        self,
        signal: dict[str, Any],
        level: Any,
        current_rate: float,
        min_stake: float | None,
        max_stake: float,
    ) -> float | bool:
        stop_loss = self._float_value(signal, "stop_loss", "sl")
        if stop_loss is False:
            return "stop_loss_missing"
        stop_distance = abs(current_rate - stop_loss)
        if stop_distance == 0:
            self._record_risk_event(
                "zero_stop_distance",
                signal_id=self._signal_id(signal),
                entry_price=current_rate,
                stop_loss=stop_loss,
            )
            return False
        equity = self._equity()
        risk_pct = self._float_value(level, "risk_pct", "risk_percent")
        if risk_pct is False:
            risk_pct = self._risk_pct(signal)
        leverage = self._signal_leverage(signal)
        quantity = equity * risk_pct / stop_distance
        stake = quantity * current_rate / leverage
        return self._clamp_stake(stake, min_stake, max_stake)

    def order_filled(
        self,
        pair: str,
        trade: Trade,
        order: Order,
        current_time: datetime,
        **kwargs,
    ) -> None:
        order_tag = kwargs.get("order_tag")
        signal = self._signal_from_context(pair, order_tag, trade)
        if not signal:
            return

        signal_id = self._signal_id(signal)
        event_type = "entered"
        trade_id = self._trade_id(trade)
        dca_price = False
        if isinstance(order_tag, str) and ":dca:" in order_tag:
            event_type = "dca_filled"
            dca_price = self._dca_price_from_order_tag(signal, order_tag)
        if event_type == "dca_filled":
            self._record_dca_fill_event(
                signal,
                signal_id,
                pair,
                trade,
                trade_id,
                dca_price,
                current_time,
                order_tag,
            )
            return
        self._record_audit_event(
            event_type,
            signal_id,
            pair=pair,
            trade_id=trade_id,
            dca_price=dca_price,
            current_time=current_time,
            order_tag=order_tag,
        )
        if event_type == "entered":
            self._mark_signal_status(
                signal,
                "entered",
                "strategy",
                pair=pair,
                trade_id=trade_id,
                current_time=current_time,
            )

    def _record_dca_fill_event(
        self,
        signal: dict[str, Any],
        signal_id: str,
        pair: str,
        trade: Trade,
        trade_id: Any,
        dca_price: float | bool,
        current_time: datetime,
        order_tag: Any,
    ) -> None:
        if dca_price is False:
            self._record_audit_event(
                "dca_rejected",
                signal_id,
                pair=pair,
                reason="dca_price_missing",
                trade_id=trade_id,
                current_time=current_time,
                order_tag=order_tag,
            )
            return

        dca_key = self._dca_key(trade, dca_price)
        idempotency_key = self._dca_fill_idempotency_key(trade, signal_id, dca_price)
        if idempotency_key in self._dca_fill_events:
            return

        self._dca_fill_events.add(idempotency_key)
        self._dca_fills.add(dca_key)
        self._record_audit_event(
            "dca_filled",
            signal_id,
            pair=pair,
            trade_id=trade_id,
            dca_price=dca_price,
            idempotency_key=idempotency_key,
            current_time=current_time,
            order_tag=order_tag,
        )

    def check_entry_timeout(
        self,
        pair: str,
        trade: Trade,
        order: Order,
        current_time: datetime,
        **kwargs,
    ) -> bool:
        result = self._handle_open_entry_order(order, current_time, trade=trade, pair=pair)
        return result == "timeout_cancelled"

    def _expire_stale_order_intents(self, current_time: datetime) -> None:
        store = self.signal_store
        if not store or not hasattr(store, "expire_stale_signals"):
            return
        try:
            store.expire_stale_signals(current_time=current_time, max_age_minutes=24 * 60)
        except TypeError:
            store.expire_stale_signals(current_time=current_time)

    def _monitor_open_entry_orders(self, current_time: datetime) -> None:
        for context in self._open_entry_order_contexts():
            order = context.get("order")
            if not order:
                continue
            trade = context.get("trade")
            pair = context.get("pair")
            self._handle_open_entry_order(order, current_time, trade=trade, pair=pair)

    def _handle_open_entry_order(
        self,
        order: Any,
        current_time: datetime,
        trade: Any = None,
        pair: str | None = None,
    ) -> str:
        if not self._is_open_entry_order(order):
            return "ignored"

        normalized_pair = self._normalize_pair(pair or self._order_pair(order, trade))
        order_tag = self._order_tag(order, trade)
        signal = self._signal_from_context(normalized_pair, order_tag, trade)
        if not signal:
            return "ignored"

        adjusted = self._adjust_entry_price_if_needed(order, signal, current_time, normalized_pair)
        if adjusted:
            return "adjusted"

        order_age_seconds = self._order_age_seconds(order, current_time)
        if order_age_seconds is False:
            return "ignored"
        if self.unfilled_timeout_seconds <= 0:
            return "ignored"
        if order_age_seconds < self.unfilled_timeout_seconds:
            return "ignored"

        signal_id = self._signal_id(signal)
        cancelled = self._cancel_entry_order(order, normalized_pair, reason="unfilled_timeout")
        if not cancelled:
            self._record_audit_event(
                "unfilled_timeout_cancel_failed",
                signal_id,
                pair=normalized_pair,
                order_id=self._order_id(order),
                order_age_seconds=order_age_seconds,
                current_time=current_time,
            )
            return "cancel_failed"

        self._release_signal_reservation(signal_id)
        self._mark_signal_status(
            signal,
            "approved",
            "strategy",
            pair=normalized_pair,
            order_id=self._order_id(order),
            reason="unfilled_timeout",
            order_age_seconds=order_age_seconds,
            current_time=current_time,
        )
        signal["reserved"] = False
        self._record_audit_event(
            "unfilled_timeout_cancelled",
            signal_id,
            pair=normalized_pair,
            order_id=self._order_id(order),
            order_age_seconds=order_age_seconds,
            current_time=current_time,
        )
        return "timeout_cancelled"

    def _adjust_entry_price_if_needed(
        self,
        order: Any,
        signal: dict[str, Any],
        current_time: datetime,
        pair: str,
    ) -> bool:
        if self.adjust_entry_price_deviation_pct <= 0:
            return False

        current_price = self._current_entry_price(order, pair, self._side(signal))
        if current_price is False or current_price <= 0:
            return False

        original_price = self._original_entry_price(signal, order)
        if original_price is False or original_price <= 0:
            return False

        deviation = abs(current_price - original_price) / original_price
        if deviation <= self.adjust_entry_price_deviation_pct:
            return False

        signal_id = self._signal_id(signal)
        cancelled = self._cancel_entry_order(order, pair, reason="adjust_entry_price")
        if not cancelled:
            self._record_audit_event(
                "adjust_entry_price_cancel_failed",
                signal_id,
                pair=pair,
                order_id=self._order_id(order),
                original_price=original_price,
                current_price=current_price,
                deviation=deviation,
                current_time=current_time,
            )
            return False

        placed = self._place_adjusted_entry_order(order, signal, pair, current_price, current_time)
        if not placed:
            self._record_audit_event(
                "adjust_entry_price_replace_failed",
                signal_id,
                pair=pair,
                order_id=self._order_id(order),
                original_price=original_price,
                current_price=current_price,
                deviation=deviation,
                current_time=current_time,
            )
            return False

        self._update_signal_entry_price(signal, current_price)
        self._record_audit_event(
            "adjust_entry_price_replaced",
            signal_id,
            pair=pair,
            order_id=self._order_id(order),
            original_price=original_price,
            adjusted_price=current_price,
            deviation=deviation,
            current_time=current_time,
        )
        return True

    def _open_entry_order_contexts(self) -> list[dict[str, Any]]:
        contexts: list[dict[str, Any]] = []

        strategy_config = self._strategy_config()
        for order in self._list_value(strategy_config, "open_orders"):
            contexts.append({"order": order})

        for order in self._list_value(self, "_open_orders", "open_orders"):
            contexts.append({"order": order})

        store = self.signal_store
        if store:
            for order in self._list_value(store, "open_orders"):
                contexts.append({"order": order})

        get_open_trades = getattr(Trade, "get_open_trades", None)
        if callable(get_open_trades):
            try:
                trades = get_open_trades()
            except Exception:
                trades = []
            for trade in trades:
                for order in self._trade_open_orders(trade):
                    contexts.append(
                        {
                            "order": order,
                            "trade": trade,
                            "pair": getattr(trade, "pair", ""),
                        }
                    )

        return contexts

    def _trade_open_orders(self, trade: Any) -> list[Any]:
        orders = self._list_value(trade, "open_orders", "orders")
        result = []
        for order in orders:
            if self._is_open_entry_order(order):
                result.append(order)
        return result

    def _is_open_entry_order(self, order: Any) -> bool:
        status = self._string_value(order, "status")
        if status and status.lower() not in {"open", "new", "partially_filled", "partially filled"}:
            return False

        filled = self._float_value(order, "filled", "filled_amount")
        amount = self._float_value(order, "amount", "safe_amount")
        if filled is not False and amount is not False and amount > 0 and filled >= amount:
            return False

        ft_order_side = self._string_value(order, "ft_order_side", "order_side")
        if ft_order_side and ft_order_side.lower() not in {"entry", "buy", "sell", "long", "short"}:
            return False

        return True

    def _order_age_seconds(self, order: Any, current_time: datetime) -> float | bool:
        opened_at = self._datetime_value(
            order,
            "order_date_utc",
            "order_date",
            "open_date_utc",
            "created_at",
            "created",
        )
        if not opened_at:
            timestamp = self._float_value(order, "order_timestamp", "timestamp")
            if timestamp is not False:
                opened_at = datetime.fromtimestamp(timestamp, tz=UTC)
        if not opened_at:
            return False
        active_time = self._coerce_datetime(current_time)
        if not active_time:
            active_time = current_time.replace(tzinfo=UTC)
        return max((active_time - opened_at).total_seconds(), 0.0)

    def _original_entry_price(self, signal: dict[str, Any], order: Any) -> float | bool:
        entry = self._entry(signal)
        price = self._entry_primary_price(entry)
        if price is not False:
            return price
        price = self._entry_midpoint(entry)
        if price is not False:
            return price
        return self._float_value(order, "price", "rate", "safe_price", "average")

    def _current_entry_price(self, order: Any, pair: str, side: str) -> float | bool:
        price = self._float_value(order, "current_price", "current_rate", "last_price", "market_price")
        if price is not False:
            return price

        exchange = getattr(self, "exchange", False)
        if exchange and hasattr(exchange, "get_rate"):
            try:
                return float(exchange.get_rate(pair, refresh=True, side=side))
            except TypeError:
                return float(exchange.get_rate(pair, refresh=True))
            except Exception:
                return False
        if exchange and hasattr(exchange, "fetch_ticker"):
            try:
                ticker = exchange.fetch_ticker(pair)
            except Exception:
                return False
            return self._float_value(ticker, "last", "close")
        return False

    def _cancel_entry_order(self, order: Any, pair: str, reason: str) -> bool:
        order_id = self._order_id(order)
        exchange = getattr(self, "exchange", False)
        if exchange and hasattr(exchange, "cancel_order") and order_id:
            try:
                exchange.cancel_order(order_id, pair)
                return True
            except TypeError:
                exchange.cancel_order(order_id)
                return True
            except Exception:
                return False

        store = self.signal_store
        if store and hasattr(store, "cancel_order"):
            result = store.cancel_order(order_id, pair=pair, reason=reason)
            return bool(result)

        cancel = getattr(order, "cancel", None)
        if callable(cancel):
            result = cancel()
            return result is not False

        if isinstance(order, dict):
            order["status"] = "cancelled"
            order["cancel_reason"] = reason
        else:
            try:
                setattr(order, "status", "cancelled")
                setattr(order, "cancel_reason", reason)
            except Exception:
                return False
        return True

    def _place_adjusted_entry_order(
        self,
        order: Any,
        signal: dict[str, Any],
        pair: str,
        price: float,
        current_time: datetime,
    ) -> bool:
        amount = self._float_value(order, "remaining", "remaining_amount", "amount", "safe_amount")
        if amount is False or amount <= 0:
            return False

        side = self._side(signal)
        order_side = "sell" if side == "short" else "buy"
        order_type = self._string_value(order, "order_type", "type") or "limit"
        order_tag = self._entry_tag(signal)

        place_entry_order = getattr(self, "place_entry_order", None)
        if callable(place_entry_order):
            result = place_entry_order(
                pair=pair,
                order_type=order_type,
                side=order_side,
                amount=amount,
                rate=price,
                entry_tag=order_tag,
                current_time=current_time,
            )
            return result is not False

        exchange = getattr(self, "exchange", False)
        if exchange and hasattr(exchange, "create_order"):
            try:
                exchange.create_order(pair, order_type, order_side, amount, price)
                return True
            except Exception:
                return False

        replacements = getattr(self, "_replacement_entry_orders", False)
        if replacements is False:
            self._replacement_entry_orders = []
            replacements = self._replacement_entry_orders
        replacements.append(
            {
                "pair": pair,
                "order_type": order_type,
                "side": order_side,
                "amount": amount,
                "rate": price,
                "entry_tag": order_tag,
                "current_time": current_time,
            }
        )
        return True

    def _update_signal_entry_price(self, signal: dict[str, Any], price: float) -> None:
        entry = self._entry(signal)
        if isinstance(entry, dict):
            entry["primary_price"] = float(price)
            return
        signal["entry"] = {"type": "limit", "primary_price": float(price)}

    def _release_signal_reservation(self, signal_id: str) -> None:
        store = self.signal_store
        if not store:
            return
        if hasattr(store, "release_signal_reservation"):
            store.release_signal_reservation(signal_id, operation_type="entry")
            return
        if hasattr(store, "release_reservation"):
            store.release_reservation(signal_id, operation_type="entry")

    def _order_pair(self, order: Any, trade: Any) -> str:
        pair = self._string_value(order, "pair", "ft_pair")
        if pair:
            return pair
        return self._string_value(trade, "pair")

    def _order_tag(self, order: Any, trade: Any) -> str | None:
        tag = self._string_value(order, "ft_order_tag", "order_tag", "tag", "client_order_id")
        if tag:
            return tag
        tag = self._string_value(trade, "entry_tag")
        if tag:
            return tag
        return None

    def _order_id(self, order: Any) -> str:
        return self._string_value(order, "order_id", "id", "ft_order_id")

    def _resolve_entry_price(self, signal: dict[str, Any], proposed_rate: float) -> tuple[float, str]:
        entry = self._entry(signal)
        mode = self._string_value(entry, "type", "mode")
        if mode.startswith("cmp"):
            return (float(proposed_rate), "cmp")

        price = self._entry_primary_price(entry)
        if price is not False:
            return (float(price), "primary_price")

        price = self._entry_midpoint(entry)
        if price is not False:
            return (float(price), "midpoint")

        return (float(proposed_rate), "proposed_rate")

    def _entry_reject_reason(
        self,
        signal: dict[str, Any],
        pair: str,
        side: str,
        amount: float,
        rate: float,
        leverage: float,
    ) -> str | bool:
        live_reason = self._live_entry_reject_reason(signal)
        if live_reason:
            return live_reason

        stop_loss = self._float_value(signal, "stop_loss", "sl")
        if stop_loss is False:
            return "stop_loss_missing"

        entry_range_reason = self._entry_range_reject_reason(signal, side, rate)
        if entry_range_reason:
            return entry_range_reason

        if rate == stop_loss:
            return "zero_stop_distance"

        if side == "long" and stop_loss >= rate:
            return "long_price_geometry_invalid"
        if side == "short" and stop_loss <= rate:
            return "short_price_geometry_invalid"

        exposure_reason = self._symbol_exposure_reject_reason(signal, pair, amount, rate)
        if exposure_reason:
            return exposure_reason

        liquidation_reason = self._liquidation_buffer_reject_reason(
            signal,
            side,
            rate,
            stop_loss,
            leverage,
        )
        if liquidation_reason:
            return liquidation_reason

        return False

    def _entry_range_reject_reason(self, signal: dict[str, Any], side: str, rate: float) -> str | bool:
        entry = self._entry(signal)
        lower = self._float_value(entry, "price_min", "min_price", "lower")
        upper = self._float_value(entry, "price_max", "max_price", "upper")

        if side == "long" and upper is not False and rate > upper:
            return "entry_price_above_max"
        if side == "short" and lower is not False and rate < lower:
            return "entry_price_below_min"
        return False

    def _live_entry_reject_reason(self, signal: dict[str, Any]) -> str | bool:
        if not self._is_live_mode():
            return False

        stop_loss = self._float_value(signal, "stop_loss", "sl")
        if stop_loss is False:
            return "live_stop_loss_required"

        if self._has_take_profit(signal):
            return False

        if self._has_live_take_profit_override(signal):
            return False

        return "live_take_profit_required"

    def _is_live_mode(self) -> bool:
        strategy_config = self._strategy_config()
        mode = self._string_value(strategy_config, "mode")
        if mode == "live":
            return True

        runmode = self._raw_value(self.config, "runmode")
        runmode_value = getattr(runmode, "value", False)
        if runmode_value is not False:
            return str(runmode_value) == "live"
        if runmode is not False:
            return str(runmode) == "live"

        if "dry_run" not in self.config:
            return False
        dry_run = self.config["dry_run"]
        return dry_run is False

    def _has_take_profit(self, signal: dict[str, Any]) -> bool:
        first_take_profit = self._first_take_profit(signal)
        if first_take_profit is not False:
            return True

        parse_status = self._string_value(signal, "take_profit_parse_status")
        if re.search(r"missing", parse_status):
            return False

        return False

    def _has_live_take_profit_override(self, signal: dict[str, Any]) -> bool:
        if self._bool_value(signal, "manual_approved", "manually_approved", "operator_approved"):
            return True
        if self._bool_value(signal, "allow_sl_only", "tp_missing_manual_approved"):
            return True

        strategy_config = self._strategy_config()
        return self._bool_value(strategy_config, "allow_live_without_take_profit")

    def _symbol_exposure_reject_reason(
        self,
        signal: dict[str, Any],
        pair: str,
        amount: float,
        rate: float,
    ) -> str | bool:
        equity = self._equity()
        if equity <= 0:
            return False

        current_exposure = self._symbol_exposure_pct(pair)
        order_exposure = amount * rate / equity * 100.0
        projected_exposure = current_exposure + order_exposure
        if projected_exposure > self._max_symbol_exposure_pct(signal):
            return "symbol_exposure_limit"
        return False

    def _dca_risk_reject_reason(
        self,
        signal: dict[str, Any],
        trade: Trade,
        level: Any,
        stake: float,
        current_rate: float,
    ) -> str | bool:
        risk_pct = self._dca_level_risk_pct(signal, level)
        total_reason = self._total_open_risk_reject_reason(signal, risk_pct)
        if total_reason:
            return total_reason

        if current_rate <= 0:
            return False

        leverage = self._signal_leverage(signal)
        amount = stake * leverage / current_rate
        return self._symbol_exposure_reject_reason(
            signal,
            getattr(trade, "pair", ""),
            amount,
            current_rate,
        )

    def _dca_level_risk_pct(self, signal: dict[str, Any], level: Any) -> float:
        risk_pct = self._float_value(level, "risk_pct", "risk_percent")
        if risk_pct is False:
            risk_pct = self._risk_pct(signal)
        return float(risk_pct)

    def _total_open_risk_reject_reason(
        self,
        signal: dict[str, Any],
        risk_pct: float,
    ) -> str | bool:
        max_total_open_risk_pct = self._max_total_open_risk_pct(signal)
        if max_total_open_risk_pct <= 0:
            return False

        projected_open_risk = self._open_risk_pct() + self._risk_fraction_to_percent(risk_pct)
        if projected_open_risk > max_total_open_risk_pct:
            return "total_open_risk_limit"
        return False

    def _max_total_open_risk_pct(self, signal: dict[str, Any]) -> float:
        policy = self._dict_value(signal, "policy", "risk_policy")
        policy_value = self._float_value(policy, "max_total_open_risk_pct")
        if policy_value is not False:
            return policy_value
        return self.max_total_open_risk_pct

    def _open_risk_pct(self) -> float:
        strategy_config = self._strategy_config()
        return float(strategy_config.get("open_risk_pct", 0.0))

    def _risk_fraction_to_percent(self, risk_pct: float) -> float:
        return float(risk_pct) * 100.0

    def _max_symbol_exposure_pct(self, signal: dict[str, Any]) -> float:
        policy = self._dict_value(signal, "policy", "risk_policy")
        policy_value = self._float_value(policy, "max_symbol_exposure_pct")
        if policy_value is not False:
            return policy_value
        return self.max_symbol_exposure_pct

    def _symbol_exposure_pct(self, pair: str) -> float:
        strategy_config = self._strategy_config()
        normalized_pair = self._normalize_pair(pair)
        exposures = strategy_config.get("symbol_exposures", {})
        if isinstance(exposures, dict):
            raw_value = exposures.get(normalized_pair)
            if raw_value is None:
                raw_value = exposures.get(pair)
            if raw_value is not None:
                return float(raw_value)
        return float(strategy_config.get("symbol_exposure_pct", 0.0))

    def _liquidation_buffer_reject_reason(
        self,
        signal: dict[str, Any],
        side: str,
        entry_price: float,
        stop_loss: float,
        leverage: float,
    ) -> str | bool:
        min_buffer_pct = self._min_liquidation_buffer_pct(signal)
        if min_buffer_pct <= 0:
            return False
        if leverage <= 1:
            return False

        liquidation_price = self._estimate_liquidation_price(side, entry_price, leverage)
        buffer_pct = self._liquidation_buffer_pct(side, entry_price, stop_loss, liquidation_price)
        if buffer_pct is False:
            return "liquidation_buffer_too_small"
        if buffer_pct < min_buffer_pct:
            return "liquidation_buffer_too_small"
        return False

    def _min_liquidation_buffer_pct(self, signal: dict[str, Any]) -> float:
        policy = self._dict_value(signal, "policy", "risk_policy")
        policy_value = self._float_value(policy, "min_liquidation_buffer_pct")
        if policy_value is not False:
            return policy_value
        return self.min_liquidation_buffer_pct

    def _estimate_liquidation_price(self, side: str, entry_price: float, leverage: float) -> float:
        if side == "short":
            return entry_price * (1.0 + 1.0 / leverage)
        return entry_price * (1.0 - 1.0 / leverage)

    def _liquidation_buffer_pct(
        self,
        side: str,
        entry_price: float,
        stop_loss: float,
        liquidation_price: float,
    ) -> float | bool:
        if side == "short":
            if stop_loss >= liquidation_price:
                return False
            return (liquidation_price - stop_loss) / entry_price * 100.0

        if stop_loss <= liquidation_price:
            return False
        return (stop_loss - liquidation_price) / entry_price * 100.0

    def _reject_signal(
        self,
        signal: dict[str, Any],
        reason: str,
        actor: str = "strategy",
        **context,
    ) -> None:
        signal_id = self._signal_id(signal)
        if not signal_id:
            return

        current_status = self._string_value(signal, "status")
        target_status = "failed"
        if current_status == "approved":
            target_status = "rejected"

        transition_context = dict(context)
        transition_context["reason"] = reason
        transitioned = self._mark_signal_status(
            signal,
            target_status,
            actor,
            **transition_context,
        )
        if not transitioned:
            self._record_risk_event(
                "trade_entry_reject_transition_failed",
                level="warning",
                signal_id=signal_id,
                from_status=current_status,
                target_status=target_status,
                reason=reason,
            )
            self._record_audit_event(
                "trade_entry_reject_transition_failed",
                signal_id,
                from_status=current_status,
                target_status=target_status,
                actor=actor,
                **transition_context,
            )
        signal["status"] = "rejected"
        signal["reject_reason"] = reason
        signal["failed_reason"] = reason

    def _dca_fill_idempotency_key(
        self,
        trade: Trade,
        signal_id: str,
        dca_price: float,
    ) -> str:
        return f"dca_fill:{self._trade_id(trade)}:{signal_id}:{float(dca_price)}"

    def _entry_block_reason(self) -> str | bool:
        strategy_config = self._strategy_config()
        if strategy_config.get("kill_switch_enabled"):
            return "kill_switch_enabled"
        if runtime_kill_switch_enabled and runtime_kill_switch_enabled():
            return "kill_switch_enabled"
        store = self.signal_store
        if store and getattr(store, "kill_switch_enabled", False):
            return "kill_switch_enabled"

        risk_state = strategy_config.get("risk_state", "")
        if not risk_state and store:
            risk_state = getattr(store, "risk_state", "")
        if risk_state == "blocked_new_entries":
            return "blocked_new_entries"
        return False

    def _record_signal_conflict(
        self,
        winner: dict[str, Any],
        loser: dict[str, Any],
    ) -> None:
        loser_id = self._signal_id(loser)
        if not loser_id:
            return
        loser["status"] = "needs_review"
        self._record_audit_event(
            "needs_review",
            loser_id,
            winner_signal_id=self._signal_id(winner),
            reason="same_pair_same_candle_conflict",
            pair=self._pair(loser),
        )

    def _take_profit_schedule(self, signal: dict[str, Any]) -> list[dict[str, Any]]:
        levels = self._list_value(signal, "take_profits", "take_profit", "targets")
        return self.normalize_take_profit_schedule(levels)

    def normalize_take_profit_schedule(self, levels: list[Any]) -> list[dict[str, Any]]:
        normalized = []
        for level in levels:
            if isinstance(level, int | float):
                item = {"price": float(level), "close_pct": 100.0}
            elif isinstance(level, dict):
                item = dict(level)
            else:
                item = {
                    "price": self._float_value(level, "price", "primary_price", "target"),
                    "close_pct": self._float_value(level, "close_pct", "pct", "percent"),
                }
            close_pct = self._float_value(item, "close_pct", "pct", "percent")
            if close_pct is False:
                close_pct = 100.0
            item["close_pct"] = float(close_pct)
            normalized.append(item)

        close_pcts = [item["close_pct"] for item in normalized]
        if self._looks_cumulative_schedule(close_pcts):
            previous = 0.0
            for item in normalized:
                current = item["close_pct"]
                item["close_pct"] = current - previous
                previous = current
        return normalized

    def _looks_cumulative_schedule(self, close_pcts: list[float]) -> bool:
        if len(close_pcts) < 2:
            return False
        previous = close_pcts[0]
        saw_increase = False
        for value in close_pcts[1:]:
            if value < previous:
                return False
            if value > previous:
                saw_increase = True
            previous = value
        return saw_increase and close_pcts[-1] <= 100.0

    def _record_take_profit_event(
        self,
        trade: Trade,
        signal: dict[str, Any],
        take_profit: dict[str, Any],
        level_index: int,
        current_time: datetime,
        current_rate: float,
    ) -> bool:
        signal_id = self._signal_id(signal)
        trade_id = self._trade_id(trade)
        event_key = (trade_id, signal_id, level_index)
        if event_key in self._tp_exits:
            return False
        self._tp_exits.add(event_key)
        close_pct = self._float_value(take_profit, "close_pct")
        self._record_audit_event(
            "trade_exit_event",
            signal_id,
            pair=getattr(trade, "pair", ""),
            trade_id=trade_id,
            level_index=level_index,
            reason="signal_take_profit",
            close_pct=close_pct,
            current_rate=current_rate,
            current_time=current_time,
        )
        return True

    def _dca_price_from_order_tag(self, signal: dict[str, Any], order_tag: Any) -> float | bool:
        if not isinstance(order_tag, str):
            return False
        match = re.search(r":dca:(\d+)", order_tag)
        if not match:
            return False
        level_index = int(match.group(1))
        levels = self._list_value(signal, "dca", "dca_levels")
        if not levels:
            entry = self._entry(signal)
            dca_prices = self._list_value(entry, "dca_prices")
            levels = [{"price": price} for price in dca_prices]
        if level_index < 1 or level_index > len(levels):
            return False
        return self._float_value(levels[level_index - 1], "price", "primary_price")

    def _trade_id(self, trade: Trade) -> Any:
        trade_id = getattr(trade, "id", False)
        if trade_id is not False:
            return trade_id
        trade_id = getattr(trade, "trade_id", False)
        if trade_id is not False:
            return trade_id
        trade_id = getattr(trade, "open_date_utc", False)
        if trade_id is not False:
            return trade_id
        return getattr(trade, "pair", "")

    def _normalize_signal(self, raw_signal: Any) -> dict[str, Any]:
        if isinstance(raw_signal, dict):
            return dict(raw_signal)
        data = {}
        for name in dir(raw_signal):
            if name.startswith("_"):
                continue
            value = getattr(raw_signal, name)
            if callable(value):
                continue
            data[name] = value
        return data

    def _is_entry_eligible(self, signal: dict[str, Any], current_time: datetime) -> bool:
        status = self._string_value(signal, "status")
        if status != "approved":
            return False
        if self._bool_value(signal, "reserved"):
            return False
        if self._bool_value(signal, "expired"):
            return False
        expires_at = self._datetime_value(signal, "expires_at", "expire_at", "expires")
        if not expires_at:
            entry = self._entry(signal)
            expires_at = self._datetime_value(entry, "expires_at", "expire_at", "expires")
        if expires_at and current_time >= expires_at:
            return False
        if not self._signal_id(signal):
            return False
        side = self._side(signal)
        if side not in {"long", "short"}:
            return False
        return True

    def _signal_from_context(
        self,
        pair: str,
        entry_tag: str | None,
        trade: Trade | None,
    ) -> dict[str, Any] | bool:
        signal_id = self._signal_id_from_tag(entry_tag)
        if not signal_id and trade:
            signal_id = self._signal_id_from_tag(getattr(trade, "entry_tag", None))
        if signal_id:
            signal = self.signals_by_id.get(signal_id)
            if signal:
                return signal
        normalized_pair = self._normalize_pair(pair)
        pair_signals = self._entry_signals_by_pair.get(normalized_pair, [])
        if pair_signals:
            return pair_signals[-1]
        return False

    def _signal_id_from_tag(self, tag: str | None) -> str | bool:
        if not tag:
            return False
        marker = "sig:"
        marker_index = tag.find(marker)
        if marker_index < 0:
            return False

        value = tag[marker_index + len(marker) :].strip()
        pair_index = value.find(" pair:")
        if pair_index >= 0:
            value = value[:pair_index]

        dca_index = value.rfind(":dca:")
        if dca_index >= 0:
            value = value[:dca_index]

        space_index = value.find(" ")
        if space_index >= 0:
            value = value[:space_index]

        if not value:
            return False
        return value

    def _entry_tag(self, signal: dict[str, Any]) -> str:
        signal_id = self._signal_id(signal)
        pair = self._pair(signal)
        tag = f"sig:{signal_id} pair:{pair}"
        if len(tag) <= 255:
            return tag
        return tag[:255]

    def _entry(self, signal: dict[str, Any]) -> Any:
        entry = self._raw_value(signal, "entry", "entry_price", "entry_policy")
        if entry:
            return entry
        return signal

    def _entry_primary_price(self, entry: Any) -> float | bool:
        return self._float_value(entry, "primary_price", "price", "limit_price")

    def _entry_midpoint(self, entry: Any) -> float | bool:
        lower = self._float_value(entry, "price_min", "min_price", "lower")
        upper = self._float_value(entry, "price_max", "max_price", "upper")
        if lower is False or upper is False:
            return False
        return (lower + upper) / 2.0

    def _first_take_profit(self, signal: dict[str, Any]) -> float | bool:
        levels = self._list_value(signal, "take_profits", "take_profit", "targets")
        if not levels:
            price = self._float_value(signal, "take_profit", "tp")
            return price
        first = levels[0]
        if isinstance(first, int | float):
            return float(first)
        return self._float_value(first, "price", "primary_price", "target")

    def _equity(self) -> float:
        if self.wallets:
            if hasattr(self.wallets, "get_total_stake_amount"):
                return float(self.wallets.get_total_stake_amount())
            if hasattr(self.wallets, "get_total"):
                stake_currency = self.config.get("stake_currency", "USDT")
                return float(self.wallets.get_total(stake_currency))
        return self.default_equity

    def _risk_pct(self, source: Any) -> float:
        risk_pct = self._float_value(source, "risk_pct", "risk_percent")
        if risk_pct is False:
            return self.default_risk_pct
        return risk_pct

    def _signal_leverage(self, signal: dict[str, Any]) -> float:
        leverage_plan = self._raw_value(signal, "leverage")
        leverage = self._float_value(leverage_plan, "selected")
        if leverage is False:
            leverage = self._float_value(signal, "leverage")
        if leverage is False:
            leverage = self.default_leverage
        plan_max = self._float_value(leverage_plan, "max")
        if plan_max is not False:
            leverage = min(leverage, plan_max)
        leverage = max(leverage, self.min_strategy_leverage)
        leverage = min(leverage, self.max_strategy_leverage)
        return float(leverage)

    def _trade_leverage(self, trade: Trade) -> float:
        leverage = getattr(trade, "leverage", 1.0)
        if not leverage:
            return 1.0
        return float(leverage)

    def _trade_is_short(self, trade: Trade) -> bool:
        is_short = getattr(trade, "is_short", False)
        return bool(is_short)

    def _breakeven_adjusted_stop_loss(
        self,
        pair: str,
        trade: Trade,
        signal: dict[str, Any],
        stop_loss: float,
        current_profit: float,
    ) -> float:
        policy = self._dict_value(self.risk_policy, "breakeven_stoploss")
        if not policy:
            return float(stop_loss)
        enabled = self._raw_value(policy, "enabled")
        if enabled is False:
            return float(stop_loss)

        pair_policy = self._breakeven_pair_policy(policy, pair)
        trigger_profit = self._breakeven_trigger_profit_pct(pair, policy, pair_policy)
        if current_profit < trigger_profit:
            return float(stop_loss)

        entry_rate = self._breakeven_entry_rate(trade, signal)
        if entry_rate is False or entry_rate <= 0:
            return float(stop_loss)

        fee_buffer = self._breakeven_fee_buffer_pct(policy, pair_policy)
        is_short = self._trade_is_short(trade)
        if is_short:
            breakeven_stop = entry_rate * (1.0 - fee_buffer)
            return float(min(stop_loss, breakeven_stop))

        breakeven_stop = entry_rate * (1.0 + fee_buffer)
        return float(max(stop_loss, breakeven_stop))

    def _apply_stoploss_directive(
        self,
        pair: str,
        trade: Trade,
        current_rate: float,
        current_profit: float,
    ) -> float | bool:
        move_to_entry = self._first_pending_directive(pair, "move_sl_to_entry")
        if move_to_entry:
            self._consume_directive(move_to_entry)
            return 0 - current_profit

        move_sl = self._first_pending_directive(pair, "move_sl")
        if not move_sl:
            return False
        price = self._directive_price(move_sl)
        if price is False:
            return False
        self._consume_directive(move_sl)
        return stoploss_from_absolute(
            price,
            current_rate,
            is_short=self._trade_is_short(trade),
            leverage=self._trade_leverage(trade),
        )

    def _first_pending_directive(self, pair: str, kind: str) -> Any:
        store = self.signal_store
        if not store or not hasattr(store, "get_pending_directives"):
            return False
        directives = store.get_pending_directives(self._normalize_pair(pair))
        for directive in directives:
            if self._directive_kind(directive) == kind:
                return directive
        return False

    def _consume_directive(self, directive: Any) -> None:
        store = self.signal_store
        if not store or not hasattr(store, "consume_directive"):
            return
        directive_id = self._raw_value(directive, "directive_id", "id")
        if directive_id is False:
            directive_id = directive
        store.consume_directive(directive_id)

    def _directive_kind(self, directive: Any) -> str:
        kind = self._raw_value(directive, "kind")
        value = getattr(kind, "value", False)
        if value is not False:
            return str(value)
        return str(kind)

    def _directive_fraction(self, directive: Any) -> float:
        fraction = self._float_value(directive, "fraction")
        if fraction is False:
            return 0.5
        return max(0.0, min(float(fraction), 1.0))

    def _directive_price(self, directive: Any) -> float | bool:
        return self._float_value(directive, "price")

    def _breakeven_pair_policy(self, policy: dict[str, Any], pair: str) -> dict[str, Any]:
        pair_overrides = self._dict_value(policy, "pair_overrides", "pairs")
        normalized_pair = self._normalize_pair(pair)
        pair_policy = self._dict_value(pair_overrides, normalized_pair)
        if pair_policy:
            return pair_policy
        raw_pair_policy = self._dict_value(pair_overrides, pair)
        if raw_pair_policy:
            return raw_pair_policy
        base = self._pair_base(normalized_pair)
        return self._dict_value(pair_overrides, base)

    def _breakeven_trigger_profit_pct(
        self,
        pair: str,
        policy: dict[str, Any],
        pair_policy: dict[str, Any],
    ) -> float:
        pair_value = self._float_value(pair_policy, "trigger_profit_pct", "profit_threshold_pct")
        if pair_value is not False:
            return self._pct_to_ratio(pair_value)

        if self._pair_base(pair) == "BTC":
            btc_value = self._float_value(policy, "btc_trigger_profit_pct")
            if btc_value is not False:
                return self._pct_to_ratio(btc_value)
            return 0.02

        default_value = self._float_value(policy, "default_trigger_profit_pct", "alt_trigger_profit_pct")
        if default_value is not False:
            return self._pct_to_ratio(default_value)
        return 0.04

    def _breakeven_fee_buffer_pct(
        self,
        policy: dict[str, Any],
        pair_policy: dict[str, Any],
    ) -> float:
        pair_value = self._float_value(pair_policy, "fee_buffer_pct")
        if pair_value is not False:
            return self._pct_to_ratio(pair_value)
        value = self._float_value(policy, "fee_buffer_pct")
        if value is not False:
            return self._pct_to_ratio(value)
        return 0.0

    def _breakeven_entry_rate(self, trade: Trade, signal: dict[str, Any]) -> float | bool:
        entry_rate = self._float_value(
            trade,
            "open_rate",
            "entry_rate",
            "enter_rate",
        )
        if entry_rate is not False:
            return entry_rate

        entry = self._entry(signal)
        primary_price = self._entry_primary_price(entry)
        if primary_price is not False:
            return primary_price

        midpoint = self._entry_midpoint(entry)
        if midpoint is not False:
            return midpoint
        return False

    def _pair_base(self, pair: str) -> str:
        normalized_pair = self._normalize_pair(pair)
        base = normalized_pair.split("/", 1)[0]
        return base.upper()

    def _pct_to_ratio(self, value: float) -> float:
        if value > 1.0:
            return value / 100.0
        return value

    def _clamp_stake(self, stake: float, min_stake: float | None, max_stake: float) -> float:
        result = stake
        if min_stake is not None:
            result = max(result, min_stake)
        result = min(result, max_stake)
        return float(result)

    def _record_risk_event(self, event_type: str, **payload) -> None:
        store = self.signal_store
        if store and hasattr(store, "record_risk_event"):
            store.record_risk_event(event_type, **payload)
            return
        if store and hasattr(store, "risk_events"):
            event = {"type": event_type}
            event.update(payload)
            store.risk_events.append(event)
            return
        logger.warning("Risk event: %s %s", event_type, payload)

    def _record_audit_event(self, event_type: str, signal_id: str, **payload) -> None:
        store = self.signal_store
        if store and hasattr(store, "record_audit_event"):
            store.record_audit_event(event_type, signal_id, **payload)
            return
        if store and hasattr(store, "audit"):
            store.audit(event_type, signal_id=signal_id, **payload)
            return
        if store:
            audit_events = getattr(store, "audit_events", False)
            if audit_events is False:
                store.audit_events = []
                audit_events = store.audit_events
            audit_events.append({"type": event_type, "signal_id": signal_id, **payload})
            return
        logger.info("Signal audit event: %s %s %s", event_type, signal_id, payload)

    def _reserve_entry_signal(
        self,
        signal: dict[str, Any],
        pair: str,
        current_time: datetime,
    ) -> bool:
        signal_id = self._signal_id(signal)
        if not signal_id:
            return False

        reserved = self._reserve_signal(signal_id, pair=pair, current_time=current_time)
        if not reserved:
            self._record_risk_event(
                "entry_reservation_failed",
                level="warning",
                signal_id=signal_id,
                pair=pair,
                current_time=current_time,
            )
            return False

        signal["reserved"] = True
        signal["status"] = "reserved"
        return True

    def _reserve_signal(self, signal_id: str, **payload) -> bool:
        store = self.signal_store
        if store and hasattr(store, "reserve"):
            result = store.reserve(signal_id, **payload)
            return self._reservation_succeeded(result)
        if store and hasattr(store, "reserve_signal"):
            result = store.reserve_signal(signal_id, operation_type="entry")
            return self._reservation_succeeded(result)
        return False

    def _reservation_succeeded(self, result: Any) -> bool:
        if result is None:
            return True
        if result is False:
            return False
        reserved = getattr(result, "reserved", False)
        if reserved is not False:
            return bool(reserved)
        if isinstance(result, dict) and "reserved" in result:
            return bool(result["reserved"])
        return bool(result)

    def _mark_signal_status(
        self,
        signal: dict[str, Any],
        target_status: str,
        actor: str,
        **context,
    ) -> bool:
        signal_id = self._signal_id(signal)
        if not signal_id:
            return False

        transitioned = False
        store = self.signal_store
        if store and hasattr(store, "transition_signal"):
            status_value: Any = target_status
            if SignalStatus:
                status_value = SignalStatus(target_status)
            result = store.transition_signal(signal_id, status_value, actor, context)
            approved = getattr(result, "approved", False)
            if approved is not False:
                transitioned = bool(approved)
            else:
                transitioned = bool(result)
            if not transitioned:
                return False

        signal["status"] = target_status
        if target_status in {"reserved", "sent_to_freqtrade"}:
            signal["reserved"] = True

        if transitioned:
            return True

        self._record_audit_event(
            target_status,
            signal_id,
            actor=actor,
            **context,
        )
        return True

    def _dataframe_current_time(self, dataframe: DataFrame) -> datetime:
        if "date" in dataframe and not dataframe.empty:
            value = dataframe["date"].iloc[-1]
            parsed = self._coerce_datetime(value)
            if parsed:
                return parsed
        return datetime.now(tz=UTC)

    def _timeframe_delta(self) -> timedelta:
        match = re.fullmatch(r"(\d+)([mhd])", self.timeframe)
        if not match:
            return timedelta(minutes=5)
        amount = int(match.group(1))
        unit = match.group(2)
        if unit == "m":
            return timedelta(minutes=amount)
        if unit == "h":
            return timedelta(hours=amount)
        return timedelta(days=amount)

    def _side(self, signal: dict[str, Any]) -> str:
        side = self._string_value(signal, "side", "direction")
        return side.lower()

    def _signal_id(self, signal: dict[str, Any]) -> str:
        signal_id = self._string_value(signal, "id", "signal_id")
        return signal_id

    def _pair(self, signal: dict[str, Any]) -> str:
        pair = self._string_value(signal, "pair", "pair_freqtrade", "pair_raw")
        return self._normalize_pair(pair)

    def _normalize_pair(self, pair: Any) -> str:
        if not pair:
            return ""
        raw_pair = str(pair).upper().strip()
        if ":" in raw_pair:
            base_pair = raw_pair.split(":", maxsplit=1)[0]
            quote = raw_pair.split(":", maxsplit=1)[1]
            if "/" in base_pair:
                return f"{base_pair}:{quote}"

        compact_match = re.fullmatch(r"([A-Z0-9]+)(USDT)", raw_pair)
        if compact_match:
            base = compact_match.group(1)
            quote = compact_match.group(2)
            return f"{base}/{quote}:{quote}"

        slash_match = re.fullmatch(r"([A-Z0-9]+)/([A-Z0-9]+)", raw_pair)
        if slash_match:
            base = slash_match.group(1)
            quote = slash_match.group(2)
            if quote == "USDT":
                return f"{base}/{quote}:{quote}"
            return f"{base}/{quote}"

        return raw_pair

    def _dict_value(self, source: Any, *names: str) -> dict[str, Any]:
        value = self._raw_value(source, *names)
        if isinstance(value, dict):
            return value
        return {}

    def _list_value(self, source: Any, *names: str) -> list[Any]:
        value = self._raw_value(source, *names)
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        if value:
            return [value]
        return []

    def _float_value(self, source: Any, *names: str) -> float | bool:
        value = self._raw_value(source, *names)
        if value is False:
            return False
        try:
            return float(value)
        except (TypeError, ValueError):
            return False

    def _string_value(self, source: Any, *names: str) -> str:
        value = self._raw_value(source, *names)
        if value is False:
            return ""
        if value is None:
            return ""
        enum_value = getattr(value, "value", False)
        if enum_value is not False:
            return str(enum_value)
        return str(value)

    def _bool_value(self, source: Any, *names: str) -> bool:
        value = self._raw_value(source, *names)
        return bool(value)

    def _datetime_value(self, source: Any, *names: str) -> datetime | bool:
        value = self._raw_value(source, *names)
        return self._coerce_datetime(value)

    def _raw_value(self, source: Any, *names: str) -> Any:
        if source is False:
            return False
        if source is None:
            return False
        for name in names:
            if isinstance(source, dict) and name in source:
                return source[name]
            if hasattr(source, name):
                return getattr(source, name)
        return False

    def _coerce_datetime(self, value: Any) -> datetime | bool:
        if value is False:
            return False
        if value is None:
            return False
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, Timestamp):
            result = value.to_pydatetime()
        elif isinstance(value, str):
            try:
                result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return False
        else:
            return False

        if result.tzinfo is None:
            return result.replace(tzinfo=UTC)
        return result.astimezone(UTC)
