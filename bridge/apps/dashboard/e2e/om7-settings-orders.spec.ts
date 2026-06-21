import { expect, test, type Page, type Route } from "@playwright/test";

const baseQuality = {
  data_source: "settings_projection",
  generated_at: "2026-06-20T09:00:00Z",
  last_execution_event_at: null,
  missing_nodes: [],
  projection_lag_ms: 12,
  reconciliation_state: "healthy",
  snapshot_id: "om7-e2e-snapshot",
  stale: false
};

const baseSettings = {
  entry: {
    default_order_type: "limit",
    limit_offset_bps: 5
  },
  general: {
    execution_mode: "shadow",
    order_manager_enabled: true
  },
  money: {
    max_total_risk_pct: 8,
    risk_per_trade_pct: 1,
    sizing_mode: "fixed_risk"
  },
  price_monitor: {
    market_data_stale_seconds: 10,
    projection_lag_threshold_ms: 5000
  },
  reconciliation: {
    orphan_order_policy: "halt",
    reconciliation_interval_seconds: 300,
    startup_reconciliation_required: true
  }
};

const effectiveSettings = {
  entry: {
    default_order_type: field("limit"),
    limit_offset_bps: field(5)
  },
  general: {
    execution_mode: field("shadow"),
    order_manager_enabled: field(true)
  },
  money: {
    max_total_risk_pct: field(8),
    risk_per_trade_pct: field(1),
    sizing_mode: field("fixed_risk")
  },
  price_monitor: {
    market_data_stale_seconds: field(10),
    projection_lag_threshold_ms: field(5000)
  },
  reconciliation: {
    orphan_order_policy: field("halt"),
    reconciliation_interval_seconds: field(300),
    startup_reconciliation_required: field(true)
  }
};

function field(value: unknown): Record<string, unknown> {
  return {
    apply_mode: "hot_reload",
    inherited: false,
    source_scope: "global",
    value
  };
}

async function fulfillJson(route: Route, json: unknown, status = 200): Promise<void> {
  await route.fulfill({
    contentType: "application/json",
    json,
    status
  });
}

async function routeCommon(page: Page): Promise<void> {
  await page.route(/\/v1\/stream$/, async (route) => {
    await route.abort();
  });
}

async function routeSettingsApi(page: Page, audit: { patchBody?: Record<string, unknown> }): Promise<void> {
  await routeCommon(page);

  await page.route(/\/v1\/order-management\/settings\?scope=global&scope_key=$/, async (route) => {
    await fulfillJson(route, {
      ...baseQuality,
      scope: "global",
      scope_key: "",
      settings: baseSettings,
      version: 7
    });
  });

  await page.route(/\/v1\/order-management\/settings\/effective\?account_id=&instrument_id=$/, async (route) => {
    await fulfillJson(route, {
      ...baseQuality,
      settings: effectiveSettings
    });
  });

  await page.route(/\/v1\/order-management\/settings\/versions\?scope=global&scope_key=$/, async (route) => {
    await fulfillJson(route, {
      ...baseQuality,
      versions: [
        {
          author: "alice",
          created_at: "2026-06-20T09:10:00Z",
          desired_version: 8,
          diff: [{ after: 2, before: 1, key: "money.risk_per_trade_pct" }],
          effective_version: 7,
          node_id: "node-a",
          reason: "temporary event risk",
          settings: {
            ...baseSettings,
            money: {
              ...baseSettings.money,
              risk_per_trade_pct: 2
            }
          },
          version: 8
        },
        {
          author: "bob",
          created_at: "2026-06-19T08:00:00Z",
          diff: [{ after: 1, before: 0.5, key: "money.risk_per_trade_pct" }],
          reason: "restore baseline risk",
          settings: baseSettings,
          version: 6
        }
      ]
    });
  });

  await page.route(/\/v1\/order-management\/settings\/validate$/, async (route) => {
    await fulfillJson(route, {
      diff: [
        { after: 2, before: 1, category: "money", effective: 2, key: "risk_per_trade_pct", label: "Risk per trade pct" }
      ],
      impact: ["node-a hot_reload"],
      valid: true
    });
  });

  await page.route(/\/v1\/order-management\/settings$/, async (route) => {
    audit.patchBody = route.request().postDataJSON() as Record<string, unknown>;
    await fulfillJson(route, { ok: true, version: 8 });
  });

  await page.route(/\/v1\/risk\/state$/, async (route) => {
    await fulfillJson(route, {
      ...baseQuality,
      daily_loss_usage_pct: 40,
      pair_locks: [],
      single_trade_risk_usage_pct: 50,
      total_open_risk_usage_pct: 25
    });
  });

  await page.route(/\/api\/system\/snapshot$/, async (route) => {
    await fulfillJson(route, {
      ...baseQuality,
      data: {
        node_health: []
      }
    });
  });
}

