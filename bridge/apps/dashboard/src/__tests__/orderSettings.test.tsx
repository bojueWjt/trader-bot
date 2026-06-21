import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";

const baseQuality = {
  data_source: "settings_projection",
  generated_at: "2026-06-20T09:00:00Z",
  last_execution_event_at: null,
  missing_nodes: [],
  projection_lag_ms: 12,
  reconciliation_state: "healthy",
  snapshot_id: "22222222-2222-4222-8222-222222222222",
  stale: false
};

afterEach(() => {
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
  window.history.pushState({}, "", "/");
  vi.unstubAllGlobals();
});

function jsonResponse(payload: unknown): Promise<Response> {
  return Promise.resolve({
    json: () => Promise.resolve(payload),
    ok: true
  } as Response);
}

function settingsPayload(
  scope: string,
  scopeKey: string,
  settings: Record<string, Record<string, unknown>>,
  version = 7
): Record<string, unknown> {
  return {
    ...baseQuality,
    scope,
    scope_key: scopeKey,
    settings,
    version
  };
}

function field(value: unknown, sourceScope: string, inherited: boolean): Record<string, unknown> {
  return {
    apply_mode: "hot_reload",
    inherited,
    source_scope: sourceScope,
    value
  };
}

function effectivePayload(settings: Record<string, Record<string, Record<string, unknown>>>): Record<string, unknown> {
  return {
    ...baseQuality,
    settings
  };
}

function systemSnapshot(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    ...baseQuality,
    data_source: "postgres_projection",
    schema_version: "1.0",
    data: {
      account: {},
      audit_trail: [],
      balances: { equity: 10000, margin: 2500 },
      hermes_decisions: [],
      node_health: [],
      orders: [],
      positions: [],
      recent_messages: [],
      risk_decisions: []
    },
    ...overrides
  };
}

function baseGlobalSettings(): Record<string, Record<string, unknown>> {
  return {
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
}

function baseEffectiveSettings(): Record<string, Record<string, Record<string, unknown>>> {
  return {
    entry: {
      default_order_type: field("limit", "global", false),
      limit_offset_bps: field(5, "global", false)
    },
    general: {
      execution_mode: field("shadow", "global", false),
      max_concurrent_execution_jobs: field(3, "global", false),
      order_manager_enabled: field(true, "global", false)
    },
    money: {
      daily_loss_limit_pct: field(5, "global", false),
      max_drawdown_pct: field(12, "global", false),
      max_total_risk_pct: field(8, "global", false),
      risk_per_trade_pct: field(1, "global", false),
      sizing_mode: field("fixed_risk", "global", false)
    },
    price_monitor: {
      market_data_stale_seconds: field(10, "global", false),
      projection_lag_threshold_ms: field(5000, "global", false)
    },
    reconciliation: {
      orphan_order_policy: field("halt", "global", false),
      reconciliation_interval_seconds: field(300, "global", false),
      startup_reconciliation_required: field(true, "global", false)
    }
  };
}

type SettingsApiOptions = {
  effectiveSettings?: Record<string, Record<string, Record<string, unknown>>>;
  globalSettings?: Record<string, Record<string, unknown>>;
  riskState?: Record<string, unknown>;
  role?: string;
  snapshot?: Record<string, unknown>;
};

function stubSettingsApi(options: string | SettingsApiOptions = "risk_admin"): ReturnType<typeof vi.fn> {
  const config = typeof options === "string" ? { role: options } : options;
  const role = config.role || "risk_admin";
  const globalSettings = config.globalSettings || baseGlobalSettings();
  const effectiveSettings = config.effectiveSettings || baseEffectiveSettings();
  const riskState = config.riskState || {
    ...baseQuality,
    daily_loss_usage_pct: 40,
    pair_locks: [],
    single_trade_risk_usage_pct: 50,
    total_open_risk_usage_pct: 25
  };
  const snapshot = config.snapshot || systemSnapshot();

  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const path = String(input);

    if (path === "/v1/order-management/settings?scope=global&scope_key=") {
      return jsonResponse(settingsPayload("global", "", globalSettings));
    }

    if (path === "/v1/order-management/settings/effective?account_id=&instrument_id=") {
      return jsonResponse(effectivePayload(effectiveSettings));
    }

    if (path === "/v1/order-management/settings?scope=account&scope_key=acc-1") {
      return jsonResponse(settingsPayload("account", "acc-1", {
        entry: {
          default_order_type: "market"
        }
      }, 8));
    }

    if (path === "/v1/order-management/settings/effective?account_id=acc-1&instrument_id=") {
      return jsonResponse(effectivePayload({
        entry: {
          default_order_type: field("market", "account", false),
          limit_offset_bps: field(5, "global", true)
        },
        general: {
          execution_mode: field("shadow", "global", true),
          order_manager_enabled: field(true, "global", true)
        }
      }));
    }

    if (path === "/v1/risk/state") {
      return jsonResponse(riskState);
    }

    if (path === "/api/system/snapshot") {
      return jsonResponse(snapshot);
    }

    return Promise.reject(new Error(`unexpected ${path}`));
  });

  if (role === "viewer") {
    const tokenPayload = btoa(JSON.stringify({ exp: 4102444800, role: "viewer" }))
      .replaceAll("+", "-")
      .replaceAll("/", "_")
      .replace(/=+$/, "");
    localStorage.setItem("hermes.auth.token", `header.${tokenPayload}.sig`);
  }

  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("EventSource", undefined);
  return fetchMock;
}

