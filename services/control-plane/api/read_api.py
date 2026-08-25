"""Control-plane read API.

Serves the real SystemSnapshotV1 from PostgreSQL projections. Reader auth is
fail-closed (tokens come from the environment; if none are configured the endpoint
returns 503 rather than allowing anonymous reads). When the projection store is
unavailable it returns 503 — never fixtures.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from uuid import UUID, uuid4

import psycopg2
from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, StreamingResponse
from psycopg2.extras import RealDictCursor

_PSYCOPG2_DRIVER = psycopg2
_HERE = Path(__file__).resolve().parent
_CONTROL_PLANE = _HERE.parent
_DB = _HERE.parent / "db"
_EXECUTION_DOMAIN = _HERE.parents[2] / "packages" / "execution-domain"
for _module_path in (
    _HERE,
    _CONTROL_PLANE,
    _DB,
    _EXECUTION_DOMAIN,
):
    if str(_module_path) not in sys.path:
        sys.path.insert(0, str(_module_path))

from execution_domain.control_plane import (  # noqa: E402
    portfolio_baseline_sha256,
)
from execution_domain.order_ownership import (  # noqa: E402
    row_is_robot_order,
)
from execution_domain.ownership_ledger import (  # noqa: E402
    OwnershipLedgerError,
    load_robot_owned_balance,
    select_flat_ownership_anchor,
)

from app_roles import (  # noqa: E402
    AppRole,
    build_role_app,
    current_app_role,
    expected_database_role_name,
    resolve_app_role,
    rollback_only_permission_probe,
)
from pools import (  # noqa: E402
    PoolCheckoutTimeout,
    checkout_role_connection,
    close_role_pools,
)

from snapshot import (  # noqa: E402
    DEFAULT_STALENESS_MS,
    _missing_nodes,
    _worst_reconciliation_state,
    build_system_snapshot,
    validate_snapshot,
)

READER_TOKEN_ENV = {
    "SYSTEM_OBSERVER_TOKEN": "system_observer",
    "VIEWER_TOKEN": "viewer",
    "RISK_ADMIN_TOKEN": "risk_admin",
    "REVIEWER_TOKEN": "reviewer",
}

app = FastAPI(title="Hermes control-plane read API", version="contracts-v1")

from settings.router import (  # noqa: E402
    router as order_management_settings_router,
)

app.router.routes.extend(order_management_settings_router.routes)

NODE_COMMAND_POLL_LIMIT = 64
_TRADING_STATE_COMMANDS = frozenset({"HALT", "REDUCE", "RESUME"})
_ACCOUNT_SCOPED_NODE_COMMANDS = _TRADING_STATE_COMMANDS | frozenset(
    {"REFRESH_EVIDENCE"}
)
_COMMAND_TARGET_MAX_AGE_SECONDS = 5.0
_LIVE_EVIDENCE_MAX_AGE_SECONDS = 5.0
_RESUME_MARGIN_EVIDENCE_MAX_AGE_SECONDS = 30.0
_TESTNET_EMERGENCY_CLOSE_EVIDENCE_MAX_AGE_SECONDS = 86_400.0
_LIVE_RECONCILIATION_MAX_LAG_MS = 5_000
_PROTECTIVE_ORDER_TYPES = frozenset(
    {
        "STOP",
        "STOP_MARKET",
        "STOP_LOSS",
        "STOP_LOSS_LIMIT",
        "TAKE_PROFIT",
        "TAKE_PROFIT_MARKET",
        "TAKE_PROFIT_LIMIT",
        "TRAILING_STOP_MARKET",
    }
)
_COMMAND_ACK_STATUSES = frozenset(
    {"accepted", "running", "completed", "failed"}
)
_INCIDENT_SEVERITIES = frozenset({"P0", "P1", "P2"})
_INCIDENT_SEVERITY_RANK = {
    "P0": 0,
    "P1": 1,
    "P2": 2,
}
_INCIDENT_REASON_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_INCIDENT_SUMMARY_MAX_LENGTH = 2_000
_ROLLOUT_ACCOUNTS = (
    "account-a",
    "account-b",
    "account-c",
    "account-d",
)
_ROLLOUT_PHASE_ACCOUNT_A_CANARY = "account_a_canary"
_ROLLOUT_PHASE_ACCOUNT_B = "account_b_rollout"
_ROLLOUT_PHASE_ACCOUNT_C = "account_c_rollout"
_ROLLOUT_PHASE_ACCOUNT_D = "account_d_rollout"
_ROLLOUT_PHASE_FLEET_COMPLETE = "fleet_complete"
_ROLLOUT_PHASE_ABORTED = "aborted"
_ACTIVE_ROLLOUT_PHASES = (
    _ROLLOUT_PHASE_ACCOUNT_A_CANARY,
    _ROLLOUT_PHASE_ACCOUNT_B,
    _ROLLOUT_PHASE_ACCOUNT_C,
    _ROLLOUT_PHASE_ACCOUNT_D,
)
_CANARY_PHASE_BY_ACCOUNT = {
    "account-a": _ROLLOUT_PHASE_ACCOUNT_A_CANARY,
    "account-b": _ROLLOUT_PHASE_ACCOUNT_B,
    "account-c": _ROLLOUT_PHASE_ACCOUNT_C,
    "account-d": _ROLLOUT_PHASE_ACCOUNT_D,
}
_MIGRATION_REBASELINE_REGISTRATION_MODE = "migration_rebaseline_stopped"
_STOPPED_REGISTRATION_MODES = frozenset(
    {"bootstrap", _MIGRATION_REBASELINE_REGISTRATION_MODE}
)


def _role_aware_database_connect(database_url: str):
    role = current_app_role()
    if role is AppRole.ALL:
        return _PSYCOPG2_DRIVER.connect(database_url)
    try:
        return checkout_role_connection(database_url, role)
    except PoolCheckoutTimeout as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{role.value} database pool exhausted",
        ) from exc


class _Psycopg2Facade:
    def __init__(self) -> None:
        self.connect = _role_aware_database_connect


psycopg2 = _Psycopg2Facade()


def _database_connection(database_url: str):
    return psycopg2.connect(database_url)


def _verify_database_session_contract(conn, role: AppRole) -> dict[str, str]:
    expected_database_role = expected_database_role_name(role)
    permission_probe = rollback_only_permission_probe(role)
    if permission_probe is False:
        raise RuntimeError(
            f"{role.value} rollback-only permission probe is unavailable"
        )

    with conn.cursor() as cur:
        cur.execute("SELECT session_user, current_user")
        row = cur.fetchone()
        if row is None or len(row) != 2:
            raise RuntimeError("database role identity query returned no row")
        session_user = str(row[0] or "")
        current_user = str(row[1] or "")
        if (
            session_user != expected_database_role
            or current_user != expected_database_role
        ):
            raise RuntimeError(
                f"{role.value} database role identity mismatch"
            )

        cur.execute("SAVEPOINT control_plane_role_permission_probe")
        forbidden_write_succeeded = False
        try:
            cur.execute(permission_probe)
            forbidden_write_succeeded = True
        except Exception as exc:
            if getattr(exc, "pgcode", "") != "42501":
                cur.execute(
                    "ROLLBACK TO SAVEPOINT "
                    "control_plane_role_permission_probe"
                )
                raise RuntimeError(
                    f"{role.value} database permission probe failed"
                ) from exc
        finally:
            cur.execute(
                "ROLLBACK TO SAVEPOINT "
                "control_plane_role_permission_probe"
            )
            cur.execute(
                "RELEASE SAVEPOINT control_plane_role_permission_probe"
            )
        if forbidden_write_succeeded:
            raise RuntimeError(
                f"{role.value} database role has forbidden write access"
            )

    return {
        "session_user": session_user,
        "current_user": current_user,
        "expected_database_role": expected_database_role,
    }


def _verify_role_database_on_startup(
    role_app: FastAPI,
    role: AppRole,
) -> None:
    configured_role = os.environ.get(
        "CONTROL_PLANE_EXPECT_DATABASE_ROLE",
        "",
    ).strip()
    if not configured_role:
        return
    expected_database_role_name(role)
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required for isolated app role")
    conn = checkout_role_connection(database_url, role)
    try:
        identity = _verify_database_session_contract(conn, role)
    finally:
        conn.close()
    role_app.state.database_identity = identity


def _reader_tokens() -> dict[str, str]:
    tokens = {}
    for env_name, role in READER_TOKEN_ENV.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            tokens[value] = role
    return tokens


def require_reader(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="bearer token required")
    tokens = _reader_tokens()
    if not tokens:
        # fail closed: no reader credentials configured
        raise HTTPException(status_code=503, detail="reader auth not configured")
    role = tokens.get(authorization[len("Bearer "):].strip())
    if not role:
        raise HTTPException(status_code=403, detail="forbidden")
    return role


@app.get("/api/system/snapshot")
def system_snapshot(authorization: str | None = Header(default=None)):
    require_reader(authorization)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="projection store unavailable")
    conn = psycopg2.connect(database_url)
    try:
        snapshot = build_system_snapshot(conn)
        validate_snapshot(snapshot)
        return snapshot
    finally:
        conn.close()


# Dashboard realtime feed (SSE). The v3 SPA opens EventSource("/v1/stream") and
# listens for `dashboard_snapshot` + `heartbeat` events. We stream the same
# SystemSnapshotV1 projection that /api/system/snapshot returns, on a fixed
# interval, plus heartbeats to keep the connection warm through the Caddy proxy.
# Read-only: no trading side effects.
_STREAM_INTERVAL_S = 5.0


def _dashboard_snapshot_payload() -> dict:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("projection store unavailable")
    conn = psycopg2.connect(database_url)
    try:
        snapshot = build_system_snapshot(conn)
        validate_snapshot(snapshot)
        return snapshot
    finally:
        conn.close()


@app.get("/v1/stream")
async def v1_stream(authorization: str | None = Header(default=None)):
    require_reader(authorization)

    async def event_gen():
        while True:
            try:
                snap = await asyncio.to_thread(_dashboard_snapshot_payload)
                data = json.dumps(jsonable_encoder(snap), separators=(",", ":"))
                yield f"event: dashboard_snapshot\ndata: {data}\n\n"
            except Exception as exc:  # keep the stream alive; surface as an SSE error event
                err = json.dumps({"error": str(exc)})
                yield f"event: error\ndata: {err}\n\n"
            beat = json.dumps({"ts": datetime.now(timezone.utc).isoformat()})
            yield f"event: heartbeat\ndata: {beat}\n\n"
            await asyncio.sleep(_STREAM_INTERVAL_S)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _node_auth_bindings() -> dict[str, dict[str, str]] | bool:
    raw = os.environ.get("NAUTILUS_NODE_AUTH_JSON", "").strip()
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="node auth binding config is invalid",
        ) from exc
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(
            status_code=503,
            detail="node auth binding config is empty",
        )

    bindings: dict[str, dict[str, str]] = {}
    token_owners: dict[str, str] = {}
    for raw_node_id, raw_binding in payload.items():
        node_id = str(raw_node_id or "").strip()
        if not node_id or not isinstance(raw_binding, dict):
            raise HTTPException(
                status_code=503,
                detail="node auth binding entry is invalid",
            )
        account_id = str(raw_binding.get("account_id") or "").strip()
        token = str(raw_binding.get("token") or "").strip()
        if not account_id or not token:
            raise HTTPException(
                status_code=503,
                detail="node auth binding identity is incomplete",
            )
        previous_owner = token_owners.get(token)
        if previous_owner:
            raise HTTPException(
                status_code=503,
                detail="node auth tokens must be unique",
            )
        token_owners[token] = node_id
        bindings[node_id] = {
            "account_id": account_id,
            "token": token,
        }
    return bindings


def _node_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="node bearer token required")
    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="node bearer token required")
    return token


def require_node(
    authorization: str | None,
    *,
    node_id: str | None = None,
    account_id: str | None = None,
    x_node_id: str | None = None,
    x_account_id: str | None = None,
) -> str | bool:
    token = _node_bearer_token(authorization)
    bindings = _node_auth_bindings()
    if bindings is False:
        expected = os.environ.get("NAUTILUS_NODE_TOKEN", "").strip()
        if not expected:
            raise HTTPException(status_code=503, detail="node auth not configured")
        if token != expected:
            raise HTTPException(status_code=401, detail="node token required")
        if node_id and x_node_id and node_id != x_node_id:
            raise HTTPException(status_code=403, detail="node identity mismatch")
        if account_id and x_account_id and account_id != x_account_id:
            raise HTTPException(status_code=403, detail="node account mismatch")
        return account_id or False

    requested_node_id = str(node_id or "").strip()
    header_node_id = str(x_node_id or "").strip()
    requested_account_id = str(account_id or "").strip()
    header_account_id = str(x_account_id or "").strip()
    if not requested_node_id or not header_node_id:
        raise HTTPException(status_code=403, detail="node identity is required")
    if requested_node_id != header_node_id:
        raise HTTPException(status_code=403, detail="node identity mismatch")
    binding = bindings.get(requested_node_id)
    if not binding:
        raise HTTPException(status_code=403, detail="unknown node identity")
    if token != binding["token"]:
        raise HTTPException(status_code=401, detail="node token required")
    bound_account_id = binding["account_id"]
    if not requested_account_id or not header_account_id:
        raise HTTPException(status_code=403, detail="node account is required")
    if requested_account_id != header_account_id:
        raise HTTPException(status_code=403, detail="node account mismatch")
    if requested_account_id != bound_account_id:
        raise HTTPException(status_code=403, detail="node account mismatch")
    return bound_account_id


def _nautilus_instrument_id(instr: str | None) -> str | None:
    """A↔B seam: map a bare Binance USDT-M symbol (BTCUSDT) to the Nautilus
    InstrumentId the node's cache uses (BTCUSDT-PERP.BINANCE). Pass through if
    already venue-qualified."""
    if not instr or "." in instr:
        return instr
    return f"{instr}-PERP.BINANCE"


def _binance_mark_price(symbol: str | None) -> float | None:
    """Live futures mark price for sizing a MARKET order that carries no entry price.
    fapi.binance.com is dest-routed via the JP WireGuard tunnel on hk, so the HK 451
    geo-block does not apply. Returns None on fetch failure; stop-loss sizing converts
    that state into a fail-closed 503 response."""
    if not symbol:
        return None
    sym = str(symbol).split("-")[0].split(".")[0].upper()
    import json as _json
    import urllib.request as _url
    url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}"
    for _attempt in range(2):
        try:
            with _url.urlopen(url, timeout=3) as resp:
                data = _json.loads(resp.read())
            price = data.get("markPrice")
            return float(price) if price and float(price) > 0 else None
        except Exception:
            continue
    return None


_MANAGEMENT_ACTIONS = frozenset(
    {
        "close_position",
        "partial_close",
        "move_stop_loss",
        "move_stop_to_entry",
        "replace_take_profits",
        "cancel_order",
    }
)
_EXECUTION_ORDER_PLAN_METADATA = (
    "authorization",
    "attribution",
    "canary_permit",
    "disable_take_profits",
    "equity",
    "live_open_gate",
    "request_semantics",
    "rollout_phase",
)

# zone-ladder v1 已定参数，改动须过再校准。
_ZONE_LADDER_TRANCHES = (
    (1, "t1_near", Decimal("0.55"), Decimal("0")),
    (2, "t2_mid", Decimal("0.30"), Decimal("0.50")),
    (3, "t3_deep", Decimal("0.15"), Decimal("0.85")),
)
_ZONE_LADDER_MIN_WIDTH_FRACTION = Decimal("0.0015")
_ZONE_LADDER_QTY_QUANTUM = Decimal("0.000000000001")


def _preserve_execution_order_plan_metadata(source: dict, target: dict) -> dict:
    for field in _EXECUTION_ORDER_PLAN_METADATA:
        if field in source:
            target[field] = source[field]
    return target


def _execution_order_plan(order_plan: dict | None, risk_budget: dict | None,
                          symbol: str | None = None,
                          action: str | None = None) -> dict:
    """A↔B seam: translate A's semantic order_plan ({side:long/short, entry:{type,price}})
    to B's execution order_plan ({side:buy/sell, type, quantity, ...}). Sizes the order
    from risk_budget.max_notional / price (notional-capped): limit/zone use the entry price;
    a MARKET order with no entry price uses the live Binance mark price. Pass through if
    already in B's shape."""
    op = dict(order_plan or {})
    side = str(op.get("side") or "").lower()
    if op.get("type") and op.get("quantity") is not None and side in ("buy", "sell"):
        return op  # already B execution format
    b_side = {"long": "buy", "buy": "buy", "short": "sell", "sell": "sell"}.get(side, side)
    entry = op.get("entry") or {}
    raw_entry_type = str(entry.get("type") or op.get("type") or "").lower()
    act = str(action or "").lower()
    if act in _MANAGEMENT_ACTIONS:
        out: dict = {"side": b_side}
        if raw_entry_type and raw_entry_type != "none":
            out["type"] = raw_entry_type
            out["time_in_force"] = "IOC" if raw_entry_type == "market" else "GTC"
        if op.get("quantity") is not None:
            out["quantity"] = str(op.get("quantity"))
        if op.get("price") is not None:
            out["price"] = op.get("price")
        if op.get("limit_price") is not None:
            out["limit_price"] = op.get("limit_price")
        if op.get("stop_price") is not None:
            out["stop_price"] = op.get("stop_price")
        elif act == "move_stop_loss" and op.get("stop_loss") is not None:
            out["stop_price"] = op.get("stop_loss")
        if op.get("trigger_price") is not None:
            out["trigger_price"] = op.get("trigger_price")
        if op.get("stop_loss") is not None:
            out["stop_loss"] = op.get("stop_loss")
        if op.get("take_profits") is not None:
            out["take_profits"] = op.get("take_profits")
        if op.get("cancel_client_order_id") is not None:
            out["cancel_client_order_id"] = op.get("cancel_client_order_id")
        if op.get("leverage") is not None:
            out["leverage"] = op.get("leverage")
        # Hedge-mode book hint must survive the A->B translation, otherwise the
        # node planner denies dual-side instruments with position_not_unique
        # (2026-07-12 ETH short SL incident, intent 15748ddd).
        if op.get("position_side") is not None:
            out["position_side"] = op.get("position_side")
        return _preserve_execution_order_plan_metadata(op, out)

    entry_type = raw_entry_type or "market"
    if entry_type == "none":
        entry_type = "market"
    time_in_force = str(
        entry.get("time_in_force")
        or op.get("time_in_force")
        or ""
    ).upper()
    if not time_in_force:
        if entry_type == "market":
            time_in_force = "IOC"
        else:
            time_in_force = "GTC"
    entry_price = entry.get("price") if entry.get("price") is not None else entry.get("price_min")
    price_min = entry.get("price_min")
    price_max = entry.get("price_max")

    single_plan = _single_execution_order_plan(
        op,
        risk_budget,
        symbol,
        b_side,
        entry_type,
        entry_price,
        price_min,
        price_max,
        time_in_force,
    )
    if entry_type == "zone":
        try:
            ladder_plan = _zone_ladder_order_plan(
                op,
                risk_budget,
                symbol,
                b_side,
                price_min,
                price_max,
            )
            if ladder_plan is not None:
                return ladder_plan
        except Exception:
            return single_plan
    return single_plan


def _single_execution_order_plan(
    op: dict,
    risk_budget: dict | None,
    symbol: str | None,
    b_side: str,
    entry_type: str,
    entry_price,
    price_min,
    price_max,
    time_in_force: str,
) -> dict:
    max_notional = (risk_budget or {}).get("max_notional")
    quantity = op.get("quantity")
    # Price used ONLY to size the notional cap into a quantity. limit/zone: the entry price.
    # market (no entry price): the live mark price (fetched over the JP-routed fapi).
    if entry_type == "zone":
        sizing_price = price_min if b_side == "sell" else price_max
    else:
        sizing_price = entry_price
    if quantity is None and sizing_price is None and entry_type == "market" and max_notional:
        sizing_price = _binance_mark_price(symbol)
    if quantity is None and sizing_price and max_notional:
        try:
            quantity = float(max_notional) / float(sizing_price)
        except (TypeError, ValueError, ZeroDivisionError):
            quantity = None
    out: dict = {
        "side": b_side,
        "type": entry_type,
        "time_in_force": time_in_force,
    }
    if quantity is not None:
        out["quantity"] = str(quantity)
    if entry_type in ("limit", "zone") and entry_price is not None:
        out["price"] = entry_price
    if entry_type == "zone":
        if price_min is not None:
            out["price_min"] = price_min
        if price_max is not None:
            out["price_max"] = price_max
    if op.get("stop_loss") is not None:
        out["stop_loss"] = op.get("stop_loss")
    if op.get("take_profits") is not None:
        out["take_profits"] = op.get("take_profits")
    if op.get("leverage") is not None:
        out["leverage"] = op.get("leverage")
    if op.get("expire_hours") is not None and entry_type in ("limit", "zone"):
        out["expire_hours"] = op.get("expire_hours")
    return _preserve_execution_order_plan_metadata(op, out)


def _zone_ladder_order_plan(
    op: dict,
    risk_budget: dict | None,
    symbol: str | None,
    b_side: str,
    price_min,
    price_max,
) -> dict | None:
    if price_min is None or price_max is None:
        return None
    if op.get("stop_loss") is None:
        return None

    min_price = _positive_decimal_or_none(price_min)
    max_price = _positive_decimal_or_none(price_max)
    stop_loss = _positive_decimal_or_none(op.get("stop_loss"))
    max_notional = _positive_decimal_or_none((risk_budget or {}).get("max_notional"))
    if min_price is None or max_price is None or stop_loss is None or max_notional is None:
        return None
    if min_price >= max_price:
        return None

    if b_side == "sell":
        if stop_loss <= max_price:
            return None
        near_edge = min_price
        far_edge = max_price
    elif b_side == "buy":
        if stop_loss >= min_price:
            return None
        near_edge = max_price
        far_edge = min_price
    else:
        return None

    if (max_price - min_price) / near_edge < _ZONE_LADDER_MIN_WIDTH_FRACTION:
        return None

    mark_price = _positive_decimal_or_none(_binance_mark_price(symbol))
    if mark_price is None:
        return None
    if b_side == "sell" and mark_price >= min_price:
        return None
    if b_side == "buy" and mark_price <= max_price:
        return None

    single_qty = max_notional / near_edge
    total_risk = single_qty * abs(near_edge - stop_loss)
    if single_qty <= 0 or total_risk <= 0:
        return None

    raw_tranches: list[tuple[int, str, Decimal, Decimal]] = []
    for seq, tranche_id, weight, depth in _ZONE_LADDER_TRANCHES:
        price = near_edge + (depth * (far_edge - near_edge))
        stop_distance = abs(price - stop_loss)
        if price <= 0 or stop_distance <= 0:
            return None
        quantity = (total_risk * weight) / stop_distance
        if quantity <= 0:
            return None
        raw_tranches.append((seq, tranche_id, price, quantity))

    total_notional = sum(quantity * price for _, _, price, quantity in raw_tranches)
    if total_notional <= 0:
        return None
    scale = Decimal("1")
    if total_notional > max_notional:
        scale = max_notional / total_notional

    tranches = []
    for seq, tranche_id, price, quantity in raw_tranches:
        scaled_quantity = _round_down_ladder_quantity(quantity * scale)
        if scaled_quantity is None or scaled_quantity <= 0:
            return None
        tranches.append(
            {
                "seq": seq,
                "tranche_id": tranche_id,
                "price": float(price),
                "quantity": _format_decimal_plain(scaled_quantity),
            }
        )
    if len(tranches) != len(_ZONE_LADDER_TRANCHES):
        return None

    out: dict = {
        "side": b_side,
        "type": "zone_ladder",
        "time_in_force": "GTC",
        "tranches": tranches,
        "stop_loss": op.get("stop_loss"),
        "take_profits": op.get("take_profits") or [],
        "price_min": price_min,
        "price_max": price_max,
    }
    if op.get("leverage") is not None:
        out["leverage"] = op.get("leverage")
    return _preserve_execution_order_plan_metadata(op, out)


def _positive_decimal_or_none(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number > 0 else None


def _round_down_ladder_quantity(value: Decimal) -> Decimal | None:
    try:
        rounded = (value / _ZONE_LADDER_QTY_QUANTUM).quantize(
            Decimal("1"),
            rounding=ROUND_DOWN,
        ) * _ZONE_LADDER_QTY_QUANTUM
    except (InvalidOperation, ValueError):
        return None
    return rounded if rounded > 0 else None


def _format_decimal_plain(value: Decimal) -> str:
    text = format(value.quantize(_ZONE_LADDER_QTY_QUANTUM), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@app.get("/v1/nodes/{node_id}/intents")
def node_intents(
    node_id: str,
    account_id: str,
    after: str | None = None,
    limit: int = 50,
    authorization: str | None = Header(default=None),
    x_node_id: str | None = Header(default=None),
    x_account_id: str | None = Header(default=None),
    x_redis_fencing_epoch: str | None = Header(default=None),
    x_runtime_generation: str | None = Header(default=None),
    x_lease_fencing_token: str | None = Header(default=None),
):
    """A↔B seam: a node pulls approved ApprovedTradeIntentV1 for its account, cursor-based
    (durable, restart-safe). Only this account's approved intents are returned."""
    require_node(
        authorization,
        node_id=node_id,
        account_id=account_id,
        x_node_id=x_node_id,
        x_account_id=x_account_id,
    )
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="intent store unavailable")
    limit = max(1, min(int(limit), 500))
    params: list = [account_id]
    cursor_clause = ""
    if after:
        ts, _, iid = after.partition("|")
        cursor_clause = " AND (created_at, intent_id) > (%s, %s)"
        params += [ts, iid]
    conn = _database_connection(database_url)
    try:
        with conn.cursor() as cur:
            _require_node_writer(
                cur,
                node_id=node_id,
                account_id=account_id,
                redis_fencing_epoch=x_redis_fencing_epoch,
                runtime_generation=x_runtime_generation,
                lease_fencing_token=x_lease_fencing_token,
            )
            cur.execute(
                "SELECT intent_id, hermes_decision_id, risk_decision_id, schema_version, "
                "account_id, instrument_id, action::text, order_plan, risk_budget, "
                "target_position_id, valid_until, idempotency_key, approved_at, created_at "
                "FROM trade_intents WHERE account_id=%s AND status='approved' AND valid_until > now()"
                + cursor_clause
                + " ORDER BY created_at, intent_id LIMIT %s",
                params + [limit],
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    items = []
    next_cursor = after
    for r in rows:
        (iid_, dec, risk, ver, acct, instr, act, order_plan, risk_budget,
         tpid, valid_until, idem, approved_at, created_at) = r
        cur_str = f"{created_at.isoformat()}|{iid_}"
        items.append({
            "cursor": cur_str,
            "intent": {
                "schema_version": ver, "intent_id": str(iid_), "decision_id": str(dec),
                "risk_decision_id": str(risk), "account_id": acct,
                "instrument_id": _nautilus_instrument_id(instr),
                "action": act,
                "order_plan": _execution_order_plan(order_plan, risk_budget, instr, action=act),
                "risk_budget": risk_budget,
                "target_position_id": tpid,
                "valid_until": valid_until.isoformat() if valid_until else None,
                "idempotency_key": idem,
                "approved_at": approved_at.isoformat() if approved_at else None,
            },
        })
        next_cursor = cur_str
    return {"items": items, "next_cursor": next_cursor}


@app.post("/v1/commands")
def issue_operator_command(
    body: dict = Body(default={}),
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
):
    """Operator audited command (HALT/REDUCE/RESUME/CANCEL_ALL/CLOSE_ALL/REFRESH_EVIDENCE).
    risk_admin only; requires request_id + reason + confirm=true; writes a durable
    audit_events row; issue_command sets risk_state for state commands."""
    role = require_reader(authorization)
    if role != "risk_admin":
        raise HTTPException(status_code=403, detail="risk_admin required")
    command_type = (body.get("type") or body.get("command_type") or "").upper()
    if command_type not in (
        "HALT",
        "REDUCE",
        "RESUME",
        "CANCEL_ALL",
        "CLOSE_ALL",
        "REFRESH_EVIDENCE",
    ):
        raise HTTPException(status_code=400, detail="invalid command_type")
    reason = (body.get("reason") or "").strip()
    request_id = (x_request_id or body.get("request_id") or "").strip()
    if not reason or body.get("confirm") is not True or not request_id:
        raise HTTPException(status_code=400, detail="dangerous op requires request_id + reason + confirm=true")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="command store unavailable")
    _cp = _HERE.parent
    for _p in (_cp, _cp / "commands", _cp / "security", _cp / "db", _cp / "risk", _cp / "risk_state"):
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
    from commands import issue_command
    from audit import dangerous_operation_payload, record_audit_event

    scope = _operator_command_scope(body, request_id, reason)
    target_nodes = _operator_command_targets(body.get("target_nodes"))
    account_id = str(scope.get("account_id") or "").strip()
    if command_type in _ACCOUNT_SCOPED_NODE_COMMANDS and not account_id:
        raise HTTPException(status_code=400, detail="scope.account_id is required")
    if command_type in _ACCOUNT_SCOPED_NODE_COMMANDS and not target_nodes:
        raise HTTPException(
            status_code=400,
            detail="account-scoped commands require target_nodes",
        )
    if (
        command_type in _ACCOUNT_SCOPED_NODE_COMMANDS
        and account_id in _ROLLOUT_ACCOUNTS
        and len(target_nodes) != 1
    ):
        raise HTTPException(
            status_code=400,
            detail=f"{account_id} commands require exactly one target node",
        )
    idempotency_key = body.get("idempotency_key") or request_id
    conn = _database_connection(database_url)
    try:
        if command_type in _ACCOUNT_SCOPED_NODE_COMMANDS:
            with conn.cursor() as cur:
                existing_scope = _operator_command_existing_scope(
                    cur,
                    idempotency_key,
                )
                if existing_scope is not False:
                    scope = existing_scope
                else:
                    _validate_fresh_command_targets(
                        cur,
                        account_id=account_id,
                        target_nodes=target_nodes,
                        require_fresh=command_type
                        not in {"RESUME", "REFRESH_EVIDENCE"},
                    )
                    if command_type == "RESUME":
                        _validate_and_arm_resume(
                            cur,
                            account_id=account_id,
                            node_id=target_nodes[0],
                            scope=scope,
                        )
        result = issue_command(
            conn, command_type=command_type, requested_by="risk_admin", reason=reason,
            idempotency_key=idempotency_key,
            target_nodes=target_nodes, scope=scope,
        )
        record_audit_event(
            conn, event_type="operator_command", aggregate_type="operator_command",
            aggregate_id=result["command_id"], actor="risk_admin",
            payload=dangerous_operation_payload(
                request_id=request_id, reason=reason,
                actor={"actor_id": "risk-admin", "role": "risk_admin"},
                operation=command_type, payload={"target_nodes": target_nodes, "scope": scope},
            ),
        )
        conn.commit()
        return result
    finally:
        conn.close()


@app.get("/v1/commands/{command_id}")
def operator_command_status(
    command_id: str,
    authorization: str | None = Header(default=None),
):
    role = require_reader(authorization)
    if role != "risk_admin":
        raise HTTPException(status_code=403, detail="risk_admin required")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="command store unavailable")
    _cp_paths()
    from commands import CommandError, get_status

    conn = _database_connection(database_url)
    try:
        try:
            return get_status(conn, command_id)
        except CommandError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


