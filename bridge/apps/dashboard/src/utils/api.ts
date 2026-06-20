import { formatCurrency, formatPercent } from "./format";

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL || import.meta.env.VITE_API_BASE || "";
export const AUTH_TOKEN_STORAGE_KEY = "hermes.auth.token";

export type Severity = "info" | "warning" | "critical";

export type DataSourceState = {
  data_source: string;
  snapshot_id: string;
  generated_at: string;
  last_execution_event_at: string | null;
  projection_lag_ms: number;
  stale: boolean;
  missing_nodes: string[];
  reconciliation_state: "healthy" | "degraded" | "failed";
  source: string;
  status: string;
  reason: string;
  degraded: boolean;
  raw: Record<string, unknown>;
};

export type Kpi = {
  label: string;
  value: string;
  delta: string;
  tone: "good" | "warning" | "danger" | "muted";
};

export type BotStatus = {
  name: string;
  status: string;
  pairCount: number;
  openTrades: number;
  lastHeartbeat: string;
};

export type RiskLamp = {
  label: string;
  status: string;
  detail: string;
};

export type SafetyState = {
  modeLabel: string;
  dataFreshness: string;
  operatorLane: string;
  lastSync: string;
  riskState: string;
};

export type ExposureBucket = {
  label: string;
  value: string;
  detail: string;
  usagePct: number;
  status: string;
};

export type EventLog = {
  id: string;
  time: string;
  severity: Severity;
  source: string;
  message: string;
};

export type Position = {
  id: string;
  pair: string;
  side: "long" | "short";
  leverage: number;
  entry: number;
  mark: number;
  pnl: number;
  pnlPercent: number;
  size: number;
  stopLoss: number | false;
  takeProfit: number | false;
  rMultiple: number;
  signalId: string;
  freqtradeTradeId: string;
  rawSignal: string;
  structuredSignal: Record<string, unknown>;
  orders: Record<string, unknown>[];
  fills: Record<string, unknown>[];
  auditTimeline: Record<string, unknown>[];
  anomaly: string | false;
};

export type DashboardOverview = {
  account: {
    equity: number;
    available: number;
    marginUsed: number;
    dailyPnl: number;
  };
  safety: SafetyState;
  exposure: ExposureBucket[];
  kpis: Kpi[];
  bots: BotStatus[];
  riskLamps: RiskLamp[];
  events: EventLog[];
  positions: Position[];
  dataSource: DataSourceState;
};

export type RiskMetric = {
  label: string;
  value: string;
  limit: string;
  status: string;
};

export type PairLock = {
  pair: string;
  reason: string;
  until: string;
  owner: string;
};

export type RiskOverview = {
  metrics: RiskMetric[];
  blockingReasons: string[];
  pairLocks: PairLock[];
  dataSource: DataSourceState;
};

export type DailyReport = {
  date: string;
  account: {
    equity: number;
    netPnl: number;
    maxDrawdown: number;
    volume: number;
  };
  performance: {
    trades: number;
    winRate: number;
    profitFactor: number;
    avgR: number;
  };
  funnel: {
    scanned: number;
    signaled: number;
    entered: number;
    closed: number;
  };
  riskEvents: EventLog[];
  positions: Position[];
  dataSource: DataSourceState;
};

export type OrderCenterPosition = {
  id: string;
  pair: string;
  side: string;
  amount: number;
  stakeAmount: number;
  entry: number;
  current: number;
  pnl: number;
  pnlPct: number;
  leverage: number;
  openDate: string;
  status: string;
};

export type OrderCenterOrder = {
  id: string;
  pair: string;
  side: string;
  type: string;
  status: string;
  price: number;
  amount: number;
  filled: number;
  remaining: number;
  createdAt: string;
  tradeId: string;
};

export type OrderCenterTrade = {
  id: string;
  pair: string;
  side: string;
  status: string;
  openRate: number;
  closeRate: number;
  amount: number;
  pnl: number;
  pnlPct: number;
  openDate: string;
  closeDate: string;
  ordersCount: number;
};

export type OrderCenterSummary = {
  realizedPnl: number;
  openPnl: number;
  totalPnl: number;
  openPositionCount: number;
  pendingOrderCount: number;
  historyCount: number;
  winCount: number;
  lossCount: number;
};

export type OrderCenterData = {
  positions: OrderCenterPosition[];
  orders: OrderCenterOrder[];
  history: OrderCenterTrade[];
  summary: OrderCenterSummary;
  dataSource: DataSourceState;
};

export type SignalReviewMediaItem = {
  index: number;
  mimeType: string;
};

export type SignalReviewItem = {
  signalId: string;
  pair: string;
  side: string;
  status: string;
  receivedAt: string;
  rawText: string;
  reasonCodes: string[];
  entryMode: string;
  entryPrice: number;
  stopLoss: number;
  takeProfits: number[];
  leverage: string;
  media: SignalReviewMediaItem[];
  classification: {
    conclusion: string;
    confidence: string;
    reasonCodes: string[];
    proposalTypes: string[];
  };
};

