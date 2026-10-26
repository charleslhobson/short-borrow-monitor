"""IBKR stock loan files and matching holdings to them.

The fixtures copy IBKR's real format (checked October 2026): US ISINs are masked to
their last 5 characters ("XXXXXXX31005" for Apple), names carry HTML entities
("JOHNSON &amp; JOHNSON") and some are stale (META is "Facebook Inc."), every row has
a composite FIGI and IBKR's contract id, and non-US ISINs are given in full."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"

import app  # noqa: E402
import etf  # noqa: E402
from ident import Ident  # noqa: E402


def load_us():
    return app.parse_borrow_text((FIX / "ibkr_usa.txt").read_text())


def load_foreign():
    return {n: app.parse_borrow_text((FIX / f"ibkr_{n}.txt").read_text())[0]
            for n in ("japan", "british", "hongkong", "austria", "germany")}


class BorrowFileTests(unittest.TestCase):
    def test_parse_basics(self):
        table, as_of = load_us()
        self.assertEqual(as_of, "2026.10.21 09:30:01")
        self.assertIn("BRK.B", table)                      # "BRK B" in the file
        self.assertIn("NEE.PRT", table)                    # "NEE PRT"
        self.assertEqual(table["AAPL"]["fee"], 0.25)
        self.assertEqual(table["AAPL"]["shares"], 10_000_000)
        self.assertTrue(table["AAPL"]["plus"])
        self.assertIsNone(table["ABCD"]["fee"])             # "NA"

    def test_masked_isin_keeps_its_tail_and_figi(self):
        table, _ = load_us()
        aapl = table["AAPL"]
        self.assertIsNone(aapl["isin"])                     # "XXXXXXX31005" isn't an ISIN
        self.assertEqual(aapl["isin_tail"], "31005")
        self.assertEqual(aapl["figi"], "BBG000B9XRY4")
        self.assertEqual(aapl["con"], "265598")
        self.assertEqual(table["STX"]["isin"], "IE00BKVD2N49")   # non-US ISINs are complete
        self.assertEqual(table["STX"]["isin_tail"], "D2N49")

    def test_html_entities_in_names(self):
        table, _ = load_us()
        self.assertEqual(table["JNJ"]["name"], "Johnson & Johnson")
        self.assertEqual(table["T"]["name"], "At&T Inc")

    def test_duplicate_symbol_keeps_row_with_fee(self):
        table, _ = load_us()
        self.assertEqual(table["DUPE"]["fee"], 3.0)
        self.assertEqual(table["DUPE"]["shares"], 50_000)

    def test_duplicate_prefers_more_shares_when_both_have_fees(self):
        text = ("#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|\n"
                "XYZ|USD|XYZ|1|XXXXXXX00001|1|2.0|1000|\n"
                "XYZ|USD|XYZ|2|XXXXXXX00001|1|5.0|9000|\n")
        table, _ = app.parse_borrow_text(text)
        self.assertEqual(table["XYZ"]["fee"], 5.0)


class IdentifierTests(unittest.TestCase):
    def test_cusip_to_isin(self):
        self.assertEqual(etf.cusip_to_isin("037833100"), "US0378331005")
        self.assertEqual(etf.cusip_to_isin("88160R101"), "US88160R1014")
        self.assertIsNone(etf.cusip_to_isin("J92676113"))   # foreign CINS code
        self.assertIsNone(etf.cusip_to_isin("000000000"))
        self.assertIsNone(etf.cusip_to_isin("N/A"))

    def test_masked_isin_is_not_an_isin(self):
        self.assertIsNone(etf.clean_isin("XXXXXXX31005"))
        self.assertIsNone(etf.clean_isin("US0378331006"))    # wrong check digit
        self.assertEqual(etf.clean_isin(" us0378331005 "), "US0378331005")

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
        self.assertEqual(etf.norm_name("JOHNSON &amp; JOHNSON"), etf.norm_name("Johnson & Johnson"))
        self.assertEqual(etf.norm_name("ARM HOLDINGS PLC-ADR"), etf.norm_name("ARM Holdings plc"))
        self.assertEqual(etf.norm_name("INFOSYS LTD-SP ADR"), "INFOSYS")
        self.assertEqual(etf.norm_name("Southern Co. (The)"), "SOUTHERN")
        self.assertIsNone(etf.norm_name("N/A"))
        self.assertEqual(etf.name_key("MC DONALD'S-CORP"), etf.name_key("McDonald's Corp."))

    def test_renamed_companies(self):
        self.assertTrue(etf.names_agree("Meta Platforms, Inc.", "Facebook Inc."))
        self.assertTrue(etf.names_agree("Alphabet Inc. Class C", "Google Inc."))
        self.assertTrue(etf.names_agree("Taiwan Semiconductor Manufacturing Co Ltd", "TSMC"))
        self.assertTrue(etf.names_agree("Space Exploration Technologies Corp.", "SpaceX"))
        self.assertTrue(etf.names_agree("Coinbase Global, Inc.", "COINBASE GLOBAL INC -CLASS A"))
        self.assertFalse(etf.names_agree("NextEra Energy, Inc.", "HDFC BANK LTD-ADR"))

    def test_home_market(self):
        self.assertEqual(etf.home_market("IE00BTN1Y115", "USD", "IE"), "us")       # Medtronic, held in dollars
        self.assertEqual(etf.home_market("IE00BTN1Y115", "EUR", "IE"), "ireland")
        self.assertEqual(etf.home_market("KR7005930003", "KRW", "KR"), "korea")
        self.assertEqual(etf.home_market("KYG875721634", "HKD", "KY"), "hongkong")
        self.assertEqual(etf.home_market("US0378331005"), "us")
        self.assertEqual(etf.home_market(ticker="7203.T", yahoo=True), "japan")
        self.assertEqual(etf.home_market(ticker="AAPL", yahoo=True), "us")
        self.assertIsNone(etf.home_market())


class MatchIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = etf.MatchIndex(load_us()[0], load_foreign())

    def find(self, **kw):
        entry, how, tried, kind = self.idx.find(**kw)
        return (entry["symbol"] if entry else None), how, tried

    def test_masked_isin_suffix_and_name(self):
        self.assertEqual(self.find(isin="US0378331005", names=("Apple Inc",))[:2], ("AAPL", "suffix"))

    def test_suffix_alone_isnt_enough(self):
        sym, _, tried = self.find(isin="US0378331005")
        self.assertIsNone(sym)
        self.assertTrue(any("ending 31005 (AAPL)" in t for t in tried), tried)

    def test_cusip_becomes_isin(self):
        self.assertEqual(self.find(cusip="594918104", names=("Microsoft Corp",))[:2], ("MSFT", "suffix"))

    def test_suffix_collision_is_settled_by_name(self):
        # NextEra (US65339F1012) and HDFC Bank's ADR (US40415F1012) both end F1012
        self.assertEqual(self.find(isin="US65339F1012", names=("NextEra Energy, Inc.",))[0], "NEE")
        self.assertEqual(self.find(isin="US40415F1012", names=("HDFC Bank Ltd ADR",))[0], "HDB")

    def test_stale_ibkr_names(self):
        self.assertEqual(self.find(isin="US30303M1027", names=("Meta Platforms, Inc.",))[0], "META")
        self.assertEqual(self.find(isin="US02079K1079", names=("Alphabet Inc.",))[0], "GOOG")
        self.assertEqual(self.find(isin="US02079K3059", names=("Alphabet Inc.",))[0], "GOOGL")
        self.assertEqual(self.find(isin="US8740391003", names=("Taiwan Semiconductor Manufacturing Co Ltd",))[0],
                         "TSM")

    def test_entities_and_apostrophes(self):
        self.assertEqual(self.find(isin="US4781601046", names=("Johnson & Johnson",))[0], "JNJ")
        self.assertEqual(self.find(isin="US5801351017", names=("McDonald's Corp.",))[0], "MCD")
        self.assertEqual(self.find(names=("Johnson & Johnson",))[:2], ("JNJ", "name"))

    def test_common_stock_over_preferred_and_notes(self):
        self.assertEqual(self.find(isin="US00206R1023", names=("AT&T, Inc.",))[0], "T")
        self.assertEqual(self.find(names=("AT&T Inc",))[0], "T")                    # T.PRA is a preferred
        self.assertEqual(self.find(names=("Southern Co. (The)",))[0], "SO")         # SOJD / SOJE are notes
        self.assertEqual(self.find(isin="US8425871071", names=("The Southern Company",))[0], "SO")

    def test_figi_from_openfigi(self):
        known = Ident("META", "BBG000MM2P62", "META PLATFORMS INC-CLASS A", "openfigi")
        self.assertEqual(self.find(isin="US30303M1027", known=known)[:2], ("META", "figi"))

    def test_ticker_from_an_outside_source_is_checked(self):
        known = Ident("SPCX", None, "SPACE EXPLORATION TECHN-CL A", "openfigi")
        self.assertEqual(self.find(isin="US84615Q1031", known=known)[:2], ("SPCX", "ticker:openfigi"))
        wrong = Ident("AAPL", None, "APPLE", "yahoo")                 # AAPL's ISIN ends 31005, not Q1031
        sym, _, tried = self.find(isin="US84615Q1031", known=wrong)
        self.assertIsNone(sym)
        self.assertTrue(any("another security" in t for t in tried))

    def test_ticker_found_elsewhere_but_not_at_ibkr(self):
        known = Ident("EA", "BBG000BP5T93", "ELECTRONIC ARTS INC", "openfigi")
        sym, _, tried = self.find(isin="US2855121099", names=("Electronic Arts Inc",), known=known)
        self.assertIsNone(sym)
        self.assertIn("OpenFIGI says EA, which isn't in IBKR's file (delisted, renamed or not lendable)", tried)

    def test_fund_name_ticker_checked_by_isin_suffix(self):
        # MSTU's swaps name the stock "Strg Trs Mstr Eq" but give Strategy's ISIN
        sym, how, _ = self.find(isin="US5949724083", names=("Strg Trs Mstr Eq",), hints=("T", "MSTR"))
        self.assertEqual((sym, how), ("MSTR", "ticker:fund_name"))

    def test_ticker_formats(self):
        self.assertEqual(self.find(ticker="BRK/B")[:2], ("BRK.B", "ticker"))
        self.assertEqual(self.find(ticker="BRK-B", yahoo=True)[:2], ("BRK.B", "ticker"))
        self.assertEqual(self.find(ticker="NVDA US")[:2], ("NVDA", "ticker"))

    def test_listed_without_fee_falls_through_to_ticker(self):
        sym, how, _ = self.find(isin="US1111111118", ticker="ABCE", names=("Abcd Corp",))
        self.assertEqual((sym, how), ("ABCE", "ticker"))

    def test_duplicate_full_isin_prefers_row_with_fee(self):
        entry, _, _, _ = self.idx.find(isin="CA3333333332", currency="USD")
        self.assertEqual((entry["symbol"], entry["fee"]), ("TWIN", 0.9))

    def test_dollar_holding_of_irish_company_uses_us_line(self):
        entry, how, _, kind = self.idx.find(isin="IE00BKVD2N49", names=("Seagate Technology Holdings PLC",),
                                            currency="USD", country="IE")
        self.assertEqual((entry["symbol"], entry["file"], how, kind), ("STX", "usa", "isin", "primary"))

    def test_foreign_isin_prefers_home_market_file(self):
        entry, how, _, kind = self.idx.find(isin="JP3633400001")
        self.assertEqual((entry["symbol"], entry["file"], how, kind), ("7203", "japan", "isin", "primary"))
        self.assertEqual(etf.match_label(entry, how, kind), "IBKR (Japan file)")

    def test_foreign_tickers(self):
        self.assertEqual(self.find(ticker="7203.T", yahoo=True)[0], "7203")
        self.assertEqual(self.find(ticker="0700.HK", yahoo=True)[0], "700")    # leading zero dropped
        self.assertEqual(self.find(ticker="700 HK")[0], "700")
        self.assertEqual(self.find(ticker="9984", country="JP")[0], "9984")      # bare code + home country

    def test_no_ibkr_market(self):
        sym, _, tried = self.find(isin="KR7005930003", names=("Samsung Electronics Co., Ltd.",), currency="KRW")
        self.assertIsNone(sym)
        self.assertIn("IBKR has no borrow file for Korea", tried)

    def test_adr_line_for_a_market_without_a_file(self):
        entry, how, _, kind = self.idx.find(isin="KR7000660001", names=("SK hynix Inc.",), currency="KRW")
        self.assertEqual((entry["symbol"], how, kind), ("SKHY", "name", "us_line"))   # not the 300-share GDS
        self.assertEqual(etf.match_label(entry, how, kind), "IBKR US line (SKHY)")

    def test_secondary_listing(self):
        entry, how, _, kind = self.idx.find(isin="BRPETRACNPR6", names=("Petroleo Brasileiro SA - Petrobras",),
                                            currency="BRL")
        self.assertEqual((entry["symbol"], kind), ("PJXB", "secondary"))
        self.assertEqual(etf.match_label(entry, how, kind), "IBKR (Germany file, secondary listing)")

    def test_thin_lines_are_turned_down(self):
        def guard(entry, kind, how):
            return etf.thin_reason(entry)
        entry, _, tried, _ = self.idx.find(isin="BRVBBRACNOR1", names=("Vibra Energia SA",), currency="BRL",
                                           guard=guard)
        self.assertIsNone(entry)
        self.assertTrue(any(t.startswith("VBREY (US file) not used: 38.99%") for t in tried), tried)
        self.assertTrue(any(t.startswith("VBRX (Germany file) not used") for t in tried), tried)

    def test_adr_lines_only_for_foreign_companies(self):
        us = {"PFE": {"name": "PFIZER INC", "fee": 0.3, "shares": 10_000_000, "isin_tail": "31030"},
              "ITUB": {"name": "ITAU UNIBANCO H-SPON PRF ADR", "fee": 0.29, "shares": 5_000_000, "isin_tail": "11023"},
              "PBR": {"name": "PETROLEO BRASILEIRO-SPON ADR", "fee": 0.25, "shares": 9_000_000, "isin_tail": "81018"},
              "PBR.A": {"name": "PETROLEO BRASILEIRO-SP PREF ADR", "fee": 0.27, "shares": 3_000_000, "isin_tail": "81026"},
              "VBREY": {"name": "VIBRA ENERGIA SA", "fee": 0.6, "shares": 900_000, "isin_tail": "Y3053"}}
        idx = etf.MatchIndex(us)
        entry, _, _, _ = idx.find(isin="INE182A01018", names=("Pfizer Ltd",), currency="INR")
        self.assertIsNone(entry)                                  # an Indian company, not Pfizer Inc
        entry, how, _, kind = idx.find(isin="BRITUBACNPR1", names=("Itau Unibanco Holding S.A.",), currency="BRL")
        self.assertEqual((entry["symbol"], kind), ("ITUB", "us_line"))
        self.assertEqual(idx.find(names=("Petroleo Brasileiro S.A. - Petrobras",), currency="BRL",
                                  country="BR")[0]["symbol"], "PBR")
        self.assertEqual(idx.find(names=("Petroleo Brasileiro S.A. - Petrobras Pref",), currency="BRL",
                                  country="BR")[0]["symbol"], "PBR.A")
        self.assertEqual(idx.find(names=("Vibra Energia SA",), currency="BRL", country="BR")[0]["symbol"], "VBREY")

    def test_fuzzy_adr_names_need_more_than_generic_words(self):
        us = {"BNS": {"name": "BANK OF NOVA SCOTIA", "fee": 0.28, "shares": 5_000_000, "isin": "CA0641491075"},
              "HKXCY": {"name": "HONG KONG EXCHANGES-UNSP ADR", "fee": 3.9, "shares": 900_000, "isin_tail": "X1004"},
              "EHLD": {"name": "EURO-HOLDINGS LTD", "fee": 6.1, "shares": 200_000, "isin": "MHY2349R1011"},
              "OVCHY": {"name": "OVERSEA-CHINESE BANK-UNS ADR", "fee": 8.94, "shares": 400_000, "isin_tail": "11009"}}
        idx = etf.MatchIndex(us)
        self.assertIsNone(idx.find(names=("Bank of Baroda",), currency="INR", country="IN")[0])
        self.assertIsNone(idx.find(names=("Hong Kong & China Gas Co Ltd",), currency="HKD", country="HK")[0])
        self.assertIsNone(idx.find(names=("Euro",), currency="EUR", country="FR")[0])
        self.assertEqual(idx.find(names=("Oversea-Chinese Banking Corporation Limited",), currency="SGD",
                                  country="SG")[0]["symbol"], "OVCHY")

    def test_ambiguous_name_is_not_used(self):
        us = {"GOOGL": {"name": "ALPHABET INC", "fee": 0.25, "shares": 10_000_000, "plus": True, "isin": None},
              "GOOG": {"name": "ALPHABET INC.", "fee": 0.25, "shares": 10_000_000, "plus": True, "isin": None}}
        entry, _, tried, _ = etf.MatchIndex(us).find(names=("Alphabet Inc",))
        self.assertIsNone(entry)
        self.assertTrue(any("matches 2 IBKR lines" in t for t in tried))

    def test_nothing_found_explains_why(self):
        entry, how, tried, _ = self.idx.find(cusip="999999999", names=("Obscure Holdings Inc",))
        self.assertIsNone(entry)
        self.assertTrue(tried)


if __name__ == "__main__":
    unittest.main()
