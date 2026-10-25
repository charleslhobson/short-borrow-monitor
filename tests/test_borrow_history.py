"""Borrow fee history: write rules, storage, iBorrowDesk parsing, merging, the service and API.

The iBorrowDesk fixtures follow the shape of its public API (iborrowdesk.com/api/ticker/<SYM>);
the values are made up. Set TEST_DATABASE_URL to also run the storage tests against Postgres.
"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"

import borrow_history as bh  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)


def gme():
    return json.loads((FIX / "iborrowdesk_GME.json").read_text())


def entry(fee, shares, rebate=3.0):
    return {"fee": fee, "rebate": rebate, "shares": shares, "plus": False, "name": "X", "isin": None}


class WriteRuleTests(unittest.TestCase):
    def test_rules(self):
        base = (1.0, 3.0, 100_000)
        cases = [
            (None, base, True, "first reading"),
            (base, base, False, "nothing changed"),
            (base, (1.01, 3.0, 100_000), True, "fee changed"),
            ((0.25000001192, 3.0, 100_000), (0.25, 3.0, 100_000), False, "4-byte float rounding isn't a change"),
            (base, (1.0, 3.2, 100_000), False, "rebate moved 0.2"),
            (base, (1.0, 3.3, 100_000), True, "rebate moved 0.3"),
            (base, (1.0, 3.0, 119_000), False, "shares +19%"),
            (base, (1.0, 3.0, 120_000), True, "shares +20%"),
            (base, (1.0, 3.0, 80_000), True, "shares -20%"),
            (base, (1.0, 3.0, 0), True, "shares to zero"),
            ((1.0, 3.0, 0), (1.0, 3.0, 5), True, "shares from zero"),
            ((1.0, 3.0, 0), (1.0, 3.0, 0), False, "still zero"),
            (base, (None, 3.0, 100_000), True, "fee gone"),
            (base, (1.0, 3.0, None), True, "shares gone"),
        ]
        for prev, cur, want, why in cases:
            self.assertEqual(bh.should_write(prev, cur), want, why)

    def test_changes_and_missing_tickers(self):
        ts = NOW
        latest = {"AAA": (1.0, 3.0, 1000), "BBB": (2.0, 1.0, 50_000), "GONE": (5.0, 0.0, 10)}
        table = {"AAA": entry(1.0, 1000), "BBB": entry(2.5, 50_000, 1.0), "NEW": entry(0.25, 10**7)}
        rows = bh.changes(latest, table, ts)
        got = {r[0]: r[2:5] for r in rows}
        self.assertEqual(set(got), {"BBB", "NEW", "GONE"})
        self.assertEqual(got["GONE"], (None, None, None))          # dropped out of the file
        self.assertTrue(all(r[1] == ts and r[5] == bh.SRC_OWN for r in rows))
        # an incomplete file (most tickers missing) doesn't mark everything as gone
        latest = {f"T{i}": (1.0, 3.0, 100) for i in range(100)}
        rows = bh.changes(latest, {"T1": entry(1.0, 100)}, ts)
        self.assertEqual(rows, [])

    def test_as_of(self):
        self.assertEqual(bh.parse_as_of("2026.10.21 09:30:01", NOW), datetime(2026, 10, 21, 13, 30, 1, tzinfo=UTC))
        self.assertEqual(bh.parse_as_of("", NOW), NOW)
        self.assertEqual(bh.parse_as_of("2026.10.23 09:30:01", NOW), NOW)       # in the future: use now


def exercise_store(t, store):
    """The same checks for MemoryStore and PgStore."""
    store.setup()
    day = lambda d, h=15: datetime(2026, 10, d, h, 0, tzinfo=UTC)   # noqa: E731
    table = {"AAA": entry(1.0, 1000), "BBB": entry(2.0, 50_000)}
    n, note = bh.record(store, table, "2026.10.20 10:00:00", now=day(20, 14), prune=False)
    t.assertEqual(n, 2)
    n, _ = bh.record(store, table, "2026.10.20 11:00:00", now=day(20, 15), prune=False)
    t.assertEqual(n, 0)                                             # nothing changed
    table["AAA"] = entry(1.5, 1000)
    n, _ = bh.record(store, table, "2026.10.21 10:00:00", now=day(21, 14), prune=False)
    t.assertEqual(n, 1)
    t.assertEqual(store.latest()["AAA"][0], 1.5)
    t.assertEqual(store.insert_points([("AAA", day(21, 14), 1.5, 3.0, 1000, bh.SRC_OWN)]), 0)   # duplicate ignored
    t.assertEqual(store.recording_started(), day(20, 14))
    t.assertEqual(store.runs(), [day(20, 14), day(20, 15), day(21, 14)])
    t.assertEqual(store.runs(day(21, 0)), [day(20, 15), day(21, 14)])   # plus the one before

    store.insert_points([("AAA", day(1), 0.9, 3.0, 900, bh.SRC_IBD_DAILY), ("AAA", day(2), 0.95, 3.0, 950, bh.SRC_IBD_DAILY)])
    pts = store.points("AAA", day(10))
    t.assertEqual([p[0] for p in pts], [day(2), datetime(2026, 10, 20, 14, 0, tzinfo=UTC), datetime(2026, 10, 21, 14, 0, tzinfo=UTC)])
    t.assertEqual(len(store.points("AAA")), 4)

    t.assertIsNone(store.backfill_get("AAA"))
    store.backfill_set("AAA", day(21), "ok", date(2026, 10, 1))
    t.assertEqual(store.backfill_get("AAA")["first_date"], date(2026, 10, 1))
    store.backfill_set("AAA", day(22), "error: x", None)
    t.assertEqual(store.backfill_get("AAA")["status"], "error: x")

    # pruning: old own readings thin to the last of each day; iBorrowDesk and recent ones stay
    old = datetime(2025, 1, 6, 15, 0, tzinfo=UTC)
    store.insert_points([("OLD", old + timedelta(hours=h), 1.0 + h, 3.0, 100, bh.SRC_OWN) for h in range(4)])
    store.insert_points([("OLD", old, 9.0, 3.0, 100, bh.SRC_IBD_DAILY)])
    t.assertFalse(store.pruned_since(day(21)))
    n, note = bh.record(store, table, "2026.10.22 10:00:00", now=day(22, 14), prune=None)
    t.assertIn("pruned 3", note)
    left = store.points("OLD")
    t.assertEqual([(p[1], p[4]) for p in left], [(9.0, bh.SRC_IBD_DAILY), (4.0, bh.SRC_OWN)])
    t.assertTrue(store.pruned_since(day(21)))
    _, note = bh.record(store, table, "2026.10.22 11:00:00", now=day(22, 15), prune=None)
    t.assertNotIn("pruned", note)                                  # once a day
    t.assertGreater(store.size_bytes(), 0)


class StoreTests(unittest.TestCase):
    def test_memory_store(self):
        exercise_store(self, bh.MemoryStore())

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to test against Postgres")
    def test_postgres_store(self):
        store = bh.PgStore(os.environ["TEST_DATABASE_URL"])
        store._do(lambda cur: [cur.execute(f"DROP TABLE IF EXISTS {t}") for t in ("borrow_points", "backfill", "recorder_runs")])
        store.ready = False
        exercise_store(self, store)
        store.close()
        exercise_store_again = bh.PgStore(os.environ["TEST_DATABASE_URL"])  # reconnects, tables exist
        self.assertEqual(exercise_store_again.latest()["AAA"][0], 1.5)
        exercise_store_again.close()


class IborrowdeskTests(unittest.TestCase):
    def test_fixture(self):
        p = bh.parse_iborrowdesk(gme())
        self.assertEqual(p["name"], "GAMESTOP CORP-CLASS A")
        self.assertEqual(len(p["daily"]), 538)
        self.assertEqual(len(p["real_time"]), 16)
        ts, fee, reb, sh = p["daily"][0]
        self.assertEqual(ts, datetime(2024, 10, 1, 20, 0, tzinfo=UTC))   # 4 pm New York
        self.assertEqual((fee, reb, sh), (0.8, 2.8, 2693000))
        self.assertEqual(p["real_time"][0][0], datetime(2026, 10, 21, 13, 15, 2, tzinfo=UTC))
        self.assertEqual(sorted(p["daily"]), p["daily"])

    def test_messy_reply(self):
        p = bh.parse_iborrowdesk(json.loads((FIX / "iborrowdesk_TSLL_messy.json").read_text()))
        self.assertEqual([(d[0].date().isoformat(), d[1], d[2], d[3]) for d in p["daily"]], [
            ("2026-09-01", 12.5, -8.4, 1_000_000),     # RFC date, strings, ">1,000,000"
            ("2026-09-02", 13.1, None, 900_000),       # no rebate
            ("2026-09-04", 14.0, -9.9, 800_000),       # capitalized field names, extra fields
        ])
        self.assertEqual(len(p["real_time"]), 1)

    def test_not_iborrowdesk(self):
        for bad in ([], "x", None, {"error": "not found"}):
            with self.assertRaises(ValueError):
                bh.parse_iborrowdesk(bad)
        self.assertEqual(bh.parse_iborrowdesk({"daily": None, "real_time": "x"})["daily"], [])

    def test_unit_check(self):
        p = bh.parse_iborrowdesk(gme())                     # latest fee 0.46
        self.assertEqual(bh.unit_scale(p, 0.46), 1.0)
        self.assertEqual(bh.unit_scale(p, 0.5), 1.0)        # moved a little since
        self.assertEqual(bh.unit_scale(p, 0.0046), 0.01)    # iBorrowDesk 100x IBKR: scale it down
        self.assertEqual(bh.unit_scale(p, 46.0), 100.0)
        self.assertEqual(bh.unit_scale(p, None), 1.0)


def pt(d, fee, src, h=15, sh=100):
    return (datetime(2026, 10, d, h, 0, tzinfo=UTC), fee, 3.0, sh, src)


class ChartTests(unittest.TestCase):
    def test_merge_prefers_own(self):
        rec = datetime(2026, 10, 10, 14, 0, tzinfo=UTC)
        gaps = [(datetime(2026, 10, 15, 0, tzinfo=UTC), datetime(2026, 10, 17, 0, tzinfo=UTC))]
        pts = [pt(5, 1.0, 1), pt(10, 1.1, 1), pt(10, 2.0, 0, h=14), pt(12, 1.2, 1),
               pt(16, 1.3, 1), pt(16, 1.4, 2, h=17), pt(18, 2.5, 0)]
        out = bh.merge(pts, rec, gaps)
        self.assertEqual([(p[1], p[4]) for p in out], [
            (1.0, 1),        # before recording started
            (2.0, 0),        # our reading wins on the 10th
            (1.3, 1), (1.4, 2),   # inside a recording gap
            (2.5, 0)])       # 12th dropped: we were recording, so no change means no new point

    def test_downsample(self):
        start = datetime(2020, 1, 1, tzinfo=UTC)
        pts = [(start + timedelta(hours=i), 1.0 + (i // 500), 3.0, i, 0) for i in range(20_000)]
        out = bh.downsample(pts)
        self.assertLessEqual(len(out), bh.MAX_POINTS)
        fees = [p[1] for p in out]
        self.assertEqual(sorted(set(fees)), sorted({p[1] for p in pts}))      # every fee level kept
        changes_in = [i for i in range(1, len(pts)) if pts[i][1] != pts[i - 1][1]]
        kept = {p[0] for p in out}
        self.assertTrue(all(pts[i][0] in kept for i in changes_in))            # every fee change kept
        self.assertEqual(out[-1], pts[-1])
        self.assertEqual(bh.downsample(pts[:10]), pts[:10])

    def test_gaps(self):
        h = lambda x: NOW - timedelta(hours=x)   # noqa: E731
        runs = [h(10), h(9), h(8), h(4), h(3.5)]
        self.assertEqual(bh.find_gaps(runs, NOW), [(h(8), h(4)), (h(3.5), NOW)])
        self.assertEqual(bh.find_gaps(runs[:-1] + [h(1)], NOW), [(h(8), h(4)), (h(4), h(1))])
        self.assertEqual(bh.find_gaps([], NOW), [])
        self.assertEqual(bh.find_gaps(runs, NOW, since=h(6)), [(h(6), h(4)), (h(3.5), NOW)])


class FakeFetch:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def __call__(self, sym):
        self.calls.append(sym)
        if self.error:
            raise self.error
        return self.reply


def service(store, fetch, live=None):
    s = bh.HistoryService(store, fetch=fetch, live=live, clock=lambda: NOW)
    s.background = False
    return s


class ServiceTests(unittest.TestCase):
    def test_backfill_then_own_readings(self):
        store = bh.MemoryStore()
        live = lambda sym: (entry(0.5, 2_000_000, 3.1), "2026.10.22 15:45:00") if sym == "GME" else None  # noqa: E731
        # we started recording on the 21st, hourly
        for h in range(30):
            store.record_run(datetime(2026, 10, 21, 13, tzinfo=UTC) + timedelta(hours=h), True, 0)
        store.insert_points([("GME", *pt(21, 0.5, 0, h=13, sh=2_000_000))])
        fetch = FakeFetch(gme())
        svc = service(store, fetch, live)
        out = svc.history("gme", "1y")
        self.assertNotIn("pending", out)
        self.assertEqual(fetch.calls, ["GME"])
        self.assertEqual(out["backfill"]["status"], "ok")
        self.assertEqual(out["backfill"]["first_date"], "2024-10-01")
        srcs = [p["src"] for p in out["points"]]
        self.assertEqual(srcs[-1], bh.SRC_LIVE)                          # latest file at the end
        self.assertIn(bh.SRC_OWN, srcs)
        self.assertNotIn(bh.SRC_IBD_INTRADAY, srcs)                      # intraday only before our first reading...
        ts = [p["t"] for p in out["points"]]
        self.assertEqual(ts, sorted(ts))
        own_start = int(datetime(2026, 10, 21, 13, tzinfo=UTC).timestamp())
        self.assertTrue(all(p["t"] < own_start for p in out["points"] if p["src"] in (1, 2)))
        self.assertEqual(out["ibd_until"], own_start)
        self.assertEqual(out["ibd_link"], "https://iborrowdesk.com/report/GME")
        self.assertIn("recorded by this site", out["sources_note"])
        self.assertLess(out["points"][0]["t"], out["since"])             # the value in force at the range start
        # a year of daily points, no older
        self.assertGreater(len(out["points"]), 240)
        # second visit: no new fetch
        svc.history("GME", "3m")
        self.assertEqual(fetch.calls, ["GME"])
        # a week later with a recording gap: fetched again to fill it
        svc.clock = lambda: NOW + timedelta(days=8)
        svc.history("GME", "1m")
        self.assertEqual(fetch.calls, ["GME", "GME"])

    def test_failed_backfill(self):
        store = bh.MemoryStore()
        fetch = FakeFetch(error=urllib.error.URLError("no route"))
        svc = service(store, fetch)
        out = svc.history("ZZZ", "all")
        self.assertEqual(out["backfill"]["status"], "error")
        self.assertIn("no route", out["backfill"]["error"])
        self.assertEqual(out["points"], [])
        svc.history("ZZZ", "all")
        self.assertEqual(len(fetch.calls), 1)                              # not retried for an hour
        svc.clock = lambda: NOW + timedelta(hours=2)
        svc.history("ZZZ", "all")
        self.assertEqual(len(fetch.calls), 2)

    def test_blocked_server(self):
        # iBorrowDesk drops the connection (how it turns away cloud servers): stop asking for a day,
        # save nothing per ticker, and show own readings with a clear reason instead of "loading"
        import http.client
        store = bh.MemoryStore()
        for h in range(5):
            store.record_run(NOW - timedelta(hours=h), True, 0)
        store.insert_points([("GME", NOW - timedelta(hours=4), 0.5, 3.0, 1000, bh.SRC_OWN)])
        fetch = FakeFetch(error=http.client.RemoteDisconnected("Remote end closed connection without response"))
        svc = service(store, fetch)
        out = svc.history("GME", "1m")
        self.assertNotIn("pending", out)
        self.assertEqual(out["backfill"]["status"], "unavailable")
        self.assertIn("turns away requests from cloud servers", out["backfill"]["reason"])
        self.assertEqual(len(out["points"]), 1)
        self.assertIsNone(store.backfill_get("GME"))                       # nothing saved: fills in later
        self.assertFalse(svc.ibd_available())
        for sym in ("AAPL", "TSLA", "GME"):                                 # no more requests today
            self.assertEqual(svc.history(sym)["backfill"]["status"], "unavailable")
        self.assertEqual(fetch.calls, ["GME"])
        svc.blocked_until = 0                                               # a day later, and it's back
        svc.fetch = FakeFetch(gme())
        out = svc.history("GME", "all")
        self.assertEqual(out["backfill"]["status"], "ok")
        self.assertTrue(any(p["src"] == bh.SRC_IBD_DAILY for p in out["points"]))

    def test_old_error_rows_show_as_unavailable_while_blocked(self):
        store = bh.MemoryStore()
        store.backfill_set("GME", NOW - timedelta(hours=3), "error: RemoteDisconnected: closed", None)
        svc = service(store, FakeFetch(error=urllib.error.HTTPError("u", 403, "Forbidden", {}, None)))
        out = svc.history("GME")                                            # retried (old error), refused
        self.assertEqual(out["backfill"]["status"], "unavailable")
        self.assertEqual(len(svc.fetch.calls), 1)

    def test_what_counts_as_a_block(self):
        import http.client
        HE = lambda code: urllib.error.HTTPError("u", code, "x", {}, None)   # noqa: E731
        for e in (HE(403), HE(429), http.client.RemoteDisconnected("x"), ConnectionResetError(),
                  urllib.error.URLError(ConnectionResetError())):
            self.assertTrue(bh.is_block(e), e)
        for e in (HE(404), HE(500), TimeoutError(), urllib.error.URLError("no route"), ValueError("bad json")):
            self.assertFalse(bh.is_block(e), e)

    def test_missing_ticker_is_not_a_block(self):
        svc = service(bh.MemoryStore(), FakeFetch(error=urllib.error.HTTPError("u", 404, "Not Found", {}, None)))
        out = svc.history("ZZZZ")
        self.assertEqual(out["backfill"]["status"], "error")
        self.assertTrue(svc.ibd_available())

    def test_turned_off(self):
        fetch = FakeFetch(gme())
        svc = service(bh.MemoryStore(), fetch)
        svc.enabled = False
        out = svc.history("GME")
        self.assertEqual(out["backfill"], {"status": "unavailable", "reason": bh.OFF_TEXT})
        nodb = service(None, fetch)
        nodb.enabled = False
        self.assertEqual(nodb.history("GME")["backfill"]["status"], "unavailable")
        self.assertEqual(fetch.calls, [])

    def test_pending_while_fetching(self):
        gate = threading.Event()

        def slow(sym):
            gate.wait(5)
            return gme()
        svc = bh.HistoryService(bh.MemoryStore(), fetch=slow, clock=lambda: NOW)
        self.assertEqual(svc.history("GME"), {"symbol": "GME", "pending": True})
        self.assertEqual(svc.history("GME"), {"symbol": "GME", "pending": True})
        gate.set()
        for _ in range(100):
            if not svc.inflight:
                break
            threading.Event().wait(0.05)
        self.assertGreater(len(svc.history("GME")["points"]), 200)

    def test_without_database(self):
        fetch = FakeFetch(gme())
        svc = service(None, fetch, live=lambda s: (entry(0.46, 0, 3.14), "2026.10.22 16:45:00"))
        svc.clock = lambda: NOW + timedelta(hours=1)
        self.assertFalse(svc.recording)
        out = svc.history("GME", "6m")
        self.assertFalse(out["recording"])
        self.assertIn("isn't saving", out["sources_note"])
        srcs = {p["src"] for p in out["points"]}
        self.assertEqual(srcs, {1, 2, 3})
        intraday_days = {datetime.fromtimestamp(p["t"], UTC).date() for p in out["points"] if p["src"] == 2}
        self.assertFalse(any(p["src"] == 1 and datetime.fromtimestamp(p["t"], UTC).date() in intraday_days
                             for p in out["points"]))                       # intraday wins on its days
        svc.history("GME", "1y")
        self.assertEqual(len(fetch.calls), 1)                              # cached
        bad = service(None, FakeFetch(error=TimeoutError("slow")))
        out = bad.history("GME")
        self.assertEqual(out["backfill"]["status"], "error")
        bad.history("GME")
        self.assertEqual(len(bad.fetch.calls), 1)

    def test_rescaled_units(self):
        reply = gme()
        for row in reply["daily"] + reply["real_time"]:
            row["fee"] *= 100                                              # pretend iBorrowDesk sent basis points
        svc = service(None, FakeFetch(reply), live=lambda s: (entry(0.46, 0), "2026.10.22 15:45:00"))
        out = svc.history("GME", "all")
        self.assertAlmostEqual(out["points"][0]["fee"], 0.8)

    def test_database_down(self):
        class Broken(bh.MemoryStore):
            def backfill_get(self, symbol):
                raise OSError("connection refused")
        svc = service(Broken(), FakeFetch(gme()))
        out = svc.history("GME")
        self.assertIn("unavailable", out["notes"][0])
        self.assertGreater(len(out["points"]), 200)

    def test_bad_input(self):
        svc = service(bh.MemoryStore(), FakeFetch(gme()))
        self.assertIn("error", svc.history("$$$"))
        self.assertIn("error", svc.history(""))
        self.assertIn("error", svc.history("GME", "2y"))
        self.assertEqual(svc.fetch.calls, [])

    def test_website_records_at_most_every_30_minutes(self):
        store = bh.MemoryStore()
        svc = bh.HistoryService(store, clock=lambda: NOW)
        table = {"AAA": entry(1.0, 100)}
        self.assertTrue(svc.maybe_record(table, "2026.10.22 10:00:00"))
        self.assertFalse(svc.maybe_record(table, "2026.10.22 10:15:00"))
        for _ in range(100):
            if store.run_rows:
                break
            threading.Event().wait(0.05)
        self.assertEqual(len(store.pts), 1)
        self.assertFalse(bh.HistoryService(None).maybe_record(table, ""))

    def test_storage_warning(self):
        store = bh.MemoryStore()
        svc = bh.HistoryService(store)
        svc.size = (1e18, 450e6)                      # checked "now", 450 MB of 500
        self.assertIn("450.0 MB of 500 MB", svc.storage_warning())
        svc.size = (1e18, 100e6)
        self.assertIsNone(svc.storage_warning())
        self.assertIsNone(bh.HistoryService(None).storage_warning())


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        import app
        cls.app = app
        cls.saved = app.HISTORY
        app.HISTORY = service(None, FakeFetch(gme()))
        app.HISTORY.live = None
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.saved_fresh = app.BORROW.ensure_fresh
        app.BORROW.ensure_fresh = lambda: None

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.app.HISTORY = cls.saved
        cls.app.BORROW.ensure_fresh = cls.saved_fresh

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    def test_history_endpoint(self):
        code, ctype, body = self.get("/api/borrow/history?symbol=GME&range=3m")
        self.assertEqual(code, 200)
        out = json.loads(body)
        self.assertEqual(out["symbol"], "GME")
        self.assertTrue(out["points"])
        for path in ("/api/borrow/history?symbol=%24%24%24", "/api/borrow/history?symbol=GME&range=5y",
                     "/api/borrow/history"):
            code, _, body = self.get(path)
            self.assertEqual(code, 400, path)
            self.assertIn("error", json.loads(body))

    def test_static_files(self):
        code, ctype, body = self.get("/static/uPlot.iife.min.js")
        self.assertEqual(code, 200)
        self.assertIn("javascript", ctype)
        self.assertIn(b"uPlot", body[:2000])
        for path in ("/static/../app.py", "/static/%2e%2e/app.py", "/static/", "/static/nope.js",
                     "/static/.hidden.js", "/static/README.md"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_config_reports_history(self):
        code, _, body = self.get("/api/config")
        self.assertEqual(json.loads(body)["history"], {"recording": False, "storage_warning": None})


if __name__ == "__main__":
    unittest.main()