export type SignalReviewProposal = {
  proposalId: string;
  type: string;
  signalId: string;
  title: string;
  detail: string;
  proposedAction: string;
  status: string;
  classification: SignalReviewItem["classification"];
};

export type SignalReviewData = {
  signals: SignalReviewItem[];
  proposals: SignalReviewProposal[];
  dataSource: DataSourceState;
};

export type CommandResult = {
  commandId: string;
  complete: boolean;
  pendingNodes: string[];
  acknowledgedNodes: string[];
  failedNodes: string[];
  statusText: string;
};

type JsonResult = {
  payload: Record<string, unknown>;
  quality: DataSourceState;
};

export type LoginResult = {
  token: string;
  role: string;
};

function apiUrl(path: string): string {
  if (!apiBaseUrl) {
    return path;
  }

  return `${apiBaseUrl}${path}`;
}

export function isAuthDisabled(): boolean {
  return import.meta.env.VITE_AUTH_DISABLED === "true";
}

export function getStoredAuthToken(): string {
  if (isAuthDisabled()) {
    return "";
  }

  const token = localStorage.getItem(AUTH_TOKEN_STORAGE_KEY) || sessionStorage.getItem(AUTH_TOKEN_STORAGE_KEY) || "";
  if (!token) {
    return "";
  }

  if (!storedTokenIsValid(token)) {
    clearAuthToken();
    return "";
  }

  return token;
}

export function storeAuthToken(token: string): void {
  localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, token);
  sessionStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
}

export function clearAuthToken(): void {
  localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
  sessionStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
}

export async function login(username: string, password: string): Promise<LoginResult | false> {
  try {
    const response = await fetch(apiUrl("/api/auth/login"), {
      body: JSON.stringify({ username, password }),
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json"
      },
      method: "POST"
    });

    if (!response.ok) {
      return false;
    }

    const payload = asRecord(await response.json());
    const token = asString(payload.access_token, "");
    if (!token) {
      return false;
    }

    return {
      token,
      role: asString(payload.role, "")
    };
  } catch {
    return false;
  }
}

export function dashboardStreamUrl(): string {
  return apiUrl("/v1/stream");
}

export function emptyQuality(reason = "not loaded"): DataSourceState {
  const raw: Pick<
    DataSourceState,
    | "data_source"
    | "generated_at"
    | "last_execution_event_at"
    | "missing_nodes"
    | "projection_lag_ms"
    | "reconciliation_state"
    | "snapshot_id"
    | "stale"
  > = {
    data_source: "control_plane",
    generated_at: "",
    last_execution_event_at: null,
    missing_nodes: [],
    projection_lag_ms: 0,
    reconciliation_state: "degraded",
    snapshot_id: "",
    stale: true
  };

  return qualityFromRecord(raw, reason);
}

export function getEmptyDashboardOverview(reason = "loading"): DashboardOverview {
  return dashboardFromParts(emptyQuality(reason), {}, [], [], [], [], [], {});
}

export function getEmptyRiskOverview(reason = "loading"): RiskOverview {
  return riskFromPayload({}, emptyQuality(reason));
}

export function getEmptyDailyReport(date: string, reason = "loading"): DailyReport {
  return reportFromPayload({}, date, emptyQuality(reason));
}

export function getEmptyOrderCenter(reason = "loading"): OrderCenterData {
  return orderCenterFromPayloads({}, {}, {}, emptyQuality(reason));
}

export function getEmptySignalReview(reason = "loading"): SignalReviewData {
  return signalReviewFromPayloads({}, {}, emptyQuality(reason));
}

async function getJson(path: string): Promise<JsonResult> {
  try {
    const response = await fetch(apiUrl(path), {
      headers: requestHeaders({
        Accept: "application/json"
      })
    });

    if (!response.ok) {
      return { payload: {}, quality: unavailableQuality(path) };
    }

    const payload = asRecord(await response.json());
    return { payload, quality: qualityFromRecord(payload, "") };
  } catch {
    return { payload: {}, quality: unavailableQuality(path) };
  }
}

async function postJson(path: string, body: Record<string, unknown>): Promise<unknown> {
  try {
    const response = await fetch(apiUrl(path), {
      body: JSON.stringify(body),
      headers: requestHeaders({
        Accept: "application/json",
        "Content-Type": "application/json"
      }),
      method: "POST"
    });

    if (!response.ok) {
      return false;
    }

    return await response.json();
  } catch {
    return false;
  }
}

async function getText(path: string): Promise<string | false> {
  try {
    const response = await fetch(apiUrl(path), {
      headers: requestHeaders({
        Accept: "text/plain"
      })
    });

    if (!response.ok) {
      return false;
    }

    return await response.text();
  } catch {
    return false;
  }
}

