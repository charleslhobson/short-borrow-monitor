"""Identifying holdings from outside sources (ident.py), with fake HTTP: OpenFIGI
batching and rate limits, SEC's fails-to-deliver files, Yahoo's ISIN search, and the
cache (memory and database)."""
import io
import json
import sys
import unittest
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import borrow_history  # noqa: E402
import ident  # noqa: E402
from ident import FtdMap, Ident, Identifier, OpenFigi  # noqa: E402

APPLE = {"figi": "BBG000B9XRY4", "compositeFIGI": "BBG000B9XRY4", "ticker": "AAPL", "exchCode": "US",
         "name": "APPLE INC"}


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class KeyTests(unittest.TestCase):
    def test_id_key(self):
        self.assertEqual(ident.id_key("US0378331005"), "ISIN:US0378331005")
        self.assertEqual(ident.id_key(None, "037833100"), "ISIN:US0378331005")       # US CUSIP -> ISIN
        self.assertEqual(ident.id_key(None, "N07059210"), "CUSIP:N07059210")         # foreign CINS stays
        self.assertEqual(ident.id_key("XXXXXXX31005", "000000000"), None)
        self.assertEqual(ident.key_cusip("ISIN:US0378331005"), "037833100")
        self.assertIsNone(ident.key_cusip("ISIN:KR7005930003"))


class OpenFigiTests(unittest.TestCase):
    def setUp(self):
        self.posts, self.sleeps = [], []

    def figi(self, answers, key=""):
        answers = list(answers)

        def post(url, payload, headers):
            self.posts.append((payload, headers))
            status, body = answers.pop(0)
            return status, {"ratelimit-reset": "7"}, json.dumps(body).encode()
        return OpenFigi(key=key, post=post, clock=Clock(), sleep=self.sleeps.append)

    def test_batches_of_ten_without_a_key(self):
        keys = [f"ISIN:US00000000{i:02d}" for i in range(12)]
        f = self.figi([(200, [{"data": [APPLE]}] * 10), (200, [{"warning": "No identifier found."}] * 2)])
        out = f.lookup(keys)
        self.assertEqual([len(p) for p, _ in self.posts], [10, 2])
        self.assertEqual(self.posts[0][0][0], {"idType": "ID_ISIN", "idValue": "US0000000000", "exchCode": "US"})
        self.assertNotIn("X-OPENFIGI-APIKEY", self.posts[0][1])
        self.assertEqual(out[keys[0]], Ident("AAPL", "BBG000B9XRY4", "APPLE INC", "openfigi"))
        self.assertIsNone(out[keys[11]])

    def test_key_raises_the_batch_size(self):
        f = self.figi([(200, [{"data": [APPLE]}] * 30)], key="k")
        f.lookup([f"ISIN:US{i:010d}" for i in range(30)])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0][1], {"X-OPENFIGI-APIKEY": "k"})

    def test_waits_and_retries_after_429(self):
        f = self.figi([(429, {"error": "Too many requests"}), (200, [{"data": [APPLE]}])])
        self.assertEqual(f.lookup(["ISIN:US0378331005"])["ISIN:US0378331005"].ticker, "AAPL")
        self.assertIn(8.0, self.sleeps)                       # ratelimit-reset 7 s, plus 1

    def test_failure_raises(self):
        f = self.figi([(500, {}), (500, {})])
        with self.assertRaises(IOError):
            f.lookup(["ISIN:US0378331005"])

    def test_us_composite_row_is_picked(self):
        rows = [dict(APPLE, exchCode="UN", figi="BBG000B9XVV8"), APPLE, {"ticker": "BRK/B", "exchCode": "US"}]
        self.assertEqual(OpenFigi.pick(rows[:2]).figi, "BBG000B9XRY4")
        self.assertEqual(OpenFigi.pick([rows[2]]).ticker, "BRK.B")
        self.assertIsNone(OpenFigi.pick([]))


