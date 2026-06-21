import { Plus, Trash2 } from "lucide-react";
import type { ReactElement } from "react";
import type { OrderSettingScalar, OrderSettingsScope } from "../../../utils/api";
import { protectionSettingsFields } from "../orderSettingsDescriptor";
import type { OrderSettingsCategoryKey, SettingsFieldDescriptor } from "../orderSettingsDescriptor";
import { SettingsField } from "./SettingsField";
import type { SettingsFieldState } from "./SettingsField";

type ProtectionTabProps = {
  fieldState: (category: OrderSettingsCategoryKey, key: string) => SettingsFieldState;
  readonly: boolean;
  scope: OrderSettingsScope;
  onClear: (category: OrderSettingsCategoryKey, key: string) => void;
  onFieldChange: (category: OrderSettingsCategoryKey, key: string, value: OrderSettingScalar) => void;
};

type TakeProfitRung = {
  fraction: string;
  targetR: string;
};

export function ProtectionTab({
  fieldState,
  readonly,
  scope,
  onClear,
  onFieldChange
}: ProtectionTabProps): ReactElement {
  const requireStop = fieldState("protection", "require_stop").value === true;

  return (
    <div className="settings-fields" role="tabpanel" aria-label="Protection and Exits settings">
      {protectionSettingsFields.map((field) => {
        const state = fieldState("protection", field.key);
        return (
          <SettingsField
            category="protection"
            disabledReason={disabledReason(field, requireStop)}
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
            {field.key === "take_profit_ladder" && (
              <TakeProfitLadderEditor
                readonly={readonly}
                value={state.value}
                onChange={(nextValue) => {
                  onFieldChange("protection", "take_profit_ladder", nextValue);
                }}
              />
            )}
          </SettingsField>
        );
      })}
    </div>
  );
}

function TakeProfitLadderEditor({
  readonly,
  value,
  onChange
}: {
  readonly: boolean;
  value: OrderSettingScalar | "";
  onChange: (value: string) => void;
}): ReactElement {
  const rows = parseTakeProfitLadder(value);
  const total = rows.reduce((sum, row) => {
    const fraction = Number(row.fraction);
    return Number.isFinite(fraction) ? sum + fraction : sum;
  }, 0);
  const sumTooHigh = total > 1;
  const parseFailed = typeof value === "string" && value.trim().length > 0 && rows.length === 0;

  function updateRows(nextRows: TakeProfitRung[]): void {
    onChange(formatTakeProfitLadder(nextRows));
  }

  return (
    <section className="tp-ladder-editor" role="group" aria-label="Take-profit ladder editor">
      <div className="tp-ladder-heading">
        <span>Take-profit ladder</span>
        <button
          className="secondary-button"
          disabled={readonly}
          onClick={() => {
            updateRows([...rows, { fraction: "0", targetR: String(rows.length + 1) }]);
          }}
          type="button"
        >
          <Plus size={14} />
          Add TP rung
        </button>
      </div>
      {rows.length === 0 && <p className="empty-state compact">No take-profit rungs configured.</p>}
      {rows.map((row, index) => (
        <div className="tp-ladder-row" key={`${index}-${row.targetR}-${row.fraction}`}>
          <label>
            <span>R</span>
            <input
              aria-label={`TP ${index + 1} R`}
              disabled={readonly}
              min="0"
              onChange={(event) => {
                const nextRows = [...rows];
                nextRows[index] = { ...row, targetR: event.target.value };
                updateRows(nextRows);
              }}
              step="any"
              type="number"
              value={row.targetR}
            />
          </label>
          <label>
            <span>Fraction</span>
            <input
              aria-label={`TP ${index + 1} fraction`}
              disabled={readonly}
              max="1"
              min="0"
              onChange={(event) => {
                const nextRows = [...rows];
                nextRows[index] = { ...row, fraction: event.target.value };
                updateRows(nextRows);
              }}
              step="any"
              type="number"
              value={row.fraction}
            />
          </label>
          <button
            aria-label={`Remove TP ${index + 1}`}
            className="icon-button"
            disabled={readonly}
            onClick={() => {
              updateRows(rows.filter((_, rowIndex) => rowIndex !== index));
            }}
            title={`Remove TP ${index + 1}`}
            type="button"
          >
            <Trash2 size={14} />
          </button>
        </div>
      ))}
      {sumTooHigh && (
        <p className="field-error" role="alert">
          Take-profit fractions total {formatLadderNumber(total)}; maximum is 1
        </p>
      )}
      {parseFailed && (
        <p className="field-error" role="alert">
          Take-profit ladder JSON must be an array of rungs
        </p>
      )}
    </section>
  );
}

function disabledReason(field: SettingsFieldDescriptor, requireStop: boolean): string {
  if (
    !requireStop &&
    (
      field.key === "stop_order_type" ||
      field.key === "stop_trigger_basis" ||
      field.key === "protection_install_timeout_seconds" ||
      field.key === "breakeven_enabled" ||
      field.key === "breakeven_trigger_r" ||
      field.key === "breakeven_offset_bps" ||
      field.key === "trailing_stop_enabled" ||
      field.key === "trailing_activation_r" ||
      field.key === "trailing_callback_rate" ||
      field.key === "trailing_min_step_bps" ||
      field.key === "trailing_update_rate_limit_seconds"
    )
  ) {
    return "Requires Require stop";
  }

  return "";
}

function parseTakeProfitLadder(value: OrderSettingScalar | ""): TakeProfitRung[] {
  if (typeof value !== "string" || !value.trim()) {
    return [];
  }

  try {
    const parsed = JSON.parse(value) as unknown;
    if (!Array.isArray(parsed)) {
      return [];
    }

    return parsed.map((raw, index) => {
      const record = typeof raw === "object" && raw !== null ? raw as Record<string, unknown> : {};
      return {
        fraction: stringifyNumber(record.fraction, "0"),
        targetR: stringifyNumber(record.r ?? record.target_r ?? record.targetR, String(index + 1))
      };
    });
  } catch {
    return [];
  }
}

function formatTakeProfitLadder(rows: TakeProfitRung[]): string {
  if (rows.length === 0) {
    return "";
  }

  return JSON.stringify(
    rows.map((row) => ({
      fraction: Number(row.fraction),
      r: Number(row.targetR)
    }))
  );
}

function stringifyNumber(value: unknown, fallback: string): string {
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }

  if (typeof value === "string" && value.length > 0) {
    return value;
  }

  return fallback;
}

function formatLadderNumber(value: number): string {
  const rounded = Math.round(value * 1000) / 1000;
  return Number.isInteger(rounded) ? String(rounded) : String(rounded);
}
