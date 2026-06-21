import { expect, test, type Page, type Route } from "@playwright/test";

const quality = {
  data_source: "postgres_projection",
  generated_at: "2026-06-21T12:00:00Z",
  last_execution_event_at: "2026-06-21T11:59:50Z",
  missing_nodes: [],
  projection_lag_ms: 42,
  reconciliation_state: "healthy",
  snapshot_id: "dashboard-e2e-snapshot",
  stale: false
};

async function fulfillJson(route: Route, json: unknown, status = 200): Promise<void> {
  await route.fulfill({
    contentType: "application/json",
    json,
    status
  });
}

async function seedRiskAdmin(page: Page): Promise<void> {
  await page.addInitScript(() => {
    window.localStorage.setItem("hermes.auth.token", "dashboard-e2e-token");
    window.localStorage.setItem("hermes.auth.role", "risk_admin");
    window.sessionStorage.clear();
  });
}

async function routeDashboardApi(page: Page): Promise<void> {
  await page.route(/\/(api|v1)\//, async (route) => {
    const url = new URL(route.request().url());

    if (url.pathname === "/v1/stream") {
      await route.abort();
      return;
    }

    if (url.pathname === "/v1/accounts") {
      await fulfillJson(route, {
        ...quality,
        accounts: [{ available: 24000, equity: 25000, margin_used: 1000, realized_pnl_today: 42 }]
      });
      return;
    }

    if (url.pathname === "/v1/nodes") {
      await fulfillJson(route, {
        ...quality,
        nodes: [
          {
            heartbeat_at: "2026-06-21T11:59:50Z",
            instrument_count: 12,
            name: "node-a",
            open_position_count: 1,
            readiness: "ready",
            trading_state: "running"
          }
        ]
      });
      return;
    }

    if (url.pathname === "/v1/orders") {
      await fulfillJson(route, {
        ...quality,
        orders: [
          {
            account_id: "acc-main",
            amount: 0.1,
            created_at: "2026-06-21T11:55:00Z",
            instrument_symbol: "BTC/USDT:USDT",
            order_id: "order-open-1",
            order_type: "limit",
            price: 65120,
            status: "open"
          }
        ]
      });
      return;
    }

    if (url.pathname === "/v1/positions") {
      await fulfillJson(route, {
        ...quality,
        positions: [
          {
            amount: 0.1,
            audit_timeline: [{ event: "entry_fill", at: "2026-06-21T11:45:07Z" }],
            current_rate: 65120,
            entry_rate: 65000,
            fills: [{ price: 65000, quantity: 0.1 }],
            instrument_symbol: "BTC/USDT:USDT",
            leverage: 3,
            orders: [{ order_id: "order-open-1", status: "open" }],
            pnl: 12,
            pnl_pct: 0.018,
            position_id: "position-77",
            raw_signal: "BTCUSDT LONG Entry: 65000 SL: 64000 TP: 67000",
            r_multiple: 0.4,
            side: "long",
            signal_id: "signal-alpha",
            stop_loss_price: 64000,
            structured_signal: {
              entry_mode: "limit",
              risk_reward: 2,
              take_profits: [67000]
            },
            take_profit_price: 67000,
            trade_id: "77"
          }
        ]
      });
      return;
    }

    if (url.pathname === "/v1/trades") {
      await fulfillJson(route, { ...quality, trades: [] });
      return;
    }

    if (url.pathname === "/v1/messages") {
      await fulfillJson(route, {
        ...quality,
        messages: [
          {
            event_id: "message-event-1",
            event_type: "status_transition",
            message: "status_transition: signal-alpha entered",
            timestamp: "2026-06-21T11:45:06Z"
          }
        ]
      });
      return;
    }

    if (url.pathname === "/v1/risk/state") {
      await fulfillJson(route, {
        ...quality,
        blocking_reasons: [],
        daily_loss_usage_pct: 10,
        pair_locks: [],
        risk_state: "normal",
        run_mode: "dry_run",
        single_trade_risk_usage_pct: 20,
        total_open_risk_usage_pct: 30
      });
      return;
    }

    await route.abort();
  });
}

