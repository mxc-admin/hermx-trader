"""Dashboard rendering: formatting/HTML-escape helpers, HTML section builders
and the legacy server-rendered page (REFACTOR_PLAN.md Phase 7 sub-step 3;
monolith formatting helpers ``money`` .. ``execution_badge``, HTML builders
``metric_cards`` .. ``summary_cards``, plus ``render()`` and its ``CSS``,
moved not rewritten).

PACKAGE LAYOUT NOTE: this directory deliberately has NO __init__.py. A regular
package named ``dashboard`` would shadow ``src/dashboard.py`` for every
``import dashboard`` (packages win over same-named modules on sys.path), which
would break the whole test suite and the shim design. Instead, dashboard.py
extends its own ``__path__`` to this directory, which makes
``dashboard.render`` importable as a submodule while ``import dashboard``
keeps resolving to the monolith. Do not add an __init__.py here.

Root-bound module state (``LOGS``-derived ledger paths, ``ASSET_META``,
``TRIAL_TAB_ID``, ``ORDER_TERMINAL_STATES_DASH``) and every dashboard_core /
snapshots / model function used below are read lazily via
``import dashboard as _dash`` rather than imported at module top -- matching
the snapshots.py / model.py pattern, because test fixtures
``importlib.reload(dashboard_core)`` + ``importlib.reload(dashboard)`` against
a fresh temp root and monkeypatch seams (``okx_live_snapshot``,
``active_strategies``, ``_dashboard_executor``, ...) directly on the
``dashboard`` module.

Cross-function calls within THIS module also dereference through ``_dash.``
so a patch applied to ``dashboard.<fn>`` is observed by every caller. In the
template-heavy functions the ``_dash.`` attributes are bound to locals at
function entry (``esc = _dash.esc``) instead of inline -- equivalent late
binding (re-resolved on every call), it just keeps the moved f-string bodies
byte-identical to the monolith.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone


# --- Formatting / escape helpers (monolith ``money`` .. ``execution_badge``) --

def money(value, digits=2):
    import dashboard as _dash
    value = _dash.as_float(value)
    if value is None:
        return "-"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.{digits}f}"


def pct(value):
    import dashboard as _dash
    value = _dash.as_float(value)
    if value is None:
        return "-"
    return f"{value:,.2f}%"


def num(value, digits=4):
    import dashboard as _dash
    value = _dash.as_float(value)
    if value is None:
        return "-"
    return f"{value:,.{digits}f}"


def esc(value):
    return html.escape(str(value if value is not None else ""))


def badge(text, kind="neutral"):
    import dashboard as _dash
    return f'<span class="badge {kind}">{_dash.esc(text)}</span>'


def side_kind(side):
    side = str(side or "").lower()
    if side in {"buy", "long"}:
        return "good"
    if side in {"sell", "short"}:
        return "bad"
    return "neutral"


def action_kind(action):
    text = str(action or "").upper()
    if "FLIP" in text:
        return "good"
    if "CLOSE ONLY" in text:
        return "warn"
    if "DUPLICATE" in text:
        return "muted"
    if "SKIP" in text:
        return "muted"
    if "CLOSE" in text and "OPEN" not in text:
        return "warn"
    if "OPEN_LONG" in text or "BUY" in text or "TRADE" in text:
        return "good"
    if "OPEN_SHORT" in text or "SELL" in text:
        return "bad"
    return "neutral"


def trade_effect(row):
    action = str(row.get("position_action") or "").upper()
    decision = str(row.get("decision") or "").upper()
    closes = "CLOSE_LONG" in action or "CLOSE_SHORT" in action
    opens = "OPEN_LONG" in action or "OPEN_SHORT" in action
    duplicate = "DUPLICATE" in action
    skips = "SKIP" in action or decision == "SKIP"
    if closes and opens:
        return "FLIP"
    if closes and skips and not opens:
        return "CLOSE ONLY"
    if opens and not closes:
        return "OPEN"
    if duplicate:
        return "DUPLICATE"
    if skips:
        return "SKIP"
    if closes:
        return "CLOSE"
    return decision or "-"


def first_present(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return None


def nested_get(obj, *path):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def fmt_seconds(value):
    import dashboard as _dash
    value = _dash.as_float(value)
    if value is None:
        return "-"
    if abs(value) >= 60:
        return f"{value / 60.0:,.2f}m"
    return f"{value:,.2f}s"


# --- HTML section builders (monolith ``metric_cards`` .. ``summary_cards``) ---

def metric_cards(items):
    import dashboard as _dash
    esc = _dash.esc
    return '<div class="metrics">' + ''.join(f'<div class="metric"><span>{esc(k)}</span><b>{esc(v)}</b></div>' for k, v in items.items()) + '</div>'


def reason_details(row):
    import dashboard as _dash
    esc, num, badge, trade_effect, action_kind = _dash.esc, _dash.num, _dash.badge, _dash.trade_effect, _dash.action_kind
    visible_reasons = [r for r in (row.get("reasons") or []) if "pulse" not in str(r).lower()]
    reasons = ''.join(f'<li>{esc(r)}</li>' for r in visible_reasons)
    ctx = row.get("ctx30") or {}
    ctx_line = f"Regime {ctx.get('regime','-')} / Phase {ctx.get('phase','-')} / Alignment {num(ctx.get('no_pulse_score'),2)} / RSI {num(ctx.get('jrsx'),2)} / Acc {num(ctx.get('pp_acc'),2)} / Vel {num(ctx.get('pp_vel'),2)}"
    raw = f"Decision {row.get('decision','-')} / Policy {row.get('policy_action','-')}"
    return f'<details class="why"><summary>{badge(trade_effect(row), action_kind(trade_effect(row)))}</summary><p>{esc(raw)}</p><p>{esc(ctx_line)}</p><ul>{reasons}</ul></details>'


def first_okx_trade_map(rows):
    import dashboard as _dash
    first = {}
    for row in rows:
        sym = row.get("symbol")
        if sym in first:
            continue
        if not sym or row.get("okx_price") is None:
            continue
        first[sym] = row.get("received_colombia") or _dash.colombia_time(row.get("received_at"))
    return first


def okx_live_entry_state(okx_live, rows):
    import dashboard as _dash
    live = {}
    positions = (okx_live or {}).get("positions") or {}
    for sym, pos in positions.items():
        side = str(pos.get("side") or "").upper()
        if side not in {"LONG", "SHORT"}:
            continue
        wanted_action = "OPEN_" + side
        for row in reversed(rows):
            if row.get("symbol") != sym:
                continue
            if str(row.get("okx_action") or "").upper() != wanted_action:
                continue
            if str(row.get("position_after") or "").upper() != side:
                continue
            key = row.get("order_id") or row.get("client_order_id") or f"{row.get('received_at')}:{sym}:{wanted_action}"
            live[key] = {"symbol": sym, "side": side, "open_pnl": _dash.as_float(pos.get("upl")) or 0.0}
            break
    return live


def okx_live_card(config, okx_live, sym, first_trade_time=None):
    import dashboard as _dash
    esc, num, badge, money, metric_cards = _dash.esc, _dash.num, _dash.badge, _dash.money, _dash.metric_cards
    as_float, first_present = _dash.as_float, _dash.first_present
    asset_cfg = (config.get("assets") or {}).get(sym) or {}
    meta = _dash.ASSET_META.get(sym, {"name": sym, "logo": ""})
    pos = ((okx_live or {}).get("positions") or {}).get(sym) or {}
    side = str(pos.get("side") or "UNKNOWN").upper()
    side_class = side.lower() if side in {"LONG", "SHORT"} else "flat"
    live_price = first_present(pos.get("mark_px"), pos.get("last"))
    budget = as_float(asset_cfg.get("budget_usd")) or 0.0
    realized = as_float(pos.get("realized_pnl")) or 0.0
    # TODO(Phase 2+): read net_realized_pnl from the ledger when verified
    # (pnl_ledger.net_realized_for_strategy). Gross stays displayed for now.
    upl = as_float(pos.get("upl")) or 0.0
    total_pnl = realized + upl
    budget_now = budget + total_pnl
    status_badge = badge("LIVE", "good") if side in {"LONG", "SHORT"} else badge("FLAT", "muted")
    start_value = money(budget, 0)
    if first_trade_time:
        start_value = f"{start_value} since {first_trade_time}"
    return f"""
    <section class="asset-card okx-card" data-symbol="{esc(sym)}">
      <div class="asset-head">
        <div class="asset-title">
          <img class="asset-logo" src="{esc(meta.get('logo'))}" alt="{esc(meta.get('name'))} logo" loading="lazy">
          <div>
            <h3>{esc(sym)}</h3>
            <p>{esc(meta.get('name'))}</p>
          </div>
        </div>
        <div>{badge("OKX demo", "good" if okx_live.get("ok") else "warn")} {status_badge}</div>
      </div>
      <div class="card-status">
        <div>
          <span class="label">Actual OKX position</span>
          <div class="position-line"><span class="position-pill {esc(side_class)}">{esc(side)}</span></div>
        </div>
        <div class="live-price">
          <span class="label">OKX live price</span>
          <b>{num(live_price, 4)}</b>
        </div>
      </div>
      {metric_cards({
        "Budget start": start_value,
        "Budget now": money(budget_now, 2),
        "PnL now": money(total_pnl, 2),
      })}
    </section>
    """


def okx_execution_table(rows, live_info=None):
    import dashboard as _dash
    esc, num, badge, money, pct, side_kind = _dash.esc, _dash.num, _dash.badge, _dash.money, _dash.pct, _dash.side_kind
    exchange_leg_label, exchange_leg_kind = _dash.exchange_leg_label, _dash.exchange_leg_kind
    exchange_display_status, exchange_display_status_kind = _dash.exchange_display_status, _dash.exchange_display_status_kind
    exchange_reduce_only_label, okx_row_details = _dash.exchange_reduce_only_label, _dash.okx_row_details
    if not rows:
        return '<table><tbody><tr><td>No OKX demo executions logged yet.</td></tr></tbody></table>'
    live_info = live_info or {}
    body = []
    for row in reversed(rows[-80:]):
        row_key = row.get("order_id") or row.get("client_order_id") or f"{row.get('received_at')}:{row.get('symbol')}:{row.get('okx_action')}"
        is_live = row_key in live_info
        pnl = live_info[row_key]["open_pnl"] if is_live else row.get("realized_pnl")
        show_pnl = is_live or str(row.get("okx_action") or "").upper().startswith("CLOSE")
        pnl_kind = "good" if (pnl or 0) > 0 else ("bad" if pnl is not None and pnl < 0 else "neutral")
        tr_class = ' class="live-row"' if is_live else ""
        body.append(f"""
        <tr{tr_class}>
          <td>{esc(row.get('received_colombia'))}</td>
          <td><b>{esc(row.get('symbol'))}</b></td>
          <td>{badge(row.get('signal'), side_kind(row.get('signal')))}</td>
          <td>{badge(exchange_leg_label(row), exchange_leg_kind(row))}</td>
          <td>{badge(exchange_display_status(row, is_live), exchange_display_status_kind(row, is_live))}</td>
          <td>{num(row.get('alert_price'), 4)}</td>
          <td>{num(row.get('okx_price'), 4)}</td>
          <td>{pct(row.get('slippage_pct'))}</td>
          <td>{num(row.get('contracts'), 4)}</td>
          <td>{money(row.get('notional'), 0)}</td>
          <td>{badge(exchange_reduce_only_label(row), 'muted' if exchange_reduce_only_label(row) == 'Yes' else 'neutral')}</td>
          <td>{money(row.get('fee'), 4)}</td>
          <td>{badge(money(pnl, 2) if show_pnl and pnl is not None else "-", pnl_kind)}</td>
          <td>{esc(row.get('margin_mode') or '-')} / {esc(row.get('leverage') or '-')}x</td>
          <td>{okx_row_details(row, is_live)}</td>
        </tr>
        """)
    return f"""
    <table>
      <thead><tr><th>Fecha</th><th>Asset</th><th>Signal</th><th>Leg</th><th>Status</th><th>Alert</th><th>Fill</th><th>Slip</th><th>Size</th><th>Value</th><th>RO</th><th>Fee</th><th>PnL</th><th>Mode</th><th>Details</th></tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>
    """


def metric_cards_colored(items):
    """Like metric_cards but each item is (value_str, color_kind_or_None)."""
    import dashboard as _dash
    esc = _dash.esc
    cells = []
    for label, (val, kind) in items.items():
        color_style = ""
        if kind == "good":
            color_style = " style=\"color:var(--positive)\""
        elif kind == "bad":
            color_style = " style=\"color:var(--negative)\""
        cells.append(f'<div class="metric"><span>{esc(label)}</span><b{color_style}>{esc(val)}</b></div>')
    return f'<div class="metrics">{"".join(cells)}</div>'


def strategy_card(strategy, okx_live, alerts, okx_live_by_mode=None, exch_live_by_env=None):
    import dashboard as _dash
    esc, num, badge, money, side_kind = _dash.esc, _dash.num, _dash.badge, _dash.money, _dash.side_kind
    metric_cards_colored, as_float = _dash.metric_cards_colored, _dash.as_float
    sym = strategy.get("asset")
    meta = _dash.ASSET_META.get(sym, {"name": sym, "logo": ""})
    rows = [row for row in alerts if row.get("strategy_id") == strategy.get("strategy_id")]
    # effective_mode (pause/demo/live) is annotated upstream in render(); fall back to
    # the file's execution_mode if a caller passes an un-annotated strategy.
    mode = (strategy.get("effective_mode") or strategy.get("execution_mode") or "demo").lower()
    # Phase 0.5: read positions from THIS strategy's own (venue, mode) account. Prefer
    # the per-env map; fall back to the legacy mode-only map, then to the single
    # snapshot passed in (legacy callers). A live strategy reads its venue's live
    # account; demo/pause read that venue's demo sandbox.
    if exch_live_by_env or okx_live_by_mode:
        okx_live = _dash._snapshot_for_env(exch_live_by_env, okx_live_by_mode, _dash._strategy_venue(strategy), mode)
    live = (okx_live.get("positions") or {}).get(sym) or {}
    _mode_labels = {"pause": "Pause", "demo": "Demo", "live": "Live"}
    mode_label = _mode_labels.get(mode, mode.title())
    mode_kind = "good" if mode == "live" else "muted" if mode == "pause" else "neutral"
    position = live.get("side") or "FLAT"
    is_live = position != "FLAT"
    # Phase 5 (Decision ⑤A): budget_usd from the strategy JSON stays the *seed*; the
    # dynamic layer is computed at runtime. effective_budget = seed + durable realized
    # net P&L (from the ledger, NOT the live position's realized_pnl which resets to 0
    # on FLAT and is bounded by the exchange's 100-row history). Total equity adds UPnL.
    budget_seed = as_float((strategy.get("capital") or {}).get("budget_usd") or strategy.get("budget_usd")) or 0.0
    upl = as_float(live.get("upl")) or 0.0
    mode_key = "live" if mode == "live" else "demo"  # ledger mode column is demo|live
    accounting_start = strategy.get("accounting_start_at")
    if accounting_start is None:
        accounting_start = _dash._accounting_start_for(strategy.get("strategy_id"))
    realized_net = _dash._ledger_net_realized(strategy.get("strategy_id"), mode_key, accounting_start)
    effective_budget = budget_seed + realized_net       # tradable capital (seed + realized)
    total_pnl = realized_net + upl                       # Total P&L (realized + unrealized)
    total_equity = budget_seed + total_pnl               # full account value
    live_badge = badge("LIVE", "good") if is_live else badge("FLAT", "muted")
    return f"""
    <section class="asset-card clean-card strategy-card">
      <div class="asset-head">
        <div class="asset-title">
          <img class="asset-logo" src="{esc(meta.get('logo'))}" alt="{esc(meta.get('name'))} logo" loading="lazy">
          <div>
            <h3>{esc(sym)} <span class="tf-chip">{esc(strategy.get('timeframe'))}</span></h3>
            <p>{esc(strategy.get('name') or meta.get('name'))}</p>
          </div>
        </div>
        <div>{badge(mode_label, mode_kind)} {live_badge}</div>
      </div>
      <div class="card-status">
        <div>
          <span class="metric-label">Strategy config</span>
          <div class="position-line">
            {badge(strategy.get("indicator") or "-", "neutral")}
            {badge(str(strategy.get("leverage") or "-") + "x", "neutral")}
            {badge(strategy.get("margin_mode") or "-", "neutral")}
            {badge((strategy.get("instrument") or {}).get("type") or "-", "neutral")}
            {badge((strategy.get("instrument") or {}).get("exchange") or "-", "good")}
          </div>
        </div>
        <div class="live-price">
          <span class="metric-label">Position</span>
          <b>{badge(position, "muted" if position == "FLAT" else side_kind(position))}</b>
          <span class="metric-sub">entry {num(live.get("avg_px"), 4)}</span>
        </div>
      </div>
      {metric_cards_colored({
        "Seed budget": (money(budget_seed, 0), None),
        "Realized P&L": (money(realized_net, 2), "good" if realized_net > 0 else ("bad" if realized_net < 0 else None)),
        "UPnL": (money(upl, 2), "good" if upl > 0 else ("bad" if upl < 0 else None)),
        "Effective budget": (money(effective_budget, 2), None),
        "Total equity": (money(total_equity, 2), "good" if total_pnl > 0 else ("bad" if total_pnl < 0 else None)),
        "Mark price": (num(live.get("last"), 4), None),
        "Alerts": (str(len(rows)), None),
      })}
    </section>
    """


def strategy_alert_table(rows):
    import dashboard as _dash
    esc, num, badge, side_kind, fmt_seconds = _dash.esc, _dash.num, _dash.badge, _dash.side_kind, _dash.fmt_seconds
    if not rows:
        return '<table><tbody><tr><td>No Duo Base Dev strategy alerts yet.</td></tr></tbody></table>'
    body = []
    for row in reversed(rows[-120:]):
        decision = "DUPLICATE" if row.get("duplicate") else (row.get("decision") or "ACCEPTED")
        body.append(f"""
        <tr>
          <td>{esc(row.get('tv_time_colombia') or row.get('received_colombia'))}</td>
          <td>{esc(row.get('received_colombia'))}</td>
          <td>{esc(row.get('strategy_name'))}</td>
          <td><b>{esc(row.get('asset'))}</b></td>
          <td>{esc(row.get('timeframe'))}</td>
          <td>{badge(row.get('side'), side_kind(row.get('side')))}</td>
          <td>{num(row.get('price'), 4)}</td>
          <td>{badge(decision, "muted" if row.get("duplicate") else "good")}</td>
          <td>{fmt_seconds(row.get('latency'))}</td>
          <td>{esc(row.get('okx_mode') or '-')}</td>
          <td>{esc(row.get('block_reason') or '-')}</td>
        </tr>
        """)
    return f"""
    <table>
      <thead><tr><th>TV time</th><th>Received</th><th>Strategy</th><th>Asset</th><th>TF</th><th>Signal</th><th>TV price</th><th>Trial decision</th><th>Latency</th><th>OKX mode</th><th>Safety reason</th></tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>
    """


def strategy_execution_rows(strategy, okx_executions):
    strategy_id = strategy.get("strategy_id")
    expected_policy = f"strategy_file:{strategy_id}"
    return [
        row for row in (okx_executions or [])
        if row.get("policy") == expected_policy or str(row.get("policy") or "").endswith(str(strategy_id or ""))
    ]


# ---------------------------------------------------------------------------
# Order / reconcile / operator observability panels (read-only). These fold the
# bounded tail of the receiver's order-journal + alert ledgers via the same bounded
# reader the rest of the dashboard uses (read_jsonl_stats), so a huge ledger can never
# OOM the dashboard and corrupt/truncated lines are surfaced, not hidden. The dashboard
# stays a pure consumer -- no submit/execute/cancel controls.
# ---------------------------------------------------------------------------

def _read_order_checkpoint_index():
    """Read the receiver's verified order-journal checkpoint and return its
    ``index_records`` (latest record per cl_ord_id for every sealed/folded order).

    The checkpoint is a single JSON object (NOT JSONL), so it is read directly rather
    than via the bounded JSONL reader. Missing/unreadable/malformed checkpoint => empty
    list, so the panel degrades to a live-segment-only read exactly as before rotation."""
    import dashboard as _dash
    path = _dash.ORDER_JOURNAL_CHECKPOINT_FILE
    if not path.exists():
        return []
    try:
        ckpt = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(ckpt, dict):
        return []
    records = ckpt.get("index_records")
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict)]


def order_journal_open_orders(limit=300):
    """Latest record per cl_ord_id across the verified checkpoint AND the live-segment
    tail, filtered to non-terminal (PLANNED/SUBMITTED/UNKNOWN) open orders. Merging the
    checkpoint is what keeps the panel correct after the journal rotates -- an order
    whose latest record sealed into a segment lives only in the checkpoint's index.
    Returns ``(open_orders, stats)``; stats carries the live-segment read/skipped/
    truncated counts plus ``checkpoint_records`` for the count merged from the checkpoint."""
    import dashboard as _dash
    rows, stats = _dash.read_jsonl_stats(_dash.ORDER_JOURNAL_FILE, limit)
    checkpoint_records = _dash._read_order_checkpoint_index()
    stats = dict(stats)
    stats["checkpoint_records"] = len(checkpoint_records)
    latest = {}
    # Seed with the checkpoint first; live-tail records (newer seq) then win the merge.
    for rec in [*checkpoint_records, *rows]:
        if not isinstance(rec, dict):
            continue
        seq = rec.get("seq")
        if not isinstance(seq, int):
            continue
        cl = rec.get("cl_ord_id")
        cur = latest.get(cl)
        if cur is None or seq > int(cur.get("seq") or -1):
            latest[cl] = rec
    open_orders = []
    for cl, rec in latest.items():
        state = str(rec.get("state") or "").upper()
        if state in _dash.ORDER_TERMINAL_STATES_DASH:
            continue
        intent = rec.get("intent") or {}
        open_orders.append({
            "cl_ord_id": cl,
            "state": state,
            "symbol": intent.get("symbol"),
            "inst_id": intent.get("inst_id"),
            "ts": rec.get("ts"),
            "prev_state": rec.get("prev_state"),
        })
    open_orders.sort(
        key=lambda r: _dash.parse_dt(r.get("ts")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return open_orders, stats


def reconcile_alert_records(limit=60):
    import dashboard as _dash
    rows, stats = _dash._alerts_rows("reconcile", limit)
    return list(reversed(rows)), stats


def operator_alert_records(limit=60):
    import dashboard as _dash
    rows, stats = _dash._alerts_rows("operator", limit)
    return list(reversed(rows)), stats


def _ledger_stat_note(stats):
    import dashboard as _dash
    bits = [f"{int(stats.get('read') or 0)} rows"]
    if stats.get("skipped"):
        bits.append(f"{int(stats['skipped'])} corrupt")
    if stats.get("truncated_tail"):
        bits.append("truncated tail")
    if stats.get("more"):
        bits.append("older rows beyond window")
    kind = "warn" if (stats.get("skipped") or stats.get("truncated_tail")) else "neutral"
    return _dash.badge(" · ".join(bits), kind)


def _alert_detail_str(detail):
    import dashboard as _dash
    if not isinstance(detail, dict):
        return _dash.esc(detail)
    return _dash.esc(", ".join(f"{k}={detail[k]}" for k in list(detail)[:8]))


def _state_kind(state):
    s = str(state or "").upper()
    if s == "SUBMITTED":
        return "good"
    if s == "UNKNOWN":
        return "warn"
    if s == "PLANNED":
        return "neutral"
    return "neutral"


def open_orders_table(open_orders):
    import dashboard as _dash
    esc, badge, display_time, _state_kind = _dash.esc, _dash.badge, _dash.display_time, _dash._state_kind
    if not open_orders:
        return '<table><tbody><tr><td>No open orders (all PLANNED/SUBMITTED/UNKNOWN orders resolved).</td></tr></tbody></table>'
    body = []
    for row in open_orders:
        body.append(f"""
        <tr>
          <td>{esc(display_time(row.get('ts')))}</td>
          <td><code>{esc(row.get('cl_ord_id'))}</code></td>
          <td><b>{esc(row.get('symbol') or '-')}</b></td>
          <td>{esc(row.get('inst_id') or '-')}</td>
          <td>{badge(row.get('state'), _state_kind(row.get('state')))}</td>
          <td>{esc(row.get('prev_state') or '-')}</td>
        </tr>
        """)
    return f"""
    <table>
      <thead><tr><th>Updated</th><th>clOrdId</th><th>Symbol</th><th>Instrument</th><th>State</th><th>Prev</th></tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>
    """


def reconcile_alert_table(rows):
    import dashboard as _dash
    esc, badge, display_time = _dash.esc, _dash.badge, _dash.display_time
    if not rows:
        return '<table><tbody><tr><td>No reconcile alerts.</td></tr></tbody></table>'
    body = []
    for row in rows:
        detail = row.get("detail") or {}
        body.append(f"""
        <tr>
          <td>{esc(display_time(row.get('ts')))}</td>
          <td>{badge(row.get('alert'), 'warn')}</td>
          <td>{esc(detail.get('stage') or '-')}</td>
          <td><code>{esc(detail.get('cl_ord_id') or '-')}</code></td>
          <td>{esc(detail.get('symbol') or '-')}</td>
          <td>{esc(detail.get('reason') or detail.get('reconciled_state') or '-')}</td>
        </tr>
        """)
    return f"""
    <table>
      <thead><tr><th>Time</th><th>Alert</th><th>Stage</th><th>clOrdId</th><th>Symbol</th><th>Reason</th></tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>
    """


def operator_alert_table(rows):
    import dashboard as _dash
    esc, badge, display_time, _alert_detail_str = _dash.esc, _dash.badge, _dash.display_time, _dash._alert_detail_str
    if not rows:
        return '<table><tbody><tr><td>No operator alerts.</td></tr></tbody></table>'
    body = []
    for row in rows:
        sev = str(row.get("severity") or "warning").lower()
        sev_kind = "bad" if sev == "error" else "warn"
        body.append(f"""
        <tr>
          <td>{esc(display_time(row.get('ts')))}</td>
          <td>{badge(sev, sev_kind)}</td>
          <td>{badge(row.get('alert'), 'neutral')}</td>
          <td>{_alert_detail_str(row.get('detail'))}</td>
        </tr>
        """)
    return f"""
    <table>
      <thead><tr><th>Time</th><th>Severity</th><th>Alert</th><th>Detail</th></tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>
    """


def order_state_section():
    """The read-only Order / Reconcile / Operator observability section."""
    import dashboard as _dash
    _ledger_stat_note = _dash._ledger_stat_note
    open_orders, oo_stats = _dash.order_journal_open_orders()
    recon, rc_stats = _dash.reconcile_alert_records()
    ops, op_stats = _dash.operator_alert_records()
    return f"""
    <section class="subsection">
      <div class="log-head">
        <div>
          <h3>Order &amp; Reconcile State</h3>
          <p>Read-only view of the submission state machine and reconciliation alerts. The dashboard never submits or cancels.</p>
        </div>
      </div>
      <section class="trade-log-card nested">
        <div class="log-head">
          <h3>Open Orders <span class="sub">{_ledger_stat_note(oo_stats)}</span></h3>
          <p>Non-terminal orders (PLANNED / SUBMITTED / UNKNOWN) from order-journal.jsonl, latest state per clOrdId.</p>
        </div>
        <div class="table-wrap unified-log">{_dash.open_orders_table(open_orders)}</div>
      </section>
      <section class="trade-log-card nested">
        <div class="log-head">
          <h3>Reconcile Alerts <span class="sub">{_ledger_stat_note(rc_stats)}</span></h3>
          <p>Latest entries from alerts.jsonl (kind=reconcile: mismatches, resolver timeouts).</p>
        </div>
        <div class="table-wrap unified-log">{_dash.reconcile_alert_table(recon)}</div>
      </section>
      <section class="trade-log-card nested">
        <div class="log-head">
          <h3>Operator Alerts <span class="sub">{_ledger_stat_note(op_stats)}</span></h3>
          <p>Latest entries from alerts.jsonl (kind=operator: auth, queue, resolver, never_submitted).</p>
        </div>
        <div class="table-wrap unified-log">{_dash.operator_alert_table(ops)}</div>
      </section>
    </section>
    """


def strategy_trial_tab(strategies, alerts, okx_live, okx_executions, okx_live_by_mode=None, exch_live_by_env=None):
    import dashboard as _dash
    badge = _dash.badge
    cards = ''.join(
        _dash.strategy_card(strategy, okx_live, alerts, okx_live_by_mode, exch_live_by_env)
        for strategy in strategies
    )
    strategy_rows = []
    for strategy in strategies:
        strategy_rows.extend(_dash.strategy_execution_rows(strategy, okx_executions))
    strategy_rows.sort(key=lambda row: _dash.parse_dt(row.get("received_at")) or datetime.min.replace(tzinfo=timezone.utc))
    live_info = _dash.okx_live_entry_state(okx_live, strategy_rows)
    return f"""
    <section class="tab-panel" id="{_dash.TRIAL_TAB_ID}">
      <div class="section-head">
        <div>
          <h2>Duo Base Dev Trial</h2>
          <p>Strategy-file-driven trial. Alerts must include strategy_id. This view is demo-only until explicit promotion.</p>
        </div>
        <div>{badge(str(len(strategies)) + " strategy files", "good")} {badge("founder package pending", "warn")}</div>
      </div>
      <div class="asset-grid">{cards}</div>
      <section class="subsection okx-section">
        <div class="log-head">
          <h3>Strategy Demo Ledger</h3>
          <p>Actual sandbox submissions for the strategy-file trial. Open rows can show live PnL while the position remains active.</p>
        </div>
        <div class="table-wrap unified-log">{_dash.okx_execution_table(strategy_rows, live_info)}</div>
      </section>
      <section class="subsection">
        <div class="log-head">
          <h3>Strategy Alert Log</h3>
          <p>Only valid Duo Base Dev alerts appear here. Invalid strategy alerts are quarantined and never routed to OKX.</p>
        </div>
        <div class="table-wrap unified-log">{_dash.strategy_alert_table(alerts)}</div>
      </section>
      {_dash.order_state_section()}
    </section>
    """


def banner(text, kind="warn"):
    import dashboard as _dash
    return f'<div class="banner {kind}">{_dash.esc(text)}</div>'


def status_banners(model):
    """Explicit banners for executor failure / stale data / corrupt ledgers."""
    import dashboard as _dash
    banner, human_age = _dash.banner, _dash.human_age
    out = []
    execu = model.get("executor") or {}
    fresh = model.get("freshness") or {}
    ledger = model.get("ledger_health") or {}
    if execu.get("error"):
        out.append(banner(f"EXECUTOR ERROR — exchange data unavailable / stale ({execu.get('error')})", "bad"))
    elif execu.get("stale"):
        out.append(banner(f"EXECUTOR DATA STALE — last exchange read {human_age(execu.get('age_seconds'))} ago", "warn"))
    if ledger.get("total_skipped"):
        out.append(banner(f"{ledger['total_skipped']} ledger lines skipped (corrupt) — see /dashboard/api ledger_health", "warn"))
    if fresh.get("stale"):
        if fresh.get("no_data"):
            out.append(banner("No recent data — dashboard has not received any alerts yet", "warn"))
        else:
            out.append(banner(f"DATA MAY BE STALE — newest data is {human_age(fresh.get('age_seconds'))} old (refresh {fresh.get('refresh_interval_seconds')}s)", "warn"))
    return "".join(out)


def strategy_indicator_label(strategies):
    indicators = {(s.get("indicator") or "").strip() for s in (strategies or []) if s.get("indicator")}
    if len(indicators) == 1:
        return indicators.pop()
    return f"{len(indicators)} indicators" if indicators else "—"


def summary_cards(model):
    import dashboard as _dash
    esc = _dash.esc
    okx_live = model.get("okx_live") or {}
    strategies = model.get("active_strategies") or []
    executor = model.get("executor") or {}

    # Card 1: System status
    live_enabled, _ = _dash.live_trading_enabled()
    demo_count = sum(1 for s in strategies if (s.get("execution_mode") or "demo") != "live")
    live_count_s = sum(1 for s in strategies if (s.get("execution_mode") or "demo") == "live")
    if live_enabled and live_count_s > 0:
        sys_label, sys_kind = "ARMED", "good"
    elif strategies:
        sys_label, sys_kind = "DEMO", "warn"
    else:
        sys_label, sys_kind = "DISARMED", "bad"

    # Card 2: Active strategies
    strat_label = str(len(strategies))
    strat_sub = f"{demo_count} demo / {live_count_s} live"
    strat_kind = "good" if strategies else "muted"

    # Card 3: Open positions
    positions = (okx_live.get("positions") or {})
    open_pos = {sym: p for sym, p in positions.items() if (p.get("side") or "FLAT") != "FLAT"}
    longs = sum(1 for p in open_pos.values() if p.get("side") == "LONG")
    shorts = sum(1 for p in open_pos.values() if p.get("side") == "SHORT")
    if open_pos:
        pos_label = f"{len(open_pos)} OPEN"
        pos_sub = f"{longs}L / {shorts}S"
        pos_kind = "good"
    else:
        pos_label = "ALL FLAT"
        pos_sub = "no open positions"
        pos_kind = "muted"

    # Card 4: Executor health
    exec_ok = executor.get("ok", False)
    exec_err = executor.get("error")
    # Staleness for THIS card is executor health, not alert freshness. `fresh`
    # ("stale") tracks the signal-bar freshness clock (used by the freshness card /
    # STALE badge elsewhere); the executor summary carries its own "stale" flag
    # (dashboard/model.py:360 aggregates per-(venue,mode) env staleness). Sourcing it
    # from `fresh` here mislabels the engine STALE whenever alerts are quiet even if
    # the executor is healthy, and misses a genuinely stale executor when alerts flow.
    stale = executor.get("stale", False)
    if stale:
        exec_label, exec_kind = "STALE", "bad"
    elif not exec_ok:
        exec_label, exec_kind = "ERROR", "bad"
    else:
        exec_label, exec_kind = "OK", "good"
    exec_sub = esc(str(exec_err)[:40]) if exec_err else "executor healthy"

    def card(icon_label, value, sub, kind):
        bar_color = "var(--positive)" if kind == "good" else ("var(--negative)" if kind == "bad" else ("var(--warning)" if kind == "warn" else "var(--text-muted)"))
        return f"""<div class="metric-card" style="border-top:3px solid {bar_color}">
  <span class="metric-label">{esc(icon_label)}</span>
  <b class="metric-value" style="color:{bar_color}">{esc(value)}</b>
  <span class="metric-sub">{sub}</span>
