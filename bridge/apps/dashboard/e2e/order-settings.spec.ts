import { expect, test, type Page, type Route } from "@playwright/test";

type SettingsValues = Record<string, Record<string, boolean | number | string>>;

type PatchResponse = {
  json: Record<string, unknown>;
  status?: number;
};

type SettingsAudit = {
  patches: Record<string, unknown>[];
  validations: Record<string, unknown>[];
};

const quality = {
  data_source: "settings_projection",
  generated_at: "2026-06-21T10:00:00Z",
  last_execution_event_at: "2026-06-21T09:59:55Z",
  missing_nodes: [],
  projection_lag_ms: 9,
  reconciliation_state: "healthy",
  snapshot_id: "om8-settings-e2e",
  stale: false
};

const tabs = [
  ["settings-tab-general", "settings-tabpanel-general"],
  ["settings-tab-entry", "settings-tabpanel-entry"],
  ["settings-tab-protection", "settings-tabpanel-protection"],
  ["settings-tab-money", "settings-tabpanel-money"],
  ["settings-tab-monitoring", "settings-tabpanel-monitoring"],
  ["settings-tab-emergency", "settings-tabpanel-emergency"],
  ["settings-tab-notifications", "settings-tabpanel-notifications"],
  ["settings-tab-advanced", "settings-tabpanel-advanced"]
] as const;

function makeSettings(executionMode: "live" | "shadow" = "shadow"): SettingsValues {
  return {
    advanced: {
      diagnostics_bundle_enabled: true,
      event_retention_days: 90,
      import_export_enabled: true,
      outbox_batch_size: 100,
      rate_limit_per_minute: 60,
      reducer_replay_window_hours: 24,
      spool_max_age_seconds: 3600,
      spool_max_items: 10000
    },
    emergency: {
      allow_close_while_halted: true,
      cancel_batch_size: 10,
      close_all_final_state: "flat",
      close_batch_size: 5,
      command_max_retries: 3,
      command_retention_days: 30,
      inter_order_delay_ms: 250,
      verify_timeout_seconds: 30
    },
    entry: {
      default_order_type: "limit",
      limit_offset_bps: 5,
      market_conversion_max_slippage_bps: 50,
      max_reprices: 3,
      max_slippage_bps: 25,
      max_submit_retries: 3,
      minimum_fill_ratio: 0.95,
      partial_fill_policy: "convert_remainder_to_market",
      partial_fill_timeout_seconds: 30,
      post_only: false,
      reprice_interval_seconds: 10,
      submit_retry_backoff_seconds: 1,
      time_in_force: "GTC",
      unfilled_timeout_seconds: 60
    },
    general: {
      allowed_instruments: "BTC/USDT,ETH/USDT",
      default_account_id: "acc-main",
      default_command_timeout_seconds: 30,
      execution_mode: executionMode,
      idempotency_retention_days: 30,
      intent_ttl_seconds: 300,
      max_concurrent_execution_jobs: 2,
      order_manager_enabled: true,
      risk_posture: "ACTIVE"
    },
    money: {
      daily_loss_limit_pct: 5,
      equity_fraction: 0,
      fixed_notional: 0,
      loss_cooldown_minutes: 1440,
      max_correlated_exposure: 50000,
      max_drawdown_pct: 10,
      max_instrument_exposure: 25000,
      max_leverage: 5,
      max_notional_per_order: 12000,
      max_open_positions: 6,
      max_total_risk_pct: 8,
      minimum_free_margin_pct: 50,
      reserve_balance_pct: 20,
      risk_per_trade_pct: 1,
      risk_reservation_ttl_seconds: 300,
      sizing_mode: "fixed_risk"
    },
    notifications: {
      daily_loss_enabled: true,
      drawdown_enabled: true,
      emergency_command_results_enabled: true,
      notification_channels: "dashboard,telegram",
      order_accepted_enabled: true,
      order_filled_enabled: true,
      order_rejected_enabled: true,
      partial_fill_enabled: true,
      protection_missing_enabled: true,
      reconciliation_drift_enabled: true,
      severity_routing: "critical:operator,high:operator,medium:dashboard",
      stale_account_enabled: true,
      stale_market_enabled: true
    },
    price_monitor: {
      account_data_stale_seconds: 15,
      disconnect_action: "halt",
      evaluation_interval_seconds: 5,
      execution_event_stale_seconds: 30,
      market_data_stale_seconds: 10,
      price_deviation_bps: 200,
      projection_lag_threshold_ms: 5000,
      protection_watchdog_interval_seconds: 10,
      reconciliation_stale_seconds: 300
    },
    protection: {
      breakeven_enabled: true,
      breakeven_offset_bps: 0,
      breakeven_trigger_r: 1,
      partial_close_default_fraction: 0.25,
      protection_install_timeout_seconds: 60,
      protection_repair_policy: "reducing",
      replace_protection_behavior: "cancel_replace",
      require_stop: true,
      stop_order_type: "stop_market",
      stop_trigger_basis: "mark",
      take_profit_ladder: "[{\"target_r\":1,\"fraction\":0.5}]",
      trailing_activation_r: 1.5,
      trailing_callback_rate: 0.5,
      trailing_min_step_bps: 5,
      trailing_stop_enabled: true,
      trailing_update_rate_limit_seconds: 5
    },
    reconciliation: {
      auto_adopt_enabled: false,
      drift_policy: "halt",
      external_position_policy: "halt",
      orphan_order_policy: "halt",
      reconciliation_interval_seconds: 300,
      startup_reconciliation_required: true
    }
  };
}

