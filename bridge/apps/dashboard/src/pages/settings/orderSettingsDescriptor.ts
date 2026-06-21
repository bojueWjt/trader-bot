import type { OrderSettingScalar } from "../../utils/api";

export type OrderSettingsCategoryKey =
  | "advanced"
  | "emergency"
  | "entry"
  | "general"
  | "money"
  | "notifications"
  | "price_monitor"
  | "protection"
  | "reconciliation";

export type SettingsFieldType = "boolean" | "enum" | "integer" | "number" | "string";

export type SettingsFieldDescriptor = {
  applyMode: "hot_reload" | "restart_required" | string;
  defaultValue: OrderSettingScalar;
  help: string;
  key: string;
  label: string;
  scopeOverridable: boolean;
  secret: boolean;
  type: SettingsFieldType;
  unit: string;
  enumValues?: string[];
  max?: number;
  min?: number;
};

export const generalSettingsFields = [
  {
    applyMode: "hot_reload",
    defaultValue: false,
    help: "Enables the order-management runtime for configured accounts and instruments.",
    key: "order_manager_enabled",
    label: "Order manager enabled",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "restart_required",
    defaultValue: "shadow",
    enumValues: ["shadow", "testnet", "live"],
    help: "Controls whether submitted intents are simulated, routed to testnet, or allowed against live venues.",
    key: "execution_mode",
    label: "Execution mode",
    scopeOverridable: true,
    secret: false,
    type: "enum",
    unit: "mode"
  },
  {
    applyMode: "hot_reload",
    defaultValue: "HALTED",
    enumValues: ["ACTIVE", "REDUCING", "HALTED"],
    help: "Sets the default posture applied before account or instrument overrides are considered.",
    key: "risk_posture",
    label: "Risk posture",
    scopeOverridable: true,
    secret: false,
    type: "enum",
    unit: "state"
  },
  {
    applyMode: "restart_required",
    defaultValue: "",
    help: "Comma-separated instrument symbols allowed for order management.",
    key: "allowed_instruments",
    label: "Allowed instruments",
    scopeOverridable: true,
    secret: false,
    type: "string",
    unit: "csv_symbols"
  },
  {
    applyMode: "restart_required",
    defaultValue: "",
    help: "Default account id used when an intent does not specify an account.",
    key: "default_account_id",
    label: "Default account id",
    scopeOverridable: false,
    secret: false,
    type: "string",
    unit: "account_id"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 1,
    help: "Maximum number of execution jobs the order manager may run at once.",
    key: "max_concurrent_execution_jobs",
    label: "Max concurrent execution jobs",
    max: 100,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "jobs"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 300,
    help: "How long an order intent remains valid before it expires.",
    key: "intent_ttl_seconds",
    label: "Intent ttl seconds",
    max: 86400,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "seconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 30,
    help: "How long request ids are retained for idempotency checks.",
    key: "idempotency_retention_days",
    label: "Idempotency retention days",
    max: 365,
    min: 1,
    scopeOverridable: false,
    secret: false,
    type: "integer",
    unit: "days"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 30,
    help: "Default timeout used for operator commands submitted by order-management flows.",
    key: "default_command_timeout_seconds",
    label: "Default command timeout seconds",
    max: 3600,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "seconds"
  }
] satisfies SettingsFieldDescriptor[];

