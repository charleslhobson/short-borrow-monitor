"""
ETF short screen
----------------
For each ETF (or stock):
  1. Quoted borrow fee, rebate and shares available (IBKR stock loan file, via app.py).
  2. Break-even borrow fees over a window (since inception, 3 years, 1 year or from
     any start date):
       - the fee at which the short still profits after the risk-free rate, and
       - the fee at which the short, levered to -1.00 beta, is as cheap a hedge as
         shorting SPY (beta from the last year of daily returns).
     The earlier path-based break-evens are kept for reference.
  3. Theoretical borrow fee from the ETF's latest reported holdings: each position's
     exposure (as a share of net assets) times its borrow fee, summed. Swaps, futures
     and options are looked through to the stock or index they track.

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
from datetime import date

from common import LRU, listing_message, resolve_symbol, yahoo_call, yahoo_symbol

SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()
GC_DEFAULT = float(os.environ.get("GC_RATE", "0.25"))     # % / yr assumed for unmatched holdings
CASH_DEFAULT = float(os.environ.get("CASH_RATE", "0"))    # % / yr for cash, T-bills and money market funds
RFR_SPREAD_DEFAULT = float(os.environ.get("RFR_SPREAD", "1.0"))  # % / yr below the T-bill rate earned on short proceeds
BENCHMARK = "SPY"
RFR_SYMBOL = "^IRX"                                       # Yahoo: 13-week T-bill yield, in %
FRED_DTB3 = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3"

HIST_MAX_AGE = 6 * 3600       # price history refreshed every 6 hours
HOLD_MAX_AGE = 12 * 3600      # holdings refreshed every 12 hours
ERROR_MAX_AGE = 5 * 60        # failed lookups retried after 5 minutes
WAIT_SEC = 8                  # how long one request waits for new data
WINDOWS = {"inception": None, "3y": 3, "1y": 1}
TOP_HOLDINGS_SHOWN = 15
CACHE_LIMIT = 300             # most ETFs whose history / holdings stay in memory
MIN_WINDOW_DAYS = 20          # trading days a window needs
BETA_FULL_DAYS = 120          # a full beta needs this many daily returns...
BETA_MIN_DAYS = 60            # ...and below this there's no beta at all
BETA_FULL_WEEKS = 52          # the same for weekly returns
BETA_MIN_WEEKS = 26
BETA_CHANGE_FLAG = 0.25       # flag when the window's beta and last year's differ by more than this share
WEEKLY_ABOVE = 0.5            # use weekly returns when more than this share of a fund's stocks trade abroad
MIN_HEDGE_BETA = 0.1          # below this, shorting isn't treated as a SPY hedge
SLEEVE_MIN_HOLDINGS = 5       # stock holdings a fund needs before they stand in for its index
LOOK_THROUGH_TOP = 15         # biggest positions used for the Stocks page's look-through averages
# Also stock exposure for look-through, though not tied to one ticker: Yahoo's "everything outside
# the top 10", and stock swaps, futures and options whose underlying couldn't be looked through.
LOOK_THROUGH_EXTRA = ("Undisclosed", "Swap", "Future", "Option", "Derivative")

# Index swaps and futures whose components aren't listed are priced from an ETF
# that holds the index. Edit freely: (pattern matched against the index name, ETF).
# SPY, QQQ and DIA are unit investment trusts, which may be missing from SEC's fund
# list (how this app finds filings), so IVV and QQQM stand in for the first two.
# If DIA's holdings don't load, Dow swaps fall back to the assumed rate.
INDEX_PROXIES = [
    (r"SEMICONDUCTOR|\bSOX\b", "SOXX"),
    (r"NASDAQ[- ]?100|\bNDX\b", "QQQM"),
    (r"RUSSELL\s*2000|\bRTY\b", "IWM"),
    (r"DOW JONES INDUSTRIAL|\bDJIA\b", "DIA"),
    (r"BIOTECH", "XBI"),
    (r"REGIONAL BANK", "KRE"),
    (r"FINANCIAL SELECT SECTOR", "XLF"),
    (r"TECHNOLOGY SELECT SECTOR", "XLK"),
    (r"ENERGY SELECT SECTOR", "XLE"),
    (r"S&P\s*MIDCAP\s*400", "IJH"),
    (r"MSCI EMERGING MARKETS", "EEM"),
    (r"MSCI EAFE", "EFA"),
    (r"GOLD MINERS", "GDX"),
    (r"S&P\s*500|\bSPX\b", "IVV"),
]


def to_float(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def num(text):
    """A number from filing text such as '1,234.5'; None for 'N/A' or blanks."""
    if text is None:
        return None
    return to_float(str(text).replace(",", "").strip())


def parse_start_date(text, today=None):
    """(date, None) for a usable YYYY-MM-DD start date, (None, message) otherwise."""
    text = (text or "").strip()
    if not text:
        return None, None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None, "Enter the start date as YYYY-MM-DD."
    try:
        d = date.fromisoformat(text)
    except ValueError:
        return None, f"{text} isn't a real date."
    if d > (today or date.today()):
        return None, "The start date is in the future. Pick a date in the past."
    if d.year < 1900:
        return None, "Pick a start date after 1900."
    return d, None


# --------------------------- identifiers & names ---------------------------
# Markets, the IBKR file names they may use, and how Yahoo, Bloomberg and ISO
# country codes point at them.
MARKET_FILES = {
    "japan": ("japan",), "uk": ("british", "uk", "unitedkingdom", "england"),
    "canada": ("canada",), "hongkong": ("hongkong", "hong_kong", "hk"),
    "netherlands": ("dutch", "netherlands", "holland"), "france": ("france", "french"),
    "germany": ("germany", "german"), "switzerland": ("swiss", "switzerland"),
    "spain": ("spain", "spanish"), "italy": ("italy", "italian"), "sweden": ("swedish", "sweden"),
    "australia": ("australia",), "belgium": ("belgium", "belgian"), "austria": ("austria", "austrian"),
    "mexico": ("mexico",), "india": ("india",), "denmark": ("danish", "denmark"),
    "norway": ("norway", "norwegian"), "finland": ("finland", "finnish"),
    "singapore": ("singapore",), "korea": ("korea", "southkorea"), "taiwan": ("taiwan",),
}
YAHOO_SUFFIX = {
    "T": "japan", "L": "uk", "TO": "canada", "V": "canada", "NE": "canada", "CN": "canada",
    "HK": "hongkong", "AS": "netherlands", "PA": "france", "DE": "germany", "F": "germany",
    "SW": "switzerland", "MC": "spain", "MI": "italy", "ST": "sweden", "AX": "australia",
    "BR": "belgium", "VI": "austria", "MX": "mexico", "NS": "india", "BO": "india",
    "CO": "denmark", "OL": "norway", "HE": "finland", "SI": "singapore", "KS": "korea", "TW": "taiwan",
}
BBG_CODE = {
    "JP": "japan", "JT": "japan", "LN": "uk", "CN": "canada", "CT": "canada", "HK": "hongkong",
    "NA": "netherlands", "FP": "france", "GY": "germany", "GR": "germany", "SW": "switzerland",
    "SE": "switzerland", "VX": "switzerland", "SM": "spain", "SQ": "spain", "IM": "italy",
    "SS": "sweden", "AU": "australia", "AT": "australia", "BB": "belgium", "AV": "austria",
    "MM": "mexico", "IN": "india", "IS": "india", "DC": "denmark", "NO": "norway",
    "FH": "finland", "SP": "singapore", "KS": "korea", "TT": "taiwan",
}
ISO_MARKET = {
    "JP": "japan", "GB": "uk", "CA": "canada", "HK": "hongkong", "NL": "netherlands",
    "FR": "france", "DE": "germany", "CH": "switzerland", "ES": "spain", "IT": "italy",
    "SE": "sweden", "AU": "australia", "BE": "belgium", "AT": "austria", "MX": "mexico",
    "IN": "india", "DK": "denmark", "NO": "norway", "FI": "finland", "SG": "singapore",
    "KR": "korea", "TW": "taiwan",
}
NAME_DROP = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COS", "LTD", "LIMITED",
    "PLC", "SA", "NV", "AG", "SE", "SPA", "AB", "ASA", "OYJ", "HOLDINGS", "HOLDING", "HLDGS",
    "HLDG", "GROUP", "GRP", "THE", "COM", "COMMON", "STOCK", "SHS", "SHARES", "SHARE", "ORD",
    "ORDINARY", "NEW", "CLASS", "CL", "REG", "REGISTERED", "NPV", "LLC", "LP",
}


def market_for_file(stem):
    for market, stems in MARKET_FILES.items():
        if stem in stems:
            return market
    return stem


def clean_isin(isin):
    s = (isin or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}[0-9A-Z]{9}[0-9]", s) or set(s[2:]) == {"0"}:
        return None
    return s


def clean_cusip(cusip):
    s = (cusip or "").strip().upper()
    if not re.fullmatch(r"[0-9A-Z*@#]{8}[0-9]", s) or s.startswith("000000"):
        return None
    return s


def cusip_to_isin(cusip):
    """US ISIN = 'US' + CUSIP + check digit. Foreign CINS codes (first character a letter) give None."""
    c = clean_cusip(cusip)
    if not c or not c[0].isdigit() or not c.isalnum():
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


def norm_ticker(t):
    """A ticker in IBKR's style (BRK.B) from Yahoo (BRK-B), filings (BRK/B, 'BRK B', 'AAPL US') etc."""
    if not t:
        return None
    s = str(t).strip().upper().lstrip("$")
    s = re.sub(r"\s+EQUITY$", "", s)
    s = re.sub(r"\s+(US|UN|UW|UQ|UA|UR|UP|UV|UF)$", "", s)
    s = re.sub(r"[\s/\-]+", ".", s).strip(".")
    return s if re.fullmatch(r"[A-Z0-9][A-Z0-9.]{0,11}", s) else None


