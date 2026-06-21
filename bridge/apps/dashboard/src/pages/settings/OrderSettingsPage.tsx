import { AlertTriangle, RefreshCcw, Save, Settings2 } from "lucide-react";
import type { ReactElement } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ScopeSelector } from "../../components/settings/ScopeSelector";
import type { SettingsScopeSelection } from "../../components/settings/ScopeSelector";
import {
  fetchEffectiveSettings,
  fetchOrderManagementSettings,
  getEmptyEffectiveSettings,
  getEmptyOrderManagementSettings,
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
  OrderSettingsValues
} from "../../utils/api";
import { entrySettingsFields, generalSettingsFields } from "./orderSettingsDescriptor";
import type { OrderSettingsCategoryKey, SettingsFieldDescriptor } from "./orderSettingsDescriptor";
import { EntryOrdersTab } from "./tabs/EntryOrdersTab";
import { GeneralTab } from "./tabs/GeneralTab";
import type { SettingsFieldState } from "./tabs/SettingsField";

type OrderSettingsPageProps = {
  role: AuthRole;
};

type SettingsTab = "entry" | "general";

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
  const [draftSettings, setDraftSettings] = useState<OrderSettingsValues>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [validationErrors, setValidationErrors] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [status, setStatus] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const readonly = role === "viewer";

  useEffect(() => {
    let active = true;

    async function load(): Promise<void> {
      setLoading(true);
      setError("");
      setValidationErrors([]);
      setStatus("");

      const inheritedScope = parentScope(scope);
      const [nextRaw, nextEffective, nextInherited] = await Promise.all([
        fetchOrderManagementSettings(scope),
        fetchEffectiveSettings(scope),
        inheritedScope ? fetchEffectiveSettings(inheritedScope) : Promise.resolve(getEmptyEffectiveSettings("global scope"))
      ]);

      if (!active) {
        return;
      }

      setRawSettings(nextRaw);
      setEffectiveSettings(nextEffective);
      setInheritedSettings(nextInherited);
      setDraftSettings(cloneSettings(nextRaw.settings));
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

  const allFields = useMemo(() => ({
    entry: entrySettingsFields,
    general: generalSettingsFields
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

  function changeField(category: OrderSettingsCategoryKey, key: string, value: OrderSettingScalar): void {
    setDraftSettings((current) => ({
      ...current,
      [category]: {
        ...(current[category] || {}),
        [key]: value
      }
    }));
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
    setValidationErrors([]);
    setStatus("");
  }

  async function validateDraft(): Promise<boolean> {
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
      settings: draftSettings
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
              <h3 id="settings-error-summary">Validation errors</h3>
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
          </section>

          <section className="settings-save-panel" aria-label="Save settings">
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
                disabled={readonly}
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
