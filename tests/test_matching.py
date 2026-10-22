"""IBKR stock loan files and matching holdings to them."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"

import app  # noqa: E402
import etf  # noqa: E402


def load_us():
    return app.parse_borrow_text((FIX / "ibkr_usa.txt").read_text())


def load_foreign():
    return {n: app.parse_borrow_text((FIX / f"ibkr_{n}.txt").read_text())[0]
            for n in ("japan", "british", "hongkong")}


class BorrowFileTests(unittest.TestCase):
    def test_parse_basics(self):
        table, as_of = load_us()
        self.assertEqual(as_of, "2026.10.21 09:30:01")
        self.assertIn("BRK.B", table)                      # "BRK B" in the file
        self.assertEqual(table["AAPL"]["fee"], 0.25)
        self.assertEqual(table["AAPL"]["shares"], 10_000_000)
        self.assertTrue(table["AAPL"]["plus"])
        self.assertIsNone(table["ABCD"]["fee"])             # "NA"

    def test_duplicate_symbol_keeps_row_with_fee(self):
        table, _ = load_us()
        self.assertEqual(table["DUPE"]["fee"], 3.0)
        self.assertEqual(table["DUPE"]["shares"], 50_000)

    def test_duplicate_prefers_more_shares_when_both_have_fees(self):
        text = ("#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|\n"
                "XYZ|USD|XYZ|1|US0000000001|1|2.0|1000|\n"
                "XYZ|USD|XYZ|2|US0000000001|1|5.0|9000|\n")
        table, _ = app.parse_borrow_text(text)
        self.assertEqual(table["XYZ"]["fee"], 5.0)


class IdentifierTests(unittest.TestCase):
    def test_cusip_to_isin(self):
        self.assertEqual(etf.cusip_to_isin("037833100"), "US0378331005")
        self.assertEqual(etf.cusip_to_isin("88160R101"), "US88160R1014")
        self.assertIsNone(etf.cusip_to_isin("J92676113"))   # foreign CINS code
        self.assertIsNone(etf.cusip_to_isin("000000000"))
        self.assertIsNone(etf.cusip_to_isin("N/A"))

    def test_norm_ticker(self):
        for raw, want in [("BRK-B", "BRK.B"), ("BRK/B", "BRK.B"), ("BRK B", "BRK.B"),
                          ("AAPL US", "AAPL"), ("aapl us equity", "AAPL"), ("$TSLA", "TSLA"), ("", None)]:
            self.assertEqual(etf.norm_ticker(raw), want, raw)

    def test_split_foreign(self):
        self.assertEqual(etf.split_foreign("7203 JP"), ("7203", "japan"))
        self.assertEqual(etf.split_foreign("700 HK Equity"), ("700", "hongkong"))
        self.assertEqual(etf.split_foreign("7203.T", yahoo=True), ("7203", "japan"))
        self.assertEqual(etf.split_foreign("SHEL.L", yahoo=True), ("SHEL", "uk"))
        self.assertIsNone(etf.split_foreign("7203.T"))           # only Yahoo uses suffixes
        self.assertIsNone(etf.split_foreign("BRK.B", yahoo=True))
        self.assertIsNone(etf.split_foreign("AAPL US"))

    def test_norm_name(self):
        self.assertEqual(etf.norm_name("Alphabet Inc Class A"), etf.norm_name("ALPHABET INC-CL A"))
        self.assertEqual(etf.norm_name("Exxon Mobil Corporation"), "EXXON MOBIL")
        self.assertNotEqual(etf.norm_name("Alphabet Inc Class A"), etf.norm_name("Alphabet Inc Class C"))


class MatchIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = etf.MatchIndex(load_us()[0], load_foreign())

    def find(self, **kw):
        entry, how, tried = self.idx.find(**kw)
        return (entry["symbol"] if entry else None), how, tried

    def test_isin(self):
        self.assertEqual(self.find(isin="US0378331005")[:2], ("AAPL", "isin"))

    def test_cusip_index_from_ibkr_isins(self):
        self.assertEqual(self.find(cusip="594918104")[:2], ("MSFT", "cusip"))

    def test_ticker_formats(self):
        self.assertEqual(self.find(ticker="BRK/B")[:2], ("BRK.B", "ticker"))
        self.assertEqual(self.find(ticker="BRK-B", yahoo=True)[:2], ("BRK.B", "ticker"))
        self.assertEqual(self.find(ticker="NVDA US")[:2], ("NVDA", "ticker"))

    def test_isin_without_fee_falls_through_to_ticker(self):
        sym, how, tried = self.find(isin="US1111111116", ticker="ABCE")
        self.assertEqual((sym, how), ("ABCE", "ticker"))
        self.assertIn("listed without a fee", tried[0])

    def test_duplicate_isin_prefers_row_with_fee(self):
        entry, _, _ = self.idx.find(isin="US3333333333")
        self.assertEqual(entry["symbol"], "TWIN")
        self.assertEqual(entry["fee"], 0.9)

    def test_foreign_isin_prefers_home_market_file(self):
        entry, how, _ = self.idx.find(isin="JP3633400001")
        self.assertEqual((entry["symbol"], entry["file"], how), ("7203", "japan", "isin"))
        self.assertEqual(etf.match_label(entry, how), "IBKR (Japan file)")

    def test_foreign_tickers(self):
        self.assertEqual(self.find(ticker="7203.T", yahoo=True)[0], "7203")
        self.assertEqual(self.find(ticker="0700.HK", yahoo=True)[0], "700")    # leading zero dropped
        self.assertEqual(self.find(ticker="700 HK")[0], "700")
        self.assertEqual(self.find(ticker="9984", country="JP")[0], "9984")      # bare code + home country

    def test_unique_name_match(self):
        sym, how, _ = self.find(names=("Exxon Mobil Corporation",))
        self.assertEqual((sym, how), ("XOM", "name"))

    def test_ambiguous_name_is_not_used(self):
        us = {"GOOGL": {"name": "ALPHABET INC", "fee": 0.25, "shares": 1, "isin": None},
              "GOOG": {"name": "ALPHABET INC.", "fee": 0.25, "shares": 1, "isin": None}}
        entry, _, tried = etf.MatchIndex(us).find(names=("Alphabet Inc",))
        self.assertIsNone(entry)
        self.assertTrue(any("matches 2 IBKR rows" in t for t in tried))

    def test_nothing_found_explains_why(self):
        entry, how, tried = self.idx.find(cusip="999999999", names=("Obscure Holdings Inc",))
        self.assertIsNone(entry)
        self.assertTrue(tried)


if __name__ == "__main__":
    unittest.main()