def _operator_command_scope(body: dict, request_id: str, reason: str) -> dict:
    scope = dict(body.get("scope") or {})
    scope.pop("live_open_gate", None)
    scope["authorization"] = {
        "authorized_by_type": "user",
        "authorized_by_id": "risk_admin",
        "source_message_id": request_id,
        "created_by_service": "control-plane",
    }
    scope["request_id"] = request_id
    scope["reason"] = reason
    return scope


def _operator_command_targets(raw_targets) -> list[str]:
    if raw_targets is None:
        return []
    if not isinstance(raw_targets, list):
        raise HTTPException(status_code=400, detail="target_nodes must be a list")
    target_nodes = [str(node_id or "").strip() for node_id in raw_targets]
    if any(not node_id for node_id in target_nodes):
        raise HTTPException(
            status_code=400,
            detail="target_nodes contains an empty node_id",
        )
    if len(set(target_nodes)) != len(target_nodes):
        raise HTTPException(
            status_code=400,
            detail="target_nodes must be unique",
        )
    return target_nodes


def _operator_command_existing_scope(
    cur,
    idempotency_key: str,
) -> dict | bool:
    cur.execute(
        "SELECT scope FROM operator_commands WHERE idempotency_key=%s",
        (idempotency_key,),
    )
    row = cur.fetchone()
    if row is None:
        return False
    scope = row[0]
    if not isinstance(scope, dict):
        raise HTTPException(
            status_code=409,
            detail="persisted operator command scope is invalid",
        )
    return dict(scope)


def _validate_fresh_command_targets(
    cur,
    *,
    account_id: str,
    target_nodes: list[str],
    require_fresh: bool,
) -> None:
    freshness_clause = ""
    params: list = [account_id, target_nodes]
    if require_fresh:
        freshness_clause = """
          AND last_seen_at >= (
              now() - make_interval(secs => %s)
          )
        """
        params.append(_command_target_max_age_seconds())
    cur.execute(
        f"""
        SELECT node_id
        FROM node_heartbeats
        WHERE account_id=%s
          AND node_id = ANY(%s)
          {freshness_clause}
        FOR SHARE
        """,
        tuple(params),
    )
    matched_nodes = {str(row[0]) for row in cur.fetchall()}
    if matched_nodes != set(target_nodes):
        raise HTTPException(
            status_code=409,
            detail="target_nodes do not match fresh account binding",
        )


def _command_target_max_age_seconds() -> float:
    return _bounded_positive_env_seconds(
        "CONTROL_PLANE_COMMAND_TARGET_MAX_AGE_SECONDS",
        _COMMAND_TARGET_MAX_AGE_SECONDS,
        maximum=60.0,
    )


def _live_evidence_max_age_seconds() -> float:
    return _bounded_positive_env_seconds(
        "CONTROL_PLANE_LIVE_EVIDENCE_MAX_AGE_SECONDS",
        _LIVE_EVIDENCE_MAX_AGE_SECONDS,
        maximum=60.0,
    )


def _resume_margin_evidence_max_age_seconds() -> float:
    return _bounded_positive_env_seconds(
        "CONTROL_PLANE_RESUME_MARGIN_EVIDENCE_MAX_AGE_SECONDS",
        _RESUME_MARGIN_EVIDENCE_MAX_AGE_SECONDS,
        maximum=120.0,
    )


def _testnet_emergency_close_evidence_max_age_seconds() -> float:
    return _bounded_positive_env_seconds(
        "CONTROL_PLANE_TESTNET_EMERGENCY_CLOSE_EVIDENCE_MAX_AGE_SECONDS",
        _TESTNET_EMERGENCY_CLOSE_EVIDENCE_MAX_AGE_SECONDS,
        maximum=604_800.0,
    )


def _bounded_positive_env_seconds(
    env_name: str,
    default: float,
    *,
    maximum: float,
) -> float:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value <= 0 or value > maximum:
        return default
    return value


def _validate_and_arm_resume(
    cur,
    *,
    account_id: str,
    node_id: str,
    scope: dict,
) -> None:
    cur.execute(
        """
        SELECT fence_id
        FROM control_plane_maintenance_fences
        WHERE domain='trader-v3'
          AND status='active'
          AND expires_at > clock_timestamp()
        LIMIT 1
        FOR SHARE
        """
    )
    if cur.fetchone() is not None:
        raise HTTPException(
            status_code=409,
            detail="RESUME is blocked by active maintenance fence",
        )

    release_id = str(scope.get("release_id") or "").strip()
    if not release_id:
        raise HTTPException(
            status_code=400,
            detail="scope.release_id is required for RESUME",
        )
    rollout = _reviewed_rollout_state(
        cur,
        release_id,
        lock=True,
    )
    live_open_gate = _live_open_gate_from_rollout(rollout)
    if live_open_gate is False:
        raise HTTPException(
            status_code=409,
            detail="reviewed release live open gate is unavailable",
        )

    raw_permit_id = scope.get("canary_permit_id")
    canary_request = _is_canary_request(
        account_id=account_id,
        raw_permit_id=raw_permit_id,
    )
    symbol = _resume_scope_symbol(
        scope,
        required=canary_request,
    )
    required_rollout_phase = None
    if canary_request:
        required_rollout_phase = _canary_phase_for_account(account_id)

    heartbeat = _load_live_heartbeat(
        cur,
        account_id=account_id,
        node_id=node_id,
    )
    _validate_live_heartbeat_evidence(
        cur,
        heartbeat=heartbeat,
        node_id=node_id,
        account_id=account_id,
        symbol=symbol,
        release_id=release_id,
        expected_trading_state="HALTED",
        required_rollout_phase=required_rollout_phase,
        require_reconciliation_health=False,
        require_portfolio_clear=False,
    )
    _validate_owned_orders_terminal(
        cur,
        heartbeat=heartbeat,
        account_id=account_id,
    )
    _validate_margin_ratio_guard(
        cur,
        account_id=account_id,
        database_now=heartbeat["database_now"],
    )
    cur.execute(
        """
        SELECT 1
        FROM production_incidents
        WHERE account_id=%s
          AND severity IN ('P0', 'P1')
          AND status='open'
        LIMIT 1
        """,
        (account_id,),
    )
    if cur.fetchone() is not None:
        raise HTTPException(
            status_code=409,
            detail="account has an open P0/P1 incident",
        )

    scope["live_open_gate"] = live_open_gate
    if not canary_request:
        return

    permit_id = _required_uuid(
        raw_permit_id,
        f"scope.canary_permit_id is required for {account_id} RESUME",
    )
    cur.execute(
        """
        SELECT account_id,
               symbol,
               max_notional_usdt,
               max_cumulative_loss_usdt,
               max_open_count,
               consumed_open_count,
               expires_at,
               release_id,
               testnet_emergency_close_evidence_sha256,
               testnet_emergency_close_verified_at,
               status
        FROM live_canary_permits
        WHERE permit_id=%s
        FOR UPDATE
        """,
        (permit_id,),
    )
    permit = cur.fetchone()
    if permit is None:
        raise HTTPException(status_code=409, detail="canary permit not found")
    (
        permit_account_id,
        permit_symbol,
        max_notional,
        max_cumulative_loss,
        max_open_count,
        consumed_open_count,
        expires_at,
        permit_release_id,
        emergency_close_evidence_sha256,
        emergency_close_verified_at,
        permit_status,
    ) = permit
    if permit_status != "issued":
        raise HTTPException(
            status_code=409,
            detail="canary permit is not available to arm",
        )
    if expires_at is None or expires_at <= heartbeat["database_now"]:
        raise HTTPException(status_code=409, detail="canary permit has expired")
    if (
        permit_account_id != account_id
        or _canonical_symbol(permit_symbol) != symbol
        or permit_release_id != release_id
    ):
        raise HTTPException(
            status_code=409,
            detail="canary permit scope does not match RESUME",
        )
    _validate_canary_permit_machine_evidence(
        max_notional=max_notional,
        max_cumulative_loss=max_cumulative_loss,
        emergency_close_evidence_sha256=emergency_close_evidence_sha256,
        emergency_close_verified_at=emergency_close_verified_at,
        database_now=heartbeat["database_now"],
    )
    _validate_robot_owned_symbol_flat(
        cur,
        account_id=account_id,
        symbol=symbol,
    )
    if int(max_open_count) != 1 or int(consumed_open_count) != 0:
        raise HTTPException(
            status_code=409,
            detail="canary permit limits are invalid",
        )
    portfolio_baseline_sha256 = _portfolio_baseline_sha256_or_unavailable(
        heartbeat,
        symbol,
    )
    cur.execute(
        """
        UPDATE live_canary_permits
        SET status='armed',
            armed_at=now(),
            armed_node_id=%s,
            portfolio_baseline_sha256=%s
        WHERE permit_id=%s
          AND status='issued'
          AND consumed_open_count=0
          AND expires_at > now()
        """,
        (node_id, portfolio_baseline_sha256, permit_id),
    )
    if cur.rowcount != 1:
        raise HTTPException(
            status_code=409,
            detail="canary permit could not be armed",
        )
    scope["canary_permit"] = _canary_permit_downlink_evidence(
        permit_id=permit_id,
        account_id=account_id,
        symbol=symbol,
        release_id=release_id,
        node_id=node_id,
        max_notional=max_notional,
        max_cumulative_loss=max_cumulative_loss,
        expires_at=expires_at,
        emergency_close_evidence_sha256=emergency_close_evidence_sha256,
        emergency_close_verified_at=emergency_close_verified_at,
        portfolio_baseline_sha256=portfolio_baseline_sha256,
    )


def _validate_canary_permit_machine_evidence(
    *,
    max_notional,
    max_cumulative_loss,
    emergency_close_evidence_sha256,
    emergency_close_verified_at,
    database_now: datetime,
) -> None:
    try:
        notional = Decimal(str(max_notional))
        cumulative_loss = Decimal(str(max_cumulative_loss))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="canary permit limits are invalid",
        ) from exc
    if (
        not notional.is_finite()
        or notional <= 0
        or notional > Decimal("12")
        or not cumulative_loss.is_finite()
        or cumulative_loss <= 0
        or cumulative_loss >= Decimal("1.5")
    ):
        raise HTTPException(
            status_code=409,
            detail="canary permit limits are invalid",
        )
    evidence_sha256 = str(emergency_close_evidence_sha256 or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", evidence_sha256) is None:
        raise HTTPException(
            status_code=409,
            detail="canary testnet emergency close evidence is invalid",
        )
    if not _timestamp_is_fresh_with_max_age(
        emergency_close_verified_at,
        database_now,
        _testnet_emergency_close_evidence_max_age_seconds(),
    ):
        raise HTTPException(
            status_code=409,
            detail="canary testnet emergency close evidence is stale",
        )


def _canary_permit_downlink_evidence(
    *,
    permit_id: str,
    account_id: str,
    symbol: str,
    release_id: str,
    node_id: str,
    max_notional,
    max_cumulative_loss,
    expires_at: datetime,
    emergency_close_evidence_sha256,
    emergency_close_verified_at: datetime,
    portfolio_baseline_sha256: str,
) -> dict:
    return {
        "permit_id": permit_id,
        "account_id": account_id,
        "symbol": symbol,
        "target_symbol": symbol,
        "release_id": release_id,
        "node_id": node_id,
        "max_notional_usdt": str(max_notional),
        "max_cumulative_loss_usdt": str(max_cumulative_loss),
        "max_open_count": 1,
        "expires_at": expires_at.isoformat(),
        "testnet_emergency_close_evidence_sha256": str(
            emergency_close_evidence_sha256
        ),
        "testnet_emergency_close_verified_at": (
            emergency_close_verified_at.isoformat()
        ),
        "portfolio_baseline_sha256": portfolio_baseline_sha256,
    }


def _resume_scope_symbol(
    scope: dict,
    *,
    required: bool,
) -> str | None:
    raw_instruments = scope.get("instruments")
    candidates: list[str] = []
    if raw_instruments is not None:
        if not isinstance(raw_instruments, list):
            raise HTTPException(
                status_code=400,
                detail="scope.instruments must be a list",
            )
        candidates.extend(str(value or "") for value in raw_instruments)
    raw_symbol = scope.get("symbol")
    if raw_symbol:
        candidates.append(str(raw_symbol))
    symbols = {_canonical_symbol(value) for value in candidates}
    symbols.discard("")
    if not symbols and not required:
        return None
    if len(symbols) != 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "canary RESUME requires exactly one target symbol"
                if required
                else "RESUME scope must contain at most one target symbol"
            ),
        )
    return next(iter(symbols))


def _canary_phase_for_account(account_id: str) -> str:
    phase = _CANARY_PHASE_BY_ACCOUNT.get(account_id)
    if phase is None:
        raise HTTPException(
            status_code=400,
            detail="canary account is not supported",
        )
    return phase


def _is_canary_request(
    *,
    account_id: str,
    raw_permit_id,
) -> bool:
    del account_id
    return raw_permit_id not in (None, "")


def _required_uuid(value, detail: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail=detail)
    try:
        return str(UUID(raw))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=detail) from exc


def _load_live_heartbeat(
    cur,
    *,
    account_id: str,
    node_id: str,
) -> dict:
    cur.execute(
        """
        SELECT status,
               payload,
               release_id,
               image_digest,
               config_sha256,
               dependency_lock_sha256,
               schema_epoch,
               positions,
               regular_orders,
               algo_orders,
               positions_snapshot_at,
               regular_orders_snapshot_at,
               algo_orders_snapshot_at,
               reconciliation_completed_at,
               redis_fencing_epoch,
               runtime_generation,
               lease_fencing_token,
               heartbeat_sequence,
               last_seen_at,
               clock_timestamp()
        FROM node_heartbeats
        WHERE node_id=%s
          AND account_id=%s
        FOR SHARE
        """,
        (node_id, account_id),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="target_nodes do not match fresh account binding",
        )
    keys = (
        "status",
        "payload",
        "release_id",
        "image_digest",
        "config_sha256",
        "dependency_lock_sha256",
        "schema_epoch",
        "positions",
        "regular_orders",
        "algo_orders",
        "positions_snapshot_at",
        "regular_orders_snapshot_at",
        "algo_orders_snapshot_at",
        "reconciliation_completed_at",
        "redis_fencing_epoch",
        "runtime_generation",
        "lease_fencing_token",
        "heartbeat_sequence",
        "last_seen_at",
        "database_now",
    )
    return dict(zip(keys, row))


def _validate_live_heartbeat_evidence(
    cur,
    *,
    heartbeat: dict,
    node_id: str,
    account_id: str,
    symbol: str | None,
    release_id: str,
    expected_trading_state: str,
    required_rollout_phase: str | None = None,
    require_reconciliation_health: bool = True,
    require_portfolio_clear: bool = True,
) -> None:
    if str(heartbeat["status"] or "").upper() != expected_trading_state:
        raise HTTPException(
            status_code=409,
            detail=f"node must be {expected_trading_state}",
        )
    runtime_generation = str(
        heartbeat.get("runtime_generation") or ""
    ).strip()
    redis_fencing_epoch = str(
        heartbeat.get("redis_fencing_epoch") or ""
    ).strip()
    lease_fencing_token = heartbeat.get("lease_fencing_token")
    heartbeat_sequence = heartbeat.get("heartbeat_sequence")
    if (
        not redis_fencing_epoch
        or not runtime_generation
        or not isinstance(lease_fencing_token, int)
        or lease_fencing_token <= 0
        or not isinstance(heartbeat_sequence, int)
        or heartbeat_sequence <= 0
    ):
        raise HTTPException(
            status_code=409,
            detail="node heartbeat writer identity is invalid",
        )
    active_redis_fencing_epoch = _active_redis_fencing_epoch(
        cur,
        lock=False,
    )
    if active_redis_fencing_epoch is None:
        raise HTTPException(
            status_code=503,
            detail="active redis fencing epoch is unavailable",
        )
    if redis_fencing_epoch != active_redis_fencing_epoch:
        raise HTTPException(
            status_code=409,
            detail="node heartbeat redis fencing epoch is stale",
        )
    payload = heartbeat["payload"]
    if not isinstance(payload, dict):
        raise HTTPException(status_code=409, detail="node readiness evidence is invalid")
    if payload.get("readiness") is not True:
        raise HTTPException(status_code=409, detail="node is not ready")
    try:
        projection_lag_ms = int(payload.get("projection_lag_ms"))
    except (TypeError, ValueError):
        projection_lag_ms = -1
    health_degraded_reasons = payload.get("health_degraded_reasons", [])
    if not isinstance(health_degraded_reasons, list):
        raise HTTPException(
            status_code=409,
            detail="node heartbeat health evidence is invalid",
        )

    now = heartbeat["database_now"]
    if not _timestamp_is_fresh(heartbeat.get("last_seen_at"), now):
        raise HTTPException(
            status_code=409,
            detail="node heartbeat evidence is stale",
        )
    if require_reconciliation_health:
        if (
            projection_lag_ms < 0
            or projection_lag_ms > _LIVE_RECONCILIATION_MAX_LAG_MS
        ):
            raise HTTPException(
                status_code=409,
                detail="node reconciliation evidence is unhealthy",
            )
        if str(payload.get("reconciliation_state") or "").lower() != "healthy":
            raise HTTPException(
                status_code=409,
                detail="node reconciliation evidence is unhealthy",
            )
        if health_degraded_reasons:
            reason = str(health_degraded_reasons[0] or "").strip()
            if not reason:
                reason = "unspecified degradation"
            raise HTTPException(
                status_code=409,
                detail=f"node heartbeat health is degraded: {reason}",
            )
        snapshot_freshness_fields = (
            "positions_snapshot_at",
            "regular_orders_snapshot_at",
            "algo_orders_snapshot_at",
        )
        if any(
            not _timestamp_is_fresh(heartbeat.get(field_name), now)
            for field_name in snapshot_freshness_fields
        ):
            raise HTTPException(
                status_code=409,
                detail="node heartbeat evidence is stale",
            )
        if not _reconciliation_health_is_fresh(payload, now):
            raise HTTPException(
                status_code=409,
                detail="node heartbeat evidence is stale",
            )

    cur.execute(
        """
        SELECT image_digest,
               config_sha256,
               dependency_lock_sha256,
               schema_epoch
        FROM reviewed_release_manifests
        WHERE account_id=%s
          AND release_id=%s
          AND review_status='reviewed'
        """,
        (account_id, release_id),
    )
    manifest = cur.fetchone()
    node_identity = (
        heartbeat.get("image_digest"),
        heartbeat.get("config_sha256"),
        heartbeat.get("dependency_lock_sha256"),
        heartbeat.get("schema_epoch"),
    )
    if (
        manifest is None
        or heartbeat.get("release_id") != release_id
        or tuple(manifest) != node_identity
    ):
        raise HTTPException(
            status_code=409,
            detail="node release identity does not match reviewed manifest",
        )

    release_identity = (
        release_id,
        heartbeat.get("image_digest"),
        heartbeat.get("config_sha256"),
        heartbeat.get("dependency_lock_sha256"),
        heartbeat.get("schema_epoch"),
    )
    if required_rollout_phase is None:
        _validate_fleet_release_ready(
            cur,
            node_id=node_id,
            account_id=account_id,
            trading_state=expected_trading_state,
            release_identity=release_identity,
        )
    else:
        _validate_canary_release_ready(
            cur,
            account_id=account_id,
            required_rollout_phase=required_rollout_phase,
            expected_trading_state=expected_trading_state,
            release_identity=release_identity,
        )

    if not require_portfolio_clear:
        return

    positions = heartbeat.get("positions")
    regular_orders = heartbeat.get("regular_orders")
    algo_orders = heartbeat.get("algo_orders")
    if not all(
        isinstance(snapshot, list)
        for snapshot in (positions, regular_orders, algo_orders)
    ):
        raise HTTPException(
            status_code=409,
            detail="node exchange evidence is invalid",
        )
    if any(
        not isinstance(item, dict) or not _snapshot_item_symbol(item)
        for snapshot in (positions, regular_orders, algo_orders)
        for item in snapshot
    ):
        raise HTTPException(
            status_code=409,
            detail="node exchange evidence is invalid",
        )
    if any(
        _snapshot_has_nonzero_position(item, symbol)
        for item in positions
        if isinstance(item, dict)
    ):
        raise HTTPException(status_code=409, detail="target symbol is not flat")
    if any(
        _snapshot_item_matches_symbol(item, symbol)
        for item in regular_orders
        if isinstance(item, dict)
    ):
        raise HTTPException(
            status_code=409,
            detail="target symbol has regular orders",
        )
    if any(
        _snapshot_item_matches_symbol(item, symbol)
        for item in algo_orders
        if isinstance(item, dict)
    ):
        raise HTTPException(
            status_code=409,
            detail="target symbol has algo orders",
        )


def _timestamp_is_fresh(value, now: datetime) -> bool:
    return _timestamp_is_fresh_with_max_age(
        value,
        now,
        _live_evidence_max_age_seconds(),
    )


def _reconciliation_health_is_fresh(payload: dict, now: datetime) -> bool:
    if str(payload.get("reconciliation_state") or "").lower() != "healthy":
        return False
    raw_ts = payload.get("ts")
    if not raw_ts:
        return False
    try:
        observed = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
    except ValueError:
        return False
    return _timestamp_is_fresh(observed, now)


def _timestamp_is_fresh_with_max_age(
    value,
    now: datetime,
    max_age_seconds: float,
) -> bool:
    if not isinstance(value, datetime) or not isinstance(now, datetime):
        return False
    age_seconds = (now - value).total_seconds()
    return -1.0 <= age_seconds <= max_age_seconds


def _canonical_symbol(value) -> str:
    return str(value or "").strip().upper().split("-")[0].split(".")[0]


def _snapshot_item_matches_symbol(item: dict, symbol: str) -> bool:
    return _snapshot_item_symbol(item) == symbol


def _snapshot_item_symbol(item: dict) -> str:
    for field_name in ("symbol", "instrument_id", "instrument"):
        symbol = _canonical_symbol(item.get(field_name))
        if symbol:
            return symbol
    return ""


def _snapshot_has_nonzero_position(item: dict, symbol: str) -> bool:
    if not _snapshot_item_matches_symbol(item, symbol):
        return False
    raw_quantity = item.get("quantity")
    if raw_quantity is None:
        raw_quantity = item.get("qty")
    if raw_quantity is None:
        return True
    try:
        return Decimal(str(raw_quantity)) != 0
    except InvalidOperation:
        return True


def _validate_owned_orders_terminal(
    cur,
    *,
    heartbeat: dict,
    account_id: str,
) -> None:
    for field_name in ("regular_orders", "algo_orders"):
        snapshot = heartbeat.get(field_name)
        if not isinstance(snapshot, list):
            raise HTTPException(
                status_code=409,
                detail="node exchange evidence is invalid",
            )
        for item in snapshot:
            if not isinstance(item, dict):
                raise HTTPException(
                    status_code=409,
                    detail="node exchange evidence is invalid",
                )
            if not row_is_robot_order(item):
                continue
            if _robot_order_is_resume_exempt(item):
                continue
            raise HTTPException(
                status_code=409,
                detail="robot-owned orders are not terminal",
            )

    # RESUME only observes projection evidence. Row locks would require UPDATE
    # privilege without making exchange or heartbeat evidence atomic.
    cur.execute(
        """
        SELECT client_order_id,
               status,
               order_type,
               reduce_only,
               payload
        FROM orders_projection
        WHERE account_id=%s
          AND client_order_id ~ '^B[0-9a-f]{32}[0-9]{2}$'
        ORDER BY updated_at DESC
        """,
        (account_id,),
    )
    for (
        client_order_id,
        status,
        order_type,
        reduce_only,
        raw_payload,
    ) in cur.fetchall():
        if str(status or "").strip().lower() in _TERMINAL_ORDER_STATES:
            continue
        projection_order = {}
        if isinstance(raw_payload, dict):
            projection_order.update(raw_payload)
        projection_order["client_order_id"] = client_order_id
        projection_order["order_type"] = order_type
        if reduce_only is not None:
            projection_order["reduce_only"] = reduce_only
        if _robot_order_is_resume_exempt(projection_order):
            continue
        raise HTTPException(
            status_code=409,
            detail="robot-owned orders are not terminal",
        )


def _robot_order_is_resume_exempt(row: dict) -> bool:
    if not row_is_robot_order(row):
        return False
    if _explicit_reduce_only(row) is not True:
        return False
    order_kind = str(
        row.get("order_kind") or row.get("orderKind") or ""
    ).strip().lower()
    if order_kind != "algo":
        return False
    raw_order_type = row.get("order_type")
    if raw_order_type is None:
        raw_order_type = row.get("type")
    order_type = str(raw_order_type or "").strip().upper()
    return order_type in _PROTECTIVE_ORDER_TYPES


def _explicit_reduce_only(row: dict) -> bool | None:
    values = []
    for field_name in ("reduce_only", "reduceOnly"):
        if field_name in row:
            values.append(row[field_name])
    if not values:
        return None
    if any(value is False for value in values):
        return False
    if all(value is True for value in values):
        return True
    return False


def _validate_robot_owned_symbol_flat(
    cur,
    *,
    account_id: str,
    symbol: str,
) -> None:
    try:
        select_flat_ownership_anchor(
            cur,
            account_id=account_id,
            candidates=(symbol,),
        )
    except OwnershipLedgerError as exc:
        raise HTTPException(
            status_code=409,
            detail="robot-owned target symbol position is not flat",
        ) from exc


def _robot_owned_symbol_footprint(
    cur,
    *,
    account_id: str,
    symbol: str,
) -> Decimal:
    try:
        balance = load_robot_owned_balance(
            cur,
            account_id=account_id,
            symbol=symbol,
        )
    except OwnershipLedgerError as exc:
        raise HTTPException(
            status_code=409,
            detail="robot-owned fill evidence is invalid",
        ) from exc
    return balance.quantity


