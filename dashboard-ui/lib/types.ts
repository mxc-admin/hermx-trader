// TypeScript shapes for the HermX dashboard JSON API.
//
// Mirrors src/dashboard.py:api_payload() and health_payload(). Field names are
// taken verbatim from the Python serializers. Most fields are optional because
// the backend degrades gracefully (an errored executor read, a missing ledger,
// a strategy file without a capital block) and emits partial objects rather than
// failing the request.

// --- /api -----------------------------------------------------------------

export interface SourceCounts {
  historical_count?: number
  backfill_count?: number
  live_count?: number
}

export interface Instrument {
  exchange?: string
  inst_id?: string
  type?: string
}

export interface Capital {
  budget_usd?: number
  reinvest?: boolean
}

/** A strategy file (strategies/*.json), schema_version 2. */
export interface Strategy {
  schema_version?: number
  strategy_id?: string
  name?: string
  indicator?: string
  timeframe?: string
  instrument?: Instrument
  capital?: Capital
  execution_mode?: string // "demo" | "live"
  submit_orders?: boolean // false = Pause (no orders submitted); default true
  effective_mode?: string // "pause" | "demo" | "live" — resolved by backend
  leverage?: number
  margin_mode?: string // "isolated" | "cross"
  notes?: string
  /** Derived symbol (strategy_asset) the dashboard keys positions/alerts by. */
  asset?: string
  // Legacy / derived fields tolerated by the renderer.
  budget_usd?: number
  okx_submit_orders?: boolean
  _path?: string
}

/** okx_live.positions[symbol] — one open/flat position per trial symbol. */
export interface LivePosition {
  inst_id?: string
  side?: string // "LONG" | "SHORT" | "FLAT"
  pos?: number
  avg_px?: number | null
  notional_usd?: number | null
  upl?: number | null
  realized_pnl?: number | null
  leverage?: string | number | null
  margin_mode?: string | null
  mark_px?: number | null
  last?: number | null
  imr?: number | null
}

export interface OkxLive {
  ok?: boolean
  error?: string | null
  generated_at?: string | null
  positions?: Record<string, LivePosition>
  account?: Record<string, unknown>
}

/** A row in okx_executions[] (unified pipeline stage="execution"). */
export interface ExecutionRow {
  received_at?: string | null
  received_colombia?: string | null
  tv_time?: string | null
  symbol?: string
  inst_id?: string | null
  signal?: string
  alert_price?: number | null
  mode?: string | null
  elapsed_ms?: number | null
  ok?: boolean | null
  status?: string | null
  policy?: string | null
  planned_notional?: number | null
  risk_weight?: number | null
  ct_val?: number | null
  okx_action?: string | null
  okx_side?: string | null
  contracts?: number | null
  notional?: number | null
  okx_price?: number | null
  slippage_pct?: number | null
  fee?: number | null
  realized_pnl?: number | null
  position_after?: string | null
  leverage?: string | number | null
  margin_mode?: string | null
  order_id?: string | null
  client_order_id?: string | null
  order_status?: string | null
  // Set when a close row is enriched from OKX order history.
  history_enriched?: boolean
  history_time_delta_ms?: number
}

/** A row in strategy_alerts[] (unified pipeline stage="strategy_match"). */
export interface StrategyAlert {
  received_at?: string | null
  received_colombia?: string | null
  strategy_id?: string
  strategy_name?: string
  asset?: string | null
  timeframe?: string | null
  side?: string
  price?: number | null
  tv_time?: string | null
  tv_time_colombia?: string | null
  duplicate?: boolean
  decision?: string | null
  mode?: string | null
  okx_mode?: string | null
  block_reason?: string | null
  latency?: number | null
}

/** A row in open_orders.rows[] (order journal, non-terminal states). */
export interface OpenOrder {
  cl_ord_id?: string
  state?: string
  symbol?: string | null
  inst_id?: string | null
  ts?: string | null
  prev_state?: string | null
}

/**
 * Rows in reconcile_alerts.rows[] / operator_alerts.rows[] are raw alerts.jsonl
 * records passed through verbatim, so their shape is open-ended. `kind` is the
 * one field the reader filters on.
 */
export interface ReconcileAlert {
  kind?: string
  [key: string]: unknown
}

export interface OperatorAlert {
  kind?: string
  [key: string]: unknown
}

/** Bounded-reader stats attached to each ledger-backed panel. */
export interface LedgerStats {
  path?: string
  exists?: boolean
  read?: number
  skipped?: number
  truncated_tail?: boolean
  more?: boolean
  /** Only present on open_orders.stats. */
  checkpoint_records?: number
}

/** executor field — explicit executor-read health verdict. */
export interface ExecutorHealth {
  ok?: boolean
  healthy?: boolean
  error?: string | null
  stale?: boolean
  degraded?: boolean
  age_seconds?: number | null
  generated_at?: string | null
}

/** ledger_health field — aggregate corrupt/skipped line counts. */
export interface LedgerHealth {
  total_skipped?: number
  truncated_tails?: number
  ledgers?: Record<
    string,
    {
      skipped?: number
      truncated_tail?: boolean
      more?: boolean
      read?: number
    }
  >
}

/** freshness field — true data age vs. the refresh interval. */
export interface Freshness {
  generated_at?: string | null
  data_at?: string | null
  age_seconds?: number | null
  stale?: boolean
  no_data?: boolean
  refresh_interval_seconds?: number
}

export interface PanelWithStats<T> {
  rows: T[]
  stats: LedgerStats
}

export interface HermesAdvisor {
  enabled?: boolean
  ok?: boolean
}

export interface ApiPayload {
  generated_at?: string
  source_counts?: SourceCounts
  strategy_overrides?: Record<string, unknown>
  backfill?: unknown
  strategies?: Strategy[]
  strategy_alerts?: StrategyAlert[]
  okx_live?: OkxLive
  okx_executions?: ExecutionRow[]
  executor?: ExecutorHealth
  hermes?: HermesAdvisor
  ledger_health?: LedgerHealth
  freshness?: Freshness
  open_orders?: PanelWithStats<OpenOrder>
  reconcile_alerts?: PanelWithStats<ReconcileAlert>
  operator_alerts?: PanelWithStats<OperatorAlert>
}

// --- /health --------------------------------------------------------------

export interface ArmState {
  kill_switch_engaged?: boolean
  live_trading_enabled?: boolean
  demo_strategies?: number
  live_strategies?: number
  armed?: boolean
}

export interface HealthPayload {
  ok?: boolean
  service?: string
  mode?: string
  policies?: string[]
  primary_policy?: string | null
  arm?: ArmState
  strategy_files?: string[]
  timestamp?: string
}
