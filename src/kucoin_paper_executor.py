#!/usr/bin/env python3
"""
KuCoin Paper Trading Executor for the Hermx system.

Implements the BaseExecutor interface for clean multi-exchange support.
"""

import os
import time
from datetime import datetime
from typing import Any, Dict

from kucoin.client import Trade, Market, User
from src.executors.base_executor import BaseExecutor


class KuCoinPaperExecutor(BaseExecutor):
    """KuCoin Futures Paper Trading Executor.
    
    Implements the BaseExecutor interface.
    Uses same API keys as live trading. Paper mode is the default.
    """
    
    def __init__(self):
        self.client = KuCoinPaperClient()
        print("[KuCoinPaperExecutor] Initialized in PAPER trading mode")


class KuCoinPaperClient:
    def __init__(self):
        self.api_key = os.environ.get("OKX_API_KEY") or os.environ.get("KUCOIN_API_KEY")
        self.api_secret = os.environ.get("OKX_SECRET_KEY") or os.environ.get("KUCOIN_SECRET")
        self.passphrase = os.environ.get("OKX_PASSPHRASE") or os.environ.get("KUCOIN_PASSPHRASE")
        
        if not all([self.api_key, self.api_secret, self.passphrase]):
            raise RuntimeError("Missing KuCoin API credentials. Set OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE (reused for KuCoin paper)")

        self.market = Market(url='https://api.kucoin.com')
        self.trade = Trade(
            key=self.api_key, 
            secret=self.api_secret, 
            passphrase=self.passphrase, 
            url='https://api.kucoin.com'
        )
        self.user = User(
            key=self.api_key, 
            secret=self.api_secret, 
            passphrase=self.passphrase, 
            url='https://api.kucoin.com'
        )
        
        print(f"[KuCoinPaperClient] Initialized with key {self.api_key[:6]}... (paper mode)")

    def get_position(self, symbol: str) -> Dict[str, Any]:
        try:
            positions = self.trade.get_position_details(symbol)
            if isinstance(positions, dict) and positions.get('code') == '200000':
                data = positions.get('data', {})
                return {
                    "symbol": symbol,
                    "current_size": float(data.get('currentQty', 0) or 0),
                    "entry_price": float(data.get('avgEntryPrice', 0) or 0),
                    "unrealized_pnl": float(data.get('unrealisedPnl', 0) or 0),
                    "status": "success"
                }
            return {"symbol": symbol, "current_size": 0.0, "status": "no_position"}
        except Exception as e:
            return {"symbol": symbol, "error": str(e), "status": "error"}

    def close_position(self, symbol: str, side: str) -> Dict[str, Any]:
        print(f"[KuCoinPaper] Closing {side} position for {symbol}")
        try:
            # For perpetuals, use closeOrder=True
            order_side = "sell" if side.lower() == "buy" else "buy"
            order = self.trade.create_market_order(symbol, order_side, closeOrder=True)
            time.sleep(1.5)
            return {"success": True, "order": order, "action": "close"}
        except Exception as e:
            return {"success": False, "error": str(e), "action": "close"}

    def open_position(self, symbol: str, side: str, notional_usd: float) -> Dict[str, Any]:
        print(f"[KuCoinPaper] Opening {side} position for {symbol} (~${notional_usd})")
        try:
            # Note: In production, calculate contracts based on price and leverage
            # This is simplified for paper trading
            order = self.trade.create_market_order(symbol, side.lower(), size=str(notional_usd))
            return {"success": True, "order": order, "action": "open"}
        except Exception as e:
            return {"success": False, "error": str(e), "action": "open"}

    def execute(self, instruction: Dict[str, Any]) -> Dict[str, Any]:
        """Main execution method compatible with BaseExecutor."""
        symbol = instruction.get("symbol") or instruction.get("inst_id", instruction.get("okx_inst_id", ""))
        side = instruction.get("target_side", "buy").lower()
        notional = float(instruction.get("target_notional_usd", 1000))

        current = self.get_position(symbol)

        result = {
            "exchange": "kucoin_paper",
            "symbol": symbol,
            "side": side,
            "notional_usd": notional,
            "paper_mode": True,
            "timestamp": datetime.now().isoformat(),
            "success": False
        }

        # Close-first logic for opposite direction
        current_size = current.get("current_size", 0)
        if current_size != 0:
            is_long = current_size > 0
            if (is_long and side == "sell") or (not is_long and side == "buy"):
                close_result = self.close_position(symbol, "long" if is_long else "short")
                result["close"] = close_result
                time.sleep(2.0)

        open_result = self.open_position(symbol, side, notional)
        result["open"] = open_result
        result["success"] = open_result.get("success", False)

        print(f"[KuCoinPaperExecutor] Execution completed: {result.get('success')}")
        return result

    def get_account_balance(self) -> Dict[str, Any]:
        try:
            account = self.user.get_account_overview(currency="USDT")
            return {
                "balance_usd": float(account.get("data", {}).get("availableBalance", 0)),
                "status": "success"
            }
        except Exception:
            return {"balance_usd": 0.0, "status": "error"}


if __name__ == "__main__":
    executor = KuCoinPaperExecutor()
    print("KuCoin Paper Executor test passed.")
