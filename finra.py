"""
FINRA short interest
--------------------
FINRA publishes short interest for every exchange-listed and OTC stock and ETF
twice a month (settlement dates mid-month and month-end, out about a week and a
half later), as a free pipe-delimited file:

    https://cdn.finra.org/equity/otcmarket/biweekly/shrtYYYYMMDD.csv

The Stocks page uses it when Yahoo has no short interest for a ticker (Yahoo
leaves it out for ETFs, for example). Two files are kept: the latest, and the one
two reports earlier (about a month before), for "Shares short, prior month".

FINRA writes tickers without punctuation (IBKR's ABR.PRD is ABRPRD, BF.A is BFA),
so for dotted tickers the company name has to agree too.
"""

import threading
import time
import urllib.request
from datetime import date, timedelta

from common import same_company

FILE_URL = "https://cdn.finra.org/equity/otcmarket/biweekly/shrt{}.csv"
REFRESH = 6 * 3600            # look for a newer file every 6 hours
RETRY = 15 * 60               # after a failed download
LOOKBACK_DAYS = 80
COLUMNS = ("symbolCode", "issueName", "marketClassCode", "currentShortPositionQuantity",
           "previousShortPositionQuantity", "averageDailyVolumeQuantity", "settlementDate")
def http_get(url, method="GET", timeout=60):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "short-borrow-monitor"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, (r.read() if method == "GET" else b"")
    except urllib.error.HTTPError as e:
        return e.code, b""


def candidate_dates(today, days=LOOKBACK_DAYS):
    """Dates a report could settle on, newest first: the 13th to 15th and the 26th to
    month-end (settlement moves to the business day before when the 15th or the
    last day is a weekend or holiday)."""
    out = []
    for back in range(days):
        d = today - timedelta(days=back)
        if d.weekday() < 5 and (13 <= d.day <= 15 or d.day >= 26):
            out.append(d)
    return out


def parse_file(text):
    """FINRA's file -> {symbol: row}. Numbers as ints, the settlement date as text."""
    lines = text.splitlines()
    if not lines:
        return {}, None
    head = [h.strip().strip('"') for h in lines[0].split("|")]
    try:
        idx = {c: head.index(c) for c in COLUMNS}
    except ValueError:
        return {}, None
    rows, settled = {}, None
    for line in lines[1:]:
        p = [x.strip().strip('"') for x in line.split("|")]
        if len(p) < len(head):
            continue

        def num(c):
            v = p[idx[c]].replace(",", "")
            try:
                return int(float(v))
            except ValueError:
                return None
        sym = p[idx["symbolCode"]].upper()
        if not sym:
            continue
        settled = settled or p[idx["settlementDate"]]
        rows[sym] = {"name": p[idx["issueName"]], "market": p[idx["marketClassCode"]],
                     "short": num("currentShortPositionQuantity"),
                     "previous": num("previousShortPositionQuantity"),
                     "adv": num("averageDailyVolumeQuantity"), "date": p[idx["settlementDate"]]}
    return rows, settled


def finra_symbols(ibkr):
    """FINRA's spelling of an IBKR ticker: the dot dropped (checked against FINRA's file,
    October 2026: ABR.PRD is ABRPRD, BRK.B is BRKB, AAC.U is AACU, DCOM.PR is DCOMPR)."""
    return [ibkr.upper().replace(".", "")]


class FinraShortInterest:
    def __init__(self, get=http_get, clock=time.time, today=date.today):
        self.get, self.clock, self.today = get, clock, today
        self.lock = threading.Lock()
        self.latest = self.month_ago = None     # {"date": "2026-09-30", "rows": {...}}
        self.checked, self.loading, self.error = 0.0, False, None

    def _find(self, after=None, skip=0):
        """The newest file (optionally only ones settled before `after`), skipping `skip`."""
        found = 0
        for d in candidate_dates(self.today() if after is None else after - timedelta(days=1)):
            status, _ = self.get(FILE_URL.format(d.strftime("%Y%m%d")), method="HEAD", timeout=20)
            if status == 200:
                if found == skip:
                    return d
                found += 1
        return None

    def _load(self, d):
        status, body = self.get(FILE_URL.format(d.strftime("%Y%m%d")))
        if status != 200:
            raise IOError(f"FINRA file for {d} answered {status}")
        rows, settled = parse_file(body.decode("utf-8", "replace"))
        if not rows:
            raise ValueError(f"FINRA file for {d} had no rows")
        return {"date": settled or d.isoformat(), "rows": rows}

    def refresh(self):
        try:
            newest = self._find()
            if newest is None:
                raise IOError("no FINRA short interest file in the last few months")
            with self.lock:
                have = self.latest and self.latest["date"] == newest.isoformat()
            if not have:
                latest = self._load(newest)
                older = self._find(after=newest, skip=1)      # two reports back: about a month
                month_ago = None
                if older:
                    o = self._load(older)
                    month_ago = {"date": o["date"], "short": {k: r["short"] for k, r in o["rows"].items()}}
                with self.lock:
                    self.latest, self.month_ago = latest, month_ago
                print(f"FINRA short interest loaded: {len(latest['rows']):,} tickers, as of {latest['date']}", flush=True)
            with self.lock:
                self.error, self.checked = None, self.clock()
        except Exception as e:
            with self.lock:
                self.error = f"Couldn't load FINRA short interest ({type(e).__name__})"
                self.checked = self.clock() - REFRESH + RETRY
            print(self.error, flush=True)
        finally:
            with self.lock:
                self.loading = False

    def ensure(self, background=True):
        """Start loading when the files are missing or stale."""
        with self.lock:
            if self.loading or self.clock() - self.checked < REFRESH:
                return
            self.loading = True
        if background:
            threading.Thread(target=self.refresh, daemon=True).start()
        else:
            self.refresh()

    def state(self):
        """"ready", "loading" or "error"."""
        with self.lock:
            if self.latest:
                return "ready"
            return "error" if self.error and not self.loading else "loading"

    def lookup(self, ibkr, ibkr_name=None):
        """FINRA's report for an IBKR ticker: {short, previous, month_ago, adv, date,
        symbol, name}, or None when FINRA has none."""
        with self.lock:
            latest, older = self.latest, self.month_ago
        if not latest:
            return None
        dotted = "." in ibkr
        for sym in finra_symbols(ibkr):
            row = latest["rows"].get(sym)
            if row is None or (dotted and not same_company(ibkr_name, row["name"])):
                continue
            prior = (older or {}).get("short", {}).get(sym)
            return dict(row, symbol=sym, month_ago=prior, month_ago_date=older["date"] if prior is not None else None)
        return None
