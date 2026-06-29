#!/usr/bin/env python3
"""Agent-facing skill runtimes.

The Hermes execution skill is the ONLY agent-facing execution surface
(REFACTOR_PLAN.md Phase 5 / P5-05). It translates an analyzed signal + strategy
into a normalized execution intent and submits *exclusively* through the
controlled :class:`execution.ExecutionService` -- never a direct exchange API --
so every money-safety control (kill-switch precedence, gate checks, write-ahead
journal, idempotency, UNKNOWN handling) stays above the adapter boundary.
"""
from __future__ import annotations

from .hermes_execution import (
    HermesRelayAdapter,
    build_execution_intent,
    build_execution_record,
)

__all__ = [
    "HermesRelayAdapter",
    "build_execution_intent",
    "build_execution_record",
]
