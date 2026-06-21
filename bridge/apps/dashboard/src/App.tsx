import {
  Activity,
  AlertTriangle,
  Bot,
  CheckCircle2,
  ChevronRight,
  ClipboardList,
  Download,
  FileText,
  History,
  Inbox,
  Lock,
  LogOut,
  Radio,
  RefreshCcw,
  Send,
  Settings,
  Sigma,
  ShieldAlert,
  ShieldCheck,
  Siren,
  X,
  XCircle
} from "lucide-react";
import type { ReactElement, ReactNode } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useRealtimeConnection } from "./hooks/useRealtime";
import { LoginPage } from "./pages/LoginPage";
import { OrderSettingsPage } from "./pages/settings/OrderSettingsPage";
import {
  activateKillSwitch,
  approveReviewProposal,
  approveReviewSignal,
  closePosition,
  createDailyReportSnapshot,
  clearAuthToken,
  fetchDailyReport,
  fetchDailyReportMarkdown,
  fetchDailyReportTelegramPreview,
  fetchDailyReportVersions,
  fetchDashboardOverview,
  fetchOrderCenter,
  fetchRiskOverview,
  fetchSignalMediaBlob,
  fetchSignalReview,
  getEmptyDashboardOverview,
  getEmptyDailyReport,
  getEmptyOrderCenter,
  getEmptyRiskOverview,
  getEmptySignalReview,
  getStoredAuthToken,
  getStoredAuthRole,
  isAuthDisabled,
  lockPair,
  moveStopLoss,
  OrderCenterData,
  OrderCenterOrder,
  OrderCenterPosition,
  OrderCenterTrade,
  partialClosePosition,
  pauseBot,
  rejectReviewProposal,
  rejectReviewSignal,
  resumeBot
} from "./utils/api";
import type {
  BotStatus,
  CommandResult,
  DailyReport,
  DashboardOverview,
  DataSourceState,
  EventLog,
  Position,
  RiskMetric,
  RiskOverview,
  SignalReviewData,
  SignalReviewItem,
  SignalReviewProposal
} from "./utils/api";
import {
  formatCurrency,
  formatPercent,
  getReportDateFromPath,
  normalizePath,
  statusTone
} from "./utils/format";
import "./styles.css";

type AppProps = {
  initialPath?: string;
};

type LoadState<T> = {
  data: T;
  loading: boolean;
};

function useDashboardData(refreshKey: number): LoadState<DashboardOverview> {
  const [data, setData] = useState<DashboardOverview>(getEmptyDashboardOverview());
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;

    setLoading(true);
    fetchDashboardOverview().then((overview) => {
      if (!active) {
        return;
      }

      setData(overview);
      setLoading(false);
    });

    return () => {
      active = false;
    };
  }, [refreshKey]);

  return { data, loading };
}

function useRiskData(): LoadState<RiskOverview> {
  const [data, setData] = useState<RiskOverview>(getEmptyRiskOverview());
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;

    fetchRiskOverview().then((overview) => {
      if (!active) {
        return;
      }

      setData(overview);
      setLoading(false);
    });

    return () => {
      active = false;
    };
  }, []);

  return { data, loading };
}

function useReportData(date: string): LoadState<DailyReport> {
  const [data, setData] = useState<DailyReport>(getEmptyDailyReport(date));
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;

    setLoading(true);
    fetchDailyReport(date).then((report) => {
      if (!active) {
        return;
      }

      setData(report);
      setLoading(false);
    });

    return () => {
      active = false;
    };
  }, [date]);

  return { data, loading };
}

function useOrderCenterData(refreshKey: number): LoadState<OrderCenterData> {
  const [data, setData] = useState<OrderCenterData>(getEmptyOrderCenter());
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;

    setLoading(true);
    fetchOrderCenter().then((orders) => {
      if (!active) {
        return;
      }

      setData(orders);
      setLoading(false);
    });

    return () => {
      active = false;
    };
  }, [refreshKey]);

  return { data, loading };
}

function useSignalReviewData(refreshKey: number): LoadState<SignalReviewData> {
  const [data, setData] = useState<SignalReviewData>(getEmptySignalReview());
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;

    setLoading(true);
    fetchSignalReview().then((review) => {
      if (!active) {
        return;
      }

      setData(review);
      setLoading(false);
    });

    return () => {
      active = false;
    };
  }, [refreshKey]);

  return { data, loading };
}

export function App({ initialPath }: AppProps): ReactElement {
  const authGateEnabled = !initialPath && !isAuthDisabled();
  const [authToken, setAuthToken] = useState(() => {
    if (!authGateEnabled) {
      return "";
    }

    return getStoredAuthToken();
  });
  const [dashboardRefreshKey, setDashboardRefreshKey] = useState(0);
  const [path, setPath] = useState(() => {
    if (initialPath) {
      return normalizePath(initialPath);
    }

    if (authGateEnabled && !getStoredAuthToken()) {
      return "/login";
    }

    return normalizePath(window.location.pathname);
  });

  useEffect(() => {
    if (initialPath) {
      return;
    }

    function handlePopState(): void {
      setPath(normalizePath(window.location.pathname));
    }

    window.addEventListener("popstate", handlePopState);

    return () => {
      window.removeEventListener("popstate", handlePopState);
    };
  }, [initialPath]);

  useEffect(() => {
    if (!authGateEnabled) {
      return;
    }

    if (!authToken) {
      if (path !== "/login") {
        setPath("/login");
      }
      if (window.location.pathname !== "/login") {
        window.history.replaceState({}, "", "/login");
      }
      return;
    }

    if (authToken && path === "/login") {
      setPath("/dashboard");
      window.history.replaceState({}, "", "/dashboard");
    }
  }, [authGateEnabled, authToken, path]);

  function navigate(nextPath: string): void {
    const normalized = normalizePath(nextPath);
    setPath(normalized);

    if (initialPath) {
      return;
    }

    window.history.pushState({}, "", normalized);
  }

  function completeLogin(): void {
    const token = getStoredAuthToken();
    setAuthToken(token);
    navigate("/dashboard");
  }

  function logout(): void {
    clearAuthToken();
    setAuthToken("");
    navigate("/login");
  }

  const resyncDashboard = useCallback(() => {
    setDashboardRefreshKey((current) => current + 1);
  }, []);
  const realtime = useRealtimeConnection(resyncDashboard, !(authGateEnabled && !authToken));
  const reportDate = getReportDateFromPath(path);

  if (authGateEnabled && !authToken) {
    return <LoginPage onAuthenticated={completeLogin} />;
  }

  return (
    <div className="app-shell">
      <Sidebar currentPath={path} onNavigate={navigate} />
      <main className="main-panel">
        <TopBar
          realtimeLabel={realtime.label}
          realtimeConnected={realtime.connected}
          showLogout={authGateEnabled}
          onLogout={logout}
        />
        {path === "/risk" && <RiskPage />}
        {path === "/orders" && <OrderCenterPage refreshKey={dashboardRefreshKey} onRefresh={resyncDashboard} />}
        {path === "/review" && <SignalReviewPage refreshKey={dashboardRefreshKey} onRefresh={resyncDashboard} />}
        {(path === "/settings" || path === "/settings/orders") && <OrderSettingsPage role={getStoredAuthRole()} />}
        {path === "/reports" && <ReportsPage onNavigate={navigate} />}
        {/^\/reports\/daily\/\d{4}-\d{2}-\d{2}$/.test(path) && <DailyReportPage date={reportDate} />}
        {path === "/dashboard" && <DashboardPage refreshKey={dashboardRefreshKey} />}
      </main>
    </div>
  );
}

type SidebarProps = {
  currentPath: string;
  onNavigate: (path: string) => void;
};