def split_foreign(t, yahoo=False):
    """(local code, market) for '7203 JP' (Bloomberg) or, from Yahoo, '7203.T'; else None."""
    s = re.sub(r"\s+EQUITY$", "", str(t or "").strip().upper())
    m = re.fullmatch(r"([A-Z0-9][A-Z0-9./\-]*)\s+([A-Z]{2})", s)
    if m and m.group(2) in BBG_CODE:
        return m.group(1), BBG_CODE[m.group(2)]
    if yahoo:
        m = re.fullmatch(r"([A-Z0-9][A-Z0-9\-]*)\.([A-Z]{1,2})", s)
        if m and m.group(2) in YAHOO_SUFFIX:
            return m.group(1), YAHOO_SUFFIX[m.group(2)]
    return None


def norm_name(s):
    """'Alphabet Inc Class A' and 'ALPHABET INC-CL A' both become 'ALPHABET A'."""
    if not s:
        return None
    words = re.sub(r"[^A-Z0-9]+", " ", str(s).upper()).split()
    return " ".join(w for w in words if w not in NAME_DROP) or None


def borrow_rank(entry):
    shares = entry.get("shares")
    return (entry.get("fee") is not None, shares if shares is not None else -1)


def _keep_best(d, key, entry):
    old = d.get(key)
    if old is None or borrow_rank(entry) > borrow_rank(old):
        d[key] = entry


class MatchIndex:
    """Every way to find an IBKR borrow row for a holding: ISIN, CUSIP, ticker,
    the non-US country files, and (last resort) a unique company-name match."""

    def __init__(self, us, foreign=None):
        self.us_sym, self.isin_us, self.isin_fx, self.cusip = {}, {}, {}, {}
        self.name_us, self.name_fx, self.fx_sym = {}, {}, {}
        for sym, e in (us or {}).items():
            ent = dict(e, symbol=sym, file="usa")
            _keep_best(self.us_sym, sym, ent)
            isin = clean_isin(e.get("isin"))
            if isin:
                _keep_best(self.isin_us, isin, ent)
                if isin.startswith("US"):
                    _keep_best(self.cusip, isin[2:11], ent)
            n = norm_name(e.get("name"))
            if n:
                self.name_us.setdefault(n, {})[sym] = ent
        for stem, table in (foreign or {}).items():
            syms = self.fx_sym.setdefault(market_for_file(stem), {})
            for sym, e in table.items():
                ent = dict(e, symbol=sym, file=stem)
                _keep_best(syms, sym, ent)
                isin = clean_isin(e.get("isin"))
                if isin:
                    _keep_best(self.isin_fx, isin, ent)
                n = norm_name(e.get("name"))
                if n:
                    self.name_fx.setdefault(n, {})[(stem, sym)] = ent

    def _isin(self, isin):
        # A non-US ISIN is borrowed in its home market first; IBKR's US file can also list it (OTC).
        order = (self.isin_us, self.isin_fx) if isin.startswith("US") else (self.isin_fx, self.isin_us)
        return [d[isin] for d in order if isin in d]

    def _fx(self, market, code):
        syms = self.fx_sym.get(market) or {}
        for c in (code, code.lstrip("0"), norm_ticker(code)):
            if c and c in syms:
                return syms[c]
        return None

    def _ticker(self, ticker, country, yahoo):
        f = split_foreign(ticker, yahoo)
        if f:
            return self._fx(f[1], f[0])
        t = norm_ticker(ticker)
        if not t:
            return None
        market = ISO_MARKET.get(country or "")
        home = self._fx(market, t) if market else None     # a foreign holding trades at home first
        return home or self.us_sym.get(t)

    def _name(self, name, country):
        """Only a unique exact match counts. US holdings match the US file; foreign
        holdings match their home market's file, then any non-US file."""
        n = norm_name(name)
        if not n:
            return None, 0
        market = ISO_MARKET.get(country or "")
        if market:
            fx = self.name_fx.get(n, {})
            stems = MARKET_FILES.get(market, ())
            choices = [{k: v for k, v in fx.items() if k[0] in stems}, fx]
        else:
            choices = [self.name_us.get(n, {})]
        for hits in choices:
            if hits:
                return (next(iter(hits.values())), 1) if len(hits) == 1 else (None, len(hits))
        return None, 0

    def find(self, isin=None, cusip=None, ticker=None, names=(), country=None, yahoo=False):
        """(entry, how, tried). `entry` has a fee when one was found; otherwise it may be
        a row IBKR lists without a fee, or None. `tried` says what didn't work."""
        tried, listed = [], []

        def take(entry, how):
            if entry is None:
                return False
            if entry.get("fee") is not None:
                return True
            listed.append((entry, how))
            return False

        isin = clean_isin(isin)
        if isin:
            for e in self._isin(isin):
                if take(e, "isin"):
                    return e, "isin", tried
            tried.append(f"ISIN {isin} {'is listed without a fee' if listed else 'is not in IBKR’s files'}")
        cus = clean_cusip(cusip)
        if cus:
            cands = [self.cusip.get(cus)]
            conv = cusip_to_isin(cus)
            if conv and conv != isin:
                cands += self._isin(conv)
            for e in cands:
                if take(e, "cusip"):
                    return e, "cusip", tried
            tried.append(f"CUSIP {cus} not found with a fee")
        if ticker:
            e = self._ticker(ticker, country, yahoo)
            if take(e, "ticker"):
                return e, "ticker", tried
            tried.append(f"ticker {ticker} {'is listed without a fee' if e else 'not found'}")
        seen = set()
        for nm in names:
            if not nm or norm_name(nm) in seen:
                continue
            seen.add(norm_name(nm))
            e, count = self._name(nm, country)
            if take(e, "name"):
                return e, "name", tried
            if count > 1:
                tried.append(f"name “{nm}” matches {count} IBKR rows, so it isn't used")
        if names and not any(t.startswith("name") for t in tried):
            tried.append("no IBKR row with the same company name")
        if not tried:
            tried.append("no ISIN, CUSIP, ticker or usable name")
        if listed:
            return listed[0][0], listed[0][1], tried
        return None, None, tried


FILE_LABELS = {"british": "UK", "uk": "UK", "hongkong": "Hong Kong", "hong_kong": "Hong Kong",
               "hk": "Hong Kong", "southkorea": "Korea", "unitedkingdom": "UK"}


def file_label(stem):
    return FILE_LABELS.get(stem, stem.replace("_", " ").title())


def match_label(entry, how):
    stem = entry.get("file", "usa")
    if how == "name":
        return "IBKR (name match)" if stem == "usa" else f"IBKR (name match, {file_label(stem)} file)"
    if stem != "usa":
        return f"IBKR ({file_label(stem)} file)"
    return {"isin": "IBKR (ISIN)", "cusip": "IBKR (CUSIP)", "ticker": "IBKR (ticker)"}.get(how, "IBKR")


# ------------------------------ SEC N-PORT --------------------------------
def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el, *names):
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el, *names):
    c = _child(el, *names)
    if c is None:
        return None
    return (c.text or "").strip() or (c.get("value") or "").strip() or None


def _cat(el, name, conditional):
    """assetCat / issuerCat, which the filing gives as <assetCat>EC</assetCat>
    or, for 'other', as <assetConditional assetCat="OTHER" desc="..."/>."""
    v = _text(el, name)
    if v:
        return v.upper()
    c = _child(el, conditional)
    return (c.get(name) or "").upper() if c is not None else ""


def _ids(el):
    """isin / cusip / ticker inside an identifiers element (as value="" or as text)."""
    out = {}
    if el is None:
        return out
    for c in el.iter():
        n = _local(c.tag)
        v = (c.get("value") or c.text or "").strip()
        if not v:
            continue
        if n in ("isin", "cusip", "ticker"):
            out.setdefault(n, v)
        elif n == "other":
            desc = (c.get("otherDesc") or "").upper()
            for key in ("isin", "cusip", "ticker"):
                if key.upper() in desc:
                    out.setdefault(key, v)
    return out


DERIV_KINDS = {"swapDeriv": "swap", "futrDeriv": "future", "fwdDeriv": "forward",
               "optionSwaptionWarrantDeriv": "option", "othDeriv": "other"}


