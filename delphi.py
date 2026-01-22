from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from dotenv import load_dotenv
from pathlib import Path
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import random
import time
import tkinter
from tkinter import ttk
import sv_ttk
import os


# Load env
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)


client = TradingClient(
    api_key=os.getenv("APCA_API_KEY_ID"),
    secret_key=os.getenv("APCA_API_SECRET_KEY"),
    paper=True
)

class GraphWidget(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)

        self.x_data = []
        self.y_data = []

        self.fig = Figure(figsize=(6, 3), dpi=100, facecolor="#1e1e1e")
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor("#1e1e1e")

        self.line, = self.ax.plot([], [], color="#4fc3f7", marker='o')

        self.ax.set_title("Live Data", color="white")
        self.ax.tick_params(colors="white")
        for spine in self.ax.spines.values():
            spine.set_color("white")

        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill=tkinter.BOTH, expand=True)

        self.update_graph()

    def update_graph(self):
        self.x_data.append(len(self.x_data))
        self.y_data.append(random.randint(0, 100))

        self.x_data = self.x_data[-10:]
        self.y_data = self.y_data[-10:]

        self.line.set_data(range(len(self.y_data)), self.y_data)
        self.ax.relim()
        self.ax.autoscale_view()

        self.canvas.draw_idle()
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

        # Positions
        # Get a list of all of our positions.
        portfolio = client.get_all_positions()
        for position in portfolio:
            self.insert_row("{} {} {}".format(position.symbol, position.qty, position.unrealized_pl ))

    def insert_row(self, name):
        self.tree.insert("", "end", values=(name))

class App(tkinter.Tk):
    def __init__(self):
        super().__init__()
        self.title("Dashboard")

        self.grid_rowconfigure(0, weight=2)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.graph = GraphWidget(self)
        self.graph.grid(row=0, column=1, sticky="nsew", padx=10, pady=10)

        self.list_widget = AttributeListWidget(self)
        self.list_widget.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)


# Buy Order
Stock_buy = MarketOrderRequest(
    symbol="AAPL",
    qty=1,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.DAY 
)

if __name__ == "__main__":
    app = App()
    sv_ttk.set_theme("dark")
    app.mainloop()

#order = client.submit_order(Stock_buy)
#print("Buy order sent:", order)