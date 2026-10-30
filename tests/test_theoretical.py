"""The theoretical fee's harder cases, each shaped like a real filing seen in October 2026:
RYLD (a fund holding one fund), TSLL (swaps on an index named "N/A"), MSTU (swaps far
smaller than the fund's 2x), JEPI (equity-linked notes), SDTY (only T-bills and an
option), written index calls filed as rate derivatives, Irish companies held in
dollars, markets IBKR doesn't lend in, thin lines, unlendable holdings and warnings."""
import io
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"

import etf  # noqa: E402
from ident import Ident, Identifier  # noqa: E402
from test_matching import load_foreign, load_us  # noqa: E402

GC = 0.25


def stock(name, pct, isin=None, cusip=None, currency="USD", country=None, cat="EC", icat="CORP", **kw):
    return dict(name=name, title=name, pct=pct, isin=isin, cusip=cusip, ticker=kw.pop("ticker", None),
                currency=currency, country=country or (isin or "US")[:2], cat=cat, issuer_cat=icat, **kw)


def swap(title, pct, notional, ref, name="Goldman Sachs International", direction="long"):
    return dict(name=name, title=title, pct=pct, cat="DE", issuer_cat="CORP", currency="USD",
                deriv={"kind": "swap", "notional": notional, "ref": ref, "direction": direction,
                       "direction_src": "filing", "deriv_cat": "SWP"})


def fund(holds, series, net_assets=1_000_000_000):
    return {"source": "SEC N-PORT", "holdings": holds, "series": series, "net_assets": net_assets}


def rows_by(s, key="name"):
    return {r[key]: r for r in s["rows"]}


# A Russell 2000 fund's holdings (RSSL), made of fixture stocks
RSSL = fund([stock("Apple Inc", 0.40, "US0378331005"), stock("Microsoft Corp", 0.30, cusip="594918104"),
             stock("Exxon Mobil Corp", 0.20, "US30231G1022"), stock("Southern Co", 0.09, "US8425871071")],
            "Global X Russell 2000 ETF")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = etf.MatchIndex(load_us()[0], load_foreign())

    def price(self, hold, **kw):
        return etf.price_holdings(hold, self.idx, GC, 0.0, **kw)


