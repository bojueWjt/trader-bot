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
        custom_data = self._build_custom_data(intent)
        data_type = getattr(custom_data, "data_type")
        data = getattr(custom_data, "data")

        publish_data = getattr(self, "publish_data", None)
        if callable(publish_data):
            # TODO(host-verify): confirm Actor.publish_data(DataType, Data) is the
            # preferred Nautilus 1.227.0 path for strategy subscribe_data delivery.
            publish_data(data_type, data)
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

    def _subscribe_execution_topic(self, topic: str) -> None:
        for subscriber in (
            _first_attr(self, ("msgbus", "message_bus", "_msgbus")),
            _first_attr(self, ("trader", "_trader")),
        ):
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