export const entrySettingsFields = [
  {
    applyMode: "hot_reload",
    defaultValue: "limit",
    enumValues: ["market", "limit", "zone"],
    help: "Default entry order style used when a signal does not specify one.",
    key: "default_order_type",
    label: "Default order type",
    scopeOverridable: true,
    secret: false,
    type: "enum",
    unit: "order_type"
  },
  {
    applyMode: "hot_reload",
    defaultValue: "GTC",
    enumValues: ["GTC", "IOC", "FOK", "GTD"],
    help: "Default exchange time-in-force for entry orders.",
    key: "time_in_force",
    label: "Time in force",
    scopeOverridable: true,
    secret: false,
    type: "enum",
    unit: "tif"
  },
  {
    applyMode: "hot_reload",
    defaultValue: false,
    help: "Requires maker-only submission where the selected order type supports it.",
    key: "post_only",
    label: "Post only",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 25,
    help: "Maximum tolerated slippage for market-style execution.",
    key: "max_slippage_bps",
    label: "Max slippage bps",
    max: 1000,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "bps"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 5,
    help: "Limit price offset in basis points. Applies only when the default order type is limit.",
    key: "limit_offset_bps",
    label: "Limit offset bps",
    max: 1000,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "bps"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 60,
    help: "Seconds before an unfilled entry order is considered timed out.",
    key: "unfilled_timeout_seconds",
    label: "Unfilled timeout seconds",
    max: 86400,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "seconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 10,
    help: "Minimum seconds between repricing attempts for eligible entry orders.",
    key: "reprice_interval_seconds",
    label: "Reprice interval seconds",
    max: 3600,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "seconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 3,
    help: "Maximum number of repricing attempts before the order is left or cancelled by policy.",
    key: "max_reprices",
    label: "Max reprices",
    max: 100,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "count"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 3,
    help: "Maximum number of submit retries after a transient venue or network failure.",
    key: "max_submit_retries",
    label: "Max submit retries",
    max: 100,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "count"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 1,
    help: "Backoff delay between submit retry attempts.",
    key: "submit_retry_backoff_seconds",
    label: "Submit retry backoff seconds",
    max: 3600,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "seconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: "cancel_remainder",
    enumValues: ["keep_remainder", "cancel_remainder", "convert_remainder_to_market", "abort_and_reduce_filled"],
    help: "Action applied when an entry order receives only a partial fill.",
    key: "partial_fill_policy",
    label: "Partial fill policy",
    scopeOverridable: true,
    secret: false,
    type: "enum",
    unit: "policy"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 0.95,
    help: "Minimum filled ratio required before a partial fill is accepted.",
    key: "minimum_fill_ratio",
    label: "Minimum fill ratio",
    max: 1,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "ratio"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 30,
    help: "Seconds to wait before applying the partial-fill policy.",
    key: "partial_fill_timeout_seconds",
    label: "Partial fill timeout seconds",
    max: 86400,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "seconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 50,
    help: "Maximum slippage allowed when converting a remainder to market.",
    key: "market_conversion_max_slippage_bps",
    label: "Market conversion max slippage bps",
    max: 2000,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "bps"
  }
] satisfies SettingsFieldDescriptor[];

