"""Window beta vs last year's beta, weekly betas for mostly non-US funds, and the
cash rate on short proceeds (T-bill rate minus a spread, never below zero)."""
import sys
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import etf  # noqa: E402
from test_matching import load_foreign, load_us  # noqa: E402
from offline import offline_ids  # noqa: E402

IDX = pd.bdate_range("2019-01-02", "2026-10-21")
MKT = np.random.default_rng(11).normal(0.0004, 0.01, len(IDX))
SPY = pd.Series(100 * np.cumprod(1 + MKT), index=IDX)
LAST = IDX[-1]


def prices(daily_returns, start=None):
    s = pd.Series(30 * np.cumprod(1 + daily_returns), index=IDX)
    return s[s.index >= start] if start else s


def regime_change():
    """Beta 2.5 until the last year, then 1.8."""
    noise = np.random.default_rng(5).normal(0, 0.002, len(IDX))
    beta = np.where(IDX >= LAST - pd.DateOffset(years=1), 1.8, 2.5)
    return prices(beta * MKT + noise)


def const_rate(pct):
    return lambda start, end: etf.rfr_over(pd.Series(pct, index=IDX), start, end)


class WindowBetaTests(unittest.TestCase):
    def setUp(self):
        self.x = regime_change()
        self.b1y = etf.compute_beta(self.x, SPY)

    def test_window_beta_vs_last_year(self):
        self.assertAlmostEqual(self.b1y["beta"], 1.8, delta=0.05)
        w = etf.analyze_window(self.x, SPY, start=(LAST - pd.DateOffset(years=5)).date(),
                               rfr=const_rate(4.0), beta1y=self.b1y, fee=1.0)
        self.assertAlmostEqual(w["beta_window"]["beta"], 2.36, delta=0.12)       # mostly the 2.5 years
        self.assertEqual(w["beta_used"], w["beta_window"]["beta"])
        self.assertTrue(w["beta_changed"])                                       # 2.36 vs 1.8 is >25% apart
        self.assertEqual(w["beta_window"]["freq"], "daily")
        self.assertGreater(w["beta_window"]["days"], 1200)
        self.assertGreater(w["beta_window"]["corr"], 0.9)
        # the break-even uses the window's beta
        r_x, r_spy, r = w["etf_ann"] * 100, w["spy_ann"] * 100, w["rfr_ann"] * 100
        self.assertAlmostEqual(w["be_beta"], w["beta_used"] * (r_spy - r) - (r_x - r))

    def test_one_year_window_uses_the_same_beta(self):
        w = etf.analyze_window(self.x, SPY, years=1, rfr=const_rate(4.0), beta1y=self.b1y, fee=1.0)
        self.assertEqual(w["beta_used"], self.b1y["beta"])
        self.assertEqual(w["beta_window"]["beta"], self.b1y["beta"])
        self.assertFalse(w["beta_changed"])

    def test_short_window_falls_back_to_last_year(self):
        start = IDX[-41].date()                                                  # 40 daily returns
        w = etf.analyze_window(self.x, SPY, start=start, rfr=const_rate(4.0), beta1y=self.b1y, fee=1.0)
        self.assertNotIn("error", w)
        self.assertTrue(w["beta_window"]["fallback"])
        self.assertEqual(w["beta_window"]["note"], "Window too short for its own beta; using the last year's")
        self.assertEqual(w["beta_used"], self.b1y["beta"])
        self.assertFalse(w["beta_changed"])

    def test_low_beta_rule_uses_window_beta(self):
        flat = prices(0.05 * MKT + np.random.default_rng(2).normal(0, 0.001, len(IDX)))
        w = etf.analyze_window(flat, SPY, years=3, rfr=const_rate(4.0),
                               beta1y={"beta": 1.0, "corr": 0.9, "days": 250}, fee=1.0)
        self.assertLess(w["beta_used"], etf.MIN_HEDGE_BETA)
        self.assertEqual(w["verdict"], "low")


