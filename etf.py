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

import html
import io
import json
import math
import os
import re
import statistics
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime

import ident
from ident import id_key
from common import (LRU, clean_cusip, cusip_to_isin, full_isin, listing_message, resolve_symbol,  # noqa: F401
                    yahoo_call, yahoo_symbol)

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
    (r"RUSSELL\s*2000|\bRTY\b|\bRUT\b", "IWM"),
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
    (r"S&P\s*500|\bSPXW?\b|\bXSP\b", "IVV"),
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
# Markets, the IBKR file names they may use, and how Yahoo, Bloomberg, ISO country
# codes and currencies point at them. Markets missing here use the ISO code itself
# ("kr" stays Korea's code only when it isn't listed): only IBKR's file names matter.
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
    "CO": "denmark", "OL": "norway", "HE": "finland", "SI": "singapore", "KS": "korea", "KQ": "korea",
    "TW": "taiwan", "SA": "brazil", "SS": "china", "SZ": "china", "JO": "southafrica", "SR": "saudi",
    "IS": "turkey", "JK": "indonesia", "BK": "thailand", "KL": "malaysia", "TA": "israel",
}
BBG_CODE = {
    "JP": "japan", "JT": "japan", "LN": "uk", "CN": "canada", "CT": "canada", "HK": "hongkong",
    "NA": "netherlands", "FP": "france", "GY": "germany", "GR": "germany", "SW": "switzerland",
    "SE": "switzerland", "VX": "switzerland", "SM": "spain", "SQ": "spain", "IM": "italy",
    "SS": "sweden", "AU": "australia", "AT": "australia", "BB": "belgium", "AV": "austria",
    "MM": "mexico", "IN": "india", "IS": "india", "DC": "denmark", "NO": "norway",
    "FH": "finland", "SP": "singapore", "KS": "korea", "TT": "taiwan", "BZ": "brazil",
    "CH": "china", "SJ": "southafrica", "AB": "saudi",
}
ISO_MARKET = {
    "JP": "japan", "GB": "uk", "CA": "canada", "HK": "hongkong", "NL": "netherlands",
    "FR": "france", "DE": "germany", "CH": "switzerland", "ES": "spain", "IT": "italy",
    "SE": "sweden", "AU": "australia", "BE": "belgium", "AT": "austria", "MX": "mexico",
    "IN": "india", "DK": "denmark", "NO": "norway", "FI": "finland", "SG": "singapore",
    "KR": "korea", "TW": "taiwan", "BR": "brazil", "CN": "china", "ZA": "southafrica",
    "SA": "saudi", "AE": "uae", "ID": "indonesia", "TH": "thailand", "MY": "malaysia",
    "PH": "philippines", "PL": "poland", "TR": "turkey", "CL": "chile", "PE": "peru",
    "CO": "colombia", "QA": "qatar", "KW": "kuwait", "GR": "greece", "HU": "hungary",
    "CZ": "czech", "EG": "egypt", "IL": "israel", "NZ": "newzealand", "IE": "ireland",
    "PT": "portugal", "AR": "argentina",
}
CURRENCY_MARKET = {
    "HKD": "hongkong", "JPY": "japan", "GBP": "uk", "GBX": "uk", "CAD": "canada", "CHF": "switzerland",
    "SEK": "sweden", "AUD": "australia", "MXN": "mexico", "DKK": "denmark", "NOK": "norway",
    "KRW": "korea", "TWD": "taiwan", "INR": "india", "BRL": "brazil", "CNY": "china", "CNH": "china",
    "ZAR": "southafrica", "SGD": "singapore", "SAR": "saudi", "AED": "uae", "IDR": "indonesia",
    "THB": "thailand", "MYR": "malaysia", "PHP": "philippines", "PLN": "poland", "TRY": "turkey",
    "CLP": "chile", "PEN": "peru", "COP": "colombia", "QAR": "qatar", "KWD": "kuwait", "HUF": "hungary",
    "CZK": "czech", "EGP": "egypt", "ILS": "israel", "NZD": "newzealand",
}
MARKET_LABELS = {"uk": "UK", "us": "US", "hongkong": "Hong Kong", "southafrica": "South Africa",
                 "newzealand": "New Zealand", "uae": "UAE", "czech": "Czech Republic"}
# Words that don't tell companies apart, including depositary receipt wording
# ("TOYOTA MOTOR CORP -SPON ADR" is Toyota Motor).
NAME_DROP = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COMPANIES", "COS", "LTD", "LIMITED",
    "PLC", "SA", "NV", "AG", "SE", "SPA", "AB", "ASA", "OYJ", "HOLDINGS", "HOLDING", "HLDGS",
    "HLDG", "GROUP", "GRP", "THE", "COM", "COMMON", "STOCK", "SHS", "SHARES", "SHARE", "ORD",
    "ORDINARY", "NEW", "CLASS", "CL", "REG", "REGISTERED", "NPV", "LLC", "LP", "AMP",
    "ADR", "ADRS", "ADS", "GDR", "GDRS", "GDS", "SPON", "SPONSORED", "SPN", "SP", "UNSP", "UNSPON",
    "UNSPONSORED", "UNS", "DEPOSITARY", "RECEIPT", "RECEIPTS", "NY",
}
NO_NAME = {"N/A", "NA", "NONE", "-", "UNNAMED", "UNKNOWN"}
# Names IBKR's file still uses after a company renamed itself (checked October 2026:
# META is "Facebook Inc.", GOOG "Google Inc.", TSM "TSMC", SPCX "SpaceX"). Both sides of
# a comparison go through this, so "Meta Platforms" and IBKR's "Facebook" agree.
RENAMES = {
    "FACEBOOK": "META PLATFORMS", "GOOGLE": "ALPHABET", "TSMC": "TAIWAN SEMICONDUCTOR MANUFACTURING",
    "SPACEX": "SPACE EXPLORATION TECHNOLOGIES", "MICROSTRATEGY": "STRATEGY",
}


def market_for_file(stem):
    for market, stems in MARKET_FILES.items():
        if stem in stems:
            return market
    return stem


def market_label(market):
    return MARKET_LABELS.get(market, (market or "").replace("_", " ").title())


def clean_isin(isin):
    """A complete ISIN with a correct check digit; None for IBKR's masked ones ("XXXXXXX31005")."""
    return full_isin(isin)


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
    """'Alphabet Inc Class A' and 'ALPHABET INC-CL A' both become 'ALPHABET A'; IBKR's
    'JOHNSON &amp; JOHNSON' and 'Johnson & Johnson' both become 'JOHNSON JOHNSON'.
    None for blanks and placeholders like 'N/A'."""
    if not s:
        return None
    text = html.unescape(str(s)).upper().replace("'", "").replace("’", "").replace(".", "")
    if text.strip() in NO_NAME:
        return None
    words = re.sub(r"[^A-Z0-9]+", " ", text).split()
    return " ".join(w for w in words if w not in NAME_DROP) or None


def canon_name(s):
    n = norm_name(s)
    return RENAMES.get(n, n) if n else None


def name_key(s):
    """The name-index key: canonical name without spaces ("MC DONALDS" = "MCDONALDS")."""
    c = canon_name(s)
    return c.replace(" ", "") if c else None


def names_agree(a, b):
    """True when two names plausibly belong to one company: the same canonical name, or
    the same first significant word ('Coinbase Global, Inc.' and 'COINBASE GLOBAL INC
    -CLASS A'). Only used together with another check (a matching ISIN suffix or ticker)."""
    ca, cb = canon_name(a), canon_name(b)
    if not ca or not cb:
        return False
    if ca.replace(" ", "") == cb.replace(" ", ""):
        return True
    wa, wb = ca.split(), cb.split()
    return wa[0] == wb[0] or "".join(wa[:2]) == wb[0] or wa[0] == "".join(wb[:2])


ADR_WORDS = re.compile(r"\b(ADRS?|ADS|GDRS?|GDS|SPON|SPONSORED|SPN|UNS|UNSP|UNSPON|UNSPONSORED|DEPOSITARY)\b|-\s*SP\b",
                       re.I)
PREF_WORDS = re.compile(r"\b(PREF|PREFERRED|PFD|PRF|PN|PNA|PNB)\b", re.I)


def lead_key(name):
    """The first two words of a canonical name ('ITAU UNIBANCO HOLDING' -> 'ITAUUNIBANCO'), for
    matching an ADR whose name IBKR abbreviates ('ITAU UNIBANCO H-SPON PRF ADR')."""
    c = canon_name(name)
    return "".join(c.split()[:2]) if c else None


def borrow_rank(entry):
    shares = entry.get("shares")
    return (entry.get("fee") is not None, shares if shares is not None else -1)


def _keep_best(d, key, entry):
    old = d.get(key)
    if old is None or borrow_rank(entry) > borrow_rank(old):
        d[key] = entry


def home_market(isin=None, currency=None, country=None, ticker=None, yahoo=False):
    """Where a holding trades: 'us', a market name ('japan', 'korea', ...), or None when
    nothing says. The filing's currency decides first: Medtronic is Irish (IE ISIN) but
    held in dollars, so it's a US holding, not one for IBKR's Austria file."""
    f = split_foreign(ticker, yahoo) if ticker else None
    if f:
        return f[1]
    cur = (currency or "").strip().upper()
    if cur == "USD":
        return "us"
    if cur in CURRENCY_MARKET:
        return CURRENCY_MARKET[cur]
    isin = full_isin(isin)
    cc = (country or "").strip().upper() or (isin[:2] if isin else "")
    if cc == "US":
        return "us"
    if cc and (cur == "EUR" or not cur):
        return ISO_MARKET.get(cc) or cc.lower()
    if yahoo and ticker:
        return "us"
    return None


