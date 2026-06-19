import { expect, test, type Page } from "@playwright/test";

const overviewPayload = {
  bot_status: "running",
  data_source: "provider",
  equity: 25000,
  free_balance: 24000,
  margin_used: 1000,
  open_order_count: 2,
  open_trade_count: 1,
  realized_pnl_today: 42,
  recent_events: [
    {
      event_id: "signal-event-smoke",
      event_type: "status_transition",
      message: "status_transition: codex-smoke:1",
      signal_id: "codex-smoke:1",
      timestamp: "2026-05-31T13:45:06Z"
    }
  ],
  risk_state: "normal",
  run_mode: "dry_run",
  status: "ok",
  status_reason: "signal_store",
  today_executed_count: 1,
  today_execution_count: 1,
  today_signal_count: 5,
  unrealized_pnl: 12
};

const openTradesPayload = [
  {
    amount: 0.1,
    current_profit_abs: 12,
    current_rate: 65120,
    entry_rate: 65000,
    leverage: 3,
    liquidation_price: 52000,
    max_rate: 65200,
    open_date: "2026-05-31T13:44:00Z",
    pair: "BTC/USDT:USDT",
    side: "long",
    signal_id: "codex-smoke:1",
    stop_loss_abs: 64000,
    structured_signal: {
      entry_mode: "limit",
      risk_reward: 2,
      take_profits: [67000]
    },
    trade_id: "77"
  }
];

const eventsPayload = [
  {
    bot_id: "hermes-signal-dryrun",
    event_id: "trade-event-77",
    event_kind: "trade",
    event_type: "entry_fill",
    signal_id: "codex-smoke:1",
    timestamp: "2026-05-31T13:45:07Z",
    trade_id: "77"
  }
];

const dailyReportPayload = {
  account: {
    equity: 25000,
    max_drawdown_pct: 1.2,
    realized_pnl_today: 42,
    unrealized_pnl: 12
  },
  open_positions: {
    notional: 6500
  },
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
  snapshot_id: "snapshot-smoke",
  trades: {
    avg_r: 0.7,
    closed_today_count: 2,
    profit_factor: 1.8,
    win_count: 1
  }
};

const orderStatusPayload = [
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
];

const orderPositionsPayload = {
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
};

const orderTradesPayload = {
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
};

const signalReviewPayload = {
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
};

async function routeDashboardApi(page: Page): Promise<void> {
  await page.route(/\/api\/dashboard\/stream$/, async (route) => {
    await route.abort();
  });

  await page.route(/\/api\/dashboard\/(overview|open-trades|events)$/, async (route) => {
    const url = route.request().url();
    let payload: unknown = false;

    if (/\/api\/dashboard\/overview$/.test(url)) {
      payload = overviewPayload;
    }
    if (/\/api\/dashboard\/open-trades$/.test(url)) {
      payload = openTradesPayload;
    }
    if (/\/api\/dashboard\/events$/.test(url)) {
      payload = eventsPayload;
    }

    if (payload === false) {
      await route.abort();
      return;
    }

    await route.fulfill({
      contentType: "application/json",
      json: payload,
      status: 200
    });
  });
}

async function routeOrderCenterApi(page: Page): Promise<void> {
  await page.route(/\/api\/freqtrade\/status$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: orderStatusPayload,
      status: 200
    });
  });

  await page.route(/\/api\/freqtrade\/positions$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: orderPositionsPayload,
      status: 200
    });
  });

  await page.route(/\/api\/freqtrade\/trades\?limit=100$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: orderTradesPayload,
      status: 200
    });
  });
}

async function routeSignalReviewApi(page: Page, decisionAudit: { reason: string }): Promise<void> {
  await page.route(/\/api\/signals\/review\/queue$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: signalReviewPayload,
      status: 200
    });
  });

  await page.route(/\/api\/signals\/review\/sig-review-1\/approve$/, async (route) => {
    const body = route.request().postDataJSON() as { reason?: string };
    decisionAudit.reason = body.reason || "";

    await route.fulfill({
      contentType: "application/json",
      json: { ok: true },
      status: 200
    });
  });
}

test("dashboard renders operator state, risk funnel, and open signal from API", async ({ page }) => {
  await routeDashboardApi(page);
  await page.goto("/dashboard");

  await expect(page.getByText("DRY-RUN LOCKED")).toBeVisible();
  await expect(page.getByText("provider / ok")).toBeVisible();
  await expect(page.getByText("Portfolio exposure")).toBeVisible();
  await expect(page.getByText("Today Signals")).toBeVisible();
  await expect(page.getByText("Signal Funnel")).toBeVisible();
  await expect(page.getByText("BTC/USDT:USDT")).toBeVisible();
  await expect(page.getByRole("cell", { name: "codex-smoke:1" })).toBeVisible();
  await expect(page.getByText("entry_fill: codex-smoke:1 / trade 77 / hermes-signal-dryrun")).toBeVisible();
});