def _components(basket):
    """Components of a custom basket, when the filing lists them."""
    out = []
    for c in basket.iter():
        if c is basket or not _local(c.tag).lower().endswith("component") or not len(c):
            continue
        name, amount = "", None
        for x in c.iter():
            xl, v = _local(x.tag).lower(), (x.text or "").strip()
            if x is c or not v:
                continue
            if not name and ("name" in xl or xl in ("title", "issuetitle")):
                name = v
            if amount is None and any(k in xl for k in ("notamt", "notional", "value", "amt")):
                amount = num(v)
        ids = _ids(c)
        if name or ids:
            out.append({"name": name, "amount": amount, **ids})
    return out


def _reference(desc):
    """What a derivative tracks: one security, or an index / basket."""
    for c in desc:
        n = _local(c.tag)
        if n == "otherRefInst":
            return {"type": "single", "name": _text(c, "issuerName") or "",
                    "title": _text(c, "issueTitle") or "", **_ids(_child(c, "identifiers"))}
        if n == "indexBasketInfo":
            return {"type": "index", "name": _text(c, "indexName") or "",
                    "id": _text(c, "indexIdentifier"), "components": _components(c)}
        if n == "nestedDerivInfo":
            inner = next((x for x in c.iter() if _local(x.tag) == "descRefInstrmnt"), None)
            if inner is not None:
                return _reference(inner)
    return None


def _swap_direction(swap, ref):
    """'long' when the fund receives the reference asset's return, 'short' when it pays it."""
    rec = [c for c in swap if _local(c.tag) in ("fixedRecDesc", "floatingRecDesc", "otherRecDesc")]
    pay = [c for c in swap if _local(c.tag) in ("fixedPmntDesc", "floatingPmntDesc", "otherPmntDesc")]
    rec_other = any(_local(c.tag) == "otherRecDesc" for c in rec)
    pay_other = any(_local(c.tag) == "otherPmntDesc" for c in pay)
    if rec_other != pay_other:
        return "long" if rec_other else "short"
    words = ["RETURN", "PERFORMANCE"] + [w for w in (norm_name((ref or {}).get("name")) or "").split() if len(w) > 3]

    def mentions(legs):
        text = " ".join(" ".join(c.itertext()) + " " + " ".join(c.attrib.values()) for c in legs).upper()
        return any(w in text for w in words)

    r, p = mentions(rec), mentions(pay)
    if r != p:
        return "long" if r else "short"
    return None


def _derivative(d):
    kind = DERIV_KINDS.get(_local(d.tag), "other")
    out = {"kind": kind, "notional": num(_text(d, "notionalAmt")), "ref": None,
           "direction": None, "direction_src": None}
    desc = _child(d, "descRefInstrmnt")
    if desc is not None:
        out["ref"] = _reference(desc)
    if kind == "swap":
        out["direction"] = _swap_direction(d, out["ref"])
    elif kind == "future":
        p = (_text(d, "payOffProf") or "").lower()
        out["direction"] = p if p in ("long", "short") else None
    elif kind == "option":
        pc, wr = (_text(d, "putOrCall") or "").lower(), (_text(d, "writtenOrPur") or "").lower()
        out.update(shares=num(_text(d, "shareNo")), strike=num(_text(d, "exercisePrice")),
                   delta=num(_text(d, "delta")), put_call=pc, written=wr)
        if pc in ("call", "put") and wr in ("written", "purchased"):
            out["direction"] = "long" if (pc == "call") == (wr == "purchased") else "short"
    if out["notional"] is not None and out["notional"] < 0:
        out["notional"] = -out["notional"]
        out["direction"] = out["direction"] or "short"
    if out["direction"]:
        out["direction_src"] = "filing"
    return out


def _holding(el):
    ids = _ids(_child(el, "identifiers"))
    h = {
        "name": _text(el, "name") or "", "title": _text(el, "title") or "",
        "isin": ids.get("isin"), "cusip": _text(el, "cusip") or ids.get("cusip"),
        "ticker": ids.get("ticker"),
        "pct": (num(_text(el, "pctVal")) or 0.0) / 100.0,
        "balance": num(_text(el, "balance")), "units": (_text(el, "units") or "").upper(),
        "payoff": (_text(el, "payoffProfile") or "").lower(),
        "cat": _cat(el, "assetCat", "assetConditional"),
        "issuer_cat": _cat(el, "issuerCat", "issuerConditional"),
        "country": (_text(el, "invCountry") or "").upper(),
    }
    info = _child(el, "derivativeInfo")
    if info is not None and len(info):
        h["deriv"] = _derivative(info[0])
    return h


def parse_nport(xml_bytes):
    """Report date, fund name, net assets and every holding from an N-PORT primary_doc.xml."""
    root = ET.fromstring(xml_bytes)
    out = {"as_of": None, "series": None, "net_assets": None, "holdings": []}
    for el in root.iter():
        n = _local(el.tag)
        if n == "repPdDate" and out["as_of"] is None:
            out["as_of"] = (el.text or "").strip() or None
        elif n == "seriesName" and out["series"] is None:
            out["series"] = (el.text or "").strip() or None
        elif n == "netAssets" and out["net_assets"] is None:
            out["net_assets"] = num(el.text)
        elif n == "invstOrSec":
            out["holdings"].append(_holding(el))
    return out


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
        return self.funds.get(sym.upper()) or self.funds.get((yahoo_symbol(sym) or sym).upper())

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
def _closes(hist, column="Close"):
    close = hist[column].dropna() if column in hist else None
    if close is None or not len(close):
        return None
    idx = close.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    close.index = idx.normalize()
    close = close[~close.index.duplicated(keep="last")].sort_index()
    return close


def fetch_history(yf, sym):
    """Daily history from Yahoo:
      close      split- and distribution-adjusted close ("Adj Close"): total return, used
                 for every return, beta and break-even on the screen;
      price      split-adjusted market price ("Close"), not adjusted for distributions;
      dividends  the distributions paid (per share, split-adjusted), by ex-date."""
    res = resolve_symbol(sym)
    if res.yahoo is None:
        return {"_error": listing_message(res) or "Yahoo has no price history for this ticker"}
    t = yf.Ticker(res.yahoo)
    hist = yahoo_call(lambda: t.history(period="max", interval="1d", auto_adjust=False, actions=True))
    close = _closes(hist, "Adj Close") if "Adj Close" in hist else _closes(hist)
    if close is None or len(close) < 20:
        return {"_error": "Yahoo has no price history for this ticker"}
    out = {"close": close, "price": _closes(hist, "Close")}
    divs = _closes(hist, "Dividends")
    out["dividends"] = divs[divs > 0] if divs is not None else None
    if sym != BENCHMARK:
        try:
            info = yahoo_call(lambda: t.info) or {}
        except Exception:
            info = {}
        out["name"] = info.get("longName") or info.get("shortName")
        out["quote_type"] = info.get("quoteType")
        out["aum"] = to_float(info.get("totalAssets"))
        er = to_float(info.get("netExpenseRatio"))          # already a percent
        if er is None:
            er = to_float(info.get("annualReportExpenseRatio"))
            er = er * 100 if er is not None and er < 1 else er
        out["expense"] = er
        out["yield"] = next((v for v in (to_float(info.get(k)) for k in
                                         ("yield", "trailingAnnualDividendYield", "dividendYield")) if v), None)
    return out


def parse_fred_csv(text):
    """FRED's CSV (DATE,DTB3 or observation_date,DTB3) as a Series of yields in %."""
    import pandas as pd
    dates, vals = [], []
    for line in text.splitlines()[1:]:
        parts = line.strip().split(",")
        if len(parts) >= 2 and to_float(parts[1]) is not None:
            dates.append(parts[0])
            vals.append(float(parts[1]))
    s = pd.Series(vals, index=pd.to_datetime(dates), dtype=float)
    return s[~s.index.duplicated(keep="last")].sort_index()


