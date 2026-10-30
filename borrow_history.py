"""
Borrow fee history
------------------
Charts of IBKR's borrow fee and shares available over time. Each point has a source:

  0  recorded by this site from IBKR's usa.txt: every hour by tools/record_borrow.py
     (GitHub Actions), and every 30 minutes by the website while it's awake;
  1  iBorrowDesk's daily history, fetched once per ticker the first time its chart
     is opened, so charts have years of history from day one;
  2  iBorrowDesk's intraday readings from before our own recording started;
  3  the latest IBKR file, added to the end of a chart and not saved.

Only changes are saved (should_write), and charts carry each value forward as a
step line. Points live in Postgres (DATABASE_URL, e.g. a free Neon database).
Without DATABASE_URL nothing is recorded and charts show iBorrowDesk's data live.
"""

import http.client
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from common import LRU, normalize, to_float

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                  # no time zone data (e.g. Windows without tzdata)
    ET = None

SRC_OWN, SRC_IBD_DAILY, SRC_IBD_INTRADAY, SRC_LIVE = 0, 1, 2, 3
SOURCE_NAMES = {SRC_OWN: "IBKR file, recorded by this site", SRC_IBD_DAILY: "iBorrowDesk (daily)",
                SRC_IBD_INTRADAY: "iBorrowDesk (intraday)", SRC_LIVE: "IBKR file, latest"}

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
STORAGE_LIMIT_MB = to_float(os.environ.get("STORAGE_LIMIT_MB")) or 500.0
SITE_URL = os.environ.get("SITE_URL", "https://short-borrow-monitor.onrender.com").strip()
USER_AGENT = f"ShortBorrowMonitor/1.0 (borrow fee charts; +{SITE_URL})"
IBD_API = "https://iborrowdesk.com/api/ticker/{}"
IBD_PAGE = "https://iborrowdesk.com/report/{}"

REBATE_STEP = 0.25             # write when the rebate moves more than this many points...
SHARES_STEP = (to_float(os.environ.get("SHARES_CHANGE_PCT")) or 20) / 100   # ...or shares available move
                               # by this share (or to or from zero); raise it to save space
KEEP_ALL_DAYS = 180            # after this, own points thin to the last reading of each day
RECORD_EVERY = 30 * 60         # the website records at most this often (seconds)
GAP_HOURS = 3                  # no successful recording for this long = a gap on the chart
MAX_POINTS = 1500              # most points one chart request returns
IBD_MIN_INTERVAL = 2.0         # seconds between any two iBorrowDesk requests
IBD_TIMEOUT = 15
IBD_RETRY_FAILED = 3600        # a failed iBorrowDesk lookup is retried after an hour
IBD_BLOCK_PAUSE = 24 * 3600    # when iBorrowDesk turns this server away, ask again once a day
IBD_ENABLED = os.environ.get("IBORROWDESK", "on").strip().lower() != "off"
BLOCKED_TEXT = ("Older history isn't available: iBorrowDesk turns away requests from cloud servers "
                "like this one. This site checks again once a day.")
OFF_TEXT = "Older history from iBorrowDesk is turned off (IBORROWDESK=off)."
IBD_REFETCH = 7 * 86400        # and a good one at most weekly, to fill our recording gaps
IBD_CACHE_AGE = 6 * 3600       # without a database, iBorrowDesk data is kept this long
MISSING_GUARD = 0.8            # a file with fewer than this share of known tickers isn't trusted
SIZE_CACHE_AGE = 3600
RANGES = {"1m": 30, "3m": 91, "6m": 182, "1y": 365, "3y": 1096, "all": None}


# ------------------------------- time helpers -------------------------------
def utcnow():
    return datetime.now(timezone.utc)


def as_utc(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET or timezone.utc)
    return dt.astimezone(timezone.utc)


def et_day(ts):
    """The US trading day a timestamp falls on (New York time)."""
    return ts.astimezone(ET).date() if ET else ts.astimezone(timezone.utc).date()


def epoch(ts):
    return int(ts.timestamp())


def parse_as_of(as_of, now=None):
    """IBKR's '#BOF|2026.10.21|09:30:01' time (New York) as UTC; now if missing or odd."""
    now = now or utcnow()
    try:
        t = datetime.strptime(as_of.strip(), "%Y.%m.%d %H:%M:%S")
    except (ValueError, AttributeError):
        return now
    t = as_utc(t)
    return now if t > now + timedelta(minutes=15) else t


# -------------------------------- write rules --------------------------------
def _num_changed(a, b, step=None):
    if (a is None) != (b is None):
        return True
    if a is None:
        return False
    if step is None:
        return round(a, 4) != round(b, 4)        # stored as 4-byte floats
    return abs(a - b) > step + 1e-6