async function getBlob(path: string): Promise<Blob | false> {
  try {
    const response = await fetch(apiUrl(path), {
      headers: requestHeaders({
        Accept: "image/*"
      })
    });

    if (!response.ok) {
      return false;
    }

    return await response.blob();
  } catch {
    return false;
  }
}

function requestHeaders(headers: Record<string, string>): Record<string, string> {
  const token = getStoredAuthToken();
  if (!token) {
    return headers;
  }

  return {
    ...headers,
    Authorization: `Bearer ${token}`
  };
}

function storedTokenIsValid(token: string): boolean {
  const segments = token.split(".");
  if (segments.length !== 3) {
    return true;
  }

  try {
    const payload = JSON.parse(base64UrlDecode(segments[1])) as Record<string, unknown>;
    const exp = payload.exp;
    return typeof exp === "number" && exp > Math.floor(Date.now() / 1000);
  } catch {
    return false;
  }
}

function base64UrlDecode(value: string): string {
  const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
  const padding = "=".repeat((4 - (normalized.length % 4)) % 4);
  return atob(`${normalized}${padding}`);
}

function asRecord(value: unknown): Record<string, unknown> {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }

  return {};
}

function asArray(value: unknown): unknown[] {
  if (Array.isArray(value)) {
    return value;
  }

  return [];
}

function asRecordArray(value: unknown): Record<string, unknown>[] {
  return asArray(value).map((item) => asRecord(item));
}

function asNumber(value: unknown, fallback = 0): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }

  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }

  return fallback;
}

function asString(value: unknown, fallback = ""): string {
  if (typeof value === "string" && value.length > 0) {
    return value;
  }

  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }

  return fallback;
}

function asStringArray(value: unknown): string[] {
  return asArray(value).map((item) => asString(item, "")).filter(Boolean);
}

function firstNumber(values: unknown[], fallback = 0): number {
  for (const value of values) {
    const normalized = asNumber(value, Number.NaN);
    if (Number.isFinite(normalized)) {
      return normalized;
    }
  }

  return fallback;
}

function firstString(values: unknown[], fallback = ""): string {
  for (const value of values) {
    const normalized = asString(value, "");
    if (normalized) {
      return normalized;
    }
  }

  return fallback;
}

function qualityFromRecord(payload: Record<string, unknown>, reason: string): DataSourceState {
  const reconciliation = asString(payload.reconciliation_state, "degraded");
  const reconciliationState = reconciliation === "healthy" || reconciliation === "failed" ? reconciliation : "degraded";
  const stale = payload.stale === true;
  const source = asString(payload.data_source, "control_plane");
  const status = stale ? "stale" : reconciliationState;
  const raw: Pick<
    DataSourceState,
    | "data_source"
    | "generated_at"
    | "last_execution_event_at"
    | "missing_nodes"
    | "projection_lag_ms"
    | "reconciliation_state"
    | "snapshot_id"
    | "stale"
  > = {
    data_source: source,
    generated_at: asString(payload.generated_at, ""),
    last_execution_event_at: typeof payload.last_execution_event_at === "string" ? payload.last_execution_event_at : null,
    missing_nodes: asStringArray(payload.missing_nodes),
    projection_lag_ms: Math.max(0, Math.round(asNumber(payload.projection_lag_ms, 0))),
    reconciliation_state: reconciliationState,
    snapshot_id: asString(payload.snapshot_id, ""),
    stale
  };

  return {
    ...raw,
    source,
    status,
    reason,
    degraded: stale || reconciliationState !== "healthy" || raw.missing_nodes.length > 0,
    raw
  };
}

function unavailableQuality(path: string): DataSourceState {
  const raw = {
    data_source: "control_plane",
    generated_at: "",
    last_execution_event_at: null,
    missing_nodes: [],
    projection_lag_ms: 0,
    reconciliation_state: "failed",
    snapshot_id: "",
    stale: true
  };

  return qualityFromRecord(raw, `${path} unavailable`);
}

function combineQuality(items: DataSourceState[]): DataSourceState {
  const first = items[0] || emptyQuality("no reads");
  const missingNodes = Array.from(new Set(items.flatMap((item) => item.missing_nodes)));
  const stale = items.some((item) => item.stale);
  let reconciliationState: DataSourceState["reconciliation_state"] = "healthy";
  if (items.some((item) => item.reconciliation_state === "failed")) {
    reconciliationState = "failed";
  } else if (items.some((item) => item.reconciliation_state === "degraded")) {
    reconciliationState = "degraded";
  }

  return qualityFromRecord(
    {
      data_source: first.data_source,
      generated_at: first.generated_at,
      last_execution_event_at: first.last_execution_event_at,
      missing_nodes: missingNodes,
      projection_lag_ms: Math.max(...items.map((item) => item.projection_lag_ms), 0),
      reconciliation_state: reconciliationState,
      snapshot_id: first.snapshot_id,
      stale
    },
    items.map((item) => item.reason).filter(Boolean).join("; ")
  );
}

