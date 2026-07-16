#!/usr/bin/env bash
# migrate-positions-v1.sh — one-time Positions-First wipe-clean migration.
#
# Operator decision (2026-07-16): transaction state restarts clean with leg-aware
# ledger rows (leg_kind open|close); NO strategy_id backfill is attempted. Wipes
# ONLY the ledger + order-attribution artifacts (each backed up first):
#   closed-trades.jsonl        — P&L trade-leg ledger
#   cl-ord-strategy-map.jsonl  — submit-time cl_ord_id -> strategy_id map
#   cloid-map.jsonl            — Hyperliquid cloid -> mxc id map
# Operator config (engine-config.json, strategies/, .env, control-state.json)
# is NEVER touched.
#
# Idempotent via a stamp file under the data dir (.migrations/positions-v1.done);
# re-runs are a silent no-op. Safe on a fresh install: nothing to wipe -> stamp
# only. Data dir resolution mirrors src/pnl_ledger.py:
#   HERMX_DATA_DIR -> HERMX_ROOT -> repo root.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="${HERMX_DATA_DIR:-${HERMX_ROOT:-$REPO_ROOT}}"
STAMP_DIR="$DATA_DIR/.migrations"
STAMP="$STAMP_DIR/positions-v1.done"

if [[ -f "$STAMP" ]]; then
  echo "[migrate-positions-v1] already applied — no-op (stamp: $STAMP)"
  exit 0
fi

TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$STAMP_DIR/backup-positions-v1-$TS"
mkdir -p "$STAMP_DIR"

WIPED=""
for f in closed-trades.jsonl cl-ord-strategy-map.jsonl cloid-map.jsonl; do
  src="$DATA_DIR/$f"
  if [[ -e "$src" ]]; then
    mkdir -p "$BACKUP_DIR"
    rows="$(wc -l < "$src" | tr -d ' ')"
    mv "$src" "$BACKUP_DIR/$f"
    WIPED="${WIPED}${f} (${rows} rows); "
    echo "[migrate-positions-v1] wiped $f ($rows rows) — backup: $BACKUP_DIR/$f"
  fi
done
if [[ -z "$WIPED" ]]; then
  echo "[migrate-positions-v1] nothing to wipe (fresh install) — stamping only"
fi

{
  echo "applied_at=$TS"
  echo "data_dir=$DATA_DIR"
  echo "wiped=${WIPED:-none}"
  if [[ -n "$WIPED" ]]; then echo "backup_dir=$BACKUP_DIR"; fi
} > "$STAMP"
echo "[migrate-positions-v1] done — stamp: $STAMP"
