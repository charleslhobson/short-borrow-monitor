"""Every Stocks page category for every ticker: Yahoo spellings (both ways), ISIN
fallback, old listings, FINRA short interest, fund equivalents, look-through,
reason kinds and the whole-row "not on Yahoo" message."""
import sys
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import borrow_history  # noqa: E402
import common  # noqa: E402
import fake_yf  # noqa: E402
import finra  # noqa: E402
import metrics  # noqa: E402
from test_metrics import fresh_service, settle  # noqa: E402

HEAD = ('"accountingYearMonthNumber"|"symbolCode"|"issueName"|"issuerServicesGroupExchangeCode"|"marketClassCode"|'
        '"currentShortPositionQuantity"|"previousShortPositionQuantity"|"stockSplitFlag"|"averageDailyVolumeQuantity"|'
        '"daysToCoverQuantity"|"revisionFlag"|"changePercent"|"changePreviousNumber"|"settlementDate"')


def finra_line(d, sym, name, short, prev, adv):
    return (f'"{d.replace("-", "")}"|"{sym}"|"{name}"|"E"|"ARCA"|"{short}"|"{prev}"||"{adv}"|"1.00"||"0"|"0"|"{d}"')


FINRA_FILES = {
    "20260930": [("FUND", "Some Index ETF", 98476070, 99297854, 48804519),
                 ("ABRPRD", "Arbor Realty Trust, Inc. 6.375% Series D", 1800, 1700, 9000),
                 ("BRKB", "Some Other Company Class B", 5, 5, 5),
                 ("THIN", "Thin Data Corp Common Stock", 2_000_000, 1_900_000, 400_000)],
    "20260915": [("FUND", "Some Index ETF", 99297854, 97000000, 40000000)],
    "20260831": [("FUND", "Some Index ETF", 90000000, 91000000, 40000000),
                 ("THIN", "Thin Data Corp Common Stock", 1_500_000, 1_400_000, 400_000)],
}


def fake_finra_get(url, method="GET", timeout=60):
    stamp = url.rsplit("shrt", 1)[-1][:8]
    rows = FINRA_FILES.get(stamp)
    if rows is None:
        return 404, b""
    d = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
    text = "\n".join([HEAD] + [finra_line(d, *r) for r in rows])
    return 200, (text.encode() if method == "GET" else b"")


def loaded_finra():
    f = finra.FinraShortInterest(get=fake_finra_get, today=lambda: date(2026, 10, 24))
    f.ensure(background=False)
    return f


class Spellings(unittest.TestCase):
    def test_ibkr_to_yahoo(self):
        cases = {"BRK.B": "BRK-B", "ABR.PRD": "ABR-PD", "ALL.PRH": "ALL-PH", "DCOM.PR": "DCOM-P", "AAC.U": "AAC-UN",
                 "XYZ.WS": "XYZ-WT", "XYZ.RT": "XYZ-RI", "MSGR.WI": "MSGR-WI", "AAPL": "AAPL", "MKC.V": "MKC-V"}
        for ibkr, yahoo in cases.items():
            self.assertEqual(common.yahoo_spellings(ibkr)[0], yahoo, ibkr)

    def test_yahoo_to_ibkr(self):
        cases = {"BRK-B": "BRK.B", "BAC-PD": "BAC.PRD", "AAC-UN": "AAC.U", "XYZ-WT": "XYZ.WS", "XYZ-RI": "XYZ.RT",
                 "DCOM-P": "DCOM.PR", "brk/b": "BRK.B", "brk b": "BRK.B", "AAPL": "AAPL", "ABR.PRD": "ABR.PRD"}
        for typed, ibkr in cases.items():
            self.assertEqual(common.ibkr_symbol(typed), ibkr, typed)
        # with IBKR's table, a ticker IBKR really writes with a dash stays as it is
        self.assertEqual(common.ibkr_symbol("ODD-X", known={"ODD-X"}), "ODD-X")
        self.assertEqual(common.ibkr_symbol("C-P", known={"C.P"}), "C.P")

    def test_old_and_special_listings(self):
        for s in ("LU.OLD", "SRCI.OLD1", "AAGH.OLD2", "TCLIF.OL1", "KARUS.RS", "AGCI.RES"):
            self.assertEqual(common.listing_kind(s), "old_listing", s)
        for s in ("OABI.RSTD", "OABI.RST1", "IHD.CNT", "INTK.CONT", "HELM.TMP", "RQI.FRAC"):
            self.assertEqual(common.listing_kind(s), "ibkr_only", s)
        for s in ("AAPL", "BRK.B", "ABR.PRD", "AAC.U"):
            self.assertIsNone(common.listing_kind(s), s)

    def test_isin_check_digit_and_ibkr_masking(self):
        self.assertEqual(common.full_isin("KYG8T98W1237"), "KYG8T98W1237")
        self.assertEqual(common.full_isin("US0378331005"), "US0378331005")
        self.assertIsNone(common.full_isin("US0378331006"))               # wrong check digit
        self.assertIsNone(common.full_isin("XXXXXXX02014"))               # IBKR masks US ISINs