class FundOfFundsTests(Base):
    """RYLD holds 101% in RSSL, whose own IBKR line is 40.35% on 9,000 shares."""
    RYLD = fund([
        stock("Global X Russell 2000 ETF", 1.0094, "US37960A4537", cusip="37960A453", icat="RF"),
        dict(stock("Rain Oncology Inc", 0.0, "US750CVR0297", cusip="750CVR029")),
        dict(name="Rut Us 08/21/26 C2970 Short", title="Rut Us 08/21/26 C2970 Short", pct=-0.004, cat="DIR",
             issuer_cat="OTHER", ticker="RUQ26C Y 2970.0 US Index", balance=-4000, units="NC",
             deriv={"kind": "option", "notional": None, "ref": {"type": "index", "name": "Russell 2000 Index"},
                    "direction": "short", "direction_src": "filing", "put_call": "call", "written": "written",
                    "shares": 100, "strike": 2970, "delta": None, "deriv_cat": "OPT"}),
    ], "Global X Russell 2000 Covered Call ETF")

    def fund_fn(self, sym):
        self.asked = sym
        s = etf.price_holdings(RSSL, self.idx, GC, 0.0)
        return ("ok", sym, s["fee"], s["coverage"], s["foreign_share"], f"{s['count']} positions")

    def test_priced_from_the_held_funds_holdings(self):
        s = self.price(self.RYLD, fund=self.fund_fn)
        self.assertEqual(self.asked, "RSSL")
        row = rows_by(s)["Global X Russell 2000 ETF"]
        inner = etf.price_holdings(RSSL, self.idx, GC, 0.0)["fee"]
        self.assertEqual(row["how"], "Look-through: RSSL's holdings")
        self.assertEqual(row["source"], "fund_lookthrough")
        self.assertAlmostEqual(row["fee"], inner)
        self.assertLess(s["fee"], 1.0)                          # not RSSL's 40% fee
        self.assertAlmostEqual(s["fee"], 1.0094 * inner, places=6)

    def test_same_index_fund_when_holdings_cant_load(self):
        def broken(sym):
            return ("error", sym, "SEC lookup failed")

        def proxy(name):
            self.assertEqual(etf.proxy_for(name), "IWM")
            return ("ok", "IWM", 0.55, 0.97, 0.0)
        row = rows_by(self.price(self.RYLD, fund=broken, proxy=proxy))["Global X Russell 2000 ETF"]
        self.assertEqual((row["how"], row["fee"], row["source"]), ("Look-through: IWM (same index)", 0.55,
                                                                   "same_index_fund"))

    def test_thin_own_fee_is_never_used(self):
        row = rows_by(self.price(self.RYLD))["Global X Russell 2000 ETF"]
        self.assertEqual((row["how"], row["fee"]), ("Assumed rate", GC))
        self.assertIn("RSSL (US file) not used: 40.35% with only 9,000 shares", row["detail"])

    def test_written_index_call_filed_as_rate_derivative(self):
        row = rows_by(self.price(self.RYLD, fund=self.fund_fn))["Rut Us 08/21/26 C2970 Short"]
        self.assertEqual((row["group"], row["how"]), ("short", "Short exposure"))
        self.assertIn("A written call", row["detail"])
        self.assertIn("size isn't in the filing", row["detail"])

    def test_cvr_is_not_lendable(self):
        row = rows_by(self.price(self.RYLD, fund=self.fund_fn))["Rain Oncology Inc"]
        self.assertEqual((row["group"], row["fee"]), ("notlendable", 0.0))

    def test_pending_held_fund(self):
        s = self.price(self.RYLD, fund=lambda sym: ("pending", sym))
        self.assertTrue(s["pending"])


class SingleStockSwapTests(Base):
    def test_swap_on_an_index_named_na(self):
        hold = fund([swap("TESLA INC Swap", 0.01, 1_200_000_000, {"type": "index", "name": "N/A", "components": []}),
                     swap("Tesla Inc Swap", 0.01, 800_000_000, {"type": "index", "name": "N/A", "components": []}),
                     stock("Tesla Inc", 0.0, "US88160R1014", cat="EC")],
                    "Direxion Daily TSLA Bull 2X Shares")
        s = self.price(hold)
        r = rows_by(s)["Tesla Inc Swap"]
        self.assertEqual((r["how"], r["fee"], r["source"]), ("Look-through: TSLA", 0.30, "stock_lookthrough"))
        self.assertAlmostEqual(s["fee"], 2.0 * 0.30)
        self.assertFalse(any("N/A" in x["how"] for x in s["rows"]))

    def test_mstu_swaps_far_below_the_stated_2x(self):
        ref = {"type": "single", "name": "Strg Trs Mstr Eq", "title": "", "isin": "US5949724083"}
        hold = fund([swap("Strg Trs Mstr Eq", 0.001, 1_500_000, ref, name="Strg Trs Mstr Eq"),
                     swap("Recv Mstu Trs Mstr Us Eq", 0.001, 1_000_000, ref, name="Recv Mstu Trs Mstr Us Eq"),
                     dict(stock("US Dollar", 1.0, cat=""), residual=True)],
                    "T-Rex 2X Long MSTR Daily Target ETF", net_assets=525_000_000)
        s = self.price(hold)
        rows = s["rows"]
        swaps = [r for r in rows if r["type"] == "Swap"]
        self.assertTrue(all(r["how"] == "Look-through: MSTR" for r in swaps), [r["how"] for r in swaps])
        stated = [r for r in rows if r["source"] == "stated_exposure"]
        self.assertEqual(len(stated), 1)
        self.assertEqual(stated[0]["how"], "Stated exposure (MSTR)")
        self.assertAlmostEqual(sum(max(r["exposure"], 0) for r in swaps + stated), 2.0)
        self.assertAlmostEqual(s["fee"], 2.0 * 0.55)

    def test_inverse_fund_gets_no_stated_exposure(self):
        ref = {"type": "single", "name": "Tesla Inc", "isin": "US88160R1014"}
        hold = fund([swap("Tesla swap", 0.0, 10_000_000, ref, direction="short")], "Direxion Daily TSLA Bear 1X")
        self.assertFalse(any(r["source"] == "stated_exposure" for r in self.price(hold)["rows"]))