def ftd_zip(lines):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cnsfails202609a.txt", "SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE\n"
                   + "\n".join(lines))
    return buf.getvalue()


class FtdTests(unittest.TestCase):
    def test_parse_and_months(self):
        self.assertEqual(ident.parse_ftd("H\n20260915|84615Q103|SPCX|100|SPACE EXPLORATION|1.0\nbad"),
                         {"84615Q103": ("SPCX", "SPACE EXPLORATION")})
        months = ident.ftd_months(date(2026, 2, 10), months=2)
        self.assertEqual(months[:3], [("202602", "b"), ("202602", "a"), ("202601", "b")])
        self.assertIn(("202512", "a"), months)

    def test_loads_linked_files_newest_wins(self):
        got = []

        def get(url):
            got.append(url)
            if url == ident.FTD_PAGE:
                return b'<a href="/files/data/fails-deliver-data/cnsfails202609a.zip">Sep</a>'
            if url.endswith("cnsfails202609a.zip"):
                return ftd_zip(["20260915|84615Q103|SPCX|100|SPACE EXPLORATION|1.0"])
            if url.endswith("cnsfails202608b.zip"):
                return ftd_zip(["20260830|84615Q103|OLDX|100|OLD NAME|1.0"])
            raise IOError("404")
        m = FtdMap(get=get, clock=Clock(), today=lambda: date(2026, 10, 1))
        self.assertEqual(m.lookup("84615Q103"), ("SPCX", "SPACE EXPLORATION"))
        self.assertIn("https://www.sec.gov/files/data/fails-deliver-data/cnsfails202609a.zip", got)
        self.assertIsNone(m.error)

    def test_refused_by_sec(self):
        def get(url):
            raise IOError("403")
        m = FtdMap(get=get, clock=Clock(), today=lambda: date(2026, 10, 1))
        self.assertIsNone(m.lookup("84615Q103"))
        self.assertIn("couldn't be downloaded", m.error)

    def test_without_sec_access(self):
        self.assertIsNone(FtdMap(get=None).lookup("84615Q103"))


class FakeFigi:
    def __init__(self, answers=None, fail=False):
        self.answers, self.fail, self.calls = answers or {}, fail, []

    def lookup(self, keys):
        self.calls.append(list(keys))
        if self.fail:
            raise IOError("OpenFIGI down")
        return {k: self.answers.get(k) for k in keys}


class FakeFtd:
    def __init__(self, table=None, error=None):
        self.table, self.error = table or {}, error

    def lookup(self, cusip):
        return self.table.get(cusip)


