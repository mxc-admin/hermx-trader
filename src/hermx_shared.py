#!/usr/bin/env python3
"""Logic shared between the webhook receiver and the dashboard (Phase 4 / D8).

Previously ``canonical_timeframe`` was duplicated in ``webhook_receiver.py`` and
``dashboard.py`` with subtly different alias tables — drift here silently breaks
strategy/alert matching vs. dashboard display. This module is the single source
of truth; both call sites import from here and re-export the name so existing
imports/tests keep working.
"""
from __future__ import annotations


# Superset of the alias tables that previously lived in both modules. The
# receiver's table was the larger one; the dashboard's was a subset, so adopting
# this union keeps the receiver byte-identical and only *widens* what the
# dashboard already accepted.
_TIMEFRAME_ALIASES = {
    "30": "30m",
    "30min": "30m",
    "30mins": "30m",
    "30minute": "30m",
    "30minutes": "30m",
    "60": "1h",
    "1hr": "1h",
    "1hour": "1h",
    "120": "2h",
    "2hr": "2h",
    "2hour": "2h",
    "180": "3h",
    "3hr": "3h",
    "3hour": "3h",
    "240": "4h",
    "4hr": "4h",
    "4hour": "4h",
}


def canonical_timeframe(value) -> str:
    """Normalize a timeframe string to its canonical short form (e.g. ``"120" -> "2h"``).

    Behaviour is identical to the two former copies for every value either side
    previously handled; unknown values pass through unchanged (lower-cased,
    whitespace-stripped).
    """
    text = str(value or "").strip().lower().replace(" ", "")
    return _TIMEFRAME_ALIASES.get(text, text)
