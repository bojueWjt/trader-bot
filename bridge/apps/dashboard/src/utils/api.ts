import {
  DataSourceState,
  DashboardOverview,
  DailyReport,
  EventLog,
  ExposureBucket,
  Kpi,
  Position,
  RiskOverview,
  SafetyState,
  getMockDashboardOverview,
  getMockDailyReport,
  getMockRiskOverview
} from "../data/mockData";
import { formatCurrency, formatPercent } from "./format";

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL || "";
export const AUTH_TOKEN_STORAGE_KEY = "hermes.auth.token";

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

export type LoginResult = {
  token: string;
  role: string;
};

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
  return apiUrl("/api/dashboard/stream");
}

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

export type SignalReviewMediaItem = {
  index: number;
  mimeType: string;
};

export type SignalReviewProposal = {
  proposalId: string;
  type: string;
  signalId: string;
  title: string;
  detail: string;
  proposedAction: string;
  status: string;
  classification: {
    conclusion: string;
    confidence: string;
    reasonCodes: string[];
    proposalTypes: string[];
  };
};

export type SignalReviewData = {
  signals: SignalReviewItem[];
  proposals: SignalReviewProposal[];
  dataSource: DataSourceState;
};

async function getJson(path: string): Promise<unknown> {
  try {
    const response = await fetch(apiUrl(path), {
      headers: requestHeaders({
        Accept: "application/json"
      })
    });

    if (!response.ok) {
      return false;
    }

    const payload = await response.json();

    if (!payload) {
      return false;
    }

    return payload;
  } catch {
    return false;
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

    const payload = await response.json();
    if (!payload) {
      return false;
    }

    return payload;
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
    if (typeof exp !== "number") {
      return false;
    }

    return exp > Math.floor(Date.now() / 1000);
  } catch {
    return false;
  }
}