async function routeRiskApi(page: Page): Promise<void> {
  await page.route(/\/(api|v1)\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname === "/v1/stream") {
      await route.abort();
      return;
    }

    if (url.pathname === "/v1/risk/state") {
      await fulfillJson(route, {
        ...quality,
        blocking_reasons: ["manual review active"],
        daily_loss_usage_pct: 40,
        pair_locks: [{ instrument_symbol: "BTC/USDT", owner: "Risk", reason: "event risk", until: "2026-06-21T13:00:00Z" }],
        risk_state: "normal",
        single_trade_risk_usage_pct: 20,
        total_open_risk_usage_pct: 30
      });
      return;
    }

    if (url.pathname === "/v1/commands" && request.method() === "POST") {
      await fulfillJson(route, {
        ...quality,
        acks: [{ acknowledged_at: "2026-06-21T12:01:00Z", node_id: "node-a", status: "acked" }],
        command_id: "command-partial",
        status: "pending",
        target_nodes: ["node-a", "node-b"]
      });
      return;
    }

    await route.abort();
  });
}

async function routeSignalReviewApi(page: Page, audit: { reason: string }): Promise<void> {
  await page.route(/\/(api|v1)\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname === "/v1/stream") {
      await route.abort();
      return;
    }

    if (url.pathname === "/v1/risk/decisions") {
      await fulfillJson(route, {
        ...quality,
        decisions: [
          {
            classification: {
              action: "needs_review",
              ambiguity_reasons: ["missing_take_profit"],
              conclusion: "requires_human_review",
              confidence: "high",
              proposal_types: ["approve_request"],
              reason_codes: ["missing_take_profit"]
            },
            created_at: "2026-06-21T10:00:00Z",
            decision_id: "decision-1",
            intent: {
              entry: { price: 70000, type: "limit" },
              instrument_symbol: "BTC/USDT:USDT",
              leverage: 3,
              side: "long",
              stop_loss: 69000,
              take_profits: []
            },
            raw_message_id: "message-1"
          }
        ]
      });
      return;
    }

    if (url.pathname === "/v1/messages") {
      await fulfillJson(route, {
        ...quality,
        messages: [
          {
            decision_id: "decision-1",
            raw_text: "BTCUSDT LONG Entry: 70000 SL: 69000",
            received_at: "2026-06-21T09:59:00Z"
          }
        ]
      });
      return;
    }

    if (url.pathname === "/v1/risk/decisions/decision-1/approve" && request.method() === "POST") {
      const body = request.postDataJSON() as { reason?: string };
      audit.reason = body.reason || "";
      await fulfillJson(route, { ...quality, ok: true });
      return;
    }

    await route.abort();
  });
}

async function routeReportApi(page: Page): Promise<void> {
  await page.route(/\/(api|v1)\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname === "/v1/stream") {
      await route.abort();
      return;
    }

    if (url.pathname === "/v1/reports/daily/2026-05-31") {
      await fulfillJson(route, {
        ...quality,
        account: {
          equity: 25000,
          max_drawdown_pct: 1.2,
          realized_pnl_today: 42,
          unrealized_pnl: 12
        },
        open_positions: {
          notional: 6500
        },
        positions: [
          {
            amount: 0.1,
            current_rate: 65120,
            entry_rate: 65000,
            instrument_symbol: "BTC/USDT:USDT",
            pnl: 12,
            side: "long",
            signal_id: "signal-alpha",
            stop_loss_price: 64000,
            trade_id: "77"
          }
        ],
        risk_events: {
          blocking_count: 0,
          count: 2
        },
        signals: {
          accepted_count: 4,
          executed_count: 1,
          received_count: 5,
          rejected_count: 1
        },
        trades: {
          avg_r: 0.7,
          closed_today_count: 2,
          profit_factor: 1.8,
          win_count: 1
        }
      });
      return;
    }

    if (url.pathname === "/v1/reports/daily/2026-05-31/markdown") {
      await route.fulfill({
        body: "# Hermes Daily Report 2026-05-31\n\nSmoke markdown",
        contentType: "text/markdown; charset=utf-8",
        status: 200
      });
      return;
    }

    if (url.pathname === "/v1/reports/daily/2026-05-31/telegram-preview" && request.method() === "POST") {
      await fulfillJson(route, { text: "Telegram smoke preview" });
      return;
    }

    if (url.pathname === "/v1/reports/daily/2026-05-31/versions") {
      await fulfillJson(route, {
        ...quality,
        versions: [{ report_id: "report-daily-demo-2026-05-31-v1" }]
      });
      return;
    }

    if (url.pathname === "/v1/reports/daily/snapshot" && request.method() === "POST") {
      await fulfillJson(route, {
        ...quality,
        snapshot_id: "daily-demo-2026-05-31"
      });
      return;
    }

    await route.abort();
  });
}

