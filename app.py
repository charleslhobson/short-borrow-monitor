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
"""

import ftplib
import io
import json
import math
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8000"))
try:
    import yfinance as yf
except ImportError:
    yf = None
PRICES_ON = yf is not None and os.environ.get("SHOW_PRICES", "on").strip().lower() != "off"

FTP_HOST, FTP_USER, FTP_FILE = "ftp2.interactivebrokers.com", "shortstock", "usa.txt"
BORROW_MAX_AGE = 15 * 60       # re-download the borrow file every 15 minutes
BORROW_RETRY = 60              # after a failed download, try again in 1 minute
PRICE_MAX_AGE = 60             # each price is reused for up to 60 seconds
PRICE_CALLS_PER_MIN = 100      # be gentle with Yahoo to avoid being blocked
MAX_SYMBOLS = 30               # most tickers one visitor can request at once
TOP_N = 25
TOP_MIN_SHARES = 1000          # leaderboard ignores stocks with tiny inventory
LIMITED_BELOW = 10_000         # under this many shares = "Limited"


def normalize(text):
    s = str(text).strip().upper().replace(" ", ".").lstrip("$")
    return s if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,9}", s) else None


DEFAULT_TICKERS = [s for s in (normalize(x) for x in os.environ.get(
    "DEFAULT_TICKERS", "AAPL,TSLA,GME,AMC,BYND").split(",")) if s]


def to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def availability(entry):
    if not entry or entry["shares"] is None:
        return None
    if entry["shares"] <= 0:
        return "none"
    return "limited" if entry["shares"] < LIMITED_BELOW else "available"


# ----------------------------- borrow data --------------------------------
def borrow_rank(entry):
    """When IBKR lists a stock twice, keep the row with a fee, then the one with more shares."""
    shares = entry.get("shares")
    return (entry.get("fee") is not None, shares if shares is not None else -1)


def parse_borrow_text(text):
    table, header, as_of = {}, None, ""
    for line in text.splitlines():
        if line.startswith("#BOF"):
            as_of = " ".join(p.strip() for p in line.split("|")[1:3])
        elif line.startswith("#SYM"):
            header = [h.strip().lstrip("#").upper() for h in line.split("|")]
        elif line.startswith("#") or not line.strip() or header is None:
            continue
        else:
            row = dict(zip(header, (p.strip() for p in line.split("|"))))
            sym = normalize(row.get("SYM", ""))
            if not sym:
                continue
            raw = row.get("AVAILABLE", "")
            digits = raw.replace(">", "").replace(",", "").strip()
            entry = {
                "name": row.get("NAME", "").title(),
                "fee": to_float(row.get("FEERATE")),
                "rebate": to_float(row.get("REBATERATE")),
                "shares": int(digits) if digits.isdigit() else None,
                "plus": raw.startswith(">"),
                "isin": row.get("ISIN", "").upper() or None,
            }
            old = table.get(sym)
            if old is None or borrow_rank(entry) > borrow_rank(old):
                table[sym] = entry
    return table, as_of


def ftp_download(ftp, name):
    buf = io.BytesIO()
    ftp.retrbinary(f"RETR {name}", buf.write)
    return buf.getvalue().decode("utf-8", errors="replace")


class Borrow:
    def __init__(self):
        self.lock = threading.Lock()
        self.table, self.top, self.as_of, self.error = {}, [], "", ""
        self.fetched, self.refreshing = 0.0, False

    def _download(self):
        with ftplib.FTP(FTP_HOST, timeout=30) as ftp:
            ftp.login(FTP_USER, "")
            return ftp_download(ftp, FTP_FILE)

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

import etf as etf_mod                      # ETF short screen (etf.py)
ETF = etf_mod.EtfService(yf, BORROW, FOREIGN) if yf is not None else None
DEFAULT_ETFS = [s for s in (normalize(x) for x in os.environ.get(
    "DEFAULT_ETFS", "TSLL,NVDL,CONL,MSTU,SOXL").split(",")) if s]


# -------------------------------- prices ----------------------------------
class Prices:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}
        self.calls = deque()
        self.pool = ThreadPoolExecutor(max_workers=8)

    def _allow(self):
        now = time.time()
        with self.lock:
            while self.calls and now - self.calls[0] > 60:
                self.calls.popleft()
            if len(self.calls) >= PRICE_CALLS_PER_MIN:
                return False
            self.calls.append(now)
            return True

    def _fetch(self, sym):
        try:
            info = yf.Ticker(sym.replace(".", "-")).fast_info   # Yahoo writes BRK.B as BRK-B
            price = to_float(info.last_price)
            prev = to_float(info.previous_close)
        except Exception:
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
            if len(self.cache) > 5000:
                self.cache.clear()
            for s in symbols:
                hit = self.cache.get(s)
                if hit and now - hit[0] < PRICE_MAX_AGE:
                    out[s] = hit[1]
                else:
                    todo.append(s)
        todo = [s for s in todo if self._allow()]
        for s, res in zip(todo, self.pool.map(self._fetch, todo)):
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


# ------------------------- yfinance metric catalog ------------------------
# fmt values (formatted in the browser):
#   money, bigmoney, compact, int, num1, num2, ratio, pct (fraction -> %),
#   pct100 (already a percent), date (epoch seconds), date_ms, text, rec, avail
# "signed": true colors the value green/red with a +/- sign.
def M(key, label, fmt, signed=False):
    return {"key": key, "label": label, "fmt": fmt, "signed": signed}


CATALOG = [
    {"id": "short", "label": "Short interest", "preset": True,
     "desc": "Short % of float, days to cover, float and ownership", "metrics": [
        M("shortPercentOfFloat", "Short % of float", "pct"),
        M("sharesShort", "Shares short", "compact"),
        M("sharesShortPriorMonth", "Shares short, prior month", "compact"),
        M("short_change", "Short interest change", "pct", True),
        M("shortRatio", "Days to cover", "num2"),
        M("sharesPercentSharesOut", "Short % of shares out", "pct"),
        M("floatShares", "Float", "compact"),
        M("sharesOutstanding", "Shares outstanding", "compact"),
        M("impliedSharesOutstanding", "Implied shares outstanding", "compact"),
        M("heldPercentInsiders", "Insider ownership", "pct"),
        M("heldPercentInstitutions", "Institutional ownership", "pct"),
        M("dateShortInterest", "Short interest as of", "date"),
     ]},
    {"id": "trading", "label": "Price & trading", "preset": True,
     "desc": "Quote, volume, 52-week range and moving averages", "metrics": [
        M("currentPrice", "Price", "money"),
        M("previousClose", "Previous close", "money"),
        M("open", "Open", "money"),
        M("dayLow", "Day low", "money"),
        M("dayHigh", "Day high", "money"),
        M("bid", "Bid", "money"),
        M("ask", "Ask", "money"),
        M("bidSize", "Bid size", "int"),
        M("askSize", "Ask size", "int"),
        M("preMarketPrice", "Pre-market price", "money"),
        M("postMarketPrice", "After-hours price", "money"),
        M("regularMarketVolume", "Volume", "compact"),
        M("averageVolume", "Avg volume (3 mo)", "compact"),
        M("averageVolume10days", "Avg volume (10 day)", "compact"),
        M("fiftyTwoWeekLow", "52-wk low", "money"),
        M("fiftyTwoWeekHigh", "52-wk high", "money"),
        M("fiftyTwoWeekLowChangePercent", "Above 52-wk low", "pct", True),
        M("fiftyTwoWeekHighChangePercent", "From 52-wk high", "pct", True),
        M("fiftyDayAverage", "50-day average", "money"),
        M("twoHundredDayAverage", "200-day average", "money"),
        M("fiftyDayAverageChangePercent", "vs 50-day average", "pct", True),
        M("twoHundredDayAverageChangePercent", "vs 200-day average", "pct", True),
        M("beta", "Beta", "num2"),
     ]},
    {"id": "performance", "label": "Performance", "preset": True,
     "desc": "Returns from 1 day to 5 years, volatility, drawdown and RSI", "metrics": [
        M("perf_1d", "1 day", "pct", True),
        M("perf_5d", "5 days", "pct", True),
        M("perf_1m", "1 month", "pct", True),
        M("perf_3m", "3 months", "pct", True),
        M("perf_6m", "6 months", "pct", True),
        M("perf_ytd", "Year to date", "pct", True),
        M("perf_1y", "1 year", "pct", True),
        M("perf_3y", "3 years", "pct", True),
        M("perf_5y", "5 years", "pct", True),
        M("range_pos", "Position in 52-wk range", "pct"),
        M("vol_30d", "Volatility (30 day, yearly)", "pct"),
        M("max_dd_1y", "Max drawdown (1 yr)", "pct", True),
        M("rsi_14", "RSI (14 day)", "num1"),
     ]},
    {"id": "valuation", "label": "Valuation", "preset": True,
     "desc": "Market cap, P/E, price-to-book, EV multiples and EPS", "metrics": [
        M("marketCap", "Market cap", "bigmoney"),
        M("enterpriseValue", "Enterprise value", "bigmoney"),
        M("trailingPE", "P/E (trailing)", "ratio"),
        M("forwardPE", "P/E (forward)", "ratio"),
        M("trailingPegRatio", "PEG ratio", "ratio"),
        M("priceToBook", "Price / book", "ratio"),
        M("priceToSalesTrailing12Months", "Price / sales", "ratio"),
        M("enterpriseToRevenue", "EV / revenue", "ratio"),
        M("enterpriseToEbitda", "EV / EBITDA", "ratio"),
        M("bookValue", "Book value / share", "money"),
        M("trailingEps", "EPS (trailing)", "money"),
        M("forwardEps", "EPS (forward)", "money"),
        M("epsCurrentYear", "EPS estimate, this year", "money"),
     ]},
    {"id": "profitability", "label": "Profitability", "preset": True,
     "desc": "Margins and returns on assets and equity", "metrics": [
        M("grossMargins", "Gross margin", "pct", True),
        M("operatingMargins", "Operating margin", "pct", True),
        M("ebitdaMargins", "EBITDA margin", "pct", True),
        M("profitMargins", "Net margin", "pct", True),
        M("returnOnAssets", "Return on assets", "pct", True),
        M("returnOnEquity", "Return on equity", "pct", True),
     ]},
    {"id": "growth", "label": "Growth", "preset": True,
     "desc": "Year-over-year revenue and earnings growth", "metrics": [
        M("revenueGrowth", "Revenue growth (yoy)", "pct", True),
        M("earningsGrowth", "Earnings growth (yoy)", "pct", True),
        M("earningsQuarterlyGrowth", "Quarterly earnings growth (yoy)", "pct", True),
     ]},
    {"id": "financials", "label": "Financial health", "preset": True,
     "desc": "Revenue, cash flow, cash, debt and liquidity", "metrics": [
        M("totalRevenue", "Revenue (ttm)", "bigmoney"),
        M("revenuePerShare", "Revenue / share", "money"),
        M("grossProfits", "Gross profit", "bigmoney"),
        M("ebitda", "EBITDA", "bigmoney"),
        M("netIncomeToCommon", "Net income", "bigmoney"),
        M("operatingCashflow", "Operating cash flow", "bigmoney"),
        M("freeCashflow", "Free cash flow", "bigmoney"),
        M("totalCash", "Total cash", "bigmoney"),
        M("totalCashPerShare", "Cash / share", "money"),
        M("totalDebt", "Total debt", "bigmoney"),
        M("debtToEquity", "Debt / equity", "pct100"),
        M("currentRatio", "Current ratio", "num2"),
        M("quickRatio", "Quick ratio", "num2"),
     ]},
    {"id": "dividends", "label": "Dividends", "preset": True,
     "desc": "Dividend rate, yield, payout ratio and dates", "metrics": [
        M("dividendRate", "Dividend / share (yearly)", "money"),
        M("yield_calc", "Dividend yield", "pct"),
        M("trailingAnnualDividendRate", "Trailing dividend", "money"),
        M("trailingAnnualDividendYield", "Trailing yield", "pct"),
        M("fiveYearAvgDividendYield", "5-yr avg yield", "pct100"),
        M("payoutRatio", "Payout ratio", "pct"),
        M("exDividendDate", "Ex-dividend date", "date"),
        M("lastDividendValue", "Last dividend", "money"),
        M("lastDividendDate", "Last dividend date", "date"),
     ]},
    {"id": "analysts", "label": "Analysts", "preset": True,
     "desc": "Ratings, number of analysts and price targets", "metrics": [
        M("recommendationKey", "Rating", "rec"),
        M("recommendationMean", "Rating score (1 buy, 5 sell)", "num2"),
        M("averageAnalystRating", "Average rating", "text"),
        M("numberOfAnalystOpinions", "Analysts", "int"),
        M("targetMeanPrice", "Target (mean)", "money"),
        M("targetMedianPrice", "Target (median)", "money"),
        M("targetHighPrice", "Target (high)", "money"),
        M("targetLowPrice", "Target (low)", "money"),
        M("target_upside", "Upside to mean target", "pct", True),
     ]},
    {"id": "events", "label": "Earnings & events", "preset": True,
     "desc": "Earnings date, fiscal year, splits and listing date", "metrics": [
        M("earningsTimestamp", "Earnings date", "date"),
        M("mostRecentQuarter", "Latest quarter", "date"),
        M("lastFiscalYearEnd", "Last fiscal year end", "date"),
        M("nextFiscalYearEnd", "Next fiscal year end", "date"),
        M("lastSplitFactor", "Last split", "text"),
        M("lastSplitDate", "Last split date", "date"),
        M("firstTradeDateMilliseconds", "First trade date", "date_ms"),
     ]},
    {"id": "company", "label": "Company", "preset": True,
     "desc": "Name, sector, industry, location and employees", "metrics": [
        M("longName", "Name", "text"),
        M("sector", "Sector", "text"),
        M("industry", "Industry", "text"),
        M("country", "Country", "text"),
        M("city", "City", "text"),
        M("fullTimeEmployees", "Employees", "int"),
        M("exchange", "Exchange", "text"),
        M("quoteType", "Type", "text"),
        M("currency", "Currency", "text"),
        M("website", "Website", "text"),
     ]},
    {"id": "risk", "label": "Governance risk", "preset": True,
     "desc": "ISS governance scores, 1 (low risk) to 10 (high risk)", "metrics": [
        M("overallRisk", "Overall", "int"),
        M("auditRisk", "Audit", "int"),
        M("boardRisk", "Board", "int"),
        M("compensationRisk", "Compensation", "int"),
        M("shareHolderRightsRisk", "Shareholder rights", "int"),
     ]},
    {"id": "borrow", "label": "Borrow (IBKR)", "preset": False,
     "desc": "Borrow data from Interactive Brokers, for mixing into custom tabs", "metrics": [
        M("ibkr_fee", "Borrow fee / yr", "pct100"),
        M("ibkr_rebate", "Rebate / yr", "pct100"),
        M("ibkr_shares", "Available to short", "compact"),
        M("ibkr_avail", "Availability", "avail"),
     ]},
]

COMPUTED = {"short_change", "yield_calc", "target_upside"}
PERF_KEYS = {m["key"] for g in CATALOG if g["id"] == "performance" for m in g["metrics"]}
INFO_KEYS = [m["key"] for g in CATALOG for m in g["metrics"]
             if m["key"] not in COMPUTED | PERF_KEYS and not m["key"].startswith("ibkr_")]

INFO_MAX_AGE = 15 * 60
PERF_MAX_AGE = 30 * 60
ERROR_MAX_AGE = 2 * 60
YAHOO_CALLS_PER_MIN = 80
METRIC_WAIT_SEC = 8


def clean_value(v):
    if isinstance(v, bool) or v is None:
        return v
    if hasattr(v, "item"):                  # numpy number
        try:
            v = v.item()
        except Exception:
            return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, str):
        return v[:200]
    return None


def ratio_change(a, b):
    a, b = to_float(a), to_float(b)
    return a / b - 1 if a is not None and b else None


def fetch_info(sym):
    info = yf.Ticker(sym.replace(".", "-")).info or {}
    if len(info) < 5:
        return {"_error": "Yahoo has no data for this ticker"}
    out = {k: clean_value(info.get(k)) for k in INFO_KEYS}
    price = to_float(info.get("currentPrice")) or to_float(info.get("regularMarketPrice"))
    if out.get("currentPrice") is None:
        out["currentPrice"] = price
    if out.get("regularMarketVolume") is None:
        out["regularMarketVolume"] = clean_value(info.get("volume"))
    out["short_change"] = ratio_change(info.get("sharesShort"), info.get("sharesShortPriorMonth"))
    rate = to_float(info.get("dividendRate"))
    out["yield_calc"] = rate / price if rate and price else None
    out["target_upside"] = ratio_change(info.get("targetMeanPrice"), price)
    return out


def fetch_perf(sym):
    import pandas as pd
    hist = yf.Ticker(sym.replace(".", "-")).history(period="10y", interval="1d", auto_adjust=True)
    close = hist["Close"].dropna() if "Close" in hist else []
    if len(close) < 2:
        return {"_error": "No price history"}
    idx, last_dt, last = close.index, close.index[-1], float(close.iloc[-1])

    def back(**kw):
        target = last_dt - pd.DateOffset(**kw)
        if idx[0] > target:
            return None
        return last / float(close[idx <= target].iloc[-1]) - 1

    ytd_start = pd.Timestamp(year=last_dt.year, month=1, day=1, tz=last_dt.tz)
    before = close[idx < ytd_start]

    year = close[idx >= last_dt - pd.DateOffset(years=1)]
    rets = close.pct_change().dropna().iloc[-21:]
    delta = close.diff().dropna()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    hi, lo = float(year.max()), float(year.min())

    out = {
        "perf_1d": last / float(close.iloc[-2]) - 1,
        "perf_5d": last / float(close.iloc[-6]) - 1 if len(close) >= 6 else None,
        "perf_1m": back(months=1), "perf_3m": back(months=3), "perf_6m": back(months=6),
        "perf_ytd": last / float(before.iloc[-1]) - 1 if len(before) else None,
        "perf_1y": back(years=1), "perf_3y": back(years=3), "perf_5y": back(years=5),
        "range_pos": (last - lo) / (hi - lo) if hi > lo else None,
        "vol_30d": float(rets.std()) * math.sqrt(252) if len(rets) >= 10 else None,
        "max_dd_1y": float((year / year.cummax() - 1).min()),
        "rsi_14": 100.0 if loss == 0 else 100 - 100 / (1 + float(gain) / float(loss)),
    }
    return {k: clean_value(v) for k, v in out.items()}


class Metrics:
    """Fetches yfinance data in the background and caches it for every visitor."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {"info": {}, "perf": {}}
        self.inflight = {}
        self.calls = deque()
        self.pool = ThreadPoolExecutor(max_workers=6)

    def _allow(self):
        now = time.time()
        with self.lock:
            while self.calls and now - self.calls[0] > 60:
                self.calls.popleft()
            if len(self.calls) >= YAHOO_CALLS_PER_MIN:
                return False
            self.calls.append(now)
            return True

    def _run(self, kind, sym):
        try:
            data = (fetch_info if kind == "info" else fetch_perf)(sym)
        except Exception:
            data = {"_error": "Couldn't get data from Yahoo right now"}
        with self.lock:
            self.cache[kind][sym] = (time.time(), data)
            self.inflight.pop((kind, sym), None)

    def _fresh(self, kind, sym):
        hit = self.cache[kind].get(sym)
        if not hit:
            return None, True
        ts, data = hit
        max_age = ERROR_MAX_AGE if "_error" in data else (INFO_MAX_AGE if kind == "info" else PERF_MAX_AGE)
        return data, time.time() - ts > max_age

    def _start(self, kind, sym):
        with self.lock:
            fut = self.inflight.get((kind, sym))
        if fut or not self._allow():
            return fut
        fut = self.pool.submit(self._run, kind, sym)
        with self.lock:
            self.inflight[(kind, sym)] = fut
        return fut

    def get(self, symbols, kinds):
        waiting = []
        for s in symbols:
            for kind in kinds:
                with self.lock:
                    data, stale = self._fresh(kind, s)
                if stale:
                    fut = self._start(kind, s)
                    if data is None and fut is not None:
                        waiting.append(fut)       # nothing to show yet: wait a bit
        if waiting:
            wait(waiting, timeout=METRIC_WAIT_SEC)

        rows, pending = [], 0
        for s in symbols:
            merged, error, missing = {}, None, False
            for kind in kinds:
                with self.lock:
                    data, _ = self._fresh(kind, s)
                if data is None:
                    missing = True
                elif "_error" in data:
                    error = error or data["_error"]
                else:
                    merged.update(data)
            pending += missing
            rows.append({"symbol": s, "data": merged, "pending": missing,
                         "error": error if not merged else None})
        return rows, pending