def fetch_rfr(yf):
    """Daily risk-free rate (13-week / 3-month T-bill yield, % per year)."""
    problems = []
    try:
        close = _closes(yahoo_call(lambda: yf.Ticker(RFR_SYMBOL).history(period="max", interval="1d", auto_adjust=False)))
        if close is not None:
            close = close[(close > -5) & (close < 40)]
        if close is not None and len(close) > 250:
            return {"rate": close, "source": "Yahoo ^IRX (13-week T-bill)"}
        problems.append("Yahoo returned no ^IRX history")
    except Exception as e:
        problems.append(f"Yahoo ^IRX failed ({type(e).__name__})")
    try:
        req = urllib.request.Request(FRED_DTB3, headers={"User-Agent": "short-borrow-monitor"})
        with urllib.request.urlopen(req, timeout=30) as r:
            s = parse_fred_csv(r.read().decode("utf-8", "replace"))
        if len(s) > 250:
            return {"rate": s, "source": "FRED DTB3 (3-month T-bill)"}
        problems.append("FRED returned no DTB3 data")
    except Exception as e:
        problems.append(f"FRED DTB3 failed ({type(e).__name__})")
    return {"_error": "Risk-free rate unavailable: " + "; ".join(problems)}


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
                    f = parse_nport(SEC.get(f"{folder}/primary_doc.xml"))
                    if f["holdings"]:
                        return {"source": "SEC N-PORT", "as_of": f["as_of"], "url": folder + "/",
                                "complete": True, "holdings": f["holdings"], "series": f["series"],
                                "net_assets": f["net_assets"], "notes": notes}
                    notes.append("SEC filing listed no holdings")
        except Exception as e:
            notes.append(f"SEC lookup failed ({type(e).__name__})")
    else:
        notes.append("SEC lookup is off (SEC_USER_AGENT not set), so only Yahoo's top 10 are used")

    ysym = yahoo_symbol(sym)
    if ysym is None:
        return {"_error": "; ".join(notes + ["Yahoo doesn't carry this ticker"])}
    t = yf.Ticker(ysym)
    try:
        top = yahoo_call(lambda: t.funds_data.top_holdings)
    except Exception:
        top = None
    holds = []
    if top is not None and len(top):
        for idx, row in top.iterrows():
            w = to_float(row.get("Holding Percent"))
            if w and w > 0:
                holds.append({"name": str(row.get("Name") or idx)[:120], "ticker": str(idx).upper(),
                              "pct": w, "yahoo": True})
    if holds:
        rest = 1.0 - sum(h["pct"] for h in holds)
        if rest > 0.001:
            holds.append({"name": "Everything outside the top 10", "pct": rest, "undisclosed": True})
        return {"source": "Yahoo top 10", "as_of": None, "url": None, "complete": False,
                "holdings": holds, "series": None, "net_assets": None, "notes": notes}
    try:
        if (yahoo_call(lambda: t.info) or {}).get("quoteType") == "EQUITY":
            return {"single_stock": True}
    except Exception:
        pass
    notes.append("Yahoo has no holdings for this ETF")
    return {"_error": "; ".join(notes)}


# --------------------------- pricing holdings ------------------------------
CASH_WORDS = re.compile(
    r"\b(CASH|MONEY MARKET|TREASURY BILLS?|T-?BILLS?|U\.?S\.? DOLLARS?|USD|COLLATERAL|REPURCHASE|"
    r"LIQUIDITY|GOVERNMENT (CASH|OBLIGATIONS|PORTFOLIO|MONEY|SECURITIES)|"
    r"TREASURY (OBLIGATIONS|INSTRUMENTS|PORTFOLIO|SECURITIES|MONEY|CASH))\b")
INDEX_WORDS = re.compile(r"\b(INDEX|BASKET)\b", re.I)
EQUITY_TYPES = ("Stock", "Preferred", "Fund", "Holding")
GROUPS = [   # summary order and labels
    ("matched", "Matched to an IBKR fee"), ("lookthrough", "Look-through to the underlying"),
    ("cash", "Cash & T-bills"), ("bond", "Bond: assumed rate"), ("short", "Short exposure"),
    ("nonequity", "Non-equity derivative"), ("unmatched", "Assumed (unmatched)"),
    ("excluded", "Excluded: no exposure reported"),
]
NEEDS_FEE = ("matched", "lookthrough", "unmatched", "bond")
EQUITY_GROUPS = ("matched", "lookthrough", "unmatched")


def holding_type(h):
    d = h.get("deriv")
    if d:
        return {"swap": "Swap", "future": "Future", "forward": "Forward", "option": "Option"}.get(d["kind"], "Derivative")
    if h.get("undisclosed"):
        return "Undisclosed"
    if h.get("residual"):
        return "Cash"
    cat, icat = h.get("cat", ""), h.get("issuer_cat", "")
    if cat == "STIV":
        return "Money market"
    if cat == "RA":
        return "Repo"
    if icat == "UST":
        return "Treasury"
    if cat == "EC":
        return "Fund" if icat == "RF" else "Stock"
    if cat == "EP":
        return "Preferred"
    if is_bond(h):
        return "Bond"
    return "Holding" if not cat else "Other"


def is_cash(h):
    return h.get("cat") in ("STIV", "RA") or h.get("issuer_cat") == "UST" or bool(h.get("residual"))


def is_bond(h):
    cat, icat = h.get("cat", ""), h.get("issuer_cat", "")
    return cat in ("DBT", "LON", "SN") or cat.startswith("ABS") or (
        icat in ("USGA", "USGSE", "MUN") and cat not in ("EC", "EP"))


def direction_from_name(name):
    n = (name or "").upper()
    if re.search(r"\b(BEAR|SHORT|INVERSE|ULTRASHORT)\b|-\s?\d(\.\d+)?X\b", n):
        return "short"
    if re.search(r"\b(BULL|LONG|ULTRA|ULTRAPRO)\b|\b\d(\.\d+)?X\b", n):
        return "long"
    return None


def proxy_for(index_name):
    for pattern, sym in INDEX_PROXIES:
        if re.search(pattern, index_name or "", re.I):
            return sym
    return None


def _display(s):
    s = (s or "").strip()
    return s.title() if s.isupper() else s


def non_us_from_entry(entry):
    """1.0 when an IBKR match is in a non-US file (the stock trades abroad), else 0.0."""
    return 0.0 if entry.get("file", "usa") == "usa" else 1.0


def non_us_from_holding(h):
    """For holdings without an IBKR match: Yahoo's exchange suffix, else the filing's country."""
    if h.get("yahoo"):
        return 1.0 if split_foreign(h.get("ticker"), yahoo=True) else 0.0
    country = (h.get("country") or "").upper()
    if not country:
        return None
    return 0.0 if country == "US" else 1.0


def _row(h, exposure, fee, how, group, detail="", matched=0.0, assumed=False, symbol=None, name=None,
         non_us=None):
    return {
        "name": (name or _display(h.get("name")) or _display(h.get("title")) or "Unnamed")[:120],
        "ticker": h.get("ticker") or symbol, "isin": h.get("isin"), "cusip": clean_cusip(h.get("cusip")),
        "type": holding_type(h), "cat": h.get("cat") or "", "issuer_cat": h.get("issuer_cat") or "",
        "exposure": exposure, "fee": fee, "assumed": assumed, "how": how, "group": group,
        "matched": matched, "contrib": max(exposure, 0.0) * fee if fee is not None else 0.0,
        "detail": detail, "non_us": non_us, "symbol": symbol,
    }


def price_security(h, idx, gc, cash_fee):
    exp = h.get("pct") or 0.0
    if h.get("undisclosed"):
        return _row(h, exp, gc, "Assumed (unmatched)", "unmatched",
                    "Yahoo only lists the top 10 holdings", assumed=True)      # where these trade isn't known
    if is_cash(h):
        detail = "Cash and other assets less liabilities" if h.get("residual") else "Not borrowed to replicate the fund"
        return _row(h, exp, cash_fee, "Cash & T-bills", "cash", detail)
    if exp < 0 or h.get("payoff") == "short":
        return _row(h, exp, 0.0, "Short exposure", "short", "The fund is short this, so no borrow is needed")
    if is_bond(h):
        return _row(h, exp, gc, "Bond: assumed rate", "bond",
                    "IBKR's stock loan file doesn't cover bonds", assumed=True)
    entry, how, tried = idx.find(h.get("isin"), h.get("cusip"), h.get("ticker"),
                                 names=(h.get("name"), h.get("title")),
                                 country=h.get("country"), yahoo=h.get("yahoo", False))
    if entry is not None and entry.get("fee") is not None:
        return _row(h, exp, entry["fee"], match_label(entry, how), "matched",
                    f"{entry['symbol']} · {entry.get('name') or ''}".strip(" ·"), matched=1.0,
                    symbol=entry["symbol"], non_us=non_us_from_entry(entry))
    text = f"{h.get('name', '')} {h.get('title', '')}".upper()
    if (h.get("issuer_cat") == "RF" or not h.get("cat")) and CASH_WORDS.search(text):
        return _row(h, exp, cash_fee, "Cash & T-bills", "cash", "Looks like a cash or money market fund")
    why = "; ".join(tried)
    if entry is not None:
        why = f"IBKR lists {entry['symbol']} without a fee; {why}"
    return _row(h, exp, gc, "Assumed (unmatched)", "unmatched", why, assumed=True,
                non_us=non_us_from_holding(h))


def weighted_non_us(pairs):
    """Exposure-weighted non-US share from (weight, non_us) pairs; None when nothing is known."""
    known = [(w, n) for w, n in pairs if n is not None and w > 0]
    total = sum(w for w, _ in known)
    return sum(w * n for w, n in known) / total if total > 0 else None


def _sleeve(rows):
    """The fund's own long stock holdings, used to stand in for an index it also has swaps on."""
    eq = [r for r in rows if r["group"] in ("matched", "unmatched") and r["type"] in EQUITY_TYPES
          and r["exposure"] > 0]
    total = sum(r["exposure"] for r in eq)
    if len(eq) < SLEEVE_MIN_HOLDINGS or total <= 0:
        return None
    return {"rate": sum(r["exposure"] * r["fee"] for r in eq) / total,
            "matched": sum(r["exposure"] * r["matched"] for r in eq) / total, "count": len(eq),
            "non_us": weighted_non_us((r["exposure"], r["non_us"]) for r in eq)}


