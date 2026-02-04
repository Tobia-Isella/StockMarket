from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from dotenv import load_dotenv
from pathlib import Path
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import tkinter
from tkinter import ttk
import sv_ttk
import os
from alpaca.trading.stream import TradingStream
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta,timezone
import threading
from alpaca.data.live import StockDataStream
import asyncio
import sys
from tkinter.scrolledtext import ScrolledText


# Load env
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)


client = TradingClient(
    api_key=os.getenv("APCA_API_KEY_ID"),
    secret_key=os.getenv("APCA_API_SECRET_KEY"),
    paper=True
)

data_client = StockHistoricalDataClient(
    api_key=os.getenv("APCA_API_KEY_ID"),
    secret_key=os.getenv("APCA_API_SECRET_KEY")
)



# Live data stream

stream = StockDataStream(
    api_key=os.getenv("APCA_API_KEY_ID"),
    secret_key=os.getenv("APCA_API_SECRET_KEY")
)

class Bot():

    def __init__(self, client, data_client):
        self.client = client
        self.data_client = data_client

        self.avg_price_momentum = None
        self.last_momentum_update = None

                # TEST: Check what data we can get
        print("🔍 Testing historical data availability...")
        test_momentum = self.get_avg_price_momentum("AAPL", 5)
        print(f"✅ Average momentum: {test_momentum}")


        self.symbol = "AAPL"
        self.qty = 1

        self.price_last = None
        self.shares_held = False

        self.market_is_open = None

    async def on_trade_update(self, trade):
        is_open = await asyncio.to_thread(self.check_market_status)

        if is_open != self.market_is_open:
            self.market_is_open = is_open
            if not is_open:
                print("🚫 Market is CLOSED")
            else:
                print("✅ Market is OPEN")

        if not is_open:
            return

        price_current = trade.price

        if self.price_last is None:
            self.price_last = price_current
            return

        current_price_momentum = price_current - self.price_last
        self.price_last = price_current

        now = datetime.now(timezone.utc)


        if (
            self.avg_price_momentum is None
            or (now - self.last_momentum_update).seconds > 300
        ):
            await asyncio.to_thread(self.update_avg_momentum)

        avg_price_momentum = self.avg_price_momentum


        if avg_price_momentum is None:
            print("NO LONG RUN PRICE MOMENTUM")
            return

        if avg_price_momentum > 0:
                if current_price_momentum > 0.05 and not self.shares_held:
                    # Cancel any existing orders first
                    await asyncio.to_thread(self.cancel_all_orders)
                    
                    try:
                        await asyncio.to_thread(
                            self.client.submit_order,
                            MarketOrderRequest(
                                symbol=self.symbol,
                                qty=self.qty,
                                side=OrderSide.BUY,
                                time_in_force=TimeInForce.DAY
                            )
                        )
                        self.shares_held = True
                        print("✅ BUY order placed")
                    except Exception as e:
                        print(f"❌ BUY failed: {e}")

                elif current_price_momentum < -0.05 and self.shares_held:
                    await asyncio.to_thread(self.cancel_all_orders)
                    
                    try:
                        await asyncio.to_thread(
                            self.client.submit_order,
                            MarketOrderRequest(
                                symbol=self.symbol,
                                qty=self.qty,
                                side=OrderSide.SELL,
                                time_in_force=TimeInForce.DAY
                            )
                        )
                        self.shares_held = False
                        print("✅ SELL order placed")
                    except Exception as e:
                        print(f"❌ SELL failed: {e}")

    def cancel_all_orders(self):
        """Cancel all open orders for the symbol"""
        try:
            orders = self.client.get_orders(filter={"symbols": [self.symbol]})
            for order in orders:
                if order.status in ['new', 'partially_filled', 'accepted']:
                    self.client.cancel_order_by_id(order.id)
                    print(f"🚫 Cancelled order {order.id}")
        except Exception as e:
            print(f"Error cancelling orders: {e}")

    def get_avg_price_momentum(self, symbol, days=5):
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days * 2)  # Get more days to account for weekends
        
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed="iex"
        )

        try:
            bars = data_client.get_stock_bars(request).df
            
            # FIX: Access close prices directly from the MultiIndex DataFrame
            if symbol in bars.index.get_level_values(0):
                closes = bars.xs(symbol, level=0)["close"].values
            else:
                closes = bars["close"].values  # Fallback if no symbol level
                
            print(f"📊 Got {len(closes)} closing prices: {closes}")
            
        except Exception as e:
            print(f"❌ Error fetching data: {e}")
            return None

        if len(closes) < 2:
            print(f"❌ Not enough data: need at least 2 days, got {len(closes)}")
            return None

        # Use whatever data we have (at least 2 days)
        actual_days = min(days, len(closes) - 1)
        
        # daily momentum
        momentums = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        
        avg_momentum = sum(momentums[-actual_days:]) / actual_days
        print(f"✅ Average momentum over {actual_days} days: {avg_momentum:.3f}")
        return avg_momentum
        
    def update_avg_momentum(self):
        self.avg_price_momentum = self.get_avg_price_momentum(self.symbol, 5)
        self.last_momentum_update = datetime.now(timezone.utc)
    
    def check_market_status(self):
        clock = self.client.get_clock()
        return clock.is_open
    

