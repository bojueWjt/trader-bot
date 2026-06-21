from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable, Iterable

try:  # pragma: no cover - Nautilus is unavailable on local dev hosts.
    from nautilus_trader.common.actor import Actor  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover

    class Actor:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs


CustomDataBuilder = Callable[[Any], Any]


DEFAULT_EXECUTION_EVENT_TOPICS: tuple[str, ...] = (
    # TODO(host-verify): confirm Nautilus 1.227.0 execution event topic names and
    # wildcard syntax against the hk wheel. These are intentionally centralized so
    # host validation has one place to adjust if MessageBus uses different topics.
    "events.order.*",
    "events.position.*",
    "events.account.*",
    "events.execution.*",
)


class IntentPublisherActor(Actor):
    """Nautilus ``Actor`` wrapper around the plain ``ApprovedIntentDataClient``."""

    def __init__(
        self,
        intent_data_client: Any,
        *,
        poll_interval_seconds: float = 1.0,
        poll_limit: int = 100,
        wait_ms: int = 0,
        timer_name: str = "approved-intents.poll",
        custom_data_builder: CustomDataBuilder | None = None,
    ) -> None:
        _init_actor_base(self)
        self._intent_data_client = intent_data_client
        self._poll_interval_seconds = poll_interval_seconds
        self._poll_limit = poll_limit
        self._wait_ms = wait_ms
        self._timer_name = timer_name
        self._custom_data_builder = custom_data_builder
        self._attach_to_plain_client_publisher()

    def on_start(self) -> None:
        self._register_poll_timer()

    def poll_once(self) -> int:
        poll_once = getattr(self._intent_data_client, "poll_once", None)
        if not callable(poll_once):
            raise RuntimeError("intent_data_client does not expose poll_once")
        return int(poll_once(limit=self._poll_limit, wait_ms=self._wait_ms))

    def publish(self, intent: Any) -> None:
        # C-08 host-verify fix: deliver the approved intent over the msgbus on the
        # per-account topic the IntentExecutionStrategy subscribes to. publish_data +
        # subscribe_data does not route clientless custom data in Nautilus 1.227.0.
        message_bus = _first_attr(self, ("msgbus", "message_bus", "_msgbus"))
        if message_bus is not None and hasattr(message_bus, "publish"):
            account_id = getattr(intent, "account_id", None)
            message_bus.publish(topic=f"intents.{account_id}", msg=intent)
            return

        self._publish_via_engine_or_bus(intent)

    def _on_poll_timer(self, *_args: Any, **_kwargs: Any) -> None:
        self.poll_once()

    def _register_poll_timer(self) -> None:
        clock = getattr(self, "clock", None)
        if clock is None:
            return
        set_timer = getattr(clock, "set_timer", None)
        if not callable(set_timer):
            return

        interval = timedelta(seconds=self._poll_interval_seconds)
        # TODO(host-verify): confirm the exact Actor.clock.set_timer signature on
        # Nautilus 1.227.0. These variants keep local tests independent from the
        # C-extension signature while failing closed if hk exposes neither form.
        try:
            set_timer(
                name=self._timer_name,
                interval=interval,
                callback=self._on_poll_timer,
            )
            return
        except TypeError:
            pass
        set_timer(self._timer_name, interval, self._on_poll_timer)

    def _attach_to_plain_client_publisher(self) -> None:
        publisher = getattr(self._intent_data_client, "_publisher", None)
        attach = getattr(publisher, "attach", None)
        if callable(attach):
            attach(self)

    def _build_custom_data(self, intent: Any) -> Any:
        if self._custom_data_builder is not None:
            return self._custom_data_builder(intent)
        from intent.custom_data import build_nautilus_custom_data  # type: ignore

        return build_nautilus_custom_data(intent)

    def _publish_via_engine_or_bus(self, intent: Any) -> None:
        from intent.custom_data import NautilusCustomDataPublisher  # type: ignore

        data_engine = _first_attr(self, ("data_engine", "_data_engine"))
        if data_engine is not None:
            NautilusCustomDataPublisher(data_engine=data_engine).publish(intent)
            return

        message_bus = _first_attr(self, ("msgbus", "message_bus", "_msgbus"))
        if message_bus is not None:
            # TODO(host-verify): confirm whether direct MessageBus.publish(CustomData)
            # is accepted for custom data in Nautilus 1.227.0. Actor.publish_data is
            # preferred above when available.
            NautilusCustomDataPublisher(message_bus=message_bus).publish(intent)
            return

        raise RuntimeError("no Nautilus publish_data/data_engine/message_bus available")


