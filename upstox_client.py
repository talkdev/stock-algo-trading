"""
================================================================================
 upstox_client.py - Upstox v2 REST API client (OAuth 2.0 + 2FA, market data,
 orders).

 UPSTOX API - KNOWN INSUFFICIENCIES AND WHAT THIS SYSTEM SKIPS / EMULATES
 ------------------------------------------------------------------------
 1) NO INTRADAY HISTORY BEFORE TODAY
    /historical_candle for 5m/15m/30m/1h returns only the current trading day
    (a couple of days at most). There is no way to pull last month of 5-min
    bars from Upstox.
    => Every COMPLETED 5-min bar is persisted to SQLite the moment it
       finalises. Multi-day backtests replay data accumulated by this system
       (or generated demo data). If the DB has no history for a window, the
       backtest is simply SKIPPED with a clear message - it never fabricates
       data.
 2) NO STREAMING OVER REST
    Live ticks require Upstox's WebSocket (Scribe) channel; REST has no push.
    => We POLL: quotes for open positions + dip-watchlist every
       POLL_FAST_SEC seconds and full-bar fetches at each 5-min boundary.
       Stop/target reaction latency is therefore bounded by the poll cadence.
 3) NO GTT (good-til-trigger) AND NO BRACKET ORDERS IN THE PUBLIC API
    Only MARKET, LIMIT and SL-M/SL-L exist, and triggers live at the broker
    only until EOD.
    => Stop/target/trailing logic is implemented by the ENGINE (poll +
       market-exit). In REAL mode we additionally place a protective SL-M
       order for the initial stop so a crash still leaves a broker-side stop
       for the day. Trailing stops are engine-side only.
 4) OAUTH IS SEMI-MANUAL
    The first login needs the user's TOTP (Google Authenticator) code, so it
    cannot be fully unattended. Access tokens live 30 minutes; refresh tokens
    until EOD. We cache both in data/upstox_token.json and refresh silently;
    if both are gone, live data for that day is SKIPPED with a message telling
    you to re-auth (no trading on stale data).
 5) NO SYMBOL -> instrument_key LOOKUP ENDPOINT
    => Instrument keys are sourced from NSE's instrument master CSV (external
       fetch, cached in DB + data/instrument_master.csv). Fallback: manual
       data/instrument_keys.json. If neither works, those symbols are SKIPPED
       (logged) - we never trade without a valid key.
 6) RATE LIMITS
    Keep REST calls throttled. We space calls by API_MIN_INTERVAL_SEC and
    back off on 429/5xx.
 7) fm_token
    Most data endpoints require a second short-lived token (fm_token) minted
    from the access token via fm-upstox. We cache it in data/upstox_fm_token.
    json (5h).
 8) CANDLES ARE AS-TRADED
    No split/bonus adjustment on history.
 9) DOC VERSIONING
    Order/quote field names follow the Upstox v2 docs as of 2025-26; verify
    against the current docs if Upstox ships changes.
================================================================================
"""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

import config
from indicators import Bar
from mkttime import epoch_to_ist_str


class UpstoxError(RuntimeError):
    pass


