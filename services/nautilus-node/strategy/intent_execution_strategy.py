from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import re
import tempfile
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from enum import Enum
from hashlib import sha256
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Lock
from typing import Any, Callable, Iterable, Mapping, Optional
from uuid import UUID, uuid4

from runtime.bounded_task_worker import BoundedTaskWorker
from runtime.live_canary_execution import (
    JsonLiveCanaryExecutionStore,
    LiveCanaryClaimResult,
    LiveCanaryExecutionIdentity,
    LiveCanaryFill,
    LiveCanaryLossDecision,
    LiveCanaryMark,
    is_live_canary_account,
    live_canary_permit_required,
    live_open_gate_denial,
    normalize_live_open_gate,
)
from runtime.intent_execution_inbox import (
    IntentDispatchResult,
    IntentExecutionIdentity,
    IntentExecutionState,
    IntentRegisterResult,
    JsonIntentExecutionInbox,
)
from strategy.intent_execution_planner import (
    CANCEL_ORDER,
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
    _authorization_source,
    _rounded_positive,
)


_TERMINAL_EXCHANGE_PENDING = object()


class _DurableIoTaskKind(str, Enum):
    INTENT_EXCHANGE_CONFIRMED = "intent_exchange_confirmed"
    INTENT_RECEIVE = "intent_receive"
    PREPARE_SUBMIT = "prepare_submit"
    MANAGEMENT_PREPARE = "management_prepare"
    MANAGEMENT_COMPLETE = "management_complete"
    RECOVERY_CONFIRMED = "recovery_confirmed"
    CANARY_MARK_DISPATCHED = "canary_mark_dispatched"
    PROTECTION_STASH_PERSIST = "protection_stash_persist"


@dataclass(frozen=True)
class _DurableIoTask:
    kind: _DurableIoTaskKind
    operation_id: str = ""
    client_order_id: str = ""
    intent: Any = False
    intent_execution: IntentExecutionIdentity | bool = False
    intent_payload: Mapping[str, Any] | bool = False
    client_order_ids: tuple[str, ...] = ()
    plans: tuple[OrderPlan, ...] = ()
    live_canary_execution: LiveCanaryExecutionIdentity | bool = False
    protection_payload: Mapping[str, Any] | bool = False
    protection_version: int = 0
    protection_payload_sha256: str = ""
    continuation: Mapping[str, Any] | bool = False


