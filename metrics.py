"""
Stock metrics for the Stocks page tabs
--------------------------------------
Every metric in CATALOG comes from Yahoo Finance's main quote data (yfinance's
.info) when Yahoo has it. When it doesn't, the metric falls back on its own:
price history, financial statements, dividend and split history, analyst data,
holders, the earnings calendar, share-count history or SEC filings. When there's
still no value, a reason says why (a fund has no P/E, a company pays no dividend,
the market is closed...).

Sources are fetched only when a metric on the open tab needs them, each is cached
separately, and every Yahoo call goes through the shared rate limiter (common.py).
"""

import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from common import LRU, YahooBusy, is_rate_limit, yahoo_call


# ------------------------------- catalog -----------------------------------
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

GROUP_OF = {m["key"]: g["id"] for g in CATALOG for m in g["metrics"]}
YAHOO_KEYS = [k for k, g in GROUP_OF.items() if g != "borrow"]
PERF_KEYS = {m["key"] for g in CATALOG if g["id"] == "performance" for m in g["metrics"]}
ALWAYS_CALCULATED = PERF_KEYS | {"short_change", "yield_calc", "target_upside"}

# Why a metric has no value. code -> (short word shown in the cell, full reason)
REASONS = {
    "fund": ("n/a", "Doesn't apply to funds"),
    "bank": ("n/a", "Doesn't apply to banks/insurers"),
    "no_dividend": ("none", "No dividend"),
    "no_splits": ("none", "No splits"),
    "neg_earnings": ("n/a", "Negative earnings"),
    "no_growth": ("n/a", "Earnings aren't growing"),
    "closed": ("closed", "Market closed: no live quote"),
    "no_pre": ("none", "No pre-market trading yet"),
    "no_post": ("none", "No after-hours trading yet"),
    "history": ("n/a", "Not enough history"),
    "no_analysts": ("none", "No analyst coverage"),
    "no_iss": ("none", "Not rated by ISS"),
    "no_short": ("–", "No short-interest report"),
    "unreachable": ("–", "Couldn't reach Yahoo just now; retrying"),
    "no_data": ("–", "Yahoo has no data for this"),
}
# Where a value came from. source -> (label, calculated by this site?)
SOURCES = {
    "yahoo": ("Yahoo Finance", False),
    "history": ("Calculated from Yahoo's price history", True),
    "intraday": ("Yahoo intraday prices", False),
    "statements": ("Calculated from Yahoo's financial statements", True),
    "dividends": ("Calculated from Yahoo's dividend history", True),
    "splits": ("Yahoo's split history", False),
    "analysts": ("Yahoo analyst data", False),
    "holders": ("Yahoo holders data", False),
    "calendar": ("Yahoo earnings calendar", False),
    "shares": ("Yahoo share-count history", False),
    "sec": ("SEC filings", False),
    "calculated": ("Calculated from other values", True),
}

TTL = {"info": 15 * 60, "hist": 30 * 60, "intraday": 2 * 60, "statements": 12 * 3600,
       "analysts": 12 * 3600, "holders": 12 * 3600, "calendar": 6 * 3600, "shares": 12 * 3600,
       "sec": 24 * 3600}
CACHE_LIMITS = {"hist": 300, "intraday": 100, "sec": 60}     # others: 400 tickers
ERROR_MAX_AGE = 2 * 60
WAIT_SEC = 8
FUND_TYPES = {"ETF", "MUTUALFUND", "MONEYMARKET"}
FUND_SKIP_SOURCES = {"statements", "analysts", "sec"}
FUND_NA_GROUPS = {"valuation", "profitability", "growth", "financials", "analysts", "risk"}
FUND_NA_KEYS = {"earningsTimestamp", "mostRecentQuarter", "lastFiscalYearEnd", "nextFiscalYearEnd",
                "country", "city", "fullTimeEmployees", "website", "floatShares", "payoutRatio"}
BANK_NA = {"grossMargins", "ebitdaMargins", "ebitda", "grossProfits", "enterpriseToEbitda",
           "currentRatio", "quickRatio"}
SHORT_REPORT_KEYS = {"sharesShort", "sharesShortPriorMonth", "dateShortInterest", "shortPercentOfFloat",
                     "shortRatio", "sharesPercentSharesOut", "short_change"}
# Days of history a metric needs; below this the reason is "Not enough history".
HISTORY_DAYS = {"perf_1d": 2, "perf_5d": 8, "perf_1m": 31, "perf_3m": 92, "perf_6m": 183, "perf_1y": 366,
                "perf_3y": 3 * 366, "perf_5y": 5 * 366, "range_pos": 28, "vol_30d": 28, "max_dd_1y": 28,
                "rsi_14": 21, "beta": 2 * 366, "fiftyDayAverage": 72, "twoHundredDayAverage": 290,
                "fiftyDayAverageChangePercent": 72, "twoHundredDayAverageChangePercent": 290}
RATING = [(1.5, "strong_buy", "Strong Buy"), (2.5, "buy", "Buy"), (3.5, "hold", "Hold"),
          (4.5, "underperform", "Underperform"), (99, "sell", "Sell")]


