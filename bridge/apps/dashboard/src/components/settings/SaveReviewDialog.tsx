import { AlertTriangle, CheckCircle2, X } from "lucide-react";
import type { ReactElement } from "react";
import { useEffect, useRef } from "react";
import type { OrderSettingsValidationResult, SettingsDiffEntry } from "../../utils/api";

export type SaveReviewChange = {
  after: string;
  before: string;
  category: string;
  effective: string;
  key: string;
  label: string;
};

type SaveReviewDialogProps = {
  changes: SaveReviewChange[];
  dangerous: boolean;
  expectedVersion: number | null;
  reason: string;
  titleDetail: string;
  validation: OrderSettingsValidationResult;
  onCancel: () => void;
  onConfirm: () => void;
};

export function SaveReviewDialog({
  changes,
  dangerous,
  expectedVersion,
  reason,
  titleDetail,
  validation,
  onCancel,
  onConfirm
}: SaveReviewDialogProps): ReactElement {
  const titleRef = useRef<HTMLHeadingElement>(null);
  const confirmLabel = dangerous ? "Continue to dangerous confirmation" : "Confirm settings save";

  useEffect(() => {
    titleRef.current?.focus();
  }, []);

  return (
    <div className="modal-backdrop" role="presentation" data-testid="save-review-backdrop">
      <section
        aria-describedby="save-review-copy"
        aria-labelledby="save-review-title"
        aria-modal="true"
        className="modal save-review-dialog"
        role="dialog"
        data-testid="save-review-dialog"
      >
        <header>
          <div>
            <p className="eyebrow">Settings Review</p>
            <h3 id="save-review-title" ref={titleRef} tabIndex={-1}>Review settings save</h3>
          </div>
          <button aria-label="Cancel settings save review" className="icon-button" onClick={onCancel} title="Cancel settings save review" type="button">
            <X size={16} />
          </button>
        </header>

        <div className="save-review-summary" id="save-review-copy">
          <span className="status-pill muted">Expected version {expectedVersion ?? "unversioned"}</span>
          {titleDetail && <span className="status-pill warning">{titleDetail}</span>}
          {validation.valid ? (
            <span className="status-pill good">
              <CheckCircle2 size={13} aria-hidden="true" />
              Server validation passed
            </span>
          ) : (
            <span className="status-pill danger">
              <AlertTriangle size={13} aria-hidden="true" />
              Server validation failed
            </span>
          )}
        </div>

        <section className="audit-panel" aria-label="Audit reason">
          <h4>Reason</h4>
          <p>{reason}</p>
        </section>

        {validation.errors.length > 0 && (
          <section className="validation-summary compact-summary" role="alert" aria-label="Server validation errors">
            <h4>Server validation errors</h4>
            <ul>
              {validation.errors.map((error) => <li key={error}>{error}</li>)}
            </ul>
          </section>
        )}

        <section className="review-diff-section" aria-label="Before after effective diff">
          <h4>Before / After / Effective</h4>
          {changes.length === 0 ? (
            <p className="empty-state compact">No local setting changes detected.</p>
          ) : (
            <div className="review-diff-table-wrap">
              <table className="review-diff-table">
                <thead>
                  <tr>
                    <th>Setting</th>
                    <th>Before</th>
                    <th>After</th>
                    <th>Effective</th>
                  </tr>
                </thead>
                <tbody>
                  {changes.map((change) => (
                    <tr key={`${change.category}.${change.key}`}>
                      <td>{change.label}</td>
                      <td>{change.before}</td>
                      <td>{change.after}</td>
                      <td>{change.effective || change.after}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <ServerDiff entries={validation.diff} />
        <ImpactList impact={validation.impact} />

        <div className="modal-actions">
          <button aria-label="Cancel settings save" className="secondary-button" onClick={onCancel} title="Cancel settings save" type="button">
            <X size={16} />
            Cancel
          </button>
          <button
            aria-label={confirmLabel}
            className={dangerous ? "danger-button" : "primary-button"}
            data-testid={dangerous ? "save-review-continue-danger" : "save-review-confirm"}
            disabled={!validation.valid}
            onClick={onConfirm}
            title={confirmLabel}
            type="button"
          >
            {confirmLabel}
          </button>
        </div>
      </section>
    </div>
  );
}

function ServerDiff({ entries }: { entries: SettingsDiffEntry[] }): ReactElement | null {
  if (entries.length === 0) {
    return null;
  }

  return (
    <section className="review-diff-section" aria-label="Server diff">
      <h4>Server diff</h4>
      <ul className="settings-history-diff">
        {entries.map((entry) => (
          <li key={`${entry.category}.${entry.key}.${entry.before}.${entry.after}`}>
            <strong>{entry.label}</strong>
            <span>{entry.before || "empty"} -&gt; {entry.after || "empty"}</span>
            {entry.effective && <span>effective {entry.effective}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}

function ImpactList({ impact }: { impact: string[] }): ReactElement | null {
  if (impact.length === 0) {
    return null;
  }

  return (
    <section className="review-diff-section" aria-label="Save impact">
      <h4>Impact</h4>
      <ul className="settings-history-diff">
        {impact.map((item) => <li key={item}>{item}</li>)}
      </ul>
    </section>
  );
}
