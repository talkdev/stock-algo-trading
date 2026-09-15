"""
execution.py - order execution: PaperBroker (DEFAULT) and UpstoxBroker (real).

PAPER MODE (default)
  * The Upstox API is used for MARKET DATA only. Orders are simulated locally
    and fills are booked at: bar close + slippage (entries), stop price (or
    the open, on a gap-through) for stops, target price (or the open, on a
    gap-through) for targets.
  * Charges model intraday MIS approximations (see config + README):
    STT 0.025% both sides, NSE txn 0.019%, SEBI 0.0001%, flat brokerage 20
    (capped 0.03%), GST 18% on charges, stamp 0.015% on buys, 5 bps
    slippage/side. Good enough for strategy validation - not for tax.

REAL MODE (opt-in: python main.py --mode real --i-understand-real-risk)
  * Market orders via Upstox REST; fills are read back from order status.
  * After a BUY we place a protective SL-M order for the initial stop
    (Upstox has NO GTT / bracket orders - see upstox_client.py, limitation
    #2/#3). Trailing is engine-side; the protective order is cancelled on
    exit. A crash between BUY and SL placement is a known residual risk.
"""
from __future__ import annotations

import config
import db


def compute_fees(side: str, price: float, qty: int) -> dict:
    c = config
    value = price * qty
    brokerage = min(c.BROKERAGE_FLAT, value * c.BROKERAGE_CAP_PCT / 100.0)
    stt = value * c.STT_BPS / 10000.0
    exchange = value * c.EXCHANGE_BPS / 10000.0
    sebi = value * c.SEBI_BPS / 10000.0
    stamp = value * c.STAMP_BPS_BUY / 10000.0 if side == "BUY" else 0.0
    gst = (brokerage + exchange + sebi) * c.GST_PCT / 100.0
    total = brokerage + stt + exchange + sebi + stamp + gst
    return {"brokerage": brokerage, "stt": stt, "exchange": exchange,
            "sebi": sebi, "stamp": stamp, "gst": gst, "total": total}


def slip(price: float, side: str) -> float:
    """Apply slippage: buys fill slightly higher, sells slightly lower."""
    f = config.SLIPPAGE_BPS / 10000.0
    return price * (1 + f) if side == "BUY" else price * (1 - f)


# ---------------------------------------------------------------------------
class PaperBroker:
    name = "paper"

    def __init__(self, conn):
        self.conn = conn
        if db.get_meta(conn, "paper_start_equity") is None:
            db.set_meta(conn, "paper_start_equity", str(config.PAPER_START_EQUITY))
            db.set_meta(conn, "paper_cash", str(config.PAPER_START_EQUITY))
            conn.commit()

    # -- account --
    def cash(self) -> float:
        return float(db.get_meta(self.conn, "paper_cash", "0"))

    def _set_cash(self, v: float) -> None:
        db.set_meta(self.conn, "paper_cash", repr(v))

    def last_prices(self, symbols) -> dict:
        """Best known price per symbol: live_5m first, else last stored bar."""
        out: dict[str, float] = {}
        for s in symbols:
            r = self.conn.execute("SELECT close FROM live_5m WHERE symbol=?", (s,)).fetchone()
            if r and r["close"]:
                out[s] = float(r["close"])
                continue
            r = self.conn.execute(
                "SELECT close FROM candles_5m WHERE symbol=? ORDER BY bar_time DESC LIMIT 1",
                (s,)).fetchone()
            if r:
                out[s] = float(r["close"])
        return out

    def equity(self, last_prices: dict) -> tuple[float, float]:
        c = self.cash()
        mv = 0.0
        for p in db.get_open_positions(self.conn):
            px = last_prices.get(p["symbol"], p["entry_fill"]) or p["entry_fill"]
            mv += p["qty"] * px
        return c, c + mv

    # -- orders --
    def buy(self, symbol, price, qty, t, stop, target, atr_v, detail) -> int | None:
        fill = slip(price, "BUY")
        fees = compute_fees("BUY", fill, qty)
        c = self.cash()
        if fill * qty + fees["total"] > c:
            return None
        self._set_cash(c - (fill * qty + fees["total"]))
        pid = db.open_position(self.conn, symbol, "paper", qty, fill, t,
                               stop, target, atr_v, fees["total"], detail, None)
        self.conn.commit()
        return pid

    def sell(self, pid, price, t, reason, detail) -> float | None:
        p = db.get_position(self.conn, pid)
        if not p or p["exit_time"]:
            return None
        fill = slip(price, "SELL")
        fees = compute_fees("SELL", fill, p["qty"])
        self._set_cash(self.cash() + (fill * p["qty"] - fees["total"]))
        db.close_position(self.conn, pid, fill, t, reason, fees["total"], detail, None)
        self.conn.commit()
        return (fill - p["entry_fill"]) * p["qty"] - p["entry_fees"] - fees["total"]


# ---------------------------------------------------------------------------
class UpstoxBroker:
    name = "real"

    def __init__(self, conn, client):
        self.conn = conn
        self.client = client
        self.keys: dict = {}

    def set_keys(self, mapping: dict) -> None:
        self.keys = mapping

    def cash(self) -> float:
        return 0.0  # real funds live at the broker, not in this DB

    def equity(self, last_prices: dict) -> tuple[float, float]:
        return 0.0, config.REAL_CAPITAL

    def last_prices(self, symbols) -> dict:
        return {}

    def _sizing_equity(self) -> tuple[float, float]:
        # Upstox v2 has no simple 'available funds' REST endpoint we rely on;
        # sizing uses REAL_CAPITAL (env STMR_REDUCED_CAPITAL? no: STMR_REAL_CAPITAL).
        return config.REAL_CAPITAL, config.REAL_CAPITAL

    def buy(self, symbol, price, qty, t, stop, target, atr_v, detail) -> int | None:
        key = self.keys.get(symbol)
        if not key or qty < 1:
            return None
        oid = self.client.place_order(key, qty, "BUY")
        d = self.client.wait_fill(oid)
        fill = float(d.get("average_trade_price") or price)
        fees = compute_fees("BUY", fill, qty)
        pid = db.open_position(self.conn, symbol, "real", qty, fill, t,
                               stop, target, atr_v, fees["total"], detail, oid)
        try:  # protective broker-side stop (limitation #3: no bracket orders)
            sl_oid = self.client.place_order(key, qty, "SELL",
                                             order_type="SL-M", trigger_price=stop)
            db.update_position(self.conn, pid, sl_order=sl_oid)
        except Exception as e:
            print(f"[warn] protective SL order failed for {symbol}: {e}")
        self.conn.commit()
        return pid

    def sell(self, pid, price, t, reason, detail) -> float | None:
        p = db.get_position(self.conn, pid)
        if not p or p["exit_time"]:
            return None
        key = self.keys.get(p["symbol"])
        if not key:
            return None
        oid = self.client.place_order(key, p["qty"], "SELL")
        d = self.client.wait_fill(oid)
        fill = float(d.get("average_trade_price") or price)
        fees = compute_fees("SELL", fill, p["qty"])
        if p["sl_order"]:
            try:
                self.client.cancel_order(p["sl_order"])
            except Exception:
                pass
        db.close_position(self.conn, pid, fill, t, reason, fees["total"], detail, oid)
        self.conn.commit()
        return (fill - p["entry_fill"]) * p["qty"] - p["entry_fees"] - fees["total"]
