"""EtfService end to end, with the network fetchers replaced by fixtures."""
import json
import sys
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"

import etf  # noqa: E402
from test_matching import load_foreign, load_us  # noqa: E402
from offline import offline_ids  # noqa: E402


class FakeBorrow:
    def __init__(self, table):
        self.table = table

    def ensure_fresh(self):
        pass

    def snapshot(self):
        return self.table, [], "2026.10.21 09:30:01", "", False


class FakeForeign:
    def __init__(self, tables):
        self.tables = tables

    def ensure_fresh(self):
        pass

    def snapshot(self):
        return self.tables, False, ""


IDX = pd.bdate_range("2019-01-02", "2026-10-21")
MKT = np.random.default_rng(3).normal(0.0004, 0.01, len(IDX))
SPY = pd.Series(100 * np.cumprod(1 + MKT), index=IDX)


def nport(name):
    f = etf.parse_nport((FIX / name).read_bytes())
    return {"source": "SEC N-PORT", "as_of": f["as_of"], "url": "https://www.sec.gov/x/", "complete": True,
            "holdings": f["holdings"], "series": f["series"], "net_assets": f["net_assets"], "notes": []}


HIST = {
    "SPY": {"close": SPY},
    "TSLL": {"close": pd.Series(20 * np.cumprod(1 + 3 * MKT - 0.001), index=IDX)[IDX >= "2022-08-09"],
             "name": "Test Daily TSLA Bull 2X Shares", "quote_type": "ETF"},
    "SOXL": {"close": pd.Series(20 * np.cumprod(1 + 4 * MKT), index=IDX), "name": "Semis 3X", "quote_type": "ETF"},
    "TSLA": {"close": pd.Series(20 * np.cumprod(1 + 2 * MKT), index=IDX), "name": "Tesla", "quote_type": "EQUITY"},
}
HOLD = {"TSLL": nport("nport_2x_single.xml"), "SOXL": nport("nport_3x_index.xml"),
        "SOXX": nport("nport_proxy_soxx.xml")}
# RYLD holds one fund, RSSL, which holds the stocks
HOLD["RSSL"] = dict(nport("nport_proxy_soxx.xml"), series="Global X Russell 2000 ETF")
HOLD["RYLD"] = {"source": "SEC N-PORT", "as_of": "2026-07-31", "url": None, "complete": True, "notes": [],
                "series": "Global X Russell 2000 Covered Call ETF", "net_assets": 1e9,
                "holdings": [{"name": "Global X Russell 2000 ETF", "title": "Global X Russell 2000 ETF", "pct": 1.0,
                              "isin": "US37960A4537", "cusip": "37960A453", "cat": "EC", "issuer_cat": "RF",
                              "currency": "USD", "country": "US"}]}


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = (etf.fetch_history, etf.fetch_holdings, etf.fetch_rfr)
        etf.fetch_history = lambda yf, sym: HIST.get(sym, {"_error": "no history"})
        etf.fetch_holdings = lambda yf, sym: HOLD.get(sym, {"single_stock": True})
        etf.fetch_rfr = lambda yf: {"rate": pd.Series(4.0, index=IDX), "source": "test"}
        table = load_us()[0]
        table["SOXL"] = {"name": "Soxl", "fee": 1.5, "rebate": 3.0, "shares": 1000, "plus": False, "isin": None}
        cls.svc = etf.EtfService(object(), FakeBorrow(table), FakeForeign(load_foreign()), ids=offline_ids())

    @classmethod
    def tearDownClass(cls):
        etf.fetch_history, etf.fetch_holdings, etf.fetch_rfr = cls.saved

    def test_report(self):
        out = self.svc.report(["TSLL", "SOXL", "TSLA"], gc=0.25, cash=0.0, start=date(2023, 5, 1))
        json.dumps(out)                                           # what the API sends must serialize
        self.assertEqual(out["pending"], 0)
        self.assertEqual(out["rfr"]["state"], "ok")
        rows = {r["symbol"]: r for r in out["rows"]}

        tsll = rows["TSLL"]
        self.assertAlmostEqual(tsll["last_close"], float(HIST["TSLL"]["close"].iloc[-1]))
        self.assertEqual(tsll["last_date"], "2026-10-21")
        self.assertAlmostEqual(tsll["holdings"]["theo"]["fee"], 0.60)
        self.assertLessEqual(len(tsll["holdings"]["theo"]["top"]), etf.TOP_HOLDINGS_SHOWN)
        self.assertNotIn("rows", tsll["holdings"])                # full list only from the holdings endpoint
        hist = tsll["history"]
        self.assertAlmostEqual(hist["beta"]["beta"], 3.0, delta=0.05)
        self.assertEqual(set(hist["windows"]), {"inception", "3y", "1y", "custom"})
        self.assertEqual(hist["windows"]["custom"]["start"], "2023-05-01")
        self.assertIn(hist["windows"]["custom"]["verdict"], ("go", "meh", "no"))
        self.assertNotIn("error", hist["windows"]["3y"])

        soxl = rows["SOXL"]["holdings"]["theo"]
        self.assertAlmostEqual(soxl["fee"], 3.0 * (0.60 * 0.28 + 0.395 * 3.0) / 0.995)   # via the SOXX proxy
        self.assertTrue(rows["TSLA"]["holdings"]["single_stock"])
        self.assertIn("custom", rows["TSLA"]["history"]["windows"])

    def test_holdings_detail(self):
        d = self.svc.holdings_detail("TSLL", gc=0.25, cash=0.0)
        json.dumps(d)
        self.assertEqual(d["symbol"], "TSLL")
        self.assertEqual(len(d["rows"]), 6)
        self.assertTrue(all("how" in r and "detail" in r and "exposure" in r for r in d["rows"]))
        self.assertTrue(self.svc.holdings_detail("TSLA", 0.25)["single_stock"])

    def test_fund_held_by_a_fund(self):
        d = self.svc.holdings_detail("RYLD", gc=0.25, cash=0.0)
        row = d["rows"][0]
        rssl = etf.price_holdings(HOLD["RSSL"], self.svc.match_index(load_us()[0]), 0.25, 0.0)["fee"]
        self.assertEqual(row["how"], "Look-through: RSSL's holdings")
        self.assertAlmostEqual(d["theo"]["fee"], rssl)
        self.assertLess(d["theo"]["fee"], 5)                      # not RSSL's own 40% fee
        self.assertEqual(d["theo"]["warnings"], [])
        # the Stocks page's look-through goes through RSSL to its stocks
        w = dict(self.svc.lookthrough_weights("RYLD")["weights"])
        self.assertEqual(set(w), {"NVDA", "DUPE"})
        self.assertAlmostEqual(w["NVDA"], 0.60 / 0.995, places=6)

    def test_no_start_means_no_custom_window(self):
        out = self.svc.report(["TSLL"], gc=0.25)
        self.assertNotIn("custom", out["rows"][0]["history"]["windows"])


if __name__ == "__main__":
    unittest.main()