class SymbolMapTests(unittest.TestCase):
    def setUp(self):
        fake_yf.reset()
        self.now = [1_800_000_000.0]
        self.store = borrow_history.MemoryStore()
        self.map = common.SymbolMap(store=self.store, quotes=fake_yf.quotes, search=fake_yf.search,
                                    clock=lambda: self.now[0])

    def calls(self, kind):
        return fake_yf.CALLS[("*", kind)]

    def test_plain_tickers_need_no_lookup(self):
        r = self.map.resolve("AAPL")
        self.assertEqual((r.yahoo, r.how), ("AAPL", "same"))
        self.assertEqual(self.calls("quotes"), 0)

    def test_dotted_tickers_are_checked_once_and_remembered(self):
        out = self.map.resolve_many(["ABR.PRD", "AAC.U", "FULL"])
        self.assertEqual(out["ABR.PRD"], ("ABR-PD", "rule", ("ABR-PD",)))
        self.assertEqual(out["AAC.U"].yahoo, "AAC-UN")
        self.assertEqual(self.calls("quotes"), 1)                          # one request for all of them
        self.map.resolve_many(["ABR.PRD", "AAC.U"])
        self.assertEqual(self.calls("quotes"), 1)                          # remembered
        self.assertEqual(self.store.symbols["ABR.PRD"][:2], ("ABR-PD", "rule"))
        # a fresh process reads them back from the database
        again = common.SymbolMap(store=self.store, quotes=fake_yf.quotes, clock=lambda: self.now[0])
        self.assertEqual(again.resolve("ABR.PRD").yahoo, "ABR-PD")
        self.assertEqual(self.calls("quotes"), 1)
        # ...and check again after a week
        self.now[0] += common.SYMBOL_TTL + 1
        again2 = common.SymbolMap(store=self.store, quotes=fake_yf.quotes, clock=lambda: self.now[0])
        again2.resolve("ABR.PRD")
        self.assertEqual(self.calls("quotes"), 2)

    def test_isin_fallback_exact_matches_only(self):
        r = self.map.resolve("ODD.U", "KYG8T98W1237")                    # Yahoo's only US listing for that ISIN
        self.assertEqual((r.yahoo, r.how), ("ODDSU", "isin"))
        r = self.map.resolve("GONE.U", "KYG5235S1315")                   # several unrelated hits: no match
        self.assertEqual((r.yahoo, r.how), (None, "not_found"))
        self.assertEqual(common.listing_message(r),
                         "Not on Yahoo Finance; tried GONE-UN, GONE-U and ISIN KYG5235S1315")
        searches = self.calls("search")
        r = self.map.resolve("LOST.PRA", "XXXXXXXX1234")                 # masked ISIN: no search at all
        self.assertEqual(common.listing_message(r), "Not on Yahoo Finance; tried LOST-PA")
        self.assertEqual(self.calls("search"), searches)

    def test_old_listings_make_no_calls(self):
        r = self.map.resolve("LU.OLD")
        self.assertEqual((r.yahoo, r.how), (None, "old_listing"))
        self.assertEqual(common.listing_message(r), "An old or renamed IBKR listing; Yahoo doesn't carry it")
        self.assertEqual(self.calls("quotes"), 0)

    def test_yahoo_unreachable_isnt_remembered(self):
        def down(symbols):
            raise ConnectionError("down")
        m = common.SymbolMap(quotes=down)
        r = m.resolve("ABR.PRD")
        self.assertEqual((r.yahoo, r.how), ("ABR-PD", "unchecked"))      # best guess, asked again next time
        m.quotes = fake_yf.quotes
        self.assertEqual(m.resolve("ABR.PRD").how, "rule")


