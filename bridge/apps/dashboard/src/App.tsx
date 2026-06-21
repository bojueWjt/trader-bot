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
    { path: "/dashboard", label: "看板", icon: Activity },
    { path: "/orders", label: "订单", icon: ClipboardList },
    { path: "/review", label: "审核", icon: Inbox },
    { path: "/risk", label: "风控", icon: ShieldAlert },
    { path: "/reports", label: "报表", icon: FileText },
    { path: `/reports/daily/${today}`, label: "日报", icon: FileText }
  ];

  return (
    <aside className="sidebar" aria-label="主导航">
      <div className="brand">
        <span className="brand-mark">HT</span>
        <div>
          <strong>Hermes Trader</strong>
          <span>运营</span>
        </div>
      </div>
      <nav className="nav-list">
        {items.map((item) => {
          const Icon = item.icon;
          let active = currentPath === item.path || currentPath.startsWith(item.path);
          if (item.path === "/reports") {
            active = currentPath === "/reports";
          }

          return (
            <button
              aria-label={`打开${item.label}`}
              className={active ? "nav-item active" : "nav-item"}
              key={item.path}
              onClick={() => {
                onNavigate(item.path);
              }}
              title={`打开${item.label}`}
              type="button"
            >
              <Icon size={17} />
              <span>{item.label}</span>
            </button>
          );
        })}
        <a
          aria-label="打开 Telegram 监听台"
          className="nav-item"
          href="/watcher/"
          rel="noreferrer"
          target="_blank"
          title="打开 Telegram 监听台"
        >
          <Radio size={17} />
          <span>监听台</span>
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
        <p className="eyebrow">加密运营控制台</p>
        <h1>实时运营看板</h1>
      </div>
      <div className="topbar-actions">
        <div className={realtimeConnected ? "connection good" : "connection danger"} role="status">
          <Radio size={16} />
          <span>{realtimeLabel}</span>
        </div>
        {showLogout && (
          <button aria-label="退出登录" className="secondary-button" onClick={onLogout} title="退出登录" type="button">
            <LogOut size={16} />
            退出登录
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

    const actionLabel = botAction === "pause" ? "暂停" : "恢复";

    setBotActionSubmitting(true);
    setBotActionStatus(`${actionLabel}请求处理中`);

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

    setBotActionStatus(`${actionLabel}失败`);
  }

  return (
    <section className="page-grid">
      <div className="section-heading">
        <div>
          <p className="eyebrow">实时总览</p>
          <h2>/dashboard</h2>
        </div>
        <span className={sourceBadgeClass}>{sourceBadgeLabel}</span>
      </div>
      <DataQualityPanel quality={data.dataSource} />

      <section className="ops-strip" aria-label="操作员状态">
        <div className={`ops-mode ${statusTone(data.safety.riskState)}`}>
          <ShieldCheck size={18} />
          <div>
            <span>运行模式</span>
            <strong>{data.safety.modeLabel}</strong>
          </div>
        </div>
        <div>
          <span>数据新鲜度</span>
          <strong>{data.safety.dataFreshness}</strong>
        </div>
        <div>
          <span>操作员通道</span>
          <strong>{data.safety.operatorLane}</strong>
        </div>
        <div>
          <span>上次同步</span>
          <strong>{data.safety.lastSync}</strong>
        </div>
      </section>

      <div className="kpi-grid">
        {data.kpis.map((kpi) => (
          <MetricCard delta={kpi.delta} key={kpi.label} label={kpi.label} tone={kpi.tone} value={kpi.value} />
        ))}
      </div>

      <Panel title="组合敞口" icon={<ShieldAlert size={17} />}>
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
    return "接口加载中";
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
    <section className={quality.stale ? "quality-panel stale" : "quality-panel"} aria-label="数据质量">
      {quality.stale && (
        <div className="stale-banner" role="alert">
          stale=true; missing_nodes={quality.missing_nodes.length > 0 ? quality.missing_nodes.join(", ") : "none"}
        </div>
      )}
      <dl className="quality-grid">
        <div>
          <dt>数据源</dt>
          <dd>{quality.data_source}</dd>
        </div>
        <div>
          <dt>快照ID</dt>
          <dd>{quality.snapshot_id || "不可用"}</dd>
        </div>
        <div>
          <dt>生成时间</dt>
          <dd>{quality.generated_at || "不可用"}</dd>
        </div>
        <div>
          <dt>投影延迟(ms)</dt>
          <dd>{quality.projection_lag_ms}</dd>
        </div>
        <div>
          <dt>是否过期</dt>
          <dd>{String(quality.stale)}</dd>
        </div>
        <div>
          <dt>缺失节点</dt>
          <dd>{quality.missing_nodes.length > 0 ? quality.missing_nodes.join(", ") : "无"}</dd>
        </div>
        <div>
          <dt>对账状态</dt>
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
    <Panel title="机器人状态" icon={<Bot size={17} />}>
      <div className="bot-list">
        {bots.map((bot) => (
          <div className="bot-row" key={bot.name}>
            <div>
              <strong>{bot.name}</strong>
              <span>{bot.pairCount} 个交易对 · {bot.openTrades} 个持仓</span>
            </div>
            <StatusPill status={bot.status} />
            <small>{bot.lastHeartbeat}</small>
          </div>
        ))}
      </div>
      <div className="button-row compact-actions">
        <button
          aria-label="暂停机器人"
          className="secondary-button"
          disabled={disabled}
          onClick={() => {
            onAction("pause");
          }}
          title="暂停机器人"
          type="button"
        >
          <Siren size={16} />
          暂停机器人
        </button>
        <button
          aria-label="恢复机器人"
          className="secondary-button"
          disabled={disabled}
          onClick={() => {
            onAction("resume");
          }}
          title="恢复机器人"
          type="button"
        >
          <RefreshCcw size={16} />
          恢复机器人
        </button>
      </div>
      {disabled && <p className="drawer-status">实盘只读模式禁止手动操作机器人。</p>}
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
        {events.length === 0 && <p className="empty-copy">控制面暂无事件。</p>}
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
              <th>交易对</th>
              <th>方向</th>
              <th>入场价</th>
              <th>现价</th>
              <th>止损</th>
              <th>止盈</th>
              <th>数量</th>
              <th>杠杆</th>
              <th>盈亏</th>
              <th>R 倍数</th>
              <th>信号</th>
              <th>Freqtrade 交易ID</th>
              <th>详情</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((position) => (
              <tr className={position.anomaly ? "row-alert" : ""} key={position.id}>
                <td>{position.pair}</td>
                <td>{position.side}</td>
                <td>{formatCurrency(position.entry)}</td>
                <td>{formatCurrency(position.mark)}</td>
                <td>{position.stopLoss ? formatCurrency(position.stopLoss) : "缺失"}</td>
                <td>{position.takeProfit ? formatCurrency(position.takeProfit) : "缺失"}</td>
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
                    aria-label={`打开 ${position.pair} 详情`}
                    className="icon-button"
                    onClick={() => {
                      onSelect(position);
                    }}
                    title={`打开 ${position.pair} 详情`}
                    type="button"
                  >
                    <ChevronRight size={16} />
                  </button>
                </td>
              </tr>
            ))}
            {positions.length === 0 && <EmptyTableRow colSpan={13} label="控制面暂无持仓。" />}
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
    setStatus("正在平仓");
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
      setStatus("请输入有效的止损价");
      return;
    }

    setMoveSubmitting(true);
    setStatus("正在移动止损");
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
      setStatus("部分平仓数量不可用");
      return;
    }

    setPartialSubmitting(true);
    setStatus("部分平仓处理中");
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
    setStatus("交易对锁定处理中");
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
    <aside className="drawer" aria-label={`${position.pair} 详情`}>
      <div className="drawer-card">
        <header>
          <div>
            <p className="eyebrow">持仓详情</p>
            <h3>{position.pair}</h3>
          </div>
          <button aria-label="关闭详情面板" className="icon-button" onClick={onClose} title="关闭详情面板" type="button">
            <X size={16} />
          </button>
        </header>
        <dl className="detail-grid">
          <div>
            <dt>方向</dt>
            <dd>{position.side}</dd>
          </div>
          <div>
            <dt>杠杆</dt>
            <dd>{position.leverage}x</dd>
          </div>
          <div>
            <dt>入场价</dt>
            <dd>{formatCurrency(position.entry)}</dd>
          </div>
          <div>
            <dt>标记价</dt>
            <dd>{formatCurrency(position.mark)}</dd>
          </div>
          <div>
            <dt>数量</dt>
            <dd>{position.size}</dd>
          </div>
          <div>
            <dt>盈亏</dt>
            <dd className={position.pnl >= 0 ? "good-text" : "danger-text"}>{formatCurrency(position.pnl)}</dd>
          </div>
          <div>
            <dt>止损</dt>
            <dd>{position.stopLoss ? formatCurrency(position.stopLoss) : "缺失"}</dd>
          </div>
          <div>
            <dt>止盈</dt>
            <dd>{position.takeProfit ? formatCurrency(position.takeProfit) : "缺失"}</dd>
          </div>
          <div>
            <dt>R 倍数</dt>
            <dd>{position.rMultiple}</dd>
          </div>
          <div>
            <dt>信号</dt>
            <dd>{position.signalId}</dd>
          </div>
          <div>
            <dt>Freqtrade 交易ID</dt>
            <dd>{position.freqtradeTradeId}</dd>
          </div>
          <div>
            <dt>异常</dt>
            <dd>{position.anomaly ? position.anomaly : "正常"}</dd>
          </div>
        </dl>
        <section className="drawer-detail-section">
          <h4>原始信号</h4>
          <p>{position.rawSignal}</p>
        </section>
        <section className="drawer-detail-section">
          <h4>结构化信号 JSON</h4>
          <pre>{JSON.stringify(position.structuredSignal, undefined, 2)}</pre>
        </section>
        <section className="drawer-detail-section">
          <h4>订单 / 成交</h4>
          <pre>{JSON.stringify({ fills: position.fills, orders: position.orders }, undefined, 2)}</pre>
        </section>
        <section className="drawer-detail-section">
          <h4>审计时间线</h4>
          <pre>{JSON.stringify(position.auditTimeline, undefined, 2)}</pre>
        </section>
        <section className="drawer-actions" aria-label="手动持仓操作">
          <button
            aria-label="平仓"
            className="danger-button"
            disabled={actionsDisabled || closeSubmitting}
            onClick={() => {
              openAction("close");
            }}
            title="平仓"
            type="button"
          >
            <XCircle size={16} />
            平仓
          </button>
          <button
            aria-label="部分平仓"
            className="secondary-button"
            disabled={actionsDisabled || partialSubmitting}
            onClick={() => {
              openAction("partial");
            }}
            title="部分平仓"
            type="button"
          >
            <XCircle size={16} />
            部分平仓 50%
          </button>
          <div className="move-stop-control">
            <input
              aria-label="移动止损价"
              inputMode="decimal"
              min="0"
              onChange={(event) => {
                setStopLossInput(event.target.value);
              }}
              placeholder="止损价"
              step="0.0001"
              type="number"
              value={stopLossInput}
            />
            <button
              aria-label="移动止损"
              className="secondary-button"
              disabled={actionsDisabled || moveSubmitting}
              onClick={() => {
                openAction("move");
              }}
              title="移动止损"
              type="button"
            >
              <Send size={16} />
              移动止损
            </button>
          </div>
          <button
            aria-label="锁定交易对"
            className="secondary-button"
            disabled={actionsDisabled || lockSubmitting}
            onClick={() => {
              openAction("lock");
            }}
            title="锁定交易对"
            type="button"
          >
            <Lock size={16} />
            锁定交易对
          </button>
          {actionsDisabled && <p className="drawer-status">实盘只读模式禁止手动持仓操作。</p>}
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
  let label = "确认平仓";
  let title = "平仓";
  let buttonText = "确认平仓";

  if (action === "move") {
    label = "确认移动止损";
    title = "移动止损";
    buttonText = "确认移动";
  }
  if (action === "partial") {
    label = "确认部分平仓";
    title = "部分平仓";
    buttonText = "确认部分平仓";
  }
  if (action === "lock") {
    label = "确认锁定交易对";
    title = "锁定交易对";
    buttonText = "确认锁定";
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="position-action-title">
        <header>
          <div>
            <p className="eyebrow">手动操作</p>
            <h3 id="position-action-title">{title}</h3>
          </div>
          <button aria-label="取消持仓操作" className="icon-button" onClick={onCancel} title="取消持仓操作" type="button">
            <X size={16} />
          </button>
        </header>
        <p className="modal-copy">提交请求前必须填写原因。</p>
        <input
          aria-label="操作原因"
          onChange={(event) => {
            onReasonChange(event.target.value);
          }}
          placeholder="原因"
          value={reason}
        />
        <div className="modal-actions">
          <button aria-label="取消手动操作" className="secondary-button" onClick={onCancel} title="取消手动操作" type="button">
            <X size={16} />
            取消
          </button>
          <button aria-label={label} className="danger-button" disabled={!enabled} onClick={onSubmit} title={label} type="button">
            <Send size={16} />
            {submitting ? "提交中" : buttonText}
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
  let title = "暂停机器人";
  let label = "确认暂停机器人";
  let buttonText = "确认暂停";

  if (action === "resume") {
    title = "恢复机器人";
    label = "确认恢复机器人";
    buttonText = "确认恢复";
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="bot-action-title">
        <header>
          <div>
            <p className="eyebrow">机器人控制</p>
            <h3 id="bot-action-title">{title}</h3>
          </div>
          <button aria-label="取消机器人操作" className="icon-button" onClick={onCancel} title="取消机器人操作" type="button">
            <X size={16} />
          </button>
        </header>
        <p className="modal-copy">提交请求前必须填写原因。</p>
        <input
          aria-label="机器人操作原因"
          onChange={(event) => {
            onReasonChange(event.target.value);
          }}
          placeholder="原因"
          value={reason}
        />
        <div className="modal-actions">
          <button aria-label="取消机器人手动操作" className="secondary-button" onClick={onCancel} title="取消机器人手动操作" type="button">
            <X size={16} />
            取消
          </button>
          <button aria-label={label} className="danger-button" disabled={!enabled} onClick={onSubmit} title={label} type="button">
            <Send size={16} />
            {submitting ? "提交中" : buttonText}
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
          <p className="eyebrow">订单中心</p>
          <h2>/orders</h2>
        </div>
        <div className="button-row">
          <span className={sourceBadgeClass}>{sourceBadgeLabel}</span>
          <button
            aria-label="刷新订单中心"
            className="secondary-button"
            onClick={onRefresh}
            title="刷新订单中心"
            type="button"
          >
            <RefreshCcw size={16} />
            刷新
          </button>
        </div>
      </div>
      <DataQualityPanel quality={data.dataSource} />

      <div className="kpi-grid">
        <MetricCard
          delta={`${data.summary.openPositionCount} 个持仓`}
          label="浮动盈亏"
          tone={data.summary.openPnl >= 0 ? "good" : "danger"}
          value={formatCurrency(data.summary.openPnl)}
        />
        <MetricCard
          delta={`${data.summary.historyCount} 笔交易`}
          label="已实现盈亏"
          tone={data.summary.realizedPnl >= 0 ? "good" : "danger"}
          value={formatCurrency(data.summary.realizedPnl)}
        />
        <MetricCard
          delta={`${data.summary.winCount} 胜 / ${data.summary.lossCount} 负`}
          label="总盈亏"
          tone={data.summary.totalPnl >= 0 ? "good" : "danger"}
          value={formatCurrency(data.summary.totalPnl)}
        />
        <MetricCard
          delta="挂单 / 待成交"
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
            <dt>浮动盈亏</dt>
            <dd className={data.summary.openPnl >= 0 ? "good-text" : "danger-text"}>{formatCurrency(data.summary.openPnl)}</dd>
          </div>
          <div>
            <dt>已实现盈亏</dt>
            <dd className={data.summary.realizedPnl >= 0 ? "good-text" : "danger-text"}>
              {formatCurrency(data.summary.realizedPnl)}
            </dd>
          </div>
          <div>
            <dt>总盈亏</dt>
            <dd className={data.summary.totalPnl >= 0 ? "good-text" : "danger-text"}>{formatCurrency(data.summary.totalPnl)}</dd>
          </div>
          <div>
            <dt>胜 / 负</dt>
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
            <th>交易对</th>
            <th>方向</th>
            <th>数量</th>
            <th>保证金</th>
            <th>入场价</th>
            <th>现价</th>
            <th>杠杆</th>
            <th>盈亏</th>
            <th>盈亏 %</th>
            <th>开仓时间</th>
            <th>状态</th>
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
          {positions.length === 0 && <EmptyTableRow colSpan={11} label="控制面暂无持仓。" />}
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
            <th>订单ID</th>
            <th>交易对</th>
            <th>方向</th>
            <th>类型</th>
            <th>状态</th>
            <th>价格</th>
            <th>数量</th>
            <th>已成交</th>
            <th>剩余</th>
            <th>创建时间</th>
            <th>交易ID</th>
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
          {orders.length === 0 && <EmptyTableRow colSpan={11} label="控制面暂无挂单或待成交订单。" />}
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
            <th>交易ID</th>
            <th>交易对</th>
            <th>方向</th>
            <th>状态</th>
            <th>数量</th>
            <th>开仓价</th>
            <th>平仓价</th>
            <th>盈亏</th>
            <th>盈亏 %</th>
            <th>开仓时间</th>
            <th>平仓时间</th>
            <th>订单数</th>
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
          {trades.length === 0 && <EmptyTableRow colSpan={12} label="控制面暂无交易历史。" />}
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
    setStatus("审核决策处理中");
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
    setStatus(accepted ? "审核决策已提交" : "审核决策失败");
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
          <p className="eyebrow">信号审核</p>
          <h2>/review</h2>
        </div>
        <div className="button-row">
          <span className={sourceBadgeClass}>{sourceBadgeLabel}</span>
          <button
            aria-label="刷新信号审核"
            className="secondary-button"
            onClick={onRefresh}
            title="刷新信号审核"
            type="button"
          >
            <RefreshCcw size={16} />
            刷新
          </button>
        </div>
      </div>
      <DataQualityPanel quality={data.dataSource} />

      <div className="kpi-grid">
        <MetricCard delta="等待人工审核" label="待审核" tone={data.signals.length > 0 ? "warning" : "muted"} value={`${data.signals.length}`} />
        <MetricCard delta="待处理 Hermes 提案" label="提案" tone={data.proposals.length > 0 ? "warning" : "muted"} value={`${data.proposals.length}`} />
        <MetricCard delta="放行请求" label="放行请求" tone="muted" value={`${countProposalType(data.proposals, "approve_request")}`} />
        <MetricCard
          delta="限流调整"
          label="限流"
          tone="muted"
          value={`${countProposalType(data.proposals, "rate_limit_adjustment")}`}
        />
      </div>

      <div className="two-column wide-left">
        <Panel title="待审核队列" icon={<Inbox size={17} />}>
          <div className="review-list">
            {data.signals.map((signal) => (
              <button
                aria-label={`打开信号 ${signal.signalId}`}
                className={selectedSignal && selectedSignal.signalId === signal.signalId ? "review-row active" : "review-row"}
                key={signal.signalId}
                onClick={() => {
                  setSelectedSignalId(signal.signalId);
                }}
                title={`打开信号 ${signal.signalId}`}
                type="button"
              >
                <div>
                  <strong>{signal.pair}</strong>
                  <span>{signal.signalId}</span>
                </div>
                <StatusPill status={signal.status} />
              </button>
            ))}
            {data.signals.length === 0 && <p className="empty-state">暂无待审核信号。</p>}
          </div>
        </Panel>

        <Panel title="Hermes 分类" icon={<ShieldCheck size={17} />}>
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
            <p className="empty-state">请选择一个信号以查看分类详情。</p>
          )}
        </Panel>
      </div>

      <Panel title="Hermes 提案" icon={<ClipboardList size={17} />}>
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
          <dt>交易对</dt>
          <dd>{signal.pair}</dd>
        </div>
        <div>
          <dt>方向</dt>
          <dd>{signal.side}</dd>
        </div>
        <div>
          <dt>入场</dt>
          <dd>{signal.entryMode} {signal.entryPrice > 0 ? formatCurrency(signal.entryPrice) : "CMP"}</dd>
        </div>
        <div>
          <dt>止损</dt>
          <dd>{signal.stopLoss > 0 ? formatCurrency(signal.stopLoss) : "缺失"}</dd>
        </div>
        <div>
          <dt>止盈</dt>
          <dd>{signal.takeProfits.length > 0 ? signal.takeProfits.map(formatCurrency).join(", ") : "缺失"}</dd>
        </div>
        <div>
          <dt>杠杆</dt>
          <dd>{signal.leverage}</dd>
        </div>
        <div>
          <dt>结论</dt>
          <dd>{signal.classification.conclusion}</dd>
        </div>
        <div>
          <dt>置信度</dt>
          <dd>{signal.classification.confidence}</dd>
        </div>
      </dl>
      <section className="drawer-detail-section">
        <h4>原因代码</h4>
        <p>{signal.reasonCodes.length > 0 ? signal.reasonCodes.join(", ") : "无"}</p>
      </section>
      <section className="drawer-detail-section">
        <h4>原始信号</h4>
        <p>{signal.rawText || "--"}</p>
      </section>
      <SignalReviewMedia signal={signal} />
      <div className="button-row compact-actions">
        <button aria-label="通过信号" className="secondary-button" onClick={onApprove} title="通过信号" type="button">
          <CheckCircle2 size={16} />
          通过
        </button>
        <button aria-label="拒绝信号" className="danger-button" onClick={onReject} title="拒绝信号" type="button">
          <XCircle size={16} />
          拒绝
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
      <section aria-label="信号媒体缩略图" className="signal-media-strip">
        {mediaUrls.map((item) => {
          const label = `信号 ${signal.signalId} 媒体 ${item.index + 1}`;
          return (
            <button
              aria-label={`打开${label}`}
              className="signal-media-thumb"
              key={item.index}
              onClick={() => {
                setPreview(item);
              }}
              title={`打开${label}`}
              type="button"
            >
              <img alt={label} src={item.url} />
            </button>
          );
        })}
      </section>
      {preview && (
        <section aria-label="信号媒体预览" aria-modal="true" className="modal-backdrop" role="dialog">
          <div className="signal-media-lightbox">
            <header>
              <h3>媒体 {preview.index + 1}</h3>
              <button
                aria-label="关闭信号媒体预览"
                className="icon-button"
                onClick={() => {
                  setPreview(false);
                }}
                title="关闭信号媒体预览"
                type="button"
              >
                <X size={16} />
              </button>
            </header>
            <img alt={`信号 ${signal.signalId} 媒体 ${preview.index + 1}`} src={preview.url} />
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
            <th>提案</th>
            <th>类型</th>
            <th>信号</th>
            <th>结论</th>
            <th>详情</th>
            <th>状态</th>
            <th>决策</th>
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
                    aria-label={`通过提案 ${proposal.proposalId}`}
                    className="secondary-button"
                    onClick={() => {
                      onApprove(proposal);
                    }}
                    title={`通过提案 ${proposal.proposalId}`}
                    type="button"
                  >
                    <CheckCircle2 size={16} />
                    通过
                  </button>
                  <button
                    aria-label={`拒绝提案 ${proposal.proposalId}`}
                    className="danger-button"
                    onClick={() => {
                      onReject(proposal);
                    }}
                    title={`拒绝提案 ${proposal.proposalId}`}
                    type="button"
                  >
                    <XCircle size={16} />
                    拒绝
                  </button>
                </div>
              </td>
            </tr>
          ))}
          {proposals.length === 0 && <EmptyTableRow colSpan={7} label="暂无待决策的 Hermes 提案。" />}
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
  const actionText = decision.action === "approve" ? "通过" : "拒绝";
  const kindText = decision.kind === "signal" ? "信号" : "提案";
  const title = `${actionText}${kindText}`;
  const ariaLabel = `确认${actionText}${kindText}`;

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="review-decision-title">
        <header>
          <div>
            <p className="eyebrow">需审计</p>
            <h3 id="review-decision-title">{title}</h3>
          </div>
          <button aria-label="取消审核决策" className="icon-button" onClick={onCancel} title="取消审核决策" type="button">
            <X size={16} />
          </button>
        </header>
        <p className="modal-copy">必须填写原因，且将写入 {targetLabel} 的审计日志。</p>
        <input
          aria-label="审核决策原因"
          onChange={(event) => {
            onReasonChange(event.target.value);
          }}
          placeholder="原因"
          value={reason}
        />
        <div className="modal-actions">
          <button aria-label="取消审核手动操作" className="secondary-button" onClick={onCancel} title="取消审核手动操作" type="button">
            <X size={16} />
            取消
          </button>
          <button aria-label={ariaLabel} className="danger-button" disabled={!enabled} onClick={onSubmit} title={ariaLabel} type="button">
            <Send size={16} />
            {submitting ? "提交中" : "确认"}
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
          <p className="eyebrow">风控</p>
          <h2>/risk</h2>
        </div>
        <button
          aria-label="打开紧急停机弹窗"
          className="danger-button"
          onClick={() => {
            setModalOpen(true);
          }}
          title="打开紧急停机弹窗"
          type="button"
        >
          <Siren size={16} />
          紧急停机
        </button>
      </div>
      <DataQualityPanel quality={data.dataSource} />

      <div className="risk-grid">
        {data.metrics.map((metric) => (
          <RiskMetricCard key={metric.label} metric={metric} />
        ))}
      </div>

      <div className="two-column">
        <Panel title="阻断原因" icon={<XCircle size={17} />}>
          <ul className="reason-list">
            {data.blockingReasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </Panel>
        <Panel title="交易对锁定表" icon={<Lock size={17} />}>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>交易对</th>
                  <th>原因</th>
                  <th>截止时间</th>
                  <th>负责人</th>
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

      <span className={loading ? "load-badge loading" : "load-badge"}>{loading ? "接口加载中" : "风控数据就绪"}</span>
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
      <small>上限 {metric.limit}</small>
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
    setStatus("正在提交紧急停机");
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
            <p className="eyebrow">高危操作</p>
            <h3 id="kill-title">紧急停机</h3>
          </div>
          <button aria-label="关闭紧急停机弹窗" className="icon-button" onClick={onClose} title="关闭紧急停机弹窗" type="button">
            <X size={16} />
          </button>
        </header>
        <p className="modal-copy">输入 CLOSE ALL 执行全平确认</p>
        <input
          aria-label="紧急停机原因"
          onChange={(event) => {
            setReason(event.target.value);
          }}
          placeholder="原因"
          value={reason}
        />
        <input
          aria-label="输入 CLOSE ALL 确认"
          onChange={(event) => {
            setConfirmText(event.target.value);
          }}
          placeholder="CLOSE ALL"
          value={confirmText}
        />
        <div className="modal-actions">
          <button aria-label="取消紧急停机" className="secondary-button" onClick={onClose} title="取消紧急停机" type="button">
            <X size={16} />
            取消
          </button>
          <button
            aria-label="全部平仓"
            className="danger-button"
            disabled={!enabled}
            onClick={() => {
              void submitKillSwitch();
            }}
            title="全部平仓"
            type="button"
          >
            <Siren size={16} />
            {submitting ? "提交中" : "CLOSE ALL"}
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
          <p className="eyebrow">报表中心</p>
          <h2>/reports</h2>
        </div>
        <button
          aria-label="打开最新日报"
          className="secondary-button"
          onClick={() => {
            onNavigate(`/reports/daily/${today}`);
          }}
          title="打开最新日报"
          type="button"
        >
          <FileText size={16} />
          打开最新日报
        </button>
      </div>

      <Panel title="日报入口" icon={<FileText size={17} />}>
        <p className="empty-copy">打开某日日报以加载该日期的控制面快照。</p>
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

    setPreviewBody("Telegram 预览不可用");
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
    setPreviewTitle("源数据快照");
    if (snapshot) {
      setPreviewBody(JSON.stringify(snapshot, undefined, 2));
      return;
    }

    setPreviewBody("源数据快照不可用");
  }

  return (
    <section className="page-grid">
      <div className="section-heading">
        <div>
          <p className="eyebrow">日报</p>
          <h2>{data.date}</h2>
        </div>
        <div className="button-row">
          <button
            aria-label="下载 Markdown"
            className="secondary-button"
            onClick={() => {
              void downloadMarkdown();
            }}
            title="下载 Markdown"
            type="button"
          >
            <Download size={16} />
            下载 Markdown
          </button>
          <button
            aria-label="Telegram 简版"
            className="secondary-button"
            onClick={() => {
              void showTelegramPreview();
            }}
            title="Telegram 简版"
            type="button"
          >
            <Send size={16} />
            Telegram 简版
          </button>
          <button
            aria-label="HTML 预览"
            className="secondary-button"
            onClick={() => {
              void showHtmlPreview();
            }}
            title="HTML 预览"
            type="button"
          >
            <FileText size={16} />
            HTML 预览
          </button>
          <button
            aria-label="版本对比"
            className="secondary-button"
            onClick={() => {
              void showVersions();
            }}
            title="版本对比"
            type="button"
          >
            <RefreshCcw size={16} />
            版本对比
          </button>
          <button
            aria-label="查看源数据快照"
            className="secondary-button"
            onClick={() => {
              void showSourceSnapshot();
            }}
            title="查看源数据快照"
            type="button"
          >
            <FileText size={16} />
            源数据快照
          </button>
        </div>
      </div>
      <DataQualityPanel quality={data.dataSource} />

      <div className="kpi-grid">
        <MetricCard delta="账户总权益" label="账户权益" tone="good" value={formatCurrency(data.account.equity)} />
        <MetricCard delta="已实现 + 浮动" label="净收益" tone="good" value={formatCurrency(data.account.netPnl)} />
        <MetricCard delta="日内最大" label="最大回撤" tone="warning" value={formatPercent(data.account.maxDrawdown)} />
        <MetricCard delta="成交名义额" label="成交额" tone="muted" value={formatCurrency(data.account.volume)} />
      </div>

      <div className="two-column">
        <Panel title="交易表现" icon={<CheckCircle2 size={17} />}>
          <dl className="detail-grid compact">
            <div>
              <dt>交易笔数</dt>
              <dd>{data.performance.trades}</dd>
            </div>
            <div>
              <dt>胜率</dt>
              <dd>{formatPercent(data.performance.winRate)}</dd>
            </div>
            <div>
              <dt>盈利因子</dt>
              <dd>{data.performance.profitFactor}</dd>
            </div>
            <div>
              <dt>平均R倍</dt>
              <dd>{data.performance.avgR}</dd>
            </div>
          </dl>
        </Panel>

        <Panel title="信号漏斗" icon={<RefreshCcw size={17} />}>
          <div className="funnel">
            <FunnelBar label="已扫描" max={data.funnel.scanned} value={data.funnel.scanned} />
            <FunnelBar label="已生成信号" max={data.funnel.scanned} value={data.funnel.signaled} />
            <FunnelBar label="已入场" max={data.funnel.scanned} value={data.funnel.entered} />
            <FunnelBar label="已平仓" max={data.funnel.scanned} value={data.funnel.closed} />
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

      <span className={loading ? "load-badge loading" : "load-badge"}>{loading ? "接口加载中" : "日报就绪"}</span>
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
    `- 已扫描: ${report.funnel.scanned}`,
    `- 已生成信号: ${report.funnel.signaled}`,
    `- 已入场: ${report.funnel.entered}`,
    `- 已平仓: ${report.funnel.closed}`
  ];
  const positions = report.positions.map((position) => {
    let stopLoss = "缺失";

    if (position.stopLoss) {
      stopLoss = formatCurrency(position.stopLoss);
    }

    return `- ${position.pair} ${position.side} 数量 ${position.size} 盈亏 ${formatCurrency(position.pnl)} 止损 ${stopLoss}`;
  });

  return [
    "---",
    `report_id: ${report.dataSource.snapshot_id || report.date}`,
    `generated_at: ${new Date().toISOString()}`,
    "renderer: dashboard",
    "---",
    "",
    `# Hermes 日报 ${report.date}`,
    "",
    `- 账户权益: ${formatCurrency(report.account.equity)}`,
    `- 净收益: ${formatCurrency(report.account.netPnl)}`,
    `- 最大回撤: ${formatPercent(report.account.maxDrawdown)}`,
    `- 交易笔数: ${report.performance.trades}`,
    `- 胜率: ${formatPercent(report.performance.winRate)}`,
    "",
    "## 信号漏斗",
    ...funnel,
    "",
    "## 风险事件",
    ...report.riskEvents.map((event) => `- ${event.time} ${event.source}: ${event.message}`),
    "",
    "## 持仓",
    ...positions
  ].join("\n");
}