function Sidebar({ currentPath, onNavigate }: SidebarProps): ReactElement {
  const today = new Date().toISOString().slice(0, 10);
  const items = [
    { path: "/dashboard", label: "Dashboard", icon: Activity },
    { path: "/orders", label: "Orders", icon: ClipboardList },
    { path: "/review", label: "Review", icon: Inbox },
    { path: "/risk", label: "Risk", icon: ShieldAlert },
    { path: "/reports", label: "Reports", icon: FileText },
    { path: "/settings/orders", label: "Settings", icon: Settings },
    { path: `/reports/daily/${today}`, label: "Daily", icon: FileText }
  ];

  return (
    <aside className="sidebar" aria-label="Primary">
      <div className="brand">
        <span className="brand-mark">HT</span>
        <div>
          <strong>Hermes Trader</strong>
          <span>Operations</span>
        </div>
      </div>
      <nav className="nav-list">
        {items.map((item) => {
          const Icon = item.icon;
          let active = currentPath === item.path || currentPath.startsWith(item.path);
          if (item.path === "/reports") {
            active = currentPath === "/reports";
          }
          if (item.path === "/settings/orders") {
            active = currentPath === "/settings" || currentPath === "/settings/orders";
          }

          return (
            <button
              aria-label={`Open ${item.label}`}
              className={active ? "nav-item active" : "nav-item"}
              key={item.path}
              onClick={() => {
                onNavigate(item.path);
              }}
              title={`Open ${item.label}`}
              type="button"
            >
              <Icon size={17} />
              <span>{item.label}</span>
            </button>
          );
        })}
        <a
          aria-label="Open Telegram watcher console"
          className="nav-item"
          href="/watcher/"
          rel="noreferrer"
          target="_blank"
          title="Open Telegram watcher console"
        >
          <Radio size={17} />
          <span>Watcher</span>
        </a>
      </nav>
    </aside>
  );
}

type TopBarProps = {
  realtimeLabel: string;
  realtimeConnected: boolean;
  showLogout: boolean;
  onLogout: () => void;
};

function TopBar({ realtimeLabel, realtimeConnected, showLogout, onLogout }: TopBarProps): ReactElement {
  return (
    <header className="topbar">
      <div>
        <p className="eyebrow">Crypto Ops Console</p>
        <h1>实时运营看板</h1>
      </div>
      <div className="topbar-actions">
        <div className={realtimeConnected ? "connection good" : "connection danger"} role="status">
          <Radio size={16} />
          <span>{realtimeLabel}</span>
        </div>
        {showLogout && (
          <button aria-label="Log out" className="secondary-button" onClick={onLogout} title="Log out" type="button">
            <LogOut size={16} />
            Log out
          </button>
        )}
      </div>
    </header>
  );
}

type BotAction = "pause" | "resume" | false;

function DashboardPage({ refreshKey }: { refreshKey: number }): ReactElement {
  const { data, loading } = useDashboardData(refreshKey);
  const [selectedPosition, setSelectedPosition] = useState<Position | false>(false);
  const [botAction, setBotAction] = useState<BotAction>(false);
  const [botActionReason, setBotActionReason] = useState("");
  const [botActionSubmitting, setBotActionSubmitting] = useState(false);
  const [botActionStatus, setBotActionStatus] = useState("");
  const sourceBadgeClass = getSourceBadgeClass(data.dataSource, loading);
  const sourceBadgeLabel = getSourceBadgeLabel(data.dataSource, loading);
  const manualActionsDisabled = /live readonly/i.test(data.safety.modeLabel);

  async function submitBotAction(): Promise<void> {
    if (!botAction) {
      return;
    }

    const reason = botActionReason.trim();
    if (!reason) {
      return;
    }

    if (botActionSubmitting) {
      return;
    }

    setBotActionSubmitting(true);
    setBotActionStatus(`${botAction} request pending`);

    let result: CommandResult | false = false;
    if (botAction === "pause") {
      result = await pauseBot(reason);
    }
    if (botAction === "resume") {
      result = await resumeBot(reason);
    }

    setBotActionSubmitting(false);
    setBotAction(false);
    setBotActionReason("");

    if (result) {
      setBotActionStatus(result.statusText);
      return;
    }

    setBotActionStatus(`${botAction} failed`);
  }

  return (
    <section className="page-grid">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Live Overview</p>
          <h2>/dashboard</h2>
        </div>
        <span className={sourceBadgeClass}>{sourceBadgeLabel}</span>
      </div>
      <DataQualityPanel quality={data.dataSource} />

      <section className="ops-strip" aria-label="Operator state">
        <div className={`ops-mode ${statusTone(data.safety.riskState)}`}>
          <ShieldCheck size={18} />
          <div>
            <span>Run mode</span>
            <strong>{data.safety.modeLabel}</strong>
          </div>
        </div>
        <div>
          <span>Data freshness</span>
          <strong>{data.safety.dataFreshness}</strong>
        </div>
        <div>
          <span>Operator lane</span>
          <strong>{data.safety.operatorLane}</strong>
        </div>
        <div>
          <span>Last sync</span>
          <strong>{data.safety.lastSync}</strong>
        </div>
      </section>

      <div className="kpi-grid">
        {data.kpis.map((kpi) => (
          <MetricCard delta={kpi.delta} key={kpi.label} label={kpi.label} tone={kpi.tone} value={kpi.value} />
        ))}
      </div>

      <Panel title="Portfolio exposure" icon={<ShieldAlert size={17} />}>
        <div className="exposure-grid">
          {data.exposure.map((bucket) => (
            <div className={`exposure-row ${statusTone(bucket.status)}`} key={bucket.label}>
              <div>
                <span>{bucket.label}</span>
                <strong>{bucket.value}</strong>
                <small>{bucket.detail}</small>
              </div>
              <div className="bar-track" aria-hidden="true">
                <div className="bar-fill" style={{ width: `${bucket.usagePct}%` }} />
              </div>
            </div>
          ))}
        </div>
      </Panel>

      <div className="two-column">
        <BotStatusPanel
          bots={data.bots}
          disabled={manualActionsDisabled}
          status={botActionStatus}
          onAction={(action) => {
            setBotAction(action);
            setBotActionReason("");
          }}
        />

        <Panel title="风险灯" icon={<ShieldCheck size={17} />}>
          <div className="risk-lamps">
            {data.riskLamps.map((lamp) => (
              <div className={`risk-lamp ${statusTone(lamp.status)}`} key={lamp.label}>
                <span>{lamp.label}</span>
                <strong>{lamp.status}</strong>
                <small>{lamp.detail}</small>
              </div>
            ))}
          </div>
        </Panel>
      </div>

      <div className="two-column wide-left">
        <EventsPanel events={data.events.slice(0, 20)} />
        <PositionsPanel
          positions={data.positions}
          onSelect={(position) => {
            setSelectedPosition(position);
          }}
        />
      </div>

      {selectedPosition && (
        <PositionDrawer
          actionsDisabled={manualActionsDisabled}
          position={selectedPosition}
          onClose={() => {
            setSelectedPosition(false);
          }}
        />
      )}
      {botAction && (
        <BotActionConfirm
          action={botAction}
          reason={botActionReason}
          submitting={botActionSubmitting}
          onCancel={() => {
            setBotAction(false);
          }}
          onReasonChange={setBotActionReason}
          onSubmit={() => {
            void submitBotAction();
          }}
        />
      )}
    </section>
  );
}

function getSourceBadgeClass(dataSource: DataSourceState, loading: boolean): string {
  if (loading) {
    return "load-badge loading";
  }

  if (dataSource.degraded) {
    return "load-badge degraded";
  }

  return "load-badge";
}

function getSourceBadgeLabel(dataSource: DataSourceState, loading: boolean): string {
  if (loading) {
    return "Loading API";
  }

  return `${dataSource.source} / ${dataSource.status}`;
}

type MetricCardProps = {
  label: string;
  value: string;
  delta: string;
  tone: string;
};