export const protectionSettingsFields = [
  {
    applyMode: "hot_reload",
    defaultValue: true,
    help: "Requires a protective stop to be installed after a managed entry opens exposure.",
    key: "require_stop",
    label: "Require stop",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: "stop_market",
    enumValues: ["stop_market", "stop_limit"],
    help: "Order type used when installing the primary protective stop.",
    key: "stop_order_type",
    label: "Stop order type",
    scopeOverridable: true,
    secret: false,
    type: "enum",
    unit: "order_type"
  },
  {
    applyMode: "hot_reload",
    defaultValue: "mark",
    enumValues: ["mark", "last"],
    help: "Price basis used to trigger protective stop orders.",
    key: "stop_trigger_basis",
    label: "Stop trigger basis",
    scopeOverridable: true,
    secret: false,
    type: "enum",
    unit: "price_basis"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 15,
    help: "Maximum time allowed for initial protection installation after entry fill.",
    key: "protection_install_timeout_seconds",
    label: "Protection install timeout seconds",
    max: 3600,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "seconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: "",
    help: "JSON take-profit ladder. Each rung uses an R multiple and fraction of the position to close.",
    key: "take_profit_ladder",
    label: "Take profit ladder",
    scopeOverridable: true,
    secret: false,
    type: "string",
    unit: "json"
  },
  {
    applyMode: "hot_reload",
    defaultValue: false,
    help: "Moves stop protection to breakeven after the configured R multiple is reached.",
    key: "breakeven_enabled",
    label: "Breakeven enabled",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 1,
    help: "R multiple that triggers breakeven stop movement.",
    key: "breakeven_trigger_r",
    label: "Breakeven trigger R",
    max: 100,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "r_multiple"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 0,
    help: "Offset from entry price applied when moving the stop to breakeven.",
    key: "breakeven_offset_bps",
    label: "Breakeven offset bps",
    max: 1000,
    min: -1000,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "bps"
  },
  {
    applyMode: "hot_reload",
    defaultValue: false,
    help: "Enables trailing stop management after activation.",
    key: "trailing_stop_enabled",
    label: "Trailing stop enabled",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 1.5,
    help: "R multiple that activates trailing stop management.",
    key: "trailing_activation_r",
    label: "Trailing activation R",
    max: 100,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "r_multiple"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 0.5,
    help: "Trailing callback rate used by venues that support percentage trailing stops.",
    key: "trailing_callback_rate",
    label: "Trailing callback rate",
    max: 100,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "percent"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 5,
    help: "Minimum stop movement before a trailing update is submitted.",
    key: "trailing_min_step_bps",
    label: "Trailing min step bps",
    max: 10000,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "bps"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 5,
    help: "Minimum delay between trailing stop update commands.",
    key: "trailing_update_rate_limit_seconds",
    label: "Trailing update rate limit seconds",
    max: 3600,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "seconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 0.25,
    help: "Default fraction used when an operator requests a manual partial close.",
    key: "partial_close_default_fraction",
    label: "Partial close default fraction",
    max: 1,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "ratio"
  },
  {
    applyMode: "hot_reload",
    defaultValue: "cancel_replace",
    enumValues: ["cancel_replace", "amend_in_place"],
    help: "How existing protection is replaced when a new protection state is computed.",
    key: "replace_protection_behavior",
    label: "Replace protection behavior",
    scopeOverridable: true,
    secret: false,
    type: "enum",
    unit: "policy"
  },
  {
    applyMode: "hot_reload",
    defaultValue: "reducing",
    enumValues: ["halt", "reducing", "auto_repair", "alert_only"],
    help: "Policy applied when required protection is missing or inconsistent.",
    key: "protection_repair_policy",
    label: "Protection repair policy",
    scopeOverridable: true,
    secret: false,
    type: "enum",
    unit: "policy"
  }
] satisfies SettingsFieldDescriptor[];

export const moneySettingsFields = [
  {
    applyMode: "hot_reload",
    defaultValue: "fixed_risk",
    enumValues: ["fixed_risk", "fixed_notional", "equity_fraction"],
    help: "Controls whether orders are sized from risk, a fixed notional amount, or an equity fraction.",
    key: "sizing_mode",
    label: "Sizing mode",
    scopeOverridable: true,
    secret: false,
    type: "enum",
    unit: "mode"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 0,
    help: "Fixed quote-currency notional used when sizing mode is fixed_notional.",
    key: "fixed_notional",
    label: "Fixed notional",
    max: 1000000000,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "quote_currency"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 0,
    help: "Fraction of account equity used when sizing mode is equity_fraction.",
    key: "equity_fraction",
    label: "Equity fraction",
    max: 1,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "ratio"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 0,
    help: "Maximum account equity percentage risked on a single trade.",
    key: "risk_per_trade_pct",
    label: "Risk per trade pct",
    max: 10,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "percent"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 0,
    help: "Maximum quote-currency notional accepted for one order.",
    key: "max_notional_per_order",
    label: "Max notional per order",
    max: 1000000000,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "quote_currency"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 1,
    help: "Maximum leverage multiple allowed for a managed order.",
    key: "max_leverage",
    label: "Max leverage",
    max: 125,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "multiple"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 0,
    help: "Maximum number of open positions allowed by the money guard.",
    key: "max_open_positions",
    label: "Max open positions",
    max: 1000,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "count"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 0,
    help: "Maximum quote-currency exposure allowed for one instrument.",
    key: "max_instrument_exposure",
    label: "Max instrument exposure",
    max: 1000000000,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "quote_currency"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 0,
    help: "Maximum quote-currency exposure allowed across correlated instruments.",
    key: "max_correlated_exposure",
    label: "Max correlated exposure",
    max: 1000000000,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "quote_currency"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 0,
    help: "Maximum total open risk as a percentage of account equity.",
    key: "max_total_risk_pct",
    label: "Max total risk pct",
    max: 100,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "percent"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 100,
    help: "Minimum free margin percentage that must remain after a new order.",
    key: "minimum_free_margin_pct",
    label: "Minimum free margin pct",
    max: 100,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "percent"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 100,
    help: "Account balance percentage reserved from new-risk sizing.",
    key: "reserve_balance_pct",
    label: "Reserve balance pct",
    max: 100,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "percent"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 0,
    help: "Maximum daily realized loss percentage before new risk is blocked.",
    key: "daily_loss_limit_pct",
    label: "Daily loss limit pct",
    max: 100,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "percent"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 0,
    help: "Maximum account drawdown percentage allowed before risk controls intervene.",
    key: "max_drawdown_pct",
    label: "Max drawdown pct",
    max: 100,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "percent"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 1440,
    help: "Cooldown after a loss-limit breach before opening risk can resume.",
    key: "loss_cooldown_minutes",
    label: "Loss cooldown minutes",
    max: 10080,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "minutes"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 300,
    help: "How long risk reservations remain valid before expiring.",
    key: "risk_reservation_ttl_seconds",
    label: "Risk reservation ttl seconds",
    max: 86400,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "seconds"
  }
] satisfies SettingsFieldDescriptor[];

