"""
Market data and order gateway.

TWO IMPLEMENTATIONS OF ONE INTERFACE
====================================
UpstoxClient  -- live, talks to api.upstox.com
MockClient    -- offline, deterministic synthetic market, byte-identical return shapes

Everything above this module is written against the interface only. That is what makes
the pipeline runnable and testable with no credentials, and it is what lets backtest.py
replay the database without a network. Selecting MockClient changes nothing downstream.

UPSTOX API -- WHERE IT IS NOT SUFFICIENT FOR THIS ALGORITHM
===========================================================
Documented in full in Part XXIII of JF_OU_NSE_2026.md. Binding gaps, worst first:

1. 5-MINUTE DEPTH. Upstox serves 5-minute candles from roughly Jan-2022, about 4.7
   years. The regime-stratified CPCV validation in Part XVI wants the March-2020 crash
   as a natural stress test. Upstox cannot supply it at 5-minute resolution. Daily data
   reaches back to Jan-2000, so daily-only validation is fine; intraday validation of
   the 2020 regime is not. Workaround: a reference-data vendor for the 2018-2021
   intraday layer. This module does not paper over the gap -- it records it.

2. BACKFILL WALL CLOCK. 5-minute history is 1 month per call and the account limit is
   2000 requests per 30 minutes. A 100-name universe needs ~4,700 calls; the rate cap,
   not the per-second rate, dominates: ~7.2 hours cold. The rate limiter below is
   therefore built around the 30-minute window, not a token-per-second bucket.

3. NO POINT-IN-TIME INDEX CONSTITUENCY. Upstox has no endpoint that answers "which
   names were in the NIFTY 100 on 2021-03-15". Any backtest run on today's constituent
   list is survivorship-biased. universe_nifty100.json is therefore labelled a
   non-PIT snapshot and stamped with as_of; it must be refreshed and old rows kept with
   is_current = 0 rather than overwritten.

4. NO CORPORATE-ACTIONS ENDPOINT. Gate G1 (Part V) blocks a name around ex-dates,
   splits and results. Upstox exposes a Fundamentals API but not a reliable
   corporate-actions calendar with ex-dates. corporate_events is populated from an
   external feed; if it is empty, G1 degrades to "unknown" and the block is conservative.

5. NO SECTOR CLASSIFICATION. The sector-concentration cap (Part XII) needs a sector per
   name. Not available from Upstox. universe.sector stays NULL until supplied, and the
   cap must fail closed rather than assume every name is a different sector.

6. INDIA VIX AVAILABILITY -- UNVERIFIED. Whether NSE_INDEX|India VIX is subscribable on
   this account could not be confirmed from this environment. resolve_vix() falls back
   to computing a realised-vol proxy and marks the macro tier DEGRADED when it does.

7. NO PRE-OPEN SNAPSHOT AT 09:08. The session clock (clock.py) needs the 09:08-09:15
   pre-open equilibrium price. Upstox's quote endpoint during that window could not be
   verified. anchor/pre-open logic tolerates a missing value and reports it.

8. INSTRUMENT MASTER REACHABILITY. https://assets.upstox.com/market-quote/instruments.json
   returned HTTP 403 from the build environment. ISINs are therefore never hard-coded:
   symbol -> instrument_key is resolved at runtime from the master, and the resolution
   is cached in the instruments table so a failed refresh does not break a live run.

9. ORDER-PLACEMENT CONSTRAINTS. SEBI's algo framework from 1 Apr 2026 requires an
   Algo-ID, a static IP and Indian hosting for algo orders. Bracket/cover order
   availability post-SEBI-restriction could not be verified, so execution.py builds
   stop-loss as an independent order rather than relying on a bracket leg.

Rate limit enforced here: 2000 requests / 30 minutes, tracked across process restarts
in the kv table so a crash does not reset the budget.
"""
from __future__ import annotations

import gzip
import io
import json
import zlib
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import requests

from .config import CFG
from .clock import now_ist
from .db import Database