test("trade detail smoke opens drawer with signal context", async ({ page }) => {
  await page.route(/\/api\/dashboard\/stream$/, async (route) => {
    await route.abort();
  });

  await page.route(/\/api\/dashboard\/(overview|open-trades|events)$/, async (route) => {
    const url = route.request().url();
    let payload: unknown = false;

    if (/\/api\/dashboard\/overview$/.test(url)) {
      payload = overviewPayload;
    }
    if (/\/api\/dashboard\/open-trades$/.test(url)) {
      payload = openTradesPayload;
    }
    if (/\/api\/dashboard\/events$/.test(url)) {
      payload = eventsPayload;
    }

    if (payload === false) {
      await route.abort();
      return;
    }

    await route.fulfill({
      contentType: "application/json",
      json: payload,
      status: 200
    });
  });

  await page.goto("/dashboard");
  await page.getByRole("button", { name: "Open BTC/USDT:USDT detail" }).click();
  const drawer = page.getByLabel("BTC/USDT:USDT detail", { exact: true });

  await expect(drawer.getByRole("button", { exact: true, name: "Close position" })).toBeVisible();
  await expect(drawer.getByRole("button", { name: "Partial close position" })).toBeVisible();
  await expect(drawer.getByText("Structured Signal JSON")).toBeVisible();
  await expect(drawer.getByText("Freqtrade Trade ID")).toBeVisible();
});

test("daily report smoke renders report actions", async ({ page }) => {
  await page.route(/\/api\/reports\/daily\/2026-05-31$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: {
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
      },
      status: 200
    });
  });
  await page.route(/\/api\/reports\/daily\/2026-05-31\/versions$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: [{ report_id: "report-daily-demo-2026-05-31-v1" }],
      status: 200
    });
  });

  await page.goto("/reports/daily/2026-05-31");

  await expect(page.getByRole("heading", { name: "2026-05-31" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Download Markdown" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Telegram brief" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Compare report versions" })).toBeVisible();
});

test("trade detail drawer renders signal metadata and controls", async ({ page }) => {
  await routeDashboardApi(page);
  await page.goto("/dashboard");

  await page.getByRole("button", { name: "Open BTC/USDT:USDT detail" }).click();
  const drawer = page.getByRole("complementary", { name: "BTC/USDT:USDT detail" });

  await expect(drawer).toBeVisible();
  await expect(drawer.getByText("Position Detail")).toBeVisible();
  await expect(drawer.getByText("Freqtrade Trade ID")).toBeVisible();
  await expect(drawer.getByText("77")).toBeVisible();
  await expect(drawer.getByText("Structured Signal JSON")).toBeVisible();
  await expect(drawer.getByText("Take Profit")).toBeVisible();
  await expect(drawer.getByRole("button", { exact: true, name: "Close position" })).toBeVisible();
  await expect(drawer.getByRole("button", { name: "Move SL" })).toBeVisible();
});

test("orders center renders open positions, pending orders, history, and P&L", async ({ page }) => {
  await routeOrderCenterApi(page);
  await page.goto("/orders");

  await expect(page.getByText("Order Center")).toBeVisible();
  await expect(page.getByRole("heading", { name: "持仓" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "挂单" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "历史" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "盈亏" })).toBeVisible();

  await expect(page.getByText("BTC/USDT").first()).toBeVisible();
  await expect(page.getByText("order-open-1")).toBeVisible();
  await expect(page.getByText("trade-closed-1")).toBeVisible();
  await expect(page.getByText("$142.00").first()).toBeVisible();
});

test("signal review queue approves a needs_review signal with an audit reason", async ({ page }) => {
  const decisionAudit = { reason: "" };
  await routeSignalReviewApi(page, decisionAudit);
  await page.goto("/review");

  await expect(page.getByText("Signal Review")).toBeVisible();
  await expect(page.getByText("needs_review queue")).toBeVisible();
  await expect(page.getByRole("button", { name: "Open signal sig-review-1" })).toBeVisible();
  await expect(page.getByText("requires_human_review")).toBeVisible();
  await expect(page.getByText("Rate-limit adjustment request")).toBeVisible();

  await page.getByRole("button", { name: "Approve signal" }).click();
  const confirmButton = page.getByRole("button", { name: "Confirm approve signal" });
  await expect(page.getByRole("dialog", { name: "Approve signal" })).toBeVisible();
  await expect(confirmButton).toBeDisabled();

  await page.getByRole("textbox", { name: "Review decision reason" }).fill("parsed fields match source");
  await expect(confirmButton).toBeEnabled();
  await confirmButton.click();

  await expect(page.getByText("Review decision submitted")).toBeVisible();
  expect(decisionAudit.reason).toBe("parsed fields match source");
});

test("daily report page renders API snapshot and preview controls", async ({ page }) => {
  await page.route(/\/api\/reports\/daily\/2026-05-31$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: dailyReportPayload,
      status: 200
    });
  });
  await page.route(/\/api\/reports\/daily\/2026-05-31\/markdown$/, async (route) => {
    await route.fulfill({
      body: "# Hermes Daily Report 2026-05-31\n\nSmoke markdown",
      contentType: "text/markdown; charset=utf-8",
      status: 200
    });
  });
  await page.route(/\/api\/reports\/daily\/2026-05-31\/telegram-preview$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: {
        text: "Telegram smoke preview"
      },
      status: 200
    });
  });

  await page.goto("/reports/daily/2026-05-31");

  await expect(page.getByText("Daily Report")).toBeVisible();
  await expect(page.getByText("2026-05-31")).toBeVisible();
  await expect(page.getByText("交易表现")).toBeVisible();
  await expect(page.getByText("信号漏斗")).toBeVisible();
  await expect(page.getByText("Report ready")).toBeVisible();

  await page.getByRole("button", { name: "Telegram brief" }).click();
  await expect(page.getByRole("heading", { name: "Telegram 简版" })).toBeVisible();
  await expect(page.getByText("Telegram smoke preview")).toBeVisible();
});