# ------------------------------- helpers -----------------------------------
def to_float(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


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


def norm_row(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def epoch(ts):
    """Epoch seconds from a pandas Timestamp / date / int."""
    import pandas as pd
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return int(ts)
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return int(t.timestamp())


def ysym(sym):
    return sym.replace(".", "-")


def fmt_ratio(r):
    s = f"{r:.4f}".rstrip("0").rstrip(".")
    return s


def split_text(r):
    return f"{fmt_ratio(r)}:1" if r >= 1 else f"1:{fmt_ratio(1 / r)}"


# ------------------------------- fetchers ----------------------------------
# Each returns a small dict (cached per ticker) or raises. Every Yahoo request
# goes through yahoo_call, which waits for a slot in the shared rate limiter.
def fetch_info(yf, sym):
    info = yahoo_call(lambda: yf.Ticker(ysym(sym)).info) or {}
    clean = {k: clean_value(v) for k, v in info.items()}
    clean = {k: v for k, v in clean.items() if v is not None and v != ""}
    return {"info": clean, "empty": len(clean) < 5}


def _naive(index):
    idx = index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    return idx


def fetch_hist(yf, sym):
    import pandas as pd
    t = yf.Ticker(ysym(sym))
    h = yahoo_call(lambda: t.history(period="max", interval="1d", auto_adjust=False, actions=True))
    if h is None or h.empty or "Close" not in h:
        return {"_error": "Yahoo has no price history for this ticker"}
    try:
        meta = dict(t.history_metadata or {})
    except Exception:
        meta = {}
    h = h.copy()
    h.index = _naive(h.index).normalize()
    h = h[~h.index.duplicated(keep="last")].sort_index()
    first = h.index[0]
    divs = h["Dividends"][h["Dividends"] != 0] if "Dividends" in h else pd.Series(dtype=float)
    splits = h["Stock Splits"][h["Stock Splits"] != 0] if "Stock Splits" in h else pd.Series(dtype=float)
    if "Adj Close" not in h:
        h["Adj Close"] = h["Close"]
    bars = h[["Open", "High", "Low", "Close", "Adj Close", "Volume"]]
    bars = bars[bars.index >= bars.index[-1] - pd.DateOffset(years=6, days=40)].astype(float)
    keep = {}
    for k in ("currency", "exchangeName", "fullExchangeName", "instrumentType", "regularMarketPrice",
              "regularMarketDayHigh", "regularMarketDayLow", "regularMarketVolume", "fiftyTwoWeekHigh",
              "fiftyTwoWeekLow", "longName", "shortName", "exchangeTimezoneName"):
        v = clean_value(meta.get(k))
        if v is not None:
            keep[k] = v
    for k in ("firstTradeDate", "regularMarketTime"):
        if meta.get(k) is not None:
            try:
                keep[k] = epoch(meta[k])
            except Exception:
                pass
    return {"bars": bars, "divs": divs.astype(float), "splits": splits.astype(float),
            "first": first, "meta": keep}


def _session_bounds(meta):
    """(open, close) time of day of the regular session, from Yahoo's metadata."""
    import pandas as pd
    reg = ((meta or {}).get("currentTradingPeriod") or {}).get("regular") or {}
    try:
        start, end = pd.Timestamp(reg["start"]), pd.Timestamp(reg["end"])
        return start.time(), end.time()
    except Exception:
        return pd.Timestamp("09:30").time(), pd.Timestamp("16:00").time()


def fetch_intraday(yf, sym):
    t = yf.Ticker(ysym(sym))
    h = yahoo_call(lambda: t.history(period="5d", interval="5m", prepost=True))
    try:
        meta = dict(t.history_metadata or {})
    except Exception:
        meta = {}
    out = {"pre": None, "post": None}
    if h is None or h.empty:
        return out
    opens, closes = _session_bounds(meta)
    h = h.dropna(subset=["Close"])
    days = sorted({ts.date() for ts in h.index})
    last_day = days[-1]
    for ts, close in h["Close"].items():
        tod = ts.time()
        if ts.date() == last_day and tod < opens:
            out["pre"] = (float(close), ts.strftime("%Y-%m-%d %H:%M %Z").strip())
        if ts.date() in days[-2:] and tod >= closes:
            out["post"] = (float(close), ts.strftime("%Y-%m-%d %H:%M %Z").strip())
    return out


def _table(df):
    """Statement DataFrame -> {normalized row: [(date, value), ...] newest first}."""
    import pandas as pd
    out = {}
    if df is None or getattr(df, "empty", True):
        return out
    cols = sorted(df.columns, key=lambda c: pd.Timestamp(c), reverse=True)
    for name, row in df.iterrows():
        vals = [(pd.Timestamp(c), to_float(row[c])) for c in cols]
        vals = [(d, v) for d, v in vals if v is not None]
        if vals:
            out[norm_row(name)] = vals
    return out


def fetch_statements(yf, sym):
    t = yf.Ticker(ysym(sym))
    out = {}
    for key, attr in (("qi", "quarterly_income_stmt"), ("qb", "quarterly_balance_sheet"),
                      ("qc", "quarterly_cashflow"), ("ai", "income_stmt")):
        try:
            out[key] = _table(yahoo_call(lambda a=attr: getattr(t, a)))
        except YahooBusy:
            raise
        except Exception as e:
            if is_rate_limit(e):
                raise
            out[key] = {}
    if not any(out.values()):
        return {"_error": "Yahoo has no financial statements for this ticker"}
    return out


def fetch_analysts(yf, sym):
    t = yf.Ticker(ysym(sym))
    out = {"targets": {}, "counts": {}, "est": {}}
    try:
        targets = yahoo_call(lambda: t.analyst_price_targets) or {}
        out["targets"] = {k: to_float(v) for k, v in targets.items() if to_float(v) is not None}
    except Exception as e:
        if is_rate_limit(e) or isinstance(e, YahooBusy):
            raise
    try:
        rec = yahoo_call(lambda: t.recommendations_summary)
        if rec is not None and not rec.empty:
            row = rec[rec["period"] == "0m"].iloc[0] if "period" in rec and (rec["period"] == "0m").any() else rec.iloc[0]
            out["counts"] = {k: int(row[k]) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")
                             if k in row and to_float(row[k]) is not None}
    except Exception as e:
        if is_rate_limit(e) or isinstance(e, YahooBusy):
            raise
    try:
        est = yahoo_call(lambda: t.earnings_estimate)
        if est is not None and not est.empty and "avg" in est:
            out["est"] = {p: to_float(est.loc[p, "avg"]) for p in est.index if to_float(est.loc[p, "avg"]) is not None}
    except Exception as e:
        if is_rate_limit(e) or isinstance(e, YahooBusy):
            raise
    return out


def fetch_holders(yf, sym):
    df = yahoo_call(lambda: yf.Ticker(ysym(sym)).major_holders)
    out = {}
    if df is not None and not df.empty:
        if "Value" in df.columns:
            for k, v in df["Value"].items():
                out[str(k)] = to_float(v)
        else:                                   # older layout: ["7.05%", "% of Shares Held by All Insider"]
            for _, row in df.iterrows():
                text, value = str(row.iloc[1]), to_float(str(row.iloc[0]).rstrip("%"))
                if value is None:
                    continue
                if "Insider" in text:
                    out["insidersPercentHeld"] = value / 100
                elif "Institutions" in text and "Float" not in text:
                    out["institutionsPercentHeld"] = value / 100
    return out


def fetch_calendar(yf, sym):
    cal = yahoo_call(lambda: yf.Ticker(ysym(sym)).calendar) or {}
    out = {"earnings": [], "exdiv": None}
    for d in cal.get("Earnings Date") or []:
        try:
            out["earnings"].append(epoch(d))
        except Exception:
            pass
    if cal.get("Ex-Dividend Date"):
        try:
            out["exdiv"] = epoch(cal["Ex-Dividend Date"])
        except Exception:
            pass
    return out


def fetch_shares(yf, sym):
    import pandas as pd
    start = (pd.Timestamp.today() - pd.DateOffset(months=18)).strftime("%Y-%m-%d")
    s = yahoo_call(lambda: yf.Ticker(ysym(sym)).get_shares_full(start=start))
    if s is None or len(s) == 0:
        return {"shares": None}
    s = s.dropna()
    return {"shares": to_float(s.iloc[-1]) if len(s) else None,
            "as_of": str(s.index[-1])[:10] if len(s) else None}


class SecFacts:
    """Latest annual figures from SEC XBRL company facts (needs SEC_USER_AGENT)."""

    def __init__(self, client, enabled):
        self.client, self.enabled = client, enabled
        self.ciks, self.ciks_at = None, 0.0
        self.lock = threading.Lock()

    def cik(self, sym):
        with self.lock:
            if self.ciks is None or time.time() - self.ciks_at > 86400:
                d = json.loads(self.client.get("https://www.sec.gov/files/company_tickers.json"))
                self.ciks = {str(r["ticker"]).upper(): int(r["cik_str"]) for r in d.values()}
                self.ciks_at = time.time()
        return self.ciks.get(sym.upper()) or self.ciks.get(ysym(sym).upper())

    def fetch(self, sym):
        cik = self.cik(sym)
        if not cik:
            return {"_error": "Not an SEC filer"}
        facts = json.loads(self.client.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"))
        f = facts.get("facts", {})

        def latest(ns, names, unit, annual):
            best = None
            for n in names:
                for e in ((f.get(ns, {}).get(n) or {}).get("units", {}).get(unit) or []):
                    if annual and (e.get("fp") != "FY" or not str(e.get("form", "")).startswith("10-K")):
                        continue
                    if annual and e.get("start") and e.get("end"):
                        days = (time.mktime(time.strptime(e["end"], "%Y-%m-%d")) -
                                time.mktime(time.strptime(e["start"], "%Y-%m-%d"))) / 86400
                        if not 330 <= days <= 400:
                            continue
                    if best is None or e.get("end", "") > best.get("end", ""):
                        best = e
            return (to_float(best["val"]), best.get("end")) if best else (None, None)

        out = {}
        out["shares"] = latest("dei", ["EntityCommonStockSharesOutstanding"], "shares", False)
        out["revenue"] = latest("us-gaap", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                                            "SalesRevenueNet"], "USD", True)
        out["net_income"] = latest("us-gaap", ["NetIncomeLoss"], "USD", True)
        out["ocf"] = latest("us-gaap", ["NetCashProvidedByUsedInOperatingActivities"], "USD", True)
        out["cash"] = latest("us-gaap", ["CashAndCashEquivalentsAtCarryingValue"], "USD", False)
        return out


# ------------------------------- resolver ----------------------------------
class Ctx:
    """Works out every metric for one ticker from whatever sources are loaded.

    source(name) returns a source's data, or None when it isn't loaded yet (it's
    then requested, and every metric being worked out is marked as loading) or
    failed (those metrics may get the "couldn't reach Yahoo" reason).
    """

    def __init__(self, sym, sources, spy=None, sec_enabled=False):
        self.sym, self.sources, self.spy_data, self.sec_enabled = sym, sources, spy, sec_enabled
        self.memo, self.stack = {}, []
        self.needs, self.used = set(), set()
        self.pending_keys, self.failed_keys = set(), set()

    # ---- sources
    def source(self, name):
        if name in FUND_SKIP_SOURCES and self.is_fund():
            return None                      # funds have no statements, analysts or SEC company facts
        self.used.add(name)
        d = self.sources.get(name)
        if d is None:
            self.needs.add(name)
            self.pending_keys.update(self.stack)
            return None
        if "_error" in d:
            self.failed_keys.update(self.stack)
            return None
        return d

    def info(self, *names):
        d = self.source("info")
        if not d:
            return None
        for n in names:
            v = d["info"].get(n)
            if v is not None and v != "":
                return v
        return None

    def hist(self):
        return self.source("hist")

    def bars(self):
        h = self.hist()
        return h["bars"] if h else None

    def meta(self, key):
        h = self.hist()
        return h["meta"].get(key) if h else None

    def spy(self):
        if self.sym == "SPY":
            return self.hist()
        self.used.add("spy")
        if self.spy_data is None:
            self.needs.add("spy")
            self.pending_keys.update(self.stack)
            return None
        return None if "_error" in self.spy_data else self.spy_data

    def stmt(self, part):
        s = self.source("statements")
        return s.get(part) if s else None

    def sec(self, field):
        if not self.sec_enabled or self.is_fund():
            return None
        s = self.source("sec")
        v = (s or {}).get(field)
        return v[0] if v else None

    # ---- values
    def v(self, key):
        if key in self.memo:
            if key in self.pending_keys:
                self.pending_keys.update(self.stack)
            if key in self.failed_keys:
                self.failed_keys.update(self.stack)
            return self.memo[key][0]
        if key in self.stack:
            return None
        self.stack.append(key)
        result = (None, None, None)
        try:
            for src, fn in STRATEGIES.get(key, []):
                try:
                    out = fn(self)
                except (ArithmeticError, TypeError, ValueError, KeyError, IndexError, AttributeError):
                    out = None
                note = None
                if isinstance(out, tuple):
                    out, note = out
                if isinstance(out, float) and not math.isfinite(out):
                    out = None
                if out is not None:
                    result = (out, src, note)
                    break
        finally:
            self.stack.pop()
        self.memo[key] = result
        return result[0]

    def price(self):
        return self.v("currentPrice")

    # ---- facts used for reasons
    def quote_type(self):
        return self.info("quoteType") or self.meta("instrumentType")

    def is_fund(self):
        return (self.quote_type() or "").upper() in FUND_TYPES

    def is_financial(self):
        sector, industry = self.info("sector") or "", self.info("industry") or ""
        return sector == "Financial Services" and bool(
            re.search(r"Bank|Insurance|Capital Markets|Credit|Mortgage|Asset Management|Financial", industry))

    def no_dividends(self):
        h = self.hist()
        if not h:
            return False
        divs = h["divs"]
        return len(divs) == 0 or (h["bars"].index[-1] - divs.index[-1]).days > 400

    def no_splits(self):
        h = self.hist()
        return bool(h) and len(h["splits"]) == 0

    def listed(self):
        h = self.hist()
        return h["first"].strftime("%Y-%m-%d") if h else None

    def too_young(self, key):
        h = self.hist()
        if not h:
            return False
        last, first = h["bars"].index[-1], h["first"]
        if key == "perf_ytd":
            return first.year == last.year
        return (last - first).days < HISTORY_DAYS[key]

    def resolve(self, keys):
        data, src, why, note, loading = {}, {}, {}, {}, []
        for k in keys:
            value = self.v(k)
            _, s, n = self.memo[k]
            if value is not None:
                data[k], src[k] = value, s
                if n:
                    note[k] = n
            elif k in self.pending_keys:
                loading.append(k)
            else:
                code, detail = reason_for(k, self)
                why[k] = code
                if detail:
                    note[k] = detail
        return {"data": data, "src": src, "why": why, "note": note, "loading": loading}


def nonpositive(x):
    return isinstance(x, (int, float)) and x <= 0


def reason_for(k, c):
    group = GROUP_OF.get(k)
    info_failed = "_error" in (c.sources.get("info") or {})
    if k in ("bid", "ask", "bidSize", "askSize"):
        if info_failed:
            return "unreachable", None
        return ("closed", None) if c.info("marketState") != "REGULAR" else ("no_data", None)
    if k == "preMarketPrice":
        return "no_pre", None
    if k == "postMarketPrice":
        return "no_post", None
    if c.is_fund() and (group in FUND_NA_GROUPS or k in FUND_NA_KEYS):
        return "fund", None
    if group == "dividends" and c.no_dividends():
        return "no_dividend", None
    if k in ("lastSplitFactor", "lastSplitDate") and c.no_splits():
        return "no_splits", None
    if (k in HISTORY_DAYS or k == "perf_ytd") and c.too_young(k):
        return "history", f"Listed {c.listed()}"
    # these were already worked out while trying the metric, so no new fetches happen here
    if k in ("trailingPE", "trailingPegRatio", "payoutRatio") and nonpositive(c.v("trailingEps")):
        return "neg_earnings", None
    if k == "forwardPE" and nonpositive(c.v("forwardEps")):
        return "neg_earnings", None
    if k == "trailingPegRatio" and nonpositive(c.v("earningsGrowth")):
        return "no_growth", None
    if c.is_financial() and k in BANK_NA:
        return "bank", None
    if k in c.failed_keys and not c.sources.get("info", {}).get("info"):
        return "unreachable", None
    if group == "analysts" or k in ("forwardEps", "epsCurrentYear", "forwardPE"):
        return "no_analysts", None
    if group == "risk":
        return "no_iss", None
    if k in SHORT_REPORT_KEYS:
        return "no_short", None
    if k in c.failed_keys:
        return "unreachable", None
    return "no_data", None


# ---- strategy helpers -------------------------------------------------------
def I(*names, nonzero=False, positive=False):
    """The first of these .info fields that Yahoo returned."""
    def fn(c):
        v = c.info(*names)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if nonzero and v == 0:
                return None
            if positive and v <= 0:
                return None
        return v
    fn.info_names = names
    return fn


def META(name, positive=False):
    def fn(c):
        v = c.meta(name)
        return None if v is None or (positive and isinstance(v, (int, float)) and v <= 0) else v
    return fn


def div(a, b, positive_b=True):
    if a is None or b is None or b == 0 or (positive_b and b < 0):
        return None
    return a / b


def latest_session_bar(c):
    """Today's bar, when the newest daily bar is the latest session."""
    b = c.bars()
    if b is None or b.empty:
        return None
    t = c.meta("regularMarketTime")
    if t is not None:
        import pandas as pd
        tz = c.meta("exchangeTimezoneName") or "America/New_York"
        day = pd.Timestamp(t, unit="s", tz="UTC").tz_convert(tz).normalize().tz_localize(None)
        if b.index[-1] != day:
            return None
    return b.iloc[-1]


def bar_field(col):
    def fn(c):
        bar = latest_session_bar(c)
        return to_float(bar[col]) if bar is not None else None
    return fn


def h_prev_close(c):
    b = c.bars()
    if b is None or len(b) < 2:
        return None
    if latest_session_bar(c) is not None:
        return float(b["Close"].iloc[-2]), "Close before the latest session"
    return float(b["Close"].iloc[-1]), "Latest close"


def h_last_close(c):
    b = c.bars()
    return (float(b["Close"].iloc[-1]), f"Last close, {b.index[-1]:%Y-%m-%d}") if b is not None and len(b) else None


def h_mean(col, n):
    def fn(c):
        b = c.bars()
        if b is None or len(b[col].dropna()) < n:
            return None
        return float(b[col].dropna().iloc[-n:].mean()), f"Average of the last {n} trading days"
    return fn


def h_year(col, how):
    def fn(c):
        import pandas as pd
        b = c.bars()
        if b is None or b.empty:
            return None
        y = b[b.index >= b.index[-1] - pd.DateOffset(years=1)][col].dropna()
        if len(y) < 20:
            return None
        return float(y.max() if how == "max" else y.min()), "Over the last 52 weeks of daily prices"
    return fn


def change_vs(base_key):
    return lambda c: (c.price() - c.v(base_key)) / c.v(base_key) if c.v(base_key) else None


def month_ends(series):
    try:
        return series.resample("ME").last()
    except ValueError:                      # pandas before 2.2
        return series.resample("M").last()


def h_beta(c):
    if c.sym == "SPY":
        return 1.0, "SPY itself"
    h, s = c.hist(), c.spy()
    if not h or not s:
        return None
    import pandas as pd
    a = month_ends(h["bars"]["Adj Close"]).pct_change()
    m = month_ends(s["bars"]["Adj Close"]).pct_change()
    df = pd.concat({"a": a, "m": m}, axis=1, join="inner").dropna().iloc[-60:]
    if len(df) < 24 or not df["m"].var() > 0:
        return None
    return float(df["a"].cov(df["m"]) / df["m"].var()), f"{len(df)} months of monthly returns vs SPY (Yahoo's method)"


def perf_values(c):
    """All performance metrics from the dividend-adjusted closes (computed once)."""
    import pandas as pd
    h = c.hist()
    if not h:
        return None
    if "_perf" in h:
        return h["_perf"]
    close = h["bars"]["Adj Close"].dropna()
    out = {}
    if len(close) >= 2:
        idx, last_dt, last = close.index, close.index[-1], float(close.iloc[-1])

        def back(**kw):
            target = last_dt - pd.DateOffset(**kw)
            if h["first"] > target:
                return None
            prior = close[idx <= target]
            return last / float(prior.iloc[-1]) - 1 if len(prior) else None

        before = close[idx < pd.Timestamp(year=last_dt.year, month=1, day=1)]
        year = close[idx >= last_dt - pd.DateOffset(years=1)]
        rets = close.pct_change().dropna().iloc[-21:]
        delta = close.diff().dropna()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1] if len(delta) else None
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1] if len(delta) else None
        hi, lo = float(year.max()), float(year.min())
        out = {
            "perf_1d": last / float(close.iloc[-2]) - 1,
            "perf_5d": last / float(close.iloc[-6]) - 1 if len(close) >= 6 else None,
            "perf_1m": back(months=1), "perf_3m": back(months=3), "perf_6m": back(months=6),
            "perf_ytd": last / float(before.iloc[-1]) - 1 if len(before) else None,
            "perf_1y": back(years=1), "perf_3y": back(years=3), "perf_5y": back(years=5),
            "range_pos": (last - lo) / (hi - lo) if hi > lo else None,
            "vol_30d": float(rets.std()) * math.sqrt(252) if len(rets) >= 10 else None,
            "max_dd_1y": float((year / year.cummax() - 1).min()) if len(year) >= 20 else None,
            "rsi_14": (100.0 if loss == 0 else 100 - 100 / (1 + float(gain) / float(loss)))
            if gain is not None and len(delta) >= 15 else None,
        }
    h["_perf"] = out
    return out


