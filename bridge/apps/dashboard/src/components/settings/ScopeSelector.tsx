import type { FormEvent, ReactElement } from "react";
import { useEffect, useState } from "react";
import type { OrderSettingsScope } from "../../utils/api";

export type SettingsScopeSelection = {
  scope: OrderSettingsScope;
  scopeKey: string;
};

type ScopeSelectorProps = {
  disabled?: boolean;
  value: SettingsScopeSelection;
  onChange: (nextScope: SettingsScopeSelection) => void;
};

export function ScopeSelector({ disabled = false, value, onChange }: ScopeSelectorProps): ReactElement {
  const [draftScope, setDraftScope] = useState<OrderSettingsScope>(value.scope);
  const [draftKey, setDraftKey] = useState(value.scopeKey);

  useEffect(() => {
    setDraftScope(value.scope);
    setDraftKey(value.scopeKey);
  }, [value.scope, value.scopeKey]);

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    onChange({
      scope: draftScope,
      scopeKey: draftScope === "global" ? "" : draftKey.trim()
    });
  }

  return (
    <form className="scope-selector" onSubmit={submit}>
      <div className="scope-buttons" role="group" aria-label="Settings scope">
        {(["global", "account", "instrument"] as OrderSettingsScope[]).map((scope) => (
          <button
            aria-pressed={draftScope === scope}
            className={draftScope === scope ? "secondary-button active" : "secondary-button"}
            disabled={disabled}
            key={scope}
            onClick={() => {
              setDraftScope(scope);
              if (scope === "global") {
                setDraftKey("");
                onChange({ scope: "global", scopeKey: "" });
              }
            }}
            type="button"
          >
            {scopeLabel(scope)}
          </button>
        ))}
      </div>
      {draftScope !== "global" && (
        <label className="scope-key-field">
          <span>{draftScope === "account" ? "Account ID" : "Instrument ID"}</span>
          <input
            disabled={disabled}
            onChange={(event) => {
              setDraftKey(event.target.value);
            }}
            required
            type="text"
            value={draftKey}
          />
        </label>
      )}
      <button className="primary-button" disabled={disabled || (draftScope !== "global" && !draftKey.trim())} type="submit">
        Apply scope
      </button>
    </form>
  );
}

function scopeLabel(scope: OrderSettingsScope): string {
  if (scope === "account") {
    return "Account";
  }

  if (scope === "instrument") {
    return "Instrument";
  }

  return "Global";
}