UPSTOX_BASE = "https://api.upstox.com"
INSTRUMENT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments.json.gz"

# Upstox interval strings accepted by the historical-candle endpoint.
INTERVALS = {"1minute", "3minute", "5minute", "15minute", "30minute", "day", "week", "month"}

# How far back each resolution is believed to reach (Part XXIII). Used to warn rather
# than to silently return an empty frame.
DEPTH_LIMITS = {"5minute": "2022-01-01", "3minute": "2022-01-01", "1minute": "2022-01-01",
                "day": "2000-01-01"}

# 5-minute candles: one month per call.
MONTHS_PER_CALL = {"5minute": 1, "3minute": 1, "1minute": 1, "15minute": 1,
                   "30minute": 6, "day": 1200, "week": 1200, "month": 1200}


class UpstoxError(RuntimeError):
    pass


class RateBudgetExhausted(UpstoxError):
    pass


# ---------------------------------------------------------------------------
@dataclass
class RateLimiter:
    """2000 requests / 30 minutes, persisted so a restart does not reset the budget.

    A plain in-memory token bucket is wrong here: the limit is account-wide and
    survives process death. Timestamps live in kv, so restarting the script after a
    crash does not buy a fresh 2000 requests and does not get the account throttled.
    """
    db: Database
    limit: int = CFG.rate_limit_per_30min
    window_s: float = 1800.0
    _hits: deque = field(default_factory=deque, init=False)
    _loaded: bool = field(default=False, init=False)

    def _load(self) -> None:
        raw = self.db.kv_get("upstox_rate_hits")
        if raw:
            try:
                for ts in json.loads(raw):
                    self._hits.append(float(ts))
            except (ValueError, TypeError):
                pass
        self._prune()
        self._loaded = True

    def _prune(self) -> None:
        cutoff = time.time() - self.window_s
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()

    def _persist(self) -> None:
        self.db.kv_set("upstox_rate_hits", json.dumps(list(self._hits)[-self.limit:]))

    def remaining(self) -> int:
        if not self._loaded:
            self._load()
        self._prune()
        return max(0, self.limit - len(self._hits))

    def acquire(self, n: int = 1) -> None:
        """Block until n requests fit in the window, then consume them."""
        if not self._loaded:
            self._load()
        while True:
            self._prune()
            if len(self._hits) + n <= self.limit:
                break
            if n > self.limit:
                raise RateBudgetExhausted(f"batch of {n} exceeds the whole budget")
            oldest = self._hits[0]
            wait = max(1.0, (oldest + self.window_s) - time.time())
            time.sleep(min(wait, 30.0))
        now = time.time()
        for _ in range(n):
            self._hits.append(now)
        self._persist()

    def seconds_until_slot(self) -> float:
        if not self._loaded:
            self._load()
        self._prune()
        if len(self._hits) < self.limit:
            return 0.0
        return max(0.0, (self._hits[0] + self.window_s) - time.time())


# ---------------------------------------------------------------------------
class MarketDataClient:
    """Interface. Every consumer in the package depends only on these signatures."""

    name = "abstract"

    def resolve_instruments(self, symbols: list[str]) -> dict[str, dict]:
        """symbol -> {instrument_key, exchange_token, trading_symbol, segment, isin,
        lot_size, tick_size}. Unresolved symbols are omitted, never invented."""
        raise NotImplementedError

    def historical(self, instrument_key: str, interval: str,
                   from_date: str, to_date: str) -> pd.DataFrame:
        """OHLCV DataFrame indexed by tz-naive IST timestamps, ascending."""
        raise NotImplementedError

    def ltp(self, instrument_keys: list[str]) -> dict[str, float]:
        raise NotImplementedError

    def ohlc(self, instrument_keys: list[str]) -> dict[str, dict]:
        """Live session snapshot: ltp/open/high/low/close/volume/upper/lower circuit."""
        raise NotImplementedError

    def funds(self) -> dict:
        raise NotImplementedError

    # ---- order side (paper mode never reaches these) ----
    def place_order(self, **kw) -> dict:
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> dict:
        raise NotImplementedError

    def order_details(self, order_id: str) -> dict:
        raise NotImplementedError

    def pending_orders(self) -> list[dict]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