class GraphWidget(ttk.Frame):
    def __init__(self, parent, client):
        super().__init__(parent)

        self.client = client
        self.x_data = []
        self.y_data = []

        self.fig = Figure(figsize=(6, 3), dpi=100, facecolor="#1e1e1e")
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor("#1e1e1e")

        self.line, = self.ax.plot([], [], color="#4fc3f7", marker='o')

        self.ax.set_title("Total Unrealized P/L", color="white")
        self.ax.tick_params(colors="white")
        for spine in self.ax.spines.values():
            spine.set_color("white")

        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill=tkinter.BOTH, expand=True)

        self.after(1000, self.update_graph)  # delay first run

    def update_graph(self):
        try:
            portfolio = self.client.get_all_positions()

            total_pl = 0.0
            for position in portfolio:
                total_pl += float(position.unrealized_pl)

            self.x_data.append(len(self.x_data))
            self.y_data.append(total_pl)

            self.x_data = self.x_data[-10:]
            self.y_data = self.y_data[-10:]

            self.line.set_data(range(len(self.y_data)), self.y_data)
            self.ax.relim()
            self.ax.autoscale_view()

            self.canvas.draw_idle()

        except Exception as e:
            print("Graph update error:", e)

        self.after(10000, self.update_graph)


class AttributeListWidget(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)

        columns = ("name", "amount", "Total P/L ($)")

        self.tree = ttk.Treeview(
            self,
            columns=columns,
            show="headings",
            height=8
        )

        self.tree.heading("name", text="Name")
        self.tree.heading("amount", text="Amount")
        self.tree.heading("Total P/L ($)", text="Total P/L ($)")

        self.tree.column("name", width=120, anchor="w")
        self.tree.column("amount", width=80, anchor="center")
        self.tree.column("Total P/L ($)", width=100, anchor="center")

        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.update_positions()

    def update_positions(self):
        # Clear existing rows
        for item in self.tree.get_children():
            self.tree.delete(item)

        portfolio = client.get_all_positions()
        for position in portfolio:
            self.insert_row(
                position.symbol,
                position.qty,
                position.unrealized_pl
            )

        self.after(10_000, self.update_positions)
    
    def insert_row(self, name, amount, pl):
        self.tree.insert(
            "",
            "end",
            values=(name, amount, pl)
        )

class TerminalRedirector:
    def __init__(self, text_widget):
        self.text_widget = text_widget

    def write(self, message):
        self.text_widget.after(0, self._append_text, message)

    def _append_text(self, message):
        self.text_widget.configure(state="normal")
        self.text_widget.insert("end", message)
        self.text_widget.see("end")
        self.text_widget.configure(state="disabled")

    def flush(self):
        pass  # required for file-like compatibility

class TerminalWidget(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=2, style="Custom.TFrame")

        # Text widget
        self.text = tkinter.Text(
            self,
            height=10,
            bg="#1e1e1e",
            fg="#00ff9c",
            insertbackground="white",
            font=("Consolas", 10),
            state="disabled",
            relief="flat",
            wrap="word",
            borderwidth=0,
            highlightthickness=0
        )

        # Scrollbar
        self.scrollbar = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self.text.yview,
            style="Vertical.TScrollbar"
        )

        self.text.configure(yscrollcommand=self.scrollbar.set)

        # Layout
        self.text.grid(row=0, column=0, sticky="nsew", padx=(0,1), pady=1)
        self.scrollbar.grid(row=0, column=1, sticky="ns", pady=1)

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Redirect stdout & stderr
        sys.stdout = TerminalRedirector(self.text)
        sys.stderr = TerminalRedirector(self.text)

        print("Terminal initialized.")

class App(tkinter.Tk):
    def __init__(self):
        super().__init__()
        self.title("Dashboard")
        
        # Make window resizable
        self.resizable(True, True)
        
        # Set window background to match your dark theme
        self.configure(bg="#1e1e1e")  # same as your graph background

        # Add your logo (top-left corner)
        try:
            from PIL import Image, ImageTk
            logo_path = Path(__file__).parent / "logo.png"  # replace with your logo path
            logo_img = Image.open(logo_path)
            logo_img = logo_img.resize((40, 40), Image.LANCZOS)
            self.logo_photo = ImageTk.PhotoImage(logo_img)
            logo_label = tkinter.Label(self, image=self.logo_photo, bg="#1e1e1e")
            logo_label.grid(row=0, column=0, sticky="nw", padx=10, pady=10)
        except Exception as e:
            print("Logo not found or PIL not installed:", e)

        # Configure grid to expand properly
        self.grid_rowconfigure(1, weight=2)
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=2)

        # Create and place widgets
        self.graph = GraphWidget(self, client)
        self.graph.grid(row=1, column=1, sticky="nsew", padx=10, pady=10)

        self.list_widget = AttributeListWidget(self)
        self.list_widget.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)

        # Terminal widget (bottom, full width)
        self.terminal = TerminalWidget(self)
        self.terminal.grid(
            row=2,
            column=0,
            columnspan=2,
            sticky="nsew",
            padx=10,
            pady=10
        )

        # Run Bot 
        bot = Bot(client, data_client)

        stream.subscribe_trades(bot.on_trade_update, "AAPL")
        threading.Thread(target=stream.run, daemon=True).start()


        # Optional: make frame borders sleek (modern flat look)
        style = ttk.Style()
        style.configure("Treeview", 
                        background="#1e1e1e", 
                        foreground="white", 
                        fieldbackground="#1e1e1e",
                        bordercolor="#1e1e1e",
                        borderwidth=0)
        style.configure(
            "Custom.TFrame",
            background="#1e1e1e",
            bordercolor="#4fc3f7",
            borderwidth=2,
            relief="solid"
        )

        style.configure(
            "Vertical.TScrollbar",
            background="#1e1e1e",
            troughcolor="#1e1e1e",
            bordercolor="#1e1e1e",
            arrowcolor="white"
        )
        style.map("Treeview", background=[('selected', '#4fc3f7')])

    
if __name__ == "__main__":
    app = App()
    sv_ttk.set_theme("dark")
    app.mainloop()