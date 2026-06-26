#!/usr/bin/env python3
"""
Base Executor Interface for the Hermx trading system.

All exchange executors (OKX, KuCoin, Bybit, Binance, etc.) must implement this interface.
This allows the webhook_receiver to be exchange-agnostic and makes adding new exchanges easy.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseExecutor(ABC):
    """
    Abstract base class for all exchange executors.
    
    Key contract:
    - All methods must return consistent dict shapes
    - Paper/demo mode must be respected
    - Close-first on reversals, no pyramiding on same direction
    - Clear error handling and logging
    """
    
    @abstractmethod
    def execute(self, instruction: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main execution entry point.
        
        instruction should contain:
        - symbol
        - target_side ("buy" or "sell")
        - target_notional_usd
        - expected_leverage
        - margin_mode
        - strategy_id (optional)
        
        Returns dict with:
        - success: bool
        - mode: str (paper, live, dry_run, error)
        - actions taken
        - fill information
        - error (if any)
        """
        pass

    @abstractmethod
    def get_position(self, symbol: str) -> Dict[str, Any]:
        """Return current position for a symbol."""
        pass

    @abstractmethod
    def close_position(self, symbol: str, side: str) -> Dict[str, Any]:
        """Close existing position before opening opposite direction."""
        pass

    @abstractmethod
    def open_position(self, symbol: str, side: str, notional_usd: float) -> Dict[str, Any]:
        """Open a new position."""
        pass

    def get_account_balance(self) -> Dict[str, Any]:
        """Optional: return account balance information."""
        return {"balance_usd": 0.0, "status": "not_implemented"}


class ExecutorFactory:
    """Factory to create the correct executor based on config."""
    
    @staticmethod
    def create_executor(config: Dict[str, Any]) -> BaseExecutor:
        exchange = str(config.get("execution", {}).get("exchange", "okx_demo")).lower()
        
        if exchange in ("okx", "okx_demo", "okx_paper"):
            from src.okx_demo_executor import OkxDemoExecutor
            return OkxDemoExecutor()
        elif exchange in ("kucoin", "kucoin_paper"):
            from src.kucoin_paper_executor import KuCoinPaperExecutor
            return KuCoinPaperExecutor()
        elif exchange in ("bybit", "bybit_testnet"):
            # Future extension point
            raise NotImplementedError(f"Bybit executor not yet implemented: {exchange}")
        else:
            raise ValueError(f"Unsupported exchange: {exchange}. Supported: okx_demo, kucoin_paper")
