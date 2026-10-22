"""Stocks page metrics: fallbacks, reasons, sources, fetching only what's needed,
the shared Yahoo rate limiter, capped caches and the ETF screen's price columns."""
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import common  # noqa: E402
import fake_yf  # noqa: E402
import metrics  # noqa: E402

PROFILES = ["FULL", "THIN", "FUND", "BANK", "NOPAY", "IPO", "NOCOV", "FAIL"]
KEYS = metrics.YAHOO_KEYS


def fresh_service():
    fake_yf.reset()
    common.YAHOO.per_minute = 100_000
    common.YAHOO.calls.clear()
    common.YAHOO.paused_until = 0.0
    return metrics.MetricService(fake_yf)


def daily(sym):
    return fake_yf.Ticker(sym)._daily()


def ts(day):
    return int(pd.Timestamp(day, tz="UTC").timestamp())


class AllProfiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows, cls.pending = fresh_service().get(PROFILES, KEYS)
        cls.rows = {r["symbol"]: r for r in rows}

    def test_every_metric_has_a_value_or_a_reason(self):
        self.assertEqual(self.pending, 0)
        for sym, r in self.rows.items():
            self.assertEqual(r["loading"], [], sym)
            self.assertEqual(set(r["data"]) | set(r["why"]), set(KEYS), sym)
            self.assertFalse(set(r["data"]) & set(r["why"]), sym)
            for code in r["why"].values():
                self.assertIn(code, metrics.REASONS)
            for src in r["src"].values():
                self.assertIn(src, metrics.SOURCES)

    def test_full_profile_comes_straight_from_yahoo(self):
        r = self.rows["FULL"]
        self.assertEqual(len(r["data"]), len(KEYS))
        for k, v in fake_yf.FULL_INFO.items():
            if k in metrics.STRATEGIES:
                self.assertEqual(r["data"][k], v, k)
                self.assertEqual(r["src"][k], "yahoo", k)
        self.assertEqual(r["src"]["perf_1y"], "history")
        self.assertAlmostEqual(r["data"]["short_change"], 5e6 / 4e6 - 1)
        self.assertAlmostEqual(r["data"]["target_upside"], 120 / 101.5 - 1)

    def test_statement_fallbacks(self):
        r = self.rows["THIN"]
        d, src = r["data"], r["src"]
        self.assertEqual((d["totalRevenue"], src["totalRevenue"]), (4.1e9, "statements"))   # last 4 quarters
        self.assertAlmostEqual(d["grossMargins"], 0.4)
        self.assertEqual(src["grossMargins"], "calculated")
        self.assertAlmostEqual(d["trailingEps"], 6.1)
        self.assertAlmostEqual(d["trailingPE"], d["currentPrice"] / 6.1)
        self.assertAlmostEqual(d["operatingMargins"], 0.2)
        self.assertAlmostEqual(d["returnOnAssets"], 610e6 / 9500e6)
        self.assertAlmostEqual(d["returnOnEquity"], 610e6 / 3800e6)
        self.assertAlmostEqual(d["revenueGrowth"], 0.1)
        self.assertAlmostEqual(d["earningsGrowth"], 0.1)
        self.assertAlmostEqual(d["currentRatio"], 3.0)
        self.assertAlmostEqual(d["quickRatio"], 2.7)
        self.assertAlmostEqual(d["debtToEquity"], 37.5)                    # x100, like Yahoo
        self.assertAlmostEqual(d["freeCashflow"], 600e6)
        self.assertAlmostEqual(d["bookValue"], 4000e6 / 1e8)
        self.assertAlmostEqual(d["enterpriseValue"], d["marketCap"] + 1500e6 - 1200e6)
        self.assertEqual(d["mostRecentQuarter"], ts("2026-06-30"))
        self.assertEqual(d["lastFiscalYearEnd"], ts("2025-12-31"))
        self.assertEqual(d["nextFiscalYearEnd"], ts("2026-12-31"))

    def test_history_fallbacks(self):
        r = self.rows["THIN"]
        d, bars = r["data"], daily("THIN")
        close = bars["Close"]
        self.assertAlmostEqual(d["currentPrice"], close.iloc[-1])
        self.assertAlmostEqual(d["previousClose"], close.iloc[-2])
        self.assertAlmostEqual(d["fiftyDayAverage"], close.iloc[-50:].mean())
        self.assertAlmostEqual(d["twoHundredDayAverage"], close.iloc[-200:].mean())
        self.assertAlmostEqual(d["averageVolume"], bars["Volume"].iloc[-63:].mean())
        year = bars[bars.index >= bars.index[-1] - pd.DateOffset(years=1)]
        self.assertAlmostEqual(d["fiftyTwoWeekHigh"], year["High"].max())
        self.assertAlmostEqual(d["fiftyTwoWeekLowChangePercent"], (d["currentPrice"] - d["fiftyTwoWeekLow"]) / d["fiftyTwoWeekLow"])
        self.assertAlmostEqual(d["beta"], 1.5, delta=0.3)                  # fake: 1.5x the market
        self.assertIn("monthly returns vs SPY", r["note"]["beta"])
        self.assertEqual(d["firstTradeDateMilliseconds"] // 1000, ts("2015-01-02") + 5 * 3600)
        self.assertEqual(d["exchange"], "NMS")

    def test_analysts_holders_calendar_shares_intraday(self):
        d, src = self.rows["THIN"]["data"], self.rows["THIN"]["src"]
        self.assertAlmostEqual(d["recommendationMean"], 2.05)
        self.assertEqual(d["recommendationKey"], "buy")
        self.assertEqual(d["averageAnalystRating"], "2.1 - Buy")
        self.assertEqual(d["numberOfAnalystOpinions"], 20)
        self.assertEqual((d["targetMeanPrice"], d["targetLowPrice"]), (110.0, 80.0))
        self.assertEqual((d["forwardEps"], d["epsCurrentYear"]), (7.4, 6.6))
        self.assertEqual(d["heldPercentInsiders"], 0.031)
        self.assertEqual(d["earningsTimestamp"], ts("2026-11-05"))
        self.assertEqual((d["sharesOutstanding"], src["sharesOutstanding"]), (1e8, "shares"))
        self.assertEqual(d["impliedSharesOutstanding"], 1e8)
        self.assertEqual(src["preMarketPrice"], "intraday")
        self.assertIn("09:25", self.rows["THIN"]["note"]["preMarketPrice"])
        self.assertIn("19:55", self.rows["THIN"]["note"]["postMarketPrice"])

    def test_dividends_and_splits(self):
        d, src = self.rows["THIN"]["data"], self.rows["THIN"]["src"]
        self.assertAlmostEqual(d["dividendRate"], 1.0)                      # 0.25 x 4 a year
        self.assertAlmostEqual(d["trailingAnnualDividendRate"], 1.0)
        self.assertAlmostEqual(d["yield_calc"], 1.0 / d["currentPrice"])
        self.assertAlmostEqual(d["payoutRatio"], 1.0 / 6.1)
        self.assertGreater(d["fiveYearAvgDividendYield"], 0.1)              # percent, like Yahoo
        self.assertEqual(d["lastDividendValue"], 0.25)
        self.assertEqual((d["exDividendDate"], src["exDividendDate"]), (ts("2026-11-10"), "calendar"))
        self.assertEqual((d["lastSplitFactor"], src["lastSplitFactor"]), ("3:1", "splits"))

    def test_reasons(self):
        why = {s: r["why"] for s, r in self.rows.items()}
        self.assertEqual(why["THIN"]["bid"], "closed")                      # Yahoo sent 0 with the market closed
        self.assertNotIn("bid", self.rows["THIN"]["data"])
        self.assertEqual(why["THIN"]["overallRisk"], "no_iss")
        self.assertEqual(why["THIN"]["sharesShort"], "no_short")
        self.assertEqual(why["FUND"]["trailingPE"], "fund")
        self.assertEqual(why["FUND"]["country"], "fund")
        self.assertEqual(why["FUND"]["targetMeanPrice"], "fund")
        self.assertEqual(why["BANK"]["currentRatio"], "bank")
        self.assertEqual(why["BANK"]["grossMargins"], "bank")
        self.assertEqual(why["NOPAY"]["dividendRate"], "no_dividend")
        self.assertEqual(why["NOPAY"]["trailingAnnualDividendRate"], "no_dividend")   # Yahoo's 0 isn't shown as $0.00
        self.assertEqual(why["NOPAY"]["payoutRatio"], "no_dividend")
        self.assertEqual(why["NOPAY"]["lastSplitFactor"], "no_splits")
        self.assertEqual(why["NOPAY"]["exDividendDate"], "no_dividend")        # no stray calendar date
        self.assertEqual(why["FAIL"]["bid"], "unreachable")                    # not "market closed"
        self.assertEqual(why["IPO"]["perf_1y"], "history")
        self.assertEqual(self.rows["IPO"]["note"]["perf_1y"], "Listed 2026-06-22")
        self.assertIn("perf_1m", self.rows["IPO"]["data"])
        self.assertEqual(why["NOCOV"]["targetMeanPrice"], "no_analysts")
        self.assertEqual(why["NOCOV"]["recommendationKey"], "no_analysts")

    def test_funds(self):
        d = self.rows["FUND"]["data"]
        self.assertEqual((d["sector"], d["industry"]), ("Large Blend", "Example Funds"))
        self.assertEqual(d["yield_calc"], 0.0125)                           # Yahoo's fund yield
        self.assertEqual(d["beta"], 1.0)                                    # beta3Year

    def test_failed_source_doesnt_blank_the_row(self):
        r = self.rows["FAIL"]
        self.assertIsNone(r["error"])
        self.assertIn("currentPrice", r["data"])
        self.assertIn("totalRevenue", r["data"])
        self.assertEqual(r["why"]["sector"], "unreachable")
        self.assertEqual(r["why"]["overallRisk"], "unreachable")

    def test_unknown_ticker(self):
        rows, _ = fresh_service().get(["NOSUCH"], ["currentPrice", "sector"])
        self.assertEqual(rows[0]["error"], "Yahoo has no data for this ticker")


class FetchOnlyWhatsNeeded(unittest.TestCase):
    def calls(self, sym):
        return {k[1] for k in fake_yf.CALLS if k[0] == sym}

    def test_price_tab_skips_statements_and_analysts(self):
        fresh_service().get(["THIN"], ["currentPrice", "fiftyDayAverage", "perf_1y"])
        self.assertEqual(self.calls("THIN"), {"info", "history:1d"})

    def test_valuation_needs_statements_only(self):
        fresh_service().get(["THIN"], ["trailingPE", "priceToSalesTrailing12Months"])
        self.assertEqual(self.calls("THIN"), {"info", "history:1d", "statements", "shares"})

    def test_complete_yahoo_data_needs_nothing_else(self):
        keys = [k for k in KEYS if k not in ("preMarketPrice", "postMarketPrice")]
        fresh_service().get(["FULL"], keys)
        self.assertEqual(self.calls("FULL"), {"info", "history:1d"})
        self.assertNotIn(("SPY", "history:1d"), fake_yf.CALLS)              # Yahoo had beta

    def test_cache_is_reused(self):
        svc = fresh_service()
        svc.get(["THIN"], ["trailingPE"])
        before = sum(fake_yf.CALLS.values())
        svc.get(["THIN"], ["trailingPE", "currentRatio"])
        self.assertEqual(sum(fake_yf.CALLS.values()), before)


class LimiterAndCaches(unittest.TestCase):
    def test_lru(self):
        c = common.LRU(2)
        c["a"], c["b"] = 1, 2
        c.get("a")                         # a is now the most recent
        c["c"] = 3
        self.assertIn("a", c)
        self.assertNotIn("b", c)
        self.assertEqual(len(c), 2)

    def test_rate_limiter(self):
        rl = common.RateLimiter(2)
        self.assertTrue(rl.try_acquire())
        self.assertTrue(rl.try_acquire())
        self.assertFalse(rl.try_acquire())
        self.assertFalse(rl.acquire(timeout=0.3))
        rl2 = common.RateLimiter(5)
        rl2.pause(30)
        self.assertFalse(rl2.try_acquire())

    def test_yahoo_429_pauses_every_call(self):
        fresh_service()
        with self.assertRaises(RuntimeError):
            common.yahoo_call(lambda: (_ for _ in ()).throw(RuntimeError("429 Client Error: Too Many Requests")))
        self.assertTrue(common.YAHOO.paused())
        with self.assertRaises(common.YahooBusy):
            common.yahoo_call(lambda: 1, timeout=0.3)
        common.YAHOO.paused_until = 0.0


class EtfPriceColumns(unittest.TestCase):
    def test_add_prices(self):
        import app
        out = {"rows": [{"symbol": "X", "quote": {"shares": 1000}, "last_close": 10.0, "last_date": "2026-10-21"},
                        {"symbol": "Y", "quote": {"shares": None}, "last_close": 5.0},
                        {"symbol": "Z", "quote": {"shares": 500}}]}
        app.add_prices(out, {"X": {"price": 12.0}}, True)
        x, y, z = out["rows"]
        self.assertEqual((x["price"], x["price_src"], x["avail_value"]), (12.0, "live", 12000.0))
        self.assertEqual((y["price"], y["price_src"], y["avail_value"]), (5.0, "close", None))
        self.assertEqual((z["price"], z["avail_value"]), (None, None))
        app.add_prices(out, {"X": {"price": 12.0}}, False)
        self.assertIsNone(out["rows"][0]["price"])
        self.assertFalse(out["prices_enabled"])


if __name__ == "__main__":
    unittest.main()
