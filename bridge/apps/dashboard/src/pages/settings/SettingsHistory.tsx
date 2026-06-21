import { History, RotateCcw } from "lucide-react";
import type { ReactElement } from "react";
import { useEffect, useState } from "react";
import { fetchSettingsVersions } from "../../utils/api";
import type { OrderSettingsScopeParams, SettingsVersionRecord, SettingsVersionsResult } from "../../utils/api";

type SettingsHistoryProps = {
  readonly: boolean;
  refreshKey: number;
  scope: OrderSettingsScopeParams;
  onRollback: (version: SettingsVersionRecord) => void;
};

export function SettingsHistory({ readonly, refreshKey, scope, onRollback }: SettingsHistoryProps): ReactElement {
  const [loading, setLoading] = useState(true);
  const [history, setHistory] = useState<SettingsVersionsResult>({
    dataSource: {
      data_source: "control_plane",
      degraded: true,
      generated_at: "",
      last_execution_event_at: null,
      missing_nodes: [],
      projection_lag_ms: 0,
      raw: {},
      reason: "loading",
      reconciliation_state: "degraded",
      snapshot_id: "",
      source: "control_plane",
      stale: true,
      status: "degraded"
    },
    raw: {},
    versions: []
  });

  useEffect(() => {
    let active = true;
    setLoading(true);

    fetchSettingsVersions(scope).then((nextHistory) => {
      if (!active) {
        return;
      }

      setHistory(nextHistory);
      setLoading(false);
    });

    return () => {
      active = false;
    };
  }, [refreshKey, scope.scope, scope.scopeKey]);

  const driftRows = history.versions.filter((version) => (
    version.desiredVersion !== null &&
    version.effectiveVersion !== null &&
    version.desiredVersion !== version.effectiveVersion
  ));

  return (
    <section
      className="panel settings-history"
      data-testid={loading ? undefined : "settings-history"}
      aria-label="Settings history"
    >
      <header className="panel-header">
        <div>
          <History size={17} />
          <h3>Settings history</h3>
        </div>
        <span className={history.dataSource.degraded ? "status-pill warning" : "status-pill muted"}>
          {history.dataSource.status}
        </span>
      </header>

      {driftRows.length > 0 && (
        <section className="settings-drift-warning" role="alert" aria-label="Desired settings not fully applied">
          <strong>Desired version has not reached every node.</strong>
          <ul>
            {driftRows.map((row) => (
              <li key={`${row.nodeId}-${row.version}`}>
                {row.nodeId || "node"} desired {row.desiredVersion} effective {row.effectiveVersion}
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="settings-history-list">
        {history.versions.map((version) => (
          <article className="settings-history-row" key={`${version.version}-${version.timestamp}`}>
            <div className="settings-history-main">
              <div>
                <strong>Version {version.version ?? "unversioned"}</strong>
                <span>{version.timestamp || "timestamp unavailable"}</span>
              </div>
              <dl className="settings-history-meta">
                <div>
                  <dt>author</dt>
                  <dd>{version.author}</dd>
                </div>
                <div>
                  <dt>reason</dt>
                  <dd>{version.reason}</dd>
                </div>
              </dl>
              {version.diff.length > 0 && (
                <ul className="settings-history-diff">
                  {version.diff.map((entry) => (
                    <li key={`${entry.category}.${entry.key}.${entry.before}.${entry.after}`}>
                      <strong>{entry.category ? `${entry.category}.${entry.key}` : entry.key || entry.label}</strong>
                      <span>{entry.before || "empty"} -&gt; {entry.after || "empty"}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            {!readonly && Object.keys(version.settings).length > 0 && (
              <button
                aria-label={`Rollback to version ${version.version ?? "unversioned"}`}
                className="secondary-button"
                onClick={() => {
                  onRollback(version);
                }}
                type="button"
              >
                <RotateCcw size={16} />
                Rollback
              </button>
            )}
          </article>
        ))}

        {loading && <p className="empty-state">Loading settings history.</p>}
        {!loading && history.versions.length === 0 && (
          <p className="empty-state">No settings versions returned for this scope.</p>
        )}
      </div>
    </section>
  );
}