export const priceMonitorSettingsFields = [
  {
    applyMode: "hot_reload",
    defaultValue: 10,
    help: "Maximum age for market data before it is treated as stale.",
    key: "market_data_stale_seconds",
    label: "Market data stale seconds",
    max: 3600,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "seconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 15,
    help: "Maximum age for account data before it is treated as stale.",
    key: "account_data_stale_seconds",
    label: "Account data stale seconds",
    max: 3600,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "seconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 30,
    help: "Maximum age for execution-event data before it is treated as stale.",
    key: "execution_event_stale_seconds",
    label: "Execution event stale seconds",
    max: 3600,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "seconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 5000,
    help: "Maximum projection lag before the price monitor treats projections as stale.",
    key: "projection_lag_threshold_ms",
    label: "Projection lag threshold ms",
    max: 600000,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "milliseconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 300,
    help: "Maximum age for reconciliation verification before it is treated as stale.",
    key: "reconciliation_stale_seconds",
    label: "Reconciliation stale seconds",
    max: 86400,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "seconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 200,
    help: "Maximum tolerated price deviation before protection policies apply.",
    key: "price_deviation_bps",
    label: "Price deviation bps",
    max: 10000,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "bps"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 5,
    help: "How often the monitor evaluates live price and data freshness.",
    key: "evaluation_interval_seconds",
    label: "Evaluation interval seconds",
    max: 3600,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "seconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 10,
    help: "How often the protection watchdog checks disconnected or stale states.",
    key: "protection_watchdog_interval_seconds",
    label: "Protection watchdog interval seconds",
    max: 3600,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "seconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: "halt",
    enumValues: ["halt", "reducing", "hold"],
    help: "Action taken when price-monitor connectivity is lost.",
    key: "disconnect_action",
    label: "Disconnect action",
    scopeOverridable: true,
    secret: false,
    type: "enum",
    unit: "action"
  }
] satisfies SettingsFieldDescriptor[];