function getRows(payload: Record<string, unknown>, keys: string[]): Record<string, unknown>[] {
  for (const key of keys) {
    const value = payload[key];
    if (Array.isArray(value)) {
      return value.map((item) => asRecord(item));
    }
  }

  const data = asRecord(payload.data);
  for (const key of keys) {
    const value = data[key];
    if (Array.isArray(value)) {
      return value.map((item) => asRecord(item));
    }
  }

  return [];
}

function dashboardFromParts(
  quality: DataSourceState,
  accountPayload: Record<string, unknown>,
  nodes: Record<string, unknown>[],
  orders: Record<string, unknown>[],
  positionsPayload: Record<string, unknown>[],
  trades: Record<string, unknown>[],
  messages: Record<string, unknown>[],
  riskPayload: Record<string, unknown>
): DashboardOverview {
  const account = getRows(accountPayload, ["accounts"])[0] || asRecord(accountPayload.account);
  const equity = firstNumber([account.equity, account.balance, account.total_equity]);
  const available = firstNumber([account.available, account.free_balance, account.cash]);
  const marginUsed = firstNumber([account.margin_used, account.initial_margin]);
  const dailyPnl = firstNumber([account.realized_pnl_today, account.daily_pnl]);
  const openPnl = positionsPayload.reduce((total, position) => total + firstNumber([position.pnl, position.unrealized_pnl]), 0);
  const openOrders = orders.length;
  const riskState = firstString([riskPayload.risk_state, riskPayload.state], quality.reconciliation_state);
  const positions = positionsPayload.map(positionFromApi);
  const todaySignals = messages.length;
  const todayExecutions = trades.length;

  return {
    account: {
      available,
      dailyPnl,
      equity,
      marginUsed
    },
    bots: nodes.map((node, index) => ({
      lastHeartbeat: firstString([node.last_heartbeat_at, node.heartbeat_at], "--"),
      name: firstString([node.name, node.node_id, node.id], `node-${index + 1}`),
      openTrades: firstNumber([node.open_position_count], positions.length),
      pairCount: firstNumber([node.instrument_count, node.pair_count]),
      status: `${firstString([node.trading_state, node.status], "unknown")} / ${firstString([node.readiness], "unknown")}`
    })),
    dataSource: quality,
    events: eventRows(messages, trades, orders).slice(0, 20),
    exposure: exposureFromValues(equity, marginUsed, available, todaySignals, todayExecutions, quality),
    kpis: [
      { label: "Equity", value: formatCurrency(equity), delta: `${positions.length} open positions`, tone: quality.stale ? "warning" : "good" },
      { label: "Open PnL", value: formatCurrency(openPnl), delta: `${openOrders} open orders`, tone: openPnl >= 0 ? "good" : "danger" },
      { label: "Margin Used", value: formatCurrency(marginUsed), delta: `${formatCurrency(available)} available`, tone: "warning" },
      { label: "Messages", value: `${todaySignals}`, delta: `${todayExecutions} trades`, tone: "muted" }
    ],
    positions,
    riskLamps: [
      { label: "Risk State", status: riskState, detail: riskState },
      { label: "Reconciliation", status: quality.reconciliation_state, detail: `stale=${String(quality.stale)}` }
    ],
    safety: {
      dataFreshness: `${quality.data_source} ${quality.status}`,
      lastSync: quality.generated_at || "unavailable",
      modeLabel: firstString([riskPayload.run_mode], "CONTROL-PLANE"),
      operatorLane: "Risk admin approval",
      riskState
    }
  };
}

function exposureFromValues(
  equity: number,
  marginUsed: number,
  available: number,
  signalCount: number,
  executionCount: number,
  quality: DataSourceState
): ExposureBucket[] {
  const marginUsagePct = equity > 0 ? Math.min(100, Math.max(0, (marginUsed / equity) * 100)) : 0;
  const signalExecutionPct = signalCount > 0 ? Math.min(100, Math.max(0, (executionCount / signalCount) * 100)) : 0;
  return [
    {
      detail: `${formatCurrency(marginUsed)} margin / ${formatCurrency(available)} available`,
      label: "Exposure usage",
      status: marginUsagePct >= 80 ? "critical" : "normal",
      usagePct: marginUsagePct,
      value: formatPercent(marginUsagePct)
    },
    {
      detail: `${executionCount} trades / ${signalCount} messages`,
      label: "Message execution",
      status: "normal",
      usagePct: signalExecutionPct,
      value: formatPercent(signalExecutionPct)
    },
    {
      detail: `missing_nodes=${quality.missing_nodes.length}`,
      label: "Projection health",
      status: quality.reconciliation_state,
      usagePct: quality.stale ? 0 : 100,
      value: quality.status
    }
  ];
}

