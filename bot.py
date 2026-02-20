"""
bot.py — Headless trading bot.
Runs standalone via GitHub Actions (or any scheduler).
No GUI dependencies whatsoever.
"""

import os
import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.live import StockDataStream


# ── environment ───────────────────────────────────────────────────────────────

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

API_KEY    = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")

trading_client = TradingClient(api_key=API_KEY, secret_key=API_SECRET, paper=True)
data_client    = StockHistoricalDataClient(api_key=API_KEY, secret_key=API_SECRET)
stream         = StockDataStream(api_key=API_KEY, secret_key=API_SECRET)


# ── watchlist ─────────────────────────────────────────────────────────────────

WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "META", "TSLA", "AMD",  "INTC", "NFLX",
]


# ─────────────────────────────────────────────────────────────────────────────
#  Binary Search Tree — ordered by avg momentum
# ─────────────────────────────────────────────────────────────────────────────

class BSTNode:
    def __init__(self, symbol: str, momentum: float):
        self.symbol   = symbol
        self.momentum = momentum
        self.left:  "BSTNode | None" = None
        self.right: "BSTNode | None" = None
        # visual layout fields (used by GUI only, harmless here)
        self._x_idx: int = 0
        self._depth: int = 0

    def __repr__(self):
        return f"BSTNode({self.symbol}, {self.momentum:.3f})"


class MomentumBST:
    """
    Balanced BST ordered by average price momentum.
    Left  → lower momentum
    Right → higher momentum
    build_balanced() guarantees O(log n) depth regardless of input order.
    """

    def __init__(self):
        self.root: BSTNode | None = None

    # ── insert (single node) ─────────────────────
    def insert(self, symbol: str, momentum: float) -> None:
        node = BSTNode(symbol, momentum)
        if self.root is None:
            self.root = node
        else:
            self._insert(self.root, node)

    def _insert(self, cur: BSTNode, new: BSTNode) -> None:
        if new.momentum <= cur.momentum:
            if cur.left  is None: cur.left  = new
            else: self._insert(cur.left, new)
        else:
            if cur.right is None: cur.right = new
            else: self._insert(cur.right, new)

    # ── balanced bulk-build ───────────────────────
    def build_balanced(self, items: list[tuple[str, float]]) -> None:
        """Sort by momentum, then recursively pick the median as root."""
        self.root = self._from_sorted(sorted(items, key=lambda x: x[1]))

    def _from_sorted(self, items: list[tuple[str, float]]) -> "BSTNode | None":
        if not items:
            return None
        mid  = len(items) // 2
        node = BSTNode(*items[mid])
        node.left  = self._from_sorted(items[:mid])
        node.right = self._from_sorted(items[mid + 1:])
        return node

    # ── traversal ────────────────────────────────
    def inorder(self) -> list[tuple[str, float]]:
        result: list[tuple[str, float]] = []
        self._inorder(self.root, result)
        return result

    def _inorder(self, node: "BSTNode | None", result: list) -> None:
        if node is None: return
        self._inorder(node.left, result)
        result.append((node.symbol, node.momentum))
        self._inorder(node.right, result)

    def top_n(self, n: int) -> list[tuple[str, float]]:
        """Return the top-n symbols with the highest *positive* momentum."""
        positive = [(s, m) for s, m in self.inorder() if m > 0]
        return list(reversed(positive))[:n]

    def clear(self) -> None:
        self.root = None

    def __len__(self) -> int:
        return len(self.inorder())


# ─────────────────────────────────────────────────────────────────────────────
#  Bot
# ─────────────────────────────────────────────────────────────────────────────

