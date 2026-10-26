"""
Short Borrow Monitor - public website
-------------------------------------
Shows borrow fee, rebate and shares available to short for US stocks, using
Interactive Brokers' public stock-loan file (no IBKR login, no TWS), plus
prices from Yahoo Finance via the yfinance package.

Requires: pip install yfinance

Settings (environment variables):
    SHOW_PRICES       "on" (default) or "off" to hide the price columns
    DEFAULT_TICKERS   starting list for new visitors, e.g. "AAPL,TSLA,GME,AMC"
    PORT              set automatically by most hosts (default 8000)
    DATABASE_URL      Postgres connection string (e.g. a free Neon database) for the
                      borrow fee history charts and Yahoo's ticker spellings; without it
                      charts use iBorrowDesk only
    STORAGE_LIMIT_MB  the database's size limit, for the "nearly full" warning (default 500)
    OPENFIGI_API_KEY  optional free key from openfigi.com: identifies ETF holdings faster
"""

import ftplib
import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import common
from common import (FTP_FILE, FTP_HOST, FTP_USER, LRU, YAHOO, borrow_rank, download_usa,  # noqa: F401
                    ftp_download, ibkr_symbol, is_rate_limit, normalize, parse_borrow_text, to_float)

APP_DIR = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8000"))
try:
    import yfinance as yf
except ImportError:
    yf = None
PRICES_ON = yf is not None and os.environ.get("SHOW_PRICES", "on").strip().lower() != "off"

BORROW_MAX_AGE = 15 * 60       # re-download the borrow file every 15 minutes
BORROW_RETRY = 60              # after a failed download, try again in 1 minute
PRICE_MAX_AGE = 60             # each price is reused for up to 60 seconds
MAX_SYMBOLS = 30               # most tickers one visitor can request at once
TOP_N = 25
TOP_MIN_SHARES = 1000          # leaderboard ignores stocks with tiny inventory
LIMITED_BELOW = 10_000         # under this many shares = "Limited"


DEFAULT_TICKERS = [s for s in (normalize(x) for x in os.environ.get(
    "DEFAULT_TICKERS", "AAPL,TSLA,GME,AMC,BYND").split(",")) if s]


def availability(entry):
    if not entry or entry["shares"] is None:
        return None
    if entry["shares"] <= 0:
        return "none"
    return "limited" if entry["shares"] < LIMITED_BELOW else "available"


# ----------------------------- borrow data --------------------------------
class Borrow:
    def __init__(self):
        self.lock = threading.Lock()
        self.table, self.top, self.as_of, self.error = {}, [], "", ""
        self.fetched, self.refreshing = 0.0, False

    def _download(self):
        return download_usa()

    def refresh(self):
        fetched = time.time()
        try:
            table, as_of = parse_borrow_text(self._download())
            if not table:
                raise ValueError("the file was empty")
            ranked = [(s, e) for s, e in table.items()
                      if e["fee"] is not None and (e["shares"] or 0) >= TOP_MIN_SHARES]
            ranked.sort(key=lambda x: x[1]["fee"], reverse=True)
            top = [dict(symbol=s, **e, availability=availability(e)) for s, e in ranked[:TOP_N]]
            with self.lock:
                self.table, self.top, self.as_of, self.error = table, top, as_of, ""
            print(f"Borrow file loaded: {len(table):,} stocks, as of {as_of}", flush=True)
            HISTORY.maybe_record(table, as_of)      # borrow fee history: at most every 30 minutes
        except Exception as e:
            fetched = time.time() - BORROW_MAX_AGE + BORROW_RETRY
            with self.lock:
                self.error = f"Couldn't download IBKR's borrow file ({e})."
            print(self.error, flush=True)
        finally:
            with self.lock:
                self.fetched, self.refreshing = fetched, False

    def ensure_fresh(self):
        with self.lock:
            if self.refreshing or time.time() - self.fetched < BORROW_MAX_AGE:
                return
            self.refreshing = True
            empty = not self.table
        if empty:
            self.refresh()              # first load: wait for it
        else:
            threading.Thread(target=self.refresh, daemon=True).start()

    def snapshot(self):
        with self.lock:
            return self.table, self.top, self.as_of, self.error, self.refreshing


