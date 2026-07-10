"""Regression tests for webhook.timeutil.parse_tv_time.

Focus: the fractional-epoch fallback. A string like "1699999999.5" is not caught by
``str.isdigit()`` (the dot fails it) yet is a valid epoch-seconds timestamp. Before the
fallback it fell through to ``datetime.fromisoformat`` and returned None. The integer-epoch
and ISO-string paths must keep working unchanged.
"""
from __future__ import annotations

from datetime import datetime, timezone

from webhook.timeutil import parse_tv_time


def test_fractional_epoch_string_parses():
    dt = parse_tv_time("1699999999.5")
    assert isinstance(dt, datetime)
    assert dt.tzinfo is not None
    assert dt == datetime.fromtimestamp(1699999999.5, tz=timezone.utc)
    assert dt.microsecond == 500000


def test_integer_epoch_seconds_still_parses():
    dt = parse_tv_time("1699999999")
    assert dt == datetime.fromtimestamp(1699999999, tz=timezone.utc)


def test_integer_epoch_millis_still_parses():
    # > 10_000_000_000 is treated as milliseconds.
    dt = parse_tv_time("1699999999000")
    assert dt == datetime.fromtimestamp(1699999999.0, tz=timezone.utc)


def test_fractional_epoch_millis_still_parses():
    # A fractional value above the millis threshold is divided by 1000, same as the int path.
    dt = parse_tv_time("1699999999000.0")
    assert dt == datetime.fromtimestamp(1699999999.0, tz=timezone.utc)


def test_iso_string_still_parses():
    dt = parse_tv_time("2026-07-09T00:00:00Z")
    assert dt == datetime(2026, 7, 9, 0, 0, 0, tzinfo=timezone.utc)


def test_garbage_returns_none():
    assert parse_tv_time("not-a-time") is None
    assert parse_tv_time("") is None
    assert parse_tv_time(None) is None