def _validate_margin_ratio_guard(
    cur,
    *,
    account_id: str,
    database_now: datetime,
) -> None:
    # Margin evidence is a pure RESUME read. A row lock would require UPDATE
    # privilege while providing no atomicity with the exchange snapshot.
    cur.execute(
        """
        SELECT equity, available_balance, updated_at
        FROM accounts_projection
        WHERE account_id=%s
        LIMIT 1
        """,
        (account_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="account margin evidence is unavailable",
        )
    equity_raw, available_raw, updated_at = row
    try:
        equity = Decimal(str(equity_raw))
        available = Decimal(str(available_raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="account margin evidence is invalid",
        ) from exc
    if (
        not equity.is_finite()
        or not available.is_finite()
        or equity <= 0
        or available < 0
    ):
        raise HTTPException(
            status_code=409,
            detail="account margin evidence is invalid",
        )
    if not isinstance(updated_at, datetime) or not isinstance(
        database_now,
        datetime,
    ):
        raise HTTPException(
            status_code=409,
            detail="account margin evidence is invalid",
        )
    age_seconds = (database_now - updated_at).total_seconds()
    max_age_seconds = _resume_margin_evidence_max_age_seconds()
    if (
        not math.isfinite(age_seconds)
        or age_seconds < -1.0
        or age_seconds > max_age_seconds
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "account margin evidence is stale: "
                f"age_seconds={age_seconds:.3f} "
                f"max_age_seconds={max_age_seconds:.3f}"
            ),
        )
    threshold = _minimum_free_margin_ratio()
    if available / equity < threshold:
        raise HTTPException(
            status_code=409,
            detail="account margin ratio is below threshold",
        )


def _minimum_free_margin_ratio() -> Decimal:
    raw = os.environ.get(
        "CONTROL_PLANE_MIN_FREE_MARGIN_RATIO",
        "0.05",
    ).strip()
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.05")
    if value < 0 or value > 1 or not value.is_finite():
        return Decimal("0.05")
    return value


def _portfolio_baseline_sha256(heartbeat: dict, target_symbol: str) -> str:
    try:
        return portfolio_baseline_sha256(heartbeat, target_symbol)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="node exchange evidence is invalid",
        ) from exc


def _portfolio_baseline_sha256_or_unavailable(
    heartbeat: dict,
    target_symbol: str,
) -> str:
    try:
        return portfolio_baseline_sha256(heartbeat, target_symbol)
    except ValueError:
        identity = "|".join(
            (
                "portfolio-baseline-unavailable",
                str(heartbeat.get("node_id") or ""),
                str(heartbeat.get("heartbeat_sequence") or ""),
                _canonical_symbol(target_symbol),
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()


@app.post("/v1/nodes/{node_id}/events")
def ingest_execution_event(
    node_id: str,
    body: dict = Body(default={}),
    authorization: str | None = Header(default=None),
    x_node_id: str | None = Header(default=None),
    x_account_id: str | None = Header(default=None),
    x_redis_fencing_epoch: str | None = Header(default=None),
    x_runtime_generation: str | None = Header(default=None),
    x_lease_fencing_token: str | None = Header(default=None),
):
    """A↔B seam: a node pushes an ExecutionEventEnvelopeV1; idempotent by event_id.
    Projection hints embedded in payload.{position,order} update the read model."""
    account_id = str(body.get("account_id") or "").strip()
    require_node(
        authorization,
        node_id=node_id,
        account_id=account_id,
        x_node_id=x_node_id,
        x_account_id=x_account_id,
    )
    event_node_id = str(body.get("node_id") or "").strip()
    if event_node_id and event_node_id != node_id:
        raise HTTPException(
            status_code=403,
            detail="execution event node mismatch",
        )
    if not body.get("event_id") or not body.get("event_type"):
        raise HTTPException(status_code=400, detail="event_id and event_type required")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="projection store unavailable")
    _cp_paths()
    from repository import ProjectionWriter

    conn = _database_connection(database_url)
    try:
        with conn.cursor() as cur:
            _require_node_writer(
                cur,
                node_id=node_id,
                account_id=account_id,
                redis_fencing_epoch=x_redis_fencing_epoch,
                runtime_generation=x_runtime_generation,
                lease_fencing_token=x_lease_fencing_token,
            )
        writer = ProjectionWriter(conn)
        event = {**body, "node_id": body.get("node_id") or node_id}
        writer.insert_execution_event(event)
        hints = event.get("payload") or {}
        ev_id, ts = event["event_id"], event.get("ts_event")
        if isinstance(hints.get("position"), dict):
            pos_hint = _normalize_position_hint(
                {**hints["position"], "event_id": ev_id, "ts_event": ts},
                event.get("event_type"),
            )
            if pos_hint is not None:
                writer.upsert_position_projection(pos_hint)
        _derive_projection_from_event(writer, event)
        conn.commit()
        return {"ingested": ev_id, "status": "ok"}
    finally:
        conn.close()


def _cp_paths() -> None:
    """Put the control-plane sibling packages on sys.path (idempotent)."""
    cp = _HERE.parent
    for p in (cp, cp / "commands", cp / "security", cp / "db", cp / "risk", cp / "risk_state"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


# operator_commands.command_type (A) -> node CommandType (B)
_NODE_COMMAND_TYPE_MAP = {
    "HALT": "halt", "RESUME": "resume", "REDUCE": "set_reducing",
    "CANCEL_ALL": "cancel_all", "CLOSE_ALL": "close_all",
    "REFRESH_EVIDENCE": "refresh_evidence",
}
@app.post("/v1/nodes/{node_id}/intents/{intent_id}/ack")
def ack_node_intent(node_id: str, intent_id: str, body: dict = Body(default={}),
                    authorization: str | None = Header(default=None),
                    x_node_id: str | None = Header(default=None),
                    x_account_id: str | None = Header(default=None),
                    x_redis_fencing_epoch: str | None = Header(default=None),
                    x_runtime_generation: str | None = Header(default=None),
                    x_lease_fencing_token: str | None = Header(default=None)):
    """A<->B seam: node acks an intent (received/accepted/executed/rejected/...)."""
    account_id = str(body.get("account_id") or "").strip()
    require_node(
        authorization,
        node_id=node_id,
        account_id=account_id,
        x_node_id=x_node_id,
        x_account_id=x_account_id,
    )
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    _cp_paths()
    from audit import record_audit_event

    status = str(getattr(body.get("status"), "value", body.get("status") or "received")).lower()
    conn = _database_connection(database_url)
    try:
        intent_status = None
        with conn.cursor() as cur:
            _require_node_writer(
                cur,
                node_id=node_id,
                account_id=account_id,
                redis_fencing_epoch=x_redis_fencing_epoch,
                runtime_generation=x_runtime_generation,
                lease_fencing_token=x_lease_fencing_token,
            )
        if status in ("rejected", "expired"):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE trade_intents SET status=%s "
                    "WHERE intent_id=%s AND account_id=%s AND status='approved' "
                    "RETURNING status::text",
                    (status, intent_id, account_id),
                )
                row = cur.fetchone()
                if row is not None:
                    intent_status = row[0]
                else:
                    cur.execute(
                        "SELECT status::text FROM trade_intents "
                        "WHERE intent_id=%s AND account_id=%s",
                        (intent_id, account_id),
                    )
                    row = cur.fetchone()
                    intent_status = row[0] if row is not None else None
        record_audit_event(
            conn, event_type=f"intent_ack.{status}", aggregate_type="trade_intent",
            aggregate_id=intent_id, actor=f"node:{node_id}",
            payload={"status": status, "detail": body.get("detail"), "account_id": body.get("account_id")},
        )
        conn.commit()
        result = {"ok": True}
        if intent_status is not None:
            result["intent_status"] = intent_status
        return result
    finally:
        conn.close()


# Nautilus enum ints: OrderSide BUY=1/SELL=2 ; PositionSide FLAT=1/LONG=2/SHORT=3.
_ORDER_SIDE = {
    1: "long", 2: "short",
    "BUY": "long", "SELL": "short", "buy": "long", "sell": "short",
    "LONG": "long", "SHORT": "short", "long": "long", "short": "short",
}
_POSITION_SIDE = {1: "flat", 2: "long", 3: "short", "FLAT": "flat", "flat": "flat",
                  "LONG": "long", "SHORT": "short", "long": "long", "short": "short"}


def _num(value):
    """Coerce a possibly-string/None numeric payload field to float, else None.
    Required because repository upserts wrap quantities in COALESCE(%s, 0): a
    string like '0.0008' would be cast to integer and raise InvalidTextRepresentation."""
    if value is None:
        return None
    try:
        return float(str(value).split()[0])  # tolerates '−0.02 USDT' style suffixes
    except (ValueError, IndexError):
        return None


def _enum_key(value):
    if hasattr(value, "value"):
        value = value.value
    if hasattr(value, "name"):
        value = value.name
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
        return stripped
    return value


def _bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("true", "t", "1", "yes", "y"):
        return True
    if text in ("false", "f", "0", "no", "n"):
        return False
    return None


def _position_side(value) -> str | None:
    return _POSITION_SIDE.get(_enum_key(value))


def _order_side(value) -> str | None:
    return _ORDER_SIDE.get(_enum_key(value))


def _canonical_position_id(instrument_id: str | None, side: str | None,
                           fallback: str | None = None) -> str | None:
    if instrument_id and side in ("long", "short"):
        return f"{instrument_id}-{side.upper()}"
    return fallback


def _normalize_position_hint(hint: dict, event_type: str | None = None) -> dict | None:
    out = dict(hint or {})
    instrument_id = out.get("instrument_id")
    side = _position_side(out.get("side") or out.get("position_side")) or out.get("side")
    if side not in ("long", "short", "flat"):
        side = "long"
    original_position_id = out.get("position_id")
    canonical_id = _canonical_position_id(instrument_id, side, original_position_id)
    if canonical_id is None:
        return None
    qty = abs(_num(out.get("quantity")) or 0.0)
    status = out.get("status")
    if event_type == "PositionClosed" or qty == 0 or side == "flat":
        status = "closed"
    else:
        status = status or "open"
    out["position_id"] = canonical_id
    out["side"] = "long" if side == "flat" else side
    out["quantity"] = qty
    out["status"] = status
    if original_position_id and original_position_id != canonical_id:
        payload = dict(out.get("payload") or {})
        payload.setdefault("node_position_id", original_position_id)
        out["payload"] = payload
    return out


def _normalize_order_hint(hint: dict) -> dict | None:
    out = dict(hint or {})
    if not out.get("client_order_id"):
        return None
    side = _order_side(out.get("side") or out.get("order_side"))
    if side is not None:
        out["side"] = side
    if out.get("order_type") is not None:
        out["order_type"] = str(out.get("order_type"))
    for src, dst in (("price", "price"), ("trigger_price", "trigger_price"),
                     ("quantity", "quantity"), ("filled_quantity", "filled_quantity")):
        if src in out:
            out[dst] = _num(out.get(src))
    if "reduce_only" in out:
        out["reduce_only"] = _bool(out.get("reduce_only"))
    return out


def _position_projection_from_event(ev: dict) -> dict | None:
    et = str(ev.get("event_type") or "")
    p = ev.get("payload") or {}
    acct = ev.get("account_id")
    instrument_id = p.get("instrument_id")
    if not acct or not et.startswith("Position") or not instrument_id:
        return None
    qty = abs(_num(p.get("quantity")) or 0.0)
    hint = {
        "account_id": acct,
        "position_id": p.get("position_id"),
        "instrument_id": instrument_id,
        "side": p.get("side") or p.get("position_side"),
        "quantity": qty,
        "avg_entry_price": _num(p.get("avg_entry_price")) or _num(p.get("last_px")),
        "mark_price": _num(p.get("mark_price")),
        "unrealized_pnl": _num(p.get("unrealized_pnl")),
        "status": "closed" if (et == "PositionClosed" or qty == 0) else "open",
        "event_id": ev.get("event_id"),
        "ts_event": ev.get("ts_event"),
        "payload": p,
    }
    return _normalize_position_hint(hint, et)


def _order_projection_from_event(ev: dict) -> dict | None:
    et = str(ev.get("event_type") or "")
    p = ev.get("payload") or {}
    acct = ev.get("account_id")
    cid = ev.get("client_order_id") or p.get("client_order_id")
    if not acct or not et.startswith("Order") or not cid:
        return None
    fill_qty = _num(p.get("last_qty")) or _num(p.get("filled_qty"))
    hint = {
        "account_id": acct,
        "instrument_id": p.get("instrument_id"),
        "client_order_id": cid,
        "venue_order_id": ev.get("venue_order_id") or p.get("venue_order_id"),
        "status": (et[5:].lower() or "submitted"),
        "side": p.get("side") or p.get("order_side"),
        "order_type": p.get("order_type"),
        "quantity": _num(p.get("quantity")) or fill_qty,
        "filled_quantity": fill_qty,
        "price": p.get("price"),
        "trigger_price": p.get("trigger_price"),
        "reduce_only": p.get("reduce_only"),
        "event_id": ev.get("event_id"),
        "ts_event": ev.get("ts_event"),
        "payload": p,
    }
    return _normalize_order_hint(hint)


def _derive_projection_from_event(writer, ev: dict) -> None:
    """Derive guarded read-model projections from one raw execution event."""
    et = str(ev.get("event_type") or "")
    payload = ev.get("payload") or {}
    if not ev.get("account_id"):
        return
    position_hint = _position_projection_from_event(ev)
    if position_hint is not None:
        writer.upsert_position_projection(position_hint)
        return
    if not et.startswith("Order"):
        return

    order_payload = dict(payload)
    nested_order = payload.get("order")
    if isinstance(nested_order, dict):
        order_payload.update(nested_order)
    client_order_id = ev.get("client_order_id") or order_payload.get("client_order_id")
    venue_order_id = ev.get("venue_order_id") or order_payload.get("venue_order_id")
    if not client_order_id and not venue_order_id:
        return

    side = _order_side(order_payload.get("side") or order_payload.get("order_side"))
    if side is not None:
        order_payload["side"] = side
    if order_payload.get("order_type") is not None:
        order_payload["order_type"] = str(order_payload.get("order_type"))
    if "reduce_only" in order_payload:
        order_payload["reduce_only"] = _bool(order_payload.get("reduce_only"))

    from order_management.order_reducer import OrderProjectionReducer

    reducer_event = {
        **ev,
        "client_order_id": client_order_id,
        "venue_order_id": venue_order_id,
        "payload": order_payload,
    }
    OrderProjectionReducer().apply_event(
        writer.conn,
        reducer_event,
        manage_transaction=False,
    )


@app.post("/v1/nodes/{node_id}/execution-events")
def post_node_events(node_id: str, body: dict = Body(default={}),
                     authorization: str | None = Header(default=None),
                     x_node_id: str | None = Header(default=None),
                     x_account_id: str | None = Header(default=None),
                     x_redis_fencing_epoch: str | None = Header(default=None),
                     x_runtime_generation: str | None = Header(default=None),
                     x_lease_fencing_token: str | None = Header(default=None)):
    """A<->B seam: node pushes a batch of ExecutionEventEnvelopeV1; idempotent by event_id."""
    account_id = str(body.get("account_id") or "").strip()
    require_node(
        authorization,
        node_id=node_id,
        account_id=account_id,
        x_node_id=x_node_id,
        x_account_id=x_account_id,
    )
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    _cp_paths()
    from repository import ProjectionWriter

    conn = _database_connection(database_url)
    acked: list[str] = []
    try:
        with conn.cursor() as cur:
            _require_node_writer(
                cur,
                node_id=node_id,
                account_id=account_id,
                redis_fencing_epoch=x_redis_fencing_epoch,
                runtime_generation=x_runtime_generation,
                lease_fencing_token=x_lease_fencing_token,
            )
        writer = ProjectionWriter(conn)
        for ev in body.get("events", []):
            if not ev.get("event_id"):
                continue
            if str(ev.get("account_id") or "").strip() != account_id:
                raise HTTPException(
                    status_code=403,
                    detail="execution event account mismatch",
                )
            event_node_id = str(ev.get("node_id") or "").strip()
            if event_node_id and event_node_id != node_id:
                raise HTTPException(
                    status_code=403,
                    detail="execution event node mismatch",
                )
            # Persist the raw event first (idempotent, outside the savepoint) so it is
            # always durable even if projection derivation fails on a malformed payload.
            event = {**ev, "node_id": ev.get("node_id") or node_id}
            writer.insert_execution_event(event)
            hints = event.get("payload") or {}
            ev_id, ts = event["event_id"], event.get("ts_event")
            with conn.cursor() as sp:
                sp.execute("SAVEPOINT proj")
            try:
                if isinstance(hints.get("position"), dict):
                    pos_hint = _normalize_position_hint(
                        {**hints["position"], "event_id": ev_id, "ts_event": ts},
                        event.get("event_type"),
                    )
                    if pos_hint is not None:
                        writer.upsert_position_projection(pos_hint)
                _derive_projection_from_event(writer, event)
                with conn.cursor() as sp:
                    sp.execute("RELEASE SAVEPOINT proj")
            except Exception:
                # One bad payload must not abort the batch: roll back just this
                # event's projection writes; the raw event above stays committed.
                with conn.cursor() as sp:
                    sp.execute("ROLLBACK TO SAVEPOINT proj")
            acked.append(str(ev["event_id"]))
        conn.commit()
        return {"acked_event_ids": acked}
    finally:
        conn.close()


@app.post("/v1/nodes/{node_id}/heartbeat")
def node_heartbeat(node_id: str, body: dict = Body(default={}),
                   authorization: str | None = Header(default=None),
                   x_node_id: str | None = Header(default=None),
                   x_account_id: str | None = Header(default=None),
                   x_redis_fencing_epoch: str | None = Header(default=None),
                   x_runtime_generation: str | None = Header(default=None),
                   x_lease_fencing_token: str | None = Header(default=None)):
    """A<->B seam: node liveness + readiness; feeds snapshot freshness/missing_nodes."""
    account_id = str(body.get("account_id") or "").strip()
    bound_account_id = require_node(
        authorization,
        node_id=node_id,
        account_id=account_id,
        x_node_id=x_node_id,
        x_account_id=x_account_id,
    )
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    from psycopg2.extras import Json

    positions = _heartbeat_snapshot(body, "positions")
    regular_orders = _heartbeat_snapshot(
        body,
        "regular_orders",
        fallback_field="open_orders",
    )
    algo_orders = _heartbeat_snapshot(body, "algo_orders")
    positions_snapshot_at = _heartbeat_timestamp(body, "positions_snapshot_at")
    regular_orders_snapshot_at = _heartbeat_timestamp(
        body,
        "regular_orders_snapshot_at",
    )
    algo_orders_snapshot_at = _heartbeat_timestamp(
        body,
        "algo_orders_snapshot_at",
    )
    reconciliation_completed_at = _heartbeat_timestamp(
        body,
        "reconciliation_completed_at",
    )
    health_degraded_reasons = _heartbeat_health_degraded_reasons(body)
    release_id = _optional_identity(body.get("release_id"))
    image_digest = _optional_identity(body.get("image_digest"))
    config_sha256 = _optional_identity(body.get("config_sha256"))
    dependency_lock_sha256 = _optional_identity(
        body.get("dependency_lock_sha256")
    )
    schema_epoch = _optional_identity(body.get("schema_epoch"))
    release_identity_present = any(
        value
        for value in (
            release_id,
            image_digest,
            config_sha256,
            dependency_lock_sha256,
            schema_epoch,
        )
    )
    exchange_evidence_complete = (
        _heartbeat_exchange_evidence_is_complete(body)
    )
    if release_identity_present and not exchange_evidence_complete:
        health_degraded_reasons.append(
            "node_exchange_evidence_missing"
        )
    exchange_evidence_accepted = exchange_evidence_complete
    (
        redis_fencing_epoch,
        runtime_generation,
        lease_fencing_token,
        heartbeat_sequence,
    ) = _heartbeat_writer_identity(
        body,
        required=release_identity_present,
    )
    payload = {
        key: body.get(key)
        for key in (
            "readiness",
            "projection_lag_ms",
            "reconciliation_state",
            "last_event_id",
            "ts",
            "open_orders",
        )
    }
    payload["health_degraded_reasons"] = health_degraded_reasons
    conn = _database_connection(database_url)
    try:
        with conn.cursor() as cur:
            _validate_heartbeat_writer_headers(
                body_redis_fencing_epoch=redis_fencing_epoch,
                body_runtime_generation=runtime_generation,
                body_lease_fencing_token=lease_fencing_token,
                header_redis_fencing_epoch=x_redis_fencing_epoch,
                header_runtime_generation=x_runtime_generation,
                header_lease_fencing_token=x_lease_fencing_token,
            )
            active_redis_fencing_epoch = _active_redis_fencing_epoch(
                cur,
                lock=True,
            )
            if active_redis_fencing_epoch is None:
                if redis_fencing_epoch is not None:
                    raise HTTPException(
                        status_code=503,
                        detail="active redis fencing epoch is unavailable",
                    )
            else:
                if redis_fencing_epoch is None:
                    _raise_writer_fence(
                        "node writer identity is required"
                    )
                if redis_fencing_epoch != active_redis_fencing_epoch:
                    _raise_writer_fence(
                        "redis fencing epoch mismatch"
                    )
            if release_identity_present and exchange_evidence_complete:
                cur.execute("SELECT now()")
                database_now = cur.fetchone()[0]
                try:
                    _validate_live_heartbeat_exchange_evidence(
                        positions=positions,
                        regular_orders=regular_orders,
                        algo_orders=algo_orders,
                        positions_snapshot_at=positions_snapshot_at,
                        regular_orders_snapshot_at=regular_orders_snapshot_at,
                        algo_orders_snapshot_at=algo_orders_snapshot_at,
                        database_now=database_now,
                    )
                except HTTPException as exc:
                    detail = str(exc.detail or "")
                    if (
                        exc.status_code == 409
                        and detail.startswith("node exchange evidence ")
                    ):
                        exchange_evidence_accepted = False
                        health_degraded_reasons.append(
                            detail.replace(" ", "_")
                        )
                    else:
                        raise
            payload["health_degraded_reasons"] = health_degraded_reasons
            cur.execute(
                """
                INSERT INTO node_heartbeats (
                    node_id,
                    account_id,
                    status,
                    version,
                    payload,
                    release_id,
                    image_digest,
                    config_sha256,
                    dependency_lock_sha256,
                    schema_epoch,
                    positions,
                    regular_orders,
                    algo_orders,
                    positions_snapshot_at,
                    regular_orders_snapshot_at,
                    algo_orders_snapshot_at,
                    reconciliation_completed_at,
                    redis_fencing_epoch,
                    runtime_generation,
                    lease_fencing_token,
                    heartbeat_sequence,
                    last_seen_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, now()
                )
                ON CONFLICT (node_id) DO UPDATE SET
                    account_id=EXCLUDED.account_id,
                    status=EXCLUDED.status,
                    version=EXCLUDED.version,
                    payload=EXCLUDED.payload,
                    release_id=EXCLUDED.release_id,
                    image_digest=EXCLUDED.image_digest,
                    config_sha256=EXCLUDED.config_sha256,
                    dependency_lock_sha256=EXCLUDED.dependency_lock_sha256,
                    schema_epoch=EXCLUDED.schema_epoch,
                    positions=CASE
                      WHEN %s THEN EXCLUDED.positions
                      ELSE node_heartbeats.positions
                    END,
                    regular_orders=CASE
                      WHEN %s THEN EXCLUDED.regular_orders
                      ELSE node_heartbeats.regular_orders
                    END,
                    algo_orders=CASE
                      WHEN %s THEN EXCLUDED.algo_orders
                      ELSE node_heartbeats.algo_orders
                    END,
                    positions_snapshot_at=CASE
                      WHEN %s THEN EXCLUDED.positions_snapshot_at
                      ELSE node_heartbeats.positions_snapshot_at
                    END,
                    regular_orders_snapshot_at=CASE
                      WHEN %s THEN EXCLUDED.regular_orders_snapshot_at
                      ELSE node_heartbeats.regular_orders_snapshot_at
                    END,
                    algo_orders_snapshot_at=CASE
                      WHEN %s THEN EXCLUDED.algo_orders_snapshot_at
                      ELSE node_heartbeats.algo_orders_snapshot_at
                    END,
                    reconciliation_completed_at=EXCLUDED.reconciliation_completed_at,
                    redis_fencing_epoch=EXCLUDED.redis_fencing_epoch,
                    runtime_generation=EXCLUDED.runtime_generation,
                    lease_fencing_token=EXCLUDED.lease_fencing_token,
                    heartbeat_sequence=EXCLUDED.heartbeat_sequence,
                    last_seen_at=now()
                WHERE (
                    (
                        node_heartbeats.redis_fencing_epoch IS NULL
                        AND
                        node_heartbeats.runtime_generation IS NULL
                        AND node_heartbeats.lease_fencing_token IS NULL
                        AND node_heartbeats.heartbeat_sequence IS NULL
                    )
                    OR (
                        node_heartbeats.redis_fencing_epoch
                            = EXCLUDED.redis_fencing_epoch
                        AND
                        node_heartbeats.runtime_generation
                            = EXCLUDED.runtime_generation
                        AND node_heartbeats.lease_fencing_token
                            = EXCLUDED.lease_fencing_token
                        AND EXCLUDED.heartbeat_sequence
                            > node_heartbeats.heartbeat_sequence
                    )
                    OR (
                        node_heartbeats.redis_fencing_epoch
                            = EXCLUDED.redis_fencing_epoch
                        AND
                        node_heartbeats.runtime_generation
                            IS DISTINCT FROM EXCLUDED.runtime_generation
                        AND EXCLUDED.lease_fencing_token
                            > COALESCE(
                                node_heartbeats.lease_fencing_token,
                                0
                            )
                    )
                    OR (
                        node_heartbeats.redis_fencing_epoch
                            IS DISTINCT FROM EXCLUDED.redis_fencing_epoch
                    )
                )
                """,
                (
                    node_id,
                    bound_account_id,
                    str(body.get("trading_state") or "UNKNOWN"),
                    body.get("version"),
                    Json(payload),
                    release_id,
                    image_digest,
                    config_sha256,
                    dependency_lock_sha256,
                    schema_epoch,
                    Json(positions),
                    Json(regular_orders),
                    Json(algo_orders),
                    positions_snapshot_at,
                    regular_orders_snapshot_at,
                    algo_orders_snapshot_at,
                    reconciliation_completed_at,
                    redis_fencing_epoch,
                    runtime_generation,
                    lease_fencing_token,
                    heartbeat_sequence,
                    exchange_evidence_accepted,
                    exchange_evidence_accepted,
                    exchange_evidence_accepted,
                    exchange_evidence_accepted,
                    exchange_evidence_accepted,
                    exchange_evidence_accepted,
                ),
            )
            if cur.rowcount != 1:
                _raise_writer_fence("stale heartbeat writer")
            if exchange_evidence_complete:
                _revoke_changed_portfolio_baselines(
                    cur,
                    account_id=bound_account_id,
                    node_id=node_id,
                    heartbeat={
                        "positions": positions,
                        "regular_orders": regular_orders,
                        "algo_orders": algo_orders,
                    },
                )
            receipt = _heartbeat_release_receipt(
                cur,
                node_id=node_id,
                account_id=str(bound_account_id or account_id),
                trading_state=str(
                    body.get("trading_state") or "UNKNOWN"
                ).upper(),
                redis_fencing_epoch=redis_fencing_epoch,
                release_identity=(
                    release_id,
                    image_digest,
                    config_sha256,
                    dependency_lock_sha256,
                    schema_epoch,
                ),
            )
        conn.commit()
        return receipt
    finally:
        conn.close()


def _heartbeat_release_receipt(
    cur,
    *,
    node_id: str,
    account_id: str,
    trading_state: str,
    redis_fencing_epoch: str | None,
    release_identity: tuple[
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
    ],
) -> dict:
    (
        release_id,
        image_digest,
        config_sha256,
        dependency_lock_sha256,
        schema_epoch,
    ) = release_identity
    cur.execute(
        """
        SELECT release_id,
               image_digest,
               config_sha256,
               dependency_lock_sha256,
               schema_epoch,
               review_status
        FROM reviewed_release_manifests
        WHERE account_id=%s
          AND release_id=%s
        """,
        (account_id, release_id),
    )
    manifest_row = cur.fetchone()
    reviewed_manifest: dict | bool = False
    release_gate_status = "missing"
    if manifest_row is not None:
        reviewed_manifest = _release_identity_payload(manifest_row[:5])
        review_status = str(manifest_row[5] or "").strip().lower()
        if review_status != "reviewed":
            release_gate_status = review_status or "missing"
        elif tuple(manifest_row[:5]) == release_identity:
            release_gate_status = "pass"
        else:
            release_gate_status = "drift"

    rollout = _reviewed_rollout_state(cur, release_id)
    awaiting_rollout_step = False
    if rollout is None and release_gate_status == "pass":
        active_rollout = _current_reviewed_rollout_state(cur)
        if (
            active_rollout is not None
            and _account_awaits_rollout_step(
                account_id=account_id,
                rollout_phase=active_rollout["phase"],
            )
            and active_rollout["redis_fencing_epoch"]
            == str(redis_fencing_epoch or "")
        ):
            rollout = active_rollout
            awaiting_rollout_step = True
    rollout_phase = None
    live_open_mode = None
    phase_version = None
    if rollout is not None:
        rollout_phase = rollout["phase"]
        phase_version = rollout["phase_version"]
        live_open_gate = _live_open_gate_from_rollout(rollout)
        if live_open_gate is not False:
            live_open_mode = live_open_gate["mode"]
        rollout_identity = (
            rollout["release_id"],
            rollout["image_digest"],
            rollout["config_sha256"],
            rollout["dependency_lock_sha256"],
            rollout["schema_epoch"],
        )
        if (
            not awaiting_rollout_step
            and release_gate_status == "pass"
            and rollout_identity != release_identity
        ):
            release_gate_status = "drift"
        if (
            release_gate_status == "pass"
            and str(rollout.get("redis_fencing_epoch") or "")
            != str(redis_fencing_epoch or "")
        ):
            release_gate_status = "drift"
    if (
        release_gate_status == "pass"
        and rollout_phase == _ROLLOUT_PHASE_ABORTED
    ):
        release_gate_status = "aborted"
    if release_gate_status == "pass" and rollout is None:
        release_gate_status = "missing"

    return {
        "ok": True,
        "release_gate": {
            "status": release_gate_status,
            "release_id": release_id or False,
            "reviewed_manifest": reviewed_manifest,
            "rollout_phase": rollout_phase or False,
            "live_open_mode": live_open_mode or False,
            "phase_version": phase_version or False,
        },
        "peers": _heartbeat_peer_receipts(
            cur,
            node_id=node_id,
            account_id=account_id,
            trading_state=trading_state,
            redis_fencing_epoch=redis_fencing_epoch,
            release_identity=release_identity,
            rollout_phase=rollout_phase,
        ),
    }


@app.post("/v1/nodes/{node_id}/incidents")
def report_node_incident(
    node_id: str,
    body: dict = Body(default={}),
    authorization: str | None = Header(default=None),
    x_node_id: str | None = Header(default=None),
    x_account_id: str | None = Header(default=None),
    x_redis_fencing_epoch: str | None = Header(default=None),
    x_runtime_generation: str | None = Header(default=None),
    x_lease_fencing_token: str | None = Header(default=None),
):
    account_id = str(body.get("account_id") or "").strip()
    if _node_auth_bindings() is False:
        raise HTTPException(
            status_code=503,
            detail=(
                "node identity bindings are required "
                "for incident reporting"
            ),
        )
    bound_account_id = require_node(
        authorization,
        node_id=node_id,
        account_id=account_id,
        x_node_id=x_node_id,
        x_account_id=x_account_id,
    )
    severity = str(body.get("severity") or "").strip().upper()
    if severity not in _INCIDENT_SEVERITIES:
        raise HTTPException(
            status_code=400,
            detail="incident severity must be P0, P1, or P2",
        )
    reason = str(body.get("reason") or "").strip().lower()
    if _INCIDENT_REASON_PATTERN.fullmatch(reason) is None:
        raise HTTPException(
            status_code=400,
            detail="incident reason is invalid",
        )
    summary = str(body.get("summary") or "").strip()
    if not summary:
        raise HTTPException(
            status_code=400,
            detail="incident summary is required",
        )
    if len(summary) > _INCIDENT_SUMMARY_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail="incident summary is too long",
        )

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    incident_key = _incident_deduplication_key(
        account_id=str(bound_account_id),
        node_id=node_id,
        reason=reason,
    )
    stored_summary = _stored_incident_summary(
        incident_key=incident_key,
        node_id=node_id,
        reason=reason,
        summary=summary,
    )
    summary_prefix = _stored_incident_summary_prefix(
        incident_key=incident_key,
        node_id=node_id,
        reason=reason,
    )

    conn = _database_connection(database_url)
    try:
        with conn.cursor() as cur:
            _require_node_writer(
                cur,
                node_id=node_id,
                account_id=str(bound_account_id),
                redis_fencing_epoch=x_redis_fencing_epoch,
                runtime_generation=x_runtime_generation,
                lease_fencing_token=x_lease_fencing_token,
            )
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (incident_key,),
            )
            cur.execute(
                """
                SELECT incident_id, severity, opened_at
                FROM production_incidents
                WHERE account_id=%s
                  AND status='open'
                  AND LEFT(summary, char_length(%s))=%s
                ORDER BY opened_at, incident_id
                LIMIT 1
                FOR UPDATE
                """,
                (
                    bound_account_id,
                    summary_prefix,
                    summary_prefix,
                ),
            )
            existing = cur.fetchone()
            deduplicated = existing is not None
            if existing is None:
                incident_id = str(uuid4())
                opened_at = datetime.now(timezone.utc)
                cur.execute(
                    """
                    INSERT INTO production_incidents (
                        incident_id,
                        account_id,
                        severity,
                        status,
                        summary,
                        opened_at
                    )
                    VALUES (%s, %s, %s, 'open', %s, %s)
                    """,
                    (
                        incident_id,
                        bound_account_id,
                        severity,
                        stored_summary,
                        opened_at,
                    ),
                )
            else:
                incident_id, existing_severity, opened_at = existing
                severity = _stronger_incident_severity(
                    str(existing_severity),
                    severity,
                )
                cur.execute(
                    """
                    UPDATE production_incidents
                    SET severity=%s,
                        summary=%s
                    WHERE incident_id=%s
                    """,
                    (
                        severity,
                        stored_summary,
                        incident_id,
                    ),
                )
        conn.commit()
    finally:
        conn.close()

    return {
        "incident_id": str(incident_id),
        "account_id": str(bound_account_id),
        "node_id": node_id,
        "reason": reason,
        "severity": severity,
        "status": "open",
        "summary": summary,
        "opened_at": opened_at,
        "deduplicated": deduplicated,
    }


