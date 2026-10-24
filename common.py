"""
Shared helpers
--------------
- YAHOO: one rate limiter for every Yahoo Finance call the site makes (Stocks page
  prices and metrics, and the ETF screen), so a busy page can't get the server
  blocked. After Yahoo answers "too many requests", all calls pause for a while.
- LRU: a dict with a size cap, so caches can't grow without limit.
- IBKR's stock loan file: download and parse usa.txt. Used by the website (app.py)
  and by the hourly borrow-history recorder (tools/record_borrow.py).
- Ticker spellings: yahoo_symbol() turns IBKR's ticker into Yahoo's (BRK.B -> BRK-B,
  ABR.PRD -> ABR-PD, AAC.U -> AAC-UN) for every Yahoo call on the site, and
  ibkr_symbol() turns a Yahoo spelling someone typed back into IBKR's.
"""

import ftplib
import io
import os
import re
import threading
import time
from collections import OrderedDict, deque, namedtuple
from datetime import datetime, timezone

YAHOO_CALLS_PER_MIN = int(os.environ.get("YAHOO_CALLS_PER_MIN", "150"))
RATE_LIMIT_PAUSE = 90          # seconds to wait after Yahoo says "too many requests"


class RateLimiter:
    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.lock = threading.Lock()
        self.calls = deque()
        self.paused_until = 0.0

    def try_acquire(self, reserve=0):
        """Take one call slot if there is one; never waits. `reserve` slots (a number,
        or a function giving one) are left free for others: background prefetching
        passes one, so it never takes the slots a visitor's open tab needs."""
        if callable(reserve):
            reserve = reserve()
        now = time.time()
        with self.lock:
            if now < self.paused_until:
                return False
            while self.calls and now - self.calls[0] > 60:
                self.calls.popleft()
            if len(self.calls) >= self.per_minute - reserve:
                return False
            self.calls.append(now)
            return True

    def acquire(self, timeout=60.0, reserve=0):
        """Wait up to `timeout` seconds for a call slot (used by background fetches)."""
        deadline = time.time() + timeout
        while True:
            if self.try_acquire(reserve):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.25)

    def pause_left(self):
        """Seconds until calls resume after a "too many requests" answer (0 if not paused)."""
        with self.lock:
            return max(0.0, self.paused_until - time.time())

    def pause(self, seconds=RATE_LIMIT_PAUSE):
        with self.lock:
            self.paused_until = max(self.paused_until, time.time() + seconds)

    def paused(self):
        with self.lock:
            return time.time() < self.paused_until


YAHOO = RateLimiter(YAHOO_CALLS_PER_MIN)


class YahooBusy(Exception):
    """No Yahoo call slot came free in time."""


def is_rate_limit(exc):
    text = str(exc)
    return (type(exc).__name__ == "YFRateLimitError" or "Too Many Requests" in text
            or "Rate limited" in text or "429" in text)


_tls = threading.local()


def set_call_reserve(n):
    """Yahoo slots this thread's calls leave free: a number or a function giving one
    (background prefetching sets one)."""
    _tls.reserve = n


def yahoo_call(fn, timeout=60.0, reserve=None):
    """Run one Yahoo request through the shared limiter. Waits for a slot; backs off on 429."""
    if reserve is None:
        reserve = getattr(_tls, "reserve", 0)
    if not YAHOO.acquire(timeout, reserve):
        raise YahooBusy("Too many Yahoo requests right now")
    try:
        return fn()
    except Exception as e:
        if is_rate_limit(e):
            YAHOO.pause()
        raise


class LRU:
    """A dict that forgets its least recently used entries above `limit`.
    Not thread-safe on its own: callers hold their own lock."""

    def __init__(self, limit):
        self.limit = limit
        self.data = OrderedDict()

    def get(self, key, default=None):
        if key not in self.data:
            return default
        self.data.move_to_end(key)
        return self.data[key]

    def __getitem__(self, key):
        self.data.move_to_end(key)
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
        self.data.move_to_end(key)
        while len(self.data) > self.limit:
            self.data.popitem(last=False)

    def __contains__(self, key):
        return key in self.data

    def __len__(self):
        return len(self.data)

    def pop(self, key, default=None):
        return self.data.pop(key, default)

    def clear(self):
        self.data.clear()