class ForeignBorrow:
    """IBKR's non-US stock loan files (japan.txt, british.txt, ...).

    Used only to price foreign holdings on the ETF screen. They load in the
    background the first time the ETF screen asks, so the Stocks page never
    waits for them.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.tables, self.error = {}, ""          # country (file name without .txt) -> table
        self.fetched, self.refreshing = 0.0, False

    def refresh(self):
        fetched = time.time()
        try:
            tables = {}
            with ftplib.FTP(FTP_HOST, timeout=30) as ftp:
                ftp.login(FTP_USER, "")
                names = sorted({n.rsplit("/", 1)[-1] for n in ftp.nlst()})
                for name in names:
                    low = name.lower()
                    if not low.endswith(".txt") or low == FTP_FILE:
                        continue
                    table, _ = parse_borrow_text(ftp_download(ftp, name))
                    if table:
                        tables[low[:-4]] = table
            if not tables:
                raise ValueError("no country files were found")
            with self.lock:
                self.tables, self.error = tables, ""
            print(f"Foreign borrow files loaded: {', '.join(sorted(tables))}", flush=True)
        except Exception as e:
            fetched = time.time() - BORROW_MAX_AGE + 5 * BORROW_RETRY
            with self.lock:
                self.error = f"Couldn't download IBKR's non-US borrow files ({e})."
            print(self.error, flush=True)
        finally:
            with self.lock:
                self.fetched, self.refreshing = fetched, False

    def ensure_fresh(self):
        with self.lock:
            if self.refreshing or time.time() - self.fetched < BORROW_MAX_AGE:
                return
            self.refreshing = True
        threading.Thread(target=self.refresh, daemon=True).start()

    def snapshot(self):
        with self.lock:
            return self.tables, self.refreshing, self.error


BORROW = Borrow()
FOREIGN = ForeignBorrow()


def live_quote(sym):
    """The latest IBKR file's row for one ticker, for the end of a history chart."""
    table, _, as_of, _, _ = BORROW.snapshot()
    entry = table.get(sym)
    return (entry, as_of) if entry else None


def isin_of(sym):
    """IBKR's ISIN for a ticker (US ones are masked in IBKR's file), for Yahoo lookups."""
    entry = BORROW.snapshot()[0].get(sym)
    return entry.get("isin") if entry else None


def parse_symbols(query, key="symbols", limit=MAX_SYMBOLS):
    """Tickers from the query string in IBKR's spelling (BRK-B -> BRK.B), plus
    {ibkr: what was typed} for the ones that were respelled."""
    table = BORROW.snapshot()[0]
    raw = ",".join(query.get(key, [""]))
    out, typed = [], {}
    for x in raw.split(","):
        s = ibkr_symbol(x, table or None)
        if s and s not in out:
            out.append(s)
            t = normalize(x)
            if t and t != s:
                typed[s] = t
    return out[:limit], typed


