"""Windows, custom start dates, beta, the risk-free rate and the beta-adjusted break-evens."""
import sys
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import etf  # noqa: E402

IDX = pd.bdate_range("2018-01-02", "2026-10-21")
RNG = np.random.default_rng(42)
MKT = RNG.normal(0.0004, 0.01, len(IDX))
SPY = pd.Series(100 * np.cumprod(1 + MKT), index=IDX)
RATE = pd.Series(4.0, index=IDX)


def etf_series(beta, start=None, noise=0.0005, seed=1):
    r = np.random.default_rng(seed)
    s = pd.Series(50 * np.cumprod(1 + beta * MKT + r.normal(0, noise, len(IDX))), index=IDX)
    return s[s.index >= start] if start else s


def rfr(start, end):
    return etf.rfr_over(RATE, start, end)


class BetaAdjustedTests(unittest.TestCase):
    def test_worked_example(self):
        # SPY +10%/yr, RFR 4%: shorting SPY costs -6%/yr per -1.00 beta.
        # ETF beta 0.2, +5%/yr, fee 0: -1%/yr for -0.2 beta, x5 -> -5%/yr per -1.00 beta.
        out = etf.beta_adjusted(r_x=5, r_spy=10, rfr=4, beta=0.2, fee=0)
        self.assertAlmostEqual(out["spy_hedge_cost"], -6)
        self.assertAlmostEqual(out["etf_hedge_cost"], -1)
        self.assertAlmostEqual(out["leverage"], 5)
        self.assertAlmostEqual(out["hedge_cost_beta"], -5)
        self.assertAlmostEqual(out["be_beta"], 0.2)                 # 0.2 x 6% - 1%
        self.assertAlmostEqual(out["be_cash"], -1)
        self.assertAlmostEqual(out["headroom_beta"], 1.0)           # 1 point better than SPY
        self.assertEqual(out["verdict"], "go")

    def test_break_even_makes_costs_equal(self):
        out = etf.beta_adjusted(5, 10, 4, 0.2, fee=0.2)
        self.assertAlmostEqual(out["hedge_cost_beta"], out["spy_hedge_cost"])    # (-1 - 0.2) x 5 = -6
        self.assertEqual(out["verdict"], "go")
        self.assertEqual(etf.beta_adjusted(5, 10, 4, 0.2, fee=0.1)["verdict"], "go")
        self.assertEqual(etf.beta_adjusted(5, 10, 4, 0.2, fee=0.3)["verdict"], "no")

    def test_seven_percent_variant_never_worthwhile(self):
        out = etf.beta_adjusted(7, 10, 4, 0.2, fee=0)
        self.assertAlmostEqual(out["etf_hedge_cost"], -3)
        self.assertAlmostEqual(out["hedge_cost_beta"], -15)
        self.assertAlmostEqual(out["be_beta"], -1.8)
        self.assertEqual(out["verdict"], "no")

    def test_profits_but_costlier_hedge(self):
        # SPY lagged cash: the short profits (fee under RFR - R_X) but costs more per beta than SPY.
        out = etf.beta_adjusted(r_x=0, r_spy=2, rfr=4, beta=1.0, fee=3)
        self.assertAlmostEqual(out["be_cash"], 4)
        self.assertAlmostEqual(out["be_beta"], 2)
        self.assertEqual(out["verdict"], "meh")

    def test_low_or_missing_beta(self):
        for beta in (None, 0.05, -0.4):
            out = etf.beta_adjusted(5, 10, 4, beta, fee=0)
            self.assertEqual(out["verdict"], "low")
            self.assertIsNone(out["be_beta"])
            self.assertIsNone(out["headroom"])
            self.assertAlmostEqual(out["be_cash"], -1)              # still shown

    def test_no_quoted_fee(self):
        out = etf.beta_adjusted(5, 10, 4, 0.2, fee=None)
        self.assertIsNone(out["verdict"])
        self.assertAlmostEqual(out["be_beta"], 0.2)


class BetaTests(unittest.TestCase):
    def test_low_and_levered_betas(self):
        self.assertAlmostEqual(etf.compute_beta(etf_series(0.2), SPY)["beta"], 0.2, delta=0.02)
        b = etf.compute_beta(etf_series(3.0), SPY)
        self.assertAlmostEqual(b["beta"], 3.0, delta=0.02)
        self.assertGreater(b["corr"], 0.99)
        self.assertFalse(b["partial"])
        self.assertGreater(b["days"], 240)                          # one year of daily returns

    def test_uses_only_the_last_year(self):
        x = etf_series(1.0)
        x = x.where(x.index >= "2025-06-01", x * 0 + 50)            # flat before, so only recent data moves
        self.assertAlmostEqual(etf.compute_beta(x, SPY)["beta"], 1.0, delta=0.05)

    def test_short_history(self):
        b = etf.compute_beta(etf_series(1.0, start="2026-06-15"), SPY)
        self.assertTrue(b["partial"])
        self.assertTrue(etf.BETA_MIN_DAYS <= b["days"] < etf.BETA_FULL_DAYS)
        self.assertIsNone(etf.compute_beta(etf_series(1.0, start="2026-09-01"), SPY)["beta"])


