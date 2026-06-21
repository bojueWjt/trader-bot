import {
  Activity,
  ChevronRight,
  ClipboardList,
  History,
  RefreshCcw,
  Send,
  Sigma,
  SlidersHorizontal,
  X,
  XCircle
} from "lucide-react";
import type { ReactElement, ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import {
  cancelOrder,
  closePosition,
  fetchOrderCenter,
  getEmptyOrderCenter,
  moveStopLoss,
  partialClosePosition
} from "../../utils/api";
import type {
  AuthRole,
  CommandResult,
  DataSourceState,
  OrderCenterData,
  OrderCenterOrder,
  OrderCenterPosition,
  OrderCenterTrade
} from "../../utils/api";
import { formatCurrency, formatPercent, statusTone } from "../../utils/format";

type OrdersPageProps = {
  refreshKey: number;
  role: AuthRole;
  onRefresh: () => void;
};

type LoadState<T> = {
  data: T;
  loading: boolean;
};

type OrdersFilters = {
  account: string;
  instrument: string;
  role: string;
  status: string;
  timeRange: string;
};

type DetailSelection =
  | { kind: "order"; id: string }
  | { kind: "position"; id: string }
  | { kind: "trade"; id: string }
  | false;

type ManualAction =
  | { kind: "cancel"; order: OrderCenterOrder }
  | { kind: "close"; position: OrderCenterPosition }
  | { kind: "move-stop"; position: OrderCenterPosition }
  | { kind: "partial-close"; position: OrderCenterPosition }
  | false;

const defaultFilters: OrdersFilters = {
  account: "all",
  instrument: "all",
  role: "all",
  status: "all",
  timeRange: "all"
};

export function OrdersPage({ refreshKey, role, onRefresh }: OrdersPageProps): ReactElement {
  const { data, loading } = useOrderCenterData(refreshKey);
  const [filters, setFilters] = useState<OrdersFilters>(defaultFilters);
  const [detail, setDetail] = useState<DetailSelection>(false);
  const [manualAction, setManualAction] = useState<ManualAction>(false);
  const [actionStatus, setActionStatus] = useState("");
  const readonly = role === "viewer";
  const sourceBadgeClass = getSourceBadgeClass(data.dataSource, loading);
  const sourceBadgeLabel = getSourceBadgeLabel(data.dataSource, loading);
  const filterOptions = useMemo(() => buildFilterOptions(data), [data]);
  const filtered = useMemo(() => filterOrderCenter(data, filters), [data, filters]);

  return (
    <section className="page-grid" data-testid="orders-page">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Order Center</p>
          <h2>/orders</h2>
        </div>
        <div className="button-row">
          {readonly && <span className="status-pill warning">Read-only viewer</span>}
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

      {data.dataSource.reconciliation_state === "failed" && data.dataSource.reason && (
        <section className="validation-summary" role="alert" aria-label="Orders data error">
          <h3>Orders data unavailable</h3>
          <p>{data.dataSource.reason}</p>
        </section>
      )}

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
          label="Open Orders"
          tone={data.summary.pendingOrderCount > 0 ? "warning" : "muted"}
          value={`${data.summary.pendingOrderCount}`}
        />
      </div>

      <OrdersFilterBar filters={filters} options={filterOptions} onChange={setFilters} />

      {loading && (
        <section className="panel settings-state" aria-live="polite">
          <p className="empty-state">Loading order center.</p>
        </section>
      )}

      {actionStatus && <p className="drawer-status" role="status">{actionStatus}</p>}

      <Panel title="Positions" icon={<Activity size={17} />}>
        <OrderPositionsTable
          actionsDisabled={readonly}
          positions={filtered.positions}
          onAction={setManualAction}
          onOpenDetail={(position) => {
            setDetail({ kind: "position", id: position.id });
          }}
        />
      </Panel>

      <Panel title="Orders" icon={<ClipboardList size={17} />}>
        <OrderOrdersTable
          actionsDisabled={readonly}
          orders={filtered.orders}
          onAction={setManualAction}
          onOpenDetail={(order) => {
            setDetail({ kind: "order", id: order.id });
          }}
        />
      </Panel>

      <Panel title="History" icon={<History size={17} />}>
        <OrderHistoryTable
          trades={filtered.history}
          onOpenDetail={(trade) => {
            setDetail({ kind: "trade", id: trade.id });
          }}
        />
      </Panel>

      <Panel title="PnL" icon={<Sigma size={17} />}>
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

      {detail && (
        <OrderDetailDrawer
          data={data}
          detail={detail}
          readonly={readonly}
          onAction={setManualAction}
          onClose={() => {
            setDetail(false);
          }}
        />
      )}

      {manualAction && (
        <OrderActionConfirm
          action={manualAction}
          onCancel={() => {
            setManualAction(false);
          }}
          onSubmit={async (request) => {
            setManualAction(false);
            setActionStatus(`${actionVerb(request.action)} pending`);
            const result = await submitManualAction(request.action, request.reason, request.stopPrice, request.partialFraction);
            setActionStatus(commandStatus(result));
          }}
        />
      )}
    </section>
  );
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

function OrdersFilterBar({
  filters,
  options,
  onChange
}: {
  filters: OrdersFilters;
  options: ReturnType<typeof buildFilterOptions>;
  onChange: (filters: OrdersFilters) => void;
}): ReactElement {
  return (
    <section className="orders-filter-bar" aria-label="Order filters" data-testid="orders-filter-bar">
      <div className="orders-filter-heading">
        <SlidersHorizontal size={16} aria-hidden="true" />
        <h3>Filters</h3>
      </div>
      <label>
        <span>Account</span>
        <select
          aria-label="Account"
          data-testid="orders-filter-account"
          onChange={(event) => {
            onChange({ ...filters, account: event.target.value });
          }}
          value={filters.account}
        >
          <option value="all">All accounts</option>
          {options.accounts.map((account) => <option key={account} value={account}>{account}</option>)}
        </select>
      </label>
      <label>
        <span>Instrument</span>
        <select
          aria-label="Instrument"
          data-testid="orders-filter-instrument"
          onChange={(event) => {
            onChange({ ...filters, instrument: event.target.value });
          }}
          value={filters.instrument}
        >
          <option value="all">All instruments</option>
          {options.instruments.map((instrument) => <option key={instrument} value={instrument}>{instrument}</option>)}
        </select>
      </label>
      <label>
        <span>Status</span>
        <select
          aria-label="Status"
          data-testid="orders-filter-status"
          onChange={(event) => {
            onChange({ ...filters, status: event.target.value });
          }}
          value={filters.status}
        >
          <option value="all">All statuses</option>
          {options.statuses.map((status) => <option key={status} value={status}>{status}</option>)}
        </select>
      </label>
      <label>
        <span>Role</span>
        <select
          aria-label="Role"
          data-testid="orders-filter-role"
          onChange={(event) => {
            onChange({ ...filters, role: event.target.value });
          }}
          value={filters.role}
        >
          <option value="all">All roles</option>
          {options.roles.map((role) => <option key={role} value={role}>{role}</option>)}
        </select>
      </label>
      <label>
        <span>Time range</span>
        <select
          aria-label="Time range"
          data-testid="orders-filter-time-range"
          onChange={(event) => {
            onChange({ ...filters, timeRange: event.target.value });
          }}
          value={filters.timeRange}
        >
          <option value="all">All time</option>
          <option value="1h">Last 1 hour</option>
          <option value="24h">Last 24 hours</option>
          <option value="7d">Last 7 days</option>
        </select>
      </label>
    </section>
  );
}

function OrderPositionsTable({
  actionsDisabled,
  positions,
  onAction,
  onOpenDetail
}: {
  actionsDisabled: boolean;
  positions: OrderCenterPosition[];
  onAction: (action: ManualAction) => void;
  onOpenDetail: (position: OrderCenterPosition) => void;
}): ReactElement {
  return (
    <div className="table-wrap" data-testid="orders-positions-table">
      <table>
        <thead>
          <tr>
            <th>Account</th>
            <th>Pair</th>
            <th>Side</th>
            <th>Amount</th>
            <th>Stake</th>
            <th>Entry</th>
            <th>Current</th>
            <th>Protection</th>
            <th>Leverage</th>
            <th>PnL</th>
            <th>PnL %</th>
            <th>Opened</th>
            <th>Status</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((position) => (
            <tr key={position.id}>
              <td>{position.accountId || "--"}</td>
              <td>{position.pair}</td>
              <td>{position.side}</td>
              <td>{formatAmount(position.amount)}</td>
              <td>{formatCurrency(position.stakeAmount)}</td>
              <td>{formatCurrency(position.entry)}</td>
              <td>{formatCurrency(position.current)}</td>
              <td><StatusPill status={position.protectionStatus} /></td>
              <td>{position.leverage}x</td>
              <td className={position.pnl >= 0 ? "num good-text" : "num danger-text"}>{formatCurrency(position.pnl)}</td>
              <td>{formatOrderPercent(position.pnlPct)}</td>
              <td>{formatOrderDate(position.openDate)}</td>
              <td><StatusPill status={position.status} /></td>
              <td>
                <div className="table-actions">
                  <button
                    aria-label={`Open position ${position.id} detail`}
                    className="icon-button"
                    onClick={() => {
                      onOpenDetail(position);
                    }}
                    title={`Open position ${position.id} detail`}
                    type="button"
                  >
                    <ChevronRight size={16} />
                  </button>
                  {!actionsDisabled && (
                    <button
                      aria-label={`Close position ${position.id}`}
                      className="danger-button"
                      onClick={() => {
                        onAction({ kind: "close", position });
                      }}
                      type="button"
                    >
                      Close
                    </button>
                  )}
                </div>
              </td>
            </tr>
          ))}
          {positions.length === 0 && <EmptyTableRow colSpan={14} label="No positions match the current filters." />}
        </tbody>
      </table>
    </div>
  );
}

function OrderOrdersTable({
  actionsDisabled,
  orders,
  onAction,
  onOpenDetail
}: {
  actionsDisabled: boolean;
  orders: OrderCenterOrder[];
  onAction: (action: ManualAction) => void;
  onOpenDetail: (order: OrderCenterOrder) => void;
}): ReactElement {
  return (
    <div className="table-wrap" data-testid="orders-open-orders-table">
      <table>
        <thead>
          <tr>
            <th>Order ID</th>
            <th>Account</th>
            <th>Pair</th>
            <th>Role</th>
            <th>Side</th>
            <th>Type</th>
            <th>Status</th>
            <th>Protection</th>
            <th>Price</th>
            <th>Amount</th>
            <th>Filled</th>
            <th>Remaining</th>
            <th>Created</th>
            <th>Position</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((order) => (
            <tr key={`${order.tradeId}-${order.id}`}>
              <td>{order.id}</td>
              <td>{order.accountId || "--"}</td>
              <td>{order.pair}</td>
              <td>{order.role}</td>
              <td>{order.side}</td>
              <td>{order.type}</td>
              <td><StatusPill status={order.status} /></td>
              <td><StatusPill status={order.protectionStatus} /></td>
              <td>{formatCurrency(order.price)}</td>
              <td>{formatAmount(order.amount)}</td>
              <td>{formatAmount(order.filled)}</td>
              <td>{formatAmount(order.remaining)}</td>
              <td>{formatOrderDate(order.createdAt)}</td>
              <td>{order.positionId || order.tradeId || "--"}</td>
              <td>
                <div className="table-actions">
                  <button
                    aria-label={`Open order ${order.id} detail`}
                    className="icon-button"
                    onClick={() => {
                      onOpenDetail(order);
                    }}
                    title={`Open order ${order.id} detail`}
                    type="button"
                  >
                    <ChevronRight size={16} />
                  </button>
                  {!actionsDisabled && (
                    <button
                      aria-label={`Cancel order ${order.id}`}
                      className="danger-button"
                      disabled={order.status.toLowerCase() === "filled" || order.status.toLowerCase() === "closed"}
                      onClick={() => {
                        onAction({ kind: "cancel", order });
                      }}
                      type="button"
                    >
                      Cancel
                    </button>
                  )}
                </div>
              </td>
            </tr>
          ))}
          {orders.length === 0 && <EmptyTableRow colSpan={15} label="No orders match the current filters." />}
        </tbody>
      </table>
    </div>
  );
}

