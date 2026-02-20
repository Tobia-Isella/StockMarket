"""
gui.py — Trading dashboard.
Imports Bot, MomentumBST, and BSTNode from bot.py — no trading logic lives here.
Run locally: python gui.py
"""

import tkinter
from tkinter import ttk
from pathlib import Path
import threading
import sys

import sv_ttk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

# ── import everything we need from the bot module ────────────────────────────
from bot import (
    Bot, MomentumBST, BSTNode,
    trading_client as client,
    data_client,
    stream,
)


# ─────────────────────────────────────────────────────────────────────────────
#  BST Rankings Widget  (right panel, full height)
# ─────────────────────────────────────────────────────────────────────────────

class BSTRankingsWidget(ttk.Frame):
    NODE_R = 28

    def __init__(self, parent, bot: Bot):
        super().__init__(parent)
        self.bot = bot

        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=3)
        self.grid_rowconfigure(2, weight=0)
        self.grid_rowconfigure(3, weight=2)
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)

        # title
        ttk.Label(
            self, text="🌳  Momentum BST",
            foreground="#4fc3f7", font=("Consolas", 12, "bold")
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 4))

        # scrollable canvas
        c_frame = ttk.Frame(self)
        c_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=6, pady=(0, 4))
        c_frame.grid_rowconfigure(0, weight=1)
        c_frame.grid_columnconfigure(0, weight=1)

        self.canvas = tkinter.Canvas(
            c_frame, bg="#12121f",
            highlightthickness=1, highlightbackground="#2e2e4a"
        )
        v_sb = ttk.Scrollbar(c_frame, orient="vertical",   command=self.canvas.yview)
        h_sb = ttk.Scrollbar(c_frame, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=v_sb.set, xscrollcommand=h_sb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        v_sb.grid(row=0, column=1, sticky="ns")
        h_sb.grid(row=1, column=0, sticky="ew")

        # sub-label
        ttk.Label(
            self, text="Rankings  (best → worst momentum)",
            foreground="#666", font=("Consolas", 8)
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(2, 0))

        # rankings table
        cols = ("rank", "symbol", "momentum", "held")
        self.table = ttk.Treeview(self, columns=cols, show="headings", height=7)
        self.table.heading("rank",     text="#")
        self.table.heading("symbol",   text="Symbol")
        self.table.heading("momentum", text="Avg Mom ($)")
        self.table.heading("held",     text="Held")
        self.table.column("rank",     width=28,  anchor="center", stretch=False)
        self.table.column("symbol",   width=60,  anchor="center", stretch=False)
        self.table.column("momentum", width=110, anchor="center")
        self.table.column("held",     width=44,  anchor="center", stretch=False)
        self.table.tag_configure("pos",  foreground="#00ff9c")
        self.table.tag_configure("neg",  foreground="#ff5555")
        self.table.tag_configure("held", foreground="#4fc3f7", background="#0c1e33")

        tb_sb = ttk.Scrollbar(self, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=tb_sb.set)
        self.table.grid(row=3, column=0, sticky="nsew", padx=(8, 0), pady=(2, 8))
        tb_sb.grid(row=3, column=1, sticky="ns", pady=(2, 8), padx=(0, 6))

        self.after(200, self.refresh)

    def refresh(self):
        self._update_table()
        self._draw_tree()
        self.after(15_000, self.refresh)

    def _update_table(self):
        for item in self.table.get_children():
            self.table.delete(item)
        ranked = list(reversed(self.bot.bst_rankings))
        held   = self.bot.bst_positions
        for i, (sym, mom) in enumerate(ranked, start=1):
            is_held = sym in held
            tag = "held" if is_held else ("pos" if mom >= 0 else "neg")
            self.table.insert("", "end",
                values=(i, sym, f"{mom:+.3f}", "✅" if is_held else "—"),
                tags=(tag,))

    # ── BST diagram ──────────────────────────────
    def _assign_positions(self, node, counter, depth=0):
        if node is None: return
        self._assign_positions(node.left, counter, depth + 1)
        node._x_idx = counter[0]
        node._depth = depth
        counter[0] += 1
        self._assign_positions(node.right, counter, depth + 1)

    def _max_depth(self, node):
        if node is None: return 0
        return 1 + max(self._max_depth(node.left), self._max_depth(node.right))

    def _draw_tree(self):
        self.canvas.delete("all")
        root = self.bot.momentum_bst.root
        if root is None:
            self.canvas.create_text(120, 60, text="No BST data yet…",
                                    fill="#444", font=("Consolas", 11))
            return

        counter = [0]
        self._assign_positions(root, counter, depth=0)
        n_nodes     = counter[0]
        depth_count = self._max_depth(root)

        R      = self.NODE_R
        H_PAD  = R + 10
        V_PAD  = R + 14
        COL_W  = max(R * 2 + 18, 80)
        ROW_H  = max(R * 2 + 24, 90)
        total_w = H_PAD * 2 + n_nodes * COL_W
        total_h = V_PAD * 2 + depth_count * ROW_H

        self.canvas.configure(scrollregion=(0, 0, total_w, total_h))

        def xy(nd):
            return (H_PAD + nd._x_idx * COL_W + COL_W // 2,
                    V_PAD + nd._depth * ROW_H)

        held = self.bot.bst_positions

        def edges(nd):
            if nd is None: return
            px, py = xy(nd)
            for child in (nd.left, nd.right):
                if child:
                    cx, cy = xy(child)
                    self.canvas.create_line(px, py, cx, cy, fill="#2e2e4e", width=2)
            edges(nd.left); edges(nd.right)

        def nodes(nd):
            if nd is None: return
            cx, cy   = xy(nd)
            is_held  = nd.symbol in held
            positive = nd.momentum >= 0
            rim  = "#00ff9c" if positive else "#ff5555"
            bg   = ("#0d2b1a" if (positive and is_held) else
                    "#2b0d0d" if (not positive and is_held) else "#1a1a2e")

            if is_held:
                self.canvas.create_oval(cx-R-5, cy-R-5, cx+R+5, cy+R+5,
                                        outline="#4fc3f7", width=1, dash=(5, 3))
            self.canvas.create_oval(cx-R, cy-R, cx+R, cy+R,
                                    fill=bg, outline=rim, width=2)
            self.canvas.create_text(cx, cy-8, text=nd.symbol,
                                    fill=rim, font=("Consolas", 9, "bold"))
            self.canvas.create_text(cx, cy+9, text=f"{nd.momentum:+.2f}",
                                    fill=rim, font=("Consolas", 8))
            nodes(nd.left); nodes(nd.right)

        edges(root)
        nodes(root)

        self.canvas.create_text(total_w // 2, total_h - 8,
            text="← lower momentum   |   higher momentum →",
            fill="#333", font=("Consolas", 7))

        lx, ly = total_w - 4, 6
        for label, color in [("● positive","#00ff9c"),("● negative","#ff5555"),("◎ held","#4fc3f7")]:
            self.canvas.create_text(lx, ly, text=label, fill=color,
                                    font=("Consolas", 8), anchor="e")
            ly += 14


# ─────────────────────────────────────────────────────────────────────────────
#  Other widgets
# ─────────────────────────────────────────────────────────────────────────────

class GraphWidget(ttk.Frame):
    def __init__(self, parent, client):
        super().__init__(parent)
        self.client = client
        self.x_data, self.y_data = [], []

        self.fig = Figure(figsize=(6, 3), dpi=100, facecolor="#1e1e1e")
        self.ax  = self.fig.add_subplot(111)
        self.ax.set_facecolor("#1e1e1e")
        self.line, = self.ax.plot([], [], color="#4fc3f7", marker='o')
        self.ax.set_title("Total Unrealized P/L", color="white")
        self.ax.tick_params(colors="white")
        for spine in self.ax.spines.values():
            spine.set_color("white")

        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill=tkinter.BOTH, expand=True)
        self.after(1000, self.update_graph)

    def update_graph(self):
        try:
            total_pl = sum(float(p.unrealized_pl) for p in self.client.get_all_positions())
            self.x_data.append(len(self.x_data))
            self.y_data.append(total_pl)
            self.x_data = self.x_data[-10:]
            self.y_data = self.y_data[-10:]
            self.line.set_data(range(len(self.y_data)), self.y_data)
            self.ax.relim(); self.ax.autoscale_view()
            self.canvas.draw_idle()
        except Exception as e:
            print("Graph update error:", e)
        self.after(10_000, self.update_graph)