@app.post("/v1/nodes/{node_id}/incidents/resolve")
def resolve_node_incident(
    node_id: str,
    body: dict = Body(default={}),
    authorization: str | None = Header(default=None),
    x_node_id: str | None = Header(default=None),
    x_account_id: str | None = Header(default=None),
    x_redis_fencing_epoch: str | None = Header(default=None),
    x_runtime_generation: str | None = Header(default=None),
    x_lease_fencing_token: str | None = Header(default=None),
):
    account_id = str(body.get("account_id") or "").strip()
    if _node_auth_bindings() is False:
        raise HTTPException(
            status_code=503,
            detail=(
                "node identity bindings are required "
                "for incident resolution"
            ),
        )
    bound_account_id = require_node(
        authorization,
        node_id=node_id,
        account_id=account_id,
        x_node_id=x_node_id,
        x_account_id=x_account_id,
    )
    reason = str(body.get("reason") or "").strip().lower()
    if _INCIDENT_REASON_PATTERN.fullmatch(reason) is None:
        raise HTTPException(
            status_code=400,
            detail="incident reason is invalid",
        )
    summary = str(body.get("summary") or "").strip()
    if not summary:
        raise HTTPException(
            status_code=400,
            detail="incident resolution summary is required",
        )
    if len(summary) > _INCIDENT_SUMMARY_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail="incident resolution summary is too long",
        )

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    incident_key = _incident_deduplication_key(
        account_id=str(bound_account_id),
        node_id=node_id,
        reason=reason,
    )
    stored_summary = _stored_incident_summary(
        incident_key=incident_key,
        node_id=node_id,
        reason=reason,
        summary=summary,
    )
    summary_prefix = _stored_incident_summary_prefix(
        incident_key=incident_key,
        node_id=node_id,
        reason=reason,
    )

    conn = _database_connection(database_url)
    try:
        with conn.cursor() as cur:
            _require_node_writer(
                cur,
                node_id=node_id,
                account_id=str(bound_account_id),
                redis_fencing_epoch=x_redis_fencing_epoch,
                runtime_generation=x_runtime_generation,
                lease_fencing_token=x_lease_fencing_token,
            )
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (incident_key,),
            )
            cur.execute("SELECT now()")
            closed_at = cur.fetchone()[0]
            cur.execute(
                """
                UPDATE production_incidents
                SET status='closed',
                    closed_at=%s,
                    summary=%s
                WHERE account_id=%s
                  AND status='open'
                  AND LEFT(summary, char_length(%s))=%s
                RETURNING incident_id
                """,
                (
                    closed_at,
                    stored_summary,
                    bound_account_id,
                    summary_prefix,
                    summary_prefix,
                ),
            )
            resolved_incident_ids = sorted(
                str(row[0])
                for row in cur.fetchall()
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "account_id": str(bound_account_id),
        "node_id": node_id,
        "reason": reason,
        "status": "closed",
        "summary": summary,
        "resolved_incident_ids": resolved_incident_ids,
        "resolved_count": len(resolved_incident_ids),
        "closed_at": closed_at,
    }


def _incident_deduplication_key(
    *,
    account_id: str,
    node_id: str,
    reason: str,
) -> str:
    identity = "\x00".join((account_id, node_id, reason))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _stored_incident_summary_prefix(
    *,
    incident_key: str,
    node_id: str,
    reason: str,
) -> str:
    return (
        f"[node-incident:v1 key={incident_key} "
        f"node={node_id} reason={reason}] "
    )


def _stored_incident_summary(
    *,
    incident_key: str,
    node_id: str,
    reason: str,
    summary: str,
) -> str:
    prefix = _stored_incident_summary_prefix(
        incident_key=incident_key,
        node_id=node_id,
        reason=reason,
    )
    return prefix + summary


def _stronger_incident_severity(current: str, reported: str) -> str:
    current_rank = _INCIDENT_SEVERITY_RANK.get(current)
    reported_rank = _INCIDENT_SEVERITY_RANK.get(reported)
    if current_rank is None:
        return reported
    if reported_rank is None:
        return current
    if reported_rank < current_rank:
        return reported
    return current


def _heartbeat_peer_receipts(
    cur,
    *,
    node_id: str,
    account_id: str,
    trading_state: str,
    redis_fencing_epoch: str | None,
    release_identity: tuple[
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
    ],
    rollout_phase: str | None,
) -> list[dict]:
    bindings = _node_auth_bindings()
    if bindings is False:
        return []
    peer_ids = sorted(
        candidate
        for candidate, binding in bindings.items()
        if (
            candidate != node_id
            and binding["account_id"] != account_id
        )
    )
    if not peer_ids:
        return []
    cur.execute(
        """
        SELECT node_id,
               account_id,
               release_id,
               image_digest,
               config_sha256,
               dependency_lock_sha256,
               schema_epoch,
               redis_fencing_epoch,
               status,
               GREATEST(
                   0,
                   EXTRACT(EPOCH FROM (now() - last_seen_at))
               )
        FROM node_heartbeats
        WHERE node_id = ANY(%s)
        ORDER BY node_id
        """,
        (peer_ids,),
    )
    rows = {
        str(row[0]): row
        for row in cur.fetchall()
    }
    max_age = _live_evidence_max_age_seconds()
    active_rollout = _active_reviewed_rollout(cur)
    receipts = []
    for peer_id in peer_ids:
        binding = bindings[peer_id]
        row = rows.get(peer_id)
        if row is None:
            receipts.append(
                {
                    "node_id": peer_id,
                    "account_id": binding["account_id"],
                    "release_id": False,
                    "image_digest": False,
                    "config_sha256": False,
                    "dependency_lock_sha256": False,
                    "schema_epoch": False,
                    "redis_fencing_epoch": False,
                    "freshness_age_seconds": False,
                    "fresh": False,
                    "identity_matches": False,
                    "status": _missing_peer_status(
                        account_id=account_id,
                        trading_state=trading_state,
                        peer_account_id=binding["account_id"],
                        release_identity=release_identity,
                        rollout_phase=rollout_phase,
                    ),
                }
            )
            continue
        peer_identity = tuple(row[2:7])
        peer_redis_fencing_epoch = str(row[7] or "").strip()
        peer_trading_state = str(row[8] or "").upper()
        age_seconds = float(row[9])
        fresh = age_seconds <= max_age
        identity_matches = (
            peer_identity == release_identity
            and peer_redis_fencing_epoch
            == str(redis_fencing_epoch or "")
        )
        peer_status = _peer_release_status(
            account_id=account_id,
            trading_state=trading_state,
            peer_account_id=str(row[1] or binding["account_id"]),
            peer_trading_state=peer_trading_state,
            release_identity=release_identity,
            peer_identity=peer_identity,
            fresh=fresh,
            identity_matches=identity_matches,
            rollout_phase=rollout_phase,
            active_rollout=active_rollout,
        )
        receipts.append(
            {
                "node_id": peer_id,
                "account_id": str(row[1] or binding["account_id"]),
                **_release_identity_payload(peer_identity),
                "redis_fencing_epoch": peer_redis_fencing_epoch or False,
                "freshness_age_seconds": age_seconds,
                "fresh": fresh,
                "identity_matches": identity_matches,
                "status": peer_status,
            }
        )
    return receipts


def _validate_fleet_release_ready(
    cur,
    *,
    node_id: str,
    account_id: str,
    trading_state: str,
    release_identity: tuple[
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
    ],
) -> None:
    release_id = release_identity[0]
    rollout = _reviewed_rollout_state(cur, release_id)
    rollout_phase = None
    if rollout is not None:
        rollout_phase = rollout["phase"]
    valid_phases = set(_ACTIVE_ROLLOUT_PHASES)
    valid_phases.add(_ROLLOUT_PHASE_FLEET_COMPLETE)
    if rollout_phase not in valid_phases:
        raise HTTPException(
            status_code=409,
            detail="reviewed release rollout is not live-open eligible",
        )
    rollout_identity = (
        rollout["release_id"],
        rollout["image_digest"],
        rollout["config_sha256"],
        rollout["dependency_lock_sha256"],
        rollout["schema_epoch"],
    )
    if rollout_identity != release_identity:
        raise HTTPException(
            status_code=409,
            detail="reviewed release rollout identity drift",
        )


def _rollout_has_stopped_all_halted_registration(cur, rollout: dict) -> bool:
    registration_key = str(
        rollout.get("registration_idempotency_key") or ""
    ).strip()
    reviewed_by = str(rollout.get("reviewed_by") or "").strip()
    if not registration_key or not reviewed_by:
        return False
    cur.execute(
        """
        SELECT event_type,
               to_phase,
               phase_version,
               actor,
               evidence
        FROM reviewed_release_rollout_events
        WHERE release_id=%s
          AND idempotency_key=%s
        FOR SHARE
        """,
        (rollout["release_id"], registration_key),
    )
    event = cur.fetchone()
    if event is None:
        return False
    event_type, to_phase, phase_version, actor, evidence = event
    if (
        str(event_type) != "registered"
        or str(to_phase) != _ROLLOUT_PHASE_ACCOUNT_A_CANARY
        or int(phase_version) != 1
        or str(actor) != reviewed_by
    ):
        raise HTTPException(
            status_code=409,
            detail="reviewed release rollout registration evidence is invalid",
        )
    if not isinstance(evidence, dict):
        raise HTTPException(
            status_code=409,
            detail="reviewed release rollout registration evidence is invalid",
        )
    registration_mode = evidence.get("registration_mode")
    all_halted = evidence.get("bootstrap_all_halted")
    marked_stopped = (
        registration_mode in _STOPPED_REGISTRATION_MODES
        or all_halted is not None
    )
    if not marked_stopped:
        return False
    if (
        registration_mode not in _STOPPED_REGISTRATION_MODES
        or all_halted is not True
    ):
        raise HTTPException(
            status_code=409,
            detail="reviewed release stopped registration evidence is incomplete",
        )
    return True


def _validate_canary_release_ready(
    cur,
    *,
    account_id: str,
    required_rollout_phase: str,
    expected_trading_state: str,
    release_identity: tuple[
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
    ],
) -> None:
    expected_phase = _canary_phase_for_account(account_id)
    if required_rollout_phase != expected_phase:
        raise HTTPException(
            status_code=409,
            detail="canary rollout phase does not match account",
        )
    rollout = _reviewed_rollout_state(cur, release_identity[0])
    if rollout is None or rollout["phase"] != required_rollout_phase:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{account_id} canary requires rollout phase "
                f"{required_rollout_phase}"
            ),
        )
    rollout_identity = (
        rollout["release_id"],
        rollout["image_digest"],
        rollout["config_sha256"],
        rollout["dependency_lock_sha256"],
        rollout["schema_epoch"],
    )
    if rollout_identity != release_identity:
        raise HTTPException(
            status_code=409,
            detail="reviewed release rollout identity drift",
        )
    active_redis_fencing_epoch = _active_redis_fencing_epoch(
        cur,
        lock=False,
    )
    if (
        active_redis_fencing_epoch is None
        or rollout["redis_fencing_epoch"]
        != active_redis_fencing_epoch
    ):
        raise HTTPException(
            status_code=409,
            detail="reviewed release rollout Redis fencing epoch drift",
        )
    _validate_reviewed_fleet_manifests(
        cur,
        release_identity=release_identity,
    )