class WeeklyBetaTests(unittest.TestCase):
    def test_lagged_fund(self):
        # follows SPY one trading day late, like a fund whose market closed before the US move
        lagged = np.concatenate([[0.0], MKT[:-1]]) + np.random.default_rng(8).normal(0, 0.001, len(IDX))
        x = prices(lagged)
        daily = etf.beta_from(etf.beta_returns(x, SPY, weekly=False))
        weekly = etf.beta_from(etf.beta_returns(x, SPY, weekly=True), weekly=True)
        self.assertLess(abs(daily["beta"]), 0.1)                                 # daily misses the move
        self.assertGreater(weekly["beta"], 0.7)                                  # weekly catches most of it
        self.assertEqual(weekly["freq"], "weekly")
        self.assertFalse(weekly["partial"])
        self.assertTrue(etf.compute_beta(x, SPY, weekly=True)["freq"] == "weekly")

    def test_weekly_minimums(self):
        x = prices(MKT, start=LAST - pd.DateOffset(weeks=20))
        self.assertIsNone(etf.compute_beta(x, SPY, weekly=True)["beta"])          # under 26 weekly returns

    def test_foreign_share_from_holdings(self):
        idx = etf.MatchIndex(load_us()[0], load_foreign())
        mixed = etf.parse_nport((ROOT / "tests/fixtures/nport_mixed.xml").read_bytes())
        s = etf.price_holdings(dict(mixed, source="SEC N-PORT"), idx, 0.25, 0.0)
        self.assertGreater(s["foreign_share"], 0.05)                             # Toyota, Tencent, Shell
        self.assertLess(s["foreign_share"], etf.WEEKLY_ABOVE)
        rows = {r["name"]: r for r in s["rows"]}
        self.assertEqual(rows["Toyota Motor Corp"]["non_us"], 1.0)
        self.assertEqual(rows["Apple Inc"]["non_us"], 0.0)
        self.assertGreater(rows["S&P 500 Index Swap"]["non_us"], 0)              # via the fund's own stocks
        japan = {"source": "SEC N-PORT", "net_assets": 1e8, "holdings": [
            {"name": "Toyota Motor Corp", "isin": "JP3633400001", "pct": 0.6, "cat": "EC", "country": "JP"},
            {"name": "Softbank Group Corp", "isin": "JP3436100006", "pct": 0.3, "cat": "EC", "country": "JP"},
            {"name": "Apple Inc", "isin": "US0378331005", "pct": 0.1, "cat": "EC", "country": "US"}]}
        self.assertAlmostEqual(etf.price_holdings(japan, idx, 0.25, 0.0)["foreign_share"], 0.9)


class CashRateTests(unittest.TestCase):
    def test_worked_example_with_spread(self):
        out = etf.beta_adjusted(r_x=5, r_spy=10, rfr=3, beta=0.2, fee=0)          # cash rate 4% - 1%
        self.assertAlmostEqual(out["spy_hedge_cost"], -7)
        self.assertAlmostEqual(out["hedge_cost_beta"], -10)                       # (3 - 5) / 0.2
        self.assertAlmostEqual(out["be_beta"], -0.6)                              # 0.2 x 7 - 2
        self.assertEqual(out["verdict"], "no")
        no_spread = etf.beta_adjusted(r_x=5, r_spy=10, rfr=4, beta=0.2, fee=0)
        self.assertAlmostEqual(no_spread["be_beta"], 0.2)
        self.assertEqual(no_spread["verdict"], "go")

    def test_half_beta_example(self):
        out = etf.beta_adjusted(r_x=5, r_spy=10, rfr=3, beta=0.5, fee=1)
        self.assertAlmostEqual(out["hedge_cost_beta"], -6)                        # (3 - 5 - 1) / 0.5

    def test_spread_on_constant_rate(self):
        rf = etf.rfr_over(pd.Series(4.0, index=IDX), pd.Timestamp("2024-01-02"), pd.Timestamp("2026-01-02"), 1.0)
        self.assertAlmostEqual(rf["ann"], (1 + 0.03 / 360) ** 365.25 - 1, places=9)
        self.assertAlmostEqual(rf["tbill_ann"], (1 + 0.04 / 360) ** 365.25 - 1, places=9)
        self.assertEqual(rf["spread"], 1.0)
        self.assertEqual(rf["floor_days"], 0)

    def test_zero_floor(self):
        start, mid, end = pd.Timestamp("2025-01-01"), pd.Timestamp("2025-07-01"), pd.Timestamp("2026-01-01")
        days = pd.date_range(start, end, freq="D")
        rate = pd.Series(np.where(days < mid, 0.5, 5.0), index=days)
        rf = etf.rfr_over(rate, start, end, spread=1.0)
        low_days, high_days = (mid - start).days, (end - mid).days
        self.assertEqual(rf["floor_days"], low_days)
        self.assertAlmostEqual(rf["total"], (1 + 0.04 / 360) ** high_days - 1, places=9)   # 0% then 4%
        self.assertGreater(rf["total"], 0)
        self.assertAlmostEqual(rf["tbill_total"], (1 + 0.005 / 360) ** low_days * (1 + 0.05 / 360) ** high_days - 1,
                               places=9)

    def test_window_uses_cash_rate(self):
        x = regime_change()
        b1y = etf.compute_beta(x, SPY)
        fn = lambda s, e: etf.rfr_over(pd.Series(4.0, index=IDX), s, e, 1.0)   # noqa: E731
        w = etf.analyze_window(x, SPY, years=3, rfr=fn, beta1y=b1y, fee=1.0)
        r = w["rfr_ann"] * 100
        self.assertLess(w["rfr_ann"], w["tbill_ann"])
        self.assertEqual(w["spread"], 1.0)
        self.assertAlmostEqual(w["spy_hedge_cost"], r - w["spy_ann"] * 100)
        self.assertAlmostEqual(w["be_cash"], r - w["etf_ann"] * 100)
        for key in ("be_profit", "be_spy", "be_hedged"):                         # previous method: no interest
            self.assertAlmostEqual(w[key], etf.analyze_window(x, SPY, years=3, beta1y=b1y, fee=1.0)[key])

    def test_spread_setting_is_validated(self):
        import app
        self.assertEqual(app.rate_param({"spread": ["2.5"]}, "spread", 1.0, hi=10), 2.5)
        self.assertEqual(app.rate_param({"spread": ["12"]}, "spread", 1.0, hi=10), 1.0)
        self.assertEqual(app.rate_param({"spread": ["-1"]}, "spread", 1.0, hi=10), 1.0)
        self.assertEqual(app.rate_param({}, "spread", 1.0, hi=10), 1.0)


