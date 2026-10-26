"""N-PORT parsing and the theoretical fee from holdings."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"

import app  # noqa: E402
import etf  # noqa: E402
from test_matching import load_foreign, load_us  # noqa: E402

GC = 0.25


def filing(name):
    f = etf.parse_nport((FIX / name).read_bytes())
    return dict(f, source="SEC N-PORT")


def by_name(summary):
    return {r["name"]: r for r in summary["rows"]}


class ParseNportTests(unittest.TestCase):
    def test_fund_fields(self):
        f = etf.parse_nport((FIX / "nport_2x_single.xml").read_bytes())
        self.assertEqual(f["as_of"], "2026-07-31")
        self.assertEqual(f["series"], "Test Daily TSLA Bull 2X Shares")
        self.assertEqual(f["net_assets"], 1_000_000_000)
        self.assertEqual(len(f["holdings"]), 5)

    def test_swap_uses_its_own_ids_not_the_reference_stock(self):
        f = etf.parse_nport((FIX / "nport_2x_single.xml").read_bytes())
        swap = f["holdings"][1]
        self.assertIsNone(swap["isin"])                      # old parser picked up TSLA's ISIN here
        self.assertEqual(swap["pct"], -0.005)                # negative value kept, not dropped
        d = swap["deriv"]
        self.assertEqual((d["kind"], d["notional"], d["direction"]), ("swap", 1_000_000_000, "long"))
        self.assertEqual(d["ref"]["isin"], "US88160R1014")
        self.assertEqual(d["ref"]["ticker"], "TSLA")

    def test_option_and_conditional_categories(self):
        f = etf.parse_nport((FIX / "nport_mixed.xml").read_bytes())
        h = {x["title"]: x for x in f["holdings"]}
        opt = h["AAPL Call 250 01/15/2027"]["deriv"]
        self.assertEqual((opt["kind"], opt["direction"], opt["delta"], opt["shares"]), ("option", "short", 0.4, 100))
        mmf = h["Dreyfus Treasury Securities Cash Management"]
        self.assertEqual((mmf["cat"], mmf["issuer_cat"]), ("STIV", "OTHER"))
        fut = h["E-mini S&P 500 Future Dec26"]["deriv"]
        self.assertEqual((fut["kind"], fut["direction"], fut["ref"]["type"]), ("future", "long", "index"))

    def test_basket_components(self):
        f = etf.parse_nport((FIX / "nport_basket.xml").read_bytes())
        comps = f["holdings"][0]["deriv"]["ref"]["components"]
        self.assertEqual([(c["name"], c.get("isin"), c.get("ticker"), c["amount"]) for c in comps],
                         [("NVIDIA Corp", "US67066G1040", None, 150_000_000),
                          ("Tesla Inc", None, "TSLA", 50_000_000)])


class PriceHoldingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = etf.MatchIndex(load_us()[0], load_foreign())

    def price(self, name, cash=0.0, proxy=None):
        return etf.price_holdings(filing(name), self.idx, GC, cash, proxy=proxy)

    def test_2x_single_stock_is_twice_the_stock_fee(self):
        s = self.price("nport_2x_single.xml")
        self.assertAlmostEqual(s["fee"], 2 * 0.30, places=9)          # TSLA fee 0.30, 200% exposure
        self.assertAlmostEqual(s["gross_long_equity"], 2.0, places=9)
        self.assertAlmostEqual(s["coverage"], 1.0)
        hows = [r["how"] for r in s["rows"]]
        self.assertEqual(hows.count("Look-through: TSLA"), 2)
        self.assertNotIn("Assumed (unmatched)", hows)
        self.assertAlmostEqual(s["cash_weight"], 0.45 + 0.30 + 0.052, places=9)

    def test_no_renormalizing(self):
        s = self.price("nport_2x_single.xml")
        tsla = by_name(s)["Tesla Inc"]
        self.assertAlmostEqual(tsla["exposure"], 0.20)            # as reported, not 20 / 95.3
        self.assertAlmostEqual(tsla["contrib"], 0.20 * 0.30)

    def test_cash_fee_setting(self):
        base = self.price("nport_2x_single.xml")
        more = self.price("nport_2x_single.xml", cash=0.1)
        self.assertAlmostEqual(more["fee"] - base["fee"], 0.1 * base["cash_weight"], places=9)

    def test_every_holding_type_in_mixed_fund(self):
        s = self.price("nport_mixed.xml")
        rows = by_name(s)
        want = {
            "Apple Inc": "IBKR (ISIN suffix + name)",
            "Microsoft Corp": "IBKR (ISIN suffix + name)",
            "Berkshire Hathaway Inc": "IBKR (ticker)",
            "Toyota Motor Corp": "IBKR (Japan file)",
            "Tencent Holdings Ltd": "IBKR (Hong Kong file)",
            "Shell PLC": "IBKR (UK file)",
            "Exxon Mobil Corporation": "IBKR (name match)",
            "SPDR S&P 500 ETF Trust": "IBKR fund's own fee (SPY)",
            "Abcd Corp": "IBKR (ticker)",
            "Obscure Holdings Inc": "Assumed rate",
            "Short Co Inc": "Short exposure",
            "NVIDIA Corp Swap": "Look-through: NVDA",
            "S&P 500 Index Swap": "Look-through: S&P 500 Index",
            "E-mini S&P 500 Future Dec26": "Look-through: S&P 500 Index",
            "AAPL Call 250 01/15/2027": "Short exposure",
            "United States Treasury": "Cash & T-bills",
            "Dreyfus Treasury Securities Cash Management": "Cash & T-bills",
            "Ford Motor Credit Co LLC": "Bond: assumed rate",
            "USD/JPY Forward": "Non-equity derivative",
            "Cash and other assets less liabilities": "Cash & T-bills",
        }
        self.assertEqual({k: rows[k]["how"] for k in want}, want)
        self.assertEqual(len(s["rows"]), len(want))
        # every row has a fee (or is a 0% short / non-equity row)
        self.assertTrue(all(r["fee"] is not None for r in s["rows"]))
        # the written call: delta 0.4 x 100 shares x 50 contracts x $250 strike / $100M
        self.assertAlmostEqual(rows["AAPL Call 250 01/15/2027"]["exposure"], -0.005)
        self.assertEqual(rows["Short Co Inc"]["contrib"], 0.0)
        self.assertEqual(rows["Ford Motor Credit Co LLC"]["fee"], GC)
        self.assertTrue(rows["Ford Motor Credit Co LLC"]["assumed"])
        self.assertAlmostEqual(s["fee"], sum(r["contrib"] for r in s["rows"]))

    def test_index_swap_priced_from_funds_own_stocks(self):
        s = self.price("nport_mixed.xml")
        rows = by_name(s)
        sleeve = [rows[n] for n in ("Apple Inc", "Microsoft Corp", "Berkshire Hathaway Inc", "Toyota Motor Corp",
                                    "Tencent Holdings Ltd", "Shell PLC", "Exxon Mobil Corporation",
                                    "SPDR S&P 500 ETF Trust", "Abcd Corp", "Obscure Holdings Inc")]
        rate = sum(r["exposure"] * r["fee"] for r in sleeve) / sum(r["exposure"] for r in sleeve)
        swap = rows["S&P 500 Index Swap"]
        self.assertAlmostEqual(swap["exposure"], 0.15)
        self.assertAlmostEqual(swap["fee"], rate)
        self.assertIn("own 10 stock holdings", swap["detail"])

    def test_index_swap_via_proxy_etf(self):
        soxx = etf.price_holdings(filing("nport_proxy_soxx.xml"), self.idx, GC, 0.0)
        self.assertAlmostEqual(soxx["fee"], 0.60 * 0.28 + 0.395 * 3.0)   # DUPE's duplicate row has the fee

        def proxy(index_name):
            self.assertEqual(etf.proxy_for(index_name), "SOXX")
            return ("ok", "SOXX", soxx["equity_rate"], soxx["equity_matched"])

        s = self.price("nport_3x_index.xml", proxy=proxy)
        swap = by_name(s)["ICE Semiconductor Index Swap"]
        self.assertAlmostEqual(swap["exposure"], 3.0)
        self.assertAlmostEqual(swap["fee"], soxx["equity_rate"])
        self.assertAlmostEqual(s["fee"], 3.0 * soxx["equity_rate"])
        self.assertEqual(swap["how"], "Look-through: ICE Semiconductor Index")

    def test_index_swap_without_proxy_or_with_pending_proxy(self):
        s = self.price("nport_3x_index.xml", proxy=lambda name: None)
        swap = by_name(s)["ICE Semiconductor Index Swap"]
        self.assertEqual((swap["how"], swap["fee"]), ("Assumed rate", GC))
        self.assertIn("INDEX_PROXIES", swap["detail"])
        s = self.price("nport_3x_index.xml", proxy=lambda name: ("pending", "SOXX"))
        self.assertTrue(s["pending"])

    def test_basket_components_and_inferred_direction(self):
        s = self.price("nport_basket.xml")
        rows = by_name(s)
        short = rows["Custom Basket Swap"]
        self.assertEqual(short["how"], "Short exposure")                 # fund name says "Bear"
        self.assertIn("inferred from the fund's name", short["detail"])
        self.assertAlmostEqual(short["exposure"], -1.0)
        leg = rows["Custom Basket Swap (long leg)"]
        self.assertEqual(leg["how"], "Look-through: Custom Basket")
        self.assertAlmostEqual(leg["fee"], 0.75 * 0.28 + 0.25 * 0.30)   # weighted by component notional
        self.assertAlmostEqual(leg["exposure"], 0.20)

    def test_yahoo_top_ten(self):
        hold = {"source": "Yahoo top 10", "holdings": [
            {"name": "Apple Inc", "ticker": "AAPL", "pct": 0.07, "yahoo": True},
            {"name": "Berkshire Hathaway", "ticker": "BRK-B", "pct": 0.04, "yahoo": True},
            {"name": "Toyota Motor", "ticker": "7203.T", "pct": 0.03, "yahoo": True},
            {"name": "US Dollar", "ticker": "USD", "pct": 0.01, "yahoo": True},
            {"name": "Everything outside the top 10", "pct": 0.85, "undisclosed": True}]}
        s = etf.price_holdings(hold, self.idx, GC, 0.0)
        rows = by_name(s)
        self.assertEqual(rows["Berkshire Hathaway"]["how"], "IBKR (ticker)")
        self.assertEqual(rows["Toyota Motor"]["how"], "IBKR (Japan file)")
        self.assertEqual(rows["US Dollar"]["how"], "Cash & T-bills")
        self.assertEqual(rows["Everything outside the top 10"]["how"], "Assumed rate")
        self.assertNotIn("Cash and other assets less liabilities", rows)     # only SEC filings get a residual

    def test_warning_when_not_from_sec(self):
        self.assertIsNone(etf.holdings_warning({"source": "SEC N-PORT"}))
        self.assertIn("top 10", etf.holdings_warning({"source": "Yahoo top 10"}))

    def test_direction_from_name(self):
        self.assertEqual(etf.direction_from_name("Direxion Daily TSLA Bull 2X Shares"), "long")
        self.assertEqual(etf.direction_from_name("Defiance Daily Target 2X Long MSTR ETF"), "long")
        self.assertEqual(etf.direction_from_name("ProShares UltraShort QQQ"), "short")
        self.assertEqual(etf.direction_from_name("Test -1X Fund"), "short")
        self.assertIsNone(etf.direction_from_name("iShares Core S&P 500 ETF"))


if __name__ == "__main__":
    unittest.main()
