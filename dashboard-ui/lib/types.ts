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

/**
 * strategy.strategy_pnl — durable per-strategy P&L contract (Phase 4).
 *
 * Sums the append-only closed-trade ledger scoped to the strategy, its account
 * `mode` (demo|live), and the `accounting_start_at` clean window, then folds in the
 * live open `upl`. The `*_usd` / `closed_*` fields are Phase-3 aliases kept for
 * back-compat; the flat names below are the Phase-4 contract. `total_net = realized_net + upl`.
 */
/** One point of strategy_pnl.pnl_series — a closed episode's running total. */
export interface PnlPoint {
  closed_at_ms?: number | null
  pnl_net?: number
  cum_net?: number
}

export interface StrategyPnl {
  strategy_id?: string
  venue?: string
  mode?: string // "demo" | "live"
  accounting_start_at?: number | null
  realized_net?: number
  realized_gross?: number
  fees?: number
  upl?: number
  total_net?: number
  trade_count?: number
  last_close_at_ms?: number | null
  /** Cumulative realized-net curve over closed episodes (capped ~200 points). */
  pnl_series?: PnlPoint[]
  // Phase-3 aggregate aliases (same numbers, legacy names).
  budget_usd?: number
  closed_realized_pnl_usd?: number
  closed_fees_usd?: number
  closed_net_pnl_usd?: number
  open_upl_usd?: number
  equity_now_usd?: number
  closed_order_count?: number
}

/** portfolio — durable P&L aggregated across all strategies (Phase 4). */
export interface Portfolio {
  realized_net?: number
  realized_gross?: number
  fees?: number
  upl?: number
  total_net?: number
  trade_count?: number
  strategies?: number // count of strategies carrying P&L data
  /** Ledger rows with no strategy_id — invisible to per-strategy sums. */
  unattributed?: { count?: number; net_realized_pnl?: number; mode?: string }
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
  /** Durable per-strategy P&L contract, annotated by the backend (Phase 4). */
  strategy_pnl?: StrategyPnl
  /** Accounting-window start (ms epoch) the P&L is scoped to, or null. */
  accounting_start_at?: number | null
  /** Strategy's own venue (e.g. "okx"), annotated by the backend. */
  venue?: string
  // Legacy / derived fields tolerated by the renderer.
  budget_usd?: number
  okx_submit_orders?: boolean
  _path?: string
}

/**
 * positions.open[] / positions.closed[] — Positions-First contract.
 *
 * Closed rows are flat-to-flat episodes folded from the durable leg ledger
 * (qty = sum of close-leg fills; realized figures are the same close-leg sums
 * the strategy card aggregates). Open rows are venue-truth (qty/upl live),
 * enriched with opened_at_ms/strategy_id/entry from ledger open legs.
 */
export interface PositionRow {
  status?: string // "open" | "closed"
  strategy_id?: string | null
  venue?: string | null
  mode?: string | null
  inst_id?: string | null
  symbol?: string
  side?: string | null // "long" | "short"
  qty?: number | null
  entry_px?: number | null
  exit_px?: number | null
  opened_at_ms?: number | null
  closed_at_ms?: number | null
  realized_pnl_gross?: number | null
  fees?: number | null
  realized_pnl_net?: number | null
  upl?: number | null
  mark_px?: number | null
  notional_usd?: number | null
  open_leg_count?: number
  close_leg_count?: number
  /** Exact cl_ord_ids of the episode's open/close orders (event-filter keys). */
  open_cl_ord_ids?: string[]
  close_cl_ord_ids?: string[]
}

/** Observe-only reconcile-by-position drift row (never auto-traded). */
export interface PositionDrift {
  kind?: string
  venue?: string | null
  mode?: string | null
  inst_id?: string | null
  ledger_qty?: number | null
  venue_qty?: number | null
}

export interface PositionsContract {
  open?: PositionRow[]
  closed?: PositionRow[]
  drift?: { count?: number; rows?: PositionDrift[] }
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
  /** Joined execution outcome (exact received_at match): FILLED | BLOCKED | NO FILL. */
  outcome?: string | null
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

/**
 * executor field — explicit executor-read health verdict.
 *
 * With ≥1 active strategy the backend aggregates every active strategy's
 * (venue, mode) environment (model.py:executor_env_health_summary) and adds
 * `envs`, keyed "{venue}:{mode}" (e.g. "kucoin:live"), each value carrying that
 * environment's own summary (never nested further). With zero active
 * strategies the legacy single-snapshot shape is emitted and `envs` is absent.
 */
export interface ExecutorHealth {
  ok?: boolean
  healthy?: boolean
  error?: string | null
  stale?: boolean
  degraded?: boolean
  age_seconds?: number | null
  generated_at?: string | null
  envs?: Record<string, ExecutorHealth>
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
  portfolio?: Portfolio
  positions?: PositionsContract
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