function MetricCard({ label, value, delta, tone }: MetricCardProps): ReactElement {
  return (
    <article className={`metric-card ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{delta}</small>
    </article>
  );
}

type PanelProps = {
  title: string;
  icon: ReactElement;
  children: ReactNode;
};

function Panel({ title, icon, children }: PanelProps): ReactElement {
  return (
    <section className="panel">
      <header className="panel-header">
        <div>
          {icon}
          <h3>{title}</h3>
        </div>
      </header>
      {children}
    </section>
  );
}

function StatusPill({ status }: { status: string }): ReactElement {
  return <span className={`status-pill ${statusTone(status)}`}>{status}</span>;
}

function DataQualityPanel({ quality }: { quality: DataSourceState }): ReactElement {
  return (
    <section className={quality.stale ? "quality-panel stale" : "quality-panel"} aria-label="Data quality">
      {quality.stale && (
        <div className="stale-banner" role="alert">
          stale=true; missing_nodes={quality.missing_nodes.length > 0 ? quality.missing_nodes.join(", ") : "none"}
        </div>
      )}
      <dl className="quality-grid">
        <div>
          <dt>data_source</dt>
          <dd>{quality.data_source}</dd>
        </div>
        <div>
          <dt>snapshot_id</dt>
          <dd>{quality.snapshot_id || "unavailable"}</dd>
        </div>
        <div>
          <dt>generated_at</dt>
          <dd>{quality.generated_at || "unavailable"}</dd>
        </div>
        <div>
          <dt>projection_lag_ms</dt>
          <dd>{quality.projection_lag_ms}</dd>
        </div>
        <div>
          <dt>stale</dt>
          <dd>{String(quality.stale)}</dd>
        </div>
        <div>
          <dt>missing_nodes</dt>
          <dd>{quality.missing_nodes.length > 0 ? quality.missing_nodes.join(", ") : "none"}</dd>
        </div>
        <div>
          <dt>reconciliation_state</dt>
          <dd>{quality.reconciliation_state}</dd>
        </div>
      </dl>
      {quality.reason && <p className="quality-reason">{quality.reason}</p>}
    </section>
  );
}

type BotStatusPanelProps = {
  bots: BotStatus[];
  disabled: boolean;
  status: string;
  onAction: (action: Exclude<BotAction, false>) => void;
};

function BotStatusPanel({ bots, disabled, status, onAction }: BotStatusPanelProps): ReactElement {
  return (
    <Panel title="Bot 状态" icon={<Bot size={17} />}>
      <div className="bot-list">
        {bots.map((bot) => (
          <div className="bot-row" key={bot.name}>
            <div>
              <strong>{bot.name}</strong>
              <span>{bot.pairCount} pairs · {bot.openTrades} open</span>
            </div>
            <StatusPill status={bot.status} />
            <small>{bot.lastHeartbeat}</small>
          </div>
        ))}
      </div>
      <div className="button-row compact-actions">
        <button
          aria-label="Pause bot"
          className="secondary-button"
          disabled={disabled}
          onClick={() => {
            onAction("pause");
          }}
          title="Pause bot"
          type="button"
        >
          <Siren size={16} />
          Pause bot
        </button>
        <button
          aria-label="Resume bot"
          className="secondary-button"
          disabled={disabled}
          onClick={() => {
            onAction("resume");
          }}
          title="Resume bot"
          type="button"
        >
          <RefreshCcw size={16} />
          Resume bot
        </button>
      </div>
      {disabled && <p className="drawer-status">Live readonly blocks manual bot actions.</p>}
      {status && <p className="drawer-status">{status}</p>}
    </Panel>
  );
}

function EventsPanel({ events }: { events: EventLog[] }): ReactElement {
  return (
    <Panel title="最近 20 事件" icon={<AlertTriangle size={17} />}>
      <div className="event-list">
        {events.map((event) => (
          <div className={`event-row ${event.severity}`} key={event.id}>
            <span>{event.time}</span>
            <strong>{event.source}</strong>
            <p>{event.message}</p>
          </div>
        ))}
        {events.length === 0 && <p className="empty-copy">No events from control-plane.</p>}
      </div>
    </Panel>
  );
}

type PositionsPanelProps = {
  positions: Position[];
  onSelect: (position: Position) => void;
};

function PositionsPanel({ positions, onSelect }: PositionsPanelProps): ReactElement {
  return (
    <Panel title="持仓表" icon={<Activity size={17} />}>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Pair</th>
              <th>Side</th>
              <th>Entry</th>
              <th>Current</th>
              <th>SL</th>
              <th>TP</th>
              <th>Size</th>
              <th>Leverage</th>
              <th>PnL</th>
              <th>R multiple</th>
              <th>Signal</th>
              <th>Freqtrade Trade ID</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((position) => (
              <tr className={position.anomaly ? "row-alert" : ""} key={position.id}>
                <td>{position.pair}</td>
                <td>{position.side}</td>
                <td>{formatCurrency(position.entry)}</td>
                <td>{formatCurrency(position.mark)}</td>
                <td>{position.stopLoss ? formatCurrency(position.stopLoss) : "Missing"}</td>
                <td>{position.takeProfit ? formatCurrency(position.takeProfit) : "Missing"}</td>
                <td>{position.size}</td>
                <td>{position.leverage}x</td>
                <td className={position.pnl >= 0 ? "num good-text" : "num danger-text"}>
                  {formatCurrency(position.pnl)}
                </td>
                <td>{position.rMultiple}</td>
                <td>{position.signalId}</td>
                <td>{position.freqtradeTradeId}</td>
                <td>
                  <button
                    aria-label={`Open ${position.pair} detail`}
                    className="icon-button"
                    onClick={() => {
                      onSelect(position);
                    }}
                    title={`Open ${position.pair} detail`}
                    type="button"
                  >
                    <ChevronRight size={16} />
                  </button>
                </td>
              </tr>
            ))}
            {positions.length === 0 && <EmptyTableRow colSpan={13} label="No positions from control-plane." />}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

type PositionDrawerProps = {
  actionsDisabled: boolean;
  position: Position;
  onClose: () => void;
};

type PositionAction = "close" | "partial" | "move" | "lock" | false;

function PositionDrawer({ actionsDisabled, position, onClose }: PositionDrawerProps): ReactElement {
  const [closeSubmitting, setCloseSubmitting] = useState(false);
  const [partialSubmitting, setPartialSubmitting] = useState(false);
  const [moveSubmitting, setMoveSubmitting] = useState(false);
  const [lockSubmitting, setLockSubmitting] = useState(false);
  const [confirmAction, setConfirmAction] = useState<PositionAction>(false);
  const [reason, setReason] = useState("");
  const [status, setStatus] = useState("");
  const [stopLossInput, setStopLossInput] = useState(() => {
    if (position.stopLoss) {
      return String(position.stopLoss);
    }

    return "";
  });

  useEffect(() => {
    setConfirmAction(false);
    setReason("");

    if (position.stopLoss) {
      setStopLossInput(String(position.stopLoss));
      setStatus("");
      return;
    }

    setStopLossInput("");
    setStatus("");
  }, [position.id, position.stopLoss]);

  function openAction(nextAction: PositionAction): void {
    setConfirmAction(nextAction);
    setReason("");
  }

  async function submitClosePosition(actionReason: string): Promise<void> {
    if (closeSubmitting) {
      return;
    }

    setCloseSubmitting(true);
    setStatus("Closing position");
    setConfirmAction(false);
    const result = await closePosition(position.id, actionReason, position.signalId);
    setCloseSubmitting(false);
    setStatus(result.statusText);
  }

  async function submitMoveStopLoss(actionReason: string): Promise<void> {
    if (moveSubmitting) {
      return;
    }

    const stopLossPrice = Number(stopLossInput);
    if (!Number.isFinite(stopLossPrice) || stopLossPrice <= 0) {
      setStatus("Enter valid SL");
      return;
    }

    setMoveSubmitting(true);
    setStatus("Moving SL");
    setConfirmAction(false);
    const result = await moveStopLoss(position.id, stopLossPrice, actionReason, position.signalId);
    setMoveSubmitting(false);
    setStatus(result.statusText);
  }

  async function submitPartialClose(actionReason: string): Promise<void> {
    if (partialSubmitting) {
      return;
    }

    const partialAmount = Number((position.size * 0.5).toFixed(8));
    if (!Number.isFinite(partialAmount) || partialAmount <= 0) {
      setStatus("Partial size unavailable");
      return;
    }

    setPartialSubmitting(true);
    setStatus("Partial close pending");
    setConfirmAction(false);
    const result = await partialClosePosition(position.id, partialAmount, actionReason, position.signalId);
    setPartialSubmitting(false);
    setStatus(result.statusText);
  }

  async function submitPairLock(actionReason: string): Promise<void> {
    if (lockSubmitting) {
      return;
    }

    setLockSubmitting(true);
    setStatus("Pair lock pending");
    setConfirmAction(false);
    const result = await lockPair(position.pair, actionReason);
    setLockSubmitting(false);
    setStatus(result.statusText);
  }

  const confirmSubmitting = getPositionActionSubmitting(
    confirmAction,
    closeSubmitting,
    partialSubmitting,
    moveSubmitting,
    lockSubmitting
  );

  return (
    <aside className="drawer" aria-label={`${position.pair} detail`}>
      <div className="drawer-card">
        <header>
          <div>
            <p className="eyebrow">Position Detail</p>
            <h3>{position.pair}</h3>
          </div>
          <button aria-label="Close detail drawer" className="icon-button" onClick={onClose} title="Close detail drawer" type="button">
            <X size={16} />
          </button>
        </header>
        <dl className="detail-grid">
          <div>
            <dt>Side</dt>
            <dd>{position.side}</dd>
          </div>
          <div>
            <dt>Leverage</dt>
            <dd>{position.leverage}x</dd>
          </div>
          <div>
            <dt>Entry</dt>
            <dd>{formatCurrency(position.entry)}</dd>
          </div>
          <div>
            <dt>Mark</dt>
            <dd>{formatCurrency(position.mark)}</dd>
          </div>
          <div>
            <dt>Size</dt>
            <dd>{position.size}</dd>
          </div>
          <div>
            <dt>PnL</dt>
            <dd className={position.pnl >= 0 ? "good-text" : "danger-text"}>{formatCurrency(position.pnl)}</dd>
          </div>
          <div>
            <dt>Stop Loss</dt>
            <dd>{position.stopLoss ? formatCurrency(position.stopLoss) : "Missing"}</dd>
          </div>
          <div>
            <dt>Take Profit</dt>
            <dd>{position.takeProfit ? formatCurrency(position.takeProfit) : "Missing"}</dd>
          </div>
          <div>
            <dt>R Multiple</dt>
            <dd>{position.rMultiple}</dd>
          </div>
          <div>
            <dt>Signal</dt>
            <dd>{position.signalId}</dd>
          </div>
          <div>
            <dt>Freqtrade Trade ID</dt>
            <dd>{position.freqtradeTradeId}</dd>
          </div>
          <div>
            <dt>Anomaly</dt>
            <dd>{position.anomaly ? position.anomaly : "Normal"}</dd>
          </div>
        </dl>
        <section className="drawer-detail-section">
          <h4>Raw Signal</h4>
          <p>{position.rawSignal}</p>
        </section>
        <section className="drawer-detail-section">
          <h4>Structured Signal JSON</h4>
          <pre>{JSON.stringify(position.structuredSignal, undefined, 2)}</pre>
        </section>
        <section className="drawer-detail-section">
          <h4>Orders / Fills</h4>
          <pre>{JSON.stringify({ fills: position.fills, orders: position.orders }, undefined, 2)}</pre>
        </section>
        <section className="drawer-detail-section">
          <h4>Audit Timeline</h4>
          <pre>{JSON.stringify(position.auditTimeline, undefined, 2)}</pre>
        </section>
        <section className="drawer-actions" aria-label="Manual position controls">
          <button
            aria-label="Close position"
            className="danger-button"
            disabled={actionsDisabled || closeSubmitting}
            onClick={() => {
              openAction("close");
            }}
            title="Close position"
            type="button"
          >
            <XCircle size={16} />
            Close position
          </button>
          <button
            aria-label="Partial close position"
            className="secondary-button"
            disabled={actionsDisabled || partialSubmitting}
            onClick={() => {
              openAction("partial");
            }}
            title="Partial close position"
            type="button"
          >
            <XCircle size={16} />
            Partial 50%
          </button>
          <div className="move-stop-control">
            <input
              aria-label="Move SL price"
              inputMode="decimal"
              min="0"
              onChange={(event) => {
                setStopLossInput(event.target.value);
              }}
              placeholder="SL price"
              step="0.0001"
              type="number"
              value={stopLossInput}
            />
            <button
              aria-label="Move SL"
              className="secondary-button"
              disabled={actionsDisabled || moveSubmitting}
              onClick={() => {
                openAction("move");
              }}
              title="Move SL"
              type="button"
            >
              <Send size={16} />
              Move SL
            </button>
          </div>
          <button
            aria-label="Lock pair"
            className="secondary-button"
            disabled={actionsDisabled || lockSubmitting}
            onClick={() => {
              openAction("lock");
            }}
            title="Lock pair"
            type="button"
          >
            <Lock size={16} />
            Lock pair
          </button>
          {actionsDisabled && <p className="drawer-status">Live readonly blocks manual position actions.</p>}
          {status && <p className="drawer-status">{status}</p>}
        </section>
        {confirmAction && (
          <PositionActionConfirm
            action={confirmAction}
            reason={reason}
            submitting={confirmSubmitting}
            onCancel={() => {
              setConfirmAction(false);
            }}
            onReasonChange={setReason}
            onSubmit={() => {
              const actionReason = reason.trim();
              if (!actionReason) {
                return;
              }

              if (confirmAction === "close") {
                void submitClosePosition(actionReason);
                return;
              }

              if (confirmAction === "partial") {
                void submitPartialClose(actionReason);
                return;
              }

              if (confirmAction === "lock") {
                void submitPairLock(actionReason);
                return;
              }

              void submitMoveStopLoss(actionReason);
            }}
          />
        )}
      </div>
    </aside>
  );
}

function getPositionActionSubmitting(
  action: PositionAction,
  closeSubmitting: boolean,
  partialSubmitting: boolean,
  moveSubmitting: boolean,
  lockSubmitting: boolean
): boolean {
  if (action === "close") {
    return closeSubmitting;
  }

  if (action === "partial") {
    return partialSubmitting;
  }

  if (action === "lock") {
    return lockSubmitting;
  }

  if (action === "move") {
    return moveSubmitting;
  }

  return false;
}

type PositionActionConfirmProps = {
  action: Exclude<PositionAction, false>;
  reason: string;
  submitting: boolean;
  onCancel: () => void;
  onReasonChange: (reason: string) => void;
  onSubmit: () => void;
};

function PositionActionConfirm({
  action,
  reason,
  submitting,
  onCancel,
  onReasonChange,
  onSubmit
}: PositionActionConfirmProps): ReactElement {
  const enabled = reason.trim().length > 0 && !submitting;
  let label = "Confirm close position";
  let title = "Close position";
  let buttonText = "Confirm close";

  if (action === "move") {
    label = "Confirm move stop loss";
    title = "Move stop loss";
    buttonText = "Confirm move";
  }
  if (action === "partial") {
    label = "Confirm partial close";
    title = "Partial close position";
    buttonText = "Confirm partial close";
  }
  if (action === "lock") {
    label = "Confirm pair lock";
    title = "Lock pair";
    buttonText = "Confirm lock";
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="position-action-title">
        <header>
          <div>
            <p className="eyebrow">Manual Action</p>
            <h3 id="position-action-title">{title}</h3>
          </div>
          <button aria-label="Cancel position action" className="icon-button" onClick={onCancel} title="Cancel position action" type="button">
            <X size={16} />
          </button>
        </header>
        <p className="modal-copy">Reason is required before submitting the request.</p>
        <input
          aria-label="Action reason"
          onChange={(event) => {
            onReasonChange(event.target.value);
          }}
          placeholder="Reason"
          value={reason}
        />
        <div className="modal-actions">
          <button aria-label="Cancel manual action" className="secondary-button" onClick={onCancel} title="Cancel manual action" type="button">
            <X size={16} />
            Cancel
          </button>
          <button aria-label={label} className="danger-button" disabled={!enabled} onClick={onSubmit} title={label} type="button">
            <Send size={16} />
            {submitting ? "Submitting" : buttonText}
          </button>
        </div>
      </section>
    </div>
  );
}

type BotActionConfirmProps = {
  action: Exclude<BotAction, false>;
  reason: string;
  submitting: boolean;
  onCancel: () => void;
  onReasonChange: (reason: string) => void;
  onSubmit: () => void;
};

function BotActionConfirm({
  action,
  reason,
  submitting,
  onCancel,
  onReasonChange,
  onSubmit
}: BotActionConfirmProps): ReactElement {
  const enabled = reason.trim().length > 0 && !submitting;
  let title = "Pause bot";
  let label = "Confirm pause bot";
  let buttonText = "Confirm pause";

  if (action === "resume") {
    title = "Resume bot";
    label = "Confirm resume bot";
    buttonText = "Confirm resume";
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="bot-action-title">
        <header>
          <div>
            <p className="eyebrow">Bot Control</p>
            <h3 id="bot-action-title">{title}</h3>
          </div>
          <button aria-label="Cancel bot action" className="icon-button" onClick={onCancel} title="Cancel bot action" type="button">
            <X size={16} />
          </button>
        </header>
        <p className="modal-copy">Reason is required before submitting the request.</p>
        <input
          aria-label="Bot action reason"
          onChange={(event) => {
            onReasonChange(event.target.value);
          }}
          placeholder="Reason"
          value={reason}
        />
        <div className="modal-actions">
          <button aria-label="Cancel bot manual action" className="secondary-button" onClick={onCancel} title="Cancel bot manual action" type="button">
            <X size={16} />
            Cancel
          </button>
          <button aria-label={label} className="danger-button" disabled={!enabled} onClick={onSubmit} title={label} type="button">
            <Send size={16} />
            {submitting ? "Submitting" : buttonText}
          </button>
        </div>
      </section>
    </div>
  );
}

function OrderCenterPage({
  refreshKey,
  onRefresh
}: {
  refreshKey: number;
  onRefresh: () => void;
}): ReactElement {
  const { data, loading } = useOrderCenterData(refreshKey);
  const sourceBadgeClass = getSourceBadgeClass(data.dataSource, loading);
  const sourceBadgeLabel = getSourceBadgeLabel(data.dataSource, loading);

  return (
    <section className="page-grid">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Order Center</p>
          <h2>/orders</h2>
        </div>
        <div className="button-row">
          <span className={sourceBadgeClass}>{sourceBadgeLabel}</span>
          <button
            aria-label="Refresh order center"
            className="secondary-button"
            onClick={onRefresh}
            title="Refresh order center"
            type="button"
          >
            <RefreshCcw size={16} />
            Refresh
          </button>
        </div>
      </div>
      <DataQualityPanel quality={data.dataSource} />

      <div className="kpi-grid">
        <MetricCard
          delta={`${data.summary.openPositionCount} open positions`}
          label="Open PnL"
          tone={data.summary.openPnl >= 0 ? "good" : "danger"}
          value={formatCurrency(data.summary.openPnl)}
        />
        <MetricCard
          delta={`${data.summary.historyCount} trades`}
          label="Realized PnL"
          tone={data.summary.realizedPnl >= 0 ? "good" : "danger"}
          value={formatCurrency(data.summary.realizedPnl)}
        />
        <MetricCard
          delta={`${data.summary.winCount} wins / ${data.summary.lossCount} losses`}
          label="Total PnL"
          tone={data.summary.totalPnl >= 0 ? "good" : "danger"}
          value={formatCurrency(data.summary.totalPnl)}
        />
        <MetricCard
          delta="open / pending"
          label="挂单"
          tone={data.summary.pendingOrderCount > 0 ? "warning" : "muted"}
          value={`${data.summary.pendingOrderCount}`}
        />
      </div>

      <Panel title="持仓" icon={<Activity size={17} />}>
        <OrderPositionsTable positions={data.positions} />
      </Panel>

      <Panel title="挂单" icon={<ClipboardList size={17} />}>
        <OrderOrdersTable orders={data.orders} />
      </Panel>

      <Panel title="历史" icon={<History size={17} />}>
        <OrderHistoryTable trades={data.history} />
      </Panel>

      <Panel title="盈亏" icon={<Sigma size={17} />}>
        <dl className="detail-grid compact">
          <div>
            <dt>Open PnL</dt>
            <dd className={data.summary.openPnl >= 0 ? "good-text" : "danger-text"}>{formatCurrency(data.summary.openPnl)}</dd>
          </div>
          <div>
            <dt>Realized PnL</dt>
            <dd className={data.summary.realizedPnl >= 0 ? "good-text" : "danger-text"}>
              {formatCurrency(data.summary.realizedPnl)}
            </dd>
          </div>
          <div>
            <dt>Total PnL</dt>
            <dd className={data.summary.totalPnl >= 0 ? "good-text" : "danger-text"}>{formatCurrency(data.summary.totalPnl)}</dd>
          </div>
          <div>
            <dt>Win / Loss</dt>
            <dd>{data.summary.winCount} / {data.summary.lossCount}</dd>
          </div>
        </dl>
      </Panel>
    </section>
  );
}

function OrderPositionsTable({ positions }: { positions: OrderCenterPosition[] }): ReactElement {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Pair</th>
            <th>Side</th>
            <th>Amount</th>
            <th>Stake</th>
            <th>Entry</th>
            <th>Current</th>
            <th>Leverage</th>
            <th>PnL</th>
            <th>PnL %</th>
            <th>Opened</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((position) => (
            <tr key={position.id}>
              <td>{position.pair}</td>
              <td>{position.side}</td>
              <td>{formatAmount(position.amount)}</td>
              <td>{formatCurrency(position.stakeAmount)}</td>
              <td>{formatCurrency(position.entry)}</td>
              <td>{formatCurrency(position.current)}</td>
              <td>{position.leverage}x</td>
              <td className={position.pnl >= 0 ? "num good-text" : "num danger-text"}>{formatCurrency(position.pnl)}</td>
              <td>{formatOrderPercent(position.pnlPct)}</td>
              <td>{formatOrderDate(position.openDate)}</td>
              <td><StatusPill status={position.status} /></td>
            </tr>
          ))}
          {positions.length === 0 && <EmptyTableRow colSpan={11} label="No open positions from control-plane." />}
        </tbody>
      </table>
    </div>
  );
}

function OrderOrdersTable({ orders }: { orders: OrderCenterOrder[] }): ReactElement {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Order ID</th>
            <th>Pair</th>
            <th>Side</th>
            <th>Type</th>
            <th>Status</th>
            <th>Price</th>
            <th>Amount</th>
            <th>Filled</th>
            <th>Remaining</th>
            <th>Created</th>
            <th>Trade ID</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((order) => (
            <tr key={`${order.tradeId}-${order.id}`}>
              <td>{order.id}</td>
              <td>{order.pair}</td>
              <td>{order.side}</td>
              <td>{order.type}</td>
              <td><StatusPill status={order.status} /></td>
              <td>{formatCurrency(order.price)}</td>
              <td>{formatAmount(order.amount)}</td>
              <td>{formatAmount(order.filled)}</td>
              <td>{formatAmount(order.remaining)}</td>
              <td>{formatOrderDate(order.createdAt)}</td>
              <td>{order.tradeId || "--"}</td>
            </tr>
          ))}
          {orders.length === 0 && <EmptyTableRow colSpan={11} label="No open or pending orders from control-plane." />}
        </tbody>
      </table>
    </div>
  );
}

function OrderHistoryTable({ trades }: { trades: OrderCenterTrade[] }): ReactElement {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Trade ID</th>
            <th>Pair</th>
            <th>Side</th>
            <th>Status</th>
            <th>Amount</th>
            <th>Open</th>
            <th>Close</th>
            <th>PnL</th>
            <th>PnL %</th>
            <th>Opened</th>
            <th>Closed</th>
            <th>Orders</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((trade) => (
            <tr key={trade.id}>
              <td>{trade.id}</td>
              <td>{trade.pair}</td>
              <td>{trade.side}</td>
              <td><StatusPill status={trade.status} /></td>
              <td>{formatAmount(trade.amount)}</td>
              <td>{formatCurrency(trade.openRate)}</td>
              <td>{trade.closeRate > 0 ? formatCurrency(trade.closeRate) : "--"}</td>
              <td className={trade.pnl >= 0 ? "num good-text" : "num danger-text"}>{formatCurrency(trade.pnl)}</td>
              <td>{formatOrderPercent(trade.pnlPct)}</td>
              <td>{formatOrderDate(trade.openDate)}</td>
              <td>{formatOrderDate(trade.closeDate)}</td>
              <td>{trade.ordersCount}</td>
            </tr>
          ))}
          {trades.length === 0 && <EmptyTableRow colSpan={12} label="No trade history from control-plane." />}
        </tbody>
      </table>
    </div>
  );
}

function EmptyTableRow({ colSpan, label }: { colSpan: number; label: string }): ReactElement {
  return (
    <tr>
      <td className="empty-table" colSpan={colSpan}>{label}</td>
    </tr>
  );
}

function formatAmount(value: number): string {
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: 8
  }).format(value);
}

function formatOrderPercent(value: number): string {
  let normalized = value;
  if (Math.abs(normalized) > 0 && Math.abs(normalized) <= 1) {
    normalized *= 100;
  }

  return formatPercent(normalized);
}

function formatOrderDate(value: string): string {
  if (!value || value === "--") {
    return "--";
  }

  if (/^\d+$/.test(value)) {
    const timestamp = Number(value);
    if (Number.isFinite(timestamp)) {
      const millis = timestamp > 1_000_000_000_000 ? timestamp : timestamp * 1000;
      return new Date(millis).toISOString().replace("T", " ").slice(0, 19);
    }
  }

  return value.replace("T", " ").slice(0, 19);
}

type ReviewDecision =
  | { kind: "signal"; action: "approve" | "reject"; item: SignalReviewItem }
  | { kind: "proposal"; action: "approve" | "reject"; item: SignalReviewProposal }
  | false;

function SignalReviewPage({
  refreshKey,
  onRefresh
}: {
  refreshKey: number;
  onRefresh: () => void;
}): ReactElement {
  const { data, loading } = useSignalReviewData(refreshKey);
  const [selectedSignalId, setSelectedSignalId] = useState("");
  const [decision, setDecision] = useState<ReviewDecision>(false);
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [status, setStatus] = useState("");
  const selectedSignal = data.signals.find((signal) => signal.signalId === selectedSignalId) || data.signals[0] || false;
  const sourceBadgeClass = getSourceBadgeClass(data.dataSource, loading);
  const sourceBadgeLabel = getSourceBadgeLabel(data.dataSource, loading);

  async function submitDecision(): Promise<void> {
    if (!decision) {
      return;
    }

    const actionReason = reason.trim();
    if (!actionReason || submitting) {
      return;
    }

    setSubmitting(true);
    setStatus("Review decision pending");
    let accepted = false;
    if (decision.kind === "signal" && decision.action === "approve") {
      accepted = await approveReviewSignal(decision.item.signalId, actionReason);
    }
    if (decision.kind === "signal" && decision.action === "reject") {
      accepted = await rejectReviewSignal(decision.item.signalId, actionReason);
    }
    if (decision.kind === "proposal" && decision.action === "approve") {
      accepted = await approveReviewProposal(decision.item.proposalId, actionReason);
    }
    if (decision.kind === "proposal" && decision.action === "reject") {
      accepted = await rejectReviewProposal(decision.item.proposalId, actionReason);
    }

    setSubmitting(false);
    setDecision(false);
    setReason("");
    setStatus(accepted ? "Review decision submitted" : "Review decision failed");
    if (accepted) {
      onRefresh();
    }
  }

  function openDecision(nextDecision: Exclude<ReviewDecision, false>): void {
    setDecision(nextDecision);
    setReason("");
  }

  return (
    <section className="page-grid">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Signal Review</p>
          <h2>/review</h2>
        </div>
        <div className="button-row">
          <span className={sourceBadgeClass}>{sourceBadgeLabel}</span>
          <button
            aria-label="Refresh signal review"
            className="secondary-button"
            onClick={onRefresh}
            title="Refresh signal review"
            type="button"
          >
            <RefreshCcw size={16} />
            Refresh
          </button>
        </div>
      </div>
      <DataQualityPanel quality={data.dataSource} />

      <div className="kpi-grid">
        <MetricCard delta="awaiting human review" label="needs_review" tone={data.signals.length > 0 ? "warning" : "muted"} value={`${data.signals.length}`} />
        <MetricCard delta="open Hermes proposals" label="Proposals" tone={data.proposals.length > 0 ? "warning" : "muted"} value={`${data.proposals.length}`} />
        <MetricCard delta="approve requests" label="Approve Requests" tone="muted" value={`${countProposalType(data.proposals, "approve_request")}`} />
        <MetricCard
          delta="rate-limit adjustment"
          label="Rate Limit"
          tone="muted"
          value={`${countProposalType(data.proposals, "rate_limit_adjustment")}`}
        />
      </div>

      <div className="two-column wide-left">
        <Panel title="needs_review queue" icon={<Inbox size={17} />}>
          <div className="review-list">
            {data.signals.map((signal) => (
              <button
                aria-label={`Open signal ${signal.signalId}`}
                className={selectedSignal && selectedSignal.signalId === signal.signalId ? "review-row active" : "review-row"}
                key={signal.signalId}
                onClick={() => {
                  setSelectedSignalId(signal.signalId);
                }}
                title={`Open signal ${signal.signalId}`}
                type="button"
              >
                <div>
                  <strong>{signal.pair}</strong>
                  <span>{signal.signalId}</span>
                </div>
                <StatusPill status={signal.status} />
              </button>
            ))}
            {data.signals.length === 0 && <p className="empty-state">No signals awaiting review.</p>}
          </div>
        </Panel>

        <Panel title="Hermes classification" icon={<ShieldCheck size={17} />}>
          {selectedSignal ? (
            <SignalReviewDetail
              signal={selectedSignal}
              onApprove={() => {
                openDecision({ kind: "signal", action: "approve", item: selectedSignal });
              }}
              onReject={() => {
                openDecision({ kind: "signal", action: "reject", item: selectedSignal });
              }}
            />
          ) : (
            <p className="empty-state">Select a signal to review classification details.</p>
          )}
        </Panel>
      </div>

      <Panel title="Hermes proposals" icon={<ClipboardList size={17} />}>
        <SignalProposalTable
          proposals={data.proposals}
          onApprove={(proposal) => {
            openDecision({ kind: "proposal", action: "approve", item: proposal });
          }}
          onReject={(proposal) => {
            openDecision({ kind: "proposal", action: "reject", item: proposal });
          }}
        />
      </Panel>

      {status && <p className="drawer-status">{status}</p>}
      {decision && (
        <ReviewDecisionConfirm
          decision={decision}
          reason={reason}
          submitting={submitting}
          onCancel={() => {
            setDecision(false);
          }}
          onReasonChange={setReason}
          onSubmit={() => {
            void submitDecision();
          }}
        />
      )}
    </section>
  );
}

function SignalReviewDetail({
  signal,
  onApprove,
  onReject
}: {
  signal: SignalReviewItem;
  onApprove: () => void;
  onReject: () => void;
}): ReactElement {
  return (
    <div className="review-detail">
      <dl className="detail-grid compact">
        <div>
          <dt>Pair</dt>
          <dd>{signal.pair}</dd>
        </div>
        <div>
          <dt>Side</dt>
          <dd>{signal.side}</dd>
        </div>
        <div>
          <dt>Entry</dt>
          <dd>{signal.entryMode} {signal.entryPrice > 0 ? formatCurrency(signal.entryPrice) : "CMP"}</dd>
        </div>
        <div>
          <dt>Stop Loss</dt>
          <dd>{signal.stopLoss > 0 ? formatCurrency(signal.stopLoss) : "Missing"}</dd>
        </div>
        <div>
          <dt>Take Profits</dt>
          <dd>{signal.takeProfits.length > 0 ? signal.takeProfits.map(formatCurrency).join(", ") : "Missing"}</dd>
        </div>
        <div>
          <dt>Leverage</dt>
          <dd>{signal.leverage}</dd>
        </div>
        <div>
          <dt>Conclusion</dt>
          <dd>{signal.classification.conclusion}</dd>
        </div>
        <div>
          <dt>Confidence</dt>
          <dd>{signal.classification.confidence}</dd>
        </div>
      </dl>
      <section className="drawer-detail-section">
        <h4>Reason Codes</h4>
        <p>{signal.reasonCodes.length > 0 ? signal.reasonCodes.join(", ") : "none"}</p>
      </section>
      <section className="drawer-detail-section">
        <h4>Raw Signal</h4>
        <p>{signal.rawText || "--"}</p>
      </section>
      <SignalReviewMedia signal={signal} />
      <div className="button-row compact-actions">
        <button aria-label="Approve signal" className="secondary-button" onClick={onApprove} title="Approve signal" type="button">
          <CheckCircle2 size={16} />
          Approve
        </button>
        <button aria-label="Reject signal" className="danger-button" onClick={onReject} title="Reject signal" type="button">
          <XCircle size={16} />
          Reject
        </button>
      </div>
    </div>
  );
}

function SignalReviewMedia({ signal }: { signal: SignalReviewItem }): ReactElement | null {
  const [mediaUrls, setMediaUrls] = useState<Array<{ index: number; url: string }>>([]);
  const [preview, setPreview] = useState<{ index: number; url: string } | false>(false);

  useEffect(() => {
    let active = true;
    const objectUrls: string[] = [];

    setMediaUrls([]);
    setPreview(false);

    if (signal.media.length === 0) {
      return () => {};
    }

    Promise.all(
      signal.media.map(async (item) => {
        const blob = await fetchSignalMediaBlob(signal.signalId, item.index);
        if (!blob) {
          return false;
        }
        const url = URL.createObjectURL(blob);
        objectUrls.push(url);
        return { index: item.index, url };
      })
    ).then((items) => {
      const loadedItems = items.filter((item): item is { index: number; url: string } => item !== false);
      if (!active) {
        loadedItems.forEach((item) => {
          URL.revokeObjectURL(item.url);
        });
        return;
      }
      setMediaUrls(loadedItems);
    });

    return () => {
      active = false;
      objectUrls.forEach((url) => {
        URL.revokeObjectURL(url);
      });
    };
  }, [signal.media, signal.signalId]);

  if (signal.media.length === 0 || mediaUrls.length === 0) {
    return null;
  }

  return (
    <>
      <section aria-label="Signal media thumbnails" className="signal-media-strip">
        {mediaUrls.map((item) => {
          const label = `Signal ${signal.signalId} media ${item.index + 1}`;
          return (
            <button
              aria-label={`Open ${label.toLowerCase()}`}
              className="signal-media-thumb"
              key={item.index}
              onClick={() => {
                setPreview(item);
              }}
              title={`Open ${label.toLowerCase()}`}
              type="button"
            >
              <img alt={label} src={item.url} />
            </button>
          );
        })}
      </section>
      {preview && (
        <section aria-label="Signal media preview" aria-modal="true" className="modal-backdrop" role="dialog">
          <div className="signal-media-lightbox">
            <header>
              <h3>Media {preview.index + 1}</h3>
              <button
                aria-label="Close signal media preview"
                className="icon-button"
                onClick={() => {
                  setPreview(false);
                }}
                title="Close signal media preview"
                type="button"
              >
                <X size={16} />
              </button>
            </header>
            <img alt={`Signal ${signal.signalId} media ${preview.index + 1}`} src={preview.url} />
          </div>
        </section>
      )}
    </>
  );
}

function SignalProposalTable({
  proposals,
  onApprove,
  onReject
}: {
  proposals: SignalReviewProposal[];
  onApprove: (proposal: SignalReviewProposal) => void;
  onReject: (proposal: SignalReviewProposal) => void;
}): ReactElement {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Proposal</th>
            <th>Type</th>
            <th>Signal</th>
            <th>Conclusion</th>
            <th>Detail</th>
            <th>Status</th>
            <th>Decision</th>
          </tr>
        </thead>
        <tbody>
          {proposals.map((proposal) => (
            <tr key={proposal.proposalId}>
              <td>{proposal.title}</td>
              <td>{proposal.type}</td>
              <td>{proposal.signalId || "--"}</td>
              <td>{proposal.classification.conclusion}</td>
              <td>{proposal.detail || proposal.proposedAction}</td>
              <td><StatusPill status={proposal.status} /></td>
              <td>
                <div className="button-row compact-actions no-margin">
                  <button
                    aria-label={`Approve proposal ${proposal.proposalId}`}
                    className="secondary-button"
                    onClick={() => {
                      onApprove(proposal);
                    }}
                    title={`Approve proposal ${proposal.proposalId}`}
                    type="button"
                  >
                    <CheckCircle2 size={16} />
                    Approve
                  </button>
                  <button
                    aria-label={`Reject proposal ${proposal.proposalId}`}
                    className="danger-button"
                    onClick={() => {
                      onReject(proposal);
                    }}
                    title={`Reject proposal ${proposal.proposalId}`}
                    type="button"
                  >
                    <XCircle size={16} />
                    Reject
                  </button>
                </div>
              </td>
            </tr>
          ))}
          {proposals.length === 0 && <EmptyTableRow colSpan={7} label="No Hermes proposals awaiting decision." />}
        </tbody>
      </table>
    </div>
  );
}

function ReviewDecisionConfirm({
  decision,
  reason,
  submitting,
  onCancel,
  onReasonChange,
  onSubmit
}: {
  decision: Exclude<ReviewDecision, false>;
  reason: string;
  submitting: boolean;
  onCancel: () => void;
  onReasonChange: (reason: string) => void;
  onSubmit: () => void;
}): ReactElement {
  const enabled = reason.trim().length > 0 && !submitting;
  const targetLabel = decision.kind === "signal" ? decision.item.signalId : decision.item.proposalId;
  const title = `${decision.action === "approve" ? "Approve" : "Reject"} ${decision.kind}`;
  const ariaLabel = `Confirm ${decision.action} ${decision.kind}`;

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="review-decision-title">
        <header>
          <div>
            <p className="eyebrow">Audit Required</p>
            <h3 id="review-decision-title">{title}</h3>
          </div>
          <button aria-label="Cancel review decision" className="icon-button" onClick={onCancel} title="Cancel review decision" type="button">
            <X size={16} />
          </button>
        </header>
        <p className="modal-copy">Reason is required and will be written to the audit log for {targetLabel}.</p>
        <input
          aria-label="Review decision reason"
          onChange={(event) => {
            onReasonChange(event.target.value);
          }}
          placeholder="Reason"
          value={reason}
        />
        <div className="modal-actions">
          <button aria-label="Cancel review manual action" className="secondary-button" onClick={onCancel} title="Cancel review manual action" type="button">
            <X size={16} />
            Cancel
          </button>
          <button aria-label={ariaLabel} className="danger-button" disabled={!enabled} onClick={onSubmit} title={ariaLabel} type="button">
            <Send size={16} />
            {submitting ? "Submitting" : "Confirm"}
          </button>
        </div>
      </section>
    </div>
  );
}

function countProposalType(proposals: SignalReviewProposal[], type: string): number {
  return proposals.filter((proposal) => proposal.type === type).length;
}

function RiskPage(): ReactElement {
  const { data, loading } = useRiskData();
  const [modalOpen, setModalOpen] = useState(false);

  return (
    <section className="page-grid">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Risk Controls</p>
          <h2>/risk</h2>
        </div>
        <button
          aria-label="Open kill switch modal"
          className="danger-button"
          onClick={() => {
            setModalOpen(true);
          }}
          title="Open kill switch modal"
          type="button"
        >
          <Siren size={16} />
          Kill Switch
        </button>
      </div>
      <DataQualityPanel quality={data.dataSource} />

      <div className="risk-grid">
        {data.metrics.map((metric) => (
          <RiskMetricCard key={metric.label} metric={metric} />
        ))}
      </div>

      <div className="two-column">
        <Panel title="Blocking Reasons" icon={<XCircle size={17} />}>
          <ul className="reason-list">
            {data.blockingReasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </Panel>
        <Panel title="Pair Lock 表" icon={<Lock size={17} />}>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Pair</th>
                  <th>Reason</th>
                  <th>Until</th>
                  <th>Owner</th>
                </tr>
              </thead>
              <tbody>
                {data.pairLocks.map((lock) => (
                  <tr key={lock.pair}>
                    <td>{lock.pair}</td>
                    <td>{lock.reason}</td>
                    <td>{lock.until}</td>
                    <td>{lock.owner}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>

      <span className={loading ? "load-badge loading" : "load-badge"}>{loading ? "Loading API" : "Risk data ready"}</span>
      {modalOpen && (
        <KillSwitchModal
          onClose={() => {
            setModalOpen(false);
          }}
        />
      )}
    </section>
  );
}

function RiskMetricCard({ metric }: { metric: RiskMetric }): ReactElement {
  return (
    <article className={`metric-card ${statusTone(metric.status)}`}>
      <span>{metric.label}</span>
      <strong>{metric.value}</strong>
      <small>Limit {metric.limit}</small>
    </article>
  );
}

function KillSwitchModal({ onClose }: { onClose: () => void }): ReactElement {
  const [confirmText, setConfirmText] = useState("");
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [status, setStatus] = useState("");
  const enabled = confirmText === "CLOSE ALL" && reason.trim().length > 0 && !submitting;

  async function submitKillSwitch(): Promise<void> {
    if (!enabled) {
      return;
    }

    setSubmitting(true);
    setStatus("Submitting kill switch");
    const result = await activateKillSwitch(reason.trim(), true, confirmText);
    setSubmitting(false);
    setStatus(result.statusText);

    if (result.complete) {
      onClose();
    }
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="kill-title">
        <header>
          <div>
            <p className="eyebrow">Destructive Control</p>
            <h3 id="kill-title">Kill Switch</h3>
          </div>
          <button aria-label="Close kill switch modal" className="icon-button" onClick={onClose} title="Close kill switch modal" type="button">
            <X size={16} />
          </button>
        </header>
        <p className="modal-copy">输入 CLOSE ALL 执行全平确认</p>
        <input
          aria-label="Kill switch reason"
          onChange={(event) => {
            setReason(event.target.value);
          }}
          placeholder="Reason"
          value={reason}
        />
        <input
          aria-label="Type CLOSE ALL to confirm"
          onChange={(event) => {
            setConfirmText(event.target.value);
          }}
          placeholder="CLOSE ALL"
          value={confirmText}
        />
        <div className="modal-actions">
          <button aria-label="Cancel kill switch" className="secondary-button" onClick={onClose} title="Cancel kill switch" type="button">
            <X size={16} />
            Cancel
          </button>
          <button
            aria-label="Close all positions"
            className="danger-button"
            disabled={!enabled}
            onClick={() => {
              void submitKillSwitch();
            }}
            title="Close all positions"
            type="button"
          >
            <Siren size={16} />
            {submitting ? "Submitting" : "CLOSE ALL"}
          </button>
        </div>
        {status && <p className="modal-status">{status}</p>}
      </section>
    </div>
  );
}

function ReportsPage({ onNavigate }: { onNavigate: (path: string) => void }): ReactElement {
  const today = new Date().toISOString().slice(0, 10);

  return (
    <section className="page-grid">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Report Center</p>
          <h2>/reports</h2>
        </div>
        <button
          aria-label="Open latest daily report"
          className="secondary-button"
          onClick={() => {
            onNavigate(`/reports/daily/${today}`);
          }}
          title="Open latest daily report"
          type="button"
        >
          <FileText size={16} />
          打开最新日报
        </button>
      </div>

      <Panel title="日报入口" icon={<FileText size={17} />}>
        <p className="empty-copy">Open a daily report to load the control-plane snapshot for that date.</p>
      </Panel>
    </section>
  );
}

function DailyReportPage({ date }: { date: string }): ReactElement {
  const { data, loading } = useReportData(date);
  const markdown = useMemo(() => buildMarkdown(data), [data]);
  const [previewTitle, setPreviewTitle] = useState("");
  const [previewBody, setPreviewBody] = useState("");

  async function downloadMarkdown(): Promise<void> {
    let content = await fetchDailyReportMarkdown(data.date);
    if (!content) {
      content = markdown;
    }

    const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `hermes-daily-${data.date}.md`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  async function showTelegramPreview(): Promise<void> {
    const text = await fetchDailyReportTelegramPreview(data.date);
    setPreviewTitle("Telegram 简版");
    if (text) {
      setPreviewBody(text);
      return;
    }

    setPreviewBody("Telegram preview unavailable");
  }

  async function showHtmlPreview(): Promise<void> {
    let content = await fetchDailyReportMarkdown(data.date);
    if (!content) {
      content = markdown;
    }

    setPreviewTitle("HTML 预览");
    setPreviewBody(content);
  }

  async function showVersions(): Promise<void> {
    const versions = await fetchDailyReportVersions(data.date);
    setPreviewTitle("版本对比");
    setPreviewBody(JSON.stringify(versions, undefined, 2));
  }

  async function showSourceSnapshot(): Promise<void> {
    const snapshot = await createDailyReportSnapshot(data.date);
    setPreviewTitle("Source Snapshot");
    if (snapshot) {
      setPreviewBody(JSON.stringify(snapshot, undefined, 2));
      return;
    }

    setPreviewBody("Source snapshot unavailable");
  }

  return (
    <section className="page-grid">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Daily Report</p>
          <h2>{data.date}</h2>
        </div>
        <div className="button-row">
          <button
            aria-label="Download Markdown"
            className="secondary-button"
            onClick={() => {
              void downloadMarkdown();
            }}
            title="Download Markdown"
            type="button"
          >
            <Download size={16} />
            下载 Markdown
          </button>
          <button
            aria-label="Telegram brief"
            className="secondary-button"
            onClick={() => {
              void showTelegramPreview();
            }}
            title="Telegram brief"
            type="button"
          >
            <Send size={16} />
            Telegram 简版
          </button>
          <button
            aria-label="HTML preview"
            className="secondary-button"
            onClick={() => {
              void showHtmlPreview();
            }}
            title="HTML preview"
            type="button"
          >
            <FileText size={16} />
            HTML 预览
          </button>
          <button
            aria-label="Compare report versions"
            className="secondary-button"
            onClick={() => {
              void showVersions();
            }}
            title="Compare report versions"
            type="button"
          >
            <RefreshCcw size={16} />
            版本对比
          </button>
          <button
            aria-label="View source snapshot"
            className="secondary-button"
            onClick={() => {
              void showSourceSnapshot();
            }}
            title="View source snapshot"
            type="button"
          >
            <FileText size={16} />
            Source Snapshot
          </button>
        </div>
      </div>
      <DataQualityPanel quality={data.dataSource} />

      <div className="kpi-grid">
        <MetricCard delta="Total equity" label="账户权益" tone="good" value={formatCurrency(data.account.equity)} />
        <MetricCard delta="Net realized + open" label="净收益" tone="good" value={formatCurrency(data.account.netPnl)} />
        <MetricCard delta="Intraday max" label="最大回撤" tone="warning" value={formatPercent(data.account.maxDrawdown)} />
        <MetricCard delta="Executed notional" label="成交额" tone="muted" value={formatCurrency(data.account.volume)} />
      </div>

      <div className="two-column">
        <Panel title="交易表现" icon={<CheckCircle2 size={17} />}>
          <dl className="detail-grid compact">
            <div>
              <dt>Trades</dt>
              <dd>{data.performance.trades}</dd>
            </div>
            <div>
              <dt>Win Rate</dt>
              <dd>{formatPercent(data.performance.winRate)}</dd>
            </div>
            <div>
              <dt>Profit Factor</dt>
              <dd>{data.performance.profitFactor}</dd>
            </div>
            <div>
              <dt>Avg R</dt>
              <dd>{data.performance.avgR}</dd>
            </div>
          </dl>
        </Panel>

        <Panel title="信号漏斗" icon={<RefreshCcw size={17} />}>
          <div className="funnel">
            <FunnelBar label="Scanned" max={data.funnel.scanned} value={data.funnel.scanned} />
            <FunnelBar label="Signaled" max={data.funnel.scanned} value={data.funnel.signaled} />
            <FunnelBar label="Entered" max={data.funnel.scanned} value={data.funnel.entered} />
            <FunnelBar label="Closed" max={data.funnel.scanned} value={data.funnel.closed} />
          </div>
        </Panel>
      </div>

      <div className="two-column wide-left">
        <EventsPanel events={data.riskEvents} />
        <PositionsPanel positions={data.positions} onSelect={() => {}} />
      </div>

      {previewTitle && (
        <Panel title={previewTitle} icon={<FileText size={17} />}>
          <pre className="preview-box">{previewBody}</pre>
        </Panel>
      )}

      <span className={loading ? "load-badge loading" : "load-badge"}>{loading ? "Loading API" : "Report ready"}</span>
    </section>
  );
}

type FunnelBarProps = {
  label: string;
  value: number;
  max: number;
};

function FunnelBar({ label, value, max }: FunnelBarProps): ReactElement {
  let width = 0;

  if (max > 0) {
    width = Math.max(4, Math.round((value / max) * 100));
  }

  return (
    <div className="funnel-row">
      <span>{label}</span>
      <div className="bar-track">
        <div className="bar-fill" style={{ width: `${width}%` }} />
      </div>
      <strong>{value}</strong>
    </div>
  );
}

function buildMarkdown(report: DailyReport): string {
  const funnel = [
    `- Scanned: ${report.funnel.scanned}`,
    `- Signaled: ${report.funnel.signaled}`,
    `- Entered: ${report.funnel.entered}`,
    `- Closed: ${report.funnel.closed}`
  ];
  const positions = report.positions.map((position) => {
    let stopLoss = "Missing";

    if (position.stopLoss) {
      stopLoss = formatCurrency(position.stopLoss);
    }

    return `- ${position.pair} ${position.side} size ${position.size} PnL ${formatCurrency(position.pnl)} SL ${stopLoss}`;
  });

  return [
    "---",
    `report_id: ${report.dataSource.snapshot_id || report.date}`,
    `generated_at: ${new Date().toISOString()}`,
    "renderer: dashboard",
    "---",
    "",
    `# Hermes Daily Report ${report.date}`,
    "",
    `- Equity: ${formatCurrency(report.account.equity)}`,
    `- Net PnL: ${formatCurrency(report.account.netPnl)}`,
    `- Max Drawdown: ${formatPercent(report.account.maxDrawdown)}`,
    `- Trades: ${report.performance.trades}`,
    `- Win Rate: ${formatPercent(report.performance.winRate)}`,
    "",
    "## Signal Funnel",
    ...funnel,
    "",
    "## Risk Events",
    ...report.riskEvents.map((event) => `- ${event.time} ${event.source}: ${event.message}`),
    "",
    "## Positions",
    ...positions
  ].join("\n");
}
