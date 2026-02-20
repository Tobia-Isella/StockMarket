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

# ── position sizing ───────────────────────────────────────────────────────────
# Sized for a ~$1,000,000 paper account.
#
#   BST_QTY  – shares per position, used by both the BST entry and the
#              tick-momentum strategy for each held symbol.
#              (~$5-15k each depending on price, 3 positions ≈ 5% of capital)
BST_QTY = 50

# Minimum seconds between tick-momentum trades on the same symbol.
# Prevents the bot trading on every tick when 3 streams fire simultaneously.
# The old single-AAPL bot naturally traded slowly; this restores that pace.
TRADE_COOLDOWN_SECS = 60


# ─────────────────────────────────────────────────────────────────────────────
#  Binary Search Tree — ordered by avg momentum
# ─────────────────────────────────────────────────────────────────────────────

class BSTNode:
    def __init__(self, symbol: str, momentum: float):
        self.symbol   = symbol
        self.momentum = momentum
        self.left:  "BSTNode | None" = None
        self.right: "BSTNode | None" = None
        self._x_idx: int = 0   # GUI layout only
        self._depth: int = 0   # GUI layout only

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

    def build_balanced(self, items: list[tuple[str, float]]) -> None:
        self.root = self._from_sorted(sorted(items, key=lambda x: x[1]))

    def _from_sorted(self, items: list[tuple[str, float]]) -> "BSTNode | None":
        if not items:
            return None
        mid  = len(items) // 2
        node = BSTNode(*items[mid])
        node.left  = self._from_sorted(items[:mid])
        node.right = self._from_sorted(items[mid + 1:])
        return node

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

        # self.symbol kept for gui.py stream subscription compatibility
        self.symbol = "AAPL"

        # ── per-symbol tick-momentum state ────────────────────────────────────
        # One entry per BST-held symbol. Initialised when BST buys a symbol,
        # cleared when BST sells it.
        # Each entry is a dict with keys:
        #   price_last       – last trade price seen on the stream
        #   avg_momentum     – 5-day average daily momentum (refreshed every 5 min)
        #   last_mom_update  – datetime of last avg_momentum refresh
        #   shares_held      – True if a tick position is currently open
        #   qty              – shares to trade (reset to BST_QTY after each sell)
        #   sell_pending     – guard flag to prevent duplicate sell orders per tick
        self.tick_state: dict[str, dict] = {}

        # ── BST strategy state ────────────────────────────────────────────────
        self.qty          = BST_QTY
        self.momentum_bst = MomentumBST()
        self.bst_rankings: list[tuple[str, float]] = []
        self.last_bst_build: datetime | None       = None

        self.market_is_open: bool | None = None

        # Sync BST positions with actual broker holdings on startup
        self.bst_positions: set[str] = self._sync_bst_positions()
        print(f"📋 Startup BST positions: {self.bst_positions}")

        print("🌳 Building initial momentum BST …")
        self._build_momentum_bst()

        # Initialise tick state for top-3 symbols regardless of whether
        # they are currently held — ensures trades fire even from a cold start.
        for sym, _ in self.momentum_bst.top_n(3):
            if sym not in self.tick_state:
                self._init_tick_state(sym)

    # ── position helpers ──────────────────────────────────────────────────────

    def _get_actual_position(self, symbol: str) -> int:
        try:
            pos = self.client.get_open_position(symbol)
            return int(float(pos.qty))
        except Exception:
            return 0

    def _sync_bst_positions(self) -> set[str]:
        held: set[str] = set()
        try:
            positions = self.client.get_all_positions()
            for pos in positions:
                if pos.symbol in WATCHLIST:
                    held.add(pos.symbol)
        except Exception as e:
            print(f"⚠️  Could not sync BST positions: {e}")
        return held

    # ── tick-momentum state management ────────────────────────────────────────

    def _init_tick_state(self, symbol: str) -> None:
        """Create a fresh tick-state entry for *symbol*."""
        actual_qty = self._get_actual_position(symbol)
        self.tick_state[symbol] = {
            "price_last":      None,
            "avg_momentum":    None,
            "last_mom_update": None,
            "shares_held":     actual_qty > 0,
            "qty":             actual_qty if actual_qty > 0 else BST_QTY,
            "sell_pending":    False,
            "last_trade_time": None,   # cooldown: prevents trades within TRADE_COOLDOWN_SECS
        }
        print(f"📋 Tick state initialised for {symbol}: "
              f"qty={self.tick_state[symbol]['qty']}, "
              f"shares_held={self.tick_state[symbol]['shares_held']}")

    def _remove_tick_state(self, symbol: str) -> None:
        self.tick_state.pop(symbol, None)

    # ── BST ───────────────────────────────────────────────────────────────────

    def _build_momentum_bst(self) -> None:
        raw: list[tuple[str, float]] = []
        for symbol in WATCHLIST:
            m = self.get_avg_price_momentum(symbol, days=5)
            if m is not None:
                raw.append((symbol, m))
                print(f"  📈 {symbol}: {m:+.3f}")

        bst = MomentumBST()
        bst.build_balanced(raw)

        self.momentum_bst   = bst
        self.bst_rankings   = bst.inorder()
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

        # Exit positions no longer in top-3
        for symbol in list(self.bst_positions):
            if symbol not in top3:
                actual = self._get_actual_position(symbol)
                if actual <= 0:
                    self.bst_positions.discard(symbol)
                    self._remove_tick_state(symbol)
                    continue
                try:
                    self.client.submit_order(MarketOrderRequest(
                        symbol=symbol, qty=actual,
                        side=OrderSide.SELL, time_in_force=TimeInForce.DAY
                    ))
                    self.bst_positions.discard(symbol)
                    self._remove_tick_state(symbol)
                    print(f"🔴 BST EXIT  {symbol} ({actual} shares)")
                except Exception as e:
                    print(f"❌ BST sell {symbol} failed: {e}")

        # Enter new top-3 positions
        for symbol in top3:
            if symbol not in self.bst_positions:
                try:
                    self.client.submit_order(MarketOrderRequest(
                        symbol=symbol, qty=self.qty,
                        side=OrderSide.BUY, time_in_force=TimeInForce.DAY
                    ))
                    self.bst_positions.add(symbol)
                    self._init_tick_state(symbol)
                    print(f"🟢 BST ENTER {symbol} ({self.qty} shares)")
                except Exception as e:
                    print(f"❌ BST buy {symbol} failed: {e}")

    # ── live trade handler ────────────────────────────────────────────────────

    async def on_trade_update(self, trade) -> None:
        is_open = await asyncio.to_thread(self.check_market_status)

        if is_open != self.market_is_open:
            self.market_is_open = is_open
            print("✅ Market is OPEN" if is_open else "🚫 Market is CLOSED")

        if not is_open:
            return

        await asyncio.to_thread(self._maybe_rebuild_bst)

        symbol = trade.symbol
        if symbol not in self.tick_state:
            return  # not a BST-managed symbol, ignore

        state         = self.tick_state[symbol]
        price_current = trade.price

        # Need at least two ticks to compute momentum
        if state["price_last"] is None:
            state["price_last"] = price_current
            return

        current_momentum    = price_current - state["price_last"]
        state["price_last"] = price_current
        now                 = datetime.now(timezone.utc)

        # Refresh 5-day average momentum every 5 minutes
        if (state["avg_momentum"] is None or state["last_mom_update"] is None
                or (now - state["last_mom_update"]).seconds > 300):
            state["avg_momentum"]    = self.get_avg_price_momentum(symbol, 5)
            state["last_mom_update"] = now

        avg = state["avg_momentum"]
        if avg is None:
            print(f"{symbol}: NO LONG RUN PRICE MOMENTUM")
            return

        # Enforce cooldown — skip if a trade fired too recently for this symbol
        if state["last_trade_time"] is not None:
            elapsed = (now - state["last_trade_time"]).total_seconds()
            if elapsed < TRADE_COOLDOWN_SECS:
                return

        # Only trade when daily trend isn't deeply negative
        if avg > -10:
            if current_momentum > 0.10 and not state["shares_held"]:
                self._cancel_orders(symbol)
                try:
                    self.client.submit_order(MarketOrderRequest(
                        symbol=symbol, qty=state["qty"],
                        side=OrderSide.BUY, time_in_force=TimeInForce.DAY
                    ))
                    state["shares_held"]  = True
                    state["sell_pending"] = False
                    state["last_trade_time"] = now
                    print(f"✅ TICK BUY  {symbol} ({state['qty']} shares)")
                except Exception as e:
                    print(f"❌ TICK BUY  {symbol} failed: {e}")

            elif current_momentum < -0.10 and state["shares_held"] and not state["sell_pending"]:
                state["sell_pending"] = True
                self._cancel_orders(symbol)

                actual_qty = self._get_actual_position(symbol)
                if actual_qty <= 0:
                    print(f"⚠️  {symbol}: sell signal but no position — correcting state.")
                    state["shares_held"]  = False
                    state["sell_pending"] = False
                    return

                try:
                    self.client.submit_order(MarketOrderRequest(
                        symbol=symbol, qty=actual_qty,
                        side=OrderSide.SELL, time_in_force=TimeInForce.DAY
                    ))
                    state["shares_held"]  = False
                    state["sell_pending"] = False
                    state["qty"]          = BST_QTY   # reset for next cycle
                    state["last_trade_time"] = now
                    print(f"✅ TICK SELL {symbol} ({actual_qty} shares)")
                except Exception as e:
                    state["sell_pending"] = False      # allow retry on next tick
                    print(f"❌ TICK SELL {symbol} failed: {e}")

            elif current_momentum < -0.10 and not state["shares_held"]:
                print(f"{symbol}: waiting")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _cancel_orders(self, symbol: str) -> None:
        try:
            orders = self.client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.OPEN, symbols=[symbol]
            ))
            for order in orders:
                try:
                    self.client.cancel_order_by_id(order.id)
                    print(f"🚫 Cancelled order {order.id} ({symbol})")
                except Exception as e:
                    if "already" in str(e).lower() or "filled" in str(e).lower():
                        pass  # order filled before cancel arrived — harmless
                    else:
                        print(f"Error cancelling order {order.id} ({symbol}): {e}")
        except Exception as e:
            print(f"Error fetching orders for {symbol}: {e}")

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

    def check_market_status(self) -> bool:
        return self.client.get_clock().is_open

    def get_stream_symbols(self) -> list[str]:
        """Return current top-3 symbols for stream subscription.
        Falls back to [self.symbol] if BST is empty."""
        top3 = [sym for sym, _ in self.momentum_bst.top_n(3)]
        return top3 if top3 else [self.symbol]


# ─────────────────────────────────────────────────────────────────────────────
#  Headless entry point  (used by GitHub Actions / cron)
# ─────────────────────────────────────────────────────────────────────────────

def run_headless() -> None:
    """
    One-shot execution: build the BST, execute BST trades, subscribe to live
    streams for all top-3 symbols, and run tick-momentum trading on each.
    """
    bot = Bot(trading_client, data_client)

    if bot.check_market_status():
        print("📡 Market open — executing BST trades now …")
        bot._execute_bst_trades()
    else:
        print("💤 Market closed — BST trades skipped.")

    # Subscribe to tick streams for all current top-3 symbols
    top3_symbols = [sym for sym, _ in bot.momentum_bst.top_n(3)]
    if not top3_symbols:
        top3_symbols = [bot.symbol]   # fallback to AAPL if BST is empty

    stream.subscribe_trades(bot.on_trade_update, *top3_symbols)
    print(f"📡 Subscribed to tick streams: {top3_symbols}")
    stream.run()   # blocks until interrupted


if __name__ == "__main__":
    run_headless()