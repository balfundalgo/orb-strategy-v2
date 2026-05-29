"""
gui.py
ORB Strategy V2 — Futures + Options GUI
Balfund Trading Private Limited

White/Blue theme. Paper trading only (Live disabled for client).
Configurable: credentials, instruments, timeframe, RSI, targets/SL/TSL, session timing.
"""

from __future__ import annotations

import io
import json
import os
import queue
import sys
import threading
import time
import importlib
from datetime import datetime
from typing import Optional, Dict

import customtkinter as ctk

from strategy import StrategyConfig, InstrumentConfig, ORBStrategyV2


# ============================================================================
# STDOUT REDIRECTOR
# ============================================================================
class QueueWriter(io.TextIOBase):
    def __init__(self, q: queue.Queue) -> None:
        self._q = q

    def write(self, s: str) -> int:
        if s and s != "\n":
            self._q.put(s)
        return len(s)

    def flush(self) -> None:
        pass


# ============================================================================
# STRATEGY RUNNER THREAD
# ============================================================================
class StrategyRunner(threading.Thread):
    def __init__(self, cfg: StrategyConfig, log_queue: queue.Queue) -> None:
        super().__init__(daemon=True)
        self.cfg = cfg
        self.log_queue = log_queue
        self.engine: Optional[ORBStrategyV2] = None
        self._status = "STOPPED"

    def run(self) -> None:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        writer = QueueWriter(self.log_queue)
        sys.stdout = writer
        sys.stderr = writer
        try:
            self._status = "RUNNING"
            self.log_queue.put("[STRATEGY] Starting...")
            self.engine = ORBStrategyV2(self.cfg)
            if self.engine.initialize():
                self.engine.run()
            else:
                self.log_queue.put("[ERROR] Initialization failed")
        except Exception as e:
            import traceback
            self.log_queue.put(f"[ERROR] Strategy crashed: {e}")
            self.log_queue.put(traceback.format_exc())
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
            self._status = "STOPPED"
            self.log_queue.put("[STRATEGY] Stopped.")

    def stop(self) -> None:
        if self.engine:
            self.engine.stop()
        self.log_queue.put("[GUI] Stop requested...")


