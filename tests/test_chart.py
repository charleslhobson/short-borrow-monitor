"""Price vs total return chart: history columns, the math, window alignment, downsampling, API."""
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import etf  # noqa: E402
import fake_yf  # noqa: E402

IDX = pd.bdate_range("2018-01-02", "2026-10-21")
MKT = np.random.default_rng(5).normal(0.0004, 0.01, len(IDX))
SPY = {"close": pd.Series(100 * np.cumprod(1 + MKT), index=IDX)}


def fund(price, divs):
    """History the way fetch_history returns it: Yahoo-style adjusted close from the dividends."""
    d = pd.Series(0.0, index=price.index)
    for day, amt in divs.items():
        d[pd.Timestamp(day)] = amt
    adj = price * fake_yf.adjust_factor(price, d)
    return {"close": adj, "price": price, "dividends": d[d > 0]}


class HistoryColumnsTests(unittest.TestCase):
    def test_fetch_history_keeps_price_and_distributions(self):
        h = etf.fetch_history(fake_yf, "FUND")
        raw = fake_yf.Ticker("FUND").history(period="max", interval="1d", auto_adjust=False)
        adjusted = fake_yf.Ticker("FUND").history(period="max", interval="1d", auto_adjust=True)
        # the screen's series is unchanged: the same adjusted close auto_adjust=True gave before
        self.assertTrue(np.allclose(h["close"].to_numpy(), adjusted["Close"].to_numpy()))
        self.assertTrue(np.allclose(h["close"].to_numpy(), raw["Adj Close"].to_numpy()))
        self.assertTrue(np.allclose(h["price"].to_numpy(), raw["Close"].to_numpy()))
        self.assertEqual(len(h["dividends"]), len(raw["Dividends"][raw["Dividends"] > 0]))
        self.assertTrue((h["dividends"] == 0.25).all())
        self.assertEqual(h["yield"], 0.0125)
        self.assertIsNone(h["close"].index.tz)
        self.assertLess(h["close"].iloc[0], h["price"].iloc[0])      # earlier closes adjusted down
        self.assertAlmostEqual(h["close"].iloc[-1], h["price"].iloc[-1])

    def test_no_distributions(self):
        h = etf.fetch_history(fake_yf, "NOPAY")
        self.assertEqual(len(h["dividends"]), 0)
        self.assertTrue(np.allclose(h["close"].to_numpy(), h["price"].to_numpy()))