def should_write(prev, cur):
    """Save a new point? prev and cur are (fee, rebate, shares); prev is the last saved."""
    if prev is None:
        return True
    (pf, pr, ps), (cf, cr, cs) = prev, cur
    if _num_changed(pf, cf) or _num_changed(pr, cr, REBATE_STEP):
        return True
    if (ps is None) != (cs is None):
        return True
    if ps is None:
        return False
    if (ps == 0) != (cs == 0):
        return True
    return ps > 0 and abs(cs - ps) / ps >= SHARES_STEP - 1e-12


def changes(latest, table, ts):
    """Rows to save from one IBKR file. A ticker that drops out of the file gets an empty
    point (not borrowable), unless the file looks incomplete."""
    rows = []
    for sym, e in table.items():
        cur = (e.get("fee"), e.get("rebate"), e.get("shares"))
        if should_write(latest.get(sym), cur):
            rows.append((sym, ts, cur[0], cur[1], cur[2], SRC_OWN))
    present = [s for s, v in latest.items() if v != (None, None, None)]
    if present and len(table) >= MISSING_GUARD * len(present):
        for sym in present:
            if sym not in table:
                rows.append((sym, ts, None, None, None, SRC_OWN))
    return rows


# --------------------------------- storage ----------------------------------
class MemoryStore:
    """The same interface as PgStore, kept in memory (tests)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pts = {}               # (symbol, ts, source) -> (fee, rebate, shares)
        self.fills = {}             # symbol -> {"fetched_at", "status", "first_date"}
        self.run_rows = {}          # ts -> (ok, rows, note)
        self.symbols = {}           # ibkr -> (yahoo, how, checked_at): Yahoo ticker spellings
        self.ids = {}               # "ISIN:..." -> (ticker, figi, name, src, checked_at): ident.Identifier

    def setup(self):
        pass

    def symbols_get(self, ibkrs):
        with self.lock:
            return {s: self.symbols[s] for s in ibkrs if s in self.symbols}

    def symbols_put(self, rows):
        with self.lock:
            for ibkr, yahoo, how, checked in rows:
                self.symbols[ibkr] = (yahoo, how, checked)

    def ids_get(self, keys):
        with self.lock:
            return {k: self.ids[k] for k in keys if k in self.ids}

    def ids_put(self, rows):
        with self.lock:
            for key, ticker, figi, name, src, checked in rows:
                self.ids[key] = (ticker, figi, name, src, checked)

    def latest(self):
        with self.lock:
            best = {}
            for (sym, ts, src), v in self.pts.items():
                if src == SRC_OWN and (sym not in best or ts > best[sym][0]):
                    best[sym] = (ts, v)
            return {s: v for s, (_, v) in best.items()}

    def insert_points(self, rows):
        n = 0
        with self.lock:
            for sym, ts, fee, reb, sh, src in rows:
                if (sym, ts, src) not in self.pts:
                    self.pts[(sym, ts, src)] = (fee, reb, sh)
                    n += 1
        return n

    def points(self, symbol, since=None):
        with self.lock:
            mine = sorted((ts, *v, src) for (s, ts, src), v in self.pts.items() if s == symbol)
        out = [(ts, f, r, sh, src) for ts, f, r, sh, src in mine if since is None or ts >= since]
        if since is not None:
            before = {}
            for ts, f, r, sh, src in mine:
                if ts < since:
                    before[src] = (ts, f, r, sh, src)
            out = sorted(list(before.values()) + out, key=lambda p: (p[0], p[4]))
        return out

    def recording_started(self):
        with self.lock:
            ok = [ts for ts, (good, _, _) in self.run_rows.items() if good]
        return min(ok) if ok else None

    def runs(self, since=None):
        with self.lock:
            ok = sorted(ts for ts, (good, _, _) in self.run_rows.items() if good)
        if since is None:
            return ok
        earlier = [t for t in ok if t < since]
        return earlier[-1:] + [t for t in ok if t >= since]

    def backfill_get(self, symbol):
        with self.lock:
            return dict(self.fills[symbol]) if symbol in self.fills else None

    def backfill_set(self, symbol, fetched_at, status, first_date):
        with self.lock:
            self.fills[symbol] = {"fetched_at": fetched_at, "status": status, "first_date": first_date}

    def record_run(self, ts, ok, rows, note=""):
        with self.lock:
            self.run_rows.setdefault(ts, (ok, rows, note))

    def pruned_since(self, since):
        with self.lock:
            return any(ts >= since and "pruned" in (note or "") for ts, (_, _, note) in self.run_rows.items())

    def prune(self, cutoff):
        with self.lock:
            last = {}
            for (sym, ts, src) in self.pts:
                if src == SRC_OWN and ts < cutoff:
                    k = (sym, et_day(ts))
                    if k not in last or ts > last[k]:
                        last[k] = ts
            drop = [k for k in self.pts if k[2] == SRC_OWN and k[1] < cutoff and last[(k[0], et_day(k[1]))] != k[1]]
            for k in drop:
                del self.pts[k]
            return len(drop)

    def size_bytes(self):
        with self.lock:
            return 100 * len(self.pts)


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS borrow_points (
         symbol text NOT NULL, ts timestamptz NOT NULL, fee real, rebate real, shares bigint,
         source smallint NOT NULL, PRIMARY KEY (symbol, ts, source))""",
    """CREATE TABLE IF NOT EXISTS backfill (
         symbol text PRIMARY KEY, fetched_at timestamptz, status text, first_date date)""",
    """CREATE TABLE IF NOT EXISTS recorder_runs (
         ts timestamptz PRIMARY KEY, ok boolean, rows int, note text)""",
    # Yahoo's spelling of IBKR tickers (common.SymbolMap); yahoo is NULL when Yahoo doesn't carry it
    """CREATE TABLE IF NOT EXISTS yahoo_symbols (
         ibkr text PRIMARY KEY, yahoo text, how text, checked_at timestamptz)""",
    # Who an ETF holding is (ident.Identifier): key is "ISIN:..." or "CUSIP:..."; src says
    # where the answer came from (openfigi, sec_ftd, yahoo) or "none" when nothing was found
    """CREATE TABLE IF NOT EXISTS security_ids (
         key text PRIMARY KEY, ticker text, figi text, name text, src text, checked_at timestamptz)""",
]


