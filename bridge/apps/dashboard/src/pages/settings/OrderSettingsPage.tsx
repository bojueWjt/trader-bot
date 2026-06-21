import { AlertTriangle, RefreshCcw, Save, Settings2 } from "lucide-react";
import type { ReactElement } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ScopeSelector } from "../../components/settings/ScopeSelector";
import type { SettingsScopeSelection } from "../../components/settings/ScopeSelector";
import {
  fetchEffectiveSettings,
  fetchOrderManagementSettings,
  fetchRiskOverview,
  fetchSystemHealthSnapshot,
  getEmptyEffectiveSettings,
  getEmptyOrderManagementSettings,
  getEmptyRiskOverview,
  getEmptySystemHealthSnapshot,
  patchSettings,
  validateSettings
} from "../../utils/api";
import type {
  AuthRole,
  EffectiveOrderSetting,
  EffectiveOrderSettings,
  OrderManagementSettings,
  OrderSettingScalar,
  OrderSettingsScope,
  OrderSettingsValues,
  RiskOverview,
  SystemHealthSnapshot
} from "../../utils/api";
import {
  advancedSettingsFields,
  emergencySettingsFields,
  entrySettingsFields,
  generalSettingsFields,
  moneySettingsFields,
  notificationsSettingsFields,
  priceMonitorSettingsFields,
  protectionSettingsFields,
  reconciliationSettingsFields
} from "./orderSettingsDescriptor";
import { AdvancedTab } from "./tabs/AdvancedTab";
import { EmergencyTab } from "./tabs/EmergencyTab";
import type { OrderSettingsCategoryKey, SettingsFieldDescriptor } from "./orderSettingsDescriptor";
import { EntryOrdersTab } from "./tabs/EntryOrdersTab";
import { GeneralTab } from "./tabs/GeneralTab";
import { MoneyRiskTab } from "./tabs/MoneyRiskTab";
import { MonitoringTab } from "./tabs/MonitoringTab";
import { NotificationsTab } from "./tabs/NotificationsTab";
import { ProtectionTab } from "./tabs/ProtectionTab";
import { formatSettingValue } from "./tabs/SettingsField";
import type { SettingsFieldState } from "./tabs/SettingsField";

type OrderSettingsPageProps = {
  role: AuthRole;
};

type SettingsTab = "advanced" | "emergency" | "entry" | "general" | "money" | "monitoring" | "notifications" | "protection";

type LiveRiskRelaxation = {
  active: boolean;
  changes: LiveRiskRelaxationChange[];
  signature: string;
};

type LiveRiskRelaxationChange = {
  after: OrderSettingScalar | "";
  before: OrderSettingScalar | "";
  key: string;
  label: string;
};

const defaultScope: SettingsScopeSelection = {
  scope: "global",
  scopeKey: ""
};