class UpstoxClient(MarketDataClient):
    """Live Upstox v2/v3 gateway.

    Never fabricates data: a failed call raises, and the caller decides whether to
    fall back to the database or to skip the name. Returning a silent empty frame on
    error is how a data outage turns into a phantom "no signals today".
    """

    name = "upstox"

    def __init__(self, db: Database, access_token: str | None = None,
                 base: str = "", timeout: float = 20.0,
                 max_retry: int | None = None, verbose: bool = False):
        self.db = db
        self.token = access_token or os.environ.get("JFOU_UPSTOX_TOKEN", "")
        if not self.token:
            raise UpstoxError(
                "no Upstox access token. Export JFOU_UPSTOX_TOKEN, "
                "Use MockClient to run the pipeline offline.")
        self.base = base or CFG.upstox_base
        self.timeout = timeout
        self.max_retry = max_retry if max_retry is not None else CFG.max_retries
        self.timeout = timeout or float(CFG.request_timeout)
        self.verbose = verbose
        self.limiter = RateLimiter(db)
        self._session = requests.Session()

    # ---------------------------------------------------------------- http
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json"}

    def _get(self, path: str, params: dict | None = None,
             stream_gz: bool = False) -> requests.Response:
        last: Exception | None = None
        for attempt in range(1, self.max_retry + 1):
            self.limiter.acquire(1)
            try:
                url = path if path.startswith("http") else self.base + path
                r = self._session.get(url, headers=self._headers(), params=params,
                                      timeout=self.timeout, stream=stream_gz)
                if r.status_code == 429 or r.status_code >= 500:
                    last = UpstoxError(f"HTTP {r.status_code} on {path}")
                    time.sleep(min(2 ** attempt, 20))
                    continue
                if r.status_code == 401:
                    raise UpstoxError("HTTP 401 - access token expired or revoked")
                r.raise_for_status()
                return r
            except requests.RequestException as exc:      # network-level
                last = exc
                time.sleep(min(2 ** attempt, 20))
        raise UpstoxError(f"giving up on {path} after {self.max_retry} attempts: {last}")

    # ---------------------------------------------------------------- instruments
    def resolve_instruments(self, symbols: list[str]) -> dict[str, dict]:
        wanted = {s.upper() for s in symbols}
        rows: dict[str, dict] = {}
        try:
            r = self._get(INSTRUMENT_MASTER_URL, stream_gz=True)
            raw = gzip.decompress(r.content).decode("utf-8") if r.content[:2] == b"\x1f\x8b" \
                else r.content.decode("utf-8")
            master = json.loads(raw)
            for row in master:
                sym = (row.get("trading_symbol") or "").upper()
                seg = (row.get("exchange") or "") + "|" + (row.get("instrument_type") or "")
                if sym not in wanted:
                    continue
                if row.get("instrument_type") not in ("EQ", "EQUITY", "INDEX"):
                    continue
                if row.get("exchange") != "NSE":
                    continue
                isin = row.get("isin") or ""
                key = row.get("instrument_key") or (f"NSE_EQ|{isin}" if isin else "")
                if not key:
                    continue
                rec = {"instrument_key": key,
                       "exchange_token": str(row.get("exchange_token") or ""),
                       "trading_symbol": sym,
                       "name": row.get("name") or sym,
                       "segment": seg, "isin": isin,
                       "lot_size": int(row.get("lot_size") or 1),
                       "tick_size": float(row.get("tick_size") or 0.05)}
                # prefer the first EQ row seen for a symbol
                rows.setdefault(sym, rec)
        except (UpstoxError, OSError, ValueError, gzip.BadGzipFile) as exc:
            # Do not invent keys. Fall back to whatever was cached previously.
            cached = self.db.query(
                "SELECT * FROM instruments WHERE UPPER(trading_symbol) IN ("
                + ",".join("?" * len(wanted)) + ")", list(wanted))
            for c in cached:
                rows[c["trading_symbol"].upper()] = dict(c)
            if not rows:
                raise UpstoxError(f"instrument master unavailable and no cache: {exc}") from exc
        if rows:
            self.db.upsert_instruments(list(rows.values()))
        return rows

    # ---------------------------------------------------------------- history
    def historical(self, instrument_key: str, interval: str,
                   from_date: str, to_date: str) -> pd.DataFrame:
        if interval not in INTERVALS:
            raise UpstoxError(f"unsupported interval {interval}")
        f, t = from_date[:10], to_date[:10]
        floor = DEPTH_LIMITS.get(interval)
        if floor and f < floor:
            raise UpstoxError(
                f"{interval} for {instrument_key} requested from {f}, but Upstox "
                f"{interval} depth starts ~{floor} (Part XXIII 23.1). Use a reference-data "
                f"vendor for this layer, or validate on daily bars.")

        frames: list[pd.DataFrame] = []
        cur = pd.Timestamp(f)
        end = pd.Timestamp(t)
        span_days = MONTHS_PER_CALL.get(interval, 1) * 30
        while cur <= end:
            chunk_end = min(cur + pd.Timedelta(days=span_days - 1), end)
            path = (f"/v2/historical-candle/{requests.utils.quote(instrument_key, safe='')}/"
                    f"{interval}/{chunk_end:%Y-%m-%d}/{cur:%Y-%m-%d}")
            r = self._get(path)
            payload = r.json()
            if payload.get("status") not in (None, "success"):
                raise UpstoxError(f"Upstox error for {instrument_key}: {payload}")
            candles = (payload.get("data") or {}).get("candles") or []
            if candles:
                frames.append(self._candles_to_df(candles))
            cur = chunk_end + pd.Timedelta(days=1)

        if not frames:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.concat(frames)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df

    @staticmethod
    def _candles_to_df(candles: list) -> pd.DataFrame:
        # Upstox candle: [ts, open, high, low, close, volume, oi?]
        df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"]
                          + [f"x{i}" for i in range(max(0, len(candles[0]) - 6))])
        ts = pd.to_datetime(df["ts"], utc=True, format="mixed")
        idx = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        out = df[["open", "high", "low", "close", "volume"]].copy()
        out.index = idx
        out = out.apply(pd.to_numeric, errors="coerce")
        return out.dropna(subset=["close"])

    # ---------------------------------------------------------------- quotes
    def ltp(self, instrument_keys: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for i in range(0, len(instrument_keys), 100):
            batch = instrument_keys[i:i + 100]
            r = self._get("/v2/market-quote/ltp",
                          params={"instrument_key": ",".join(batch)})
            for key, v in (r.json().get("data") or {}).items():
                out[key] = float(v.get("last_price"))
        return out

    def ohlc(self, instrument_keys: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(instrument_keys), 100):
            batch = instrument_keys[i:i + 100]
            r = self._get("/v2/market-quote/ohlc",
                          params={"instrument_key": ",".join(batch), "interval": "1minute"})
            for key, v in (r.json().get("data") or {}).items():
                o = v.get("ohlc") or {}
                out[key] = {"ltp": float(o.get("last_price") or 0.0),
                            "open": float(o.get("open") or 0.0),
                            "high": float(o.get("high") or 0.0),
                            "low": float(o.get("low") or 0.0),
                            "close": float(o.get("close") or 0.0),
                            "volume": float(v.get("volume") or 0.0),
                            "upper_circuit": float(v.get("upper_circuit_limit") or 0.0),
                            "lower_circuit": float(v.get("lower_circuit_limit") or 0.0)}
        return out

    def funds(self) -> dict:
        r = self._get("/v2/user/get-funds-and-margin")
        d = (r.json().get("data") or {}).get("equity") or {}
        return {"available_margin": float(d.get("available_margin") or 0.0),
                "used_margin": float(d.get("used_margin") or 0.0),
                "collateral": float((d.get("collateral") or {}).get("collateral_amount") or 0.0)}

    # ---------------------------------------------------------------- orders
    def _post(self, path: str, body: dict) -> dict:
        self.limiter.acquire(1)
        r = self._session.post(self.base + path, headers=self._headers(),
                               data=json.dumps(body), timeout=self.timeout)
        if r.status_code == 401:
            raise UpstoxError("HTTP 401 - access token expired or revoked")
        r.raise_for_status()
        payload = r.json()
        if payload.get("status") not in (None, "success"):
            raise UpstoxError(f"order rejected: {payload}")
        return payload.get("data") or {}

    def place_order(self, **kw) -> dict:
        body = {"quantity": int(kw["qty"]), "product": kw.get("product", "I"),
                "validity": kw.get("validity", "DAY"), "price": float(kw.get("price", 0.0)),
                "tag": kw.get("tag", "jfou"), "instrument_token": kw["instrument_key"],
                "order_type": kw.get("order_type", "MARKET"),
                "transaction_type": kw["side"],
                "disclosed_quantity": int(kw.get("disclosed", 0)),
                "trigger_price": float(kw.get("trigger_price", 0.0)), "is_amo": False}
        data = self._post("/v2/order/place", body)
        return {"order_id": str(data.get("order_id") or ""), "raw": data}

    def cancel_order(self, order_id: str) -> dict:
        return self._post("/v2/order/cancel", {"order_id": order_id})

    def order_details(self, order_id: str) -> dict:
        r = self._get("/v2/order/details", params={"order_id": order_id})
        return r.json().get("data") or {}

    def pending_orders(self) -> list[dict]:
        r = self._get("/v2/order/retrieve-all")
        return r.json().get("data") or []


# ---------------------------------------------------------------------------
class MockClient(MarketDataClient):
    """Deterministic offline market. Same shapes as UpstoxClient, no network.

    Every name is generated from a seeded PRNG keyed on its symbol, so a run is exactly
    reproducible and a restart produces identical bars -- which is what makes the
    restart-safety and replay guarantees testable.

    The universe is deliberately heterogeneous so the gate cascade has something to
    reject: some names mean-revert, some trend, some carry a jump, some are illiquid.
    A mock that makes everything pass proves nothing.
    """

    name = "mock"
    INSTRUMENT_MASTER_FLOOR = "2022-01-01"

    # archetypes -> (return persistence phi, spread AR(1) lambda, jump prob, adv Rs)
    #
    # lambda is applied to the EMA20 SPREAD, not to the return series. An earlier
    # version injected it into the returns, but the EMA20 filter is itself a strong
    # low-pass: a return series with lambda = -0.22 produced a spread with lambda
    # ~ -0.05 (tau 14-18 sessions). Nothing could ever pass S2's 1-3.5 session band,
    # so the sizing and execution paths were never exercised by any scan.
    _ARCHETYPES = {
        "mean_revert": (0.30, -0.30, 0.00, 60e8),
        "slow_revert": (0.40, -0.12, 0.00, 40e8),
        "trend":       (0.72, -0.02, 0.00, 80e8),
        "jumpy":       (0.45, -0.25, 0.04, 25e8),
        "illiquid":    (0.45, -0.28, 0.00, 3e8),
    }

    def __init__(self, db: Database, symbols: list[str] | None = None, seed: int = 20260913):
        self.db = db
        self.symbols = [s.upper() for s in (symbols or [])]
        self.seed = seed
        self._arch: dict[str, str] = {}
        self._px: dict[str, dict] = {}
        for i, s in enumerate(self.symbols):
            self._arch[s] = list(self._ARCHETYPES)[i % len(self._ARCHETYPES)]
            self._px[s] = self._synth(s)

    # ---------------------------------------------------------------- synth
    def _archetype(self, sym: str) -> str:
        return self._arch.get(sym, "mean_revert")

    def _synth(self, sym: str) -> dict:
        """Build a full daily history with a stable, symbol-keyed PRNG."""
        kind = self._archetype(sym)
        phi_h, lam, pjump, adv = self._ARCHETYPES[kind]
        # zlib.crc32, NOT hash(): CPython salts str hashing per process, so hash()
        # would generate a different market on every restart and destroy replay.
        rng = np.random.default_rng(zlib.crc32(sym.encode("utf-8")))
        n = 900
        dates = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=n)

        # persistent innovation -> controllable Hurst
        r = np.zeros(n)
        eps = rng.normal(0, 0.014, n)
        for i in range(1, n):
            r[i] = (2 * phi_h - 1) * r[i - 1] + eps[i]
        # Mean-reverting component. Build it so that the deviation from the SLOW trend
        # is itself AR(1) with coefficient (1+lam); that is the process ou_halflife
        # measures once the EMA20 has removed the drift.
        dev = np.zeros(n)
        for i in range(1, n):
            dev[i] = (1 + lam) * dev[i - 1] + rng.normal(0, 0.006)
        drift = np.linspace(0, 0.10 if kind != "trend" else 0.45, n)
        if pjump > 0:
            hits = rng.random(n) < pjump
            r[hits] += rng.choice([-1, 1], hits.sum()) * rng.uniform(0.06, 0.12, hits.sum())

        px0 = 200 * (0.4 + 3.0 * ((zlib.crc32(sym.encode()) % 97) / 97))
        close = px0 * np.exp(np.cumsum(r) + dev + drift)
        hi = close * (1 + np.abs(rng.normal(0, 0.008, n)))
        lo = close * (1 - np.abs(rng.normal(0, 0.008, n)))
        op = lo + (hi - lo) * rng.random(n)
        base_vol = adv / np.maximum(close, 1e-6)
        vol = np.maximum(base_vol * rng.uniform(0.5, 1.6, n), 1e3)

        daily = pd.DataFrame({"open": op, "high": np.maximum(hi, np.maximum(op, close)),
                              "low": np.minimum(lo, np.minimum(op, close)),
                              "close": close, "volume": vol}, index=dates)
        return {"daily": daily, "adv": adv}

    def _intraday(self, sym: str, day: pd.Timestamp) -> pd.DataFrame:
        """75 five-minute bars for one session, with a realistic U-shaped volume."""
        d = self._px[sym]["daily"]
        # get_indexer(method="ffill") on a day absent from the index returns the day
        # itself, not the previous session -- so a weekday holiday produced an empty
        # frame and silently shortened the backfill. searchsorted with side="right"
        # genuinely steps back to the last traded session.
        # Anchor the session on the DAILY OPEN of `day`, not on the previous close.
        # Basing it on the prior close makes bar 1 carry the whole overnight gap, which
        # seasonal_factors() then averages into slot 1. Slot 1 came out 9.3x slot 2 and
        # de-seasonalisation actively distorted the series instead of cleaning it.
        # Overnight gaps are the business of gate G3.3, not of the intraday profile.
        pos = int(d.index.searchsorted(day, side="right")) - 1
        if pos < 0:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        if day in d.index:
            base = float(d.loc[day, "open"])
        else:
            base = float(d["close"].iloc[pos])
        rng = np.random.default_rng(
            int(day.toordinal()) * 7919 + zlib.crc32(sym.encode()) % 10000)
        slots = pd.date_range(day + pd.Timedelta(hours=9, minutes=15), periods=75, freq="5min")
        slot = np.arange(75)
        u = 1.0 + 2.5 * ((slot / 74 - 0.5) ** 2) * 4          # open/close volatility bulge
        r = rng.normal(0, 0.0011, 75) * u
        # The first 5-minute bar's CLOSE is compared against the daily open by
        # seasonal_factors(), whereas every later bar is a bar-to-bar step. Sizing bar 1
        # like the others made slot 1 read ~10x slot 2 purely as an artifact, and
        # de-seasonalisation then distorted the series instead of cleaning it. A real
        # opening print is the first tick after the open, not a full bar of drift.
        r[0] *= 0.15
        close = base * np.exp(np.cumsum(r))
        # Pin the session's last bar to the daily close. A free random walk from the
        # daily open leaves the 15:25 close uncorrelated with the daily close, which is
        # not what real intraday data looks like and which the jump test reads as
        # unexplained dispersion.
        if day in d.index:
            target = float(d.loc[day, "close"])
            if target > 0 and close[-1] > 0:
                close = close * (target / close[-1])
        hi = close * (1 + np.abs(rng.normal(0, 0.0008, 75)))
        lo = close * (1 - np.abs(rng.normal(0, 0.0008, 75)))
        op = lo + (hi - lo) * rng.random(75)
        volu = 2e5 * u * rng.uniform(0.6, 1.5, 75)
        return pd.DataFrame({"open": op, "high": hi, "low": lo, "close": close,
                             "volume": volu}, index=slots)

    # ---------------------------------------------------------------- interface
    def resolve_instruments(self, symbols: list[str]) -> dict[str, dict]:
        """Synthetic but STABLE keys. Clearly marked so they can never be mistaken
        for real ISINs."""
        out = {}
        for s in symbols:
            s = s.upper()
            if s not in self._px:
                continue
            token = f"MOCK{s:0>6}"[:12]
            out[s] = {"instrument_key": f"NSE_EQ|{token}", "exchange_token": token,
                      "trading_symbol": s, "name": f"{s} (mock)", "segment": "NSE|EQ",
                      "isin": "", "lot_size": 1, "tick_size": 0.05}
        return out

    def historical(self, instrument_key: str, interval: str,
                   from_date: str, to_date: str) -> pd.DataFrame:
        f, t = pd.Timestamp(from_date[:10]), pd.Timestamp(to_date[:10])
        # Only the two index keys are restricted to daily resolution. This check used to
        # run before the equity branch, which made every 5-minute equity request fail
        # with "index keys" and silently emptied gate G3 for the whole universe.
        if instrument_key in (self._INDEX_KEY, self._VIX_KEY):
            if interval != "day":
                raise UpstoxError(f"mock does not serve interval {interval} for index keys")
            d = (self._index_series() if instrument_key == self._INDEX_KEY
                 else self._vix_series())
            return d.loc[(d.index >= f) & (d.index <= t)].copy()

        sym = self._symbol_for(instrument_key)
        if sym is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        if interval == "day":
            d = self._px[sym]["daily"]
            return d.loc[(d.index >= f) & (d.index <= t)].copy()
        if interval == "5minute":
            if str(f.date()) < self.INSTRUMENT_MASTER_FLOOR:
                # mirror the real gap so the pipeline's warning path gets exercised
                raise UpstoxError(
                    f"mock 5-minute depth starts {self.INSTRUMENT_MASTER_FLOOR} "
                    f"(mirrors Upstox, Part XXIII 23.1); requested {f.date()}")
            d = self._px[sym]["daily"]
            days = d.index[(d.index >= f) & (d.index <= t)]
            if not len(days):
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            return pd.concat([self._intraday(sym, day) for day in days])
        raise UpstoxError(f"mock does not serve interval {interval}")

    # ------------------------------------------------------------------ index / VIX
    # The real gate cascade needs Nifty 50 and India VIX. A mock that cannot serve them
    # leaves G0 permanently DEGRADED and the engine never scans a single name, so the
    # whole pipeline goes untested. These are synthetic and labelled as such.
    _INDEX_KEY = "NSE_INDEX|Nifty 50"
    _VIX_KEY = "NSE_INDEX|India VIX"

    def _index_series(self) -> pd.DataFrame:
        rng = np.random.default_rng(7)
        n = 900
        dates = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=n)
        # a 2026-shaped path: decline into March, recovery, then Aug-Sep weakness
        drift = np.concatenate([
            np.full(300, -0.0006), np.full(250, 0.0011),
            np.full(200, 0.0004), np.full(150, -0.0007)])[:n]
        r = drift + rng.normal(0, 0.0075, n)
        close = 26900 * np.exp(np.cumsum(r))
        hi = close * (1 + np.abs(rng.normal(0, 0.004, n)))
        lo = close * (1 - np.abs(rng.normal(0, 0.004, n)))
        op = lo + (hi - lo) * rng.random(n)
        return pd.DataFrame({"open": op, "high": hi, "low": lo, "close": close,
                             "volume": 3e8 * rng.uniform(0.7, 1.4, n)}, index=dates)

    def _vix_series(self) -> pd.DataFrame:
        rng = np.random.default_rng(19)
        n = 900
        dates = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=n)
        # calm 10.6-12.4 baseline with a Feb-Mar 2026 style shock, mean-reverting
        base = 11.5
        v = np.zeros(n)
        v[0] = base
        for i in range(1, n):
            shock = 0.0
            if 560 <= i < 610:                       # the Feb-Mar volatility shock
                shock = 9.0 * np.exp(-abs(i - 585) / 12.0)
            v[i] = max(8.0, v[i - 1] + 0.35 * (base + shock - v[i - 1])
                       + rng.normal(0, 0.35))
        close = v
        hi = close * (1 + np.abs(rng.normal(0, 0.02, n)))
        lo = close * (1 - np.abs(rng.normal(0, 0.02, n)))
        return pd.DataFrame({"open": lo + (hi - lo) * rng.random(n), "high": hi,
                             "low": lo, "close": close,
                             "volume": np.zeros(n)}, index=dates)

    def _symbol_for(self, instrument_key: str) -> str | None:
        for s, rec in self.resolve_instruments(self.symbols).items():
            if rec["instrument_key"] == instrument_key:
                return s
        return None

    def ltp(self, instrument_keys: list[str]) -> dict[str, float]:
        out = {}
        for k in instrument_keys:
            sym = self._symbol_for(k)
            if sym:
                out[k] = float(self._px[sym]["daily"]["close"].iloc[-1])
        return out

    def ohlc(self, instrument_keys: list[str]) -> dict[str, dict]:
        out = {}
        for k in instrument_keys:
            sym = self._symbol_for(k)
            if not sym:
                continue
            last = self._px[sym]["daily"].iloc[-1]
            out[k] = {"ltp": float(last["close"]), "open": float(last["open"]),
                      "high": float(last["high"]), "low": float(last["low"]),
                      "close": float(last["close"]), "volume": float(last["volume"]),
                      "upper_circuit": float(last["close"]) * 1.10,
                      "lower_circuit": float(last["close"]) * 0.90}
        return out

    def funds(self) -> dict:
        eq = float(CFG.paper_unencumbered_cash)
        return {"available_margin": eq, "used_margin": 0.0, "collateral": 0.0}

    def place_order(self, **kw) -> dict:
        return {"order_id": f"MOCK{int(time.time()*1000)}", "raw": kw}

    def cancel_order(self, order_id: str) -> dict:
        return {"order_id": order_id, "status": "cancelled"}

    def order_details(self, order_id: str) -> dict:
        return {"order_id": order_id, "status": "complete"}

    def pending_orders(self) -> list[dict]:
        return []


# ---------------------------------------------------------------------------
def make_client(db: Database, mode: str = "paper",
                symbols: list[str] | None = None) -> MarketDataClient:
    """paper/mock -> MockClient; live -> UpstoxClient (raises if no token)."""
    live = (mode == "live") and bool(os.environ.get("JFOU_UPSTOX_TOKEN", "")) \
        and not CFG.paper_trade
    if live:
        return UpstoxClient(db)
    return MockClient(db, symbols=symbols or [])