METRICS = Metrics()


# ------------------------------ web server --------------------------------
def rate_param(query, key, default):
    """A % / yr rate from the query string, between 0 and 100."""
    v = to_float(query.get(key, [""])[0])
    return default if v is None or not math.isfinite(v) or not 0 <= v <= 100 else v


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

    def _json(self, obj):
        self._send(200, json.dumps(obj, separators=(",", ":")))

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
        if path in ("/api/etf", "/api/etf/holdings"):
            if ETF is None:
                return self._json({"error": "yfinance is not installed on the server."})
            gc = rate_param(query, "gc", etf_mod.GC_DEFAULT)
            cash = rate_param(query, "cash", etf_mod.CASH_DEFAULT)
            if path == "/api/etf/holdings":
                sym = normalize(query.get("symbol", [""])[0])
                if not sym:
                    return self._json({"error": "Give one ETF ticker, e.g. ?symbol=TSLL"})
                return self._json(ETF.holdings_detail(sym, gc, cash))
            raw = ",".join(query.get("symbols", [""]))
            syms = list(dict.fromkeys(s for s in (normalize(x) for x in raw.split(",")) if s))
            start, start_error = etf_mod.parse_start_date(query.get("start", [""])[0])
            out = ETF.report(syms[:MAX_SYMBOLS], gc, cash, start)
            out["start_error"] = start_error
            return self._json(out)
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if path == "/api/config":
            return self._json({"default_tickers": DEFAULT_TICKERS,
                               "default_etfs": DEFAULT_ETFS,
                               "gc_default": etf_mod.GC_DEFAULT,
                               "cash_default": etf_mod.CASH_DEFAULT,
                               "prices_enabled": PRICES_ON,
                               "limited_below": LIMITED_BELOW})

        if path == "/api/quotes":
            BORROW.ensure_fresh()
            raw = ",".join(query.get("symbols", [""]))
            syms = list(dict.fromkeys(s for s in (normalize(x) for x in raw.split(",")) if s))
            syms = syms[:MAX_SYMBOLS]
            prices = PRICES.get(syms)
            table, _, as_of, error, refreshing = BORROW.snapshot()
            rows = []
            for s in syms:
                b, p = table.get(s), prices.get(s, {})
                rows.append({
                    "symbol": s,
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
            return self._json({"groups": groups, "yahoo": PRICES_ON})

        if path == "/api/metrics":
            if not PRICES_ON:
                return self._json({"rows": [], "pending": 0})
            raw = ",".join(query.get("symbols", [""]))
            syms = list(dict.fromkeys(s for s in (normalize(x) for x in raw.split(",")) if s))
            kinds = ["info"] + (["perf"] if query.get("perf", ["0"])[0] == "1" else [])
            rows, pending = METRICS.get(syms[:MAX_SYMBOLS], kinds)
            return self._json({"rows": rows, "pending": pending})

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
    server.serve_forever()


if __name__ == "__main__":
    main()
