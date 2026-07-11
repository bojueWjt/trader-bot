from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Callable, Iterable, Optional
from uuid import UUID

from strategy.intent_execution_planner import (
    InstrumentSpec,
    ManagementPlan,
    OrderDenied,
    OrderPlan,
    OrderSnapshot,
    PlannerContext,
    PositionSnapshot,
    decode_client_order_id,
    encode_client_order_id,
    plan_intent_execution,
    _rounded_positive,
)


try:  # pragma: no cover - Nautilus is unavailable on local Py3.14 dev hosts.
    from nautilus_trader.trading.strategy import Strategy  # type: ignore[import-not-found]
    from nautilus_trader.trading.config import StrategyConfig  # type: ignore[import-not-found]

    class IntentExecutionStrategyConfig(StrategyConfig, frozen=True):  # type: ignore[call-arg,misc]
        """Serializable Nautilus ``StrategyConfig`` (msgspec.Struct). Runtime-only
        collaborators (e.g. the node trading-state getter) are injected after
        construction via ``set_trading_state_getter``, never stored in config."""

        account_id: str = ""
        node_id: str = ""
        trading_state: str = "HALTED"
        existing_intent_ids: tuple[str, ...] = ()

except ImportError:  # pragma: no cover - local dev fallback without Nautilus

    class Strategy:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.config = kwargs.get("config") or (args[0] if args else None)

    @dataclass(frozen=True)
    class IntentExecutionStrategyConfig:  # type: ignore[no-redef]
        account_id: str = ""
        node_id: str = ""
        trading_state: str = "HALTED"
        existing_intent_ids: tuple[str, ...] = ()


