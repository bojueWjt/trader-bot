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

function stubSettingsApi(role = "risk_admin"): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const path = String(input);

    if (path === "/v1/order-management/settings?scope=global&scope_key=") {
      return jsonResponse(settingsPayload("global", "", {
        entry: {
          default_order_type: "limit",
          limit_offset_bps: 5
        },
        general: {
          execution_mode: "shadow",
          order_manager_enabled: true
        }
      }));
    }

    if (path === "/v1/order-management/settings/effective?account_id=&instrument_id=") {
      return jsonResponse(effectivePayload({
        entry: {
          default_order_type: field("limit", "global", false),
          limit_offset_bps: field(5, "global", false)
        },
        general: {
          execution_mode: field("shadow", "global", false),
          max_concurrent_execution_jobs: field(3, "global", false),
          order_manager_enabled: field(true, "global", false)
        }
      }));
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
    expect(screen.getByRole("button", { name: "Save settings" })).toBeDisabled();
  });
});