# ============================================================================
# GUI — White + Blue Theme
# ============================================================================
class StrategyGUI:
    CLR_BG       = "#f7f8fc"
    CLR_PANEL    = "#ffffff"
    CLR_CARD     = "#f0f4ff"
    CLR_HEADER   = "#1a56db"
    CLR_ACCENT   = "#2563eb"
    CLR_ACCENT_L = "#3b82f6"
    CLR_GREEN    = "#16a34a"
    CLR_RED      = "#dc2626"
    CLR_TEXT     = "#1e293b"
    CLR_MUTED    = "#64748b"
    CLR_BORDER   = "#e2e8f0"
    CLR_INPUT_BG = "#f1f5f9"
    CLR_LOG_BG   = "#fafbfe"

    SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "orb_v2_settings.json")
    CREDS_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "fyers_credentials.json")

    @staticmethod
    def _load_credentials() -> dict:
        if os.path.exists(StrategyGUI.CREDS_FILE):
            try:
                with open(StrategyGUI.CREDS_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"app_id": "", "secret_key": "", "fy_id": "", "totp_key": "", "pin": ""}

    @staticmethod
    def _save_credentials(data: dict) -> None:
        with open(StrategyGUI.CREDS_FILE, "w") as f:
            json.dump(data, f)

    def __init__(self) -> None:
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.root = ctk.CTk()
        self.root.title("ORB Strategy V2  |  Balfund Trading Pvt. Ltd.")
        self.root.geometry("1280x820")
        self.root.minsize(1050, 700)
        self.root.configure(fg_color=self.CLR_BG)

        self.log_queue: queue.Queue = queue.Queue()
        self.runner: Optional[StrategyRunner] = None
        self._status = "STOPPED"

        self._build_ui()
        self._load_settings()
        self._poll_log()
        self._poll_pnl()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI Helpers ─────────────────────────────────────────────────────────
    def _section(self, parent, title: str) -> ctk.CTkFrame:
        ctk.CTkLabel(parent, text=title, font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=self.CLR_ACCENT).pack(anchor="w", padx=16, pady=(12, 2))
        frame = ctk.CTkFrame(parent, fg_color=self.CLR_CARD, corner_radius=8,
                             border_width=1, border_color=self.CLR_BORDER)
        frame.pack(fill="x", padx=10, pady=(0, 4))
        return frame

    def _row(self, parent, label: str, widget_fn, label_width=130):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(row, text=label, font=ctk.CTkFont(size=11),
                     text_color=self.CLR_TEXT, width=label_width, anchor="w").pack(side="left")
        w = widget_fn(row)
        w.pack(side="right")
        return w

    def _entry(self, parent, var, width=80, show=None, placeholder=None):
        kwargs = {"textvariable": var, "width": width, "font": ctk.CTkFont(size=11),
                  "fg_color": self.CLR_INPUT_BG, "border_color": self.CLR_BORDER,
                  "text_color": self.CLR_TEXT}
        if show:
            kwargs["show"] = show
        if placeholder:
            kwargs["placeholder_text"] = placeholder
        return ctk.CTkEntry(parent, **kwargs)

    # ── Build UI ───────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        # Header
        header = ctk.CTkFrame(self.root, fg_color=self.CLR_HEADER, height=54, corner_radius=0)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        ctk.CTkLabel(header, text="  ORB Strategy V2 — Futures + Options",
                     font=ctk.CTkFont(size=16, weight="bold"), text_color="#fff").pack(side="left", padx=16, pady=12)
        ctk.CTkLabel(header, text="Balfund Trading Pvt. Ltd.  ",
                     font=ctk.CTkFont(size=11), text_color="#93c5fd").pack(side="right", padx=16, pady=12)

        body = ctk.CTkFrame(self.root, fg_color=self.CLR_BG, corner_radius=0)
        body.pack(fill="both", expand=True, padx=10, pady=(6, 10))

        # Left panel (config)
        left = ctk.CTkFrame(body, fg_color=self.CLR_PANEL, width=360,
                            corner_radius=10, border_width=1, border_color=self.CLR_BORDER)
        left.pack(fill="y", side="left", padx=(0, 5))
        left.pack_propagate(False)
        left_scroll = ctk.CTkScrollableFrame(left, fg_color=self.CLR_PANEL, corner_radius=0)
        left_scroll.pack(fill="both", expand=True)

        # Right panel (log)
        right = ctk.CTkFrame(body, fg_color=self.CLR_PANEL,
                             corner_radius=10, border_width=1, border_color=self.CLR_BORDER)
        right.pack(fill="both", expand=True, side="left", padx=(5, 0))

        self._build_left(left_scroll)
        self._build_right(right)

    def _build_left(self, parent) -> None:
        # ── Credentials ──
        fc = self._section(parent, "FYERS CREDENTIALS")
        saved = self._load_credentials()
        self.app_id_var = ctk.StringVar(value=saved.get("app_id", ""))
        self.secret_var = ctk.StringVar(value=saved.get("secret_key", ""))
        self.fy_id_var = ctk.StringVar(value=saved.get("fy_id", ""))
        self.totp_var = ctk.StringVar(value=saved.get("totp_key", ""))
        self.pin_var = ctk.StringVar(value=saved.get("pin", ""))

        self._row(fc, "App ID", lambda p: self._entry(p, self.app_id_var, 155, placeholder="e.g. XXXXXX-200"))
        self._row(fc, "Secret Key", lambda p: self._entry(p, self.secret_var, 155, show="*"))
        self._row(fc, "Fyers ID", lambda p: self._entry(p, self.fy_id_var, 155, placeholder="e.g. YN04712"))
        self._row(fc, "TOTP Secret", lambda p: self._entry(p, self.totp_var, 155, show="*"))
        self._row(fc, "PIN", lambda p: self._entry(p, self.pin_var, 155, show="*"))

        save_row = ctk.CTkFrame(fc, fg_color="transparent")
        save_row.pack(fill="x", padx=10, pady=(2, 6))
        self.creds_status = ctk.CTkLabel(save_row, text="", font=ctk.CTkFont(size=10), text_color=self.CLR_GREEN)
        self.creds_status.pack(side="left")
        ctk.CTkButton(save_row, text="Save", width=60, height=26, font=ctk.CTkFont(size=11),
                      fg_color=self.CLR_ACCENT, hover_color=self.CLR_ACCENT_L, text_color="#fff",
                      corner_radius=6, command=self._save_creds_clicked).pack(side="right")

        # ── Mode ──
        f = self._section(parent, "TRADING MODE")
        mode_row = ctk.CTkFrame(f, fg_color="transparent")
        mode_row.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(mode_row, text="Mode", font=ctk.CTkFont(size=11),
                     text_color=self.CLR_TEXT, width=130, anchor="w").pack(side="left")
        ctk.CTkLabel(mode_row, text="Paper Trading", font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=self.CLR_ACCENT).pack(side="right")

        # ── Timeframe ──
        tf = self._section(parent, "TIMEFRAME")
        self.tf_var = ctk.StringVar(value="5")
        self._row(tf, "Candle (min)", lambda p: ctk.CTkSegmentedButton(
            p, values=["1", "5", "10", "15", "30"], variable=self.tf_var,
            font=ctk.CTkFont(size=11), width=200,
            selected_color=self.CLR_ACCENT, selected_hover_color=self.CLR_ACCENT_L))

        # ── Session ──
        ss = self._section(parent, "SESSION TIMING (IST)")
        self.entry_end_h = ctk.StringVar(value="14")
        self.entry_end_m = ctk.StringVar(value="45")
        self.exit_h = ctk.StringVar(value="15")
        self.exit_m = ctk.StringVar(value="29")
        self._row(ss, "Entry End (HH:MM)", lambda p: self._time_entry(p, self.entry_end_h, self.entry_end_m))
        self._row(ss, "Force Exit (HH:MM)", lambda p: self._time_entry(p, self.exit_h, self.exit_m))

        # ── RSI ──
        rs = self._section(parent, "RSI SETTINGS")
        self.rsi_period_var = ctk.StringVar(value="14")
        self.rsi_upper_var = ctk.StringVar(value="80")
        self.rsi_lower_var = ctk.StringVar(value="30")
        self._row(rs, "Period", lambda p: self._entry(p, self.rsi_period_var, 60))
        self._row(rs, "Upper Band", lambda p: self._entry(p, self.rsi_upper_var, 60))
        self._row(rs, "Lower Band", lambda p: self._entry(p, self.rsi_lower_var, 60))

        # ── Futures Target/SL/TSL ──
        ft = self._section(parent, "FUTURES — Target/SL/TSL (₹/lot)")
        self.fut_target_var = ctk.StringVar(value="3000")
        self.fut_sl_var = ctk.StringVar(value="1500")
        self.fut_tsl_var = ctk.StringVar(value="500")
        self._row(ft, "Target", lambda p: self._entry(p, self.fut_target_var, 70))
        self._row(ft, "Stop Loss", lambda p: self._entry(p, self.fut_sl_var, 70))
        self._row(ft, "TSL Step", lambda p: self._entry(p, self.fut_tsl_var, 70))

        # ── Options Target/SL/TSL ──
        ot = self._section(parent, "OPTIONS — Target/SL/TSL (₹/lot)")
        self.opt_target_var = ctk.StringVar(value="2000")
        self.opt_sl_var = ctk.StringVar(value="1000")
        self.opt_tsl_var = ctk.StringVar(value="400")
        self._row(ot, "Target", lambda p: self._entry(p, self.opt_target_var, 70))
        self._row(ot, "Stop Loss", lambda p: self._entry(p, self.opt_sl_var, 70))
        self._row(ot, "TSL Step", lambda p: self._entry(p, self.opt_tsl_var, 70))

        # ── Instruments ──
        inst_sec = self._section(parent, "INSTRUMENTS")
        self.inst_vars: Dict[str, Dict[str, ctk.StringVar]] = {}

        instruments = [
            ("NIFTY", "50", "200", "1", "1", True, True),
            ("BANKNIFTY", "100", "300", "1", "1", True, True),
            ("SENSEX", "100", "500", "1", "1", False, False),
            ("MIDCPNIFTY", "50", "200", "1", "1", False, False),
            ("FINNIFTY", "50", "200", "1", "1", False, False),
        ]

        for name, step, offset, flots, olots, fut_en, opt_en in instruments:
            d = {}
            fr = ctk.CTkFrame(inst_sec, fg_color="transparent")
            fr.pack(fill="x", padx=6, pady=2)

            d["enable_fut"] = ctk.StringVar(value="1" if fut_en else "0")
            d["enable_opt"] = ctk.StringVar(value="1" if opt_en else "0")
            d["offset"] = ctk.StringVar(value=offset)
            d["flots"] = ctk.StringVar(value=flots)
            d["olots"] = ctk.StringVar(value=olots)
            d["step"] = step

            ctk.CTkLabel(fr, text=name, font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=self.CLR_TEXT, width=80, anchor="w").pack(side="left")
            ctk.CTkCheckBox(fr, text="Fut", variable=d["enable_fut"], onvalue="1", offvalue="0",
                            font=ctk.CTkFont(size=10), fg_color=self.CLR_ACCENT, width=45).pack(side="left")
            ctk.CTkCheckBox(fr, text="Opt", variable=d["enable_opt"], onvalue="1", offvalue="0",
                            font=ctk.CTkFont(size=10), fg_color=self.CLR_ACCENT, width=45).pack(side="left")
            ctk.CTkLabel(fr, text="OTM:", font=ctk.CTkFont(size=9), text_color=self.CLR_MUTED).pack(side="left", padx=(2,0))
            ctk.CTkEntry(fr, textvariable=d["offset"], width=45, font=ctk.CTkFont(size=10),
                         fg_color=self.CLR_INPUT_BG, border_color=self.CLR_BORDER, text_color=self.CLR_TEXT).pack(side="left", padx=1)
            ctk.CTkLabel(fr, text="FL:", font=ctk.CTkFont(size=9), text_color=self.CLR_MUTED).pack(side="left")
            ctk.CTkEntry(fr, textvariable=d["flots"], width=30, font=ctk.CTkFont(size=10),
                         fg_color=self.CLR_INPUT_BG, border_color=self.CLR_BORDER, text_color=self.CLR_TEXT).pack(side="left", padx=1)
            ctk.CTkLabel(fr, text="OL:", font=ctk.CTkFont(size=9), text_color=self.CLR_MUTED).pack(side="left")
            ctk.CTkEntry(fr, textvariable=d["olots"], width=30, font=ctk.CTkFont(size=10),
                         fg_color=self.CLR_INPUT_BG, border_color=self.CLR_BORDER, text_color=self.CLR_TEXT).pack(side="left", padx=1)

            self.inst_vars[name] = d

        # ── P&L ──
        pnl_sec = self._section(parent, "TODAY'S P&L")
        self.pnl_label = ctk.CTkLabel(pnl_sec, text="₹ 0",
                                       font=ctk.CTkFont(size=26, weight="bold"), text_color=self.CLR_TEXT)
        self.pnl_label.pack(pady=8)

        # ── Status + Buttons ──
        self.status_label = ctk.CTkLabel(parent, text="  ■ STOPPED",
                                          font=ctk.CTkFont(size=13, weight="bold"), text_color=self.CLR_RED)
        self.status_label.pack(pady=(8, 4))

        btn = ctk.CTkFrame(parent, fg_color="transparent")
        btn.pack(fill="x", padx=10, pady=4)
        self.start_btn = ctk.CTkButton(btn, text="▶  START STRATEGY", font=ctk.CTkFont(size=14, weight="bold"),
                                        fg_color=self.CLR_ACCENT, hover_color=self.CLR_ACCENT_L, text_color="#fff",
                                        height=42, corner_radius=8, command=self._start)
        self.start_btn.pack(fill="x", pady=(0, 5))
        self.stop_btn = ctk.CTkButton(btn, text="■  STOP", font=ctk.CTkFont(size=14, weight="bold"),
                                       fg_color=self.CLR_RED, hover_color="#b91c1c", text_color="#fff",
                                       height=42, corner_radius=8, state="disabled", command=self._stop)
        self.stop_btn.pack(fill="x")

    def _time_entry(self, parent, hvar, mvar):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkEntry(f, textvariable=hvar, width=35, font=ctk.CTkFont(size=11),
                     fg_color=self.CLR_INPUT_BG, border_color=self.CLR_BORDER, text_color=self.CLR_TEXT).pack(side="left")
        ctk.CTkLabel(f, text=":", font=ctk.CTkFont(size=11), text_color=self.CLR_TEXT).pack(side="left")
        ctk.CTkEntry(f, textvariable=mvar, width=35, font=ctk.CTkFont(size=11),
                     fg_color=self.CLR_INPUT_BG, border_color=self.CLR_BORDER, text_color=self.CLR_TEXT).pack(side="left")
        return f

    def _build_right(self, parent) -> None:
        tabs = ctk.CTkTabview(parent, fg_color=self.CLR_PANEL,
                              segmented_button_fg_color=self.CLR_CARD,
                              segmented_button_selected_color=self.CLR_ACCENT,
                              segmented_button_selected_hover_color=self.CLR_ACCENT_L,
                              text_color=self.CLR_TEXT, corner_radius=8)
        tabs.pack(fill="both", expand=True, padx=6, pady=6)
        self.tabs = tabs

        # ── P&L Summary bar at top ──
        pnl_bar = ctk.CTkFrame(parent, fg_color=self.CLR_CARD, height=50, corner_radius=8,
                                border_width=1, border_color=self.CLR_BORDER)
        pnl_bar.pack(fill="x", padx=6, pady=(0, 6), side="bottom")
        pnl_bar.pack_propagate(False)

        ctk.CTkLabel(pnl_bar, text="Futures P&L:", font=ctk.CTkFont(size=11),
                     text_color=self.CLR_MUTED).pack(side="left", padx=(12, 2))
        self.fut_pnl_label = ctk.CTkLabel(pnl_bar, text="₹ 0", font=ctk.CTkFont(size=13, weight="bold"),
                                           text_color=self.CLR_TEXT)
        self.fut_pnl_label.pack(side="left", padx=(0, 20))

        ctk.CTkLabel(pnl_bar, text="Options P&L:", font=ctk.CTkFont(size=11),
                     text_color=self.CLR_MUTED).pack(side="left", padx=(0, 2))
        self.opt_pnl_label = ctk.CTkLabel(pnl_bar, text="₹ 0", font=ctk.CTkFont(size=13, weight="bold"),
                                           text_color=self.CLR_TEXT)
        self.opt_pnl_label.pack(side="left", padx=(0, 20))

        ctk.CTkLabel(pnl_bar, text="TOTAL:", font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=self.CLR_TEXT).pack(side="left", padx=(0, 2))
        self.total_pnl_label = ctk.CTkLabel(pnl_bar, text="₹ 0",
                                             font=ctk.CTkFont(size=16, weight="bold"), text_color=self.CLR_TEXT)
        self.total_pnl_label.pack(side="left", padx=(0, 10))

        # ── Futures Status Tab ──
        fut_tab = tabs.add("Futures")
        self._build_futures_table(fut_tab)

        # ── Options Status Tab ──
        opt_tab = tabs.add("Options")
        self._build_options_table(opt_tab)

        # ── Log Tab ──
        log_tab = tabs.add("Live Log")
        log_frame = ctk.CTkFrame(log_tab, fg_color=self.CLR_LOG_BG, corner_radius=8)
        log_frame.pack(fill="both", expand=True, padx=4, pady=4)
        self.log_text = ctk.CTkTextbox(log_frame, wrap="word", font=ctk.CTkFont(family="Consolas", size=11),
                                        fg_color=self.CLR_LOG_BG, text_color=self.CLR_TEXT, border_width=0, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)
        ctk.CTkButton(log_frame, text="Clear", width=70, height=26, font=ctk.CTkFont(size=11),
                      fg_color=self.CLR_BORDER, hover_color=self.CLR_MUTED, text_color=self.CLR_TEXT,
                      corner_radius=6, command=self._clear_log).pack(side="right", padx=8, pady=4)

    def _build_futures_table(self, parent) -> None:
        scroll = ctk.CTkScrollableFrame(parent, fg_color=self.CLR_PANEL)
        scroll.pack(fill="both", expand=True)

        # Header
        hdr = ctk.CTkFrame(scroll, fg_color=self.CLR_ACCENT, corner_radius=6, height=32)
        hdr.pack(fill="x", padx=4, pady=(4, 2))
        hdr.pack_propagate(False)

        fut_headers = ["Instrument", "Symbol", "ORB High", "ORB Low", "Direction", "Entry", "CMP", "Lot", "Status", "PNL"]
        fut_widths = [70, 120, 65, 65, 55, 65, 65, 35, 60, 75]

        for text, w in zip(fut_headers, fut_widths):
            ctk.CTkLabel(hdr, text=text, width=w, font=ctk.CTkFont(size=10, weight="bold"),
                         text_color="#fff", anchor="center").pack(side="left", padx=1)

        self.fut_table_rows: Dict[str, Dict[str, ctk.CTkLabel]] = {}
        for i, name in enumerate(["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY", "FINNIFTY"]):
            bg = self.CLR_CARD if i % 2 == 0 else self.CLR_PANEL
            row = ctk.CTkFrame(scroll, fg_color=bg, corner_radius=4, height=30)
            row.pack(fill="x", padx=4, pady=1)
            row.pack_propagate(False)
            labels = {}
            fields = ["name", "symbol", "or_high", "or_low", "direction", "entry", "cmp", "lot_size", "status", "pnl"]
            for field, w in zip(fields, fut_widths):
                val = name if field == "name" else "—"
                lbl = ctk.CTkLabel(row, text=val, width=w, font=ctk.CTkFont(size=10), text_color=self.CLR_MUTED, anchor="center")
                lbl.pack(side="left", padx=1)
                labels[field] = lbl
            self.fut_table_rows[name] = labels

    def _build_options_table(self, parent) -> None:
        scroll = ctk.CTkScrollableFrame(parent, fg_color=self.CLR_PANEL)
        scroll.pack(fill="both", expand=True)

        hdr = ctk.CTkFrame(scroll, fg_color=self.CLR_ACCENT, corner_radius=6, height=32)
        hdr.pack(fill="x", padx=4, pady=(4, 2))
        hdr.pack_propagate(False)

        opt_headers = ["Instrument", "Strike", "ORB High", "ORB Low", "Entry", "CMP", "Lot", "Status", "PNL"]
        opt_widths = [70, 130, 60, 60, 60, 60, 35, 60, 75]

        for text, w in zip(opt_headers, opt_widths):
            ctk.CTkLabel(hdr, text=text, width=w, font=ctk.CTkFont(size=10, weight="bold"),
                         text_color="#fff", anchor="center").pack(side="left", padx=1)

        self.opt_table_rows: Dict[str, Dict[str, ctk.CTkLabel]] = {}
        idx = 0
        for name in ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY", "FINNIFTY"]:
            for ot in ["CE", "PE"]:
                key = f"{name}_{ot}"
                bg = self.CLR_CARD if idx % 2 == 0 else self.CLR_PANEL
                row = ctk.CTkFrame(scroll, fg_color=bg, corner_radius=4, height=30)
                row.pack(fill="x", padx=4, pady=1)
                row.pack_propagate(False)
                labels = {}
                fields = ["name", "strike", "or_high", "or_low", "entry", "cmp", "lot_size", "status", "pnl"]
                defaults = {
                    "name": name, "strike": ot,
                }
                for field, w in zip(fields, opt_widths):
                    val = defaults.get(field, "—")
                    lbl = ctk.CTkLabel(row, text=val, width=w, font=ctk.CTkFont(size=10), text_color=self.CLR_MUTED, anchor="center")
                    lbl.pack(side="left", padx=1)
                    labels[field] = lbl
                self.opt_table_rows[key] = labels
                idx += 1

    # ── Credentials ────────────────────────────────────────────────────────
    def _save_creds_clicked(self) -> None:
        data = {
            "app_id": self.app_id_var.get().strip(),
            "secret_key": self.secret_var.get().strip(),
            "fy_id": self.fy_id_var.get().strip(),
            "totp_key": self.totp_var.get().strip(),
            "pin": self.pin_var.get().strip(),
        }
        if not all(data.values()):
            self.creds_status.configure(text="All fields required", text_color=self.CLR_RED)
            return
        self._save_credentials(data)
        self.creds_status.configure(text="Saved ✓", text_color=self.CLR_GREEN)

    def _apply_credentials(self) -> None:
        saved = self._load_credentials()
        app_id_full = saved.get("app_id", "").strip()
        secret = saved.get("secret_key", "").strip()
        fy_id = saved.get("fy_id", "").strip()
        totp_key = saved.get("totp_key", "").strip()
        pin = saved.get("pin", "").strip()
        if not app_id_full or not secret:
            return

        if "-" in app_id_full:
            app_id, app_type = app_id_full.rsplit("-", 1)
        else:
            app_id, app_type = app_id_full, "200"
        client_id = f"{app_id}-{app_type}"

        patch = {"APP_ID": app_id, "APP_TYPE": app_type, "SECRET_KEY": secret,
                 "CLIENT_ID": client_id, "REDIRECT_URL": "https://trade.fyers.in/api-login/redirect-uri/index.html"}
        if fy_id:
            patch["FYERS_ID"] = fy_id
            patch["FY_ID"] = fy_id
        if pin:
            patch["PIN"] = pin
        if totp_key:
            patch["TOTP_SECRET"] = totp_key
            patch["TOTP_KEY"] = totp_key

        # Patch modules
        for mod_name in ("fyers_connect",):
            try:
                mod = importlib.import_module(mod_name)
                for k, v in patch.items():
                    if hasattr(mod, k):
                        setattr(mod, k, v)
                self.log_queue.put(f"[CREDS] {mod_name} patched: CLIENT_ID={client_id}")
            except ImportError:
                self.log_queue.put(f"[CREDS] {mod_name} not found (bundled mode)")

        # Patch __main__ globals for bundled mode
        main_mod = sys.modules.get("__main__")
        if main_mod:
            for k, v in patch.items():
                if hasattr(main_mod, k):
                    setattr(main_mod, k, v)
            self.log_queue.put(f"[CREDS] __main__ patched: CLIENT_ID={getattr(main_mod, 'CLIENT_ID', '?')}")

    # ── Build Config ───────────────────────────────────────────────────────
    def _build_config(self) -> StrategyConfig:
        instruments = {}
        step_map = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100, "MIDCPNIFTY": 50, "FINNIFTY": 50}

        for name, d in self.inst_vars.items():
            instruments[name] = InstrumentConfig(
                name=name,
                strike_step=int(d["step"]),
                otm_offset=int(d["offset"].get() or "200"),
                lots_futures=int(d["flots"].get() or "1"),
                lots_options=int(d["olots"].get() or "1"),
                enable_futures=d["enable_fut"].get() == "1",
                enable_options=d["enable_opt"].get() == "1",
            )

        def safe_int(var, default):
            try: return int(var.get())
            except: return default

        def safe_float(var, default):
            try: return float(var.get())
            except: return default

        return StrategyConfig(
            paper_trading=True,
            timeframe_minutes=safe_int(self.tf_var, 5),
            entry_end_time=(safe_int(self.entry_end_h, 14), safe_int(self.entry_end_m, 45)),
            force_exit_time=(safe_int(self.exit_h, 15), safe_int(self.exit_m, 29)),
            rsi_period=safe_int(self.rsi_period_var, 14),
            rsi_upper=safe_int(self.rsi_upper_var, 80),
            rsi_lower=safe_int(self.rsi_lower_var, 30),
            futures_target=safe_float(self.fut_target_var, 3000),
            futures_sl=safe_float(self.fut_sl_var, 1500),
            futures_tsl_step=safe_float(self.fut_tsl_var, 500),
            options_target=safe_float(self.opt_target_var, 2000),
            options_sl=safe_float(self.opt_sl_var, 1000),
            options_tsl_step=safe_float(self.opt_tsl_var, 400),
            instruments=instruments,
        )

    # ── Start / Stop ───────────────────────────────────────────────────────
    def _start(self) -> None:
        try:
            if self.runner and self.runner.is_alive():
                return

            self.tabs.set("Live Log")
            self.log_queue.put("[GUI] Start button clicked.")

            saved = self._load_credentials()
            if not saved.get("app_id") or not saved.get("secret_key"):
                self.log_queue.put("[ERROR] Save your Fyers credentials first.")
                return

            self._apply_credentials()
            self._save_settings()
            cfg = self._build_config()

            enabled_f = [k for k, v in cfg.instruments.items() if v.enable_futures]
            enabled_o = [k for k, v in cfg.instruments.items() if v.enable_options]
            self.log_queue.put(f"[GUI] Futures: {enabled_f}, Options: {enabled_o}, TF: {cfg.timeframe_minutes}m")

            self.runner = StrategyRunner(cfg=cfg, log_queue=self.log_queue)
            self.runner.start()
            self._update_status("RUNNING")
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
        except Exception as e:
            import traceback
            self.log_queue.put(f"[GUI ERROR] {e}")
            self.log_queue.put(traceback.format_exc())

    def _stop(self) -> None:
        if self.runner:
            self.runner.stop()
        self.stop_btn.configure(state="disabled")

    # ── Polling ────────────────────────────────────────────────────────────
    def _poll_log(self) -> None:
        count = 0
        while not self.log_queue.empty() and count < 50:
            try:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                ts = datetime.now().strftime("%H:%M:%S")
                self.log_text.insert("end", f"[{ts}] {msg}\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
                count += 1
            except Exception:
                break
        if self.runner and not self.runner.is_alive() and self._status == "RUNNING":
            self._update_status("STOPPED")
        self.root.after(200, self._poll_log)

    def _poll_pnl(self) -> None:
        if self.runner and self.runner.engine:
            try:
                data = self.runner.engine.get_live_pnl()

                # P&L summary bar
                fut_pnl = data.get("futures_pnl", 0)
                opt_pnl = data.get("options_pnl", 0)
                total = fut_pnl + opt_pnl

                self.fut_pnl_label.configure(text=f"₹ {fut_pnl:,.0f}",
                                              text_color=self.CLR_GREEN if fut_pnl >= 0 else self.CLR_RED)
                self.opt_pnl_label.configure(text=f"₹ {opt_pnl:,.0f}",
                                              text_color=self.CLR_GREEN if opt_pnl >= 0 else self.CLR_RED)
                self.total_pnl_label.configure(text=f"₹ {total:,.0f}",
                                                text_color=self.CLR_GREEN if total >= 0 else self.CLR_RED)
                self.pnl_label.configure(text=f"₹ {total:,.0f}",
                                          text_color=self.CLR_GREEN if total >= 0 else self.CLR_RED)

                # Futures table
                for name, info in data.get("futures", {}).items():
                    if name not in self.fut_table_rows:
                        continue
                    r = self.fut_table_rows[name]
                    # Symbol
                    sym = info.get("symbol", "")
                    if sym:
                        sym_short = sym.split(":")[-1]
                        r["symbol"].configure(text=sym_short, text_color=self.CLR_TEXT)
                    if info.get("or_high") is not None:
                        r["or_high"].configure(text=f"{info['or_high']:.0f}", text_color=self.CLR_TEXT)
                        r["or_low"].configure(text=f"{info['or_low']:.0f}", text_color=self.CLR_TEXT)
                    if info.get("direction"):
                        dc = self.CLR_GREEN if info["direction"] == "BUY" else self.CLR_RED
                        r["direction"].configure(text=info["direction"], text_color=dc)
                    if info.get("entry"):
                        r["entry"].configure(text=f"{info['entry']:.1f}", text_color=self.CLR_TEXT)
                    if info.get("cmp") and info["cmp"] > 0:
                        r["cmp"].configure(text=f"{info['cmp']:.1f}", text_color=self.CLR_ACCENT)
                    r["lot_size"].configure(text=str(info.get("lot_size", "")), text_color=self.CLR_TEXT)
                    st = info.get("status", "Watching")
                    sc = self.CLR_GREEN if st == "IN TRADE" else self.CLR_MUTED
                    r["status"].configure(text=st, text_color=sc)
                    pnl = info.get("live_pnl", 0) + info.get("closed_pnl", 0)
                    pc = self.CLR_GREEN if pnl >= 0 else self.CLR_RED
                    r["pnl"].configure(text=f"₹{pnl:,.0f}" if pnl != 0 else "—", text_color=pc if pnl != 0 else self.CLR_MUTED)

                # Options table
                for key, info in data.get("options", {}).items():
                    if key not in self.opt_table_rows:
                        continue
                    r = self.opt_table_rows[key]
                    # Strike symbol (e.g. NIFTY26JUN24150CE)
                    sym = info.get("symbol", "")
                    if sym:
                        sym_short = sym.split(":")[-1]
                        r["strike"].configure(text=sym_short, text_color=self.CLR_TEXT)
                    else:
                        r["strike"].configure(text=info.get("opt_type", ""), text_color=self.CLR_MUTED)
                    if info.get("or_high") is not None:
                        r["or_high"].configure(text=f"{info['or_high']:.1f}", text_color=self.CLR_TEXT)
                        r["or_low"].configure(text=f"{info['or_low']:.1f}", text_color=self.CLR_TEXT)
                    if info.get("entry"):
                        r["entry"].configure(text=f"{info['entry']:.1f}", text_color=self.CLR_TEXT)
                    if info.get("cmp") and info["cmp"] > 0:
                        r["cmp"].configure(text=f"{info['cmp']:.1f}", text_color=self.CLR_ACCENT)
                    r["lot_size"].configure(text=str(info.get("lot_size", "")), text_color=self.CLR_TEXT)
                    st = info.get("status", "Watching")
                    sc = self.CLR_GREEN if st == "IN TRADE" else self.CLR_MUTED
                    r["status"].configure(text=st, text_color=sc)
                    pnl = info.get("live_pnl", 0) + info.get("closed_pnl", 0)
                    pc = self.CLR_GREEN if pnl >= 0 else self.CLR_RED
                    r["pnl"].configure(text=f"₹{pnl:,.0f}" if pnl != 0 else "—", text_color=pc if pnl != 0 else self.CLR_MUTED)

            except Exception:
                pass
        self.root.after(500, self._poll_pnl)

    def _update_status(self, status: str) -> None:
        self._status = status
        if status == "RUNNING":
            self.status_label.configure(text="  ● RUNNING", text_color=self.CLR_GREEN)
        else:
            self.status_label.configure(text="  ■ STOPPED", text_color=self.CLR_RED)
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ── Settings ───────────────────────────────────────────────────────────
    def _save_settings(self) -> None:
        data = {
            "timeframe": self.tf_var.get(),
            "entry_end_h": self.entry_end_h.get(), "entry_end_m": self.entry_end_m.get(),
            "exit_h": self.exit_h.get(), "exit_m": self.exit_m.get(),
            "rsi_period": self.rsi_period_var.get(),
            "rsi_upper": self.rsi_upper_var.get(), "rsi_lower": self.rsi_lower_var.get(),
            "fut_target": self.fut_target_var.get(), "fut_sl": self.fut_sl_var.get(), "fut_tsl": self.fut_tsl_var.get(),
            "opt_target": self.opt_target_var.get(), "opt_sl": self.opt_sl_var.get(), "opt_tsl": self.opt_tsl_var.get(),
            "instruments": {},
        }
        for name, d in self.inst_vars.items():
            data["instruments"][name] = {
                "enable_fut": d["enable_fut"].get(), "enable_opt": d["enable_opt"].get(),
                "offset": d["offset"].get(), "flots": d["flots"].get(), "olots": d["olots"].get(),
            }
        try:
            with open(self.SETTINGS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _load_settings(self) -> None:
        if not os.path.exists(self.SETTINGS_FILE):
            return
        try:
            with open(self.SETTINGS_FILE, "r") as f:
                data = json.load(f)
            for var_name, key in [("tf_var", "timeframe"), ("entry_end_h", "entry_end_h"),
                                   ("entry_end_m", "entry_end_m"), ("exit_h", "exit_h"), ("exit_m", "exit_m"),
                                   ("rsi_period_var", "rsi_period"), ("rsi_upper_var", "rsi_upper"),
                                   ("rsi_lower_var", "rsi_lower"),
                                   ("fut_target_var", "fut_target"), ("fut_sl_var", "fut_sl"), ("fut_tsl_var", "fut_tsl"),
                                   ("opt_target_var", "opt_target"), ("opt_sl_var", "opt_sl"), ("opt_tsl_var", "opt_tsl")]:
                if key in data and hasattr(self, var_name):
                    getattr(self, var_name).set(data[key])
            for name, vals in data.get("instruments", {}).items():
                if name in self.inst_vars:
                    for k, v in vals.items():
                        if k in self.inst_vars[name]:
                            self.inst_vars[name][k].set(v)
        except Exception:
            pass

    def _on_close(self) -> None:
        self._save_settings()
        if self.runner and self.runner.is_alive():
            self.runner.stop()
            time.sleep(0.5)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    app = StrategyGUI()
    app.run()
