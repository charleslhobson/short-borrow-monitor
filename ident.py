"""
Identifying ETF holdings
------------------------
IBKR's US stock loan file hides most of every US ISIN ("XXXXXXX31005" for Apple),
so the ISIN or CUSIP an SEC filing gives for a holding can't be looked up there
directly. etf.MatchIndex first uses what IBKR does show (full ISINs of non-US
companies, the last 5 characters of masked ones, FIGIs and names). For holdings
that still don't match, this module finds the US ticker and FIGI from outside
sources, in this order:

  1. OpenFIGI (free; ISIN or CUSIP -> FIGI and ticker). OPENFIGI_API_KEY (a free
     key from openfigi.com) raises its limits from 25 requests of 10 identifiers a
     minute to 25 requests of 100 identifiers every 6 seconds.
  2. SEC's fails-to-deliver files (twice a month; CUSIP, symbol and description of
     every security with settlement fails), the last 12 months. Needs SEC_USER_AGENT.
  3. Yahoo's ISIN search.

Answers are kept in memory and, when a database is attached (DATABASE_URL), in its
security_ids table: found for 30 days, "not found" for 7. Lookups run in one
background thread. Pricing uses what's already known and says how many holdings
are still being identified, so a page never waits on these sources.
"""

import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import deque, namedtuple
from datetime import date, datetime, timezone

from common import LRU, clean_cusip, cusip_to_isin, full_isin, ibkr_symbol, set_call_reserve

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
OPENFIGI_KEY = os.environ.get("OPENFIGI_API_KEY", "").strip()
FTD_PAGE = "https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data"
FTD_FILE = "https://www.sec.gov/files/data/fails-deliver-data/cnsfails{}{}.zip"
FTD_MONTHS = 12
FTD_REFRESH = 7 * 86400          # a new file comes out twice a month
FTD_RETRY = 86400                # after SEC refused or failed
FOUND_TTL = 30 * 86400
NOT_FOUND_TTL = 7 * 86400
ERROR_TTL = 3600                 # every source failed: try again in an hour (not saved)
YAHOO_PER_BATCH = 20             # Yahoo ISIN searches per batch of lookups, at most
YAHOO_RESERVE = 30               # Yahoo calls per minute left free for visitors' pages
# Yahoo exchange codes for US listings (Nasdaq, NYSE, NYSE American, NYSE Arca, Cboe, OTC)
US_YAHOO_EXCHANGES = {"NMS", "NGM", "NCM", "NAS", "NYQ", "NYS", "ASE", "PCX", "BTS", "PNK", "OQB", "OQX",
                      "OBB", "OEM", "OGM"}

# ticker: US ticker in IBKR's spelling; figi: composite FIGI; src: openfigi, sec_ftd, yahoo,
# or none (nothing found) / error (every source failed this time)
Ident = namedtuple("Ident", "ticker figi name src")
SOURCE_LABELS = {"openfigi": "OpenFIGI", "sec_ftd": "SEC fails-to-deliver data", "yahoo": "Yahoo's ISIN search"}


def id_key(isin=None, cusip=None):
    """The cache key for a holding: 'ISIN:...' (US CUSIPs are turned into ISINs) or
    'CUSIP:...' for a foreign CINS code; None when there's nothing to look up."""
    i = full_isin(isin)
    if i:
        return "ISIN:" + i
    c = clean_cusip(cusip)
    if not c:
        return None
    conv = cusip_to_isin(c)
    return "ISIN:" + conv if conv else "CUSIP:" + c


def key_cusip(key):
    """The CUSIP inside a key, when there is one (US ISINs carry it)."""
    kind, _, value = key.partition(":")
    if kind == "CUSIP":
        return value
    if value.startswith("US") or value.startswith("CA"):
        return value[2:11]
    return None