function OrderHistoryTable({
  trades,
  onOpenDetail
}: {
  trades: OrderCenterTrade[];
  onOpenDetail: (trade: OrderCenterTrade) => void;
}): ReactElement {
  return (
    <div className="table-wrap" data-testid="orders-history-table">
      <table>
        <thead>
          <tr>
            <th>Trade ID</th>
            <th>Account</th>
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
            <th>Detail</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((trade) => (
            <tr key={trade.id}>
              <td>{trade.id}</td>
              <td>{trade.accountId || "--"}</td>
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
              <td>
                <button
                  aria-label={`Open trade ${trade.id} detail`}
                  className="icon-button"
                  onClick={() => {
                    onOpenDetail(trade);
                  }}
                  title={`Open trade ${trade.id} detail`}
                  type="button"
                >
                  <ChevronRight size={16} />
                </button>
              </td>
            </tr>
          ))}
          {trades.length === 0 && <EmptyTableRow colSpan={14} label="No history rows match the current filters." />}
        </tbody>
      </table>
    </div>
  );
}

function OrderDetailDrawer({
  data,
  detail,
  readonly,
  onAction,
  onClose
}: {
  data: OrderCenterData;
  detail: Exclude<DetailSelection, false>;
  readonly: boolean;
  onAction: (action: ManualAction) => void;
  onClose: () => void;
}): ReactElement {
  const order = detail.kind === "order" ? data.orders.find((candidate) => candidate.id === detail.id) : undefined;
  const position = detail.kind === "position"
    ? data.positions.find((candidate) => candidate.id === detail.id)
    : data.positions.find((candidate) => candidate.id === order?.positionId || candidate.id === order?.tradeId);
  const trade = detail.kind === "trade" ? data.history.find((candidate) => candidate.id === detail.id) : undefined;
  const title = order ? `Order ${order.id}` : position ? `Position ${position.id}` : trade ? `Trade ${trade.id}` : "Order detail";
  const displayTitle = order ? `Order detail ${order.id}` : position ? `Position detail ${position.id}` : trade ? `Trade detail ${trade.id}` : title;
  const protectionStatus = order?.protectionStatus || position?.protectionStatus || "unknown";
  const events = order?.events || [];

  return (
    <aside className="drawer" role="complementary" aria-label={`${title} detail`} data-testid="orders-detail-drawer">
      <div className="drawer-card orders-detail-drawer">
        <header>
          <div>
            <p className="eyebrow">Order Detail</p>
            <h3>{displayTitle}</h3>
          </div>
          <button aria-label="Close order detail" className="icon-button" onClick={onClose} title="Close order detail" type="button">
            <X size={16} />
          </button>
        </header>

        <dl className="detail-grid">
          <div>
            <dt>Account</dt>
            <dd>{order?.accountId || position?.accountId || trade?.accountId || "--"}</dd>
          </div>
          <div>
            <dt>Instrument</dt>
            <dd>{order?.pair || position?.pair || trade?.pair || "--"}</dd>
          </div>
          <div>
            <dt>Status</dt>
            <dd>{order?.status || position?.status || trade?.status || "--"}</dd>
          </div>
          <div>
            <dt>Protection</dt>
            <dd>Protection {protectionStatus}</dd>
          </div>
        </dl>

        <section className="drawer-detail-section">
          <h4>Intent to Position Chain</h4>
          <ol className="orders-timeline">
            <TimelineNode label={`Intent ${order?.intentId || position?.signalId || "unavailable"}`} status={order?.intentId ? "linked" : "missing"} />
            <TimelineNode label={`Execution job ${order?.executionJobId || position?.executionJobId || "unavailable"}`} status={order?.executionJobId || position?.executionJobId ? "linked" : "missing"} />
            <TimelineNode label={`Order ${order?.id || "unavailable"}`} status={order?.status || "missing"} />
            <li>
              <strong>Events</strong>
              {events.length === 0 ? (
                <span>No order events returned</span>
              ) : (
                <ul className="orders-event-list">
                  {events.map((event) => (
                    <li key={event.id}>
                      <span>{formatOrderDate(event.time)}</span>
                      <strong>{event.type}</strong>
                      <span>{event.message}</span>
                    </li>
                  ))}
                </ul>
              )}
            </li>
            <TimelineNode label={`Position ${position?.id || order?.positionId || order?.tradeId || "unavailable"}`} status={position?.status || "missing"} />
          </ol>
        </section>

        <section className="drawer-actions" aria-label="Manual order controls">
          {!readonly && order && (
            <button
              aria-label={`Cancel order ${order.id}`}
              className="danger-button"
              disabled={order.status.toLowerCase() === "filled" || order.status.toLowerCase() === "closed"}
              onClick={() => {
                onAction({ kind: "cancel", order });
              }}
              type="button"
            >
              <XCircle size={16} />
              Cancel order
            </button>
          )}
          {!readonly && position && (
            <>
              <button
                aria-label={`Move stop ${position.id}`}
                className="secondary-button"
                onClick={() => {
                  onAction({ kind: "move-stop", position });
                }}
                type="button"
              >
                <Send size={16} />
                Move stop
              </button>
              <button
                aria-label={`Partial close ${position.id}`}
                className="secondary-button"
                onClick={() => {
                  onAction({ kind: "partial-close", position });
                }}
                type="button"
              >
                <XCircle size={16} />
                Partial close
              </button>
              <button
                aria-label={`Close position ${position.id}`}
                className="danger-button"
                onClick={() => {
                  onAction({ kind: "close", position });
                }}
                type="button"
              >
                <XCircle size={16} />
                Close
              </button>
            </>
          )}
          {readonly && <p className="drawer-status">Read-only viewer cannot submit manual order actions.</p>}
        </section>
      </div>
    </aside>
  );
}