class MatchIndex:
    """Every way to find IBKR's borrow line for a holding.

    IBKR masks US ISINs in its US file (Apple is XXXXXXX31005) but keeps their last 5
    characters, its composite FIGI and every non-US ISIN in full. So a holding is
    matched, depending on where it trades (see home_market):
      US holdings:   FIGI (from OpenFIGI) > full ISIN > ticker found by ISIN (OpenFIGI,
                     SEC, Yahoo) > ticker in the filing or fund name > masked-ISIN suffix
                     plus a matching name > a unique company name > another country's
                     file (secondary listing)
      abroad:        the home market's file by ISIN, ticker or name > IBKR's US file for
                     the same company (its OTC line by ISIN, or an ADR by name) > another
                     country's file by ISIN (secondary listing)
    Lines that aren't the holding's own home-market line can be turned down by a guard
    (thin, expensive lines that don't stand for the holding's real borrow cost)."""

    def __init__(self, us, foreign=None):
        self.us_sym, self.isin_us, self.figi_us, self.tail_us, self.name_us = {}, {}, {}, {}, {}
        for sym, e in (us or {}).items():
            ent = dict(e, symbol=sym, file="usa")
            _keep_best(self.us_sym, sym, ent)
            isin = full_isin(e.get("isin"))
            if isin:
                _keep_best(self.isin_us, isin, ent)
            tail = e.get("isin_tail") or (isin[-5:] if isin else None)
            if tail:
                self.tail_us.setdefault(tail, []).append(ent)
            if e.get("figi"):
                _keep_best(self.figi_us, e["figi"], ent)
            k = name_key(e.get("name"))
            if k:
                self.name_us.setdefault(k, {})[sym] = ent
        # lines in the US file that stand for foreign companies: depositary receipts, US
        # listings of foreign-domiciled companies, and the ADRs listed in COUNTRY_LINES
        listed = {t for ts in COUNTRY_LINES.values() for t in ts}
        self.adr_us = {}
        for sym, ent in self.us_sym.items():
            isin = full_isin(ent.get("isin"))
            # (5-letter OTC tickers ending in Y are ADRs and in F foreign shares: VBREY, SSNLF)
            if ADR_WORDS.search(ent.get("name") or "") or sym in listed or (isin and not isin.startswith("US")) \
                    or re.fullmatch(r"[A-Z]{4}[YF]", sym):
                ent["foreign_line"] = True
                k = lead_key(ent.get("name"))
                if k:
                    self.adr_us.setdefault(k, []).append(ent)
        self.isin_fx, self.name_fx, self.fx_sym = {}, {}, {}
        for stem, table in (foreign or {}).items():
            syms = self.fx_sym.setdefault(market_for_file(stem), {})
            for sym, e in table.items():
                ent = dict(e, symbol=sym, file=stem)
                _keep_best(syms, sym, ent)
                isin = full_isin(e.get("isin"))
                if isin:
                    _keep_best(self.isin_fx.setdefault(isin, {}), stem, ent)
                k = name_key(e.get("name"))
                if k:
                    self.name_fx.setdefault(k, {})[(stem, sym)] = ent
        self.markets = {m for m, syms in self.fx_sym.items() if syms}

    def has_file(self, market):
        return market in self.markets

    # ---- single lookups ----
    def _fx(self, market, code):
        syms = self.fx_sym.get(market) or {}
        for c in (code, code.lstrip("0"), norm_ticker(code)):
            if c and c in syms:
                return syms[c]
        return None

    @staticmethod
    def same_security(entry, isin, names, figi=None):
        """True when an IBKR line found by ticker is the holding's security: same FIGI,
        same (masked) ISIN ending, else the same company name."""
        if figi and entry.get("figi"):
            return entry["figi"] == figi
        if isin and entry.get("isin"):
            return entry["isin"] == isin
        if isin and entry.get("isin_tail"):
            return entry["isin_tail"] == isin[-5:]
        return any(names_agree(n, entry.get("name")) for n in names)

    def us_by_tail(self, isin, names):
        """The US line whose masked ISIN ends like this one and whose name agrees:
        (entry or None, why not)."""
        tail = isin[-5:]
        cands = [e for e in self.tail_us.get(tail, []) if not e.get("isin") or e["isin"] == isin]
        if not cands:
            return None, f"no IBKR line's ISIN ends {tail}"
        if not names:
            syms = ", ".join(sorted(e["symbol"] for e in cands)[:4])
            return None, f"no name to check IBKR's line ending {tail} ({syms}) against"
        ok = [e for e in cands if any(names_agree(n, e.get("name")) for n in names)]
        if len(ok) > 1:
            keys = {name_key(n) for n in names}
            exact = [e for e in ok if name_key(e.get("name")) in keys]
            plain = [e for e in ok if "." not in e["symbol"]]
            ok = exact if len(exact) == 1 else (plain if len(plain) == 1 else ok)
        if len(ok) == 1:
            return ok[0], None
        syms = ", ".join(sorted(e["symbol"] for e in (ok or cands))[:4])
        if ok:
            return None, f"IBKR lines ending {tail} ({syms}) can't be told apart"
        return None, f"the IBKR line ending {tail} ({syms}) has another company's name"

    def us_by_name(self, names):
        """(entry, count): a unique name match in the US file. When several lines share
        the name, a plain line (no '.PR', '.U', '.WS' suffix) is used if it's the only
        one, or if it has at least 10 times the shares available of every other plain
        line (SO's common stock over its SOJD and SOJE notes, which IBKR also calls
        "Southern Co"). GOOG and GOOGL, both with over 10 million, stay ambiguous."""
        best = (None, 0)
        for nm in names:
            hits = self.name_us.get(name_key(nm), {})
            if not hits:
                continue
            if len(hits) == 1:
                return next(iter(hits.values())), 1
            plain = sorted((e for s, e in hits.items() if "." not in s),
                           key=lambda e: e.get("shares") or 0, reverse=True)
            if len(plain) == 1:
                return plain[0], len(hits)
            if len(plain) > 1:
                a, b = plain[0], plain[1]
                if not (a.get("plus") and b.get("plus")) and \
                        (a.get("shares") or 0) >= 10 * max(b.get("shares") or 0, 1):
                    return a, len(hits)
            best = (None, max(best[1], len(hits)))
        return best

    def adr_by_name(self, names):
        """IBKR's US line for a foreign company, by name: only depositary receipts and other
        lines for foreign companies count (an Indian "Pfizer Ltd" isn't Pfizer Inc). First
        an exact name, then the first two words ("Itau Unibanco Holding" and IBKR's
        "ITAU UNIBANCO H-SPON PRF ADR"); between a company's ordinary and preferred ADRs,
        the one like the holding, then the one with more shares available."""
        keys = {name_key(n) for n in names} - {None}
        for k in keys:
            hits = [e for e in self.name_us.get(k, {}).values() if e.get("foreign_line")]
            if len(hits) == 1:
                return hits[0]
        pref = any(PREF_WORDS.search(n or "") for n in names)

        def third_agrees(a, b):            # share-class words (PREF, PN) aside
            wa = [w for w in (canon_name(a) or "").split() if not PREF_WORDS.fullmatch(w)]
            wb = [w for w in (canon_name(b) or "").split() if not PREF_WORDS.fullmatch(w)]
            return len(wa) < 3 or len(wb) < 3 or wa[2].startswith(wb[2]) or wb[2].startswith(wa[2])
        for n in names:
            if len((canon_name(n) or "").split()) < 2:           # "Euro" isn't Euro-Holdings
                continue
            cands = [e for e in self.adr_us.get(lead_key(n) or "", []) if third_agrees(n, e.get("name"))]
            if not cands:
                continue
            same = [e for e in cands if bool(PREF_WORDS.search(e.get("name") or "")) == pref]
            pool = same or cands
            return max(pool, key=lambda e: (e.get("fee") is not None, e.get("plus", False), e.get("shares") or 0))
        return None

    def fx_by_name(self, names, stems=None):
        best = (None, 0)
        for nm in names:
            hits = self.name_fx.get(name_key(nm), {})
            if stems is not None:
                hits = {k: v for k, v in hits.items() if k[0] in stems}
            if len(hits) == 1:
                return next(iter(hits.values())), 1
            best = (None, max(best[1], len(hits)))
        return best

    # ---- the search ----
    def find(self, isin=None, cusip=None, ticker=None, names=(), country=None, yahoo=False,
             currency=None, known=None, hints=(), guard=None):
        """(entry, how, tried, kind).

        entry  IBKR's line (it has a fee when one was found; otherwise it may be a line
               IBKR lists without a fee, or None)
        how    isin, figi, ticker, ticker:<source> (openfigi, sec_ftd, yahoo, fund_name),
               suffix (masked-ISIN suffix + name) or name
        tried  what didn't work, for the row's detail
        kind   primary (the holding's own line), secondary (its listing in another
               country's file) or us_line (IBKR's US line for a foreign company: an ADR
               or OTC line)
        known  an ident.Ident (ticker and FIGI found by ISIN elsewhere); hints: tickers
               worth trying (from the fund's name), accepted only when they're the same
               security; guard(entry, kind, how) -> reason or None turns down lines
               that aren't the holding's own."""
        tried, listed, seen, named = [], [], set(), []
        isin = full_isin(isin) or cusip_to_isin(cusip)
        names = [n for n in dict.fromkeys(names or ()) if norm_name(n)]
        home = home_market(isin, currency, country, ticker, yahoo)
        figi = known.figi if known else None

        def offer(entry, how, kind):
            """True when this line is the answer."""
            if entry is None or id(entry) in seen:
                return False
            seen.add(id(entry))
            if how == "name":
                named.append(entry)
            if entry.get("fee") is None:
                listed.append((entry, how, kind))
                return False
            if kind != "primary" and guard is not None:
                why = guard(entry, kind, how)
                if why:
                    tried.append(why)
                    return False
            return True

        def name_note(count):
            if count > 1:
                tried.append(f"the name matches {count} IBKR lines, so it isn't used")

        steps = []
        local = split_foreign(ticker, yahoo) if ticker else None
        if home in (None, "us"):
            if figi:
                steps.append(lambda: (self.figi_us.get(figi), "figi", "primary"))
            if isin:
                steps.append(lambda: (self.isin_us.get(isin), "isin", "primary"))
            if known and known.ticker:
                def by_known():
                    e = self.us_sym.get(known.ticker)
                    if e is None:
                        tried.append(f"{SOURCE_NAMES.get(known.src, known.src)} says {known.ticker}, which isn't in "
                                     "IBKR's file (delisted, renamed or not lendable)")
                        return None, None, None
                    if not self.same_security(e, isin, names, figi):
                        tried.append(f"{known.ticker} (from {known.src}) is another security in IBKR's file")
                        return None, None, None
                    return e, f"ticker:{known.src}", "primary"
                steps.append(by_known)
            if ticker and not local:
                def by_ticker():
                    e = self.us_sym.get(norm_ticker(ticker))
                    if e is None:
                        tried.append(f"ticker {ticker} not found")
                    return e, "ticker", "primary"
                steps.append(by_ticker)
            for t in hints:
                steps.append(lambda t=t: (self.us_sym.get(t) if e_ok(t) else None, "ticker:fund_name", "primary"))
            if isin:
                def by_tail():
                    e, why = self.us_by_tail(isin, names)
                    if why:
                        tried.append(why)
                    return e, "suffix", "primary"
                steps.append(by_tail)
            if names:
                def by_name():
                    e, count = self.us_by_name(names)
                    name_note(count)
                    return e, "name", "primary"
                steps.append(by_name)
            if isin:
                for stem, e in sorted(self.isin_fx.get(isin, {}).items(), key=lambda kv: borrow_rank(kv[1]),
                                      reverse=True):
                    steps.append(lambda e=e: (e, "isin", "secondary"))
        else:
            stems = MARKET_FILES.get(home, (home,))
            if self.has_file(home):
                if isin:
                    steps.append(lambda: (next((e for s, e in self.isin_fx.get(isin, {}).items() if s in stems), None),
                                          "isin", "primary"))
                code = local[0] if local else (ticker if ticker else None)
                if code:
                    steps.append(lambda: (self._fx(home, code) or self._fx(home, norm_ticker(code) or code),
                                          "ticker", "primary"))
                if names:
                    def by_home_name():
                        e, count = self.fx_by_name(names, stems)
                        name_note(count)
                        return e, "name", "primary"
                    steps.append(by_home_name)
            else:
                tried.append(f"IBKR has no borrow file for {market_label(home)}")
            if isin:
                steps.append(lambda: (self.isin_us.get(isin), "isin", "us_line"))
            if known and known.ticker:
                steps.append(lambda: (self.us_sym.get(known.ticker), f"ticker:{known.src}", "us_line"))
            if names:
                def by_adr_name():
                    return self.adr_by_name(names), "name", "us_line"
                steps.append(by_adr_name)
            if isin:
                for stem, e in sorted(self.isin_fx.get(isin, {}).items(), key=lambda kv: borrow_rank(kv[1]),
                                      reverse=True):
                    if stem not in stems:
                        steps.append(lambda e=e: (e, "isin", "secondary"))

        def e_ok(t):
            e = self.us_sym.get(t)
            return e is not None and self.same_security(e, isin, names, figi)

        for step in steps:
            entry, how, kind = step()
            if offer(entry, how, kind):
                return entry, how, tried, kind
        if isin and not isin.startswith("US") and not any(t.startswith("IBKR has no") for t in tried):
            tried.insert(0, f"ISIN {isin} is not in IBKR's files")
        if names and not named and not any("name" in t for t in tried):
            tried.append("no IBKR line with the same company name")
        if not tried:
            tried.append("no ISIN, CUSIP, ticker or usable name")
        if listed:
            e, how, kind = listed[0]
            return e, how, tried, kind
        return None, None, tried, None