class UpstoxClient:
    def __init__(self, client_id: str | None = None, base: str | None = None,
                 fm_base: str | None = None, token_file: str | Path | None = None):
        self.client_id = client_id or config.UPSTOX_CLIENT_ID
        self.base = (base or config.UPSTOX_API_BASE).rstrip("/")
        self.fm_base = (fm_base or config.UPSTOX_FM_BASE).rstrip("/")
        self.token_file = Path(token_file or config.TOKEN_FILE)
        self.fm_file = Path(config.FM_TOKEN_FILE)
        self.sess = requests.Session()
        self._last_call = 0.0
        self._fm: str | None = None
        self._fm_ts = 0.0

    # ------------------------------------------------------------------ auth
    def _load_token(self) -> dict | None:
        if not self.token_file.exists():
            return None
        try:
            d = json.loads(self.token_file.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    def _save_token(self, tok: dict) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(tok, indent=2), encoding="utf-8")

    def token_valid(self) -> bool:
        tok = self._load_token()
        if not tok or not tok.get("access_token"):
            return False
        return time.time() < (float(tok.get("expires_at", 0)) - 60)

    def ensure_token(self) -> None:
        """Make sure we hold a working access token (silent refresh or login)."""
        if self.token_valid():
            return
        tok = self._load_token()
        if tok and tok.get("refresh_token"):
            try:
                self._save_token(self._refresh(tok["refresh_token"]))
                return
            except UpstoxError as e:
                print(f"[warn] token refresh failed ({e}); interactive re-login required")
        self.login_interactive()

    def authorize_url(self, state: str = "stmr") -> str:
        return (f"{self.base}/login/authorization/dialog?client_id={self.client_id}"
                f"&state={state}&redirect_uri={quote(config.UPSTOX_REDIRECT_URI)}")

    def login_interactive(self) -> None:
        """One-time browser login. Cannot be fully unattended (limitation #4)."""
        if not self.client_id:
            raise UpstoxError(
                "UPSTOX_CLIENT_ID is not set. Create an API key at the Upstox "
                "developer portal and set the environment variable (see README).")
        print("\n--- Upstox one-time login (see README section 'Upstox setup') ---")
        print("1) Open this URL in a browser and log in to Upstox:")
        print("   " + self.authorize_url())
        print("2) The browser will try to redirect to 127.0.0.1 - that page may "
              "not open; that is fine.")
        try:
            code = input("3) Paste the FULL redirect URL (or just the auth_code): ").strip()
            totp = input("4) Paste the 6-digit authenticator (TOTP) code: ").strip()
        except EOFError:
            raise UpstoxError(
                "no TTY available for interactive login - create data/upstox_token"
                ".json manually or run in a terminal (see README)")
        if "auth_code=" in code:
            code = code.split("auth_code=")[1].split("&")[0]
        self._save_token(self._exchange(code, totp))
        print(f"[ok] token stored in {self.token_file}")

    def _post(self, url: str, data: dict, totp: str | None = None) -> dict:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if totp:
            headers["upstox-2fa-auth"] = totp
        r = self.sess.post(url, data=data, headers=headers,
                           timeout=config.API_TIMEOUT_SEC)
        if r.status_code in (429, 500, 502, 503, 504):
            raise UpstoxError(f"HTTP {r.status_code} from {url}")
        try:
            j = r.json()
        except Exception:
            j = {}
        if "data" not in j or j.get("data") is None:
            raise UpstoxError(f"auth failed: {j.get('message') or r.status_code}")
        return j["data"]

    def _exchange(self, code: str, totp: str) -> dict:
        d = self._post(f"{self.base}/login/authorization/token",
                       {"code": code, "client_id": self.client_id}, totp)
        return {
            "access_token": d["access_token"],
            "refresh_token": d.get("refresh_token", ""),
            "expires_at": time.time() + int(d.get("expires_in", 1800)),
        }

    def _refresh(self, refresh_token: str) -> dict:
        d = self._post(f"{self.base}/login/authorization/token",
                       {"refresh_token": refresh_token, "client_id": self.client_id})
        out = self._load_token() or {}
        out.update({
            "access_token": d["access_token"],
            "refresh_token": d.get("refresh_token") or out.get("refresh_token", ""),
            "expires_at": time.time() + int(d.get("expires_in", 1800)),
        })
        return out

    def _auth(self) -> str:
        tok = self._load_token()
        if not tok or not tok.get("access_token"):
            raise UpstoxError("no access token - run the Upstox login first")
        return tok["access_token"]

    def _fm_token(self) -> str:
        """Short-lived token required by most data endpoints (limitation #7)."""
        if self._fm and time.time() - self._fm_ts < 5 * 3600:
            return self._fm
        if self.fm_file.exists():
            try:
                d = json.loads(self.fm_file.read_text(encoding="utf-8"))
                if d.get("token") and time.time() - float(d.get("ts", 0)) < 5 * 3600:
                    self._fm, self._fm_ts = d["token"], d["ts"]
                    return self._fm
            except Exception:
                pass
        r = self.sess.get(f"{self.fm_base}/route", params={"token": self._auth()},
                          timeout=config.API_TIMEOUT_SEC)
        j = r.json() if r.status_code == 200 else {}
        tok = j.get("token")
        if not tok:
            raise UpstoxError("fm_token not issued: " + str(j)[:200])
        self._fm, self._fm_ts = tok, time.time()
        self.fm_file.write_text(json.dumps({"token": tok, "ts": self._fm_ts}),
                                encoding="utf-8")
        return tok

    # ------------------------------------------------------------- http core
    def _throttle(self) -> None:
        wait = config.API_MIN_INTERVAL_SEC - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _get(self, path: str, params: dict | None = None, fm: bool = True) -> dict:
        params = dict(params or {})
        if fm:
            params["fm_token"] = self._fm_token()
        url = self.base + path
        for attempt in range(config.API_MAX_RETRIES):
            self._throttle()
            try:
                r = self.sess.get(url, params=params,
                                  headers={"Authorization": "Bearer " + self._auth()},
                                  timeout=config.API_TIMEOUT_SEC)
            except requests.RequestException as e:
                raise UpstoxError(f"network error: {e}")
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise UpstoxError(f"HTTP {r.status_code} GET {path}: {r.text[:200]}")
        raise UpstoxError(f"retries exhausted GET {path}")

    def _post_order(self, body: dict) -> str:
        url = self.base + "/order"
        for attempt in range(config.API_MAX_RETRIES):
            self._throttle()
            try:
                r = self.sess.post(url, json=body,
                                   headers={"Authorization": "Bearer " + self._auth()},
                                   timeout=config.API_TIMEOUT_SEC)
            except requests.RequestException as e:
                raise UpstoxError(f"network error: {e}")
            if r.status_code == 200:
                j = r.json()
                if j.get("data"):
                    return j["data"]
                raise UpstoxError(f"order rejected: {j.get('message') or j}")
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise UpstoxError(f"HTTP {r.status_code} POST /order: {r.text[:200]}")
        raise UpstoxError("retries exhausted POST /order")

    # ----------------------------------------------------------- market data
    def get_candles(self, instrument_key: str, interval: str,
                    start_epoch: int, end_epoch: int) -> list[Bar]:
        """interval: '5m' | '15m' | '1h' | 'd' ...  epochs in UTC seconds.
        NOTE: intraday intervals return current-day data only (limitation #1)."""
        j = self._get(f"/historical_candle/{instrument_key}/{interval}/{int(start_epoch)}/{int(end_epoch)}")
        d = j.get("data") or {}
        out: list[Bar] = []
        for row in (d.get("candle") or []):
            try:
                ts = int(row[0])
                o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
                v = int(float(row[5]))
            except Exception:
                continue
            out.append(Bar(epoch_to_ist_str(ts), o, h, l, c, v))
        return out

    def get_quote(self, instrument_key: str) -> dict | None:
        """Last trade quote (used for tick-level stop/target checks)."""
        j = self._get(f"/market-quote/{instrument_key}")
        data = j.get("data") or []
        if not data:
            return None
        q = data[0]
        lp = q.get("last_price")
        if lp in (None, 0):
            return None
        def _f(x):
            try:
                return float(x)
            except Exception:
                return None
        return {
            "last_price": float(lp),
            "open": _f(q.get("open")),
            "high": _f(q.get("high")),
            "low": _f(q.get("low")),
            "volume": int(_f(q.get("volume")) or 0),
        }

    # ---------------------------------------------------------------- orders
    def place_order(self, instrument_key: str, qty: int, side: str,
                    product: str = "INTRADAY", order_type: str = "MARKET",
                    price: float = 0.0, trigger_price: float = 0.0,
                    validity: str = "DAY") -> str:
        """side: 'BUY' | 'SELL'. order_type: MARKET | LIMIT | SL-M | SL-L."""
        body = {
            "product": product,
            "exchange": "NSE",
            "instrument_key": instrument_key,
            "order_type": order_type,
            "transaction_type": side.upper(),
            "quantity": int(qty),
            "price": float(price),
            "trigger_price": float(trigger_price),
            "validity": validity,
            "tag": "stmr",
        }
        return self._post_order(body)

    def cancel_order(self, order_id: str) -> dict:
        self._throttle()
        try:
            r = self.sess.delete(self.base + f"/order/{order_id}",
                                 headers={"Authorization": "Bearer " + self._auth()},
                                 timeout=config.API_TIMEOUT_SEC)
        except requests.RequestException as e:
            raise UpstoxError(f"network error: {e}")
        if r.status_code != 200:
            raise UpstoxError(f"cancel failed: {r.text[:200]}")
        return r.json()

    def get_order(self, order_id: str) -> dict:
        j = self._get(f"/order/{order_id}", fm=False)
        return j.get("data") or {}

    def wait_fill(self, order_id: str, timeout: float = 15.0) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            d = self.get_order(order_id)
            st = (d.get("status") or "").upper()
            if st in ("COMPLETE", "CANCELED", "CANCELLED", "REJECTED"):
                return d
            time.sleep(1.0)
        return self.get_order(order_id)

    def get_positions(self) -> list:
        j = self._get("/user/positions")
        return j.get("data") or []

    # -------------------------------------------------- instrument master
    def fetch_instrument_master(self, cache_file: str | Path | None = None) -> dict:
        """
        Fetch NSE's equity instrument master CSV and parse symbol->key.
        (Upstox has no symbol->instrument_key lookup - limitation #5, so the
        master comes from NSE and is cached locally for a week.)
        Returns {SYMBOL: {instrument_key, lot_size, isin}}.
        """
        cache_file = Path(cache_file or (config.DATA_DIR / "instrument_master.csv"))
        text: str | None = None
        if cache_file.exists() and time.time() - cache_file.stat().st_mtime < 7 * 86400:
            text = cache_file.read_text(encoding="utf-8", errors="ignore")
        if not text:
            self._throttle()
            try:
                r = self.sess.get(
                    config.NSE_INSTRUMENT_CSV,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                                      "Chrome/124.0 Safari/537.36",
                        "Accept": "text/csv,*/*",
                    },
                    timeout=60,
                )
                if r.status_code == 200 and r.text.strip().lower().startswith("symbol"):
                    cache_file.write_text(r.text, encoding="utf-8")
                    text = r.text
            except Exception as e:
                raise UpstoxError(f"instrument master fetch failed: {e}")
        if not text:
            raise UpstoxError("no instrument master data (cache empty and fetch failed)")
        out: dict = {}
        for row in csv.DictReader(io.StringIO(text)):
            sym = (row.get("Symbol") or "").strip().upper()
            key = (row.get("Security ID") or "").strip()
            if not sym or not key:
                continue
            try:
                lot = max(1, int(float(row.get("Lot Size") or 1)))
            except Exception:
                lot = 1
            out[sym] = {"instrument_key": key, "lot_size": lot,
                        "isin": (row.get("ISIN") or "").strip()}
        return out


def fmt_ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