function OrderActionConfirm({
  action,
  onCancel,
  onSubmit
}: {
  action: Exclude<ManualAction, false>;
  onCancel: () => void;
  onSubmit: (request: { action: Exclude<ManualAction, false>; partialFraction: number; reason: string; stopPrice: number }) => void;
}): ReactElement {
  const [reason, setReason] = useState("");
  const [stopPrice, setStopPrice] = useState(() => {
    if (action.kind === "move-stop" && action.position.stopLoss) {
      return String(action.position.stopLoss);
    }
    return "";
  });
  const [partialFraction, setPartialFraction] = useState("0.5");
  const title = actionTitle(action);
  const confirmLabel = `Confirm ${title.toLowerCase()}`;
  const parsedStopPrice = Number(stopPrice);
  const parsedPartialFraction = Number(partialFraction);
  const stopValid = action.kind !== "move-stop" || (Number.isFinite(parsedStopPrice) && parsedStopPrice > 0);
  const partialValid = action.kind !== "partial-close" || (Number.isFinite(parsedPartialFraction) && parsedPartialFraction > 0 && parsedPartialFraction <= 1);
  const enabled = reason.trim().length > 0 && stopValid && partialValid;

  return (
    <div className="modal-backdrop" role="presentation" data-testid="order-action-backdrop">
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="order-action-title" data-testid="order-action-dialog">
        <header>
          <div>
            <p className="eyebrow">Manual Action</p>
            <h3 id="order-action-title">{title}</h3>
          </div>
          <button aria-label="Cancel order action" className="icon-button" onClick={onCancel} title="Cancel order action" type="button">
            <X size={16} />
          </button>
        </header>
        <p className="modal-copy">Reason is required before submitting the request.</p>
        <input
          aria-label="Action reason"
          onChange={(event) => {
            setReason(event.target.value);
          }}
          placeholder="Reason"
          value={reason}
        />
        {action.kind === "move-stop" && (
          <input
            aria-label="Stop price"
            min="0"
            onChange={(event) => {
              setStopPrice(event.target.value);
            }}
            placeholder="Stop price"
            step="any"
            type="number"
            value={stopPrice}
          />
        )}
        {action.kind === "partial-close" && (
          <input
            aria-label="Partial close fraction"
            max="1"
            min="0"
            onChange={(event) => {
              setPartialFraction(event.target.value);
            }}
            placeholder="Fraction"
            step="any"
            type="number"
            value={partialFraction}
          />
        )}
        <div className="modal-actions">
          <button aria-label="Cancel manual action" className="secondary-button" onClick={onCancel} title="Cancel manual action" type="button">
            <X size={16} />
            Cancel
          </button>
          <button
            aria-label={confirmLabel}
            className="danger-button"
            disabled={!enabled}
            onClick={() => {
              onSubmit({
                action,
                partialFraction: parsedPartialFraction,
                reason: reason.trim(),
                stopPrice: parsedStopPrice
              });
            }}
            title={confirmLabel}
            type="button"
          >
            <Send size={16} />
            {title}
          </button>
        </div>
      </section>
    </div>
  );
}

