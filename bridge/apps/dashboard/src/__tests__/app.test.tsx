import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { fetchDashboardOverview } from "../utils/api";
import { formatCurrency, getReconnectDelayMs, normalizePath } from "../utils/format";
import dataQualitySchema from "../../../../../tests/nautilus/_fixtures/contracts-v1/data_quality_envelope_v1.schema.json";

const baseQuality = {
  data_source: "postgres_projection",
  generated_at: "2026-06-19T12:00:00Z",
  last_execution_event_at: null,
  missing_nodes: [],
  projection_lag_ms: 42,
  reconciliation_state: "healthy",
  snapshot_id: "11111111-1111-4111-8111-111111111111",
  stale: false
};

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function envelope(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    ...baseQuality,
    ...overrides
  };
}

function jsonResponse(payload: unknown): Promise<Response> {
  return Promise.resolve({
    json: () => Promise.resolve(payload),
    ok: true
  } as Response);
}

function expectDataQualityEnvelope(payload: Record<string, unknown>): void {
  const required = dataQualitySchema.required as string[];
  required.forEach((field) => {
    expect(payload).toHaveProperty(field);
  });
  expect(dataQualitySchema.properties.reconciliation_state.enum).toContain(payload.reconciliation_state);
  expect(typeof payload.stale).toBe("boolean");
  expect(Array.isArray(payload.missing_nodes)).toBe(true);
}

function stubControlPlane(fetchMock = vi.fn(), useDefault = true): ReturnType<typeof vi.fn> {
  if (useDefault) {
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (init?.method === "POST") {
      return jsonResponse({
        ...envelope(),
        command_id: "command-ok",
        target_nodes: ["node-a"],
        acks: [{ node_id: "node-a", status: "acked", acknowledged_at: "2026-06-19T12:00:01Z" }],
        status: "completed"
      });
    }
    if (path === "/v1/accounts") {
      return jsonResponse(envelope({ accounts: [] }));
    }
    if (path === "/v1/nodes") {
      return jsonResponse(envelope({ nodes: [] }));
    }
    if (path.startsWith("/v1/orders")) {
      return jsonResponse(envelope({ orders: [] }));
    }
    if (path.startsWith("/v1/positions")) {
      return jsonResponse(envelope({ positions: [] }));
    }
    if (path.startsWith("/v1/trades")) {
      return jsonResponse(envelope({ trades: [] }));
    }
    if (path.startsWith("/v1/messages")) {
      return jsonResponse(envelope({ messages: [] }));
    }
    if (path === "/v1/risk/state") {
      return jsonResponse(envelope({ blocking_reasons: [], pair_locks: [], risk_state: "normal" }));
    }
    if (path === "/v1/risk/decisions") {
      return jsonResponse(envelope({ decisions: [] }));
    }
    if (path.startsWith("/v1/reports/daily/")) {
      return jsonResponse(envelope({
        account: { equity: 0, max_drawdown_pct: 0, realized_pnl_today: 0, unrealized_pnl: 0 },
        open_positions: { notional: 0 },
        risk_events: { blocking_count: 0, count: 0 },
        signals: { accepted_count: 0, executed_count: 0, received_count: 0, rejected_count: 0 },
        trades: { avg_r: 0, closed_today_count: 0, profit_factor: 0, win_count: 0 }
      }));
    }
    return Promise.reject(new Error(`unexpected ${path}`));
    });
  }
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("EventSource", undefined);
  return fetchMock;
}