function eventRows(
  messages: Record<string, unknown>[],
  trades: Record<string, unknown>[],
  orders: Record<string, unknown>[]
): EventLog[] {
  const rows = messages.length > 0 ? messages : [...trades, ...orders];
  return rows.map((row, index) => ({
    id: firstString([row.event_id, row.message_id, row.trade_id, row.order_id, row.id], `event-${index}`),
    message: firstString([row.summary, row.message, row.raw_message, row.status], "control-plane event"),
    severity: row.severity === "critical" || row.severity === "warning" ? row.severity : "info",
    source: firstString([row.source, row.event_type, row.type], "control-plane"),
    time: firstString([row.generated_at, row.created_at, row.timestamp, row.occurred_at], "--").slice(11, 19) || "--"
  }));
}

function positionFromApi(value: Record<string, unknown>, index: number): Position {
  const side = firstString([value.side, value.position_side], "long").toLowerCase();
  const normalizedSide = side === "short" || side === "sell" ? "short" : "long";
  const stopLoss = firstNumber([value.stop_loss, value.stop_loss_price], Number.NaN);
  const takeProfit = firstNumber([value.take_profit, value.next_take_profit_price], Number.NaN);
  const positionId = firstString([value.position_id, value.trade_id, value.id], `position-${index}`);

  return {
    anomaly: Number.isFinite(stopLoss) && stopLoss > 0 ? false : "Missing SL",
    auditTimeline: asRecordArray(value.audit_timeline),
    entry: firstNumber([value.entry_price, value.entry_rate, value.open_rate]),
    fills: asRecordArray(value.fills),
    freqtradeTradeId: firstString([value.trade_id, value.venue_position_id], positionId),
    id: positionId,
    leverage: firstNumber([value.leverage], 1),
    mark: firstNumber([value.mark_price, value.current_rate, value.current_price]),
    orders: asRecordArray(value.orders),
    pair: firstString([value.instrument_symbol, value.pair, value.symbol], "UNAVAILABLE"),
    pnl: firstNumber([value.unrealized_pnl, value.pnl]),
    pnlPercent: firstNumber([value.pnl_pct, value.profit_pct]),
    rMultiple: firstNumber([value.r_multiple]),
    rawSignal: firstString([value.raw_signal, value.raw_text]),
    side: normalizedSide,
    signalId: firstString([value.signal_id, value.intent_id]),
    size: firstNumber([value.size, value.amount, value.quantity]),
    stopLoss: Number.isFinite(stopLoss) && stopLoss > 0 ? stopLoss : false,
    structuredSignal: asRecord(value.structured_signal),
    takeProfit: Number.isFinite(takeProfit) && takeProfit > 0 ? takeProfit : false
  };
}

function orderPositionFromApi(value: Record<string, unknown>, index: number): OrderCenterPosition {
  const side = firstString([value.side, value.position_side], "long").toLowerCase();
  const entry = firstNumber([value.entry_price, value.entry_rate, value.open_rate]);
  return {
    amount: firstNumber([value.amount, value.size, value.quantity]),
    current: firstNumber([value.mark_price, value.current_rate, value.current_price], entry),
    entry,
    id: firstString([value.position_id, value.trade_id, value.id], `position-${index}`),
    leverage: firstNumber([value.leverage], 1),
    openDate: firstString([value.opened_at, value.created_at, value.open_date], "--"),
    pair: firstString([value.instrument_symbol, value.pair, value.symbol], "UNAVAILABLE"),
    pnl: firstNumber([value.unrealized_pnl, value.pnl]),
    pnlPct: firstNumber([value.pnl_pct, value.profit_pct]),
    side: side === "short" || side === "sell" ? "short" : "long",
    stakeAmount: firstNumber([value.notional, value.stake_amount]),
    status: firstString([value.status], "open")
  };
}

function orderFromApi(value: Record<string, unknown>, index: number): OrderCenterOrder {
  const amount = firstNumber([value.amount, value.quantity, value.order_amount]);
  const filled = firstNumber([value.filled, value.filled_amount], 0);
  return {
    amount,
    createdAt: firstString([value.created_at, value.submitted_at], "--"),
    filled,
    id: firstString([value.order_id, value.venue_order_id, value.id], `order-${index}`),
    pair: firstString([value.instrument_symbol, value.pair, value.symbol], "UNAVAILABLE"),
    price: firstNumber([value.price, value.limit_price, value.average_price]),
    remaining: firstNumber([value.remaining, value.remaining_amount], Math.max(0, amount - filled)),
    side: firstString([value.side], "--"),
    status: firstString([value.status], "--"),
    tradeId: firstString([value.trade_id, value.position_id]),
    type: firstString([value.order_type, value.type], "--")
  };
}