class RiskFreeTests(unittest.TestCase):
    def test_constant_rate_compounds_daily_actual_360(self):
        rf = etf.rfr_over(RATE, pd.Timestamp("2024-01-02"), pd.Timestamp("2026-01-02"))
        self.assertAlmostEqual(rf["ann"], (1 + 0.04 / 360) ** 365.25 - 1, places=9)
        self.assertAlmostEqual(rf["total"], (1 + 0.04 / 360) ** 731 - 1, places=9)

    def test_rate_change_and_missing_data(self):
        rate = pd.Series([2.0, 6.0], index=pd.to_datetime(["2024-01-01", "2024-07-01"]))
        rate = rate.reindex(pd.bdate_range("2024-01-01", "2025-01-01")).ffill()
        rf = etf.rfr_over(rate, pd.Timestamp("2024-01-01"), pd.Timestamp("2025-01-01"))
        self.assertTrue(0.039 < rf["ann"] < 0.042)
        self.assertIsNone(etf.rfr_over(rate, pd.Timestamp("2020-01-01"), pd.Timestamp("2021-01-01")))
        self.assertIsNone(etf.rfr_over(None, pd.Timestamp("2024-01-01"), pd.Timestamp("2025-01-01")))

    def test_fred_csv(self):
        s = etf.parse_fred_csv("observation_date,DTB3\n2024-01-02,5.2\n2024-01-03,.\n2024-01-04,5.1\n")
        self.assertEqual(list(s.values), [5.2, 5.1])


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.young = etf_series(1.5, start="2022-08-09")
        self.beta1y = etf.compute_beta(self.young, SPY)

    def test_parse_start_date(self):
        today = date(2026, 10, 22)
        self.assertEqual(etf.parse_start_date("", today), (None, None))
        self.assertEqual(etf.parse_start_date("2023-05-01", today), (date(2023, 5, 1), None))
        for bad in ("2024-02-30", "2024/01/01", "May 1", "2099-01-01", "1850-01-01"):
            d, err = etf.parse_start_date(bad, today)
            self.assertIsNone(d)
            self.assertTrue(err, bad)

    def test_snaps_to_first_trading_day(self):
        w = etf.analyze_window(self.young, SPY, start=date(2023, 4, 29), rfr=rfr, beta1y=self.beta1y, fee=1.0)
        self.assertEqual(w["start"], "2023-05-01")                  # Saturday -> Monday
        self.assertFalse(w["clamped"])
        self.assertEqual(w["requested_start"], "2023-04-29")

    def test_clamps_to_inception(self):
        w = etf.analyze_window(self.young, SPY, start=date(2020, 1, 1), rfr=rfr, beta1y=self.beta1y, fee=1.0)
        self.assertEqual(w["start"], "2022-08-09")
        self.assertTrue(w["clamped"])

    def test_too_few_days_or_after_last_price(self):
        w = etf.analyze_window(self.young, SPY, start=date(2026, 10, 1))
        self.assertIn("Pick an earlier date", w["error"])
        w = etf.analyze_window(self.young, SPY, start=date(2026, 10, 22))
        self.assertIn("no prices after", w["error"])

    def test_presets_unchanged(self):
        self.assertIsNone(etf.analyze_window(self.young, SPY, years=5))     # younger than 5 years
        w = etf.analyze_window(self.young, SPY, years=1)
        self.assertAlmostEqual(w["years"], 1.0, delta=0.01)
        self.assertIsNone(w["rfr_ann"])                              # no rate given -> no beta-adjusted figures
        self.assertNotIn("be_beta", w)

    def test_window_uses_the_formula(self):
        w = etf.analyze_window(self.young, SPY, start=date(2023, 5, 1), rfr=rfr, beta1y=self.beta1y, fee=1.0)
        r_x, r_spy, rf = w["etf_ann"] * 100, w["spy_ann"] * 100, w["rfr_ann"] * 100
        beta = w["beta_used"]                                       # the window's own beta
        self.assertAlmostEqual(beta, w["beta_window"]["beta"])
        self.assertAlmostEqual(w["be_beta"], beta * (r_spy - rf) - (r_x - rf))
        self.assertAlmostEqual(w["be_cash"], rf - r_x)
        self.assertAlmostEqual(w["hedge_cost_beta"], (rf - r_x - 1.0) / beta)
        self.assertAlmostEqual(w["rfr_ann"], (1 + 0.04 / 360) ** 365.25 - 1, places=9)
        for key in ("be_profit", "be_spy", "be_hedged"):            # previous method kept
            self.assertIn(key, w)


if __name__ == "__main__":
    unittest.main()
