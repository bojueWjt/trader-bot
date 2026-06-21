import { ShieldAlert } from "lucide-react";
import type { ReactElement } from "react";
import type { OrderSettingScalar, OrderSettingsScope, RiskOverview } from "../../../utils/api";
import { moneySettingsFields } from "../orderSettingsDescriptor";
import type { OrderSettingsCategoryKey, SettingsFieldDescriptor } from "../orderSettingsDescriptor";
import { SettingsField } from "./SettingsField";
import type { SettingsFieldState } from "./SettingsField";

type MoneyRiskTabProps = {
  baselineFieldValue: (category: OrderSettingsCategoryKey, key: string) => OrderSettingScalar | "";
  fieldState: (category: OrderSettingsCategoryKey, key: string) => SettingsFieldState;
  readonly: boolean;
  riskOverview: RiskOverview;
  scope: OrderSettingsScope;
  onClear: (category: OrderSettingsCategoryKey, key: string) => void;
  onFieldChange: (category: OrderSettingsCategoryKey, key: string, value: OrderSettingScalar) => void;
};

const usageMetricLabels: Record<string, string[]> = {
  daily_loss_limit_pct: ["日亏损", "daily loss"],
  max_total_risk_pct: ["总风险", "total risk"],
  risk_per_trade_pct: ["单笔风险", "single trade risk"]
};

const limitFields = new Set([
  "daily_loss_limit_pct",
  "equity_fraction",
  "fixed_notional",
  "loss_cooldown_minutes",
  "max_correlated_exposure",
  "max_drawdown_pct",
  "max_instrument_exposure",
  "max_leverage",
  "max_notional_per_order",
  "max_open_positions",
  "max_total_risk_pct",
  "minimum_free_margin_pct",
  "reserve_balance_pct",
  "risk_per_trade_pct",
  "risk_reservation_ttl_seconds"
]);

export function MoneyRiskTab({
  baselineFieldValue,
  fieldState,
  readonly,
  riskOverview,
  scope,
  onClear,
  onFieldChange
}: MoneyRiskTabProps): ReactElement {
  return (
    <div className="settings-fields" role="tabpanel" aria-label="Money and Risk settings">
      {moneySettingsFields.map((field) => {
        const state = fieldState("money", field.key);
        return (
          <SettingsField
            category="money"
            disabledReason={disabledReason(field, fieldState)}
            effective={state.effective}
            field={field}
            isOverridden={state.isOverridden}
            key={field.key}
            readonly={readonly}
            scope={scope}
            value={state.value}
            onChange={onFieldChange}
            onClear={onClear}
          >
            <MoneyLimitUsage
              baselineValue={baselineFieldValue("money", field.key)}
              field={field}
              riskOverview={riskOverview}
              value={state.value}
            />
          </SettingsField>
        );
      })}
    </div>
  );
}

function MoneyLimitUsage({
  baselineValue,
  field,
  riskOverview,
  value
}: {
  baselineValue: OrderSettingScalar | "";
  field: SettingsFieldDescriptor;
  riskOverview: RiskOverview;
  value: OrderSettingScalar | "";
}): ReactElement | null {
  if (!limitFields.has(field.key)) {
    return null;
  }

  const metric = metricForField(field.key, riskOverview);
  const unavailable = !metric || riskOverview.dataSource.reconciliation_state === "failed";
  const currentUsage = unavailable ? "unavailable" : metric.value;
  const postChange = unavailable ? "unavailable" : estimatePostChange(metric.value, baselineValue, value);

  return (
    <dl className={unavailable ? "settings-limit-usage unavailable" : "settings-limit-usage"}>
      <div>
        <dt>
          <ShieldAlert size={13} aria-hidden="true" />
          Current usage
        </dt>
        <dd>{currentUsage}</dd>
      </div>
      <div>
        <dt>Post-change estimate</dt>
        <dd>{postChange}</dd>
      </div>
    </dl>
  );
}

function disabledReason(
  field: SettingsFieldDescriptor,
  fieldState: (category: OrderSettingsCategoryKey, key: string) => SettingsFieldState
): string {
  const sizingMode = String(fieldState("money", "sizing_mode").value);

  if (field.key === "fixed_notional" && sizingMode !== "fixed_notional") {
    return "Requires fixed_notional sizing";
  }

  if (field.key === "equity_fraction" && sizingMode !== "equity_fraction") {
    return "Requires equity_fraction sizing";
  }

  if (field.key === "risk_per_trade_pct" && sizingMode !== "fixed_risk") {
    return "Requires fixed_risk sizing";
  }

  return "";
}

function metricForField(fieldKey: string, riskOverview: RiskOverview): { value: string } | null {
  const labels = usageMetricLabels[fieldKey] || [];
  return riskOverview.metrics.find((metric) => {
    const normalized = metric.label.toLowerCase();
    return labels.some((label) => normalized.includes(label.toLowerCase()));
  }) || null;
}

function estimatePostChange(
  currentUsage: string,
  baselineValue: OrderSettingScalar | "",
  nextValue: OrderSettingScalar | ""
): string {
  const usagePct = parsePercent(currentUsage);
  const baseline = Number(baselineValue);
  const next = Number(nextValue);

  if (usagePct === null || !Number.isFinite(baseline) || !Number.isFinite(next) || baseline <= 0 || next <= 0) {
    return "unavailable";
  }

  return formatPercent((usagePct * baseline) / next);
}

function parsePercent(value: string): number | null {
  const match = value.match(/-?\d+(\.\d+)?/);
  if (!match) {
    return null;
  }

  const parsed = Number(match[0]);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatPercent(value: number): string {
  if (!Number.isFinite(value)) {
    return "unavailable";
  }

  const rounded = Math.round(value * 10) / 10;
  return Number.isInteger(rounded) ? `${rounded}%` : `${rounded.toFixed(1)}%`;
}