export function OrderSettingsPage({ role }: OrderSettingsPageProps): ReactElement {
  const [scope, setScope] = useState<SettingsScopeSelection>(defaultScope);
  const [activeTab, setActiveTab] = useState<SettingsTab>("general");
  const [rawSettings, setRawSettings] = useState<OrderManagementSettings>(() => getEmptyOrderManagementSettings());
  const [effectiveSettings, setEffectiveSettings] = useState<EffectiveOrderSettings>(() => getEmptyEffectiveSettings());
  const [inheritedSettings, setInheritedSettings] = useState<EffectiveOrderSettings>(() => getEmptyEffectiveSettings());
  const [riskOverview, setRiskOverview] = useState<RiskOverview>(() => getEmptyRiskOverview());
  const [systemHealth, setSystemHealth] = useState<SystemHealthSnapshot>(() => getEmptySystemHealthSnapshot());
  const [draftSettings, setDraftSettings] = useState<OrderSettingsValues>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [validationErrors, setValidationErrors] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [status, setStatus] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [liveRiskConfirmed, setLiveRiskConfirmed] = useState(false);
  const readonly = role === "viewer";

  useEffect(() => {
    let active = true;

    async function load(): Promise<void> {
      setLoading(true);
      setError("");
      setValidationErrors([]);
      setStatus("");

      const inheritedScope = parentScope(scope);
      const [nextRaw, nextEffective, nextInherited, nextRiskOverview, nextSystemHealth] = await Promise.all([
        fetchOrderManagementSettings(scope),
        fetchEffectiveSettings(scope),
        inheritedScope ? fetchEffectiveSettings(inheritedScope) : Promise.resolve(getEmptyEffectiveSettings("global scope")),
        fetchRiskOverview(),
        fetchSystemHealthSnapshot()
      ]);

      if (!active) {
        return;
      }

      setRawSettings(nextRaw);
      setEffectiveSettings(nextEffective);
      setInheritedSettings(nextInherited);
      setRiskOverview(nextRiskOverview);
      setSystemHealth(nextSystemHealth);
      setDraftSettings(cloneSettings(nextRaw.settings));
      setLiveRiskConfirmed(false);
      setLoading(false);

      const failure = [nextRaw.dataSource, nextEffective.dataSource, nextInherited.dataSource].find(
        (quality) => quality.reconciliation_state === "failed"
      );
      if (failure) {
        setError(failure.reason || "Order management settings unavailable");
      }
    }

    void load();

    return () => {
      active = false;
    };
  }, [refreshKey, scope]);

  const allFields = useMemo<Record<OrderSettingsCategoryKey, SettingsFieldDescriptor[]>>(() => ({
    advanced: advancedSettingsFields,
    emergency: emergencySettingsFields,
    entry: entrySettingsFields,
    general: generalSettingsFields,
    money: moneySettingsFields,
    notifications: notificationsSettingsFields,
    price_monitor: priceMonitorSettingsFields,
    protection: protectionSettingsFields,
    reconciliation: reconciliationSettingsFields
  }), []);

  const fieldState = useCallback(
    (category: OrderSettingsCategoryKey, key: string): SettingsFieldState => {
      const field = allFields[category].find((candidate) => candidate.key === key);
      const fallback = field ? field.defaultValue : "";
      const categoryDraft = draftSettings[category] || {};
      const hasOverride = Object.prototype.hasOwnProperty.call(categoryDraft, key);
      const draftValue = categoryDraft[key];
      const apiEffective = effectiveSettings.settings[category]?.[key];
      const inheritedEffective = inheritedSettings.settings[category]?.[key];
      let effective: EffectiveOrderSetting;

      if (hasOverride) {
        effective = {
          applyMode: field?.applyMode || apiEffective?.applyMode || "hot_reload",
          inherited: false,
          sourceScope: scope.scope,
          value: draftValue
        };
      } else if (scope.scope !== "global" && inheritedEffective) {
        effective = {
          ...inheritedEffective,
          inherited: true
        };
      } else if (apiEffective) {
        effective = apiEffective;
      } else {
        effective = {
          applyMode: field?.applyMode || "hot_reload",
          inherited: true,
          sourceScope: "default",
          value: fallback
        };
      }

      return {
        effective,
        isOverridden: hasOverride,
        value: hasOverride ? draftValue : effective.value
      };
    },
    [allFields, draftSettings, effectiveSettings.settings, inheritedSettings.settings, scope.scope]
  );

  const hasSettings = !rawSettings.empty || !effectiveSettings.empty;

  const baselineFieldValue = useCallback(
    (category: OrderSettingsCategoryKey, key: string): OrderSettingScalar | "" => {
      const field = allFields[category].find((candidate) => candidate.key === key);
      const rawCategory = rawSettings.settings[category] || {};
      if (Object.prototype.hasOwnProperty.call(rawCategory, key)) {
        return rawCategory[key];
      }

      return effectiveSettings.settings[category]?.[key]?.value ?? field?.defaultValue ?? "";
    },
    [allFields, effectiveSettings.settings, rawSettings.settings]
  );

  const currentFieldValue = useCallback(
    (category: OrderSettingsCategoryKey, key: string): OrderSettingScalar | "" => fieldState(category, key).value,
    [fieldState]
  );

  const liveRiskRelaxation = useMemo(
    () => detectLiveRiskRelaxation(currentFieldValue, baselineFieldValue),
    [baselineFieldValue, currentFieldValue]
  );

  useEffect(() => {
    setLiveRiskConfirmed(false);
  }, [liveRiskRelaxation.signature]);

  function changeField(category: OrderSettingsCategoryKey, key: string, value: OrderSettingScalar): void {
    setDraftSettings((current) => ({
      ...current,
      [category]: {
        ...(current[category] || {}),
        [key]: value
      }
    }));
    setLiveRiskConfirmed(false);
    setValidationErrors([]);
    setStatus("");
  }

  function clearOverride(category: OrderSettingsCategoryKey, key: string): void {
    setDraftSettings((current) => {
      const categoryValues = { ...(current[category] || {}) };
      delete categoryValues[key];
      return {
        ...current,
        [category]: categoryValues
      };
    });
    setLiveRiskConfirmed(false);
    setValidationErrors([]);
    setStatus("");
  }

  async function validateDraft(): Promise<boolean> {
    const localErrors = validateLocalSettings(currentFieldValue);
    if (localErrors.length > 0) {
      setValidationErrors(localErrors);
      setStatus("Validation failed");
      return false;
    }

    const result = await validateSettings(draftSettings);
    setValidationErrors(result.errors);
    if (!result.valid) {
      setStatus("Validation failed");
      return false;
    }

    setStatus("Validation passed");
    return true;
  }

  async function saveDraft(): Promise<void> {
    if (readonly) {
      return;
    }

    const trimmedReason = reason.trim();
    if (!trimmedReason) {
      setValidationErrors(["Reason is required before saving settings"]);
      setStatus("Validation failed");
      return;
    }

    if (liveRiskRelaxation.active && !liveRiskConfirmed) {
      setValidationErrors(["Live risk relaxation confirmation is required before saving settings"]);
      setStatus("Validation failed");
      return;
    }

    const localErrors = validateLocalSettings(currentFieldValue);
    if (localErrors.length > 0) {
      setValidationErrors(localErrors);
      setStatus("Validation failed");
      return;
    }

    const valid = await validateDraft();
    if (!valid) {
      return;
    }

    const result = await patchSettings({
      expected_version: rawSettings.version,
      reason: trimmedReason,
      request_id: requestId(),
      scope: scope.scope,
      scope_key: scope.scopeKey,
      settings: draftSettings,
      ...(liveRiskRelaxation.active ? { confirm: true } : {})
    });

    setValidationErrors(result.errors);
    if (!result.ok) {
      setStatus("Save failed");
      return;
    }

    setReason("");
    setStatus(result.version === null ? "Settings saved" : `Settings saved at version ${result.version}`);
    setRefreshKey((current) => current + 1);
  }

  return (
    <section className="page-grid">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Order Management</p>
          <h2>Order Management Settings</h2>
        </div>
        <div className="settings-heading-actions">
          <span className={readonly ? "status-pill warning" : "status-pill good"}>
            {readonly ? "Read-only viewer" : role}
          </span>
          <button
            aria-label="Refresh settings"
            className="icon-button"
            disabled={loading}
            onClick={() => {
              setRefreshKey((current) => current + 1);
            }}
            title="Refresh settings"
            type="button"
          >
            <RefreshCcw size={16} />
          </button>
        </div>
      </div>

      <section className="settings-toolbar" aria-label="Settings scope and status">
        <ScopeSelector disabled={loading} value={scope} onChange={setScope} />
        <dl className="settings-version">
          <div>
            <dt>scope</dt>
            <dd>{scope.scope}</dd>
          </div>
          <div>
            <dt>scope_key</dt>
            <dd>{scope.scopeKey || "global"}</dd>
          </div>
          <div>
            <dt>version</dt>
            <dd>{rawSettings.version ?? "unversioned"}</dd>
          </div>
          <div>
            <dt>freshness</dt>
            <dd>{rawSettings.dataSource.status}</dd>
          </div>
        </dl>
      </section>

      {loading && (
        <section className="panel settings-state" aria-live="polite">
          <p className="empty-state">Loading order management settings.</p>
        </section>
      )}

      {!loading && error && (
        <section className="panel settings-state" role="alert">
          <header className="panel-header">
            <div>
              <AlertTriangle size={17} />
              <h3>Settings unavailable</h3>
            </div>
          </header>
          <p className="empty-state">{error}</p>
          <div className="settings-state-actions">
            <button
              className="secondary-button"
              onClick={() => {
                setRefreshKey((current) => current + 1);
              }}
              type="button"
            >
              <RefreshCcw size={16} />
              Retry
            </button>
          </div>
        </section>
      )}

      {!loading && !error && !hasSettings && (
        <section className="panel settings-state">
          <header className="panel-header">
            <div>
              <Settings2 size={17} />
              <h3>No settings returned</h3>
            </div>
          </header>
          <p className="empty-state">No order management settings were returned for this scope.</p>
        </section>
      )}

      {!loading && !error && hasSettings && (
        <>
          {validationErrors.length > 0 && (
            <section className="validation-summary" role="alert" aria-labelledby="settings-error-summary">
              <h3 id="settings-error-summary">Settings validation errors</h3>
              <ul>
                {validationErrors.map((validationError) => (
                  <li key={validationError}>{validationError}</li>
                ))}
              </ul>
            </section>
          )}

          <section className="panel settings-panel">
            <header className="panel-header">
              <div>
                <Settings2 size={17} />
                <h3>Order settings</h3>
              </div>
              <div className="settings-tabs" role="tablist" aria-label="Order settings categories">
                <button
                  aria-selected={activeTab === "general"}
                  className={activeTab === "general" ? "secondary-button active" : "secondary-button"}
                  onClick={() => {
                    setActiveTab("general");
                  }}
                  role="tab"
                  type="button"
                >
                  General
                </button>
                <button
                  aria-selected={activeTab === "entry"}
                  className={activeTab === "entry" ? "secondary-button active" : "secondary-button"}
                  onClick={() => {
                    setActiveTab("entry");
                  }}
                  role="tab"
                  type="button"
                >
                  Entry Orders
                </button>
                <button
                  aria-selected={activeTab === "money"}
                  className={activeTab === "money" ? "secondary-button active" : "secondary-button"}
                  onClick={() => {
                    setActiveTab("money");
                  }}
                  role="tab"
                  type="button"
                >
                  Money & Risk
                </button>
                <button
                  aria-selected={activeTab === "protection"}
                  className={activeTab === "protection" ? "secondary-button active" : "secondary-button"}
                  onClick={() => {
                    setActiveTab("protection");
                  }}
                  role="tab"
                  type="button"
                >
                  Protection & Exits
                </button>
                <button
                  aria-selected={activeTab === "monitoring"}
                  className={activeTab === "monitoring" ? "secondary-button active" : "secondary-button"}
                  onClick={() => {
                    setActiveTab("monitoring");
                  }}
                  role="tab"
                  type="button"
                >
                  Monitoring
                </button>
                <button
                  aria-selected={activeTab === "emergency"}
                  className={activeTab === "emergency" ? "secondary-button active" : "secondary-button"}
                  onClick={() => {
                    setActiveTab("emergency");
                  }}
                  role="tab"
                  type="button"
                >
                  Emergency
                </button>
                <button
                  aria-selected={activeTab === "notifications"}
                  className={activeTab === "notifications" ? "secondary-button active" : "secondary-button"}
                  onClick={() => {
                    setActiveTab("notifications");
                  }}
                  role="tab"
                  type="button"
                >
                  Notifications
                </button>
                <button
                  aria-selected={activeTab === "advanced"}
                  className={activeTab === "advanced" ? "secondary-button active" : "secondary-button"}
                  onClick={() => {
                    setActiveTab("advanced");
                  }}
                  role="tab"
                  type="button"
                >
                  Advanced
                </button>
              </div>
            </header>
            {activeTab === "general" && (
              <GeneralTab
                fieldState={fieldState}
                readonly={readonly}
                scope={scope.scope}
                onClear={clearOverride}
                onFieldChange={changeField}
              />
            )}
            {activeTab === "entry" && (
              <EntryOrdersTab
                fieldState={fieldState}
                readonly={readonly}
                scope={scope.scope}
                onClear={clearOverride}
                onFieldChange={changeField}
              />
            )}
            {activeTab === "money" && (
              <MoneyRiskTab
                baselineFieldValue={baselineFieldValue}
                fieldState={fieldState}
                readonly={readonly}
                riskOverview={riskOverview}
                scope={scope.scope}
                onClear={clearOverride}
                onFieldChange={changeField}
              />
            )}
            {activeTab === "protection" && (
              <ProtectionTab
                fieldState={fieldState}
                readonly={readonly}
                scope={scope.scope}
                onClear={clearOverride}
                onFieldChange={changeField}
              />
            )}
            {activeTab === "monitoring" && (
              <MonitoringTab
                fieldState={fieldState}
                readonly={readonly}
                scope={scope.scope}
                systemHealth={systemHealth}
                onClear={clearOverride}
                onFieldChange={changeField}
              />
            )}
            {activeTab === "emergency" && (
              <EmergencyTab
                fieldState={fieldState}
                readonly={readonly}
                scope={scope.scope}
                onClear={clearOverride}
                onFieldChange={changeField}
              />
            )}
            {activeTab === "notifications" && (
              <NotificationsTab
                fieldState={fieldState}
                readonly={readonly}
                scope={scope.scope}
                onClear={clearOverride}
                onFieldChange={changeField}
              />
            )}
            {activeTab === "advanced" && (
              <AdvancedTab
                fieldState={fieldState}
                readonly={readonly}
                scope={scope.scope}
                onClear={clearOverride}
                onFieldChange={changeField}
              />
            )}
          </section>

          <section className="settings-save-panel" aria-label="Save settings">
            {liveRiskRelaxation.active && (
              <section
                aria-label="Live risk relaxation confirmation"
                className="live-risk-confirmation"
                role="alert"
              >
                <header>
                  <AlertTriangle size={18} aria-hidden="true" />
                  <h3>Live risk relaxation requires confirmation</h3>
                </header>
                <p>Execution mode is live and these changes relax risk limits:</p>
                <ul>
                  {liveRiskRelaxation.changes.map((change) => (
                    <li key={change.key}>
                      {change.label}: {formatSettingValue(change.before)} -&gt; {formatSettingValue(change.after)}
                    </li>
                  ))}
                </ul>
                <label>
                  <input
                    checked={liveRiskConfirmed}
                    disabled={readonly}
                    onChange={(event) => {
                      setLiveRiskConfirmed(event.target.checked);
                    }}
                    type="checkbox"
                  />
                  <span>I understand this relaxes live risk limits</span>
                </label>
              </section>
            )}
            <label>
              <span>Reason</span>
              <textarea
                disabled={readonly}
                onChange={(event) => {
                  setReason(event.target.value);
                }}
                rows={3}
                value={reason}
              />
            </label>
            <div className="button-row">
              <button
                className="secondary-button"
                disabled={readonly}
                onClick={() => {
                  void validateDraft();
                }}
                type="button"
              >
                Validate
              </button>
              <button
                className="primary-button"
                disabled={readonly || (liveRiskRelaxation.active && !liveRiskConfirmed)}
                onClick={() => {
                  void saveDraft();
                }}
                type="button"
              >
                <Save size={16} />
                Save settings
              </button>
            </div>
            {status && <p className="drawer-status" role="status">{status}</p>}
          </section>
        </>
      )}
    </section>
  );
}

