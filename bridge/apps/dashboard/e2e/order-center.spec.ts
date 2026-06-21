import { expect, test, type Page, type Route } from "@playwright/test";

type CommandAudit = {
  commands: Record<string, unknown>[];
};

const quality = {
  data_source: "order_center_projection",
  generated_at: new Date().toISOString(),
  last_execution_event_at: new Date().toISOString(),
  missing_nodes: [],
  projection_lag_ms: 14,
  reconciliation_state: "healthy",
  snapshot_id: "om8-order-center-e2e",
  stale: false
};

function minutesAgo(minutes: number): string {
  return new Date(Date.now() - minutes * 60 * 1000).toISOString();
}

function daysAgo(days: number): string {
  return new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();
}

function positionsPayload(): Record<string, unknown> {
  return {
    ...quality,
    positions: [
      {
        account_id: "acc-main",
        amount: 1.2,
        entry_price: 65000,
        execution_job_id: "job-alpha",
        instrument_symbol: "BTC/USDT",
        leverage: 3,
        mark_price: 66100,
        notional: 79320,
        opened_at: minutesAgo(35),
        pnl_pct: 0.0169,
        position_id: "pos-alpha",
        protection_status: "protected",
        side: "long",
        signal_id: "intent-alpha",
        status: "open",
        stop_loss_price: 64000,
        take_profit_price: 69000,
        unrealized_pnl: 120
      },
      {
        account_id: "acc-secondary",
        amount: 3,
        entry_price: 3500,
        execution_job_id: "job-beta",
        instrument_symbol: "ETH/USDT",
        leverage: 2,
        mark_price: 3470,
        notional: 10410,
        opened_at: daysAgo(10),
        pnl_pct: -0.0085,
        position_id: "pos-beta",
        protection_status: "protected",
        side: "short",
        signal_id: "intent-beta",
        status: "open",
        stop_loss_price: 3600,
        take_profit_price: 3300,
        unrealized_pnl: -90
      }
    ]
  };
}

function ordersPayload(): Record<string, unknown> {
  return {
    ...quality,
    orders: [
      {
        account_id: "acc-main",
        amount: 1.2,
        created_at: minutesAgo(30),
        events: [
          {
            event_id: "evt-accepted",
            event_type: "accepted",
            message: "venue accepted entry order",
            occurred_at: minutesAgo(29),
            status: "acked"
          },
          {
            event_id: "evt-working",
            event_type: "working",
            message: "order resting on book",
            occurred_at: minutesAgo(28),
            status: "open"
          }
        ],
        execution_job_id: "job-alpha",
        filled: 0.4,
        instrument_symbol: "BTC/USDT",
        intent_id: "intent-alpha",
        order_id: "ord-entry-recent",
        order_type: "limit",
        position_id: "pos-alpha",
        price: 65050,
        protection_status: "protected",
        remaining: 0.8,
        role: "entry",
        side: "buy",
        status: "open",
        trade_id: "pos-alpha"
      },
      {
        account_id: "acc-secondary",
        amount: 3,
        created_at: daysAgo(10),
        events: [
          {
            event_id: "evt-protect",
            event_type: "replace_stop",
            message: "protection stop replaced",
            occurred_at: daysAgo(10),
            status: "acked"
          }
        ],
        execution_job_id: "job-beta",
        filled: 0,
        instrument_symbol: "ETH/USDT",
        intent_id: "intent-beta",
        order_id: "ord-protect-old",
        order_type: "stop_market",
        position_id: "pos-beta",
        price: 3600,
        protection_status: "protected",
        remaining: 3,
        role: "protection",
        side: "buy",
        status: "open",
        trade_id: "pos-beta"
      }
    ]
  };
}

