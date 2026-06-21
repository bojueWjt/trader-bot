import type { ReactElement } from "react";
import type { OrderSettingScalar, OrderSettingsScope } from "../../../utils/api";
import { notificationsSettingsFields } from "../orderSettingsDescriptor";
import type { OrderSettingsCategoryKey } from "../orderSettingsDescriptor";
import { SettingsField } from "./SettingsField";
import type { SettingsFieldState } from "./SettingsField";

type NotificationsTabProps = {
  fieldState: (category: OrderSettingsCategoryKey, key: string) => SettingsFieldState;
  readonly: boolean;
  scope: OrderSettingsScope;
  onClear: (category: OrderSettingsCategoryKey, key: string) => void;
  onFieldChange: (category: OrderSettingsCategoryKey, key: string, value: OrderSettingScalar) => void;
};

export function NotificationsTab({
  fieldState,
  readonly,
  scope,
  onClear,
  onFieldChange
}: NotificationsTabProps): ReactElement {
  return (
    <div className="settings-fields" role="tabpanel" aria-label="Notifications settings" data-testid="settings-tabpanel-notifications">
      {notificationsSettingsFields.map((field) => {
        const state = fieldState("notifications", field.key);
        return (
          <SettingsField
            category="notifications"
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