class EquityLinkedNoteTests(Base):
    def test_notes_in_a_stock_fund_are_priced_as_its_index(self):
        stocks = [stock("Apple Inc", 0.2, "US0378331005"), stock("Microsoft Corp", 0.2, cusip="594918104"),
                  stock("Exxon Mobil Corp", 0.15, "US30231G1022"), stock("Johnson & Johnson", 0.15, "US4781601046"),
                  stock("Nvidia Corp", 0.15, "US67066G1040")]
        notes = [stock("BNP Paribas Issuance BV", 0.07, "US09656N8267", cat="SN"),
                 stock("Barclays Bank plc", 0.06, "US06748U7761", cat="SN")]
        s = self.price(fund(stocks + notes, "JPMorgan Equity Premium Income ETF"))
        sleeve = sum(r["exposure"] * r["fee"] for r in s["rows"] if r["type"] == "Stock") / 0.85
        r = rows_by(s)["BNP Paribas Issuance BV"]
        self.assertEqual((r["type"], r["group"], r["source"]), ("Equity-linked note", "lookthrough", "eln"))
        self.assertAlmostEqual(r["fee"], sleeve)
        self.assertIn("own 5 stock holdings", r["detail"])

    def test_notes_in_a_bond_fund_stay_bonds(self):
        s = self.price(fund([stock("Barclays Bank plc", 0.5, "US06748U7761", cat="SN")], "Some Income Fund"))
        self.assertEqual(rows_by(s)["Barclays Bank plc"]["group"], "bond")


class OptionsOnlyFundTests(Base):
    def test_sdty_is_priced_as_its_stated_index(self):
        opt = dict(name="Spx 12/18/2026 1200.26 C", title="Spx 12/18/2026 1200.26 C", pct=0.84, cat="DE",
                   issuer_cat="OTHER", balance=1, units="NC",
                   deriv={"kind": "option", "notional": None, "ref": None, "direction": "long",
                          "direction_src": "filing", "put_call": "call", "written": "purchased", "shares": 100,
                          "strike": 1200.26, "delta": None, "deriv_cat": "OPT"})
        hold = fund([opt, stock("First American Government Obligations Fund", 0.12, cat="STIV", icat="RF"),
                     stock("Treasury Bill", 0.02, "US912797UK13", cat="DBT", icat="UST")],
                    "YieldMax S&P 500 0DTE Covered Call Strategy ETF", net_assets=46_000_000)

        def proxy(name):
            self.assertEqual(etf.proxy_for(name), "IVV")
            return ("ok", "IVV", 0.27, 0.99, 0.0)
        s = self.price(hold, proxy=proxy)
        stated = [r for r in s["rows"] if r["source"] == "stated_exposure"]
        self.assertEqual(len(stated), 1)
        self.assertEqual(stated[0]["how"], "Stated exposure (IVV's holdings)")
        self.assertAlmostEqual(stated[0]["exposure"], 1.0)
        self.assertAlmostEqual(s["fee"], 0.27)
        self.assertGreater(s["fee"], 0)                       # not 0% because the filing shows only cash


