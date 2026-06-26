#!/usr/bin/env python3
"""Exchange-agnostic execution layer.

Public API:
    from executors import ExecutorFactory, BaseExecutor

``ExecutorFactory.create(config, root)`` returns the executor adapter selected by
``config["execution"]["exchange"]``. All adapters implement :class:`BaseExecutor`.
"""
from .base import BaseExecutor, empty_fill_summary, empty_normalized_order
from .factory import ExecutorFactory

__all__ = ["BaseExecutor", "ExecutorFactory", "empty_fill_summary", "empty_normalized_order"]