export const reconciliationSettingsFields = [
  {
    applyMode: "restart_required",
    defaultValue: true,
    help: "Requires startup reconciliation before order management resumes.",
    key: "startup_reconciliation_required",
    label: "Startup reconciliation required",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 300,
    help: "Seconds between scheduled reconciliation checks.",
    key: "reconciliation_interval_seconds",
    label: "Reconciliation interval seconds",
    max: 86400,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "seconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: "halt",
    enumValues: ["halt", "cancel", "adopt", "ignore"],
    help: "Policy applied when an unmanaged open order is found.",
    key: "orphan_order_policy",
    label: "Orphan order policy",
    scopeOverridable: true,
    secret: false,
    type: "enum",
    unit: "policy"
  },
  {
    applyMode: "hot_reload",
    defaultValue: "halt",
    enumValues: ["halt", "reduce", "adopt", "ignore"],
    help: "Policy applied when an unmanaged external position is found.",
    key: "external_position_policy",
    label: "External position policy",
    scopeOverridable: true,
    secret: false,
    type: "enum",
    unit: "policy"
  },
  {
    applyMode: "hot_reload",
    defaultValue: "halt",
    enumValues: ["halt", "reducing", "auto_repair", "alert_only"],
    help: "Policy applied when projection and venue state drift.",
    key: "drift_policy",
    label: "Drift policy",
    scopeOverridable: true,
    secret: false,
    type: "enum",
    unit: "policy"
  },
  {
    applyMode: "hot_reload",
    defaultValue: false,
    help: "Allows reconciliation to adopt eligible external resources automatically.",
    key: "auto_adopt_enabled",
    label: "Auto adopt enabled",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 3,
    help: "Maximum retry attempts for reconciliation repair actions.",
    key: "retry_limit",
    label: "Retry limit",
    max: 100,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "count"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 1,
    help: "Backoff delay between reconciliation repair retry attempts.",
    key: "retry_backoff_seconds",
    label: "Retry backoff seconds",
    max: 3600,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "number",
    unit: "seconds"
  },
  {
    applyMode: "restart_required",
    defaultValue: "halt_until_reconciled",
    enumValues: ["halt_until_reconciled", "resume_after_reconcile"],
    help: "Restart behavior after reconciliation has completed.",
    key: "restart_recovery_mode",
    label: "Restart recovery mode",
    scopeOverridable: true,
    secret: false,
    type: "enum",
    unit: "mode"
  }
] satisfies SettingsFieldDescriptor[];

export const emergencySettingsFields = [
  {
    applyMode: "hot_reload",
    defaultValue: 10,
    help: "Number of open orders included in each emergency cancel batch.",
    key: "cancel_batch_size",
    label: "Cancel batch size",
    max: 1000,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "orders"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 5,
    help: "Number of positions included in each emergency close batch.",
    key: "close_batch_size",
    label: "Close batch size",
    max: 1000,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "positions"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 250,
    help: "Delay inserted between emergency order commands.",
    key: "inter_order_delay_ms",
    label: "Inter order delay ms",
    max: 60000,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "milliseconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 3,
    help: "Maximum retry attempts for emergency operator commands.",
    key: "command_max_retries",
    label: "Command max retries",
    max: 100,
    min: 0,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "count"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 30,
    help: "Maximum wait time for emergency command verification.",
    key: "verify_timeout_seconds",
    label: "Verify timeout seconds",
    max: 86400,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "seconds"
  },
  {
    applyMode: "hot_reload",
    defaultValue: "flat",
    enumValues: ["flat", "cancelled_and_flat"],
    help: "Final state required after a close-all emergency workflow completes.",
    key: "close_all_final_state",
    label: "Close all final state",
    scopeOverridable: true,
    secret: false,
    type: "enum",
    unit: "state"
  },
  {
    applyMode: "hot_reload",
    defaultValue: true,
    help: "Allows emergency close commands to proceed when the order manager is HALTED.",
    key: "allow_close_while_halted",
    label: "Allow close while HALTED",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "restart_required",
    defaultValue: 30,
    help: "Number of days emergency command records are retained.",
    key: "command_retention_days",
    label: "Command retention days",
    max: 3650,
    min: 1,
    scopeOverridable: false,
    secret: false,
    type: "integer",
    unit: "days"
  }
] satisfies SettingsFieldDescriptor[];