function settingField(value: boolean | number | string, sourceScope = "global", inherited = false): Record<string, unknown> {
  return {
    apply_mode: "hot_reload",
    inherited,
    source_scope: sourceScope,
    value
  };
}

function effectiveFrom(settings: SettingsValues, sourceScope = "global", inherited = false): Record<string, Record<string, Record<string, unknown>>> {
  return Object.fromEntries(
    Object.entries(settings).map(([category, fields]) => [
      category,
      Object.fromEntries(
        Object.entries(fields).map(([key, value]) => [key, settingField(value, sourceScope, inherited)])
      )
    ])
  );
}

function mergeSettings(base: SettingsValues, override: SettingsValues): SettingsValues {
  return Object.fromEntries(
    Object.entries({ ...base, ...override }).map(([category]) => [
      category,
      {
        ...(base[category] || {}),
        ...(override[category] || {})
      }
    ])
  );
}

function accountOverride(): SettingsValues {
  return {
    money: {
      risk_per_trade_pct: 0.75
    }
  };
}

function instrumentOverride(): SettingsValues {
  return {
    entry: {
      limit_offset_bps: 7
    }
  };
}

async function fulfillJson(route: Route, json: unknown, status = 200): Promise<void> {
  await route.fulfill({
    contentType: "application/json",
    json,
    status
  });
}

async function seedRole(page: Page, role: "risk_admin" | "viewer" = "risk_admin"): Promise<void> {
  await page.addInitScript((nextRole) => {
    window.localStorage.setItem("hermes.auth.token", "om8-e2e-token");
    window.localStorage.setItem("hermes.auth.role", nextRole);
    window.sessionStorage.clear();
  }, role);
}

async function routeSettingsApi(
  page: Page,
  audit: SettingsAudit,
  options: {
    executionMode?: "live" | "shadow";
    patchResponses?: PatchResponse[];
  } = {}
): Promise<void> {
  const baseSettings = makeSettings(options.executionMode || "shadow");
  const patchResponses = options.patchResponses || [{ json: { ok: true, version: 12 }, status: 200 }];
  let patchCount = 0;

  await page.route(/\/(api|v1)\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const scope = url.searchParams.get("scope") || "global";
    const scopeKey = url.searchParams.get("scope_key") || "";
    const version = patchCount > 0 ? 8 : 7;

    if (url.pathname === "/v1/stream") {
      await route.abort();
      return;
    }

    if (url.pathname === "/v1/order-management/settings/validate") {
      const body = request.postDataJSON() as Record<string, unknown>;
      audit.validations.push(body);
      await fulfillJson(route, {
        diff: [
          {
            after: 2,
            before: 1,
            effective: 2,
            key: "money.risk_per_trade_pct",
            label: "Risk per trade pct"
          }
        ],
        impact: ["node-a hot_reload", "node-b hot_reload"],
        valid: true
      });
      return;
    }

    if (url.pathname === "/v1/order-management/settings/effective") {
      const accountId = url.searchParams.get("account_id") || "";
      const instrumentId = url.searchParams.get("instrument_id") || "";
      const override = instrumentId ? instrumentOverride() : accountId ? accountOverride() : {};
      const sourceScope = instrumentId ? "instrument" : accountId ? "account" : "global";
      await fulfillJson(route, {
        ...quality,
        settings: effectiveFrom(mergeSettings(baseSettings, override), sourceScope, sourceScope !== "global")
      });
      return;
    }

    if (url.pathname === "/v1/order-management/settings/versions") {
      await fulfillJson(route, {
        ...quality,
        versions: [
          {
            author: "risk-admin",
            created_at: "2026-06-21T09:45:00Z",
            desired_version: 9,
            diff: [{ after: 2, before: 1, key: "money.risk_per_trade_pct" }],
            effective_version: 8,
            node_id: "node-a",
            reason: "temporary CPI event risk",
            settings: mergeSettings(baseSettings, { money: { risk_per_trade_pct: 2 } }),
            version: 9
          },
          {
            author: "ops",
            created_at: "2026-06-20T08:00:00Z",
            diff: [{ after: 1, before: 0.5, key: "money.risk_per_trade_pct" }],
            reason: "restore normal risk",
            settings: mergeSettings(baseSettings, { money: { risk_per_trade_pct: 0.5 } }),
            version: 6
          }
        ]
      });
      return;
    }

    if (url.pathname === "/v1/order-management/settings") {
      if (request.method() === "PATCH") {
        audit.patches.push(request.postDataJSON() as Record<string, unknown>);
        const response = patchResponses[Math.min(patchCount, patchResponses.length - 1)];
        patchCount += 1;
        await fulfillJson(route, response.json, response.status || 200);
        return;
      }

      const settings = scope === "account" ? accountOverride() : scope === "instrument" ? instrumentOverride() : baseSettings;
      await fulfillJson(route, {
        ...quality,
        scope,
        scope_key: scopeKey,
        settings,
        version
      });
      return;
    }

    if (url.pathname === "/v1/risk/state") {
      await fulfillJson(route, {
        ...quality,
        daily_loss_usage_pct: 32,
        no_sl_trade_count: 0,
        pair_locks: [],
        single_trade_risk_usage_pct: 45,
        total_open_risk_usage_pct: 55
      });
      return;
    }

    if (url.pathname === "/api/system/snapshot") {
      await fulfillJson(route, {
        ...quality,
        data: {
          node_health: [
            {
              node_id: "node-a",
              payload: JSON.stringify({
                account_data_last_seen_at: "2026-06-21T09:59:45Z",
                execution_event_last_seen_at: "2026-06-21T09:59:55Z",
                market_data_last_seen_at: "2026-06-21T09:59:50Z",
                reconciliation_verified_at: "2026-06-21T09:59:40Z"
              })
            }
          ]
        }
      });
      return;
    }

    await route.abort();
  });
}