# --------------------------- IBKR stock loan file ---------------------------
FTP_HOST, FTP_USER, FTP_FILE = "ftp2.interactivebrokers.com", "shortstock", "usa.txt"


def normalize(text):
    """A ticker in IBKR's style (BRK.B), or None if it doesn't look like one."""
    s = str(text).strip().upper().replace(" ", ".").lstrip("$")
    return s if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,9}", s) else None


def to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


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


def download_usa(timeout=30):
    """IBKR's US stock loan file as text."""
    with ftplib.FTP(FTP_HOST, timeout=timeout) as ftp:
        ftp.login(FTP_USER, "")
        return ftp_download(ftp, FTP_FILE)


# --------------------------- Yahoo ticker spellings ---------------------------
# IBKR writes share classes, preferreds, units, warrants and rights with a dot; Yahoo
# uses a dash and its own suffixes. Checked against Yahoo with tickers from IBKR's
# file (October 2026): BRK.B -> BRK-B, ABR.PRD -> ABR-PD, DCOM.PR -> DCOM-P,
# AAC.U -> AAC-UN, MSGR.WI -> MSGR-WI; NYSE warrants and rights are AAC-WT and
# DGAC-RI on Yahoo. Dropping the suffix is never tried: TCLIF.OL1 is not TCLIF.
SYMBOL_TTL = 7 * 86400                   # good and bad answers are re-checked weekly
OLD_LISTING = re.compile(r"\.(OLD\d*|OL\d+|RS|RES)$")
IBKR_ONLY = re.compile(r"\.(RST\d*|RSTD|CNT|CONT|TMP|FRAC)$")
LISTING_TEXT = {
    "old_listing": "An old or renamed IBKR listing; Yahoo doesn't carry it",
    "ibkr_only": "A special IBKR line (restricted, contra, temporary or fractional shares); "
                 "Yahoo doesn't carry it",
}
YAHOO_TYPES = {"EQUITY", "ETF", "MUTUALFUND", "CLOSEDENDFUND"}
QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"

# How a spelling was found: same (a plain ticker, used as it is without asking Yahoo),
# verified (IBKR's spelling, checked on Yahoo), rule (translated and checked), isin
# (found by Yahoo's ISIN search), unchecked (Yahoo couldn't be asked; not remembered),
# not_found, old_listing, ibkr_only.
Resolution = namedtuple("Resolution", "yahoo how tried")


def listing_kind(ibkr):
    """'old_listing' or 'ibkr_only' for lines Yahoo never carries, else None."""
    if OLD_LISTING.search(ibkr):
        return "old_listing"
    if IBKR_ONLY.search(ibkr):
        return "ibkr_only"
    return None


def yahoo_spellings(ibkr):
    """Yahoo spellings to try for an IBKR ticker, most likely first. No network."""
    s = ibkr.upper()
    base, dot, suffix = s.partition(".")
    if not dot:
        return [s]
    m = re.fullmatch(r"PR([A-Z]?)", suffix)
    if m:
        return [f"{base}-P{m.group(1)}"]
    if suffix == "U":
        return [f"{base}-UN", f"{base}-U"]
    if suffix == "WS":
        return [f"{base}-WT", f"{base}-WS"]
    if suffix == "RT":
        return [f"{base}-RI", f"{base}-RT"]
    return [f"{base}-{suffix}"]


def ibkr_symbol(text, known=None):
    """IBKR's spelling of a ticker typed in Yahoo's style: BRK-B -> BRK.B, BAC-PD ->
    BAC.PRD, AAC-UN -> AAC.U. `known` (IBKR's table) settles ambiguous cases."""
    s = normalize(str(text or "").replace("/", "."))
    if not s or "-" not in s or (known is not None and s in known):
        return s
    base, _, suf = s.partition("-")
    if not base or not suf or "-" in suf or "." in s:
        return s
    cands = []
    m = re.fullmatch(r"P([A-Z]?)", suf)
    if m:
        cands.append(f"{base}.PR{m.group(1)}")
    cands += {"UN": [f"{base}.U"], "WT": [f"{base}.WS"], "RI": [f"{base}.RT"]}.get(suf, [])
    cands.append(f"{base}.{suf}")
    if known is not None:
        for c in cands:
            if c in known:
                return c
    return cands[0]