async function routeOrdersApi(page: Page, audit: { commandBody?: Record<string, unknown> }): Promise<void> {
  await routeCommon(page);

  await page.route(/\/v1\/positions$/, async (route) => {
    await fulfillJson(route, {
      ...baseQuality,
      positions: [
        {
          account_id: "acc-1",
          amount: 1,
          entry_price: 40000,
          execution_job_id: "job-1",
          instrument_symbol: "BTC/USDT",
          mark_price: 41000,
          position_id: "pos-1",
          protection_status: "protected",
          signal_id: "sig-1",
          status: "open"
        }
      ]
    });
  });

  await page.route(/\/v1\/orders$/, async (route) => {
    await fulfillJson(route, {
      ...baseQuality,
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
          protection_status: "protected",
          remaining: 1,
          role: "entry",
          side: "buy",
          status: "open",
          trade_id: "pos-1"
        }
      ]
    });
  });

  await page.route(/\/v1\/trades$/, async (route) => {
    await fulfillJson(route, {
      ...baseQuality,
      trades: []
    });
  });

  await page.route(/\/v1\/commands$/, async (route) => {
    audit.commandBody = route.request().postDataJSON() as Record<string, unknown>;
    await fulfillJson(route, {
      acks: [{ acknowledged_at: "2026-06-20T12:03:00Z", node_id: "node-a", status: "acked" }],
      command_id: "command-cancel",
      status: "completed",
      target_nodes: ["node-a"]
    });
  });
}

async function replaceFocusedValue(page: Page, value: string): Promise<void> {
  await page.keyboard.press(process.platform === "darwin" ? "Meta+A" : "Control+A");
  await page.keyboard.type(value);
}

test("settings save review is completable by keyboard with server diff and audit reason", async ({ page }) => {
  const audit: { patchBody?: Record<string, unknown> } = {};
  await routeSettingsApi(page, audit);
  await page.goto("/settings/orders");

  await page.getByTestId("settings-tab-money").focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("settings-tabpanel-money")).toBeVisible();

  await page.getByTestId("settings-control-money-risk_per_trade_pct").focus();
  await replaceFocusedValue(page, "2");
  await page.getByTestId("settings-save-reason").focus();
  await page.keyboard.type("keyboard risk review");
  await page.getByTestId("settings-save-button").focus();
  await page.keyboard.press("Enter");

  await expect(page.getByTestId("save-review-dialog")).toBeVisible();
  await expect(page.getByText("Risk per trade pct")).toBeVisible();
  await expect(page.getByText("node-a hot_reload")).toBeVisible();

  await page.getByTestId("save-review-confirm").focus();
  await page.keyboard.press("Enter");

  await expect(page.getByText("Settings saved at version 8")).toBeVisible();
  expect(audit.patchBody?.reason).toBe("keyboard risk review");
});

test("settings history rollback opens the same review flow and warns on desired effective drift", async ({ page }) => {
  const audit: { patchBody?: Record<string, unknown> } = {};
  await routeSettingsApi(page, audit);
  await page.goto("/settings/orders");

  await expect(page.getByRole("alert", { name: "Desired settings not fully applied" })).toContainText("node-a desired 8 effective 7");

  await page.getByRole("button", { name: "Rollback to version 6" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("save-review-dialog")).toContainText("Rollback to version 6");
  await page.getByTestId("save-review-confirm").focus();
  await page.keyboard.press("Enter");

  await expect(page.getByText("Settings saved at version 8")).toBeVisible();
  expect(audit.patchBody?.reason).toBe("Rollback to version 6");
});

test("orders filters, detail, and manual cancel are keyboard reachable", async ({ page }) => {
  const audit: { commandBody?: Record<string, unknown> } = {};
  await routeOrdersApi(page, audit);
  await page.goto("/orders");

  await expect(page.getByTestId("orders-page")).toBeVisible();
  await page.getByTestId("orders-filter-account").focus();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(page.getByText("ord-1")).toBeVisible();

  await page.getByRole("button", { name: "Open order ord-1 detail" }).focus();
  await page.keyboard.press("Enter");
  const drawer = page.getByTestId("orders-detail-drawer");
  await expect(drawer).toContainText("Intent intent-1");

  await drawer.getByRole("button", { name: "Cancel order ord-1" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("order-action-dialog")).toBeVisible();
  await page.getByRole("textbox", { name: "Action reason" }).focus();
  await page.keyboard.type("keyboard cancel stale order");
  await page.getByRole("button", { name: "Confirm cancel order" }).focus();
  await page.keyboard.press("Enter");

  await expect(page.getByText("Command completed")).toBeVisible();
  expect(audit.commandBody).toEqual({
    args: {
      order_id: "ord-1",
      reason: "keyboard cancel stale order"
    },
    type: "cancel_order"
  });
});