class ListingTests(Base):
    def test_irish_company_held_in_dollars_uses_its_us_line(self):
        s = self.price(fund([stock("Seagate Technology Holdings PLC", 0.5, "IE00BKVD2N49", country="IE"),
                             stock("Medtronic PLC", 0.5, "IE00BTN1Y115", country="IE")], "Test"))
        for r in s["rows"]:
            if r["type"] == "Stock":
                self.assertEqual((r["fee"], r["how"]), (0.25, "IBKR (ISIN)"), r["name"])

    def test_markets_ibkr_doesnt_lend_in(self):
        s = self.price(fund([
            stock("Taiwan Semiconductor Manufacturing Co Ltd", 0.15, "TW0002330008", currency="TWD"),
            stock("MediaTek Inc.", 0.02, "TW0002454006", currency="TWD"),
            stock("Samsung Electronics Co., Ltd.", 0.07, "KR7005930003", currency="KRW"),
            stock("SK hynix Inc.", 0.05, "KR7000660001", currency="KRW"),
            stock("Vibra Energia SA", 0.01, "BRVBBRACNOR1", currency="BRL"),
            stock("Petroleo Brasileiro SA - Petrobras", 0.01, "BRPETRACNPR6", currency="BRL"),
        ], "iShares MSCI Emerging Markets ETF"))
        rows = rows_by(s)
        tsmc = rows["Taiwan Semiconductor Manufacturing Co Ltd"]
        self.assertEqual((tsmc["how"], tsmc["group"], tsmc["fee"]), ("IBKR US line (TSM)", "proxy", 0.2533))
        hynix = rows["SK hynix Inc."]
        self.assertEqual(hynix["how"], "IBKR US line (SKHY)")
        samsung = rows["Samsung Electronics Co., Ltd."]
        self.assertEqual((samsung["how"], samsung["source"]), ("Country median (Korea)", "country_median"))
        self.assertAlmostEqual(samsung["fee"], (0.3192 + 0.4978) / 2)    # KB, SHG, PKX, SKHY
        self.assertIn("IBKR has no borrow market for Korea", samsung["detail"])
        mediatek = rows["MediaTek Inc."]                                  # only TSM stands for Taiwan here
        self.assertEqual((mediatek["how"], mediatek["fee"]), ("Assumed rate", GC))
        self.assertIn("fewer than 3 IBKR lines for companies from Taiwan", mediatek["detail"])
        vibra = rows["Vibra Energia SA"]
        self.assertIn("VBREY (US file) not used: 38.99%", vibra["detail"])
        self.assertLess(vibra["fee"], 5)
        self.assertEqual(rows["Petroleo Brasileiro SA - Petrobras"]["how"], "IBKR (Germany file, secondary listing)")