class PgStore:
    """Postgres (Neon). One connection, reopened when Neon drops it after idling."""

    def __init__(self, url):
        import psycopg                       # only needed when DATABASE_URL is set
        self.psycopg, self.url = psycopg, url
        self.lock = threading.Lock()
        self.conn, self.ready = None, False

    def _connect(self):
        # prepare_threshold=None: Neon's pooled connections (PgBouncer) can't keep prepared statements
        return self.psycopg.connect(self.url, autocommit=True, connect_timeout=10, prepare_threshold=None,
                                    client_encoding="utf8")

    def _do(self, fn):
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.conn is None or self.conn.closed:
                        self.conn = self._connect()
                    with self.conn.cursor() as cur:
                        if not self.ready:
                            for sql in SCHEMA:
                                cur.execute(sql)
                            self.ready = True
                        return fn(cur)
                except self.psycopg.OperationalError:
                    try:
                        self.conn.close()
                    except Exception:
                        pass
                    self.conn = None
                    if attempt:
                        raise

    def close(self):
        with self.lock:
            if self.conn is not None:
                self.conn.close()
                self.conn = None

    def setup(self):
        self._do(lambda cur: None)

    def symbols_get(self, ibkrs):
        def q(cur):
            cur.execute("SELECT ibkr, yahoo, how, checked_at FROM yahoo_symbols WHERE ibkr = ANY(%s)", (list(ibkrs),))
            return {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
        return self._do(q) if ibkrs else {}

    def symbols_put(self, rows):
        if rows:
            self._do(lambda cur: cur.executemany(
                """INSERT INTO yahoo_symbols (ibkr, yahoo, how, checked_at) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (ibkr) DO UPDATE SET yahoo = EXCLUDED.yahoo, how = EXCLUDED.how,
                   checked_at = EXCLUDED.checked_at""", rows))

    def ids_get(self, keys):
        def q(cur):
            cur.execute("SELECT key, ticker, figi, name, src, checked_at FROM security_ids WHERE key = ANY(%s)",
                        (list(keys),))
            return {r[0]: tuple(r[1:]) for r in cur.fetchall()}
        return self._do(q) if keys else {}

    def ids_put(self, rows):
        if rows:
            self._do(lambda cur: cur.executemany(
                """INSERT INTO security_ids (key, ticker, figi, name, src, checked_at) VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (key) DO UPDATE SET ticker = EXCLUDED.ticker, figi = EXCLUDED.figi,
                   name = EXCLUDED.name, src = EXCLUDED.src, checked_at = EXCLUDED.checked_at""", rows))

    def latest(self):
        def q(cur):
            # ordered to match the primary key backwards, so Postgres can walk the index
            cur.execute("""SELECT DISTINCT ON (symbol) symbol, fee, rebate, shares FROM borrow_points
                           WHERE source = 0 ORDER BY symbol DESC, ts DESC""")
            return {s: (f, r, sh) for s, f, r, sh in cur.fetchall()}
        return self._do(q)

    def insert_points(self, rows):
        if not rows:
            return 0

        def q(cur):
            cur.executemany("""INSERT INTO borrow_points (symbol, ts, fee, rebate, shares, source)
                               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""", rows)
            return max(cur.rowcount, 0)
        return self._do(q)

    def points(self, symbol, since=None):
        def q(cur):
            if since is None:
                cur.execute("""SELECT ts, fee, rebate, shares, source FROM borrow_points
                               WHERE symbol = %s ORDER BY ts, source""", (symbol,))
            else:
                cur.execute("""
                    (SELECT ts, fee, rebate, shares, source FROM borrow_points WHERE symbol = %(s)s AND ts >= %(t)s)
                    UNION ALL
                    (SELECT DISTINCT ON (source) ts, fee, rebate, shares, source FROM borrow_points
                     WHERE symbol = %(s)s AND ts < %(t)s ORDER BY source, ts DESC)
                    ORDER BY ts, source""", {"s": symbol, "t": since})
            return [tuple(r) for r in cur.fetchall()]
        return self._do(q)

    def recording_started(self):
        def q(cur):
            cur.execute("SELECT min(ts) FROM recorder_runs WHERE ok")
            return cur.fetchone()[0]
        return self._do(q)

    def runs(self, since=None):
        def q(cur):
            if since is None:
                cur.execute("SELECT ts FROM recorder_runs WHERE ok ORDER BY ts")
            else:
                cur.execute("""(SELECT ts FROM recorder_runs WHERE ok AND ts >= %(t)s)
                               UNION ALL
                               (SELECT ts FROM recorder_runs WHERE ok AND ts < %(t)s ORDER BY ts DESC LIMIT 1)
                               ORDER BY ts""", {"t": since})
            return [r[0] for r in cur.fetchall()]
        return self._do(q)

    def backfill_get(self, symbol):
        def q(cur):
            cur.execute("SELECT fetched_at, status, first_date FROM backfill WHERE symbol = %s", (symbol,))
            r = cur.fetchone()
            return {"fetched_at": r[0], "status": r[1], "first_date": r[2]} if r else None
        return self._do(q)

    def backfill_set(self, symbol, fetched_at, status, first_date):
        self._do(lambda cur: cur.execute(
            """INSERT INTO backfill (symbol, fetched_at, status, first_date) VALUES (%s, %s, %s, %s)
               ON CONFLICT (symbol) DO UPDATE SET fetched_at = EXCLUDED.fetched_at,
               status = EXCLUDED.status, first_date = EXCLUDED.first_date""",
            (symbol, fetched_at, status, first_date)))

    def record_run(self, ts, ok, rows, note=""):
        self._do(lambda cur: cur.execute(
            "INSERT INTO recorder_runs (ts, ok, rows, note) VALUES (%s, %s, %s, %s) ON CONFLICT (ts) DO NOTHING",
            (ts, ok, rows, note)))

    def pruned_since(self, since):
        def q(cur):
            cur.execute("SELECT 1 FROM recorder_runs WHERE ts >= %s AND note LIKE '%%pruned%%' LIMIT 1", (since,))
            return cur.fetchone() is not None
        return self._do(q)

    def prune(self, cutoff):
        def q(cur):
            cur.execute("""
                DELETE FROM borrow_points p
                WHERE p.source = 0 AND p.ts < %(c)s AND EXISTS (
                  SELECT 1 FROM borrow_points q
                  WHERE q.symbol = p.symbol AND q.source = 0 AND q.ts > p.ts AND q.ts < %(c)s
                    AND (q.ts AT TIME ZONE 'America/New_York')::date = (p.ts AT TIME ZONE 'America/New_York')::date)""",
                        {"c": cutoff})
            return max(cur.rowcount, 0)
        return self._do(q)

    def size_bytes(self):
        def q(cur):
            cur.execute("SELECT pg_database_size(current_database())")
            return cur.fetchone()[0]
        return self._do(q)


def make_store(url=DATABASE_URL):
    if not url:
        return None
    try:
        return PgStore(url)
    except ImportError:
        print("DATABASE_URL is set but psycopg isn't installed, so borrow history isn't recorded.", flush=True)
        return None


# ------------------------------- recording ----------------------------------
def record(store, table, as_of, now=None, prune=None):
    """Save one IBKR file's changes and log the run. Returns (rows saved, note).
    prune: True / False, or None to prune when the last prune was over a day ago."""
    now = now or utcnow()
    ts = parse_as_of(as_of, now)
    try:
        rows = changes(store.latest(), table, ts)
        n = store.insert_points(rows)
        note = f"{len(table):,} tickers in file, {n:,} points saved"
        if prune is None:
            prune = not store.pruned_since(now - timedelta(hours=24))
        if prune:
            note += f", pruned {store.prune(now - timedelta(days=KEEP_ALL_DAYS)):,}"
        store.record_run(now, True, n, note)
        return n, note
    except Exception as e:
        try:
            store.record_run(now, False, 0, f"{type(e).__name__}: {e}"[:300])
        except Exception:
            pass
        raise


def storage_text(size, limit_mb=STORAGE_LIMIT_MB):
    if size is None:
        return None
    mb = size / 1e6
    return f"{mb:,.1f} MB of {limit_mb:,.0f} MB ({mb / limit_mb * 100:.0f}%)"


# ------------------------------- iBorrowDesk --------------------------------
_IBD_LOCK = threading.Lock()
_IBD_LAST = [0.0]


class IbdBlocked(Exception):
    """iBorrowDesk refused this server (not just one ticker)."""


def is_block(exc):
    """True when iBorrowDesk turned the request away: a 401/403/429 reply, or the connection
    closed with no reply (how its server drops requests it won't serve). A missing ticker
    (404) or a slow reply isn't a block."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (401, 403, 429, 444, 451)
    if isinstance(exc, (http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError)):
        return True
    return isinstance(getattr(exc, "reason", None), (ConnectionResetError, ConnectionAbortedError))


def fetch_iborrowdesk(symbol, timeout=IBD_TIMEOUT):
    """iBorrowDesk's JSON for one ticker. At most one request every IBD_MIN_INTERVAL
    seconds across the whole site."""
    with _IBD_LOCK:
        pause = IBD_MIN_INTERVAL - (time.time() - _IBD_LAST[0])
        if pause > 0:
            time.sleep(pause)
        _IBD_LAST[0] = time.time()
    req = urllib.request.Request(IBD_API.format(urllib.parse.quote(symbol)),
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _pick(d, *names):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    low = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        if low.get(n) not in (None, ""):
            return low[n]
    return None


def _num(v):
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        return to_float(v.replace(">", "").replace(",", "").replace("%", "").strip())
    return None


def _when(v, daily):
    """A datetime (UTC) from iBorrowDesk's date or time field."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc)
    s = str(v).strip()
    try:
        if daily and len(s) <= 10:
            d = date.fromisoformat(s)
            return as_utc(datetime(d.year, d.month, d.day, 16, 0))     # the day's close, New York
        if s[:3].isalpha():                                             # "Tue, 03 Jan 2023 00:00:00 GMT"
            t = parsedate_to_datetime(s)
        else:
            t = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
    except (ValueError, TypeError, IndexError):
        return None
    if daily and t.hour == 0 and t.minute == 0:                         # a date: use that day's close
        return as_utc(datetime(t.year, t.month, t.day, 16, 0))
    return as_utc(t)


def _series(items, daily):
    out = {}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        ts = _when(_pick(it, "date", "day", "time", "timestamp", "datetime"), daily)
        if ts is None:
            continue
        fee = _num(_pick(it, "fee", "fee_rate", "feerate", "borrow_fee"))
        reb = _num(_pick(it, "rebate", "rebate_rate", "rebaterate"))
        sh = _num(_pick(it, "available", "shares", "available_shares", "availableshares", "availability"))
        if fee is None and sh is None:
            continue
        out[ts] = (fee, reb, int(sh) if sh is not None else None)
    return [(ts, *v) for ts, v in sorted(out.items())]


def parse_iborrowdesk(obj):
    """{"name", "daily": [(ts, fee, rebate, shares)], "real_time": [...]}; unknown fields are
    ignored and broken rows skipped. Raises ValueError if it isn't iBorrowDesk's format."""
    if not isinstance(obj, dict):
        raise ValueError("iBorrowDesk sent something other than a JSON object")
    daily = _series(obj.get("daily"), True)
    rt = _series(obj.get("real_time") or obj.get("realtime") or obj.get("intraday"), False)
    if "daily" not in obj and "real_time" not in obj:
        raise ValueError("iBorrowDesk's reply has no daily or real_time data")
    return {"name": obj.get("name"), "symbol": obj.get("symbol"), "daily": daily, "real_time": rt}


def unit_scale(parsed, live_fee):
    """1, or 0.01 / 100 if iBorrowDesk's fees are clearly in other units than IBKR's."""
    last = (parsed["real_time"] or parsed["daily"] or [None])[-1]
    if not last or last[1] is None or not live_fee or live_fee <= 0 or last[1] <= 0:
        return 1.0
    ratio = last[1] / live_fee
    if 50 <= ratio <= 200:
        return 0.01
    if 0.005 <= ratio <= 0.02:
        return 100.0
    return 1.0


def scaled(rows, k):
    if k == 1.0:
        return rows
    return [(ts, f * k if f is not None else None, r * k if r is not None else None, sh) for ts, f, r, sh in rows]


# ------------------------------ chart building ------------------------------
def find_gaps(runs, now, since=None, hours=GAP_HOURS):
    """[(from, to)] stretches of `hours` or more with no successful recording."""
    gaps, limit = [], timedelta(hours=hours)
    for a, b in zip(runs, runs[1:]):
        if b - a >= limit:
            gaps.append((a, b))
    if runs and now - runs[-1] >= limit:
        gaps.append((runs[-1], now))
    if since is not None:
        gaps = [(max(a, since), b) for a, b in gaps if b > since]
    return gaps


def _in_gap(ts, gaps):
    return any(a < ts < b for a, b in gaps)


def merge(points, rec_start, gaps):
    """Own readings win: iBorrowDesk points are kept only on days without an own reading,
    and only before our recording started or inside a recording gap."""
    own_days = {et_day(p[0]) for p in points if p[4] == SRC_OWN}
    out = []
    for p in points:
        if p[4] in (SRC_IBD_DAILY, SRC_IBD_INTRADAY):
            if et_day(p[0]) in own_days:
                continue
            if rec_start is not None and p[0] >= rec_start and not _in_gap(p[0], gaps):
                continue
        out.append(p)
    out.sort(key=lambda p: (p[0], p[4] != SRC_OWN))
    dedup = []
    for p in out:
        if dedup and dedup[-1][0] == p[0]:
            continue
        dedup.append(p)
    return dedup


def _thin(points, key):
    keep = []
    for i, p in enumerate(points):
        last_in_bucket = i == len(points) - 1 or key(points[i + 1][0]) != key(p[0])
        fee_change = i == 0 or p[1] != points[i - 1][1]
        if last_in_bucket or fee_change:
            keep.append(p)
    return keep


def downsample(points, max_points=MAX_POINTS):
    """At most max_points: the last reading per day, then per week, always keeping every fee change."""
    if len(points) <= max_points:
        return points
    for key in (et_day, lambda ts: et_day(ts).isocalendar()[:2]):
        points = _thin(points, key)
        if len(points) <= max_points:
            return points
    step = math.ceil(len(points) / max_points)
    return [p for i, p in enumerate(points) if i % step == 0 or i == len(points) - 1]


# --------------------------------- service ----------------------------------
class HistoryService:
    """The /api/borrow/history endpoint, the website's own recording, and backfills.

    live(symbol) -> (entry, as_of text) from the latest IBKR file, or None.
    """

    def __init__(self, store=None, fetch=fetch_iborrowdesk, live=None, clock=utcnow):
        self.store, self.fetch, self.live, self.clock = store, fetch, live, clock
        self.lock = threading.Lock()
        self.cache = LRU(500)              # no database: symbol -> (time, parsed iBorrowDesk data)
        self.failed = {}                   # symbol -> (time, error) of the last failed iBorrowDesk lookup
        self.enabled = IBD_ENABLED         # IBORROWDESK=off skips iBorrowDesk entirely
        self.blocked_until = 0.0           # iBorrowDesk turned this server away: wait until then
        self.inflight = set()
        self.last_record = 0.0
        self.size = (0.0, None)            # (checked at, bytes)
        self.background = True             # fetch iBorrowDesk in a thread (tests turn this off)

    @property
    def recording(self):
        return self.store is not None

    # ---------- recording by the website ----------
    def maybe_record(self, table, as_of):
        if self.store is None or not table:
            return False
        with self.lock:
            if time.time() - self.last_record < RECORD_EVERY:
                return False
            self.last_record = time.time()
        threading.Thread(target=self._record, args=(table, as_of), daemon=True).start()
        return True

    def _record(self, table, as_of):
        try:
            n, note = record(self.store, table, as_of)
            size = self.store.size_bytes()
            with self.lock:
                self.size = (time.time(), size)
            print(f"Borrow history: {note}. Database {storage_text(size)}.", flush=True)
        except Exception as e:
            print(f"Borrow history: couldn't record ({type(e).__name__}: {e}).", flush=True)

    def storage_warning(self):
        """Text for /api/config when the database passes 80% of STORAGE_LIMIT_MB."""
        if self.store is None:
            return None
        with self.lock:
            checked, size = self.size
            stale = time.time() - checked > SIZE_CACHE_AGE
            if stale:
                self.size = (time.time(), size)
        if stale:
            threading.Thread(target=self._check_size, daemon=True).start()
        if size is not None and size > 0.8 * STORAGE_LIMIT_MB * 1e6:
            return f"Borrow history database is at {storage_text(size)}. Raise STORAGE_LIMIT_MB or thin old data."
        return None

    def _check_size(self):
        try:
            size = self.store.size_bytes()
            with self.lock:
                self.size = (time.time(), size)
        except Exception as e:
            print(f"Borrow history: couldn't check the database size ({e}).", flush=True)

    # ---------- iBorrowDesk ----------
    def _live(self, symbol):
        got = self.live(symbol) if self.live else None
        if not got or not got[0]:
            return None
        entry, as_of = got
        return (parse_as_of(as_of, self.clock()), entry.get("fee"), entry.get("rebate"), entry.get("shares"))

    def ibd_available(self):
        with self.lock:
            return self.enabled and time.time() >= self.blocked_until

    def _ibd_reason(self):
        return OFF_TEXT if not self.enabled else BLOCKED_TEXT

    def _ibd(self, symbol):
        """Fetch and parse; scaled to IBKR's units. Raises on failure, IbdBlocked when
        iBorrowDesk refuses this server (then nothing is asked for a day)."""
        try:
            raw = self.fetch(symbol)
        except Exception as e:
            if not is_block(e):
                raise
            with self.lock:
                self.blocked_until = time.time() + IBD_BLOCK_PAUSE
            print(f"Borrow history: iBorrowDesk refused this server ({type(e).__name__}: {e}); "
                  "trying again in 24 hours.", flush=True)
            raise IbdBlocked(str(e)) from e
        parsed = parse_iborrowdesk(raw)
        live = self._live(symbol)
        k = unit_scale(parsed, live[1] if live else None)
        if k != 1.0:
            print(f"Borrow history: iBorrowDesk fees for {symbol} rescaled by {k} to match IBKR.", flush=True)
        parsed["daily"], parsed["real_time"] = scaled(parsed["daily"], k), scaled(parsed["real_time"], k)
        return parsed

    def _start(self, target, symbol):
        with self.lock:
            if symbol in self.inflight:
                return
            self.inflight.add(symbol)
        if self.background:
            threading.Thread(target=target, args=(symbol,), daemon=True).start()
        else:                                  # tests: run it now
            target(symbol)

    def _backfill(self, symbol):
        now = self.clock()
        try:
            try:
                parsed = self._ibd(symbol)
            except IbdBlocked:
                return                     # nothing saved, so this ticker fills in once iBorrowDesk allows it
            except Exception as e:
                self.store.backfill_set(symbol, now, f"error: {type(e).__name__}: {e}"[:200], None)
                return
            rec_start = self.store.recording_started()
            gaps = find_gaps(self.store.runs(), now) if rec_start else []
            own = [p[0] for p in self.store.points(symbol) if p[4] == SRC_OWN]
            first_own = min(own) if own else rec_start

            def fills_hole(ts):
                return rec_start is None or ts < rec_start or _in_gap(ts, gaps)

            rows = [(symbol, ts, f, r, sh, SRC_IBD_DAILY) for ts, f, r, sh in parsed["daily"] if fills_hole(ts)]
            rows += [(symbol, ts, f, r, sh, SRC_IBD_INTRADAY) for ts, f, r, sh in parsed["real_time"]
                     if first_own is None or ts < first_own]
            self.store.insert_points(rows)
            first = parsed["daily"][0][0] if parsed["daily"] else (parsed["real_time"] or [(None,)])[0][0]
            status = "ok" if parsed["daily"] or parsed["real_time"] else "empty"
            self.store.backfill_set(symbol, now, status, et_day(first) if first else None)
        except Exception as e:
            print(f"Borrow history: backfill of {symbol} failed ({type(e).__name__}: {e}).", flush=True)
        finally:
            with self.lock:
                self.inflight.discard(symbol)

    def _fetch_cached(self, symbol):
        try:
            parsed = self._ibd(symbol)
            with self.lock:
                self.cache[symbol] = (time.time(), parsed)
                self.failed.pop(symbol, None)
        except IbdBlocked:
            pass
        except Exception as e:
            with self.lock:
                self.failed[symbol] = (time.time(), f"{type(e).__name__}: {e}")
        finally:
            with self.lock:
                self.inflight.discard(symbol)

    # ---------- the endpoint ----------
    def history(self, symbol, rng="1y"):
        sym = normalize(symbol or "")
        if not sym:
            return {"error": "Give one ticker, e.g. ?symbol=GME"}
        if rng not in RANGES:
            return {"error": f"range must be one of {', '.join(RANGES)}"}
        now = self.clock()
        since = now - timedelta(days=RANGES[rng]) if RANGES[rng] else None
        if self.store is not None:
            try:
                return self._from_store(sym, rng, now, since)
            except Exception as e:
                print(f"Borrow history: database unavailable ({type(e).__name__}: {e}).", flush=True)
                out = self._from_ibd(sym, rng, now, since)
                out["notes"] = ["The history database is unavailable right now, so this shows iBorrowDesk's data."]
                return out
        return self._from_ibd(sym, rng, now, since)

    def _from_store(self, sym, rng, now, since):
        fill = self.store.backfill_get(sym)
        if fill is None and self.ibd_available():   # first chart for this ticker: fetch iBorrowDesk's history
            self._start(self._backfill, sym)
            fill = self.store.backfill_get(sym)
            if fill is None and self.ibd_available():
                return {"symbol": sym, "pending": True}
        available = self.ibd_available()
        with self.lock:
            running = sym in self.inflight
        rec_start = self.store.recording_started()
        all_runs = self.store.runs(since)
        gaps = find_gaps(all_runs, now, since)
        fill = fill or {}
        status = str(fill.get("status") or "")
        failed = status.startswith("error")
        age = (now - fill["fetched_at"]).total_seconds() if fill.get("fetched_at") else math.inf
        if available and not running and fill and (
                (failed and age > IBD_RETRY_FAILED) or
                (not failed and age > IBD_REFETCH and
                 find_gaps(self.store.runs(fill["fetched_at"]), now, fill["fetched_at"]))):
            self._start(self._backfill, sym)
            available = self.ibd_available()
        if not available and (not fill or failed):
            backfill = {"status": "unavailable", "reason": self._ibd_reason()}
        else:
            backfill = {"status": "error" if failed else status, "error": status[7:] if failed else None,
                        "first_date": fill["first_date"].isoformat() if fill.get("first_date") else None,
                        "refreshing": running}
        pts = merge(self.store.points(sym, since), rec_start, gaps)
        return self._response(sym, rng, now, since, pts, gaps, backfill, rec_start)

    def _from_ibd(self, sym, rng, now, since):
        for attempt in (0, 1):
            with self.lock:
                hit = self.cache.get(sym)
                fail = self.failed.get(sym)
            if hit and time.time() - hit[0] < IBD_CACHE_AGE:
                parsed, backfill = hit[1], {"status": "ok"}
                break
            if not self.ibd_available():
                hit = None
                parsed, backfill = {"daily": [], "real_time": []}, {"status": "unavailable", "reason": self._ibd_reason()}
                break
            if fail and time.time() - fail[0] < IBD_RETRY_FAILED:
                parsed, backfill = {"daily": [], "real_time": []}, {"status": "error", "error": fail[1]}
                break
            if attempt:
                return {"symbol": sym, "pending": True}
            self._start(self._fetch_cached, sym)
        pts = [(ts, f, r, sh, SRC_IBD_DAILY) for ts, f, r, sh in parsed["daily"]]
        pts += [(ts, f, r, sh, SRC_IBD_INTRADAY) for ts, f, r, sh in parsed["real_time"]]
        days_rt = {et_day(p[0]) for p in pts if p[4] == SRC_IBD_INTRADAY}
        pts = [p for p in pts if p[4] == SRC_IBD_INTRADAY or et_day(p[0]) not in days_rt]
        pts.sort(key=lambda p: p[0])
        if since is not None:
            before = [p for p in pts if p[0] < since][-1:]
            pts = before + [p for p in pts if p[0] >= since]
        if pts or hit:
            first = parsed["daily"][0][0] if parsed["daily"] else (pts[0][0] if pts else None)
            backfill["first_date"] = et_day(first).isoformat() if first else None
        return self._response(sym, rng, now, since, pts, [], backfill, None)

    def _response(self, sym, rng, now, since, pts, gaps, backfill, rec_start):
        live = self._live(sym)
        if live and (not pts or live[0] > pts[-1][0]):
            pts = pts + [(live[0], live[1], live[2], live[3], SRC_LIVE)]
        pts = downsample(pts)
        own = [p[0] for p in pts if p[4] == SRC_OWN]
        ibd = [p[0] for p in pts if p[4] in (SRC_IBD_DAILY, SRC_IBD_INTRADAY)]
        first_own = min(own) if own else None
        ibd_until = first_own if first_own and ibd and min(ibd) < first_own else None
        # the page adds "Data before <date>: iBorrowDesk" with a link when ibd_link is set
        if self.store is None:
            note = "This site isn't saving its own readings (no database is set up)."
        elif ibd and own:
            note = f"From {et_day(first_own).isoformat()}: recorded by this site from IBKR's file every hour."
        elif ibd:
            note = "This site's own hourly readings will appear as they're recorded."
        elif rec_start:
            note = f"Recorded by this site from IBKR's file every hour since {et_day(rec_start).isoformat()}."
        else:
            note = "Recorded by this site from IBKR's file every hour."
        return {
            "symbol": sym, "range": rng, "now": epoch(now),
            "since": epoch(since) if since else None,
            "points": [{"t": epoch(t), "fee": f, "rebate": r, "shares": sh, "src": s} for t, f, r, sh, s in pts],
            "gaps": [{"from": epoch(a), "to": epoch(b)} for a, b in gaps],
            "backfill": backfill,
            "recording": self.store is not None,
            "recording_started": epoch(rec_start) if rec_start else None,
            "ibd_until": epoch(ibd_until) if ibd_until else None,
            "ibd_link": IBD_PAGE.format(urllib.parse.quote(sym)) if ibd else None,
            "sources": {str(k): v for k, v in SOURCE_NAMES.items()},
            "sources_note": note,
        }