async function submitManualAction(
  action: Exclude<ManualAction, false>,
  reason: string,
  stopPrice: number,
  partialFraction: number
): Promise<CommandResult> {
  if (action.kind === "cancel") {
    return cancelOrder(action.order.id, reason);
  }

  if (action.kind === "move-stop") {
    return moveStopLoss(action.position.id, stopPrice, reason, action.position.signalId);
  }

  if (action.kind === "partial-close") {
    const amount = Number((action.position.amount * partialFraction).toFixed(8));
    return partialClosePosition(action.position.id, amount, reason, action.position.signalId);
  }

  return closePosition(action.position.id, reason, action.position.signalId);
}

function TimelineNode({ label, status }: { label: string; status: string }): ReactElement {
  return (
    <li>
      <strong>{label}</strong>
      <span>{status}</span>
    </li>
  );
}

function buildFilterOptions(data: OrderCenterData): {
  accounts: string[];
  instruments: string[];
  roles: string[];
  statuses: string[];
} {
  return {
    accounts: uniqueSorted([
      ...data.orders.map((order) => order.accountId),
      ...data.positions.map((position) => position.accountId),
      ...data.history.map((trade) => trade.accountId)
    ]),
    instruments: uniqueSorted([
      ...data.orders.map((order) => order.instrument || order.pair),
      ...data.positions.map((position) => position.instrument || position.pair),
      ...data.history.map((trade) => trade.instrument || trade.pair)
    ]),
    roles: uniqueSorted(data.orders.map((order) => order.role)),
    statuses: uniqueSorted([
      ...data.orders.map((order) => order.status),
      ...data.positions.map((position) => position.status),
      ...data.history.map((trade) => trade.status)
    ])
  };
}