def perf(key):
    return lambda c: (perf_values(c) or {}).get(key)


# ---- financial statements
def rows(c, part, *names):
    t = c.stmt(part)
    if not t:
        return None
    for n in names:
        vals = t.get(norm_row(n))
        if vals:
            return vals
    return None


def ttm(*names, cash_flow=False):
    """Trailing twelve months: the last 4 quarters summed, else the latest fiscal year."""
    def fn(c):
        q = rows(c, "qc" if cash_flow else "qi", *names)
        if q and len(q) >= 4 and (q[0][0] - q[3][0]).days < 300:
            return sum(v for _, v in q[:4]), "Sum of the last 4 quarters"
        if not cash_flow:
            a = rows(c, "ai", *names)
            if a:
                return a[0][1], f"Fiscal year to {a[0][0]:%Y-%m-%d}"
        return None
    return fn


def latest_bs(*names):
    def fn(c):
        b = rows(c, "qb", *names)
        return (b[0][1], f"Balance sheet, {b[0][0]:%Y-%m-%d}") if b else None
    return fn


def year_ago(vals):
    """(latest, a year earlier) from [(date, value)...] newest first."""
    if not vals:
        return None
    d0, v0 = vals[0]
    for d, v in vals[1:]:
        if 300 <= (d0 - d).days <= 430:
            return v0, v
    return None