class IntentExecutionStrategy(Strategy):
    """Execute approved open/add intents as Binance USDM entry orders.

    Mapping:
    - ``open_position`` + ``market`` -> Nautilus ``MARKET`` entry order.
    - ``open_position`` + ``limit`` -> Nautilus ``LIMIT`` entry order.
    - ``open_position`` + ``zone`` -> Nautilus ``LIMIT`` at the aggressive
      boundary: buy uses ``price_max``; sell uses ``price_min``.
    - ``add_position`` uses the same order mapping, but requires an existing
      same-side position.

    Client order id scheme is documented in ``encode_client_order_id``:
    ``B{intent_uuid_hex}{sequence:02d}``, which can be decoded back to the full
    intent id. Tags carry the full intent trace.
    """

    def __init__(self, config: IntentExecutionStrategyConfig) -> None:
        try:
            super().__init__(config=config)
        except TypeError:
            super().__init__(config)
        self._processed_intent_ids: set[str] = set(config.existing_intent_ids)
        self.denials: list[OrderDenied] = []
        self._trading_state_getter: Optional[Callable[[], Any]] = None
        self._denial_reporter: Optional[Callable[[Any, OrderDenied], None]] = None
        self._entry_protection_stash: dict[str, dict[str, Any]] = {}

    def set_trading_state_getter(self, getter: Optional[Callable[[], Any]]) -> None:
        """Inject the node's live trading-state source. Kept out of the serializable
        StrategyConfig; node wiring calls this after construction."""
        self._trading_state_getter = getter

    def set_denial_reporter(self, reporter: Optional[Callable[[Any, OrderDenied], None]]) -> None:
        """Inject best-effort denial reporting without making StrategyConfig carry
        non-serializable runtime clients."""
        self._denial_reporter = reporter

    def on_start(self) -> None:
        # C-08 host-verify fix: subscribe_data(data_type) is rejected for clientless
        # custom data in Nautilus 1.227.0 (it requires client_id/instrument_id).
        # Approved intents are internal actor->strategy data, so deliver them over the
        # msgbus on a controlled per-account topic the IntentPublisher publishes to.
        self.msgbus.subscribe(  # type: ignore[attr-defined]
            topic=f"intents.{self.config.account_id}",
            handler=self._on_intent_msg,
        )
        # Kill-switch operator actions (cancel_all/close_all) are routed here by the
        # CommandPollerActor: only a Strategy may submit/cancel/close on Nautilus.
        self.msgbus.subscribe(  # type: ignore[attr-defined]
            topic=f"node.commands.{self.config.account_id}",
            handler=self._on_node_command,
        )
        self._entry_protection_stash = self._load_entry_protection_stash()
        self._schedule_startup_protection_syncs()

    _PROTECTION_STASH_FILENAME = "protection_stash.json"
    _PROTECTION_STASH_TUPLE_FIELDS = frozenset(
        {"take_profits", "entry_tags", "protection_ids", "take_profit_quantities"}
    )
    _PROTECTION_STASH_TRANSIENT_FIELDS = frozenset(
        {"sync_scheduled", "inline_sync_running", "sync_retries"}
    )

    def _protection_stash_path(self) -> str:
        return os.path.join(
            os.environ.get("NODE_STATE_DIR") or "/state",
            self._PROTECTION_STASH_FILENAME,
        )

    def _load_entry_protection_stash(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self._protection_stash_path(), "r") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        loaded: dict[str, dict[str, Any]] = {}
        for intent_key, value in raw.items():
            if not isinstance(value, dict):
                continue
            clean: dict[str, Any] = {}
            for key, item in value.items():
                if self._is_protection_stash_transient_key(str(key)):
                    continue
                if key in self._PROTECTION_STASH_TUPLE_FIELDS and isinstance(item, list):
                    clean[str(key)] = tuple(item)
                else:
                    clean[str(key)] = item
            loaded[str(intent_key)] = clean
        return loaded

    def _persist_entry_protection_stash(self) -> None:
        path = self._protection_stash_path()
        directory = os.path.dirname(path)
        tmp_path = os.path.join(directory, f".{self._PROTECTION_STASH_FILENAME}.tmp.{id(self)}")
        try:
            os.makedirs(directory, exist_ok=True)
            payload = {
                str(intent_key): self._jsonable_protection_stash_value(value)
                for intent_key, value in self._entry_protection_stash.items()
                if isinstance(value, dict)
            }
            with open(tmp_path, "w") as fh:
                json.dump(payload, fh, sort_keys=True, separators=(",", ":"), default=str)
            os.replace(tmp_path, path)
        except Exception as exc:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            log = getattr(self, "log", None)
            if log is not None and hasattr(log, "error"):
                log.error(f"protection stash persist failed: {exc!r}")

    def _jsonable_protection_stash_value(self, value: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in value.items():
            key = str(key)
            if self._is_protection_stash_transient_key(key):
                continue
            if isinstance(item, tuple):
                result[key] = list(item)
            else:
                result[key] = item
        return result

    def _is_protection_stash_transient_key(self, key: str) -> bool:
        return key.startswith("_") or key in self._PROTECTION_STASH_TRANSIENT_FIELDS

    def _schedule_startup_protection_syncs(self) -> None:
        clock = getattr(self, "clock", None)
        if clock is None or getattr(clock, "set_time_alert", None) is None:
            return
        for intent_key, stash in tuple(self._entry_protection_stash.items()):
            instrument_id = str(stash.get("instrument_id", ""))
            if not instrument_id:
                continue
            try:
                has_position = bool(tuple(_nonzero_positions(self._cache_positions(instrument_id))))
            except Exception:
                has_position = False
            if has_position:
                self._schedule_protection_sync(intent_key, delay_seconds=15.0)

    def _on_intent_msg(self, intent: Any) -> None:
        # msgbus delivers the ApprovedTradeIntentV1 directly.
        if intent is not None:
            self._handle_intent(intent)

    def _on_node_command(self, cmd: Any) -> None:
        """Execute operator cancel_all / close_all. Best-effort per item: a failure on
        one order/position is recorded but does not stop the rest (kill-switch must be
        as complete as possible)."""
        ctype = getattr(cmd, "type", cmd)
        ctype = str(getattr(ctype, "value", ctype))
        if ctype == "cancel_all":
            for order in self._all_open_orders():
                try:
                    self.cancel_order(order)  # type: ignore[attr-defined]
                except Exception as exc:
                    self._record_denial(OrderDenied("order_cancel_failed", repr(exc)))
        elif ctype == "close_all":
            for position in self._all_open_positions():
                try:
                    # Nautilus submits a reduce-only market order to flatten.
                    self.close_position(position)  # type: ignore[attr-defined]
                except Exception as exc:
                    self._record_denial(OrderDenied("position_close_failed", repr(exc)))

    def _all_open_orders(self) -> tuple[Any, ...]:
        cache = getattr(self, "cache", None)
        if cache is None:
            return ()
        for name in ("orders_open", "orders"):
            method = getattr(cache, name, None)
            if method is None:
                continue
            try:
                return tuple(method() or ())
            except TypeError:
                continue
        return ()

    def _all_open_positions(self) -> tuple[Any, ...]:
        cache = getattr(self, "cache", None)
        if cache is None:
            return ()
        for name in ("positions_open", "positions"):
            method = getattr(cache, name, None)
            if method is None:
                continue
            try:
                return tuple(
                    p for p in (method() or ())
                    if Decimal(str(_position_quantity(p) or 0)) != 0
                )
            except TypeError:
                continue
        return ()

    def on_data(self, data: Any) -> None:
        # Retained for the publish_data path / tests: unwrap CustomData if used.
        intent = _intent_from_custom_data(data)
        if intent is None:
            return
        self._handle_intent(intent)

    def _handle_intent(self, intent: Any) -> None:
        action = str(getattr(getattr(intent, "action", ""), "value", getattr(intent, "action", "")))
        context = PlannerContext(
            account_id=self.config.account_id,
            trading_state=self._trading_state(),
            now=self._now(),
            instrument=self._instrument_spec(str(intent.instrument_id)),
            position=self._position_snapshot(str(intent.instrument_id)),
            positions=self._position_snapshots(str(intent.instrument_id)),
            existing_orders=self._order_snapshots(str(intent.instrument_id)),
            existing_intent_ids=frozenset(
                self._processed_intent_ids | self._active_intent_ids(intent.instrument_id)
            ),
        )
        raw_order_plan = getattr(intent, "order_plan", {}) or {}
        if str(raw_order_plan.get("type", "")).lower() == "zone_ladder":
            self._handle_zone_ladder(intent, raw_order_plan, context, action)
            return

        result = plan_intent_execution(intent, context)
        if isinstance(result, OrderDenied):
            self._record_denial(result)
            self._report_denial(intent, result)
            return

        if isinstance(result, ManagementPlan):
            submitted = self._submit_management_plan(result)
        else:
            if action in ("open_position", "add_position"):
                self._stash_entry_protection(intent, result)
            submitted = self._submit_order_plan(result)
        if submitted:
            self._processed_intent_ids.add(str(result.intent_id))
        else:
            if not isinstance(result, ManagementPlan):
                self._entry_protection_stash.pop(str(result.intent_id), None)
                self._persist_entry_protection_stash()
            denial = self.denials[-1] if self.denials else OrderDenied(
                "order_submit_failed",
                str(result.intent_id),
            )
            self._report_denial(intent, denial)

    def _stash_entry_protection(self, intent: Any, plan: OrderPlan) -> None:
        order_plan = getattr(intent, "order_plan", {}) or {}
        stop_loss = order_plan.get("stop_loss")
        take_profits = order_plan.get("take_profits")
        if stop_loss is None and not take_profits:
            return
        # One protection owner per instrument+side: a newer entry intent (e.g.
        # add_position) supersedes the older stash — its sync adopts the whole
        # position via position-scoped ownership, so two stashes would just fight.
        for key, other in tuple(self._entry_protection_stash.items()):
            if (
                str(other.get("instrument_id")) == plan.instrument_id
                and str(other.get("entry_side")) == plan.side
            ):
                self._entry_protection_stash.pop(key, None)
                self._cancel_clock_timer(self._PROTECTION_TIMER_PREFIX + key)
        # protection_sequence_start=11 for all paths: revisioned protection ids live
        # in the 11..99 space (see _protection_order_plans), entries stay in 1..9.
        self._entry_protection_stash[str(plan.intent_id)] = {
            "stop_loss": stop_loss,
            "take_profits": tuple(take_profits or ()),
            "instrument_id": plan.instrument_id,
            "entry_side": plan.side,
            "entry_tags": plan.tags,
            "entry_sequence_max": 1,
            "protection_sequence_start": 11,
        }
        self._persist_entry_protection_stash()

    def _handle_zone_ladder(
        self,
        intent: Any,
        order_plan: dict[str, Any],
        context: PlannerContext,
        action: str,
    ) -> None:
        plans = self._zone_ladder_order_plans(intent, order_plan, context, action)
        if isinstance(plans, OrderDenied):
            self._record_denial(plans)
            self._report_denial(intent, plans)
            return

        self._stash_entry_protection(intent, plans[0])
        stash = self._entry_protection_stash.get(str(plans[0].intent_id))
        if stash is not None:
            stash["entry_sequence_max"] = 9
            stash["protection_sequence_start"] = 11
            self._persist_entry_protection_stash()
        submitted = True
        submitted_plans: list[OrderPlan] = []
        for plan in plans:
            if not self._submit_order_plan(plan):
                submitted = False
                break
            submitted_plans.append(plan)

        if submitted:
            self._processed_intent_ids.add(str(plans[0].intent_id))
        else:
            # Best-effort rollback of rungs already sent; KEEP the stash either way:
            # a rung that survives the cancel attempt and fills later must still get
            # protections (an orphan stash is harmless, a naked fill is not).
            for plan in submitted_plans:
                self._cancel_order_by_client_order_id(plan.instrument_id, plan.client_order_id)
            denial = self.denials[-1] if self.denials else OrderDenied(
                "order_submit_failed",
                str(plans[0].intent_id),
            )
            self._report_denial(intent, denial)

    def _zone_ladder_order_plans(
        self,
        intent: Any,
        order_plan: dict[str, Any],
        context: PlannerContext,
        action: str,
    ) -> tuple[OrderPlan, ...] | OrderDenied:
        if action not in ("open_position", "add_position"):
            return OrderDenied("unsupported_action", action)

        trading_state = str(getattr(context.trading_state, "value", context.trading_state)).upper()
        if trading_state != "ACTIVE":
            return OrderDenied("trading_not_active", trading_state)

        intent_id = getattr(intent, "intent_id")
        if str(intent_id) in context.existing_intent_ids:
            return OrderDenied("duplicate_intent", str(intent_id))
        if getattr(intent, "account_id") != context.account_id:
            return OrderDenied("wrong_account", str(getattr(intent, "account_id")))

        instrument_id = str(getattr(intent, "instrument_id"))
        instrument = context.instrument
        if instrument is None or instrument.instrument_id != instrument_id:
            return OrderDenied("instrument_not_found", instrument_id)

        try:
            if _aware_datetime(getattr(intent, "valid_until")) <= _aware_datetime(context.now):
                return OrderDenied("expired", _aware_datetime(getattr(intent, "valid_until")).isoformat())
        except Exception:
            return OrderDenied("expired", str(getattr(intent, "valid_until", "")))

        side_raw = str(order_plan.get("side", "")).lower()
        if side_raw == "buy":
            side = "BUY"
        elif side_raw == "sell":
            side = "SELL"
        else:
            return OrderDenied("unsupported_order_spec", f"side={side_raw}")

        position_denial = _entry_position_denial(action, side, context.position, instrument_id)
        if position_denial is not None:
            return position_denial

        tranches = order_plan.get("tranches")
        if not isinstance(tranches, (list, tuple)) or len(tranches) != 3:
            return OrderDenied("unsupported_order_spec", "zone_ladder.tranches")

        tags = _entry_tags(intent, action)
        plans: list[OrderPlan] = []
        for index, tranche in enumerate(tranches):
            if not isinstance(tranche, dict):
                return OrderDenied("unsupported_order_spec", f"tranches[{index}]")
            try:
                sequence = int(tranche.get("seq"))
            except (TypeError, ValueError):
                return OrderDenied("unsupported_order_spec", f"tranches[{index}].seq")
            if sequence != index + 1 or sequence < 1 or sequence > 9:
                return OrderDenied("unsupported_order_spec", f"tranches[{index}].seq")

            quantity = _rounded_positive(
                tranche.get("quantity"),
                instrument.quantity_increment,
                f"tranches[{index}].quantity",
            )
            if isinstance(quantity, OrderDenied):
                return quantity
            price = _rounded_positive(
                tranche.get("price"),
                instrument.price_increment,
                f"tranches[{index}].price",
            )
            if isinstance(price, OrderDenied):
                return price

            plans.append(
                OrderPlan(
                    intent_id=intent_id,
                    client_order_id=encode_client_order_id(intent_id, sequence=sequence),
                    tags=tags,
                    instrument_id=instrument_id,
                    side=side,
                    order_type="LIMIT",
                    quantity=quantity,
                    price=price,
                    time_in_force="GTC",
                )
            )
        return tuple(plans)

    # Protection lifecycle (SL + TP tiers) for entry intents.
    #
    # Rewritten 2026-07-03 after two live incidents:
    # 1. WLDUSDT market entry: ~20 partial fills each re-planned protections with the
    #    SAME deterministic client ids -> every re-submit denied duplicate; SL stayed
    #    sized to the FIRST partial fill (14 of 1430), TP never reached the venue.
    # 2. BTCUSDT zone ladder: rung 2/3 fills cancelled the rung-1-sized protections to
    #    resize them, but the re-place step skipped every plan because the freshly
    #    PENDING_CANCEL orders were still in the open-orders cache -> naked position.
    #
    # Design now: fill events only SCHEDULE a debounced sync (clock alert, 1.5s).
    # The sync is idempotent and convergent: it compares desired vs live protection
    # orders, and when they differ cancels the live set and places a NEW REVISION
    # with fresh client ids (revision r uses sequences 10*(r+1)+1 .. 10*(r+1)+9, so
    # ids are never reused). If any of this intent's protection orders are still
    # in flight (INITIALIZED/SUBMITTED/PENDING_*), the sync reschedules instead of
    # racing them. If no clock alert API is available, the sync runs inline.

    _PROTECTION_SYNC_DELAY_S = 1.5
    _PROTECTION_MAX_REVISION = 8  # revision 8 -> sequences 91..99 (2-digit id ceiling)
    _PROTECTION_MAX_RETRIES = 40
    _PROTECTION_INFLIGHT_STATUSES = frozenset(
        {"INITIALIZED", "EMULATED", "RELEASED", "SUBMITTED", "PENDING_UPDATE", "PENDING_CANCEL"}
    )
    _PROTECTION_TERMINAL_STATUSES = frozenset(
        {"DENIED", "REJECTED", "CANCELED", "EXPIRED", "FILLED"}
    )
    _PROTECTION_TIMER_PREFIX = "protsync-"

    def on_order_filled(self, event: Any) -> None:
        client_order_id = _event_client_order_id(event)
        if client_order_id is None:
            return
        try:
            trace = decode_client_order_id(client_order_id)
        except ValueError:
            return
        stash = self._entry_protection_stash.get(str(trace.intent_id))
        if stash is not None:
            entry_sequence_max = int(stash.get("entry_sequence_max", 1))
            if 1 <= trace.sequence <= entry_sequence_max:
                self._schedule_protection_sync(str(trace.intent_id))
            return
        # Entry fill of a superseded intent (its stash was replaced by a newer one
        # on the same instrument): the position size changed, so the surviving
        # stash must resize its protections.
        if trace.sequence <= 9:
            instrument_id = _event_instrument_id(event)
            if instrument_id:
                for key, other in tuple(self._entry_protection_stash.items()):
                    if str(other.get("instrument_id")) == instrument_id:
                        self._schedule_protection_sync(key)

    def on_stop(self) -> None:
        for intent_key in tuple(self._entry_protection_stash):
            self._cancel_clock_timer(self._PROTECTION_TIMER_PREFIX + intent_key)

    def on_event(self, event: Any) -> None:
        # TimeEvent fallback path for clocks whose set_time_alert has no callback arg.
        name = getattr(event, "name", None)
        if name is not None and str(name).startswith(self._PROTECTION_TIMER_PREFIX):
            self._sync_protection(str(name)[len(self._PROTECTION_TIMER_PREFIX):])

    # A protection order dying at/before the venue (rejected/denied/expired, or
    # cancelled by something other than this strategy) is invisible to the fill
    # path — without these hooks a venue rejection (e.g. Binance -2021 "would
    # immediately trigger") leaves the position naked exactly when price is
    # attacking the stop. Any terminal event on a protection id re-arms the sync.
    def on_order_rejected(self, event: Any) -> None:
        self._on_protection_order_terminal(event, count_retry=True)

    def on_order_denied(self, event: Any) -> None:
        self._on_protection_order_terminal(event, count_retry=True)

    def on_order_canceled(self, event: Any) -> None:
        # Usually our own make-before-break cancel confirmations: resync to
        # verify convergence, but do NOT feed the backoff counter (review P2-3).
        self._on_protection_order_terminal(event, count_retry=False)

    def on_order_expired(self, event: Any) -> None:
        self._on_protection_order_terminal(event, count_retry=True)

    def _on_protection_order_terminal(self, event: Any, count_retry: bool = True) -> None:
        client_order_id = _event_client_order_id(event)
        if client_order_id is None:
            return
        try:
            trace = decode_client_order_id(client_order_id)
        except ValueError:
            return
        intent_key = str(trace.intent_id)
        stash = self._entry_protection_stash.get(intent_key)
        adopted = False
        if stash is None or (
            trace.sequence < int(stash.get("protection_sequence_start", 11))
            and client_order_id not in tuple(stash.get("protection_ids") or ())
        ):
            for key, other in self._entry_protection_stash.items():
                if client_order_id in tuple(other.get("protection_ids") or ()):
                    intent_key = key
                    stash = other
                    adopted = True
                    break
        if stash is None:
            return
        if (
            not adopted
            and trace.sequence < int(stash.get("protection_sequence_start", 11))
            and client_order_id not in tuple(stash.get("protection_ids") or ())
        ):
            return
        if client_order_id in tuple(stash.get("protection_ids") or ()):
            stash["protected_quantity"] = None  # current/adopted revision lost a leg
            self._persist_entry_protection_stash()
        self._reschedule_protection_sync(intent_key, stash, count_retry=count_retry)

    def _on_protection_sync_alert(self, event: Any) -> None:
        name = str(getattr(event, "name", ""))
        if name.startswith(self._PROTECTION_TIMER_PREFIX):
            self._sync_protection(name[len(self._PROTECTION_TIMER_PREFIX):])

    def _schedule_protection_sync(self, intent_key: str, delay_seconds: Optional[float] = None) -> None:
        stash = self._entry_protection_stash.get(intent_key)
        if stash is None:
            return
        # A pending timer already guarantees a sync within the debounce window;
        # re-arming it on every fill would let a steady nibble postpone protection
        # placement indefinitely.
        if stash.get("sync_scheduled"):
            return
        name = self._PROTECTION_TIMER_PREFIX + intent_key
        clock = getattr(self, "clock", None)
        alert_setter = getattr(clock, "set_time_alert", None) if clock is not None else None
        if alert_setter is not None:
            self._cancel_clock_timer(name)
            delay = self._PROTECTION_SYNC_DELAY_S if delay_seconds is None else delay_seconds
            when = self._now() + timedelta(seconds=delay)
            try:
                alert_setter(name, when, callback=self._on_protection_sync_alert)
                stash["sync_scheduled"] = True
                return
            except TypeError:
                try:
                    alert_setter(name, when)
                    stash["sync_scheduled"] = True
                    return
                except Exception:
                    pass
            except Exception:
                pass
        # No usable clock alert API: converge inline (still revision-safe). The
        # reentrancy latch stops reschedule loops from recursing.
        if stash.get("inline_sync_running"):
            return
        stash["inline_sync_running"] = True
        try:
            self._sync_protection(intent_key)
        finally:
            stash.pop("inline_sync_running", None)

    def _cancel_clock_timer(self, name: str) -> None:
        clock = getattr(self, "clock", None)
        cancel = getattr(clock, "cancel_timer", None) if clock is not None else None
        if cancel is None:
            return
        try:
            cancel(name)
        except Exception:
            pass

    def _sync_protection(self, intent_key: str) -> None:
        stash = self._entry_protection_stash.get(intent_key)
        if stash is None:
            return
        stash.pop("sync_scheduled", None)
        instrument_id = str(stash["instrument_id"])
        instrument = self._instrument_spec(instrument_id)
        if instrument is None:
            self._reschedule_protection_sync(intent_key, stash, count_retry=True)
            return
        sequence_start = int(stash.get("protection_sequence_start", 11))
        position = self._protection_position(instrument_id, str(stash["entry_side"]))
        live = self._live_protection_orders(
            instrument_id,
            intent_key,
            sequence_start,
            position_id=_position_id(position) if position is not None else None,
        )
        inflight = [
            order for order in live
            if self._order_status_name(order) in self._PROTECTION_INFLIGHT_STATUSES
        ]
        if position is None:
            # Position gone (closed/flipped) while we debounced: drop leftovers.
            # In-flight orders cannot be cancelled yet — wait for them instead of
            # orphaning a resting reduce-only stop that could clip a future position.
            if inflight:
                self._reschedule_protection_sync(intent_key, stash, count_retry=True)
                return
            for order in live:
                self._cancel_order_object(order)
            self._entry_protection_stash.pop(intent_key, None)
            self._persist_entry_protection_stash()
            self._cancel_clock_timer(self._PROTECTION_TIMER_PREFIX + intent_key)
            return

        quantity = _round_down_positive(
            _position_quantity(position),
            instrument.quantity_increment,
        )
        if quantity is None:
            self._reschedule_protection_sync(intent_key, stash, count_retry=True)
            return

        # Converged: sized for the current position AND every order of the current
        # revision is still alive (a lost leg — rejected SL, user-cancelled TP —
        # fails this check and forces a re-place).
        current_ids = tuple(stash.get("protection_ids") or ())
        live_ids = {str(getattr(order, "client_order_id", "")) for order in live}
        if (
            stash.get("protected_quantity") == quantity
            and current_ids
            and all(cid in live_ids for cid in current_ids)
        ):
            stash.pop("sync_retries", None)
            return

        if inflight:
            self._reschedule_protection_sync(intent_key, stash, count_retry=True)
            return

        plans_for_adoption = self._protection_order_plans(
            UUID(intent_key),
            stash,
            instrument,
            position,
            quantity,
            revision=0,
        )
        adopted_ids = self._adopt_matching_live_protections(
            live,
            plans_for_adoption,
            instrument,
        )
        if adopted_ids is not None:
            stash["protection_ids"] = adopted_ids
            stash["protected_quantity"] = quantity
            stash.pop("sync_retries", None)
            self._persist_entry_protection_stash()
            return

        revision = int(stash.get("protection_revision", -1)) + 1
        if revision > self._PROTECTION_MAX_REVISION:
            # Make-before-break below means the previous revision is still live;
            # keep it rather than churn further. Loud denial for the operator.
            self._record_denial(OrderDenied("protection_revisions_exhausted", intent_key))
            return

        plans = self._protection_order_plans(
            UUID(intent_key),
            stash,
            instrument,
            position,
            quantity,
            revision=revision,
        )
        if not plans:
            return

        # Make-before-break: place the new revision FIRST, cancel the old set only
        # after something actually went out. All protections are reduce-only, so a
        # brief overlap cannot over-close the position; the reverse order (the old
        # code) is what produced a naked position when the re-place step failed.
        stash["protection_revision"] = revision
        submitted_ids: list[str] = []
        for plan in plans:
            if self._submit_order_plan(plan):
                submitted_ids.append(plan.client_order_id)
        if not submitted_ids:
            stash["protected_quantity"] = None
            self._persist_entry_protection_stash()
            self._reschedule_protection_sync(intent_key, stash, count_retry=True)
            return
        keep = set(submitted_ids)
        for order in live:
            if str(getattr(order, "client_order_id", "")) in keep:
                continue
            self._cancel_order_object(order)
        stash["protection_ids"] = tuple(submitted_ids)
        if len(submitted_ids) == len(plans):
            stash["protected_quantity"] = quantity
        else:
            stash["protected_quantity"] = None
        self._persist_entry_protection_stash()
        if len(submitted_ids) != len(plans):
            self._reschedule_protection_sync(intent_key, stash, count_retry=True)

    def _adopt_matching_live_protections(
        self,
        live: tuple[Any, ...],
        expected: tuple[OrderPlan, ...],
        instrument: InstrumentSpec,
    ) -> Optional[tuple[str, ...]]:
        if not expected or len(live) != len(expected):
            return None
        unmatched = list(live)
        adopted: list[str] = []
        for plan in expected:
            match_index = next(
                (
                    index for index, order in enumerate(unmatched)
                    if self._live_order_matches_plan(order, plan, instrument)
                ),
                None,
            )
            if match_index is None:
                return None
            order = unmatched.pop(match_index)
            adopted.append(str(getattr(order, "client_order_id", "")))
        if not adopted or any(not cid for cid in adopted):
            return None
        return tuple(adopted)

    def _live_order_matches_plan(
        self,
        order: Any,
        plan: OrderPlan,
        instrument: InstrumentSpec,
    ) -> bool:
        if getattr(order, "reduce_only", True) is False:
            return False
        order_side = _enum_name(getattr(order, "side", getattr(order, "order_side", "")))
        if order_side and plan.side not in order_side:
            return False
        order_type = _enum_name(getattr(order, "order_type", ""))
        if plan.order_type == "STOP_MARKET":
            if "STOP" not in order_type or "LIMIT" in order_type:
                return False
            order_trigger = _round_down_positive(
                _optional_str(getattr(order, "trigger_price", getattr(order, "stop_price", None))),
                instrument.price_increment,
            )
            if order_trigger != plan.trigger_price:
                return False
            order_quantity = _round_down_positive(
                _optional_str(getattr(order, "quantity", getattr(order, "qty", None))),
                instrument.quantity_increment,
            )
            return order_quantity == plan.quantity
        if plan.order_type == "LIMIT":
            if "LIMIT" not in order_type or "STOP" in order_type:
                return False
            order_price = _round_down_positive(
                _optional_str(getattr(order, "price", None)),
                instrument.price_increment,
            )
            if order_price != plan.price:
                return False
            return _decimal_abs_lte(
                _optional_str(getattr(order, "quantity", getattr(order, "qty", None))),
                plan.quantity,
                instrument.quantity_increment,
            )
        return False

    def _reschedule_protection_sync(
        self,
        intent_key: str,
        stash: dict[str, Any],
        count_retry: bool = False,
    ) -> None:
        delay_seconds: Optional[float] = None
        if count_retry:
            retries = int(stash.get("sync_retries", 0)) + 1
            stash["sync_retries"] = retries
            if retries > self._PROTECTION_MAX_RETRIES:
                self._record_denial(OrderDenied("protection_sync_stuck", intent_key))
                stash.pop("sync_retries", None)
                return
            delay_seconds = min(
                self._PROTECTION_SYNC_DELAY_S * (2 ** (retries - 1)),
                60.0,
            )
        self._schedule_protection_sync(intent_key, delay_seconds=delay_seconds)

    def _live_protection_orders(
        self,
        instrument_id: str,
        intent_key: str,
        sequence_start: int,
        position_id: Optional[str] = None,
    ) -> tuple[Any, ...]:
        """Live protection orders for this position: the syncing intent's own
        revisioned ids PLUS any order tagged lifecycle_role=stop_loss/take_profit
        for the same position book (operator management intents, superseded entry
        intents). Position-scoped ownership prevents duplicate SL/TP stacks."""
        result: dict[str, Any] = {}
        for order in self._cache_orders_all(instrument_id):
            oid = str(getattr(order, "client_order_id", ""))
            if oid in result:
                continue
            if self._order_status_name(order) in self._PROTECTION_TERMINAL_STATUSES:
                continue
            mine = False
            try:
                trace = decode_client_order_id(oid)
                mine = str(trace.intent_id) == intent_key and trace.sequence >= sequence_start
            except ValueError:
                pass
            if not mine and position_id is not None:
                tags = {str(t) for t in (getattr(order, "tags", ()) or ())}
                mine = (
                    f"position_id={position_id}" in tags
                    and (
                        "lifecycle_role=stop_loss" in tags
                        or "lifecycle_role=take_profit" in tags
                    )
                )
            if mine:
                result[oid] = order
        return tuple(result.values())

    def _cache_orders_all(self, instrument_id: Any) -> tuple[Any, ...]:
        """Open orders PLUS in-flight ones (orders_open excludes INITIALIZED/SUBMITTED
        on some cache implementations, which is exactly when races happen)."""
        seen: dict[str, Any] = {}
        for order in self._cache_orders(instrument_id):
            seen[str(getattr(order, "client_order_id", id(order)))] = order
        cache = getattr(self, "cache", None)
        method = getattr(cache, "orders", None) if cache is not None else None
        if method is not None:
            raw = None
            for call in (
                lambda: method(instrument_id=self._as_instrument_id(instrument_id)),
                lambda: method(self._as_instrument_id(instrument_id)),
                lambda: method(),
            ):
                try:
                    raw = call() or ()
                    break
                except TypeError:
                    continue
                except Exception:
                    break
            for order in raw or ():
                if str(getattr(order, "instrument_id", "")) != str(instrument_id):
                    continue
                seen.setdefault(str(getattr(order, "client_order_id", id(order))), order)
        return tuple(seen.values())

    def _order_status_name(self, order: Any) -> str:
        raw = getattr(order, "status", None)
        if raw is None:
            return "UNKNOWN"
        return str(getattr(raw, "name", raw)).upper().replace("ORDERSTATUS.", "")

    def _cancel_order_object(self, order: Any) -> bool:
        try:
            self.cancel_order(order)  # type: ignore[attr-defined]
            return True
        except Exception as exc:
            self._record_denial(OrderDenied("order_cancel_failed", repr(exc)))
            return False

    def _protection_position(self, instrument_id: str, entry_side: str) -> Optional[Any]:
        target_book = "LONG" if entry_side == "BUY" else "SHORT"
        for position in _nonzero_positions(self._cache_positions(instrument_id)):
            if _position_side(position) == target_book:
                return position
        return None

    def _protection_order_plans(
        self,
        intent_id: Any,
        stash: dict[str, Any],
        instrument: InstrumentSpec,
        position: Any,
        quantity: str,
        revision: int = 0,
    ) -> tuple[OrderPlan, ...]:
        instrument_id = str(stash["instrument_id"])
        entry_side = str(stash["entry_side"])
        exit_side = "SELL" if entry_side == "BUY" else "BUY"
        position_id = _position_id(position)
        base_tags = tuple(stash.get("entry_tags") or ())
        # Revision r owns the sequence block 10*(r+1)+1 .. 10*(r+1)+9: SL at +1,
        # TP tier i at +1+i. Client order ids are single-use in Nautilus (resubmit
        # of a used id is denied), so every re-place MUST mint fresh ids.
        if revision < 0 or revision > self._PROTECTION_MAX_REVISION:
            self._record_denial(
                OrderDenied("protection_revisions_exhausted", f"{intent_id}:{revision}")
            )
            return ()
        block = 10 * (revision + 1)
        plans: list[OrderPlan] = []

        stop_loss = stash.get("stop_loss")
        if stop_loss is not None:
            trigger_price = _rounded_positive(stop_loss, instrument.price_increment, "stop_loss")
            if isinstance(trigger_price, OrderDenied):
                self._record_denial(trigger_price)
            else:
                plans.append(
                    OrderPlan(
                        intent_id=intent_id,
                        client_order_id=encode_client_order_id(
                            intent_id,
                            sequence=block + 1,
                        ),
                        tags=_protection_tags(base_tags, "stop_loss", position_id),
                        instrument_id=instrument_id,
                        side=exit_side,
                        order_type="STOP_MARKET",
                        quantity=quantity,
                        price=None,
                        time_in_force="GTC",
                        reduce_only=True,
                        trigger_price=trigger_price,
                    )
                )

        targets = _take_profit_prices(stash.get("take_profits"))[:8]
        quantities = _absorbed_or_split_quantities(
            stash.get("take_profit_quantities"),
            quantity,
            len(targets),
            instrument.quantity_increment,
        )
        for index, (target, target_quantity) in enumerate(zip(targets, quantities), start=1):
            if target_quantity is None:
                continue
            price = _rounded_positive(target, instrument.price_increment, f"take_profits[{index - 1}].price")
            if isinstance(price, OrderDenied):
                self._record_denial(price)
                continue
            plans.append(
                OrderPlan(
                    intent_id=intent_id,
                    client_order_id=encode_client_order_id(
                        intent_id,
                        sequence=block + 1 + index,
                    ),
                    tags=_protection_tags(base_tags, "take_profit", position_id, index=index),
                    instrument_id=instrument_id,
                    side=exit_side,
                    order_type="LIMIT",
                    quantity=target_quantity,
                    price=price,
                    time_in_force="GTC",
                    reduce_only=True,
                )
            )
        return tuple(plans)

    def _trading_state(self) -> str:
        if self._trading_state_getter is not None:
            raw_state = self._trading_state_getter()
        else:
            raw_state = self.config.trading_state
        return str(getattr(raw_state, "value", raw_state))

    def _now(self) -> datetime:
        clock = getattr(self, "clock", None)
        if clock is not None and hasattr(clock, "utc_now"):
            return clock.utc_now()
        return datetime.now(timezone.utc)

    def _instrument_spec(self, instrument_id: str) -> Optional[InstrumentSpec]:
        instrument = self._cache_instrument(instrument_id)
        if instrument is None:
            return None
        price_increment = _extract_increment(
            instrument,
            increment_names=("price_increment", "price_tick", "tick_size"),
            precision_names=("price_precision",),
        )
        quantity_increment = _extract_increment(
            instrument,
            increment_names=("size_increment", "quantity_increment", "lot_size", "step_size"),
            precision_names=("size_precision", "quantity_precision"),
        )
        if price_increment is None or quantity_increment is None:
            self._record_denial(
                OrderDenied("instrument_precision_unavailable", instrument_id)
            )
            return None
        return InstrumentSpec(
            instrument_id=instrument_id,
            price_increment=price_increment,
            quantity_increment=quantity_increment,
        )

    def _position_snapshot(self, instrument_id: str) -> Optional[PositionSnapshot]:
        position = _first_nonzero_position(self._cache_positions(instrument_id))
        if position is None:
            return None
        return self._position_snapshot_from_cache(position, instrument_id)

    def _position_snapshots(self, instrument_id: str) -> tuple[PositionSnapshot, ...]:
        return tuple(
            self._position_snapshot_from_cache(position, instrument_id)
            for position in _nonzero_positions(self._cache_positions(instrument_id))
        )

    def _position_snapshot_from_cache(self, position: Any, instrument_id: str) -> PositionSnapshot:
        return PositionSnapshot(
            instrument_id=instrument_id,
            side=_position_side(position),
            quantity=_position_quantity(position),
            position_id=_position_id(position),
            entry_price=_position_entry_price(position),
        )

    def _order_snapshots(self, instrument_id: str) -> tuple[OrderSnapshot, ...]:
        snapshots: list[OrderSnapshot] = []
        for order in self._cache_orders(instrument_id):
            client_order_id = getattr(order, "client_order_id", None)
            if client_order_id is None:
                continue
            snapshots.append(
                OrderSnapshot(
                    client_order_id=str(client_order_id),
                    instrument_id=str(getattr(order, "instrument_id", instrument_id)),
                    order_type=_enum_name(getattr(order, "order_type", "")),
                    side=_enum_name(getattr(order, "side", getattr(order, "order_side", ""))),
                    quantity=str(getattr(order, "quantity", getattr(order, "qty", ""))),
                    price=_optional_str(getattr(order, "price", None)),
                    trigger_price=_optional_str(
                        getattr(order, "trigger_price", getattr(order, "stop_price", None))
                    ),
                    tags=tuple(str(tag) for tag in (getattr(order, "tags", ()) or ())),
                )
            )
        return tuple(snapshots)

    def _active_intent_ids(self, instrument_id: Any) -> set[str]:
        ids: set[str] = set()
        for item in list(self._cache_orders(instrument_id)) + list(
            self._cache_positions(str(instrument_id))
        ):
            ids.update(_intent_ids_from_tags(item))
            client_order_id = getattr(item, "client_order_id", None)
            if client_order_id is not None:
                try:
                    ids.add(str(decode_client_order_id(str(client_order_id)).intent_id))
                except ValueError:
                    pass
        return ids

    def _cache_instrument(self, instrument_id: str) -> Any:
        cache = getattr(self, "cache", None)
        if cache is None or not hasattr(cache, "instrument"):
            return None
        try:
            # TODO(host-verify): confirm whether cache.instrument requires
            # InstrumentId.from_str(...) instead of a string in Nautilus 1.227.0.
            return cache.instrument(instrument_id)
        except TypeError:
            try:
                from nautilus_trader.model.identifiers import InstrumentId  # type: ignore

                return cache.instrument(InstrumentId.from_str(instrument_id))
            except Exception:
                return None

    def _cache_orders(self, instrument_id: Any) -> Iterable[Any]:
        cache = getattr(self, "cache", None)
        if cache is None:
            return ()
        # TODO(host-verify): confirm active/open order cache method names on
        # Nautilus 1.227.0. These candidates keep the strategy fail-closed.
        for name in ("orders_open", "orders_active", "open_orders"):
            method = getattr(cache, name, None)
            if method is None:
                continue
            try:
                return method(instrument_id) or ()
            except TypeError:
                try:
                    return method() or ()
                except TypeError:
                    continue
        return ()

    def _cache_positions(self, instrument_id: str) -> Iterable[Any]:
        cache = getattr(self, "cache", None)
        if cache is None:
            return ()
        target = str(instrument_id)
        iid = self._as_instrument_id(instrument_id)
        # Prefer positions_open (excludes closed positions). Try an InstrumentId-typed
        # scoped query first, then a string arg, then the no-arg form.
        # TODO(host-verify): confirm open-position cache method names on Nautilus 1.227.0.
        for name in ("positions_open", "positions", "open_positions"):
            method = getattr(cache, name, None)
            if method is None:
                continue
            raw = None
            for call in (lambda: method(iid), lambda: method(instrument_id), lambda: method()):
                try:
                    raw = call() or ()
                    break
                except TypeError:
                    continue
            if raw is None:
                continue
            # ALWAYS filter by instrument: a no-arg (unscoped) result must never leak
            # other instruments' positions, e.g. an open ETH blocking a BTC open
            # (position_exists false-positive). Closed positions are also excluded here.
            return [
                p for p in raw
                if str(getattr(p, "instrument_id", "")) == target
                and Decimal(str(_position_quantity(p) or 0)) != 0
            ]
        return ()

    def _as_instrument_id(self, instrument_id: str) -> Any:
        try:
            from nautilus_trader.model.identifiers import InstrumentId  # type: ignore

            return InstrumentId.from_str(str(instrument_id))
        except Exception:
            return str(instrument_id)

    def _submit_order_plan(self, plan: OrderPlan) -> bool:
        instrument = self._cache_instrument(plan.instrument_id)
        if instrument is None:
            self._record_denial(OrderDenied("instrument_not_found", plan.instrument_id))
            return False

        try:
            # NOTE: order kwargs may drop the internal reduce_only flag (external
            # position quirk, see _plan_for_submission) but the position BOOK is
            # always derived from the ORIGINAL plan's semantics.
            order = self._build_nautilus_order(self._plan_for_submission(plan), instrument)
            position_id = self._hedge_position_id(order, plan)
            if position_id is not None:
                self.submit_order(order, position_id=position_id)  # type: ignore[attr-defined]
            else:
                self.submit_order(order)  # type: ignore[attr-defined]
            return True
        except Exception as exc:  # Fail closed: no silent drops on adapter/API mismatch.
            self._record_denial(OrderDenied("order_submit_failed", repr(exc)))
            return False

    def _plan_for_submission(self, plan: OrderPlan) -> OrderPlan:
        """Hedge-mode reconciliation quirk (live incident 2026-07-07): a node
        restart rebuilds in-flight fills into positions named ...-EXTERNAL.
        RiskEngine then blocks reduce-only orders attached to the constructed
        ...-LONG/SHORT book id ("would increase position"), while the Binance
        adapter refuses non-LONG/SHORT position-id suffixes — a deadlock that
        left an ETH short naked. Binance ignores reduceOnly in hedge mode anyway
        (the adapter suppresses the param), so dropping the INTERNAL flag when
        the book position exists only under a non-book id is venue-identical
        and unblocks the submission."""
        if not plan.reduce_only:
            return plan
        try:
            from nautilus_trader.model.enums import OmsType
        except Exception:  # pragma: no cover - non-Nautilus host
            return plan
        if getattr(self.config, "oms_type", None) != OmsType.HEDGING:
            return plan
        book = "SHORT" if plan.side == "BUY" else "LONG"
        if _book_position_has_external_id(
            plan.instrument_id,
            book,
            _nonzero_positions(self._cache_positions(plan.instrument_id)),
        ):
            import dataclasses
            return dataclasses.replace(plan, reduce_only=False)
        return plan

    def _hedge_position_id(self, order: Any, plan: OrderPlan) -> Any:
        """Binance Hedge Mode requires every order to carry a ``position_id`` whose value
        ends with ``LONG``/``SHORT`` — the exec client parses that suffix into the
        Binance ``positionSide``. One-way (NETTING) mode needs none, so return None there
        and the order submits unchanged (the tested testnet path).

        positionSide is the position BOOK the order acts on, not the order side:
        opening  -> BUY=LONG,  SELL=SHORT;
        reducing -> BUY closes SHORT, SELL closes LONG.
        """
        try:
            from nautilus_trader.model.enums import OmsType
        except Exception:  # pragma: no cover - non-Nautilus host
            return None
        if getattr(self.config, "oms_type", None) != OmsType.HEDGING:
            return None
        from nautilus_trader.model.identifiers import PositionId

        is_buy = plan.side == "BUY"
        if plan.reduce_only:
            book = "SHORT" if is_buy else "LONG"
        else:
            book = "LONG" if is_buy else "SHORT"
        return PositionId(f"{order.instrument_id}-{book}")

    def _submit_management_plan(self, plan: ManagementPlan) -> bool:
        # Make-before-break: place replacements first, then cancel the superseded
        # orders. If the new stop is rejected by the venue the old one is still
        # standing; the reverse order can leave the position naked. Reduce-only
        # orders briefly coexisting cannot over-close the position.
        for order_plan in plan.orders:
            if not self._submit_order_plan(order_plan):
                return False
        for client_order_id in plan.cancel_order_ids:
            if not self._cancel_order_by_client_order_id(plan.instrument_id, client_order_id):
                return False
        self._absorb_management_plan(plan)
        return True

    def _absorb_management_plan(self, plan: ManagementPlan) -> None:
        """Keep entry stashes coherent with operator-managed protections: without
        this, a later entry fill re-places SL/TP at the ORIGINAL signal prices and
        silently undoes an operator's move_stop_loss/replace_take_profits."""
        action = str(plan.action)
        if action not in ("move_stop_loss", "move_stop_to_entry", "replace_take_profits"):
            return
        for stash in self._entry_protection_stash.values():
            if str(stash.get("instrument_id")) != str(plan.instrument_id):
                continue
            if action in ("move_stop_loss", "move_stop_to_entry"):
                trigger = next(
                    (op.trigger_price for op in plan.orders if op.trigger_price is not None),
                    None,
                )
                if trigger is not None:
                    stash["stop_loss"] = trigger
            else:
                prices = tuple(op.price for op in plan.orders if op.price is not None)
                if prices:
                    stash["take_profits"] = prices
                    stash["take_profit_quantities"] = tuple(
                        op.quantity for op in plan.orders if op.price is not None
                    )
            # The operator's orders belong to a different intent id: adopt nothing,
            # but force the next entry-fill sync to rebuild from the updated prices.
            stash["protected_quantity"] = None
            self._persist_entry_protection_stash()

    def _cancel_order_by_client_order_id(
        self,
        instrument_id: str,
        client_order_id: str,
    ) -> bool:
        for order in self._cache_orders(instrument_id):
            if str(getattr(order, "client_order_id", "")) != client_order_id:
                continue
            try:
                # TODO(host-verify): confirm Strategy.cancel_order takes the order
                # object directly in Nautilus 1.227.0 for Binance futures.
                self.cancel_order(order)  # type: ignore[attr-defined]
                return True
            except Exception as exc:
                self._record_denial(OrderDenied("order_cancel_failed", repr(exc)))
                return False
        # Already gone (filled/cancelled between planning and execution): the goal
        # of the cancel — "this order must not stay live" — is met, so proceed.
        self._record_denial(OrderDenied("order_cancel_not_found", client_order_id))
        return True

    def _build_nautilus_order(self, plan: OrderPlan, instrument: Any) -> Any:
        # TODO(host-verify): confirm OrderFactory methods and whether market,
        # stop_market, stop_limit, and limit_if_touched orders accept
        # time_in_force/client_order_id/tags/reduce_only directly in Nautilus
        # 1.227.0.
        from nautilus_trader.model.enums import OrderSide, TimeInForce  # type: ignore
        from nautilus_trader.model.identifiers import ClientOrderId  # type: ignore

        side = OrderSide.BUY if plan.side == "BUY" else OrderSide.SELL
        tif = getattr(TimeInForce, plan.time_in_force)
        expire_time = getattr(plan, "expire_time", None)
        quantity = _make_quantity(instrument, plan.quantity)
        kwargs = {
            "instrument_id": getattr(instrument, "id", plan.instrument_id),
            "order_side": side,
            "quantity": quantity,
            "time_in_force": tif,
            # host-verify: Nautilus order factory needs a ClientOrderId, not a str.
            "client_order_id": ClientOrderId(str(plan.client_order_id)),
            "tags": list(plan.tags),
        }
        if plan.reduce_only:
            kwargs["reduce_only"] = True
        if expire_time is not None and plan.time_in_force == "GTD":
            kwargs["expire_time"] = expire_time
        if plan.order_type == "MARKET":
            return self.order_factory.market(**kwargs)  # type: ignore[attr-defined]
        if plan.order_type == "LIMIT":
            kwargs["price"] = _make_price(instrument, plan.price)
            return self.order_factory.limit(**kwargs)  # type: ignore[attr-defined]
        if plan.order_type == "STOP_MARKET":
            kwargs["trigger_price"] = _make_price(instrument, plan.trigger_price)
            return self.order_factory.stop_market(**kwargs)  # type: ignore[attr-defined]
        if plan.order_type == "STOP_LIMIT":
            kwargs["price"] = _make_price(instrument, plan.price)
            kwargs["trigger_price"] = _make_price(instrument, plan.trigger_price)
            return self.order_factory.stop_limit(**kwargs)  # type: ignore[attr-defined]
        if plan.order_type == "LIMIT_IF_TOUCHED":
            kwargs["price"] = _make_price(instrument, plan.price)
            kwargs["trigger_price"] = _make_price(instrument, plan.trigger_price)
            return self.order_factory.limit_if_touched(**kwargs)  # type: ignore[attr-defined]
        if plan.order_type == "MARKET_IF_TOUCHED":
            kwargs["trigger_price"] = _make_price(instrument, plan.trigger_price)
            return self.order_factory.market_if_touched(**kwargs)  # type: ignore[attr-defined]
        raise RuntimeError(f"unsupported order_type from planner: {plan.order_type}")

    def _record_denial(self, denial: OrderDenied) -> None:
        self.denials.append(denial)
        log = getattr(self, "log", None)
        if log is not None and hasattr(log, "error"):
            log.error(f"OrderDenied reason={denial.reason} detail={denial.detail}")

    def _report_denial(self, intent: Any, denial: OrderDenied) -> None:
        if self._denial_reporter is None:
            return
        try:
            self._denial_reporter(intent, denial)
        except Exception:
            log = getattr(self, "log", None)
            if log is not None and hasattr(log, "error"):
                log.error(
                    f"OrderDenied reporter failed reason={denial.reason} "
                    f"detail={denial.detail}"
                )


def _event_client_order_id(event: Any) -> Optional[str]:
    for holder in (event, getattr(event, "order", None)):
        if holder is None:
            continue
        value = getattr(holder, "client_order_id", None)
        if value is not None:
            return str(value)
    return None


def _event_instrument_id(event: Any) -> Optional[str]:
    for holder in (event, getattr(event, "order", None)):
        if holder is None:
            continue
        value = getattr(holder, "instrument_id", None)
        if value is not None:
            return str(value)
    return None


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _entry_position_denial(
    action: str,
    side: str,
    position: Optional[PositionSnapshot],
    instrument_id: str,
) -> Optional[OrderDenied]:
    if action == "open_position":
        return None

    if position is None or Decimal(str(position.quantity)) == Decimal("0"):
        return OrderDenied("position_required", instrument_id)
    position_side = position.side.upper()
    expected = "LONG" if side == "BUY" else "SHORT"
    if position_side != expected:
        return OrderDenied("position_side_mismatch", position_side)
    return None


def _entry_tags(intent: Any, action: str) -> tuple[str, ...]:
    return (
        f"intent_id={getattr(intent, 'intent_id')}",
        f"decision_id={getattr(intent, 'decision_id')}",
        f"risk_decision_id={getattr(intent, 'risk_decision_id')}",
        f"idempotency_key={getattr(intent, 'idempotency_key')}",
        f"action={action}",
        f"account_id={getattr(intent, 'account_id')}",
    )


def _book_position_has_external_id(
    instrument_id: str,
    book: str,
    positions: Iterable[Any],
) -> bool:
    """True when the position on `book` exists but under a non-book id (e.g.
    ...-EXTERNAL from post-restart reconciliation). Pure for tests."""
    book_id = f"{instrument_id}-{book}"
    for position in positions:
        if _position_side(position) != book:
            continue
        pid = _position_id(position) or ""
        return pid != book_id
    return False


def _round_down_positive(raw: Any, increment: str) -> Optional[str]:
    try:
        value = Decimal(str(raw))
        step = Decimal(str(increment))
    except (InvalidOperation, ValueError):
        return None
    if value <= 0 or step <= 0:
        return None
    snapped = (value / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
    if snapped <= 0:
        return None
    return format(snapped.quantize(step), "f")


def _decimal_abs_lte(left: Any, right: Any, tolerance: str) -> bool:
    try:
        return abs(Decimal(str(left)) - Decimal(str(right))) <= Decimal(str(tolerance))
    except (InvalidOperation, ValueError):
        return False


def _absorbed_or_split_quantities(
    absorbed: Any,
    total_quantity: str,
    target_count: int,
    increment: str,
) -> tuple[Optional[str], ...]:
    """Prefer operator-chosen per-tier quantities (absorbed from a management
    intent) as long as they still exactly cover the current position; any
    mismatch (position resized since) falls back to the even split."""
    if isinstance(absorbed, (list, tuple)) and len(absorbed) == target_count:
        try:
            from decimal import Decimal as _D
            if sum(_D(str(q)) for q in absorbed) == _D(str(total_quantity)):
                rounded = [
                    _round_down_positive(q, increment) for q in absorbed
                ]
                if all(r is not None for r in rounded):
                    return tuple(rounded)
        except (InvalidOperation, ValueError, TypeError):
            pass
    return _split_take_profit_quantities(total_quantity, target_count, increment)


def _take_profit_prices(raw_targets: Any) -> tuple[Any, ...]:
    if not isinstance(raw_targets, (list, tuple)):
        return ()
    prices: list[Any] = []
    for target in raw_targets:
        if isinstance(target, dict):
            price = target.get("price", target.get("trigger_price", target.get("limit_price")))
        else:
            price = target
        if price is not None:
            prices.append(price)
    return tuple(prices)


def _split_take_profit_quantities(
    quantity: str,
    target_count: int,
    increment: str,
) -> tuple[Optional[str], ...]:
    if target_count <= 0:
        return ()
    try:
        total = Decimal(quantity)
        step = Decimal(str(increment))
    except (InvalidOperation, ValueError):
        return tuple(None for _ in range(target_count))
    if total <= 0 or step <= 0:
        return tuple(None for _ in range(target_count))

    base = (total / Decimal(target_count) / step).quantize(
        Decimal("1"),
        rounding=ROUND_DOWN,
    ) * step
    quantities = [base for _ in range(target_count)]
    quantities[0] += total - (base * target_count)
    return tuple(
        format(value.quantize(step), "f") if value > 0 else None
        for value in quantities
    )


def _protection_tags(
    base_tags: tuple[str, ...],
    lifecycle_role: str,
    position_id: Optional[str],
    *,
    index: Optional[int] = None,
) -> tuple[str, ...]:
    tags = tuple(
        tag for tag in base_tags
        if not tag.startswith("lifecycle_role=")
        and not tag.startswith("position_id=")
        and not tag.startswith("take_profit_index=")
    )
    tags += (f"lifecycle_role={lifecycle_role}",)
    if position_id is not None:
        tags += (f"position_id={position_id}",)
    if index is not None:
        tags += (f"take_profit_index={index}",)
    return tags


def _approved_intent_data_type(account_id: str) -> Any:
    from intent.custom_data import _approved_trade_intent_data_class  # type: ignore
    from nautilus_trader.model.data import DataType  # type: ignore

    data_cls = _approved_trade_intent_data_class()
    return DataType(
        data_cls,
        metadata={"schema_version": "1.0", "account_id": account_id},
    )


def _intent_from_custom_data(data: Any) -> Any:
    payload_holder = getattr(data, "data", data)
    payload = getattr(payload_holder, "payload", None)
    if payload is None:
        return None
    from intent.custom_data import ApprovedTradeIntentCustomData  # type: ignore

    return ApprovedTradeIntentCustomData.from_wire(payload).intent


def _extract_increment(
    instrument: Any,
    increment_names: tuple[str, ...],
    precision_names: tuple[str, ...],
) -> Optional[str]:
    for name in increment_names:
        value = getattr(instrument, name, None)
        if value is not None:
            return _decimalish_to_str(value)
    for name in precision_names:
        value = getattr(instrument, name, None)
        if value is not None:
            precision = int(value)
            return "1" if precision == 0 else "0." + ("0" * (precision - 1)) + "1"
    return None


def _decimalish_to_str(value: Any) -> str:
    for name in ("as_decimal", "to_decimal"):
        method = getattr(value, name, None)
        if method is not None:
            return str(method())
    return str(value)


def _first_nonzero_position(positions: Iterable[Any]) -> Optional[Any]:
    for position in _nonzero_positions(positions):
        return position
    return None


def _nonzero_positions(positions: Iterable[Any]) -> tuple[Any, ...]:
    result: list[Any] = []
    for position in positions:
        try:
            if float(_position_quantity(position)) != 0.0:
                result.append(position)
        except (TypeError, ValueError):
            continue
    return tuple(result)


def _position_side(position: Any) -> str:
    for name in ("side", "position_side"):
        value = getattr(position, name, None)
        if value is not None:
            raw = str(getattr(value, "value", value)).upper()
            if "SHORT" in raw:
                return "SHORT"
            if "LONG" in raw:
                return "LONG"
    signed_qty = getattr(position, "signed_qty", None)
    if signed_qty is not None and str(signed_qty).startswith("-"):
        return "SHORT"
    return "LONG"


def _position_quantity(position: Any) -> str:
    for name in ("quantity", "qty", "signed_qty"):
        value = getattr(position, name, None)
        if value is not None:
            return str(value).lstrip("-")
    return "0"


def _position_id(position: Any) -> Optional[str]:
    for name in ("id", "position_id"):
        value = getattr(position, name, None)
        if value is not None:
            return str(value)
    return None


def _position_entry_price(position: Any) -> Optional[str]:
    for name in ("entry_price", "avg_px_open", "average_open_price"):
        value = getattr(position, name, None)
        if value is not None:
            return _decimalish_to_str(value)
    return None


def _intent_ids_from_tags(item: Any) -> set[str]:
    ids: set[str] = set()
    tags = getattr(item, "tags", ()) or ()
    for tag in tags:
        text = str(tag)
        if text.startswith("intent_id="):
            ids.add(text.split("=", 1)[1])
    return ids


def _enum_name(value: Any) -> str:
    raw = getattr(value, "name", getattr(value, "value", value))
    return str(raw).upper()


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return _decimalish_to_str(value)


def _make_quantity(instrument: Any, quantity: str) -> Any:
    make_qty = getattr(instrument, "make_qty", None)
    if make_qty is not None:
        return make_qty(quantity)
    from nautilus_trader.model.objects import Quantity  # type: ignore

    return Quantity.from_str(quantity)


def _make_price(instrument: Any, price: Optional[str]) -> Any:
    if price is None:
        raise ValueError("price is required")
    make_price = getattr(instrument, "make_price", None)
    if make_price is not None:
        return make_price(price)
    from nautilus_trader.model.objects import Price  # type: ignore

    return Price.from_str(price)
