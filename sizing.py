"""
Position sizing (spec Part XI) and portfolio constraints (Part XIV).

The 2026 India correction lives here. Under SEBI's Rs15 lakh minimum contract value the
generic "Kelly as a fraction of notional" reading is not merely suboptimal, it is
unaffordable: the smallest legal 2-lot position needs Rs7.47 Cr of equity at p=0.45.
In F&O you commit margin, not notional, so Kelly is applied to margin and the notional
is grossed up by 1/margin_rate (Part 11.3, Reading B).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np

from . import indicators as I
from .config import CFG, LAKH, CRORE


@dataclass
class SizeDecision:
    lots: int = 0
    shares: int = 0
    notional: float = 0.0
    risk_rupees: float = 0.0
    r_value: float = 0.0
    r_pct: float = 0.0
    b_net: float = float("nan")
    prob_p: float = float("nan")
    kelly_f: float = 0.0
    margin_required: float = 0.0
    binding_constraint: str = ""
    reject_code: str = ""
    reason: str = ""
    trace: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.lots >= CFG.min_lots and not self.reject_code


def size_position(entry: float, stop: float, equity: float, unencumbered_cash: float,
                  adv_shares: float, lot_size: int = 1, segment: str = "futures",
                  sigma_t: float | None = None, expected_move: float | None = None,
                  prob_override: float | None = None,
                  tier: str = "Green", open_heat_rupees: float = 0.0) -> SizeDecision:
    """Full Part XI sizing chain. Returns lots=0 plus a reject code when infeasible.

    segment: "futures" (Rs15L min contract, ~15% margin) or "cash" (delivery, no
             leverage, full notional committed).
    """
    d = SizeDecision()
    R = entry - stop
    if not np.isfinite(R) or R <= 0:
        d.reject_code = "REJ_STOP_TOO_FAR"
        d.reason = f"stop {stop:,.2f} is not below entry {entry:,.2f}; R is not positive"
        return d
    d.r_value = float(R)
    d.r_pct = float(R / entry)

    # ---- 11.1 dynamic payoff ratio ----
    cost = (CFG.cost_futures_roundtrip if segment == "futures"
            else CFG.cost_cash_roundtrip)
    d.b_net = I.b_net(d.r_pct, cost, CFG.slippage_roundtrip, CFG.payoff_gross)
    d.trace["cost_roundtrip"] = cost
    d.trace["friction_in_R"] = (cost + CFG.slippage_roundtrip) / d.r_pct

    # ---- 11.2 probability, fat-tail corrected ----
    # prob_override lets the backtest inject an empirically-estimated p and lets the
    # spec's own worked examples be reproduced exactly.
    if prob_override is not None:
        d.prob_p = float(prob_override)
        d.trace["prob_source"] = "override"
    elif sigma_t is not None and sigma_t > 0:
        move = expected_move if expected_move is not None else (2.5 * R)
        d.prob_p = I.student_t_prob(move, sigma_t * entry, CFG.prob_nu)
        d.trace["prob_source"] = "student_t"
    else:
        d.prob_p = float("nan")
        d.trace["prob_source"] = "unavailable"
    d.trace["prob_break_even"] = 1.0 / (1.0 + d.b_net) if np.isfinite(d.b_net) else np.nan

    if not np.isfinite(d.prob_p) or d.prob_p <= CFG.prob_floor:
        d.reject_code = "REJ_LOW_PROB"
        d.reason = (f"p={d.prob_p:.4f} <= floor {CFG.prob_floor}. Kelly turns negative "
                    f"below p=1/(1+b)={d.trace['prob_break_even']:.4f} at "
                    f"b={d.b_net:.3f}, so the old p>0.40 floor sat ON the break-even and "
                    f"would have authorised near-zero-edge trades")
        return d

    # ---- 11.3 Kelly on margin, grossed up ----
    d.kelly_f = I.kelly_fraction(d.prob_p, d.b_net, CFG.kelly_fraction)
    if segment == "futures":
        margin_target = d.kelly_f * equity
        notional_kelly = margin_target / CFG.margin_rate
    else:
        notional_kelly = d.kelly_f * equity          # cash: no leverage

    # ---- 11.4 the four constraints, minimum wins ----
    risk_budget_pct = CFG.risk_cap_pct
    if tier == "Amber":
        risk_budget_pct *= 0.5                       # Part 4.1: halve the risk budget
    risk_budget = risk_budget_pct * equity
    notional_risk = risk_budget / d.r_pct            # R as a fraction of entry
    shares_risk = risk_budget / R

    cap = CFG.participation_cap
    shares_adv = cap * adv_shares if adv_shares > 0 else 0.0
    shares_kelly = notional_kelly / entry if entry > 0 else 0.0

    shares = min(shares_risk, shares_adv, shares_kelly)
    binding = min([("risk_cap", shares_risk), ("adv_cap", shares_adv),
                   ("kelly_margin", shares_kelly)], key=lambda kv: kv[1])[0]

    # ---- 11.6 even-lot quantization ----
    # N_lots = floor(shares / (2 * Lot_Size)) * 2.  The factor of 2 is not cosmetic:
    # the ladder scales 50% at Target 1, and a market order to sell 50% of an odd lot
    # count is rejected by the exchange (Part 11.6).
    if segment == "futures":
        even_lots = int(shares // (2 * lot_size)) * 2 if lot_size > 0 else 0
    else:
        even_lots = int(shares // lot_size) if lot_size > 0 else 0
    d.trace.update({"shares_risk": shares_risk, "shares_adv": shares_adv,
                    "shares_kelly": shares_kelly, "risk_budget": risk_budget,
                    "notional_kelly": notional_kelly, "margin_rate": CFG.margin_rate,
                    "risk_budget_pct": risk_budget_pct})

    if even_lots < CFG.min_lots:
        d.lots = even_lots
        d.reject_code = "REJ_CAPACITY"
        d.reason = (f"{even_lots} lot(s) < {CFG.min_lots} required for the two-tranche "
                    f"scale-out. Binding constraint was '{binding}': risk cap "
                    f"{shares_risk:,.0f} sh / ADV cap {shares_adv:,.0f} sh / Kelly "
                    f"{shares_kelly:,.0f} sh. Note REJ_CAPACITY is a CAPITAL rejection "
                    f"and must never be counted against signal quality (Part XIX)")
        return d

    d.lots = even_lots
    d.shares = even_lots * lot_size
    d.notional = d.shares * entry
    d.risk_rupees = d.shares * R
    d.binding_constraint = binding

    # ---- margin / cash verification ----
    d.margin_required = d.notional * CFG.margin_rate if segment == "futures" else d.notional
    if d.margin_required > unencumbered_cash:
        d.reject_code = "REJ_MARGIN"
        d.reason = (f"margin {d.margin_required/ LAKH:,.2f} L > unencumbered cash "
                    f"{unencumbered_cash/ LAKH:,.2f} L")
        return d

    # ---- Part XIV P3 heat ----
    heat_max = CFG.heat_max_amber if tier == "Amber" else CFG.heat_max_green
    heat = (open_heat_rupees + d.risk_rupees) / equity if equity > 0 else 1.0
    d.trace["heat_after"] = heat
    if heat > heat_max:
        d.reject_code = "REJ_HEAT"
        d.reason = (f"portfolio heat {heat:.2%} would exceed the {heat_max:.1%} cap "
                    f"(open {open_heat_rupees/ LAKH:,.2f} L + this "
                    f"{d.risk_rupees/ LAKH:,.2f} L of {equity/ LAKH:,.2f} L equity)")
        return d

    d.reason = (f"{even_lots} lot(s) = {d.shares:,.0f} sh, notional "
                f"{d.notional/ LAKH:,.2f} L, risk {d.risk_rupees:,.0f} "
                f"({d.risk_rupees/equity:.2%} of equity). Binding: {binding}. "
                f"p={d.prob_p:.3f} b={d.b_net:.3f} half-Kelly f={d.kelly_f:.4f}")
    return d


def time_stop_days(tau_halflife: float | None) -> int:
    """Part XIII ladder rule 5: min(ceil(2.5 * tau_half), 8). Day 8 if no valid tau."""
    if tau_halflife is None or not np.isfinite(tau_halflife) or tau_halflife <= 0:
        return CFG.time_stop_cap_days
    return int(min(int(np.ceil(CFG.time_stop_mult * tau_halflife)),
                   CFG.time_stop_cap_days))


def sector_slots(open_positions: list[dict], sector: str | None) -> bool:
    """Part XIV P1: max 1 concurrent position per sector/sub-industry."""
    if not sector:
        return True            # unknown sector: P2 still applies, do not silently allow
    held = sum(1 for p in open_positions if (p.get("sector") or "") == sector)
    return held < CFG.max_per_sector


def sector_exposure_ok(open_positions: list[dict], sector: str | None,
                       candidate_notional: float, book_value: float) -> bool:
    """Part XIV P2: total exposure in one sector <= 25% of book."""
    if not sector or book_value <= 0:
        return True
    held = sum(float(p.get("notional") or 0.0) for p in open_positions
               if (p.get("sector") or "") == sector)
    return (held + candidate_notional) <= CFG.sector_exposure_max * book_value


def concurrency_ok(n_open: int, tier: str) -> bool:
    """Part XIV P5 / Part 4.1."""
    if tier == "Green":
        return n_open < CFG.max_names_green
    if tier == "Amber":
        return n_open < CFG.max_names_amber
    return False