</div>"""

    return f"""<div class="summary-metrics">
  {card("SYSTEM STATUS", sys_label, f"{len(strategies)} strategies active", sys_kind)}
  {card("ACTIVE STRATEGIES", strat_label, strat_sub, strat_kind)}
  {card("OPEN POSITIONS", pos_label, pos_sub, pos_kind)}
  {card("EXECUTION ENGINE", exec_label, exec_sub, exec_kind)}
</div>"""


def render():
    import dashboard as _dash
    esc, badge, display_time, human_age = _dash.esc, _dash.badge, _dash.display_time, _dash.human_age
    TRIAL_TAB_ID = _dash.TRIAL_TAB_ID
    model = _dash.dashboard_model()
    cfg = model["config"]
    okx_live = model.get("okx_live") or {}
    okx_executions = model.get("okx_executions") or []
    # Annotate each strategy with its effective UI mode (pause/demo/live) so the card
    # badge reflects overrides + the file's submit_orders, mirroring api_payload.
    _ctrl = _dash._load_control_state()
    _overrides = _ctrl.get("strategy_overrides") if isinstance(_ctrl.get("strategy_overrides"), dict) else {}
    strategies = []
    for _s in model.get("active_strategies") or []:
        _s = dict(_s)
        _s["effective_mode"] = _dash._effective_strategy_mode(_s, _overrides)
        _s["venue"] = _dash._strategy_venue(_s)
        _s["okx_account_source"] = "live" if _s["effective_mode"] == "live" else "demo"
        strategies.append(_s)
    strategy_alerts = model.get("strategy_alerts") or []
    source_line = (
        f"{len(strategies)} active strategies | "
        f"{len(strategy_alerts)} strategy alerts received"
    )
    warn = _dash.status_banners(model)
    execution_cfg = cfg.get("execution") or {}
    okx_enabled = bool(execution_cfg.get("enabled"))
    okx_badge = badge("Execution enabled", "good") if okx_enabled else badge("Orders disabled", "warn")
    fresh = model.get("freshness") or {}
    updated_text = "Updated " + (display_time(model["generated_at"]) or model["generated_at"])
    if fresh.get("age_seconds") is not None:
        updated_text += f" · data age {human_age(fresh.get('age_seconds'))}"
    updated_badge = badge(updated_text, "warn" if fresh.get("stale") else "neutral")
    stale_badge = badge("STALE", "bad") if fresh.get("stale") else ""
    tabs = f'<button class="tab-btn" data-target="{TRIAL_TAB_ID}">Duo Base Dev Trial</button>'
    panels = _dash.strategy_trial_tab(
        strategies, strategy_alerts, okx_live, okx_executions,
        okx_live_by_mode=model.get("okx_live_by_mode"),
        exch_live_by_env=model.get("exch_live_by_env"),
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kinetic Flow Execution Dashboard</title>
  <style>{_dash.CSS}</style>
</head>
<body>
  <main>
    <header>
      <div>
        <p class="eyebrow">KINETIC FLOW</p>
        <h1>Execution Dashboard</h1>
        <p class="subtitle">{esc(source_line)}</p>
      </div>
      <div class="header-metrics">
        <span>{badge("Strategy files", "good")}</span>
        <span>{badge(_dash.strategy_indicator_label(strategies), "neutral")}</span>
        <span>{okx_badge}</span>
        <span>{updated_badge}</span>
        {("<span>" + stale_badge + "</span>") if stale_badge else ""}
      </div>
    </header>
    {warn}
    {_dash.summary_cards(model)}
    <nav class="tabs">{tabs}</nav>
    {panels}
  </main>
  <script>
    let buttons = [];
    let panels = [];
    let validTabs = new Set();
    let subButtons = [];
    let subPanels = [];
    function show(id) {{
      if (!validTabs.has(id)) id = '{TRIAL_TAB_ID}';
      panels.forEach(p => p.classList.toggle('active', p.id === id));
      buttons.forEach(b => b.classList.toggle('active', b.dataset.target === id));
      localStorage.setItem('clean_dashboard_tab', id);
      ensureSubtab(id);
    }}
    function showSubtab(parent, id) {{
      const parentPanels = subPanels.filter(p => p.dataset.parent === parent);
      const valid = new Set(parentPanels.map(p => p.id));
      if (!valid.has(id)) id = parent + '-paper';
      parentPanels.forEach(p => p.classList.toggle('active', p.id === id));
      subButtons
        .filter(b => b.dataset.parent === parent)
        .forEach(b => b.classList.toggle('active', b.dataset.target === id));
      localStorage.setItem('clean_dashboard_subtab_' + parent, id);
    }}
    function ensureSubtab(parent) {{
      const saved = localStorage.getItem('clean_dashboard_subtab_' + parent);
      showSubtab(parent, saved || parent + '-paper');
    }}
    function bindDashboard() {{
      buttons = [...document.querySelectorAll('.tab-btn')];
      panels = [...document.querySelectorAll('.tab-panel')];
      subButtons = [...document.querySelectorAll('.subtab-btn')];
      subPanels = [...document.querySelectorAll('.subtab-panel')];
      validTabs = new Set(buttons.map(b => b.dataset.target));
      const savedTab = localStorage.getItem('clean_dashboard_tab');
      const active = validTabs.has(savedTab) ? savedTab : '{TRIAL_TAB_ID}';
      buttons.forEach(b => b.addEventListener('click', () => show(b.dataset.target)));
      subButtons.forEach(b => b.addEventListener('click', () => showSubtab(b.dataset.parent, b.dataset.target)));
      show(active);
    }}
    async function refreshSilently() {{
      const activeTab = localStorage.getItem('clean_dashboard_tab') || '{TRIAL_TAB_ID}';
      const scrollY = window.scrollY;
      try {{
        const response = await fetch(location.pathname + '?_=' + Date.now(), {{cache: 'no-store'}});
        if (!response.ok) return;
        const html = await response.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const nextMain = doc.querySelector('main');
        const currentMain = document.querySelector('main');
        if (!nextMain || !currentMain) return;
        currentMain.replaceWith(nextMain);
        bindDashboard();
        show(activeTab);
        window.scrollTo(0, scrollY);
      }} catch (err) {{
        console.debug('silent refresh failed', err);
      }}
    }}
    bindDashboard();
    setInterval(refreshSilently, 20000);
  </script>
</body>
</html>"""


