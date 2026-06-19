import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { getMockDashboardOverview } from "../data/mockData";
import { formatCurrency, getReconnectDelayMs, normalizePath } from "../utils/format";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Hermes dashboard smoke", () => {
  it("renders dashboard with mock fallback and disconnected realtime copy", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    vi.stubGlobal("EventSource", undefined);

    render(<App initialPath="/dashboard" />);

    expect(await screen.findByText("实时连接断开")).toBeInTheDocument();
    expect(await screen.findByText("demo / runtime_degraded")).toBeInTheDocument();
    expect(screen.getByText("DRY-RUN LOCKED")).toBeInTheDocument();
    expect(screen.getByText("Data freshness")).toBeInTheDocument();
    expect(screen.getByText("Portfolio exposure")).toBeInTheDocument();
    expect(await screen.findByText("ETH/USDT")).toBeInTheDocument();
    expect(screen.getByText("Missing Stop Loss")).toBeInTheDocument();
    expect(screen.getAllByText("Risk Guard").length).toBeGreaterThan(0);
  });

  it("shows disconnected realtime state and resyncs dashboard REST after stream failure", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/dashboard/overview") {
        return Promise.resolve({
          json: () => Promise.resolve({
            bot_status: "running",
            data_source: "provider",
            equity: 25000,
            free_balance: 24000,
            margin_used: 1000,
            open_order_count: 0,
            open_trade_count: 0,
            realized_pnl_today: 0,
            risk_state: "normal",
            run_mode: "dry_run",
            status: "ok",
            today_execution_count: 0,
            today_signal_count: 0,
            unrealized_pnl: 0
          }),
          ok: true
        });
      }

      if (path === "/api/dashboard/open-trades") {
        return Promise.resolve({
          json: () => Promise.resolve([]),
          ok: true
        });
      }

      if (path === "/api/dashboard/events") {
        return Promise.resolve({
          json: () => Promise.resolve([
            {
              actor: "risk-admin",
              event_id: "signal-event-1",
              event_kind: "audit",
              event_type: "manual_approval",
              message: "manual_approval: audit-smoke:1",
              reason: "approved test signal",
              signal_id: "audit-smoke:1",
              timestamp: "2026-05-31T11:00:00Z"
            },
            {
              actor: "risk-admin",
              event_id: "signal-event-1",
              event_kind: "audit",
              event_type: "manual_approval",
              message: "manual_approval: audit-smoke:1",
              reason: "approved test signal",
              signal_id: "audit-smoke:1",
              timestamp: "2026-05-31T11:00:00Z"
            },
            {
              bot_id: "hermes-signal-dryrun",
              event_id: "trade-event-77",
              event_kind: "trade",
              event_type: "entry_fill",
              signal_id: "telegram:991",
              timestamp: "2026-05-31T11:00:01Z",
              trade_id: 77
            }
          ]),
          ok: true
        });
      }

      return Promise.reject(new Error("offline"));
    });
    const sources: FailingEventSource[] = [];

    class FailingEventSource {
      onerror: (() => void) | false = false;
      onopen: (() => void) | false = false;
      onmessage: (() => void) | false = false;

      constructor(_url: string) {
        sources.push(this);
      }

      addEventListener(_type: string, _listener: () => void): void {}

      close(): void {}
    }

    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", FailingEventSource);

    render(<App initialPath="/dashboard" />);

    expect(await screen.findByText("$25,000.00")).toBeInTheDocument();
    expect(screen.getByText("provider / ok")).toBeInTheDocument();
    expect(screen.getAllByText("manual_approval: audit-smoke:1")).toHaveLength(1);
    expect(screen.getByText("entry_fill: telegram:991 / trade 77 / hermes-signal-dryrun")).toBeInTheDocument();

    act(() => {
      sources.forEach((source) => {
        if (source.onerror) {
          source.onerror();
        }
      });
    });

    expect(await screen.findByText("实时连接断开")).toBeInTheDocument();
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(6);
    });
  });

  it("renders risk kill switch confirmation copy", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

    render(<App initialPath="/risk" />);

    expect(await screen.findByText("Kill Switch")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open kill switch modal" }));

    expect(screen.getByText("输入 CLOSE ALL 执行全平确认")).toBeInTheDocument();
  });

  it("posts kill switch activation after reason and close-all confirmation", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init && init.method === "POST") {
        return Promise.resolve({
          json: () => Promise.resolve({ enabled: true }),
          ok: true
        });
      }

      return Promise.reject(new Error("offline"));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App initialPath="/risk" />);

    fireEvent.click(await screen.findByRole("button", { name: "Open kill switch modal" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Kill switch reason" }), {
      target: { value: "manual risk stop" }
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Type CLOSE ALL to confirm" }), {
      target: { value: "CLOSE ALL" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Close all positions" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/risk/kill-switch",
        expect.objectContaining({
          body: JSON.stringify({
            close_all: true,
            confirmation_phrase: "CLOSE ALL",
            reason: "manual risk stop"
          }),
          method: "POST"
        })
      );
    });
  });

  it("renders position manual controls and posts close and move stop requests", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);

      if (init && init.method === "POST") {
        return Promise.resolve({
          json: () => Promise.resolve({ ok: true }),
          ok: true
        });
      }

      if (path === "/api/dashboard/overview") {
        return Promise.resolve({
          json: () => Promise.resolve({
            bot_status: "running",
            data_source: "provider",
            equity: 25000,
            free_balance: 24000,
            margin_used: 1000,
            open_order_count: 0,
            open_trade_count: 1,
            realized_pnl_today: 0,
            risk_state: "normal",
            run_mode: "dry_run",
            status: "ok",
            today_execution_count: 0,
            today_signal_count: 0,
            unrealized_pnl: 12
          }),
          ok: true
        });
      }

      if (path === "/api/dashboard/open-trades") {
        return Promise.resolve({
          json: () => Promise.resolve([
            {
              audit_timeline: [
                {
                  event_id: "audit-1",
                  event_type: "raw_received",
                  occurred_at: "2026-05-31T11:00:00Z",
                  reason: "telegram import"
                }
              ],
              current_rate: 2580,
              entry_rate: 2500,
              fills: [
                {
                  fill_id: "fill-1",
                  price: 2500,
                  size: 0.5
                }
              ],
              leverage: 3,
              next_take_profit_price: 2700,
              orders: [
                {
                  order_id: "order-1",
                  side: "buy",
                  status: "filled"
                }
              ],
              pair: "ETH/USDT",
              pnl: 12,
              r_multiple: 0.4,
              raw_text: "ETH long entry 2500 sl 2400 tp 2700",
              side: "long",
              signal_id: "sig-eth-1",
              size: 0.5,
              stop_loss_price: 2400,
              structured_signal: {
                entry: 2500,
                pair: "ETH/USDT",
                side: "long"
              },
              trade_id: "trade-42"
            }
          ]),
          ok: true
        });
      }

      if (path === "/api/dashboard/events") {
        return Promise.resolve({
          json: () => Promise.resolve([]),
          ok: true
        });
      }

      return Promise.reject(new Error("offline"));
    });
    vi.stubGlobal("fetch", fetchMock);

    class StableEventSource {
      onerror: (() => void) | false = false;
      onopen: (() => void) | false = false;
      onmessage: (() => void) | false = false;

      constructor(_url: string) {}

      addEventListener(_type: string, _listener: () => void): void {}

      close(): void {}
    }

    vi.stubGlobal("EventSource", StableEventSource);

    render(<App initialPath="/dashboard" />);

    expect(await screen.findByText("provider / ok")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Pause bot" }));
    expect(screen.getByRole("button", { name: "Confirm pause bot" })).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Bot action reason" }), {
      target: { value: "maintenance window" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm pause bot" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/dashboard/actions/pause-bot",
        expect.objectContaining({
          body: JSON.stringify({
            reason: "maintenance window"
          }),
          method: "POST"
        })
      );
    });

    const detailButton = await screen.findByRole("button", { name: "Open ETH/USDT detail" });
    act(() => {
      fireEvent.click(detailButton);
    });

    expect(await screen.findByRole("button", { name: "Close position" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Partial close position" })).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "Move SL price" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move SL" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Lock pair" })).toBeInTheDocument();
    expect(screen.getAllByText("sig-eth-1").length).toBeGreaterThan(0);
    expect(screen.getAllByText("trade-42").length).toBeGreaterThan(0);
    expect(screen.getByText("ETH long entry 2500 sl 2400 tp 2700")).toBeInTheDocument();
    expect(screen.getByText(/raw_received/)).toBeInTheDocument();

    fetchMock.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Close position" }));
    expect(screen.getByRole("button", { name: "Confirm close position" })).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Action reason" }), {
      target: { value: "operator dashboard close" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm close position" }));

    fireEvent.change(screen.getByRole("spinbutton", { name: "Move SL price" }), {
      target: { value: "2450" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Move SL" }));
    expect(screen.getByRole("button", { name: "Confirm move stop loss" })).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Action reason" }), {
      target: { value: "protect profit" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm move stop loss" }));

    fireEvent.click(screen.getByRole("button", { name: "Partial close position" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Action reason" }), {
      target: { value: "reduce risk" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm partial close" }));

    fireEvent.click(screen.getByRole("button", { name: "Lock pair" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Action reason" }), {
      target: { value: "news risk" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm pair lock" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/dashboard/actions/close-trade",
        expect.objectContaining({
          body: JSON.stringify({
            reason: "operator dashboard close",
            signal_id: "sig-eth-1",
            trade_id: "trade-42"
          }),
          method: "POST"
        })
      );
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/dashboard/actions/move-stoploss",
        expect.objectContaining({
          body: JSON.stringify({
            reason: "protect profit",
            signal_id: "sig-eth-1",
            stop_loss_price: 2450,
            trade_id: "trade-42"
          }),
          method: "POST"
        })
      );
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/dashboard/actions/close-trade",
        expect.objectContaining({
          body: JSON.stringify({
            amount: 0.25,
            reason: "reduce risk",
            signal_id: "sig-eth-1",
            trade_id: "trade-42"
          }),
          method: "POST"
        })
      );
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/dashboard/actions/lock-pair",
        expect.objectContaining({
          body: JSON.stringify({
            pair: "ETH/USDT",
            reason: "news risk"
          }),
          method: "POST"
        })
      );
    });
  });

  it("shows failed state when manual trade action returns unavailable", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init && init.method === "POST") {
        return Promise.resolve({
          json: () => Promise.resolve({ result: { status: "unavailable" } }),
          ok: true
        });
      }

      return Promise.reject(new Error("offline"));
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", undefined);

    render(<App initialPath="/dashboard" />);

    const detailButton = await screen.findByRole("button", { name: "Open ETH/USDT detail" });
    act(() => {
      fireEvent.click(detailButton);
    });
    expect(await screen.findByLabelText("ETH/USDT detail")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Close position" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Action reason" }), {
      target: { value: "operator close" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm close position" }));

    expect(await screen.findByText("Close failed")).toBeInTheDocument();
  });

  it("renders reports center and daily report operations", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

    render(<App initialPath="/reports" />);

    expect(await screen.findByText("Report Center")).toBeInTheDocument();
    expect(screen.getByText("日报列表")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open latest daily report" })).toBeInTheDocument();
  });

  it("renders daily report route for the requested date", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/reports/daily/2026-05-31") {
        return Promise.resolve({
          json: () => Promise.resolve({
            account: {
              equity: 25000,
              free_balance: 24000,
              margin_used: 1000,
              realized_pnl_today: 120,
              unrealized_pnl: 30
            },
            open_positions: {
              count: 1,
              notional: 5000,
              pnl: 30
            },
            risk_events: {
              blocking_count: 0,
              count: 1
            },
            signals: {
              accepted_count: 2,
              received_count: 3,
              rejected_count: 1
            },
            snapshot_id: "daily-demo-2026-05-31",
            trades: {
              closed_today_count: 1,
              win_count: 1
            }
          }),
          ok: true
        });
      }

      if (path === "/api/reports/daily/2026-05-31/versions") {
        return Promise.resolve({
          json: () => Promise.resolve([{ report_id: "report-daily-demo-2026-05-31-v1" }]),
          ok: true
        });
      }

      if (path === "/api/reports/daily/2026-05-31/telegram-preview") {
        return Promise.resolve({
          json: () => Promise.resolve({ text: "Hermes Daily\nReport: report-daily-demo-2026-05-31-v1" }),
          ok: true
        });
      }

      if (path === "/api/reports/daily/snapshot" && init && init.method === "POST") {
        return Promise.resolve({
          json: () => Promise.resolve({ snapshot_id: "daily-demo-2026-05-31" }),
          ok: true
        });
      }

      if (path === "/api/reports/daily/2026-05-31/markdown") {
        return Promise.resolve({
          ok: true,
          text: () => Promise.resolve("---\nreport_id: report-daily-demo-2026-05-31-v1\ngenerated_at: 2026-05-31T00:00:00Z\n---")
        });
      }

      return Promise.reject(new Error("offline"));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App initialPath="/reports/daily/2026-05-31" />);

    expect(await screen.findByText("Daily Report")).toBeInTheDocument();
    expect(screen.getByText("2026-05-31")).toBeInTheDocument();
    expect(screen.getByText("下载 Markdown")).toBeInTheDocument();
    expect(screen.getByText("HTML 预览")).toBeInTheDocument();
    expect(screen.getByText("版本对比")).toBeInTheDocument();
    expect(screen.getByText("Source Snapshot")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Mock Telegram brief" }));
    expect(await screen.findByText(/Hermes Daily/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Compare report versions" }));
    expect(await screen.findByText(/report-daily-demo-2026-05-31-v1/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "View source snapshot" }));
    expect(await screen.findByText(/daily-demo-2026-05-31/)).toBeInTheDocument();
  });

  it("formats operational values without depending on server data", () => {
    const overview = getMockDashboardOverview();

    expect(formatCurrency(overview.account.equity)).toBe("$128,420.52");
    expect(getReconnectDelayMs()).toBe(10000);
    expect(normalizePath("/reports/daily/2026-05-31")).toBe("/reports/daily/2026-05-31");
  });

  it("renders order center views from the freqtrade readonly proxy", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);

      if (path === "/api/freqtrade/status") {
        return Promise.resolve({
          json: () => Promise.resolve([
            {
              amount: 0.2,
              current_rate: 70500,
              is_open: true,
              orders: [
                {
                  amount: 0.2,
                  filled: 0.05,
                  order_date: "2026-06-10T10:00:00Z",
                  order_id: "order-open-1",
                  order_type: "limit",
                  price: 70400,
                  remaining: 0.15,
                  side: "buy",
                  status: "open"
                }
              ],
              open_date: "2026-06-10T09:00:00Z",
              open_rate: 70000,
              pair: "BTC/USDT",
              profit_abs: 100,
              profit_ratio: 0.01,
              trade_id: "trade-open-1"
            }
          ]),
          ok: true
        });
      }

      if (path === "/api/freqtrade/positions") {
        return Promise.resolve({
          json: () => Promise.resolve({
            positions: [
              {
                amount: 0.2,
                current_rate: 70500,
                leverage: 2,
                open_date: "2026-06-10T09:00:00Z",
                open_rate: 70000,
                pair: "BTC/USDT",
                profit_abs: 100,
                profit_ratio: 0.01,
                trade_id: "trade-open-1"
              }
            ]
          }),
          ok: true
        });
      }

      if (path === "/api/freqtrade/trades?limit=100") {
        return Promise.resolve({
          json: () => Promise.resolve({
            trades: [
              {
                amount: 1.5,
                close_date: "2026-06-10T08:00:00Z",
                close_profit_abs: 42,
                close_profit_pct: 2.1,
                close_rate: 161,
                is_open: false,
                open_date: "2026-06-10T07:00:00Z",
                open_rate: 158,
                orders: [{ order_id: "filled-1", status: "closed" }],
                pair: "SOL/USDT",
                trade_id: "trade-closed-1"
              }
            ]
          }),
          ok: true
        });
      }

      return Promise.reject(new Error("unexpected path"));
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", undefined);

    render(<App initialPath="/orders" />);

    expect(await screen.findByText("Order Center")).toBeInTheDocument();
    expect(screen.getByText("持仓")).toBeInTheDocument();
    expect(screen.getAllByText("挂单").length).toBeGreaterThan(0);
    expect(screen.getByText("历史")).toBeInTheDocument();
    expect(screen.getByText("盈亏")).toBeInTheDocument();
    expect(screen.getAllByText("BTC/USDT").length).toBeGreaterThan(0);
    expect(screen.getByText("order-open-1")).toBeInTheDocument();
    expect(screen.getByText("trade-closed-1")).toBeInTheDocument();
    expect(screen.getAllByText("$142.00").length).toBeGreaterThan(0);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith("/api/freqtrade/status", expect.any(Object));
      expect(fetchMock).toHaveBeenCalledWith("/api/freqtrade/positions", expect.any(Object));
      expect(fetchMock).toHaveBeenCalledWith("/api/freqtrade/trades?limit=100", expect.any(Object));
    });
  });

  it("renders signal review queue and posts audited decisions with reasons", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);

      if (path === "/api/signals/review/queue" && (!init || init.method !== "POST")) {
        return Promise.resolve({
          json: () => Promise.resolve({
            proposals: [
              {
                classification: {
                  conclusion: "rate_limit_pressure",
                  confidence: "high",
                  proposal_types: ["rate_limit_adjustment"],
                  reason_codes: ["rate_limit"]
                },
                detail: "raise BTC channel review limit",
                proposal_id: "proposal-rate-1",
                proposed_action: "increase_review_limit",
                signal_id: "sig-review-1",
                status: "open",
                title: "Rate-limit adjustment request",
                type: "rate_limit_adjustment"
              }
            ],
            signals: [
              {
                classification: {
                  conclusion: "requires_human_review",
                  confidence: "high",
                  proposal_types: ["approve_request"],
                  reason_codes: ["missing_take_profit"]
                },
                entry: {
                  mode: "limit",
                  primary_price: 70000
                },
                leverage: {
                  selected: 3
                },
                pair_freqtrade: "BTC/USDT:USDT",
                raw_text: "BTCUSDT LONG Entry: 70000 SL: 69000",
                received_at: "2026-06-10T10:00:00Z",
                review_reason_codes: ["missing_take_profit"],
                side: "long",
                signal_id: "sig-review-1",
                status: "needs_review",
                stop_loss: 69000,
                take_profits: []
              }
            ]
          }),
          ok: true
        });
      }

      if (init && init.method === "POST") {
        return Promise.resolve({
          json: () => Promise.resolve({ ok: true }),
          ok: true
        });
      }

      return Promise.reject(new Error("unexpected path"));
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", undefined);

    render(<App initialPath="/review" />);

    expect(await screen.findByText("Signal Review")).toBeInTheDocument();
    expect(screen.getByText("needs_review queue")).toBeInTheDocument();
    expect(screen.getByText("requires_human_review")).toBeInTheDocument();
    expect(screen.getByText("Rate-limit adjustment request")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Approve signal" }));
    expect(screen.getByRole("button", { name: "Confirm approve signal" })).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Review decision reason" }), {
      target: { value: "parsed fields match source" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm approve signal" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/signals/review/sig-review-1/approve",
        expect.objectContaining({
          body: JSON.stringify({ reason: "parsed fields match source" }),
          method: "POST"
        })
      );
    });

    fireEvent.click(await screen.findByRole("button", { name: "Reject proposal proposal-rate-1" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Review decision reason" }), {
      target: { value: "keep current review throttle" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm reject proposal" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/signals/review/proposals/proposal-rate-1/reject",
        expect.objectContaining({
          body: JSON.stringify({ reason: "keep current review throttle" }),
          method: "POST"
        })
      );
    });
  });

  it("renders review signal image media through an authorized blob fetch and opens a lightbox", async () => {
    localStorage.setItem("hermes.auth.token", "viewer-token");
    const createObjectURL = vi.fn(() => "blob:signal-photo");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL,
      revokeObjectURL
    });
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);

      if (path === "/api/signals/review/queue") {
        return Promise.resolve({
          json: () => Promise.resolve({
            proposals: [],
            signals: [
              {
                classification: {
                  conclusion: "requires_human_review",
                  confidence: "high",
                  proposal_types: ["approve_request"],
                  reason_codes: ["image_signal"]
                },
                entry: {
                  mode: "limit",
                  primary_price: 70000
                },
                leverage: {
                  selected: 3
                },
                media_metadata: {
                  count: 1,
                  items: [
                    {
                      index: 0,
                      mime_type: "image/jpeg"
                    }
                  ]
                },
                pair_freqtrade: "BTC/USDT:USDT",
                raw_text: "BTCUSDT LONG from chart image",
                received_at: "2026-06-10T10:00:00Z",
                review_reason_codes: ["image_signal"],
                side: "long",
                signal_id: "sig-media-1",
                status: "needs_review",
                stop_loss: 69000,
                take_profits: []
              }
            ]
          }),
          ok: true
        });
      }

      if (path === "/api/signals/sig-media-1/media/0") {
        return Promise.resolve({
          blob: () => Promise.resolve(new Blob(["image-bytes"], { type: "image/jpeg" })),
          ok: true
        });
      }

      return Promise.reject(new Error(`unexpected path ${path}`));
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", undefined);

    render(<App initialPath="/review" />);

    const thumbnail = await screen.findByRole("img", { name: "Signal sig-media-1 media 1" });
    expect(thumbnail).toHaveAttribute("src", "blob:signal-photo");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/signals/sig-media-1/media/0",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer viewer-token"
        })
      })
    );

    fireEvent.click(screen.getByRole("button", { name: "Open signal sig-media-1 media 1" }));

    expect(screen.getByRole("dialog", { name: "Signal media preview" })).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: "Signal sig-media-1 media 1" })).toHaveLength(2);
  });

  it("does not render a review media region when a signal has no media", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);

      if (path === "/api/signals/review/queue") {
        return Promise.resolve({
          json: () => Promise.resolve({
            proposals: [],
            signals: [
              {
                classification: {
                  conclusion: "requires_human_review",
                  confidence: "medium",
                  proposal_types: ["approve_request"],
                  reason_codes: ["missing_take_profit"]
                },
                entry: {
                  mode: "limit",
                  primary_price: 70000
                },
                leverage: {
                  selected: 3
                },
                media_metadata: {
                  count: 0,
                  items: []
                },
                pair_freqtrade: "BTC/USDT:USDT",
                raw_text: "BTCUSDT LONG Entry: 70000 SL: 69000",
                received_at: "2026-06-10T10:00:00Z",
                review_reason_codes: ["missing_take_profit"],
                side: "long",
                signal_id: "sig-no-media",
                status: "needs_review",
                stop_loss: 69000,
                take_profits: []
              }
            ]
          }),
          ok: true
        });
      }

      return Promise.reject(new Error(`unexpected path ${path}`));
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", undefined);

    render(<App initialPath="/review" />);

    expect(await screen.findByText("sig-no-media")).toBeInTheDocument();
    expect(screen.queryByLabelText("Signal media thumbnails")).not.toBeInTheDocument();
  });
});