class StandInOutlierTests(unittest.TestCase):
    def test_adr_far_above_its_market_is_not_used(self):
        us = {"OVCHY": {"name": "OVERSEA-CHINESE BANK-UNS ADR", "fee": 8.94, "shares": 400_000, "isin_tail": "11009"},
              "BBD": {"name": "BANCO BRADESCO-ADR", "fee": 4.34, "shares": 9_000_000, "isin_tail": "01017"},
              "VALE": {"name": "VALE SA-SP ADR", "fee": 0.41, "shares": 9_000_000, "isin_tail": "21026"},
              "PBR": {"name": "PETROLEO BRASILEIRO-SPON ADR", "fee": 0.25, "shares": 9_000_000, "isin_tail": "81018"},
              "ITUB": {"name": "ITAU UNIBANCO H-SPON PRF ADR", "fee": 0.29, "shares": 5_000_000, "isin_tail": "11023"}}
        idx = etf.MatchIndex(us)
        s = etf.price_holdings(fund([
            stock("Oversea-Chinese Banking Corporation Limited", 0.02, "SG1S04926220", currency="SGD"),
            stock("Banco Bradesco S.A.", 0.04, "BRBBDCACNPR8", currency="BRL"),
            stock("Vale S.A.", 0.10, "BRVALEACNOR0", currency="BRL"),
            stock("Hong Kong Dollar", 0.003, cat="", currency="HKD")], "Test"), idx, GC, 0.0)
        rows = rows_by(s)
        ovch = rows["Oversea-Chinese Banking Corporation Limited"]
        self.assertEqual((ovch["how"], ovch["fee"]), ("Assumed rate", GC))
        self.assertIn("OVCHY's 8.94% is far above the assumed rate", ovch["detail"])
        bbd = rows["Banco Bradesco S.A."]
        self.assertEqual(bbd["how"], "Country median (Brazil)")              # VALE, PBR, ITUB: 0.29%
        self.assertAlmostEqual(bbd["fee"], 0.29)
        self.assertIn("BBD's 4.34% is far above the median", bbd["detail"])
        self.assertEqual(rows["Vale S.A."]["how"], "IBKR US line (VALE)")
        self.assertEqual(rows["Hong Kong Dollar"]["group"], "cash")


    def test_several_expensive_adrs_cant_shield_each_other(self):
        lines = {"NVO": 0.28, "DSDVY": 0.25, "VWDRY": 0.45, "PANDY": 2.6, "CABGY": 3.1, "DNKEY": 4.0, "DNNGY": 5.26}
        names = {"NVO": "Novo Nordisk", "DSDVY": "DSV", "VWDRY": "Vestas Wind Systems", "PANDY": "Pandora",
                 "CABGY": "Carlsberg", "DNKEY": "Danske Bank", "DNNGY": "Orsted"}
        us = {s: {"name": f"{names[s].upper()} A/S-UNSP ADR", "fee": f, "shares": 900_000, "isin_tail": f"{i:05d}"}
              for i, (s, f) in enumerate(lines.items())}
        idx = etf.MatchIndex(us)
        s = etf.price_holdings(fund([stock(f"{n} A/S", 0.01, None, currency="DKK", country="DK") for n in names.values()],
                                    "Test"), idx, GC, 0.0)
        fees = {r["name"]: (r["how"], round(r["fee"], 2)) for r in s["rows"] if r["type"] == "Stock"}
        self.assertEqual(fees["Novo Nordisk A/S"], ("IBKR US line (NVO)", 0.28))
        for n in ("Pandora A/S", "Carlsberg A/S", "Danske Bank A/S", "Orsted A/S"):
            self.assertEqual(fees[n], ("Country median (Denmark)", 0.28), n)


class NotLendableTests(Base):
    def test_private_and_warrants(self):
        s = self.price(fund([stock("Openai Group Pbc Series C", 0.03, cat="EP", fair_level="3"),
                             stock("Brera Holdings Plc Wts", 0.001),
                             stock("Apple Inc", 0.5, "US0378331005")], "ARK Innovation ETF"))
        rows = rows_by(s)
        self.assertEqual(rows["Openai Group Pbc Series C"]["group"], "notlendable")
        self.assertIn("fair value level 3", rows["Openai Group Pbc Series C"]["detail"])
        self.assertEqual(rows["Brera Holdings Plc Wts"]["group"], "notlendable")
        self.assertAlmostEqual(s["notlendable_weight"], 0.031)
        self.assertEqual(s["coverage"], 1.0)                 # unlendable holdings don't count against coverage


