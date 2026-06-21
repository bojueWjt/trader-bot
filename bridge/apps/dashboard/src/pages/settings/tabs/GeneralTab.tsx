import type { ReactElement } from "react";
import type { OrderSettingScalar, OrderSettingsScope } from "../../../utils/api";
import { generalSettingsFields } from "../orderSettingsDescriptor";
import type { OrderSettingsCategoryKey } from "../orderSettingsDescriptor";
import { SettingsField } from "./SettingsField";
import type { SettingsFieldState } from "./SettingsField";

type GeneralTabProps = {
  fieldState: (category: OrderSettingsCategoryKey, key: string) => SettingsFieldState;
  readonly: boolean;
  scope: OrderSettingsScope;
  onClear: (category: OrderSettingsCategoryKey, key: string) => void;
  onFieldChange: (category: OrderSettingsCategoryKey, key: string, value: OrderSettingScalar) => void;
};

export function GeneralTab({ fieldState, readonly, scope, onClear, onFieldChange }: GeneralTabProps): ReactElement {
  return (
    <div className="settings-fields" role="tabpanel" aria-label="General settings">
      {generalSettingsFields.map((field) => {
        const state = fieldState("general", field.key);
        return (
          <SettingsField
            category="general"
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
