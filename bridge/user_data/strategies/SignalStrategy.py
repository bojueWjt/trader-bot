from __future__ import annotations

import json
import os
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    from pandas import DataFrame
except ModuleNotFoundError:
    DataFrame = Any

from signal_strategy_support import normalize_signal, signal_id, signal_pair, signal_status

try:
    from app.services.signal_parser import SignalStatus
except ModuleNotFoundError:
    SignalStatus = False


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


class SignalStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "5m"
    can_short = True
    startup_candle_count = 0
    minimal_roi = {"0": 100}
    stoploss = -0.99

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        strategy_config = self._strategy_config()
        self.lookback_minutes = int(strategy_config.get("lookback_minutes", 240))
        self.default_risk_pct = float(strategy_config.get("risk_pct", 0.01))
        self.default_equity = float(strategy_config.get("equity", 0.0))
        self.risk_policy = self._load_risk_policy(strategy_config)
        self.default_leverage = float(strategy_config.get("default_leverage", 3.0))
        self.min_strategy_leverage = float(strategy_config.get("min_leverage", 1.0))
        self.max_strategy_leverage = float(strategy_config.get("max_leverage", 5.0))
        # Per-account channel routing: when account_id is set this instance only
        # trades signals whose source channel is routed to it in channel_routing.
        # Empty account_id keeps legacy behaviour (trade every channel) so
        # dry-run / back-test / unit tests are unaffected.
        self.account_id = str(strategy_config.get("account_id", "")).strip()
        self.routing_db_path = str(
            strategy_config.get("routing_db_path")
            or os.environ.get("TRADING_DB_PATH", "")
            or "/data/watcher-trading.db"
        )
        self.trade_unrouted = bool(strategy_config.get("trade_unrouted", False))
        self.signal_store = strategy_config.get("store", False) or self._store_from_url(strategy_config)
        self.signals_by_id: dict[str, dict[str, Any]] = {}
        self.entry_signals_by_pair: dict[str, list[dict[str, Any]]] = {}
        self.store_available = True
        self.store_failures: list[dict[str, str]] = []
        self._dca_fills: set[tuple[Any, float]] = set()
        self._tp_exits: set[tuple[Any, str, int]] = set()
        self._breakeven_notifications: set[tuple[Any, str, float]] = set()

    def _store_from_url(self, strategy_config: dict[str, Any]) -> Any:
        import os

        store_url = str(strategy_config.get("store_url", "") or os.environ.get("SIGNAL_STORE_URL", ""))
        if not store_url:
            return False
        try:
            from freqtrade.signal_strategy.store import make_signal_store_from_url
        except ImportError:
            return False
        try:
            return make_signal_store_from_url(store_url)
        except Exception:
            return False

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

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        try:
            signals = self._load_approved_signals(current_time)
        except TimeoutError as exc:
            self._record_store_failure("signal_store_timeout", exc)
            return
        except Exception as exc:
            self._record_store_failure("signal_store_unavailable", exc)
            return

        self.store_available = True
        self.entry_signals_by_pair = {}
        signals = self._filter_signals_for_account(signals)
        for raw_signal in signals:
            signal = normalize_signal(raw_signal)
            current_signal_id = signal_id(signal)
            if not current_signal_id:
                continue
            self.signals_by_id[current_signal_id] = signal
            if signal_status(signal) != "approved":
                continue
            pair = signal_pair(signal)
            if not pair:
                continue
            pair_signals = self.entry_signals_by_pair.setdefault(pair, [])
            pair_signals.append(signal)

    def _load_approved_signals(self, current_time: datetime) -> list[Any]:
        if not self.signal_store:
            return []
        if hasattr(self.signal_store, "get_approved_signals"):
            return self.signal_store.get_approved_signals(
                since_minutes=self.lookback_minutes,
                current_time=current_time,
            )
        if hasattr(self.signal_store, "list_approved"):
            return self.signal_store.list_approved(
                since_minutes=self.lookback_minutes,
                current_time=current_time,
            )
        raise RuntimeError("signal store has no approved signal loader")

    def _signal_channel_id(self, raw: Any) -> str:
        for key in ("source_channel_id", "channel_id"):
            if isinstance(raw, dict) and key in raw:
                return str(raw.get(key) or "").strip()
            if hasattr(raw, key):
                return str(getattr(raw, key) or "").strip()
        return ""

    def _load_channel_routing(self) -> dict[str, str]:
        """Read channel_id -> target_account_id from the watcher trading DB.

        Read-only, re-read each loop so watcher-UI routing changes take effect
        live. On any failure return {} — with account_id set and trade_unrouted
        False this halts trading (safe) rather than misrouting real money.
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
        for raw in signals:
            channel_id = self._signal_channel_id(raw)
            target = routing.get(channel_id) if channel_id else None
            if not target:
                if self.trade_unrouted:
                    kept.append(raw)
                else:
                    skipped_unrouted += 1
                continue
            if target == self.account_id:
                kept.append(raw)
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

    def _record_store_failure(self, event_type: str, exc: Exception) -> None:
        self.store_available = False
        self.entry_signals_by_pair = {}
        self.store_failures.append({"event_type": event_type, "error": str(exc)})

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        self._ensure_entry_columns(dataframe)
        if not self.store_available:
            return dataframe
        pair = str(metadata.get("pair", ""))
        if not pair:
            return dataframe
        signals = self.entry_signals_by_pair.get(pair, [])
        if not signals:
            return dataframe
        latest_index = dataframe.index[-1]
        signal = signals[-1]
        reserved = self._reserve_entry_signal(signal)
        if not reserved:
            return dataframe
        self._mark_signal_status(signal, "sent_to_freqtrade", "strategy")
        side = str(signal.get("side", "long"))
        if side == "short":
            dataframe.at[latest_index, "enter_short"] = 1
        else:
            dataframe.at[latest_index, "enter_long"] = 1
        dataframe.at[latest_index, "enter_tag"] = f"sig:{signal_id(signal)}"
        return dataframe

    def custom_entry_price(
        self,
        pair: str,
        trade: Any,
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
        return self._resolve_entry_price(signal, proposed_rate)

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
            return False

        entry_price = self._resolve_entry_price(signal, current_rate)
        stop_distance = abs(entry_price - stop_loss)
        if stop_distance == 0:
            return False

        active_leverage = leverage
        if active_leverage <= 0:
            active_leverage = self._signal_leverage(signal, self.max_strategy_leverage)
        risk_amount = self._equity() * self._risk_pct(signal)
        quantity = risk_amount / stop_distance
        stake = quantity * entry_price / active_leverage
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
        if not signal:
            return float(min(proposed_leverage, max_leverage, self.max_strategy_leverage))
        return self._signal_leverage(signal, max_leverage)

    def custom_stoploss(
        self,
        pair: str,
        trade: Any,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool = False,
        **kwargs,
    ) -> float:
        if not self._directive_block_reason():
            directive_stoploss = self._apply_stoploss_directive(pair, trade, current_rate, current_profit)
            if directive_stoploss is not False:
                return directive_stoploss

        signal = self._signal_from_context(pair, None, trade)
        if not signal:
            return self.stoploss

        stop_loss = self._float_value(signal, "stop_loss", "sl")
        if stop_loss is False:
            return self.stoploss

        trade_leverage = getattr(trade, "leverage", 1.0)
        if not trade_leverage:
            trade_leverage = 1.0
        is_short = bool(getattr(trade, "is_short", False))
        stop_loss = self._breakeven_adjusted_stop_loss(
            pair,
            trade,
            signal,
            stop_loss,
            current_profit,
        )
        return stoploss_from_absolute(
            stop_loss,
            current_rate,
            is_short=is_short,
            leverage=float(trade_leverage),
        )

    def custom_exit(
        self,
        pair: str,
        trade: Any,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> str | bool:
        close_directive = self._first_pending_directive(pair, "close")
        if close_directive:
            self._consume_directive(close_directive)
            return "directive_close"

        signal = self._signal_from_context(pair, None, trade)
        if not signal:
            return False

        is_short = bool(getattr(trade, "is_short", False))
        take_profits = self._list_value(signal, "take_profits", "take_profit", "targets")
        for index, take_profit in enumerate(take_profits, start=1):
            price = self._float_value(take_profit, "price", "primary_price", "target")
            if price is False:
                continue
            if not self._take_profit_reached(current_rate, price, is_short):
                continue
            exit_key = (self._trade_id(trade), signal_id(signal), index)
            if exit_key in self._tp_exits:
                continue
            self._tp_exits.add(exit_key)
            return "trade_exit_event/signal_take_profit"
        return False

    def adjust_trade_position(
        self,
        trade: Any,
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
    ) -> float | None | tuple[float, str]:
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

        levels = self._dca_levels(signal)
        if not levels:
            return None

        is_short = bool(getattr(trade, "is_short", False))
        for index, level in enumerate(levels, start=1):
            price = self._float_value(level, "price", "primary_price")
            if price is False:
                continue
            dca_key = (self._trade_id(trade), float(price))
            if dca_key in self._dca_fills:
                continue
            if not self._dca_price_reached(current_rate, price, is_short):
                continue
            stake = self._dca_stake(signal, level, current_rate, min_stake, max_stake)
            if stake is False:
                return None
            self._dca_fills.add(dca_key)
            return (stake, f"sig:{signal_id(signal)}:dca:{index}")
        return None

    def order_filled(
        self,
        pair: str,
        trade: Any,
        order: Any,
        current_time: datetime,
        **kwargs,
    ) -> None:
        order_tag = kwargs.get("order_tag")
        signal = self._signal_from_context(pair, order_tag, trade)
        if not signal:
            return
        if isinstance(order_tag, str) and ":dca:" in order_tag:
            signal["last_dca_filled_at"] = current_time.isoformat()
            return
        self._mark_signal_status(signal, "entered", "strategy")
        signal["trade_id"] = self._trade_id(trade)

    def _ensure_entry_columns(self, dataframe: DataFrame) -> None:
        if "enter_long" not in dataframe:
            dataframe["enter_long"] = 0
        if "enter_short" not in dataframe:
            dataframe["enter_short"] = 0
        if "enter_tag" not in dataframe:
            dataframe["enter_tag"] = ""

    def _signal_from_context(
        self,
        pair: str,
        entry_tag: str | None,
        trade: Any,
    ) -> dict[str, Any] | bool:
        current_signal_id = self._signal_id_from_tag(entry_tag)
        if not current_signal_id and trade:
            current_signal_id = self._signal_id_from_tag(getattr(trade, "entry_tag", None))
        if current_signal_id:
            signal = self.signals_by_id.get(current_signal_id)
            if signal:
                return signal
        pair_signals = self.entry_signals_by_pair.get(pair, [])
        if pair_signals:
            return pair_signals[-1]
        return False

    def _signal_id_from_tag(self, entry_tag: str | None) -> str | bool:
        if not entry_tag:
            return False
        marker = "sig:"
        marker_index = entry_tag.find(marker)
        if marker_index < 0:
            return False
        value = entry_tag[marker_index + len(marker) :].strip()
        dca_index = value.find(":dca:")
        if dca_index >= 0:
            value = value[:dca_index]
        space_index = value.find(" ")
        if space_index >= 0:
            value = value[:space_index]
        if not value:
            return False
        return value

    def _resolve_entry_price(self, signal: dict[str, Any], proposed_rate: float) -> float:
        entry = self._dict_value(signal, "entry", "entry_price", "entry_policy")
        mode = self._string_value(entry, "type", "mode")
        if mode.startswith("cmp"):
            return float(proposed_rate)
        price = self._float_value(entry, "primary_price", "price", "limit_price")
        if price is not False:
            return float(price)
        lower = self._float_value(entry, "price_min", "min_price", "lower")
        upper = self._float_value(entry, "price_max", "max_price", "upper")
        if lower is not False and upper is not False:
            return float((lower + upper) / 2.0)
        return float(proposed_rate)

    def _signal_leverage(self, signal: dict[str, Any], exchange_max_leverage: float) -> float:
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
        leverage = min(leverage, exchange_max_leverage)
        return float(leverage)

    def _equity(self) -> float:
        wallets = getattr(self, "wallets", False)
        if wallets and hasattr(wallets, "get_total_stake_amount"):
            return float(wallets.get_total_stake_amount())
        if wallets and hasattr(wallets, "get_total"):
            stake_currency = self.config.get("stake_currency", "USDT")
            return float(wallets.get_total(stake_currency))
        return self.default_equity

    def _risk_pct(self, source: Any) -> float:
        risk_pct = self._float_value(source, "risk_pct", "risk_percent")
        if risk_pct is False:
            return self.default_risk_pct
        return float(risk_pct)

    def _clamp_stake(self, stake: float, min_stake: float | None, max_stake: float) -> float:
        result = stake
        if min_stake is not None:
            result = max(result, min_stake)
        result = min(result, max_stake)
        return float(result)

    def _take_profit_reached(self, current_rate: float, take_profit: float, is_short: bool) -> bool:
        if is_short:
            return current_rate <= take_profit
        return current_rate >= take_profit

    def _dca_levels(self, signal: dict[str, Any]) -> list[Any]:
        levels = self._list_value(signal, "dca", "dca_levels")
        if levels:
            return levels
        entry = self._dict_value(signal, "entry")
        prices = self._list_value(entry, "dca_prices")
        result = []
        for price in prices:
            result.append({"price": price})
        return result

    def _dca_price_reached(self, current_rate: float, price: float, is_short: bool) -> bool:
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
            return False
        stop_distance = abs(current_rate - stop_loss)
        if stop_distance == 0:
            return False
        risk_pct = self._float_value(level, "risk_pct", "risk_percent")
        if risk_pct is False:
            risk_pct = self._risk_pct(signal)
        leverage = self._signal_leverage(signal, self.max_strategy_leverage)
        quantity = self._equity() * risk_pct / stop_distance
        stake = quantity * current_rate / leverage
        return self._clamp_stake(stake, min_stake, max_stake)

    def _breakeven_adjusted_stop_loss(
        self,
        pair: str,
        trade: Any,
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
        is_short = bool(getattr(trade, "is_short", False))
        if is_short:
            breakeven_stop = entry_rate * (1.0 - fee_buffer)
            adjusted_stop = float(min(stop_loss, breakeven_stop))
            if adjusted_stop != float(stop_loss):
                self._notify_breakeven_stoploss(pair, trade, signal, adjusted_stop)
            return adjusted_stop

        breakeven_stop = entry_rate * (1.0 + fee_buffer)
        adjusted_stop = float(max(stop_loss, breakeven_stop))
        if adjusted_stop != float(stop_loss):
            self._notify_breakeven_stoploss(pair, trade, signal, adjusted_stop)
        return adjusted_stop

    def _notify_breakeven_stoploss(
        self,
        pair: str,
        trade: Any,
        signal: dict[str, Any],
        stop_loss: float,
    ) -> None:
        dp = getattr(self, "dp", False)
        if not dp or not hasattr(dp, "send_msg"):
            return

        current_signal_id = signal_id(signal)
        notify_key = (self._trade_id(trade), str(current_signal_id), round(stop_loss, 8))
        if notify_key in self._breakeven_notifications:
            return

        self._breakeven_notifications.add(notify_key)
        side = "short" if bool(getattr(trade, "is_short", False)) else "long"
        message = f"Breakeven stoploss active: {pair} {side} stop={stop_loss:.8g}"
        if current_signal_id:
            message = f"{message} signal={current_signal_id}"
        try:
            dp.send_msg(message)
        except Exception:
            self._breakeven_notifications.discard(notify_key)

    def _apply_stoploss_directive(
        self,
        pair: str,
        trade: Any,
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
        trade_leverage = getattr(trade, "leverage", 1.0)
        if not trade_leverage:
            trade_leverage = 1.0
        return stoploss_from_absolute(
            price,
            current_rate,
            is_short=bool(getattr(trade, "is_short", False)),
            leverage=float(trade_leverage),
        )

    def _first_pending_directive(self, pair: str, kind: str) -> Any:
        store = self.signal_store
        if not store or not hasattr(store, "get_pending_directives"):
            return False
        normalized_pair = self._normalize_pair(pair)
        directives = store.get_pending_directives(normalized_pair)
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

    def _directive_block_reason(self) -> str | bool:
        strategy_config = self._strategy_config()
        if strategy_config.get("kill_switch_enabled"):
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

    def _breakeven_entry_rate(self, trade: Any, signal: dict[str, Any]) -> float | bool:
        entry_rate = self._float_value(trade, "open_rate", "entry_rate", "enter_rate")
        if entry_rate is not False:
            return entry_rate
        return self._resolve_entry_price(signal, 0.0)

    def _normalize_pair(self, pair: Any) -> str:
        if pair is None:
            return ""
        raw_pair = str(pair).strip().upper()
        if not raw_pair:
            return ""
        if ":" in raw_pair or "/" in raw_pair:
            return raw_pair
        if raw_pair.endswith("USDT") and raw_pair != "USDT":
            base = raw_pair[:-4]
            return f"{base}/USDT:USDT"
        return raw_pair

    def _pair_base(self, pair: str) -> str:
        normalized_pair = self._normalize_pair(pair)
        base = normalized_pair.split("/", 1)[0]
        return base.upper()

    def _pct_to_ratio(self, value: float) -> float:
        if value > 1.0:
            return value / 100.0
        return value

    def _mark_signal_status(
        self,
        signal: dict[str, Any],
        target_status: str,
        actor: str,
    ) -> bool:
        current_signal_id = signal_id(signal)
        if not current_signal_id:
            return False
        store = self.signal_store
        if store and hasattr(store, "transition_signal"):
            status_value: Any = target_status
            if SignalStatus:
                status_value = SignalStatus(target_status)
            result = store.transition_signal(current_signal_id, status_value, actor)
            ok = getattr(result, "ok", False)
            if ok is False and result is not True:
                signal["status_writeback_failed"] = True
                return False
        signal["status"] = target_status
        return True

    def _reserve_entry_signal(self, signal: dict[str, Any]) -> bool:
        current_signal_id = signal_id(signal)
        if not current_signal_id:
            return False
        store = self.signal_store
        if store and hasattr(store, "reserve_signal"):
            result = store.reserve_signal(current_signal_id, operation_type="entry")
            return self._reservation_succeeded(result)
        if store and hasattr(store, "reserve"):
            result = store.reserve(current_signal_id, "entry")
            return self._reservation_succeeded(result)
        return True

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

    def _trade_id(self, trade: Any) -> Any:
        trade_id = getattr(trade, "id", False)
        if trade_id is not False:
            return trade_id
        trade_id = getattr(trade, "trade_id", False)
        if trade_id is not False:
            return trade_id
        return getattr(trade, "pair", "")

    def _dict_value(self, source: Any, *names: str) -> dict[str, Any]:
        value = self._raw_value(source, *names)
        if isinstance(value, dict):
            return value
        return {}

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

    def _list_value(self, source: Any, *names: str) -> list[Any]:
        value = self._raw_value(source, *names)
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        if value:
            return [value]
        return []
