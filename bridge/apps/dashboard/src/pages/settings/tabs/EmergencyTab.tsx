import type { ReactElement } from "react";
import type { OrderSettingScalar, OrderSettingsScope } from "../../../utils/api";
import { emergencySettingsFields } from "../orderSettingsDescriptor";
import type { OrderSettingsCategoryKey } from "../orderSettingsDescriptor";
import { SettingsField } from "./SettingsField";
import type { SettingsFieldState } from "./SettingsField";

type EmergencyTabProps = {
  fieldState: (category: OrderSettingsCategoryKey, key: string) => SettingsFieldState;
  readonly: boolean;
  scope: OrderSettingsScope;
  onClear: (category: OrderSettingsCategoryKey, key: string) => void;
  onFieldChange: (category: OrderSettingsCategoryKey, key: string, value: OrderSettingScalar) => void;
};

export function EmergencyTab({
  fieldState,
  readonly,
  scope,
  onClear,
  onFieldChange
}: EmergencyTabProps): ReactElement {
  return (
    <div className="settings-fields" role="tabpanel" aria-label="Emergency settings">
      {emergencySettingsFields.map((field) => {
        const state = fieldState("emergency", field.key);
        return (
          <SettingsField
            category="emergency"
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
  );
}