function base64UrlDecode(value: string): string {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
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

function asNumber(value: unknown, fallback: number): number {
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

function asRecordArray(value: unknown): Record<string, unknown>[] {
  return asArray(value).map((item) => asRecord(item));
}

function asString(value: unknown, fallback: string): string {
  if (typeof value === "string" && value.length > 0) {
    return value;
  }

  return fallback;
}

function statusIsFailure(status: string): boolean {
  if (/failed|unavailable|error|rejected/i.test(status)) {
    return true;
  }

  return false;
}

function actionAccepted(payload: unknown): boolean {
  if (payload === false) {
    return false;
  }

  const result = asRecord(payload);
  const status = asString(result.status, "");
  if (status && statusIsFailure(status)) {
    return false;
  }

  const nestedResult = asRecord(result.result);
  const nestedStatus = asString(nestedResult.status, "");
  if (nestedStatus && statusIsFailure(nestedStatus)) {
    return false;
  }

  return true;
}

function closeAllAccepted(payload: unknown): boolean {
  if (!actionAccepted(payload)) {
    return false;
  }

  const result = asRecord(payload);
  if (result.enabled !== true) {
    return false;
  }

  const closeAll = asRecord(result.close_all_result);
  const closeAllStatus = asString(closeAll.status, "");
  if (closeAllStatus && statusIsFailure(closeAllStatus)) {
    return false;
  }

  return true;
}

function asDisplayString(value: unknown, fallback: string): string {
  if (typeof value === "string" && value.length > 0) {
    return value;
  }

  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }

  return fallback;
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

function firstString(values: unknown[], fallback: string): string {
  for (const value of values) {
    const normalized = asDisplayString(value, "");
    if (normalized) {
      return normalized;
    }
  }

  return fallback;
}

function extractRecordArray(payload: unknown, keys: string[]): Record<string, unknown>[] {
  if (Array.isArray(payload)) {
    return payload.map((item) => asRecord(item));
  }

  const record = asRecord(payload);
  for (const key of keys) {
    const value = record[key];
    if (Array.isArray(value)) {
      return value.map((item) => asRecord(item));
    }
  }

  const result = record.result;
  if (result) {
    return extractRecordArray(result, keys);
  }

  const data = record.data;
  if (data) {
    return extractRecordArray(data, keys);
  }

  return [];
}

function dataSourceFromApi(value: Record<string, unknown>): DataSourceState {
  const source = asString(value.data_source, "provider");
  const status = asString(value.status, "ok");
  const reason = asString(value.status_reason, "");
  let degraded = false;

  if (/degraded|demo/i.test(status)) {
    degraded = true;
  }
  if (/demo/i.test(source)) {
    degraded = true;
  }

  return {
    source,
    status,
    reason,
    degraded
  };
}

function safetyFromApi(value: Record<string, unknown>): SafetyState {
  const runMode = asString(value.run_mode, "dry_run");
  const riskState = asString(value.risk_state, "normal");
  const source = asString(value.data_source, "provider");
  const status = asString(value.status, "ok");
  const reason = asString(value.status_reason, "");
  let modeLabel = "DRY-RUN LOCKED";

  if (/live/i.test(runMode)) {
    modeLabel = "LIVE READONLY";
  }

  let dataFreshness = `${source} ${status}`;
  if (reason) {
    dataFreshness = `${dataFreshness} ${reason}`;
  }

  return {
    modeLabel,
    dataFreshness,
    operatorLane: "Manual approval",
    lastSync: "API latest",
    riskState
  };
}

function exposureFromApi(value: Record<string, unknown>): ExposureBucket[] {
  const equity = asNumber(value.equity, 0);
  const marginUsed = asNumber(value.margin_used, 0);
  const freeBalance = asNumber(value.free_balance, 0);
  const todaySignals = asNumber(value.today_signal_count, 0);
  const todayExecutions = asNumber(value.today_execution_count, asNumber(value.today_executed_count, 0));
  let marginUsagePct = 0;
  let signalExecutionPct = 0;

  if (equity > 0) {
    marginUsagePct = Math.min(100, Math.max(0, (marginUsed / equity) * 100));
  }

  if (todaySignals > 0) {
    signalExecutionPct = Math.min(100, Math.max(0, (todayExecutions / todaySignals) * 100));
  }

  return [
    {
      label: "Exposure usage",
      value: formatPercent(marginUsagePct),
      detail: `${formatCurrency(marginUsed)} margin / ${formatCurrency(freeBalance)} free`,
      usagePct: marginUsagePct,
      status: marginUsagePct >= 80 ? "critical" : "normal"
    },
    {
      label: "Signal execution",
      value: formatPercent(signalExecutionPct),
      detail: `${todayExecutions} executed / ${todaySignals} signals`,
      usagePct: signalExecutionPct,
      status: "normal"
    },
    {
      label: "Freshness health",
      value: asString(value.status, "ok"),
      detail: asString(value.status_reason, "provider"),
      usagePct: 100,
      status: asString(value.status, "ok")
    }
  ];
}

function eventMessageFromApi(event: Record<string, unknown>): string {
  const explicit = asString(event.message, "");
  if (explicit) {
    return explicit;
  }

  const eventType = asDisplayString(event.event_type, asDisplayString(event.type, ""));
  const signalId = asDisplayString(event.signal_id, "");
  const tradeId = asDisplayString(event.trade_id, "");
  const botId = asDisplayString(event.bot_id, "");

  if (eventType && signalId && tradeId && botId) {
    return `${eventType}: ${signalId} / trade ${tradeId} / ${botId}`;
  }

  if (eventType && signalId) {
    return `${eventType}: ${signalId}`;
  }

  if (eventType) {
    return eventType;
  }

  return "Signal dashboard event";
}

function eventFromApi(value: unknown, index: number): EventLog {
  const event = asRecord(value);
  const kind = asString(event.event_kind, "");
  let severity: EventLog["severity"] = "info";
  if (kind === "risk") {
    severity = "warning";
  }

  return {
    id: asString(event.event_id, `api-event-${index}`),
    time: asString(event.timestamp, "--").slice(11, 19),
    severity,
    source: asString(event.event_kind, asString(event.type, "API")),
    message: eventMessageFromApi(event)
  };
}

function dedupeEventPayloads(values: unknown[]): unknown[] {
  const seen = new Set<string>();
  const deduped: unknown[] = [];

  values.forEach((value, index) => {
    const event = asRecord(value);
    const eventId = asDisplayString(event.event_id, "");
    let key = eventId;
    if (!key) {
      const eventType = asDisplayString(event.event_type, asDisplayString(event.type, ""));
      const signalId = asDisplayString(event.signal_id, "");
      const timestamp = asDisplayString(event.timestamp, asDisplayString(event.occurred_at, ""));
      key = `${eventType}:${signalId}:${timestamp}:${index}`;
    }

    if (seen.has(key)) {
      return;
    }

    seen.add(key);
    deduped.push(value);
  });

  return deduped;
}

function positionFromApi(value: unknown, index: number): Position {
  const trade = asRecord(value);
  const side = asString(trade.side, "long").toLowerCase();
  let normalizedSide: "long" | "short" = "long";

  if (side === "short") {
    normalizedSide = "short";
  }

  const stopLoss = asNumber(trade.stop_loss_price, 0);
  const takeProfit = asNumber(trade.next_take_profit_price, 0);
  let anomaly: string | false = false;
  if (stopLoss <= 0) {
    anomaly = "No SL";
  }

  const structuredSignal = asRecord(trade.structured_signal);

  return {
    id: asString(trade.trade_id, `api-trade-${index}`),
    pair: asString(trade.pair, "UNKNOWN/USDT"),
    side: normalizedSide,
    leverage: asNumber(trade.leverage, 1),
    entry: asNumber(trade.entry_rate, 0),
    mark: asNumber(trade.current_rate, 0),
    pnl: asNumber(trade.pnl, 0),
    pnlPercent: 0,
    size: asNumber(trade.size, 0),
    stopLoss: stopLoss > 0 ? stopLoss : false,
    takeProfit: takeProfit > 0 ? takeProfit : false,
    rMultiple: asNumber(trade.r_multiple, 0),
    signalId: asString(trade.signal_id, ""),
    freqtradeTradeId: asString(trade.trade_id, `api-trade-${index}`),
    rawSignal: asString(trade.raw_text, ""),
    structuredSignal,
    orders: asRecordArray(trade.orders),
    fills: asRecordArray(trade.fills),
    auditTimeline: asRecordArray(trade.audit_timeline),
    anomaly
  };
}

function dashboardFromApi(
  overviewPayload: unknown,
  tradesPayload: unknown,
  auditEventsPayload: unknown
): DashboardOverview | false {
  const overview = asRecord(overviewPayload);
  if (!Object.prototype.hasOwnProperty.call(overview, "bot_status")) {
    return false;
  }

  const equity = asNumber(overview.equity, 0);
  const freeBalance = asNumber(overview.free_balance, 0);
  const marginUsed = asNumber(overview.margin_used, 0);
  const dailyPnl = asNumber(overview.realized_pnl_today, 0);
  const unrealizedPnl = asNumber(overview.unrealized_pnl, 0);
  const openTrades = asNumber(overview.open_trade_count, 0);
  const openOrders = asNumber(overview.open_order_count, 0);
  const todaySignals = asNumber(overview.today_signal_count, 0);
  const todayExecutions = asNumber(overview.today_execution_count, asNumber(overview.today_executed_count, 0));
  const riskState = asString(overview.risk_state, "normal");
  const kpis: Kpi[] = [
    { label: "Equity", value: formatCurrency(equity), delta: `${openTrades} open trades`, tone: "good" },
    { label: "Open PnL", value: formatCurrency(unrealizedPnl), delta: `${openOrders} open orders`, tone: "good" },
    { label: "Margin Used", value: formatCurrency(marginUsed), delta: `${formatCurrency(freeBalance)} free`, tone: "warning" },
    { label: "Today Signals", value: `${todaySignals}`, delta: `${todayExecutions} executed`, tone: "muted" }
  ];
  const positions = asArray(tradesPayload).map(positionFromApi);
  const auditEvents = asArray(auditEventsPayload);
  let events = asArray(overview.recent_events);
  if (auditEvents.length > 0) {
    events = auditEvents;
  }

  return {
    dataSource: dataSourceFromApi(overview),
    safety: safetyFromApi(overview),
    exposure: exposureFromApi(overview),
    account: {
      equity,
      available: freeBalance,
      marginUsed,
      dailyPnl
    },
    kpis,
    bots: [
      {
        name: "Hermes SignalStrategy",
        status: `${asString(overview.bot_status, "unknown")} / ${asString(overview.run_mode, "unknown")}`,
        pairCount: positions.length,
        openTrades,
        lastHeartbeat: "API"
      }
    ],
    riskLamps: [
      { label: "Risk State", status: riskState, detail: riskState },
      { label: "Signal Funnel", status: "normal", detail: `${todayExecutions}/${todaySignals} executed` }
    ],
    events: dedupeEventPayloads(events).slice(0, 20).map(eventFromApi),
    positions
  };
}

function riskFromApi(payload: unknown): RiskOverview | false {
  const risk = asRecord(payload);
  if (!Object.prototype.hasOwnProperty.call(risk, "single_trade_risk_usage_pct")) {
    return false;
  }

  const blockingReasons = asArray(risk.blocking_reasons).map((item) => asString(item, ""));
  return {
    metrics: [
      { label: "单笔风险", value: formatPercent(asNumber(risk.single_trade_risk_usage_pct, 0)), limit: "100%", status: "tracked" },
      { label: "总风险", value: formatPercent(asNumber(risk.total_open_risk_usage_pct, 0)), limit: "100%", status: "tracked" },
      { label: "日亏损", value: formatPercent(asNumber(risk.daily_loss_usage_pct, 0)), limit: "100%", status: "tracked" },
      { label: "无 SL", value: `${asNumber(risk.no_sl_trade_count, 0)}`, limit: "0", status: "critical" },
      { label: "高杠杆", value: `${asNumber(risk.high_leverage_trade_count, 0)}`, limit: "0", status: "warning" }
    ],
    blockingReasons,
    pairLocks: asArray(risk.pair_locks).map((item) => {
      const lock = asRecord(item);
      return {
        pair: asString(lock.pair, "UNKNOWN/USDT"),
        reason: asString(lock.reason, asString(lock.source, "risk rule")),
        until: asString(lock.expires_at, "--"),
        owner: asString(lock.owner, "Risk Guard")
      };
    })
  };
}

function reportFromApi(payload: unknown, date: string): DailyReport | false {
  const report = asRecord(payload);
  if (!Object.prototype.hasOwnProperty.call(report, "snapshot_id")) {
    return false;
  }

  const account = asRecord(report.account);
  const trades = asRecord(report.trades);
  const signals = asRecord(report.signals);
  const riskEvents = asRecord(report.risk_events);
  const openPositions = asRecord(report.open_positions);
  const closedTrades = asNumber(trades.closed_today_count, asNumber(trades.closed_count, 0));
  const wins = asNumber(trades.win_count, 0);
  let winRate = 0;

  if (closedTrades > 0) {
    winRate = (wins / closedTrades) * 100;
  }

  return {
    date,
    account: {
      equity: asNumber(account.equity, 0),
      netPnl: asNumber(account.realized_pnl_today, 0) + asNumber(account.unrealized_pnl, 0),
      maxDrawdown: asNumber(account.max_drawdown_pct, 0),
      volume: asNumber(openPositions.notional, 0)
    },
    performance: {
      trades: closedTrades,
      winRate,
      profitFactor: asNumber(trades.profit_factor, 0),
      avgR: asNumber(trades.avg_r, 0)
    },
    funnel: {
      scanned: asNumber(signals.received_count, 0),
      signaled: asNumber(signals.accepted_count, 0) + asNumber(signals.rejected_count, 0),
      entered: asNumber(signals.executed_count, asNumber(signals.accepted_count, 0)),
      closed: closedTrades
    },
    riskEvents: [
      {
        id: "api-risk-events",
        time: "--",
        severity: "warning",
        source: "Risk",
        message: `${asNumber(riskEvents.count, 0)} risk events, ${asNumber(riskEvents.blocking_count, 0)} blocking`
      }
    ],
    positions: getMockDailyReport(date).positions
  };
}

function orderPositionFromApi(value: Record<string, unknown>, index: number): OrderCenterPosition {
  const isShort = value.is_short === true || /short|sell/i.test(asDisplayString(value.side, ""));
  const entry = firstNumber([value.open_rate, value.entry_rate, value.entry_price, value.open_price]);
  const current = firstNumber([value.current_rate, value.mark_price, value.current_price, value.close_rate], entry);
  const pnl = firstNumber([
    value.profit_abs,
    value.current_profit_abs,
    value.realized_profit,
    value.unrealized_profit,
    value.pnl,
    value.close_profit_abs
  ]);
  const pnlPct = firstNumber([value.profit_pct, value.profit_ratio, value.close_profit_pct, value.pnl_pct]);
  const tradeId = firstString([value.trade_id, value.id], `position-${index}`);

  return {
    id: tradeId,
    pair: firstString([value.pair, value.symbol], "UNKNOWN/USDT"),
    side: isShort ? "short" : "long",
    amount: firstNumber([value.amount, value.contracts, value.size]),
    stakeAmount: firstNumber([value.stake_amount, value.stake_amount_filled, value.notional]),
    entry,
    current,
    pnl,
    pnlPct,
    leverage: firstNumber([value.leverage], 1),
    openDate: firstString([value.open_date, value.open_timestamp, value.created_at], "--"),
    status: firstString([value.status], "open")
  };
}

function isPendingOrder(order: Record<string, unknown>): boolean {
  const status = firstString([order.status, order.order_status], "");
  if (/closed|filled|canceled|cancelled|expired|rejected/i.test(status)) {
    return false;
  }

  if (/open|pending|new|partially/i.test(status)) {
    return true;
  }

  const remaining = firstNumber([order.remaining, order.remaining_amount], 0);
  return remaining > 0;
}

function orderFromApi(order: Record<string, unknown>, trade: Record<string, unknown>, index: number): OrderCenterOrder {
  const amount = firstNumber([order.amount, order.order_amount, order.safe_amount]);
  const filled = firstNumber([order.filled, order.filled_amount], 0);
  const remaining = firstNumber([order.remaining, order.remaining_amount], Math.max(0, amount - filled));
  const tradeId = firstString([trade.trade_id, trade.id, order.trade_id], "");

  return {
    id: firstString([order.order_id, order.id, order.ft_order_id], `order-${index}`),
    pair: firstString([order.pair, trade.pair, trade.symbol], "UNKNOWN/USDT"),
    side: firstString([order.side, order.ft_order_side], "--"),
    type: firstString([order.type, order.order_type], "--"),
    status: firstString([order.status, order.order_status], "pending"),
    price: firstNumber([order.price, order.safe_price, order.average, order.ft_price]),
    amount,
    filled,
    remaining,
    createdAt: firstString([order.order_date, order.order_timestamp, order.created_at], "--"),
    tradeId
  };
}

function tradeFromApi(value: Record<string, unknown>, index: number): OrderCenterTrade {
  const isOpen = value.is_open === true || !firstString([value.close_date, value.close_timestamp], "");
  const isShort = value.is_short === true || /short|sell/i.test(asDisplayString(value.side, ""));
  const orders = asRecordArray(value.orders);

  return {
    id: firstString([value.trade_id, value.id], `trade-${index}`),
    pair: firstString([value.pair, value.symbol], "UNKNOWN/USDT"),
    side: isShort ? "short" : "long",
    status: isOpen ? "open" : firstString([value.exit_reason, value.status], "closed"),
    openRate: firstNumber([value.open_rate, value.entry_rate, value.open_price]),
    closeRate: firstNumber([value.close_rate, value.exit_rate, value.close_price]),
    amount: firstNumber([value.amount, value.contracts, value.size]),
    pnl: firstNumber([value.close_profit_abs, value.profit_abs, value.realized_profit, value.pnl]),
    pnlPct: firstNumber([value.close_profit_pct, value.profit_pct, value.profit_ratio, value.pnl_pct]),
    openDate: firstString([value.open_date, value.open_timestamp, value.created_at], "--"),
    closeDate: firstString([value.close_date, value.close_timestamp, value.closed_at], "--"),
    ordersCount: orders.length
  };
}

function orderCenterFromApi(statusPayload: unknown, positionsPayload: unknown, tradesPayload: unknown): OrderCenterData {
  const statusPositions = extractRecordArray(statusPayload, ["trades", "open_trades", "status"]);
  const positionRows = extractRecordArray(positionsPayload, ["positions"]);
  const tradeRows = extractRecordArray(tradesPayload, ["trades"]);
  const positionSource = positionRows.length > 0 ? positionRows : statusPositions;
  const history = tradeRows.map(tradeFromApi);
  const positions = positionSource.map(orderPositionFromApi);
  const directOrders = extractRecordArray(statusPayload, ["open_orders", "orders"]);
  const tradeOrders = [...statusPositions, ...tradeRows].flatMap((trade) =>
    asRecordArray(trade.orders).map((order) => ({ order, trade }))
  );
  const orders = [
    ...directOrders.map((order, index) => orderFromApi(order, {}, index)),
    ...tradeOrders
      .filter(({ order }) => isPendingOrder(order))
      .map(({ order, trade }, index) => orderFromApi(order, trade, directOrders.length + index))
  ];
  const realizedPnl = history
    .filter((trade) => trade.status !== "open")
    .reduce((total, trade) => total + trade.pnl, 0);
  const openPnl = positions.reduce((total, position) => total + position.pnl, 0);
  const available = statusPayload !== false || positionsPayload !== false || tradesPayload !== false;

  return {
    positions,
    orders,
    history,
    summary: {
      realizedPnl,
      openPnl,
      totalPnl: realizedPnl + openPnl,
      openPositionCount: positions.length,
      pendingOrderCount: orders.length,
      historyCount: history.length,
      winCount: history.filter((trade) => trade.pnl > 0).length,
      lossCount: history.filter((trade) => trade.pnl < 0).length
    },
    dataSource: {
      source: "freqtrade",
      status: available ? "readonly" : "unavailable",
      reason: available ? "proxy" : "proxy unavailable",
      degraded: !available
    }
  };
}

function signalClassificationFromApi(value: unknown): SignalReviewItem["classification"] {
  const classification = asRecord(value);
  return {
    conclusion: asString(classification.conclusion, "needs_review"),
    confidence: asString(classification.confidence, "unknown"),
    reasonCodes: asArray(classification.reason_codes).map((item) => asDisplayString(item, "")).filter(Boolean),
    proposalTypes: asArray(classification.proposal_types).map((item) => asDisplayString(item, "")).filter(Boolean)
  };
}

function signalMediaFromApi(value: unknown): SignalReviewMediaItem[] {
  const metadata = asRecord(value);
  return asRecordArray(metadata.items)
    .map((item) => ({
      index: asNumber(item.index, -1),
      mimeType: asString(item.mime_type, "")
    }))
    .filter((item) => item.index >= 0 && item.mimeType.startsWith("image/"));
}

function signalReviewItemFromApi(value: unknown, index: number): SignalReviewItem {
  const signal = asRecord(value);
  const entry = asRecord(signal.entry);
  const leverage = asRecord(signal.leverage);
  const takeProfits = asRecordArray(signal.take_profits)
    .map((takeProfit) => asNumber(takeProfit.price, Number.NaN))
    .filter((price) => Number.isFinite(price));
  const reasonCodes = asArray(signal.review_reason_codes).map((item) => asDisplayString(item, "")).filter(Boolean);

  return {
    signalId: asString(signal.signal_id, `signal-${index}`),
    pair: asString(signal.pair_freqtrade, asString(signal.pair_raw, "UNKNOWN/USDT")),
    side: asString(signal.side, "long"),
    status: asString(signal.status, "needs_review"),
    receivedAt: asString(signal.received_at, "--"),
    rawText: asString(signal.raw_text, ""),
    reasonCodes,
    entryMode: asString(entry.mode, "cmp"),
    entryPrice: asNumber(entry.primary_price, 0),
    stopLoss: asNumber(signal.stop_loss, 0),
    takeProfits,
    leverage: `${asNumber(leverage.selected, asNumber(leverage.min, 0))}x`,
    media: signalMediaFromApi(signal.media_metadata),
    classification: signalClassificationFromApi(signal.classification)
  };
}

function signalReviewProposalFromApi(value: unknown, index: number): SignalReviewProposal {
  const proposal = asRecord(value);
  return {
    proposalId: asString(proposal.proposal_id, `proposal-${index}`),
    type: asString(proposal.type, "approve_request"),
    signalId: asString(proposal.signal_id, ""),
    title: asString(proposal.title, asString(proposal.type, "Proposal")),
    detail: asString(proposal.detail, ""),
    proposedAction: asString(proposal.proposed_action, asString(proposal.type, "review")),
    status: asString(proposal.status, "open"),
    classification: signalClassificationFromApi(proposal.classification)
  };
}

function signalReviewFromApi(payload: unknown): SignalReviewData {
  const review = asRecord(payload);
  const available = payload !== false;
  return {
    signals: asArray(review.signals).map(signalReviewItemFromApi),
    proposals: asArray(review.proposals).map(signalReviewProposalFromApi),
    dataSource: {
      source: "signals",
      status: available ? "ready" : "unavailable",
      reason: available ? "review queue" : "review queue unavailable",
      degraded: !available
    }
  };
}

export async function fetchDashboardOverview(): Promise<DashboardOverview> {
  const fallback = getMockDashboardOverview();
  const overviewPayload = await getJson("/api/dashboard/overview");
  const tradesPayload = await getJson("/api/dashboard/open-trades");
  const auditEventsPayload = await getJson("/api/dashboard/events");
  const adapted = dashboardFromApi(overviewPayload, tradesPayload, auditEventsPayload);
  if (adapted) {
    return adapted;
  }

  return fallback;
}

export async function fetchOrderCenter(): Promise<OrderCenterData> {
  const statusPayload = await getJson("/api/freqtrade/status");
  const positionsPayload = await getJson("/api/freqtrade/positions");
  const tradesPayload = await getJson("/api/freqtrade/trades?limit=100");
  return orderCenterFromApi(statusPayload, positionsPayload, tradesPayload);
}

export async function fetchSignalReview(): Promise<SignalReviewData> {
  const payload = await getJson("/api/signals/review/queue");
  return signalReviewFromApi(payload);
}

export async function fetchSignalMediaBlob(signalId: string, index: number): Promise<Blob | false> {
  return getBlob(`/api/signals/${encodeURIComponent(signalId)}/media/${index}`);
}

export async function approveReviewSignal(signalId: string, reason: string): Promise<boolean> {
  const payload = await postJson(`/api/signals/review/${encodeURIComponent(signalId)}/approve`, {
    reason
  });
  return actionAccepted(payload);
}

export async function rejectReviewSignal(signalId: string, reason: string): Promise<boolean> {
  const payload = await postJson(`/api/signals/review/${encodeURIComponent(signalId)}/reject`, {
    reason
  });
  return actionAccepted(payload);
}

export async function approveReviewProposal(proposalId: string, reason: string): Promise<boolean> {
  const payload = await postJson(`/api/signals/review/proposals/${encodeURIComponent(proposalId)}/approve`, {
    reason
  });
  return actionAccepted(payload);
}

export async function rejectReviewProposal(proposalId: string, reason: string): Promise<boolean> {
  const payload = await postJson(`/api/signals/review/proposals/${encodeURIComponent(proposalId)}/reject`, {
    reason
  });
  return actionAccepted(payload);
}

export async function fetchRiskOverview(): Promise<RiskOverview> {
  const payload = await getJson("/api/risk/overview");
  const adapted = riskFromApi(payload);
  if (adapted) {
    return adapted;
  }

  return getMockRiskOverview();
}

export async function fetchDailyReport(date: string): Promise<DailyReport> {
  const payload = await getJson(`/api/reports/daily/${date}`);
  const adapted = reportFromApi(payload, date);
  if (adapted) {
    return adapted;
  }

  return getMockDailyReport(date);
}

export async function activateKillSwitch(
  reason: string,
  closeAll: boolean,
  confirmationPhrase: string
): Promise<boolean> {
  const payload = await postJson("/api/risk/kill-switch", {
    close_all: closeAll,
    confirmation_phrase: confirmationPhrase,
    reason
  });
  const result = asRecord(payload);
  return closeAllAccepted(result);
}

export async function closePosition(tradeId: string, reason: string, signalId: string): Promise<boolean> {
  const payload = await postJson("/api/dashboard/actions/close-trade", {
    reason,
    signal_id: signalId,
    trade_id: tradeId
  });
  return actionAccepted(payload);
}

export async function partialClosePosition(
  tradeId: string,
  amount: number,
  reason: string,
  signalId: string
): Promise<boolean> {
  const payload = await postJson("/api/dashboard/actions/close-trade", {
    amount,
    reason,
    signal_id: signalId,
    trade_id: tradeId
  });
  return actionAccepted(payload);
}

export async function moveStopLoss(
  tradeId: string,
  stopLossPrice: number,
  reason: string,
  signalId: string
): Promise<boolean> {
  const payload = await postJson("/api/dashboard/actions/move-stoploss", {
    reason,
    signal_id: signalId,
    stop_loss_price: stopLossPrice,
    trade_id: tradeId
  });
  return actionAccepted(payload);
}

export async function pauseBot(reason: string): Promise<boolean> {
  const payload = await postJson("/api/dashboard/actions/pause-bot", {
    reason
  });
  return actionAccepted(payload);
}

export async function resumeBot(reason: string): Promise<boolean> {
  const payload = await postJson("/api/dashboard/actions/resume-bot", {
    reason
  });
  return actionAccepted(payload);
}

export async function lockPair(pair: string, reason: string): Promise<boolean> {
  const payload = await postJson("/api/dashboard/actions/lock-pair", {
    pair,
    reason
  });
  return actionAccepted(payload);
}

export async function fetchDailyReportMarkdown(date: string): Promise<string | false> {
  return getText(`/api/reports/daily/${date}/markdown`);
}

export async function fetchDailyReportVersions(date: string): Promise<Record<string, unknown>[]> {
  const payload = await getJson(`/api/reports/daily/${date}/versions`);
  return asRecordArray(payload);
}

export async function fetchDailyReportTelegramPreview(date: string): Promise<string | false> {
  const payload = await postJson(`/api/reports/daily/${date}/telegram-preview`, {});
  const result = asRecord(payload);
  const text = asString(result.text, "");
  if (!text) {
    return false;
  }

  return text;
}

export async function createDailyReportSnapshot(date: string): Promise<Record<string, unknown> | false> {
  const payload = await postJson("/api/reports/daily/snapshot", {
    date
  });
  const result = asRecord(payload);
  if (!Object.prototype.hasOwnProperty.call(result, "snapshot_id")) {
    return false;
  }

  return result;
}