function tradesPayload(): Record<string, unknown> {
  return {
    ...quality,
    trades: [
      {
        account_id: "acc-main",
        amount: 5,
        close_date: daysAgo(2),
        close_profit_abs: 42,
        close_rate: 164,
        instrument_symbol: "SOL/USDT",
        open_date: daysAgo(3),
        open_rate: 155,
        orders: [{ order_id: "sol-entry", status: "closed" }],
        pnl_pct: 0.021,
        side: "long",
        status: "closed",
        trade_id: "trade-closed-sol"
      }
    ]
  };
}

async function fulfillJson(route: Route, json: unknown, status = 200): Promise<void> {
  await route.fulfill({
    contentType: "application/json",
    json,
    status
  });
}

async function seedRiskAdmin(page: Page): Promise<void> {
  await page.addInitScript(() => {
    window.localStorage.setItem("hermes.auth.token", "om8-e2e-token");
    window.localStorage.setItem("hermes.auth.role", "risk_admin");
    window.sessionStorage.clear();
  });
}

async function routeOrderCenterApi(page: Page, audit: CommandAudit): Promise<void> {
  await page.route(/\/(api|v1)\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname === "/v1/stream") {
      await route.abort();
      return;
    }

    if (url.pathname === "/v1/positions") {
      await fulfillJson(route, positionsPayload());
      return;
    }

    if (url.pathname === "/v1/orders") {
      await fulfillJson(route, ordersPayload());
      return;
    }

    if (url.pathname === "/v1/trades") {
      await fulfillJson(route, tradesPayload());
      return;
    }

    if (url.pathname === "/v1/commands") {
      audit.commands.push(request.postDataJSON() as Record<string, unknown>);
      await fulfillJson(route, {
        acks: [{ acknowledged_at: new Date().toISOString(), node_id: "node-a", status: "acked" }],
        command_id: `command-${audit.commands.length}`,
        status: "completed",
        target_nodes: ["node-a"]
      });
      return;
    }

    await route.abort();
  });
}

async function submitManualAction(
  page: Page,
  confirmName: string,
  reason: string,
  options: {
    fraction?: string;
    stopPrice?: string;
  } = {}
): Promise<void> {
  const dialog = page.getByTestId("order-action-dialog");
  await expect(dialog).toBeVisible();
  const confirmButton = page.getByRole("button", { name: confirmName });
  await expect(confirmButton).toBeDisabled();
  await page.getByRole("textbox", { name: "Action reason" }).fill(reason);
  if (options.stopPrice) {
    await page.getByRole("spinbutton", { name: "Stop price" }).fill(options.stopPrice);
  }
  if (options.fraction) {
    await page.getByRole("spinbutton", { name: "Partial close fraction" }).fill(options.fraction);
  }
  await expect(confirmButton).toBeEnabled();
  await confirmButton.click();
  await expect(page.getByText("Command completed")).toBeVisible();
}

test.beforeEach(async ({ page }) => {
  await seedRiskAdmin(page);
});

test("filters orders by account, instrument, status, role, and time range", async ({ page }) => {
  const audit: CommandAudit = { commands: [] };
  await routeOrderCenterApi(page, audit);
  await page.goto("/orders");

  const filters = page.getByTestId("orders-filter-bar");
  const ordersTable = page.getByTestId("orders-open-orders-table");
  const positionsTable = page.getByTestId("orders-positions-table");
  const historyTable = page.getByTestId("orders-history-table");

  await expect(filters).toBeVisible();
  await expect(page.getByTestId("orders-filter-account")).toBeVisible();
  await expect(page.getByTestId("orders-filter-instrument")).toBeVisible();
  await expect(page.getByTestId("orders-filter-status")).toBeVisible();
  await expect(page.getByTestId("orders-filter-role")).toBeVisible();
  await expect(page.getByTestId("orders-filter-time-range")).toBeVisible();

  await page.getByTestId("orders-filter-account").selectOption("acc-secondary");
  await expect(ordersTable).toContainText("ord-protect-old");
  await expect(positionsTable).toContainText("ETH/USDT");
  await expect(ordersTable).not.toContainText("ord-entry-recent");

  await page.getByTestId("orders-filter-account").selectOption("all");
  await page.getByTestId("orders-filter-instrument").selectOption("BTC/USDT");
  await expect(ordersTable).toContainText("ord-entry-recent");
  await expect(ordersTable).not.toContainText("ord-protect-old");

  await page.getByTestId("orders-filter-instrument").selectOption("all");
  await page.getByTestId("orders-filter-status").selectOption("closed");
  await expect(historyTable).toContainText("trade-closed-sol");
  await expect(ordersTable).toContainText("No orders match the current filters.");

  await page.getByTestId("orders-filter-status").selectOption("all");
  await page.getByTestId("orders-filter-role").selectOption("protection");
  await expect(ordersTable).toContainText("ord-protect-old");
  await expect(ordersTable).not.toContainText("ord-entry-recent");

  await page.getByTestId("orders-filter-role").selectOption("all");
  await page.getByTestId("orders-filter-time-range").selectOption("1h");
  await expect(ordersTable).toContainText("ord-entry-recent");
  await expect(ordersTable).not.toContainText("ord-protect-old");
  await expect(historyTable).toContainText("No history rows match the current filters.");
});