def _underlying(ref, idx, gc, sleeve, proxy):
    """(fee, matched share, label, detail, pending, non-US share, look) for what a long
    derivative tracks; look says how it was looked through ({"symbol": IBKR ticker,
    "via": "stock" | "components" | "sleeve" | "proxy"}), for the Stocks page's look-through."""
    ref = ref or {}
    rname = _display(ref.get("name") or ref.get("title")) or "the reference asset"
    looks_like_index = bool(INDEX_WORDS.search(f"{ref.get('name') or ''} {ref.get('title') or ''}"))
    if ref.get("type") == "single":
        entry, how, tried = idx.find(ref.get("isin"), ref.get("cusip"), ref.get("ticker"),
                                     names=(ref.get("name"), ref.get("title")))
        if entry is not None and entry.get("fee") is not None:
            return (entry["fee"], 1.0, f"Look-through: {entry['symbol']}",
                    f"Tracks {rname}; fee via {match_label(entry, how)}", False, non_us_from_entry(entry),
                    {"symbol": entry["symbol"], "via": "stock"})
        if not looks_like_index:
            return gc, 0.0, "Assumed (unmatched)", f"Tracks {rname}, but {'; '.join(tried)}", False, None, {}
    if ref.get("type") == "index" or looks_like_index:
        comps = ref.get("components") or []
        if comps:
            total, acc, matched, where = 0.0, 0.0, 0.0, []
            for c in comps:
                w = abs(c["amount"]) if c.get("amount") else 1.0
                entry, _, _ = idx.find(c.get("isin"), c.get("cusip"), c.get("ticker"), names=(c.get("name"),))
                ok = entry is not None and entry.get("fee") is not None
                acc += w * (entry["fee"] if ok else gc)
                matched += w if ok else 0.0
                total += w
                if ok:
                    where.append((w, non_us_from_entry(entry)))
            return (acc / total, matched / total, f"Look-through: {rname}",
                    f"{len(comps)} components listed in the filing; {matched / total:.0%} of them matched", False,
                    weighted_non_us(where), {"via": "components"})
        if sleeve:
            return (sleeve["rate"], sleeve["matched"], f"Look-through: {rname}",
                    f"Priced from the fund's own {sleeve['count']} stock holdings, as a stand-in for the index", False,
                    sleeve.get("non_us"), {"via": "sleeve"})
        p = proxy(ref.get("name") or "") if proxy else None
        if p and p[0] == "ok":
            return (p[2], p[3], f"Look-through: {rname}",
                    f"Priced from {p[1]}'s holdings, which track this index", False, p[4] if len(p) > 4 else None,
                    {"via": "proxy", "proxy": p[1]})
        if p and p[0] == "pending":
            return gc, 0.0, "Assumed (unmatched)", f"Index {rname}: {p[1]}'s holdings are still loading", True, None, {}
        why = (f"couldn't load {p[1]}'s holdings ({p[2]})" if p else
               "no components listed and no proxy ETF set for it (see INDEX_PROXIES in etf.py)")
        return gc, 0.0, "Assumed (unmatched)", f"Index {rname}: {why}", False, None, {}
    return gc, 0.0, "Assumed (unmatched)", "The filing doesn't say what this tracks", False, None, {}


def price_derivative(h, idx, gc, nav, fund_name, sleeve, proxy):
    d = h["deriv"]
    name = _display(h.get("title")) or _display(h.get("name"))
    if h.get("cat") in ("DIR", "DCR", "DFE", "DCO") or d["kind"] == "forward":
        exp = d["notional"] / nav if d.get("notional") and nav else 0.0
        exp = -exp if d.get("direction") == "short" else exp
        return _row(h, exp, 0.0, "Non-equity derivative", "nonequity",
                    "Rates, currency, credit or commodity exposure needs no stock borrow", name=name), False
    direction, src = d.get("direction"), d.get("direction_src")
    if direction is None:
        direction = direction_from_name(fund_name)
        src = "inferred from the fund's name" if direction else None
    if direction is None:
        direction, src = "long", "assumed long (the filing doesn't say)"
    src = "as stated in the filing" if src == "filing" else src
    exposure, approx = None, ""
    if d["kind"] == "option":
        contracts = abs(h["balance"]) if h.get("units") == "NC" and h.get("balance") else 1.0
        if d.get("delta") is not None and d.get("shares") and d.get("strike") and nav:
            exposure = abs(d["delta"]) * d["shares"] * contracts * d["strike"] / nav
            approx = "delta × shares × strike (strike stands in for the price)"
    elif d.get("notional") and nav:
        exposure = d["notional"] / nav
    if direction == "short":
        return _row(h, -(exposure or 0.0), 0.0, "Short exposure", "short",
                    f"Short exposure ({src}), so no borrow is needed", name=name), False
    if exposure is None:
        what = "delta-adjusted exposure can't be computed" if d["kind"] == "option" else "no notional amount"
        return _row(h, 0.0, None, "Excluded: no exposure reported", "excluded",
                    f"The filing gives {what}", name=name), False
    fee, matched, how, detail, pending, non_us, look = _underlying(d.get("ref"), idx, gc, sleeve, proxy)
    notes = [detail, f"exposure {exposure:.1%} of net assets from notional" if not approx else approx]
    if src != "as stated in the filing":
        notes.append(f"direction {src}")
    group = "lookthrough" if how.startswith("Look-through") else "unmatched"
    row = _row(h, exposure, fee, how, group, "; ".join(notes), matched=matched,
               assumed=matched < 1.0, name=name, non_us=non_us, symbol=look.get("symbol"))
    row["via"], row["proxy"] = look.get("via"), look.get("proxy")
    return row, pending


def price_holdings(hold, idx, gc, cash_fee, fund_name=None, proxy=None):
    """Every position with its fee and where it came from, plus the totals."""
    holds = list(hold.get("holdings") or [])
    nav = hold.get("net_assets")
    rows, derivs = [], []
    for h in holds:
        (derivs if h.get("deriv") else rows).append(h)
    rows = [price_security(h, idx, gc, cash_fee) for h in rows]
    if hold.get("source") == "SEC N-PORT":
        rest = 1.0 - sum(h.get("pct") or 0.0 for h in holds)
        if abs(rest) > 1e-4:
            rows.append(price_security({"name": "Cash and other assets less liabilities", "pct": rest,
                                        "residual": True}, idx, gc, cash_fee))
    sleeve = _sleeve(rows)
    pending = False
    for h in derivs:
        row, p = price_derivative(h, idx, gc, nav, fund_name or hold.get("series") or "", sleeve, proxy)
        rows.append(row)
        pending = pending or p
    rows.sort(key=lambda r: abs(r["exposure"]), reverse=True)
    return summarize(rows, pending)


def summarize(rows, pending=False):
    def pos(r):
        return max(r["exposure"], 0.0)

    need = [r for r in rows if r["group"] in NEEDS_FEE]
    need_w = sum(pos(r) for r in need)
    matched_w = sum(pos(r) * r["matched"] for r in need)
    eq = [r for r in rows if r["group"] in EQUITY_GROUPS]
    gross = sum(pos(r) for r in eq)
    by_group = []
    for key, label in GROUPS:
        g = [r for r in rows if r["group"] == key]
        if g:
            by_group.append({"group": key, "label": label, "count": len(g),
                             "weight": sum(r["exposure"] for r in g), "contrib": sum(r["contrib"] for r in g)})
    unmatched = sorted((r for r in need if r["matched"] < 1.0 and pos(r) > 0),
                       key=lambda r: pos(r) * (1 - r["matched"]), reverse=True)
    return {
        "fee": sum(r["contrib"] for r in rows),
        "coverage": matched_w / need_w if need_w > 0 else None,
        "count": len(rows),
        "need_count": len(need),
        "matched": sum(1 for r in need if r["matched"] > 0),
        "gross_long_equity": gross,
        "cash_weight": sum(pos(r) for r in rows if r["group"] == "cash"),
        "assumed_weight": need_w - matched_w,
        "equity_rate": sum(r["contrib"] for r in eq) / gross if gross > 0 else None,
        "equity_matched": sum(pos(r) * r["matched"] for r in eq) / gross if gross > 0 else None,
        # share of long stock exposure that trades outside the US (decides daily vs weekly beta)
        "foreign_share": weighted_non_us((pos(r), r.get("non_us")) for r in eq),
        "by_group": by_group,
        "unmatched": [{"name": r["name"], "weight": pos(r) * (1 - r["matched"]), "detail": r["detail"]}
                      for r in unmatched[:10]],
        "pending": pending,
        "rows": rows,
    }


def holdings_warning(hold):
    if hold.get("source") == "SEC N-PORT":
        return None
    if not SEC_USER_AGENT:
        return ("Only Yahoo's top 10 holdings are used, so everything else is charged the assumed rate. "
                "For the full list from the SEC filing, set SEC_USER_AGENT to your name and email "
                "in Render → Environment.")
    return ("Couldn't use this ETF's SEC filing, so only Yahoo's top 10 holdings are used "
            "and everything else is charged the assumed rate.")