function tradeFromApi(value: Record<string, unknown>, index: number): OrderCenterTrade {
  const side = firstString([value.side, value.position_side], "long").toLowerCase();
  return {
    amount: firstNumber([value.amount, value.size, value.quantity]),
    closeDate: firstString([value.closed_at, value.close_date], "--"),
    closeRate: firstNumber([value.close_price, value.close_rate, value.exit_rate]),
    id: firstString([value.trade_id, value.id], `trade-${index}`),
    openDate: firstString([value.opened_at, value.created_at, value.open_date], "--"),
    openRate: firstNumber([value.open_price, value.entry_price, value.open_rate]),
    ordersCount: asRecordArray(value.orders).length,
    pair: firstString([value.instrument_symbol, value.pair, value.symbol], "UNAVAILABLE"),
    pnl: firstNumber([value.realized_pnl, value.pnl, value.close_profit_abs]),
    pnlPct: firstNumber([value.pnl_pct, value.profit_pct]),
    side: side === "short" || side === "sell" ? "short" : "long",
    status: firstString([value.status], "closed")
  };
}

function orderCenterFromPayloads(
  positionsPayload: Record<string, unknown>,
  ordersPayload: Record<string, unknown>,
  tradesPayload: Record<string, unknown>,
  quality: DataSourceState
): OrderCenterData {
  const positions = getRows(positionsPayload, ["positions"]).map(orderPositionFromApi);
  const orders = getRows(ordersPayload, ["orders"]).map(orderFromApi);
  const history = getRows(tradesPayload, ["trades"]).map(tradeFromApi);
  const realizedPnl = history.reduce((total, trade) => total + trade.pnl, 0);
  const openPnl = positions.reduce((total, position) => total + position.pnl, 0);

  return {
    dataSource: quality,
    history,
    orders,
    positions,
    summary: {
      historyCount: history.length,
      lossCount: history.filter((trade) => trade.pnl < 0).length,
      openPnl,
      openPositionCount: positions.length,
      pendingOrderCount: orders.length,
      realizedPnl,
      totalPnl: realizedPnl + openPnl,
      winCount: history.filter((trade) => trade.pnl > 0).length
    }
  };
}

function riskFromPayload(payload: Record<string, unknown>, quality: DataSourceState): RiskOverview {
  return {
    blockingReasons: asStringArray(payload.blocking_reasons),
    dataSource: quality,
    metrics: [
      { label: "单笔风险", limit: "100%", status: "tracked", value: formatPercent(asNumber(payload.single_trade_risk_usage_pct)) },
      { label: "总风险", limit: "100%", status: "tracked", value: formatPercent(asNumber(payload.total_open_risk_usage_pct)) },
      { label: "日亏损", limit: "100%", status: "tracked", value: formatPercent(asNumber(payload.daily_loss_usage_pct)) },
      { label: "无 SL", limit: "0", status: asNumber(payload.no_sl_trade_count) > 0 ? "critical" : "normal", value: `${asNumber(payload.no_sl_trade_count)}` },
      { label: "高杠杆", limit: "0", status: asNumber(payload.high_leverage_trade_count) > 0 ? "warning" : "normal", value: `${asNumber(payload.high_leverage_trade_count)}` }
    ],
    pairLocks: getRows(payload, ["pair_locks"]).map((lock) => ({
      owner: firstString([lock.owner], "Risk Guard"),
      pair: firstString([lock.instrument_symbol, lock.pair], "UNAVAILABLE"),
      reason: firstString([lock.reason, lock.source], "risk rule"),
      until: firstString([lock.expires_at, lock.until], "--")
    }))
  };
}

function reportFromPayload(payload: Record<string, unknown>, date: string, quality: DataSourceState): DailyReport {
  const account = asRecord(payload.account);
  const trades = asRecord(payload.trades);
  const signals = asRecord(payload.signals);
  const riskEvents = asRecord(payload.risk_events);
  const openPositions = asRecord(payload.open_positions);
  const closedTrades = firstNumber([trades.closed_today_count, trades.closed_count]);
  const wins = asNumber(trades.win_count);

  return {
    account: {
      equity: asNumber(account.equity),
      maxDrawdown: asNumber(account.max_drawdown_pct),
      netPnl: asNumber(account.realized_pnl_today) + asNumber(account.unrealized_pnl),
      volume: asNumber(openPositions.notional)
    },
    dataSource: quality,
    date,
    funnel: {
      closed: closedTrades,
      entered: firstNumber([signals.executed_count, signals.accepted_count]),
      scanned: asNumber(signals.received_count),
      signaled: asNumber(signals.accepted_count) + asNumber(signals.rejected_count)
    },
    performance: {
      avgR: asNumber(trades.avg_r),
      profitFactor: asNumber(trades.profit_factor),
      trades: closedTrades,
      winRate: closedTrades > 0 ? (wins / closedTrades) * 100 : 0
    },
    positions: getRows(payload, ["positions", "open_positions"]).map(positionFromApi),
    riskEvents: [
      {
        id: "risk-events",
        message: `${asNumber(riskEvents.count)} risk events, ${asNumber(riskEvents.blocking_count)} blocking`,
        severity: asNumber(riskEvents.blocking_count) > 0 ? "warning" : "info",
        source: "Risk",
        time: "--"
      }
    ]
  };
}