class Bot:

    def __init__(self, client: TradingClient, data_client: StockHistoricalDataClient):
        self.client      = client
        self.data_client = data_client

        # primary AAPL momentum signal state
        self.symbol       = "AAPL"
        self.primary_qty  = 10
        self.price_last:  float | None    = None
        self.shares_held: bool            = False

        self.avg_price_momentum:  float | None    = None
        self.last_momentum_update: datetime | None = None

        # BST strategy state
        self.qty          = 1
        self.momentum_bst = MomentumBST()
        self.bst_rankings: list[tuple[str, float]] = []
        self.bst_positions: set[str]               = set()
        self.last_bst_build: datetime | None       = None

        self.market_is_open: bool | None = None

        print("🌳 Building initial momentum BST …")
        self._build_momentum_bst()

    # ── BST ───────────────────────────────────────
    def _build_momentum_bst(self) -> None:
        raw: list[tuple[str, float]] = []
        for symbol in WATCHLIST:
            m = self.get_avg_price_momentum(symbol, days=5)
            if m is not None:
                raw.append((symbol, m))
                print(f"  📈 {symbol}: {m:+.3f}")

        bst = MomentumBST()
        bst.build_balanced(raw)

        self.momentum_bst  = bst
        self.bst_rankings  = bst.inorder()
        self.last_bst_build = datetime.now(timezone.utc)
        print(f"🏆 Top momentum stocks: {bst.top_n(3)}")

    def _maybe_rebuild_bst(self) -> None:
        now = datetime.now(timezone.utc)
        if self.last_bst_build is None or (now - self.last_bst_build).seconds > 600:
            print("🔄 Rebuilding momentum BST …")
            self._build_momentum_bst()
            self._execute_bst_trades()

    def _execute_bst_trades(self) -> None:
        top3 = {sym for sym, _ in self.momentum_bst.top_n(3)}

        for symbol in list(self.bst_positions):
            if symbol not in top3:
                try:
                    self.client.submit_order(MarketOrderRequest(
                        symbol=symbol, qty=self.qty,
                        side=OrderSide.SELL, time_in_force=TimeInForce.DAY
                    ))
                    self.bst_positions.discard(symbol)
                    print(f"🔴 BST EXIT  {symbol}")
                except Exception as e:
                    print(f"❌ BST sell {symbol} failed: {e}")

        for symbol in top3:
            if symbol not in self.bst_positions:
                try:
                    self.client.submit_order(MarketOrderRequest(
                        symbol=symbol, qty=self.qty,
                        side=OrderSide.BUY, time_in_force=TimeInForce.DAY
                    ))
                    self.bst_positions.add(symbol)
                    print(f"🟢 BST ENTER {symbol}")
                except Exception as e:
                    print(f"❌ BST buy {symbol} failed: {e}")

    # ── live trade handler ────────────────────────
    async def on_trade_update(self, trade) -> None:
        is_open = await asyncio.to_thread(self.check_market_status)

        if is_open != self.market_is_open:
            self.market_is_open = is_open
            print("✅ Market is OPEN" if is_open else "🚫 Market is CLOSED")

        if not is_open:
            return

        await asyncio.to_thread(self._maybe_rebuild_bst)

        price_current = trade.price
        if self.price_last is None:
            self.price_last = price_current
            return

        current_momentum = price_current - self.price_last
        self.price_last  = price_current
        now = datetime.now(timezone.utc)

        if (self.avg_price_momentum is None
                or (now - self.last_momentum_update).seconds > 300):
            await asyncio.to_thread(self.update_avg_momentum)

        avg = self.avg_price_momentum
        if avg is None:
            print("NO LONG RUN PRICE MOMENTUM")
            return

        if avg > -10:
            if current_momentum > 0.05 and not self.shares_held:
                await asyncio.to_thread(self.cancel_all_orders)
                try:
                    await asyncio.to_thread(self.client.submit_order, MarketOrderRequest(
                        symbol=self.symbol, qty=self.primary_qty,
                        side=OrderSide.BUY, time_in_force=TimeInForce.DAY
                    ))
                    self.shares_held = True
                    print("✅ BUY order placed")
                except Exception as e:
                    print(f"❌ BUY failed: {e}")

            elif current_momentum < -0.05 and self.shares_held:
                await asyncio.to_thread(self.cancel_all_orders)
                try:
                    await asyncio.to_thread(self.client.submit_order, MarketOrderRequest(
                        symbol=self.symbol, qty=self.primary_qty,
                        side=OrderSide.SELL, time_in_force=TimeInForce.DAY
                    ))
                    self.shares_held = False
                    print("✅ SELL order placed")
                except Exception as e:
                    print(f"❌ SELL failed: {e}")

            elif current_momentum < -0.05 and not self.shares_held:
                print("waiting")

    # ── helpers ───────────────────────────────────
    def cancel_all_orders(self) -> None:
        try:
            orders = self.client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.OPEN, symbols=[self.symbol]
            ))
            for order in orders:
                self.client.cancel_order_by_id(order.id)
                print(f"🚫 Cancelled order {order.id}")
        except Exception as e:
            print(f"Error cancelling orders: {e}")

    def get_avg_price_momentum(self, symbol: str, days: int = 5) -> float | None:
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=days * 2)
        try:
            bars = self.data_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                start=start, end=end, feed="iex"
            )).df
            closes = (bars.xs(symbol, level=0)["close"].values
                      if symbol in bars.index.get_level_values(0)
                      else bars["close"].values)
            print(f"📊 {symbol}: {len(closes)} closing prices")
        except Exception as e:
            print(f"❌ Error fetching {symbol}: {e}")
            return None

        if len(closes) < 2:
            return None

        actual = min(days, len(closes) - 1)
        diffs  = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        return sum(diffs[-actual:]) / actual

    def update_avg_momentum(self) -> None:
        self.avg_price_momentum   = self.get_avg_price_momentum(self.symbol, 5)
        self.last_momentum_update = datetime.now(timezone.utc)

    def check_market_status(self) -> bool:
        return self.client.get_clock().is_open


# ─────────────────────────────────────────────────────────────────────────────
#  Headless entry point  (used by GitHub Actions / cron)
# ─────────────────────────────────────────────────────────────────────────────

def run_headless() -> None:
    """
    One-shot execution: build the BST, execute BST trades, then subscribe
    to the live AAPL stream and run until the market closes or the process
    is killed by the scheduler.
    """
    bot = Bot(trading_client, data_client)

    # Execute BST trades immediately on startup
    if bot.check_market_status():
        print("📡 Market open — executing BST trades now …")
        bot._execute_bst_trades()
    else:
        print("💤 Market closed — BST trades skipped.")

    # Subscribe to live stream for tick-level momentum trading
    stream.subscribe_trades(bot.on_trade_update, bot.symbol)
    print(f"📡 Subscribed to {bot.symbol} trade stream. Running …")
    stream.run()   # blocks until interrupted


if __name__ == "__main__":
    run_headless()