test.beforeEach(async ({ page }) => {
  await seedRole(page, "risk_admin");
});

test("risk_admin covers all tabs, scope inheritance, validation, and save review diff", async ({ page }) => {
  const audit: SettingsAudit = { patches: [], validations: [] };
  await routeSettingsApi(page, audit);
  await page.goto("/settings/orders");

  await expect(page.getByTestId("settings-orders-page")).toBeVisible();
  await expect(page.getByText("risk_admin")).toBeVisible();

  for (const [tabId, panelId] of tabs) {
    await page.getByTestId(tabId).click();
    await expect(page.getByTestId(panelId)).toBeVisible();
  }

  await page.getByTestId("settings-scope-account").click();
  await page.getByTestId("settings-scope-key").fill("acc-main");
  await page.getByTestId("settings-scope-apply").click();
  const scopeStatus = page.getByRole("region", { name: "Settings scope and status" });
  await expect(scopeStatus.getByText("acc-main", { exact: true })).toBeVisible();
  await page.getByTestId("settings-tab-general").click();
  await expect(page.getByTestId("settings-field-general-execution_mode")).toContainText("Inherited from global");

  await page.getByTestId("settings-scope-instrument").click();
  await page.getByTestId("settings-scope-key").fill("BTC/USDT");
  await page.getByTestId("settings-scope-apply").click();
  await expect(scopeStatus.getByText("BTC/USDT", { exact: true })).toBeVisible();
  await page.getByTestId("settings-tab-entry").click();
  await expect(page.getByTestId("settings-field-entry-default_order_type")).toContainText("Inherited from global");

  await page.getByTestId("settings-scope-global").click();
  await expect(page.getByText("global").first()).toBeVisible();
  await page.getByTestId("settings-tab-money").click();
  await page.getByTestId("settings-control-money-risk_per_trade_pct").fill("2");

  await page.getByTestId("settings-save-button").click();
  await expect(page.getByRole("alert", { name: "Settings validation errors" })).toContainText(
    "Reason is required before saving settings"
  );

  await page.getByTestId("settings-validate-button").click();
  await expect(page.getByText("Validation passed")).toBeVisible();

  await page.getByTestId("settings-save-reason").fill("raise risk for BTC liquidity window");
  await page.getByTestId("settings-save-button").click();

  const dialog = page.getByTestId("save-review-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("Expected version 7");
  await expect(dialog).toContainText("Risk per trade pct");
  await expect(dialog).toContainText("node-a hot_reload");
  await page.getByTestId("save-review-confirm").click();

  await expect(page.getByText("Settings saved at version 12")).toBeVisible();
  expect(audit.validations).toHaveLength(2);
  expect(audit.patches).toHaveLength(1);
  expect(audit.patches[0]).toMatchObject({
    expected_version: 7,
    reason: "raise risk for BTC liquidity window",
    scope: "global",
    scope_key: "",
    settings: {
      money: {
        risk_per_trade_pct: 2
      }
    }
  });
});

test("expected_version conflict refreshes settings and replays the review with the new version", async ({ page }) => {
  const audit: SettingsAudit = { patches: [], validations: [] };
  await routeSettingsApi(page, audit, {
    patchResponses: [
      { json: { current_version: 8, detail: [{ message: "expected_version conflict" }] }, status: 409 },
      { json: { ok: true, version: 9 }, status: 200 }
    ]
  });
  await page.goto("/settings/orders");

  await page.getByTestId("settings-tab-money").click();
  await page.getByTestId("settings-control-money-risk_per_trade_pct").fill("1.5");
  await page.getByTestId("settings-save-reason").fill("replay after concurrent operator update");
  await page.getByTestId("settings-save-button").click();
  await expect(page.getByTestId("save-review-dialog")).toContainText("Expected version 7");
  await page.getByTestId("save-review-confirm").click();

  const conflict = page.getByRole("alert", { name: "Settings version conflict" });
  await expect(conflict).toContainText("current version 8");
  await page.getByRole("button", { name: "Refresh and replay review" }).click();

  await expect(page.getByTestId("save-review-dialog")).toContainText("Expected version 8");
  await page.getByTestId("save-review-confirm").click();
  await expect(page.getByText("Settings saved at version 9")).toBeVisible();

  expect(audit.patches).toHaveLength(2);
  expect(audit.patches[0].expected_version).toBe(7);
  expect(audit.patches[1].expected_version).toBe(8);
});

test("version history surfaces desired/effective drift and rollback uses review flow", async ({ page }) => {
  const audit: SettingsAudit = { patches: [], validations: [] };
  await routeSettingsApi(page, audit);
  await page.goto("/settings/orders");

  await expect(page.getByTestId("settings-history")).toBeVisible();
  await expect(page.getByRole("alert", { name: "Desired settings not fully applied" })).toContainText("node-a desired 9 effective 8");
  await expect(page.getByText("Version 6")).toBeVisible();

  await page.getByRole("button", { name: "Rollback to version 6" }).click();
  await expect(page.getByTestId("save-review-dialog")).toContainText("Rollback to version 6");
  await page.getByTestId("save-review-confirm").click();

  await expect(page.getByText("Settings saved at version 12")).toBeVisible();
  expect(audit.patches.at(-1)).toMatchObject({
    reason: "Rollback to version 6",
    settings: {
      money: {
        risk_per_trade_pct: 0.5
      }
    }
  });
});

test("live risk relaxation requires dangerous confirmation and operator signoff", async ({ page }) => {
  const audit: SettingsAudit = { patches: [], validations: [] };
  await routeSettingsApi(page, audit, { executionMode: "live" });
  await page.goto("/settings/orders");

  await page.getByTestId("settings-tab-money").click();
  await page.getByTestId("settings-control-money-risk_per_trade_pct").fill("2");
  await expect(page.getByRole("alert", { name: "Live risk relaxation confirmation" })).toContainText("Risk per trade pct");

  await page.getByTestId("settings-save-reason").fill("temporary live risk relaxation approved in incident bridge");
  await page.getByTestId("settings-save-button").click();
  await expect(page.getByTestId("save-review-dialog")).toContainText("Risk per trade pct");
  await page.getByTestId("save-review-continue-danger").click();

  const dangerDialog = page.getByTestId("danger-confirm-dialog");
  await expect(dangerDialog).toBeVisible();
  await expect(dangerDialog).toContainText("Operator signoff");
  await expect(page.getByTestId("danger-confirm-submit")).toBeDisabled();
  await page.getByTestId("danger-confirm-checkbox").check();
  await page.getByTestId("danger-confirm-signoff").fill("risk-admin#123");
  await page.getByTestId("danger-confirm-submit").click();

  await expect(page.getByText("Settings saved at version 12")).toBeVisible();
  expect(audit.patches.at(-1)).toMatchObject({
    confirm: true,
    operator_signoff: "risk-admin#123"
  });
});

test("viewer role has read-only settings without write controls", async ({ page }) => {
  const audit: SettingsAudit = { patches: [], validations: [] };
  await seedRole(page, "viewer");
  await routeSettingsApi(page, audit);
  await page.goto("/settings/orders");

  await expect(page.getByText("Read-only viewer")).toBeVisible();
  await expect(page.getByTestId("settings-save-panel")).toHaveCount(0);
  await expect(page.getByTestId("settings-save-button")).toHaveCount(0);
  await expect(page.getByTestId("settings-validate-button")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Rollback to version/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Clear .* override/ })).toHaveCount(0);
  await expect(page.getByTestId("settings-control-money-risk_per_trade_pct")).toHaveCount(0);

  await page.getByTestId("settings-tab-money").click();
  await expect(page.getByTestId("settings-control-money-risk_per_trade_pct")).toBeDisabled();
  expect(audit.patches).toHaveLength(0);
});