class ServiceTests(unittest.TestCase):
    def test_weekly_for_mostly_foreign_fund(self):
        from test_service import FakeBorrow, FakeForeign
        lagged = np.concatenate([[0.0], MKT[:-1]])
        hist = {"SPY": {"close": SPY},
                "JPN": {"close": prices(lagged), "name": "Japan Fund", "quote_type": "ETF"},
                "USA": {"close": prices(MKT * 1.1), "name": "US Fund", "quote_type": "ETF"}}
        jp = [{"name": "Toyota Motor Corp", "isin": "JP3633400001", "pct": 0.7, "cat": "EC", "country": "JP"},
              {"name": "Apple Inc", "isin": "US0378331005", "pct": 0.3, "cat": "EC", "country": "US"}]
        us = [{"name": "Apple Inc", "isin": "US0378331005", "pct": 1.0, "cat": "EC", "country": "US"}]
        hold = {s: {"source": "SEC N-PORT", "as_of": "2026-06-30", "url": None, "complete": True, "series": n,
                    "net_assets": 1e8, "holdings": h, "notes": []}
                for s, n, h in (("JPN", "Japan Fund", jp), ("USA", "US Fund", us))}
        saved = (etf.fetch_history, etf.fetch_holdings, etf.fetch_rfr)
        try:
            etf.fetch_history = lambda yf, sym: hist[sym]
            etf.fetch_holdings = lambda yf, sym: hold[sym]
            etf.fetch_rfr = lambda yf: {"rate": pd.Series(4.0, index=IDX), "source": "test"}
            svc = etf.EtfService(object(), FakeBorrow(load_us()[0]), FakeForeign(load_foreign()), ids=offline_ids())
            out = svc.report(["JPN", "USA"], gc=0.25, cash=0.0, spread=1.0)
        finally:
            etf.fetch_history, etf.fetch_holdings, etf.fetch_rfr = saved
        rows = {r["symbol"]: r for r in out["rows"]}
        self.assertEqual(out["spread"], 1.0)
        jph, ush = rows["JPN"]["history"], rows["USA"]["history"]
        self.assertTrue(jph["weekly"])
        self.assertAlmostEqual(jph["foreign_share"], 0.7)
        self.assertEqual(jph["beta"]["freq"], "weekly")
        self.assertEqual(jph["windows"]["3y"]["beta_window"]["freq"], "weekly")
        self.assertGreater(jph["windows"]["3y"]["beta_used"], 0.7)
        self.assertFalse(ush["weekly"])
        self.assertEqual(ush["beta"]["freq"], "daily")
        self.assertAlmostEqual(ush["beta"]["beta"], 1.1, delta=0.05)
        w = ush["windows"]["inception"]
        self.assertLess(w["rfr_ann"], w["tbill_ann"])                           # the spread reached the windows


if __name__ == "__main__":
    unittest.main()