# ------------------------------- history ----------------------------------
def beta_returns(close, spy, start=None, end=None, weekly=False):
    """Returns of the ETF (x) and SPY (m) on the days both traded, daily or Friday to Friday."""
    import pandas as pd
    df = pd.concat({"x": close, "m": spy}, axis=1, join="inner").dropna()
    if start is not None:
        df = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df[df.index <= pd.Timestamp(end)]
    if weekly and len(df):
        df = df.resample("W-FRI").last().dropna()
    return df.pct_change().dropna()


def beta_from(r, weekly=False):
    """cov / var of a returns frame from beta_returns, with the correlation and count."""
    n = len(r)
    freq = "weekly" if weekly else "daily"
    minimum, full = (BETA_MIN_WEEKS, BETA_FULL_WEEKS) if weekly else (BETA_MIN_DAYS, BETA_FULL_DAYS)
    var = float(r["m"].var()) if n > 1 else 0.0
    if n < minimum or not var > 0:
        return {"beta": None, "corr": None, "days": n, "freq": freq}
    return {"beta": float(r["x"].cov(r["m"]) / var), "corr": float(r["x"].corr(r["m"])),
            "days": n, "freq": freq, "partial": n < full,
            "start": r.index[0].strftime("%Y-%m-%d"), "end": r.index[-1].strftime("%Y-%m-%d")}


def compute_beta(close, spy, weekly=False):
    """Beta vs SPY over the last year, for sizing a hedge today ("days" counts the returns used)."""
    import pandas as pd
    both = pd.concat({"x": close, "m": spy}, axis=1, join="inner").dropna()
    if both.empty:
        return {"beta": None, "corr": None, "days": 0, "freq": "weekly" if weekly else "daily"}
    start = both.index[-1] - pd.DateOffset(years=1)
    return beta_from(beta_returns(close, spy, start=start, weekly=weekly), weekly)


def window_beta(close, spy, start, end, beta1y=None, weekly=False, same_as_1y=False):
    """Beta over a window's own dates. Falls back to the 1-year beta when the window is too short."""
    one = (beta1y or {}).get("beta")
    if same_as_1y and beta1y is not None:
        wb = dict(beta1y)
    else:
        wb = beta_from(beta_returns(close, spy, start, end, weekly), weekly)
    if wb.get("beta") is None and one is not None:
        wb = dict(beta1y, fallback=True, note="Window too short for its own beta; using the last year's")
    used = wb.get("beta")
    changed = bool(not wb.get("fallback") and used is not None and one
                   and abs(used - one) > BETA_CHANGE_FLAG * abs(one))
    return wb, used, changed


def rfr_over(rate, start, end, spread=0.0):
    """Return on cash from start to end: each calendar day earns max(T-bill yield - spread, 0)
    on actual/360, compounded, then annualized the same way as the ETF's return.

    "ann"/"total" are this cash rate (what short proceeds earn); "tbill_ann"/"tbill_total"
    are the plain T-bill rate, and "floor_days" counts days the spread was above the yield."""
    import numpy as np
    import pandas as pd
    if rate is None or not len(rate) or end <= start:
        return None
    if rate.index[0] > start + pd.Timedelta(days=31) or rate.index[-1] < end - pd.Timedelta(days=31):
        return None
    days = pd.date_range(start, end - pd.Timedelta(days=1), freq="D")
    y = rate.reindex(rate.index.union(days)).ffill().bfill().reindex(days)
    if y.isna().any():
        return None
    yields = y.to_numpy(dtype=float)
    earned = np.maximum(yields - spread, 0.0)
    yrs = (end - start).days / 365.25

    def grow(pct):
        g = float(np.exp(np.log1p(pct / 100.0 / 360.0).sum()))
        return g - 1, g ** (1 / yrs) - 1

    cash_total, cash_ann = grow(earned)
    tbill_total, tbill_ann = grow(np.maximum(yields, 0.0))
    return {"total": cash_total, "ann": cash_ann, "tbill_total": tbill_total, "tbill_ann": tbill_ann,
            "spread": spread, "floor_days": int((yields - spread < 0).sum()), "days": len(yields)}


def beta_adjusted(r_x, r_spy, rfr, beta, fee=None):
    """Beta-adjusted break-evens. All inputs and outputs are % per year.

    `rfr` is the cash rate short proceeds earn (the T-bill rate minus the spread), so
    shorting SPY gives -1.00 beta at a hedge cost of r - R_SPY. Shorting the ETF at fee f
    gives -beta at a cost of r - R_X - f; levered to -1.00 beta that's (r - R_X - f) / beta,
    which also earns r on the larger (or smaller) proceeds.
      be_cash = r - R_X                          (short profits after the cash rate)
      be_beta = beta * (R_SPY - r) - (R_X - r)   (as cheap a hedge as shorting SPY)
    """
    eps = 1e-9
    out = {"spy_hedge_cost": rfr - r_spy, "be_cash": rfr - r_x, "be_beta": None,
           "etf_hedge_cost": rfr - r_x - fee if fee is not None else None,
           "hedge_cost_beta": None, "leverage": None, "verdict": None,
           "headroom": None, "headroom_beta": None}
    if beta is None or beta < MIN_HEDGE_BETA:
        out["verdict"] = "low"
        return out
    out["be_beta"] = beta * (r_spy - rfr) - (r_x - rfr)
    out["leverage"] = 1 / beta
    if fee is not None:
        out["hedge_cost_beta"] = (rfr - r_x - fee) / beta
        out["headroom"] = out["be_beta"] - fee
        out["headroom_beta"] = out["headroom"] / beta
        out["verdict"] = ("go" if fee <= out["be_beta"] + eps else
                          "meh" if fee <= out["be_cash"] + eps else "no")
    return out


def window_slice(close, spy, years=None, start=None):
    """The dates a window covers, used by both the table and the chart.

    Returns {"etf", "spy", "requested", "clamped"} (both series cut to the window's
    trading days), None when a preset window is longer than the ETF's history, or
    {"error", "requested_start"} for a custom start that leaves too few days.
    """
    import pandas as pd

    c = close
    requested, clamped = None, False
    if years:
        s0 = c.index[-1] - pd.DateOffset(years=years)
        if c.index[0] > s0 + pd.Timedelta(days=7):
            return None                    # ETF is younger than the window
        c = c[c.index >= s0]
    elif start is not None:
        requested = pd.Timestamp(start)
        if requested > c.index[-1]:
            return {"error": f"There are no prices after {requested:%Y-%m-%d} yet. Pick an earlier date.",
                    "requested_start": f"{requested:%Y-%m-%d}"}
        clamped = requested < c.index[0]
        c = c[c.index >= requested]
    s = spy.reindex(c.index).ffill()
    keep = s.notna()
    c, s = c[keep], s[keep]
    if len(c) < MIN_WINDOW_DAYS:
        if requested is not None:
            return {"error": f"Only {len(c)} trading days from {requested:%Y-%m-%d}. Pick an earlier date "
                             f"(at least {MIN_WINDOW_DAYS} are needed).",
                    "requested_start": f"{requested:%Y-%m-%d}"}
        return None
    return {"etf": c, "spy": s, "requested": requested, "clamped": clamped}


def analyze_window(close, spy, years=None, start=None, rfr=None, beta1y=None, fee=None, weekly=False):
    """Returns, break-even fees and the beta-adjusted hedge comparison over one window.

    Preset windows (years) return None when the ETF is younger than the window. A
    custom start date starts at inception if it's earlier than that, and returns
    {"error": ...} when fewer than MIN_WINDOW_DAYS trading days are left.

    Beta comes from the window's own returns (window_beta), with the 1-year beta as
    the fallback; `rfr` gives the cash rate earned on short proceeds over the dates.

    The previous, path-based break-evens are kept: short P&L (fraction of starting
    notional) = -R_etf - fee * A, where A = sum over days of (short's market value /
    starting notional) * days / 360, because borrow accrues daily on the current value.
    """
    win = window_slice(close, spy, years, start)
    if win is None or "error" in win:
        return win
    c, s, requested, clamped = win["etf"], win["spy"], win["requested"], win["clamped"]

    rel = c / c.iloc[0]
    gaps = c.index.to_series().diff().dt.days.iloc[1:].to_numpy()
    a = float((rel.iloc[:-1].to_numpy() * gaps).sum() / 360.0)
    days = (c.index[-1] - c.index[0]).days
    yrs = days / 365.25
    r_etf = float(rel.iloc[-1] - 1)
    r_spy = float(s.iloc[-1] / s.iloc[0] - 1)

    def ann(r):
        return (1 + r) ** (1 / yrs) - 1 if r > -1 and yrs > 0 else None

    out = {
        "start": c.index[0].strftime("%Y-%m-%d"),
        "end": c.index[-1].strftime("%Y-%m-%d"),
        "requested_start": f"{requested:%Y-%m-%d}" if requested is not None else None,
        "clamped": clamped,
        "years": yrs,
        "etf_total": r_etf, "spy_total": r_spy,
        "etf_ann": ann(r_etf), "spy_ann": ann(r_spy),
        "a": a,
        "avg_size": a / (days / 360.0) if days else None,
        # previous method, fees in % per year
        "be_profit": -r_etf / a * 100 if a > 0 else None,
        "be_spy": (-r_etf - r_spy) / a * 100 if a > 0 else None,
        "be_hedged": (r_spy - r_etf) / a * 100 if a > 0 else None,
        "rfr_total": None, "rfr_ann": None,          # the cash rate used (T-bill minus spread)
        "tbill_total": None, "tbill_ann": None, "spread": None, "floor_days": None,
    }
    wb, used, changed = window_beta(close, spy, c.index[0], c.index[-1], beta1y, weekly,
                                    same_as_1y=years == 1)
    out.update(beta_window=wb, beta_used=used, beta_changed=changed)
    rf = rfr(c.index[0], c.index[-1]) if rfr else None
    if rf:
        out["rfr_total"], out["rfr_ann"] = rf["total"], rf["ann"]
        out["tbill_total"], out["tbill_ann"] = rf.get("tbill_total", rf["total"]), rf.get("tbill_ann", rf["ann"])
        out["spread"], out["floor_days"] = rf.get("spread", 0.0), rf.get("floor_days", 0)
        if out["etf_ann"] is not None and out["spy_ann"] is not None:
            out.update(beta_adjusted(out["etf_ann"] * 100, out["spy_ann"] * 100, rf["ann"] * 100, used, fee))
    return out