function parentScope(scope: SettingsScopeSelection): SettingsScopeSelection | false {
  if (scope.scope === "global") {
    return false;
  }

  return defaultScope;
}

function cloneSettings(settings: OrderSettingsValues): OrderSettingsValues {
  return Object.fromEntries(
    Object.entries(settings).map(([category, values]) => [category, { ...values }])
  );
}

function requestId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }

  return `settings-${Date.now()}`;
}

function validateLocalSettings(
  currentFieldValue: (category: OrderSettingsCategoryKey, key: string) => OrderSettingScalar | ""
): string[] {
  const errors: string[] = [];
  const executionMode = String(currentFieldValue("general", "execution_mode"));
  const requireStop = currentFieldValue("protection", "require_stop") === true;
  const breakevenEnabled = currentFieldValue("protection", "breakeven_enabled") === true;
  const trailingEnabled = currentFieldValue("protection", "trailing_stop_enabled") === true;
  const trailingCallbackRate = Number(currentFieldValue("protection", "trailing_callback_rate"));
  const ladderTotal = takeProfitLadderFractionTotal(currentFieldValue("protection", "take_profit_ladder"));

  if (ladderTotal > 1) {
    errors.push(`Take-profit fractions total ${formatValidationNumber(ladderTotal)}; maximum is 1`);
  }

  if (executionMode === "live" && !requireStop) {
    errors.push("Require stop cannot be disabled while execution mode is live");
  }

  if (!requireStop && breakevenEnabled) {
    errors.push("Breakeven cannot be enabled when Require stop is disabled");
  }

  if (!requireStop && trailingEnabled) {
    errors.push("Trailing stop cannot be enabled when Require stop is disabled");
  }

  if (trailingEnabled && (!Number.isFinite(trailingCallbackRate) || trailingCallbackRate <= 0)) {
    errors.push("Trailing callback rate must be greater than 0 when trailing stop is enabled");
  }

  return errors;
}