function filterOrderCenter(data: OrderCenterData, filters: OrdersFilters): OrderCenterData {
  return {
    ...data,
    history: data.history.filter((trade) => commonRowMatches(trade, filters, trade.openDate || trade.closeDate)),
    orders: data.orders.filter((order) => commonRowMatches(order, filters, order.createdAt) && matches(filters.role, order.role)),
    positions: data.positions.filter((position) => commonRowMatches(position, filters, position.openDate))
  };
}

function commonRowMatches(
  row: { accountId: string; instrument: string; pair: string; status: string },
  filters: OrdersFilters,
  timeValue: string
): boolean {
  return (
    matches(filters.account, row.accountId) &&
    matches(filters.instrument, row.instrument || row.pair) &&
    matches(filters.status, row.status) &&
    timeRangeMatches(filters.timeRange, timeValue)
  );
}

function matches(filterValue: string, rowValue: string): boolean {
  return filterValue === "all" || rowValue === filterValue;
}

function timeRangeMatches(filterValue: string, timeValue: string): boolean {
  if (filterValue === "all") {
    return true;
  }

  const timestamp = Date.parse(timeValue);
  if (!Number.isFinite(timestamp)) {
    return false;
  }

  const ageMs = Date.now() - timestamp;
  if (filterValue === "1h") {
    return ageMs <= 60 * 60 * 1000;
  }
  if (filterValue === "24h") {
    return ageMs <= 24 * 60 * 60 * 1000;
  }

  return ageMs <= 7 * 24 * 60 * 60 * 1000;
}