SOURCE_NAMES = {"openfigi": "OpenFIGI", "sec_ftd": "SEC's fails-to-deliver data", "yahoo": "Yahoo"}
FILE_LABELS = {"british": "UK", "uk": "UK", "hongkong": "Hong Kong", "hong_kong": "Hong Kong",
               "hk": "Hong Kong", "southkorea": "Korea", "unitedkingdom": "UK"}
HOW_LABELS = {"isin": "ISIN", "figi": "FIGI via OpenFIGI", "ticker": "ticker", "ticker:openfigi": "ticker via OpenFIGI",
              "ticker:sec_ftd": "ticker via SEC", "ticker:yahoo": "ticker via Yahoo",
              "ticker:fund_name": "ticker in the fund's name", "suffix": "ISIN suffix + name", "name": "name match"}


def file_label(stem):
    return FILE_LABELS.get(stem, stem.replace("_", " ").title())


def match_label(entry, how, kind="primary"):
    stem = entry.get("file", "usa")
    if kind == "us_line":
        return f"IBKR US line ({entry['symbol']})"
    if kind == "secondary":
        return f"IBKR ({file_label(stem)} file, secondary listing)"
    if stem != "usa":
        return f"IBKR ({file_label(stem)} file)" if how != "name" else f"IBKR (name match, {file_label(stem)} file)"
    return f"IBKR ({HOW_LABELS.get(how, how)})"


def match_source(how, kind="primary"):
    """A short code for a row's source (CSV and reports)."""
    if kind in ("us_line", "secondary"):
        return {"us_line": "us_line", "secondary": "secondary_listing"}[kind]
    return {"isin": "isin", "figi": "figi", "ticker": "ticker_filing", "suffix": "isin_suffix_name",
            "name": "name"}.get(how, (how or "").replace("ticker:", "ticker_"))


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
           "direction": None, "direction_src": None, "deriv_cat": (d.get("derivCat") or "").upper()}
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


def _currency(el):
    """curCd, given as <curCd>EUR</curCd> or <currencyConditional curCd="EUR" .../>."""
    v = _text(el, "curCd")
    if v:
        return v.upper()
    c = _child(el, "currencyConditional")
    return (c.get("curCd") or "").upper() if c is not None else ""


def _holding(el):
    ids = _ids(_child(el, "identifiers"))
    h = {
        "name": _text(el, "name") or "", "title": _text(el, "title") or "",
        "isin": ids.get("isin"), "cusip": _text(el, "cusip") or ids.get("cusip"),
        "ticker": ids.get("ticker"),
        "pct": (num(_text(el, "pctVal")) or 0.0) / 100.0,
        "balance": num(_text(el, "balance")), "units": (_text(el, "units") or "").upper(),
        "value": num(_text(el, "valUSD")),
        "payoff": (_text(el, "payoffProfile") or "").lower(),
        "cat": _cat(el, "assetCat", "assetConditional"),
        "issuer_cat": _cat(el, "issuerCat", "issuerConditional"),
        "country": (_text(el, "invCountry") or "").upper(),
        "currency": _currency(el),
        "fair_level": (_text(el, "fairValLevel") or "").strip(),
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
if SEC_USER_AGENT:
    ident.IDS.ftd.get = SEC.get          # SEC's fails-to-deliver files (CUSIP -> ticker)


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


# Unit investment trusts don't file N-PORT. SPY, DIA and MDY's holdings come from State
# Street's daily file (checked October 2026: an .xlsx with name, ticker, CUSIP, weight
# and currency); QQQ uses QQQM's filing (the same index and manager; Invesco's own file
# is behind a script-only page).
SSGA_URL = ("https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/"
            "holdings-daily-us-en-{}.xlsx")
SSGA_UITS = {"SPY", "DIA", "MDY"}
UIT_STANDINS = {"QQQ": "QQQM"}


def _col(ref):
    n = 0
    for ch in re.match(r"[A-Z]*", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return max(n - 1, 0)


def read_xlsx_rows(data):
    """The first sheet of an .xlsx file as rows of text (enough for issuers' holdings files)."""
    z = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
            shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    sheet = sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))[0]
    rows = []
    for row in ET.fromstring(z.read(sheet)).iter():
        if _local(row.tag) != "row":
            continue
        cells = {}
        for c in row:
            if _local(c.tag) != "c":
                continue
            v = next((x.text for x in c if _local(x.tag) == "v"), None)
            if c.get("t") == "s" and v is not None:
                v = shared[int(v)]
            elif c.get("t") == "inlineStr":
                v = "".join(x.text or "" for x in c.iter() if _local(x.tag) == "t")
            cells[_col(c.get("r", "A"))] = (v or "").strip()
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return rows