describe("control-plane dashboard contracts", () => {
  it("fetches only v1 control-plane read APIs and validates frozen data-quality fields", async () => {
    const fetchMock = stubControlPlane();

    const overview = await fetchDashboardOverview();

    expect(overview.dataSource.data_source).toBe("postgres_projection");
    expect(overview.dataSource.snapshot_id).toBe(baseQuality.snapshot_id);
    expectDataQualityEnvelope(overview.dataSource.raw);
    expect(fetchMock).toHaveBeenCalledWith("/v1/accounts", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith("/v1/nodes", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith("/v1/orders?status=open", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith("/v1/positions", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith("/v1/trades", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith("/v1/messages?limit=20", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith("/v1/risk/state", expect.any(Object));
  });

  it("renders real empty state with data-quality metadata and no seeded positions", async () => {
    stubControlPlane();

    render(<App initialPath="/dashboard" />);

    expect(await screen.findByText("postgres_projection")).toBeInTheDocument();
    expect(screen.getByText(baseQuality.snapshot_id)).toBeInTheDocument();
    expect(screen.getByText("projection_lag_ms")).toBeInTheDocument();
    expect(screen.getByText("No positions from control-plane.")).toBeInTheDocument();
    expect(screen.getAllByText("$0.00").length).toBeGreaterThan(0);
    expect(screen.queryByText("ETH/USDT")).not.toBeInTheDocument();
    expect(screen.queryByText("BTC/USDT")).not.toBeInTheDocument();
  });

  it("shows stale banner with missing nodes and reconciliation state", async () => {
    stubControlPlane(vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      const stalePayload = envelope({
        missing_nodes: ["node-b"],
        projection_lag_ms: 120000,
        reconciliation_state: "degraded",
        stale: true
      });
      if (path === "/v1/accounts") {
        return jsonResponse({ ...stalePayload, accounts: [] });
      }
      if (path === "/v1/nodes") {
        return jsonResponse({ ...stalePayload, nodes: [] });
      }
      if (path.startsWith("/v1/orders")) {
        return jsonResponse({ ...stalePayload, orders: [] });
      }
      if (path.startsWith("/v1/positions")) {
        return jsonResponse({ ...stalePayload, positions: [] });
      }
      if (path.startsWith("/v1/trades")) {
        return jsonResponse({ ...stalePayload, trades: [] });
      }
      if (path.startsWith("/v1/messages")) {
        return jsonResponse({ ...stalePayload, messages: [] });
      }
      if (path === "/v1/risk/state") {
        return jsonResponse({ ...stalePayload, blocking_reasons: [], pair_locks: [], risk_state: "normal" });
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    }), false);

    render(<App initialPath="/dashboard" />);

    expect(await screen.findByRole("alert")).toHaveTextContent("stale=true");
    expect(screen.getByText("node-b")).toBeInTheDocument();
    expect(screen.getAllByText("degraded").length).toBeGreaterThan(0);
  });

  it("does not show dangerous command success until every target node ack arrives", async () => {
    const fetchMock = stubControlPlane();
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === "POST" && path === "/v1/commands") {
        return jsonResponse({
          ...envelope(),
          acks: [{ acknowledged_at: "2026-06-19T12:00:01Z", node_id: "node-a", status: "acked" }],
          command_id: "command-partial",
          status: "pending",
          target_nodes: ["node-a", "node-b"]
        });
      }
      if (path === "/v1/risk/state") {
        return jsonResponse(envelope({ blocking_reasons: [], pair_locks: [], risk_state: "normal" }));
      }
      return jsonResponse(envelope({ accounts: [], decisions: [], messages: [], nodes: [], orders: [], positions: [], trades: [] }));
    });

    render(<App initialPath="/risk" />);

    fireEvent.click(await screen.findByRole("button", { name: "Open kill switch modal" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Kill switch reason" }), {
      target: { value: "manual risk stop" }
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Type CLOSE ALL to confirm" }), {
      target: { value: "CLOSE ALL" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Close all positions" }));

    expect(await screen.findByText("Waiting for 1 node ack: node-b")).toBeInTheDocument();
    expect(screen.queryByText("Kill switch active")).not.toBeInTheDocument();
  });

  it("filters orders and renders the intent to position timeline with protection status", async () => {
    stubControlPlane(vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.startsWith("/v1/positions")) {
        return jsonResponse(envelope({
          positions: [
            {
              account_id: "acc-1",
              amount: 1,
              entry_price: 40000,
              execution_job_id: "job-1",
              instrument_symbol: "BTC/USDT",
              leverage: 3,
              mark_price: 42000,
              opened_at: "2026-06-20T12:00:00Z",
              pnl: 2000,
              pnl_pct: 0.05,
              position_id: "pos-1",
              protection_status: "protected",
              side: "long",
              status: "open",
              stop_loss_price: 39000
            }
          ]
        }));
      }
      if (path.startsWith("/v1/orders")) {
        return jsonResponse(envelope({
          orders: [
            {
              account_id: "acc-1",
              amount: 1,
              created_at: "2026-06-20T12:02:00Z",
              events: [
                {
                  event_id: "evt-1",
                  event_type: "order_accepted",
                  message: "Venue accepted order",
                  occurred_at: "2026-06-20T12:02:03Z",
                  status: "accepted"
                }
              ],
              execution_job_id: "job-1",
              filled: 0.25,
              instrument_symbol: "BTC/USDT",
              intent_id: "intent-1",
              order_id: "ord-1",
              order_type: "limit",
              price: 41000,
              protection_status: "protected",
              remaining: 0.75,
              role: "entry",
              side: "buy",
              status: "open",
              trade_id: "pos-1"
            },
            {
              account_id: "acc-2",
              amount: 2,
              created_at: "2026-06-20T13:02:00Z",
              execution_job_id: "job-2",
              filled: 2,
              instrument_symbol: "ETH/USDT",
              intent_id: "intent-2",
              order_id: "ord-2",
              order_type: "market",
              price: 2500,
              protection_status: "missing",
              remaining: 0,
              role: "take_profit",
              side: "sell",
              status: "filled",
              trade_id: "pos-2"
            }
          ]
        }));
      }
      if (path.startsWith("/v1/trades")) {
        return jsonResponse(envelope({ trades: [] }));
      }
      return jsonResponse(envelope({ accounts: [], decisions: [], messages: [], nodes: [], positions: [], trades: [] }));
    }), false);

    render(<App initialPath="/orders" />);

    expect(await screen.findByLabelText("Account")).toBeInTheDocument();
    expect(screen.getByLabelText("Instrument")).toBeInTheDocument();
    expect(screen.getByLabelText("Status")).toBeInTheDocument();
    expect(screen.getByLabelText("Role")).toBeInTheDocument();
    expect(screen.getByLabelText("Time range")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Account"), { target: { value: "acc-1" } });

    expect(screen.getByText("ord-1")).toBeInTheDocument();
    expect(screen.queryByText("ord-2")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Open order ord-1 detail" }));

    const detail = await screen.findByRole("complementary", { name: "Order ord-1 detail" });
    expect(within(detail).getByText("Intent intent-1")).toBeInTheDocument();
    expect(within(detail).getByText("Execution job job-1")).toBeInTheDocument();
    expect(within(detail).getByText("Order ord-1")).toBeInTheDocument();
    expect(within(detail).getByText("Events")).toBeInTheDocument();
    expect(within(detail).getByText("Position pos-1")).toBeInTheDocument();
    expect(within(detail).getByText("Protection protected")).toBeInTheDocument();
  });

  it("requires confirmation before submitting a manual cancel order action", async () => {
    const fetchMock = stubControlPlane(vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === "POST" && path === "/v1/commands") {
        return jsonResponse({
          ...envelope(),
          acks: [{ acknowledged_at: "2026-06-20T12:03:00Z", node_id: "node-a", status: "acked" }],
          command_id: "command-cancel",
          status: "completed",
          target_nodes: ["node-a"]
        });
      }
      if (path.startsWith("/v1/positions")) {
        return jsonResponse(envelope({ positions: [] }));
      }
      if (path.startsWith("/v1/orders")) {
        return jsonResponse(envelope({
          orders: [
            {
              account_id: "acc-1",
              amount: 1,
              created_at: "2026-06-20T12:02:00Z",
              execution_job_id: "job-1",
              filled: 0,
              instrument_symbol: "BTC/USDT",
              intent_id: "intent-1",
              order_id: "ord-1",
              order_type: "limit",
              price: 41000,
              protection_status: "pending",
              remaining: 1,
              role: "entry",
              side: "buy",
              status: "open",
              trade_id: "pos-1"
            }
          ]
        }));
      }
      if (path.startsWith("/v1/trades")) {
        return jsonResponse(envelope({ trades: [] }));
      }
      return jsonResponse(envelope({ accounts: [], decisions: [], messages: [], nodes: [], positions: [], trades: [] }));
    }), false);

    render(<App initialPath="/orders" />);

    fireEvent.click(await screen.findByRole("button", { name: "Cancel order ord-1" }));

    const dialog = screen.getByRole("dialog", { name: "Cancel order" });
    expect(within(dialog).getByRole("button", { name: "Confirm cancel order" })).toBeDisabled();

    fireEvent.change(within(dialog).getByLabelText("Action reason"), { target: { value: "stale order" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirm cancel order" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith("/v1/commands", expect.objectContaining({
        body: JSON.stringify({
          args: {
            order_id: "ord-1",
            reason: "stale order"
          },
          type: "cancel_order"
        }),
        method: "POST"
      }));
    });
    expect(await screen.findByText("Command completed")).toBeInTheDocument();
  });
});

describe("format utilities", () => {
  it("formats currency and paths predictably", () => {
    expect(formatCurrency(1234.5)).toBe("$1,234.50");
    expect(normalizePath("/orders/")).toBe("/dashboard");
    expect(getReconnectDelayMs()).toBeGreaterThan(0);
  });
});