function uniqueSorted(values: string[]): string[] {
  return Array.from(new Set(values.filter(Boolean))).sort((left, right) => left.localeCompare(right));
}

function actionTitle(action: Exclude<ManualAction, false>): string {
  if (action.kind === "cancel") {
    return "Cancel order";
  }
  if (action.kind === "move-stop") {
    return "Move stop";
  }
  if (action.kind === "partial-close") {
    return "Partial close";
  }

  return "Close position";
}

function actionVerb(action: Exclude<ManualAction, false>): string {
  if (action.kind === "cancel") {
    return "Cancel order";
  }
  if (action.kind === "move-stop") {
    return "Move stop";
  }
  if (action.kind === "partial-close") {
    return "Partial close";
  }

  return "Close position";
}

function commandStatus(result: CommandResult): string {
  return result.statusText || "Command completed";
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
  return <span className={`status-pill ${statusTone(status)}`}>{status || "--"}</span>;
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
          <dt>last_execution_event_at</dt>
          <dd>{quality.last_execution_event_at || "unavailable"}</dd>
        </div>
        <div>
          <dt>projection_lag_ms</dt>
          <dd>{quality.projection_lag_ms}</dd>
        </div>
        <div>
          <dt>reconciliation</dt>
          <dd>{quality.reconciliation_state}</dd>
        </div>
        <div>
          <dt>stale</dt>
          <dd>{String(quality.stale)}</dd>
        </div>
      </dl>
      {quality.reason && <p className="quality-reason">{quality.reason}</p>}
    </section>
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
