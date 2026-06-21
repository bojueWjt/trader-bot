import { AlertTriangle } from "lucide-react";
import type { ReactElement } from "react";
import type { OrderSettingScalar, OrderSettingsScope } from "../../../utils/api";
import { advancedSettingsFields } from "../orderSettingsDescriptor";
import type { OrderSettingsCategoryKey } from "../orderSettingsDescriptor";
import { SettingsField } from "./SettingsField";
import type { SettingsFieldState } from "./SettingsField";

type AdvancedTabProps = {
  fieldState: (category: OrderSettingsCategoryKey, key: string) => SettingsFieldState;
  readonly: boolean;
  scope: OrderSettingsScope;
  onClear: (category: OrderSettingsCategoryKey, key: string) => void;
  onFieldChange: (category: OrderSettingsCategoryKey, key: string, value: OrderSettingScalar) => void;
};

export function AdvancedTab({
  fieldState,
  readonly,
  scope,
  onClear,
  onFieldChange
}: AdvancedTabProps): ReactElement {
  return (
    <div className="settings-fields" role="tabpanel" aria-label="Advanced settings">
      {advancedSettingsFields.map((field) => {
        const state = fieldState("advanced", field.key);
        return (
          <SettingsField
            category="advanced"
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
          >
            <span className="advanced-risk-flag">
              <AlertTriangle size={13} aria-hidden="true" />
              HIGH RISK
            </span>
          </SettingsField>
        );
      })}
    </div>
  );
}