class IdentificationTests(Base):
    def test_unmatched_holding_is_identified_by_openfigi(self):
        class Figi:
            calls = []

            def lookup(self, keys):
                self.calls.append(list(keys))
                return {k: Ident("SPCX", "BBG000NQF3Z5", "SPACE EXPLORATION TECHN-CL A", "openfigi") for k in keys}
        ids = Identifier(figi=Figi(), ftd=None, yahoo=None, background=False)
        hold = fund([stock("Brand New Name Corp", 0.5, "US84615Q1031")], "Test")
        first = self.price(hold, ids=ids)
        self.assertEqual(Figi.calls, [["ISIN:US84615Q1031"]])
        again = self.price(hold, ids=ids)
        r = rows_by(again)["Brand New Name Corp"]
        self.assertEqual((r["how"], r["source"]), ("IBKR (FIGI via OpenFIGI)", "figi"))
        self.assertEqual(first["identifying"], 0)            # looked up synchronously here

    def test_pending_count_while_identifying(self):
        ids = Identifier(figi=None, ftd=None, yahoo=None, background=True)
        ids.queue.append("ISIN:US84615Q1031")
        ids.queued.add("ISIN:US84615Q1031")
        ids.running = True                                   # a lookup already in progress
        s = self.price(fund([stock("Brand New Name Corp", 0.5, "US84615Q1031")], "Test"), ids=ids)
        self.assertEqual(s["identifying"], 1)
        self.assertIn("still being identified", s["rows"][0]["detail"])


class WarningTests(unittest.TestCase):
    def test_one_thin_holding_drives_the_estimate(self):
        theo = {"fee": 40.7, "coverage": 1.0, "rows": [
            {"name": "Global X Russell 2000 ETF", "contrib": 40.7, "fee": 40.35, "exposure": 1.0094}]}
        w = etf.outlier_warnings(theo, quoted=4.15)
        self.assertEqual(len(w), 2)
        self.assertIn("drives most of this estimate", w[0])
        self.assertIn("more than 3×", w[1])

    def test_mostly_assumed(self):
        theo = {"fee": 0.3, "stock_assumed_share": 0.6, "rows": []}
        self.assertIn("More than half of the stock exposure", etf.outlier_warnings(theo)[0])
        theo.update(stock_assumed_share=0.07)      # ADR lines and country medians aren't assumed
        self.assertEqual(etf.outlier_warnings(theo), [])

    def test_bond_fund_isnt_flagged(self):
        s = etf.price_holdings(fund([stock("Ford Motor Credit Co LLC 5.1% 2030", 0.95, cat="DBT")], "High Yield Bond ETF"),
                               etf.MatchIndex(load_us()[0]), GC, 0.0)
        self.assertEqual(etf.outlier_warnings(s), [])

    def test_no_warning_for_a_normal_fund(self):
        theo = {"fee": 0.3, "coverage": 0.98, "rows": [{"name": "A", "contrib": 0.05, "fee": 0.25, "exposure": 0.2}]}
        self.assertEqual(etf.outlier_warnings(theo, quoted=0.27), [])


