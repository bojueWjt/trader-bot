import { AlertTriangle, ShieldAlert, X } from "lucide-react";
import type { ReactElement } from "react";
import { useEffect, useRef, useState } from "react";
import type { SaveReviewChange } from "./SaveReviewDialog";

type DangerConfirmDialogProps = {
  changes: SaveReviewChange[];
  reason: string;
  onCancel: () => void;
  onConfirm: (operatorSignoff: string) => void;
};

export function DangerConfirmDialog({
  changes,
  reason,
  onCancel,
  onConfirm
}: DangerConfirmDialogProps): ReactElement {
  const [confirmed, setConfirmed] = useState(false);
  const [operatorSignoff, setOperatorSignoff] = useState("");
  const titleRef = useRef<HTMLHeadingElement>(null);
  const canConfirm = confirmed && operatorSignoff.trim().length > 0;

  useEffect(() => {
    titleRef.current?.focus();
  }, []);

  return (
    <div className="modal-backdrop" role="presentation" data-testid="danger-confirm-backdrop">
      <section
        aria-describedby="danger-confirm-copy"
        aria-labelledby="danger-confirm-title"
        aria-modal="true"
        className="modal danger-confirm-dialog"
        role="dialog"
        data-testid="danger-confirm-dialog"
      >
        <header>
          <div>
            <p className="eyebrow">Dangerous Change</p>
            <h3 id="danger-confirm-title" ref={titleRef} tabIndex={-1}>Dangerous settings confirmation</h3>
          </div>
          <button aria-label="Cancel dangerous settings confirmation" className="icon-button" onClick={onCancel} title="Cancel dangerous settings confirmation" type="button">
            <X size={16} />
          </button>
        </header>

        <p className="modal-copy" id="danger-confirm-copy">
          This live-risk settings change relaxes active risk limits. The UI collects operator confirmation and signoff for the audit trail.
        </p>

        <section className="audit-panel danger-audit-panel" aria-label="Why this is required">
          <h4>
            <ShieldAlert size={14} aria-hidden="true" />
            Why this is required
          </h4>
          <p>Backend validation and audit policy remain authoritative. The server can still reject this request even after this confirmation.</p>
          <dl className="audit-grid">
            <div>
              <dt>Reason</dt>
              <dd>{reason}</dd>
            </div>
            <div>
              <dt>Changed limits</dt>
              <dd>{changes.length}</dd>
            </div>
          </dl>
          {changes.length > 0 && (
            <ul className="settings-history-diff">
              {changes.map((change) => (
                <li key={`${change.category}.${change.key}`}>
                  <strong>{change.label}</strong>
                  <span>{change.before} -&gt; {change.after}</span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <label className="danger-confirm-check">
          <input
            checked={confirmed}
            data-testid="danger-confirm-checkbox"
            onChange={(event) => {
              setConfirmed(event.target.checked);
            }}
            type="checkbox"
          />
          <span>I confirm this dangerous live-risk settings change</span>
        </label>

        <label className="dialog-field">
          <span>Operator signoff</span>
          <input
            aria-label="Operator signoff"
            data-testid="danger-confirm-signoff"
            onChange={(event) => {
              setOperatorSignoff(event.target.value);
            }}
            placeholder="operator name or ticket"
            value={operatorSignoff}
          />
        </label>

        <div className="modal-actions">
          <button aria-label="Cancel dangerous settings save" className="secondary-button" onClick={onCancel} title="Cancel dangerous settings save" type="button">
            <X size={16} />
            Cancel
          </button>
          <button
            aria-label="Confirm dangerous settings save"
            className="danger-button"
            data-testid="danger-confirm-submit"
            disabled={!canConfirm}
            onClick={() => {
              onConfirm(operatorSignoff.trim());
            }}
            title="Confirm dangerous settings save"
            type="button"
          >
            <AlertTriangle size={16} />
            Confirm save
          </button>
        </div>
      </section>
    </div>
  );
}