def full_isin(isin):
    """The ISIN when it's complete and its check digit is right. IBKR masks US ISINs
    (XXXXXXX02014), which fail here."""
    s = str(isin or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", s) or "XX" in s[:4]:
        return None
    digits = "".join(str(int(ch, 36)) for ch in s[:-1])
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            d = d - 9 if d > 9 else d
        total += d
    return s if (10 - total % 10) % 10 == int(s[-1]) else None


def listed_quote(q):
    """True when Yahoo's quote is a real, tradable listing (Yahoo also answers for
    symbols it doesn't carry, with no exchange, no price and quoteType NONE)."""
    return bool(q) and q.get("quoteType") in YAHOO_TYPES and bool(q.get("exchange")) and bool(
        q.get("regularMarketPrice") or q.get("longName") or q.get("shortName"))


def yahoo_quotes(symbols, timeout=20):
    """Yahoo's quote for up to 50 symbols per request: {symbol: quote}."""
    from yfinance.data import YfData
    out = {}
    symbols = list(dict.fromkeys(symbols))
    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i + 50]
        r = yahoo_call(lambda: YfData().get_raw_json(
            QUOTE_URL, params={"symbols": ",".join(chunk), "formatted": "false"}, timeout=timeout), timeout=timeout)
        for q in ((r or {}).get("quoteResponse") or {}).get("result") or []:
            if q.get("symbol"):
                out[q["symbol"]] = q
    return out


def yahoo_isin_search(isin):
    """Yahoo's search results for an ISIN: [{symbol, exchange, quoteType, ...}]."""
    import yfinance as yf
    return yahoo_call(lambda: yf.Search(isin, max_results=5, news_count=0, lists_count=0,
                                        include_cb=False).quotes) or []


NAME_STOP = {"THE", "INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "PLC", "SA", "NV", "AG", "HOLDINGS",
             "GROUP", "TRUST", "FUND", "ETF", "CLASS", "SHARES", "COMMON", "STOCK", "AMP", "LLC", "LP"}


def name_words(name):
    words = re.findall(r"[A-Z0-9]+", (name or "").upper().replace("&AMP;", "&"))
    return [w for w in words if w not in NAME_STOP and len(w) > 1]


def same_company(a, b):
    """True when two names start with the same significant word ("Arbor Realty Trust Inc" and
    "Arbor Realty Trust, Inc. 6.375% Series D")."""
    wa, wb = name_words(a), set(name_words(b))
    return bool(wa) and wa[0] in wb


def tried_text(tried):
    items = list(tried)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def listing_message(res):
    """The row-level message for a ticker Yahoo doesn't carry, or None."""
    if res.how in LISTING_TEXT:
        return LISTING_TEXT[res.how]
    if res.how == "not_found":
        return "Not on Yahoo Finance; tried " + tried_text(res.tried)
    return None