@dataclass(frozen=True)
class _DurableIoResult:
    task: _DurableIoTask
    outcome: Any = False


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
        environment: str = "testnet"
        release_id: str = ""
        live_canary_execution_path: str = ""
        intent_execution_inbox_path: str = ""
        live_entry_notional_inventory: tuple[tuple[str, str], ...] = ()
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
        environment: str = "testnet"
        release_id: str = ""
        live_canary_execution_path: str = ""
        intent_execution_inbox_path: str = ""
        live_entry_notional_inventory: tuple[tuple[str, str], ...] = ()
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

    _DURABLE_IO_QUEUE_CAPACITY = 128
    _DURABLE_IO_TASK_TIMEOUT_SECONDS = 1.0
    _DURABLE_IO_SHUTDOWN_TIMEOUT_SECONDS = 2.0

    def __init__(self, config: IntentExecutionStrategyConfig) -> None:
        try:
            super().__init__(config=config)
        except TypeError:
            super().__init__(config)
        self._processed_intent_ids: set[str] = set(config.existing_intent_ids)
        self.denials: list[OrderDenied] = []
        self._trading_state_getter: Optional[Callable[[], Any]] = None
        self._denial_reporter: Optional[Callable[[Any, OrderDenied], None]] = None
        self._protection_event_reporter: Optional[
            Callable[[dict[str, Any]], bool]
        ] = None
        self._live_canary_risk_reporter: Optional[
            Callable[[dict[str, Any]], bool]
        ] = None
        self._live_canary_halt_handler: Optional[
            Callable[[str], None]
        ] = None
        self._live_canary_portfolio_baseline: Optional[
            Callable[[str], str | bool]
        ] = None
        self._live_rollout_phase_getter: Optional[
            Callable[[], str | None]
        ] = None
        self._live_open_gate_getter: Optional[
            Callable[[], Mapping[str, Any] | bool]
        ] = None
        self._live_canary_monitor_targets: dict[str, str] = {}
        self._live_canary_monitor_baselines: dict[str, tuple[str, str]] = {}
        self._entry_protection_stash: dict[str, dict[str, Any]] = {}
        self._quick_fill_windows: dict[str, list[datetime]] = {}
        self._orphan_cancel_attempts: dict[str, int] = {}
        self._reported_protection_denials: set[tuple[str, str]] = set()
        self._exchange_cancel_adapter: Any = False
        self._exchange_state_mirror: Any = False
        self._terminal_exchange_worker: Any = False
        self._terminal_exchange_halt_handler: Optional[
            Callable[[str], None]
        ] = None
        self._terminal_exchange_mailbox: Queue[Any] = Queue(
            maxsize=128
        )
        self._pending_terminal_exchange: dict[
            str,
            dict[str, Any],
        ] = {}
        self._terminal_command_request_ids: dict[str, str] = {}
        self._terminal_command_results: dict[
            str,
            dict[str, Any],
        ] = {}
        inventory = tuple(
            getattr(config, "live_entry_notional_inventory", ()) or ()
        )
        self._live_entry_notional_caps = (
            _parse_live_entry_notional_inventory(inventory)
        )
        execution_path = str(
            getattr(config, "live_canary_execution_path", "") or ""
        ).strip()
        if not execution_path:
            execution_path = os.path.join(
                os.environ.get("NODE_STATE_DIR") or "/state",
                "live_canary_execution.json",
            )
        self._live_canary_execution_store = (
            JsonLiveCanaryExecutionStore(execution_path)
        )
        inbox_path = str(
            getattr(config, "intent_execution_inbox_path", "") or ""
        ).strip()
        if not inbox_path:
            if execution_path:
                inbox_path = str(
                    Path(execution_path).with_name(
                        "intent_execution_inbox.json"
                    )
                )
            else:
                inbox_path = os.path.join(
                    os.environ.get("NODE_STATE_DIR") or "/state",
                    "intent_execution_inbox.json",
                )
        self._intent_execution_inbox = JsonIntentExecutionInbox(
            inbox_path
        )
        self._strategy_stopping = False
        self._durable_io_halted_reason = ""
        self._durable_io_halt_lock = Lock()
        self._protection_stash_version = 0
        self._protection_stash_persisted_version = 0
        self._protection_durable_continuations: dict[
            int,
            list[Mapping[str, Any]],
        ] = {}
        self._durable_io_mailbox: Queue[_DurableIoResult] = Queue(
            maxsize=self._DURABLE_IO_QUEUE_CAPACITY
        )
        worker_name = str(
            getattr(config, "node_id", "")
            or getattr(config, "account_id", "")
            or "strategy"
        )
        self._durable_io_worker = BoundedTaskWorker(
            f"{worker_name}.intent-durable-io",
            self._process_durable_io_task,
            capacity=self._DURABLE_IO_QUEUE_CAPACITY,
            task_timeout_seconds=(
                self._DURABLE_IO_TASK_TIMEOUT_SECONDS
            ),
            on_overflow=self._halt_durable_io,
            on_error=self._halt_durable_io,
        )

    def set_trading_state_getter(self, getter: Optional[Callable[[], Any]]) -> None:
        """Inject the node's live trading-state source. Kept out of the serializable
        StrategyConfig; node wiring calls this after construction."""
        self._trading_state_getter = getter

    def set_durable_io_fatal_handler(
        self,
        handler: Optional[Callable[[str], None]],
    ) -> None:
        self._durable_io_worker.set_timeout_handler(handler)

    def set_denial_reporter(self, reporter: Optional[Callable[[Any, OrderDenied], None]]) -> None:
        """Inject best-effort denial reporting without making StrategyConfig carry
        non-serializable runtime clients."""
        self._denial_reporter = reporter

    def set_protection_event_reporter(
        self,
        reporter: Optional[Callable[[dict[str, Any]], bool]],
    ) -> None:
        self._protection_event_reporter = reporter

    def set_live_canary_risk_reporter(
        self,
        reporter: Optional[Callable[[dict[str, Any]], bool]],
    ) -> None:
        self._live_canary_risk_reporter = reporter

    def set_live_canary_halt_handler(
        self,
        handler: Optional[Callable[[str], None]],
    ) -> None:
        self._live_canary_halt_handler = handler

    def set_live_canary_portfolio_baseline_getter(
        self,
        getter: Optional[Callable[[str], str | bool]],
    ) -> None:
        self._live_canary_portfolio_baseline = getter

    def set_live_rollout_phase_getter(
        self,
        getter: Optional[Callable[[], str | None]],
    ) -> None:
        self._live_rollout_phase_getter = getter

    def set_live_open_gate_getter(
        self,
        getter: Optional[
            Callable[[], Mapping[str, Any] | bool]
        ],
    ) -> None:
        self._live_open_gate_getter = getter

    def set_exchange_cancel_adapter(self, adapter: Any, mirror: Any) -> None:
        self._exchange_cancel_adapter = adapter
        self._exchange_state_mirror = mirror

    def set_terminal_exchange_worker(
        self,
        worker: Any,
        halt_handler: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._terminal_exchange_worker = worker
        self._terminal_exchange_halt_handler = halt_handler

    def enqueue_terminal_exchange_result(self, result: Any) -> None:
        try:
            self._terminal_exchange_mailbox.put_nowait(result)
        except Full:
            self._halt_terminal_exchange(
                "terminal exchange result mailbox capacity exceeded"
            )

    def drain_terminal_exchange_mailbox(
        self,
        *,
        max_results: int = 16,
    ) -> int:
        if max_results < 1:
            raise ValueError("max_results must be positive")
        drained = 0
        while drained < max_results:
            try:
                result = self._terminal_exchange_mailbox.get_nowait()
            except Empty:
                break
            try:
                self._on_terminal_exchange_result(result)
            finally:
                self._terminal_exchange_mailbox.task_done()
            drained += 1
        return drained

    def _requires_live_canary_runtime(self) -> bool:
        return (
            is_live_canary_account(
                getattr(self.config, "account_id", "")
            )
            and str(getattr(self.config, "environment", "")).lower()
            == "live"
        )

    def _live_canary_loss_topic(self) -> str:
        return f"live-canary.loss.{self.config.account_id}"

    def publish_live_canary_loss_decision(
        self,
        decision: LiveCanaryLossDecision,
    ) -> None:
        message_bus = getattr(self, "msgbus", None)
        publish = getattr(message_bus, "publish", None)
        if not callable(publish):
            raise RuntimeError(
                "live canary loss decision message bus is unavailable"
            )
        publish(
            topic=self._live_canary_loss_topic(),
            msg=decision,
        )

    def on_start(self) -> None:
        self._strategy_stopping = False
        self._require_running_terminal_exchange_worker()
        self._durable_io_worker.start()
        self._register_durable_io_mailbox_timer()
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
        if self._requires_live_canary_runtime():
            if self._live_canary_risk_reporter is None:
                raise RuntimeError(
                    "live canary account requires canary risk reporter"
                )
            if self._live_canary_halt_handler is None:
                raise RuntimeError(
                    "live canary account requires canary halt handler"
                )
            self.msgbus.subscribe(  # type: ignore[attr-defined]
                topic=self._live_canary_loss_topic(),
                handler=self._on_live_canary_loss_decision,
            )
            for (
                client_order_id,
                instrument_id,
                symbol,
                portfolio_baseline,
            ) in (
                self._live_canary_execution_store.active_monitor_contexts()
            ):
                self._live_canary_monitor_targets[
                    client_order_id
                ] = instrument_id
                self._live_canary_monitor_baselines[
                    client_order_id
                ] = (symbol, portfolio_baseline)
            self._live_canary_risk_reporter({"kind": "recover"})
            self._register_live_canary_mark_timer()
            self._queue_live_canary_mark_checks()
        self._entry_protection_stash = self._load_entry_protection_stash()
        self._schedule_startup_protection_syncs()
        self._register_terminal_exchange_mailbox_timer()
        if self._terminal_exchange_worker:
            self._queue_exchange_refresh(
                purpose="startup_reconcile",
                continuation={"kind": "reconcile"},
            )
        elif self._refresh_exchange_state():
            self._retry_pending_take_profit_disables()
        self._register_exchange_state_timer()

    def _require_running_terminal_exchange_worker(self) -> None:
        if (
            not self._exchange_cancel_adapter
            and not self._exchange_state_mirror
        ):
            return
        worker = self._terminal_exchange_worker
        if not worker:
            raise RuntimeError(
                "exchange dependencies require terminal exchange worker"
            )
        snapshot = getattr(worker, "snapshot", None)
        if not callable(snapshot):
            raise RuntimeError(
                "terminal exchange worker health is unavailable"
            )
        if not snapshot().running:
            raise RuntimeError(
                "terminal exchange worker must be running before strategy start"
            )

    def _register_terminal_exchange_mailbox_timer(self) -> None:
        if not self._terminal_exchange_worker:
            return
        clock = getattr(self, "clock", None)
        set_timer = (
            getattr(clock, "set_timer", None)
            if clock is not None
            else None
        )
        if not callable(set_timer):
            return
        interval = timedelta(milliseconds=10)
        try:
            set_timer(
                name="terminal-exchange.mailbox",
                interval=interval,
                callback=self._on_terminal_exchange_mailbox_timer,
            )
            return
        except TypeError:
            pass
        set_timer(
            "terminal-exchange.mailbox",
            interval,
            self._on_terminal_exchange_mailbox_timer,
        )

    def _register_durable_io_mailbox_timer(self) -> None:
        clock = getattr(self, "clock", None)
        set_timer = getattr(clock, "set_timer", None)
        if not callable(set_timer):
            return
        interval = timedelta(milliseconds=10)
        try:
            set_timer(
                name="strategy.durable-io.mailbox",
                interval=interval,
                callback=self._on_durable_io_mailbox_timer,
            )
            return
        except TypeError:
            pass
        set_timer(
            "strategy.durable-io.mailbox",
            interval,
            self._on_durable_io_mailbox_timer,
        )

    def _on_durable_io_mailbox_timer(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        if self._strategy_stopping:
            return
        self.drain_durable_io_mailbox()

    def drain_durable_io_mailbox(
        self,
        *,
        max_results: int = 16,
    ) -> int:
        if max_results < 1:
            raise ValueError("max_results must be positive")
        drained = 0
        while drained < max_results:
            try:
                result = self._durable_io_mailbox.get_nowait()
            except Empty:
                break
            try:
                if self._strategy_stopping:
                    self._discard_durable_io_result(result)
                else:
                    self._on_durable_io_result(result)
            finally:
                self._durable_io_mailbox.task_done()
            drained += 1
        return drained

    def _on_terminal_exchange_mailbox_timer(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        self.drain_terminal_exchange_mailbox()

    def _register_exchange_state_timer(self) -> None:
        clock = getattr(self, "clock", None)
        set_timer = getattr(clock, "set_timer", None) if clock is not None else None
        if not callable(set_timer):
            return
        interval = timedelta(seconds=45)
        try:
            set_timer(
                name="exchange-state.reconcile",
                interval=interval,
                callback=self._on_exchange_state_timer,
            )
            return
        except TypeError:
            pass
        set_timer("exchange-state.reconcile", interval, self._on_exchange_state_timer)

    def _on_exchange_state_timer(self, *_args: Any, **_kwargs: Any) -> None:
        if self._terminal_exchange_worker:
            self._queue_exchange_refresh(
                purpose="periodic_reconcile",
                continuation={"kind": "reconcile"},
            )
            return
        if self._refresh_exchange_state():
            self._retry_pending_take_profit_disables()

    def _refresh_exchange_state(self) -> bool:
        mirror = self._exchange_state_mirror
        if not mirror:
            return False
        refresh = getattr(mirror, "refresh", None)
        if not callable(refresh):
            return False
        try:
            refresh()
            return True
        except Exception as exc:
            self._record_denial(OrderDenied("exchange_state_refresh_failed", repr(exc)))
            return False

    _PROTECTION_STASH_FILENAME = "protection_stash.json"
    _PROTECTION_TERMINAL_EVENT_LIMIT = 32
    _PROTECTION_STASH_TUPLE_FIELDS = frozenset(
        {
            "take_profits",
            "entry_tags",
            "protection_ids",
            "take_profit_quantities",
            "pending_cancel_ids",
        }
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
            self._normalize_protection_stash(str(intent_key), clean)
            loaded[str(intent_key)] = clean
        return loaded

    def _normalize_protection_stash(self, intent_key: str, stash: dict[str, Any]) -> None:
        roles = stash.get("protection_roles")
        stash["protection_roles"] = roles if isinstance(roles, dict) else {}
        consumed = stash.get("tp_consumed")
        stash["tp_consumed"] = consumed if isinstance(consumed, dict) else {}
        fallbacks = stash.get("tp_market_fallbacks")
        stash["tp_market_fallbacks"] = (
            fallbacks if isinstance(fallbacks, dict) else {}
        )
        terminal_events = stash.get("protection_terminal_events")
        if isinstance(terminal_events, list):
            stash["protection_terminal_events"] = terminal_events[
                -self._PROTECTION_TERMINAL_EVENT_LIMIT:
            ]
        else:
            stash["protection_terminal_events"] = []
        last_terminal = stash.get("last_protection_terminal_event")
        if not isinstance(last_terminal, dict):
            stash.pop("last_protection_terminal_event", None)
        entry_tags = tuple(str(tag) for tag in (stash.get("entry_tags") or ()))
        entry_intent_id = _tag_value(entry_tags, "intent_id")
        authorization = _authorization_from_tags(entry_tags)
        if (
            entry_intent_id == intent_key
            and _valid_uuid_text(entry_intent_id)
            and authorization
        ):
            protection_parent = authorization["parent_intent_id"]
            if stash.get("stop_loss") is not None:
                stash.setdefault(
                    "stop_loss_parent_intent_id",
                    protection_parent,
                )
            if _take_profit_prices(stash.get("take_profits")):
                stash.setdefault(
                    "take_profit_parent_intent_id",
                    protection_parent,
                )
            stash.setdefault("stop_loss_authorization", authorization)
            stash.setdefault("take_profit_authorization", authorization)
        pending = stash.get("pending_cancel_ids")
        if isinstance(pending, list):
            stash["pending_cancel_ids"] = tuple(str(item) for item in pending)
        elif isinstance(pending, tuple):
            stash["pending_cancel_ids"] = tuple(str(item) for item in pending)
        else:
            stash["pending_cancel_ids"] = ()
        try:
            revision = int(stash.get("protection_revision", -1))
        except (TypeError, ValueError):
            revision = -1
        if revision >= self._PROTECTION_MAX_REVISION and not stash.get("protection_frozen"):
            self._freeze_protection(
                intent_key,
                stash,
                "revisions_exhausted",
                denial_reason="protection_revisions_exhausted",
            )
        if stash.get("protection_frozen"):
            self._report_protection_freeze_event(intent_key, stash)
        self._retry_pending_tp_market_fallback_events(intent_key, stash)

    def _persist_entry_protection_stash(self) -> bool:
        try:
            self._write_entry_protection_stash(
                self._entry_protection_stash_payload()
            )
            return True
        except Exception as exc:
            log = getattr(self, "log", None)
            if log is not None and hasattr(log, "error"):
                log.error(f"protection stash persist failed: {exc!r}")
            self._record_denial(
                OrderDenied("protection_stash_persist_failed", repr(exc))
            )
            return False

    def _entry_protection_stash_payload(self) -> dict[str, Any]:
        return {
            str(intent_key): copy.deepcopy(
                self._jsonable_protection_stash_value(value)
            )
            for intent_key, value in self._entry_protection_stash.items()
            if isinstance(value, dict)
        }

    def _write_entry_protection_stash(
        self,
        payload: Mapping[str, Any],
    ) -> None:
        path = Path(self._protection_stash_path())
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._PROTECTION_STASH_FILENAME}.tmp.",
            dir=str(directory),
            text=True,
        )
        try:
            with os.fdopen(fd, "w") as tmp:
                json.dump(
                    dict(payload),
                    tmp,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, path)
            _fsync_strategy_directory(directory)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _queue_entry_protection_stash_persist(
        self,
        *,
        continuation: Mapping[str, Any] | bool = False,
    ) -> bool:
        payload = self._entry_protection_stash_payload()
        self._protection_stash_version += 1
        version = self._protection_stash_version
        pending_continuations: list[Mapping[str, Any]] = []
        for queued_version in tuple(
            self._protection_durable_continuations
        ):
            pending_continuations.extend(
                self._protection_durable_continuations.pop(
                    queued_version
                )
            )
        if isinstance(continuation, Mapping):
            pending_continuations.append(dict(continuation))
        if pending_continuations:
            self._protection_durable_continuations[
                version
            ] = pending_continuations
        payload_sha256 = _protection_payload_sha256(payload)
        return self._submit_durable_io_task(
            _DurableIoTask(
                kind=_DurableIoTaskKind.PROTECTION_STASH_PERSIST,
                operation_id=uuid4().hex,
                protection_payload=payload,
                protection_version=version,
                protection_payload_sha256=payload_sha256,
                continuation={"kind": "protection_persisted"},
            )
        )

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
        if intent is None:
            return
        self._queue_intent_receive(intent)

    def _on_node_command(self, cmd: Any) -> None:
        if not _node_command_has_authorization(cmd):
            self._record_denial(
                OrderDenied(
                    "authorization_source_required",
                    "node command requires user or channel authorization",
                )
            )
            return
        ctype = getattr(cmd, "type", cmd)
        ctype = str(getattr(ctype, "value", ctype)).lower()
        if ctype not in {"cancel_all", "close_all"}:
            return
        command_id = str(getattr(cmd, "command_id", "") or "").strip()
        args = getattr(cmd, "args", {})
        if not isinstance(args, dict):
            return
        command_account_id = str(
            args.get("account_id") or self.config.account_id
        ).strip()
        if command_account_id != str(self.config.account_id):
            self._record_denial(
                OrderDenied(
                    "command_account_mismatch",
                    command_account_id,
                )
            )
            return
        try:
            instrument_ids = _terminal_command_instrument_ids(args)
        except ValueError as exc:
            self._record_denial(
                OrderDenied("terminal_command_scope_invalid", str(exc))
            )
            return

        dispatched_at = datetime.now(timezone.utc)
        if command_id:
            completed = self._terminal_command_results.get(command_id)
            if completed is not None:
                self._publish_terminal_command_result(dict(completed))
                return
            if command_id in self._terminal_command_request_ids:
                return
        if self._terminal_exchange_worker:
            self._queue_terminal_command(
                command_id=command_id,
                command_type=ctype,
                instrument_ids=instrument_ids,
                dispatched_at=dispatched_at,
            )
            return
        operations: list[dict[str, Any]] = []
        errors: list[str] = []
        self._cancel_terminal_orders(
            instrument_ids,
            operations,
            errors,
        )
        if ctype == "close_all":
            self._close_terminal_positions(
                instrument_ids,
                operations,
                errors,
            )
        payload = {
            "command_id": command_id,
            "command_type": ctype,
            "account_id": str(self.config.account_id),
            "instrument_ids": list(instrument_ids),
            "operations": operations,
            "errors": errors,
            "dispatched_at": dispatched_at.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        if command_id:
            self._remember_terminal_command_result(
                command_id,
                payload,
            )
            self._publish_terminal_command_result(payload)

    def _queue_terminal_command(
        self,
        *,
        command_id: str,
        command_type: str,
        instrument_ids: tuple[str, ...],
        dispatched_at: datetime,
    ) -> bool:
        if not command_id:
            self._record_denial(
                OrderDenied(
                    "terminal_command_id_required",
                    command_type,
                )
            )
            return False
        from runtime.exchange_cancel_adapter import (
            TerminalExchangeRequest,
        )

        request_id = f"terminal-command:{command_id}"
        request = TerminalExchangeRequest(
            request_id=request_id,
            account_id=str(self.config.account_id),
            operation="terminal_command",
            purpose=command_type,
            deadline_monotonic=(
                self._terminal_exchange_worker.new_deadline()
            ),
            instrument_ids=instrument_ids,
        )
        self._pending_terminal_exchange[request_id] = {
            "kind": "terminal_command",
            "command_id": command_id,
            "command_type": command_type,
            "instrument_ids": instrument_ids,
            "dispatched_at": dispatched_at,
        }
        self._terminal_command_request_ids[command_id] = request_id
        if self._terminal_exchange_worker.submit(request):
            return True
        self._pending_terminal_exchange.pop(request_id, None)
        self._terminal_command_request_ids.pop(command_id, None)
        self._record_denial(
            OrderDenied(
                "terminal_exchange_queue_rejected",
                command_id,
            )
        )
        return False

    def _queue_exchange_refresh(
        self,
        *,
        purpose: str,
        continuation: dict[str, Any],
    ) -> bool:
        worker = self._terminal_exchange_worker
        if not worker:
            return False
        kind = str(continuation.get("kind") or "")
        intent = continuation.get("intent")
        request_id = ""
        if intent is not None:
            intent_id = str(getattr(intent, "intent_id", "") or "")
            for pending in self._pending_terminal_exchange.values():
                pending_intent = pending.get("intent")
                pending_intent_id = str(
                    getattr(pending_intent, "intent_id", "") or ""
                )
                if (
                    str(pending.get("kind") or "")
                    == "intent_refresh"
                    and pending_intent_id == intent_id
                ):
                    return True
            request_id = (
                f"intent-refresh:{intent_id}:{uuid4().hex}"
            )
        else:
            for pending_id, pending in (
                self._pending_terminal_exchange.items()
            ):
                if (
                    str(pending.get("kind") or "") == kind
                    and str(pending.get("purpose") or "") == purpose
                ):
                    return True
            request_id = f"exchange-refresh:{purpose}:{uuid4().hex}"
        from runtime.exchange_cancel_adapter import (
            TerminalExchangeRequest,
        )

        request = TerminalExchangeRequest(
            request_id=request_id,
            account_id=str(self.config.account_id),
            operation="refresh",
            purpose=purpose,
            deadline_monotonic=worker.new_deadline(),
        )
        pending = dict(continuation)
        pending["purpose"] = purpose
        self._pending_terminal_exchange[request_id] = pending
        if worker.submit(request):
            return True
        self._pending_terminal_exchange.pop(request_id, None)
        denial = OrderDenied(
            "terminal_exchange_queue_rejected",
            purpose,
        )
        self._record_denial(denial)
        if intent is not None:
            self._report_denial(intent, denial)
        return False

    def _on_terminal_exchange_result(self, result: Any) -> None:
        request_id = str(
            getattr(result, "request_id", "") or ""
        )
        if not request_id:
            self._halt_terminal_exchange(
                "terminal exchange result missing request_id"
            )
            return
        account_id = str(
            getattr(result, "account_id", "") or ""
        )
        if account_id != str(self.config.account_id):
            self._halt_terminal_exchange(
                "terminal exchange result account mismatch"
            )
            return
        pending = self._pending_terminal_exchange.pop(
            request_id,
            None,
        )
        if pending is None:
            return
        kind = str(pending.get("kind") or "")
        if kind == "terminal_command":
            self._complete_terminal_command(result, pending)
            return
        if kind == "intent_refresh":
            self._complete_intent_refresh(result, pending)
            return
        if kind == "management_cancel":
            self._complete_management_cancels(result, pending)
            return
        if kind == "take_profit_retry":
            self._complete_take_profit_retry(result, pending)
            return
        if kind == "reconcile":
            if self._terminal_exchange_result_failed(result):
                self._record_terminal_exchange_failure(
                    result,
                    purpose=str(pending.get("purpose") or "reconcile"),
                )
                return
            self._retry_pending_take_profit_disables()

    def _complete_terminal_command(
        self,
        result: Any,
        pending: dict[str, Any],
    ) -> None:
        command_id = str(pending["command_id"])
        command_type = str(pending["command_type"])
        instrument_ids = tuple(pending["instrument_ids"])
        operations = [
            self._terminal_cancel_outcome_payload(outcome)
            for outcome in tuple(
                getattr(result, "cancel_outcomes", ()) or ()
            )
        ]
        errors = [
            str(operation["error"])
            for operation in operations
            if operation["status"] == "failed"
        ]
        result_error = str(getattr(result, "error", "") or "")
        if result_error:
            errors.append(result_error)
            self._record_denial(
                OrderDenied(
                    "terminal_exchange_operation_failed",
                    result_error,
                )
            )
        if command_type == "close_all":
            self._close_terminal_positions(
                instrument_ids,
                operations,
                errors,
            )
        dispatched_at = pending["dispatched_at"]
        payload = {
            "command_id": command_id,
            "command_type": command_type,
            "account_id": str(self.config.account_id),
            "instrument_ids": list(instrument_ids),
            "operations": operations,
            "errors": errors,
            "dispatched_at": dispatched_at.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        self._terminal_command_request_ids.pop(command_id, None)
        self._remember_terminal_command_result(
            command_id,
            payload,
        )
        self._publish_terminal_command_result(payload)

    def _remember_terminal_command_result(
        self,
        command_id: str,
        payload: dict[str, Any],
    ) -> None:
        self._terminal_command_results[command_id] = copy.deepcopy(
            payload
        )
        while len(self._terminal_command_results) > 256:
            oldest_command_id = next(
                iter(self._terminal_command_results)
            )
            self._terminal_command_results.pop(
                oldest_command_id,
                None,
            )

    @staticmethod
    def _terminal_cancel_outcome_payload(
        outcome: Any,
    ) -> dict[str, Any]:
        request = getattr(outcome, "request", None)
        symbol = str(getattr(request, "symbol", "") or "")
        payload = {
            "kind": "cancel_order",
            "instrument_id": f"{symbol}-PERP.BINANCE",
            "symbol": symbol,
            "position_side": str(
                getattr(request, "position_side", "") or ""
            ),
            "order_kind": str(
                getattr(request, "order_kind", "") or ""
            ),
            "venue_order_id": str(
                getattr(request, "venue_order_id", "") or ""
            ),
            "client_order_id": str(
                getattr(request, "client_order_id", "") or ""
            ),
            "status": str(
                getattr(outcome, "status", "") or "failed"
            ),
        }
        result_outcome = str(
            getattr(outcome, "outcome", "") or ""
        )
        terminal_status = str(
            getattr(outcome, "terminal_status", "") or ""
        )
        error = str(getattr(outcome, "error", "") or "")
        if result_outcome:
            payload["outcome"] = result_outcome
        if terminal_status:
            payload["terminal_status"] = terminal_status
        if error:
            payload["error"] = error
        return payload

    def _complete_intent_refresh(
        self,
        result: Any,
        pending: dict[str, Any],
    ) -> None:
        intent = pending["intent"]
        if self._terminal_exchange_result_failed(result):
            denial = self._record_terminal_exchange_failure(
                result,
                purpose=str(pending.get("purpose") or "intent_refresh"),
            )
            self._report_denial(intent, denial)
            return
        self._handle_intent_ready(
            intent,
            exchange_state_ready=True,
            durable_async=bool(
                pending.get("durable_async", False)
            ),
        )

    @staticmethod
    def _terminal_exchange_result_failed(result: Any) -> bool:
        return bool(
            str(getattr(result, "error", "") or "")
        )

    def _record_terminal_exchange_failure(
        self,
        result: Any,
        *,
        purpose: str,
    ) -> OrderDenied:
        detail = str(getattr(result, "error", "") or "")
        denial = OrderDenied(
            "exchange_state_refresh_failed",
            f"{purpose}:{detail}",
        )
        self._record_denial(denial)
        return denial

    def _halt_terminal_exchange(self, reason: str) -> None:
        handler = self._terminal_exchange_halt_handler
        if handler is not None:
            handler(str(reason))
        self._record_denial(
            OrderDenied(
                "terminal_exchange_halted",
                str(reason),
            )
        )

    def _cancel_terminal_orders(
        self,
        instrument_ids: tuple[str, ...],
        operations: list[dict[str, Any]],
        errors: list[str],
    ) -> None:
        if self._exchange_cancel_adapter and self._exchange_state_mirror:
            try:
                orders = tuple(self._exchange_state_mirror.refresh())
            except Exception as exc:
                detail = f"exchange order snapshot failed: {exc!r}"
                errors.append(detail)
                self._record_denial(
                    OrderDenied("exchange_state_refresh_failed", repr(exc))
                )
                return
            for order in orders:
                instrument_id = str(
                    getattr(order, "instrument_id", "") or ""
                )
                if not _terminal_instrument_matches(
                    instrument_id,
                    instrument_ids,
                ):
                    continue
                operation = self._cancel_terminal_exchange_order(order)
                operations.append(operation)
                if operation["status"] == "failed":
                    errors.append(str(operation["error"]))
            return

        environment = str(
            getattr(self.config, "environment", "")
        ).lower()
        if environment == "live":
            errors.append("exchange cancel adapter unavailable")
            return
        for order in self._all_open_orders():
            instrument_id = str(
                getattr(order, "instrument_id", "") or ""
            )
            if not _terminal_instrument_matches(
                instrument_id,
                instrument_ids,
            ):
                continue
            client_order_id = str(
                getattr(order, "client_order_id", "") or ""
            )
            operation = {
                "kind": "cancel_order",
                "instrument_id": instrument_id,
                "client_order_id": client_order_id,
                "status": "requested",
            }
            try:
                self.cancel_order(order)  # type: ignore[attr-defined]
            except Exception as exc:
                operation["status"] = "failed"
                operation["error"] = repr(exc)
                errors.append(repr(exc))
                self._record_denial(
                    OrderDenied("order_cancel_failed", repr(exc))
                )
            operations.append(operation)

    def _cancel_terminal_exchange_order(
        self,
        order: Any,
    ) -> dict[str, Any]:
        from runtime.exchange_cancel_adapter import CancelOrderRequest

        operation = {
            "kind": "cancel_order",
            "instrument_id": str(
                getattr(order, "instrument_id", "") or ""
            ),
            "symbol": str(getattr(order, "symbol", "") or ""),
            "position_side": str(
                getattr(order, "position_side", "") or ""
            ),
            "order_kind": str(getattr(order, "order_kind", "") or ""),
            "venue_order_id": str(
                getattr(order, "venue_order_id", "") or ""
            ),
            "client_order_id": str(
                getattr(order, "client_order_id", "") or ""
            ),
            "status": "requested",
        }
        request = CancelOrderRequest(
            account_id=str(getattr(order, "account_id", "") or ""),
            symbol=operation["symbol"],
            position_side=operation["position_side"],
            order_kind=operation["order_kind"],
            venue_order_id=operation["venue_order_id"] or None,
            client_order_id=operation["client_order_id"] or None,
        )
        try:
            result = self._exchange_cancel_adapter.cancel(
                "cancel_order",
                request,
            )
            operation["status"] = "confirmed"
            operation["outcome"] = str(
                getattr(result, "outcome", "") or ""
            )
            operation["terminal_status"] = str(
                getattr(result, "terminal_status", "") or ""
            )
        except Exception as exc:
            operation["status"] = "failed"
            operation["error"] = repr(exc)
            self._record_denial(
                OrderDenied("order_cancel_failed", repr(exc))
            )
        return operation

    def _close_terminal_positions(
        self,
        instrument_ids: tuple[str, ...],
        operations: list[dict[str, Any]],
        errors: list[str],
    ) -> None:
        for position in self._all_open_positions():
            instrument_id = str(
                getattr(position, "instrument_id", "") or ""
            )
            if not _terminal_instrument_matches(
                instrument_id,
                instrument_ids,
            ):
                continue
            operation = {
                "kind": "close_position",
                "position_id": _position_id(position) or "",
                "instrument_id": instrument_id,
                "position_side": _position_side(position),
                "quantity": _position_quantity(position),
                "reduce_only": True,
                "status": "requested",
            }
            try:
                self.close_position(position)  # type: ignore[attr-defined]
            except Exception as exc:
                operation["status"] = "failed"
                operation["error"] = repr(exc)
                errors.append(repr(exc))
                self._record_denial(
                    OrderDenied("position_close_failed", repr(exc))
                )
            operations.append(operation)

    def _publish_terminal_command_result(
        self,
        payload: dict[str, Any],
    ) -> None:
        message_bus = getattr(self, "msgbus", None)
        publish = getattr(message_bus, "publish", None)
        if not callable(publish):
            self._record_denial(
                OrderDenied(
                    "terminal_command_result_bus_unavailable",
                    str(payload.get("command_id") or ""),
                )
            )
            return
        publish(
            topic=f"node.command-results.{self.config.account_id}",
            msg=payload,
        )

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
        self._queue_intent_receive(intent)

    def _queue_intent_receive(self, intent: Any) -> bool:
        try:
            execution_identity = _intent_execution_identity(intent)
            intent_payload = _intent_execution_payload(intent)
        except Exception as exc:
            denial = OrderDenied(
                "durable_intent_identity_invalid",
                repr(exc),
            )
            self._record_denial(denial)
            self._report_denial(intent, denial)
            return False
        submitted = self._submit_durable_io_task(
            _DurableIoTask(
                kind=_DurableIoTaskKind.INTENT_RECEIVE,
                intent=intent,
                intent_execution=execution_identity,
                intent_payload=intent_payload,
                continuation={"kind": "intent_received"},
            )
        )
        if submitted:
            return True
        denial = self.denials[-1] if self.denials else OrderDenied(
            "durable_intent_queue_rejected",
            execution_identity.intent_id,
        )
        self._report_denial(intent, denial)
        return False

    def _handle_intent(self, intent: Any) -> None:
        """Synchronous internal seam retained for recovery tools and unit tests."""
        execution_identity = _intent_execution_identity(intent)
        task = _DurableIoTask(
            kind=_DurableIoTaskKind.INTENT_RECEIVE,
            intent=intent,
            intent_execution=execution_identity,
            intent_payload=_intent_execution_payload(intent),
        )
        try:
            outcome = self._process_intent_receive_task(task)
        except RuntimeError as exc:
            denial = OrderDenied(
                "durable_intent_receipt_failed",
                repr(exc),
            )
            self._record_denial(denial)
            self._report_denial(intent, denial)
            return
        self._continue_intent_after_receive(
            intent,
            execution_identity,
            outcome,
            durable_async=False,
        )

    def _continue_intent_after_receive(
        self,
        intent: Any,
        execution_identity: IntentExecutionIdentity,
        outcome: Mapping[str, Any],
        *,
        durable_async: bool,
    ) -> None:
        register_result = outcome.get("register_result", False)
        if register_result in {
            IntentRegisterResult.INTENT_CONFLICT,
            IntentRegisterResult.IDEMPOTENCY_CONFLICT,
        }:
            denial = OrderDenied(
                "durable_intent_identity_conflict",
                str(getattr(intent, "intent_id", "")),
            )
            self._record_denial(denial)
            self._report_denial(intent, denial)
            return
        durable_record = outcome.get("record", False)
        if durable_record is False:
            denial = OrderDenied(
                "durable_intent_receipt_missing",
                str(getattr(intent, "intent_id", "")),
            )
            self._record_denial(denial)
            self._report_denial(intent, denial)
            return
        if (
            durable_record.state
            is IntentExecutionState.EXCHANGE_CONFIRMED
        ):
            self._processed_intent_ids.add(
                execution_identity.intent_id
            )
            return
        if durable_record.state is IntentExecutionState.REJECTED:
            denial = OrderDenied(
                "durable_intent_rejected",
                durable_record.rejection_reason,
            )
            self._record_denial(denial)
            self._report_denial(intent, denial)
            return
        if durable_record.state is IntentExecutionState.DISPATCHED:
            if self._durable_intent_orders_exist(durable_record):
                if durable_async:
                    self._submit_durable_io_task(
                        _DurableIoTask(
                            kind=(
                                _DurableIoTaskKind.RECOVERY_CONFIRMED
                            ),
                            intent=intent,
                            intent_execution=execution_identity,
                            continuation={
                                "kind": "intent_recovery_confirmed",
                            },
                        )
                    )
                else:
                    self._intent_execution_inbox.mark_exchange_confirmed(
                        execution_identity
                    )
                    self._processed_intent_ids.add(
                        execution_identity.intent_id
                    )
                return
            denial = OrderDenied(
                "intent_exchange_confirmation_required",
                execution_identity.intent_id,
            )
            self._record_denial(denial)
            self._report_denial(intent, denial)
            return

        raw_action = getattr(intent, "action", "")
        action = str(getattr(raw_action, "value", raw_action))
        raw_order_plan = getattr(intent, "order_plan", {}) or {}
        canary_denial = self._live_canary_intent_denial(
            intent,
            action=action,
            order_plan=raw_order_plan,
        )
        if canary_denial is not None:
            self._record_denial(canary_denial)
            self._report_denial(intent, canary_denial)
            return
        disabling_take_profits = (
            action == "replace_take_profits"
            and raw_order_plan.get("disable_take_profits") is True
            and not raw_order_plan.get("take_profits")
        )
        needs_exchange_state = action in {
            "cancel",
            "cancel_order",
            "move_stop_loss",
            "move_stop_to_entry",
            "replace_take_profits",
        }
        if needs_exchange_state and self._terminal_exchange_worker:
            self._queue_exchange_refresh(
                purpose=f"intent:{intent.intent_id}",
                continuation={
                    "kind": "intent_refresh",
                    "intent": intent,
                    "durable_async": durable_async,
                },
            )
            return
        exchange_state_ready = False
        if needs_exchange_state and not disabling_take_profits:
            exchange_state_ready = self._refresh_exchange_state()
        if (
            needs_exchange_state
            and not disabling_take_profits
            and not exchange_state_ready
        ):
            denial = self.denials[-1] if self.denials else OrderDenied(
                "exchange_state_refresh_failed",
                str(intent.intent_id),
            )
            self._report_denial(intent, denial)
            return
        self._handle_intent_ready(
            intent,
            exchange_state_ready=exchange_state_ready,
            durable_async=durable_async,
        )

    def _handle_intent_ready(
        self,
        intent: Any,
        *,
        exchange_state_ready: bool,
        durable_async: bool = False,
    ) -> None:
        raw_action = getattr(intent, "action", "")
        action = str(getattr(raw_action, "value", raw_action))
        raw_order_plan = getattr(intent, "order_plan", {}) or {}
        context = PlannerContext(
            account_id=self.config.account_id,
            trading_state=self._trading_state(),
            now=self._now(),
            instrument=self._instrument_spec(str(intent.instrument_id)),
            position=self._position_snapshot(str(intent.instrument_id)),
            positions=self._position_snapshots(str(intent.instrument_id)),
            existing_orders=self._order_snapshots(
                str(intent.instrument_id),
                include_exchange_mirror=exchange_state_ready,
            ),
            existing_intent_ids=frozenset(
                self._processed_intent_ids | self._active_intent_ids(intent.instrument_id)
            ),
        )
        if str(raw_order_plan.get("type", "")).lower() == "zone_ladder":
            self._handle_zone_ladder(
                intent,
                raw_order_plan,
                context,
                action,
                intent_execution=_intent_execution_identity(intent),
                durable_async=durable_async,
            )
            return

        result = plan_intent_execution(intent, context)
        if isinstance(result, OrderDenied):
            self._record_denial(result)
            self._report_denial(intent, result)
            return

        if isinstance(result, ManagementPlan):
            if durable_async:
                self._queue_management_plan_after_persist(
                    result,
                    source_intent=intent,
                )
                return
            submitted = self._submit_management_plan(
                result,
                source_intent=intent,
            )
            if submitted is _TERMINAL_EXCHANGE_PENDING:
                return
        else:
            live_canary_execution = self._live_canary_execution_identity(
                intent,
                result,
            )
            if isinstance(live_canary_execution, OrderDenied):
                self._record_denial(live_canary_execution)
                self._report_denial(intent, live_canary_execution)
                return
            if durable_async:
                self._queue_single_intent_submit(
                    intent,
                    result,
                    live_canary_execution=live_canary_execution,
                    intent_execution=_intent_execution_identity(intent),
                    action=action,
                )
                return
            protection_preimage: Optional[dict[str, dict[str, Any]]] = None
            protection_ready = True
            if action in ("open_position", "add_position"):
                protection_preimage = copy.deepcopy(
                    self._entry_protection_stash
                )
                protection_ready = self._stash_entry_protection(intent, result)
            if protection_ready:
                submitted = self._submit_order_plan(
                    result,
                    live_canary_execution=live_canary_execution,
                    intent_execution=_intent_execution_identity(intent),
                )
            else:
                submitted = False
            if (
                not submitted
                and protection_preimage is not None
            ):
                self._entry_protection_stash = protection_preimage
                if protection_ready:
                    self._persist_entry_protection_stash()
        if submitted:
            self._processed_intent_ids.add(str(result.intent_id))
        else:
            denial = self.denials[-1] if self.denials else OrderDenied(
                "order_submit_failed",
                str(result.intent_id),
            )
            self._report_denial(intent, denial)

    def _live_canary_intent_denial(
        self,
        intent: Any,
        *,
        action: str,
        order_plan: dict[str, Any],
    ) -> OrderDenied | None:
        canary_applies = self._live_canary_applies(
            action=action,
            order_plan=order_plan,
        )
        if canary_applies and action != "open_position":
            return OrderDenied("canary_open_position_only", action)
        gate_denial = self._live_open_gate_intent_denial(
            intent,
            action=action,
            order_plan=order_plan,
        )
        if gate_denial is not None:
            return gate_denial
        if not canary_applies:
            return None
        permit = order_plan.get("canary_permit")
        if not isinstance(permit, dict):
            return OrderDenied("canary_permit_missing", str(intent.intent_id))
        raw_permit_id = str(permit.get("permit_id") or "").strip()
        try:
            UUID(raw_permit_id)
        except ValueError:
            return OrderDenied("canary_permit_invalid", raw_permit_id)
        expected_release_id = str(
            getattr(self.config, "release_id", "") or ""
        ).strip()
        if not expected_release_id:
            return OrderDenied(
                "canary_release_missing",
                str(intent.intent_id),
            )
        permit_identity = (
            str(permit.get("account_id") or "").strip(),
            str(permit.get("node_id") or "").strip(),
            str(permit.get("release_id") or "").strip(),
        )
        expected_identity = (
            str(getattr(self.config, "account_id", "")).strip(),
            str(getattr(self.config, "node_id", "")).strip(),
            expected_release_id,
        )
        if permit_identity != expected_identity:
            return OrderDenied(
                "canary_identity_mismatch",
                str(intent.intent_id),
            )
        expires_at = _permit_expiry(permit.get("expires_at"))
        if expires_at is False:
            return OrderDenied(
                "canary_permit_expiry_invalid",
                str(intent.intent_id),
            )
        if expires_at <= _aware_datetime(self._now()):
            return OrderDenied(
                "canary_permit_expired",
                expires_at.isoformat(),
            )
        permit_symbol = _canonical_symbol(permit.get("symbol"))
        intent_symbol = _canonical_symbol(
            getattr(intent, "instrument_id", "")
        )
        if not permit_symbol or permit_symbol != intent_symbol:
            return OrderDenied(
                "canary_symbol_mismatch",
                str(intent.intent_id),
            )
        expected_baseline = str(
            permit.get("portfolio_baseline_sha256") or ""
        ).strip()
        if re.fullmatch(r"[0-9a-f]{64}", expected_baseline) is None:
            return OrderDenied(
                "canary_portfolio_baseline_invalid",
                str(intent.intent_id),
            )
        baseline_provider = self._live_canary_portfolio_baseline
        if baseline_provider is None:
            return OrderDenied(
                "canary_portfolio_baseline_unavailable",
                str(intent.intent_id),
            )
        try:
            current_baseline = baseline_provider(permit_symbol)
        except Exception as exc:
            return OrderDenied(
                "canary_portfolio_baseline_unavailable",
                repr(exc),
            )
        current_baseline = str(current_baseline or "").strip()
        if re.fullmatch(r"[0-9a-f]{64}", current_baseline) is None:
            return OrderDenied(
                "canary_portfolio_baseline_unavailable",
                str(intent.intent_id),
            )
        if current_baseline != expected_baseline:
            return OrderDenied(
                "canary_portfolio_baseline_drift",
                str(intent.intent_id),
            )
        permit_notional = _positive_canary_decimal(
            permit.get("max_notional_usdt")
        )
        permit_loss_limit = _positive_canary_decimal(
            permit.get("max_cumulative_loss_usdt")
        )
        risk_budget = getattr(intent, "risk_budget", None)
        intent_notional = _positive_canary_decimal(
            getattr(risk_budget, "max_notional", None)
        )
        if (
            permit_notional is None
            or permit_loss_limit is None
            or intent_notional is None
        ):
            return OrderDenied(
                "canary_notional_invalid",
                str(intent.intent_id),
            )
        if (
            permit_notional > Decimal("12")
            or intent_notional > permit_notional
        ):
            return OrderDenied(
                "canary_notional_exceeded",
                str(intent.intent_id),
            )
        if permit_loss_limit >= Decimal("1.5"):
            return OrderDenied(
                "canary_loss_limit_exceeded",
                str(intent.intent_id),
            )
        if str(order_plan.get("type") or "").strip().lower() != "limit":
            return OrderDenied(
                "canary_limit_ioc_required",
                str(intent.intent_id),
            )
        if (
            str(order_plan.get("time_in_force") or "").strip().upper()
            != "IOC"
        ):
            return OrderDenied(
                "canary_limit_ioc_required",
                str(intent.intent_id),
            )
        quantity = _positive_canary_decimal(order_plan.get("quantity"))
        price = _positive_canary_decimal(order_plan.get("price"))
        if quantity is None or price is None:
            return OrderDenied(
                "canary_limit_quantity_price_required",
                str(intent.intent_id),
            )
        return None

    def _live_open_gate_intent_denial(
        self,
        intent: Any,
        *,
        action: str,
        order_plan: Mapping[str, Any],
    ) -> OrderDenied | None:
        if not self._requires_live_canary_runtime():
            return None
        if action not in {"open_position", "add_position"}:
            return None
        if isinstance(order_plan.get("canary_permit"), Mapping):
            return None
        trusted_gate = self._live_open_gate()
        normalized_trusted = normalize_live_open_gate(trusted_gate)
        if normalized_trusted is False:
            return OrderDenied(
                "live_open_gate_unavailable",
                str(getattr(intent, "intent_id", "")),
            )
        if normalized_trusted["mode"] == "canary_only":
            return OrderDenied(
                "canary_permit_missing",
                str(getattr(intent, "intent_id", "")),
            )
        expected_release_id = str(
            getattr(self.config, "release_id", "") or ""
        ).strip()
        denial = live_open_gate_denial(
            order_plan.get("live_open_gate"),
            trusted_gate=normalized_trusted,
            expected_release_id=expected_release_id,
            require_normal=True,
        )
        if denial is None:
            return None
        return OrderDenied(
            denial,
            str(getattr(intent, "intent_id", "")),
        )

    def _live_canary_applies(
        self,
        *,
        action: str,
        order_plan: Mapping[str, Any],
    ) -> bool:
        if not self._requires_live_canary_runtime():
            return False
        if action not in {"open_position", "add_position"}:
            return False
        account_id = getattr(self.config, "account_id", "")
        rollout_phase = str(
            order_plan.get("rollout_phase") or ""
        ).strip()
        if not rollout_phase:
            rollout_phase = self._live_rollout_phase()
        if live_canary_permit_required(
            account_id,
            rollout_phase,
        ):
            return True
        return "canary_permit" in order_plan

    def _live_canary_execution_identity(
        self,
        intent: Any,
        plan: OrderPlan,
    ) -> LiveCanaryExecutionIdentity | OrderDenied | bool:
        if not self._requires_live_canary_runtime():
            return False
        if plan.reduce_only:
            return False
        order_plan = getattr(intent, "order_plan", {}) or {}
        account_id = getattr(self.config, "account_id", "")
        rollout_phase = str(
            order_plan.get("rollout_phase") or ""
        ).strip()
        if not rollout_phase:
            rollout_phase = self._live_rollout_phase()
        if (
            not live_canary_permit_required(
                account_id,
                rollout_phase,
            )
            and "canary_permit" not in order_plan
        ):
            return False
        permit = order_plan.get("canary_permit")
        if not isinstance(permit, dict):
            return OrderDenied(
                "canary_permit_missing",
                str(plan.intent_id),
            )
        try:
            return LiveCanaryExecutionIdentity(
                permit_id=str(permit.get("permit_id") or ""),
                release_id=str(permit.get("release_id") or ""),
                intent_id=str(plan.intent_id),
                client_order_id=str(plan.client_order_id),
                account_id=str(permit.get("account_id") or ""),
                node_id=str(permit.get("node_id") or ""),
                symbol=_canonical_symbol(permit.get("symbol")),
                max_notional_usdt=str(
                    permit.get("max_notional_usdt") or ""
                ),
                max_cumulative_loss_usdt=str(
                    permit.get("max_cumulative_loss_usdt") or ""
                ),
                authorized_limit_price_usdt=str(plan.price or ""),
                expires_at=str(permit.get("expires_at") or ""),
                portfolio_baseline_sha256=str(
                    permit.get("portfolio_baseline_sha256") or ""
                ),
            ).normalized()
        except (TypeError, ValueError, RuntimeError) as exc:
            return OrderDenied(
                "canary_execution_identity_invalid",
                repr(exc),
            )

    def _live_rollout_phase(self) -> str | None:
        getter = self._live_rollout_phase_getter
        if getter is None:
            return None
        try:
            phase = getter()
        except Exception:
            return None
        normalized = str(phase or "").strip()
        return normalized or None

    def _live_open_gate(self) -> Mapping[str, Any] | bool:
        getter = self._live_open_gate_getter
        if getter is None:
            return False
        try:
            return getter()
        except Exception:
            return False

    def _stash_entry_protection(self, intent: Any, plan: OrderPlan) -> bool:
        if not self._stage_entry_protection(intent, plan):
            return False
        return self._persist_entry_protection_stash()

    def _stage_entry_protection(
        self,
        intent: Any,
        plan: OrderPlan,
    ) -> bool:
        order_plan = getattr(intent, "order_plan", {}) or {}
        stop_loss = order_plan.get("stop_loss")
        take_profits = order_plan.get("take_profits")
        authorization = _authorization_from_tags(plan.tags)
        if not authorization:
            self._record_denial(
                OrderDenied(
                    "protection_authorization_missing",
                    str(plan.intent_id),
                )
            )
            return False
        source_message_id = authorization["source_message_id"]
        parent_intent_id = authorization["parent_intent_id"]
        same_source_owner: Optional[tuple[str, dict[str, Any]]] = None
        for key, other in tuple(self._entry_protection_stash.items()):
            if str(other.get("instrument_id")) != plan.instrument_id:
                continue
            if str(other.get("entry_side")) != plan.side:
                continue
            other_authorization = _stash_protection_authorization(other)
            other_source_message_id = other_authorization.get("source_message_id")
            if other_source_message_id == source_message_id:
                same_source_owner = (key, other)
                continue
            self._entry_protection_stash.pop(key, None)
            self._cancel_clock_timer(self._PROTECTION_TIMER_PREFIX + key)

        if stop_loss is None and not take_profits:
            return True

        if same_source_owner is not None:
            key, _other = same_source_owner
            if key != str(plan.intent_id):
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
            "stop_loss_parent_intent_id": parent_intent_id,
            "take_profit_parent_intent_id": parent_intent_id,
            "stop_loss_authorization": authorization,
            "take_profit_authorization": authorization,
            "entry_sequence_max": 1,
            "protection_sequence_start": 11,
            "protection_roles": {},
            "tp_consumed": {},
            "pending_cancel_ids": (),
        }
        return True

    def _queue_single_intent_submit(
        self,
        intent: Any,
        plan: OrderPlan,
        *,
        live_canary_execution: LiveCanaryExecutionIdentity | bool,
        intent_execution: IntentExecutionIdentity,
        action: str,
    ) -> bool:
        protection_preimage: Mapping[str, Any] | bool = False
        protection_payload: Mapping[str, Any] | bool = False
        if action in {"open_position", "add_position"}:
            protection_preimage = copy.deepcopy(
                self._entry_protection_stash
            )
            if not self._stage_entry_protection(intent, plan):
                denial = self.denials[-1] if self.denials else OrderDenied(
                    "protection_stash_invalid",
                    str(plan.intent_id),
                )
                self._report_denial(intent, denial)
                return False
            if self._entry_protection_stash != protection_preimage:
                protection_payload = (
                    self._entry_protection_stash_payload()
                )
        task = _DurableIoTask(
            kind=_DurableIoTaskKind.PREPARE_SUBMIT,
            intent=intent,
            intent_execution=intent_execution,
            client_order_ids=(str(plan.client_order_id),),
            plans=(plan,),
            live_canary_execution=live_canary_execution,
            protection_payload=protection_payload,
            continuation={
                "kind": "prepare_submit",
                "mode": "single",
                "protection_preimage": protection_preimage,
            },
        )
        if self._submit_durable_io_task(task):
            return True
        if isinstance(protection_preimage, Mapping):
            self._entry_protection_stash = copy.deepcopy(
                dict(protection_preimage)
            )
        denial = self.denials[-1] if self.denials else OrderDenied(
            "intent_dispatch_queue_rejected",
            intent_execution.intent_id,
        )
        self._report_denial(intent, denial)
        return False

    def _handle_zone_ladder(
        self,
        intent: Any,
        order_plan: dict[str, Any],
        context: PlannerContext,
        action: str,
        *,
        intent_execution: IntentExecutionIdentity,
        durable_async: bool = False,
    ) -> None:
        plans = self._zone_ladder_order_plans(intent, order_plan, context, action)
        if isinstance(plans, OrderDenied):
            self._record_denial(plans)
            self._report_denial(intent, plans)
            return

        if durable_async:
            protection_preimage = copy.deepcopy(
                self._entry_protection_stash
            )
            if not self._stage_entry_protection(intent, plans[0]):
                self._entry_protection_stash = protection_preimage
                denial = self.denials[-1] if self.denials else OrderDenied(
                    "protection_stash_invalid",
                    str(plans[0].intent_id),
                )
                self._report_denial(intent, denial)
                return
            stash = self._entry_protection_stash.get(
                str(plans[0].intent_id)
            )
            if stash is not None:
                stash["entry_sequence_max"] = 9
                stash["protection_sequence_start"] = 11
            protection_payload: Mapping[str, Any] | bool = False
            if self._entry_protection_stash != protection_preimage:
                protection_payload = (
                    self._entry_protection_stash_payload()
                )
            task = _DurableIoTask(
                kind=_DurableIoTaskKind.PREPARE_SUBMIT,
                intent=intent,
                intent_execution=intent_execution,
                client_order_ids=tuple(
                    plan.client_order_id for plan in plans
                ),
                plans=plans,
                protection_payload=protection_payload,
                continuation={
                    "kind": "prepare_submit",
                    "mode": "zone_ladder",
                    "protection_preimage": protection_preimage,
                },
            )
            if self._submit_durable_io_task(task):
                return
            self._entry_protection_stash = protection_preimage
            denial = self.denials[-1] if self.denials else OrderDenied(
                "intent_dispatch_queue_rejected",
                intent_execution.intent_id,
            )
            self._report_denial(intent, denial)
            return

        try:
            dispatch_result = self._intent_execution_inbox.begin_dispatch(
                intent_execution,
                tuple(plan.client_order_id for plan in plans),
            )
        except RuntimeError as exc:
            denial = OrderDenied(
                "intent_dispatch_persist_failed",
                repr(exc),
            )
            self._record_denial(denial)
            self._report_denial(intent, denial)
            return
        if dispatch_result is IntentDispatchResult.EXCHANGE_CONFIRMED:
            self._processed_intent_ids.add(intent_execution.intent_id)
            return
        if dispatch_result is IntentDispatchResult.REJECTED:
            denial = OrderDenied(
                "durable_intent_rejected",
                intent_execution.intent_id,
            )
            self._record_denial(denial)
            self._report_denial(intent, denial)
            return
        if dispatch_result is IntentDispatchResult.RECOVERY_REQUIRED:
            record = self._intent_execution_inbox.get(intent_execution)
            if (
                record is not False
                and self._durable_intent_orders_exist(record)
            ):
                self._intent_execution_inbox.mark_exchange_confirmed(
                    intent_execution
                )
                self._processed_intent_ids.add(
                    intent_execution.intent_id
                )
                return
            denial = OrderDenied(
                "intent_exchange_confirmation_required",
                intent_execution.intent_id,
            )
            self._record_denial(denial)
            self._report_denial(intent, denial)
            return

        protection_preimage = copy.deepcopy(self._entry_protection_stash)
        if not self._stash_entry_protection(intent, plans[0]):
            self._entry_protection_stash = protection_preimage
            self._report_denial(intent, self.denials[-1])
            return
        stash = self._entry_protection_stash.get(str(plans[0].intent_id))
        if stash is not None:
            stash["entry_sequence_max"] = 9
            stash["protection_sequence_start"] = 11
            if not self._persist_entry_protection_stash():
                self._entry_protection_stash = protection_preimage
                self._report_denial(intent, self.denials[-1])
                return
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
        authorization = _authorization_source(intent)
        if isinstance(authorization, OrderDenied):
            return authorization

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

    def on_order_submitted(self, event: Any) -> None:
        del event

    def on_order_accepted(self, event: Any) -> None:
        self._confirm_durable_intent_order_event(event)
        self._confirm_live_canary_order_event(event)

    def on_order_filled(self, event: Any) -> None:
        self._confirm_durable_intent_order_event(event)
        self._confirm_live_canary_order_event(event)
        self._queue_live_canary_fill(event)
        client_order_id = _event_client_order_id(event)
        if client_order_id is None:
            return
        instrument_id = _event_instrument_id(event)
        protection_match = self._protection_role_for_order(client_order_id, instrument_id)
        if protection_match is not None:
            intent_key, stash, role_info = protection_match
            if role_info.get("role") == "take_profit":
                qty = _event_last_qty(event)
                price = role_info.get("tp_price")
                if qty is not None and price is not None:
                    self._add_tp_consumed(stash, str(price), qty)
                    self._consume_tp_market_fallback(
                        stash,
                        client_order_id,
                        qty,
                    )
            self._record_quick_protection_fill(intent_key, stash, role_info)
            continuation: Mapping[str, Any] | bool = False
            if not stash.get("protection_frozen"):
                continuation = {
                    "kind": "protection_schedule",
                    "intent_key": intent_key,
                }
            self._queue_entry_protection_stash_persist(
                continuation=continuation
            )
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

    def _protection_role_for_order(
        self,
        client_order_id: str,
        instrument_id: Optional[str],
    ) -> Optional[tuple[str, dict[str, Any], dict[str, Any]]]:
        for intent_key, stash in self._entry_protection_stash.items():
            if instrument_id is not None and str(stash.get("instrument_id")) != str(instrument_id):
                continue
            roles = stash.get("protection_roles")
            if isinstance(roles, dict):
                role_info = roles.get(client_order_id)
                if isinstance(role_info, dict):
                    return intent_key, stash, role_info
            if client_order_id in tuple(stash.get("protection_ids") or ()):
                role_info = self._legacy_protection_role_info(client_order_id, stash)
                if role_info is not None:
                    return intent_key, stash, role_info
        return None

    def _legacy_protection_role_info(
        self,
        client_order_id: str,
        stash: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        try:
            trace = decode_client_order_id(client_order_id)
        except ValueError:
            return None
        if trace.sequence < int(stash.get("protection_sequence_start", 11)):
            return None
        offset = trace.sequence % 10
        if offset == 1:
            return {
                "role": "stop_loss",
                "tp_price": None,
                "quantity": str(stash.get("protected_quantity") or ""),
                "submitted_at": "",
            }
        index = offset - 2
        targets = _take_profit_prices(stash.get("take_profits"))
        quantities = stash.get("take_profit_quantities")
        if 0 <= index < len(targets):
            quantity = ""
            if isinstance(quantities, (list, tuple)) and index < len(quantities):
                quantity = str(quantities[index])
            return {
                "role": "take_profit",
                "tp_price": _price_key(targets[index]),
                "quantity": quantity,
                "submitted_at": "",
            }
        return None

    def _add_tp_consumed(self, stash: dict[str, Any], price: str, quantity: str) -> None:
        consumed = stash.setdefault("tp_consumed", {})
        try:
            total = Decimal(str(consumed.get(price, "0"))) + Decimal(str(quantity))
        except (InvalidOperation, ValueError, TypeError):
            total = Decimal(str(quantity))
        consumed[price] = format(total, "f")

    def _record_quick_protection_fill(
        self,
        intent_key: str,
        stash: dict[str, Any],
        role_info: dict[str, Any],
    ) -> None:
        submitted_at_raw = role_info.get("submitted_at")
        try:
            submitted_at = _aware_datetime(datetime.fromisoformat(str(submitted_at_raw)))
        except (TypeError, ValueError):
            return
        now = self._now()
        if now - submitted_at > timedelta(seconds=30):
            return
        window = [
            item for item in self._quick_fill_windows.get(intent_key, [])
            if now - item <= timedelta(seconds=120)
        ]
        window.append(now)
        self._quick_fill_windows[intent_key] = window
        if len(window) >= 2:
            self._freeze_protection(
                intent_key,
                stash,
                "cascade",
                denial_reason="protection_cascade_frozen",
            )

    def _freeze_protection(
        self,
        intent_key: str,
        stash: dict[str, Any],
        reason: str,
        *,
        denial_reason: str,
    ) -> None:
        stash["protection_frozen"] = reason
        stash["protection_freeze_denial_reason"] = denial_reason
        key = (denial_reason, intent_key)
        if key not in self._reported_protection_denials:
            self._reported_protection_denials.add(key)
            self._record_denial(OrderDenied(denial_reason, intent_key))
        self._report_protection_freeze_event(intent_key, stash)
        self._cancel_clock_timer(self._PROTECTION_TIMER_PREFIX + intent_key)

    def _report_protection_freeze_event(
        self,
        intent_key: str,
        stash: dict[str, Any],
    ) -> None:
        reason = str(stash.get("protection_frozen") or "")
        if not reason:
            return
        denial_reason = str(
            stash.get("protection_freeze_denial_reason")
            or f"protection_{reason}_frozen"
        )
        event_key = f"{intent_key}:{reason}:{denial_reason}"
        if stash.get("protection_alert_sent") == event_key:
            return
        sent = self._report_protection_event(
            intent_key,
            stash,
            event_type="ProtectionFrozen",
            event_key=event_key,
            payload={
                "reason": reason,
                "denial_reason": denial_reason,
                "protection_revision": int(
                    stash.get("protection_revision", -1)
                ),
            },
        )
        if sent:
            stash["protection_alert_sent"] = event_key

    def _report_protection_event(
        self,
        intent_key: str,
        stash: dict[str, Any],
        *,
        event_type: str,
        event_key: str,
        payload: dict[str, Any],
        client_order_id: Optional[str] = None,
    ) -> bool:
        event = {
            "event_type": event_type,
            "event_key": event_key,
            "intent_id": intent_key,
            "client_order_id": client_order_id,
            "instrument_id": str(stash.get("instrument_id") or ""),
            "ts_event": self._now(),
            "payload": payload,
        }
        log = getattr(self, "log", None)
        if log is not None and hasattr(log, "error"):
            log.error(
                event_type
                + " "
                + json.dumps(
                    event,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
            )
        reporter = self._protection_event_reporter
        if reporter is None:
            return False
        try:
            return bool(reporter(event))
        except Exception as exc:
            if log is not None and hasattr(log, "error"):
                log.error(
                    f"{event_type} reporter failed event_key={event_key} "
                    f"error={exc!r}"
                )
            return False

    def on_stop(self) -> None:
        self._strategy_stopping = True
        self._cancel_clock_timer("strategy.durable-io.mailbox")
        self._cancel_clock_timer("exchange-state.reconcile")
        self._cancel_clock_timer("terminal-exchange.mailbox")
        self._cancel_clock_timer("live-canary.mark-to-market")
        for intent_key in tuple(self._entry_protection_stash):
            self._cancel_clock_timer(self._PROTECTION_TIMER_PREFIX + intent_key)
        self._durable_io_worker.stop(
            timeout_seconds=self._DURABLE_IO_SHUTDOWN_TIMEOUT_SECONDS
        )
        self.drain_durable_io_mailbox(
            max_results=self._DURABLE_IO_QUEUE_CAPACITY
        )

    def on_event(self, event: Any) -> None:
        if self._strategy_stopping:
            return
        # TimeEvent fallback path for clocks whose set_time_alert has no callback arg.
        name = getattr(event, "name", None)
        if str(name or "") == "strategy.durable-io.mailbox":
            self.drain_durable_io_mailbox()
            return
        if str(name or "") == "terminal-exchange.mailbox":
            self.drain_terminal_exchange_mailbox()
            return
        if name is not None and str(name).startswith(self._PROTECTION_TIMER_PREFIX):
            self._sync_protection(str(name)[len(self._PROTECTION_TIMER_PREFIX):])

    # A protection order dying at/before the venue (rejected/denied/expired, or
    # cancelled by something other than this strategy) is invisible to the fill
    # path — without these hooks a venue rejection (e.g. Binance -2021 "would
    # immediately trigger") leaves the position naked exactly when price is
    # attacking the stop. Any terminal event on a protection id re-arms the sync.
    def on_order_rejected(self, event: Any) -> None:
        self._confirm_durable_intent_order_event(event)
        self._confirm_live_canary_order_event(event)
        self._on_protection_order_terminal(event, count_retry=True)

    def on_order_denied(self, event: Any) -> None:
        self._on_protection_order_terminal(event, count_retry=True)

    def on_order_canceled(self, event: Any) -> None:
        self._confirm_durable_intent_order_event(event)
        self._confirm_live_canary_order_event(event)
        # Usually our own make-before-break cancel confirmations: resync to
        # verify convergence, but do NOT feed the backoff counter (review P2-3).
        self._on_protection_order_terminal(event, count_retry=False)

    def on_order_expired(self, event: Any) -> None:
        self._confirm_durable_intent_order_event(event)
        self._confirm_live_canary_order_event(event)
        self._on_protection_order_terminal(event, count_retry=True)

    def _confirm_durable_intent_order_event(self, event: Any) -> None:
        client_order_id = _event_client_order_id(event)
        if client_order_id is None:
            return
        self._submit_durable_io_task(
            _DurableIoTask(
                kind=_DurableIoTaskKind.INTENT_EXCHANGE_CONFIRMED,
                client_order_id=client_order_id,
            )
        )

    def _submit_durable_io_task(
        self,
        task: _DurableIoTask,
    ) -> bool:
        if self._strategy_stopping:
            return False
        if self._durable_io_halted_reason:
            return False
        if not self._durable_io_worker.snapshot().running:
            self._durable_io_worker.start()
        return self._durable_io_worker.submit(task)

    def _process_durable_io_task(
        self,
        task: _DurableIoTask,
    ) -> None:
        if task.kind is _DurableIoTaskKind.INTENT_EXCHANGE_CONFIRMED:
            self._intent_execution_inbox.mark_exchange_confirmed_by_client_order_id(
                task.client_order_id
            )
            return
        outcome: Any = False
        if task.kind is _DurableIoTaskKind.INTENT_RECEIVE:
            outcome = self._process_intent_receive_task(task)
        elif task.kind is _DurableIoTaskKind.PREPARE_SUBMIT:
            outcome = self._process_prepare_submit_task(task)
        elif task.kind is _DurableIoTaskKind.MANAGEMENT_PREPARE:
            outcome = self._process_management_prepare_task(task)
        elif task.kind is _DurableIoTaskKind.MANAGEMENT_COMPLETE:
            self._process_management_complete_task(task)
            outcome = True
        elif task.kind is _DurableIoTaskKind.RECOVERY_CONFIRMED:
            self._process_recovery_confirmed_task(task)
            outcome = True
        elif task.kind is _DurableIoTaskKind.CANARY_MARK_DISPATCHED:
            identity = task.live_canary_execution
            if not isinstance(
                identity,
                LiveCanaryExecutionIdentity,
            ):
                raise ValueError(
                    "canary mark-dispatched task requires identity"
                )
            self._live_canary_execution_store.mark_dispatched(identity)
            outcome = True
        elif task.kind is _DurableIoTaskKind.PROTECTION_STASH_PERSIST:
            payload = task.protection_payload
            if not isinstance(payload, Mapping):
                raise ValueError(
                    "protection stash task requires payload"
                )
            if not task.operation_id:
                raise ValueError(
                    "protection stash task requires operation_id"
                )
            if task.protection_version < 1:
                raise ValueError(
                    "protection stash task requires version"
                )
            expected_sha256 = _protection_payload_sha256(payload)
            if task.protection_payload_sha256 != expected_sha256:
                raise ValueError(
                    "protection stash task payload digest mismatch"
                )
            self._write_entry_protection_stash(payload)
            outcome = True
        else:
            raise ValueError(f"unsupported durable I/O task: {task.kind}")
        if task.continuation is not False:
            self._publish_durable_io_result(
                _DurableIoResult(task=task, outcome=outcome)
            )

    def _publish_durable_io_result(
        self,
        result: _DurableIoResult,
    ) -> None:
        if self._strategy_stopping:
            return
        try:
            self._durable_io_mailbox.put_nowait(result)
        except Full:
            self._halt_durable_io(
                "strategy durable I/O result mailbox capacity exceeded"
            )

    def _on_durable_io_result(
        self,
        result: _DurableIoResult,
    ) -> None:
        if self._strategy_stopping or self._durable_io_halted_reason:
            self._discard_durable_io_result(result)
            return
        continuation = result.task.continuation
        if not isinstance(continuation, Mapping):
            return
        kind = str(continuation.get("kind") or "")
        if kind == "intent_received":
            self._on_intent_received_result(result)
            return
        if kind == "prepare_submit":
            self._on_prepare_submit_result(result)
            return
        if kind == "management_prepared":
            self._on_management_prepared_result(result)
            return
        if kind == "management_completed":
            identity = result.task.intent_execution
            if isinstance(identity, IntentExecutionIdentity):
                self._processed_intent_ids.add(identity.intent_id)
            return
        if kind == "intent_recovery_confirmed":
            identity = result.task.intent_execution
            if isinstance(identity, IntentExecutionIdentity):
                self._processed_intent_ids.add(identity.intent_id)
            return
        if kind == "canary_dispatched":
            identity = result.task.live_canary_execution
            if isinstance(identity, LiveCanaryExecutionIdentity):
                self._after_live_canary_dispatched(identity)
            intent_execution = result.task.intent_execution
            if isinstance(intent_execution, IntentExecutionIdentity):
                self._processed_intent_ids.add(
                    intent_execution.intent_id
                )
            return
        if kind == "protection_persisted":
            self._on_protection_persisted_result(result)
            return
        self._halt_durable_io(
            f"unsupported durable I/O continuation: {kind}"
        )

    def _discard_durable_io_result(
        self,
        result: _DurableIoResult,
    ) -> None:
        task = result.task
        if task.kind in {
            _DurableIoTaskKind.PREPARE_SUBMIT,
            _DurableIoTaskKind.MANAGEMENT_PREPARE,
        }:
            self._restore_prepare_submit_preimage(task)
            return
        if (
            task.kind
            is _DurableIoTaskKind.PROTECTION_STASH_PERSIST
            and task.protection_version > 0
        ):
            self._protection_durable_continuations.pop(
                task.protection_version,
                None,
            )

    def _on_protection_persisted_result(
        self,
        result: _DurableIoResult,
    ) -> None:
        version = result.task.protection_version
        if version < 1:
            self._halt_durable_io(
                "protection persist result missing version"
            )
            return
        self._protection_stash_persisted_version = max(
            self._protection_stash_persisted_version,
            version,
        )
        if version != self._protection_stash_version:
            return
        continuations = self._protection_durable_continuations.pop(
            version,
            [],
        )
        for continuation in continuations:
            self._run_protection_continuation(continuation)

    def _run_protection_continuation(
        self,
        continuation: Mapping[str, Any],
    ) -> None:
        kind = str(continuation.get("kind") or "")
        if kind == "protection_retry":
            self._continue_protection_retry(continuation)
            return
        if kind == "protection_schedule":
            intent_key = str(
                continuation.get("intent_key") or ""
            )
            if intent_key:
                self._schedule_protection_sync(intent_key)
            return
        if kind == "protection_schedule_delay":
            intent_key = str(
                continuation.get("intent_key") or ""
            )
            raw_delay = continuation.get("delay_seconds", False)
            delay_seconds: float | None = None
            if raw_delay is not False:
                delay_seconds = float(raw_delay)
            if intent_key:
                self._schedule_protection_sync(
                    intent_key,
                    delay_seconds=delay_seconds,
                )
            return
        if kind == "protection_submit_revision":
            self._continue_protection_revision_submit(
                continuation
            )
            return
        if kind == "management_dispatch_after_persist":
            self._queue_management_dispatch_task(
                continuation
            )
            return
        if kind == "management_finalize_after_persist":
            plan = continuation.get("plan")
            source_intent = continuation.get("source_intent")
            if not isinstance(plan, ManagementPlan):
                self._halt_durable_io(
                    "management finalize continuation missing plan"
                )
                return
            self._queue_management_complete_task(
                plan,
                source_intent=source_intent,
            )
            return
        if kind == "take_profit_retry_after_persist":
            intent_key = str(
                continuation.get("intent_key") or ""
            )
            stash = self._entry_protection_stash.get(intent_key)
            requests = continuation.get("requests")
            if isinstance(stash, dict) and isinstance(requests, tuple):
                self._queue_take_profit_retry(
                    intent_key=intent_key,
                    stash=stash,
                    requests=requests,
                )
            return
        if kind == "immediate_tp_market_fallback":
            self._continue_immediate_tp_market_fallback(
                continuation
            )
            return
        self._halt_durable_io(
            f"unsupported protection continuation: {kind}"
        )

    def _on_intent_received_result(
        self,
        result: _DurableIoResult,
    ) -> None:
        task = result.task
        identity = task.intent_execution
        if not isinstance(identity, IntentExecutionIdentity):
            self._halt_durable_io(
                "intent receipt continuation missing identity"
            )
            return
        if not isinstance(result.outcome, Mapping):
            self._halt_durable_io(
                "intent receipt continuation missing outcome"
            )
            return
        self._continue_intent_after_receive(
            task.intent,
            identity,
            result.outcome,
            durable_async=True,
        )

    def _on_prepare_submit_result(
        self,
        result: _DurableIoResult,
    ) -> None:
        task = result.task
        outcome = result.outcome
        if not isinstance(outcome, Mapping):
            self._halt_durable_io(
                "prepare-submit continuation missing outcome"
            )
            return
        intent = task.intent
        intent_execution = task.intent_execution
        if not isinstance(intent_execution, IntentExecutionIdentity):
            self._halt_durable_io(
                "prepare-submit continuation missing intent identity"
            )
            return
        dispatch_result = outcome.get("dispatch_result", False)
        if dispatch_result is IntentDispatchResult.EXCHANGE_CONFIRMED:
            self._processed_intent_ids.add(intent_execution.intent_id)
            return
        if dispatch_result is IntentDispatchResult.REJECTED:
            denial = OrderDenied(
                "durable_intent_rejected",
                intent_execution.intent_id,
            )
            self._record_denial(denial)
            self._report_denial(intent, denial)
            self._restore_prepare_submit_preimage(task)
            return
        if dispatch_result is IntentDispatchResult.RECOVERY_REQUIRED:
            record = outcome.get("durable_record", False)
            if (
                record is not False
                and self._durable_intent_orders_exist(record)
            ):
                self._queue_recovery_confirmation(task)
                return
            denial = OrderDenied(
                "intent_exchange_confirmation_required",
                intent_execution.intent_id,
            )
            self._record_denial(denial)
            self._report_denial(intent, denial)
            self._restore_prepare_submit_preimage(task)
            return

        claim_result = outcome.get("claim_result", False)
        if claim_result is LiveCanaryClaimResult.PERMIT_CONFLICT:
            live_canary = task.live_canary_execution
            permit_id = str(
                getattr(live_canary, "permit_id", "") or ""
            )
            denial = OrderDenied(
                "canary_permit_already_claimed",
                permit_id,
            )
            self._record_denial(denial)
            self._report_denial(intent, denial)
            self._restore_prepare_submit_preimage(task)
            return
        if claim_result is LiveCanaryClaimResult.RECOVERY_REQUIRED:
            plans = task.plans
            if (
                len(plans) == 1
                and self._live_canary_order_exists(plans[0])
            ):
                self._queue_recovery_confirmation(task)
                return
            live_canary = task.live_canary_execution
            client_order_id = str(
                getattr(live_canary, "client_order_id", "") or ""
            )
            denial = OrderDenied(
                "canary_exchange_confirmation_required",
                client_order_id,
            )
            self._record_denial(denial)
            self._report_denial(intent, denial)
            self._restore_prepare_submit_preimage(task)
            return

        if (
            any(not plan.reduce_only for plan in task.plans)
            and self._trading_state().upper() != "ACTIVE"
        ):
            denial = OrderDenied(
                "trading_not_active",
                self._trading_state(),
            )
            self._record_denial(denial)
            self._report_denial(intent, denial)
            self._restore_prepare_submit_preimage(task)
            return

        submitted_plans: list[OrderPlan] = []
        for plan in task.plans:
            if not self._submit_order_plan_after_durable_prepare(
                plan,
                live_canary_execution=task.live_canary_execution,
            ):
                break
            submitted_plans.append(plan)
        if len(submitted_plans) != len(task.plans):
            self._cancel_partial_prepared_submit(
                task,
                submitted_plans,
            )
            denial = self.denials[-1] if self.denials else OrderDenied(
                "order_submit_failed",
                intent_execution.intent_id,
            )
            self._report_denial(intent, denial)
            return

        live_canary = task.live_canary_execution
        if isinstance(live_canary, LiveCanaryExecutionIdentity):
            self._submit_durable_io_task(
                _DurableIoTask(
                    kind=_DurableIoTaskKind.CANARY_MARK_DISPATCHED,
                    intent=intent,
                    intent_execution=intent_execution,
                    live_canary_execution=live_canary,
                    continuation={"kind": "canary_dispatched"},
                )
            )
            return
        self._processed_intent_ids.add(intent_execution.intent_id)

    def _on_management_prepared_result(
        self,
        result: _DurableIoResult,
    ) -> None:
        task = result.task
        identity = task.intent_execution
        continuation = task.continuation
        if not isinstance(identity, IntentExecutionIdentity):
            self._halt_durable_io(
                "management prepare continuation missing identity"
            )
            return
        if not isinstance(continuation, Mapping):
            self._halt_durable_io(
                "management prepare continuation missing payload"
            )
            return
        if not isinstance(result.outcome, Mapping):
            self._halt_durable_io(
                "management prepare continuation missing outcome"
            )
            return
        dispatch_result = result.outcome.get(
            "dispatch_result",
            False,
        )
        if dispatch_result is IntentDispatchResult.EXCHANGE_CONFIRMED:
            self._processed_intent_ids.add(identity.intent_id)
            return
        source_intent = task.intent
        if dispatch_result is IntentDispatchResult.REJECTED:
            denial = OrderDenied(
                "durable_intent_rejected",
                identity.intent_id,
            )
            self._record_denial(denial)
            self._report_denial(source_intent, denial)
            self._restore_prepare_submit_preimage(task)
            return
        if dispatch_result is IntentDispatchResult.RECOVERY_REQUIRED:
            denial = OrderDenied(
                "management_terminal_confirmation_required",
                identity.intent_id,
            )
            self._record_denial(denial)
            self._report_denial(source_intent, denial)
            return
        if dispatch_result is not IntentDispatchResult.READY:
            self._halt_durable_io(
                "management prepare continuation has invalid dispatch result"
            )
            return
        self._continue_management_after_persist(continuation)

    def _queue_recovery_confirmation(
        self,
        task: _DurableIoTask,
    ) -> bool:
        return self._submit_durable_io_task(
            _DurableIoTask(
                kind=_DurableIoTaskKind.RECOVERY_CONFIRMED,
                intent=task.intent,
                intent_execution=task.intent_execution,
                live_canary_execution=task.live_canary_execution,
                continuation={
                    "kind": "intent_recovery_confirmed",
                },
            )
        )

    def _restore_prepare_submit_preimage(
        self,
        task: _DurableIoTask,
    ) -> None:
        continuation = task.continuation
        if not isinstance(continuation, Mapping):
            return
        preimage = continuation.get("protection_preimage", False)
        if not isinstance(preimage, Mapping):
            return
        self._entry_protection_stash = copy.deepcopy(dict(preimage))

    def _cancel_partial_prepared_submit(
        self,
        task: _DurableIoTask,
        submitted_plans: list[OrderPlan],
    ) -> None:
        continuation = task.continuation
        mode = ""
        if isinstance(continuation, Mapping):
            mode = str(continuation.get("mode") or "")
        if mode != "zone_ladder":
            return
        for plan in submitted_plans:
            self._cancel_order_by_client_order_id(
                plan.instrument_id,
                plan.client_order_id,
            )

    def _continue_protection_retry(
        self,
        continuation: Mapping[str, Any],
    ) -> None:
        intent_key = str(continuation.get("intent_key") or "")
        if not intent_key:
            return
        stash = self._entry_protection_stash.get(intent_key)
        if not isinstance(stash, dict):
            return
        count_retry = bool(
            continuation.get("count_retry", True)
        )
        self._reschedule_protection_sync(
            intent_key,
            stash,
            count_retry=count_retry,
        )

    def _process_intent_receive_task(
        self,
        task: _DurableIoTask,
    ) -> dict[str, Any]:
        identity = task.intent_execution
        payload = task.intent_payload
        if not isinstance(identity, IntentExecutionIdentity):
            raise ValueError("intent receive task requires identity")
        if not isinstance(payload, Mapping):
            raise ValueError("intent receive task requires payload")
        record = self._intent_execution_inbox.get(identity)
        register_result: IntentRegisterResult | bool = False
        if record is False:
            register_result = (
                self._intent_execution_inbox.register_received(
                    identity,
                    payload,
                )
            )
            record = self._intent_execution_inbox.get(identity)
        return {
            "record": record,
            "register_result": register_result,
        }

    def _process_prepare_submit_task(
        self,
        task: _DurableIoTask,
    ) -> dict[str, Any]:
        intent_execution = task.intent_execution
        dispatch_result: IntentDispatchResult | bool = False
        durable_record: Any = False
        if isinstance(intent_execution, IntentExecutionIdentity):
            dispatch_result = (
                self._intent_execution_inbox.begin_dispatch(
                    intent_execution,
                    task.client_order_ids,
                )
            )
            if (
                dispatch_result
                is IntentDispatchResult.RECOVERY_REQUIRED
            ):
                durable_record = self._intent_execution_inbox.get(
                    intent_execution
                )
                return {
                    "dispatch_result": dispatch_result,
                    "durable_record": durable_record,
                    "claim_result": False,
                }
            if dispatch_result in {
                IntentDispatchResult.EXCHANGE_CONFIRMED,
                IntentDispatchResult.REJECTED,
            }:
                return {
                    "dispatch_result": dispatch_result,
                    "durable_record": False,
                    "claim_result": False,
                }
        claim_result: LiveCanaryClaimResult | bool = False
        live_canary = task.live_canary_execution
        if isinstance(live_canary, LiveCanaryExecutionIdentity):
            claim_result = self._live_canary_execution_store.claim(
                live_canary
            )
            if claim_result is LiveCanaryClaimResult.PERMIT_CONFLICT:
                if isinstance(
                    intent_execution,
                    IntentExecutionIdentity,
                ):
                    self._intent_execution_inbox.mark_rejected(
                        intent_execution,
                        "canary_permit_already_claimed",
                    )
                return {
                    "dispatch_result": dispatch_result,
                    "durable_record": False,
                    "claim_result": claim_result,
                }
        protection_payload = task.protection_payload
        if isinstance(protection_payload, Mapping):
            self._write_entry_protection_stash(protection_payload)
        return {
            "dispatch_result": dispatch_result,
            "durable_record": durable_record,
            "claim_result": claim_result,
        }

    def _process_management_prepare_task(
        self,
        task: _DurableIoTask,
    ) -> dict[str, Any]:
        identity = task.intent_execution
        if not isinstance(identity, IntentExecutionIdentity):
            raise ValueError(
                "management prepare task requires intent identity"
            )
        dispatch_result = self._intent_execution_inbox.begin_dispatch(
            identity,
            task.client_order_ids,
        )
        return {"dispatch_result": dispatch_result}

    def _process_management_complete_task(
        self,
        task: _DurableIoTask,
    ) -> None:
        identity = task.intent_execution
        if not isinstance(identity, IntentExecutionIdentity):
            raise ValueError(
                "management complete task requires intent identity"
            )
        self._intent_execution_inbox.mark_exchange_confirmed(identity)

    def _process_recovery_confirmed_task(
        self,
        task: _DurableIoTask,
    ) -> None:
        live_canary = task.live_canary_execution
        if isinstance(live_canary, LiveCanaryExecutionIdentity):
            self._live_canary_execution_store.mark_exchange_confirmed(
                live_canary
            )
        intent_execution = task.intent_execution
        if isinstance(intent_execution, IntentExecutionIdentity):
            self._intent_execution_inbox.mark_exchange_confirmed(
                intent_execution
            )

    def wait_for_durable_io(self, *, timeout_seconds: float) -> bool:
        return self._durable_io_worker.wait_empty(
            timeout_seconds=timeout_seconds
        )

    @property
    def durable_io_halted_reason(self) -> str:
        return self._durable_io_halted_reason

    def _halt_durable_io(self, reason: str) -> None:
        halt_reason = f"strategy durable I/O failed: {str(reason).strip()}"
        with self._durable_io_halt_lock:
            if self._durable_io_halted_reason:
                return
            self._durable_io_halted_reason = halt_reason
        handler = self._terminal_exchange_halt_handler
        if handler is None and self._requires_live_canary_runtime():
            handler = self._live_canary_halt_handler
        if handler is not None:
            handler(halt_reason)
        self._record_denial(
            OrderDenied(
                "strategy_durable_io_halted",
                halt_reason,
            )
        )

    def _confirm_live_canary_order_event(self, event: Any) -> None:
        if not self._requires_live_canary_runtime():
            return
        client_order_id = _event_client_order_id(event)
        if client_order_id is None:
            return
        reporter = self._live_canary_risk_reporter
        if reporter is None:
            self._halt_live_canary(
                "live canary risk reporter is unavailable"
            )
            return
        accepted = reporter(
            {
                "kind": "exchange_confirmed",
                "client_order_id": client_order_id,
            }
        )
        if accepted:
            return
        self._halt_live_canary(
            "live canary risk reporter queue rejected exchange event"
        )

    def _queue_live_canary_fill(self, event: Any) -> None:
        if not self._requires_live_canary_runtime():
            return
        reporter = self._live_canary_risk_reporter
        if reporter is None:
            self._halt_live_canary(
                "live canary risk reporter is unavailable"
            )
            return
        payload = self._live_canary_fill_payload(event)
        if payload is False:
            return
        client_order_id = str(
            payload.get("client_order_id") or ""
        ).strip()
        instrument_id = str(
            payload.get("instrument_id") or ""
        ).strip()
        if client_order_id and instrument_id != "UNKNOWN":
            self._live_canary_monitor_targets[
                client_order_id
            ] = instrument_id
        accepted = reporter(
            {
                "kind": "fill",
                "fill": payload,
            }
        )
        if accepted:
            return
        self._halt_live_canary(
            "live canary risk reporter queue rejected fill"
        )

    def _register_live_canary_mark_timer(self) -> None:
        clock = getattr(self, "clock", None)
        set_timer = getattr(clock, "set_timer", None)
        if not callable(set_timer):
            return
        interval = timedelta(seconds=1)
        try:
            set_timer(
                name="live-canary.mark-to-market",
                interval=interval,
                callback=self._on_live_canary_mark_timer,
            )
            return
        except TypeError:
            pass
        set_timer(
            "live-canary.mark-to-market",
            interval,
            self._on_live_canary_mark_timer,
        )

    def _on_live_canary_mark_timer(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        self._queue_live_canary_mark_checks()

    def _queue_live_canary_mark_checks(self) -> None:
        reporter = self._live_canary_risk_reporter
        if reporter is None:
            self._halt_live_canary(
                "live canary risk reporter is unavailable"
            )
            return
        for client_order_id, instrument_id in tuple(
            self._live_canary_monitor_targets.items()
        ):
            accepted = reporter(
                {
                    "kind": "mark",
                    "mark": self._live_canary_mark_payload(
                        client_order_id,
                        instrument_id,
                    ),
                }
            )
            if accepted:
                continue
            self._halt_live_canary(
                "live canary risk reporter queue rejected mark"
            )
            return

    def _live_canary_mark_payload(
        self,
        client_order_id: str,
        instrument_id: str,
    ) -> dict[str, str]:
        evaluated_at = _aware_datetime(self._now())
        update = self._cache_mark_price(instrument_id)
        mark_price = _positive_canary_decimal(
            getattr(update, "value", None)
        )
        accounting_errors = []
        if mark_price is None:
            accounting_errors.append("mark price is unavailable")
        raw_ts_event = getattr(update, "ts_event", None)
        try:
            ts_event = int(raw_ts_event)
        except (TypeError, ValueError, OverflowError):
            ts_event = 0
            accounting_errors.append(
                "mark price timestamp is unavailable"
            )
        observed_at = evaluated_at
        if ts_event > 0:
            observed_at = datetime.fromtimestamp(
                ts_event / 1_000_000_000,
                tz=timezone.utc,
            )
        baseline_context = self._live_canary_monitor_baselines.get(
            client_order_id
        )
        if baseline_context is None:
            accounting_errors.append(
                "portfolio baseline context is unavailable"
            )
        else:
            symbol, expected_baseline = baseline_context
            baseline_provider = self._live_canary_portfolio_baseline
            if baseline_provider is None:
                accounting_errors.append(
                    "portfolio baseline provider is unavailable"
                )
            else:
                try:
                    current_baseline = baseline_provider(symbol)
                except Exception:
                    current_baseline = False
                current_baseline = str(
                    current_baseline or ""
                ).strip()
                if current_baseline != expected_baseline:
                    accounting_errors.append(
                        "portfolio baseline drifted after permit consumption"
                    )
        mark_price_text = "0"
        if mark_price is not None:
            mark_price_text = format(mark_price, "f")
        return {
            "client_order_id": client_order_id,
            "instrument_id": instrument_id,
            "mark_price_usdt": mark_price_text,
            "observed_at": observed_at.isoformat(),
            "evaluated_at": evaluated_at.isoformat(),
            "accounting_error": "; ".join(accounting_errors),
        }

    def _live_canary_fill_payload(
        self,
        event: Any,
    ) -> dict[str, Any] | bool:
        client_order_id = _event_client_order_id(event)
        if client_order_id is None:
            return False
        instrument_id = _event_instrument_id(event)
        side = _event_order_side(event)
        quantity = _event_last_qty(event)
        price = _event_fill_price(event)
        fee, fee_error = _event_fee_usdt(event)
        accounting_errors = []
        if instrument_id is None:
            accounting_errors.append("fill instrument_id is unavailable")
        if side is None:
            accounting_errors.append("fill side is unavailable")
        if quantity is None:
            accounting_errors.append("fill quantity is unavailable")
        if price is None:
            accounting_errors.append("fill price is unavailable")
        if fee_error:
            accounting_errors.append(fee_error)
        mark_price = "0"
        if instrument_id is not None:
            mark_update = self._cache_mark_price(instrument_id)
            mark_value = getattr(mark_update, "value", None)
            parsed_mark = _positive_canary_decimal(mark_value)
            if parsed_mark is not None:
                mark_price = format(parsed_mark, "f")
            else:
                accounting_errors.append("mark price is unavailable")
        fill_id = _event_fill_id(
            event,
            client_order_id=client_order_id,
            quantity=quantity,
            price=price,
        )
        reduce_only = _event_reduce_only(event)
        if client_order_id.endswith("99"):
            reduce_only = True
        return {
            "fill_id": fill_id,
            "client_order_id": client_order_id,
            "instrument_id": instrument_id or "UNKNOWN",
            "side": side or "UNKNOWN",
            "quantity": quantity or "0",
            "price_usdt": price or "0",
            "fee_usdt": fee,
            "mark_price_usdt": mark_price,
            "reduce_only": reduce_only,
            "occurred_at": _event_occurred_at(event),
            "accounting_error": "; ".join(accounting_errors),
        }

    def process_live_canary_risk_task(
        self,
        task: dict[str, Any],
    ) -> tuple[LiveCanaryLossDecision, ...]:
        kind = str(task.get("kind") or "").strip()
        if kind == "recover":
            return self._live_canary_execution_store.pending_close_decisions()
        if kind == "close_dispatched":
            identity = task.get("identity")
            if not isinstance(identity, LiveCanaryExecutionIdentity):
                raise ValueError(
                    "close_dispatched task requires canary identity"
                )
            self._live_canary_execution_store.mark_close_dispatched(identity)
            return ()
        if kind == "exchange_confirmed":
            self._live_canary_execution_store.mark_exchange_confirmed_by_client_order_id(
                str(task.get("client_order_id") or "")
            )
            return ()
        if kind == "mark":
            raw_mark = task.get("mark")
            if not isinstance(raw_mark, dict):
                raise ValueError(
                    "live canary mark task requires mark payload"
                )
            decision = self._live_canary_execution_store.record_mark(
                LiveCanaryMark(**raw_mark)
            )
            if decision is False:
                return ()
            return (decision,)
        if kind != "fill":
            raise ValueError(f"unsupported live canary risk task: {kind}")
        raw_fill = task.get("fill")
        if not isinstance(raw_fill, dict):
            raise ValueError("live canary fill task requires fill payload")
        decision = self._live_canary_execution_store.record_fill(
            LiveCanaryFill(**raw_fill)
        )
        if decision is False:
            return ()
        return (decision,)

    def _on_live_canary_loss_decision(self, decision: Any) -> None:
        if not isinstance(decision, LiveCanaryLossDecision):
            self._halt_live_canary(
                "live canary risk worker returned an invalid decision"
            )
            return
        expected_identity = (
            str(getattr(self.config, "account_id", "")).strip(),
            str(getattr(self.config, "node_id", "")).strip(),
            str(getattr(self.config, "release_id", "")).strip(),
        )
        actual_identity = (
            decision.identity.account_id,
            decision.identity.node_id,
            decision.identity.release_id,
        )
        if actual_identity != expected_identity:
            self._halt_live_canary(
                "live canary loss decision identity mismatch"
            )
            return
        self._halt_live_canary(decision.halt_reason)
        self._report_live_canary_loss_breach(decision)
        plan = OrderPlan(
            intent_id=UUID(decision.identity.intent_id),
            client_order_id=decision.close_client_order_id,
            tags=(
                f"intent_id={decision.identity.intent_id}",
                f"canary_permit_id={decision.identity.permit_id}",
                "lifecycle_role=live_canary_emergency_close",
            ),
            instrument_id=decision.instrument_id,
            side=decision.close_side,
            order_type="MARKET",
            quantity=decision.close_quantity,
            price=None,
            time_in_force="IOC",
            reduce_only=True,
        )
        submitted = self._live_canary_order_exists(plan)
        if not submitted:
            submitted = self._submit_order_plan(plan)
        if not submitted:
            self._record_denial(
                OrderDenied(
                    "canary_emergency_close_submit_failed",
                    decision.close_client_order_id,
                )
            )
            return
        reporter = self._live_canary_risk_reporter
        if reporter is None:
            return
        reporter(
            {
                "kind": "close_dispatched",
                "identity": decision.identity,
            }
        )

    def _halt_live_canary(self, reason: str) -> None:
        handler = self._live_canary_halt_handler
        if handler is not None:
            handler(reason)
        self._record_denial(
            OrderDenied(
                "canary_loss_limit_halted",
                reason,
            )
        )

    def _report_live_canary_loss_breach(
        self,
        decision: LiveCanaryLossDecision,
    ) -> None:
        reporter = self._protection_event_reporter
        if reporter is None:
            return
        reporter(
            {
                "event_type": "LiveCanaryLossLimitBreached",
                "event_key": decision.identity.permit_id,
                "intent_id": decision.identity.intent_id,
                "client_order_id": decision.close_client_order_id,
                "instrument_id": decision.instrument_id,
                "ts_event": self._now(),
                "payload": {
                    "permit_id": decision.identity.permit_id,
                    "current_loss_usdt": decision.current_loss_usdt,
                    "peak_loss_usdt": decision.peak_loss_usdt,
                    "max_cumulative_loss_usdt": (
                        decision.max_cumulative_loss_usdt
                    ),
                    "close_side": decision.close_side,
                    "close_quantity": decision.close_quantity,
                    "halt_reason": decision.halt_reason,
                },
            }
        )

    def on_order_cancel_rejected(self, event: Any) -> None:
        client_order_id = _event_client_order_id(event)
        instrument_id = _event_instrument_id(event)
        if client_order_id is None:
            return
        for intent_key, stash in self._entry_protection_stash.items():
            if instrument_id is not None and str(stash.get("instrument_id")) != str(instrument_id):
                continue
            pending = tuple(stash.get("pending_cancel_ids") or ())
            if client_order_id not in pending:
                continue
            live_ids = {
                str(getattr(order, "client_order_id", ""))
                for order in self._live_protection_orders(
                    str(stash.get("instrument_id")),
                    intent_key,
                    int(stash.get("protection_sequence_start", 11)),
                )
            }
            if client_order_id not in live_ids:
                self._remove_pending_cancel_id(stash, client_order_id)
                self._queue_entry_protection_stash_persist()
            return

    def _on_protection_order_terminal(self, event: Any, count_retry: bool = True) -> None:
        client_order_id = _event_client_order_id(event)
        if client_order_id is None:
            return
        for stash in self._entry_protection_stash.values():
            if client_order_id in tuple(stash.get("pending_cancel_ids") or ()):
                self._remove_pending_cancel_id(stash, client_order_id)
                self._queue_entry_protection_stash_persist()
                return
        role_match = self._protection_role_for_order(
            client_order_id,
            _event_instrument_id(event),
        )
        if role_match is not None:
            intent_key, stash, role_info = role_match
            self._record_protection_terminal_event(
                event,
                stash,
                client_order_id,
                role_info,
            )
            if self._is_immediate_trigger_mit_rejection(
                event,
                stash,
                client_order_id,
                role_info,
            ):
                self._submit_immediate_tp_market_fallback(
                    event,
                    intent_key,
                    stash,
                    client_order_id,
                    role_info,
                )
                return
            if role_info.get("market_fallback"):
                fallback = stash.get("tp_market_fallbacks")
                if isinstance(fallback, dict):
                    state = fallback.get(client_order_id)
                    if isinstance(state, dict):
                        state["status"] = "failed"
                        state["failed_at"] = self._now().isoformat()
                self._freeze_protection(
                    intent_key,
                    stash,
                    "market_fallback_failed",
                    denial_reason="take_profit_market_fallback_failed",
                )
                self._queue_entry_protection_stash_persist()
                return
            stash["protected_quantity"] = None
            self._queue_entry_protection_stash_persist(
                continuation={
                    "kind": "protection_retry",
                    "intent_key": intent_key,
                    "count_retry": count_retry,
                }
            )
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
        self._record_protection_terminal_event(
            event,
            stash,
            client_order_id,
            self._legacy_protection_role_info(client_order_id, stash),
        )
        if client_order_id in tuple(stash.get("protection_ids") or ()):
            stash["protected_quantity"] = None  # current/adopted revision lost a leg
        self._queue_entry_protection_stash_persist(
            continuation={
                "kind": "protection_retry",
                "intent_key": intent_key,
                "count_retry": count_retry,
            }
        )

    def _record_protection_terminal_event(
        self,
        event: Any,
        stash: dict[str, Any],
        client_order_id: str,
        role_info: Optional[dict[str, Any]],
    ) -> None:
        reason = _event_text_field(event, "reason", "message", "detail", "error")
        error_code = _event_text_field(
            event,
            "error_code",
            "reject_code",
            "code",
        )
        if error_code is None and reason is not None:
            code_match = re.search(r"(?<!\d)-\d{3,5}(?!\d)", reason)
            if code_match is not None:
                error_code = code_match.group(0)
        instrument_id = _event_instrument_id(event)
        if instrument_id is None:
            instrument_id = str(stash.get("instrument_id") or "")
        terminal = {
            "event_type": _event_type_name(event),
            "reason": reason,
            "error_code": error_code,
            "client_order_id": client_order_id,
            "instrument_id": instrument_id,
            "role": role_info.get("role") if isinstance(role_info, dict) else None,
            "tp_price": role_info.get("tp_price") if isinstance(role_info, dict) else None,
            "protection_revision": int(stash.get("protection_revision", -1)),
            "ts_event": _event_text_field(
                event,
                "ts_event",
                "timestamp",
                "event_time",
            ),
            "observed_at": self._now().isoformat(),
        }
        history = stash.get("protection_terminal_events")
        if not isinstance(history, list):
            history = []
        history.append(terminal)
        stash["protection_terminal_events"] = history[
            -self._PROTECTION_TERMINAL_EVENT_LIMIT:
        ]
        stash["last_protection_terminal_event"] = terminal
        log = getattr(self, "log", None)
        if log is not None and hasattr(log, "error"):
            log.error(
                "ProtectionOrderTerminal "
                + json.dumps(terminal, sort_keys=True, separators=(",", ":"))
            )

    def _is_immediate_trigger_mit_rejection(
        self,
        event: Any,
        stash: dict[str, Any],
        client_order_id: str,
        role_info: dict[str, Any],
    ) -> bool:
        if role_info.get("role") != "take_profit":
            return False
        order_type = str(role_info.get("order_type") or "")
        if not order_type:
            order_type = _event_order_type(event) or ""
        if not order_type:
            instrument_id = _event_instrument_id(event)
            if instrument_id is None:
                instrument_id = str(stash.get("instrument_id") or "")
            for order in self._cache_orders_all(instrument_id):
                if str(getattr(order, "client_order_id", "")) != client_order_id:
                    continue
                order_type = _enum_name(getattr(order, "order_type", ""))
                break
        if _enum_name(order_type) != "MARKET_IF_TOUCHED":
            return False
        reason = _event_text_field(event, "reason", "message", "detail", "error")
        error_code = _event_text_field(
            event,
            "error_code",
            "reject_code",
            "code",
        )
        text = " ".join(
            value for value in (error_code, reason) if value
        ).lower()
        return "-2021" in text or "would immediately trigger" in text

    def _submit_immediate_tp_market_fallback(
        self,
        event: Any,
        intent_key: str,
        stash: dict[str, Any],
        source_client_order_id: str,
        role_info: dict[str, Any],
    ) -> bool:
        fallback_id = _market_fallback_client_order_id(source_client_order_id)
        fallbacks = stash.setdefault("tp_market_fallbacks", {})
        existing = fallbacks.get(fallback_id)
        if isinstance(existing, dict):
            return True
        quantity = str(
            role_info.get("quantity")
            or _event_last_qty(event)
            or ""
        )
        try:
            valid_quantity = Decimal(quantity) > 0
        except (InvalidOperation, ValueError):
            valid_quantity = False
        if not valid_quantity:
            self._freeze_protection(
                intent_key,
                stash,
                "market_fallback_invalid_quantity",
                denial_reason="take_profit_market_fallback_invalid_quantity",
            )
            return False
        tags = self._tp_market_fallback_tags(
            event,
            stash,
            role_info,
        )
        side = str(role_info.get("side") or _event_order_side(event) or "")
        if side not in {"BUY", "SELL"}:
            side = "SELL" if str(stash.get("entry_side")) == "BUY" else "BUY"
        tp_price = str(role_info.get("tp_price") or "")
        event_key = f"{intent_key}:{source_client_order_id}:{fallback_id}"
        state = {
            "source_client_order_id": source_client_order_id,
            "client_order_id": fallback_id,
            "tp_price": tp_price,
            "quantity": quantity,
            "remaining_quantity": quantity,
            "side": side,
            "tags": tags,
            "status": "submitting",
            "created_at": self._now().isoformat(),
            "event_key": event_key,
            "event_sent": False,
        }
        fallbacks[fallback_id] = state
        plan = OrderPlan(
            intent_id=UUID(intent_key),
            client_order_id=fallback_id,
            tags=tags,
            instrument_id=str(stash.get("instrument_id") or ""),
            side=side,
            order_type="MARKET",
            quantity=quantity,
            price=None,
            time_in_force="GTC",
            reduce_only=True,
        )
        queued = self._queue_entry_protection_stash_persist(
            continuation={
                "kind": "immediate_tp_market_fallback",
                "intent_key": intent_key,
                "source_client_order_id": source_client_order_id,
                "fallback_id": fallback_id,
                "tp_price": tp_price,
                "event": event,
                "plan": plan,
            }
        )
        if not queued:
            self._freeze_protection(
                intent_key,
                stash,
                "market_fallback_state_persist_failed",
                denial_reason="take_profit_market_fallback_state_persist_failed",
            )
            return False
        return True

    def _continue_immediate_tp_market_fallback(
        self,
        continuation: Mapping[str, Any],
    ) -> None:
        intent_key = str(continuation.get("intent_key") or "")
        fallback_id = str(continuation.get("fallback_id") or "")
        source_client_order_id = str(
            continuation.get("source_client_order_id") or ""
        )
        tp_price = str(continuation.get("tp_price") or "")
        plan = continuation.get("plan")
        if not intent_key or not fallback_id:
            return
        if not isinstance(plan, OrderPlan):
            self._halt_durable_io(
                "immediate TP fallback continuation missing plan"
            )
            return
        stash = self._entry_protection_stash.get(intent_key)
        if not isinstance(stash, dict):
            return
        fallbacks = stash.get("tp_market_fallbacks")
        if not isinstance(fallbacks, dict):
            return
        state = fallbacks.get(fallback_id)
        if not isinstance(state, dict):
            return
        if not self._submit_order_plan(plan):
            state["status"] = "failed"
            state["failed_at"] = self._now().isoformat()
            self._freeze_protection(
                intent_key,
                stash,
                "market_fallback_submit_failed",
                denial_reason="take_profit_market_fallback_submit_failed",
            )
            self._queue_entry_protection_stash_persist()
            return
        state["status"] = "submitted"
        state["submitted_at"] = self._now().isoformat()
        protection_ids = tuple(
            cid
            for cid in tuple(stash.get("protection_ids") or ())
            if str(cid) != source_client_order_id
        )
        stash["protection_ids"] = protection_ids + (fallback_id,)
        self._register_protection_role(
            intent_key,
            stash,
            fallback_id,
            plan,
            tp_price=tp_price,
            market_fallback=True,
            source_client_order_id=source_client_order_id,
        )
        terminal = stash.get("last_protection_terminal_event")
        quantity = str(state.get("quantity") or "")
        event_key = str(state.get("event_key") or "")
        payload = {
            "source_client_order_id": source_client_order_id,
            "fallback_client_order_id": fallback_id,
            "quantity": quantity,
            "tp_price": tp_price,
            "reason": (
                terminal.get("reason")
                if isinstance(terminal, dict)
                else None
            ),
            "error_code": (
                terminal.get("error_code")
                if isinstance(terminal, dict)
                else None
            ),
            "reduce_only": True,
        }
        state["event_sent"] = self._report_protection_event(
            intent_key,
            stash,
            event_type="TakeProfitImmediateMarketFallback",
            event_key=event_key,
            client_order_id=fallback_id,
            payload=payload,
        )
        self._queue_entry_protection_stash_persist()

    def _tp_market_fallback_tags(
        self,
        event: Any,
        stash: dict[str, Any],
        role_info: dict[str, Any],
    ) -> tuple[str, ...]:
        for holder in (event, getattr(event, "order", None)):
            if holder is None:
                continue
            tags = tuple(str(tag) for tag in (getattr(holder, "tags", ()) or ()))
            if tags:
                return tags
        saved_tags = role_info.get("tags")
        if isinstance(saved_tags, (list, tuple)) and saved_tags:
            return tuple(str(tag) for tag in saved_tags)
        authorization = stash.get("take_profit_authorization")
        parent_intent_id = str(stash.get("take_profit_parent_intent_id") or "")
        position_id = _tag_value(stash.get("entry_tags") or (), "position_id")
        return _protection_tags(
            tuple(stash.get("entry_tags") or ()),
            "take_profit",
            position_id,
            parent_intent_id=parent_intent_id,
            authorization=authorization,
        )

    def _retry_pending_tp_market_fallback_events(
        self,
        intent_key: str,
        stash: dict[str, Any],
    ) -> None:
        fallbacks = stash.get("tp_market_fallbacks")
        if not isinstance(fallbacks, dict):
            return
        for fallback_id, state in fallbacks.items():
            if not isinstance(state, dict) or state.get("event_sent"):
                continue
            if state.get("status") not in {"submitted", "filled"}:
                continue
            event_key = str(
                state.get("event_key")
                or f"{intent_key}:{state.get('source_client_order_id')}:{fallback_id}"
            )
            payload = {
                "source_client_order_id": state.get("source_client_order_id"),
                "fallback_client_order_id": fallback_id,
                "quantity": state.get("quantity"),
                "tp_price": state.get("tp_price"),
                "reduce_only": True,
            }
            state["event_sent"] = self._report_protection_event(
                intent_key,
                stash,
                event_type="TakeProfitImmediateMarketFallback",
                event_key=event_key,
                client_order_id=str(fallback_id),
                payload=payload,
            )

    def _consume_tp_market_fallback(
        self,
        stash: dict[str, Any],
        client_order_id: str,
        quantity: str,
    ) -> None:
        fallbacks = stash.get("tp_market_fallbacks")
        if not isinstance(fallbacks, dict):
            return
        state = fallbacks.get(client_order_id)
        if not isinstance(state, dict):
            return
        try:
            remaining = Decimal(str(state.get("remaining_quantity") or "0"))
            remaining -= Decimal(str(quantity))
        except (InvalidOperation, ValueError):
            remaining = Decimal("0")
        if remaining <= 0:
            state["remaining_quantity"] = "0"
            state["status"] = "filled"
            state["filled_at"] = self._now().isoformat()
            return
        state["remaining_quantity"] = format(remaining, "f")

    def _pending_tp_market_fallback_quantity(
        self,
        stash: dict[str, Any],
        tp_price: str,
    ) -> Decimal:
        fallbacks = stash.get("tp_market_fallbacks")
        if not isinstance(fallbacks, dict):
            return Decimal("0")
        total = Decimal("0")
        for state in fallbacks.values():
            if not isinstance(state, dict):
                continue
            if str(state.get("tp_price") or "") != tp_price:
                continue
            if state.get("status") not in {"submitting", "submitted"}:
                continue
            try:
                total += Decimal(str(state.get("remaining_quantity") or "0"))
            except (InvalidOperation, ValueError):
                continue
        return total

    def _has_pending_tp_market_fallback(self, stash: dict[str, Any]) -> bool:
        fallbacks = stash.get("tp_market_fallbacks")
        if not isinstance(fallbacks, dict):
            return False
        return any(
            isinstance(state, dict)
            and state.get("status") in {"submitting", "submitted"}
            and str(state.get("remaining_quantity") or "0") != "0"
            for state in fallbacks.values()
        )

    def _on_protection_sync_alert(self, event: Any) -> None:
        name = str(getattr(event, "name", ""))
        if name.startswith(self._PROTECTION_TIMER_PREFIX):
            self._sync_protection(name[len(self._PROTECTION_TIMER_PREFIX):])

    def _schedule_protection_sync(self, intent_key: str, delay_seconds: Optional[float] = None) -> None:
        stash = self._entry_protection_stash.get(intent_key)
        if stash is None:
            return
        if stash.get("protection_frozen"):
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
        self._normalize_protection_stash(intent_key, stash)
        if stash.get("protection_frozen"):
            self._queue_entry_protection_stash_persist()
            return
        if not self._has_authorized_protection_parent(intent_key, stash):
            self._queue_entry_protection_stash_persist()
            return
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
            self._queue_entry_protection_stash_persist()
            self._cancel_clock_timer(self._PROTECTION_TIMER_PREFIX + intent_key)
            return

        if self._has_pending_tp_market_fallback(stash):
            self._reschedule_protection_sync(intent_key, stash, count_retry=True)
            return

        quantity = _round_down_positive(
            _position_quantity(position),
            instrument.quantity_increment,
        )
        if quantity is None:
            self._reschedule_protection_sync(intent_key, stash, count_retry=True)
            return

        if inflight:
            self._reschedule_protection_sync(intent_key, stash, count_retry=True)
            return

        self._retry_pending_protection_cancels(intent_key, stash, live)

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
            for client_order_id, plan in zip(adopted_ids, plans_for_adoption):
                self._register_protection_role(
                    intent_key,
                    stash,
                    client_order_id,
                    plan,
                )
            stash.pop("sync_retries", None)
            stash.pop("pending_protection_revision", None)
            self._queue_entry_protection_stash_persist()
            return

        desired = plans_for_adoption
        actions, keep_ids, replace_ids = self._protection_replacement_actions(
            stash,
            live,
            desired,
            instrument,
        )
        if not actions:
            keep = set(keep_ids)
            for order in live:
                client_order_id = str(getattr(order, "client_order_id", ""))
                if client_order_id in keep or client_order_id not in replace_ids:
                    continue
                self._cancel_replaced_protection_order(intent_key, stash, order)
            stash["protection_ids"] = tuple(keep_ids)
            stash["protected_quantity"] = quantity
            stash.pop("sync_retries", None)
            stash.pop("pending_protection_revision", None)
            self._prune_protection_roles(intent_key, stash)
            self._queue_entry_protection_stash_persist()
            return

        revision = int(stash.get("protection_revision", -1)) + 1
        if revision > self._PROTECTION_MAX_REVISION:
            self._freeze_protection(
                intent_key,
                stash,
                "revisions_exhausted",
                denial_reason="protection_revisions_exhausted",
            )
            self._queue_entry_protection_stash_persist()
            return

        revision_plans = self._protection_order_plans(
            UUID(intent_key),
            stash,
            instrument,
            position,
            quantity,
            revision=revision,
        )
        action_keys = {_protection_plan_key(plan) for plan in actions}
        plans = tuple(
            plan for plan in revision_plans
            if _protection_plan_key(plan) in action_keys
        )
        if not plans:
            return

        stash["protection_revision"] = revision
        stash["pending_protection_revision"] = {
            "revision": revision,
            "client_order_ids": tuple(
                plan.client_order_id for plan in plans
            ),
            "quantity": quantity,
            "created_at": self._now().isoformat(),
        }
        self._queue_entry_protection_stash_persist(
            continuation={
                "kind": "protection_submit_revision",
                "intent_key": intent_key,
                "plans": plans,
                "live_orders": live,
                "keep_ids": keep_ids,
                "replace_ids": tuple(replace_ids),
                "quantity": quantity,
            }
        )

    def _continue_protection_revision_submit(
        self,
        continuation: Mapping[str, Any],
    ) -> None:
        intent_key = str(continuation.get("intent_key") or "")
        stash = self._entry_protection_stash.get(intent_key)
        if not isinstance(stash, dict):
            return
        plans = continuation.get("plans")
        if not isinstance(plans, tuple):
            self._halt_durable_io(
                "protection revision continuation missing plans"
            )
            return
        pending = stash.get("pending_protection_revision")
        if not isinstance(pending, dict):
            return
        expected_ids = tuple(
            str(value)
            for value in pending.get("client_order_ids", ())
        )
        plan_ids = tuple(plan.client_order_id for plan in plans)
        if expected_ids != plan_ids:
            return

        submitted_ids: list[str] = []
        for plan in plans:
            if self._submit_order_plan(plan):
                submitted_ids.append(plan.client_order_id)
                self._register_protection_role(intent_key, stash, plan.client_order_id, plan)
        if not submitted_ids:
            stash["protected_quantity"] = None
            stash.pop("pending_protection_revision", None)
            self._reschedule_protection_sync(intent_key, stash, count_retry=True)
            return
        keep_ids = tuple(
            str(value)
            for value in continuation.get("keep_ids", ())
        )
        replace_ids = {
            str(value)
            for value in continuation.get("replace_ids", ())
        }
        live = tuple(continuation.get("live_orders", ()))
        keep = set(keep_ids) | set(submitted_ids)
        for order in live:
            oid = str(getattr(order, "client_order_id", ""))
            if oid in keep or oid not in replace_ids:
                continue
            self._cancel_replaced_protection_order(intent_key, stash, order)
        stash["protection_ids"] = tuple(
            cid for cid in tuple(keep_ids) + tuple(submitted_ids)
            if cid
        )
        if len(submitted_ids) == len(plans):
            stash["protected_quantity"] = str(
                continuation.get("quantity") or ""
            )
        else:
            stash["protected_quantity"] = None
        stash.pop("pending_protection_revision", None)
        self._prune_protection_roles(intent_key, stash)
        if len(submitted_ids) != len(plans):
            self._reschedule_protection_sync(intent_key, stash, count_retry=True)
            return
        self._queue_entry_protection_stash_persist()

    def _has_authorized_protection_parent(
        self,
        intent_key: str,
        stash: dict[str, Any],
    ) -> bool:
        required: list[tuple[str, str, str]] = []
        if stash.get("stop_loss") is not None:
            required.append(
                (
                    "stop_loss",
                    "stop_loss_parent_intent_id",
                    "stop_loss_authorization",
                )
            )
        tombstone = stash.get("take_profit_tombstone")
        if tombstone is not None and not _valid_take_profit_tombstone(
            tombstone,
            stash.get("take_profit_parent_intent_id"),
        ):
            key = ("take_profit_tombstone_invalid", intent_key)
            if key not in self._reported_protection_denials:
                self._reported_protection_denials.add(key)
                self._record_denial(
                    OrderDenied("take_profit_tombstone_invalid", intent_key)
                )
            return False
        if _valid_take_profit_tombstone(
            tombstone,
            stash.get("take_profit_parent_intent_id"),
        ) or _take_profit_prices(
            stash.get("take_profits")
        ):
            required.append(
                (
                    "take_profit",
                    "take_profit_parent_intent_id",
                    "take_profit_authorization",
                )
            )
        for role, parent_field, authorization_field in required:
            parent_intent_id = str(
                stash.get(parent_field) or ""
            )
            authorization = stash.get(authorization_field)
            if (
                _valid_uuid_text(parent_intent_id)
                and _authorization_matches_parent(
                    authorization,
                    parent_intent_id,
                )
            ):
                continue
            key = ("protection_parent_intent_missing", f"{intent_key}:{role}")
            if key not in self._reported_protection_denials:
                self._reported_protection_denials.add(key)
                self._record_denial(
                    OrderDenied(
                        "protection_parent_intent_missing",
                        f"{intent_key}:{role}",
                    )
                )
            return False
        return True

    def _protection_replacement_actions(
        self,
        stash: dict[str, Any],
        live: tuple[Any, ...],
        desired: tuple[OrderPlan, ...],
        instrument: InstrumentSpec,
    ) -> tuple[tuple[OrderPlan, ...], tuple[str, ...], set[str]]:
        desired_stop = next((plan for plan in desired if self._protection_plan_role(plan) == "stop_loss"), None)
        desired_tps = [
            plan for plan in desired
            if (
                self._protection_plan_role(plan) == "take_profit"
                and plan.trigger_price is not None
            )
        ]
        live_stops = [
            order for order in live
            if self._protection_order_role(stash, order) == "stop_loss"
        ]
        live_tps = [
            order for order in live
            if self._protection_order_role(stash, order) == "take_profit"
        ]

        actions: list[OrderPlan] = []
        keep_ids: list[str] = []
        replace_ids: set[str] = set()
        if desired_stop is not None:
            healthy_stops = [
                order for order in live_stops
                if self._live_order_matches_plan(order, desired_stop, instrument)
            ]
            if healthy_stops:
                keep_ids.extend(str(getattr(order, "client_order_id", "")) for order in healthy_stops)
                replace_ids.update(
                    str(getattr(order, "client_order_id", ""))
                    for order in live_stops
                    if order not in healthy_stops
                )
            else:
                actions.append(desired_stop)
                replace_ids.update(str(getattr(order, "client_order_id", "")) for order in live_stops)

        desired_triggers = {str(plan.trigger_price) for plan in desired_tps}
        for plan in desired_tps:
            same_trigger = [
                order for order in live_tps
                if (
                    self._protection_order_trigger_price(order, instrument)
                    == plan.trigger_price
                )
            ]
            healthy = [
                order for order in same_trigger
                if self._live_order_matches_plan(order, plan, instrument)
            ]
            live_quantity = sum(
                (
                    Decimal(str(qty))
                    for qty in (
                        self._protection_order_quantity(order, instrument)
                        for order in healthy
                    )
                    if qty is not None
                ),
                Decimal("0"),
            )
            target = Decimal(str(plan.quantity))
            increment = Decimal(str(instrument.quantity_increment))
            if not healthy or live_quantity < target - increment:
                actions.append(plan)
                replace_ids.update(
                    str(getattr(order, "client_order_id", ""))
                    for order in same_trigger
                )
            else:
                keep_ids.extend(
                    str(getattr(order, "client_order_id", ""))
                    for order in healthy
                )
                healthy_ids = {
                    str(getattr(order, "client_order_id", ""))
                    for order in healthy
                }
                replace_ids.update(
                    str(getattr(order, "client_order_id", ""))
                    for order in same_trigger
                    if str(getattr(order, "client_order_id", "")) not in healthy_ids
                )
        replace_ids.update(
            str(getattr(order, "client_order_id", ""))
            for order in live_tps
            if (
                self._protection_order_trigger_price(order, instrument)
                not in desired_triggers
            )
        )
        pending = set(str(cid) for cid in tuple(stash.get("pending_cancel_ids") or ()))
        keep_ids = [cid for cid in keep_ids if cid and cid not in pending]
        replace_ids = {cid for cid in replace_ids if cid and cid not in keep_ids}
        return tuple(actions), tuple(dict.fromkeys(keep_ids)), replace_ids

    def _retry_pending_protection_cancels(
        self,
        intent_key: str,
        stash: dict[str, Any],
        live: tuple[Any, ...],
    ) -> None:
        live_by_id = {str(getattr(order, "client_order_id", "")): order for order in live}
        for client_order_id in tuple(stash.get("pending_cancel_ids") or ()):
            order = live_by_id.get(str(client_order_id))
            if order is None:
                self._remove_pending_cancel_id(stash, str(client_order_id))
                continue
            self._attempt_pending_cancel(intent_key, order)

    def _cancel_replaced_protection_order(
        self,
        intent_key: str,
        stash: dict[str, Any],
        order: Any,
    ) -> None:
        client_order_id = str(getattr(order, "client_order_id", ""))
        if not client_order_id:
            return
        pending = tuple(stash.get("pending_cancel_ids") or ())
        if client_order_id in pending:
            return
        stash["pending_cancel_ids"] = pending + (client_order_id,)
        self._attempt_pending_cancel(intent_key, order)

    def _attempt_pending_cancel(self, intent_key: str, order: Any) -> None:
        client_order_id = str(getattr(order, "client_order_id", ""))
        attempts = self._orphan_cancel_attempts.get(client_order_id, 0) + 1
        self._orphan_cancel_attempts[client_order_id] = attempts
        if attempts > 5:
            key = ("protection_orphan_order", client_order_id)
            if key not in self._reported_protection_denials:
                self._reported_protection_denials.add(key)
                self._record_denial(OrderDenied("protection_orphan_order", client_order_id))
            return
        self._cancel_order_object(order)

    def _remove_pending_cancel_id(self, stash: dict[str, Any], client_order_id: str) -> None:
        stash["pending_cancel_ids"] = tuple(
            cid for cid in tuple(stash.get("pending_cancel_ids") or ())
            if str(cid) != client_order_id
        )

    def _register_protection_role(
        self,
        intent_key: str,
        stash: dict[str, Any],
        client_order_id: str,
        plan: OrderPlan,
        *,
        tp_price: Optional[str] = None,
        market_fallback: bool = False,
        source_client_order_id: Optional[str] = None,
    ) -> None:
        if not client_order_id:
            return
        role = self._protection_plan_role(plan)
        if role is None:
            return
        roles = stash.setdefault("protection_roles", {})
        role_tp_price = plan.trigger_price
        if tp_price is not None:
            role_tp_price = tp_price
        roles[client_order_id] = {
            "role": role,
            "tp_price": (
                str(role_tp_price)
                if role == "take_profit" and role_tp_price is not None
                else None
            ),
            "quantity": str(plan.quantity),
            "submitted_at": self._now().isoformat(),
            "order_type": plan.order_type,
            "side": plan.side,
            "tags": tuple(plan.tags),
            "market_fallback": market_fallback,
            "source_client_order_id": source_client_order_id,
        }
        self._prune_protection_roles(intent_key, stash)

    def _prune_protection_roles(self, intent_key: str, stash: dict[str, Any]) -> None:
        roles = stash.get("protection_roles")
        if not isinstance(roles, dict):
            stash["protection_roles"] = {}
            return
        try:
            current_revision = int(stash.get("protection_revision", -1))
        except (TypeError, ValueError):
            current_revision = -1
        min_revision = current_revision - 1
        protected = set(str(cid) for cid in tuple(stash.get("protection_ids") or ()))
        pending = set(str(cid) for cid in tuple(stash.get("pending_cancel_ids") or ()))
        kept: dict[str, Any] = {}
        for client_order_id, role_info in roles.items():
            cid = str(client_order_id)
            keep = cid in protected or cid in pending
            try:
                trace = decode_client_order_id(cid)
                if str(trace.intent_id) != intent_key:
                    keep = True
                else:
                    revision = (trace.sequence // 10) - 1
                    keep = keep or revision >= min_revision
            except ValueError:
                keep = True
            if keep:
                kept[cid] = role_info
        stash["protection_roles"] = kept

    def _protection_plan_role(self, plan: OrderPlan) -> Optional[str]:
        tags = {str(tag) for tag in (plan.tags or ())}
        if "lifecycle_role=stop_loss" in tags:
            return "stop_loss"
        if "lifecycle_role=take_profit" in tags:
            return "take_profit"
        if plan.order_type == "STOP_MARKET":
            return "stop_loss"
        if plan.order_type in {"LIMIT", "LIMIT_IF_TOUCHED", "MARKET_IF_TOUCHED"}:
            return "take_profit"
        return None

    def _protection_order_role(self, stash: dict[str, Any], order: Any) -> Optional[str]:
        client_order_id = str(getattr(order, "client_order_id", ""))
        roles = stash.get("protection_roles")
        if isinstance(roles, dict):
            role_info = roles.get(client_order_id)
            if isinstance(role_info, dict) and role_info.get("role") is not None:
                return str(role_info.get("role"))
        tags = {str(tag) for tag in (getattr(order, "tags", ()) or ())}
        if "lifecycle_role=stop_loss" in tags:
            return "stop_loss"
        if "lifecycle_role=take_profit" in tags:
            return "take_profit"
        order_type = _enum_name(getattr(order, "order_type", ""))
        if "STOP" in order_type:
            return "stop_loss"
        if order_type == "MARKET_IF_TOUCHED":
            return "take_profit"
        if "LIMIT" in order_type:
            return "take_profit"
        return None

    def _protection_order_trigger_price(
        self,
        order: Any,
        instrument: InstrumentSpec,
    ) -> Optional[str]:
        raw_trigger = getattr(order, "trigger_price", None)
        if raw_trigger is None:
            raw_trigger = getattr(order, "stop_price", None)
        if raw_trigger is None:
            raw_trigger = getattr(order, "price", None)
        return _round_down_positive(
            _optional_str(raw_trigger),
            instrument.price_increment,
        )

    def _protection_order_quantity(self, order: Any, instrument: InstrumentSpec) -> Optional[str]:
        return _round_down_positive(
            _optional_str(getattr(order, "quantity", getattr(order, "qty", None))),
            instrument.quantity_increment,
        )

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
        if plan.order_type == "MARKET_IF_TOUCHED":
            if order_type != "MARKET_IF_TOUCHED":
                return False
            order_trigger = self._protection_order_trigger_price(order, instrument)
            if order_trigger != plan.trigger_price:
                return False
            return _decimal_abs_lte(
                _optional_str(getattr(order, "quantity", getattr(order, "qty", None))),
                plan.quantity,
                instrument.quantity_increment,
            )
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
                self._queue_entry_protection_stash_persist()
                return
            delay_seconds = min(
                self._PROTECTION_SYNC_DELAY_S * (2 ** (retries - 1)),
                60.0,
            )
        continuation: dict[str, Any] = {
            "kind": "protection_schedule_delay",
            "intent_key": intent_key,
        }
        if delay_seconds is not None:
            continuation["delay_seconds"] = delay_seconds
        self._queue_entry_protection_stash_persist(
            continuation=continuation
        )

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
        stop_parent_intent_id = str(stash.get("stop_loss_parent_intent_id") or "")
        stop_authorization = stash.get("stop_loss_authorization")
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
                        tags=_protection_tags(
                            base_tags,
                            "stop_loss",
                            position_id,
                            parent_intent_id=stop_parent_intent_id,
                            authorization=stop_authorization,
                        ),
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

        tombstone = stash.get("take_profit_tombstone")
        targets: tuple[Any, ...] = ()
        if not _valid_take_profit_tombstone(
            tombstone,
            stash.get("take_profit_parent_intent_id"),
        ):
            targets = _take_profit_prices(stash.get("take_profits"))[:8]
        take_profit_parent_intent_id = str(
            stash.get("take_profit_parent_intent_id") or ""
        )
        take_profit_authorization = stash.get("take_profit_authorization")
        quantities = self._take_profit_remaining_quantities(
            stash,
            targets,
            quantity,
            instrument.quantity_increment,
        )
        for index, (target, target_quantity) in enumerate(zip(targets, quantities), start=1):
            if target_quantity is None:
                continue
            trigger_price = _rounded_positive(
                target,
                instrument.price_increment,
                f"take_profits[{index - 1}].price",
            )
            if isinstance(trigger_price, OrderDenied):
                self._record_denial(trigger_price)
                continue
            plans.append(
                OrderPlan(
                    intent_id=intent_id,
                    client_order_id=encode_client_order_id(
                        intent_id,
                        sequence=block + 1 + index,
                    ),
                    tags=_protection_tags(
                        base_tags,
                        "take_profit",
                        position_id,
                        index=index,
                        parent_intent_id=take_profit_parent_intent_id,
                        authorization=take_profit_authorization,
                    ),
                    instrument_id=instrument_id,
                    side=exit_side,
                    order_type="MARKET_IF_TOUCHED",
                    quantity=target_quantity,
                    price=None,
                    time_in_force="GTC",
                    reduce_only=True,
                    trigger_price=trigger_price,
                )
            )
        return tuple(plans)

    def _take_profit_remaining_quantities(
        self,
        stash: dict[str, Any],
        targets: tuple[Any, ...],
        quantity: str,
        increment: str,
    ) -> tuple[Optional[str], ...]:
        consumed = stash.get("tp_consumed")
        consumed_by_price = consumed if isinstance(consumed, dict) else {}
        absorbed = stash.get("take_profit_quantities")
        if isinstance(absorbed, (list, tuple)) and len(absorbed) == len(targets):
            result: list[Optional[str]] = []
            for target, target_quantity in zip(targets, absorbed):
                price_key = _price_key(target)
                try:
                    remaining = Decimal(str(target_quantity)) - Decimal(
                        str(consumed_by_price.get(price_key, "0"))
                    )
                    remaining -= self._pending_tp_market_fallback_quantity(
                        stash,
                        price_key,
                    )
                except (InvalidOperation, ValueError, TypeError):
                    result.append(None)
                    continue
                result.append(_round_down_positive(remaining, increment))
            return tuple(result)

        unconsumed_indexes: list[int] = []
        for index, target in enumerate(targets):
            try:
                consumed_quantity = Decimal(str(consumed_by_price.get(_price_key(target), "0")))
            except (InvalidOperation, ValueError, TypeError):
                consumed_quantity = Decimal("0")
            consumed_quantity += self._pending_tp_market_fallback_quantity(
                stash,
                _price_key(target),
            )
            if consumed_quantity <= 0:
                unconsumed_indexes.append(index)
        split = _split_take_profit_quantities(quantity, len(unconsumed_indexes), increment)
        result = [None for _ in targets]
        for index, target_quantity in zip(unconsumed_indexes, split):
            result[index] = target_quantity
        return tuple(result)

    def _trading_state(self) -> str:
        if self._durable_io_halted_reason:
            return "HALTED"
        if self._trading_state_getter is not None:
            raw_state = self._trading_state_getter()
        else:
            raw_state = self.config.trading_state
        return str(getattr(raw_state, "value", raw_state))

    def _now(self) -> datetime:
        clock = getattr(self, "clock", None)
        if clock is not None and hasattr(clock, "utc_now"):
            try:
                return clock.utc_now()
            except NotImplementedError:
                pass
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

    def _order_snapshots(
        self,
        instrument_id: str,
        *,
        include_exchange_mirror: bool = False,
    ) -> tuple[OrderSnapshot, ...]:
        snapshots: dict[str, OrderSnapshot] = {}
        for order in self._cache_orders(instrument_id):
            client_order_id = getattr(order, "client_order_id", None)
            if client_order_id is None:
                continue
            snapshot = OrderSnapshot(
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
            snapshots[snapshot.client_order_id] = snapshot
        mirror = self._exchange_state_mirror if include_exchange_mirror else False
        mirror_orders: Iterable[Any] = ()
        if mirror:
            orders_for_instrument = getattr(mirror, "orders_for_instrument", None)
            if callable(orders_for_instrument):
                mirror_orders = orders_for_instrument(instrument_id)
        for order in mirror_orders:
            client_order_id = str(getattr(order, "client_order_id", ""))
            if not client_order_id or client_order_id in snapshots:
                continue
            snapshots[client_order_id] = OrderSnapshot(
                client_order_id=client_order_id,
                instrument_id=str(getattr(order, "instrument_id", instrument_id)),
                order_type=_enum_name(getattr(order, "order_type", "")),
                side=_enum_name(getattr(order, "side", "")),
                quantity=str(getattr(order, "quantity", "")),
                price=_optional_str(getattr(order, "price", None)),
                trigger_price=_optional_str(getattr(order, "trigger_price", None)),
                tags=tuple(str(tag) for tag in (getattr(order, "tags", ()) or ())),
            )
        return tuple(snapshots.values())

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

    def _submit_order_plan_after_durable_prepare(
        self,
        plan: OrderPlan,
        *,
        live_canary_execution: LiveCanaryExecutionIdentity | bool,
    ) -> bool:
        requires_live_canary = (
            str(getattr(self.config, "account_id", "")) == "account-a"
            and str(getattr(self.config, "environment", "")).lower()
            == "live"
            and not plan.reduce_only
        )
        if requires_live_canary and not isinstance(
            live_canary_execution,
            LiveCanaryExecutionIdentity,
        ):
            self._record_denial(
                OrderDenied(
                    "canary_execution_context_missing",
                    str(plan.intent_id),
                )
            )
            return False
        if isinstance(
            live_canary_execution,
            LiveCanaryExecutionIdentity,
        ):
            canary_denial = self._live_canary_plan_denial(
                plan,
                live_canary_execution,
            )
            if canary_denial is not None:
                self._record_denial(canary_denial)
                return False

        instrument = self._cache_instrument(plan.instrument_id)
        if instrument is None:
            self._record_denial(
                OrderDenied(
                    "instrument_not_found",
                    plan.instrument_id,
                )
            )
            return False
        try:
            order = self._build_nautilus_order(
                self._plan_for_submission(plan),
                instrument,
            )
        except Exception as exc:
            self._record_denial(
                OrderDenied("order_submit_failed", repr(exc))
            )
            return False
        entry_denial = self._live_entry_notional_denial(plan, order)
        if entry_denial is not None:
            self._record_denial(entry_denial)
            return False

        if isinstance(
            live_canary_execution,
            LiveCanaryExecutionIdentity,
        ):
            client_order_id = (
                live_canary_execution.client_order_id
            )
            self._live_canary_monitor_targets[
                client_order_id
            ] = plan.instrument_id
            self._live_canary_monitor_baselines[
                client_order_id
            ] = (
                live_canary_execution.symbol,
                live_canary_execution.portfolio_baseline_sha256,
            )
        try:
            position_id = self._hedge_position_id(order, plan)
            if position_id is not None:
                self.submit_order(  # type: ignore[attr-defined]
                    order,
                    position_id=position_id,
                )
            else:
                self.submit_order(order)  # type: ignore[attr-defined]
        except Exception as exc:
            self._record_denial(
                OrderDenied("order_submit_failed", repr(exc))
            )
            return False
        return True

    def _submit_order_plan(
        self,
        plan: OrderPlan,
        *,
        live_canary_execution: LiveCanaryExecutionIdentity | bool = False,
        intent_execution: IntentExecutionIdentity | bool = False,
    ) -> bool:
        requires_live_canary = (
            str(getattr(self.config, "account_id", "")) == "account-a"
            and str(getattr(self.config, "environment", "")).lower()
            == "live"
            and not plan.reduce_only
        )
        if requires_live_canary and not isinstance(
            live_canary_execution,
            LiveCanaryExecutionIdentity,
        ):
            self._record_denial(
                OrderDenied(
                    "canary_execution_context_missing",
                    str(plan.intent_id),
                )
            )
            return False
        if isinstance(
            live_canary_execution,
            LiveCanaryExecutionIdentity,
        ):
            canary_denial = self._live_canary_plan_denial(
                plan,
                live_canary_execution,
            )
            if canary_denial is not None:
                self._record_denial(canary_denial)
                return False

        instrument = self._cache_instrument(plan.instrument_id)
        if instrument is None:
            self._record_denial(OrderDenied("instrument_not_found", plan.instrument_id))
            return False

        try:
            # NOTE: order kwargs may drop the internal reduce_only flag (external
            # position quirk, see _plan_for_submission) but the live-entry gate
            # always uses the ORIGINAL plan semantics.
            order = self._build_nautilus_order(
                self._plan_for_submission(plan),
                instrument,
            )
        except Exception as exc:
            self._record_denial(OrderDenied("order_submit_failed", repr(exc)))
            return False

        entry_denial = self._live_entry_notional_denial(plan, order)
        if entry_denial is not None:
            self._record_denial(entry_denial)
            return False

        if isinstance(intent_execution, IntentExecutionIdentity):
            try:
                dispatch_result = (
                    self._intent_execution_inbox.begin_dispatch(
                        intent_execution,
                        (str(plan.client_order_id),),
                    )
                )
            except RuntimeError as exc:
                self._record_denial(
                    OrderDenied(
                        "intent_dispatch_persist_failed",
                        repr(exc),
                    )
                )
                return False
            if (
                dispatch_result
                is IntentDispatchResult.EXCHANGE_CONFIRMED
            ):
                return True
            if dispatch_result is IntentDispatchResult.REJECTED:
                self._record_denial(
                    OrderDenied(
                        "durable_intent_rejected",
                        intent_execution.intent_id,
                    )
                )
                return False
            if (
                dispatch_result
                is IntentDispatchResult.RECOVERY_REQUIRED
            ):
                record = self._intent_execution_inbox.get(
                    intent_execution
                )
                if (
                    record is not False
                    and self._durable_intent_orders_exist(record)
                ):
                    self._intent_execution_inbox.mark_exchange_confirmed(
                        intent_execution
                    )
                    return True
                self._record_denial(
                    OrderDenied(
                        "intent_exchange_confirmation_required",
                        intent_execution.intent_id,
                    )
                )
                return False

        if isinstance(
            live_canary_execution,
            LiveCanaryExecutionIdentity,
        ):
            self._live_canary_monitor_targets[
                live_canary_execution.client_order_id
            ] = plan.instrument_id
            self._live_canary_monitor_baselines[
                live_canary_execution.client_order_id
            ] = (
                live_canary_execution.symbol,
                live_canary_execution.portfolio_baseline_sha256,
            )
            try:
                claim_result = self._live_canary_execution_store.claim(
                    live_canary_execution
                )
            except RuntimeError as exc:
                self._record_denial(
                    OrderDenied(
                        "canary_execution_claim_failed",
                        repr(exc),
                    )
                )
                return False
            if claim_result is LiveCanaryClaimResult.PERMIT_CONFLICT:
                if isinstance(
                    intent_execution,
                    IntentExecutionIdentity,
                ):
                    self._intent_execution_inbox.mark_rejected(
                        intent_execution,
                        "canary_permit_already_claimed",
                    )
                self._record_denial(
                    OrderDenied(
                        "canary_permit_already_claimed",
                        live_canary_execution.permit_id,
                    )
                )
                return False
            if claim_result is LiveCanaryClaimResult.RECOVERY_REQUIRED:
                if self._live_canary_order_exists(plan):
                    self._live_canary_execution_store.mark_exchange_confirmed(
                        live_canary_execution
                    )
                    if isinstance(
                        intent_execution,
                        IntentExecutionIdentity,
                    ):
                        self._intent_execution_inbox.mark_exchange_confirmed(
                            intent_execution
                        )
                    return True
                self._record_denial(
                    OrderDenied(
                        "canary_exchange_confirmation_required",
                        live_canary_execution.client_order_id,
                    )
                )
                return False

        try:
            position_id = self._hedge_position_id(order, plan)
            if position_id is not None:
                self.submit_order(order, position_id=position_id)  # type: ignore[attr-defined]
            else:
                self.submit_order(order)  # type: ignore[attr-defined]
        except Exception as exc:  # Fail closed: no silent drops on adapter/API mismatch.
            self._record_denial(OrderDenied("order_submit_failed", repr(exc)))
            return False
        if isinstance(
            live_canary_execution,
            LiveCanaryExecutionIdentity,
        ):
            self._mark_live_canary_dispatched(
                live_canary_execution
            )
            self._after_live_canary_dispatched(live_canary_execution)
        return True

    def _live_entry_notional_denial(
        self,
        plan: OrderPlan,
        order: Any,
    ) -> OrderDenied | None:
        environment = str(
            getattr(self.config, "environment", "")
        ).lower()
        if environment != "live":
            return None
        if plan.reduce_only:
            return None

        cap = self._live_entry_notional_caps.get(plan.instrument_id)
        if cap is None:
            return OrderDenied(
                "live_entry_instrument_not_allowed",
                f"instrument={plan.instrument_id}",
            )

        quantity = _positive_canary_decimal(
            getattr(order, "quantity", None)
        )
        if quantity is None:
            return OrderDenied(
                "live_entry_quantity_invalid",
                f"instrument={plan.instrument_id}",
            )

        if plan.order_type == "MARKET":
            price_or_denial = self._fresh_live_mark_price(
                plan.instrument_id
            )
            if isinstance(price_or_denial, OrderDenied):
                return price_or_denial
            price = price_or_denial
        else:
            price = _positive_canary_decimal(
                getattr(order, "price", None)
            )
            if price is None:
                return OrderDenied(
                    "live_entry_limit_price_required",
                    f"instrument={plan.instrument_id}",
                )

        actual_notional = quantity * price
        if actual_notional > cap:
            return OrderDenied(
                "live_entry_notional_exceeded",
                (
                    f"instrument={plan.instrument_id}:"
                    f"actual={format(actual_notional, 'f')}:"
                    f"cap={format(cap, 'f')}"
                ),
            )
        return None

    def _fresh_live_mark_price(
        self,
        instrument_id: str,
    ) -> Decimal | OrderDenied:
        update = self._cache_mark_price(instrument_id)
        if not update:
            return OrderDenied(
                "live_entry_mark_price_unavailable",
                f"instrument={instrument_id}",
            )
        price = _positive_canary_decimal(
            getattr(update, "value", None)
        )
        if price is None:
            return OrderDenied(
                "live_entry_mark_price_unavailable",
                f"instrument={instrument_id}",
            )
        raw_ts_event = getattr(update, "ts_event", None)
        try:
            ts_event = int(raw_ts_event)
        except (TypeError, ValueError, OverflowError):
            return OrderDenied(
                "live_entry_mark_price_unavailable",
                f"instrument={instrument_id}",
            )
        now_ns = int(self._now().timestamp() * 1_000_000_000)
        age_ns = now_ns - ts_event
        max_age_ns = 10 * 1_000_000_000
        max_future_skew_ns = 1_000_000_000
        if age_ns < -max_future_skew_ns or age_ns > max_age_ns:
            age_seconds = Decimal(age_ns) / Decimal(1_000_000_000)
            return OrderDenied(
                "live_entry_mark_price_stale",
                (
                    f"instrument={instrument_id}:"
                    f"age_seconds={format(age_seconds, 'f')}"
                ),
            )
        return price

    def _cache_mark_price(self, instrument_id: str) -> Any:
        cache = getattr(self, "cache", None)
        if cache is None:
            return False
        method = getattr(cache, "mark_price", None)
        if not callable(method):
            return False
        typed_instrument_id = self._as_instrument_id(instrument_id)
        for candidate in (typed_instrument_id, instrument_id):
            try:
                return method(candidate) or False
            except (TypeError, ValueError):
                continue
            except Exception:
                return False
        return False

    def _live_canary_plan_denial(
        self,
        plan: OrderPlan,
        identity: LiveCanaryExecutionIdentity,
    ) -> OrderDenied | None:
        expected_runtime_identity = (
            str(getattr(self.config, "account_id", "")).strip(),
            str(getattr(self.config, "node_id", "")).strip(),
            str(getattr(self.config, "release_id", "")).strip(),
        )
        actual_runtime_identity = (
            identity.account_id,
            identity.node_id,
            identity.release_id,
        )
        if actual_runtime_identity != expected_runtime_identity:
            return OrderDenied(
                "canary_execution_identity_mismatch",
                str(plan.intent_id),
            )
        if str(plan.intent_id) != identity.intent_id:
            return OrderDenied(
                "canary_execution_identity_mismatch",
                str(plan.intent_id),
            )
        if str(plan.client_order_id) != identity.client_order_id:
            return OrderDenied(
                "canary_execution_identity_mismatch",
                str(plan.client_order_id),
            )
        expires_at = _permit_expiry(identity.expires_at)
        if expires_at is False:
            return OrderDenied(
                "canary_permit_expiry_invalid",
                str(plan.intent_id),
            )
        if expires_at <= _aware_datetime(self._now()):
            return OrderDenied(
                "canary_permit_expired",
                expires_at.isoformat(),
            )
        baseline_provider = self._live_canary_portfolio_baseline
        if baseline_provider is None:
            return OrderDenied(
                "canary_portfolio_baseline_unavailable",
                str(plan.intent_id),
            )
        try:
            current_baseline = baseline_provider(identity.symbol)
        except Exception as exc:
            return OrderDenied(
                "canary_portfolio_baseline_unavailable",
                repr(exc),
            )
        current_baseline = str(current_baseline or "").strip()
        if current_baseline != identity.portfolio_baseline_sha256:
            return OrderDenied(
                "canary_portfolio_baseline_drift",
                str(plan.intent_id),
            )
        if _canonical_symbol(plan.instrument_id) != identity.symbol:
            return OrderDenied(
                "canary_symbol_mismatch",
                str(plan.instrument_id),
            )
        if plan.order_type != "LIMIT" or plan.time_in_force != "IOC":
            return OrderDenied(
                "canary_limit_ioc_required",
                str(plan.intent_id),
            )
        quantity = _positive_canary_decimal(plan.quantity)
        price = _positive_canary_decimal(plan.price)
        permit_notional = _positive_canary_decimal(
            identity.max_notional_usdt
        )
        permit_loss_limit = _positive_canary_decimal(
            identity.max_cumulative_loss_usdt
        )
        if (
            quantity is None
            or price is None
            or permit_notional is None
            or permit_loss_limit is None
        ):
            return OrderDenied(
                "canary_limit_quantity_price_required",
                str(plan.intent_id),
            )
        actual_notional = quantity * price
        if permit_notional > Decimal("12"):
            return OrderDenied(
                "canary_notional_exceeded",
                f"permit={format(permit_notional, 'f')}",
            )
        if actual_notional > permit_notional:
            return OrderDenied(
                "canary_notional_exceeded",
                (
                    f"actual={format(actual_notional, 'f')}:"
                    f"permit={format(permit_notional, 'f')}"
                ),
            )
        if actual_notional > Decimal("12"):
            return OrderDenied(
                "canary_notional_exceeded",
                f"actual={format(actual_notional, 'f')}:hard_cap=12",
            )
        if permit_loss_limit >= Decimal("1.5"):
            return OrderDenied(
                "canary_loss_limit_exceeded",
                (
                    f"permit={format(permit_loss_limit, 'f')}:"
                    "hard_cap=1.5"
                ),
            )
        return None

    def _live_canary_order_exists(self, plan: OrderPlan) -> bool:
        return self._client_order_id_exists(
            plan.instrument_id,
            str(plan.client_order_id),
        )

    def _durable_intent_orders_exist(self, record: Any) -> bool:
        client_order_ids = tuple(
            str(value)
            for value in getattr(record, "client_order_ids", ())
        )
        if not client_order_ids:
            return False
        instrument_id = str(
            getattr(record, "instrument_id", "")
        )
        if not instrument_id:
            return False
        return all(
            self._client_order_id_exists(
                instrument_id,
                client_order_id,
            )
            for client_order_id in client_order_ids
        )

    def _client_order_id_exists(
        self,
        instrument_id: str,
        client_order_id: str,
    ) -> bool:
        target = str(client_order_id)
        for order in self._cache_orders(instrument_id):
            if str(getattr(order, "client_order_id", "")) == target:
                return True
        cache = getattr(self, "cache", None)
        if cache is not None:
            orders_method = getattr(cache, "orders", None)
            if callable(orders_method):
                try:
                    all_orders = orders_method()
                except TypeError:
                    all_orders = ()
                for order in all_orders or ():
                    if str(getattr(order, "client_order_id", "")) == target:
                        return True
        mirror = self._exchange_state_mirror
        if not mirror:
            return False
        find_order = getattr(mirror, "find_order", None)
        if callable(find_order):
            try:
                found = find_order(instrument_id, target)
            except Exception:
                found = False
            if found:
                return True
        orders_for_instrument = getattr(
            mirror,
            "orders_for_instrument",
            None,
        )
        if not callable(orders_for_instrument):
            return False
        try:
            orders = orders_for_instrument(instrument_id)
        except Exception:
            return False
        for order in orders or ():
            if str(getattr(order, "client_order_id", "")) == target:
                return True
        return False

    def _after_live_canary_dispatched(
        self,
        identity: LiveCanaryExecutionIdentity,
    ) -> None:
        del identity

    def _mark_live_canary_dispatched(
        self,
        identity: LiveCanaryExecutionIdentity,
    ) -> None:
        self._live_canary_execution_store.mark_dispatched(identity)

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

    def _queue_management_plan_after_persist(
        self,
        plan: ManagementPlan,
        *,
        source_intent: Any,
    ) -> bool:
        parent_intent_id = _management_parent_intent_id(plan)
        if not _authorization_matches_parent(
            plan.authorization,
            parent_intent_id,
        ):
            denial = OrderDenied(
                "management_authorization_missing",
                str(plan.intent_id),
            )
            self._record_denial(denial)
            self._report_denial(source_intent, denial)
            return False
        if not self._terminal_exchange_worker:
            environment = str(
                getattr(self.config, "environment", "")
            ).lower()
            if environment == "live":
                denial = OrderDenied(
                    "management_terminal_worker_required",
                    str(plan.intent_id),
                )
                self._record_denial(denial)
                self._report_denial(source_intent, denial)
                return False
            submitted = self._submit_management_plan(
                plan,
                source_intent=source_intent,
            )
            if not submitted:
                denial = self.denials[-1] if self.denials else OrderDenied(
                    "management_submit_failed",
                    str(plan.intent_id),
                )
                self._report_denial(source_intent, denial)
            return bool(submitted)

        if plan.action == CANCEL_ORDER:
            requests = self._management_cancel_requests(
                plan.instrument_id,
                tuple(plan.cancel_order_ids),
            )
            if requests is False:
                denial = self.denials[-1]
                self._report_denial(source_intent, denial)
                return False
            return self._queue_management_dispatch_task(
                {
                    "plan": plan,
                    "source_intent": source_intent,
                    "cancel_order_ids": tuple(plan.cancel_order_ids),
                    "disabling_take_profits": False,
                }
            )

        disabling_take_profits = (
            str(plan.action) == "replace_take_profits"
            and plan.disable_take_profits
        )
        cancel_order_ids = self._management_cancel_order_ids(plan)
        if cancel_order_ids is None:
            denial = self.denials[-1]
            self._report_denial(source_intent, denial)
            return False
        protection_preimage = copy.deepcopy(
            self._entry_protection_stash
        )
        tombstone_state = "disabled"
        if disabling_take_profits:
            tombstone_state = "cancel_pending"
        if not self._absorb_management_plan(
            plan,
            take_profit_tombstone_state=tombstone_state,
            persist=False,
        ):
            denial = self.denials[-1]
            self._report_denial(source_intent, denial)
            return False
        if disabling_take_profits and not (
            self._set_take_profit_disable_pending_ids(
                plan,
                cancel_order_ids,
                persist=False,
            )
        ):
            self._entry_protection_stash = protection_preimage
            denial = self.denials[-1]
            self._report_denial(source_intent, denial)
            return False
        queued = self._queue_entry_protection_stash_persist(
            continuation={
                "kind": "management_dispatch_after_persist",
                "plan": plan,
                "source_intent": source_intent,
                "cancel_order_ids": cancel_order_ids,
                "disabling_take_profits": (
                    disabling_take_profits
                ),
            }
        )
        if queued:
            return True
        self._entry_protection_stash = protection_preimage
        denial = self.denials[-1] if self.denials else OrderDenied(
            "management_persist_queue_rejected",
            str(plan.intent_id),
        )
        self._report_denial(source_intent, denial)
        return False

    def _queue_management_dispatch_task(
        self,
        continuation: Mapping[str, Any],
    ) -> bool:
        plan = continuation.get("plan")
        source_intent = continuation.get("source_intent")
        if not isinstance(plan, ManagementPlan):
            self._halt_durable_io(
                "management dispatch continuation missing plan"
            )
            return False
        try:
            identity = _intent_execution_identity(source_intent)
            operation_ids = _management_operation_ids(plan)
        except Exception as exc:
            denial = OrderDenied(
                "management_durable_identity_invalid",
                repr(exc),
            )
            self._record_denial(denial)
            self._report_denial(source_intent, denial)
            return False
        task_continuation = dict(continuation)
        task_continuation["kind"] = "management_prepared"
        queued = self._submit_durable_io_task(
            _DurableIoTask(
                kind=_DurableIoTaskKind.MANAGEMENT_PREPARE,
                intent=source_intent,
                intent_execution=identity,
                client_order_ids=operation_ids,
                continuation=task_continuation,
            )
        )
        if queued:
            return True
        denial = self.denials[-1] if self.denials else OrderDenied(
            "management_dispatch_queue_rejected",
            str(plan.intent_id),
        )
        self._report_denial(source_intent, denial)
        return False

    def _continue_management_after_persist(
        self,
        continuation: Mapping[str, Any],
    ) -> None:
        plan = continuation.get("plan")
        source_intent = continuation.get("source_intent")
        if not isinstance(plan, ManagementPlan):
            self._halt_durable_io(
                "management continuation missing plan"
            )
            return
        cancel_order_ids = tuple(
            str(value)
            for value in continuation.get(
                "cancel_order_ids",
                (),
            )
        )
        disabling_take_profits = bool(
            continuation.get("disabling_take_profits", False)
        )
        if not disabling_take_profits:
            for order_plan in plan.orders:
                if self._submit_order_plan(order_plan):
                    continue
                denial = self.denials[-1] if self.denials else OrderDenied(
                    "management_submit_failed",
                    str(plan.intent_id),
                )
                self._report_denial(source_intent, denial)
                return
        if cancel_order_ids:
            requests = self._management_cancel_requests(
                plan.instrument_id,
                cancel_order_ids,
            )
            if requests is False:
                denial = self.denials[-1]
                self._report_denial(source_intent, denial)
                return
            self._queue_management_cancel_batch(
                plan=plan,
                source_intent=source_intent,
                requests=requests,
                finalize_take_profit_disable=(
                    disabling_take_profits
                ),
            )
            return
        if disabling_take_profits:
            if not self._finalize_take_profit_disable(
                plan,
                persist=False,
            ):
                denial = self.denials[-1]
                self._report_denial(source_intent, denial)
                return
            self._queue_entry_protection_stash_persist(
                continuation={
                    "kind": "management_finalize_after_persist",
                    "plan": plan,
                    "source_intent": source_intent,
                }
            )
            return
        self._queue_management_complete_task(
            plan,
            source_intent=source_intent,
        )

    def _queue_management_complete_task(
        self,
        plan: ManagementPlan,
        *,
        source_intent: Any,
    ) -> bool:
        try:
            identity = _intent_execution_identity(source_intent)
        except Exception as exc:
            self._halt_durable_io(
                f"management completion identity invalid: {exc!r}"
            )
            denial = self.denials[-1]
            self._report_denial(source_intent, denial)
            return False
        queued = self._submit_durable_io_task(
            _DurableIoTask(
                kind=_DurableIoTaskKind.MANAGEMENT_COMPLETE,
                intent=source_intent,
                intent_execution=identity,
                continuation={"kind": "management_completed"},
            )
        )
        if queued:
            return True
        self._halt_durable_io(
            "management completion queue rejected"
        )
        denial = self.denials[-1]
        self._report_denial(source_intent, denial)
        return False

    def _submit_management_plan(
        self,
        plan: ManagementPlan,
        *,
        source_intent: Any = None,
    ) -> Any:
        parent_intent_id = _management_parent_intent_id(plan)
        if not _authorization_matches_parent(
            plan.authorization,
            parent_intent_id,
        ):
            self._record_denial(
                OrderDenied(
                    "management_authorization_missing",
                    str(plan.intent_id),
                )
            )
            return False
        if self._terminal_exchange_worker:
            return self._submit_management_plan_via_lane(
                plan,
                source_intent=source_intent,
            )
        if plan.action == CANCEL_ORDER:
            for client_order_id in plan.cancel_order_ids:
                if not self._cancel_via_exchange_adapter(
                    plan.instrument_id,
                    client_order_id,
                ):
                    return False
            return True
        disabling_take_profits = (
            str(plan.action) == "replace_take_profits"
            and plan.disable_take_profits
        )
        if disabling_take_profits:
            if not self._absorb_management_plan(
                plan,
                take_profit_tombstone_state="cancel_pending",
            ):
                return False
            if not self._refresh_exchange_state():
                return False
            cancel_order_ids = self._management_cancel_order_ids(plan)
            if cancel_order_ids is None:
                return False
            if not self._set_take_profit_disable_pending_ids(
                plan,
                cancel_order_ids,
            ):
                return False
            for client_order_id in cancel_order_ids:
                if not self._cancel_via_exchange_adapter(
                    plan.instrument_id,
                    client_order_id,
                ):
                    return False
            return self._finalize_take_profit_disable(plan)
        cancel_order_ids = self._management_cancel_order_ids(plan)
        if cancel_order_ids is None:
            return False
        if not self._absorb_management_plan(plan):
            return False
        # Make-before-break: place replacements first, then cancel the superseded
        # orders. If the new stop is rejected by the venue the old one is still
        # standing; the reverse order can leave the position naked. Reduce-only
        # orders briefly coexisting cannot over-close the position.
        for order_plan in plan.orders:
            if not self._submit_order_plan(order_plan):
                return False
        for client_order_id in cancel_order_ids:
            if not self._cancel_management_order(
                plan.instrument_id,
                client_order_id,
            ):
                return False
        return True

    def _submit_management_plan_via_lane(
        self,
        plan: ManagementPlan,
        *,
        source_intent: Any,
    ) -> Any:
        if source_intent is None:
            self._record_denial(
                OrderDenied(
                    "management_source_intent_missing",
                    str(plan.intent_id),
                )
            )
            return False
        disabling_take_profits = (
            str(plan.action) == "replace_take_profits"
            and plan.disable_take_profits
        )
        if plan.action == CANCEL_ORDER:
            cancel_order_ids = tuple(plan.cancel_order_ids)
        elif disabling_take_profits:
            if not self._absorb_management_plan(
                plan,
                take_profit_tombstone_state="cancel_pending",
            ):
                return False
            cancel_order_ids = self._management_cancel_order_ids(plan)
            if cancel_order_ids is None:
                return False
            if not self._set_take_profit_disable_pending_ids(
                plan,
                cancel_order_ids,
            ):
                return False
        else:
            cancel_order_ids = self._management_cancel_order_ids(plan)
            if cancel_order_ids is None:
                return False
            if not self._absorb_management_plan(plan):
                return False
            for order_plan in plan.orders:
                if not self._submit_order_plan(order_plan):
                    return False
        if not cancel_order_ids:
            if disabling_take_profits:
                return self._finalize_take_profit_disable(plan)
            return True
        requests = self._management_cancel_requests(
            plan.instrument_id,
            cancel_order_ids,
        )
        if requests is False:
            return False
        return self._queue_management_cancel_batch(
            plan=plan,
            source_intent=source_intent,
            requests=requests,
            finalize_take_profit_disable=disabling_take_profits,
        )

    def _management_cancel_requests(
        self,
        instrument_id: str,
        client_order_ids: tuple[str, ...],
    ) -> tuple[Any, ...] | bool:
        mirror = self._exchange_state_mirror
        find_order = getattr(mirror, "find_order", None)
        if not callable(find_order):
            self._record_denial(
                OrderDenied(
                    "exchange_cancel_adapter_unavailable",
                    instrument_id,
                )
            )
            return False
        from runtime.exchange_cancel_adapter import CancelOrderRequest

        requests: list[CancelOrderRequest] = []
        for client_order_id in client_order_ids:
            try:
                order = find_order(instrument_id, client_order_id)
            except Exception as exc:
                self._record_denial(
                    OrderDenied(
                        "exchange_state_refresh_failed",
                        repr(exc),
                    )
                )
                return False
            if not order:
                self._record_denial(
                    OrderDenied(
                        "order_cancel_not_found",
                        client_order_id,
                    )
                )
                return False
            venue_order_id = _optional_str(
                getattr(order, "venue_order_id", None)
            )
            requests.append(
                CancelOrderRequest(
                    account_id=str(
                        getattr(order, "account_id", "") or ""
                    ),
                    symbol=str(getattr(order, "symbol", "") or ""),
                    position_side=str(
                        getattr(order, "position_side", "") or ""
                    ),
                    order_kind=str(
                        getattr(order, "order_kind", "") or ""
                    ),
                    venue_order_id=venue_order_id,
                    client_order_id=client_order_id,
                )
            )
        return tuple(requests)

    def _queue_management_cancel_batch(
        self,
        *,
        plan: ManagementPlan,
        source_intent: Any,
        requests: tuple[Any, ...],
        finalize_take_profit_disable: bool,
    ) -> Any:
        from runtime.exchange_cancel_adapter import (
            TerminalExchangeRequest,
        )

        request_id = f"management-cancel:{plan.intent_id}"
        request = TerminalExchangeRequest(
            request_id=request_id,
            account_id=str(self.config.account_id),
            operation="cancel_batch",
            purpose=f"management:{plan.action}",
            deadline_monotonic=(
                self._terminal_exchange_worker.new_deadline()
            ),
            cancel_requests=requests,
        )
        self._pending_terminal_exchange[request_id] = {
            "kind": "management_cancel",
            "intent": source_intent,
            "plan": plan,
            "expected_cancel_ids": tuple(
                str(request.client_order_id or "")
                for request in requests
            ),
            "finalize_take_profit_disable": (
                finalize_take_profit_disable
            ),
        }
        if self._terminal_exchange_worker.submit(request):
            return _TERMINAL_EXCHANGE_PENDING
        self._pending_terminal_exchange.pop(request_id, None)
        self._record_denial(
            OrderDenied(
                "terminal_exchange_queue_rejected",
                str(plan.intent_id),
            )
        )
        return False

    def _complete_management_cancels(
        self,
        result: Any,
        pending: dict[str, Any],
    ) -> None:
        intent = pending["intent"]
        plan = pending["plan"]
        failure = str(getattr(result, "error", "") or "")
        observed_cancel_ids: set[str] = set()
        if not failure:
            for outcome in tuple(
                getattr(result, "cancel_outcomes", ()) or ()
            ):
                request = getattr(outcome, "request", None)
                observed_cancel_ids.add(
                    str(
                        getattr(
                            request,
                            "client_order_id",
                            "",
                        )
                        or ""
                    )
                )
                if str(getattr(outcome, "status", "")) == "confirmed":
                    continue
                failure = str(
                    getattr(outcome, "error", "") or ""
                )
                if not failure:
                    failure = "order cancellation was not confirmed"
                break
        expected_cancel_ids = {
            str(client_order_id)
            for client_order_id in pending["expected_cancel_ids"]
        }
        if not failure and observed_cancel_ids != expected_cancel_ids:
            failure = (
                "order cancellation result set mismatch: "
                f"expected={sorted(expected_cancel_ids)}:"
                f"observed={sorted(observed_cancel_ids)}"
            )
        if failure:
            denial = OrderDenied("order_cancel_failed", failure)
            self._record_denial(denial)
            self._report_denial(intent, denial)
            return
        if pending["finalize_take_profit_disable"]:
            if not self._finalize_take_profit_disable(
                plan,
                persist=False,
            ):
                denial = self.denials[-1]
                self._report_denial(intent, denial)
                return
            self._queue_entry_protection_stash_persist(
                continuation={
                    "kind": "management_finalize_after_persist",
                    "plan": plan,
                    "source_intent": intent,
                }
            )
            return
        self._queue_management_complete_task(
            plan,
            source_intent=intent,
        )

    def _management_cancel_order_ids(
        self,
        plan: ManagementPlan,
    ) -> Optional[tuple[str, ...]]:
        role = ""
        if str(plan.action) in {"move_stop_loss", "move_stop_to_entry"}:
            role = "stop_loss"
        if str(plan.action) == "replace_take_profits":
            role = "take_profit"
        ids = {
            str(client_order_id)
            for client_order_id in plan.cancel_order_ids
            if str(client_order_id)
        }
        if not role:
            return tuple(sorted(ids))

        live_ids = {
            str(getattr(order, "client_order_id", ""))
            for order in self._cache_orders(plan.instrument_id)
        }
        mirror_live_ids: set[str] = set()
        mirror = self._exchange_state_mirror
        orders_for_instrument = (
            getattr(mirror, "orders_for_instrument", None) if mirror else None
        )
        if callable(orders_for_instrument):
            try:
                mirror_orders = orders_for_instrument(plan.instrument_id)
            except Exception as exc:
                self._record_denial(
                    OrderDenied("exchange_state_refresh_failed", repr(exc))
                )
                return None
            mirror_live_ids.update(
                str(getattr(order, "client_order_id", ""))
                for order in mirror_orders
            )
            live_ids.update(mirror_live_ids)
        if plan.disable_take_profits:
            ids.intersection_update(mirror_live_ids)
        authoritative_live_ids = live_ids
        if plan.disable_take_profits:
            authoritative_live_ids = mirror_live_ids

        for stash in self._entry_protection_stash.values():
            if not self._management_plan_targets_stash(plan, stash):
                continue
            roles = stash.get("protection_roles")
            if not isinstance(roles, dict):
                continue
            for client_order_id, role_info in roles.items():
                if not isinstance(role_info, dict):
                    continue
                if str(role_info.get("role") or "") != role:
                    continue
                client_order_id = str(client_order_id)
                if client_order_id in authoritative_live_ids:
                    ids.add(client_order_id)
        return tuple(sorted(ids))

    def _cancel_management_order(
        self,
        instrument_id: str,
        client_order_id: str,
    ) -> bool:
        for order in self._cache_orders(instrument_id):
            if str(getattr(order, "client_order_id", "")) != client_order_id:
                continue
            try:
                self.cancel_order(order)  # type: ignore[attr-defined]
                return True
            except Exception as exc:
                self._record_denial(OrderDenied("order_cancel_failed", repr(exc)))
                return False
        return self._cancel_via_exchange_adapter(
            instrument_id,
            client_order_id,
        )

    def _cancel_via_exchange_adapter(
        self,
        instrument_id: str,
        client_order_id: str,
    ) -> bool:
        if not self._exchange_cancel_adapter or not self._exchange_state_mirror:
            self._record_denial(
                OrderDenied("exchange_cancel_adapter_unavailable", client_order_id)
            )
            return False
        find_order = getattr(self._exchange_state_mirror, "find_order", None)
        if not callable(find_order):
            self._record_denial(OrderDenied("order_cancel_not_found", client_order_id))
            return False
        try:
            order = find_order(instrument_id, client_order_id)
        except Exception as exc:
            self._record_denial(
                OrderDenied("exchange_state_refresh_failed", repr(exc))
            )
            return False
        if not order:
            self._record_denial(OrderDenied("order_cancel_not_found", client_order_id))
            return False
        from runtime.exchange_cancel_adapter import (
            CancelOrderRequest,
            OrderAlreadyFilledError,
        )

        request = CancelOrderRequest(
            account_id=str(getattr(order, "account_id", "")),
            symbol=str(getattr(order, "symbol", "")),
            position_side=str(getattr(order, "position_side", "")),
            order_kind=str(getattr(order, "order_kind", "")),
            venue_order_id=_optional_str(getattr(order, "venue_order_id", None)),
            client_order_id=client_order_id,
        )
        try:
            result = self._exchange_cancel_adapter.cancel("cancel_order", request)
            terminal_status = str(
                getattr(result, "terminal_status", "")
            ).upper()
            if terminal_status not in {"CANCELED", "CANCELLED"}:
                self._record_denial(
                    OrderDenied(
                        "order_cancel_unconfirmed",
                        f"{client_order_id}:{terminal_status or 'UNKNOWN'}",
                    )
                )
                return False
            return True
        except OrderAlreadyFilledError as exc:
            self._record_denial(OrderDenied("order_already_filled", str(exc)))
            return False
        except Exception as exc:
            self._record_denial(OrderDenied("order_cancel_failed", repr(exc)))
            return False

    def _absorb_management_plan(
        self,
        plan: ManagementPlan,
        *,
        take_profit_tombstone_state: str = "disabled",
        persist: bool = True,
    ) -> bool:
        """Keep entry stashes coherent with operator-managed protections: without
        this, a later entry fill re-places SL/TP at the ORIGINAL signal prices and
        silently undoes an operator's move_stop_loss/replace_take_profits."""
        action = str(plan.action)
        if action not in ("move_stop_loss", "move_stop_to_entry", "replace_take_profits"):
            return True
        targeted: list[tuple[str, dict[str, Any]]] = []
        for intent_key, stash in self._entry_protection_stash.items():
            if not self._management_plan_targets_stash(plan, stash):
                continue
            targeted.append((intent_key, stash))
        if not targeted:
            return True
        preimage = {
            intent_key: copy.deepcopy(stash)
            for intent_key, stash in targeted
        }
        for intent_key, stash in targeted:
            parent_intent_id = _management_parent_intent_id(plan)
            if action in ("move_stop_loss", "move_stop_to_entry"):
                trigger = next(
                    (op.trigger_price for op in plan.orders if op.trigger_price is not None),
                    None,
                )
                if trigger is not None:
                    stash["stop_loss"] = trigger
                    stash["stop_loss_parent_intent_id"] = parent_intent_id
                    stash["stop_loss_authorization"] = dict(plan.authorization or {})
            else:
                triggers = tuple(
                    op.trigger_price
                    for op in plan.orders
                    if op.trigger_price is not None
                )
                if triggers:
                    tombstone = stash.pop("take_profit_tombstone", None)
                    if isinstance(tombstone, dict):
                        superseded = dict(tombstone)
                        superseded["superseded_at"] = self._now().isoformat()
                        superseded["superseded_by_intent_id"] = str(plan.intent_id)
                        stash["last_take_profit_tombstone"] = superseded
                    stash["take_profits"] = triggers
                    stash["take_profit_quantities"] = tuple(
                        op.quantity
                        for op in plan.orders
                        if op.trigger_price is not None
                    )
                    stash["tp_consumed"] = {}
                    stash["take_profit_parent_intent_id"] = parent_intent_id
                    stash["take_profit_authorization"] = dict(
                        plan.authorization or {}
                    )
                elif plan.disable_take_profits:
                    authorization = dict(plan.authorization or {})
                    tombstone = {
                        "state": take_profit_tombstone_state,
                        "reason": "authorized_take_profit_disable",
                        "created_at": self._now().isoformat(),
                        "parent_intent_id": parent_intent_id,
                    }
                    tombstone.update(authorization)
                    stash["take_profit_tombstone"] = tombstone
                    stash["take_profit_parent_intent_id"] = parent_intent_id
                    stash["take_profit_authorization"] = authorization
                else:
                    for key, value in preimage.items():
                        self._entry_protection_stash[key] = value
                    self._record_denial(
                        OrderDenied(
                            "take_profit_disable_flag_required",
                            str(plan.intent_id),
                        )
                    )
                    return False
            # The operator's orders belong to a different intent id: adopt nothing,
            # but force the next entry-fill sync to rebuild from the updated prices.
            stash.pop("protection_frozen", None)
            if intent_key:
                self._quick_fill_windows.pop(intent_key, None)
                for order_plan in plan.orders:
                    self._register_protection_role(
                        intent_key,
                        stash,
                        order_plan.client_order_id,
                        order_plan,
                    )
            stash["protected_quantity"] = None
        if not persist:
            return True
        if self._persist_entry_protection_stash():
            return True
        for key, value in preimage.items():
            self._entry_protection_stash[key] = value
        return False

    def _set_take_profit_disable_pending_ids(
        self,
        plan: ManagementPlan,
        cancel_order_ids: tuple[str, ...],
        *,
        persist: bool = True,
    ) -> bool:
        targeted = [
            (intent_key, stash)
            for intent_key, stash in self._entry_protection_stash.items()
            if self._management_plan_targets_stash(plan, stash)
        ]
        if not targeted:
            return True
        preimage = {
            intent_key: copy.deepcopy(stash)
            for intent_key, stash in targeted
        }
        parent_intent_id = _management_parent_intent_id(plan)
        pending_ids = sorted(
            {
                str(client_order_id)
                for client_order_id in cancel_order_ids
                if str(client_order_id)
            }
        )
        for _intent_key, stash in targeted:
            tombstone = stash.get("take_profit_tombstone")
            if not _valid_take_profit_tombstone(
                tombstone,
                parent_intent_id,
            ):
                self._record_denial(
                    OrderDenied(
                        "take_profit_tombstone_invalid",
                        str(plan.intent_id),
                    )
                )
                return False
            tombstone["pending_cancel_ids"] = pending_ids
        if not persist:
            return True
        if self._persist_entry_protection_stash():
            return True
        for key, value in preimage.items():
            self._entry_protection_stash[key] = value
        return False

    def _retry_pending_take_profit_disables(self) -> None:
        if self._terminal_exchange_worker:
            self._queue_pending_take_profit_disable_retries()
            return
        mirror = self._exchange_state_mirror
        orders_for_instrument = (
            getattr(mirror, "orders_for_instrument", None) if mirror else None
        )
        if not callable(orders_for_instrument):
            return
        for intent_key, stash in self._entry_protection_stash.items():
            tombstone = stash.get("take_profit_tombstone")
            parent_intent_id = stash.get("take_profit_parent_intent_id")
            if (
                not _valid_take_profit_tombstone(
                    tombstone,
                    parent_intent_id,
                )
                or str(tombstone.get("state") or "") != "cancel_pending"
            ):
                continue
            instrument_id = str(stash.get("instrument_id") or "")
            if not instrument_id:
                continue
            try:
                mirror_orders = tuple(orders_for_instrument(instrument_id))
            except Exception as exc:
                self._record_denial(
                    OrderDenied("exchange_state_refresh_failed", repr(exc))
                )
                continue
            live_by_id = {
                str(getattr(order, "client_order_id", "")): order
                for order in mirror_orders
                if str(getattr(order, "client_order_id", ""))
            }
            pending_ids = {
                str(client_order_id)
                for client_order_id in tombstone.get("pending_cancel_ids", [])
                if str(client_order_id)
            }
            roles = stash.get("protection_roles")
            if isinstance(roles, dict):
                for client_order_id, role_info in roles.items():
                    if not isinstance(role_info, dict):
                        continue
                    if str(role_info.get("role") or "") != "take_profit":
                        continue
                    client_order_id = str(client_order_id)
                    if client_order_id in live_by_id:
                        pending_ids.add(client_order_id)
            tombstone["pending_cancel_ids"] = sorted(pending_ids)
            if not self._persist_entry_protection_stash():
                continue
            for client_order_id in sorted(tuple(pending_ids)):
                if client_order_id not in live_by_id:
                    pending_ids.discard(client_order_id)
                    continue
                if self._cancel_via_exchange_adapter(
                    instrument_id,
                    client_order_id,
                ):
                    pending_ids.discard(client_order_id)
            tombstone["pending_cancel_ids"] = sorted(pending_ids)
            if not pending_ids:
                tombstone["state"] = "disabled"
                tombstone["completed_at"] = self._now().isoformat()
            self._persist_entry_protection_stash()

    def _queue_pending_take_profit_disable_retries(self) -> None:
        mirror = self._exchange_state_mirror
        orders_for_instrument = (
            getattr(mirror, "orders_for_instrument", None)
            if mirror
            else None
        )
        if not callable(orders_for_instrument):
            return
        for intent_key, stash in (
            self._entry_protection_stash.items()
        ):
            if stash.get("_take_profit_retry_request_id"):
                continue
            tombstone = stash.get("take_profit_tombstone")
            parent_intent_id = stash.get(
                "take_profit_parent_intent_id"
            )
            if (
                not _valid_take_profit_tombstone(
                    tombstone,
                    parent_intent_id,
                )
                or str(tombstone.get("state") or "")
                != "cancel_pending"
            ):
                continue
            instrument_id = str(stash.get("instrument_id") or "")
            if not instrument_id:
                continue
            try:
                mirror_orders = tuple(
                    orders_for_instrument(instrument_id)
                )
            except Exception as exc:
                self._record_denial(
                    OrderDenied(
                        "exchange_state_refresh_failed",
                        repr(exc),
                    )
                )
                continue
            live_ids = {
                str(getattr(order, "client_order_id", ""))
                for order in mirror_orders
                if str(getattr(order, "client_order_id", ""))
            }
            pending_ids = {
                str(client_order_id)
                for client_order_id in tombstone.get(
                    "pending_cancel_ids",
                    [],
                )
                if str(client_order_id)
            }
            roles = stash.get("protection_roles")
            if isinstance(roles, dict):
                for client_order_id, role_info in roles.items():
                    if not isinstance(role_info, dict):
                        continue
                    if (
                        str(role_info.get("role") or "")
                        != "take_profit"
                    ):
                        continue
                    normalized_id = str(client_order_id)
                    if normalized_id in live_ids:
                        pending_ids.add(normalized_id)
            active_pending_ids = tuple(
                sorted(pending_ids.intersection(live_ids))
            )
            tombstone["pending_cancel_ids"] = list(
                active_pending_ids
            )
            if not active_pending_ids:
                tombstone["state"] = "disabled"
                tombstone["completed_at"] = self._now().isoformat()
                self._queue_entry_protection_stash_persist()
                continue
            requests = self._management_cancel_requests(
                instrument_id,
                active_pending_ids,
            )
            if requests is False:
                continue
            self._queue_entry_protection_stash_persist(
                continuation={
                    "kind": "take_profit_retry_after_persist",
                    "intent_key": intent_key,
                    "requests": requests,
                }
            )

    def _queue_take_profit_retry(
        self,
        *,
        intent_key: str,
        stash: dict[str, Any],
        requests: tuple[Any, ...],
    ) -> bool:
        from runtime.exchange_cancel_adapter import (
            TerminalExchangeRequest,
        )

        request_id = (
            f"take-profit-retry:{intent_key}:{uuid4().hex}"
        )
        request = TerminalExchangeRequest(
            request_id=request_id,
            account_id=str(self.config.account_id),
            operation="cancel_batch",
            purpose="take_profit_disable_retry",
            deadline_monotonic=(
                self._terminal_exchange_worker.new_deadline()
            ),
            cancel_requests=requests,
        )
        stash["_take_profit_retry_request_id"] = request_id
        self._pending_terminal_exchange[request_id] = {
            "kind": "take_profit_retry",
            "intent_key": intent_key,
        }
        if self._terminal_exchange_worker.submit(request):
            return True
        stash.pop("_take_profit_retry_request_id", None)
        self._pending_terminal_exchange.pop(request_id, None)
        self._record_denial(
            OrderDenied(
                "terminal_exchange_queue_rejected",
                intent_key,
            )
        )
        return False

    def _complete_take_profit_retry(
        self,
        result: Any,
        pending: dict[str, Any],
    ) -> None:
        intent_key = str(pending["intent_key"])
        stash = self._entry_protection_stash.get(intent_key)
        if not isinstance(stash, dict):
            return
        stash.pop("_take_profit_retry_request_id", None)
        tombstone = stash.get("take_profit_tombstone")
        if not isinstance(tombstone, dict):
            return
        pending_ids = {
            str(client_order_id)
            for client_order_id in tombstone.get(
                "pending_cancel_ids",
                [],
            )
            if str(client_order_id)
        }
        for outcome in tuple(
            getattr(result, "cancel_outcomes", ()) or ()
        ):
            request = getattr(outcome, "request", None)
            client_order_id = str(
                getattr(request, "client_order_id", "") or ""
            )
            if str(getattr(outcome, "status", "")) == "confirmed":
                pending_ids.discard(client_order_id)
                continue
            error = str(getattr(outcome, "error", "") or "")
            self._record_denial(
                OrderDenied(
                    "order_cancel_failed",
                    error or client_order_id,
                )
            )
        result_error = str(getattr(result, "error", "") or "")
        if result_error:
            self._record_denial(
                OrderDenied(
                    "order_cancel_failed",
                    result_error,
                )
            )
        tombstone["pending_cancel_ids"] = sorted(pending_ids)
        if not pending_ids:
            tombstone["state"] = "disabled"
            tombstone["completed_at"] = self._now().isoformat()
        self._queue_entry_protection_stash_persist()

    def _finalize_take_profit_disable(
        self,
        plan: ManagementPlan,
        *,
        persist: bool = True,
    ) -> bool:
        targeted: list[tuple[str, dict[str, Any]]] = []
        for intent_key, stash in self._entry_protection_stash.items():
            if self._management_plan_targets_stash(plan, stash):
                targeted.append((intent_key, stash))
        if not targeted:
            return True
        preimage = {
            intent_key: copy.deepcopy(stash)
            for intent_key, stash in targeted
        }
        parent_intent_id = _management_parent_intent_id(plan)
        tombstones: list[dict[str, Any]] = []
        for _intent_key, stash in targeted:
            tombstone = stash.get("take_profit_tombstone")
            if not _valid_take_profit_tombstone(tombstone, parent_intent_id):
                self._record_denial(
                    OrderDenied(
                        "take_profit_tombstone_invalid",
                        str(plan.intent_id),
                    )
                )
                return False
            tombstones.append(tombstone)
        for tombstone in tombstones:
            tombstone["state"] = "disabled"
            tombstone["completed_at"] = self._now().isoformat()
            tombstone["pending_cancel_ids"] = []
        if not persist:
            return True
        if self._persist_entry_protection_stash():
            return True
        for key, value in preimage.items():
            self._entry_protection_stash[key] = value
        return False

    def _management_plan_targets_stash(
        self,
        plan: ManagementPlan,
        stash: dict[str, Any],
    ) -> bool:
        if str(stash.get("instrument_id")) != str(plan.instrument_id):
            return False
        target_side = str(plan.target_position_side or "").upper()
        if target_side not in {"LONG", "SHORT"}:
            target_side = _position_book_from_id(plan.target_position_id)
        if target_side not in {"LONG", "SHORT"}:
            for order in plan.orders:
                side = str(order.side).upper()
                if side == "SELL":
                    target_side = "LONG"
                    break
                if side == "BUY":
                    target_side = "SHORT"
                    break
        if target_side not in {"LONG", "SHORT"}:
            return False
        expected_entry_side = "BUY" if target_side == "LONG" else "SELL"
        return str(stash.get("entry_side") or "").upper() == expected_entry_side

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
        # stop_market, stop_limit, and market_if_touched orders accept
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


def _event_order_type(event: Any) -> Optional[str]:
    for holder in (event, getattr(event, "order", None)):
        if holder is None:
            continue
        value = getattr(holder, "order_type", None)
        if value is not None:
            return _enum_name(value)
    return None


def _event_order_side(event: Any) -> Optional[str]:
    for holder in (event, getattr(event, "order", None)):
        if holder is None:
            continue
        value = getattr(holder, "side", getattr(holder, "order_side", None))
        if value is not None:
            return _enum_name(value)
    return None


def _event_type_name(event: Any) -> str:
    value = getattr(event, "event_type", getattr(event, "type", None))
    if value is not None:
        text = str(getattr(value, "value", value))
        if text:
            return text
    return event.__class__.__name__


def _event_text_field(event: Any, *names: str) -> Optional[str]:
    for holder in (event, getattr(event, "order", None)):
        if holder is None:
            continue
        for name in names:
            value = getattr(holder, name, None)
            if value is not None:
                return str(getattr(value, "value", value))
    return None


def _event_last_qty(event: Any) -> Optional[str]:
    for holder in (event, getattr(event, "order", None)):
        if holder is None:
            continue
        for name in ("last_qty", "last_quantity", "quantity", "filled_qty", "filled_quantity"):
            value = getattr(holder, name, None)
            if value is not None:
                return str(value)
    return None


def _event_fill_price(event: Any) -> Optional[str]:
    for holder in (event, getattr(event, "order", None)):
        if holder is None:
            continue
        for name in (
            "last_px",
            "last_price",
            "price",
            "average_price",
            "avg_px",
        ):
            value = getattr(holder, name, None)
            if value is not None:
                return _decimalish_to_str(value)
    return None


def _event_fee_usdt(event: Any) -> tuple[str, str]:
    for holder in (event, getattr(event, "order", None)):
        if holder is None:
            continue
        for name in ("commission", "fee", "fees"):
            value = getattr(holder, name, None)
            if value is None:
                continue
            amount = _money_amount(value)
            currency = _money_currency(value)
            if amount is None:
                return "0", "fill fee amount is unavailable"
            if currency and currency not in {"USDT", "USDC"}:
                return (
                    "0",
                    f"fill fee currency is unsupported: {currency}",
                )
            if not currency:
                return "0", "fill fee currency is unavailable"
            return amount, ""
    return "0", "fill fee is unavailable"


def _money_amount(value: Any) -> Optional[str]:
    for name in ("as_decimal", "to_decimal"):
        method = getattr(value, name, None)
        if callable(method):
            try:
                return str(method())
            except Exception:
                return None
    for name in ("amount", "value", "raw"):
        raw = getattr(value, name, None)
        parsed = _decimal_text(raw)
        if parsed is not None:
            return parsed
    return _decimal_text(value)


def _money_currency(value: Any) -> str:
    currency = getattr(value, "currency", None)
    if currency is not None:
        code = getattr(currency, "code", getattr(currency, "value", currency))
        return str(code).strip().upper()
    match = re.search(r"\b(USDT|USDC)\b", str(value).upper())
    if match is None:
        return ""
    return match.group(1)


def _decimal_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    match = re.search(r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", text)
    if match is None:
        return None
    try:
        number = Decimal(match.group(0))
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0:
        return None
    return format(number, "f")


def _event_reduce_only(event: Any) -> bool:
    for holder in (event, getattr(event, "order", None)):
        if holder is None:
            continue
        value = getattr(holder, "reduce_only", None)
        if value is not None:
            return bool(value)
    return False


def _event_fill_id(
    event: Any,
    *,
    client_order_id: str,
    quantity: Optional[str],
    price: Optional[str],
) -> str:
    for holder in (event, getattr(event, "order", None)):
        if holder is None:
            continue
        for name in ("trade_id", "venue_trade_id", "fill_id", "event_id"):
            value = getattr(holder, name, None)
            if value is not None and str(value).strip():
                return str(value).strip()
    material = json.dumps(
        {
            "client_order_id": client_order_id,
            "quantity": quantity or "",
            "price": price or "",
            "ts_event": _event_text_field(
                event,
                "ts_event",
                "timestamp",
                "occurred_at",
            )
            or "",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "fill-" + sha256(material.encode("utf-8")).hexdigest()


def _event_occurred_at(event: Any) -> str:
    raw = _event_text_field(
        event,
        "occurred_at",
        "timestamp",
        "ts_event",
    )
    if raw:
        return raw
    return datetime.now(timezone.utc).isoformat()


def _market_fallback_client_order_id(source_client_order_id: str) -> str:
    source = str(source_client_order_id)
    if source.startswith("B"):
        return "M" + source[1:]
    return ("M" + source)[-36:]


def _protection_plan_key(plan: OrderPlan) -> tuple[str, Optional[str]]:
    role = "stop_loss" if plan.order_type == "STOP_MARKET" else "take_profit"
    return role, str(plan.trigger_price) if role == "take_profit" else None


def _price_key(value: Any) -> str:
    if isinstance(value, dict):
        raw = value.get(
            "trigger_price",
            value.get("price", value.get("limit_price")),
        )
    else:
        raw = value
    return str(raw)


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
    authorization = _authorization_source(intent)
    tags = (
        f"intent_id={getattr(intent, 'intent_id')}",
        f"decision_id={getattr(intent, 'decision_id')}",
        f"risk_decision_id={getattr(intent, 'risk_decision_id')}",
        f"idempotency_key={getattr(intent, 'idempotency_key')}",
        f"action={action}",
        f"account_id={getattr(intent, 'account_id')}",
    )
    if isinstance(authorization, OrderDenied):
        return tags
    tags += (
        f"parent_intent_id={authorization['parent_intent_id']}",
        f"authorized_by_type={authorization['authorized_by_type']}",
        f"authorized_by_id={authorization['authorized_by_id']}",
        f"source_message_id={authorization['source_message_id']}",
    )
    channel_id = authorization.get("channel_id")
    if channel_id:
        tags += (f"channel_id={channel_id}",)
    return tags


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
            price = target.get(
                "trigger_price",
                target.get("price", target.get("limit_price")),
            )
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
    parent_intent_id: str,
    authorization: Any,
    index: Optional[int] = None,
) -> tuple[str, ...]:
    tags = tuple(
        tag for tag in base_tags
        if not tag.startswith("lifecycle_role=")
        and not tag.startswith("position_id=")
        and not tag.startswith("take_profit_index=")
        and not tag.startswith("parent_intent_id=")
        and not tag.startswith("authorized_by_type=")
        and not tag.startswith("authorized_by_id=")
        and not tag.startswith("source_message_id=")
        and not tag.startswith("channel_id=")
    )
    tags += (
        f"lifecycle_role={lifecycle_role}",
        f"parent_intent_id={parent_intent_id}",
    )
    source = authorization if isinstance(authorization, dict) else {}
    for key in (
        "authorized_by_type",
        "authorized_by_id",
        "source_message_id",
        "channel_id",
    ):
        value = source.get(key)
        if value:
            tags += (f"{key}={value}",)
    if position_id is not None:
        tags += (f"position_id={position_id}",)
    if index is not None:
        tags += (f"take_profit_index={index}",)
    return tags


def _tag_value(tags: Iterable[Any], key: str) -> Optional[str]:
    prefix = key + "="
    for tag in tags:
        text = str(tag)
        if text.startswith(prefix):
            value = text.split("=", 1)[1].strip()
            if value:
                return value
    return None


def _authorization_from_tags(tags: Iterable[Any]) -> dict[str, str]:
    source: dict[str, str] = {}
    for key in (
        "authorized_by_type",
        "authorized_by_id",
        "source_message_id",
        "channel_id",
        "decision_id",
        "risk_decision_id",
        "idempotency_key",
    ):
        value = _tag_value(tags, key)
        if value:
            source[key] = value
    parent_intent_id = _tag_value(tags, "parent_intent_id")
    if not parent_intent_id:
        parent_intent_id = _tag_value(tags, "intent_id")
    if parent_intent_id:
        source["parent_intent_id"] = parent_intent_id
    if not _authorization_matches_parent(source, parent_intent_id):
        return {}
    return source


def _authorization_matches_parent(
    authorization: Any,
    parent_intent_id: Any,
) -> bool:
    if not isinstance(authorization, dict):
        return False
    if str(authorization.get("authorized_by_type") or "") not in {
        "user",
        "channel",
    }:
        return False
    if not str(authorization.get("authorized_by_id") or "").strip():
        return False
    if not str(authorization.get("source_message_id") or "").strip():
        return False
    expected_parent = str(parent_intent_id or "")
    actual_parent = str(authorization.get("parent_intent_id") or "")
    if not _valid_uuid_text(expected_parent):
        return False
    return actual_parent == expected_parent


def _valid_take_profit_tombstone(
    tombstone: Any,
    parent_intent_id: Any,
) -> bool:
    if not isinstance(tombstone, dict):
        return False
    if str(tombstone.get("state") or "") not in {
        "cancel_pending",
        "disabled",
    }:
        return False
    expected_parent = str(parent_intent_id or "").strip()
    actual_parent = str(tombstone.get("parent_intent_id") or "").strip()
    if not _valid_uuid_text(expected_parent):
        return False
    if actual_parent != expected_parent:
        return False
    return _authorization_matches_parent(tombstone, expected_parent)


def _stash_protection_authorization(stash: dict[str, Any]) -> dict[str, str]:
    for key in ("take_profit_authorization", "stop_loss_authorization"):
        authorization = stash.get(key)
        if not isinstance(authorization, dict):
            continue
        parent_intent_id = authorization.get("parent_intent_id")
        if _authorization_matches_parent(authorization, parent_intent_id):
            return authorization
    return _authorization_from_tags(stash.get("entry_tags") or ())


def _valid_uuid_text(value: Any) -> bool:
    try:
        UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _node_command_has_authorization(cmd: Any) -> bool:
    args = getattr(cmd, "args", {})
    if not isinstance(args, dict):
        return False
    authorization = args.get("authorization")
    if not isinstance(authorization, dict):
        return False
    authorized_by_type = str(
        authorization.get("authorized_by_type") or ""
    ).strip()
    authorized_by_id = str(
        authorization.get("authorized_by_id") or ""
    ).strip()
    source_message_id = str(
        authorization.get("source_message_id") or ""
    ).strip()
    return (
        authorized_by_type in {"user", "channel"}
        and bool(authorized_by_id)
        and bool(source_message_id)
    )


def _terminal_command_instrument_ids(
    args: dict[str, Any],
) -> tuple[str, ...]:
    raw = args.get("instrument_ids")
    alias = args.get("instruments")
    if raw is not None and alias is not None and raw != alias:
        raise ValueError("instrument scope fields disagree")
    if raw is None:
        raw = alias
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("instrument_ids must be a list")
    result = []
    seen = set()
    for item in raw:
        value = str(item or "").strip()
        if not value:
            raise ValueError(
                "instrument_ids must contain non-empty strings"
            )
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


def _terminal_instrument_matches(
    instrument_id: str,
    instrument_ids: tuple[str, ...],
) -> bool:
    if not instrument_ids:
        return True
    target = _canonical_symbol(instrument_id)
    return any(
        _canonical_symbol(candidate) == target
        for candidate in instrument_ids
    )


def _management_parent_intent_id(plan: ManagementPlan) -> str:
    authorization = dict(plan.authorization or {})
    supplied_parent = str(authorization.get("parent_intent_id") or "").strip()
    if _valid_uuid_text(supplied_parent):
        return supplied_parent
    return str(plan.intent_id)


def _management_operation_ids(
    plan: ManagementPlan,
) -> tuple[str, ...]:
    return (
        encode_client_order_id(
            UUID(str(plan.intent_id)),
            sequence=99,
        ),
    )


def _position_book_from_id(position_id: Any) -> str:
    text = str(position_id or "").strip().upper()
    if text.endswith("-LONG") or text.endswith(":LONG"):
        return "LONG"
    if text.endswith("-SHORT") or text.endswith(":SHORT"):
        return "SHORT"
    return ""


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


def _canonical_symbol(value: Any) -> str:
    return str(value or "").strip().upper().split("-")[0].split(".")[0]


def _positive_canary_decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def _intent_execution_identity(intent: Any) -> IntentExecutionIdentity:
    raw_action = getattr(intent, "action", "")
    action = str(getattr(raw_action, "value", raw_action))
    return IntentExecutionIdentity(
        account_id=str(getattr(intent, "account_id", "")),
        intent_id=str(getattr(intent, "intent_id", "")),
        idempotency_key=str(
            getattr(intent, "idempotency_key", "")
        ),
        instrument_id=str(getattr(intent, "instrument_id", "")),
        action=action,
    ).normalized()


def _intent_execution_payload(intent: Any) -> dict[str, Any]:
    model_dump = getattr(intent, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(mode="json")
        if isinstance(payload, dict):
            return payload
    fields = (
        "schema_version",
        "intent_id",
        "decision_id",
        "risk_decision_id",
        "idempotency_key",
        "account_id",
        "instrument_id",
        "action",
        "valid_until",
        "risk_budget",
        "order_plan",
        "target_position_id",
        "approved_at",
    )
    payload = {}
    for field_name in fields:
        if not hasattr(intent, field_name):
            continue
        payload[field_name] = _jsonable_intent_value(
            getattr(intent, field_name)
        )
    payload.setdefault("schema_version", "1.0")
    return payload


def _jsonable_intent_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _aware_datetime(value).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return _jsonable_intent_value(value.value)
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable_intent_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable_intent_value(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _jsonable_intent_value(model_dump(mode="json"))
    raw_values = getattr(value, "__dict__", None)
    if isinstance(raw_values, dict):
        return {
            str(key): _jsonable_intent_value(item)
            for key, item in raw_values.items()
            if not str(key).startswith("_")
        }
    return value


def _permit_expiry(value: Any) -> datetime | bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    return parsed.astimezone(timezone.utc)


def _parse_live_entry_notional_inventory(
    inventory: Iterable[tuple[Any, Any]],
) -> dict[str, Decimal]:
    caps: dict[str, Decimal] = {}
    for raw_instrument_id, raw_cap in inventory:
        instrument_id = str(raw_instrument_id).strip()
        if not instrument_id:
            raise ValueError(
                "live entry notional inventory instrument must be non-empty"
            )
        if instrument_id in caps:
            raise ValueError(
                f"duplicate live entry notional inventory instrument: {instrument_id}"
            )
        cap = _positive_canary_decimal(raw_cap)
        if cap is None:
            raise ValueError(
                f"live entry notional cap must be positive: {instrument_id}"
            )
        caps[instrument_id] = cap
    return caps


def _fsync_strategy_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _protection_payload_sha256(
    payload: Mapping[str, Any],
) -> str:
    encoded = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


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
