from __future__ import annotations

"""
ORB Strategy V2 — Futures + Options Trading System
Balfund Trading Private Limited

Two independent systems:
  System 1 (Futures): ORB breakout on futures chart + RSI filter → Buy/Sell futures
  System 2 (Options): ORB breakdown on option premium chart + RSI filter → Sell options

Features:
  - Configurable timeframe (1, 5, 10, 15, 30 min)
  - RSI(14) with configurable period/bands
  - Fixed Target/SL/Trailing SL per lot
  - Monthly expiry with calendar-based roll (>=20th → next month)
  - Supports: NIFTY, BANKNIFTY, SENSEX, MIDCPNIFTY, FINNIFTY
  - Fyers API V3
"""

import csv
import json
import re
import time
import urllib.request
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws

import fyers_connect
from fyers_connect import auto_login

IST = pytz.timezone("Asia/Kolkata")

# ── Live LTP store (thread-safe via GIL for simple dict writes) ──────────
_live_ltp: Dict[str, float] = {}
_live_ltp_time: Dict[str, float] = {}  # symbol → time.time() of last tick
STALE_LTP_SECONDS = 60  # If no tick for 60s, LTP is considered stale

# ── SSL fix for PyInstaller ──────────────────────────────────────────────────
import ssl as _ssl
try:
    import certifi as _certifi
    _ssl_ctx = _ssl.create_default_context(cafile=_certifi.where())
except ImportError:
    _ssl_ctx = _ssl.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = _ssl.CERT_NONE
_ssl._create_default_https_context = lambda: _ssl_ctx

import websocket as _ws_mod
_orig_run_forever = _ws_mod.WebSocketApp.run_forever
def _patched_run_forever(self, **kwargs):
    if "sslopt" not in kwargs:
        kwargs["sslopt"] = {"cert_reqs": _ssl.CERT_NONE}
    return _orig_run_forever(self, **kwargs)
_ws_mod.WebSocketApp.run_forever = _patched_run_forever