def http_post_json(url, payload, headers, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


# ---------------------------------- OpenFIGI ----------------------------------
class OpenFigi:
    """ISIN / CUSIP -> the US composite listing's FIGI and ticker."""

    def __init__(self, key=OPENFIGI_KEY, post=http_post_json, clock=time.time, sleep=time.sleep):
        self.key, self.post, self.clock, self.sleep = key, post, clock, sleep
        self.batch = 100 if key else 10
        self.gap = 6.0 / 25 if key else 60.0 / 25        # seconds between requests
        self.last = 0.0
        self.lock = threading.Lock()

    @staticmethod
    def job(key):
        kind, _, value = key.partition(":")
        id_type = "ID_ISIN" if kind == "ISIN" else ("ID_CINS" if value[:1].isalpha() else "ID_CUSIP")
        return {"idType": id_type, "idValue": value, "exchCode": "US"}

    @staticmethod
    def pick(data):
        """The US composite row (exchCode US), else the first row."""
        rows = [d for d in data or [] if d.get("ticker")]
        row = next((d for d in rows if d.get("exchCode") == "US"), rows[0] if rows else None)
        if row is None:
            return None
        ticker = ibkr_symbol(str(row["ticker"]).replace("/", "."))
        return Ident(ticker, row.get("compositeFIGI") or row.get("figi"), row.get("name"), "openfigi")

    def _request(self, jobs):
        headers = {"X-OPENFIGI-APIKEY": self.key} if self.key else {}
        for attempt in (0, 1):
            with self.lock:
                wait = self.gap - (self.clock() - self.last)
                if wait > 0:
                    self.sleep(wait)
                self.last = self.clock()
            status, head, body = self.post(OPENFIGI_URL, jobs, headers)
            if status == 429 and not attempt:
                reset = {k.lower(): v for k, v in head.items()}.get("ratelimit-reset", "60")
                self.sleep(min(60.0, float(reset) if str(reset).replace(".", "").isdigit() else 60.0) + 1)
                continue
            if status != 200:
                raise IOError(f"OpenFIGI answered {status}")
            return json.loads(body)
        raise IOError("OpenFIGI kept answering 429")

    def lookup(self, keys):
        """{key: Ident or None (not found)} for every key; raises when OpenFIGI fails."""
        out = {}
        for i in range(0, len(keys), self.batch):
            chunk = keys[i:i + self.batch]
            res = self._request([self.job(k) for k in chunk])
            for k, r in zip(chunk, res):
                out[k] = self.pick(r.get("data")) if isinstance(r, dict) else None
        return out


# ------------------------------ SEC fails to deliver ------------------------------
def parse_ftd(text):
    """One fails-to-deliver file -> {CUSIP: (symbol, description)}."""
    out = {}
    for line in text.splitlines()[1:]:
        p = line.split("|")
        if len(p) >= 5 and p[1].strip() and p[2].strip():
            out[p[1].strip().upper()] = (p[2].strip().upper(), p[4].strip())
    return out


def ftd_months(today, months=FTD_MONTHS):
    """(YYYYMM, half) for the last `months` months, newest first ('b' = second half)."""
    y, m = today.year, today.month
    out = []
    for _ in range(months + 1):
        out += [(f"{y}{m:02d}", "b"), (f"{y}{m:02d}", "a")]
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


class FtdMap:
    """CUSIP -> US symbol from SEC's fails-to-deliver files (the last 12 months)."""

    def __init__(self, get=None, clock=time.time, today=date.today):
        self.get, self.clock, self.today = get, clock, today
        self.map, self.loaded_at, self.error = None, 0.0, None
        self.lock = threading.Lock()

    def _load(self):
        found, files = {}, 0
        try:
            page = self.get(FTD_PAGE).decode("utf-8", "replace")
            links = re.findall(r'href="([^"]*cnsfails(\d{6})([ab])\.zip)"', page)
        except Exception:
            links = []
        wanted = ftd_months(self.today())
        urls = {}
        for href, ym, half in links:
            if (ym, half) in wanted:
                urls[(ym, half)] = href if href.startswith("http") else "https://www.sec.gov" + href
        for ym, half in wanted:
            urls.setdefault((ym, half), FTD_FILE.format(ym, half))
        for ym, half in reversed(wanted):            # oldest first, so newer files win
            try:
                body = self.get(urls[(ym, half)])
                z = zipfile.ZipFile(io.BytesIO(body))
                found.update(parse_ftd(z.read(z.namelist()[0]).decode("latin-1")))
                files += 1
            except Exception:
                continue
        return found, files

    def lookup(self, cusip):
        """(symbol, description) or None. Loads the files the first time (slow, in the
        identifier's background thread)."""
        with self.lock:
            stale = self.map is None or self.clock() - self.loaded_at > (FTD_REFRESH if self.map else FTD_RETRY)
            if stale and self.get is not None:
                found, files = self._load()
                self.map, self.loaded_at = found, self.clock()
                self.error = None if files else "SEC's fails-to-deliver files couldn't be downloaded"
                if files:
                    print(f"SEC fails-to-deliver files loaded: {files} files, {len(found):,} CUSIPs", flush=True)
            return (self.map or {}).get(cusip)


# --------------------------------- the identifier ---------------------------------
def yahoo_isin_search(isin):
    from common import yahoo_isin_search as search
    return search(isin)


class Identifier:
    def __init__(self, store=None, figi=None, ftd=None, yahoo=yahoo_isin_search, clock=time.time,
                 background=True):
        self.store = store
        self.figi = figi if figi is not None else OpenFigi()
        self.ftd = ftd if ftd is not None else FtdMap()
        self.yahoo = yahoo
        self.clock = clock
        self.background = background
        self.lock = threading.Lock()
        self.mem = LRU(50000)                  # key -> (Ident, checked_at)
        self.queue, self.queued = deque(), set()
        self.running = False

    # ---- reading ----
    def _fresh(self, ident, checked, now):
        ttl = {"none": NOT_FOUND_TTL, "error": ERROR_TTL}.get(ident.src, FOUND_TTL)
        return now - checked < ttl

    def known(self, key):
        """The Ident for a key when one was found (src openfigi / sec_ftd / yahoo), else None."""
        if not key:
            return None
        with self.lock:
            hit = self.mem.get(key)
        if hit and hit[0].src not in ("none", "error") and self._fresh(hit[0], hit[1], self.clock()):
            return hit[0]
        return None

    def state(self, key):
        """'found', 'none' (looked up, nothing found), 'error', 'pending' or 'new'."""
        with self.lock:
            hit = self.mem.get(key)
            if key in self.queued:
                return "pending"
        if hit and self._fresh(hit[0], hit[1], self.clock()):
            return {"none": "none", "error": "error"}.get(hit[0].src, "found")
        return "new"

    # ---- asking ----
    def request(self, keys):
        """Queue the keys that aren't known yet; returns how many of them are still
        being looked up."""
        pending, start = 0, False
        with self.lock:
            now = self.clock()
            for k in dict.fromkeys(k for k in keys if k):
                hit = self.mem.get(k)
                if hit and self._fresh(hit[0], hit[1], now):
                    continue
                pending += 1
                if k not in self.queued:
                    self.queue.append(k)
                    self.queued.add(k)
            if self.queue and not self.running:
                self.running = start = True
        if start:
            if self.background:
                threading.Thread(target=self._work, daemon=True).start()
            else:
                self._work()
                pending = sum(1 for k in dict.fromkeys(k for k in keys if k) if self.state(k) == "pending")
        return pending

    def _work(self):
        set_call_reserve(YAHOO_RESERVE)
        while True:
            with self.lock:
                batch = [self.queue.popleft() for _ in range(min(100, len(self.queue)))]
                if not batch:
                    self.running = False
                    return
            try:
                results = self.resolve(batch)
            except Exception as e:                       # never leave keys stuck as pending
                print(f"Identifying holdings failed ({type(e).__name__}: {e})", flush=True)
                results = {k: Ident(None, None, None, "error") for k in batch}
            now = self.clock()
            with self.lock:
                for k in batch:
                    self.mem[k] = (results.get(k) or Ident(None, None, None, "error"), now)
                    self.queued.discard(k)

    def resolve(self, keys):
        """Look the keys up now (database, then OpenFIGI, SEC, Yahoo); {key: Ident}."""
        now = self.clock()
        out, todo = {}, list(keys)
        if self.store is not None and todo:
            try:
                saved = self.store.ids_get(todo)
            except Exception:
                saved = {}
            for k, (ticker, figi, name, src, checked) in saved.items():
                ident = Ident(ticker, figi, name, src)
                if checked is not None and self._fresh(ident, checked.timestamp(), now):
                    out[k] = ident
            todo = [k for k in todo if k not in out]
        failed = set()
        if todo:
            try:
                for k, ident in self.figi.lookup(todo).items():
                    if ident is not None and ident.ticker:
                        out[k] = ident
            except Exception as e:
                print(f"OpenFIGI lookup failed ({type(e).__name__}: {e})", flush=True)
                failed.add("openfigi")
            todo = [k for k in todo if k not in out]
        for k in list(todo):
            cusip = key_cusip(k)
            hit = self.ftd.lookup(cusip) if cusip and self.ftd is not None else None
            if hit:
                out[k] = Ident(ibkr_symbol(hit[0]), None, hit[1], "sec_ftd")
                todo.remove(k)
        if self.ftd is not None and getattr(self.ftd, "error", None):
            failed.add("sec_ftd")
        searched = 0
        for k in list(todo):
            kind, _, value = k.partition(":")
            if kind != "ISIN" or self.yahoo is None or searched >= YAHOO_PER_BATCH:
                continue
            searched += 1
            try:
                quotes = self.yahoo(value) or []
            except Exception:
                failed.add("yahoo")
                continue
            us = [q for q in quotes if q.get("symbol") and q.get("exchange") in US_YAHOO_EXCHANGES
                  and "." not in q["symbol"]]
            if us:
                q = us[0]
                out[k] = Ident(ibkr_symbol(q["symbol"]), None, q.get("longname") or q.get("shortname"), "yahoo")
                todo.remove(k)
        for k in todo:
            # nothing found: remembered as "none" for a week, unless OpenFIGI (the main source)
            # failed this time, so the answer isn't known yet ("error": tried again in an hour)
            out[k] = Ident(None, None, None, "error" if "openfigi" in failed else "none")
        self._save(out, now)
        return out

    def _save(self, results, now):
        if self.store is None:
            return
        when = datetime.fromtimestamp(now, timezone.utc)
        rows = [(k, i.ticker, i.figi, i.name, i.src, when) for k, i in results.items() if i.src != "error"]
        try:
            self.store.ids_put(rows)
        except Exception as e:
            print(f"Couldn't save holding identifiers ({type(e).__name__})", flush=True)

    def drain(self, timeout=600.0, poll=0.5):
        """Wait until nothing is queued (reports and tests)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if not self.queue and not self.running:
                    return True
            time.sleep(poll)
        return False


IDS = Identifier()