function signalClassificationFromApi(value: Record<string, unknown>): SignalReviewItem["classification"] {
  return {
    confidence: asString(value.confidence, "unknown"),
    conclusion: firstString([value.conclusion, value.action, value.message_type], "needs_review"),
    proposalTypes: asStringArray(value.proposal_types),
    reasonCodes: asStringArray(value.reason_codes)
  };
}

function signalReviewFromPayloads(
  decisionsPayload: Record<string, unknown>,
  messagesPayload: Record<string, unknown>,
  quality: DataSourceState
): SignalReviewData {
  const messages = getRows(messagesPayload, ["messages"]);
  const decisions = getRows(decisionsPayload, ["decisions"]);
  const signals = decisions.map((decision, index) => {
    const intent = asRecord(decision.intent);
    const entry = asRecord(intent.entry);
    const classification = asRecord(decision.classification);
    const message = messages.find((item) => asString(item.decision_id) === asString(decision.decision_id)) || {};
    return {
      classification: signalClassificationFromApi(classification),
      entryMode: asString(entry.type, "none"),
      entryPrice: asNumber(entry.price),
      leverage: `${asNumber(intent.leverage)}x`,
      media: getRows(message, ["media"]).map((item, mediaIndex) => ({
        index: asNumber(item.index, mediaIndex),
        mimeType: asString(item.mime_type)
      })),
      pair: asString(intent.instrument_symbol, "UNAVAILABLE"),
      rawText: firstString([message.raw_text, message.raw_message]),
      reasonCodes: asStringArray(classification.ambiguity_reasons),
      receivedAt: firstString([message.received_at, decision.created_at], "--"),
      side: asString(intent.side, "--"),
      signalId: firstString([decision.decision_id, decision.raw_message_id], `decision-${index}`),
      status: asString(classification.action, "needs_review"),
      stopLoss: asNumber(intent.stop_loss),
      takeProfits: asArray(intent.take_profits).map((item) => asNumber(item, Number.NaN)).filter(Number.isFinite)
    };
  });

  return {
    dataSource: quality,
    proposals: [],
    signals
  };
}

function commandResultFromPayload(payload: unknown): CommandResult {
  if (payload === false) {
    return {
      acknowledgedNodes: [],
      commandId: "",
      complete: false,
      failedNodes: [],
      pendingNodes: [],
      statusText: "Command request failed"
    };
  }

  const result = asRecord(payload);
  const targetNodes = asStringArray(result.target_nodes);
  const acks = asRecordArray(result.acks);
  const acknowledgedNodes = acks
    .filter((ack) => {
      const status = asString(ack.status).toLowerCase();
      return status === "acked" || status === "accepted" || status === "completed" || status === "succeeded";
    })
    .map((ack) => asString(ack.node_id))
    .filter(Boolean);
  const failedNodes = acks
    .filter((ack) => {
      const status = asString(ack.status).toLowerCase();
      return status === "failed" || status === "rejected" || status === "error";
    })
    .map((ack) => asString(ack.node_id))
    .filter(Boolean);
  const pendingNodes = targetNodes.filter((node) => !acknowledgedNodes.includes(node) && !failedNodes.includes(node));
  const complete = targetNodes.length > 0 ? pendingNodes.length === 0 && failedNodes.length === 0 : asString(result.status) === "completed";
  let statusText = "Command completed";

  if (failedNodes.length > 0) {
    statusText = `Command failed on ${failedNodes.length} node${failedNodes.length === 1 ? "" : "s"}: ${failedNodes.join(", ")}`;
  } else if (!complete && pendingNodes.length > 0) {
    statusText = `Waiting for ${pendingNodes.length} node ack: ${pendingNodes.join(", ")}`;
  } else if (!complete) {
    statusText = "Command pending";
  }

  return {
    acknowledgedNodes,
    commandId: asString(result.command_id),
    complete,
    failedNodes,
    pendingNodes,
    statusText
  };
}

async function issueCommand(type: string, args: Record<string, unknown>): Promise<CommandResult> {
  const payload = await postJson("/v1/commands", {
    args,
    type
  });
  return commandResultFromPayload(payload);
}

export async function fetchDashboardOverview(): Promise<DashboardOverview> {
  const [accounts, nodes, orders, positions, trades, messages, risk] = await Promise.all([
    getJson("/v1/accounts"),
    getJson("/v1/nodes"),
    getJson("/v1/orders?status=open"),
    getJson("/v1/positions"),
    getJson("/v1/trades"),
    getJson("/v1/messages?limit=20"),
    getJson("/v1/risk/state")
  ]);
  const quality = combineQuality([accounts.quality, nodes.quality, orders.quality, positions.quality, trades.quality, messages.quality, risk.quality]);

  return dashboardFromParts(
    quality,
    accounts.payload,
    getRows(nodes.payload, ["nodes"]),
    getRows(orders.payload, ["orders"]),
    getRows(positions.payload, ["positions"]),
    getRows(trades.payload, ["trades"]),
    getRows(messages.payload, ["messages"]),
    risk.payload
  );
}