def yoy(*names):
    def fn(c):
        pair = year_ago(rows(c, "qi", *names))
        if not pair or pair[1] <= 0:
            return None
        return pair[0] / pair[1] - 1, "Latest quarter vs the same quarter a year earlier"
    return fn


def avg_bs(*names):
    def fn(c):
        b = rows(c, "qb", *names)
        if not b:
            return None
        pair = year_ago(b)
        return (pair[0] + pair[1]) / 2 if pair else b[0][1]
    return fn


def fcf(c):
    f = ttm("Free Cash Flow", cash_flow=True)(c)
    if f:
        return f
    ocf, capex = ttm("Operating Cash Flow", cash_flow=True)(c), ttm("Capital Expenditure", cash_flow=True)(c)
    if ocf and capex:
        return ocf[0] + capex[0], "Operating cash flow minus capital spending, last 4 quarters"
    return None


def quick(c):
    ca, cl = latest_bs("Current Assets")(c), latest_bs("Current Liabilities")(c)
    if not ca or not cl or cl[0] <= 0:
        return None
    inv = latest_bs("Inventory")(c)
    return (ca[0] - (inv[0] if inv else 0.0)) / cl[0], cl[1]


def shares(c):
    return c.v("sharesOutstanding")


# ---- dividends and splits
def divs(c):
    h = c.hist()
    return h["divs"] if h else None