CSS = """
:root {
  /* Kinetic palette */
  --bg-base:#0a0f14; --bg-panel:#0f1519; --bg-panel-raised:#151c22;
  --border-dim:#1e293b; --border-focus:#05AD98;
  --text-primary:#e2e8f0; --text-secondary:#94a3b8; --text-muted:#475569;
  --positive:#05AD98; --negative:#E85D6C; --warning:#F5A623; --info:#8B5CF6;
  /* aliases for legacy selectors */
  --bg:var(--bg-base); --panel:var(--bg-panel); --panel2:var(--bg-panel-raised);
  --line:var(--border-dim); --text:var(--text-primary); --muted:var(--text-secondary);
  --green:var(--positive); --red:var(--negative); --yellow:var(--warning);
  --blue:#78b7ff;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-family:Inter, Segoe UI, Arial, sans-serif; font-size:13px; }
main { width:min(1880px, calc(100vw - 32px)); margin:0 auto; padding:22px 0 40px; }
header { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom:14px; }
h1,h2,h3,p { margin:0; }
h1 { font-size:25px; letter-spacing:0; }
h2 { font-size:17px; }
h3 { font-size:15px; }
.eyebrow { color:var(--blue); font-size:11px; font-weight:800; letter-spacing:.12em; margin-bottom:5px; }
.subtitle, .sub, .section-head p { color:var(--muted); line-height:1.45; }
.header-metrics { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
.notice { border:1px solid var(--line); background:var(--panel); padding:12px 14px; border-radius:8px; margin:12px 0; color:var(--muted); }
.notice.warn { border-color:rgba(242,201,76,.45); background:rgba(242,201,76,.08); color:#f4df91; }
.tabs { display:flex; gap:8px; margin:16px 0; }
.tab-btn { cursor:pointer; border:1px solid var(--line); background:var(--panel); color:var(--muted); padding:9px 13px; border-radius:7px; font-weight:800; }
.tab-btn.active { color:var(--text); border-color:#4c8ed9; background:#132235; }
.legacy-tab { opacity:.72; }
.tf-chip { color:#9fc7ef; font-size:13px; font-weight:850; margin-left:4px; }
.tab-panel { display:none; }
.tab-panel.active { display:block; }
.subtabs { display:flex; gap:7px; margin:10px 0 12px; padding:5px; width:max-content; max-width:100%; overflow:auto; border:1px solid var(--line); background:#0b1119; border-radius:8px; }
.subtab-btn { cursor:pointer; border:0; background:transparent; color:var(--muted); padding:8px 11px; border-radius:6px; font-weight:850; white-space:nowrap; }
.subtab-btn.active { color:var(--text); background:#1a2a3d; box-shadow:inset 0 0 0 1px #385678; }
.subtab-panel { display:none; }
.subtab-panel.active { display:block; }
.section-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin:10px 0 12px; }
.subsection { border:1px solid var(--line); background:rgba(16,23,32,.66); border-radius:8px; padding:12px; margin:12px 0; }
.subsection .metrics { margin-top:8px; }
.okx-section { border-color:rgba(64,217,123,.28); background:rgba(64,217,123,.04); }
.comparison-section { border-color:rgba(120,183,255,.28); background:rgba(120,183,255,.04); }
.nested { margin-top:12px; background:#0b1119; }
.metrics { display:grid; grid-template-columns:repeat(9,minmax(110px,1fr)); gap:8px; margin:10px 0 12px; }
.metric { border:1px solid var(--line); background:var(--panel2); border-radius:7px; padding:10px; min-height:58px; }
.metric span { display:block; color:var(--muted); font-size:11px; margin-bottom:7px; }
.metric b { font-size:15px; }
.asset-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; align-items:start; }
#duo_base_dev_trial > .asset-grid { grid-template-columns:repeat(4,minmax(0,1fr)); }
.asset-card { border:1px solid var(--line); background:var(--panel); border-radius:8px; padding:12px; min-width:0; }
.strategy-card { padding:10px; }
.okx-card { background:#0c1513; border-color:rgba(64,217,123,.22); }
.asset-head { display:flex; justify-content:space-between; gap:10px; align-items:flex-start; margin-bottom:10px; }
.asset-title { display:flex; align-items:center; gap:10px; min-width:0; }
.strategy-card .asset-head { gap:8px; margin-bottom:8px; }
.strategy-card .asset-title { gap:8px; }
.strategy-card h3 { font-size:14px; }
.strategy-card .asset-title p { display:none; }
.asset-title p { color:var(--muted); margin-top:3px; font-size:12px; }
.asset-logo { width:32px; height:32px; border-radius:50%; object-fit:contain; background:#0b1018; border:1px solid var(--line); padding:3px; flex:0 0 auto; }
.strategy-card .asset-logo { width:28px; height:28px; }
.card-status { display:grid; grid-template-columns:1fr auto; gap:12px; align-items:center; padding:10px; border:1px solid var(--line); border-radius:8px; background:#0b1119; margin-bottom:10px; }
.strategy-card .card-status { gap:8px; padding:8px; margin-bottom:8px; }
.label { display:block; color:var(--muted); font-size:11px; margin-bottom:6px; }
.position-line { min-height:28px; display:flex; align-items:center; gap:8px; margin-bottom:8px; }
.strategy-card .position-line { min-height:22px; gap:5px; margin-bottom:0; flex-wrap:wrap; }
.position-pill { display:inline-flex; align-items:center; justify-content:center; min-height:28px; padding:5px 9px; border-radius:7px; font-size:12px; font-weight:900; text-decoration:none; border:1px solid var(--line); }
.position-pill.long { color:#b9f9cc; background:rgba(64,217,123,.16); border-color:rgba(64,217,123,.45); }
.position-pill.short { color:#ffd0d5; background:rgba(255,92,105,.15); border-color:rgba(255,92,105,.48); }
.position-pill.flat { color:#c3ccd8; background:#2a3440; border-color:#3a4654; }
.live-price { min-width:118px; text-align:right; }
.live-price b { font-size:16px; }
.asset-card .metrics { grid-template-columns:repeat(4,minmax(0,1fr)); }
.strategy-card .metrics { grid-template-columns:repeat(5,minmax(0,1fr)); gap:6px; margin:8px 0 0; }
.strategy-card .metric { min-height:52px; padding:8px; }
.strategy-card .metric span { margin-bottom:5px; }
.strategy-card .metric b { font-size:14px; }
.trade-log-card { border:1px solid var(--line); background:var(--panel); border-radius:8px; padding:12px; margin-top:12px; }
.log-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-end; margin-bottom:8px; }
.log-head p { color:var(--muted); font-size:12px; max-width:720px; line-height:1.4; text-align:right; }
.table-wrap { overflow:auto; border:1px solid var(--line); border-radius:7px; margin-top:10px; max-height:520px; }
.compact-table { max-height:300px; }
table { width:100%; border-collapse:collapse; min-width:1120px; }
th, td { padding:8px 9px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; white-space:nowrap; }
th { position:sticky; top:0; background:#0d141d; z-index:1; color:#bcd0e8; font-size:11px; }
td { color:#dce8f7; font-size:12px; }
tr:target { outline:2px solid var(--yellow); outline-offset:-2px; background:rgba(242,201,76,.10); }
tr:target td { background:rgba(242,201,76,.08); }
.live-row td { background:rgba(64,217,123,.055); }
.live-dot { display:inline-block; width:9px; height:9px; margin-right:6px; border-radius:999px; background:var(--green); box-shadow:0 0 0 3px rgba(64,217,123,.12); vertical-align:middle; }
.badge { display:inline-flex; align-items:center; justify-content:center; min-height:22px; padding:3px 7px; border-radius:6px; border:1px solid var(--line); font-size:11px; font-weight:900; white-space:nowrap; }
.badge.good { color:#b9f9cc; background:rgba(64,217,123,.16); border-color:rgba(64,217,123,.45); }
.badge.bad { color:#ffd0d5; background:rgba(255,92,105,.15); border-color:rgba(255,92,105,.48); }
.badge.warn { color:#ffe59b; background:rgba(242,201,76,.15); border-color:rgba(242,201,76,.42); }
.badge.muted { color:#c3ccd8; background:#2a3440; border-color:#3a4654; }
.badge.neutral { color:#dbe9ff; background:#1d2938; border-color:#34465c; }
.banner { margin:10px 0; padding:10px 14px; border-radius:8px; font-size:13px; font-weight:800; border:1px solid var(--line); }
.banner.bad { color:#ffd0d5; background:rgba(255,92,105,.14); border-color:rgba(255,92,105,.55); }
.banner.warn { color:#ffe59b; background:rgba(242,201,76,.12); border-color:rgba(242,201,76,.5); }
.why summary { list-style:none; cursor:pointer; }
.why summary::-webkit-details-marker { display:none; }
.why p { color:var(--blue); margin:8px 0 4px; white-space:normal; max-width:520px; }
.why ul { margin:0; padding-left:18px; color:var(--muted); white-space:normal; min-width:340px; max-width:560px; }
.why li { margin:4px 0; line-height:1.35; }
.row-details summary { cursor:pointer; list-style:none; width:24px; height:24px; display:inline-flex; align-items:center; justify-content:center; border:1px solid var(--line); border-radius:999px; color:#bcd0e8; background:#111b27; font-weight:900; }
.row-details summary::-webkit-details-marker { display:none; }
.row-details p { min-width:280px; max-width:520px; color:var(--muted); white-space:normal; line-height:1.35; margin:8px 0; }
.row-details pre { min-width:340px; max-width:620px; max-height:280px; overflow:auto; margin:8px 0 0; padding:10px; border:1px solid var(--line); border-radius:7px; background:#08111b; color:#dce8f7; white-space:pre-wrap; line-height:1.35; }
@media (max-width: 1180px) {
  .asset-grid, #duo_base_dev_trial > .asset-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .metrics { grid-template-columns:repeat(3,minmax(0,1fr)); }
}
@media (max-width: 720px) {
  main { width:calc(100vw - 18px); padding-top:12px; }
  header { flex-direction:column; }
  .asset-grid, #duo_base_dev_trial > .asset-grid { grid-template-columns:1fr; }
  .metrics, .asset-card .metrics, .strategy-card .metrics { grid-template-columns:repeat(2,minmax(0,1fr)); }
}
/* ── Kinetic component classes ─────────────────────────── */
.metric-card {
  display:flex; flex-direction:column; gap:4px;
  padding:12px 16px;
  background:var(--bg-panel-raised);
  border:1px solid var(--border-dim);
  border-radius:4px; min-width:0;
}
.metric-label {
  font-size:10px; font-weight:600;
  font-family:var(--font-mono,monospace);
  letter-spacing:.1em; color:var(--text-muted);
  text-transform:uppercase;
}
.metric-value {
  font-size:20px; font-weight:600;
  font-family:var(--font-mono,monospace);
  color:var(--text-primary); line-height:1.2;
}
.metric-sub {
  font-size:11px;
  font-family:var(--font-mono,monospace);
  color:var(--text-secondary);
}
.section-header {
  font-size:11px; font-weight:600;
  font-family:var(--font-mono,monospace);
  letter-spacing:.12em; color:var(--text-muted);
  text-transform:uppercase;
  padding-bottom:8px;
  border-bottom:1px solid var(--border-dim);
  margin-bottom:16px;
}
.level-badge {
  display:inline-flex; align-items:center;
  padding:2px 10px; border-radius:3px;
  font-size:11px; font-weight:600;
  font-family:var(--font-mono,monospace);
  letter-spacing:.08em; text-transform:uppercase;
}
.signal-badge {
  display:inline-flex; align-items:center; gap:4px;
  padding:2px 8px; border-radius:3px;
  font-size:10px; font-weight:700;
  font-family:var(--font-mono,monospace);
  letter-spacing:.1em; text-transform:uppercase;
  border:1px solid;
}
.bar-track {
  height:8px; background:var(--border-dim);
  border-radius:4px; overflow:hidden;
}
.bar-fill {
  height:100%; border-radius:4px;
  transition:width .6s cubic-bezier(.25,1,.5,1);
}
.status-indicator {
  display:inline-flex; align-items:center; gap:6px;
  font-size:11px;
  font-family:var(--font-mono,monospace);
  letter-spacing:.06em;
}
.status-dot { width:8px; height:8px; border-radius:50%; }
.live-dot.active {
  background:var(--positive);
  box-shadow:0 0 6px var(--positive);
  animation:pulse-glow 2s ease-in-out infinite;
}
.live-dot.inactive { background:var(--text-muted); }
@keyframes pulse-glow {
  0%,100% { opacity:1; }
  50% { opacity:.4; }
}
/* Summary cards row */
.summary-metrics {
  display:grid;
  grid-template-columns:repeat(4,minmax(0,1fr));
  gap:12px; margin:16px 0;
}
@media(max-width:900px) {
  .summary-metrics { grid-template-columns:repeat(2,minmax(0,1fr)); }
}
@media(max-width:500px) {
  .summary-metrics { grid-template-columns:1fr; }
}
"""