class IdentifierTests(unittest.TestCase):
    def make(self, figi=None, ftd=None, yahoo=None, store=None, clock=None):
        return Identifier(store=store, figi=figi or FakeFigi(), ftd=ftd or FakeFtd(), yahoo=yahoo,
                          clock=clock or Clock(), background=False)

    def test_sources_in_order(self):
        figi = FakeFigi({"ISIN:US0378331005": Ident("AAPL", "BBG000B9XRY4", "APPLE INC", "openfigi")})
        ftd = FakeFtd({"84615Q103": ("SPCX", "SPACE EXPLORATION")})
        searched = []

        def yahoo(isin):
            searched.append(isin)
            return [{"symbol": "005930.KS", "exchange": "KSC"}] if isin.startswith("KR") else \
                [{"symbol": "BRK-B", "exchange": "NYQ", "longname": "Berkshire Hathaway Inc."}]
        ids = self.make(figi, ftd, yahoo)
        keys = ["ISIN:US0378331005", "ISIN:US84615Q1031", "ISIN:US0846707026", "ISIN:KR7005930003"]
        out = ids.resolve(keys)
        self.assertEqual(out["ISIN:US0378331005"].src, "openfigi")
        self.assertEqual(out["ISIN:US84615Q1031"], Ident("SPCX", None, "SPACE EXPLORATION", "sec_ftd"))
        self.assertEqual(out["ISIN:US0846707026"], Ident("BRK.B", None, "Berkshire Hathaway Inc.", "yahoo"))
        self.assertEqual(out["ISIN:KR7005930003"].src, "none")        # Yahoo only had a Korean listing
        self.assertEqual(searched, ["US0846707026", "KR7005930003"])

    def test_all_sources_failing_is_an_error_not_a_not_found(self):
        def yahoo(isin):
            raise IOError("Yahoo down")
        ids = self.make(FakeFigi(fail=True), FakeFtd(error="refused"), yahoo)
        self.assertEqual(ids.resolve(["ISIN:US0378331005"])["ISIN:US0378331005"].src, "error")

    def test_database_answers_first_and_results_are_saved(self):
        store = borrow_history.MemoryStore()
        clock = Clock()
        when = datetime.fromtimestamp(clock.t - 86400, timezone.utc)
        store.ids_put([("ISIN:US0378331005", "AAPL", "BBG000B9XRY4", "APPLE INC", "openfigi", when)])
        figi = FakeFigi({"ISIN:US30303M1027": Ident("META", "BBG000MM2P62", "META", "openfigi")})
        ids = self.make(figi, store=store, clock=clock)
        out = ids.resolve(["ISIN:US0378331005", "ISIN:US30303M1027", "ISIN:US9999999999"])
        self.assertEqual(figi.calls, [["ISIN:US30303M1027", "ISIN:US9999999999"]])
        self.assertEqual(out["ISIN:US0378331005"].ticker, "AAPL")
        saved = store.ids_get(["ISIN:US30303M1027", "ISIN:US9999999999"])
        self.assertEqual(saved["ISIN:US30303M1027"][0], "META")
        self.assertEqual(saved["ISIN:US9999999999"][3], "none")

    def test_old_answers_are_looked_up_again(self):
        store = borrow_history.MemoryStore()
        clock = Clock()
        stale = datetime.fromtimestamp(clock.t - ident.FOUND_TTL - 10, timezone.utc)
        store.ids_put([("ISIN:US0378331005", "AAPL", None, None, "openfigi", stale)])
        figi = FakeFigi({"ISIN:US0378331005": Ident("AAPL", "BBG000B9XRY4", "APPLE INC", "openfigi")})
        self.make(figi, store=store, clock=clock).resolve(["ISIN:US0378331005"])
        self.assertEqual(figi.calls, [["ISIN:US0378331005"]])

    def test_request_known_and_state(self):
        clock = Clock()
        figi = FakeFigi({"ISIN:US0378331005": Ident("AAPL", "BBG000B9XRY4", "APPLE INC", "openfigi")})
        ids = self.make(figi, clock=clock)
        self.assertEqual(ids.state("ISIN:US0378331005"), "new")
        self.assertEqual(ids.request(["ISIN:US0378331005", "ISIN:US9999999999", None]), 0)
        self.assertEqual(ids.known("ISIN:US0378331005").ticker, "AAPL")
        self.assertEqual(ids.state("ISIN:US9999999999"), "none")
        self.assertIsNone(ids.known("ISIN:US9999999999"))
        ids.request(["ISIN:US0378331005"])
        self.assertEqual(len(figi.calls), 1)                  # already known: not asked again
        clock.t += ident.NOT_FOUND_TTL + 1
        self.assertEqual(ids.state("ISIN:US9999999999"), "new")

    def test_errors_are_retried_after_an_hour(self):
        clock = Clock()
        ids = self.make(FakeFigi(fail=True), FakeFtd(error="refused"), lambda isin: (_ for _ in ()).throw(IOError()),
                        clock=clock)
        ids.request(["ISIN:US0378331005"])
        self.assertEqual(ids.state("ISIN:US0378331005"), "error")
        clock.t += ident.ERROR_TTL + 1
        self.assertEqual(ids.state("ISIN:US0378331005"), "new")


if __name__ == "__main__":
    unittest.main()