# ============================================================================
# TRADE LOG — Auto-saves CSV alongside EXE
# ============================================================================
class TradeLogger:
    """Saves all trade details to a CSV file alongside the EXE."""
    HEADERS = [
        "Date", "Time_Entry", "Time_Exit", "System", "Instrument", "Option_Type",
        "Symbol", "Strike", "Side", "Entry_Price", "Exit_Price", "Lot_Size",
        "Lots", "PnL_Per_Lot", "Total_PnL", "Exit_Reason", "TSL_At_Exit"
    ]

    def __init__(self) -> None:
        import sys as _sys
        # Save alongside the EXE / script
        exe_dir = Path(_sys.argv[0]).resolve().parent
        today = datetime.now().strftime("%Y-%m-%d")
        self.filepath = exe_dir / f"trade_log_{today}.csv"
        self._write_header()

    def _write_header(self) -> None:
        if not self.filepath.exists():
            with self.filepath.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(self.HEADERS)

    def log_trade(self, trade) -> None:
        """Append a completed trade to the CSV."""
        try:
            pnl_per_lot = 0.0
            if trade.exit_price is not None:
                if trade.side == 1:
                    pnl_per_lot = (trade.exit_price - trade.entry_price) * trade.lot_size
                else:
                    pnl_per_lot = (trade.entry_price - trade.exit_price) * trade.lot_size
            total_pnl = pnl_per_lot * trade.lots

            # Determine option type and strike
            opt_type = ""
            strike = ""
            sym = trade.symbol
            if "CE" in sym.upper():
                opt_type = "CE"
            elif "PE" in sym.upper():
                opt_type = "PE"
            elif "FUT" in sym.upper():
                opt_type = "FUT"

            # Extract strike from symbol if option
            import re
            strike_match = re.search(r'(\d{4,6})(CE|PE)$', sym.split(":")[-1])
            if strike_match:
                strike = strike_match.group(1)

            entry_date = trade.entry_time.strftime("%Y-%m-%d") if trade.entry_time else ""
            entry_time = trade.entry_time.strftime("%H:%M:%S") if trade.entry_time else ""
            exit_time = trade.exit_time.strftime("%H:%M:%S") if trade.exit_time else ""
            side_str = "BUY" if trade.side == 1 else "SELL"

            row = [
                entry_date, entry_time, exit_time, trade.system, trade.instrument,
                opt_type, sym, strike, side_str,
                f"{trade.entry_price:.2f}",
                f"{trade.exit_price:.2f}" if trade.exit_price else "",
                trade.lot_size, trade.lots,
                f"{pnl_per_lot:.0f}", f"{total_pnl:.0f}",
                trade.exit_reason or "",
                f"{trade.current_sl_per_lot:.0f}",
            ]

            with self.filepath.open("a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(row)
        except Exception as e:
            print(f"[TRADE LOG ERROR] {e}")


# ============================================================================
# CONFIG
# ============================================================================
@dataclass
class InstrumentConfig:
    name: str
    strike_step: int        # ATM rounding (50 or 100)
    otm_offset: int         # OTM offset in points
    lots_futures: int = 1
    lots_options: int = 1
    enable_futures: bool = True
    enable_options: bool = True


@dataclass
class StrategyConfig:
    paper_trading: bool = True

    # Timeframe
    timeframe_minutes: int = 5  # 1, 5, 10, 15, 30

    # Session timing (IST) — user configurable
    orb_candle_start: Tuple[int, int] = (9, 15)
    entry_start_time: Tuple[int, int] = (9, 20)   # After first candle closes
    entry_end_time: Tuple[int, int] = (14, 45)
    force_exit_time: Tuple[int, int] = (15, 29)

    # RSI
    rsi_period: int = 14
    rsi_upper: int = 80
    rsi_lower: int = 30

    # Target / SL / Trail Gap per lot (in ₹)
    futures_target: float = 3000.0
    futures_sl: float = 1500.0
    futures_target_trail_gap: float = 100.0  # Trail gap after target reached

    options_target: float = 2000.0
    options_sl: float = 1000.0
    options_target_trail_gap: float = 100.0  # Trail gap after target reached

    # Instruments
    instruments: Dict[str, InstrumentConfig] = field(default_factory=dict)

    # Symbol master
    symbol_master_refresh_days: int = 1

    # Polling
    poll_interval_seconds: float = 1.0

    def __post_init__(self):
        if not self.instruments:
            self.instruments = {
                "NIFTY": InstrumentConfig("NIFTY", 50, 200),
                "BANKNIFTY": InstrumentConfig("BANKNIFTY", 100, 300),
                "SENSEX": InstrumentConfig("SENSEX", 100, 500),
                "MIDCPNIFTY": InstrumentConfig("MIDCPNIFTY", 50, 200),
                "FINNIFTY": InstrumentConfig("FINNIFTY", 50, 200),
            }
        # Compute entry_start based on timeframe
        orb_h, orb_m = self.orb_candle_start
        total_min = orb_m + self.timeframe_minutes
        self.entry_start_time = (orb_h + total_min // 60, total_min % 60)


# ============================================================================
# DATA STRUCTURES
# ============================================================================
@dataclass
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def is_green(self) -> bool:
        return self.close > self.open


@dataclass
class OpeningRange:
    high: float
    low: float


@dataclass
class TradeState:
    system: str                 # "FUTURES" or "OPTIONS"
    instrument: str
    symbol: str                 # Fyers symbol being traded
    side: int                   # 1=BUY, -1=SELL
    entry_price: float
    entry_time: datetime
    lots: int
    lot_size: int
    target_per_lot: float
    sl_per_lot: float
    target_trail_gap: float = 100.0  # ₹100 trail gap after target reached
    is_live: bool = True
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None
    # Trailing SL state
    highest_pnl_per_lot: float = 0.0
    current_sl_per_lot: float = 0.0  # Will be set to -sl_per_lot initially
    order_id: Optional[str] = None

    def __post_init__(self):
        if self.current_sl_per_lot == 0.0:
            self.current_sl_per_lot = -self.sl_per_lot  # e.g. -1500 for futures

    @property
    def total_qty(self) -> int:
        return self.lots * self.lot_size

    def calc_pnl_per_lot(self, current_price: float) -> float:
        """P&L per lot based on current price"""
        if self.side == 1:  # BUY
            return (current_price - self.entry_price) * self.lot_size
        else:  # SELL
            return (self.entry_price - current_price) * self.lot_size

    def update_trailing_sl(self, current_price: float) -> None:
        """Update trailing SL: activates only after profit reaches target level.
        Before target: initial SL stays fixed. After target: tight trail with trail_gap."""
        pnl = self.calc_pnl_per_lot(current_price)
        if pnl > self.highest_pnl_per_lot:
            self.highest_pnl_per_lot = pnl

        # Trail only after target level reached
        if self.highest_pnl_per_lot >= self.target_per_lot:
            new_sl = self.highest_pnl_per_lot - self.target_trail_gap
            if new_sl > self.current_sl_per_lot:
                self.current_sl_per_lot = new_sl

    def should_exit(self, current_price: float) -> Optional[str]:
        """Check if trade should exit. Returns reason or None.
        No hard target exit — target activates tight trailing instead."""
        pnl = self.calc_pnl_per_lot(current_price)

        # Update TSL (handles both pre-target steps and post-target tight trail)
        self.update_trailing_sl(current_price)

        # SL/TSL hit
        if pnl <= self.current_sl_per_lot:
            if self.current_sl_per_lot >= 0:
                return f"TSL (₹{pnl:.0f}/lot, trail@₹{self.current_sl_per_lot:.0f})"
            else:
                return f"SL (₹{pnl:.0f}/lot)"

        return None


# ── Futures System Runtime ─────────────────────────────────────────────────
@dataclass
class FuturesRuntime:
    config: InstrumentConfig
    futures_symbol: str
    lot_size: int = 1
    candles: List[Candle] = field(default_factory=list)
    opening_range: Optional[OpeningRange] = None
    current_trade: Optional[TradeState] = None
    trade_history: List[TradeState] = field(default_factory=list)
    last_processed_bucket: Optional[datetime] = None
    # Entry state machine: WAITING → BREAKOUT → (confirmation+RSI) → IN_TRADE → WAITING
    entry_state: str = "WAITING"
    breakout_level: Optional[float] = None  # high of breakout candle (BUY) or low (SELL)
    breakout_direction: Optional[str] = None  # "BUY" or "SELL"


# ── Options System Runtime ──────────────────────────────────────────────────
@dataclass
class OptionLeg:
    """Runtime for one option leg (CE or PE)"""
    opt_type: str               # "CE" or "PE"
    symbol: str                 # Fyers option symbol
    strike: int
    lot_size: int
    candles: List[Candle] = field(default_factory=list)
    opening_range: Optional[OpeningRange] = None
    current_trade: Optional[TradeState] = None
    trade_history: List[TradeState] = field(default_factory=list)
    # Entry state machine: WAITING → BREAKOUT → (confirmation+RSI) → IN_TRADE → WAITING
    entry_state: str = "WAITING"
    breakdown_level: Optional[float] = None  # low of breakdown candle


@dataclass
class OptionsRuntime:
    config: InstrumentConfig
    futures_symbol: str         # For ATM calculation
    ce_leg: Optional[OptionLeg] = None
    pe_leg: Optional[OptionLeg] = None
    last_processed_bucket: Optional[datetime] = None


# ============================================================================
# HELPERS
# ============================================================================
def now_ist() -> datetime:
    return datetime.now(IST)


def epoch_to_ist(ts) -> datetime:
    if ts > 1e12:
        ts = ts / 1000.0
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(IST)


def time_past(hh: int, mm: int) -> bool:
    n = now_ist()
    return (n.hour, n.minute) >= (hh, mm)


def calc_rsi(candles: List[Candle], period: int = 14, smoothing_length: int = 14) -> Optional[float]:
    """
    Raw RSI calculation matching broker/TradingView RSI when smoothing line is OFF.

    Broker settings now used:
      - RSI Length = 14
      - Smoothing Line = OFF / not used

    Important:
      This returns only the main raw RSI line.
      The SMA smoothing line is intentionally NOT used in strategy signals.
    """
    if len(candles) < period + 1:
        return None

    closes = [float(c.close) for c in candles]

    gains: List[float] = []
    losses: List[float] = []

    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    if len(gains) < period:
        return None

    # First RSI seed uses simple average of the first `period` gains/losses.
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder/RMA smoothing for the remaining candles.
    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return float(rsi)

def calc_atm_strike(price: float, step: int) -> int:
    return int(round(price / step) * step)


def get_monthly_expiry_month() -> Tuple[int, int]:
    today = now_ist().date()
    if today.day >= 20:
        if today.month == 12:
            return (today.year + 1, 1)
        return (today.year, today.month + 1)
    return (today.year, today.month)


# ============================================================================
# FYERS BROKER WRAPPER
# ============================================================================
class FyersBroker:
    def __init__(self, access_token: Optional[str] = None) -> None:
        raw_token = access_token or auto_login()
        if not raw_token:
            raise RuntimeError("Login failed — no access token returned")
        cid = fyers_connect.CLIENT_ID or f"{fyers_connect.APP_ID}-{fyers_connect.APP_TYPE}"
        self.access_token = raw_token if ":" in raw_token else f"{cid}:{raw_token}"
        self.token_only = self.access_token.split(":", 1)[1]
        self.fyers = fyersModel.FyersModel(
            token=self.token_only, is_async=False,
            client_id=cid, log_path="",
        )

    def history(self, symbol: str, resolution: str,
                range_from: int, range_to: int) -> List[Candle]:
        payload = {
            "symbol": symbol, "resolution": resolution, "date_format": "0",
            "range_from": str(range_from), "range_to": str(range_to), "cont_flag": "1",
        }
        resp = self.fyers.history(payload)
        candles = []
        for row in resp.get("candles", []) or []:
            if len(row) < 6:
                continue
            candles.append(Candle(
                ts=epoch_to_ist(row[0]), open=float(row[1]), high=float(row[2]),
                low=float(row[3]), close=float(row[4]), volume=float(row[5]),
            ))
        return candles

    def quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        payload = {"symbols": ",".join(symbols)}
        resp = self.fyers.quotes(payload)
        out = {}
        for item in resp.get("d", []) or []:
            key = item.get("n") or item.get("symbol")
            v = item.get("v", {}) if isinstance(item.get("v"), dict) else {}
            if key:
                out[key] = {
                    "ltp": float(v.get("lp", v.get("ltp", 0)) or 0),
                    "bid": float(v.get("bid_price", v.get("bid", 0)) or 0),
                    "ask": float(v.get("ask_price", v.get("ask", 0)) or 0),
                }
        return out

    def place_market_order(self, symbol: str, side: int, qty: int,
                           product_type: str = "INTRADAY") -> Dict[str, Any]:
        payload = {
            "symbol": symbol, "qty": qty, "type": 2, "side": side,
            "productType": product_type, "limitPrice": 0, "stopPrice": 0,
            "validity": "DAY", "disclosedQty": 0, "offlineOrder": False,
        }
        return self.fyers.place_order(payload)


# ============================================================================
# WEBSOCKET LTP FEED
# ============================================================================
class LiveLTPFeed:
    """WebSocket-based live LTP feed with tick-by-tick exit callback.
    Includes: stale LTP detection, auto-reconnect watchdog, never gives up."""

    def __init__(self, access_token: str, on_tick=None) -> None:
        self.access_token = access_token
        self.symbols: List[str] = []
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._on_tick_callback = on_tick
        self._running = False
        self._last_any_tick = time.time()

    def subscribe(self, symbols: List[str]) -> None:
        self.symbols = list(set(symbols))
        if not self.symbols:
            return
        self._running = True
        print(f"[WS] Subscribing to {len(self.symbols)} symbols for live LTP + tick exits")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._watchdog_thread = threading.Thread(target=self._watchdog, daemon=True)
        self._watchdog_thread.start()

    def _on_message(self, message: Any) -> None:
        try:
            if isinstance(message, dict):
                sym = message.get("symbol") or message.get("sym")
                ltp = message.get("ltp") or message.get("lp")
                if sym and ltp:
                    ltp_f = float(ltp)
                    _live_ltp[sym] = ltp_f
                    _live_ltp_time[sym] = time.time()
                    self._last_any_tick = time.time()
                    if self._on_tick_callback:
                        self._on_tick_callback(sym, ltp_f)
            elif isinstance(message, list):
                for item in message:
                    if isinstance(item, dict):
                        sym = item.get("symbol") or item.get("sym")
                        ltp = item.get("ltp") or item.get("lp")
                        if sym and ltp:
                            ltp_f = float(ltp)
                            _live_ltp[sym] = ltp_f
                            _live_ltp_time[sym] = time.time()
                            self._last_any_tick = time.time()
                            if self._on_tick_callback:
                                self._on_tick_callback(sym, ltp_f)
        except Exception:
            pass

    def _on_connect(self) -> None:
        print(f"[WS] Connected. Subscribing {len(self.symbols)} symbols...")
        data_type = "SymbolUpdate"
        self._ws.subscribe(symbols=self.symbols, data_type=data_type)
        self._ws.keep_running()

    def _on_error(self, msg: Any) -> None:
        print(f"[WS ERROR] {msg}")

    def _on_close(self, msg: Any) -> None:
        print(f"[WS] Closed: {msg}")

    def _run(self) -> None:
        try:
            self._ws = data_ws.FyersDataSocket(
                access_token=self.access_token,
                log_path="",
                litemode=True,
                write_to_file=False,
                reconnect=True,
                on_connect=self._on_connect,
                on_close=self._on_close,
                on_error=self._on_error,
                on_message=self._on_message,
            )
            self._ws.connect()
        except Exception as e:
            print(f"[WS ERROR] Failed to start: {e}")

    def _watchdog(self) -> None:
        """Monitor WebSocket health. If no ticks for 30s, force full reconnect."""
        while self._running:
            time.sleep(10)
            try:
                elapsed = time.time() - self._last_any_tick
                if elapsed > 30 and self._running:
                    print(f"[WS WATCHDOG] No ticks for {elapsed:.0f}s — forcing full reconnect...")
                    self._force_reconnect()
            except Exception as e:
                print(f"[WS WATCHDOG ERROR] {e}")

    def _force_reconnect(self) -> None:
        """Kill existing WS and create a fresh connection."""
        try:
            if self._ws:
                try:
                    self._ws.close_connection()
                except Exception:
                    pass
            self._ws = None
            time.sleep(2)
            print(f"[WS RECONNECT] Creating fresh WebSocket connection...")
            self._ws = data_ws.FyersDataSocket(
                access_token=self.access_token,
                log_path="",
                litemode=True,
                write_to_file=False,
                reconnect=True,
                on_connect=self._on_connect,
                on_close=self._on_close,
                on_error=self._on_error,
                on_message=self._on_message,
            )
            t = threading.Thread(target=self._ws.connect, daemon=True)
            t.start()
            self._last_any_tick = time.time()
        except Exception as e:
            print(f"[WS RECONNECT ERROR] {e}")

    @staticmethod
    def get_ltp(symbol: str) -> Optional[float]:
        return _live_ltp.get(symbol)

    @staticmethod
    def is_stale(symbol: str) -> bool:
        """Check if LTP for a symbol is stale (no tick for STALE_LTP_SECONDS)."""
        last = _live_ltp_time.get(symbol)
        if last is None:
            return True
        return (time.time() - last) > STALE_LTP_SECONDS


# ============================================================================
# INSTRUMENT MASTER
# ============================================================================
class InstrumentMaster:
    FYERS_NSE_FO_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
    FYERS_BSE_FO_URL = "https://public.fyers.in/sym_details/BSE_FO.csv"
    FIELDNAMES = [
        "token", "description", "instrument_type_code", "lot_size", "tick_size",
        "isin", "trading_session", "last_update_date", "expiry_epoch", "symbol",
        "exchange", "segment", "scrip_code", "underlying", "underlying_code",
        "strike", "option_type", "underlying_token", "reserved_1", "reserved_2", "ltp",
    ]

    def __init__(self, refresh_days: int = 1) -> None:
        self.all_rows: List[Dict[str, Any]] = []
        cache_dir = Path.cwd() / "cache" / "fyers"
        cache_dir.mkdir(parents=True, exist_ok=True)

        for url, fname in [(self.FYERS_NSE_FO_URL, "NSE_FO.csv"),
                           (self.FYERS_BSE_FO_URL, "BSE_FO.csv")]:
            path = cache_dir / fname
            self._ensure_csv(path, url, refresh_days)
            if path.exists():
                rows = self._load_csv(path)
                print(f"[MASTER] Loaded {len(rows)} rows from {fname}")
                self.all_rows.extend(rows)

        print(f"[MASTER] Total: {len(self.all_rows)} rows")

    def _ensure_csv(self, path: Path, url: str, refresh_days: int) -> None:
        if path.exists():
            age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
            if age <= timedelta(days=refresh_days):
                return
        try:
            import ssl
            print(f"[MASTER] Downloading {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            ssl_ctx = ssl.create_default_context()
            try:
                import certifi
                ssl_ctx.load_verify_locations(certifi.where())
            except ImportError:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
                data = resp.read()
            if data:
                path.write_bytes(data)
                print(f"[MASTER] Downloaded {len(data)} bytes")
        except Exception as e:
            print(f"[MASTER WARN] Download failed: {e}")

    def _load_csv(self, path: Path) -> List[Dict[str, Any]]:
        rows = []
        with path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
            for raw in csv.reader(f):
                if not raw or len(raw) < 10:
                    continue
                row = {self.FIELDNAMES[i]: raw[i] if i < len(raw) else ""
                       for i in range(len(self.FIELDNAMES))}
                parsed = self._normalize(row)
                if parsed:
                    rows.append(parsed)
        return rows

    def _normalize(self, row: Dict[str, str]) -> Optional[Dict[str, Any]]:
        symbol = row.get("symbol", "").strip()
        raw_type = row.get("instrument_type_code", "").strip()
        opt_type_col = row.get("option_type", "").strip().upper()

        if raw_type in {"11", "13"} or "FUT" in symbol.upper():
            itype = "FUT"
        elif raw_type == "14":
            if opt_type_col in {"CE", "PE"}:
                itype = opt_type_col
            elif "CE" in symbol.upper()[-5:]:
                itype = "CE"
            elif "PE" in symbol.upper()[-5:]:
                itype = "PE"
            else:
                return None
        else:
            return None

        base = self._extract_base(symbol)
        if not base:
            return None

        expiry = None
        ep = row.get("expiry_epoch", "").strip()
        if ep:
            try:
                expiry = datetime.fromtimestamp(int(float(ep)), tz=timezone.utc).date()
            except Exception:
                pass

        strike = None
        sr = row.get("strike", "").strip()
        try:
            strike = float(sr.replace(",", "")) if sr else None
        except Exception:
            pass

        lr = row.get("lot_size", "").strip()
        try:
            lot_size = max(int(float(lr)), 1) if lr else 1
        except Exception:
            lot_size = 1

        return {
            "base": base, "fyers_symbol": symbol, "instrument_type": itype,
            "expiry": expiry, "strike": strike, "lot_size": lot_size,
        }

    @staticmethod
    def _extract_base(symbol: str) -> str:
        s = symbol.split(":")[-1].upper() if ":" in symbol else symbol.upper()
        s = re.sub(r"-EQ$|-INDEX$", "", s)
        s = re.sub(r"\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC).*$", "", s)
        return s.strip()

    def resolve_futures(self, base: str) -> Optional[Dict[str, Any]]:
        today = now_ist().date()
        rows = [r for r in self.all_rows
                if r["base"] == base and r["instrument_type"] == "FUT"
                and r["expiry"] and r["expiry"] >= today]
        if not rows:
            return None
        rows.sort(key=lambda x: x["expiry"])
        return rows[0]

    def resolve_option(self, base: str, strike: int, opt_type: str,
                       target_year: int, target_month: int) -> Optional[Dict[str, Any]]:
        today = now_ist().date()
        candidates = [
            r for r in self.all_rows
            if r["base"] == base and r["instrument_type"] == opt_type
            and r["strike"] is not None and abs(r["strike"] - strike) < 0.01
            and r["expiry"] is not None and r["expiry"] >= today
            and r["expiry"].year == target_year and r["expiry"].month == target_month
        ]
        if not candidates:
            # Fallback: nearest expiry
            fallback = [
                r for r in self.all_rows
                if r["base"] == base and r["instrument_type"] == opt_type
                and r["strike"] is not None and abs(r["strike"] - strike) < 0.01
                and r["expiry"] is not None and r["expiry"] >= today
            ]
            if fallback:
                fallback.sort(key=lambda x: x["expiry"])
                return fallback[0]
            return None
        candidates.sort(key=lambda x: x["expiry"], reverse=True)
        return candidates[0]


# ============================================================================
# MAIN STRATEGY ENGINE
# ============================================================================
class ORBStrategyV2:
    def __init__(self, config: StrategyConfig) -> None:
        self.cfg = config
        self.broker: Optional[FyersBroker] = None
        self.master: Optional[InstrumentMaster] = None
        self.ltp_feed: Optional[LiveLTPFeed] = None
        self.trade_logger = TradeLogger()
        self.futures_runtimes: Dict[str, FuturesRuntime] = {}
        self.options_runtimes: Dict[str, OptionsRuntime] = {}
        self.day_pnl: float = 0.0
        self._stop_event = threading.Event()
        # Lock for thread-safe trade exits (WebSocket callback runs on WS thread)
        self._exit_lock = threading.Lock()

    def stop(self):
        if self.ltp_feed:
            self.ltp_feed._running = False
        self._stop_event.set()

    def _stopped(self) -> bool:
        return self._stop_event.is_set()

    # ── Tick-by-tick exit checker (called from WebSocket thread) ──────────
    def _on_tick(self, symbol: str, ltp: float) -> None:
        """Called on every WebSocket tick. Checks SL/Target/TSL in real-time."""
        with self._exit_lock:
            # Check futures trades
            for name, rt in self.futures_runtimes.items():
                if rt.current_trade and rt.current_trade.is_live and rt.current_trade.symbol == symbol:
                    reason = rt.current_trade.should_exit(ltp)
                    if reason:
                        self._exit_trade(rt.current_trade, ltp, reason)
                        rt.trade_history.append(rt.current_trade)
                        rt.current_trade = None
                        # Reset entry state — need fresh breakout+confirmation for re-entry
                        rt.entry_state = "WAITING"
                        rt.breakout_level = None
                        rt.breakout_direction = None
                        print(f"[TICK EXIT] FUT {name} @ {ltp:.2f}")
                    return

            # Check options trades
            for name, rt in self.options_runtimes.items():
                for leg in [rt.ce_leg, rt.pe_leg]:
                    if leg and leg.current_trade and leg.current_trade.is_live and leg.current_trade.symbol == symbol:
                        reason = leg.current_trade.should_exit(ltp)
                        if reason:
                            self._exit_trade(leg.current_trade, ltp, reason)
                            leg.trade_history.append(leg.current_trade)
                            leg.current_trade = None
                            # Reset entry state — need fresh breakdown+confirmation for re-entry
                            leg.entry_state = "WAITING"
                            leg.breakdown_level = None
                            print(f"[TICK EXIT] OPT {name} {leg.opt_type} @ {ltp:.2f}")
                        return

    # ── Init ───────────────────────────────────────────────────────────────
    def initialize(self) -> bool:
        print("=" * 60)
        print("  ORB STRATEGY V2 — Futures + Options")
        print("  Balfund Trading Pvt. Ltd.")
        print("=" * 60)

        # Login
        print("\n[INIT] Logging in to Fyers...")
        try:
            self.broker = FyersBroker()
        except Exception as e:
            print(f"[INIT ERROR] Login failed: {e}")
            return False
        print("[INIT] ✓ Broker connected")

        # Master
        print("[INIT] Loading instrument master...")
        try:
            self.master = InstrumentMaster(self.cfg.symbol_master_refresh_days)
        except Exception as e:
            print(f"[INIT ERROR] Master: {e}")
            return False

        # Resolve instruments
        enabled = {k: v for k, v in self.cfg.instruments.items()
                   if v.enable_futures or v.enable_options}
        if not enabled:
            print("[INIT ERROR] No instruments enabled")
            return False

        exp_year, exp_month = get_monthly_expiry_month()
        print(f"[INIT] Expiry target: {exp_year}-{exp_month:02d}")
        print(f"[INIT] Timeframe: {self.cfg.timeframe_minutes}m")

        for name, icfg in enabled.items():
            # Resolve futures
            fut_row = self.master.resolve_futures(name)
            if not fut_row:
                print(f"[INIT WARN] {name}: No futures found, skipping")
                continue

            fut_symbol = fut_row["fyers_symbol"]
            fut_lot_size = fut_row["lot_size"]

            # Futures system
            if icfg.enable_futures:
                self.futures_runtimes[name] = FuturesRuntime(
                    config=icfg, futures_symbol=fut_symbol, lot_size=fut_lot_size,
                )
                print(f"[INIT] ✓ {name} FUTURES: {fut_symbol}, lot={fut_lot_size}, lots={icfg.lots_futures}")

            # Options system
            if icfg.enable_options:
                # Get current futures price for ATM calc
                try:
                    q = self.broker.quotes([fut_symbol])
                    fut_ltp = q.get(fut_symbol, {}).get("ltp", 0)
                except Exception:
                    fut_ltp = 0

                if fut_ltp <= 0:
                    print(f"[INIT WARN] {name}: No futures LTP for ATM calc, skipping options")
                    continue

                atm = calc_atm_strike(fut_ltp, icfg.strike_step)
                ce_strike = atm + icfg.otm_offset
                pe_strike = atm - icfg.otm_offset
                print(f"[INIT]   {name} OPTIONS: Futures LTP={fut_ltp:.2f}, ATM={atm}, "
                      f"OTM offset={icfg.otm_offset}, CE strike={ce_strike}, PE strike={pe_strike}")

                ce_row = self.master.resolve_option(name, ce_strike, "CE", exp_year, exp_month)
                pe_row = self.master.resolve_option(name, pe_strike, "PE", exp_year, exp_month)

                ce_leg = None
                pe_leg = None
                if ce_row:
                    ce_leg = OptionLeg("CE", ce_row["fyers_symbol"], ce_strike, ce_row["lot_size"])
                    print(f"[INIT] ✓ {name} CE: {ce_row['fyers_symbol']} strike={ce_strike}")
                if pe_row:
                    pe_leg = OptionLeg("PE", pe_row["fyers_symbol"], pe_strike, pe_row["lot_size"])
                    print(f"[INIT] ✓ {name} PE: {pe_row['fyers_symbol']} strike={pe_strike}")

                if ce_leg or pe_leg:
                    self.options_runtimes[name] = OptionsRuntime(
                        config=icfg, futures_symbol=fut_symbol,
                        ce_leg=ce_leg, pe_leg=pe_leg,
                    )

        if not self.futures_runtimes and not self.options_runtimes:
            print("[INIT ERROR] No systems initialized")
            return False

        mode = "PAPER" if self.cfg.paper_trading else "LIVE"
        print(f"\n[INIT] ✓ Ready. Mode: {mode}")
        print(f"[INIT]   Futures systems: {list(self.futures_runtimes.keys())}")
        print(f"[INIT]   Options systems: {list(self.options_runtimes.keys())}")
        print(f"[INIT]   Futures — Target: ₹{self.cfg.futures_target:.0f}, SL: ₹{self.cfg.futures_sl:.0f}, Trail Gap: ₹{self.cfg.futures_target_trail_gap:.0f}")
        print(f"[INIT]   Options — Target: ₹{self.cfg.options_target:.0f}, SL: ₹{self.cfg.options_sl:.0f}, Trail Gap: ₹{self.cfg.options_target_trail_gap:.0f}")
        print(f"[INIT]   RSI: period={self.cfg.rsi_period}, upper={self.cfg.rsi_upper}, lower={self.cfg.rsi_lower}")
        print(f"[INIT]   Trade log: {self.trade_logger.filepath}")

        # Start WebSocket LTP feed for all symbols
        try:
            ws_symbols = []
            for name, rt in self.futures_runtimes.items():
                ws_symbols.append(rt.futures_symbol)
            for name, rt in self.options_runtimes.items():
                if rt.ce_leg:
                    ws_symbols.append(rt.ce_leg.symbol)
                if rt.pe_leg:
                    ws_symbols.append(rt.pe_leg.symbol)
            if ws_symbols:
                self.ltp_feed = LiveLTPFeed(self.broker.access_token, on_tick=self._on_tick)
                self.ltp_feed.subscribe(ws_symbols)
                print(f"[INIT] ✓ WebSocket LTP feed started for {len(ws_symbols)} symbols (tick-by-tick exits enabled)")
        except Exception as e:
            print(f"[INIT WARN] WebSocket failed: {e}")

        return True

    # ── Main Loop ──────────────────────────────────────────────────────────
    def run(self) -> None:
        print("\n[STRATEGY] Entering main loop...")
        tf = self.cfg.timeframe_minutes
        entry_h, entry_m = self.cfg.entry_start_time
        print(f"[WAIT] Waiting for first {tf}m candle close ({entry_h:02d}:{entry_m:02d})...")

        # Wait for entry start
        while not self._stopped():
            n = now_ist()
            if (n.hour, n.minute) >= (entry_h, entry_m):
                break
            time.sleep(1)

        if self._stopped():
            return

        # Fetch opening range candles + history for RSI warmup
        self._fetch_opening_ranges()

        # Set last_processed_bucket to current so we don't reprocess loaded candles
        n = now_ist()
        tf = self.cfg.timeframe_minutes
        current_bucket = n.replace(second=0, microsecond=0)
        bucket_min = current_bucket.minute - (current_bucket.minute % tf)
        current_bucket = current_bucket.replace(minute=bucket_min)
        for name, rt in self.futures_runtimes.items():
            rt.last_processed_bucket = current_bucket
        for name, rt in self.options_runtimes.items():
            rt.last_processed_bucket = current_bucket

        print(f"\n[STRATEGY] ✓ Monitoring started. Next candle update at next {tf}m boundary.\n")

        # Main loop
        while not self._stopped():
            n = now_ist()
            if (n.hour, n.minute) >= tuple(self.cfg.force_exit_time):
                self._force_exit_all("TIME_EXIT_15:29")
                break

            self._process_all()
            time.sleep(self.cfg.poll_interval_seconds)

        print("\n[STRATEGY] Loop ended.")
        # Force exit any open trades (handles Stop button + safety net after time exit)
        has_open = any(
            (rt.current_trade and rt.current_trade.is_live)
            for rt in list(self.futures_runtimes.values()) + [
                leg for rt in self.options_runtimes.values()
                for leg in [rt.ce_leg, rt.pe_leg] if leg
            ]
        )
        if has_open:
            self._force_exit_all("STOP_BUTTON")
        self._print_summary()

    # ── Opening Range + Historical Candles for RSI ──────────────────────────
    def _fetch_opening_ranges(self) -> None:
        tf = self.cfg.timeframe_minutes
        orb_h, orb_m = self.cfg.orb_candle_start
        today_orb_start = now_ist().replace(hour=orb_h, minute=orb_m, second=0, microsecond=0)
        now = now_ist()

        # Load from 5 days ago for RSI warmup (handles weekends + holidays + Fyers API quirks)
        prev_start = (today_orb_start - timedelta(days=5)).replace(hour=orb_h, minute=orb_m)

        print(f"\n[HISTORY] Loading candles from {prev_start.strftime('%Y-%m-%d %H:%M')} "
              f"to {now.strftime('%H:%M')} for RSI warmup (prev day + today)...")

        # Futures ORB + history
        for name, rt in self.futures_runtimes.items():
            try:
                candles = self.broker.history(rt.futures_symbol, str(tf),
                                              int(prev_start.timestamp()), int(now.timestamp()))
                if candles:
                    # Find today's first candle for ORB
                    today_candles = [c for c in candles if c.ts.date() == today_orb_start.date()]
                    if today_candles:
                        orb_candle = today_candles[0]
                    else:
                        # Market hasn't opened yet or no data — use last candle as fallback
                        orb_candle = candles[-1]
                    rt.opening_range = OpeningRange(high=orb_candle.high, low=orb_candle.low)
                    rt.candles = candles
                    rsi = calc_rsi(rt.candles, self.cfg.rsi_period)
                    rsi_str = f"{rsi:.1f}" if rsi else "need more candles"
                    prev_count = len(candles) - len(today_candles) if today_candles else len(candles)
                    today_count = len(today_candles) if today_candles else 0
                    print(f"[ORB FUT] {name}: High={orb_candle.high:.2f} Low={orb_candle.low:.2f} "
                          f"| {len(candles)} candles ({prev_count} prev + {today_count} today) | RSI={rsi_str}")
                else:
                    print(f"[ORB FUT WARN] {name}: No candle data")
            except Exception as e:
                print(f"[ORB FUT ERROR] {name}: {e}")

        # Options ORB + history
        for name, rt in self.options_runtimes.items():
            for leg in [rt.ce_leg, rt.pe_leg]:
                if not leg:
                    continue
                try:
                    candles = self.broker.history(leg.symbol, str(tf),
                                                  int(prev_start.timestamp()), int(now.timestamp()))
                    if candles:
                        today_candles = [c for c in candles if c.ts.date() == today_orb_start.date()]
                        if today_candles:
                            orb_candle = today_candles[0]
                        else:
                            orb_candle = candles[-1]
                        leg.opening_range = OpeningRange(high=orb_candle.high, low=orb_candle.low)
                        leg.candles = candles
                        rsi = calc_rsi(leg.candles, self.cfg.rsi_period)
                        rsi_str = f"{rsi:.1f}" if rsi else "need more candles"
                        prev_count = len(candles) - len(today_candles) if today_candles else len(candles)
                        today_count = len(today_candles) if today_candles else 0
                        print(f"[ORB OPT] {name} {leg.opt_type}: High={orb_candle.high:.2f} Low={orb_candle.low:.2f} "
                              f"| {len(candles)} candles ({prev_count} prev + {today_count} today) | RSI={rsi_str}")
                    else:
                        print(f"[ORB OPT WARN] {name} {leg.opt_type}: No candle data")
                except Exception as e:
                    print(f"[ORB OPT ERROR] {name} {leg.opt_type}: {e}")

    # ── Process All ────────────────────────────────────────────────────────
    def _process_all(self) -> None:
        n = now_ist()
        tf = self.cfg.timeframe_minutes
        # Align to timeframe buckets
        current_bucket = n.replace(second=0, microsecond=0)
        bucket_min = current_bucket.minute - (current_bucket.minute % tf)
        current_bucket = current_bucket.replace(minute=bucket_min)
        prev_bucket = current_bucket - timedelta(minutes=tf)

        # Check if this is a new bucket (avoid duplicate processing)
        any_new = False

        # Futures
        for name, rt in self.futures_runtimes.items():
            if rt.last_processed_bucket == current_bucket:
                continue

            # Retry ORB fetch if opening_range is still None
            if rt.opening_range is None:
                try:
                    orb_h, orb_m = self.cfg.orb_candle_start
                    today_orb = now_ist().replace(hour=orb_h, minute=orb_m, second=0, microsecond=0)
                    # Use 5-day lookback for RSI warmup, same as initial load
                    prev_start = (today_orb - timedelta(days=5)).replace(hour=orb_h, minute=orb_m)
                    candles = self.broker.history(rt.futures_symbol, str(tf),
                                                  int(prev_start.timestamp()), int(n.timestamp()))
                    if candles:
                        today_candles = [c for c in candles if c.ts.date() == today_orb.date()]
                        if today_candles:
                            orb_candle = today_candles[0]
                        else:
                            orb_candle = candles[-1]
                        rt.opening_range = OpeningRange(high=orb_candle.high, low=orb_candle.low)
                        rt.candles = candles
                        rsi = calc_rsi(rt.candles, self.cfg.rsi_period)
                        rsi_str = f"{rsi:.1f}" if rsi else "need more candles"
                        prev_count = len(candles) - len(today_candles) if today_candles else len(candles)
                        today_count = len(today_candles) if today_candles else 0
                        print(f"[ORB RETRY ✓] {name} FUT: High={orb_candle.high:.2f} Low={orb_candle.low:.2f} "
                              f"| {len(candles)} candles ({prev_count} prev + {today_count} today) | RSI={rsi_str}")
                    else:
                        rt.last_processed_bucket = current_bucket
                        continue
                except Exception:
                    rt.last_processed_bucket = current_bucket
                    continue
            if not any_new:
                ts = n.strftime("%H:%M:%S")
                print(f"\n{'─'*60}")
                print(f"  📊 Candle Update @ {ts}  (TF: {tf}m)")
                print(f"{'─'*60}")
                any_new = True
            self._process_futures(name, rt, prev_bucket)
            rt.last_processed_bucket = current_bucket

        # Options
        for name, rt in self.options_runtimes.items():
            if rt.last_processed_bucket == current_bucket:
                continue
            if not any_new:
                ts = n.strftime("%H:%M:%S")
                print(f"\n{'─'*60}")
                print(f"  📊 Candle Update @ {ts}  (TF: {tf}m)")
                print(f"{'─'*60}")
                any_new = True
            self._process_options(name, rt, prev_bucket)
            rt.last_processed_bucket = current_bucket

    # ── Futures System ─────────────────────────────────────────────────────
    def _process_futures(self, name: str, rt: FuturesRuntime,
                         bucket: datetime) -> None:
        try:
            tf = str(self.cfg.timeframe_minutes)
            candle = self._fetch_candle(rt.futures_symbol, tf, bucket)
            if not candle:
                return

            rt.candles.append(candle)
            rsi = calc_rsi(rt.candles, self.cfg.rsi_period)
            rsi_text = f"{rsi:.1f}" if rsi is not None else "?"

            # If in trade — log status (exits handled by WebSocket tick callback)
            if rt.current_trade and rt.current_trade.is_live:
                ws_ltp = LiveLTPFeed.get_ltp(rt.futures_symbol)
                stale = LiveLTPFeed.is_stale(rt.futures_symbol)

                # If LTP is stale, fetch REST quote as fallback
                if stale and ws_ltp:
                    try:
                        q = self.broker.quotes([rt.futures_symbol])
                        rest_ltp = q.get(rt.futures_symbol, {}).get("ltp", 0)
                        if rest_ltp and rest_ltp > 0:
                            print(f"[STALE LTP] {name} FUT: WS stuck at {ws_ltp:.2f}, "
                                  f"REST={rest_ltp:.2f} — using REST")
                            _live_ltp[rt.futures_symbol] = rest_ltp
                            _live_ltp_time[rt.futures_symbol] = time.time()
                            ws_ltp = rest_ltp
                            with self._exit_lock:
                                reason = rt.current_trade.should_exit(rest_ltp)
                                if reason:
                                    self._exit_trade(rt.current_trade, rest_ltp, reason)
                                    rt.trade_history.append(rt.current_trade)
                                    rt.current_trade = None
                                    rt.entry_state = "WAITING"
                                    rt.breakout_level = None
                                    rt.breakout_direction = None
                                    print(f"[STALE EXIT] FUT {name} @ {rest_ltp:.2f}")
                                    return
                    except Exception as e:
                        print(f"[STALE LTP ERROR] {name} FUT: {e}")

                price = ws_ltp if ws_ltp and ws_ltp > 0 else candle.close
                pnl = rt.current_trade.calc_pnl_per_lot(price)
                side_str = "BUY" if rt.current_trade.side == 1 else "SELL"
                stale_tag = " ⚠️STALE" if stale else ""
                print(f"[FUT TRADE] {name}: {side_str} LTP={price:.2f} "
                      f"RSI={rsi_text} "
                      f"P&L/lot=₹{pnl:.0f} TSL@₹{rt.current_trade.current_sl_per_lot:.0f}{stale_tag}")
                return

            # Check entry window
            n = now_ist()
            if (n.hour, n.minute) > tuple(self.cfg.entry_end_time):
                return

            orb = rt.opening_range
            rsi_str = f"{rsi:.1f}" if rsi else "warming up"
            is_green = candle.close > candle.open
            is_red = candle.close < candle.open

            # ── STATE: WAITING — looking for breakout candle ──
            if rt.entry_state == "WAITING":
                # Bullish: GREEN candle opens below ORB High, closes above ORB High
                if (is_green and candle.open < orb.high and candle.close > orb.high):
                    rt.breakout_direction = "BUY"
                    rt.breakout_level = candle.high
                    rt.entry_state = "BREAKOUT"
                    print(f"[FUT] {name}: C={candle.close:.2f} O={candle.open:.2f} "
                          f"H={orb.high:.2f} L={orb.low:.2f} RSI={rsi_str} "
                          f"[BREAKOUT CANDLE ✓ GREEN above ORB High, confirm>{rt.breakout_level:.2f}]")

                # Bearish: RED candle opens above ORB Low, closes below ORB Low
                elif (is_red and candle.open > orb.low and candle.close < orb.low):
                    rt.breakout_direction = "SELL"
                    rt.breakout_level = candle.low
                    rt.entry_state = "BREAKOUT"
                    print(f"[FUT] {name}: C={candle.close:.2f} O={candle.open:.2f} "
                          f"H={orb.high:.2f} L={orb.low:.2f} RSI={rsi_str} "
                          f"[BREAKDOWN CANDLE ✓ RED below ORB Low, confirm<{rt.breakout_level:.2f}]")

                else:
                    dist_high = candle.close - orb.high
                    dist_low = candle.close - orb.low
                    print(f"[FUT] {name}: C={candle.close:.2f} "
                          f"H={orb.high:.2f}({dist_high:+.2f}) L={orb.low:.2f}({dist_low:+.2f}) "
                          f"RSI={rsi_str} [WAITING]")

            # ── STATE: BREAKOUT — waiting for confirmation candle ──
            elif rt.entry_state == "BREAKOUT":

                if rt.breakout_direction == "BUY":
                    # Invalidation: candle closes below ORB High
                    if candle.close < orb.high:
                        rt.entry_state = "WAITING"
                        rt.breakout_level = None
                        rt.breakout_direction = None
                        print(f"[FUT] {name}: C={candle.close:.2f} < ORB High {orb.high:.2f} "
                              f"RSI={rsi_str} [BREAKOUT RESET — closed below ORB High]")

                    # Confirmation: candle closes above breakout_level
                    elif candle.close > rt.breakout_level:
                        if rsi is not None and rsi > 50:
                            print(f"[FUT CONFIRMED] {name}: C={candle.close:.2f} > "
                                  f"Breakout High {rt.breakout_level:.2f}, RSI={rsi:.1f} > 50")
                            self._enter_futures(name, rt, 1, candle.close)
                            rt.entry_state = "WAITING"
                            rt.breakout_level = None
                            rt.breakout_direction = None
                        else:
                            reason = "RSI not ready" if rsi is None else f"RSI={rsi:.1f}<50"
                            print(f"[FUT] {name}: C={candle.close:.2f} > "
                                  f"Breakout High {rt.breakout_level:.2f} ✓ "
                                  f"RSI={rsi_str} [CONFIRMED but {reason}, waiting...]")

                    # Between ORB High and breakout_level — still waiting
                    else:
                        print(f"[FUT] {name}: C={candle.close:.2f} "
                              f"ORB High={orb.high:.2f} Confirm>{rt.breakout_level:.2f} "
                              f"RSI={rsi_str} [BREAKOUT — waiting confirmation]")

                elif rt.breakout_direction == "SELL":
                    # Invalidation: candle closes above ORB Low
                    if candle.close > orb.low:
                        rt.entry_state = "WAITING"
                        rt.breakout_level = None
                        rt.breakout_direction = None
                        print(f"[FUT] {name}: C={candle.close:.2f} > ORB Low {orb.low:.2f} "
                              f"RSI={rsi_str} [BREAKDOWN RESET — closed above ORB Low]")

                    # Confirmation: candle closes below breakout_level
                    elif candle.close < rt.breakout_level:
                        if rsi is not None and rsi < 50:
                            print(f"[FUT CONFIRMED] {name}: C={candle.close:.2f} < "
                                  f"Breakdown Low {rt.breakout_level:.2f}, RSI={rsi:.1f} < 50")
                            self._enter_futures(name, rt, -1, candle.close)
                            rt.entry_state = "WAITING"
                            rt.breakout_level = None
                            rt.breakout_direction = None
                        else:
                            reason = "RSI not ready" if rsi is None else f"RSI={rsi:.1f}>50"
                            print(f"[FUT] {name}: C={candle.close:.2f} < "
                                  f"Breakdown Low {rt.breakout_level:.2f} ✓ "
                                  f"RSI={rsi_str} [CONFIRMED but {reason}, waiting...]")

                    # Between ORB Low and breakout_level — still waiting
                    else:
                        print(f"[FUT] {name}: C={candle.close:.2f} "
                              f"ORB Low={orb.low:.2f} Confirm<{rt.breakout_level:.2f} "
                              f"RSI={rsi_str} [BREAKDOWN — waiting confirmation]")

        except Exception as e:
            print(f"[FUT ERROR] {name}: {e}")

    def _enter_futures(self, name: str, rt: FuturesRuntime,
                       side: int, price: float) -> None:
        n = now_ist()
        total_qty = rt.lot_size * rt.config.lots_futures
        side_str = "BUY" if side == 1 else "SELL"

        if self.cfg.paper_trading:
            print(f"[FUT PAPER] {name}: {side_str} {rt.futures_symbol} "
                  f"@ {price:.2f} x{total_qty}")
            oid = f"PAPER_FUT_{name}_{n.strftime('%H%M%S')}"
        else:
            resp = self.broker.place_market_order(rt.futures_symbol, side, total_qty)
            oid = resp.get("id") or ""
            if resp.get("s") != "ok":
                print(f"[FUT ORDER ERROR] {name}: {resp}")
                return
            print(f"[FUT LIVE] {name}: {side_str} {rt.futures_symbol} order_id={oid}")

        rt.current_trade = TradeState(
            system="FUTURES", instrument=name, symbol=rt.futures_symbol,
            side=side, entry_price=price, entry_time=n,
            lots=rt.config.lots_futures, lot_size=rt.lot_size,
            target_per_lot=self.cfg.futures_target,
            sl_per_lot=self.cfg.futures_sl,
            target_trail_gap=self.cfg.futures_target_trail_gap,
            order_id=oid,
        )

    # ── Options System ─────────────────────────────────────────────────────
    def _process_options(self, name: str, rt: OptionsRuntime,
                         bucket: datetime) -> None:
        for leg in [rt.ce_leg, rt.pe_leg]:
            if not leg:
                continue

            # Retry ORB fetch if opening_range is still None
            if not leg.opening_range:
                try:
                    tf = self.cfg.timeframe_minutes
                    orb_h, orb_m = self.cfg.orb_candle_start
                    today_orb = now_ist().replace(hour=orb_h, minute=orb_m, second=0, microsecond=0)
                    n = now_ist()
                    # Use 5-day lookback for RSI warmup, same as initial load
                    prev_start = (today_orb - timedelta(days=5)).replace(hour=orb_h, minute=orb_m)
                    candles = self.broker.history(leg.symbol, str(tf),
                                                  int(prev_start.timestamp()), int(n.timestamp()))
                    if candles:
                        today_candles = [c for c in candles if c.ts.date() == today_orb.date()]
                        if today_candles:
                            orb_candle = today_candles[0]
                        else:
                            orb_candle = candles[-1]
                        leg.opening_range = OpeningRange(high=orb_candle.high, low=orb_candle.low)
                        leg.candles = candles
                        rsi = calc_rsi(leg.candles, self.cfg.rsi_period)
                        rsi_str = f"{rsi:.1f}" if rsi else "need more candles"
                        prev_count = len(candles) - len(today_candles) if today_candles else len(candles)
                        today_count = len(today_candles) if today_candles else 0
                        print(f"[ORB RETRY ✓] {name} {leg.opt_type}: High={orb_candle.high:.2f} Low={orb_candle.low:.2f} "
                              f"| {len(candles)} candles ({prev_count} prev + {today_count} today) | RSI={rsi_str}")
                    else:
                        continue
                except Exception:
                    continue
            try:
                tf = str(self.cfg.timeframe_minutes)
                candle = self._fetch_candle(leg.symbol, tf, bucket)
                if not candle:
                    continue

                leg.candles.append(candle)
                rsi = calc_rsi(leg.candles, self.cfg.rsi_period)
                rsi_text = f"{rsi:.1f}" if rsi is not None else "?"

                # If in trade — log status (exits handled by WebSocket tick callback)
                if leg.current_trade and leg.current_trade.is_live:
                    ws_ltp = LiveLTPFeed.get_ltp(leg.symbol)
                    stale = LiveLTPFeed.is_stale(leg.symbol)

                    # If LTP is stale, fetch REST quote as fallback
                    if stale and ws_ltp:
                        try:
                            q = self.broker.quotes([leg.symbol])
                            rest_ltp = q.get(leg.symbol, {}).get("ltp", 0)
                            if rest_ltp and rest_ltp > 0:
                                print(f"[STALE LTP] {name} {leg.opt_type}: WS stuck at {ws_ltp:.2f}, "
                                      f"REST={rest_ltp:.2f} — using REST")
                                _live_ltp[leg.symbol] = rest_ltp
                                _live_ltp_time[leg.symbol] = time.time()
                                ws_ltp = rest_ltp
                                with self._exit_lock:
                                    reason = leg.current_trade.should_exit(rest_ltp)
                                    if reason:
                                        self._exit_trade(leg.current_trade, rest_ltp, reason)
                                        leg.trade_history.append(leg.current_trade)
                                        leg.current_trade = None
                                        leg.entry_state = "WAITING"
                                        leg.breakdown_level = None
                                        print(f"[STALE EXIT] OPT {name} {leg.opt_type} @ {rest_ltp:.2f}")
                                        continue
                        except Exception as e:
                            print(f"[STALE LTP ERROR] {name} {leg.opt_type}: {e}")

                    price = ws_ltp if ws_ltp and ws_ltp > 0 else candle.close
                    pnl = leg.current_trade.calc_pnl_per_lot(price)
                    stale_tag = " ⚠️STALE" if stale else ""
                    print(f"[OPT TRADE] {name} {leg.opt_type}: SELL Prem={price:.2f} "
                          f"RSI={rsi_text} "
                          f"P&L/lot=₹{pnl:.0f} TSL@₹{leg.current_trade.current_sl_per_lot:.0f}{stale_tag}")
                    continue

                # Check entry window
                n = now_ist()
                if (n.hour, n.minute) > tuple(self.cfg.entry_end_time):
                    continue

                orb = leg.opening_range
                rsi_str = f"{rsi:.1f}" if rsi else "warming up"
                is_red = candle.close < candle.open

                # ── STATE: WAITING — looking for breakdown candle ──
                if leg.entry_state == "WAITING":
                    # RED candle opens above ORB Low, closes below ORB Low
                    if is_red and candle.open > orb.low and candle.close < orb.low:
                        leg.breakdown_level = candle.low
                        leg.entry_state = "BREAKOUT"
                        print(f"[OPT] {name} {leg.opt_type}: Prem={candle.close:.2f} O={candle.open:.2f} "
                              f"Low={orb.low:.2f} RSI={rsi_str} "
                              f"[BREAKDOWN CANDLE ✓ RED below ORB Low, confirm<{leg.breakdown_level:.2f}]")
                    else:
                        dist_low = candle.close - orb.low
                        print(f"[OPT] {name} {leg.opt_type}: Prem={candle.close:.2f} "
                              f"Low={orb.low:.2f}({dist_low:+.2f}) RSI={rsi_str} [WAITING]")

                # ── STATE: BREAKOUT — waiting for confirmation ──
                elif leg.entry_state == "BREAKOUT":
                    # Invalidation: candle closes above ORB Low
                    if candle.close > orb.low:
                        leg.entry_state = "WAITING"
                        leg.breakdown_level = None
                        print(f"[OPT] {name} {leg.opt_type}: Prem={candle.close:.2f} > "
                              f"ORB Low {orb.low:.2f} RSI={rsi_str} "
                              f"[BREAKDOWN RESET — closed above ORB Low]")

                    # Confirmation: candle closes below breakdown_level
                    elif candle.close < leg.breakdown_level:
                        if rsi is not None and rsi < 50:
                            print(f"[OPT CONFIRMED] {name} {leg.opt_type}: Prem={candle.close:.2f} < "
                                  f"Breakdown Low {leg.breakdown_level:.2f}, RSI={rsi:.1f} < 50")
                            self._enter_option(name, leg, candle.close)
                            leg.entry_state = "WAITING"
                            leg.breakdown_level = None
                        else:
                            reason = "RSI not ready" if rsi is None else f"RSI={rsi:.1f}>50"
                            print(f"[OPT] {name} {leg.opt_type}: Prem={candle.close:.2f} < "
                                  f"Breakdown Low {leg.breakdown_level:.2f} ✓ "
                                  f"RSI={rsi_str} [CONFIRMED but {reason}, waiting...]")

                    # Between ORB Low and breakdown_level — still waiting
                    else:
                        print(f"[OPT] {name} {leg.opt_type}: Prem={candle.close:.2f} "
                              f"ORB Low={orb.low:.2f} Confirm<{leg.breakdown_level:.2f} "
                              f"RSI={rsi_str} [BREAKDOWN — waiting confirmation]")

            except Exception as e:
                print(f"[OPT ERROR] {name} {leg.opt_type}: {e}")

    def _enter_option(self, name: str, leg: OptionLeg, price: float) -> None:
        n = now_ist()
        total_qty = leg.lot_size * leg.config.lots_options if hasattr(leg, 'config') else leg.lot_size
        # Get lots from the parent config — leg doesn't store it, so use 1 lot default
        # We'll need to pass lots through; for now use lot_size directly
        lots = 1  # Will be overridden by config in GUI

        if self.cfg.paper_trading:
            print(f"[OPT PAPER] {name} {leg.opt_type}: SELL {leg.symbol} "
                  f"@ {price:.2f} x{leg.lot_size}")
            oid = f"PAPER_OPT_{name}_{leg.opt_type}_{n.strftime('%H%M%S')}"
        else:
            resp = self.broker.place_market_order(leg.symbol, -1, leg.lot_size * lots)
            oid = resp.get("id") or ""
            if resp.get("s") != "ok":
                print(f"[OPT ORDER ERROR] {name} {leg.opt_type}: {resp}")
                return
            print(f"[OPT LIVE] {name} {leg.opt_type}: SELL {leg.symbol} order_id={oid}")

        leg.current_trade = TradeState(
            system="OPTIONS", instrument=name, symbol=leg.symbol,
            side=-1, entry_price=price, entry_time=n,
            lots=lots, lot_size=leg.lot_size,
            target_per_lot=self.cfg.options_target,
            sl_per_lot=self.cfg.options_sl,
            target_trail_gap=self.cfg.options_target_trail_gap,
            order_id=oid,
        )

    # ── Exit ───────────────────────────────────────────────────────────────
    def _exit_trade(self, trade: TradeState, exit_price: float, reason: str) -> None:
        trade.exit_price = exit_price
        trade.exit_time = now_ist()
        trade.exit_reason = reason
        trade.is_live = False

        pnl_per_lot = trade.calc_pnl_per_lot(exit_price)
        total_pnl = pnl_per_lot * trade.lots
        self.day_pnl += total_pnl

        side_str = "BUY" if trade.side == 1 else "SELL"
        exit_side = -trade.side

        if not self.cfg.paper_trading:
            try:
                self.broker.place_market_order(trade.symbol, exit_side, trade.total_qty)
            except Exception as e:
                print(f"[EXIT ERROR] {trade.instrument}: {e}")

        tag = "PAPER" if self.cfg.paper_trading else "LIVE"
        print(f"[{tag} EXIT] {trade.system} {trade.instrument}: {trade.symbol} "
              f"Entry={trade.entry_price:.2f} Exit={exit_price:.2f} "
              f"P&L/lot=₹{pnl_per_lot:.0f} Total=₹{total_pnl:.0f} ({reason})")

        # Save to trade log CSV
        if self.trade_logger:
            self.trade_logger.log_trade(trade)

    def _force_exit_all(self, reason: str) -> None:
        print(f"\n[FORCE EXIT] {reason}")
        # Futures
        for name, rt in self.futures_runtimes.items():
            if rt.current_trade and rt.current_trade.is_live:
                try:
                    q = self.broker.quotes([rt.current_trade.symbol])
                    ltp = q.get(rt.current_trade.symbol, {}).get("ltp", rt.current_trade.entry_price)
                except Exception:
                    ltp = rt.current_trade.entry_price
                self._exit_trade(rt.current_trade, ltp, reason)
                rt.trade_history.append(rt.current_trade)
                rt.current_trade = None

        # Options
        for name, rt in self.options_runtimes.items():
            for leg in [rt.ce_leg, rt.pe_leg]:
                if leg and leg.current_trade and leg.current_trade.is_live:
                    try:
                        q = self.broker.quotes([leg.current_trade.symbol])
                        ltp = q.get(leg.current_trade.symbol, {}).get("ltp", leg.current_trade.entry_price)
                    except Exception:
                        ltp = leg.current_trade.entry_price
                    self._exit_trade(leg.current_trade, ltp, reason)
                    leg.trade_history.append(leg.current_trade)
                    leg.current_trade = None

    # ── Data Fetching ──────────────────────────────────────────────────────
    def _fetch_candle(self, symbol: str, tf: str, bucket: datetime) -> Optional[Candle]:
        tf_mins = int(tf)
        start_ts = int(bucket.timestamp())
        end_ts = start_ts + tf_mins * 60
        candles = self.broker.history(symbol, tf, start_ts, end_ts)
        if candles:
            return candles[0]
        return None

    # ── Summary ────────────────────────────────────────────────────────────
    def _print_summary(self) -> None:
        print("\n" + "=" * 60)
        print("  DAY SUMMARY")
        print("=" * 60)

        total_trades = 0
        # Futures
        for name, rt in self.futures_runtimes.items():
            for t in rt.trade_history:
                pnl = t.calc_pnl_per_lot(t.exit_price or t.entry_price) * t.lots
                total_trades += 1
                side_str = "BUY" if t.side == 1 else "SELL"
                print(f"  FUT {name}: {side_str} Entry={t.entry_price:.2f} "
                      f"Exit={t.exit_price:.2f} P&L=₹{pnl:.0f} ({t.exit_reason})")
            if not rt.trade_history:
                if rt.opening_range:
                    print(f"  FUT {name}: No breakout (OR: {rt.opening_range.high:.2f}/{rt.opening_range.low:.2f})")

        # Options
        for name, rt in self.options_runtimes.items():
            for leg in [rt.ce_leg, rt.pe_leg]:
                if not leg:
                    continue
                for t in leg.trade_history:
                    pnl = t.calc_pnl_per_lot(t.exit_price or t.entry_price) * t.lots
                    total_trades += 1
                    print(f"  OPT {name} {leg.opt_type}: SELL Entry={t.entry_price:.2f} "
                          f"Exit={t.exit_price:.2f} P&L=₹{pnl:.0f} ({t.exit_reason})")
                if not leg.trade_history:
                    if leg.opening_range:
                        print(f"  OPT {name} {leg.opt_type}: No breakdown "
                              f"(OR: {leg.opening_range.high:.2f}/{leg.opening_range.low:.2f})")

        print(f"\n  Total Trades: {total_trades}")
        print(f"  Day P&L: ₹{self.day_pnl:.0f}")
        print("=" * 60)

    # ── Live P&L for GUI ──────────────────────────────────────────────────
    def get_live_pnl(self) -> Dict[str, Any]:
        futures_pnl = 0.0
        options_pnl = 0.0
        result = {"day_pnl": self.day_pnl, "futures_pnl": 0.0, "options_pnl": 0.0,
                  "futures": {}, "options": {}}

        # Futures
        for name, rt in self.futures_runtimes.items():
            cmp = LiveLTPFeed.get_ltp(rt.futures_symbol) or 0.0
            lot_size = rt.lot_size
            info = {
                "or_high": rt.opening_range.high if rt.opening_range else None,
                "or_low": rt.opening_range.low if rt.opening_range else None,
                "direction": rt.breakout_direction,
                "in_trade": False,
                "symbol": rt.futures_symbol,
                "entry": None,
                "cmp": cmp,
                "lot_size": lot_size,
                "lots": rt.config.lots_futures,
                "status": "Watching",
                "live_pnl": 0.0,
                "tsl": None,
                "trade_count": len(rt.trade_history),
                "closed_pnl": 0.0,
            }
            # Closed trades P&L
            for t in rt.trade_history:
                p = t.calc_pnl_per_lot(t.exit_price or t.entry_price) * t.lots
                info["closed_pnl"] += p
                futures_pnl += p

            # Current trade
            if rt.current_trade and rt.current_trade.is_live:
                info["in_trade"] = True
                info["entry"] = rt.current_trade.entry_price
                info["direction"] = "BUY" if rt.current_trade.side == 1 else "SELL"
                info["tsl"] = rt.current_trade.current_sl_per_lot
                info["status"] = "IN TRADE"
                if cmp > 0:
                    live = rt.current_trade.calc_pnl_per_lot(cmp) * rt.current_trade.lots
                    info["live_pnl"] = live
                    futures_pnl += live

            result["futures"][name] = info

        # Options
        for name, rt in self.options_runtimes.items():
            for leg in [rt.ce_leg, rt.pe_leg]:
                if not leg:
                    continue
                key = f"{name}_{leg.opt_type}"
                cmp = LiveLTPFeed.get_ltp(leg.symbol) or 0.0
                info = {
                    "instrument": name,
                    "opt_type": leg.opt_type,
                    "or_high": leg.opening_range.high if leg.opening_range else None,
                    "or_low": leg.opening_range.low if leg.opening_range else None,
                    "symbol": leg.symbol,
                    "strike": leg.strike,
                    "in_trade": False,
                    "entry": None,
                    "cmp": cmp,
                    "lot_size": leg.lot_size,
                    "lots": rt.config.lots_options,
                    "status": "Watching",
                    "live_pnl": 0.0,
                    "tsl": None,
                    "trade_count": len(leg.trade_history),
                    "closed_pnl": 0.0,
                }
                for t in leg.trade_history:
                    p = t.calc_pnl_per_lot(t.exit_price or t.entry_price) * t.lots
                    info["closed_pnl"] += p
                    options_pnl += p

                if leg.current_trade and leg.current_trade.is_live:
                    info["in_trade"] = True
                    info["entry"] = leg.current_trade.entry_price
                    info["tsl"] = leg.current_trade.current_sl_per_lot
                    info["status"] = "IN TRADE"
                    if cmp > 0:
                        live = leg.current_trade.calc_pnl_per_lot(cmp) * leg.current_trade.lots
                        info["live_pnl"] = live
                        options_pnl += live

                result["options"][key] = info

        result["futures_pnl"] = futures_pnl
        result["options_pnl"] = options_pnl
        result["day_pnl"] = self.day_pnl + futures_pnl + options_pnl - self.day_pnl  # live
        result["realized_pnl"] = self.day_pnl
        result["unrealized_pnl"] = (futures_pnl + options_pnl) - self.day_pnl

        # Closed trades list for GUI
        closed = []
        for name, rt in self.futures_runtimes.items():
            for t in rt.trade_history:
                pnl = t.calc_pnl_per_lot(t.exit_price or t.entry_price) * t.lots
                closed.append({
                    "time": t.exit_time.strftime("%H:%M:%S") if t.exit_time else "",
                    "instrument": f"{name} FUT",
                    "side": "BUY" if t.side == 1 else "SELL",
                    "entry": f"{t.entry_price:.2f}",
                    "exit": f"{t.exit_price:.2f}" if t.exit_price else "—",
                    "pnl": pnl,
                    "reason": t.exit_reason or "",
                })
        for name, rt in self.options_runtimes.items():
            for leg in [rt.ce_leg, rt.pe_leg]:
                if not leg:
                    continue
                for t in leg.trade_history:
                    pnl = t.calc_pnl_per_lot(t.exit_price or t.entry_price) * t.lots
                    closed.append({
                        "time": t.exit_time.strftime("%H:%M:%S") if t.exit_time else "",
                        "instrument": f"{name} {leg.opt_type}",
                        "side": "BUY" if t.side == 1 else "SELL",
                        "entry": f"{t.entry_price:.2f}",
                        "exit": f"{t.exit_price:.2f}" if t.exit_price else "—",
                        "pnl": pnl,
                        "reason": t.exit_reason or "",
                    })
        result["closed_trades"] = closed
        return result


# ============================================================================
# STANDALONE TEST
# ============================================================================
if __name__ == "__main__":
    cfg = StrategyConfig(
        paper_trading=True,
        timeframe_minutes=5,
        instruments={
            "NIFTY": InstrumentConfig("NIFTY", 50, 200, lots_futures=1, lots_options=1),
            "BANKNIFTY": InstrumentConfig("BANKNIFTY", 100, 300, lots_futures=1, lots_options=1),
        },
    )
    engine = ORBStrategyV2(cfg)
    if engine.initialize():
        engine.run()
