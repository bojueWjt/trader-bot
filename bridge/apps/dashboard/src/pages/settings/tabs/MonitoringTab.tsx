import { Activity, AlertTriangle } from "lucide-react";
import type { ReactElement } from "react";
import type {
  FreshnessSignal,
  FreshnessSignalKey,
  OrderSettingScalar,
  OrderSettingsScope,
  SystemHealthSnapshot
} from "../../../utils/api";
import { priceMonitorSettingsFields, reconciliationSettingsFields } from "../orderSettingsDescriptor";
import type { OrderSettingsCategoryKey } from "../orderSettingsDescriptor";
import { SettingsField } from "./SettingsField";
import type { SettingsFieldState } from "./SettingsField";

type MonitoringTabProps = {
  fieldState: (category: OrderSettingsCategoryKey, key: string) => SettingsFieldState;
  readonly: boolean;
  scope: OrderSettingsScope;
  systemHealth: SystemHealthSnapshot;
  onClear: (category: OrderSettingsCategoryKey, key: string) => void;
  onFieldChange: (category: OrderSettingsCategoryKey, key: string, value: OrderSettingScalar) => void;
};

type FreshnessThreshold = {
  unit: "ms" | "seconds";
  value: number | null;
};

const thresholdFields: Record<FreshnessSignalKey, { key: string; unit: FreshnessThreshold["unit"] }> = {
  account_data: { key: "account_data_stale_seconds", unit: "seconds" },
  execution_event: { key: "execution_event_stale_seconds", unit: "seconds" },
  market_data: { key: "market_data_stale_seconds", unit: "seconds" },
  projection: { key: "projection_lag_threshold_ms", unit: "ms" },
  reconciliation: { key: "reconciliation_stale_seconds", unit: "seconds" }
};

export function MonitoringTab({
  fieldState,
  readonly,
  scope,
  systemHealth,
  onClear,
  onFieldChange
}: MonitoringTabProps): ReactElement {
  return (
    <div className="monitoring-tab" role="tabpanel" aria-label="Monitoring settings">
      <FreshnessPanel fieldState={fieldState} systemHealth={systemHealth} />

      <div className="settings-fields">
        {priceMonitorSettingsFields.map((field) => {
          const state = fieldState("price_monitor", field.key);
          return (
            <SettingsField
              category="price_monitor"
              disabledReason=""
              effective={state.effective}
              field={field}
              isOverridden={state.isOverridden}
              key={field.key}
              readonly={readonly}
              scope={scope}
              value={state.value}
              onChange={onFieldChange}
              onClear={onClear}
            />
          );
        })}
        {reconciliationSettingsFields.map((field) => {
          const state = fieldState("reconciliation", field.key);
          return (
            <SettingsField
              category="reconciliation"
              disabledReason=""
              effective={state.effective}
              field={field}
              isOverridden={state.isOverridden}
              key={field.key}
              readonly={readonly}
              scope={scope}
              value={state.value}
              onChange={onFieldChange}
              onClear={onClear}
            />
          );
        })}
      </div>
    </div>
  );
}

function FreshnessPanel({
  fieldState,
  systemHealth
}: {
  fieldState: (category: OrderSettingsCategoryKey, key: string) => SettingsFieldState;
  systemHealth: SystemHealthSnapshot;
}): ReactElement {
  const missingNodes = systemHealth.dataSource.missing_nodes.length > 0
    ? systemHealth.dataSource.missing_nodes.join(", ")
    : "none";

  return (
    <section className="monitoring-freshness-panel" aria-label="Live freshness status">
      <header className="monitoring-freshness-heading">
        <div>
          <Activity size={16} aria-hidden="true" />
          <h4>Live freshness</h4>
        </div>
        <span className={`status-pill ${systemHealth.dataSource.degraded ? "warning" : "good"}`}>
          {systemHealth.dataSource.status}
        </span>
      </header>
      {systemHealth.dataSource.degraded && (
        <div className="monitoring-freshness-alert" role="alert" aria-label="Monitoring freshness alert">
          <AlertTriangle size={16} aria-hidden="true" />
          {`stale=${String(systemHealth.dataSource.stale)}; missing_nodes=${missingNodes}; reconciliation_state=${systemHealth.dataSource.reconciliation_state}`}
        </div>
      )}
      <div className="freshness-grid">
        {systemHealth.signals.map((signal) => {
          const threshold = thresholdForSignal(signal.key, fieldState);
          return <FreshnessCard key={signal.key} signal={signal} threshold={threshold} />;
        })}
      </div>
      {systemHealth.dataSource.reason && <p className="quality-reason">{systemHealth.dataSource.reason}</p>}
    </section>
  );
}

function FreshnessCard({
  signal,
  threshold
}: {
  signal: FreshnessSignal;
  threshold: FreshnessThreshold;
}): ReactElement {
  const stale = freshnessIsStale(signal, threshold);
  const status = signal.available ? (stale ? "stale" : signal.status) : "unavailable";

  return (
    <section
      aria-label={`${signal.label} freshness`}
      className={stale ? "freshness-card danger" : signal.available ? "freshness-card" : "freshness-card warning"}
      role="group"
    >
      <div className="freshness-card-heading">
        <strong>{signal.label}</strong>
        <span className={`status-pill ${stale ? "danger" : signal.available ? "good" : "warning"}`}>{status}</span>
      </div>
      <dl>
        <div>
          <dt>Threshold</dt>
          <dd>Threshold {formatThreshold(threshold)}</dd>
        </div>
        <div>
          <dt>Current</dt>
          <dd>Current {signal.current}</dd>
        </div>
        <div>
          <dt>Observed</dt>
          <dd>{signal.observedAt || "unavailable"}</dd>
        </div>
      </dl>
    </section>
  );
}

function thresholdForSignal(
  signalKey: FreshnessSignalKey,
  fieldState: (category: OrderSettingsCategoryKey, key: string) => SettingsFieldState
): FreshnessThreshold {
  const thresholdField = thresholdFields[signalKey];
  const value = Number(fieldState("price_monitor", thresholdField.key).value);
  return {
    unit: thresholdField.unit,
    value: Number.isFinite(value) ? value : null
  };
}

function freshnessIsStale(signal: FreshnessSignal, threshold: FreshnessThreshold): boolean {
  if (!signal.available) {
    return false;
  }

  if (signal.key === "projection" && signal.valueMs !== null && threshold.value !== null) {
    return signal.valueMs > threshold.value;
  }

  if (signal.key === "reconciliation") {
    return signal.status !== "healthy";
  }

  return signal.stale;
}

function formatThreshold(threshold: FreshnessThreshold): string {
  if (threshold.value === null) {
    return "unavailable";
  }

  return `${threshold.value} ${threshold.unit}`;
}