describe("order management settings", () => {
  it("shows effective inheritance and restores inherited values when clearing an account override", async () => {
    stubSettingsApi();

    render(<App initialPath="/settings/orders" />);

    expect(await screen.findByRole("heading", { name: "Order Management Settings" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Account" }));
    fireEvent.change(screen.getByLabelText("Account ID"), { target: { value: "acc-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply scope" }));
    fireEvent.click(await screen.findByRole("tab", { name: "Entry Orders" }));

    const defaultType = await screen.findByRole("group", { name: "Default order type setting" });
    const defaultTypeEffective = within(defaultType).getByLabelText("Default order type effective value");
    expect(within(defaultTypeEffective).getByText("Effective value")).toBeInTheDocument();
    expect(within(defaultTypeEffective).getByText("market")).toBeInTheDocument();
    expect(within(defaultTypeEffective).getByText("source_scope")).toBeInTheDocument();
    expect(within(defaultTypeEffective).getByText("account")).toBeInTheDocument();
    expect(within(defaultType).getByText("Overridden at account")).toBeInTheDocument();

    const limitOffset = screen.getByRole("group", { name: "Limit offset bps setting" });
    expect(within(limitOffset).getByText("Inherited from global")).toBeInTheDocument();
    expect(within(limitOffset).getByText("5")).toBeInTheDocument();

    fireEvent.click(within(defaultType).getByRole("button", { name: "Clear Default order type override" }));

    expect(within(defaultTypeEffective).getByText("limit")).toBeInTheDocument();
    expect(within(defaultType).getByText("Inherited from global")).toBeInTheDocument();
  });

  it("renders General and Entry fields from metadata with correct controls and interlocks", async () => {
    stubSettingsApi();

    render(<App initialPath="/settings/orders" />);

    expect(await screen.findByLabelText("Order manager enabled")).toHaveAttribute("type", "checkbox");
    expect(screen.getByLabelText("Execution mode")).toHaveRole("combobox");
    expect(screen.getByText("Range 1-100")).toBeInTheDocument();
    expect(screen.getByText("Default 1")).toBeInTheDocument();
    expect(screen.getByText("Unit jobs")).toBeInTheDocument();
    expect(screen.getByLabelText("Allowed instruments")).toHaveAttribute("type", "text");

    fireEvent.click(screen.getByRole("tab", { name: "Entry Orders" }));

    expect(screen.getByLabelText("Default order type")).toHaveRole("combobox");
    expect(screen.getByLabelText("Time in force")).toHaveRole("combobox");
    expect(screen.getByLabelText("Post only")).toHaveAttribute("type", "checkbox");
    expect(screen.getByLabelText("Minimum fill ratio")).toHaveAttribute("type", "number");
    expect(screen.getByText("Default cancel_remainder")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Default order type"), { target: { value: "market" } });
    expect(screen.getByLabelText("Limit offset bps")).toBeDisabled();
  });

  it("disables settings controls for viewer role", async () => {
    stubSettingsApi("viewer");
    window.history.pushState({}, "", "/settings/orders");

    render(<App />);

    expect(await screen.findByText("Read-only viewer")).toBeInTheDocument();
    expect(screen.getByLabelText("Order manager enabled")).toBeDisabled();
    expect(screen.getByLabelText("Execution mode")).toBeDisabled();
    fireEvent.click(screen.getByRole("tab", { name: "Money & Risk" }));
    expect(await screen.findByLabelText("Risk per trade pct")).toBeDisabled();
    fireEvent.click(screen.getByRole("tab", { name: "Monitoring" }));
    expect(await screen.findByLabelText("Market data stale seconds")).toBeDisabled();
    expect(screen.getByLabelText("Startup reconciliation required")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save settings" })).toBeDisabled();
  });

  it("renders Money & Risk fields from metadata with usage and post-change estimates", async () => {
    stubSettingsApi();

    render(<App initialPath="/settings/orders" />);

    fireEvent.click(await screen.findByRole("tab", { name: "Money & Risk" }));

    expect(screen.getByLabelText("Sizing mode")).toHaveRole("combobox");
    expect(screen.getByLabelText("Risk per trade pct")).toHaveAttribute("type", "number");
    expect(screen.getByLabelText("Max instrument exposure")).toHaveAttribute("type", "number");
    expect(screen.getByLabelText("Risk reservation ttl seconds")).toHaveAttribute("type", "number");
    expect(screen.getByText("Default fixed_risk")).toBeInTheDocument();
    expect(screen.getByText("Range 0-10")).toBeInTheDocument();

    const riskPerTrade = screen.getByRole("group", { name: "Risk per trade pct setting" });
    expect(within(riskPerTrade).getByText("Current usage")).toBeInTheDocument();
    expect(within(riskPerTrade).getByText("50%")).toBeInTheDocument();
    expect(within(riskPerTrade).getByText("Post-change estimate")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Risk per trade pct"), { target: { value: "2" } });
    expect(within(riskPerTrade).getByText("25%")).toBeInTheDocument();
  });

  it("requires a prominent confirmation before saving live risk relaxations", async () => {
    const globalSettings = baseGlobalSettings();
    globalSettings.general = {
      ...globalSettings.general,
      execution_mode: "live"
    };
    const effectiveSettings = baseEffectiveSettings();
    effectiveSettings.general = {
      ...effectiveSettings.general,
      execution_mode: field("live", "global", false)
    };
    stubSettingsApi({ effectiveSettings, globalSettings });

    render(<App initialPath="/settings/orders" />);

    fireEvent.click(await screen.findByRole("tab", { name: "Money & Risk" }));
    fireEvent.change(screen.getByLabelText("Risk per trade pct"), { target: { value: "2" } });

    const dangerConfirmation = await screen.findByRole("alert", { name: "Live risk relaxation confirmation" });
    expect(dangerConfirmation).toHaveTextContent("Live risk relaxation requires confirmation");
    expect(dangerConfirmation).toHaveTextContent("Risk per trade pct: 1 -> 2");
    expect(screen.getByRole("button", { name: "Save settings" })).toBeDisabled();

    fireEvent.click(within(dangerConfirmation).getByLabelText("I understand this relaxes live risk limits"));
    expect(screen.getByRole("button", { name: "Save settings" })).not.toBeDisabled();
  });

  it("renders Monitoring fields and surfaces stale live freshness state", async () => {
    stubSettingsApi({
      snapshot: systemSnapshot({
        missing_nodes: ["node-a"],
        projection_lag_ms: 120000,
        reconciliation_state: "failed",
        stale: true
      })
    });

    render(<App initialPath="/settings/orders" />);

    fireEvent.click(await screen.findByRole("tab", { name: "Monitoring" }));

    expect(screen.getByLabelText("Market data stale seconds")).toHaveAttribute("type", "number");
    expect(screen.getByLabelText("Projection lag threshold ms")).toHaveAttribute("type", "number");
    expect(screen.getByLabelText("Orphan order policy")).toHaveRole("combobox");
    expect(screen.getByLabelText("External position policy")).toHaveRole("combobox");
    expect(screen.getByLabelText("Drift policy")).toHaveRole("combobox");
    expect(screen.getByText("Default halt_until_reconciled")).toBeInTheDocument();

    const staleAlert = await screen.findByRole("alert", { name: "Monitoring freshness alert" });
    expect(staleAlert).toHaveTextContent("stale=true; missing_nodes=node-a; reconciliation_state=failed");

    expect(screen.getByRole("group", { name: "Market data freshness" })).toHaveTextContent("Threshold 10 seconds");
    expect(screen.getByRole("group", { name: "Account data freshness" })).toHaveTextContent("unavailable");
    const projectionFreshness = screen.getByRole("group", { name: "Projection freshness" });
    expect(projectionFreshness).toHaveTextContent("Current 120000 ms");
    expect(projectionFreshness).toHaveTextContent("stale");
    expect(screen.getByRole("group", { name: "Reconciliation freshness" })).toHaveTextContent("failed");
  });

  it("validates take-profit ladder fraction sums in real time", async () => {
    stubSettingsApi();

    render(<App initialPath="/settings/orders" />);

    fireEvent.click(await screen.findByRole("tab", { name: "Protection & Exits" }));
    fireEvent.click(screen.getByRole("button", { name: "Add TP rung" }));
    fireEvent.click(screen.getByRole("button", { name: "Add TP rung" }));

    fireEvent.change(screen.getByLabelText("TP 1 fraction"), { target: { value: "0.7" } });
    fireEvent.change(screen.getByLabelText("TP 2 fraction"), { target: { value: "0.6" } });

    const ladderEditor = screen.getByRole("group", { name: "Take-profit ladder editor" });
    expect(within(ladderEditor).getByRole("alert")).toHaveTextContent("Take-profit fractions total 1.3; maximum is 1");

    fireEvent.change(screen.getByLabelText("TP 2 fraction"), { target: { value: "0.3" } });
    expect(within(ladderEditor).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("blocks dangerous protection combinations before settings save", async () => {
    const globalSettings = baseGlobalSettings();
    globalSettings.general = {
      ...globalSettings.general,
      execution_mode: "live"
    };
    const effectiveSettings = baseEffectiveSettings();
    effectiveSettings.general = {
      ...effectiveSettings.general,
      execution_mode: field("live", "global", false)
    };
    const fetchMock = stubSettingsApi({ effectiveSettings, globalSettings });

    render(<App initialPath="/settings/orders" />);

    fireEvent.click(await screen.findByRole("tab", { name: "Protection & Exits" }));
    fireEvent.click(screen.getByLabelText("Require stop"));
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "temporarily disable stops" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    expect(await screen.findByRole("alert", { name: "Settings validation errors" })).toHaveTextContent(
      "Require stop cannot be disabled while execution mode is live"
    );
    expect(fetchMock).not.toHaveBeenCalledWith("/v1/order-management/settings/validate", expect.any(Object));

    fireEvent.click(screen.getByLabelText("Require stop"));
    expect(screen.queryByRole("alert", { name: "Settings validation errors" })).not.toBeInTheDocument();
  });

  it("flags every Advanced setting as high risk", async () => {
    stubSettingsApi();

    render(<App initialPath="/settings/orders" />);

    fireEvent.click(await screen.findByRole("tab", { name: "Advanced" }));

    expect(screen.getByRole("group", { name: "Outbox batch size setting" })).toHaveTextContent("HIGH RISK");
    expect(screen.getByRole("group", { name: "Spool max items setting" })).toHaveTextContent("HIGH RISK");
    expect(screen.getByRole("group", { name: "Import export enabled setting" })).toHaveTextContent("HIGH RISK");
  });
});