class ChartMathTests(unittest.TestCase):
    def test_total_return_with_known_distributions(self):
        # the price drops by exactly each $2 payout on its ex-date: price -6%, total return 0%
        idx = pd.bdate_range("2025-01-02", "2026-10-21")
        ex = ["2025-03-03", "2025-06-02", "2025-09-02"]
        price = pd.Series(100.0, index=idx)
        for d in ex:
            price[price.index >= d] -= 2.0
        h = fund(price, {d: 2.0 for d in ex})
        out = etf.chart_data(h, {"close": pd.Series(1.0, index=idx)}, start=date(2025, 1, 2))
        s = out["summary"]
        self.assertAlmostEqual(s["price_return"], -0.06, places=12)
        self.assertAlmostEqual(s["total_return"], 0.0, places=12)
        self.assertAlmostEqual(out["tr"][-1], 100.0, places=4)
        self.assertEqual(out["tr"][0], out["price"][0])
        self.assertAlmostEqual(s["added_pts"], 6.0, places=9)
        self.assertEqual(s["distributions_per_share"], 6.0)
        self.assertEqual([d["date"] for d in out["distributions"]], ex)
        # between payouts the total-return line sits above the price by what's been paid and reinvested
        i = out["dates"].index("2025-07-01")
        self.assertAlmostEqual(out["tr"][i] - out["price"][i], 4.0, places=4)

    def test_yahoo_adjustment_convention(self):
        # if the price doesn't drop on the ex-date, Yahoo's method (prices before it x (1 - D/P))
        # gives P / (P - D) per payout, slightly more than (P + D) / P
        idx = pd.bdate_range("2025-01-02", "2026-10-21")
        h = fund(pd.Series(100.0, index=idx), {"2025-03-03": 2.0})
        out = etf.chart_data(h, {"close": pd.Series(1.0, index=idx)})
        self.assertAlmostEqual(out["summary"]["total_return"], 100 / 98 - 1, places=12)

    def test_price_falls_while_total_return_rises(self):
        idx = pd.bdate_range("2025-01-02", "2025-12-31")
        price = pd.Series(np.linspace(20, 14, len(idx)), index=idx)          # like a high-payout fund
        divs = {f"2025-{m:02d}-03": 0.6 for m in range(2, 13)}
        h = fund(price, {k: v for k, v in divs.items() if pd.Timestamp(k) in idx})
        out = etf.chart_data(h, {"close": pd.Series(1.0, index=idx)})
        s = out["summary"]
        self.assertAlmostEqual(s["price_return"], -0.30, places=6)
        self.assertGreater(s["total_return"], s["price_return"])
        self.assertAlmostEqual(s["added_pts"], (s["total_return"] - s["price_return"]) * 100)
        self.assertAlmostEqual(s["distributions_per_share"], 0.6 * len(out["distributions"]))

    def test_matches_the_table_window(self):
        idx = IDX[IDX >= "2022-08-09"]
        price = pd.Series(30 * np.cumprod(1 + 1.5 * MKT[-len(idx):]), index=idx)
        h = fund(price, {"2023-02-01": 0.3, "2024-02-01": 0.3, "2026-02-02": 0.3})
        spy = SPY["close"]
        for kw in ({"years": None}, {"years": 1}, {"years": 3}, {"start": date(2023, 4, 29)},
                   {"start": date(2020, 1, 1)}):
            w = etf.analyze_window(h["close"], spy, **kw)
            c = etf.chart_data(h, SPY, **kw)
            self.assertEqual((c["start"], c["end"], c["clamped"]), (w["start"], w["end"], w["clamped"]), kw)
            self.assertAlmostEqual(c["summary"]["total_return"], w["etf_total"], places=12)
            self.assertAlmostEqual(c["summary"]["spy_total"], w["spy_total"], places=12)
        self.assertTrue(etf.chart_data(h, SPY, start=date(2020, 1, 1))["clamped"])
        self.assertIn("error", etf.chart_data(h, SPY, years=5))                 # younger than 5 years
        self.assertIn("error", etf.chart_data(h, SPY, start=date(2026, 10, 15)))

    def test_downsampling(self):
        idx = pd.bdate_range("2000-01-03", "2026-10-21")                          # 26 years
        price = pd.Series(np.linspace(10, 50, len(idx)), index=idx)
        h = fund(price, {"2010-05-03": 1.0})
        out = etf.chart_data(h, {"close": pd.Series(1.0, index=idx)})
        self.assertLessEqual(len(out["dates"]), 1000)
        self.assertTrue(out["weekly"])
        self.assertEqual(out["dates"][0], "2000-01-03")
        self.assertEqual(out["dates"][-1], "2026-10-21")
        self.assertEqual(out["dates"], sorted(out["dates"]))
        self.assertAlmostEqual(out["price"][-1], 50.0)
        short = etf.chart_data(h, {"close": pd.Series(1.0, index=idx)}, years=3)
        self.assertFalse(short["weekly"])                                           # 3 years: daily
        four = etf.chart_points(pd.bdate_range("2022-01-03", "2026-10-21"))
        self.assertLessEqual(len(four), 1000)

    def test_missing_payouts_note(self):
        idx = pd.bdate_range("2024-01-02", "2026-10-21")
        price = pd.Series(10.0, index=idx)
        h = fund(price, {})
        self.assertEqual(etf.chart_data(h, {"close": price})["notes"], [])
        h["yield"] = 0.08
        self.assertIn("missing payouts", etf.chart_data(h, {"close": price})["notes"][0])
        self.assertTrue(json.dumps(etf.chart_data(h, {"close": price})))


class FakeEtf:
    def chart(self, sym, years=None, start=None):
        if sym == "NOPE":
            return {"symbol": sym, "error": "Yahoo has no price history for this ticker", "unknown": True}
        return {"symbol": sym, "years": years, "start": start.isoformat() if start else None}


class ChartApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        import app
        cls.app, cls.saved = app, app.ETF
        app.ETF = FakeEtf()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.app.ETF = cls.saved

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_windows(self):
        self.assertEqual(self.get("/api/etf/chart?symbol=tsll&win=1y"), (200, {"symbol": "TSLL", "years": 1, "start": None}))
        self.assertEqual(self.get("/api/etf/chart?symbol=TSLL&win=inception")[1]["years"], None)
        self.assertEqual(self.get("/api/etf/chart?symbol=TSLL&win=custom&start=2023-05-01")[1]["start"], "2023-05-01")
        self.assertEqual(self.get("/api/etf/chart?symbol=TSLL&years=5")[1]["years"], 5)
        self.assertEqual(self.get("/api/etf/chart?symbol=TSLL&years=max")[1]["years"], None)
        self.assertEqual(self.get("/api/etf/chart?symbol=TSLL&start=2024-01-02")[1]["start"], "2024-01-02")

    def test_errors(self):
        for path in ("/api/etf/chart", "/api/etf/chart?symbol=%24%24",
                     "/api/etf/chart?symbol=TSLL&win=custom", "/api/etf/chart?symbol=TSLL&win=custom&start=2024-02-30",
                     "/api/etf/chart?symbol=TSLL&start=2099-01-01", "/api/etf/chart?symbol=TSLL&win=2y",
                     "/api/etf/chart?symbol=TSLL&years=abc"):
            code, body = self.get(path)
            self.assertEqual(code, 400, path)
            self.assertIn("error", body)
        code, body = self.get("/api/etf/chart?symbol=NOPE&win=1y")
        self.assertEqual(code, 404)
        self.assertIn("no price history", body["error"])


if __name__ == "__main__":
    unittest.main()