class FinraTests(unittest.TestCase):
    def test_parse(self):
        _, body = fake_finra_get(finra.FILE_URL.format("20260930"))
        rows, settled = finra.parse_file(body.decode())
        self.assertEqual(settled, "2026-09-30")
        self.assertEqual(rows["FUND"]["short"], 98476070)
        self.assertEqual(rows["FUND"]["adv"], 48804519)

    def test_candidate_dates(self):
        days = finra.candidate_dates(date(2026, 10, 24), 40)
        self.assertEqual(days[0], date(2026, 10, 15))
        self.assertIn(date(2026, 9, 30), days)
        self.assertNotIn(date(2026, 9, 20), days)
        self.assertTrue(all(d.weekday() < 5 for d in days))

    def test_lookup_latest_and_month_ago(self):
        f = loaded_finra()
        r = f.lookup("FUND")
        self.assertEqual((r["short"], r["date"], r["month_ago"], r["month_ago_date"]),
                         (98476070, "2026-09-30", 90000000, "2026-08-31"))
        # dotted tickers: FINRA drops the dot, so the company name must agree too
        self.assertEqual(f.lookup("ABR.PRD", "Arbor Realty Trust Inc")["short"], 1800)
        self.assertIsNone(f.lookup("BRK.B", "Berkshire Hathaway Inc-Cl B"))
        self.assertIsNone(f.lookup("NOPE"))


class FundEquivalents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        svc = fresh_service(finra=loaded_finra())
        rows = settle(svc, ["FUND", "THINTOP", "BOND", "FULL"], metrics.YAHOO_KEYS)
        cls.rows = {r["symbol"]: r for r in rows}

    def test_valuation(self):
        r = self.rows["FUND"]
        d, src = r["data"], r["src"]
        self.assertAlmostEqual(d["trailingPE"], 25.0)                       # Yahoo sends 1 / P/E
        self.assertAlmostEqual(d["priceToBook"], 5.0)
        self.assertAlmostEqual(d["priceToSalesTrailing12Months"], 4.0)
        self.assertEqual(src["trailingPE"], "fund")
        self.assertAlmostEqual(d["nav_premium"], 55.0 / 54.9 - 1)
        self.assertEqual((d["marketCap"], src["marketCap"]), (5.49e9, "fund"))

    def test_company_and_dividends(self):
        d = self.rows["FUND"]["data"]
        self.assertEqual((d["fundFamily"], d["category"]), ("Example Funds", "Large Blend"))
        self.assertEqual((d["expenseRatio"], d["netAssets"], d["fundInceptionDate"]), (0.09, 5.49e9, 1262563200))
        self.assertEqual(self.rows["FUND"]["why"]["holdingsCount"], "top10_only")
        self.assertAlmostEqual(d["distribution_rate"], 0.25 * 4 / 55.0)
        self.assertAlmostEqual(d["sharesOutstanding"], 5.49e9 / 54.9)

    def test_finra_short_interest(self):
        r = self.rows["FUND"]
        d, src = r["data"], r["src"]
        self.assertEqual((d["sharesShort"], src["sharesShort"]), (98476070, "finra"))
        self.assertEqual(r["note"]["sharesShort"], "as of 2026-09-30")      # "FINRA short interest, as of 2026-09-30"
        self.assertEqual(d["sharesShortPriorMonth"], 90000000)
        self.assertAlmostEqual(d["shortRatio"], 98476070 / 48804519)
        self.assertAlmostEqual(d["sharesPercentSharesOut"], 98476070 / (5.49e9 / 54.9))
        self.assertEqual(d["dateShortInterest"], int(datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp()))
        self.assertAlmostEqual(d["short_change"], 98476070 / 90000000 - 1)
        self.assertEqual(r["why"]["shortPercentOfFloat"], "fund")           # no float for a fund

    def test_look_through(self):
        r = self.rows["FUND"]
        d, src, note = r["data"], r["src"], r["note"]
        # holdings FULL 40%, PEER 20%, BANK 10%, NOCOV 5% of a fund that's 98% stocks
        self.assertAlmostEqual(d["forwardPE"], 0.6 / (0.4 / 18 + 0.2 / 9))     # weighted harmonic mean
        self.assertAlmostEqual(d["grossMargins"], (0.4 * 0.4 + 0.2 * 0.2) / 0.6)
        self.assertEqual(src["grossMargins"], "lookthrough")
        self.assertEqual(note["grossMargins"], "weighted by 61% of holdings")
        self.assertAlmostEqual(d["profitMargins"], 0.15)                      # only FULL reports it: 41%
        self.assertEqual(note["profitMargins"], "weighted by 41% of holdings")
        self.assertAlmostEqual(d["target_upside"], (0.4 * (120 / 101.5 - 1) + 0.2 * (44 / 40 - 1)) / 0.6)
        self.assertEqual(d["recommendationKey"], "buy")                       # from the weighted score

    def test_look_through_needs_30_percent(self):
        r = self.rows["THINTOP"]
        self.assertEqual(r["why"]["grossMargins"], "holdings_unavailable")
        self.assertEqual(r["note"]["grossMargins"], "data for 11% of holdings")

    def test_bond_fund_has_no_stocks(self):
        r = self.rows["BOND"]
        self.assertEqual(r["why"]["grossMargins"], "no_stocks")
        self.assertEqual(r["why"]["overallRisk"], "fund")

    def test_stocks_get_fund_only_reason(self):
        why = self.rows["FULL"]["why"]
        self.assertEqual({k for k in why}, metrics.FUND_ONLY)
        self.assertTrue(all(code == "fund_only" for code in why.values()))


