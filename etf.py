"""
ETF short screen
----------------
For each ETF:
  1. Quoted borrow fee, rebate and shares available (IBKR stock loan file, via app.py).
  2. Break-even borrow fee from history: the fee at which a short held since
     inception (or over the last 3 / 1 years) would have broken even, and the fee
     at which it would have matched simply owning SPY.
  3. Theoretical borrow fee from the ETF's latest reported holdings: the
     weighted average of each holding's IBKR borrow fee.

Holdings come from the ETF's latest SEC N-PORT filing (full list, filed
quarterly) when SEC_USER_AGENT is set, otherwise from Yahoo's top 10 holdings.
"""

import json
import math
import os
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait

SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()
GC_DEFAULT = float(os.environ.get("GC_RATE", "0.25"))     # % / yr assumed for unmatched holdings
BENCHMARK = "SPY"

HIST_MAX_AGE = 6 * 3600       # price history refreshed every 6 hours
HOLD_MAX_AGE = 12 * 3600      # holdings refreshed every 12 hours
ERROR_MAX_AGE = 5 * 60        # failed lookups retried after 5 minutes
WAIT_SEC = 8                  # how long one request waits for new data
WINDOWS = {"inception": None, "3y": 3, "1y": 1}
TOP_HOLDINGS_SHOWN = 15