export const notificationsSettingsFields = [
  {
    applyMode: "hot_reload",
    defaultValue: true,
    help: "Emits notifications when a venue accepts an order.",
    key: "order_accepted_enabled",
    label: "Order accepted enabled",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: true,
    help: "Emits notifications when a venue rejects an order.",
    key: "order_rejected_enabled",
    label: "Order rejected enabled",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: true,
    help: "Emits notifications when an order is filled.",
    key: "order_filled_enabled",
    label: "Order filled enabled",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: true,
    help: "Emits notifications for partial fills.",
    key: "partial_fill_enabled",
    label: "Partial fill enabled",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: true,
    help: "Emits notifications when required protection is missing.",
    key: "protection_missing_enabled",
    label: "Protection missing enabled",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: true,
    help: "Emits notifications for stale market-data conditions.",
    key: "stale_market_enabled",
    label: "Stale market enabled",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: true,
    help: "Emits notifications for stale account-data conditions.",
    key: "stale_account_enabled",
    label: "Stale account enabled",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: true,
    help: "Emits notifications when reconciliation detects venue/projection drift.",
    key: "reconciliation_drift_enabled",
    label: "Reconciliation drift enabled",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: true,
    help: "Emits notifications when daily loss thresholds are reached.",
    key: "daily_loss_enabled",
    label: "Daily loss enabled",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: true,
    help: "Emits notifications when drawdown thresholds are reached.",
    key: "drawdown_enabled",
    label: "Drawdown enabled",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: true,
    help: "Emits notifications for cancel-all and close-all command results.",
    key: "emergency_command_results_enabled",
    label: "Emergency command results enabled",
    scopeOverridable: true,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: "dashboard",
    help: "Comma-separated notification channels available to severity routing.",
    key: "notification_channels",
    label: "Notification channels",
    scopeOverridable: true,
    secret: false,
    type: "string",
    unit: "csv_channels"
  },
  {
    applyMode: "hot_reload",
    defaultValue: "critical:operator,high:operator,medium:dashboard",
    help: "Severity-to-channel routing policy for order-management events.",
    key: "severity_routing",
    label: "Severity routing",
    scopeOverridable: true,
    secret: false,
    type: "string",
    unit: "routing_policy"
  }
] satisfies SettingsFieldDescriptor[];

export const advancedSettingsFields = [
  {
    applyMode: "hot_reload",
    defaultValue: 100,
    help: "Maximum outbox events processed per worker batch.",
    key: "outbox_batch_size",
    label: "Outbox batch size",
    max: 10000,
    min: 1,
    scopeOverridable: false,
    secret: false,
    type: "integer",
    unit: "events"
  },
  {
    applyMode: "restart_required",
    defaultValue: 10000,
    help: "Maximum command spool items retained before backpressure policies apply.",
    key: "spool_max_items",
    label: "Spool max items",
    max: 1000000,
    min: 0,
    scopeOverridable: false,
    secret: false,
    type: "integer",
    unit: "items"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 3600,
    help: "Maximum age for spool items before they are eligible for cleanup.",
    key: "spool_max_age_seconds",
    label: "Spool max age seconds",
    max: 2592000,
    min: 1,
    scopeOverridable: false,
    secret: false,
    type: "integer",
    unit: "seconds"
  },
  {
    applyMode: "restart_required",
    defaultValue: 24,
    help: "Projection replay lookback used when rebuilding reducer state.",
    key: "reducer_replay_window_hours",
    label: "Reducer replay window hours",
    max: 8760,
    min: 1,
    scopeOverridable: false,
    secret: false,
    type: "integer",
    unit: "hours"
  },
  {
    applyMode: "restart_required",
    defaultValue: 90,
    help: "Number of days order-management events are retained.",
    key: "event_retention_days",
    label: "Event retention days",
    max: 3650,
    min: 1,
    scopeOverridable: false,
    secret: false,
    type: "integer",
    unit: "days"
  },
  {
    applyMode: "hot_reload",
    defaultValue: 60,
    help: "Global order-management command rate limit.",
    key: "rate_limit_per_minute",
    label: "Rate limit per minute",
    max: 100000,
    min: 1,
    scopeOverridable: true,
    secret: false,
    type: "integer",
    unit: "requests_per_minute"
  },
  {
    applyMode: "hot_reload",
    defaultValue: true,
    help: "Allows diagnostics bundles to be generated from order-management state.",
    key: "diagnostics_bundle_enabled",
    label: "Diagnostics bundle enabled",
    scopeOverridable: false,
    secret: false,
    type: "boolean",
    unit: "flag"
  },
  {
    applyMode: "hot_reload",
    defaultValue: true,
    help: "Allows settings import/export workflows for order-management configuration.",
    key: "import_export_enabled",
    label: "Import export enabled",
    scopeOverridable: false,
    secret: false,
    type: "boolean",
    unit: "flag"
  }
] satisfies SettingsFieldDescriptor[];