class PreferredsUnitsAndReasons(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = fresh_service(finra=loaded_finra())
        rows = settle(cls.svc, ["ABR.PRD", "AAC.U", "LU.OLD", "GONE.U", "THIN"], metrics.YAHOO_KEYS)
        cls.rows = {r["symbol"]: r for r in rows}

    def test_preferred_uses_yahoo_spelling_and_issuer_facts(self):
        r = self.rows["ABR.PRD"]
        self.assertEqual(r["yahoo"], "ABR-PD")
        self.assertIsNone(r["error"])
        self.assertEqual(r["data"]["currentPrice"], 11.55)
        self.assertEqual((r["data"]["sector"], r["src"]["sector"]), ("Real Estate", "issuer"))
        self.assertEqual(r["note"]["sector"], "From ABR, the issuer's common stock")
        self.assertEqual(r["data"]["overallRisk"], 7)
        self.assertEqual(r["data"]["sharesShort"], 1687)                    # Yahoo first; FINRA only fills gaps

    def test_unit_and_foreign_issuer(self):
        r = self.rows["AAC.U"]
        self.assertEqual((r["yahoo"], r["data"]["currentPrice"]), ("AAC-UN", 10.05))
        self.assertEqual(r["data"]["country"], "Cayman Islands")
        self.assertEqual(r["why"]["overallRisk"], "iss_us_only")

    def test_issuer_facts_need_matching_names(self):
        rows = settle(self.svc, ["MIX.U"], ["sector", "currentPrice"])
        self.assertEqual(rows[0]["data"]["currentPrice"], 10.0)
        self.assertNotEqual(rows[0]["data"].get("sector"), "Technology")     # MIX is another company
        self.assertIn("sector", rows[0]["why"])

    def test_no_fund_data_at_yahoo_is_an_answer(self):
        d = metrics.fetch_funds(fake_yf, "FULL")                             # Yahoo: "No Fund data found"
        self.assertEqual((d["ratios"], d["top"]), ({}, []))

    def test_rows_yahoo_doesnt_carry(self):
        old = self.rows["LU.OLD"]
        self.assertEqual((old["error"], old["error_kind"]),
                         ("An old or renamed IBKR listing; Yahoo doesn't carry it", "listing"))
        self.assertEqual(old["data"], {})
        gone = self.rows["GONE.U"]
        self.assertEqual(gone["error"], "Not on Yahoo Finance; tried GONE-UN, GONE-U and ISIN KYG5235S1315")
        self.assertNotIn(("LU.OLD", "info"), fake_yf.CALLS)
        self.assertNotIn(("LU-OLD", "info"), fake_yf.CALLS)

    def test_every_blank_cell_has_a_reason_of_a_known_kind(self):
        for sym, r in self.rows.items():
            if r["error"]:
                continue
            self.assertEqual(r["loading"], [], sym)
            self.assertEqual(set(r["data"]) | set(r["why"]), set(metrics.YAHOO_KEYS), sym)
            for code in r["why"].values():
                self.assertIn(metrics.REASONS[code][0], ("na", "none", "temp"), code)

    def test_analysts_cover_the_common_stock(self):
        why = self.rows["ABR.PRD"]["why"]
        self.assertEqual(why["targetMeanPrice"], "common_only")
        self.assertEqual(metrics.REASONS["common_only"][0], "na")

    def test_yahoos_none_rating_is_no_rating(self):
        info = {"info": {"quoteType": "EQUITY", "recommendationKey": "none", "longName": "X"}, "empty": False}
        c = metrics.Ctx("X", lambda source, sym: info if source == "info" else {"_error": "x", "nodata": True})
        self.assertIsNone(c.v("recommendationKey"))

    def test_growth_from_a_negative_base_doesnt_apply(self):
        svc = fresh_service()
        import pandas as pd
        q = [pd.Timestamp(d) for d in ("2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30")]
        rows_ = {"totalrevenue": [(d, 100.0) for d in q], "netincome": [(q[0], 5.0)] + [(d, -3.0) for d in q[1:]]}
        svc.cache["stmt_qi"]["NOCOV"] = (time.time(), {"rows": rows_})
        rows = settle(svc, ["NOCOV"], ["earningsQuarterlyGrowth", "earningsGrowth", "revenueGrowth"])
        r = rows[0]
        self.assertEqual(r["why"]["earningsQuarterlyGrowth"], "neg_base")          # -3 a year ago
        self.assertEqual(r["why"]["earningsGrowth"], "no_data")                    # no EPS rows at all
        self.assertEqual(r["data"]["revenueGrowth"], 0.0)

    def test_finra_fills_short_interest_for_stocks(self):
        d, src = self.rows["THIN"]["data"], self.rows["THIN"]["src"]
        self.assertEqual((d["sharesShort"], src["sharesShort"]), (2_000_000, "finra"))
        self.assertEqual(d["sharesShortPriorMonth"], 1_500_000)
        self.assertAlmostEqual(d["shortRatio"], 5.0)
        self.assertEqual(self.rows["THIN"]["why"]["overallRisk"], "no_iss")    # a US company ISS skipped


class LoadingOrder(unittest.TestCase):
    def test_prefetch_fills_the_cache_on_the_background_workers(self):
        svc = fresh_service()
        rows, pending = svc.get(["FULL", "THIN"], ["currentPrice", "trailingPE"], prefetch=True)
        self.assertEqual(pending, 0)
        self.assertIsNotNone(svc._peek("hist", "THIN"))
        self.assertIsNotNone(svc._peek("stmt_qi", "THIN"))             # the second layer too
        self.assertEqual(svc.fg_waiting, 0)

    def test_prefetch_leaves_room_only_while_the_open_tab_waits(self):
        svc = fresh_service()
        self.assertEqual(svc._reserve(), 0)
        svc.fg_waiting = 1
        self.assertEqual(svc._reserve(), int(common.YAHOO.per_minute * metrics.PREFETCH_SHARE))
        svc.fg_waiting = 0
        rl = common.RateLimiter(10)
        for _ in range(6):
            self.assertTrue(rl.try_acquire(lambda: 4))
        self.assertFalse(rl.try_acquire(lambda: 4))                   # 4 of 10 kept free
        self.assertTrue(rl.try_acquire(lambda: 0))

    def test_open_tab_takes_over_a_queued_prefetch(self):
        svc = fresh_service()
        self.addCleanup(setattr, metrics, "WAIT_SEC", metrics.WAIT_SEC)
        metrics.WAIT_SEC = 0.3
        gate = __import__("threading").Event()
        svc.bg.submit(gate.wait)                    # keep the prefetch workers busy
        svc.bg.submit(gate.wait)
        svc.get(["FULL"], ["currentPrice"], prefetch=True)
        fut, prio = svc.inflight[("info", "FULL")]
        self.assertEqual(prio, metrics.PREFETCH)
        rows, pending = svc.get(["FULL"], ["currentPrice"])        # the visitor's request doesn't wait
        gate.set()
        self.assertEqual(pending, 0)
        self.assertEqual(rows[0]["data"]["currentPrice"], 101.5)
        self.assertTrue(fut.cancelled())

    def test_retry_forgets_failures(self):
        svc = fresh_service()
        svc.cache["stmt_qb"]["THIN"] = (time.time(), {"_error": "failed (ConnectionError)"})
        svc.get(["THIN"], ["currentRatio"], retry=["THIN"])
        self.assertNotIn("_error", svc._peek("stmt_qb", "THIN") or {"_error": 1})

    def test_no_data_answers_arent_retried(self):
        svc = fresh_service()
        svc.fetchers["stmt_qi"] = lambda yf, s: (_ for _ in ()).throw(
            RuntimeError('HTTP Error 404: {"error":{"description":"No fundamentals data found for symbol"}}'))
        svc.fetchers["stmt_ai"] = svc.fetchers["stmt_qi"]
        rows = settle(svc, ["NOCOV"], ["trailingEps", "mostRecentQuarter"])
        self.assertEqual(rows[0]["why"]["mostRecentQuarter"], "no_data")         # "–", not "retrying…"
        svc.fetchers["stmt_qi"] = lambda yf, s: (_ for _ in ()).throw(ConnectionError("reset"))
        svc.forget("NOCOV")
        rows = settle(svc, ["NOCOV"], ["mostRecentQuarter"])
        self.assertEqual(rows[0]["why"]["mostRecentQuarter"], "unreachable")     # a real failure is retried

    def test_prefetch_never_caches_busy_answers(self):
        svc = fresh_service()
        svc._run("info", "NOSUCH-X", metrics.PREFETCH)            # unknown: an ordinary empty answer is cached
        self.assertIsNotNone(svc._peek("info", "NOSUCH-X"))
        orig = svc._fetch
        svc._fetch = lambda source, sym: (_ for _ in ()).throw(common.YahooBusy("busy"))
        svc._run("info", "FULL", metrics.PREFETCH)
        self.assertIsNone(svc._peek("info", "FULL"))
        svc._run("info", "FULL", metrics.FOREGROUND)
        self.assertTrue(svc._peek("info", "FULL").get("busy"))
        svc._fetch = orig


class EtfLookThroughWeights(unittest.TestCase):
    def test_weights_from_priced_rows(self):
        import etf

        class Borrow:
            def ensure_fresh(self):
                pass

            def snapshot(self):
                return ({"AAA": {"fee": 0.3, "shares": 1000, "name": "Aaa Corp"},
                         "BBB": {"fee": 0.5, "shares": 1000, "name": "Bbb Corp"}}, [], "", "", False)
        svc = etf.EtfService(None, Borrow())
        hold = {"source": "Yahoo top 10", "as_of": None, "complete": False, "net_assets": None,
                "holdings": [{"name": "Aaa Corp", "ticker": "AAA", "pct": 0.3, "yahoo": True},
                             {"name": "Bbb Corp", "ticker": "BBB", "pct": 0.2, "yahoo": True},
                             {"name": "Everything outside the top 10", "pct": 0.5, "undisclosed": True}]}
        svc.cache["hold"]["XYZ"] = (time.time(), hold)
        w = svc.lookthrough_weights("XYZ")
        self.assertEqual(w["weights"], [("AAA", 0.3), ("BBB", 0.2)])
        self.assertAlmostEqual(w["equity"], 1.0)               # the undisclosed rest counts, so coverage is honest
        self.assertIsNone(w["count"])


if __name__ == "__main__":
    unittest.main()