function takeProfitLadderFractionTotal(value: OrderSettingScalar | ""): number {
  if (typeof value !== "string" || !value.trim()) {
    return 0;
  }

  try {
    const parsed = JSON.parse(value) as unknown;
    if (!Array.isArray(parsed)) {
      return 0;
    }

    return parsed.reduce((sum, raw) => {
      const record = typeof raw === "object" && raw !== null ? raw as Record<string, unknown> : {};
      const fraction = Number(record.fraction);
      return Number.isFinite(fraction) ? sum + fraction : sum;
    }, 0);
  } catch {
    return 0;
  }
}

function formatValidationNumber(value: number): string {
  const rounded = Math.round(value * 1000) / 1000;
  return Number.isInteger(rounded) ? String(rounded) : String(rounded);
}

const riskRelaxationRules: Record<string, "decrease" | "increase"> = {
  daily_loss_limit_pct: "increase",
  equity_fraction: "increase",
  fixed_notional: "increase",
  loss_cooldown_minutes: "decrease",
  max_correlated_exposure: "increase",
  max_drawdown_pct: "increase",
  max_instrument_exposure: "increase",
  max_leverage: "increase",
  max_notional_per_order: "increase",
  max_open_positions: "increase",
  max_total_risk_pct: "increase",
  minimum_free_margin_pct: "decrease",
  reserve_balance_pct: "decrease",
  risk_per_trade_pct: "increase",
  risk_reservation_ttl_seconds: "decrease"
};