test.beforeEach(async ({ page }) => {
  await seedRiskAdmin(page);
});

test("dashboard renders operator state, events, and position detail from v1 APIs", async ({ page }) => {
  await routeDashboardApi(page);
  await page.goto("/dashboard");

  await expect(page.getByText("postgres_projection / healthy")).toBeVisible();
  await expect(page.getByText("dry_run")).toBeVisible();
  await expect(page.getByText("Portfolio exposure")).toBeVisible();
  await expect(page.getByText("最近 20 事件")).toBeVisible();
  await expect(page.getByText("status_transition: signal-alpha entered")).toBeVisible();
  await expect(page.getByRole("cell", { name: "signal-alpha" })).toBeVisible();

  await page.getByRole("button", { name: "Open BTC/USDT:USDT detail" }).click();
  const drawer = page.getByLabel("BTC/USDT:USDT detail", { exact: true });
  await expect(drawer).toContainText("Structured Signal JSON");
  await expect(drawer).toContainText("Freqtrade Trade ID");
  await expect(drawer.getByRole("button", { name: "Close position", exact: true })).toBeVisible();
  await expect(drawer.getByRole("button", { name: "Move SL" })).toBeVisible();
});

test("risk kill switch remains pending until every target node acknowledges", async ({ page }) => {
  await routeRiskApi(page);
  await page.goto("/risk");

  await expect(page.getByText("Risk data ready")).toBeVisible();
  await page.getByRole("button", { name: "Open kill switch modal" }).click();
  await page.getByRole("textbox", { name: "Kill switch reason" }).fill("manual risk stop");
  await page.getByRole("textbox", { name: "Type CLOSE ALL to confirm" }).fill("CLOSE ALL");
  await page.getByRole("button", { name: "Close all positions" }).click();

  await expect(page.getByText("Waiting for 1 node ack: node-b")).toBeVisible();
  await expect(page.getByRole("dialog", { name: "Kill Switch" })).toBeVisible();
});

test("signal review approves a needs_review decision with an audit reason", async ({ page }) => {
  const audit = { reason: "" };
  await routeSignalReviewApi(page, audit);
  await page.goto("/review");

  await expect(page.getByText("needs_review queue")).toBeVisible();
  await expect(page.getByRole("button", { name: "Open signal decision-1" })).toBeVisible();
  await expect(page.getByText("requires_human_review")).toBeVisible();

  await page.getByRole("button", { name: "Approve signal" }).click();
  const confirmButton = page.getByRole("button", { name: "Confirm approve signal" });
  await expect(page.getByRole("dialog", { name: "Approve signal" })).toBeVisible();
  await expect(confirmButton).toBeDisabled();

  await page.getByRole("textbox", { name: "Review decision reason" }).fill("parsed fields match source");
  await expect(confirmButton).toBeEnabled();
  await confirmButton.click();

  await expect(page.getByText("Review decision submitted")).toBeVisible();
  expect(audit.reason).toBe("parsed fields match source");
});

test("daily report renders snapshot actions and telegram preview", async ({ page }) => {
  await routeReportApi(page);
  await page.goto("/reports/daily/2026-05-31");

  await expect(page.getByText("Daily Report")).toBeVisible();
  await expect(page.getByRole("heading", { name: "2026-05-31" })).toBeVisible();
  await expect(page.getByText("交易表现")).toBeVisible();
  await expect(page.getByText("信号漏斗")).toBeVisible();
  await expect(page.getByText("Report ready")).toBeVisible();
  await expect(page.getByRole("button", { name: "Download Markdown" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Compare report versions" })).toBeVisible();
  await expect(page.getByRole("button", { name: "View source snapshot" })).toBeVisible();

  await page.getByRole("button", { name: "Telegram brief" }).click();
  await expect(page.getByRole("heading", { name: "Telegram 简版" })).toBeVisible();
  await expect(page.getByText("Telegram smoke preview")).toBeVisible();
});
