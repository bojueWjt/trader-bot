import { AlertTriangle, RefreshCcw, Save, Settings2 } from "lucide-react";
import type { ReactElement } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { DangerConfirmDialog } from "../../components/settings/DangerConfirmDialog";
import { SaveReviewDialog } from "../../components/settings/SaveReviewDialog";
import type { SaveReviewChange } from "../../components/settings/SaveReviewDialog";
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
  OrderSettingsValidationResult,
  RiskOverview,
  SettingsVersionRecord,
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
import { SettingsHistory } from "./SettingsHistory";
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

type SettingsSnapshot = {
  effective: EffectiveOrderSettings;
  inherited: EffectiveOrderSettings;
  raw: OrderManagementSettings;
  risk: RiskOverview;
  systemHealth: SystemHealthSnapshot;
};

type PendingSaveReview = {
  changes: SaveReviewChange[];
  dangerous: boolean;
  expectedVersion: number | null;
  reason: string;
  settings: OrderSettingsValues;
  titleDetail: string;
  validation: OrderSettingsValidationResult;
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
  const [review, setReview] = useState<PendingSaveReview | null>(null);
  const [dangerReview, setDangerReview] = useState<PendingSaveReview | null>(null);
  const [conflictedReview, setConflictedReview] = useState<PendingSaveReview | null>(null);
  const readonly = role === "viewer";

  async function fetchSettingsSnapshot(nextScope: SettingsScopeSelection): Promise<SettingsSnapshot> {
    const inheritedScope = parentScope(nextScope);
    const [nextRaw, nextEffective, nextInherited, nextRiskOverview, nextSystemHealth] = await Promise.all([
      fetchOrderManagementSettings(nextScope),
      fetchEffectiveSettings(nextScope),
      inheritedScope ? fetchEffectiveSettings(inheritedScope) : Promise.resolve(getEmptyEffectiveSettings("global scope")),
      fetchRiskOverview(),
      fetchSystemHealthSnapshot()
    ]);

    return {
      effective: nextEffective,
      inherited: nextInherited,
      raw: nextRaw,
      risk: nextRiskOverview,
      systemHealth: nextSystemHealth
    };
  }

  function applySettingsSnapshot(snapshot: SettingsSnapshot, replaceDraft: boolean): void {
    setRawSettings(snapshot.raw);
    setEffectiveSettings(snapshot.effective);
    setInheritedSettings(snapshot.inherited);
    setRiskOverview(snapshot.risk);
    setSystemHealth(snapshot.systemHealth);
    if (replaceDraft) {
      setDraftSettings(cloneSettings(snapshot.raw.settings));
    }

    const failure = [snapshot.raw.dataSource, snapshot.effective.dataSource, snapshot.inherited.dataSource].find(
      (quality) => quality.reconciliation_state === "failed"
    );
    setError(failure ? failure.reason || "Order management settings unavailable" : "");
  }

  useEffect(() => {
    let active = true;

    async function load(): Promise<void> {
      setLoading(true);
      setError("");
      setValidationErrors([]);
      setReview(null);
      setDangerReview(null);
      setConflictedReview(null);

      const snapshot = await fetchSettingsSnapshot(scope);

      if (!active) {
        return;
      }

      applySettingsSnapshot(snapshot, true);
      setLoading(false);
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

  function changeField(category: OrderSettingsCategoryKey, key: string, value: OrderSettingScalar): void {
    setDraftSettings((current) => ({
      ...current,
      [category]: {
        ...(current[category] || {}),
        [key]: value
      }
    }));
    clearPendingSaveState();
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
    clearPendingSaveState();
    setValidationErrors([]);
    setStatus("");
  }

  function clearPendingSaveState(): void {
    setReview(null);
    setDangerReview(null);
    setConflictedReview(null);
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
    await prepareReview(draftSettings, trimmedReason, "");
  }

  async function prepareReview(settings: OrderSettingsValues, trimmedReason: string, titleDetail: string): Promise<void> {
    if (!trimmedReason) {
      setValidationErrors(["Reason is required before saving settings"]);
      setStatus("Validation failed");
      return;
    }

    const localErrors = validateLocalSettings((category, key) => reviewFieldValue(settings, category, key));
    if (localErrors.length > 0) {
      setValidationErrors(localErrors);
      setStatus("Validation failed");
      return;
    }

    const validation = await validateSettings(settings);
    setValidationErrors(validation.errors);
    if (!validation.valid) {
      setStatus("Validation failed");
      return;
    }

    setReview({
      changes: buildReviewChanges(rawSettings.settings, settings),
      dangerous: detectLiveRiskRelaxation(
        (category, key) => reviewFieldValue(settings, category, key),
        baselineFieldValue
      ).active,
      expectedVersion: rawSettings.version,
      reason: trimmedReason,
      settings: cloneSettings(settings),
      titleDetail,
      validation
    });
    setStatus("Review required before save");
  }

  async function submitReviewedSettings(nextReview: PendingSaveReview, danger?: { operatorSignoff: string }): Promise<void> {
    const result = await patchSettings({
      expected_version: nextReview.expectedVersion,
      reason: nextReview.reason,
      request_id: requestId(),
      scope: scope.scope,
      scope_key: scope.scopeKey,
      settings: nextReview.settings,
      ...(danger ? { confirm: true, operator_signoff: danger.operatorSignoff } : {})
    });

    setValidationErrors(result.errors);
    setReview(null);
    setDangerReview(null);
    if (!result.ok) {
      if (result.conflict) {
        setConflictedReview(nextReview);
        setStatus(`Version conflict: current version ${result.currentVersion ?? "unknown"}`);
        return;
      }

      setStatus("Save failed");
      return;
    }

    setReason("");
    setStatus(result.version === null ? "Settings saved" : `Settings saved at version ${result.version}`);
    setRefreshKey((current) => current + 1);
  }

  async function replayAfterConflict(): Promise<void> {
    if (!conflictedReview) {
      return;
    }

    const snapshot = await fetchSettingsSnapshot(scope);
    applySettingsSnapshot(snapshot, false);
    setDraftSettings(cloneSettings(conflictedReview.settings));

    const validation = await validateSettings(conflictedReview.settings);
    setValidationErrors(validation.errors);
    if (!validation.valid) {
      setStatus("Validation failed after refresh");
      setConflictedReview(null);
      return;
    }

    setReview({
      ...conflictedReview,
      changes: buildReviewChanges(snapshot.raw.settings, conflictedReview.settings),
      dangerous: detectLiveRiskRelaxation(
        (category, key) => reviewFieldValue(conflictedReview.settings, category, key),
        (category, key) => baselineFieldValueFromSnapshot(snapshot, category, key)
      ).active,
      expectedVersion: snapshot.raw.version,
      validation
    });
    setConflictedReview(null);
    setStatus("Review refreshed after conflict");
  }

  function rollbackVersion(version: SettingsVersionRecord): void {
    if (readonly || Object.keys(version.settings).length === 0) {
      return;
    }

    const rollbackReason = `Rollback to version ${version.version ?? "unversioned"}`;
    setReason(rollbackReason);
    setDraftSettings(cloneSettings(version.settings));
    void prepareReview(version.settings, rollbackReason, rollbackReason);
  }

  function reviewFieldValue(
    settings: OrderSettingsValues,
    category: OrderSettingsCategoryKey,
    key: string
  ): OrderSettingScalar | "" {
    const categorySettings = settings[category] || {};
    if (Object.prototype.hasOwnProperty.call(categorySettings, key)) {
      return categorySettings[key];
    }

    const field = allFields[category].find((candidate) => candidate.key === key);
    if (scope.scope !== "global") {
      return inheritedSettings.settings[category]?.[key]?.value ?? field?.defaultValue ?? "";
    }

    return effectiveSettings.settings[category]?.[key]?.value ?? field?.defaultValue ?? "";
  }

  function buildReviewChanges(before: OrderSettingsValues, after: OrderSettingsValues): SaveReviewChange[] {
    const categories = Array.from(new Set([...Object.keys(before), ...Object.keys(after)])) as OrderSettingsCategoryKey[];
    return categories.flatMap((category) => {
      const descriptors = allFields[category] || [];
      const keys = Array.from(new Set([
        ...Object.keys(before[category] || {}),
        ...Object.keys(after[category] || {})
      ]));

      return keys.flatMap((key) => {
        const descriptor = descriptors.find((field) => field.key === key);
        const beforeValue = reviewValueFromSettings(before, category, key);
        const afterValue = reviewFieldValue(after, category, key);
        const beforeText = formatSettingValue(beforeValue);
        const afterText = formatSettingValue(afterValue);
        if (beforeText === afterText) {
          return [];
        }

        return [{
          after: afterText,
          before: beforeText,
          category,
          effective: formatSettingValue(afterValue),
          key,
          label: descriptor?.label || key
        }];
      });
    });
  }

  function reviewValueFromSettings(
    settings: OrderSettingsValues,
    category: OrderSettingsCategoryKey,
    key: string
  ): OrderSettingScalar | "" {
    const categorySettings = settings[category] || {};
    if (Object.prototype.hasOwnProperty.call(categorySettings, key)) {
      return categorySettings[key];
    }

    const field = allFields[category].find((candidate) => candidate.key === key);
    return effectiveSettings.settings[category]?.[key]?.value ?? field?.defaultValue ?? "";
  }

  function baselineFieldValueFromSnapshot(
    snapshot: SettingsSnapshot,
    category: OrderSettingsCategoryKey,
    key: string
  ): OrderSettingScalar | "" {
    const field = allFields[category].find((candidate) => candidate.key === key);
    const rawCategory = snapshot.raw.settings[category] || {};
    if (Object.prototype.hasOwnProperty.call(rawCategory, key)) {
      return rawCategory[key];
    }

    return snapshot.effective.settings[category]?.[key]?.value ?? field?.defaultValue ?? "";
  }

  return (
    <section className="page-grid" data-testid="settings-orders-page">
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
              setStatus("");
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
        <ScopeSelector
          disabled={loading}
          value={scope}
          onChange={(nextScope) => {
            setStatus("");
            setScope(nextScope);
          }}
        />
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
                setStatus("");
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

          {conflictedReview && (
            <section className="validation-summary" role="alert" aria-label="Settings version conflict">
              <h3>Settings version conflict</h3>
              <p>{status}</p>
              <div className="settings-state-actions">
                <button
                  className="secondary-button"
                  onClick={() => {
                    void replayAfterConflict();
                  }}
                  type="button"
                >
                  <RefreshCcw size={16} />
                  Refresh and replay review
                </button>
              </div>
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
                  data-testid="settings-tab-general"
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
                  data-testid="settings-tab-entry"
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
                  data-testid="settings-tab-money"
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
                  data-testid="settings-tab-protection"
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
                  data-testid="settings-tab-monitoring"
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
                  data-testid="settings-tab-emergency"
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
                  data-testid="settings-tab-notifications"
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
                  data-testid="settings-tab-advanced"
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

          {!readonly && (
            <section className="settings-save-panel" aria-label="Save settings" data-testid="settings-save-panel">
              {liveRiskRelaxation.active && (
                <section
                  aria-label="Live risk relaxation confirmation"
                  className="live-risk-confirmation"
                  role="alert"
                >
                  <header>
                    <AlertTriangle size={18} aria-hidden="true" />
                    <h3>Live risk relaxation requires dangerous confirmation</h3>
                  </header>
                  <p>Execution mode is live and these changes relax risk limits:</p>
                  <ul>
                    {liveRiskRelaxation.changes.map((change) => (
                      <li key={change.key}>
                        {change.label}: {formatSettingValue(change.before)} -&gt; {formatSettingValue(change.after)}
                      </li>
                    ))}
                  </ul>
                </section>
              )}
              <label>
                <span>Reason</span>
                <textarea
                  data-testid="settings-save-reason"
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
                  data-testid="settings-validate-button"
                  onClick={() => {
                    void validateDraft();
                  }}
                  type="button"
                >
                  Validate
                </button>
                <button
                  className="primary-button"
                  data-testid="settings-save-button"
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
          )}

          <SettingsHistory
            readonly={readonly}
            refreshKey={refreshKey}
            scope={{ scope: scope.scope, scopeKey: scope.scopeKey }}
            onRollback={rollbackVersion}
          />
        </>
      )}
      {review && (
        <SaveReviewDialog
          changes={review.changes}
          dangerous={review.dangerous}
          expectedVersion={review.expectedVersion}
          reason={review.reason}
          titleDetail={review.titleDetail}
          validation={review.validation}
          onCancel={() => {
            setReview(null);
          }}
          onConfirm={() => {
            if (review.dangerous) {
              setDangerReview(review);
              setReview(null);
              return;
            }

            void submitReviewedSettings(review);
          }}
        />
      )}
      {dangerReview && (
        <DangerConfirmDialog
          changes={dangerReview.changes}
          reason={dangerReview.reason}
          onCancel={() => {
            setDangerReview(null);
          }}
          onConfirm={(operatorSignoff) => {
            void submitReviewedSettings(dangerReview, { operatorSignoff });
          }}
        />
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