import borrow_history as history_mod      # borrow fee history charts (borrow_history.py)
STORE = history_mod.make_store()
HISTORY = history_mod.HistoryService(STORE, live=live_quote)
common.SYMBOLS.store = STORE              # remembers Yahoo's spelling of each ticker (yahoo_symbols table)
STATIC_DIR = APP_DIR / "static"
STATIC_TYPES = {".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}

import etf as etf_mod                      # ETF short screen (etf.py)
import ident as ident_mod                  # identifying ETF holdings (ident.py)
ident_mod.IDS.store = STORE                # remembers who each holding is (security_ids table)
ETF = etf_mod.EtfService(yf, BORROW, FOREIGN) if yf is not None else None
DEFAULT_ETFS = [s for s in (normalize(x) for x in os.environ.get(
    "DEFAULT_ETFS", "TSLL,NVDL,CONL,MSTU,SOXL").split(",")) if s]


# -------------------------------- prices ----------------------------------
class Prices:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = LRU(3000)
        self.pool = ThreadPoolExecutor(max_workers=8)

    def _fetch(self, sym, ysym):
        if ysym is None:                    # Yahoo doesn't carry this ticker
            return {"price": None, "change": None}
        try:
            info = yf.Ticker(ysym).fast_info   # Yahoo's spelling: BRK.B is BRK-B (common.yahoo_symbol)
            price = to_float(info.last_price)
            prev = to_float(info.previous_close)
        except Exception as e:
            if is_rate_limit(e):
                YAHOO.pause()
            return None
        if not price:
            return {"price": None, "change": None}
        change = (price / prev - 1) * 100 if prev else None
        return {"price": price, "change": change}

    def get(self, symbols):
        if not PRICES_ON:
            return {}
        now, out, todo = time.time(), {}, []
        with self.lock:
            for s in symbols:
                hit = self.cache.get(s)
                if hit and now - hit[0] < PRICE_MAX_AGE:
                    out[s] = hit[1]
                else:
                    todo.append(s)
        spell = common.SYMBOLS.resolve_many(todo, {s: isin_of(s) for s in todo}) if todo else {}
        todo = [s for s in todo if spell[s].yahoo is None or YAHOO.try_acquire()]   # shared Yahoo rate limit
        for s, res in zip(todo, self.pool.map(lambda x: self._fetch(x, spell[x].yahoo), todo)):
            if res is not None:
                with self.lock:
                    self.cache[s] = (now, res)
                out[s] = res
        with self.lock:                  # fall back to an older price if needed
            for s in symbols:
                if s not in out and s in self.cache:
                    out[s] = self.cache[s][1]
        return out


PRICES = Prices()


# --------------------------- Stocks page metrics ---------------------------
import finra as finra_mod                 # FINRA's free short interest file (finra.py)
import metrics as metrics_mod             # metric catalog, fallbacks and reasons (metrics.py)
CATALOG = metrics_mod.CATALOG
FINRA = finra_mod.FinraShortInterest()
METRICS = (metrics_mod.MetricService(yf, etf_mod.SEC, bool(etf_mod.SEC_USER_AGENT),
                                     ibkr=lambda s: BORROW.snapshot()[0].get(s),
                                     holdings=ETF.lookthrough_weights if ETF else None, finra=FINRA)
           if PRICES_ON else None)


# ------------------------------ web server --------------------------------
def add_prices(out, prices, enabled):
    """ETF screen: each row's price (live, else the last close) and price × shares available."""
    out["prices_enabled"] = enabled
    for row in out.get("rows", []):
        live = (prices.get(row["symbol"]) or {}).get("price") if enabled else None
        price = live if live else (row.get("last_close") if enabled else None)
        row["price"] = price
        row["price_src"] = "live" if live else ("close" if price else None)
        shares = (row.get("quote") or {}).get("shares")
        row["avail_value"] = price * shares if price and shares is not None else None


def rate_param(query, key, default, hi=100):
    """A % / yr rate from the query string, between 0 and `hi`."""
    v = to_float(query.get(key, [""])[0])
    return default if v is None or not math.isfinite(v) or not 0 <= v <= hi else v


def borrow_status(table, as_of, error, refreshing):
    if table:
        return {"state": "ok", "as_of": as_of}
    if error and not refreshing:
        return {"state": "error", "text": error + " Retrying shortly."}
    return {"state": "loading"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json", cache="no-store"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, separators=(",", ":")))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)

        if path in ("/", "/index.html"):
            return self._send(200, (APP_DIR / "index.html").read_bytes(),
                              "text/html; charset=utf-8", "no-cache")
        if path in ("/etf", "/etf.html"):
            return self._send(200, (APP_DIR / "etf.html").read_bytes(),
                              "text/html; charset=utf-8", "no-cache")
        if path == "/api/etf/chart":
            if ETF is None:
                return self._json({"error": "yfinance is not installed on the server."}, 503)
            sym = ibkr_symbol(query.get("symbol", [""])[0], BORROW.snapshot()[0] or None)
            if not sym:
                return self._json({"error": "Give one ETF ticker, e.g. ?symbol=TSLL"}, 400)
            win = query.get("win", [""])[0].lower()
            years_raw = query.get("years", [""])[0].lower()
            start = None
            if win == "custom":
                start, err = etf_mod.parse_start_date(query.get("start", [""])[0])
                if start is None:
                    return self._json({"error": err or "Give a start date as YYYY-MM-DD."}, 400)
                years = None
            elif win:
                if win not in etf_mod.CHART_WINDOWS:
                    return self._json({"error": "win must be inception, 3y, 1y or custom"}, 400)
                years = etf_mod.CHART_WINDOWS[win]
            elif query.get("start", [""])[0]:
                start, err = etf_mod.parse_start_date(query.get("start", [""])[0])
                if start is None:
                    return self._json({"error": err}, 400)
                years = None
            elif years_raw in ("", "max"):
                years = None
            elif years_raw.isdigit() and 1 <= int(years_raw) <= 50:
                years = int(years_raw)
            else:
                return self._json({"error": "years must be a whole number of years or max"}, 400)
            out = ETF.chart(sym, years, start)
            return self._json(out, 404 if out.get("unknown") else 200)
        if path in ("/api/etf", "/api/etf/holdings"):
            if ETF is None:
                return self._json({"error": "yfinance is not installed on the server."})
            gc = rate_param(query, "gc", etf_mod.GC_DEFAULT)
            cash = rate_param(query, "cash", etf_mod.CASH_DEFAULT)
            if path == "/api/etf/holdings":
                sym = ibkr_symbol(query.get("symbol", [""])[0], BORROW.snapshot()[0] or None)
                if not sym:
                    return self._json({"error": "Give one ETF ticker, e.g. ?symbol=TSLL"})
                return self._json(ETF.holdings_detail(sym, gc, cash))
            syms, _ = parse_symbols(query)
            start, start_error = etf_mod.parse_start_date(query.get("start", [""])[0])
            spread = rate_param(query, "spread", etf_mod.RFR_SPREAD_DEFAULT, hi=10)
            out = ETF.report(syms, gc, cash, start, spread)
            out["start_error"] = start_error
            add_prices(out, PRICES.get(syms) if PRICES_ON else {}, PRICES_ON)
            return self._json(out)
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            f = STATIC_DIR / name
            if (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) and f.suffix in STATIC_TYPES
                    and f.is_file()):
                return self._send(200, f.read_bytes(), STATIC_TYPES[f.suffix], "public, max-age=86400")
            return self._send(404, '{"error":"not found"}')
        if path == "/api/borrow/history":
            BORROW.ensure_fresh()
            sym = ibkr_symbol(query.get("symbol", [""])[0], BORROW.snapshot()[0] or None) or ""
            out = HISTORY.history(sym, query.get("range", ["1y"])[0].lower())
            return self._json(out, 400 if "error" in out else 200)
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if path == "/api/config":
            return self._json({"default_tickers": DEFAULT_TICKERS,
                               "default_etfs": DEFAULT_ETFS,
                               "gc_default": etf_mod.GC_DEFAULT,
                               "cash_default": etf_mod.CASH_DEFAULT,
                               "spread_default": etf_mod.RFR_SPREAD_DEFAULT,
                               "prices_enabled": PRICES_ON,
                               "limited_below": LIMITED_BELOW,
                               "history": {"recording": HISTORY.recording,
                                           "storage_warning": HISTORY.storage_warning()}})

        if path == "/api/quotes":
            BORROW.ensure_fresh()
            syms, typed = parse_symbols(query)
            prices = PRICES.get(syms)
            table, _, as_of, error, refreshing = BORROW.snapshot()
            spell = common.SYMBOLS.resolve_many(syms, {s: isin_of(s) for s in syms}) if PRICES_ON else {}
            rows = []
            for s in syms:
                b, p = table.get(s), prices.get(s, {})
                res = spell.get(s)
                rows.append({
                    "symbol": s,
                    "typed": typed.get(s),                 # what the visitor typed, when respelled (BRK-B)
                    "yahoo": res.yahoo if res else None,   # Yahoo's ticker, shown on hover
                    "yahoo_note": common.listing_message(res) if res else None,
                    "name": b["name"] if b else None,
                    "listed": b is not None,
                    "price": p.get("price"),
                    "change": p.get("change"),
                    "shares": b["shares"] if b else None,
                    "plus": b["plus"] if b else False,
                    "fee": b["fee"] if b else None,
                    "rebate": b["rebate"] if b else None,
                    "availability": availability(b),
                })
            return self._json({"borrow": borrow_status(table, as_of, error, refreshing),
                               "prices_enabled": PRICES_ON, "rows": rows})

        if path == "/api/catalog":
            groups = CATALOG if PRICES_ON else [g for g in CATALOG if g["id"] == "borrow"]
            return self._json({"groups": groups, "yahoo": PRICES_ON,
                               "reasons": metrics_mod.REASONS, "sources": metrics_mod.SOURCES,
                               "always_calculated": sorted(metrics_mod.ALWAYS_CALCULATED)})

        if path == "/api/metrics":
            if not PRICES_ON:
                return self._json({"rows": [], "pending": 0})
            syms, typed = parse_symbols(query)
            keys = [k for k in ",".join(query.get("keys", [""])).split(",") if k]
            if not keys:                          # older pages: everything, performance only if asked
                keys = [k for k in metrics_mod.YAHOO_KEYS
                        if k not in metrics_mod.PERF_KEYS or query.get("perf", ["0"])[0] == "1"]
            BORROW.ensure_fresh()
            prefetch = query.get("prefetch", ["0"])[0] == "1"     # another tab, fetched ahead while idle
            retry = [s for s in (ibkr_symbol(x) for x in ",".join(query.get("retry", [""])).split(",")) if s]
            rows, pending = METRICS.get(syms, keys, prefetch=prefetch, retry=[s for s in retry if s in syms])
            busy = METRICS.busy_for()
            if prefetch:
                return self._json({"pending": pending, "busy": busy})
            for r in rows:
                r["typed"] = typed.get(r["symbol"])
            return self._json({"rows": rows, "pending": pending, "busy": busy})

        if path == "/api/top":
            BORROW.ensure_fresh()
            table, top, as_of, error, refreshing = BORROW.snapshot()
            return self._json({"borrow": borrow_status(table, as_of, error, refreshing),
                               "rows": top})

        self._send(404, '{"error":"not found"}')


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Short Borrow Monitor running on port {PORT}", flush=True)
    if not PRICES_ON:
        print("Prices are hidden (yfinance not installed, or SHOW_PRICES=off).", flush=True)
    threading.Thread(target=BORROW.ensure_fresh, daemon=True).start()
    if PRICES_ON:
        FINRA.ensure()                        # FINRA's short interest file loads in the background
    server.serve_forever()


if __name__ == "__main__":
    main()