def to_float(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def yahoo_symbol(sym):
    return sym.replace(".", "-")


# ------------------------------ SEC N-PORT --------------------------------
def cusip_to_isin(cusip):
    """US ISIN = 'US' + CUSIP + check digit."""
    c = (cusip or "").strip().upper()
    if not re.fullmatch(r"[0-9A-Z]{9}", c) or set(c) == {"0"}:
        return None
    base = "US" + c
    digits = "".join(str(int(ch, 36)) for ch in base)
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return base + str((10 - total % 10) % 10)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_nport(xml_bytes):
    """Return (report date, [holding]) from an N-PORT primary_doc.xml."""
    root = ET.fromstring(xml_bytes)
    rep_date, holdings = None, []
    for el in root.iter():
        name = _local(el.tag)
        if name == "repPdDate" and rep_date is None:
            rep_date = (el.text or "").strip()
        elif name == "invstOrSec":
            h = {}
            for c in el.iter():      # first occurrence = the holding itself, not nested derivative refs
                cn = _local(c.tag)
                if cn in ("name", "title", "cusip", "pctVal", "assetCat", "payoffProfile") and cn not in h:
                    h[cn] = (c.text or "").strip()
                elif cn in ("isin", "ticker") and cn not in h:
                    h[cn] = (c.get("value") or "").strip()
            w = to_float(h.get("pctVal"))
            if w is None or w <= 0 or h.get("payoffProfile", "").lower() == "short":
                continue
            isin = h.get("isin") or cusip_to_isin(h.get("cusip"))
            holdings.append({
                "name": (h.get("name") or h.get("title") or "").title()[:80],
                "ticker": (h.get("ticker") or "").upper() or None,
                "isin": (isin or "").upper() or None,
                "w": w / 100.0,
                "cat": h.get("assetCat") or "",
            })
    return rep_date, holdings


class Sec:
    """Minimal EDGAR client: one request at a time, under SEC's 10 requests/second limit."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last = 0.0
        self.funds, self.funds_at = None, 0.0
        self.folders = {}          # series id -> (time, folder url)

    def get(self, url):
        with self.lock:
            gap = 0.15 - (time.time() - self.last)
            if gap > 0:
                time.sleep(gap)
            self.last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": SEC_USER_AGENT, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()

    def series_for(self, sym):
        if self.funds is None or time.time() - self.funds_at > 86400:
            d = json.loads(self.get("https://www.sec.gov/files/company_tickers_mf.json"))
            f = d["fields"]
            i_cik, i_ser, i_sym = f.index("cik"), f.index("seriesId"), f.index("symbol")
            funds = {}
            for row in d["data"]:
                funds.setdefault(str(row[i_sym]).upper(), (int(row[i_cik]), row[i_ser]))
            self.funds, self.funds_at = funds, time.time()
        return self.funds.get(sym.upper()) or self.funds.get(yahoo_symbol(sym).upper())

    def latest_nport_folder(self, cik, series):
        hit = self.folders.get(series)
        if hit and time.time() - hit[0] < HOLD_MAX_AGE:
            return hit[1]
        folder = None
        # 1) EDGAR's filing list for this one series
        try:
            atom = self.get("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                            f"&CIK={series}&type=NPORT-P&dateb=&owner=include&count=10&output=atom")
            atom = atom.decode("utf-8", "replace")
            for entry in re.findall(r"<entry[^>]*>(.*?)</entry>", atom, re.S):
                ftype = re.search(r"<filing-type>\s*(.*?)\s*</filing-type>", entry)
                href = re.search(r"<filing-href>\s*(.*?)\s*</filing-href>", entry)
                if ftype and href and ftype.group(1).startswith("NPORT-P"):
                    folder = href.group(1).rsplit("/", 1)[0]
                    break
        except Exception:
            folder = None
        # 2) Fallback: scan the trust's recent N-PORT filings for this series
        if not folder:
            sub = json.loads(self.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json"))
            recent = sub["filings"]["recent"]
            checked = 0
            for form, acc in zip(recent["form"], recent["accessionNumber"]):
                if not form.startswith("NPORT-P"):
                    continue
                checked += 1
                if checked > 40:
                    break
                f = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}"
                head = self.get(f"{f}/{acc}-index-headers.html").decode("utf-8", "replace")
                if series in head:
                    folder = f
                    break
        self.folders[series] = (time.time(), folder)
        return folder


SEC = Sec()


# ------------------------------ fetchers ----------------------------------
def fetch_history(yf, sym):
    t = yf.Ticker(yahoo_symbol(sym))
    hist = t.history(period="max", interval="1d", auto_adjust=True)
    close = hist["Close"].dropna() if "Close" in hist else None
    if close is None or len(close) < 20:
        return {"_error": "Yahoo has no price history for this ticker"}
    idx = close.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    close.index = idx.normalize()
    close = close[~close.index.duplicated(keep="last")]
    out = {"close": close}
    if sym != BENCHMARK:
        try:
            info = t.info or {}
        except Exception:
            info = {}
        out["name"] = info.get("longName") or info.get("shortName")
        out["aum"] = to_float(info.get("totalAssets"))
        er = to_float(info.get("netExpenseRatio"))          # already a percent
        if er is None:
            er = to_float(info.get("annualReportExpenseRatio"))
            er = er * 100 if er is not None and er < 1 else er
        out["expense"] = er
    return out


def fetch_holdings(yf, sym):
    notes = []
    if SEC_USER_AGENT:
        try:
            ser = SEC.series_for(sym)
            if not ser:
                notes.append("Not in SEC's fund list")
            else:
                folder = SEC.latest_nport_folder(*ser)
                if not folder:
                    notes.append("No N-PORT filing found at SEC")
                else:
                    rep, holds = parse_nport(SEC.get(f"{folder}/primary_doc.xml"))
                    if holds:
                        return {"source": "SEC N-PORT", "as_of": rep, "url": folder + "/",
                                "complete": True, "holdings": holds, "notes": notes}
                    notes.append("SEC filing listed no holdings")
        except Exception as e:
            notes.append(f"SEC lookup failed ({type(e).__name__})")
    else:
        notes.append("SEC lookup is off (SEC_USER_AGENT not set), so only Yahoo's top 10 are used")

    try:
        top = yf.Ticker(yahoo_symbol(sym)).funds_data.top_holdings
    except Exception:
        top = None
    holds = []
    if top is not None and len(top):
        for idx, row in top.iterrows():
            w = to_float(row.get("Holding Percent"))
            if w and w > 0:
                holds.append({"name": str(row.get("Name") or idx)[:80], "ticker": str(idx).upper(),
                              "isin": None, "w": w, "cat": ""})
    if holds:
        rest = 1.0 - sum(h["w"] for h in holds)
        if rest > 0.001:
            holds.append({"name": "Everything outside the top 10", "ticker": None, "isin": None,
                          "w": rest, "cat": "rest"})
        return {"source": "Yahoo top 10", "as_of": None, "url": None, "complete": False,
                "holdings": holds, "notes": notes}
    notes.append("Yahoo has no holdings for this ETF")
    return {"_error": "; ".join(notes)}


# ------------------------------ analysis ----------------------------------
def analyze_window(close, spy, years):
    """Break-even borrow fees for a short held over the window.

    Short P&L (fraction of starting notional) = -R_etf - fee * A, where
    A = sum over days of (short's market value / starting notional) * days / 360,
    because borrow accrues daily on the current value of the borrowed shares.
    """
    import pandas as pd

    c = close
    if years:
        start = c.index[-1] - pd.DateOffset(years=years)
        if c.index[0] > start + pd.Timedelta(days=7):
            return None                    # ETF is younger than the window
        c = c[c.index >= start]
    s = spy.reindex(c.index).ffill()
    keep = s.notna()
    c, s = c[keep], s[keep]
    if len(c) < 20:
        return None

    rel = c / c.iloc[0]
    gaps = c.index.to_series().diff().dt.days.iloc[1:].to_numpy()
    a = float((rel.iloc[:-1].to_numpy() * gaps).sum() / 360.0)
    days = (c.index[-1] - c.index[0]).days
    yrs = days / 365.25
    r_etf = float(rel.iloc[-1] - 1)
    r_spy = float(s.iloc[-1] / s.iloc[0] - 1)

    def ann(r):
        return (1 + r) ** (1 / yrs) - 1 if r > -1 and yrs > 0 else None

    return {
        "start": c.index[0].strftime("%Y-%m-%d"),
        "end": c.index[-1].strftime("%Y-%m-%d"),
        "years": yrs,
        "etf_total": r_etf, "spy_total": r_spy,
        "etf_ann": ann(r_etf), "spy_ann": ann(r_spy),
        "a": a,
        "avg_size": a / (days / 360.0) if days else None,
        # fees in % per year
        "be_profit": -r_etf / a * 100 if a > 0 else None,
        "be_spy": (-r_etf - r_spy) / a * 100 if a > 0 else None,
        "be_hedged": (r_spy - r_etf) / a * 100 if a > 0 else None,
    }


def theoretical_fee(holdings, by_sym, by_isin, gc):
    total = sum(h["w"] for h in holdings)
    if total <= 0:
        return None
    acc, matched_w, rows = 0.0, 0.0, []
    for h in holdings:
        loan = (by_isin.get(h["isin"]) if h.get("isin") else None) or \
               (by_sym.get(h["ticker"]) if h.get("ticker") else None)
        fee = loan["fee"] if loan and loan.get("fee") is not None else None
        w = h["w"] / total
        used = fee if fee is not None else gc
        acc += w * used
        if fee is not None:
            matched_w += w
        rows.append({"name": h["name"], "ticker": h.get("ticker") or (loan or {}).get("symbol"),
                     "w": w, "fee": fee, "contrib": w * used})
    rows.sort(key=lambda r: r["w"], reverse=True)
    return {"fee": acc, "coverage": matched_w, "count": len(holdings),
            "matched": sum(1 for r in rows if r["fee"] is not None),
            "top": rows[:TOP_HOLDINGS_SHOWN],
            "top_contrib": sorted(rows, key=lambda r: r["contrib"], reverse=True)[:5]}


# ------------------------------- service ----------------------------------
class EtfService:
    def __init__(self, yf, borrow):
        self.yf, self.borrow = yf, borrow
        self.lock = threading.Lock()
        self.cache = {"hist": {}, "hold": {}}
        self.inflight = {}
        self.pool = ThreadPoolExecutor(max_workers=4)

    def _run(self, kind, sym):
        try:
            data = fetch_history(self.yf, sym) if kind == "hist" else fetch_holdings(self.yf, sym)
        except Exception as e:
            data = {"_error": f"Couldn't load data right now ({type(e).__name__})"}
        with self.lock:
            self.cache[kind][sym] = (time.time(), data)
            self.inflight.pop((kind, sym), None)

    def _lookup(self, kind, sym):
        with self.lock:
            hit = self.cache[kind].get(sym)
        if not hit:
            return None, True
        ts, data = hit
        max_age = ERROR_MAX_AGE if "_error" in data else (HIST_MAX_AGE if kind == "hist" else HOLD_MAX_AGE)
        return data, time.time() - ts > max_age

    def _ensure(self, kind, sym):
        data, stale = self._lookup(kind, sym)
        if not stale:
            return None
        with self.lock:
            fut = self.inflight.get((kind, sym))
            if fut is None:
                fut = self.pool.submit(self._run, kind, sym)
                self.inflight[(kind, sym)] = fut
        return fut if data is None else None

    def report(self, symbols, gc):
        waiting = [self._ensure("hist", BENCHMARK)]
        for s in symbols:
            waiting += [self._ensure("hist", s), self._ensure("hold", s)]
        waiting = [f for f in waiting if f is not None]
        if waiting:
            wait(waiting, timeout=WAIT_SEC)

        self.borrow.ensure_fresh()
        table, _, as_of, error, refreshing = self.borrow.snapshot()
        by_isin = self.borrow.isin_index()
        spy, _ = self._lookup("hist", BENCHMARK)
        spy_close = spy.get("close") if spy and "_error" not in spy else None

        rows, pending = [], 0
        for s in symbols:
            loan = table.get(s)
            row = {"symbol": s, "quote": {
                "listed": loan is not None,
                "fee": loan["fee"] if loan else None,
                "rebate": loan["rebate"] if loan else None,
                "shares": loan["shares"] if loan else None,
                "plus": loan["plus"] if loan else False,
                "name": loan["name"] if loan else None,
            }}

            hist, _ = self._lookup("hist", s)
            if hist is None or (spy is None):
                row["history"] = {"pending": True}
                pending += 1
            elif "_error" in hist:
                row["history"] = {"error": hist["_error"]}
            elif spy_close is None:
                row["history"] = {"error": "Couldn't load SPY history from Yahoo"}
            else:
                close = hist["close"]
                row["name"] = hist.get("name")
                row["aum"] = hist.get("aum")
                row["expense"] = hist.get("expense")
                row["history"] = {
                    "inception": close.index[0].strftime("%Y-%m-%d"),
                    "windows": {k: analyze_window(close, spy_close, y) for k, y in WINDOWS.items()},
                }

            hold, _ = self._lookup("hold", s)
            if hold is None:
                row["holdings"] = {"pending": True}
                pending += 1
            elif "_error" in hold:
                row["holdings"] = {"error": hold["_error"]}
            else:
                theo = theoretical_fee(hold["holdings"], table, by_isin, gc)
                row["holdings"] = {"source": hold["source"], "as_of": hold["as_of"], "url": hold["url"],
                                   "complete": hold["complete"], "notes": hold.get("notes", []),
                                   "theo": theo}
            rows.append(row)

        borrow_state = ({"state": "ok", "as_of": as_of} if table else
                        {"state": "error", "text": error} if error and not refreshing else
                        {"state": "loading"})
        return {"rows": rows, "pending": pending, "gc": gc, "borrow": borrow_state,
                "sec": bool(SEC_USER_AGENT)}