def xlsx(rows):
    """A minimal .xlsx with shared strings, like State Street's holdings file."""
    strings, sheet_rows = [], []
    for i, row in enumerate(rows, 1):
        cells = []
        for j, v in enumerate(row):
            ref = f"{chr(65 + j)}{i}"
            if isinstance(v, (int, float)):
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            else:
                strings.append(v)
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
        sheet_rows.append(f'<row r="{i}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}>' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>')
    return buf.getvalue()


class UnitTrustTests(Base):
    def test_state_street_daily_file(self):
        data = xlsx([["Fund Name:", "State Street SPDR S&amp;P 500 ETF Trust"], ["Ticker Symbol:", "SPY"],
                     ["Holdings:", "As of 23-Oct-2026"], [],
                     ["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"],
                     ["APPLE INC", "AAPL", "037833100", "2046251", 7.7, "-", 178676765, "USD"],
                     ["MICROSOFT CORP", "MSFT", "594918104", "2588173", 5.9, "-", 90904736, "USD"],
                     ["US DOLLAR", "-", "-", "-", 0.1, "-", 1, "USD"]])
        name, as_of, holds = etf.parse_ssga(etf.read_xlsx_rows(data))
        self.assertEqual((name, as_of, len(holds)), ("State Street SPDR S&P 500 ETF Trust", "2026-10-23", 3))
        s = self.price({"source": "State Street daily holdings", "holdings": holds, "series": name})
        rows = rows_by(s)
        self.assertEqual(rows["Apple Inc"]["how"], "IBKR (ticker)")
        self.assertEqual(rows["Us Dollar"]["group"], "cash")
        self.assertAlmostEqual(s["fee"], 0.077 * 0.25 + 0.059 * 0.26)

    def test_qqq_uses_qqqm_filing(self):
        class FakeSec:
            def series_for(self, sym):
                return (1, "S000001") if sym == "QQQM" else None

            def latest_nport_folder(self, cik, series):
                return "https://www.sec.gov/x"

            def get(self, url):
                return (FIX / "nport_proxy_soxx.xml").read_bytes()
        old = etf.SEC, etf.SEC_USER_AGENT
        etf.SEC, etf.SEC_USER_AGENT = FakeSec(), "test test@example.com"
        try:
            h = etf.fetch_holdings(None, "QQQ")
        finally:
            etf.SEC, etf.SEC_USER_AGENT = old
        self.assertEqual(h["standin"], "QQQM")
        self.assertIn("unit investment trust", h["notes"][0])
        self.assertTrue(h["holdings"])


class ParseTests(unittest.TestCase):
    def test_currency_fair_value_and_warrant_category(self):
        xml = b"""<edgarSubmission xmlns="http://www.sec.gov/edgar/nport"><formData><invstOrSecs>
          <invstOrSec><name>Seagate</name><title>Seagate</title><cusip>G7997R103</cusip>
            <identifiers><isin value="IE00BKVD2N49"/></identifiers><balance>100</balance><units>NS</units>
            <curCd>USD</curCd><valUSD>12000</valUSD><pctVal>1</pctVal><payoffProfile>Long</payoffProfile>
            <assetCat>EC</assetCat><issuerCat>CORP</issuerCat><invCountry>IE</invCountry>
            <fairValLevel>1</fairValLevel></invstOrSec>
          <invstOrSec><name>Samsung</name><title>Samsung</title><cusip>000000000</cusip>
            <identifiers><isin value="KR7005930003"/></identifiers>
            <currencyConditional curCd="KRW" exchangeRt="1400"/><pctVal>1</pctVal>
            <assetCat>EC</assetCat><issuerCat>CORP</issuerCat><invCountry>KR</invCountry>
            <fairValLevel>3</fairValLevel></invstOrSec>
          <invstOrSec><name>Brera</name><title>Brera Wts</title><cusip>000000000</cusip><pctVal>0.1</pctVal>
            <assetCat>DE</assetCat><issuerCat>CORP</issuerCat>
            <derivativeInfo><optionSwaptionWarrantDeriv derivCat="WAR"><putOrCall>Call</putOrCall>
            <writtenOrPur>Purchased</writtenOrPur></optionSwaptionWarrantDeriv></derivativeInfo></invstOrSec>
        </invstOrSecs></formData></edgarSubmission>"""
        h = etf.parse_nport(xml)["holdings"]
        self.assertEqual((h[0]["currency"], h[0]["fair_level"], h[0]["value"]), ("USD", "1", 12000))
        self.assertEqual(etf.holding_price(h[0]), 120)
        self.assertEqual((h[1]["currency"], h[1]["fair_level"]), ("KRW", "3"))
        self.assertEqual(h[2]["deriv"]["deriv_cat"], "WAR")
        idx = etf.MatchIndex(load_us()[0], load_foreign())
        row, _ = etf.price_derivative(h[2], idx, GC, 1e6, "Test", None, None)
        self.assertEqual(row["group"], "notlendable")

    def test_stated_leverage(self):
        self.assertEqual(etf.stated_leverage("T-Rex 2X Long MSTR Daily Target ETF"), 2.0)
        self.assertEqual(etf.stated_leverage("ProShares UltraPro QQQ"), 3.0)
        self.assertEqual(etf.stated_leverage("ProShares Ultra S&P500"), 2.0)
        self.assertEqual(etf.stated_leverage("YieldMax S&P 500 0DTE Covered Call Strategy ETF"), 1.0)


if __name__ == "__main__":
    unittest.main()
