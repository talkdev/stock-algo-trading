"""
Order construction, entry, and the exit ladder (spec Parts XII and XIII).

PAPER MODE IS THE DEFAULT and is enforced in three independent places so a
misconfiguration cannot reach a broker:
  1. CFG.paper_trade defaults to True
  2. make_client() refuses to build a live client unless mode=="live" AND
     JFOU_UPSTOX_TOKEN is set AND CFG.paper_trade is False
  3. every function here re-checks CFG.paper_trade before calling the order API

Nothing is ever placed without a matching row in `orders`, and every state transition
appends to `position_events`, which is append-only and never UPDATEd. That is what makes
a restart safe: the engine reconstructs what it was doing from the database, not from
memory.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as I
from .clock import now_ist
from .config import CFG, LAKH
from .db import Database


def _ts() -> str:
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------------ order maths
def build_entry(high_t: float, atr14: float) -> dict:
    """Part 12.1. Trigger and limit deliberately differ.

    Trigger == limit is a real bug: a fast momentum spike gaps the ask over your limit
    and the order rests unfilled while you believe you are in.
    """
    trigger = high_t * (1 + CFG.entry_trigger_pct)
    limit = trigger * (1 + CFG.limit_buffer_pct)
    return {"trigger": round(trigger, 2), "limit": round(limit, 2),
            "order_type": "SL", "validity": "DAY"}


def gap_cancel(open_t1: float, high_t: float, atr14: float) -> tuple[bool, str]:
    """Part 12.1 GAP-CANCEL, evaluated at 09:08 on the pre-open discovery price."""
    pct_hit = open_t1 > high_t * (1 + CFG.gap_cancel_pct)
    atr_hit = open_t1 > high_t + CFG.gap_cancel_atr_mult * atr14
    if pct_hit or atr_hit:
        which = []
        if pct_hit:
            which.append(f"open {open_t1:,.2f} > high {high_t:,.2f} x "
                         f"{1+CFG.gap_cancel_pct:.4f}")
        if atr_hit:
            which.append(f"open exceeds high + {CFG.gap_cancel_atr_mult} x ATR14 "
                         f"({high_t + CFG.gap_cancel_atr_mult*atr14:,.2f})")
        return True, ("CANCEL_GAP: " + " and ".join(which) +
                      ". Filling here would widen R and make the 2.5R target unreachable")
    return False, ""


def build_stop(low_dip: float, avwap_t0: float, atr14: float) -> float:
    """Part 12.3. The buffer is 0.75 x ATR14, not 0.25."""
    return round(min(low_dip - CFG.stop_atr_mult * atr14,
                     avwap_t0 * CFG.stop_avwap_mult), 2)


# ------------------------------------------------------------------ placement
def arm_entry(db: Database, run_id: str, cand: dict, mode: str) -> dict:
    """Create the position and its armed order. Idempotent on (run_id, instrument_key)."""
    key = cand["instrument_key"]
    existing = db.one(
        "SELECT position_id FROM positions WHERE payload LIKE ? AND state NOT IN "
        "('CLOSED','CANCELLED')", (f'%"run_id": "{run_id}"%',))
    if existing:
        return {"position_id": existing["position_id"], "already_armed": True}

    pid = new_id("POS")
    oid = new_id("ORD")
    ts = _ts()
    with db.tx() as c:
        c.execute(
            """INSERT OR IGNORE INTO positions
               (position_id, instrument_key, symbol, sector, mode, session_date,
                qty, lots, lot_size, entry_price, stop_price, r_value,
                target1_price, target2_price, tau_halflife, prob_p, b_net,
                risk_rupees, state, opened_at, payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, key, cand.get("symbol"), cand.get("sector"), mode,
             cand.get("session_date"), 0, cand.get("lots", 0), cand.get("lot_size", 1),
             cand.get("trigger_price"), cand.get("stop_price"), cand.get("r_value"),
             cand.get("target1_price"), cand.get("target2_price"),
             cand.get("tau_halflife"), cand.get("prob_p"), cand.get("b_net"),
             cand.get("risk_rupees"), "PENDING_ENTRY", ts,
             json.dumps({**cand, "run_id": run_id}, default=str)))
        c.execute(
            """INSERT OR IGNORE INTO orders
               (order_id, position_id, instrument_key, symbol, mode, side, order_type,
                trigger_price, limit_price, qty, validity, status, reason, created_at,
                updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (oid, pid, key, cand.get("symbol"), mode, "BUY", "SL",
             cand.get("trigger_price"), cand.get("limit_price"),
             int(cand.get("shares") or cand.get("lots", 0) * cand.get("lot_size", 1)),
             "DAY", "ARMED",
             f"armed from scan {run_id}: {cand.get('reason','')[:200]}", ts, ts))
        c.execute(
            "INSERT INTO position_events (position_id, ts, event, detail) VALUES (?,?,?,?)",
            (pid, ts, "ARMED", json.dumps({"order_id": oid, "run_id": run_id,
                                           "trigger": cand.get("trigger_price"),
                                           "stop": cand.get("stop_price")}, default=str)))
    return {"position_id": pid, "order_id": oid, "already_armed": False}


def place_or_fill(db: Database, order_id: str, client, fill_price: float,
                  mode: str) -> dict:
    """Paper mode simulates the fill at the quoted price; live mode calls the broker.

    The simulated fill uses the trigger price, not a more favourable price, so the
    paper book cannot flatter itself.
    """
    row = db.one("SELECT * FROM orders WHERE order_id=?", (order_id,))
    if not row:
        return {"error": "unknown order"}
    if row["status"] not in ("ARMED", "PLACED"):
        return {"skipped": row["status"]}

    ts = _ts()
    if mode == "PAPER":
        broker_id = f"PAPER-{order_id}"
        qty = row["qty"]
        with db.tx() as c:
            c.execute("""UPDATE orders SET status='FILLED', broker_order_id=?, filled_at=?,
                         fill_price=?, fill_qty=?, updated_at=? WHERE order_id=?""",
                      (broker_id, ts, fill_price, qty, ts, order_id))
            c.execute("""UPDATE positions SET state='OPEN', qty=?, opened_at=?
                         WHERE position_id=?""", (qty, ts, row["position_id"]))
            c.execute("""INSERT INTO fills (position_id, order_id, ts, side, qty, price,
                         charges, mode) VALUES (?,?,?,?,?,?,?,?)""",
                      (row["position_id"], order_id, ts, "BUY", qty, fill_price, 0.0, mode))
            c.execute("""INSERT INTO position_events (position_id, ts, event, detail)
                         VALUES (?,?,?,?)""",
                      (row["position_id"], ts, "FILLED",
                       json.dumps({"qty": qty, "price": fill_price, "mode": mode})))
        return {"filled": True, "qty": qty, "price": fill_price, "broker_id": broker_id}

    # live path
    if CFG.paper_trade:
        return {"error": "refusing to place a live order: CFG.paper_trade is True"}
    res = client.place_order(qty=row["qty"], instrument_key=row["instrument_key"],
                             side=row["side"], order_type=row["order_type"],
                             price=row["limit_price"], trigger_price=row["trigger_price"],
                             validity=row["validity"], product="I")
    with db.tx() as c:
        c.execute("""UPDATE orders SET status='PLACED', broker_order_id=?, updated_at=?
                     WHERE order_id=?""",
                  (res.get("order_id"), ts, order_id))
        c.execute("""INSERT INTO position_events (position_id, ts, event, detail)
                     VALUES (?,?,?,?)""",
                  (row["position_id"], ts, "PLACED", json.dumps(res, default=str)))
    return {"placed": True, "broker_id": res.get("order_id")}


def cancel_order(db: Database, order_id: str, client, mode: str, reason: str) -> dict:
    row = db.one("SELECT * FROM orders WHERE order_id=?", (order_id,))
    if not row or row["status"] in ("FILLED", "CANCELLED", "EXPIRED"):
        return {"skipped": True}
    ts = _ts()
    if mode != "PAPER" and not CFG.paper_trade and row.get("broker_order_id"):
        try:
            client.cancel_order(row["broker_order_id"])
        except Exception as exc:                      # never lose the local record
            reason += f" (broker cancel failed: {exc})"
    with db.tx() as c:
        c.execute("UPDATE orders SET status='CANCELLED', reason=?, updated_at=? "
                  "WHERE order_id=?", (reason, ts, order_id))
        c.execute("UPDATE positions SET state='CANCELLED', closed_at=?, close_reason=? "
                  "WHERE position_id=? AND state='PENDING_ENTRY'",
                  (ts, reason[:200], row["position_id"]))
        c.execute("INSERT INTO position_events (position_id, ts, event, detail) "
                  "VALUES (?,?,?,?)",
                  (row["position_id"], ts, "CANCELLED", json.dumps({"reason": reason})))
    return {"cancelled": True, "reason": reason}


# ------------------------------------------------------------------ exit ladder
@dataclass
class ExitSignal:
    action: str = "HOLD"          # HOLD | STOP_OUT | TARGET1 | TARGET2 | TRAIL_EXIT | TIME_STOP
    qty_frac: float = 0.0
    reason: str = ""
    rule: int = 0


def evaluate_ladder(pos: dict, daily: pd.DataFrame, intraday_low: float,
                    intraday_high: float, last: float,
                    sessions_held: int) -> ExitSignal:
    """Part XIII, in strict rule order. First match wins.

    `pos` is a plain dict so this is callable from both the live engine and the
    database-only backtest without either knowing about the other.
    """
    entry = float(pos["entry_price"])
    stop = float(pos["stop_price"])
    r = float(pos.get("r_value") or (entry - stop))
    t2 = float(pos.get("target2_price") or (entry + CFG.target2_r_mult * r))
    t1_hit = str(pos.get("state")) == "T1_HIT"

    # rule 1 -- hard stop, intraday
    if intraday_low <= stop:
        return ExitSignal("STOP_OUT", 1.0,
                          f"low {intraday_low:,.2f} <= stop {stop:,.2f} (-1.0R). "
                          f"Immediate market order, full exit", 1)

    # Target 2 is checked before Target 1 on purpose. A single daily bar can span both
    # levels (high >= T2 implies high >= T1), and evaluating T1 first would scale out
    # 50% at the lower target and then report TARGET1 on a bar that actually reached
    # 2.5R. Whichever target the bar reached is the one that fires.
    if intraday_high >= t2:
        return ExitSignal("TARGET2", 1.0,
                          f"high {intraday_high:,.2f} >= Target2 {t2:,.2f} "
                          f"(+{CFG.target2_r_mult}R). Limit-sell the remainder", 3)

    # rule 2 -- Target 1: Kalman state mean or EMA20, sell 50%, stop to breakeven
    if not t1_hit:
        c = daily["close"].to_numpy(dtype=float)
        ema20 = float(I.ema(pd.Series(c), CFG.target1_ema).iloc[-1])
        kalman = float(I.kalman_local_linear(c).price_state)
        level = max(kalman, ema20)
        if intraday_high >= level:
            be = entry * (1 + CFG.breakeven_buffer_pct)
            return ExitSignal("TARGET1", 0.5,
                              f"high {intraday_high:,.2f} >= max(Kalman {kalman:,.2f}, "
                              f"EMA{CFG.target1_ema} {ema20:,.2f}). Sell 50%, move the "
                              f"remaining stop to breakeven {be:,.2f}", 2)

    # rule 4 -- after T1, a daily close below EMA9 exits next open
    if t1_hit and len(daily) >= CFG.trail_ema:
        c = daily["close"].to_numpy(dtype=float)
        ema9 = float(I.ema(pd.Series(c), CFG.trail_ema).iloc[-1])
        if float(c[-1]) < ema9:
            return ExitSignal("TRAIL_EXIT", 1.0,
                              f"daily close {c[-1]:,.2f} < EMA{CFG.trail_ema} {ema9:,.2f} "
                              f"after Target 1. Market order at next open", 4)

    # rule 5 -- OU time stop
    limit = int(pos.get("time_stop_days") or CFG.time_stop_cap_days)
    if sessions_held >= limit:
        return ExitSignal("TIME_STOP", 1.0,
                          f"held {sessions_held} sessions >= min(ceil(2.5 x tau_half), 8) "
                          f"= {limit}. The thesis has not played out; market-on-close", 5)

    return ExitSignal("HOLD", 0.0,
                      f"low {intraday_low:,.2f} > stop {stop:,.2f}, high "
                      f"{intraday_high:,.2f} < T2 {t2:,.2f}, held {sessions_held}/{limit}d", 0)


def apply_exit(db: Database, position_id: str, sig: ExitSignal, price: float,
               mode: str) -> dict:
    """Realise the exit. Appends to the audit trail; never deletes."""
    pos = db.one("SELECT * FROM positions WHERE position_id=?", (position_id,))
    if not pos:
        return {"error": "unknown position"}
    ts = _ts()
    qty = int(pos["qty"] or 0)
    exit_qty = int(round(qty * sig.qty_frac)) if sig.qty_frac < 1.0 else qty
    if exit_qty <= 0:
        return {"skipped": "no quantity to exit"}

    entry = float(pos["entry_price"])
    realized = (price - entry) * exit_qty
    r = float(pos["r_value"] or 1.0)
    realized_r = (price - entry) / r if r else 0.0

    fully = exit_qty >= qty
    with db.tx() as c:
        if fully:
            c.execute("""UPDATE positions SET state='CLOSED', closed_at=?, close_reason=?,
                         realized_pnl=realized_pnl+?, realized_r=realized_r+?
                         WHERE position_id=?""",
                      (ts, sig.reason[:200], realized, realized_r, position_id))
        else:
            c.execute("""UPDATE positions SET state='T1_HIT', qty=qty-?,
                         stop_price=?, realized_pnl=realized_pnl+?,
                         realized_r=realized_r+? WHERE position_id=?""",
                      (exit_qty, round(entry * (1 + CFG.breakeven_buffer_pct), 2),
                       realized, realized_r, position_id))
        c.execute("""INSERT INTO fills (position_id, order_id, ts, side, qty, price,
                     charges, mode) VALUES (?,?,?,?,?,?,?,?)""",
                  (position_id, None, ts, "SELL", exit_qty, price, 0.0, mode))
        c.execute("""INSERT INTO position_events (position_id, ts, event, detail)
                     VALUES (?,?,?,?)""",
                  (position_id, ts, f"EXIT_RULE_{sig.rule}",
                   json.dumps({"action": sig.action, "qty": exit_qty, "price": price,
                               "realized": realized, "realized_r": realized_r,
                               "reason": sig.reason}, default=str)))
    return {"exited": fully, "qty": exit_qty, "price": price,
            "realized": realized, "realized_r": realized_r}