class AttributeListWidget(ttk.Frame):
    def __init__(self, parent, client):
        super().__init__(parent)
        self.client = client
        cols = ("name", "amount", "Total P/L ($)")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=8)
        self.tree.heading("name",          text="Name")
        self.tree.heading("amount",        text="Amount")
        self.tree.heading("Total P/L ($)", text="Total P/L ($)")
        self.tree.column("name",          width=120, anchor="w")
        self.tree.column("amount",        width=80,  anchor="center")
        self.tree.column("Total P/L ($)", width=100, anchor="center")

        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.update_positions()

    def update_positions(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for p in self.client.get_all_positions():
            self.tree.insert("", "end", values=(p.symbol, p.qty, p.unrealized_pl))
        self.after(10_000, self.update_positions)


class TerminalRedirector:
    def __init__(self, widget):
        self.widget = widget

    def write(self, msg):
        self.widget.after(0, self._append, msg)

    def _append(self, msg):
        self.widget.configure(state="normal")
        self.widget.insert("end", msg)
        self.widget.see("end")
        self.widget.configure(state="disabled")

    def flush(self):
        pass


class TerminalWidget(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=2, style="Custom.TFrame")
        self.text = tkinter.Text(
            self, height=10, bg="#1e1e1e", fg="#00ff9c",
            insertbackground="white", font=("Consolas", 10),
            state="disabled", relief="flat", wrap="word",
            borderwidth=0, highlightthickness=0
        )
        sb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview,
                           style="Vertical.TScrollbar")
        self.text.configure(yscrollcommand=sb.set)
        self.text.grid(row=0, column=0, sticky="nsew", padx=(0, 1), pady=1)
        sb.grid(row=0, column=1, sticky="ns", pady=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        sys.stdout = TerminalRedirector(self.text)
        sys.stderr = TerminalRedirector(self.text)
        print("Terminal initialized.")


# ─────────────────────────────────────────────────────────────────────────────
#  App  —  3-column layout
# ─────────────────────────────────────────────────────────────────────────────
#
#   col 0          col 1           col 2
#  ┌────────────┬──────────────┬────────────────┐
#  │ logo       │              │                │  row 0
#  ├────────────┼──────────────┤  BST Widget    │
#  │ positions  │  P/L graph   │  (full height) │  row 1
#  ├────────────┼──────────────┤                │
#  │       terminal            │                │  row 2
#  └───────────────────────────┴────────────────┘

class App(tkinter.Tk):
    def __init__(self):
        super().__init__()
        self.title("Trading Dashboard")
        self.resizable(True, True)
        self.configure(bg="#1e1e1e")

        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=3)
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=2)
        self.grid_columnconfigure(2, weight=2)

        # logo
        try:
            from PIL import Image, ImageTk
            img = Image.open(Path(__file__).parent / "logo.png").resize((40, 40))
            self.logo = ImageTk.PhotoImage(img)
            tkinter.Label(self, image=self.logo, bg="#1e1e1e").grid(
                row=0, column=0, sticky="nw", padx=10, pady=10)
        except Exception:
            pass

        # positions list
        self.list_widget = AttributeListWidget(self, client)
        self.list_widget.grid(row=1, column=0, sticky="nsew", padx=(10, 5), pady=10)

        # P/L graph
        self.graph = GraphWidget(self, client)
        self.graph.grid(row=1, column=1, sticky="nsew", padx=5, pady=10)

        # bot (must exist before BST widget)
        self.bot = Bot(client, data_client)

        # BST panel — full height right column
        self.bst_widget = BSTRankingsWidget(self, self.bot)
        self.bst_widget.grid(row=0, column=2, rowspan=3,
                             sticky="nsew", padx=(5, 10), pady=10)

        # terminal — bottom, left two columns
        self.terminal = TerminalWidget(self)
        self.terminal.grid(row=2, column=0, columnspan=2,
                           sticky="nsew", padx=(10, 5), pady=(0, 10))

        # start live stream — subscribe to all current top-3 BST symbols
        # so tick-momentum trading fires for each held position
        stream.subscribe_trades(self.bot.on_trade_update, *self.bot.get_stream_symbols())
        print(f"📡 Subscribed to tick streams: {self.bot.get_stream_symbols()}")
        threading.Thread(target=stream.run, daemon=True).start()

        # styles
        style = ttk.Style()
        style.configure("Treeview",
                        background="#1e1e1e", foreground="white",
                        fieldbackground="#1e1e1e", borderwidth=0)
        style.configure("Custom.TFrame",
                        background="#1e1e1e", bordercolor="#4fc3f7",
                        borderwidth=2, relief="solid")
        style.configure("Vertical.TScrollbar",
                        background="#1e1e1e", troughcolor="#1e1e1e",
                        bordercolor="#1e1e1e", arrowcolor="white")
        style.map("Treeview", background=[("selected", "#4fc3f7")])


if __name__ == "__main__":
    app = App()
    sv_ttk.set_theme("dark")
    app.mainloop()