class ExecutionProjectionActor(Actor):
    """Nautilus ``Actor`` wrapper around the plain execution ``ProjectionActor``."""

    def __init__(
        self,
        projection_actor: Any,
        *,
        event_topics: Iterable[str] = DEFAULT_EXECUTION_EVENT_TOPICS,
    ) -> None:
        _init_actor_base(self)
        self._projection_actor = projection_actor
        self._event_topics = tuple(event_topics)

    def on_start(self) -> None:
        for topic in self._event_topics:
            self._subscribe_execution_topic(topic)
        flush = getattr(self._projection_actor, "flush", None)
        if callable(flush):
            flush()

    def on_event(self, event: Any) -> Any:
        return self._projection_actor.on_event(event)

    def on_order_event(self, event: Any) -> Any:
        return self.on_event(event)

    def on_position_event(self, event: Any) -> Any:
        return self.on_event(event)

    def on_account_state(self, event: Any) -> Any:
        return self.on_event(event)

    def _on_bus_event(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs:
            event = kwargs.get("event") or kwargs.get("message") or kwargs.get("msg")
            if event is not None:
                return self.on_event(event)
        if not args:
            return None
        return self.on_event(args[-1])

    def _subscription_targets(self) -> tuple[Any, ...]:
        # Resolved at runtime from the registered node. On a bare (unregistered)
        # Actor these are None and subscription is a no-op; Nautilus sets msgbus on
        # register. Extracted as a seam so tests can inject a recording bus without
        # assigning to the read-only ``Actor.msgbus`` property.
        return (
            _first_attr(self, ("msgbus", "message_bus", "_msgbus")),
            _first_attr(self, ("trader", "_trader")),
        )

    def _subscribe_execution_topic(self, topic: str) -> None:
        for subscriber in self._subscription_targets():
            if subscriber is None:
                continue
            subscribe = getattr(subscriber, "subscribe", None)
            if not callable(subscribe):
                continue
            # TODO(host-verify): confirm Nautilus 1.227.0 MessageBus/Trader
            # subscribe signature for execution events in hk.
            try:
                subscribe(topic=topic, handler=self._on_bus_event)
                return
            except TypeError:
                pass
            try:
                subscribe(topic, self._on_bus_event)
                return
            except TypeError:
                pass
            subscribe(topic, callback=self._on_bus_event)
            return


class CommandPollerActor(Actor):
    """Polls operator commands from the control-plane and applies them to the node
    lifecycle (kill-switch path: HALT/RESUME/SET_REDUCING -> trading state). The intent
    consumer already gates new positions on HALTED, so HALT stops new entries at once."""

    def __init__(
        self,
        control_plane: Any,
        lifecycle: Any,
        node_id: str,
        *,
        poll_interval_seconds: float = 2.0,
        timer_name: str = "operator-commands.poll",
    ) -> None:
        _init_actor_base(self)
        self._control_plane = control_plane
        self._lifecycle = lifecycle
        self._node_id = node_id
        self._poll_interval_seconds = poll_interval_seconds
        self._timer_name = timer_name

    def on_start(self) -> None:
        self._register_poll_timer()

    def _register_poll_timer(self) -> None:
        clock = getattr(self, "clock", None)
        set_timer = getattr(clock, "set_timer", None) if clock is not None else None
        if not callable(set_timer):
            return
        interval = timedelta(seconds=self._poll_interval_seconds)
        try:
            set_timer(name=self._timer_name, interval=interval, callback=self._on_poll_timer)
            return
        except TypeError:
            pass
        set_timer(self._timer_name, interval, self._on_poll_timer)

    def _on_poll_timer(self, *_args: Any, **_kwargs: Any) -> None:
        self.poll_once()

    def poll_once(self) -> int:
        # Liveness: refresh the control-plane heartbeat on every tick so
        # node_heartbeats.last_seen_at stays fresh and the system snapshot's
        # stale/missing_nodes gate reflects the node actually being alive. Without
        # this the heartbeat is sent only once at startup and the snapshot goes
        # permanently stale. Failure here must not stop operator-command polling.
        try:
            self._lifecycle.send_heartbeat()
        except Exception:
            pass
        commands = self._control_plane.poll_commands(self._node_id, None)
        for cmd in commands:
            status, error = self._apply(cmd)
            try:
                self._control_plane.ack_command(self._node_id, cmd.command_id, status, error=error)
            except Exception:  # ack failure must not crash the poll loop
                pass
        return len(commands)

    def _apply(self, cmd: Any):
        from execution_domain.control_plane import (  # type: ignore
            CommandAckStatus,
            CommandType,
            TradingState,
        )

        try:
            if cmd.type == CommandType.HALT:
                self._lifecycle.apply_operator_state(TradingState.HALTED, "operator_command")
            elif cmd.type == CommandType.RESUME:
                self._lifecycle.apply_operator_state(TradingState.ACTIVE, "operator_command")
            elif cmd.type == CommandType.SET_REDUCING:
                self._lifecycle.apply_operator_state(TradingState.REDUCING, "operator_command")
            else:
                # cancel_all / close_all need order/position actions on the trader -
                # not yet wired into the node (follow-up).
                return CommandAckStatus.ACCEPTED, "node_action_not_wired"
            return CommandAckStatus.COMPLETED, None
        except Exception as exc:  # e.g. readiness gate on RESUME
            return CommandAckStatus.FAILED, repr(exc)


def _init_actor_base(instance: Actor) -> None:
    try:
        Actor.__init__(instance)
    except TypeError:
        Actor.__init__(instance, config=None)


def _first_attr(source: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None