def last_bar_day(c):
    b = c.bars()
    return b.index[-1] if b is not None and len(b) else None


def trailing_dividends(c):
    d, today = divs(c), last_bar_day(c)
    if d is None or today is None or c.no_dividends():
        return None
    import pandas as pd
    recent = d[d.index > today - pd.Timedelta(days=365)]
    return float(recent.sum()) if len(recent) else None


def forward_dividend(c):
    d, today = divs(c), last_bar_day(c)
    if d is None or today is None or c.no_dividends():
        return None
    import pandas as pd
    per_year = max(1, len(d[d.index > today - pd.Timedelta(days=365)]))
    return float(d.iloc[-1]) * per_year, f"Latest payment × {per_year} a year"


def five_year_yield(c):
    h = c.hist()
    if not h or c.no_dividends():
        return None
    d, closes = h["divs"], h["bars"]["Close"]
    last_year = closes.index[-1].year - 1
    yields = []
    for y in range(last_year - 4, last_year + 1):
        paid = float(d[d.index.year == y].sum())
        px = closes[closes.index.year == y]
        if len(px) > 200:
            yields.append(paid / float(px.mean()))
    if not yields:
        return None
    return sum(yields) / len(yields) * 100, f"Average of {len(yields)} calendar years"


def last_div(field):
    def fn(c):
        d = divs(c)
        if d is None or not len(d):
            return None
        return float(d.iloc[-1]) if field == "value" else epoch(d.index[-1])
    return fn


def last_split(field):
    def fn(c):
        h = c.hist()
        if not h or not len(h["splits"]):
            return None
        r = float(h["splits"].iloc[-1])
        return split_text(r) if field == "factor" else epoch(h["splits"].index[-1])
    return fn


# ---- analysts
def rec_counts(c):
    a = c.source("analysts")
    counts = (a or {}).get("counts") or {}
    n = sum(counts.values())
    return (counts, n) if n else (None, 0)


def rec_mean(c):
    counts, n = rec_counts(c)
    if not counts:
        return None
    w = {"strongBuy": 1, "buy": 2, "hold": 3, "sell": 4, "strongSell": 5}
    return sum(w[k] * v for k, v in counts.items()) / n, f"From {n} analyst ratings this month"


def rating(field):
    def fn(c):
        mean = c.v("recommendationMean")
        if mean is None:
            return None
        for top, key, label in RATING:
            if mean < top:
                return key if field == "key" else f"{round(mean + 1e-9, 1):.1f} - {label}"
    return fn


def target(name):
    def fn(c):
        a = c.source("analysts")
        return ((a or {}).get("targets") or {}).get(name)
    return fn


def estimate(period):
    def fn(c):
        a = c.source("analysts")
        v = ((a or {}).get("est") or {}).get(period)
        return (v, f"Analysts' average estimate ({period})") if v is not None else None
    return fn


def holder(name):
    def fn(c):
        h = c.source("holders")
        return (h or {}).get(name)
    return fn


def fund_info(name):
    return lambda c: c.info(name) if c.is_fund() else None


def next_fiscal_year(c):
    last = c.v("lastFiscalYearEnd")
    if last is None:
        return None
    import pandas as pd
    return epoch(pd.Timestamp(last, unit="s") + pd.DateOffset(years=1)), "A year after the last fiscal year end"


def earnings_from_calendar(c):
    cal = c.source("calendar")
    dates = (cal or {}).get("earnings") or []
    if not dates:
        return None
    now = time.time() - 86400
    future = [d for d in dates if d >= now]
    return (future or dates)[0]


def statement_date(part):
    def fn(c):
        t = c.stmt(part)
        if not t:
            return None
        dates = [vals[0][0] for vals in t.values() if vals]
        return epoch(max(dates)) if dates else None
    return fn


def intraday(field):
    def fn(c):
        d = c.source("intraday")
        hit = (d or {}).get(field)
        return (hit[0], f"As of {hit[1]}") if hit else None
    return fn


def calc(f, note=None):
    def fn(c):
        v = f(c)
        return (v, note) if v is not None and note else v
    return fn


