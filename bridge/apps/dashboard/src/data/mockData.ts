export type Severity = "info" | "warning" | "critical";

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

export type DataSourceState = {
  source: string;
  status: string;
  reason: string;
  degraded: boolean;
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
};

const events: EventLog[] = [
  {
    id: "evt-001",
    time: "06:42:11",
    severity: "critical",
    source: "Risk Guard",
    message: "ETH/USDT leverage above strategy ceiling"
  },
  {
    id: "evt-002",
    time: "06:40:02",
    severity: "warning",
    source: "Execution",
    message: "BTC/USDT partial fill delayed 4.2s"
  },
  {
    id: "evt-003",
    time: "06:38:19",
    severity: "info",
    source: "Signal",
    message: "SOL/USDT breakout rejected by volatility filter"
  },
  {
    id: "evt-004",
    time: "06:34:55",
    severity: "warning",
    source: "Exposure",
    message: "USDT exposure reached 78% of limit"
  },
  {
    id: "evt-005",
    time: "06:30:41",
    severity: "info",
    source: "Bot",
    message: "Hermes scalper heartbeat received"
  },
  {
    id: "evt-006",
    time: "06:28:03",
    severity: "critical",
    source: "Stop Loss",
    message: "MATIC/USDT open trade missing SL"
  },
  {
    id: "evt-007",
    time: "06:23:48",
    severity: "info",
    source: "Report",
    message: "Daily markdown snapshot prepared"
  }
];

const positions: Position[] = [
  {
    id: "pos-eth",
    pair: "ETH/USDT",
    side: "long",
    leverage: 8,
    entry: 3820.12,
    mark: 3856.44,
    pnl: 842.35,
    pnlPercent: 1.91,
    size: 4.88,
    stopLoss: 3760,
    takeProfit: 4020,
    rMultiple: 0.72,
    signalId: "sig-eth-demo",
    freqtradeTradeId: "pos-eth",
    rawSignal: "ETH long entry 3820 SL 3760 TP 4020",
    structuredSignal: {
      entry: 3820.12,
      pair: "ETH/USDT",
      side: "long",
      stop_loss: 3760,
      take_profit: 4020
    },
    orders: [{ order_id: "eth-order-1", side: "buy", status: "filled" }],
    fills: [{ fill_id: "eth-fill-1", price: 3820.12, size: 4.88 }],
    auditTimeline: [{ event_type: "signal_received", occurred_at: "2026-05-31T06:10:00Z" }],
    anomaly: "Leverage high"
  },
  {
    id: "pos-btc",
    pair: "BTC/USDT",
    side: "short",
    leverage: 3,
    entry: 68340.2,
    mark: 68112.8,
    pnl: 310.21,
    pnlPercent: 0.62,
    size: 0.42,
    stopLoss: 69020,
    takeProfit: 67200,
    rMultiple: 0.48,
    signalId: "sig-btc-demo",
    freqtradeTradeId: "pos-btc",
    rawSignal: "BTC short entry 68340 SL 69020 TP 67200",
    structuredSignal: {
      entry: 68340.2,
      pair: "BTC/USDT",
      side: "short",
      stop_loss: 69020,
      take_profit: 67200
    },
    orders: [{ order_id: "btc-order-1", side: "sell", status: "filled" }],
    fills: [{ fill_id: "btc-fill-1", price: 68340.2, size: 0.42 }],
    auditTimeline: [{ event_type: "order_submitted", occurred_at: "2026-05-31T06:14:00Z" }],
    anomaly: false
  },
  {
    id: "pos-sol",
    pair: "SOL/USDT",
    side: "long",
    leverage: 5,
    entry: 171.42,
    mark: 168.91,
    pnl: -226.18,
    pnlPercent: -1.46,
    size: 92,
    stopLoss: 164.8,
    takeProfit: 181.2,
    rMultiple: -0.38,
    signalId: "sig-sol-demo",
    freqtradeTradeId: "pos-sol",
    rawSignal: "SOL long entry 171.42 SL 164.8 TP 181.2",
    structuredSignal: {
      entry: 171.42,
      pair: "SOL/USDT",
      side: "long",
      stop_loss: 164.8,
      take_profit: 181.2
    },
    orders: [{ order_id: "sol-order-1", side: "buy", status: "filled" }],
    fills: [{ fill_id: "sol-fill-1", price: 171.42, size: 92 }],
    auditTimeline: [{ event_type: "risk_watch", occurred_at: "2026-05-31T06:18:00Z" }],
    anomaly: "Drawdown watch"
  },
  {
    id: "pos-matic",
    pair: "MATIC/USDT",
    side: "long",
    leverage: 6,
    entry: 0.743,
    mark: 0.739,
    pnl: -48.75,
    pnlPercent: -0.54,
    size: 18600,
    stopLoss: false,
    takeProfit: 0.81,
    rMultiple: -0.12,
    signalId: "sig-matic-demo",
    freqtradeTradeId: "pos-matic",
    rawSignal: "MATIC long entry 0.743 TP 0.81",
    structuredSignal: {
      entry: 0.743,
      pair: "MATIC/USDT",
      side: "long",
      take_profit: 0.81
    },
    orders: [{ order_id: "matic-order-1", side: "buy", status: "filled" }],
    fills: [{ fill_id: "matic-fill-1", price: 0.743, size: 18600 }],
    auditTimeline: [{ event_type: "stop_loss_missing", occurred_at: "2026-05-31T06:21:00Z" }],
    anomaly: "No SL"
  }
];

