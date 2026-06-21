import type { OrderSettingScalar } from "../../utils/api";

export type OrderSettingsCategoryKey = "entry" | "general";

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