def parse_ssga(rows):
    """State Street's daily holdings sheet -> (fund name, as-of date, holdings)."""
    name = as_of = None
    head = None
    holds = []
    for r in rows:
        first = (r[0] if r else "").strip()
        if first.startswith("Fund Name") and len(r) > 1:
            name = r[1]
        elif first.startswith("Holdings") and len(r) > 1:
            m = re.search(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", r[1])
            if m:
                try:
                    as_of = datetime.strptime(m.group(0), "%d-%b-%Y").date().isoformat()
                except ValueError:
                    as_of = None
        elif head is None and "Name" in r and "Weight" in r:
            head = {k: i for i, k in enumerate(r)}
        elif head is not None:
            def cell(k):
                i = head.get(k)
                return r[i].strip() if i is not None and i < len(r) else ""
            w = num(cell("Weight"))
            if not cell("Name") or w is None:
                if holds:
                    break
                continue
            ticker, ident = cell("Ticker"), cell("Identifier").upper()
            h = {"name": cell("Name"), "title": "", "pct": w / 100.0, "currency": cell("Local Currency").upper(),
                 "ticker": None if ticker in ("", "-") else ticker, "isin": None,
                 "cusip": ident if re.fullmatch(r"[0-9A-Z]{9}", ident) else None, "cat": "EC", "issuer_cat": ""}
            if h["ticker"] is None and CASH_WORDS.search(h["name"].upper()):
                h.update(cat="", residual=True)
            holds.append(h)
    return name, as_of, holds


def fetch_ssga(sym):
    req = urllib.request.Request(SSGA_URL.format(sym.lower()), headers={"User-Agent": "Mozilla/5.0 short-borrow-monitor"})
    with urllib.request.urlopen(req, timeout=30) as r:
        name, as_of, holds = parse_ssga(read_xlsx_rows(r.read()))
    if not holds:
        raise ValueError("State Street's file listed no holdings")
    return {"source": "State Street daily holdings", "as_of": as_of, "url": SSGA_URL.format(sym.lower()),
            "complete": True, "holdings": holds, "series": name, "net_assets": None, "notes": []}


def fetch_holdings(yf, sym):
    notes = []
    if sym in UIT_STANDINS:
        sub = fetch_holdings(yf, UIT_STANDINS[sym])
        if "_error" not in sub and not sub.get("single_stock"):
            stand = UIT_STANDINS[sym]
            return dict(sub, source=f"{sub['source']} of {stand} (stand-in)", standin=stand,
                        notes=[f"{sym} is a unit investment trust, which doesn't file N-PORT; {stand} tracks the "
                               f"same index, so its holdings are used"] + list(sub.get("notes") or []))
        notes.append(f"Couldn't load {UIT_STANDINS[sym]}'s holdings as a stand-in")
    if sym in SSGA_UITS:
        try:
            return fetch_ssga(sym)
        except Exception as e:
            notes.append(f"State Street's daily holdings file failed ({type(e).__name__})")
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
# Stock indexes options are written on. Some filers put index options under the interest
# rate category (Global X files RYLD's, QYLD's and XYLD's written calls as DIR).
EQUITY_INDEX_RE = re.compile(r"\b(RUT|RTY|SPX|SPXW|XSP|NDX|NDXP|XND|DJX|RUY|MXEF|MXEA|S&P|NASDAQ|RUSSELL|"
                             r"DOW JONES)\b", re.I)
# Structured notes paying an index's return (JPMorgan's premium income funds hold them).
ELN_WORDS = re.compile(r"\b(ELNS?|EQUITY[- ]LINKED|PARTICIPATION NOTES?)\b", re.I)
NOT_LENDABLE_WORDS = re.compile(r"\b(WTS?|WARRANTS?|RIGHTS?|RTS|CVRS?|CONTINGENT VALUE|ESCROW|"
                                r"LITIGATION TRUST|LIQUIDATING TRUST)\b", re.I)
# Words in a swap's own name that aren't the stock it tracks ("TESLA INC Swap", "Recv Long
# 2x Coin Gs1", "Strg Trs Mstr Eq").
DERIV_NAME_WORDS = re.compile(r"\b(SWAPS?|TRS|TOTAL|RETURN|EQUITY|EQ|RECV|RECEIVE|PAY|LONG|SHORT|FUTURES?|"
                              r"FORWARDS?|FWD|OPTIONS?|CALLS?|PUTS?|US|BULL|BEAR|DAILY|TARGET|STRG|\w*\d\w*)\b")
NONEQUITY_CATS = ("DIR", "DCR", "DFE", "DCO")
EQUITY_TYPES = ("Stock", "Preferred", "Fund", "Holding")
GROUPS = [   # summary order and labels
    ("matched", "Matched to an IBKR fee"), ("lookthrough", "Look-through to the underlying"),
    ("proxy", "Priced from a stand-in"), ("cash", "Cash & T-bills"), ("bond", "Bond: assumed rate"),
    ("short", "Short exposure"), ("nonequity", "Non-equity derivative"),
    ("unmatched", "Assumed rate (nothing else known)"), ("notlendable", "Not lendable"),
    ("excluded", "Excluded: no exposure reported"),
]
NEEDS_FEE = ("matched", "lookthrough", "proxy", "unmatched", "bond")
EQUITY_GROUPS = ("matched", "lookthrough", "proxy", "unmatched")
STOCK_GROUPS = EQUITY_GROUPS + ("notlendable",)

# A line that isn't the holding's own (a secondary listing, an OTC or ADR line, or a fund's
# own fee) isn't used when it's this expensive and this thin: RSSL's 40% fee on 9,000
# shares said nothing about the Russell 2000 stocks RYLD really holds.
THIN_MIN_FEE = 5.0            # % / yr
THIN_MAX_SHARES = 100_000
THIN_MAX_VALUE = 2_000_000    # $ (used when the holding's price is known)
# Markets IBKR doesn't lend in (or holdings it doesn't list) are priced at the median fee
# of IBKR's US lines for that market's companies (ADRs and other US listings), plus the
# fund's own matched holdings from there. Needs at least this many lines.
COUNTRY_MIN_LINES = 3
COUNTRY_MIN_SHARES = 100_000  # shares available for a line to count toward a market's median...
COUNTRY_MAX_FEE = THIN_MIN_FEE  # ...and its fee under this (one hard-to-borrow ADR says little about a market)
# A stand-in line (an ADR or another country's listing, not the holding's own line) isn't used
# when its fee is at least this much and this many times the median of its market's other
# lines (or of the assumed rate, when there are too few): OVCHY, an unsponsored ADR of a
# Singapore bank, at 8.94% says little about borrowing the bank's shares in Singapore.
STANDIN_MIN_FEE = 2.0         # % / yr
STANDIN_MAX_MULTIPLE = 4.0
COUNTRY_LINES = {
    "korea": ("KB", "SHG", "PKX", "KEP", "WF", "SKM", "LPL", "SKHY"),
    "taiwan": ("TSM", "UMC", "ASX", "CHT", "HIMX"),
    "india": ("HDB", "IBN", "INFY", "WIT", "RDY"),
    "brazil": ("VALE", "PBR", "ITUB", "BBD", "ABEV", "SBS", "GGB", "EBR", "ERJ", "CIG", "SID", "UGP", "TIMB",
               "VIV", "BSBR", "SUZ", "BRFS"),
    "china": ("BABA", "JD", "PDD", "BIDU", "NTES", "TCOM", "BILI", "LI", "NIO", "XPEV", "YUMC", "ZTO", "BEKE",
              "TME", "VIPS", "HTHT", "EDU", "TAL", "IQ", "WB", "ATHM", "QFIN", "YMM", "BZ"),
    "southafrica": ("GFI", "SBSW", "HMY", "DRD"),
    "chile": ("SQM", "BCH", "BSAC", "ENIC", "LTM", "CCU"),
    "peru": ("BAP", "SCCO", "BVN", "IFS"),
    "colombia": ("EC", "CIB", "AVAL"),
    "argentina": ("YPF", "GGAL", "BMA", "PAM", "TEO", "TGS", "SUPV", "LOMA", "CEPU", "EDN", "IRS", "BBAR"),
    "indonesia": ("TLK",),
    "philippines": ("PHI",),
    "turkey": ("TKC",),
    "israel": ("TEVA", "CHKP", "NICE", "ICL", "ESLT", "CYBR", "WIX", "MNDY"),
    "ireland": ("RYAAY", "CRH", "FLUT", "STE", "ICLR", "KRYAY"),
    "greece": ("GRBK",),
}


def holding_type(h):
    d = h.get("deriv")
    if d:
        return {"swap": "Swap", "future": "Future", "forward": "Forward", "option": "Option"}.get(d["kind"], "Derivative")
    if h.get("undisclosed"):
        return "Undisclosed"
    if h.get("residual"):
        return "Cash"
    if h.get("stated"):
        return "Stated exposure"
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
    if cat == "SN" and h.get("eln"):
        return "Equity-linked note"
    if is_bond(h):
        return "Bond"
    return "Holding" if not cat else "Other"


def is_cash(h):
    return h.get("cat") in ("STIV", "RA") or h.get("issuer_cat") == "UST" or bool(h.get("residual"))


def is_bond(h):
    cat, icat = h.get("cat", ""), h.get("issuer_cat", "")
    return cat in ("DBT", "LON", "SN") or cat.startswith("ABS") or (
        icat in ("USGA", "USGSE", "MUN") and cat not in ("EC", "EP"))


CURRENCY_NAME = re.compile(
    r"(?:[A-Z.]+\s+){0,3}(DOLLARS?|EUROS?|YEN|POUNDS?|STERLING|FRANCS?|KRONER?|KRONA|KRONOR|YUAN|RENMINBI|RUPEES?|"
    r"RUPIAH|REAIS|REAL|WON|PESOS?|RINGGIT|BAHT|RAND|LIRA|ZLOTY|SHEKELS?|RIYALS?|DIRHAMS?|DINARS?|KORUNA|FORINT|"
    r"SOL|NAIRA|DONG|TENGE|SHILLINGS?|LEU|LEV)(\s+(STERLING|RENMINBI|CASH|OFFSHORE))?")


def is_currency(h):
    """A currency the fund holds as cash ("Hong Kong Dollar", "Euro"), not a company."""
    name = re.sub(r"\s+", " ", (h.get("name") or h.get("title") or "").upper()).strip()
    return not full_isin(h.get("isin")) and not clean_cusip(h.get("cusip")) and bool(CURRENCY_NAME.fullmatch(name))


def is_cash_fund(h):
    """A fund (or unknown holding) whose name says cash, T-bills or money market."""
    text = f"{h.get('name', '')} {h.get('title', '')}".upper()
    return (h.get("issuer_cat") == "RF" or not h.get("cat")) and bool(CASH_WORDS.search(text))


def direction_from_name(name):
    n = (name or "").upper()
    if re.search(r"\b(BEAR|SHORT|INVERSE|ULTRASHORT)\b|-\s?\d(\.\d+)?X\b", n):
        return "short"
    if re.search(r"\b(BULL|LONG|ULTRA|ULTRAPRO)\b|\b\d(\.\d+)?X\b", n):
        return "long"
    return None


def stated_leverage(name):
    """The exposure a fund's name promises: '2X' -> 2, 'UltraPro' -> 3, 'Ultra' -> 2, else 1."""
    n = (name or "").upper()
    m = re.search(r"(\d+(?:\.\d+)?)\s*X\b", n)
    if m:
        return float(m.group(1))
    if "ULTRAPRO" in n:
        return 3.0
    if re.search(r"\bULTRA\b", n):
        return 2.0
    return 1.0


def proxy_for(index_name):
    for pattern, sym in INDEX_PROXIES:
        if re.search(pattern, index_name or "", re.I):
            return sym
    return None


def _display(s):
    s = (s or "").strip()
    if not norm_name(s):
        return ""
    return s.title() if s.isupper() else s


def holding_name(h):
    return _display(h.get("name")) or _display(h.get("title")) or "Unnamed"


def non_us_from_entry(entry):
    """1.0 when an IBKR match is in a non-US file (the stock trades abroad), else 0.0."""
    return 0.0 if entry.get("file", "usa") == "usa" else 1.0


def non_us_from_holding(h):
    """For holdings without an IBKR match: Yahoo's exchange suffix, else the filing's currency or country."""
    if h.get("yahoo"):
        return 1.0 if split_foreign(h.get("ticker"), yahoo=True) else 0.0
    home = home_market(h.get("isin"), h.get("currency"), h.get("country"))
    if home is None:
        return None
    return 0.0 if home == "us" else 1.0


def _row(h, exposure, fee, how, group, detail="", matched=0.0, assumed=False, symbol=None, name=None,
         non_us=None, source=None):
    return {
        "name": (name or holding_name(h))[:120],
        "ticker": h.get("ticker") or symbol, "isin": h.get("isin"), "cusip": clean_cusip(h.get("cusip")),
        "type": holding_type(h), "cat": h.get("cat") or "", "issuer_cat": h.get("issuer_cat") or "",
        "exposure": exposure, "fee": fee, "assumed": assumed, "how": how, "group": group,
        "matched": matched, "contrib": max(exposure, 0.0) * fee if fee is not None else 0.0,
        "detail": detail, "non_us": non_us, "symbol": symbol, "source": source or group,
    }


def thin_reason(entry, price=None):
    """Why a line isn't a fair stand-in (expensive and thin), or None."""
    fee, shares = entry.get("fee"), entry.get("shares")
    if fee is None or fee < THIN_MIN_FEE or entry.get("plus") or shares is None:
        return None
    value = shares * price if price else None
    if shares < THIN_MAX_SHARES or (value is not None and value < THIN_MAX_VALUE):
        where = "US file" if entry.get("file", "usa") == "usa" else f"{file_label(entry['file'])} file"
        return (f"{entry['symbol']} ({where}) not used: {fee:.2f}% with only {shares:,} shares available, "
                "too thin to stand for this holding")
    return None


def holding_price(h):
    v, b = h.get("value"), h.get("balance")
    return v / b if v and b and b > 0 and h.get("units") == "NS" else None


def not_lendable_reason(h):
    text = f"{h.get('name') or ''} {h.get('title') or ''}"
    if NOT_LENDABLE_WORDS.search(text) or "CVR" in (h.get("cusip") or "").upper() \
            or (h.get("deriv") or {}).get("deriv_cat") == "WAR":
        return "Warrants, rights, CVRs and escrow shares can't be borrowed"
    if h.get("fair_level") == "3":
        return ("A private or illiquid holding (valued from unobservable inputs: fair value level 3), "
                "which can't be borrowed")
    return None


def _matched_row(h, exp, entry, how, kind, home):
    label = match_label(entry, how, kind)
    if kind == "us_line":
        detail = (f"No IBKR line for the holding itself; priced from {entry['symbol']} "
                  f"({entry.get('name') or ''}), IBKR's US line for the same company")
        row = _row(h, exp, entry["fee"], label, "proxy", detail, symbol=entry["symbol"],
                   non_us=non_us_from_holding(h), source=match_source(how, kind))
    else:
        detail = f"{entry['symbol']} · {entry.get('name') or ''}".strip(" ·")
        if kind == "secondary":
            detail += f"; no line in the holding's own market, so its {file_label(entry['file'])} listing is used"
        row = _row(h, exp, entry["fee"], label, "matched", detail, matched=1.0, symbol=entry["symbol"],
                   non_us=non_us_from_entry(entry), source=match_source(how, kind))
    row["home"], row["avail"], row["avail_plus"] = home, entry.get("shares"), entry.get("plus", False)
    return row


def _pending_row(h, exp, gc, detail, source="assumed"):
    row = _row(h, exp, gc, "Assumed rate", "unmatched", detail, assumed=True, source=source)
    row["_pending"] = True
    return row


def price_fund(h, exp, entry, tried, gc, fund, proxy):
    """A fund held by the fund: priced from its own holdings, not its own borrow fee."""
    sym = entry["symbol"] if entry is not None else None
    notes = []
    if fund is not None and sym:
        res = fund(sym)
        if res and res[0] == "ok":
            _, fsym, fee, matched, non_us, info = res
            row = _row(h, exp, fee, f"Look-through: {fsym}'s holdings", "lookthrough",
                       f"A fund: priced from its own holdings ({info})", matched=matched, assumed=matched < 1.0,
                       non_us=non_us, source="fund_lookthrough")
            row["via"], row["proxy"] = "fund", fsym
            return row
        if res and res[0] == "pending":
            return _pending_row(h, exp, gc, f"{sym}'s holdings are still loading")
        if res:
            notes.append(f"couldn't use {sym}'s holdings ({res[2]})")
    elif not sym:
        notes.append("the fund isn't in IBKR's file, so its holdings can't be looked up")
    p = proxy(h.get("name") or h.get("title") or "") if proxy else None
    if p and p[0] == "ok":
        row = _row(h, exp, p[2], f"Look-through: {p[1]} (same index)", "lookthrough",
                   "; ".join(notes + [f"priced from {p[1]}'s holdings, which track the same index"]),
                   matched=p[3], assumed=p[3] < 1.0, non_us=p[4] if len(p) > 4 else None, source="same_index_fund")
        row["via"], row["proxy"] = "proxy", p[1]
        return row
    if p and p[0] == "pending":
        return _pending_row(h, exp, gc, f"{p[1]}'s holdings are still loading")
    if entry is not None and entry.get("fee") is not None:
        thin = thin_reason(entry)
        if not thin:
            return _row(h, exp, entry["fee"], f"IBKR fund's own fee ({sym})", "matched",
                        "; ".join(notes + ["its holdings couldn't be used, so the fund's own IBKR fee is"]),
                        matched=1.0, symbol=sym, non_us=non_us_from_entry(entry), source="fund_own_fee")
        notes.append(thin)
    return _row(h, exp, gc, "Assumed rate", "unmatched", "; ".join(notes + tried), assumed=True, source="assumed")


def price_security(h, idx, gc, cash_fee, ids=None, fund=None, proxy=None):
    exp = h.get("pct") or 0.0
    if h.get("undisclosed"):
        return _row(h, exp, gc, "Assumed rate", "unmatched",
                    "Yahoo only lists the top 10 holdings", assumed=True, source="assumed")
    if is_cash(h) or is_currency(h) or (h.get("issuer_cat") == "RF" and is_cash_fund(h)):
        detail = "Cash and other assets less liabilities" if h.get("residual") else "Not borrowed to replicate the fund"
        return _row(h, exp, cash_fee, "Cash & T-bills", "cash", detail, source="cash")
    if exp < 0 or h.get("payoff") == "short":
        return _row(h, exp, 0.0, "Short exposure", "short", "The fund is short this, so no borrow is needed",
                    source="short")
    if is_bond(h):
        return _row(h, exp, gc, "Bond: assumed rate", "bond",
                    "IBKR's stock loan file doesn't cover bonds", assumed=True, source="bond")
    key = id_key(h.get("isin"), h.get("cusip"))
    known = ids.known(key) if ids is not None and key else None
    price = holding_price(h)
    home = home_market(h.get("isin"), h.get("currency"), h.get("country"), h.get("ticker"), h.get("yahoo", False))

    def guard(entry, kind, how):
        return thin_reason(entry, price if how == "isin" else None)

    entry, how, tried, kind = idx.find(h.get("isin"), h.get("cusip"), h.get("ticker"),
                                       names=(h.get("name"), h.get("title")), country=h.get("country"),
                                       yahoo=h.get("yahoo", False), currency=h.get("currency"), known=known,
                                       guard=guard)
    if holding_type(h) == "Fund":
        return price_fund(h, exp, entry, tried, gc, fund, proxy)
    if entry is not None and entry.get("fee") is not None:
        return _matched_row(h, exp, entry, how, kind, home)
    if is_cash_fund(h):
        return _row(h, exp, cash_fee, "Cash & T-bills", "cash", "Looks like a cash or money market fund",
                    source="cash")
    why = "; ".join(tried)
    if entry is not None:
        why = f"IBKR lists {entry['symbol']} without a fee; {why}"
    nl = not_lendable_reason(h)
    if nl:
        return _row(h, exp, 0.0, "Not lendable", "notlendable", f"{nl}; {why}", non_us=non_us_from_holding(h),
                    source="not_lendable")
    state = ids.state(key) if ids is not None and key and home in (None, "us") else None
    if state in ("pending", "new"):
        why = f"still being identified (OpenFIGI, SEC, Yahoo); {why}"
    row = _row(h, exp, gc, "Assumed rate", "unmatched", why, assumed=True, non_us=non_us_from_holding(h),
               source="assumed")
    row["home"] = home
    if state is not None:
        row["ident"] = key
    return row


def weighted_non_us(pairs):
    """Exposure-weighted non-US share from (weight, non_us) pairs; None when nothing is known."""
    known = [(w, n) for w, n in pairs if n is not None and w > 0]
    total = sum(w for w, _ in known)
    return sum(w * n for w, n in known) / total if total > 0 else None


def _sleeve(rows):
    """The fund's own long stock holdings, used to stand in for an index it also has swaps on."""
    eq = [r for r in rows if r["group"] in ("matched", "proxy", "unmatched") and r["type"] in EQUITY_TYPES
          and r["exposure"] > 0]
    total = sum(r["exposure"] for r in eq)
    if len(eq) < SLEEVE_MIN_HOLDINGS or total <= 0:
        return None
    return {"rate": sum(r["exposure"] * r["fee"] for r in eq) / total,
            "matched": sum(r["exposure"] * r["matched"] for r in eq) / total, "count": len(eq),
            "non_us": weighted_non_us((r["exposure"], r["non_us"]) for r in eq)}


def deriv_subject(h):
    """What a derivative's own name says it tracks: 'TESLA INC Swap' -> 'TESLA INC'."""
    text = (_display(h.get("title")) or _display(h.get("name")) or "").upper()
    return re.sub(r"\s+", " ", DERIV_NAME_WORDS.sub(" ", re.sub(r"[^A-Z0-9&.' ]+", " ", text))).strip()


def name_tickers(text, idx):
    """Tickers IBKR lists that appear as words in a name ('T-REX 2X LONG MSTR DAILY' -> MSTR, T, ...)."""
    out = []
    for w in re.findall(r"\b[A-Z][A-Z0-9.]{0,5}\b", (text or "").upper()):
        if w in idx.us_sym and w not in out:
            out.append(w)
    return out


def _underlying(ref, idx, gc, sleeve, proxy, h=None, fund_name=None, ids=None):
    """(fee, matched share, label, detail, pending, non-US share, look) for what a long
    derivative tracks; look says how it was looked through ({"symbol": IBKR ticker,
    "via": "stock" | "components" | "sleeve" | "proxy", "ident": a key still being
    identified}), for the Stocks page's look-through."""
    ref = dict(ref or {})
    for k in ("name", "title"):
        ref[k] = ref.get(k) if norm_name(ref.get(k)) else ""
    subject = deriv_subject(h) if h else ""
    if not any(ref.get(k) for k in ("isin", "cusip", "ticker", "name", "title")) and not ref.get("components"):
        # the filing doesn't say (TSLL's swaps name an index "N/A"): use the derivative's own name
        if subject and (INDEX_WORDS.search(subject) or proxy_for(subject)):
            ref = {"type": "index", "name": subject}
        elif subject:
            ref = {"type": "single", "name": subject}
    rname = _display(ref.get("name") or ref.get("title")) or "the reference asset"
    looks_like_index = bool(INDEX_WORDS.search(f"{ref.get('name') or ''} {ref.get('title') or ''}"))
    if ref.get("type") == "single":
        key = id_key(ref.get("isin"), ref.get("cusip"))
        known = ids.known(key) if ids is not None and key else None
        names = (ref.get("name"), ref.get("title"), subject)
        hints = name_tickers(fund_name, idx)
        entry, how, tried, kind = idx.find(ref.get("isin"), ref.get("cusip"), ref.get("ticker"), names=names,
                                           known=known, hints=hints)
        if entry is None or entry.get("fee") is None:
            # a ticker named both in the swap's name and in the fund's name (NVDA in "Nvda Equity Swap 15")
            both = [t for t in name_tickers(subject, idx) if t in hints]
            if len(both) == 1 and idx.us_sym[both[0]].get("fee") is not None:
                entry, how, kind = idx.us_sym[both[0]], "ticker:fund_name", "primary"
        if entry is not None and entry.get("fee") is not None:
            return (entry["fee"], 1.0, f"Look-through: {entry['symbol']}",
                    f"Tracks {rname}; fee via {match_label(entry, how, kind)}", False, non_us_from_entry(entry),
                    {"symbol": entry["symbol"], "via": "stock"})
        if not looks_like_index:
            look = {"ident": key} if key and ids is not None else {}
            return gc, 0.0, "Assumed rate", f"Tracks {rname}, but {'; '.join(tried)}", False, None, look
    if ref.get("type") == "index" or looks_like_index:
        comps = ref.get("components") or []
        if comps:
            total, acc, matched, where = 0.0, 0.0, 0.0, []
            for c in comps:
                w = abs(c["amount"]) if c.get("amount") else 1.0
                ckey = id_key(c.get("isin"), c.get("cusip"))
                entry, _, _, _ = idx.find(c.get("isin"), c.get("cusip"), c.get("ticker"), names=(c.get("name"),),
                                          known=ids.known(ckey) if ids is not None and ckey else None)
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
            return gc, 0.0, "Assumed rate", f"Index {rname}: {p[1]}'s holdings are still loading", True, None, {}
        why = (f"couldn't load {p[1]}'s holdings ({p[2]})" if p else
               "no components listed and no proxy ETF set for it (see INDEX_PROXIES in etf.py)")
        return gc, 0.0, "Assumed rate", f"Index {rname}: {why}", False, None, {}
    return gc, 0.0, "Assumed rate", "The filing doesn't say what this tracks", False, None, {}


def _equity_option(h, d):
    """True for an option (or swap / future) on a stock index filed under a non-equity category."""
    text = " ".join(str(x or "") for x in (h.get("name"), h.get("title"), h.get("ticker"),
                                           (d.get("ref") or {}).get("name")))
    return d["kind"] in ("option", "swap", "future") and bool(EQUITY_INDEX_RE.search(text))


def price_derivative(h, idx, gc, nav, fund_name, sleeve, proxy, ids=None):
    d = h["deriv"]
    name = _display(h.get("title")) or _display(h.get("name"))
    if (h.get("cat") in NONEQUITY_CATS and not _equity_option(h, d)) or d["kind"] == "forward":
        exp = d["notional"] / nav if d.get("notional") and nav else 0.0
        exp = -exp if d.get("direction") == "short" else exp
        return _row(h, exp, 0.0, "Non-equity derivative", "nonequity",
                    "Rates, currency, credit or commodity exposure needs no stock borrow", name=name,
                    source="nonequity"), False
    if d.get("deriv_cat") == "WAR":
        return _row(h, h.get("pct") or 0.0, 0.0, "Not lendable", "notlendable",
                    "Warrants can't be borrowed", name=name, source="not_lendable"), False
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
        what = "A written call" if d.get("put_call") == "call" and d.get("written") == "written" else "Short exposure"
        size = "" if exposure is not None else "; its size isn't in the filing"
        return _row(h, -(exposure or 0.0), 0.0, "Short exposure", "short",
                    f"{what}, so no borrow is needed (direction {src}{size})", name=name, source="short"), False
    if exposure is None:
        what = "delta-adjusted exposure can't be computed" if d["kind"] == "option" else "no notional amount"
        return _row(h, 0.0, None, "Excluded: no exposure reported", "excluded",
                    f"The filing gives {what}", name=name, source="excluded"), False
    fee, matched, how, detail, pending, non_us, look = _underlying(d.get("ref"), idx, gc, sleeve, proxy, h=h,
                                                                   fund_name=fund_name, ids=ids)
    notes = [detail, f"exposure {exposure:.1%} of net assets from notional" if not approx else approx]
    if src != "as stated in the filing":
        notes.append(f"direction {src}")
    group = "lookthrough" if how.startswith("Look-through") else "unmatched"
    source = {"stock": "stock_lookthrough", "components": "index_components", "sleeve": "fund_sleeve",
              "proxy": "same_index_fund"}.get(look.get("via"), "assumed")
    row = _row(h, exposure, fee, how, group, "; ".join(notes), matched=matched,
               assumed=matched < 1.0, name=name, non_us=non_us, symbol=look.get("symbol"), source=source)
    row["via"], row["proxy"] = look.get("via"), look.get("proxy")
    if look.get("ident"):
        row["ident"] = look["ident"]
    return row, pending


def price_eln(h, idx, gc, sleeve, proxy, fund_name):
    """A structured note paying a stock index's return, priced like that index exposure."""
    text = f"{h.get('name') or ''} {h.get('title') or ''}"
    index = text if proxy_for(text) else (fund_name if proxy_for(fund_name) else "")
    label = next((lbl for pat, lbl in INDEX_LABELS if re.search(pat, index, re.I)), "the fund's index")
    fee, matched, how, detail, pending, non_us, look = _underlying({"type": "index", "name": label}, idx, gc,
                                                                   sleeve, proxy)
    exp = h.get("pct") or 0.0
    ok = how.startswith("Look-through")
    row = _row(h, exp, fee, f"Equity-linked note ({label})" if ok else "Assumed rate",
               "lookthrough" if ok else "unmatched",
               f"A note paying the return of {label}, priced like that exposure: {detail}",
               matched=matched, assumed=matched < 1.0, non_us=non_us, source="eln" if ok else "assumed")
    row["via"], row["proxy"] = look.get("via"), look.get("proxy")
    if pending:
        row["_pending"] = True
    return row


INDEX_LABELS = [(r"S&P\s*500|\bSPX\b|\bXSP\b", "the S&P 500"), (r"NASDAQ[- ]?100|\bNDX\b", "the Nasdaq-100"),
                (r"RUSSELL\s*2000|\bRUT\b|\bRTY\b", "the Russell 2000"), (r"DOW JONES|\bDJIA\b|\bDJX\b", "the Dow")]


def liquid(shares, plus=False):
    return plus or (shares or 0) >= COUNTRY_MIN_SHARES


def _country_fee_ok(fee):
    return fee is not None and fee < COUNTRY_MAX_FEE


def country_lines(idx, market, rows):
    """IBKR fees that stand for a market's companies: listed US lines for them plus the
    fund's own holdings from there that matched an IBKR line; liquid lines only, so a
    small company's expensive ADR doesn't set the rate for a market's large caps."""
    fees = {}
    for t in COUNTRY_LINES.get(market, ()):
        e = idx.us_sym.get(t)
        if e and _country_fee_ok(e.get("fee")) and liquid(e.get("shares"), e.get("plus")):
            fees[t] = e["fee"]
    for r in rows:
        if (r.get("home") == market and r["group"] in ("matched", "proxy") and r.get("symbol")
                and _country_fee_ok(r.get("fee")) and r.get("source") != "country_median"
                and liquid(r.get("avail"), r.get("avail_plus"))):
            fees.setdefault(r["symbol"], r["fee"])
    return trim_outliers(fees)


def trim_outliers(fees):
    """Leave out lines far above the rest, most expensive first, so a few expensive ADRs
    can't hold each other's medians up."""
    fees = dict(fees)
    while len(fees) >= COUNTRY_MIN_LINES:
        top = max(fees, key=fees.get)
        rest = statistics.median(f for s, f in fees.items() if s != top)
        if fees[top] < max(STANDIN_MIN_FEE, STANDIN_MAX_MULTIPLE * rest):
            break
        del fees[top]
    return fees


def drop_outlier_standins(rows, idx, gc):
    """Stand-in lines far above their market's other lines go back to being unmatched (and
    then take the market's median, or the assumed rate)."""
    cache = {}
    for r in rows:
        if r.get("source") not in ("us_line", "secondary_listing") or r.get("fee") is None:
            continue
        home = r.get("home")
        if home not in cache:
            cache[home] = country_lines(idx, home, rows)
        peers = [f for s, f in cache[home].items() if s != r.get("symbol")]
        enough = len(peers) >= COUNTRY_MIN_LINES
        base = statistics.median(peers) if enough else gc
        if r["fee"] >= max(STANDIN_MIN_FEE, STANDIN_MAX_MULTIPLE * base):
            label = market_label(home) if home else "its market"
            what = f"the median of other lines for companies from {label}, {base:.2f}%" if enough else \
                f"the assumed rate, {base:.2f}% (too few other lines for companies from {label})"
            note = f"{r['symbol']}'s {r['fee']:.2f}% is far above {what}, so it isn't used"
            r.update(group="unmatched", fee=gc, contrib=max(r["exposure"], 0.0) * gc, how="Assumed rate",
                     assumed=True, matched=0.0, source="assumed", symbol=None, detail=f"{note}; {r['detail']}")


def apply_country_medians(rows, idx):
    """Stocks from a market IBKR doesn't lend in (or that IBKR doesn't list) take the
    median fee of IBKR's lines for that market's companies, labelled as such."""
    cache = {}
    for r in rows:
        home = r.get("home")
        if r["group"] != "unmatched" or not home or home == "us" or r["type"] not in ("Stock", "Preferred", "Holding"):
            continue
        if home not in cache:
            cache[home] = country_lines(idx, home, rows)
        fees, label = cache[home], market_label(home)
        if len(fees) < COUNTRY_MIN_LINES:
            r["detail"] += (f"; fewer than {COUNTRY_MIN_LINES} IBKR lines for companies from {label}, "
                            "so the assumed rate is used")
            continue
        med = statistics.median(fees.values())
        where = "IBKR has no borrow market for" if not idx.has_file(home) else "IBKR doesn't list this holding in"
        examples = ", ".join(f"{s} {f:.2f}%" for s, f in sorted(fees.items(), key=lambda kv: kv[1])[:6])
        before = "; ".join(t for t in r["detail"].split("; ") if not t.startswith("IBKR has no borrow file"))
        r.update(fee=med, contrib=max(r["exposure"], 0.0) * med, group="proxy", how=f"Country median ({label})",
                 assumed=False, source="country_median",
                 detail=f"{where} {label}: priced at the median fee of {len(fees)} IBKR lines for companies from "
                        f"{label} ({examples}{', …' if len(fees) > 6 else ''})" + (f"; {before}" if before else ""))


def add_stated_exposure(rows, idx, gc, fund_name, proxy):
    """When the filing shows far less stock exposure than the fund's name promises (MSTU's
    swaps add up to 1.3% of net assets for a 2× fund; SDTY's S&P 500 exposure sits in a
    deep in-the-money call the filing gives no delta for), the missing exposure is
    added as a labelled row, priced as the stock or index the fund tracks."""
    def pos(r):
        return max(r["exposure"], 0.0)

    if direction_from_name(fund_name) == "short":
        return None
    lev = stated_leverage(fund_name)
    long_eq = sum(pos(r) for r in rows if r["group"] in STOCK_GROUPS)
    cash = sum(pos(r) for r in rows if r["group"] == "cash")
    derivs = [r for r in rows if r["type"] in ("Swap", "Future", "Option", "Derivative")
              and r["group"] in ("lookthrough", "unmatched", "excluded")]
    excluded_value = sum(abs(r.get("value_pct") or 0.0) for r in derivs if r["group"] == "excluded")
    levered = lev >= 1.5 and direction_from_name(fund_name) == "long" and derivs and long_eq < 0.5 * lev
    synthetic = long_eq < 0.1 and derivs and cash + excluded_value >= 0.5
    if not (levered or synthetic):
        return None
    gap = lev - long_eq
    if gap <= 0.05:
        return None
    by_sym = {}
    for r in derivs:
        if r.get("via") == "stock" and r.get("symbol"):
            by_sym[r["symbol"]] = by_sym.get(r["symbol"], 0.0) + pos(r)
    stated = {"name": f"Stated {lev:g}× exposure not in the filing", "stated": True, "pct": gap}
    what, fee, matched, via, sym, prox = None, gc, 0.0, None, None, None
    if by_sym:
        sym = max(by_sym, key=by_sym.get)
        e = idx.us_sym.get(sym)
        if e and e.get("fee") is not None:
            what, fee, matched, via = sym, e["fee"], 1.0, "stock"
    if what is None:
        both = [t for t in name_tickers(" ".join(r["name"] for r in derivs), idx) if t in name_tickers(fund_name, idx)]
        e = idx.us_sym.get(both[0]) if len(both) == 1 else None
        if e is not None and e.get("fee") is not None:
            what, fee, matched, via, sym = both[0], e["fee"], 1.0, "stock", both[0]
    if what is None:
        text = " ".join([fund_name or ""] + [r["name"] for r in derivs])
        p = proxy(text) if proxy and proxy_for(text) else None
        if p and p[0] == "ok":
            what, fee, matched, via, prox = f"{p[1]}'s holdings", p[2], p[3], "proxy", p[1]
        elif p and p[0] == "pending":
            return True
    shown = f"{long_eq:.1%}"
    detail = (f"The filing's stock exposure adds up to {shown} of net assets, but the fund's name says "
              f"{lev:g}×; the missing {gap:.1%} is priced as {what or 'the assumed rate'}")
    row = _row(stated, gap, fee, f"Stated exposure ({what})" if what else "Assumed rate",
               "proxy" if what else "unmatched", detail, matched=matched, assumed=not what,
               symbol=sym if via == "stock" else None, non_us=0.0 if via == "stock" else None,
               source="stated_exposure" if what else "assumed")
    row["via"], row["proxy"] = via, prox
    rows.append(row)
    return False


def price_holdings(hold, idx, gc, cash_fee, fund_name=None, proxy=None, fund=None, ids=None):
    """Every position with its fee and where it came from, plus the totals."""
    holds = list(hold.get("holdings") or [])
    nav = hold.get("net_assets")
    fund_name = fund_name or hold.get("series") or ""
    plain, derivs, notes = [], [], []
    for h in holds:
        if h.get("deriv"):
            derivs.append(h)
        elif h.get("cat") == "SN" or ELN_WORDS.search(f"{h.get('name') or ''} {h.get('title') or ''}"):
            notes.append(h)
        else:
            plain.append(h)
    rows = [price_security(h, idx, gc, cash_fee, ids, fund, proxy) for h in plain]
    if hold.get("source") == "SEC N-PORT":
        rest = 1.0 - sum(h.get("pct") or 0.0 for h in holds)
        if abs(rest) > 1e-4:
            rows.append(price_security({"name": "Cash and other assets less liabilities", "pct": rest,
                                        "residual": True}, idx, gc, cash_fee))
    sleeve = _sleeve(rows)
    # Structured notes in a fund that's mostly stocks pay a stock index's return (JEPI's notes
    # are on the S&P 500); in a bond fund they stay bonds.
    stock_w = sum(max(r["exposure"], 0.0) for r in rows if r["type"] in ("Stock", "Preferred")
                  and r["group"] in EQUITY_GROUPS)
    for h in notes:
        if ELN_WORDS.search(f"{h.get('name') or ''} {h.get('title') or ''}") or stock_w >= 0.5:
            rows.append(price_eln(dict(h, eln=True), idx, gc, sleeve, proxy, fund_name))
        else:
            rows.append(price_security(h, idx, gc, cash_fee, ids, fund, proxy))
    pending = False
    for r in rows:
        pending = r.pop("_pending", False) or pending
    for h in derivs:
        row, p = price_derivative(h, idx, gc, nav, fund_name, sleeve, proxy, ids)
        row["value_pct"] = h.get("pct") or 0.0
        rows.append(row)
        pending = pending or p
    drop_outlier_standins(rows, idx, gc)
    apply_country_medians(rows, idx)
    pending = bool(add_stated_exposure(rows, idx, gc, fund_name, proxy)) or pending
    rows.sort(key=lambda r: abs(r["exposure"]), reverse=True)
    keys = [r["ident"] for r in rows if r.get("ident") and r["group"] == "unmatched"]
    identifying = ids.request(keys) if ids is not None and keys else 0
    s = summarize(rows, pending)
    s["identifying"] = identifying
    return s


def summarize(rows, pending=False):
    def pos(r):
        return max(r["exposure"], 0.0)

    need = [r for r in rows if r["group"] in NEEDS_FEE]
    need_w = sum(pos(r) for r in need)
    matched_w = sum(pos(r) * r["matched"] for r in need)
    proxy_w = sum(pos(r) for r in need if r["group"] == "proxy")
    eq = [r for r in rows if r["group"] in EQUITY_GROUPS]
    gross = sum(pos(r) for r in eq)
    stock = [r for r in rows if r["group"] in STOCK_GROUPS]
    stock_w = sum(pos(r) for r in stock)
    ident_w = sum(pos(r) * (r["matched"] if r["group"] == "lookthrough" else
                            1.0 if r["group"] in ("matched", "proxy", "notlendable") else 0.0) for r in stock)
    by_group = []
    for key, label in GROUPS:
        g = [r for r in rows if r["group"] == key]
        if g:
            by_group.append({"group": key, "label": label, "count": len(g),
                             "weight": sum(r["exposure"] for r in g), "contrib": sum(r["contrib"] for r in g)})
    sources = {}
    for r in rows:
        sources[r["source"]] = sources.get(r["source"], 0.0) + pos(r)
    unmatched = sorted((r for r in need if r["group"] != "proxy" and r["matched"] < 1.0 and pos(r) > 0),
                       key=lambda r: pos(r) * (1 - r["matched"]), reverse=True)
    proxied = sorted((r for r in need if r["group"] == "proxy"), key=pos, reverse=True)
    eq_w = sum(pos(r) for r in eq)
    eq_assumed = sum(pos(r) * (1 - r["matched"]) for r in eq if r["group"] != "proxy")
    return {
        "fee": sum(r["contrib"] for r in rows),
        "coverage": matched_w / need_w if need_w > 0 else None,
        "count": len(rows),
        "need_count": len(need),
        "matched": sum(1 for r in need if r["matched"] > 0),
        "gross_long_equity": gross,
        "cash_weight": sum(pos(r) for r in rows if r["group"] == "cash"),
        "need_weight": need_w,
        "stock_assumed_share": eq_assumed / eq_w if eq_w > 0 else None,
        "assumed_weight": max(need_w - matched_w - proxy_w, 0.0),
        "proxy_weight": proxy_w,
        "notlendable_weight": sum(pos(r) for r in rows if r["group"] == "notlendable"),
        "identified": ident_w / stock_w if stock_w > 0 else None,
        "equity_rate": sum(r["contrib"] for r in eq) / gross if gross > 0 else None,
        "equity_matched": sum(pos(r) * r["matched"] for r in eq) / gross if gross > 0 else None,
        # share of long stock exposure that trades outside the US (decides daily vs weekly beta)
        "foreign_share": weighted_non_us((pos(r), r.get("non_us")) for r in eq),
        "by_group": by_group,
        "sources": sources,
        "unmatched": [{"name": r["name"], "weight": pos(r) * (1 - r["matched"]), "detail": r["detail"]}
                      for r in unmatched[:10]],
        "proxied": [{"name": r["name"], "weight": pos(r), "how": r["how"], "fee": r["fee"]} for r in proxied[:10]],
        "pending": pending,
        "rows": rows,
    }


def outlier_warnings(theo, quoted=None):
    """Plain-language warnings when the estimate looks off."""
    out = []
    fee = theo.get("fee") or 0.0
    rows = theo.get("rows") or theo.get("top") or []
    if fee > 0 and rows:
        top = max(rows, key=lambda r: r["contrib"])
        if top["contrib"] > 0.5 * fee and (top.get("fee") or 0) >= THIN_MIN_FEE:
            out.append(f"{top['name']} drives most of this estimate: {top['fee']:.2f}% a year on "
                       f"{top['exposure']:.0%} of the fund")
    if quoted is not None and fee > 3 * quoted and fee > 2.0:
        out.append(f"The theoretical fee ({fee:.2f}%) is more than 3× the ETF's own IBKR fee ({quoted:.2f}%)")
    if (theo.get("stock_assumed_share") or 0.0) > 0.5:
        out.append("More than half of the stock exposure is charged the assumed rate, so it drives this estimate")
    return out


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
FUND_DEPTH = 2                # funds held by funds are looked through this many levels deep


class EtfService:
    def __init__(self, yf, borrow, foreign=None, ids=None):
        self.yf, self.borrow, self.foreign = yf, borrow, foreign
        self.ids = ids if ids is not None else ident.IDS
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
            s = price_holdings(data, idx, gc, cash, proxy=None, ids=self.ids)
            if s["equity_rate"] is None:
                return ("error", sym, "it has no stock holdings")
            return ("ok", sym, s["equity_rate"], s["equity_matched"], s["foreign_share"])
        return proxy

    def _fund_fn(self, idx, gc, cash, waiting, chain):
        """Prices a fund held by the fund from its own holdings (RYLD holds RSSL)."""
        def fund(sym):
            if sym in chain:
                return ("error", sym, "it holds the fund back")
            if len(chain) > FUND_DEPTH:
                return ("error", sym, "funds inside funds go too deep")
            fut = self._ensure("hold", sym)
            data, _ = self._lookup("hold", sym)
            if data is None:
                if fut is not None:
                    waiting.append(fut)
                return ("pending", sym)
            if "_error" in data or data.get("single_stock"):
                return ("error", sym, data.get("_error", "not a fund"))
            s = self._price(sym, data, idx, gc, cash, waiting, chain=chain)
            if s["pending"]:
                return ("pending", sym)
            if not s["count"]:
                return ("error", sym, "its filing lists no holdings")
            info = (f"{s['count']} positions from its {data['source']}{' of ' + data['as_of'] if data.get('as_of') else ''}"
                    f", {s['fee']:.2f}% a year")
            return ("ok", sym, s["fee"], s["coverage"] if s["coverage"] is not None else 1.0, s["foreign_share"], info)
        return fund

    def _price(self, sym, hold, idx, gc, cash, waiting, fund_name=None, chain=()):
        chain = tuple(chain) + (sym,)
        return price_holdings(hold, idx, gc, cash, fund_name=hold.get("series") or fund_name,
                              proxy=self._proxy_fn(idx, gc, cash, sym, waiting),
                              fund=self._fund_fn(idx, gc, cash, waiting, chain), ids=self.ids)

    def _holdings_block(self, sym, hold, hist, idx, gc, cash, waiting, full=False, quoted=None):
        if hist and hist.get("quote_type") == "EQUITY" or (hold and hold.get("single_stock")):
            return {"single_stock": True}
        if hold is None:
            return {"pending": True}
        if "_error" in hold:
            return {"error": hold["_error"]}
        theo = self._price(sym, hold, idx, gc, cash, waiting, (hist or {}).get("name"))
        theo["warnings"] = outlier_warnings(theo, quoted)
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
        quoted = (table.get(sym) or {}).get("fee")
        proxies = []
        block = self._holdings_block(sym, hold, hist, idx, gc, cash, proxies, full=True, quoted=quoted)
        if proxies:
            wait(proxies, timeout=WAIT_SEC)
            block = self._holdings_block(sym, hold, hist, idx, gc, cash, [], full=True, quoted=quoted)
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
            if r["group"] != "lookthrough" and r["type"] not in EQUITY_TYPES + LOOK_THROUGH_EXTRA + (
                    "Stated exposure", "Equity-linked note"):
                continue
            equity += r["exposure"]
            if r.get("via") == "sleeve":            # an index swap standing on the fund's own stocks
                sleeve += r["exposure"]
            elif r.get("via") in ("proxy", "fund") and r.get("proxy") and depth == 0:
                # priced from another fund's holdings: an index proxy, or a fund this fund holds
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
                out[s] = self._holdings_block(s, hold, hist, idx, gc, cash, proxy_wait,
                                              quoted=(table.get(s) or {}).get("fee"))
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
            theo = row["holdings"].get("theo") or {}
            if row["holdings"].get("pending") or theo.get("pending") or theo.get("identifying"):
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