# ------------------------------ price chart ---------------------------------
CHART_MAX_POINTS = 1000
CHART_WINDOWS = {"inception": None, "3y": 3, "1y": 1}


def chart_points(index, max_points=CHART_MAX_POINTS):
    """Positions to plot: every day if they fit, else the last day of each week (then
    every n-th week), always keeping the first and last day."""
    import pandas as pd

    n = len(index)
    if n <= max_points:
        return list(range(n))
    weeks = pd.Series(range(n), index=index).groupby(index.to_period("W-FRI")).last().tolist()
    if len(weeks) > max_points - 1:
        step = math.ceil(len(weeks) / (max_points - 1))
        weeks = weeks[::step]
    return sorted({0, n - 1, *weeks})


def chart_data(hist, spy, years=None, start=None):
    """The ETF's market price and its total return (distributions reinvested) over the
    same dates as the table's window. Total return and SPY's are scaled to start at the
    first price: the value of one share bought then with every distribution reinvested."""
    close = hist["close"]
    price = hist.get("price")
    win = window_slice(close, spy["close"], years, start)
    if win is None:
        return {"error": f"Not enough history for this window (since {close.index[0]:%Y-%m-%d}).",
                "inception": f"{close.index[0]:%Y-%m-%d}"}
    if "error" in win:
        return win
    c, s = win["etf"], win["spy"]
    p = (price if price is not None else close).reindex(c.index).ffill().bfill()
    p0 = float(p.iloc[0])
    tr = c / c.iloc[0] * p0
    spy_tr = s / s.iloc[0] * p0
    divs = hist.get("dividends")
    if divs is not None and len(divs):
        divs = divs[(divs.index > c.index[0]) & (divs.index <= c.index[-1])]
    days = (c.index[-1] - c.index[0]).days
    yrs = days / 365.25

    def ann(r):
        return (1 + r) ** (1 / yrs) - 1 if r > -1 and yrs > 0 else None

    price_ret = float(p.iloc[-1] / p0 - 1)
    total_ret = float(c.iloc[-1] / c.iloc[0] - 1)
    notes = []
    if price is None:
        notes.append("Yahoo gave no unadjusted price, so both lines show total return.")
    ever = hist.get("dividends")
    if (ever is None or not len(ever)) and (hist.get("yield") or 0) > 0:
        notes.append("Yahoo shows no distributions for this fund, but it reports a yield, so the "
                     "total-return line may be missing payouts.")
    keep = chart_points(c.index)
    r4 = lambda v: round(float(v), 4)                       # noqa: E731
    return {
        "start": f"{c.index[0]:%Y-%m-%d}", "end": f"{c.index[-1]:%Y-%m-%d}",
        "requested_start": f"{win['requested']:%Y-%m-%d}" if win["requested"] is not None else None,
        "clamped": win["clamped"], "years": yrs, "weekly": len(keep) < len(c),
        "dates": [f"{c.index[i]:%Y-%m-%d}" for i in keep],
        "price": [r4(p.iloc[i]) for i in keep],
        "tr": [r4(tr.iloc[i]) for i in keep],
        "spy_tr": [r4(spy_tr.iloc[i]) for i in keep],
        "distributions": [{"date": f"{d:%Y-%m-%d}", "amount": round(float(v), 6)}
                          for d, v in (divs.items() if divs is not None else [])],
        "summary": {
            "price_return": price_ret, "total_return": total_ret,
            "price_ann": ann(price_ret), "total_ann": ann(total_ret),
            "spy_total": float(s.iloc[-1] / s.iloc[0] - 1),
            "distributions_per_share": float(divs.sum()) if divs is not None and len(divs) else 0.0,
            "distributions_count": int(len(divs)) if divs is not None else 0,
            "added_pts": (total_ret - price_ret) * 100,
        },
        "notes": notes,
    }


