import type { ReactElement, ReactNode } from "react";
import type { EffectiveOrderSetting, OrderSettingScalar, OrderSettingsScope } from "../../../utils/api";
import type { OrderSettingsCategoryKey, SettingsFieldDescriptor } from "../orderSettingsDescriptor";

type SettingsFieldProps = {
  category: OrderSettingsCategoryKey;
  children?: ReactNode;
  disabledReason: string;
  effective: EffectiveOrderSetting;
  field: SettingsFieldDescriptor;
  isOverridden: boolean;
  readonly: boolean;
  scope: OrderSettingsScope;
  value: OrderSettingScalar | "";
  onChange: (category: OrderSettingsCategoryKey, key: string, value: OrderSettingScalar) => void;
  onClear: (category: OrderSettingsCategoryKey, key: string) => void;
};

export type SettingsFieldState = {
  effective: EffectiveOrderSetting;
  isOverridden: boolean;
  value: OrderSettingScalar | "";
};

export function SettingsField({
  category,
  children,
  disabledReason,
  effective,
  field,
  isOverridden,
  readonly,
  scope,
  value,
  onChange,
  onClear
}: SettingsFieldProps): ReactElement | null {
  if (field.secret) {
    return null;
  }

  const inputId = `${category}-${field.key}`;
  const helpId = `${inputId}-help`;
  const metaId = `${inputId}-meta`;
  const scopeLocked = scope !== "global" && !field.scopeOverridable;
  const controlDisabled = readonly || scopeLocked || Boolean(disabledReason);
  const sourceLabel = effective.inherited
    ? `Inherited from ${effective.sourceScope || "default"}`
    : `Overridden at ${effective.sourceScope || scope}`;

  return (
    <section className="settings-field" role="group" aria-label={`${field.label} setting`}>
      <div className="settings-field-copy">
        <div className="settings-field-heading">
          <label htmlFor={inputId}>{field.label}</label>
          <span className={effective.inherited ? "status-pill muted" : "status-pill good"}>{sourceLabel}</span>
        </div>
        <p id={helpId}>{field.help}</p>
        <div className="settings-meta" id={metaId}>
          <span>Unit {field.unit}</span>
          {field.min !== undefined && field.max !== undefined && <span>Range {field.min}-{field.max}</span>}
          {field.enumValues && <span>Values {field.enumValues.join(", ")}</span>}
          <span>Default {formatSettingValue(field.defaultValue)}</span>
          <span>{field.applyMode}</span>
          {scopeLocked && <span>Global scope only</span>}
          {disabledReason && <span>{disabledReason}</span>}
        </div>
      </div>
      <div className="settings-control">
        {renderControl()}
        {children && <div className="settings-control-extra">{children}</div>}
      </div>
      <dl className="settings-effective" aria-label={`${field.label} effective value`}>
        <div>
          <dt>Effective value</dt>
          <dd>{formatSettingValue(effective.value)}</dd>
        </div>
        <div>
          <dt>source_scope</dt>
          <dd>{effective.sourceScope || "default"}</dd>
        </div>
        <div>
          <dt>inherited</dt>
          <dd>{String(effective.inherited)}</dd>
        </div>
      </dl>
      <button
        aria-label={`Clear ${field.label} override`}
        className="secondary-button"
        disabled={readonly || !isOverridden}
        onClick={() => {
          onClear(category, field.key);
        }}
        type="button"
      >
        Clear override
      </button>
    </section>
  );

  function renderControl(): ReactElement {
    if (field.type === "boolean") {
      return (
        <input
          aria-describedby={`${helpId} ${metaId}`}
          checked={value === true}
          disabled={controlDisabled}
          id={inputId}
          onChange={(event) => {
            onChange(category, field.key, event.target.checked);
          }}
          type="checkbox"
        />
      );
    }

    if (field.type === "enum") {
      return (
        <select
          aria-describedby={`${helpId} ${metaId}`}
          disabled={controlDisabled}
          id={inputId}
          onChange={(event) => {
            onChange(category, field.key, event.target.value);
          }}
          value={String(value)}
        >
          {(field.enumValues || []).map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      );
    }

    if (field.type === "integer" || field.type === "number") {
      return (
        <input
          aria-describedby={`${helpId} ${metaId}`}
          disabled={controlDisabled}
          id={inputId}
          max={field.max}
          min={field.min}
          onChange={(event) => {
            const nextValue = event.target.value;
            onChange(category, field.key, nextValue === "" ? "" : Number(nextValue));
          }}
          step={field.type === "integer" ? 1 : "any"}
          type="number"
          value={String(value)}
        />
      );
    }

    return (
      <input
        aria-describedby={`${helpId} ${metaId}`}
        disabled={controlDisabled}
        id={inputId}
        onChange={(event) => {
          onChange(category, field.key, event.target.value);
        }}
        type="text"
        value={String(value)}
      />
    );
  }
}

export function formatSettingValue(value: OrderSettingScalar | ""): string {
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }

  if (value === "") {
    return "empty";
  }

  return String(value);
}