test("order detail shows the intent to job to order to event to position timeline", async ({ page }) => {
  const audit: CommandAudit = { commands: [] };
  await routeOrderCenterApi(page, audit);
  await page.goto("/orders");

  await page.getByRole("button", { name: "Open order ord-entry-recent detail" }).click();
  const drawer = page.getByTestId("orders-detail-drawer");

  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText("Intent intent-alpha");
  await expect(drawer).toContainText("Execution job job-alpha");
  await expect(drawer).toContainText("Order ord-entry-recent");
  await expect(drawer).toContainText("accepted");
  await expect(drawer).toContainText("venue accepted entry order");
  await expect(drawer).toContainText("Position pos-alpha");
});

test("manual cancel, move-stop, partial-close, and close actions are confirmation gated", async ({ page }) => {
  const audit: CommandAudit = { commands: [] };
  await routeOrderCenterApi(page, audit);
  await page.goto("/orders");

  await page.getByRole("button", { name: "Open order ord-entry-recent detail" }).click();
  const drawer = page.getByTestId("orders-detail-drawer");

  await drawer.getByRole("button", { name: "Cancel order ord-entry-recent" }).click();
  await submitManualAction(page, "Confirm cancel order", "stale entry order after venue halt");
  expect(audit.commands.at(-1)).toEqual({
    args: {
      order_id: "ord-entry-recent",
      reason: "stale entry order after venue halt"
    },
    type: "cancel_order"
  });

  await drawer.getByRole("button", { name: "Move stop pos-alpha" }).click();
  await submitManualAction(page, "Confirm move stop", "raise stop after first target", { stopPrice: "64123" });
  expect(audit.commands.at(-1)).toEqual({
    args: {
      position_id: "pos-alpha",
      reason: "raise stop after first target",
      signal_id: "intent-alpha",
      stop_loss_price: 64123
    },
    type: "move_stop_loss"
  });

  await drawer.getByRole("button", { name: "Partial close pos-alpha" }).click();
  await submitManualAction(page, "Confirm partial close", "take partial profit into resistance", { fraction: "0.25" });
  expect(audit.commands.at(-1)).toEqual({
    args: {
      amount: 0.3,
      position_id: "pos-alpha",
      reason: "take partial profit into resistance",
      scope: "position_partial",
      signal_id: "intent-alpha"
    },
    type: "close_all"
  });

  await drawer.getByRole("button", { name: "Close position pos-alpha" }).click();
  await submitManualAction(page, "Confirm close position", "exit remaining exposure before maintenance");
  expect(audit.commands.at(-1)).toEqual({
    args: {
      position_id: "pos-alpha",
      reason: "exit remaining exposure before maintenance",
      scope: "position",
      signal_id: "intent-alpha"
    },
    type: "close_all"
  });
});