function detectLiveRiskRelaxation(
  currentFieldValue: (category: OrderSettingsCategoryKey, key: string) => OrderSettingScalar | "",
  baselineFieldValue: (category: OrderSettingsCategoryKey, key: string) => OrderSettingScalar | ""
): LiveRiskRelaxation {
  if (String(currentFieldValue("general", "execution_mode")) !== "live") {
    return {
      active: false,
      changes: [],
      signature: ""
    };
  }

  const changes = moneySettingsFields.flatMap((field) => {
    const direction = riskRelaxationRules[field.key];
    if (!direction) {
      return [];
    }

    const before = baselineFieldValue("money", field.key);
    const after = currentFieldValue("money", field.key);
    const beforeNumber = Number(before);
    const afterNumber = Number(after);

    if (!Number.isFinite(beforeNumber) || !Number.isFinite(afterNumber) || beforeNumber === afterNumber) {
      return [];
    }

    const relaxed = direction === "increase" ? afterNumber > beforeNumber : afterNumber < beforeNumber;
    if (!relaxed) {
      return [];
    }

    return [{
      after,
      before,
      key: field.key,
      label: field.label
    }];
  });

  return {
    active: changes.length > 0,
    changes,
    signature: changes.map((change) => `${change.key}:${change.before}->${change.after}`).join("|")
  };
}