def _reviewed_rollout_state(
    cur,
    release_id: str | None,
    *,
    lock: bool = False,
) -> dict | None:
    if not release_id:
        return None
    lock_clause = ""
    if lock:
        lock_clause = " FOR SHARE"
    cur.execute(
        """
        SELECT release_id,
               redis_fencing_epoch,
               image_digest,
               config_sha256,
               dependency_lock_sha256,
               schema_epoch,
               phase,
               phase_version,
               registration_idempotency_key,
               reviewed_by
        FROM reviewed_release_rollouts
        WHERE release_id=%s
        """
        + lock_clause,
        (release_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "release_id": str(row[0]),
        "redis_fencing_epoch": str(row[1]),
        "image_digest": str(row[2]),
        "config_sha256": str(row[3]),
        "dependency_lock_sha256": str(row[4]),
        "schema_epoch": str(row[5]),
        "phase": str(row[6]),
        "phase_version": row[7],
        "registration_idempotency_key": str(row[8] or ""),
        "reviewed_by": str(row[9] or ""),
    }


def _current_reviewed_rollout_state(
    cur,
    *,
    lock: bool = False,
) -> dict | None:
    lock_clause = ""
    if lock:
        lock_clause = " FOR SHARE"
    cur.execute(
        """
        SELECT release_id,
               redis_fencing_epoch,
               image_digest,
               config_sha256,
               dependency_lock_sha256,
               schema_epoch,
               phase,
               phase_version,
               registration_idempotency_key,
               reviewed_by
        FROM reviewed_release_rollouts
        ORDER BY created_at DESC, release_id DESC
        LIMIT 1
        """
        + lock_clause
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "release_id": str(row[0]),
        "redis_fencing_epoch": str(row[1]),
        "image_digest": str(row[2]),
        "config_sha256": str(row[3]),
        "dependency_lock_sha256": str(row[4]),
        "schema_epoch": str(row[5]),
        "phase": str(row[6]),
        "phase_version": row[7],
        "registration_idempotency_key": str(row[8] or ""),
        "reviewed_by": str(row[9] or ""),
    }


def _live_open_gate_from_rollout(
    rollout: dict | None,
) -> dict | bool:
    if not isinstance(rollout, dict):
        return False
    release_id = str(rollout.get("release_id") or "").strip()
    rollout_phase = str(rollout.get("phase") or "").strip()
    phase_version = rollout.get("phase_version")
    valid_phases = set(_ACTIVE_ROLLOUT_PHASES)
    valid_phases.add(_ROLLOUT_PHASE_FLEET_COMPLETE)
    if (
        not release_id
        or rollout_phase not in valid_phases
        or isinstance(phase_version, bool)
        or not isinstance(phase_version, int)
        or phase_version < 1
    ):
        return False
    return {
        "mode": "normal",
        "release_id": release_id,
        "rollout_phase": rollout_phase,
        "phase_version": phase_version,
    }


def _validate_reviewed_fleet_manifests(
    cur,
    *,
    release_identity: tuple[
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
    ],
) -> None:
    cur.execute(
        """
        SELECT account_id,
               release_id,
               image_digest,
               config_sha256,
               dependency_lock_sha256,
               schema_epoch,
               review_status
        FROM reviewed_release_manifests
        WHERE release_id=%s
          AND account_id = ANY(%s)
        ORDER BY account_id
        """,
        (
            release_identity[0],
            list(_ROLLOUT_ACCOUNTS),
        ),
    )
    rows = cur.fetchall()
    if [str(row[0]) for row in rows] != list(_ROLLOUT_ACCOUNTS):
        raise HTTPException(
            status_code=409,
            detail="reviewed release fleet registration is incomplete",
        )
    for row in rows:
        manifest_identity = tuple(row[1:6])
        review_status = str(row[6] or "").strip().lower()
        if (
            manifest_identity != release_identity
            or review_status != "reviewed"
        ):
            raise HTTPException(
                status_code=409,
                detail="reviewed release fleet registration drift",
            )


def _active_reviewed_rollout(cur) -> dict | None:
    cur.execute(
        """
        SELECT release_id,
               phase
        FROM reviewed_release_rollouts
        WHERE phase = ANY(%s)
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (list(_ACTIVE_ROLLOUT_PHASES),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "release_id": str(row[0]),
        "phase": str(row[1]),
    }


def _peer_awaits_rollout_step(
    *,
    trading_state: str,
    peer_account_id: str,
    release_identity: tuple[
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
    ],
    rollout_phase: str | None,
) -> bool:
    # During an active reviewed rollout the accounts are recreated in
    # ladder order (a canary, then b, c, d).  A HALTED reporter must
    # tolerate peers whose ladder step has not completed yet — the peer
    # currently being recreated and every later one — otherwise the
    # canary can never reach ready.  A LIVE reporter never tolerates a
    # missing peer, and outside an active rollout phase (fleet_complete,
    # aborted) no tolerance applies.
    if trading_state != "HALTED" or not release_identity[0]:
        return False
    if rollout_phase not in _ACTIVE_ROLLOUT_PHASES:
        return False
    peer_phase = _CANARY_PHASE_BY_ACCOUNT.get(peer_account_id)
    if peer_phase is None:
        return False
    return (
        _ACTIVE_ROLLOUT_PHASES.index(peer_phase)
        >= _ACTIVE_ROLLOUT_PHASES.index(rollout_phase)
    )


def _account_awaits_rollout_step(
    *,
    account_id: str,
    rollout_phase: str | None,
) -> bool:
    if rollout_phase not in _ACTIVE_ROLLOUT_PHASES:
        return False
    account_phase = _CANARY_PHASE_BY_ACCOUNT.get(account_id)
    if account_phase is None:
        return False
    return (
        _ACTIVE_ROLLOUT_PHASES.index(account_phase)
        > _ACTIVE_ROLLOUT_PHASES.index(rollout_phase)
    )


def _missing_peer_status(
    *,
    account_id: str,
    trading_state: str,
    peer_account_id: str,
    release_identity: tuple[
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
    ],
    rollout_phase: str | None,
) -> str:
    if _peer_awaits_rollout_step(
        trading_state=trading_state,
        peer_account_id=peer_account_id,
        release_identity=release_identity,
        rollout_phase=rollout_phase,
    ):
        return "rollout_pending"
    return "missing"


def _peer_release_status(
    *,
    account_id: str,
    trading_state: str,
    peer_account_id: str,
    peer_trading_state: str,
    release_identity: tuple[
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
    ],
    peer_identity: tuple[
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
    ],
    fresh: bool,
    identity_matches: bool,
    rollout_phase: str | None,
    active_rollout: dict | None,
) -> str:
    if fresh and identity_matches:
        return "consistent"
    if _peer_awaits_rollout_step(
        trading_state=trading_state,
        peer_account_id=peer_account_id,
        release_identity=release_identity,
        rollout_phase=rollout_phase,
    ):
        return "rollout_pending"
    # An old-release reporter must tolerate a fresh HALTED peer that is
    # already on the actively rolling-out release: that peer is ahead of
    # the reporter on the ladder, not drifted.
    if (
        fresh
        and peer_trading_state == "HALTED"
        and active_rollout is not None
        and active_rollout["phase"] in _ACTIVE_ROLLOUT_PHASES
        and peer_identity[0] == active_rollout["release_id"]
    ):
        return "rollout_pending"
    if not fresh:
        return "stale"
    return "identity_drift"


def _release_identity_payload(values) -> dict:
    (
        release_id,
        image_digest,
        config_sha256,
        dependency_lock_sha256,
        schema_epoch,
    ) = values
    return {
        "release_id": release_id or False,
        "image_digest": image_digest or False,
        "config_sha256": config_sha256 or False,
        "dependency_lock_sha256": dependency_lock_sha256 or False,
        "schema_epoch": schema_epoch or False,
    }


def _heartbeat_snapshot(
    body: dict,
    field_name: str,
    *,
    fallback_field: str | None = None,
) -> list[dict]:
    value = body.get(field_name)
    if value is None and fallback_field:
        value = body.get(fallback_field)
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be a list",
        )
    if any(not isinstance(item, dict) for item in value):
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} entries must be objects",
        )
    return [dict(item) for item in value]


def _heartbeat_health_degraded_reasons(body: dict) -> list[str]:
    raw_reasons = body.get("health_degraded_reasons")
    if raw_reasons is None:
        return []
    if not isinstance(raw_reasons, list):
        raise HTTPException(
            status_code=400,
            detail="health_degraded_reasons must be a list",
        )
    reasons: list[str] = []
    for raw_reason in raw_reasons:
        reason = str(raw_reason or "").strip()
        if not reason:
            raise HTTPException(
                status_code=400,
                detail="health_degraded_reasons entries must be non-empty",
            )
        if len(reason) > _INCIDENT_SUMMARY_MAX_LENGTH:
            raise HTTPException(
                status_code=400,
                detail="health_degraded_reasons entry is too long",
            )
        if reason in reasons:
            continue
        reasons.append(reason)
    return reasons


def _heartbeat_exchange_evidence_is_complete(body: dict) -> bool:
    snapshot_fields = (
        "positions",
        "regular_orders",
        "algo_orders",
    )
    timestamp_fields = (
        "positions_snapshot_at",
        "regular_orders_snapshot_at",
        "algo_orders_snapshot_at",
    )
    for field_name in snapshot_fields:
        if body.get(field_name) is None:
            return False
    for field_name in timestamp_fields:
        value = body.get(field_name)
        if value is None or value == "":
            return False
    return True


def _validate_live_heartbeat_exchange_evidence(
    *,
    positions: list[dict],
    regular_orders: list[dict],
    algo_orders: list[dict],
    positions_snapshot_at: datetime | None,
    regular_orders_snapshot_at: datetime | None,
    algo_orders_snapshot_at: datetime | None,
    database_now: datetime,
) -> None:
    snapshots = (
        positions,
        regular_orders,
        algo_orders,
    )
    if any(
        not _snapshot_item_symbol(item)
        for snapshot in snapshots
        for item in snapshot
    ):
        raise HTTPException(
            status_code=409,
            detail="node exchange evidence is invalid",
        )
    snapshot_times = (
        positions_snapshot_at,
        regular_orders_snapshot_at,
        algo_orders_snapshot_at,
    )
    if any(
        not _timestamp_is_fresh(value, database_now)
        for value in snapshot_times
    ):
        raise HTTPException(
            status_code=409,
            detail="node exchange evidence is stale",
        )


def _heartbeat_timestamp(body: dict, field_name: str) -> datetime | None:
    value = body.get(field_name)
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"{field_name} must be an ISO-8601 timestamp",
            ) from exc
    else:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be an ISO-8601 timestamp",
        )
    if parsed.tzinfo is None:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must include a timezone",
        )
    return parsed


def _optional_identity(value) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return normalized


def _optional_uuid4_identity(
    value,
    *,
    field_name: str,
) -> str | None:
    normalized = _optional_identity(value)
    if normalized is None:
        return None
    try:
        parsed = UUID(normalized)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be a canonical UUID4",
        ) from exc
    if parsed.version != 4 or str(parsed) != normalized:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be a canonical UUID4",
        )
    return normalized


def _heartbeat_writer_identity(
    body: dict,
    *,
    required: bool,
) -> tuple[str | None, str | None, int | None, int | None]:
    redis_fencing_epoch = _optional_uuid4_identity(
        body.get("redis_fencing_epoch"),
        field_name="redis_fencing_epoch",
    )
    runtime_generation = _optional_identity(body.get("runtime_generation"))
    lease_fencing_token = _positive_integer_or_none(
        body.get("lease_fencing_token")
    )
    heartbeat_sequence = _positive_integer_or_none(
        body.get("heartbeat_sequence")
    )
    values = (
        redis_fencing_epoch,
        runtime_generation,
        lease_fencing_token,
        heartbeat_sequence,
    )
    if all(value is None for value in values) and not required:
        return None, None, None, None
    if any(value is None for value in values):
        raise HTTPException(
            status_code=400,
            detail=(
                "live heartbeat requires redis_fencing_epoch, "
                "runtime_generation, lease_fencing_token, "
                "and heartbeat_sequence"
            ),
        )
    return (
        redis_fencing_epoch,
        runtime_generation,
        lease_fencing_token,
        heartbeat_sequence,
    )


def _active_redis_fencing_epoch(
    cur,
    *,
    lock: bool,
) -> str | None:
    lock_clause = ""
    if lock:
        lock_clause = " FOR SHARE"
    cur.execute(
        """
        SELECT redis_fencing_epoch
        FROM redis_fencing_epochs
        WHERE domain='trader-v3'
          AND status='active'
        """
        + lock_clause
    )
    row = cur.fetchone()
    if row is None:
        return None
    return str(row[0])


def _require_node_writer(
    cur,
    *,
    node_id: str,
    account_id: str,
    redis_fencing_epoch: str | None,
    runtime_generation: str | None,
    lease_fencing_token: str | int | None,
) -> None:
    active_epoch = _active_redis_fencing_epoch(cur, lock=True)
    identity = _node_writer_header_identity(
        redis_fencing_epoch=redis_fencing_epoch,
        runtime_generation=runtime_generation,
        lease_fencing_token=lease_fencing_token,
    )
    if active_epoch is None:
        if identity is None:
            return
        raise HTTPException(
            status_code=503,
            detail="active redis fencing epoch is unavailable",
        )
    if identity is None:
        _raise_writer_fence("node writer identity is required")
    (
        provided_epoch,
        provided_generation,
        provided_token,
    ) = identity
    if provided_epoch != active_epoch:
        _raise_writer_fence("redis fencing epoch mismatch")
    cur.execute(
        """
        SELECT redis_fencing_epoch::text,
               runtime_generation,
               lease_fencing_token
        FROM node_heartbeats
        WHERE node_id=%s
          AND account_id=%s
        FOR SHARE
        """,
        (node_id, account_id),
    )
    row = cur.fetchone()
    if row is None:
        _raise_writer_fence("node writer heartbeat is missing")
    expected_epoch = str(row[0] or "").strip()
    expected_generation = str(row[1] or "").strip()
    expected_token = row[2]
    if expected_epoch != provided_epoch:
        _raise_writer_fence("redis fencing epoch mismatch")
    if expected_generation != provided_generation:
        _raise_writer_fence("runtime generation mismatch")
    if expected_token != provided_token:
        _raise_writer_fence("lease fencing token mismatch")


def _node_writer_header_identity(
    *,
    redis_fencing_epoch: str | None,
    runtime_generation: str | None,
    lease_fencing_token: str | int | None,
) -> tuple[str, str, int] | None:
    raw_values = (
        redis_fencing_epoch,
        runtime_generation,
        lease_fencing_token,
    )
    if all(value is None or value == "" for value in raw_values):
        return None
    if any(value is None or value == "" for value in raw_values):
        _raise_writer_fence("node writer identity is incomplete")
    epoch = str(redis_fencing_epoch).strip()
    try:
        parsed_epoch = UUID(epoch)
    except ValueError:
        _raise_writer_fence("redis fencing epoch is invalid")
    if parsed_epoch.version != 4 or str(parsed_epoch) != epoch:
        _raise_writer_fence("redis fencing epoch is invalid")
    generation = str(runtime_generation).strip()
    if not generation:
        _raise_writer_fence("runtime generation is invalid")
    token = _writer_fencing_token(lease_fencing_token)
    return epoch, generation, token


def _writer_fencing_token(value: str | int | None) -> int:
    if isinstance(value, bool):
        _raise_writer_fence("lease fencing token is invalid")
    normalized = str(value or "").strip()
    if re.fullmatch(r"[1-9][0-9]*", normalized) is None:
        _raise_writer_fence("lease fencing token is invalid")
    return int(normalized)


def _validate_heartbeat_writer_headers(
    *,
    body_redis_fencing_epoch: str | None,
    body_runtime_generation: str | None,
    body_lease_fencing_token: int | None,
    header_redis_fencing_epoch: str | None,
    header_runtime_generation: str | None,
    header_lease_fencing_token: str | None,
) -> None:
    header_identity = _node_writer_header_identity(
        redis_fencing_epoch=header_redis_fencing_epoch,
        runtime_generation=header_runtime_generation,
        lease_fencing_token=header_lease_fencing_token,
    )
    if header_identity is None:
        return
    body_identity = (
        body_redis_fencing_epoch,
        body_runtime_generation,
        body_lease_fencing_token,
    )
    if header_identity != body_identity:
        _raise_writer_fence(
            "heartbeat writer identity mismatch"
        )


def _raise_writer_fence(detail: str) -> None:
    raise HTTPException(
        status_code=409,
        detail=detail,
        headers={"X-Writer-Fence-Rejected": "1"},
    )


def _positive_integer_or_none(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise HTTPException(
            status_code=400,
            detail="heartbeat writer counters must be positive integers",
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="heartbeat writer counters must be positive integers",
        ) from exc
    if parsed <= 0 or str(parsed) != str(value).strip():
        raise HTTPException(
            status_code=400,
            detail="heartbeat writer counters must be positive integers",
        )
    return parsed


def _revoke_changed_portfolio_baselines(
    cur,
    *,
    account_id: str,
    node_id: str,
    heartbeat: dict,
) -> None:
    if account_id not in _ROLLOUT_ACCOUNTS:
        return
    cur.execute(
        """
        SELECT permit_id,
               symbol,
               portfolio_baseline_sha256
        FROM live_canary_permits
        WHERE account_id=%s
          AND armed_node_id=%s
          AND status='armed'
        FOR UPDATE
        """,
        (account_id, node_id),
    )
    for permit_id, target_symbol, expected_baseline in cur.fetchall():
        current_baseline = _portfolio_baseline_sha256(
            heartbeat,
            _canonical_symbol(target_symbol),
        )
        if current_baseline == expected_baseline:
            continue
        cur.execute(
            """
            UPDATE live_canary_permits
            SET status='revoked'
            WHERE permit_id=%s
              AND status='armed'
            """,
            (permit_id,),
        )


@app.get("/v1/nodes/{node_id}/commands")
def node_commands(
    node_id: str,
    account_id: str,
    after: str | None = None,
    authorization: str | None = Header(default=None),
    x_node_id: str | None = Header(default=None),
    x_account_id: str | None = Header(default=None),
    x_redis_fencing_epoch: str | None = Header(default=None),
    x_runtime_generation: str | None = Header(default=None),
    x_lease_fencing_token: str | None = Header(default=None),
):
    """A<->B seam: node polls its pending operator commands (kill-switch path)."""
    bound_account_id = require_node(
        authorization,
        node_id=node_id,
        account_id=account_id,
        x_node_id=x_node_id,
        x_account_id=x_account_id,
    )
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    conn = _database_connection(database_url)
    try:
        with conn.cursor() as cur:
            _require_node_writer(
                cur,
                node_id=node_id,
                account_id=str(bound_account_id or account_id),
                redis_fencing_epoch=x_redis_fencing_epoch,
                runtime_generation=x_runtime_generation,
                lease_fencing_token=x_lease_fencing_token,
            )
            cur.execute(
                "SELECT oc.command_id::text, oc.command_type, oc.scope, oc.created_at "
                "FROM operator_commands oc JOIN command_node_acks na ON na.command_id=oc.command_id "
                "WHERE na.node_id=%s AND na.status='pending' "
                "AND oc.scope->>'account_id'=%s "
                "ORDER BY oc.created_at LIMIT %s",
                (
                    node_id,
                    str(bound_account_id or account_id),
                    NODE_COMMAND_POLL_LIMIT,
                ),
            )
            rows = cur.fetchall()
        commands = []
        for row in rows:
            args = dict(row[2] or {})
            commands.append(
                {
                    "command_id": row[0],
                    "type": _NODE_COMMAND_TYPE_MAP.get(row[1], row[1].lower()),
                    "args": args,
                    "issued_at": row[3].isoformat() if row[3] else None,
                }
            )
        return {"commands": commands}
    finally:
        conn.close()


@app.post("/v1/nodes/{node_id}/commands/{command_id}/ack")
def ack_node_command(node_id: str, command_id: str, body: dict = Body(default={}),
                     authorization: str | None = Header(default=None),
                     x_node_id: str | None = Header(default=None),
                     x_account_id: str | None = Header(default=None),
                     x_redis_fencing_epoch: str | None = Header(default=None),
                     x_runtime_generation: str | None = Header(default=None),
                     x_lease_fencing_token: str | None = Header(default=None)):
    """A<->B seam: node acks an operator command; recomputes the command's rollup."""
    account_id = str(body.get("account_id") or "").strip()
    require_node(
        authorization,
        node_id=node_id,
        account_id=account_id,
        x_node_id=x_node_id,
        x_account_id=x_account_id,
    )
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    status = str(body.get("status") or "").strip().lower()
    if status not in _COMMAND_ACK_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="invalid command ack status",
        )
    ack_result = body.get("result")
    if ack_result is None:
        ack_result = {}
    if not isinstance(ack_result, dict):
        raise HTTPException(
            status_code=400,
            detail="command ack result must be an object",
        )
    ack_detail = body.get("error")
    if ack_detail is None:
        ack_detail = body.get("detail")
    if ack_detail is not None:
        ack_detail = str(ack_detail)
    conn = _database_connection(database_url)
    try:
        from psycopg2.extras import Json

        with conn.cursor() as cur:
            _require_node_writer(
                cur,
                node_id=node_id,
                account_id=account_id,
                redis_fencing_epoch=x_redis_fencing_epoch,
                runtime_generation=x_runtime_generation,
                lease_fencing_token=x_lease_fencing_token,
            )
            cur.execute(
                """
                SELECT node_ack.status
                FROM command_node_acks AS node_ack
                JOIN operator_commands AS command
                  ON node_ack.command_id=command.command_id
                WHERE node_ack.command_id=%s
                  AND node_ack.node_id=%s
                  AND command.scope->>'account_id'=%s
                FOR UPDATE OF node_ack
                """,
                (
                    command_id,
                    node_id,
                    account_id,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail="unknown command/node/account binding",
                )
            current_status = str(row[0])
            if not _command_ack_transition_allowed(current_status, status):
                raise HTTPException(
                    status_code=409,
                    detail="invalid command ack transition",
                )
            cur.execute(
                """
                UPDATE command_node_acks
                SET status=%s,
                    detail=COALESCE(%s, detail),
                    result=result || %s,
                    ack_at=now()
                WHERE command_id=%s
                  AND node_id=%s
                """,
                (
                    status,
                    ack_detail,
                    Json(ack_result),
                    command_id,
                    node_id,
                ),
            )
            _recompute_operator_command_status(cur, command_id)
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def _command_ack_transition_allowed(
    current_status: str,
    requested_status: str,
) -> bool:
    if current_status == requested_status:
        return True
    allowed_transitions = {
        "pending": {"accepted", "running", "completed", "failed"},
        "accepted": {"running", "completed", "failed"},
        "running": {"completed", "failed"},
        "acked": {"completed"},
        "completed": set(),
        "failed": set(),
    }
    return requested_status in allowed_transitions.get(current_status, set())


def _recompute_operator_command_status(cur, command_id: str) -> None:
    cur.execute(
        """
        SELECT status
        FROM command_node_acks
        WHERE command_id=%s
        FOR UPDATE
        """,
        (command_id,),
    )
    statuses = [str(row[0]) for row in cur.fetchall()]
    terminal = {"completed", "failed"}
    if not statuses or all(status == "completed" for status in statuses):
        rollup_status = "completed"
    elif all(status == "failed" for status in statuses):
        rollup_status = "failed"
    elif all(status in terminal for status in statuses):
        rollup_status = "partial"
    elif any(status == "running" for status in statuses):
        rollup_status = "running"
    elif any(status in {"accepted", "completed"} for status in statuses):
        rollup_status = "accepted"
    elif any(status == "acked" for status in statuses):
        rollup_status = "acknowledged"
    else:
        rollup_status = "pending"
    cur.execute(
        """
        UPDATE operator_commands
        SET status=%s,
            completed_at=CASE
                WHEN %s AND completed_at IS NULL THEN now()
                ELSE completed_at
            END
        WHERE command_id=%s
        """,
        (
            rollup_status,
            rollup_status in {"completed", "partial", "failed"},
            command_id,
        ),
    )


@app.get("/v1/accounts/{account_id}")
def account_generated_at(
    account_id: str,
    node_id: str,
    authorization: str | None = Header(default=None),
    x_node_id: str | None = Header(default=None),
    x_account_id: str | None = Header(default=None),
    x_redis_fencing_epoch: str | None = Header(default=None),
    x_runtime_generation: str | None = Header(default=None),
    x_lease_fencing_token: str | None = Header(default=None),
):
    """A<->B seam: node reads the snapshot freshness (generated_at) for its account."""
    require_node(
        authorization,
        node_id=node_id,
        account_id=account_id,
        x_node_id=x_node_id,
        x_account_id=x_account_id,
    )
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    conn = _database_connection(database_url)
    try:
        with conn.cursor() as cur:
            _require_node_writer(
                cur,
                node_id=node_id,
                account_id=account_id,
                redis_fencing_epoch=x_redis_fencing_epoch,
                runtime_generation=x_runtime_generation,
                lease_fencing_token=x_lease_fencing_token,
            )
        snap = build_system_snapshot(conn)
        return {"account_id": account_id, "generated_at": snap.get("generated_at")}
    finally:
        conn.close()


@app.get("/v1/nodes/{node_id}/exchange-state")
def node_exchange_state(
    node_id: str,
    account_id: str,
    authorization: str | None = Header(default=None),
    x_node_id: str | None = Header(default=None),
    x_account_id: str | None = Header(default=None),
    x_redis_fencing_epoch: str | None = Header(default=None),
    x_runtime_generation: str | None = Header(default=None),
    x_lease_fencing_token: str | None = Header(default=None),
):
    """Return one account's read-only venue mirror for node reconciliation."""
    require_node(
        authorization,
        node_id=node_id,
        account_id=account_id,
        x_node_id=x_node_id,
        x_account_id=x_account_id,
    )
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    conn = _database_connection(database_url)
    try:
        with conn.cursor() as cur:
            _require_node_writer(
                cur,
                node_id=node_id,
                account_id=account_id,
                redis_fencing_epoch=x_redis_fencing_epoch,
                runtime_generation=x_runtime_generation,
                lease_fencing_token=x_lease_fencing_token,
            )
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT to_regclass('public.exchange_state_mirror') IS NOT NULL AS present"
            )
            if not cur.fetchone()["present"]:
                raise HTTPException(status_code=503, detail="exchange state mirror unavailable")
            cur.execute(
                "SELECT account_id, payload, updated_at, "
                "(now() - updated_at) > interval '180 seconds' AS stale "
                "FROM exchange_state_mirror WHERE account_id=%s",
                (account_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="exchange state mirror missing")
        return dict(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Operator read surface (/v1/*) consumed by the v3 dashboard.
#
# Every response embeds the §2.2 SystemSnapshotV1 data-quality envelope at the top
# level (data_source / stale / missing_nodes / reconciliation_state / ...), because
# the dashboard derives freshness from the SAME payload object it reads rows from.
# Reads are PostgreSQL-only projections and fail-closed (503 if the store is gone).
# Reader auth = viewer|reviewer|risk_admin|system_observer; review writes need reviewer.
# ---------------------------------------------------------------------------

# terminal order states: anything not in this set is still "open/working".
_TERMINAL_ORDER_STATES = (
    "filled", "canceled", "cancelled", "rejected", "expired", "denied", "closed", "done",
)


def _read_conn() -> "psycopg2.extensions.connection":
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="projection store unavailable")
    return _database_connection(database_url)


def _f(value):
    """Coerce numeric/Decimal/str to float, else None (dashboard tolerates null)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value):
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else value


def _symbol(instrument_id: str | None) -> str | None:
    """Nautilus instrument id (BTCUSDT-PERP.BINANCE) -> venue symbol (BTCUSDT) for display."""
    if not instrument_id:
        return instrument_id
    return instrument_id.split("-", 1)[0]


def _envelope(cur, *, now: datetime | None = None, threshold_ms: int = DEFAULT_STALENESS_MS) -> dict:
    """Compute the SystemSnapshotV1 §2.2 envelope from the live projections — identical
    semantics to snapshot.build_system_snapshot, reused here so every /v1 row payload
    carries the same freshness verdict the snapshot endpoint reports."""
    now = now or datetime.now(timezone.utc)
    cur.execute("SELECT account_id, reconciliation_state::text AS reconciliation_state FROM accounts_projection")
    accounts = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT node_id, last_seen_at FROM node_heartbeats")
    nodes = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT max(ts_event) AS t FROM execution_events")
    last_event = cur.fetchone()["t"]
    lag = max(0, int((now - last_event).total_seconds() * 1000)) if last_event is not None else 0
    recon = _worst_reconciliation_state(accounts)
    missing = _missing_nodes(nodes, now, threshold_ms)
    stale = bool((last_event is not None and lag > threshold_ms) or recon == "failed" or missing)
    return {
        "schema_version": "1.0",
        "data_source": "postgres_projection",
        "snapshot_id": str(uuid4()),
        "generated_at": now.isoformat(),
        "last_execution_event_at": last_event.isoformat() if last_event is not None else None,
        "projection_lag_ms": lag,
        "stale": stale,
        "missing_nodes": missing,
        "reconciliation_state": recon,
    }


def _valid_uuid(value: str) -> str | None:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


@app.get("/v1/accounts")
def v1_accounts(authorization: str | None = Header(default=None)):
    require_reader(authorization)
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            cur.execute(
                "SELECT account_id, currency, equity, margin, available_balance, "
                "reconciliation_state::text AS reconciliation_state, projection_lag_ms, "
                "updated_at, payload FROM accounts_projection ORDER BY account_id"
            )
            rows = [dict(r) for r in cur.fetchall()]
        accounts = []
        for r in rows:
            payload = r.get("payload") or {}
            accounts.append({
                "account_id": r["account_id"],
                "currency": r["currency"],
                "equity": _f(r["equity"]),
                "available": _f(r.get("available_balance")),
                "margin_used": _f(r["margin"]),
                "realized_pnl_today": _f(payload.get("realized_pnl_today") or payload.get("daily_pnl") or 0),
                "reconciliation_state": r["reconciliation_state"],
                "updated_at": _iso(r["updated_at"]),
            })
        return {**env, "accounts": accounts}
    finally:
        conn.close()


@app.get("/v1/nodes")
def v1_nodes(authorization: str | None = Header(default=None)):
    require_reader(authorization)
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            cur.execute(
                """
                SELECT nh.node_id, nh.account_id, nh.status, nh.version, nh.payload, nh.last_seen_at,
                       (SELECT count(*) FROM positions_projection p
                          WHERE p.account_id = nh.account_id AND p.status = 'open') AS open_position_count,
                       (SELECT count(DISTINCT p.instrument_id) FROM positions_projection p
                          WHERE p.account_id = nh.account_id AND p.status = 'open') AS instrument_count
                FROM node_heartbeats nh ORDER BY nh.last_seen_at DESC
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
        nodes = []
        for r in rows:
            payload = r.get("payload") or {}
            nodes.append({
                "node_id": r["node_id"],
                "name": r["node_id"],
                "account_id": r["account_id"],
                "trading_state": r["status"],
                "status": r["status"],
                "readiness": payload.get("readiness"),
                "last_heartbeat_at": _iso(r["last_seen_at"]),
                "open_position_count": int(r["open_position_count"] or 0),
                "instrument_count": int(r["instrument_count"] or 0),
                "projection_lag_ms": payload.get("projection_lag_ms"),
                "reconciliation_state": payload.get("reconciliation_state"),
                "health_degraded_reasons": payload.get(
                    "health_degraded_reasons",
                    [],
                ),
                "version": r["version"],
            })
        return {**env, "nodes": nodes}
    finally:
        conn.close()


@app.get("/v1/orders")
def v1_orders(status: str | None = None, authorization: str | None = Header(default=None)):
    require_reader(authorization)
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            sql = "SELECT * FROM orders_projection"
            params: list = []
            if status == "open":
                sql += " WHERE status NOT IN %s"
                params.append(_TERMINAL_ORDER_STATES)
            elif status:
                sql += " WHERE status = %s"
                params.append(status)
            sql += " ORDER BY updated_at DESC LIMIT 200"
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        orders = []
        for r in rows:
            qty = _f(r.get("quantity")) or 0.0
            filled = _f(r.get("filled_quantity")) or 0.0
            orders.append({
                "order_id": r.get("client_order_id") or str(r.get("order_projection_id")),
                "client_order_id": r.get("client_order_id"),
                "venue_order_id": r.get("venue_order_id"),
                "instrument_symbol": _symbol(r.get("instrument_id")),
                "instrument_id": r.get("instrument_id"),
                "side": r.get("side"),
                "order_type": r.get("order_type"),
                "type": r.get("order_type"),
                "status": r.get("status"),
                "price": _f(r.get("price")),
                "average_price": _f(r.get("average_fill_price")),
                "quantity": qty,
                "amount": qty,
                "filled": filled,
                "filled_quantity": filled,
                "remaining": max(0.0, qty - filled),
                "created_at": _iso(r.get("ts_event") or r.get("updated_at")),
                "trade_id": str(r["intent_id"]) if r.get("intent_id") else None,
            })
        return {**env, "orders": orders}
    finally:
        conn.close()


@app.get("/v1/positions")
def v1_positions(authorization: str | None = Header(default=None)):
    require_reader(authorization)
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            cur.execute(
                "SELECT * FROM positions_projection WHERE status = 'open' "
                "ORDER BY updated_at DESC LIMIT 200"
            )
            rows = [dict(r) for r in cur.fetchall()]
        positions = []
        for r in rows:
            payload = r.get("payload") or {}
            qty = _f(r.get("quantity"))
            entry = _f(r.get("avg_entry_price"))
            notional = _f(payload.get("notional"))
            if notional is None and qty is not None and entry is not None:
                notional = qty * entry
            positions.append({
                "position_id": r.get("position_id"),
                "instrument_symbol": _symbol(r.get("instrument_id")),
                "instrument_id": r.get("instrument_id"),
                "side": r.get("side"),
                "entry_price": entry,
                "mark_price": _f(r.get("mark_price")),
                "unrealized_pnl": _f(r.get("unrealized_pnl")),
                "quantity": qty,
                "size": qty,
                "notional": notional,
                "status": r.get("status"),
                "opened_at": _iso(payload.get("opened_at") or r.get("updated_at")),
                "leverage": _f(payload.get("leverage")),
                "stop_loss": _f(payload.get("stop_loss")),
                "take_profit": _f(payload.get("take_profit")),
                "signal_id": payload.get("signal_id") or payload.get("intent_id"),
                "intent_id": payload.get("intent_id"),
                "raw_signal": payload.get("raw_signal"),
            })
        return {**env, "positions": positions}
    finally:
        conn.close()


@app.get("/v1/trades")
def v1_trades(authorization: str | None = Header(default=None)):
    """Closed-position history projected as trades. There is no separate trades table;
    a closed positions_projection row IS the realized trade record we can honestly serve."""
    require_reader(authorization)
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            cur.execute(
                "SELECT * FROM positions_projection WHERE status = 'closed' "
                "ORDER BY updated_at DESC LIMIT 200"
            )
            rows = [dict(r) for r in cur.fetchall()]
        trades = []
        for r in rows:
            payload = r.get("payload") or {}
            trades.append({
                "trade_id": r.get("position_id"),
                "instrument_symbol": _symbol(r.get("instrument_id")),
                "instrument_id": r.get("instrument_id"),
                "side": r.get("side"),
                "status": "closed",
                "open_price": _f(r.get("avg_entry_price")),
                "close_price": _f(r.get("mark_price")),
                "amount": _f(r.get("quantity")),
                "realized_pnl": _f(payload.get("realized_pnl") or r.get("unrealized_pnl")),
                "opened_at": _iso(payload.get("opened_at")),
                "closed_at": _iso(r.get("updated_at")),
            })
        return {**env, "trades": trades}
    finally:
        conn.close()


@app.get("/v1/messages")
def v1_messages(
    limit: int = 200,
    status: str | None = None,
    authorization: str | None = Header(default=None),
):
    require_reader(authorization)
    limit = max(1, min(int(limit), 500))
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            sql = """
                SELECT rm.id, rm.channel_id, rm.source, rm.source_received_at, rm.message_text,
                       hd.decision_id, hd.action::text AS action, hd.message_type::text AS message_type,
                       rd.status::text AS risk_status
                FROM raw_messages rm
                LEFT JOIN LATERAL (
                    SELECT decision_id, action, message_type FROM hermes_decisions h
                    WHERE h.raw_message_id = rm.id ORDER BY created_at DESC LIMIT 1
                ) hd ON true
                LEFT JOIN LATERAL (
                    SELECT status FROM risk_decisions r
                    WHERE r.hermes_decision_id = hd.decision_id ORDER BY decided_at DESC LIMIT 1
                ) rd ON true
            """
            if status == "needs_review":
                sql += " WHERE (rd.status = 'needs_review' OR hd.action = 'needs_review')"
            sql += " ORDER BY rm.source_received_at DESC LIMIT %s"
            cur.execute(sql, [limit])
            rows = [dict(r) for r in cur.fetchall()]
            messages = []
            for r in rows:
                cur.execute(
                    "SELECT mime FROM media_assets WHERE raw_message_id = %s ORDER BY created_at",
                    (r["id"],),
                )
                media = [
                    {"index": i, "mime_type": m["mime"]}
                    for i, m in enumerate(cur.fetchall())
                ]
                text = r.get("message_text") or ""
                messages.append({
                    "message_id": str(r["id"]),
                    "id": str(r["id"]),
                    "decision_id": str(r["decision_id"]) if r.get("decision_id") else None,
                    "channel_id": r.get("channel_id"),
                    "source": r.get("source"),
                    "raw_text": text,
                    "raw_message": text,
                    "summary": text[:140],
                    "received_at": _iso(r.get("source_received_at")),
                    "created_at": _iso(r.get("source_received_at")),
                    "status": r.get("risk_status") or r.get("action"),
                    "action": r.get("action"),
                    "message_type": r.get("message_type"),
                    "event_type": r.get("message_type"),
                    "media": media,
                })
        return {**env, "messages": messages}
    finally:
        conn.close()


@app.get("/v1/messages/{signal_id}/media/{index}")
def v1_message_media(signal_id: str, index: int, authorization: str | None = Header(default=None)):
    """Serve a message image. signal_id is a decision_id (dashboard) or raw_message_id.
    Bytes resolve from MEDIA_ROOT/object_key; 404 (graceful) when bytes are not present."""
    require_reader(authorization)
    sid = _valid_uuid(signal_id)
    if sid is None:
        raise HTTPException(status_code=404, detail="media missing")
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT raw_message_id FROM hermes_decisions WHERE decision_id = %s", (sid,))
            row = cur.fetchone()
            raw_message_id = str(row["raw_message_id"]) if row else sid
            cur.execute(
                "SELECT object_key, mime FROM media_assets WHERE raw_message_id = %s ORDER BY created_at",
                (raw_message_id,),
            )
            assets = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    if index < 0 or index >= len(assets):
        raise HTTPException(status_code=404, detail="media missing")
    asset = assets[index]
    media_root = os.environ.get("MEDIA_ROOT", "").strip()
    object_key = asset.get("object_key")
    if media_root and object_key:
        base = Path(media_root).resolve()
        candidate = Path(object_key)
        if not candidate.is_absolute():
            candidate = base / object_key
        try:
            candidate = candidate.resolve()
        except OSError:
            candidate = None
        # sandbox: the resolved path must live inside MEDIA_ROOT. This rejects path
        # traversal AND an absolute object_key that points outside the media store,
        # so a poisoned object_key can never exfiltrate an arbitrary host file.
        if (
            candidate is not None
            and candidate.is_file()
            and (candidate == base or str(candidate).startswith(str(base) + os.sep))
        ):
            return FileResponse(candidate, media_type=asset.get("mime") or "application/octet-stream")
    raise HTTPException(status_code=404, detail="media bytes unavailable")


@app.get("/v1/risk/state")
def v1_risk_state(authorization: str | None = Header(default=None)):
    require_reader(authorization)
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            cur.execute(
                "SELECT account_id, instrument_id, exposure_notional, open_risk_fraction, state "
                "FROM risk_state"
            )
            rows = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT payload FROM positions_projection WHERE status = 'open'")
            open_positions = [dict(r) for r in cur.fetchall()]
        modes = [str((r.get("state") or {}).get("mode") or "ACTIVE").upper() for r in rows]
        overall = "HALTED" if "HALTED" in modes else "REDUCING" if "REDUCING" in modes else "ACTIVE"
        total_open_risk = sum(_f(r.get("open_risk_fraction")) or 0.0 for r in rows)
        no_sl = sum(1 for p in open_positions if not (p.get("payload") or {}).get("stop_loss"))
        high_lev = sum(
            1 for p in open_positions
            if (_f((p.get("payload") or {}).get("leverage")) or 0) > 20
        )
        pair_locks = [
            {
                "instrument_symbol": _symbol(r.get("instrument_id")),
                "reason": str((r.get("state") or {}).get("mode")),
                "expires_at": None,
                "owner": "risk_governor",
            }
            for r in rows
            if str((r.get("state") or {}).get("mode") or "ACTIVE").upper() in ("HALTED", "REDUCING")
        ]
        blocking = []
        if overall == "HALTED":
            blocking.append("risk_state HALTED: no new risk")
        elif overall == "REDUCING":
            blocking.append("risk_state REDUCING: opening blocked")
        return {
            **env,
            "risk_state": overall,
            "state": overall,
            "run_mode": overall,
            "single_trade_risk_usage_pct": 0,
            "total_open_risk_usage_pct": total_open_risk * 100,
            "daily_loss_usage_pct": 0,
            "no_sl_trade_count": no_sl,
            "high_leverage_trade_count": high_lev,
            "blocking_reasons": blocking,
            "pair_locks": pair_locks,
        }
    finally:
        conn.close()


@app.get("/v1/risk/decisions")
def v1_risk_decisions(authorization: str | None = Header(default=None)):
    """The human-review queue: risk_decisions in needs_review joined to their Hermes
    decision so the dashboard can render the structured signal + classification."""
    require_reader(authorization)
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            cur.execute(
                """
                SELECT hd.decision_id, hd.raw_message_id, hd.created_at,
                       hd.message_type::text AS message_type, hd.action::text AS action,
                       hd.ambiguous, hd.ambiguity_reasons, hd.instrument_symbol,
                       hd.side::text AS side, hd.entry_type::text AS entry_type,
                       hd.entry_price, hd.stop_loss, hd.take_profits, hd.leverage, hd.confidence,
                       rd.risk_decision_id, rd.status::text AS risk_status, rd.reason AS risk_reason
                FROM risk_decisions rd
                JOIN hermes_decisions hd ON hd.decision_id = rd.hermes_decision_id
                WHERE rd.status = 'needs_review'
                ORDER BY hd.created_at DESC LIMIT 100
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
        decisions = []
        for r in rows:
            reasons = r.get("ambiguity_reasons") or []
            decisions.append({
                "decision_id": str(r["decision_id"]),
                "risk_decision_id": str(r["risk_decision_id"]),
                "raw_message_id": str(r["raw_message_id"]),
                "created_at": _iso(r.get("created_at")),
                "classification": {
                    "action": r.get("action"),
                    "message_type": r.get("message_type"),
                    "conclusion": r.get("action"),
                    "confidence": str(_f(r.get("confidence")) or ""),
                    "ambiguity_reasons": reasons,
                    "reason_codes": reasons,
                    "proposal_types": [],
                },
                "intent": {
                    "instrument_symbol": r.get("instrument_symbol"),
                    "side": r.get("side"),
                    "leverage": _f(r.get("leverage")),
                    "stop_loss": _f(r.get("stop_loss")),
                    "take_profits": [_f(x) for x in (r.get("take_profits") or [])],
                    "entry": {"type": r.get("entry_type"), "price": _f(r.get("entry_price"))},
                },
                "risk_status": r.get("risk_status"),
                "reason": r.get("risk_reason"),
            })
        return {**env, "decisions": decisions}
    finally:
        conn.close()


def _review_decide(decision_id: str, new_status: str, body: dict, role: str) -> dict:
    sid = _valid_uuid(decision_id)
    if sid is None:
        raise HTTPException(status_code=404, detail="decision not found")
    reason = (body.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason required")
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT risk_decision_id, status::text AS status FROM risk_decisions "
                "WHERE hermes_decision_id = %s OR risk_decision_id = %s "
                "ORDER BY decided_at DESC LIMIT 1 FOR UPDATE",
                (sid, sid),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="decision not found")
            risk_decision_id = str(row["risk_decision_id"])
            previous = row["status"]
            cur.execute(
                "UPDATE risk_decisions SET status = %s, reason = %s, decided_by = %s, decided_at = now() "
                "WHERE risk_decision_id = %s",
                (new_status, f"manual_review:{reason}", f"reviewer:{role}", risk_decision_id),
            )
        _cp_paths()
        from audit import record_audit_event

        record_audit_event(
            conn, event_type=f"manual_review.{new_status}", aggregate_type="risk_decision",
            aggregate_id=risk_decision_id, actor=f"reviewer:{role}",
            payload={"reason": reason, "decision_id": sid, "from_status": previous, "to_status": new_status},
        )
        conn.commit()
        return {"ok": True, "risk_decision_id": risk_decision_id, "status": new_status,
                "from_status": previous}
    finally:
        conn.close()


@app.post("/v1/risk/decisions/{decision_id}/approve")
def v1_review_approve(decision_id: str, body: dict = Body(default={}),
                      authorization: str | None = Header(default=None)):
    """Reviewer marks a needs_review decision approved. Records an immutable audit trail
    and lifts the item out of the queue. Execution still flows only through the
    deterministic gateway path — a manual approval does not itself emit a trade intent."""
    role = require_reader(authorization)
    if role not in ("reviewer", "risk_admin"):
        raise HTTPException(status_code=403, detail="reviewer required")
    return _review_decide(decision_id, "approved", body, role)


@app.post("/v1/risk/decisions/{decision_id}/reject")
def v1_review_reject(decision_id: str, body: dict = Body(default={}),
                     authorization: str | None = Header(default=None)):
    role = require_reader(authorization)
    if role not in ("reviewer", "risk_admin"):
        raise HTTPException(status_code=403, detail="reviewer required")
    return _review_decide(decision_id, "rejected", body, role)


# ---------------------------------------------------------------------------
# Operator order entry (Hermes agent = the decision maker; added 2026-07-02)
#
# Hermes decides WHETHER to trade (from channel signals or the user's verbal
# instruction) and places the order HERE — never on the exchange directly.
# This endpoint writes the same audit chain the decision gateway wrote
# (raw_message -> processing_run -> context_snapshot -> hermes_decision ->
# risk_decision -> approved trade_intent), so the nautilus node pulls and
# executes it exactly like any other approved intent (JP-routed egress,
# hedge-mode planner, reduce-only exits). Caps are enforced server-side and
# fail closed because the caller is an LLM agent.
# ---------------------------------------------------------------------------

_OPERATOR_ACTIONS = (
    "open_position",
    "close_position",
    "partial_close",
    "move_stop_loss",
    "replace_take_profits",
    "cancel_order",
)
# Protection management: no new exposure (node places reduce-only orders sized to
# the live position), so these skip notional sizing entirely.
_OPERATOR_PROTECT_ACTIONS = ("move_stop_loss", "replace_take_profits")
_OPERATOR_MANAGEMENT_ACTIONS = (
    "close_position",
    "partial_close",
    "move_stop_loss",
    "replace_take_profits",
    "cancel_order",
)
_EXECUTABLE_PARENT_ACTIONS = frozenset(
    {
        "open_position",
        "add_position",
        "partial_close",
        "close_position",
        "move_stop_loss",
        "move_stop_to_entry",
        "replace_take_profits",
        "cancel_order",
    }
)
_DEFAULT_OPERATOR_ACCOUNTS = (
    "account-a",
    "account-b",
    "account-c",
    "account-d",
)
_OPERATOR_ACCOUNT_ID_RE = re.compile(r"^account-[a-z0-9][a-z0-9-]{0,31}$")
_ORDER_AUTHORIZATION_TYPES = ("user", "channel")
_INTERNAL_ORDER_SERVICE_RE = re.compile(r"internal|watchdog|reconciler", re.IGNORECASE)
_TG_SIGNAL_REF_RE = re.compile(
    r"(?:^|-)tg-sig-c(?P<channel>\d+)-m(?P<message>\d+)(?:-e\d+)?(?:$|-)"
)
_DEFAULT_WATCHER_TRADING_DB = (
    "/var/lib/docker/volumes/trader_signal-data/_data/watcher-trading.db"
)
_WATCHER_TRADING_DB_ENVS = (
    "TRADER_TRADING_DB_PATH",
    "WATCHER_TRADING_DB",
    "TRADING_DB_PATH",
)


def _resolve_watcher_trading_db_path(env: dict[str, str] | None = None) -> str:
    if env is None:
        env = os.environ
    configured = []
    for name in _WATCHER_TRADING_DB_ENVS:
        value = str(env.get(name) or "").strip()
        if value:
            configured.append((name, value))
    if not configured:
        return _DEFAULT_WATCHER_TRADING_DB
    canonical_name, canonical_value = configured[0]
    for name, value in configured[1:]:
        if value != canonical_value:
            raise RuntimeError(
                "conflicting trading DB path environment: "
                f"{canonical_name}={canonical_value} {name}={value}"
            )
    return canonical_value


_WATCHER_TRADING_DB = _resolve_watcher_trading_db_path()
_ATTRIBUTION_SHADOW_LOG = "/srv/trader-v3/logs/attribution-shadow.jsonl"


def _operator_account_registry() -> dict[str, dict]:
    raw = os.environ.get("OPERATOR_ACCOUNT_REGISTRY_JSON", "").strip()
    if not raw:
        return {
            account_id: {}
            for account_id in _DEFAULT_OPERATOR_ACCOUNTS
        }
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="operator account registry JSON is invalid",
        ) from exc
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(
            status_code=503,
            detail="operator account registry must be a non-empty object",
        )

    registry: dict[str, dict] = {}
    for raw_account_id, raw_config in payload.items():
        account_id = str(raw_account_id or "").strip()
        if not _OPERATOR_ACCOUNT_ID_RE.fullmatch(account_id):
            raise HTTPException(
                status_code=503,
                detail="operator account registry contains an invalid account_id",
            )
        if not isinstance(raw_config, dict):
            raise HTTPException(
                status_code=503,
                detail=f"operator account registry entry for {account_id} is invalid",
            )
        if "effective_equity" in raw_config:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"operator account registry {account_id}.effective_equity "
                    "is unsupported; configure risk_capital_multiplier in the "
                    "watcher account registry"
                ),
            )
        registry[account_id] = {}
    return registry


def _operator_accounts() -> tuple[str, ...]:
    return tuple(_operator_account_registry())


def _watcher_account_is_enabled(row, account_columns: set[str]) -> bool:
    for field_name in ("is_enabled", "enabled"):
        if field_name not in account_columns:
            continue
        value = str(row[field_name] or "").strip().lower()
        if value not in {"1", "active", "enabled", "true"}:
            return False
    if "status" not in account_columns:
        return True
    status = str(row["status"] or "").strip().lower()
    return not status or status in {"1", "active", "enabled", "true"}


def _channel_risk_capital_multiplier(
    channel_id: str,
    account_id: str,
) -> float:
    import sqlite3

    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        raise HTTPException(
            status_code=503,
            detail="channel risk route has no channel_id",
        )
    try:
        conn = sqlite3.connect(
            f"file:{_WATCHER_TRADING_DB}?mode=ro",
            uri=True,
        )
        conn.row_factory = sqlite3.Row
        try:
            account_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(account_configs)"
                ).fetchall()
            }
            route_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(channel_routing)"
                ).fetchall()
            }
            required_account_columns = {
                "account_id",
                "account_type",
                "parent_account_id",
                "execution_account_id",
                "risk_capital_multiplier",
            }
            if not required_account_columns <= account_columns:
                raise HTTPException(
                    status_code=503,
                    detail="watcher account routing schema is unavailable",
                )
            if not {"channel_id", "target_account_id"} <= route_columns:
                raise HTTPException(
                    status_code=503,
                    detail="watcher channel routing schema is unavailable",
                )
            fields = [
                "route.target_account_id AS target_account_id",
                "account.account_id AS account_id",
                "account.execution_account_id AS execution_account_id",
                "(SELECT COUNT(*) FROM account_configs AS candidate "
                "WHERE candidate.execution_account_id = "
                "account.execution_account_id) AS execution_account_count",
                "account.account_type AS account_type",
                "account.parent_account_id AS parent_account_id",
                "(SELECT COUNT(*) FROM account_configs AS parent "
                "WHERE parent.account_id = account.parent_account_id "
                "AND lower(trim(parent.account_type)) = 'main') "
                "AS parent_main_account_count",
                "account.risk_capital_multiplier "
                "AS risk_capital_multiplier",
            ]
            for field_name in ("is_enabled", "enabled", "status"):
                if field_name in account_columns:
                    fields.append(f"account.{field_name} AS {field_name}")
            rows = conn.execute(
                "SELECT "
                + ", ".join(fields)
                + " FROM channel_routing AS route "
                + "LEFT JOIN account_configs AS account "
                + "ON account.account_id = route.target_account_id "
                + "WHERE route.channel_id = ?",
                (normalized_channel_id,),
            ).fetchall()
        finally:
            conn.close()
    except HTTPException:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=503,
            detail="watcher channel risk route is unavailable",
        ) from exc

    if len(rows) != 1:
        raise HTTPException(
            status_code=503,
            detail=(
                f"channel {normalized_channel_id} must resolve to exactly "
                "one execution account"
            ),
        )
    row = rows[0]
    target_account_id = str(row["target_account_id"] or "").strip()
    credential_account_id = str(row["account_id"] or "").strip()
    execution_account_id = str(
        row["execution_account_id"] or ""
    ).strip()
    if not credential_account_id or target_account_id != credential_account_id:
        raise HTTPException(
            status_code=503,
            detail="watcher channel route target account is invalid",
        )
    if execution_account_id != account_id:
        raise HTTPException(
            status_code=409,
            detail="watcher channel route conflicts with requested account_id",
        )
    try:
        execution_account_count = int(row["execution_account_count"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="watcher execution account identity is invalid",
        ) from exc
    if execution_account_count != 1:
        raise HTTPException(
            status_code=503,
            detail="watcher execution account identity must be unique",
        )
    account_type = str(row["account_type"] or "").strip().lower()
    parent_account_id = str(row["parent_account_id"] or "").strip()
    if account_type == "main":
        if parent_account_id:
            raise HTTPException(
                status_code=503,
                detail="watcher main account hierarchy is invalid",
            )
    elif account_type == "subaccount":
        try:
            parent_main_account_count = int(row["parent_main_account_count"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail="watcher subaccount parent identity is invalid",
            ) from exc
        if not parent_account_id or parent_main_account_count != 1:
            raise HTTPException(
                status_code=503,
                detail=(
                    "watcher subaccount parent must resolve to exactly "
                    "one main account"
                ),
            )
    else:
        raise HTTPException(
            status_code=503,
            detail="watcher account type is invalid",
        )
    if not _watcher_account_is_enabled(row, account_columns):
        raise HTTPException(
            status_code=503,
            detail="watcher channel route target account is disabled",
        )
    try:
        multiplier = float(row["risk_capital_multiplier"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="watcher channel risk capital multiplier is invalid",
        ) from exc
    if not math.isfinite(multiplier) or multiplier <= 0:
        raise HTTPException(
            status_code=503,
            detail="watcher channel risk capital multiplier is invalid",
        )
    return multiplier


def _account_risk_capital_multiplier(account_id: str) -> float:
    import sqlite3

    try:
        conn = sqlite3.connect(
            f"file:{_WATCHER_TRADING_DB}?mode=ro",
            uri=True,
        )
        conn.row_factory = sqlite3.Row
        try:
            account_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(account_configs)"
                ).fetchall()
            }
            required_account_columns = {
                "account_id",
                "account_type",
                "parent_account_id",
                "execution_account_id",
                "risk_capital_multiplier",
            }
            if not required_account_columns <= account_columns:
                raise HTTPException(
                    status_code=503,
                    detail="watcher account risk schema is unavailable",
                )
            fields = [
                "account.account_id AS account_id",
                "account.account_type AS account_type",
                "account.parent_account_id AS parent_account_id",
                "(SELECT COUNT(*) FROM account_configs AS parent "
                "WHERE parent.account_id = account.parent_account_id "
                "AND lower(trim(parent.account_type)) = 'main') "
                "AS parent_main_account_count",
                "account.execution_account_id AS execution_account_id",
                "account.risk_capital_multiplier "
                "AS risk_capital_multiplier",
            ]
            for field_name in ("is_enabled", "enabled", "status"):
                if field_name in account_columns:
                    fields.append(f"account.{field_name} AS {field_name}")
            rows = conn.execute(
                "SELECT "
                + ", ".join(fields)
                + " FROM account_configs AS account "
                + "WHERE account.execution_account_id = ?",
                (account_id,),
            ).fetchall()
        finally:
            conn.close()
    except HTTPException:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=503,
            detail="watcher account risk configuration is unavailable",
        ) from exc

    if len(rows) != 1:
        raise HTTPException(
            status_code=503,
            detail=(
                f"execution account {account_id} must resolve to exactly "
                "one risk configuration"
            ),
        )
    row = rows[0]
    account_type = str(row["account_type"] or "").strip().lower()
    parent_account_id = str(row["parent_account_id"] or "").strip()
    if account_type == "main":
        if parent_account_id:
            raise HTTPException(
                status_code=503,
                detail="watcher main account hierarchy is invalid",
            )
    elif account_type == "subaccount":
        try:
            parent_main_account_count = int(row["parent_main_account_count"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail="watcher subaccount parent identity is invalid",
            ) from exc
        if not parent_account_id or parent_main_account_count != 1:
            raise HTTPException(
                status_code=503,
                detail=(
                    "watcher subaccount parent must resolve to exactly "
                    "one main account"
                ),
            )
    else:
        raise HTTPException(
            status_code=503,
            detail="watcher account type is invalid",
        )
    if not _watcher_account_is_enabled(row, account_columns):
        raise HTTPException(
            status_code=503,
            detail="watcher account risk configuration is disabled",
        )
    try:
        multiplier = float(row["risk_capital_multiplier"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="watcher account risk capital multiplier is invalid",
        ) from exc
    if not math.isfinite(multiplier) or multiplier <= 0:
        raise HTTPException(
            status_code=503,
            detail="watcher account risk capital multiplier is invalid",
        )
    return multiplier


def _channel_from_signal_ref(value) -> str | bool:
    ref = str(value or "").strip()
    if not ref:
        return False
    match = _TG_SIGNAL_REF_RE.search(ref)
    if not match:
        return False
    return "-" + match.group("channel")


def _open_source_channel(body: dict, client_ref: str) -> tuple[str, bool]:
    source_channel = str(body.get("source_channel") or "").strip()
    ref_channel = _channel_from_signal_ref(client_ref)
    if source_channel and ref_channel and source_channel != ref_channel:
        raise HTTPException(
            status_code=400,
            detail=f"source_channel conflicts with client_ref channel {ref_channel}",
        )
    if source_channel:
        return source_channel, True
    if ref_channel:
        return str(ref_channel), True
    return "hermes-operator", False


def _authorized_parent(
    database_url: str,
    parent_intent_id: str,
    account_id: str,
    instrument_id: str,
) -> dict:
    try:
        UUID(parent_intent_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="parent_intent_id must be a uuid")

    conn = _database_connection(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT account_id, instrument_id, action::text, status::text, "
                "order_plan, target_position_id "
                "FROM trade_intents "
                "WHERE intent_id::text=%s",
                (parent_intent_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(
            status_code=400,
            detail="parent_intent_id does not reference an authorized trade intent",
        )
    parent_account, parent_instrument, parent_action, parent_status, parent_plan = row[:5]
    parent_target_position_id = row[5] if len(row) > 5 else False
    if parent_account != account_id:
        raise HTTPException(
            status_code=400,
            detail="parent_intent_id account does not match account_id",
        )
    normalized_parent = _attribution_symbol(parent_instrument)
    normalized_current = _attribution_symbol(instrument_id)
    if normalized_parent != normalized_current:
        raise HTTPException(
            status_code=400,
            detail=(
                f"parent_intent_id instrument does not match request instrument "
                f"{normalized_current}"
            ),
        )
    if str(parent_status or "").lower() != "approved":
        raise HTTPException(
            status_code=400,
            detail="parent_intent_id status is not executable",
        )
    if str(parent_action or "").lower() not in _EXECUTABLE_PARENT_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail="parent_intent_id action is not executable",
        )
    plan = parent_plan or {}
    parent_authorization = plan.get("authorization")
    if not isinstance(parent_authorization, dict):
        raise HTTPException(
            status_code=400,
            detail="parent_intent_id has no auditable authorization evidence",
        )
    parent_type = str(parent_authorization.get("authorized_by_type") or "").strip()
    parent_id = str(parent_authorization.get("authorized_by_id") or "").strip()
    parent_message = str(parent_authorization.get("source_message_id") or "").strip()
    if parent_type not in _ORDER_AUTHORIZATION_TYPES:
        raise HTTPException(
            status_code=400,
            detail="parent_intent_id authorization type is invalid",
        )
    if not parent_id or not parent_message:
        raise HTTPException(
            status_code=400,
            detail="parent_intent_id authorization evidence is incomplete",
        )
    parent_side = str(
        plan.get("position_side")
        or plan.get("side")
        or ""
    ).strip().lower()
    target_position_id = str(parent_target_position_id or "").strip()
    if not target_position_id and parent_side in ("long", "short"):
        target_position_id = str(
            _canonical_position_id(
                _nautilus_instrument_id(parent_instrument),
                parent_side,
            )
            or ""
        )
    result = dict(parent_authorization)
    result["_target_position_id"] = target_position_id or False
    return result


def _order_authorization(
    body: dict,
    database_url: str,
    account_id: str,
    instrument_id: str,
    reason: str,
    source: str,
    authenticated_actor_id: str,
    request_id: str,
    client_ref: str,
) -> dict:
    authorized_by_type = str(body.get("authorized_by_type") or "").strip().lower()
    authorized_by_id = str(body.get("authorized_by_id") or "").strip()
    source_message_id = str(body.get("source_message_id") or "").strip()
    created_by_service = str(body.get("created_by_service") or "").strip()
    parent_intent_id = str(body.get("parent_intent_id") or "").strip()
    is_internal = _INTERNAL_ORDER_SERVICE_RE.search(created_by_service) is not None
    if is_internal and not parent_intent_id:
        raise HTTPException(
            status_code=400,
            detail="internal/watchdog/reconciler source requires parent_intent_id",
        )
    if parent_intent_id:
        if not created_by_service:
            raise HTTPException(status_code=400, detail="created_by_service required")
        if source != created_by_service:
            raise HTTPException(
                status_code=400,
                detail="source must match created_by_service",
            )
        parent_authorization = _authorized_parent(
            database_url,
            parent_intent_id,
            account_id,
            instrument_id,
        )
        parent_type = str(parent_authorization.get("authorized_by_type") or "").strip()
        parent_id = str(parent_authorization.get("authorized_by_id") or "").strip()
        parent_message = str(parent_authorization.get("source_message_id") or "").strip()
        target_position_id = parent_authorization.get("_target_position_id") or False
        if (
            authorized_by_type != parent_type
            or authorized_by_id != parent_id
            or source_message_id != parent_message
        ):
            raise HTTPException(
                status_code=400,
                detail="authorization evidence does not match parent_intent_id",
            )
        return {
            "authorized_by_type": parent_type,
            "authorized_by_id": parent_id,
            "reason": reason,
            "source_message_id": parent_message,
            "created_by_service": created_by_service,
            "parent_intent_id": parent_intent_id,
            "_target_position_id": target_position_id,
        }

    if authorized_by_type == "channel":
        if not authorized_by_id:
            raise HTTPException(status_code=400, detail="authorized_by_id required")
        if not source_message_id:
            raise HTTPException(status_code=400, detail="source_message_id required")
        if not created_by_service:
            raise HTTPException(status_code=400, detail="created_by_service required")
        if source != created_by_service:
            raise HTTPException(
                status_code=400,
                detail="source must match created_by_service",
            )
        return {
            "authorized_by_type": "channel",
            "authorized_by_id": authorized_by_id,
            "reason": reason,
            "source_message_id": source_message_id,
            "created_by_service": created_by_service,
            "parent_intent_id": False,
        }

    if authorized_by_type and authorized_by_type != "user":
        raise HTTPException(
            status_code=400,
            detail=f"authorized_by_type must be one of {list(_ORDER_AUTHORIZATION_TYPES)}",
        )
    if not request_id:
        raise HTTPException(
            status_code=400,
            detail="authenticated user order requires X-Request-Id or client_ref",
        )
    return {
        "authorized_by_type": "user",
        "authorized_by_id": authenticated_actor_id,
        "reason": reason,
        "source_message_id": client_ref or request_id,
        "created_by_service": "control-plane",
        "parent_intent_id": False,
    }


_AUTHORIZATION_REPLAY_FIELDS = (
    "authorized_by_type",
    "authorized_by_id",
    "source_message_id",
    "created_by_service",
    "parent_intent_id",
    "reason",
)


def _stable_authorization_evidence(authorization: object) -> dict:
    if not isinstance(authorization, dict):
        return {}
    return {
        field: authorization.get(field)
        for field in _AUTHORIZATION_REPLAY_FIELDS
    }


def _canonical_order_request(body: dict, authorization: dict) -> dict:
    canonical = dict(body)
    canonical.update(
        {
            "authorized_by_type": authorization["authorized_by_type"],
            "authorized_by_id": authorization["authorized_by_id"],
            "source_message_id": authorization["source_message_id"],
            "created_by_service": authorization["created_by_service"],
            "source": authorization["created_by_service"],
        }
    )
    return canonical


def _operator_request_semantics(
    *,
    action: str,
    account_id: str,
    symbol: str,
    position_side: str | None,
    client_ref: str,
    order_plan: dict,
    target_position_id: str | bool,
    body: dict,
    valid_seconds: int,
) -> dict:
    semantic_plan = {
        key: value
        for key, value in order_plan.items()
        if key not in {
            "authorization",
            "attribution",
            "canary_permit",
            "equity",
            "request_semantics",
        }
    }
    payload = {
        "version": "operator-management-v1",
        "action": action,
        "account_id": account_id,
        "instrument_id": symbol,
        "position_side": position_side or False,
        "client_ref": client_ref,
        "entry_ref": str(body.get("entry_ref") or "").strip() or False,
        "channel": str(body.get("channel") or "").strip() or False,
        "parent_intent_id": str(body.get("parent_intent_id") or "").strip() or False,
        "target_position_id": target_position_id or False,
        "valid_seconds": valid_seconds,
        "order_plan": semantic_plan,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode()
    return {
        "version": payload["version"],
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _operator_open_request_semantics(
    *,
    body: dict,
    account_id: str,
    symbol: str,
    client_ref: str,
    authorization: dict,
) -> dict:
    canonical = _canonical_order_request(body, authorization)
    canonical.pop("live_open_gate", None)
    payload = {
        "version": "operator-open-v1",
        "action": "open_position",
        "account_id": account_id,
        "instrument_id": symbol,
        "client_ref": client_ref,
        "request": canonical,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode()
    return {
        "version": payload["version"],
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _operator_open_replay(
    database_url: str,
    *,
    account_id: str,
    symbol: str,
    client_ref: str,
    authorization: dict,
    request_semantics: dict,
) -> dict | bool:
    idempotency_key = hashlib.sha256(
        f"operator|{account_id}|{client_ref}".encode()
    ).hexdigest()
    conn = _database_connection(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT intent_id::text,
                       status::text,
                       valid_until,
                       order_plan,
                       risk_budget,
                       target_position_id
                FROM trade_intents
                WHERE idempotency_key=%s
                """,
                (idempotency_key,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    order_plan = row[3]
    if not isinstance(order_plan, dict):
        raise HTTPException(
            status_code=409,
            detail="persisted open intent order_plan is invalid",
        )
    persisted_authorization = order_plan.get("authorization")
    if (
        _stable_authorization_evidence(persisted_authorization)
        != _stable_authorization_evidence(authorization)
    ):
        raise HTTPException(
            status_code=409,
            detail="idempotency key authorization evidence mismatch",
        )
    persisted_semantics = order_plan.get("request_semantics")
    if not isinstance(persisted_semantics, dict):
        raise HTTPException(
            status_code=409,
            detail=(
                "legacy idempotency record has no request semantics; "
                "retry with a new client_ref"
            ),
        )
    if persisted_semantics.get("sha256") != request_semantics["sha256"]:
        raise HTTPException(
            status_code=409,
            detail="idempotency key request payload mismatch",
        )
    risk_budget = row[4]
    if not isinstance(risk_budget, dict):
        risk_budget = {}
    return {
        "intent_id": row[0],
        "status": row[1],
        "replay": True,
        "account_id": account_id,
        "instrument_id": symbol,
        "action": "open_position",
        "order_plan": order_plan,
        "execution_preview": _safe_execution_preview(
            order_plan,
            risk_budget,
            symbol,
            "open_position",
        ),
        "risk_budget": risk_budget,
        "valid_until": (
            row[2].isoformat()
            if row[2]
            else None
        ),
        "authorization": persisted_authorization,
        "target_position_id": row[5] or False,
    }


def _write_attribution_shadow(event: dict) -> None:
    path = os.environ.get("ATTRIBUTION_SHADOW_LOG", _ATTRIBUTION_SHADOW_LOG)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        print(f"attribution shadow log write failed: {exc}", file=sys.stderr)


def _attribution_intent(database_url: str, account_id: str, entry_ref: str):
    accounts = [account_id]
    for candidate in _operator_accounts():
        if candidate != account_id:
            accounts.append(candidate)
    conn = _database_connection(database_url)
    try:
        with conn.cursor() as cur:
            for candidate in accounts:
                idem = hashlib.sha256(
                    f"operator|{candidate}|{entry_ref}".encode()
                ).hexdigest()
                cur.execute(
                    "SELECT ti.intent_id::text, ti.action::text, ti.instrument_id, "
                    "ti.account_id, ti.order_plan, rm.channel_id, rm.source_message_id "
                    "FROM trade_intents ti "
                    "JOIN hermes_decisions hd ON hd.decision_id = ti.hermes_decision_id "
                    "JOIN raw_messages rm ON rm.id = hd.raw_message_id "
                    "WHERE ti.idempotency_key=%s",
                    (idem,),
                )
                row = cur.fetchone()
                if row:
                    return row
    finally:
        conn.close()
    return False


def _channel_management_entry_account(
    database_url: str,
    *,
    requested_account_id: str,
    symbol: str,
    channel: str,
    entry_ref: str,
    position_side: str | None,
) -> str:
    if not entry_ref or not channel or channel == "operator":
        return requested_account_id
    row = _attribution_intent(
        database_url,
        requested_account_id,
        entry_ref,
    )
    if not row:
        return requested_account_id

    _, entry_action, entry_symbol, entry_account, order_plan, raw_channel, source_ref = row
    owner_channel = str(raw_channel or "").strip()
    if owner_channel == "hermes-operator":
        parsed_channel = _channel_from_signal_ref(source_ref)
        if parsed_channel:
            owner_channel = str(parsed_channel)
    entry_plan = order_plan or {}
    entry_side = str(entry_plan.get("side") or "").strip().lower()
    identity_matches = (
        entry_action == "open_position"
        and _attribution_symbol(entry_symbol) == symbol
        and owner_channel == channel
    )
    if position_side:
        identity_matches = identity_matches and entry_side == position_side
    if not identity_matches:
        return requested_account_id
    normalized_account = str(entry_account or "").strip()
    if normalized_account not in _operator_accounts():
        return requested_account_id
    return normalized_account


def _cancel_order_owner(
    database_url: str,
    *,
    client_order_id: str,
    account_id: str | bool,
    symbol: str,
) -> dict:
    intent_id = str(UUID(hex=client_order_id[1:33]))
    conn = _database_connection(database_url)
    try:
        with conn.cursor() as cur:
            account_predicate = ""
            params: tuple = (
                client_order_id,
                intent_id,
                symbol,
                symbol,
            )
            if account_id:
                account_predicate = (
                    " AND op.account_id=%s"
                    " AND ti.account_id=%s"
                )
                params = (
                    client_order_id,
                    account_id,
                    account_id,
                    intent_id,
                    symbol,
                    symbol,
                )
            cur.execute(
                f"""
                SELECT ti.intent_id::text,
                       ti.account_id,
                       ti.instrument_id,
                       ti.order_plan,
                       rm.channel_id,
                       rm.source_message_id
                FROM orders_projection AS op
                JOIN trade_intents AS ti
                  ON ti.intent_id = op.intent_id
                JOIN hermes_decisions AS hd
                  ON hd.decision_id = ti.hermes_decision_id
                JOIN raw_messages AS rm
                  ON rm.id = hd.raw_message_id
                WHERE op.client_order_id=%s
                  AND op.account_id=ti.account_id
                  {account_predicate}
                  AND ti.intent_id::text=%s
                  AND upper(split_part(op.instrument_id, '-', 1))=%s
                  AND upper(split_part(ti.instrument_id, '-', 1))=%s
                """,
                params,
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if len(rows) != 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "client_order_id does not belong to the requested "
                "account and instrument"
            ),
        )
    row = rows[0]
    order_plan = row[3] or {}
    authorization = order_plan.get("authorization")
    if not isinstance(authorization, dict):
        raise HTTPException(
            status_code=400,
            detail="cancel_order owner intent has no authorization evidence",
        )
    owner_channel = str(row[4] or "").strip()
    if owner_channel == "hermes-operator":
        parsed_channel = _channel_from_signal_ref(row[5])
        if parsed_channel:
            owner_channel = str(parsed_channel)
    return {
        "intent_id": str(row[0]),
        "account_id": str(row[1]),
        "instrument_id": str(row[2]),
        "owner_channel": owner_channel or False,
        "authorization": authorization,
    }


def _attribution_symbol(value) -> str:
    return str(value or "").upper().split("-")[0].split(".")[0]


def _resolve_attribution(database_url: str, action: str, symbol: str,
                         account_id: str, channel: str, entry_ref: str,
                         position_side: str | None) -> tuple[dict, str | bool, str | bool]:
    resolution = "none"
    owner_channel: str | bool = False
    channel_match: bool | str = "unknown"
    errors = []
    hard_error: str | bool = False
    target_position_id: str | bool = False
    bypass = channel == "operator"

    row = False
    if entry_ref:
        try:
            row = _attribution_intent(database_url, account_id, entry_ref)
        except Exception as exc:
            errors.append(f"shadow_lookup_failed:{str(exc)[:160]}")
    else:
        errors.append("entry_ref_missing")

    if row:
        resolution = "intent"
        _, entry_action, entry_symbol, entry_account, order_plan, raw_channel, source_ref = row
        owner_channel = str(raw_channel or "").strip()
        if owner_channel == "hermes-operator":
            parsed_channel = _channel_from_signal_ref(source_ref)
            if parsed_channel:
                owner_channel = parsed_channel
        if entry_action != "open_position":
            errors.append(f"entry_action:{entry_action}")
        if _attribution_symbol(entry_symbol) != symbol:
            errors.append(f"instrument_mismatch:{entry_symbol}")
            hard_error = (
                f"entry_ref instrument {entry_symbol} does not match request symbol {symbol}"
            )
        if entry_account != account_id:
            errors.append(f"account_mismatch:{entry_account}")
            hard_error = (
                f"entry_ref account {entry_account} does not match request account {account_id}"
            )
        entry_plan = order_plan or {}
        entry_side = str(entry_plan.get("side") or "").lower()
        if entry_side in ("long", "short"):
            target_position_id = (
                _canonical_position_id(
                    _nautilus_instrument_id(entry_symbol),
                    entry_side,
                )
                or False
            )
        if position_side:
            if entry_side != position_side:
                errors.append(f"position_side_mismatch:{entry_side or 'unknown'}")
        elif entry_side:
            errors.append("position_side_missing")
    elif entry_ref:
        parsed_channel = _channel_from_signal_ref(entry_ref)
        if parsed_channel:
            resolution = "ref_parse_only"
            owner_channel = parsed_channel
            errors.append("entry_intent_not_found")
        else:
            errors.append("entry_ref_unresolved")

    if channel and owner_channel:
        channel_match = channel == owner_channel
        if channel_match is False:
            errors.append("channel_mismatch")
    elif not channel:
        errors.append("channel_missing")

    would_reject = bool(errors)
    if bypass:
        would_reject = False
    error: str | bool = False
    if errors:
        error = ";".join(errors)
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "symbol": symbol,
        "account": account_id,
        "channel": channel or False,
        "entry_ref": entry_ref or False,
        "resolution": resolution,
        "owner_channel": owner_channel,
        "channel_match": channel_match,
        "would_reject": would_reject,
        "bypass": bypass,
        "error": error,
    }
    return event, hard_error, target_position_id


def _operator_caps() -> dict:
    return {
        # optional extra fixed ceiling; unset/0 = disabled (risk cap governs)
        "max_notional": float(os.environ.get("OPERATOR_MAX_NOTIONAL_USDT", "0") or 0) or None,
        "max_leverage": float(os.environ.get("OPERATOR_MAX_LEVERAGE", "10")),
        # per-order max loss at stop, as fraction of account equity (user rule: 6%)
        "max_risk_fraction": float(os.environ.get("OPERATOR_MAX_RISK_FRACTION", "0.06")),
        # orders without a stop loss cannot be risk-checked: cap notional instead
        "no_sl_equity_fraction": float(os.environ.get("OPERATOR_NO_SL_EQUITY_FRACTION", "0.2")),
    }


def _account_financial_state(account_id: str) -> dict[str, float] | bool:
    """Return fresh real equity and available balance from the projection DB."""
    max_age_raw = os.environ.get("OPERATOR_EQUITY_MAX_AGE_SECONDS", "60")
    try:
        max_age_seconds = max(1.0, float(max_age_raw))
    except ValueError:
        max_age_seconds = 60.0
    if not math.isfinite(max_age_seconds) or max_age_seconds > 60:
        max_age_seconds = 60.0

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        return False
    try:
        conn = _database_connection(database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT equity, available_balance, "
                    "payload->>'account_snapshot_source', "
                    "payload->>'account_snapshot_fetched_at', "
                    "EXTRACT(EPOCH FROM ("
                    "now() - "
                    "(payload->>'account_snapshot_fetched_at')::timestamptz"
                    ")) "
                    "FROM accounts_projection WHERE account_id=%s "
                    "LIMIT 1",
                    (account_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        return False
    if not row or row[1] is None:
        return False
    snapshot_source = str(row[2] or "").strip()
    snapshot_fetched_at = str(row[3] or "").strip()
    if snapshot_source != "binance_fapi_account_v3":
        return False
    if not snapshot_fetched_at:
        return False
    try:
        equity = float(row[0])
        available_balance = float(row[1])
        age_seconds = float(row[4])
    except (TypeError, ValueError):
        return False
    if not all(
        math.isfinite(value)
        for value in (equity, available_balance, age_seconds)
    ):
        return False
    if equity <= 0 or available_balance < 0:
        return False
    if age_seconds < 0 or age_seconds > max_age_seconds:
        return False
    return {
        "real_equity": equity,
        "available_balance": available_balance,
    }


def _account_equity(account_id: str) -> float | None:
    state = _account_financial_state(account_id)
    if not state:
        return None
    return state["real_equity"]


def _symbol_risk_ratio(symbol: str) -> float:
    """Per-symbol risk fraction — the user's config in the watcher DB is the
    single source of truth (e.g. BTCUSDT 0.02 = risk 2% of equity per trade);
    symbols without a config default to 1%."""
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{_WATCHER_TRADING_DB}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT risk_ratio FROM symbol_risk_configs WHERE symbol=?", (symbol,)
            ).fetchone()
            if row and row[0] and 0 < float(row[0]) <= 0.1:
                return float(row[0])
        finally:
            conn.close()
    except Exception:
        pass
    return float(os.environ.get("OPERATOR_DEFAULT_RISK_RATIO", "0.01"))


def _size_open_order(explicit_notional, symbol, account_id, side, entry_type,
                     entry_price, entry_price_min, entry_price_max,
                     stop_loss, leverage, caps, checks,
                     risk_capital_multiplier=False) -> float:
    """Risk-based sizing: notional = equity * risk_ratio / stop_distance.
    Hard cap (fail closed): loss at stop <= max_risk_fraction of equity.
    Without a stop loss the order cannot be risk-checked, so an explicit
    notional is required and capped at no_sl_equity_fraction of equity."""
    state = _account_financial_state(account_id)
    if not state:
        raise HTTPException(
            status_code=503,
            detail=f"fresh account financial state unavailable for {account_id}",
        )
    real_equity = state["real_equity"]
    available_balance = state["available_balance"]
    try:
        multiplier = float(risk_capital_multiplier)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="account risk capital multiplier is unavailable",
        ) from exc
    if not math.isfinite(multiplier) or multiplier <= 0:
        raise HTTPException(
            status_code=503,
            detail="account risk capital multiplier is invalid",
        )
    effective_equity = real_equity * multiplier
    checks.append(
        {
            "name": "account_equity_basis",
            "passed": True,
            "real_equity": real_equity,
            "available_balance": available_balance,
            "effective_equity": effective_equity,
            "risk_capital_multiplier": multiplier,
        }
    )
    # price reference for the stop distance
    if entry_type == "limit":
        ref = entry_price
    elif entry_type == "zone":
        if stop_loss is not None:
            # conservative: the zone boundary FARTHEST from the stop gives the
            # largest loss per unit if filled there — size against that
            ref = (entry_price_min
                   if abs(entry_price_min - stop_loss) >= abs(entry_price_max - stop_loss)
                   else entry_price_max)
        else:
            ref = (entry_price_min + entry_price_max) / 2
    else:
        ref = _binance_mark_price(symbol)

    if entry_type == "market" and stop_loss is not None and not ref:
        raise HTTPException(
            status_code=503,
            detail=f"live mark price unavailable for {symbol} stop-loss sizing",
        )

    notional: float
    if stop_loss is not None and ref:
        if side == "short" and stop_loss <= ref:
            raise HTTPException(status_code=400, detail="short stop_loss must be above entry")
        if side == "long" and stop_loss >= ref:
            raise HTTPException(status_code=400, detail="long stop_loss must be below entry")
        stop_frac = abs(ref - stop_loss) / ref
        risk_ratio = _symbol_risk_ratio(symbol)
        auto = effective_equity * risk_ratio / stop_frac
        allow_canary_override = (
            caps.get("_canary_explicit_notional_override") is True
        )
        if (
            explicit_notional is not None
            and explicit_notional > auto + 1e-9
            and not allow_canary_override
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"explicit notional {explicit_notional:.1f}U exceeds "
                    f"dynamic auto sizing {auto:.1f}U"
                ),
            )
        notional = explicit_notional if explicit_notional is not None else auto
        max_risk = effective_equity * caps["max_risk_fraction"]
        est_risk = notional * stop_frac
        if est_risk > max_risk + 1e-9:
            raise HTTPException(
                status_code=400,
                detail=f"loss at stop ~{est_risk:.1f}U exceeds per-order risk cap "
                       f"{max_risk:.1f}U ({caps['max_risk_fraction']:.0%} of "
                       f"effective equity {effective_equity:.0f}U)",
            )
        checks.append({"name": "risk_sizing", "passed": True,
                       "real_equity": real_equity,
                       "effective_equity": effective_equity,
                       "risk_ratio": risk_ratio, "stop_frac": round(stop_frac, 5),
                       "sizing_price": ref, "notional": round(notional, 1),
                       "est_risk": round(est_risk, 1), "risk_cap": round(max_risk, 1),
                       "auto_sized": explicit_notional is None})
    else:
        if explicit_notional is None:
            raise HTTPException(
                status_code=400,
                detail="auto-sizing needs a stop_loss (and a resolvable price); "
                       "pass notional_usdt explicitly for stop-less orders",
            )
        ceiling = effective_equity * caps["no_sl_equity_fraction"]
        if explicit_notional > ceiling:
            raise HTTPException(
                status_code=400,
                detail=f"stop-less order notional {explicit_notional:.0f}U exceeds "
                       f"{ceiling:.0f}U ({caps['no_sl_equity_fraction']:.0%} of equity)",
            )
        notional = explicit_notional
        checks.append({"name": "no_sl_notional_cap", "passed": True,
                       "notional": notional, "ceiling": round(ceiling, 1)})

    hard_leverage = leverage or caps["max_leverage"]
    real_equity_ceiling = real_equity * hard_leverage
    if notional > real_equity_ceiling:
        raise HTTPException(
            status_code=400,
            detail=(
                f"notional {notional:.0f}U exceeds real_equity*leverage "
                f"{real_equity_ceiling:.0f}U"
            ),
        )
    available_ceiling = available_balance * hard_leverage
    if notional > available_ceiling:
        raise HTTPException(
            status_code=400,
            detail=(
                f"notional {notional:.0f}U exceeds available_balance*leverage "
                f"{available_ceiling:.0f}U"
            ),
        )
    checks.append(
        {
            "name": "real_funds_gate",
            "passed": True,
            "leverage": hard_leverage,
            "real_equity_ceiling": round(real_equity_ceiling, 1),
            "available_balance_ceiling": round(available_ceiling, 1),
        }
    )
    if caps["max_notional"] and notional > caps["max_notional"]:
        raise HTTPException(status_code=400,
                            detail=f"notional {notional:.0f}U exceeds OPERATOR_MAX_NOTIONAL_USDT {caps['max_notional']:.0f}U")
    return notional


def _validate_stop_direction(symbol: str, account_id: str, stop_loss: float,
                             position_side: str | None = None) -> None:
    """A stop on the wrong side of the mark price is rejected by Binance (-2021)
    only AFTER the node has already cancelled the old stop — validate up front.
    Soft check: skipped when the position or mark price cannot be resolved.
    position_side (long|short) narrows the check to one book in hedge mode."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return
    side = None
    try:
        conn = _database_connection(database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT side FROM positions_projection WHERE account_id=%s "
                    "AND instrument_id LIKE %s AND status='open' AND quantity::numeric != 0",
                    (account_id, symbol + "%"),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        if position_side:
            rows = [r for r in rows if str(r[0]).lower() == position_side]
        if len(rows) != 1:
            return  # none/ambiguous: let the node planner decide
        side = str(rows[0][0]).lower()
    except Exception:
        return
    mark = _binance_mark_price(symbol)
    if not mark:
        return
    if side == "long" and stop_loss >= mark:
        raise HTTPException(
            status_code=400,
            detail=f"stop_loss {stop_loss} is above mark {mark} for a long position",
        )
    if side == "short" and stop_loss <= mark:
        raise HTTPException(
            status_code=400,
            detail=f"stop_loss {stop_loss} is below mark {mark} for a short position",
        )


def _validate_take_profit_direction(symbol: str, account_id: str, take_profits: list,
                                    position_side: str | None = None) -> None:
    """A take-profit too close to or through the mark can execute immediately and
    cascade reduce-only exits. Soft check: skipped when the position or mark price
    cannot be resolved. position_side (long|short) narrows hedge-mode books."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return
    side = None
    try:
        conn = _database_connection(database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT side FROM positions_projection WHERE account_id=%s "
                    "AND instrument_id LIKE %s AND status='open' AND quantity::numeric != 0",
                    (account_id, symbol + "%"),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        if position_side:
            rows = [r for r in rows if str(r[0]).lower() == position_side]
        if len(rows) != 1:
            return  # none/ambiguous: let the node planner decide
        side = str(rows[0][0]).lower()
    except Exception:
        return
    mark = _binance_mark_price(symbol)
    if not mark:
        return
    if side == "long":
        min_price = mark * 1.001
        for tp in take_profits:
            price = float(tp["price"])
            if price <= min_price:
                raise HTTPException(
                    status_code=400,
                    detail=f"take_profit {price} is not at least 0.1% beyond mark "
                           f"{mark} for a long position",
                )
    if side == "short":
        max_price = mark * 0.999
        for tp in take_profits:
            price = float(tp["price"])
            if price >= max_price:
                raise HTTPException(
                    status_code=400,
                    detail=f"take_profit {price} is not at least 0.1% beyond mark "
                           f"{mark} for a short position",
                )


def _safe_execution_preview(order_plan, risk_budget, symbol, action):
    """The intent row is committed before the response is built: a preview failure
    must never turn a successfully-placed intent into an HTTP error."""
    try:
        return _execution_order_plan(order_plan, risk_budget, symbol, action=action)
    except Exception as exc:  # noqa: BLE001
        return {"preview_unavailable": str(exc)[:200]}


def _op_num(value, field: str, required: bool = False):
    if value is None:
        if required:
            raise HTTPException(status_code=400, detail=f"{field} required")
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be a number")
    if not math.isfinite(num) or num <= 0:
        raise HTTPException(status_code=400, detail=f"{field} must be > 0")
    return num


def _op_decimal(value, field: str, required: bool = False) -> Decimal | None:
    if value is None:
        if required:
            raise HTTPException(status_code=400, detail=f"{field} required")
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be a number")
    if not number.is_finite() or number <= 0:
        raise HTTPException(status_code=400, detail=f"{field} must be > 0")
    return number


def _lock_canary_permit(
    cur,
    *,
    raw_permit_id,
    account_id: str,
    symbol: str,
    notional: Decimal,
) -> dict:
    permit_id = _required_uuid(
        raw_permit_id,
        f"canary_permit_id is required for {account_id} open_position",
    )
    cur.execute(
        """
        SELECT account_id,
               symbol,
               max_notional_usdt,
               max_cumulative_loss_usdt,
               max_open_count,
               consumed_open_count,
               expires_at,
               release_id,
               testnet_emergency_close_evidence_sha256,
               testnet_emergency_close_verified_at,
               status,
               armed_node_id,
               portfolio_baseline_sha256,
               now()
        FROM live_canary_permits
        WHERE permit_id=%s
        FOR UPDATE
        """,
        (permit_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=409, detail="canary permit not found")
    (
        permit_account_id,
        permit_symbol,
        max_notional,
        max_cumulative_loss,
        max_open_count,
        consumed_open_count,
        expires_at,
        release_id,
        emergency_close_evidence_sha256,
        emergency_close_verified_at,
        permit_status,
        armed_node_id,
        portfolio_baseline_sha256,
        database_now,
    ) = row
    if permit_status == "consumed" or int(consumed_open_count) >= 1:
        raise HTTPException(
            status_code=409,
            detail="canary permit has already been consumed",
        )
    if permit_status != "armed":
        raise HTTPException(
            status_code=409,
            detail="canary permit is not armed",
        )
    if expires_at is None or expires_at <= database_now:
        raise HTTPException(status_code=409, detail="canary permit has expired")
    if (
        permit_account_id != account_id
        or _canonical_symbol(permit_symbol) != symbol
    ):
        raise HTTPException(
            status_code=409,
            detail="canary permit scope does not match order",
        )
    if int(max_open_count) != 1:
        raise HTTPException(
            status_code=409,
            detail="canary permit open count is invalid",
        )
    _validate_canary_permit_machine_evidence(
        max_notional=max_notional,
        max_cumulative_loss=max_cumulative_loss,
        emergency_close_evidence_sha256=emergency_close_evidence_sha256,
        emergency_close_verified_at=emergency_close_verified_at,
        database_now=database_now,
    )
    if (
        not isinstance(portfolio_baseline_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", portfolio_baseline_sha256) is None
    ):
        raise HTTPException(
            status_code=409,
            detail="canary permit portfolio baseline is invalid",
        )
    if notional > Decimal(str(max_notional)):
        raise HTTPException(
            status_code=400,
            detail="canary notional exceeds permit limit",
        )

    node_id = str(armed_node_id or "").strip()
    if not node_id:
        cur.execute(
            """
            SELECT node_id
            FROM node_heartbeats
            WHERE account_id=%s
              AND status='ACTIVE'
              AND last_seen_at >= (
                  now() - make_interval(secs => %s)
              )
            ORDER BY node_id
            FOR SHARE
            """,
            (account_id, _command_target_max_age_seconds()),
        )
        active_nodes = [str(active_row[0]) for active_row in cur.fetchall()]
        if len(active_nodes) != 1:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{account_id} requires exactly one fresh ACTIVE node"
                ),
            )
        node_id = active_nodes[0]

    heartbeat = _load_live_heartbeat(
        cur,
        account_id=account_id,
        node_id=node_id,
    )
    _validate_live_heartbeat_evidence(
        cur,
        heartbeat=heartbeat,
        node_id=node_id,
        account_id=account_id,
        symbol=symbol,
        release_id=str(release_id),
        expected_trading_state="ACTIVE",
        required_rollout_phase=_canary_phase_for_account(account_id),
    )
    _validate_robot_owned_symbol_flat(
        cur,
        account_id=account_id,
        symbol=symbol,
    )
    current_baseline = _portfolio_baseline_sha256(heartbeat, symbol)
    if current_baseline != portfolio_baseline_sha256:
        cur.execute(
            """
            UPDATE live_canary_permits
            SET status='revoked'
            WHERE permit_id=%s
              AND status='armed'
            """,
            (permit_id,),
        )
        cur.connection.commit()
        raise HTTPException(
            status_code=409,
            detail="robot-owned target symbol baseline changed",
        )
    cur.execute(
        """
        SELECT 1
        FROM production_incidents
        WHERE account_id=%s
          AND severity IN ('P0', 'P1')
          AND status='open'
        LIMIT 1
        """,
        (account_id,),
    )
    if cur.fetchone() is not None:
        raise HTTPException(
            status_code=409,
            detail="account has an open P0/P1 incident",
        )
    return _canary_permit_downlink_evidence(
        permit_id=permit_id,
        account_id=account_id,
        symbol=symbol,
        release_id=str(release_id),
        node_id=node_id,
        max_notional=max_notional,
        max_cumulative_loss=max_cumulative_loss,
        expires_at=expires_at,
        emergency_close_evidence_sha256=emergency_close_evidence_sha256,
        emergency_close_verified_at=emergency_close_verified_at,
        portfolio_baseline_sha256=portfolio_baseline_sha256,
    )


def _supplied_canary_intent_id(body: dict, *, canary_open: bool) -> str | bool:
    if not canary_open:
        return False
    raw_intent_id = str(body.get("intent_id") or "").strip()
    if not raw_intent_id:
        return False
    try:
        return str(UUID(raw_intent_id))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="intent_id must be a uuid for canary open",
        )


@app.post("/v1/operator/orders")
def operator_order(
    body: dict = Body(default={}),
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
):
    from datetime import timedelta
    from psycopg2.extras import Json

    role = require_reader(authorization)
    if role != "risk_admin":
        raise HTTPException(status_code=403, detail="risk_admin required")

    action = str(body.get("action") or "").strip()
    if action not in _OPERATOR_ACTIONS:
        raise HTTPException(status_code=400, detail=f"action must be one of {list(_OPERATOR_ACTIONS)}")
    symbol = str(body.get("symbol") or "").upper().split("-")[0].split(".")[0]
    if not symbol or not symbol.isalnum() or not symbol.endswith("USDT"):
        raise HTTPException(status_code=400, detail="symbol required, e.g. BTCUSDT")
    dry_run = body.get("dry_run") is True
    explicit_account_id = str(body.get("account_id") or "").strip()
    if not explicit_account_id and not dry_run:
        raise HTTPException(status_code=400, detail="account_id is required")
    account_id = explicit_account_id
    if not account_id:
        account_id = str(
            os.environ.get("OPERATOR_DEFAULT_ACCOUNT", "account-a")
        ).strip()
    operator_accounts = _operator_accounts()
    if account_id not in operator_accounts:
        raise HTTPException(
            status_code=400,
            detail=f"account_id must be one of {list(operator_accounts)}",
        )
    reason = str(body.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason required (audit trail)")
    source = str(body.get("source") or "hermes-agent")[:64]
    client_ref = str(body.get("client_ref") or "").strip()
    open_raw_channel = "hermes-operator"
    open_has_provenance = False
    open_risk_capital_multiplier: float | bool = False
    if action == "open_position":
        open_raw_channel, open_has_provenance = _open_source_channel(
            body,
            client_ref,
        )
        if open_raw_channel not in ("hermes-operator", "operator"):
            open_risk_capital_multiplier = (
                _channel_risk_capital_multiplier(
                    open_raw_channel,
                    account_id,
                )
            )
        else:
            open_risk_capital_multiplier = (
                _account_risk_capital_multiplier(account_id)
            )
    authorization_evidence: dict | None = None
    open_request_semantics: dict | bool = False

    caps = _operator_caps()
    checks = [{"name": "operator_auth", "passed": True}]
    entry = dict(body.get("entry") or {"type": "market"})
    entry_type = str(entry.get("type") or "market").lower()
    if entry_type == "none":
        entry_type = "market"
    if entry_type not in ("market", "limit", "zone"):
        raise HTTPException(status_code=400, detail="entry.type must be market|limit|zone")
    entry_price = _op_num(entry.get("price"), "entry.price")
    entry_price_min = _op_num(entry.get("price_min"), "entry.price_min")
    entry_price_max = _op_num(entry.get("price_max"), "entry.price_max")
    if entry_type == "limit" and entry_price is None:
        raise HTTPException(status_code=400, detail="limit entry requires entry.price")
    if entry_type == "zone" and (entry_price_min is None or entry_price_max is None):
        raise HTTPException(status_code=400, detail="zone entry requires entry.price_min + entry.price_max")
    entry_time_in_force = str(entry.get("time_in_force") or "").upper()
    if not entry_time_in_force:
        if entry_type == "market":
            entry_time_in_force = "IOC"
        else:
            entry_time_in_force = "GTC"
    database_url = os.environ.get("DATABASE_URL")
    if action == "open_position" and not client_ref:
        raise HTTPException(
            status_code=400,
            detail="open_position requires client_ref (idempotency key): use the "
                   "signal message id, or a stable slug for verbal orders",
        )
    if action == "open_position" and not dry_run:
        if not database_url:
            raise HTTPException(
                status_code=503,
                detail="intent store unavailable",
            )
        request_id = str(x_request_id or client_ref or "").strip()
        authorization_evidence = _order_authorization(
            body,
            database_url,
            account_id,
            symbol,
            reason,
            source,
            role,
            request_id,
            client_ref,
        )
        authorization_evidence.pop("_target_position_id", False)
        if (
            authorization_evidence["authorized_by_type"] == "channel"
            and open_raw_channel
            != authorization_evidence["authorized_by_id"]
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "channel authorization does not match source channel"
                ),
            )
        if (
            authorization_evidence["authorized_by_type"] == "channel"
            and not open_has_provenance
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "channel authorization requires source_channel "
                    "or canonical client_ref"
                ),
            )
        if (
            open_raw_channel not in ("hermes-operator", "operator")
            and authorization_evidence["authorized_by_type"] != "channel"
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "channel-sourced order requires "
                    "authorized_by_type=channel"
                ),
            )
        open_request_semantics = _operator_open_request_semantics(
            body=body,
            account_id=account_id,
            symbol=symbol,
            client_ref=client_ref,
            authorization=authorization_evidence,
        )
        replay = _operator_open_replay(
            database_url,
            account_id=account_id,
            symbol=symbol,
            client_ref=client_ref,
            authorization=authorization_evidence,
            request_semantics=open_request_semantics,
        )
        if replay is not False:
            return replay
    raw_canary_permit_id = body.get("canary_permit_id")
    canary_open = (
        action == "open_position"
        and not dry_run
        and _is_canary_request(
            account_id=account_id,
            raw_permit_id=raw_canary_permit_id,
        )
    )
    canary_quantity = None
    canary_price = None
    if canary_open:
        _canary_phase_for_account(account_id)
        if entry_type != "limit":
            raise HTTPException(
                status_code=400,
                detail=f"{account_id} canary entry.type must be limit",
            )
        if entry_time_in_force != "IOC":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{account_id} canary time_in_force must be IOC"
                ),
            )
        canary_quantity = _op_decimal(
            body.get("quantity"),
            f"{account_id} canary quantity",
        )
        if canary_quantity is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{account_id} canary requires explicit quantity"
                ),
            )
        canary_price = _op_decimal(
            entry.get("price"),
            f"{account_id} canary entry.price",
            required=True,
        )

    side = str(body.get("side") or "").lower() or None
    # Hedge mode holds LONG and SHORT simultaneously: management actions on a
    # dual-side instrument need an explicit book, otherwise the node planner
    # denies with position_not_unique (2026-07-10 ETH incident).
    position_side = str(body.get("position_side") or "").strip().lower() or None
    if position_side is not None and position_side not in ("long", "short"):
        raise HTTPException(status_code=400, detail="position_side must be long|short")
    leverage = _op_num(body.get("leverage"), "leverage")
    if leverage is not None and leverage <= 0:
        raise HTTPException(status_code=400, detail="leverage must be greater than zero")
    if leverage is not None and leverage > caps["max_leverage"]:
        raise HTTPException(status_code=400, detail=f"leverage {leverage} exceeds cap {caps['max_leverage']}")
    cancel_client_order_id = None
    cancel_order_owner = False
    if action == "cancel_order":
        cancel_client_order_id = str(body.get("client_order_id") or "").strip()
        # Only OUR deterministic ids are cancellable: external/manual orders on
        # the same account must never be touchable through this endpoint.
        if not re.fullmatch(r"B[0-9a-f]{32}[0-9]{2}", cancel_client_order_id):
            raise HTTPException(
                status_code=400,
                detail="cancel_order requires client_order_id in system format "
                       "(B + 32 hex + 2 digits); external orders cannot be cancelled here",
            )
    raw_authorization_type = str(
        body.get("authorized_by_type") or ""
    ).strip().lower()
    channel_authorized_management = (
        action in _OPERATOR_MANAGEMENT_ACTIONS
        and raw_authorization_type == "channel"
    )
    if channel_authorized_management:
        if not database_url:
            raise HTTPException(status_code=503, detail="intent store unavailable")
        channel = str(body.get("channel") or "").strip()
        if action == "cancel_order":
            cancel_order_owner = _cancel_order_owner(
                database_url,
                client_order_id=cancel_client_order_id,
                account_id=False,
                symbol=symbol,
            )
            account_id = cancel_order_owner["account_id"]
            if account_id not in operator_accounts:
                raise HTTPException(
                    status_code=503,
                    detail="cancel_order owner account is not registered",
                )
        else:
            entry_ref = str(body.get("entry_ref") or "").strip()
            account_id = _channel_management_entry_account(
                database_url,
                requested_account_id=account_id,
                symbol=symbol,
                channel=channel,
                entry_ref=entry_ref,
                position_side=position_side,
            )
    stop_loss = _op_num(body.get("stop_loss"), "stop_loss")
    if action == "move_stop_loss":
        if stop_loss is None:
            raise HTTPException(status_code=400, detail="move_stop_loss requires stop_loss")
        _validate_stop_direction(symbol, account_id, stop_loss, position_side)
    if action in _OPERATOR_MANAGEMENT_ACTIONS and not client_ref:
        raise HTTPException(
            status_code=400,
            detail=f"{action} requires client_ref: pass a stable operation ref "
                   "(for example close-btc-tg-sig-c1002136478186-m5026) and reuse "
                   "the same ref for every retry",
        )
    disable_take_profits = body.get("disable_take_profits") is True
    if disable_take_profits and action != "replace_take_profits":
        raise HTTPException(
            status_code=400,
            detail="disable_take_profits is only valid for replace_take_profits",
        )
    if action == "replace_take_profits":
        raw_tps = body.get("take_profits")
        if disable_take_profits:
            if raw_tps != []:
                raise HTTPException(
                    status_code=400,
                    detail="disable_take_profits requires take_profits=[]",
                )
            if not position_side:
                raise HTTPException(
                    status_code=400,
                    detail="disable_take_profits requires position_side",
                )
            take_profits = []
        elif not isinstance(raw_tps, list) or not raw_tps:
            raise HTTPException(
                status_code=400,
                detail="replace_take_profits requires take_profits: [{price, quantity}, ...]",
            )
        else:
            take_profits = []
            for i, item in enumerate(raw_tps):
                if not isinstance(item, dict):
                    raise HTTPException(
                        status_code=400,
                        detail="take_profits entries must be {price, quantity} objects",
                    )
                take_profits.append({
                    "price": _op_num(
                        item.get("price"),
                        f"take_profits[{i}].price",
                        required=True,
                    ),
                    "quantity": _op_num(
                        item.get("quantity"),
                        f"take_profits[{i}].quantity",
                        required=True,
                    ),
                })
            _validate_take_profit_direction(
                symbol,
                account_id,
                take_profits,
                position_side,
            )
    else:
        take_profits = [
            _op_num(tp, "take_profits[]") for tp in (body.get("take_profits") or [])
        ]

    notional = None
    quantity = None
    canary_actual_notional = None
    if action == "open_position":
        if side not in ("long", "short"):
            raise HTTPException(status_code=400, detail="side must be long|short for open_position")
        explicit_notional = _op_num(
            body.get("notional_usdt"),
            "notional_usdt",
        )
        if canary_open:
            if canary_quantity is None or canary_price is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{account_id} canary quantity and price are required"
                    ),
                )
            quantity = format(canary_quantity, "f")
            canary_actual_notional = canary_quantity * canary_price
            raw_explicit_notional = body.get("notional_usdt")
            declared_notional = _op_decimal(
                raw_explicit_notional,
                "notional_usdt",
            )
            if declared_notional is not None:
                if declared_notional != canary_actual_notional:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"{account_id} canary notional_usdt must equal "
                            "quantity * entry.price"
                        ),
                    )
            explicit_notional = float(canary_actual_notional)
        sizing_caps = caps
        if canary_open:
            sizing_caps = dict(caps)
            sizing_caps["_canary_explicit_notional_override"] = True
        notional = _size_open_order(
            explicit_notional,
            symbol, account_id, side, entry_type,
            entry_price, entry_price_min, entry_price_max,
            stop_loss, leverage, sizing_caps, checks,
            open_risk_capital_multiplier,
        )
    elif action == "partial_close":
        quantity = _op_num(body.get("quantity"), "quantity", required=True)

    expire_hours = _op_num(body.get("expire_hours"), "expire_hours")
    valid_seconds = int(_op_num(body.get("valid_seconds"), "valid_seconds") or 900)
    valid_seconds = max(60, min(valid_seconds, 3600))

    if action == "cancel_order":
        order_plan = {"cancel_client_order_id": cancel_client_order_id}
    elif action in _OPERATOR_PROTECT_ACTIONS:
        # Minimal semantic plan: the node planner derives side/quantity from the
        # live position; an entry block here would be misparsed as an order type.
        order_plan = {}
        if action == "move_stop_loss":
            order_plan["stop_loss"] = stop_loss
        else:
            order_plan["take_profits"] = take_profits
            if disable_take_profits:
                order_plan["disable_take_profits"] = True
        if position_side:
            order_plan["position_side"] = position_side
    else:
        order_plan = {
            "side": side,
            "entry": {
                "type": entry_type,
                "time_in_force": entry_time_in_force,
                "price": entry_price,
                "price_min": entry_price_min,
                "price_max": entry_price_max,
            },
            "stop_loss": stop_loss,
            "take_profits": take_profits,
            "leverage": leverage,
        }
        if expire_hours and entry_type in ("limit", "zone"):
            order_plan["expire_hours"] = expire_hours
        if quantity is not None:
            order_plan["quantity"] = str(quantity)
        if canary_open and canary_price is not None:
            order_plan["entry"]["price"] = format(canary_price, "f")
        if position_side and action in ("close_position", "partial_close"):
            order_plan["position_side"] = position_side
    # Shape must match the node RiskBudget contract exactly (extra=forbid,
    # all three fields required): risk_fraction/max_notional/max_leverage.
    risk_max_notional = notional
    if risk_max_notional is None:
        risk_max_notional = 0.0
    if canary_actual_notional is not None:
        risk_max_notional = format(canary_actual_notional, "f")
    risk_budget: dict = {
        "risk_fraction": 0.0,
        "max_notional": risk_max_notional,
        "max_leverage": leverage or caps["max_leverage"],
    }

    raw_channel = "hermes-operator"
    has_provenance = False
    if action == "open_position":
        raw_channel = open_raw_channel
        has_provenance = open_has_provenance

    if not database_url:
        raise HTTPException(status_code=503, detail="intent store unavailable")
    if action == "cancel_order" and not cancel_order_owner:
        cancel_order_owner = _cancel_order_owner(
            database_url,
            client_order_id=cancel_client_order_id,
            account_id=account_id,
            symbol=symbol,
        )

    request_id = str(x_request_id or client_ref or "").strip()
    if authorization_evidence is None:
        authorization_evidence = _order_authorization(
            body,
            database_url,
            account_id,
            symbol,
            reason,
            source,
            role,
            request_id,
            client_ref,
        )
    parent_target_position_id = (
        authorization_evidence.pop("_target_position_id", False)
        or False
    )
    if (
        authorization_evidence["authorized_by_type"] == "channel"
        and action == "open_position"
    ):
        if raw_channel != authorization_evidence["authorized_by_id"]:
            raise HTTPException(
                status_code=400,
                detail="channel authorization does not match source channel",
            )
        if not has_provenance:
            raise HTTPException(
                status_code=400,
                detail="channel authorization requires source_channel or canonical client_ref",
            )
    if (
        action == "open_position"
        and raw_channel not in ("hermes-operator", "operator")
        and authorization_evidence["authorized_by_type"] != "channel"
    ):
        raise HTTPException(
            status_code=400,
            detail="channel-sourced order requires authorized_by_type=channel",
        )
    if (
        action in _OPERATOR_MANAGEMENT_ACTIONS
        and authorization_evidence["authorized_by_type"] == "channel"
    ):
        raw_channel = authorization_evidence["authorized_by_id"]
        if not position_side and action != "cancel_order":
            raise HTTPException(
                status_code=400,
                detail="channel management requires position_side",
            )
    if (
        action == "cancel_order"
        and authorization_evidence["authorized_by_type"] == "channel"
    ):
        owner_channel = cancel_order_owner["owner_channel"]
        if owner_channel != authorization_evidence["authorized_by_id"]:
            raise HTTPException(
                status_code=400,
                detail="cancel_order authorization channel does not own order",
            )

    attribution = False
    attribution_target_position_id: str | bool = False
    if action in _OPERATOR_MANAGEMENT_ACTIONS:
        channel = str(body.get("channel") or "").strip()
        if (
            channel
            and channel != "operator"
            and authorization_evidence["authorized_by_type"] != "channel"
        ):
            raise HTTPException(
                status_code=400,
                detail="channel-sourced management requires authorized_by_type=channel",
            )
        entry_ref = str(body.get("entry_ref") or "").strip()
        if action == "cancel_order":
            owner_channel = cancel_order_owner["owner_channel"]
            channel_match: bool | str = "unknown"
            if channel and owner_channel:
                channel_match = channel == owner_channel
            attribution_event = {
                "resolution": "order",
                "owner_channel": owner_channel,
                "channel_match": channel_match,
                "would_reject": channel_match is False,
            }
            hard_error = False
        else:
            (
                attribution_event,
                hard_error,
                attribution_target_position_id,
            ) = _resolve_attribution(
                database_url,
                action,
                symbol,
                account_id,
                channel,
                entry_ref,
                position_side,
            )
        if not dry_run:
            _write_attribution_shadow(attribution_event)
        attribution = {
            "resolution": attribution_event["resolution"],
            "owner_channel": attribution_event["owner_channel"],
            "channel_match": attribution_event["channel_match"],
            "would_reject": attribution_event["would_reject"],
        }
        if hard_error:
            raise HTTPException(status_code=400, detail=hard_error)
        if authorization_evidence["authorized_by_type"] == "channel":
            channel_valid = (
                channel == authorization_evidence["authorized_by_id"]
                and attribution_event["resolution"] in {"intent", "order"}
                and attribution_event["channel_match"] is True
                and attribution_event["would_reject"] is False
            )
            if not channel_valid:
                raise HTTPException(
                    status_code=400,
                    detail="channel authorization attribution failed",
                )

    target_position_id: str | bool = parent_target_position_id
    if attribution_target_position_id:
        if (
            target_position_id
            and target_position_id != attribution_target_position_id
        ):
            raise HTTPException(
                status_code=400,
                detail="parent_intent_id and entry_ref resolve different positions",
            )
        target_position_id = attribution_target_position_id
    if (
        not target_position_id
        and action in _OPERATOR_MANAGEMENT_ACTIONS
        and action != "cancel_order"
        and position_side
    ):
        target_position_id = (
            _canonical_position_id(
                _nautilus_instrument_id(symbol),
                position_side,
            )
            or False
        )
    supplied_target_position_id = str(
        body.get("target_position_id") or ""
    ).strip()
    if (
        supplied_target_position_id
        and supplied_target_position_id != str(target_position_id or "")
    ):
        raise HTTPException(
            status_code=400,
            detail="target_position_id does not match server-resolved position",
        )
    if (
        action in _OPERATOR_MANAGEMENT_ACTIONS
        and action != "cancel_order"
        and not target_position_id
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "management action requires a server-resolved "
                "target_position_id; pass parent_intent_id, entry_ref, "
                "or position_side"
            ),
        )

    equity_evidence = next(
        (
            check
            for check in checks
            if check.get("name") == "account_equity_basis"
        ),
        False,
    )
    if equity_evidence:
        order_plan["equity"] = {
            "real_equity": equity_evidence["real_equity"],
            "available_balance": equity_evidence["available_balance"],
            "risk_capital_multiplier": (
                equity_evidence["risk_capital_multiplier"]
            ),
            "effective_equity": equity_evidence["effective_equity"],
        }
    order_plan["authorization"] = authorization_evidence
    if attribution:
        order_plan["attribution"] = attribution
    request_semantics = False
    if action == "open_position":
        if open_request_semantics is False:
            open_request_semantics = _operator_open_request_semantics(
                body=body,
                account_id=account_id,
                symbol=symbol,
                client_ref=client_ref,
                authorization=authorization_evidence,
            )
        request_semantics = open_request_semantics
        order_plan["request_semantics"] = request_semantics
    elif action in _OPERATOR_MANAGEMENT_ACTIONS:
        request_semantics = _operator_request_semantics(
            action=action,
            account_id=account_id,
            symbol=symbol,
            position_side=position_side,
            client_ref=client_ref,
            order_plan=order_plan,
            target_position_id=target_position_id,
            body=body,
            valid_seconds=valid_seconds,
        )
        order_plan["request_semantics"] = request_semantics
    canonical_request = _canonical_order_request(body, authorization_evidence)

    if dry_run:
        order_plan_preview = dict(order_plan)
        return {
            "dry_run": True,
            "action": action,
            "symbol": symbol,
            "account_id": account_id,
            "computed_notional": notional,
            "checks": checks,
            "order_plan_preview": order_plan_preview,
            "authorization": authorization_evidence,
            "attribution": attribution,
            "target_position_id": target_position_id,
        }

    supplied_canary_intent_id = _supplied_canary_intent_id(
        body,
        canary_open=canary_open,
    )
    now = datetime.now(timezone.utc)
    raw_id, run_id, ctx_id, dec_id, risk_id = (
        str(uuid4()) for _ in range(5)
    )
    intent_id = str(uuid4())
    if supplied_canary_intent_id:
        intent_id = supplied_canary_intent_id
    if action == "open_position":
        idem = hashlib.sha256(
            f"operator|{account_id}|{client_ref}".encode()
        ).hexdigest()
    else:
        idem = hashlib.sha256(
            f"operator-v2|{account_id}|{action}|{symbol}|"
            f"{position_side or ''}|{client_ref}".encode()
        ).hexdigest()
    message_type = "new_signal" if action == "open_position" else "position_update"
    valid_until = now + timedelta(seconds=valid_seconds)

    conn = _database_connection(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(%s, 0))",
                (idem,),
            )
            cur.execute(
                "SELECT intent_id::text, status::text, valid_until, order_plan, "
                "risk_budget "
                "FROM trade_intents WHERE idempotency_key=%s",
                (idem,),
            )
            existing = cur.fetchone()
            if existing:
                existing_plan = existing[3] or {}
                persisted_authorization = existing_plan.get("authorization")
                if (
                    _stable_authorization_evidence(persisted_authorization)
                    != _stable_authorization_evidence(authorization_evidence)
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="idempotency key authorization evidence mismatch",
                    )
                persisted_semantics = existing_plan.get("request_semantics")
                if request_semantics:
                    if not isinstance(persisted_semantics, dict):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "legacy idempotency record has no request "
                                "semantics; retry with a new client_ref"
                            ),
                        )
                    persisted_semantic_hash = False
                    persisted_semantic_hash = persisted_semantics.get("sha256")
                    if persisted_semantic_hash != request_semantics["sha256"]:
                        raise HTTPException(
                            status_code=409,
                            detail="idempotency key request payload mismatch",
                        )
                replay_response = {
                    "intent_id": existing[0], "status": existing[1], "replay": True,
                    "valid_until": existing[2].isoformat() if existing[2] else None,
                    "order_plan": existing_plan,
                    "risk_budget": existing[4] or {},
                }
                if attribution:
                    replay_response["attribution"] = attribution
                replay_response["authorization"] = persisted_authorization
                replay_response["target_position_id"] = target_position_id
                return replay_response
            if action == "open_position":
                rollout = _current_reviewed_rollout_state(
                    cur,
                    lock=True,
                )
                live_open_gate = _live_open_gate_from_rollout(rollout)
                if account_id in _ROLLOUT_ACCOUNTS:
                    if live_open_gate is False:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "reviewed release live open gate "
                                "is unavailable"
                            ),
                        )
                    if (
                        live_open_gate["mode"] == "canary_only"
                        and not canary_open
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"canary_permit_id is required for "
                                f"{account_id}"
                            ),
                        )
                if live_open_gate is not False:
                    order_plan["live_open_gate"] = live_open_gate
                    order_plan["rollout_phase"] = (
                        live_open_gate["rollout_phase"]
                    )
            canary_evidence = None
            if canary_open:
                if canary_actual_notional is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{account_id} canary notional is required",
                    )
                canary_evidence = _lock_canary_permit(
                    cur,
                    raw_permit_id=raw_canary_permit_id,
                    account_id=account_id,
                    symbol=symbol,
                    notional=canary_actual_notional,
                )
                order_plan["canary_permit"] = canary_evidence
            cur.execute(
                "INSERT INTO raw_messages (id, source, channel_id, source_message_id, source_version, "
                "source_received_at, author_id, content_hash, message_text, raw_payload) "
                "VALUES (%s,'operator',%s,%s,'v1',%s,%s,%s,%s,%s)",
                (
                    raw_id,
                    raw_channel,
                    client_ref,
                    now,
                    authorization_evidence["authorized_by_id"],
                    hashlib.sha256(
                        f"{raw_id}|{json.dumps(canonical_request, sort_keys=True, default=str)}".encode()
                    ).hexdigest(),
                    f"[{authorization_evidence['created_by_service']}] "
                    f"{action} {symbol}: {reason}",
                    Json(
                        {
                            "authorization": authorization_evidence,
                            "operator_request": canonical_request,
                        }
                    ),
                ),
            )
            cur.execute(
                "INSERT INTO message_processing_runs (processing_run_id, raw_message_id, status) "
                "VALUES (%s,%s,'succeeded')",
                (run_id, raw_id),
            )
            cur.execute(
                "INSERT INTO context_snapshots (context_snapshot_id, raw_message_id, snapshot_type, "
                "context_version, snapshot) VALUES (%s,%s,'system','v1',%s)",
                (ctx_id, raw_id, Json({"operator_request": canonical_request})),
            )
            cur.execute(
                "INSERT INTO hermes_decisions (decision_id, raw_message_id, processing_run_id, "
                "context_snapshot_id, message_type, action, ambiguous, account_scope, target_account_id, "
                "instrument_symbol, side, entry_type, entry_price, entry_price_min, entry_price_max, "
                "stop_loss, take_profits, leverage, valid_until, evidence, model_provider, model_version, "
                "prompt_version, context_version, temperature, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,false,'single',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'hermes',%s,"
                "'operator-v1','v1',0,%s)",
                (dec_id, raw_id, run_id, ctx_id, message_type, action, account_id, symbol, side,
                 entry_type, entry_price, entry_price_min, entry_price_max, stop_loss,
                 Json(take_profits), leverage, valid_until,
                 Json([authorization_evidence]),
                 authorization_evidence["created_by_service"],
                 now),
            )
            cur.execute(
                "INSERT INTO risk_decisions (risk_decision_id, hermes_decision_id, status, account_id, "
                "instrument_id, risk_budget, checks, reason, decided_by) "
                "VALUES (%s,%s,'approved',%s,%s,%s,%s,%s,'hermes-operator')",
                (risk_id, dec_id, account_id, symbol, Json(risk_budget), Json(checks), reason),
            )
            cur.execute(
                "INSERT INTO trade_intents (intent_id, hermes_decision_id, risk_decision_id, schema_version, "
                "account_id, instrument_id, action, status, order_plan, risk_budget, target_position_id, "
                "valid_until, idempotency_key, approved_at) "
                "VALUES (%s,%s,%s,'1.0',%s,%s,%s,'approved',%s,%s,%s,%s,%s, now())",
                (intent_id, dec_id, risk_id, account_id, symbol, action, Json(order_plan),
                 Json(risk_budget), target_position_id or None, valid_until, idem),
            )
            if canary_evidence is not None:
                cur.execute(
                    """
                    UPDATE live_canary_permits
                    SET status='consumed',
                        consumed_open_count=1,
                        consumed_at=now(),
                        consumed_intent_id=%s,
                        armed_node_id=COALESCE(armed_node_id, %s)
                    WHERE permit_id=%s
                      AND status='armed'
                      AND consumed_open_count=0
                      AND expires_at > now()
                    """,
                    (
                        intent_id,
                        canary_evidence["node_id"],
                        canary_evidence["permit_id"],
                    ),
                )
                if cur.rowcount != 1:
                    raise HTTPException(
                        status_code=409,
                        detail="canary permit could not be consumed",
                    )
            cur.execute(
                "INSERT INTO audit_events (audit_event_id, event_type, aggregate_type, "
                "aggregate_id, actor, raw_message_id, hermes_decision_id, "
                "risk_decision_id, intent_id, payload) "
                "VALUES (%s,'operator_order','trade_intent',%s,%s,%s,%s,%s,%s,%s)",
                (
                    str(uuid4()),
                    intent_id,
                    authorization_evidence["authorized_by_id"],
                    raw_id,
                    dec_id,
                    risk_id,
                    intent_id,
                    Json(
                        {
                            "authorization": authorization_evidence,
                            "action": action,
                            "account_id": account_id,
                            "instrument_id": symbol,
                            "client_ref": client_ref,
                            "target_position_id": target_position_id or False,
                            "real_equity": (
                                equity_evidence["real_equity"]
                                if equity_evidence
                                else False
                            ),
                            "effective_equity": (
                                equity_evidence["effective_equity"]
                                if equity_evidence
                                else False
                            ),
                            "risk_capital_multiplier": (
                                equity_evidence["risk_capital_multiplier"]
                                if equity_evidence
                                else False
                            ),
                            "request_semantics": request_semantics,
                        }
                    ),
                ),
            )
            cur.execute(
                "INSERT INTO outbox_events (outbox_event_id, status, aggregate_type, aggregate_id, "
                "event_type, payload) VALUES (%s,'pending','trade_intent',%s,'trade_intent.approved',%s)",
                (str(uuid4()), intent_id,
                 Json({
                     "intent_id": intent_id,
                     "risk_decision_id": risk_id,
                     "source": authorization_evidence["created_by_service"],
                     "authorization": authorization_evidence,
                 })),
            )
        conn.commit()
    finally:
        conn.close()

    response = {
        "intent_id": intent_id,
        "status": "approved",
        "replay": False,
        "account_id": account_id,
        "instrument_id": symbol,
        "action": action,
        "order_plan": order_plan,
        "execution_preview": _safe_execution_preview(order_plan, risk_budget, symbol, action),
        "risk_budget": risk_budget,
        "valid_until": valid_until.isoformat(),
        "authorization": authorization_evidence,
        "target_position_id": target_position_id or False,
    }
    if attribution:
        response["attribution"] = attribution
    return response


@app.get("/v1/operator/orders/{intent_id}")
def operator_order_status(intent_id: str, authorization: str | None = Header(default=None)):
    """Execution status of an operator-placed intent: intent row + order projections
    (fills) + the current position on that instrument, so Hermes can report back."""
    require_reader(authorization)
    try:
        UUID(intent_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="intent_id must be a uuid")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    conn = _database_connection(database_url)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT intent_id::text, account_id, instrument_id, action::text, status::text, "
                "order_plan, risk_budget, valid_until, approved_at, created_at "
                "FROM trade_intents WHERE intent_id=%s",
                (intent_id,),
            )
            intent = cur.fetchone()
            if intent is None:
                raise HTTPException(status_code=404, detail="intent not found")
            cur.execute(
                "SELECT client_order_id, status::text, filled_quantity, average_fill_price, updated_at "
                "FROM orders_projection WHERE intent_id=%s ORDER BY updated_at",
                (intent_id,),
            )
            orders = cur.fetchall()
            cur.execute(
                "SELECT account_id, instrument_id, side::text, quantity, avg_entry_price, status::text "
                "FROM positions_projection WHERE account_id=%s AND instrument_id LIKE %s AND status='open'",
                (intent["account_id"], intent["instrument_id"].split("-")[0] + "%"),
            )
            positions = cur.fetchall()
            cur.execute(
                "SELECT event_type, client_order_id, venue_order_id, trade_id, payload, ts_event "
                "FROM execution_events WHERE intent_id=%s ORDER BY ts_event",
                (intent_id,),
            )
            events = cur.fetchall()
    finally:
        conn.close()
    return jsonable_encoder({"intent": intent, "orders": orders,
                             "execution_events": events, "open_positions": positions})


@app.get("/health/role", name="role_database_health")
def role_database_health():
    role = current_app_role()
    if role is AppRole.ALL:
        raise HTTPException(
            status_code=503,
            detail="isolated control-plane app role is required",
        )
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise HTTPException(
            status_code=503,
            detail="database role health is unavailable",
        )
    conn = _database_connection(database_url)
    try:
        identity = _verify_database_session_contract(conn, role)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="database role health check failed",
        ) from exc
    finally:
        conn.close()
    return {
        "status": "healthy",
        "app_role": role.value,
        **identity,
        "rollback_only_permission_probe": "pass",
    }


all_role_app = app


def create_app(role: AppRole | str | None = None) -> FastAPI:
    resolved = resolve_app_role(role)
    role_app = build_role_app(all_role_app, resolved)
    if resolved is not AppRole.ALL:
        role_app.router.on_startup.append(
            lambda: _verify_role_database_on_startup(role_app, resolved)
        )
        role_app.router.on_shutdown.append(
            lambda: close_role_pools(resolved)
        )
    return role_app


app = create_app()