export async function fetchOrderCenter(): Promise<OrderCenterData> {
  const [positions, orders, trades] = await Promise.all([
    getJson("/v1/positions"),
    getJson("/v1/orders"),
    getJson("/v1/trades")
  ]);
  const quality = combineQuality([positions.quality, orders.quality, trades.quality]);
  return orderCenterFromPayloads(positions.payload, orders.payload, trades.payload, quality);
}

export async function fetchSignalReview(): Promise<SignalReviewData> {
  const [decisions, messages] = await Promise.all([
    getJson("/v1/risk/decisions"),
    getJson("/v1/messages?status=needs_review")
  ]);
  return signalReviewFromPayloads(decisions.payload, messages.payload, combineQuality([decisions.quality, messages.quality]));
}

export async function fetchSignalMediaBlob(signalId: string, index: number): Promise<Blob | false> {
  return getBlob(`/v1/messages/${encodeURIComponent(signalId)}/media/${index}`);
}

export async function approveReviewSignal(signalId: string, reason: string): Promise<boolean> {
  const payload = await postJson(`/v1/risk/decisions/${encodeURIComponent(signalId)}/approve`, { reason });
  return payload !== false;
}

export async function rejectReviewSignal(signalId: string, reason: string): Promise<boolean> {
  const payload = await postJson(`/v1/risk/decisions/${encodeURIComponent(signalId)}/reject`, { reason });
  return payload !== false;
}

export async function approveReviewProposal(proposalId: string, reason: string): Promise<boolean> {
  const payload = await postJson(`/v1/risk/decisions/${encodeURIComponent(proposalId)}/approve`, { reason });
  return payload !== false;
}

export async function rejectReviewProposal(proposalId: string, reason: string): Promise<boolean> {
  const payload = await postJson(`/v1/risk/decisions/${encodeURIComponent(proposalId)}/reject`, { reason });
  return payload !== false;
}

export async function fetchRiskOverview(): Promise<RiskOverview> {
  const risk = await getJson("/v1/risk/state");
  return riskFromPayload(risk.payload, risk.quality);
}

export async function fetchDailyReport(date: string): Promise<DailyReport> {
  const report = await getJson(`/v1/reports/daily/${date}`);
  return reportFromPayload(report.payload, date, report.quality);
}

export async function activateKillSwitch(reason: string, closeAll: boolean, confirmationPhrase: string): Promise<CommandResult> {
  return issueCommand("halt", {
    close_all: closeAll,
    confirmation_phrase: confirmationPhrase,
    reason
  });
}

export async function closePosition(tradeId: string, reason: string, signalId: string): Promise<CommandResult> {
  return issueCommand("close_all", {
    position_id: tradeId,
    reason,
    scope: "position",
    signal_id: signalId
  });
}

export async function partialClosePosition(
  tradeId: string,
  amount: number,
  reason: string,
  signalId: string
): Promise<CommandResult> {
  return issueCommand("close_all", {
    amount,
    position_id: tradeId,
    reason,
    scope: "position_partial",
    signal_id: signalId
  });
}

export async function moveStopLoss(
  tradeId: string,
  stopLossPrice: number,
  reason: string,
  signalId: string
): Promise<CommandResult> {
  return issueCommand("move_stop_loss", {
    position_id: tradeId,
    reason,
    signal_id: signalId,
    stop_loss_price: stopLossPrice
  });
}

export async function pauseBot(reason: string): Promise<CommandResult> {
  return issueCommand("halt", { reason });
}

export async function resumeBot(reason: string): Promise<CommandResult> {
  return issueCommand("resume", { reason });
}

export async function lockPair(pair: string, reason: string): Promise<CommandResult> {
  return issueCommand("set_reducing", {
    instrument_symbol: pair,
    reason,
    scope: "instrument"
  });
}

export async function fetchDailyReportMarkdown(date: string): Promise<string | false> {
  return getText(`/v1/reports/daily/${date}/markdown`);
}

export async function fetchDailyReportVersions(date: string): Promise<Record<string, unknown>[]> {
  const result = await getJson(`/v1/reports/daily/${date}/versions`);
  return getRows(result.payload, ["versions"]);
}

export async function fetchDailyReportTelegramPreview(date: string): Promise<string | false> {
  const payload = await postJson(`/v1/reports/daily/${date}/telegram-preview`, {});
  const result = asRecord(payload);
  const text = asString(result.text, "");
  if (!text) {
    return false;
  }

  return text;
}

export async function createDailyReportSnapshot(date: string): Promise<Record<string, unknown> | false> {
  const payload = await postJson("/v1/reports/daily/snapshot", { date });
  const result = asRecord(payload);
  if (!asString(result.snapshot_id)) {
    return false;
  }

  return result;
}
