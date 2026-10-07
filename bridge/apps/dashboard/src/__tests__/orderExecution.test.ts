import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchOrderCenter } from "../utils/api";

const envelope = {
  data_source: "postgres_projection",
  generated_at: "2026-08-28T10:00:10Z",
  last_execution_event_at: "2026-08-28T10:00:10Z",
  missing_nodes: [],
  projection_lag_ms: 0,
  reconciliation_state: "healthy",
  snapshot_id: "11111111-1111-4111-8111-111111111111",
  stale: false
};

const FILLED_ID = "Baaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01";
const DENIED_ID = "Baaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa02";
const PROTECTION_ID = "Baaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa11";

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(payload: unknown): Promise<Response> {
  return Promise.resolve({
    json: () => Promise.resolve(payload),
    ok: true
  } as Response);
}

function filledOrder(clientOrderId: string): Record<string, unknown> {
  return {
    account_id: "account-a",
    amount: 5,
    client_order_id: clientOrderId,
    events: [],
    filled: 5,
    filled_quantity: 5,
    instrument_id: "ATOMUSDT-PERP.BINANCE",
    instrument_symbol: "ATOMUSDT",
    intent_id: "11111111-1111-4111-8111-111111111111",
    order_id: clientOrderId,
    quantity: 5,
    remaining: 0,
    status: "filled"
  };
}

function deniedOrder(
  clientOrderId: string,
  extras: Record<string, unknown> = {}
): Record<string, unknown> {
  return {
    account_id: "account-a",
    amount: 5,
    client_order_id: clientOrderId,
    denial_reason: "lot-size",
    events: [
      {
        detail: "lot-size",
        event_type: "OrderDenied",
        message: "lot-size",
        status: "denied"
      }
    ],
    filled: 0,
    filled_quantity: 0,
    instrument_id: "ATOMUSDT-PERP.BINANCE",
    instrument_symbol: "ATOMUSDT",
    intent_id: "11111111-1111-4111-8111-111111111111",
    order_id: clientOrderId,
    quantity: 5,
    remaining: 5,
    status: "denied",
    ...extras
  };
}

function stubOrderCenter(orders: Record<string, unknown>[]): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const path = String(input);
    if (path === "/v1/orders") {
      return jsonResponse({ ...envelope, orders });
    }
    if (path === "/v1/positions") {
      return jsonResponse({ ...envelope, positions: [] });
    }
    if (path === "/v1/trades") {
      return jsonResponse({ ...envelope, trades: [] });
    }
    return Promise.reject(new Error(`unexpected ${path}`));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("order center execution mapping", () => {
  it("maps mixed fill plus denied second leg through fetchOrderCenter", async () => {
    const fetchMock = stubOrderCenter([filledOrder(FILLED_ID), deniedOrder(DENIED_ID)]);

    const center = await fetchOrderCenter();

    expect(fetchMock).toHaveBeenCalledWith("/v1/orders", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith("/v1/positions", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith("/v1/trades", expect.any(Object));
    const byId = Object.fromEntries(center.orders.map((order) => [order.id, order]));
    expect(byId[FILLED_ID].status).toBe("filled");
    expect(byId[FILLED_ID].filled).toBe(5);
    expect(byId[DENIED_ID].status).toBe("rejected");
    expect(byId[DENIED_ID].filled).toBe(0);
    expect(byId[DENIED_ID].events[0]?.type).toBe("OrderDenied");
    expect(byId[DENIED_ID].events[0]?.status).toBe("rejected");
    expect(byId[DENIED_ID].events[0]?.message).toContain("lot-size");
  });

  it("maps zero-fill denial as rejected without a fill", async () => {
    stubOrderCenter([deniedOrder(DENIED_ID)]);

    const center = await fetchOrderCenter();
    expect(center.orders).toHaveLength(1);
    expect(center.orders[0].status).toBe("rejected");
    expect(center.orders[0].filled).toBe(0);
    expect(center.orders[0].events[0]?.message).toContain("lot-size");
  });

  it("keeps a filled entry when a protection leg is denied", async () => {
    stubOrderCenter([
      filledOrder(FILLED_ID),
      deniedOrder(PROTECTION_ID, { lifecycle_role: "stop_loss", order_type: "STOP_MARKET" })
    ]);

    const center = await fetchOrderCenter();
    const byId = Object.fromEntries(center.orders.map((order) => [order.id, order]));
    expect(byId[FILLED_ID].status).toBe("filled");
    expect(byId[FILLED_ID].filled).toBe(5);
    expect(byId[PROTECTION_ID].status).toBe("rejected");
    expect(byId[PROTECTION_ID].events[0]?.message).toContain("lot-size");
  });
});