class SymbolMap:
    """IBKR ticker -> Yahoo ticker, remembered for a week in memory and, when a
    database is attached (DATABASE_URL), in its yahoo_symbols table.

    Plain tickers (AAPL) are taken as they are with no lookup; verify them with
    resolve_many(..., verify=...) when Yahoo turns out to have nothing under that name.
    Dotted tickers are checked against Yahoo's quote list (one request for up to 50),
    then by ISIN when IBKR's file gives a complete one.
    """

    def __init__(self, store=None, quotes=None, search=None, clock=time.time):
        self.store = store
        self.quotes = quotes or yahoo_quotes
        self.search = search or yahoo_isin_search
        self.clock = clock
        self.lock = threading.Lock()
        self.mem = LRU(20000)                # ibkr -> (Resolution, checked_at)

    def _cached(self, sym, now):
        with self.lock:
            hit = self.mem.get(sym)
        if hit and now - hit[1] < SYMBOL_TTL:
            return hit[0]
        return None

    def _remember(self, rows, now):
        with self.lock:
            for sym, res in rows:
                self.mem[sym] = (res, now)
        if self.store is not None and rows:
            try:
                when = datetime.fromtimestamp(now, timezone.utc)
                self.store.symbols_put([(s, r.yahoo, r.how, when) for s, r in rows])
            except Exception as e:
                print(f"Couldn't save Yahoo ticker spellings ({type(e).__name__})", flush=True)

    def forget(self, sym):
        with self.lock:
            self.mem.pop(sym, None)

    def resolve(self, ibkr, isin=None, verify=False):
        return self.resolve_many([ibkr], {ibkr: isin}, verify={ibkr} if verify else ())[ibkr]

    def resolve_many(self, symbols, isins=None, verify=()):
        """{ibkr: Resolution} for each symbol; at most a couple of Yahoo requests in all."""
        isins, verify = isins or {}, set(verify)
        now = self.clock()
        out, todo = {}, []
        for s in dict.fromkeys(symbols):
            kind = listing_kind(s)
            if kind:
                out[s] = Resolution(None, kind, ())
                continue
            hit = self._cached(s, now)            # only checked answers are remembered
            if hit is not None:
                out[s] = hit
                continue
            if "." not in s and s not in verify:
                out[s] = Resolution(s, "same", (s,))
                continue
            todo.append(s)
        if todo and self.store is not None:
            try:
                saved = self.store.symbols_get(todo)
            except Exception:
                saved = {}
            fresh = []
            for s in list(todo):
                row = saved.get(s)
                if not row:
                    continue
                yahoo, how, checked = row
                age = now - checked.timestamp() if checked else SYMBOL_TTL
                if age < SYMBOL_TTL and how in ("same", "rule", "isin", "not_found", "verified"):
                    res = Resolution(yahoo, how, self._tried(s, isins.get(s)) if how == "not_found" else (yahoo,))
                    out[s] = res
                    fresh.append((s, res))
                    todo.remove(s)
            with self.lock:
                for s, res in fresh:
                    self.mem[s] = (res, now)
        if todo:
            self._check(todo, isins, out, now)
        return out

    def _tried(self, sym, isin):
        tried = list(yahoo_spellings(sym))
        isin = full_isin(isin)
        return tuple(tried + ([f"ISIN {isin}"] if isin else []))

    def _check(self, todo, isins, out, now):
        cands = {s: yahoo_spellings(s) for s in todo}
        try:
            found = self.quotes(sorted({c for cs in cands.values() for c in cs}))
        except Exception:
            for s in todo:
                out[s] = Resolution(cands[s][0], "unchecked", tuple(cands[s]))
            return
        keep = []
        for s in todo:
            hit = next((c for c in cands[s] if listed_quote(found.get(c))), None)
            if hit:
                res = Resolution(hit, "verified" if hit == s else "rule", tuple(cands[s]))
            else:
                res = self._by_isin(s, full_isin(isins.get(s)), cands[s])
            out[s] = res
            if res.how != "unchecked":
                keep.append((s, res))
        self._remember(keep, now)

    def _by_isin(self, sym, isin, tried):
        """An exact ISIN match on Yahoo: a US listing (no foreign suffix) whose ticker
        starts like IBKR's, or the only listing Yahoo returns for that ISIN."""
        if not isin:
            return Resolution(None, "not_found", tuple(tried))
        try:
            quotes = [q for q in self.search(isin) or []
                      if q.get("symbol") and "." not in q["symbol"] and q.get("quoteType") in YAHOO_TYPES]
        except Exception:
            return Resolution(tried[0], "unchecked", tuple(tried))
        root = re.split(r"[.\-]", sym)[0]
        pick = next((q for q in quotes if re.split(r"[.\-]", q["symbol"])[0] == root), None)
        if pick is None and len(quotes) == 1:
            pick = quotes[0]
        if pick is None:
            return Resolution(None, "not_found", tuple(tried) + (f"ISIN {isin}",))
        return Resolution(pick["symbol"], "isin", tuple(tried) + (f"ISIN {isin}",))


SYMBOLS = SymbolMap()


def yahoo_symbol(ibkr_symbol, isin=None):
    """Yahoo's ticker for an IBKR ticker (BRK.B -> BRK-B), or None when Yahoo doesn't
    carry it. Plain tickers need no lookup; others are checked once and remembered."""
    return SYMBOLS.resolve(ibkr_symbol, isin).yahoo


def resolve_symbol(ibkr_symbol, isin=None):
    """The full Resolution (yahoo, how, tried) for one IBKR ticker."""
    return SYMBOLS.resolve(ibkr_symbol, isin)
