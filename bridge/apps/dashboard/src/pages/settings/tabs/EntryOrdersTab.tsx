import type { ReactElement } from "react";
import type { OrderSettingScalar, OrderSettingsScope } from "../../../utils/api";
import { entrySettingsFields } from "../orderSettingsDescriptor";
import type { OrderSettingsCategoryKey, SettingsFieldDescriptor } from "../orderSettingsDescriptor";
import { SettingsField } from "./SettingsField";
import type { SettingsFieldState } from "./SettingsField";

type EntryOrdersTabProps = {
  fieldState: (category: OrderSettingsCategoryKey, key: string) => SettingsFieldState;
  readonly: boolean;
  scope: OrderSettingsScope;
  onClear: (category: OrderSettingsCategoryKey, key: string) => void;
  onFieldChange: (category: OrderSettingsCategoryKey, key: string, value: OrderSettingScalar) => void;
};

export function EntryOrdersTab({
  fieldState,
  readonly,
  scope,
  onClear,
  onFieldChange
}: EntryOrdersTabProps): ReactElement {
  const orderType = String(fieldState("entry", "default_order_type").value);
  const partialFillPolicy = String(fieldState("entry", "partial_fill_policy").value);

  return (
    <div className="settings-fields" role="tabpanel" aria-label="Entry Orders settings">
      {entrySettingsFields.map((field) => {
        const state = fieldState("entry", field.key);
        return (
          <SettingsField
            category="entry"
            disabledReason={disabledReason(field, orderType, partialFillPolicy)}
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

function disabledReason(field: SettingsFieldDescriptor, orderType: string, partialFillPolicy: string): string {
  if (field.key === "limit_offset_bps" && orderType !== "limit") {
    return "Requires limit order type";
  }

  if (field.key === "post_only" && orderType === "market") {
    return "Unavailable for market orders";
  }

  if (
    (field.key === "reprice_interval_seconds" || field.key === "max_reprices") &&
    orderType === "market"
  ) {
    return "Requires resting order type";
  }

  if (field.key === "market_conversion_max_slippage_bps" && partialFillPolicy !== "convert_remainder_to_market") {
    return "Requires market conversion policy";
  }

  return "";
}