export function getMockDashboardOverview(): DashboardOverview {
  return {
    dataSource: {
      source: "demo",
      status: "runtime_degraded",
      reason: "provider_missing",
      degraded: true
    },
    safety: {
      modeLabel: "DRY-RUN LOCKED",
      dataFreshness: "API fallback",
      operatorLane: "Risk review",
      lastSync: "demo snapshot",
      riskState: "warning"
    },
    exposure: [
      {
        label: "Exposure usage",
        value: "32.8%",
        detail: "$42.1k margin / 45% cap",
        usagePct: 73,
        status: "warning"
      },
      {
        label: "Signal execution",
        value: "22.8%",
        detail: "42 entries / 184 accepted",
        usagePct: 23,
        status: "ok"
      },
      {
        label: "Freshness health",
        value: "degraded",
        detail: "using demo fallback",
        usagePct: 38,
        status: "warning"
      }
    ],
    account: {
      equity: 128420.52,
      available: 38210.14,
      marginUsed: 42180.29,
      dailyPnl: 1648.38
    },
    kpis: [
      { label: "Equity", value: "$128,420.52", delta: "+1.30% today", tone: "good" },
      { label: "Open PnL", value: "$877.63", delta: "4 live trades", tone: "good" },
      { label: "Margin Used", value: "32.8%", delta: "limit 45%", tone: "warning" },
      { label: "Daily Loss Buffer", value: "$3,351", delta: "67% remaining", tone: "good" }
    ],
    bots: [
      {
        name: "Hermes Scalper",
        status: "running",
        pairCount: 18,
        openTrades: 3,
        lastHeartbeat: "6s ago"
      },
      {
        name: "Mean Reversion",
        status: "paused",
        pairCount: 9,
        openTrades: 1,
        lastHeartbeat: "42s ago"
      },
      {
        name: "Risk Guard",
        status: "degraded",
        pairCount: 31,
        openTrades: 4,
        lastHeartbeat: "11s ago"
      }
    ],
    riskLamps: [
      { label: "Single Trade", status: "ok", detail: "0.82% / 1.0%" },
      { label: "Total Exposure", status: "elevated", detail: "78% / 80%" },
      { label: "Missing Stop Loss", status: "critical", detail: "1 position missing" },
      { label: "Daily Loss", status: "ok", detail: "$1.6k / $5k" }
    ],
    events: events.slice(0, 20),
    positions
  };
}

export function getMockRiskOverview(): RiskOverview {
  return {
    metrics: [
      { label: "单笔风险", value: "0.82%", limit: "1.00%", status: "ok" },
      { label: "总风险", value: "4.7%", limit: "6.0%", status: "ok" },
      { label: "日亏损", value: "$1,648 gain", limit: "$5,000 loss", status: "ok" },
      { label: "敞口", value: "78%", limit: "80%", status: "warning" },
      { label: "无 SL", value: "1", limit: "0", status: "critical" },
      { label: "高杠杆", value: "1", limit: "0", status: "critical" }
    ],
    blockingReasons: [
      "MATIC/USDT missing stop loss blocks new correlated entries",
      "ETH/USDT leverage exceeds configured cap",
      "Exposure limit near ceiling for USDT collateral bucket"
    ],
    pairLocks: [
      { pair: "MATIC/USDT", reason: "No SL remediation", until: "07:30", owner: "Risk Guard" },
      { pair: "ETH/USDT", reason: "Leverage cooldown", until: "08:00", owner: "Hermes Scalper" },
      { pair: "DOGE/USDT", reason: "Spread anomaly", until: "09:15", owner: "Execution" }
    ]
  };
}

export function getMockDailyReport(date: string): DailyReport {
  return {
    date,
    account: {
      equity: 128420.52,
      netPnl: 1648.38,
      maxDrawdown: 2.8,
      volume: 928410
    },
    performance: {
      trades: 42,
      winRate: 61.9,
      profitFactor: 1.74,
      avgR: 0.38
    },
    funnel: {
      scanned: 2140,
      signaled: 184,
      entered: 42,
      closed: 38
    },
    riskEvents: events.filter((event) => event.severity !== "info"),
    positions
  };
}