Y = "yahoo"
STRATEGIES = {
    # short interest
    "shortPercentOfFloat": [(Y, I("shortPercentOfFloat")),
                            ("calculated", calc(lambda c: div(c.v("sharesShort"), c.v("floatShares")), "Shares short ÷ float"))],
    "sharesShort": [(Y, I("sharesShort"))],
    "sharesShortPriorMonth": [(Y, I("sharesShortPriorMonth"))],
    "short_change": [("calculated", lambda c: (lambda a, b: a / b - 1 if a is not None and b else None)(
        c.v("sharesShort"), c.v("sharesShortPriorMonth")))],
    "shortRatio": [(Y, I("shortRatio")),
                   ("calculated", calc(lambda c: div(c.v("sharesShort"), c.v("averageVolume")),
                                       "Shares short ÷ 3-month average daily volume"))],
    "sharesPercentSharesOut": [(Y, I("sharesPercentSharesOut")),
                               ("calculated", calc(lambda c: div(c.v("sharesShort"), c.v("sharesOutstanding")),
                                                   "Shares short ÷ shares outstanding"))],
    "floatShares": [(Y, I("floatShares"))],
    "sharesOutstanding": [(Y, I("sharesOutstanding", positive=True)),
                          ("shares", lambda c: (lambda d: (d["shares"], f"As of {d['as_of']}") if d and d.get("shares") else None)(c.source("shares"))),
                          ("sec", lambda c: c.sec("shares"))],
    "impliedSharesOutstanding": [(Y, I("impliedSharesOutstanding", positive=True)),
                                 ("calculated", calc(shares, "Same as shares outstanding"))],
    "heldPercentInsiders": [(Y, I("heldPercentInsiders")), ("holders", holder("insidersPercentHeld"))],
    "heldPercentInstitutions": [(Y, I("heldPercentInstitutions")), ("holders", holder("institutionsPercentHeld"))],
    "dateShortInterest": [(Y, I("dateShortInterest"))],
    # price & trading
    "currentPrice": [(Y, I("currentPrice", "regularMarketPrice", positive=True)),
                     ("history", META("regularMarketPrice", positive=True)), ("history", h_last_close)],
    "previousClose": [(Y, I("previousClose", "regularMarketPreviousClose", positive=True)), ("history", h_prev_close)],
    "open": [(Y, I("open", "regularMarketOpen", positive=True)), ("history", bar_field("Open"))],
    "dayLow": [(Y, I("dayLow", "regularMarketDayLow", positive=True)),
               ("history", META("regularMarketDayLow", positive=True)), ("history", bar_field("Low"))],
    "dayHigh": [(Y, I("dayHigh", "regularMarketDayHigh", positive=True)),
                ("history", META("regularMarketDayHigh", positive=True)), ("history", bar_field("High"))],
    "bid": [(Y, I("bid", positive=True))],
    "ask": [(Y, I("ask", positive=True))],
    "bidSize": [(Y, I("bidSize", positive=True))],
    "askSize": [(Y, I("askSize", positive=True))],
    "preMarketPrice": [(Y, I("preMarketPrice", positive=True)), ("intraday", intraday("pre"))],
    "postMarketPrice": [(Y, I("postMarketPrice", positive=True)), ("intraday", intraday("post"))],
    "regularMarketVolume": [(Y, I("regularMarketVolume", "volume")), ("history", META("regularMarketVolume")),
                            ("history", bar_field("Volume"))],
    "averageVolume": [(Y, I("averageVolume", "averageDailyVolume3Month")), ("history", h_mean("Volume", 63))],
    "averageVolume10days": [(Y, I("averageVolume10days", "averageDailyVolume10Day")), ("history", h_mean("Volume", 10))],
    "fiftyTwoWeekLow": [(Y, I("fiftyTwoWeekLow", positive=True)), ("history", META("fiftyTwoWeekLow", positive=True)),
                        ("history", h_year("Low", "min"))],
    "fiftyTwoWeekHigh": [(Y, I("fiftyTwoWeekHigh", positive=True)), ("history", META("fiftyTwoWeekHigh", positive=True)),
                         ("history", h_year("High", "max"))],
    "fiftyTwoWeekLowChangePercent": [(Y, I("fiftyTwoWeekLowChangePercent")),
                                     ("calculated", calc(change_vs("fiftyTwoWeekLow"), "(Price − 52-week low) ÷ low"))],
    "fiftyTwoWeekHighChangePercent": [(Y, I("fiftyTwoWeekHighChangePercent")),
                                      ("calculated", calc(change_vs("fiftyTwoWeekHigh"), "(Price − 52-week high) ÷ high"))],
    "fiftyDayAverage": [(Y, I("fiftyDayAverage", positive=True)), ("history", h_mean("Close", 50))],
    "twoHundredDayAverage": [(Y, I("twoHundredDayAverage", positive=True)), ("history", h_mean("Close", 200))],
    "fiftyDayAverageChangePercent": [(Y, I("fiftyDayAverageChangePercent")),
                                     ("calculated", calc(change_vs("fiftyDayAverage"), "(Price − 50-day average) ÷ average"))],
    "twoHundredDayAverageChangePercent": [(Y, I("twoHundredDayAverageChangePercent")),
                                          ("calculated", calc(change_vs("twoHundredDayAverage"), "(Price − 200-day average) ÷ average"))],
    "beta": [(Y, I("beta", "beta3Year")), ("history", h_beta)],
    # valuation
    "marketCap": [(Y, I("marketCap", positive=True)),
                  ("calculated", calc(lambda c: c.price() * shares(c) if c.price() and shares(c) else None,
                                      "Price × shares outstanding"))],
    "enterpriseValue": [(Y, I("enterpriseValue")),
                        ("calculated", calc(lambda c: c.v("marketCap") + c.v("totalDebt") - c.v("totalCash")
                                            if None not in (c.v("marketCap"), c.v("totalDebt"), c.v("totalCash")) else None,
                                            "Market cap + debt − cash"))],
    "trailingPE": [(Y, I("trailingPE", positive=True)),
                   ("calculated", calc(lambda c: div(c.price(), c.v("trailingEps")), "Price ÷ trailing EPS"))],
    "forwardPE": [(Y, I("forwardPE", positive=True)),
                  ("calculated", calc(lambda c: div(c.price(), c.v("forwardEps")), "Price ÷ forward EPS"))],
    "trailingPegRatio": [(Y, I("trailingPegRatio", "pegRatio", positive=True)),
                         ("calculated", calc(lambda c: div(c.v("trailingPE"), (c.v("earningsGrowth") or 0) * 100),
                                             "Trailing P/E ÷ (earnings growth × 100)"))],
    "priceToBook": [(Y, I("priceToBook", positive=True)),
                    ("calculated", calc(lambda c: div(c.price(), c.v("bookValue")), "Price ÷ book value per share"))],
    "priceToSalesTrailing12Months": [(Y, I("priceToSalesTrailing12Months", positive=True)),
                                     ("calculated", calc(lambda c: div(c.v("marketCap"), c.v("totalRevenue")),
                                                         "Market cap ÷ revenue"))],
    "enterpriseToRevenue": [(Y, I("enterpriseToRevenue")),
                            ("calculated", calc(lambda c: div(c.v("enterpriseValue"), c.v("totalRevenue")),
                                                "Enterprise value ÷ revenue"))],
    "enterpriseToEbitda": [(Y, I("enterpriseToEbitda")),
                           ("calculated", calc(lambda c: div(c.v("enterpriseValue"), c.v("ebitda")),
                                               "Enterprise value ÷ EBITDA"))],
    "bookValue": [(Y, I("bookValue")),
                  ("statements", lambda c: (lambda e: (e[0] / shares(c), e[1]) if e and shares(c) else None)(
                      latest_bs("Stockholders Equity", "Common Stock Equity")(c)))],
    "trailingEps": [(Y, I("trailingEps")), ("statements", ttm("Diluted EPS", "Basic EPS"))],
    "forwardEps": [(Y, I("forwardEps")), ("analysts", estimate("+1y"))],
    "epsCurrentYear": [(Y, I("epsCurrentYear")), ("analysts", estimate("0y"))],
    # profitability
    "grossMargins": [(Y, I("grossMargins", nonzero=True)),
                     ("calculated", calc(lambda c: div(c.v("grossProfits"), c.v("totalRevenue")), "Gross profit ÷ revenue"))],
    "operatingMargins": [(Y, I("operatingMargins")),
                         ("statements", lambda c: (lambda o, r: (o[0] / r[0], "Operating income ÷ revenue, " + o[1].lower())
                                                   if o and r and r[0] > 0 else None)(
                             ttm("Operating Income")(c), ttm("Total Revenue")(c)))],
    "ebitdaMargins": [(Y, I("ebitdaMargins", nonzero=True)),
                      ("calculated", calc(lambda c: div(c.v("ebitda"), c.v("totalRevenue")), "EBITDA ÷ revenue"))],
    "profitMargins": [(Y, I("profitMargins")),
                      ("calculated", calc(lambda c: div(c.v("netIncomeToCommon"), c.v("totalRevenue")), "Net income ÷ revenue"))],
    "returnOnAssets": [(Y, I("returnOnAssets")),
                       ("statements", lambda c: (lambda n, a: (n[0] / a, "Net income ÷ average total assets")
                                                 if n and a and a > 0 else None)(ttm("Net Income")(c), avg_bs("Total Assets")(c)))],
    "returnOnEquity": [(Y, I("returnOnEquity")),
                       ("statements", lambda c: (lambda n, e: (n[0] / e, "Net income ÷ average equity")
                                                 if n and e and e > 0 else None)(
                           ttm("Net Income")(c), avg_bs("Stockholders Equity", "Common Stock Equity")(c)))],
    # growth
    "revenueGrowth": [(Y, I("revenueGrowth")), ("statements", yoy("Total Revenue"))],
    "earningsGrowth": [(Y, I("earningsGrowth")), ("statements", yoy("Diluted EPS", "Basic EPS"))],
    "earningsQuarterlyGrowth": [(Y, I("earningsQuarterlyGrowth")), ("statements", yoy("Net Income"))],
    # financial health
    "totalRevenue": [(Y, I("totalRevenue", nonzero=True)), ("statements", ttm("Total Revenue")), ("sec", lambda c: c.sec("revenue"))],
    "revenuePerShare": [(Y, I("revenuePerShare")),
                        ("calculated", calc(lambda c: div(c.v("totalRevenue"), shares(c)), "Revenue ÷ shares outstanding"))],
    "grossProfits": [(Y, I("grossProfits", nonzero=True)), ("statements", ttm("Gross Profit"))],
    "ebitda": [(Y, I("ebitda", nonzero=True)), ("statements", ttm("EBITDA", "Normalized EBITDA"))],
    "netIncomeToCommon": [(Y, I("netIncomeToCommon")), ("statements", ttm("Net Income Common Stockholders", "Net Income")),
                          ("sec", lambda c: c.sec("net_income"))],
    "operatingCashflow": [(Y, I("operatingCashflow")), ("statements", ttm("Operating Cash Flow", cash_flow=True)),
                          ("sec", lambda c: c.sec("ocf"))],
    "freeCashflow": [(Y, I("freeCashflow")), ("statements", fcf)],
    "totalCash": [(Y, I("totalCash")),
                  ("statements", latest_bs("Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents")),
                  ("sec", lambda c: c.sec("cash"))],
    "totalCashPerShare": [(Y, I("totalCashPerShare")),
                          ("calculated", calc(lambda c: div(c.v("totalCash"), shares(c)), "Total cash ÷ shares outstanding"))],
    "totalDebt": [(Y, I("totalDebt")), ("statements", latest_bs("Total Debt"))],
    "debtToEquity": [(Y, I("debtToEquity")),
                     ("statements", lambda c: (lambda d, e: (d / e[0] * 100, e[1]) if d is not None and e and e[0] > 0 else None)(
                         c.v("totalDebt"), latest_bs("Stockholders Equity", "Common Stock Equity")(c)))],
    "currentRatio": [(Y, I("currentRatio")),
                     ("statements", lambda c: (lambda a, l: (a[0] / l[0], l[1]) if a and l and l[0] > 0 else None)(
                         latest_bs("Current Assets")(c), latest_bs("Current Liabilities")(c)))],
    "quickRatio": [(Y, I("quickRatio")), ("statements", quick)],
    # dividends
    "dividendRate": [(Y, I("dividendRate", positive=True)), ("dividends", forward_dividend)],
    "yield_calc": [(Y, lambda c: c.info("yield") if c.is_fund() and c.info("yield") else None),
                   ("calculated", lambda c: div(c.v("dividendRate"), c.price()))],
    "trailingAnnualDividendRate": [(Y, I("trailingAnnualDividendRate", positive=True)), ("dividends", trailing_dividends)],
    "trailingAnnualDividendYield": [(Y, I("trailingAnnualDividendYield", positive=True)),
                                    ("calculated", calc(lambda c: div(c.v("trailingAnnualDividendRate"), c.price()),
                                                        "Trailing dividends ÷ price"))],
    "fiveYearAvgDividendYield": [(Y, I("fiveYearAvgDividendYield", positive=True)), ("dividends", five_year_yield)],
    "payoutRatio": [(Y, I("payoutRatio", positive=True)),
                    ("calculated", calc(lambda c: div(c.v("trailingAnnualDividendRate"), c.v("trailingEps")),
                                        "Trailing dividends ÷ trailing EPS"))],
    "exDividendDate": [(Y, I("exDividendDate")),
                       ("calendar", lambda c: None if c.no_dividends() else (c.source("calendar") or {}).get("exdiv")),
                       ("dividends", calc(last_div("date"), "Ex-date of the last dividend"))],
    "lastDividendValue": [(Y, I("lastDividendValue", positive=True)), ("dividends", last_div("value"))],
    "lastDividendDate": [(Y, I("lastDividendDate")), ("dividends", last_div("date"))],
    # analysts
    "recommendationKey": [(Y, I("recommendationKey")), ("calculated", rating("key"))],
    "recommendationMean": [(Y, I("recommendationMean")), ("analysts", rec_mean)],
    "averageAnalystRating": [(Y, I("averageAnalystRating")), ("calculated", rating("label"))],
    "numberOfAnalystOpinions": [(Y, I("numberOfAnalystOpinions", positive=True)),
                                ("analysts", lambda c: rec_counts(c)[1] or None)],
    "targetMeanPrice": [(Y, I("targetMeanPrice", positive=True)), ("analysts", target("mean"))],
    "targetMedianPrice": [(Y, I("targetMedianPrice", positive=True)), ("analysts", target("median"))],
    "targetHighPrice": [(Y, I("targetHighPrice", positive=True)), ("analysts", target("high"))],
    "targetLowPrice": [(Y, I("targetLowPrice", positive=True)), ("analysts", target("low"))],
    "target_upside": [("calculated", lambda c: (lambda t, p: t / p - 1 if t and p else None)(c.v("targetMeanPrice"), c.price()))],
    # earnings & events
    "earningsTimestamp": [(Y, I("earningsTimestamp", "earningsTimestampStart")), ("calendar", earnings_from_calendar)],
    "mostRecentQuarter": [(Y, I("mostRecentQuarter")), ("statements", statement_date("qi"))],
    "lastFiscalYearEnd": [(Y, I("lastFiscalYearEnd")), ("statements", statement_date("ai"))],
    "nextFiscalYearEnd": [(Y, I("nextFiscalYearEnd")), ("calculated", next_fiscal_year)],
    "lastSplitFactor": [(Y, I("lastSplitFactor")), ("splits", last_split("factor"))],
    "lastSplitDate": [(Y, I("lastSplitDate")), ("splits", last_split("date"))],
    "firstTradeDateMilliseconds": [(Y, I("firstTradeDateMilliseconds")),
                                   (Y, lambda c: c.info("firstTradeDateEpochUtc") * 1000 if c.info("firstTradeDateEpochUtc") else None),
                                   ("history", lambda c: c.meta("firstTradeDate") * 1000 if c.meta("firstTradeDate") else None),
                                   ("history", lambda c: (epoch(c.hist()["first"]) * 1000, "First day in Yahoo's price history")
                                    if c.hist() else None)],
    # company
    "longName": [(Y, I("longName", "shortName")), ("history", META("longName")), ("history", META("shortName"))],
    "sector": [(Y, I("sector")), (Y, fund_info("category"))],
    "industry": [(Y, I("industry")), (Y, fund_info("fundFamily"))],
    "country": [(Y, I("country"))],
    "city": [(Y, I("city"))],
    "fullTimeEmployees": [(Y, I("fullTimeEmployees", positive=True))],
    "exchange": [(Y, I("exchange")), ("history", META("exchangeName"))],
    "quoteType": [(Y, I("quoteType")), ("history", META("instrumentType"))],
    "currency": [(Y, I("currency")), ("history", META("currency"))],
    "website": [(Y, I("website"))],
    # governance (ISS via Yahoo only)
    **{k: [(Y, I(k))] for k in ("overallRisk", "auditRisk", "boardRisk", "compensationRisk", "shareHolderRightsRisk")},
    # performance (always calculated from history)
    **{k: [("history", perf(k))] for k in PERF_KEYS},
}
assert set(STRATEGIES) == set(YAHOO_KEYS), set(YAHOO_KEYS) ^ set(STRATEGIES)