# ------------------------------- service ----------------------------------
class EtfService:
    def __init__(self, yf, borrow, foreign=None):
        self.yf, self.borrow, self.foreign = yf, borrow, foreign
        self.lock = threading.Lock()
        self.cache = {"hist": LRU(CACHE_LIMIT), "hold": LRU(CACHE_LIMIT), "rfr": LRU(4)}
        self.inflight = {}
        self.pool = ThreadPoolExecutor(max_workers=4)
        self._index = (None, None, None)      # (US table, foreign tables, MatchIndex)
        self._rf = {}                         # (rate id, start, end) -> rfr_over result

    def _fetch(self, kind, sym):
        if kind == "hist":
            return fetch_history(self.yf, sym)
        if kind == "hold":
            return fetch_holdings(self.yf, sym)
        return fetch_rfr(self.yf)

    def _run(self, kind, sym):
        try:
            data = self._fetch(kind, sym)
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
        max_age = ERROR_MAX_AGE if "_error" in data else (HOLD_MAX_AGE if kind == "hold" else HIST_MAX_AGE)
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

    def match_index(self, table):
        foreign = {}
        if self.foreign is not None:
            self.foreign.ensure_fresh()
            foreign = self.foreign.snapshot()[0]
        with self.lock:
            us, fx, idx = self._index
            if us is table and fx is foreign and idx is not None:
                return idx
        idx = MatchIndex(table, foreign)
        with self.lock:
            self._index = (table, foreign, idx)
        return idx

    def _rfr_fn(self, rate, spread=0.0):
        if rate is None:
            return None

        def fn(start, end):
            key = (id(rate), start, end, spread)
            with self.lock:
                if key in self._rf:
                    return self._rf[key]
            res = rfr_over(rate, start, end, spread)
            with self.lock:
                if len(self._rf) > 2000:
                    self._rf.clear()
                self._rf[key] = res
            return res
        return fn

    def _proxy_fn(self, idx, gc, cash, own, waiting):
        def proxy(index_name):
            sym = proxy_for(index_name)
            if not sym or sym == own:
                return None
            fut = self._ensure("hold", sym)
            data, _ = self._lookup("hold", sym)
            if data is None:
                if fut is not None:
                    waiting.append(fut)
                return ("pending", sym)
            if "_error" in data or data.get("single_stock"):
                return ("error", sym, data.get("_error", "not a fund"))
            s = price_holdings(data, idx, gc, cash, proxy=None)
            if s["equity_rate"] is None:
                return ("error", sym, "it has no stock holdings")
            return ("ok", sym, s["equity_rate"], s["equity_matched"], s["foreign_share"])
        return proxy

    def _price(self, sym, hold, idx, gc, cash, waiting, fund_name=None):
        return price_holdings(hold, idx, gc, cash, fund_name=hold.get("series") or fund_name,
                              proxy=self._proxy_fn(idx, gc, cash, sym, waiting))

    def _holdings_block(self, sym, hold, hist, idx, gc, cash, waiting, full=False):
        if hist and hist.get("quote_type") == "EQUITY" or (hold and hold.get("single_stock")):
            return {"single_stock": True}
        if hold is None:
            return {"pending": True}
        if "_error" in hold:
            return {"error": hold["_error"]}
        theo = self._price(sym, hold, idx, gc, cash, waiting, (hist or {}).get("name"))
        rows = theo.pop("rows")
        out = {"source": hold["source"], "as_of": hold["as_of"], "url": hold["url"],
               "complete": hold["complete"], "notes": hold.get("notes", []),
               "warning": holdings_warning(hold), "net_assets": hold.get("net_assets"), "theo": theo}
        if full:
            out["rows"] = rows
        else:
            theo["top"] = rows[:TOP_HOLDINGS_SHOWN]
        return out

    def holdings_detail(self, sym, gc, cash=CASH_DEFAULT):
        """Every holding of one ETF, with its fee and how it was matched (or why not)."""
        waiting = [f for f in (self._ensure("hold", sym), self._ensure("hist", sym)) if f]
        if waiting:
            wait(waiting, timeout=WAIT_SEC)
        self.borrow.ensure_fresh()
        table = self.borrow.snapshot()[0]
        idx = self.match_index(table)
        hold, _ = self._lookup("hold", sym)
        hist, _ = self._lookup("hist", sym)
        proxies = []
        block = self._holdings_block(sym, hold, hist, idx, gc, cash, proxies, full=True)
        if proxies:
            wait(proxies, timeout=WAIT_SEC)
            block = self._holdings_block(sym, hold, hist, idx, gc, cash, [], full=True)
        return dict(block, symbol=sym, gc=gc, cash=cash)

    def lookthrough_weights(self, sym, timeout=WAIT_SEC, depth=0):
        """A fund's biggest US stock positions, for the Stocks page's look-through
        averages: {"weights": [(IBKR ticker, share of net assets), ...], "equity": all
        stock exposure (swaps on a stock or the fund's own index count too), "count":
        holdings in the filing (None for Yahoo's top 10), "source", "as_of"}. None while
        the holdings are still loading."""
        fut = self._ensure("hold", sym)
        if fut is not None:
            wait([fut], timeout=timeout)
        hold, _ = self._lookup("hold", sym)
        if hold is None:
            return None
        if "_error" in hold:
            return {"_error": hold["_error"]}
        if hold.get("single_stock"):
            return {"_error": "Not a fund"}
        self.borrow.ensure_fresh()
        idx = self.match_index(self.borrow.snapshot()[0])
        waiting = []
        rows = self._price(sym, hold, idx, GC_DEFAULT, CASH_DEFAULT, waiting)["rows"]
        if waiting:
            wait(waiting, timeout=timeout)
            rows = self._price(sym, hold, idx, GC_DEFAULT, CASH_DEFAULT, [])["rows"]
        by_sym, equity, sleeve, proxies = {}, 0.0, 0.0, {}
        for r in rows:
            if r["group"] not in EQUITY_GROUPS or r["exposure"] <= 0:
                continue
            if r["group"] != "lookthrough" and r["type"] not in EQUITY_TYPES + LOOK_THROUGH_EXTRA:
                continue
            equity += r["exposure"]
            if r.get("via") == "sleeve":            # an index swap standing on the fund's own stocks
                sleeve += r["exposure"]
            elif r.get("via") == "proxy" and r.get("proxy") and depth == 0:
                proxies[r["proxy"]] = proxies.get(r["proxy"], 0.0) + r["exposure"]
            elif r.get("symbol") and not r.get("non_us"):
                by_sym[r["symbol"]] = by_sym.get(r["symbol"], 0.0) + r["exposure"]
        direct = sum(by_sym.values())
        if sleeve and direct:
            by_sym = {k: v * (direct + sleeve) / direct for k, v in by_sym.items()}
        for proxy, exposure in proxies.items():     # an index swap priced from an ETF that holds the index
            pw = self.lookthrough_weights(proxy, timeout, depth=1)
            if pw is None:
                return None
            if "_error" in pw or not pw.get("equity"):
                continue
            for s, w in pw["weights"]:
                by_sym[s] = by_sym.get(s, 0.0) + exposure * w / pw["equity"]
        weights = sorted(by_sym.items(), key=lambda kv: -kv[1])[:LOOK_THROUGH_TOP]
        count = (sum(1 for h in hold.get("holdings") or [] if not h.get("undisclosed"))
                 if hold.get("complete") else None)
        return {"weights": weights, "equity": equity, "count": count,
                "source": hold.get("source"), "as_of": hold.get("as_of")}

    def chart(self, sym, years=None, start=None):
        """Price vs total return for one ETF (the details panel's chart), from cached history."""
        waiting = [f for f in (self._ensure("hist", BENCHMARK), self._ensure("hist", sym)) if f]
        if waiting:
            wait(waiting, timeout=WAIT_SEC)
        spy, _ = self._lookup("hist", BENCHMARK)
        hist, _ = self._lookup("hist", sym)
        if hist is None or spy is None:
            return {"symbol": sym, "pending": True}
        if "_error" in hist:
            return {"symbol": sym, "error": hist["_error"], "unknown": True}
        if "_error" in spy:
            return {"symbol": sym, "error": "Couldn't load SPY history from Yahoo"}
        return dict(chart_data(hist, spy, years, start), symbol=sym)

    def report(self, symbols, gc, cash=CASH_DEFAULT, start=None, spread=RFR_SPREAD_DEFAULT):
        t0 = time.time()
        waiting = [self._ensure("hist", BENCHMARK), self._ensure("rfr", "RFR")]
        for s in symbols:
            waiting += [self._ensure("hist", s), self._ensure("hold", s)]
        waiting = [f for f in waiting if f is not None]
        if waiting:
            wait(waiting, timeout=WAIT_SEC)

        self.borrow.ensure_fresh()
        table, _, as_of, error, refreshing = self.borrow.snapshot()
        idx = self.match_index(table)
        spy, _ = self._lookup("hist", BENCHMARK)
        spy_close = spy.get("close") if spy and "_error" not in spy else None
        rf, _ = self._lookup("rfr", "RFR")
        rate = rf.get("rate") if rf and "_error" not in rf else None
        rfr_fn = self._rfr_fn(rate, spread)
        rfr_state = ({"state": "ok", "source": rf["source"]} if rate is not None else
                     {"state": "loading"} if rf is None else {"state": "error", "text": rf["_error"]})

        def blocks(proxy_wait):
            out = {}
            for s in symbols:
                hold, _ = self._lookup("hold", s)
                hist, _ = self._lookup("hist", s)
                out[s] = self._holdings_block(s, hold, hist, idx, gc, cash, proxy_wait)
            return out

        proxy_wait = []
        holdings = blocks(proxy_wait)
        if proxy_wait:
            wait(proxy_wait, timeout=max(1.0, WAIT_SEC - (time.time() - t0)))
            holdings = blocks([])

        rows, pending = [], 0
        for s in symbols:
            loan = table.get(s)
            fee = loan["fee"] if loan else None
            row = {"symbol": s, "quote": {
                "listed": loan is not None,
                "fee": fee,
                "rebate": loan["rebate"] if loan else None,
                "shares": loan["shares"] if loan else None,
                "plus": loan["plus"] if loan else False,
                "name": loan["name"] if loan else None,
            }}

            hist, _ = self._lookup("hist", s)
            if hist is None or spy is None:
                row["history"] = {"pending": True}
                pending += 1
            elif "_error" in hist:
                row["history"] = {"error": hist["_error"]}
            elif spy_close is None:
                row["history"] = {"error": "Couldn't load SPY history from Yahoo"}
            else:
                close = hist["close"]
                row["last_close"] = float(close.iloc[-1])
                row["last_date"] = close.index[-1].strftime("%Y-%m-%d")
                row["name"] = hist.get("name")
                row["aum"] = hist.get("aum")
                row["expense"] = hist.get("expense")
                row["quote_type"] = hist.get("quote_type")
                # mostly non-US funds use weekly returns (their markets close before the US does)
                foreign = (holdings[s].get("theo") or {}).get("foreign_share")
                weekly = foreign is not None and foreign > WEEKLY_ABOVE
                beta = compute_beta(close, spy_close, weekly)
                windows = {k: analyze_window(close, spy_close, years=y, rfr=rfr_fn, beta1y=beta, fee=fee,
                                             weekly=weekly)
                           for k, y in WINDOWS.items()}
                if start is not None:
                    windows["custom"] = analyze_window(close, spy_close, start=start, rfr=rfr_fn,
                                                       beta1y=beta, fee=fee, weekly=weekly)
                row["history"] = {"inception": close.index[0].strftime("%Y-%m-%d"),
                                  "windows": windows, "beta": beta, "weekly": weekly,
                                  "foreign_share": foreign}

            row["holdings"] = holdings[s]
            if row["holdings"].get("pending") or (row["holdings"].get("theo") or {}).get("pending"):
                pending += 1
            rows.append(row)

        if rfr_state["state"] == "loading":
            pending += 1
        foreign_state = None
        if self.foreign is not None:
            tables, fx_refreshing, fx_error = self.foreign.snapshot()
            foreign_state = ({"state": "ok", "files": sorted(tables)} if tables else
                             {"state": "loading"} if fx_refreshing else {"state": "error", "text": fx_error})
            if foreign_state["state"] == "loading" and any(r["holdings"].get("theo") for r in rows):
                pending += 1                  # foreign holdings will match once these load
        borrow_state = ({"state": "ok", "as_of": as_of} if table else
                        {"state": "error", "text": error} if error and not refreshing else
                        {"state": "loading"})
        return {"rows": rows, "pending": pending, "gc": gc, "cash": cash, "spread": spread,
                "borrow": borrow_state,
                "rfr": rfr_state, "foreign": foreign_state, "sec": bool(SEC_USER_AGENT),
                "start": start.isoformat() if start else None}