# ------------------------------- service -----------------------------------
class MetricService:
    """Fetches the sources each tab needs in the background, caches them for every
    visitor, and works out values, sources and reasons per ticker."""

    def __init__(self, yf, sec_client=None, sec_enabled=False):
        self.yf = yf
        self.sec_enabled = bool(sec_enabled and sec_client)
        self.secfacts = SecFacts(sec_client, self.sec_enabled) if sec_client else None
        self.lock = threading.Lock()
        self.cache = {s: LRU(CACHE_LIMITS.get(s, 400)) for s in TTL}
        self.inflight = {}
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.fetchers = {"info": fetch_info, "hist": fetch_hist, "intraday": fetch_intraday,
                         "statements": fetch_statements, "analysts": fetch_analysts, "holders": fetch_holders,
                         "calendar": fetch_calendar, "shares": fetch_shares}

    def _fetch(self, source, sym):
        if source == "sec":
            return self.secfacts.fetch(sym)
        return self.fetchers[source](self.yf, sym)

    def _run(self, source, sym):
        try:
            data = self._fetch(source, sym)
        except YahooBusy:
            data = {"_error": "busy"}
        except Exception as e:
            data = {"_error": "rate limited" if is_rate_limit(e) else f"failed ({type(e).__name__})"}
        with self.lock:
            self.cache[source][sym] = (time.time(), data)
            self.inflight.pop((source, sym), None)

    def _lookup(self, source, sym):
        with self.lock:
            hit = self.cache[source].get(sym)
        if not hit:
            return None, True
        ts, data = hit
        max_age = ERROR_MAX_AGE if "_error" in data else TTL[source]
        return data, time.time() - ts > max_age

    def _ensure(self, source, sym):
        """Start a fetch when the cached copy is missing or stale. Returns a future
        only when there's nothing cached to show meanwhile."""
        data, stale = self._lookup(source, sym)
        if not stale:
            return None
        with self.lock:
            fut = self.inflight.get((source, sym))
            if fut is None:
                fut = self.pool.submit(self._run, source, sym)
                self.inflight[(source, sym)] = fut
        return fut if data is None else None

    def _sources(self, sym):
        out = {}
        for source in TTL:
            data, _ = self._lookup(source, sym)
            if data is not None:
                out[source] = data
        return out

    def _resolve(self, sym, keys):
        spy, _ = self._lookup("hist", "SPY")
        ctx = Ctx(sym, self._sources(sym), spy=spy, sec_enabled=self.sec_enabled)
        row = ctx.resolve(keys)
        return ctx, row

    def get(self, symbols, keys):
        keys = [k for k in dict.fromkeys(keys) if k in STRATEGIES]
        t0 = time.time()
        futs = []
        for s in symbols:
            futs += [self._ensure("info", s), self._ensure("hist", s)]
        futs = [f for f in futs if f is not None]
        if futs:
            wait(futs, timeout=WAIT_SEC)

        results = {}
        for _ in range(3):
            futs = []
            for s in symbols:
                ctx, row = self._resolve(s, keys)
                results[s] = (ctx, row)
                for source in ctx.used | ctx.needs:
                    if source == "spy":
                        futs.append(self._ensure("hist", "SPY"))
                    elif source in TTL:
                        futs.append(self._ensure(source, s))
            futs = [f for f in futs if f is not None]
            remaining = WAIT_SEC - (time.time() - t0)
            if not futs or remaining < 0.5:
                break
            wait(futs, timeout=remaining)

        rows, pending = [], 0
        for s in symbols:
            ctx, row = results[s]
            info, hist = ctx.sources.get("info") or {}, ctx.sources.get("hist") or {}
            error = None
            if not row["data"] and "_error" in hist and info and (info.get("empty") or "_error" in info):
                error = ("Yahoo has no data for this ticker" if info.get("empty")
                         else "Couldn't reach Yahoo just now")
            loading = bool(row["loading"])
            pending += loading
            rows.append({"symbol": s, **row, "pending": loading, "error": error})
        return rows, pending
