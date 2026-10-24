"""A stand-in for the yfinance package, built from small hand-made data.

Profiles:
  FULL   Yahoo's .info has every field
  THIN   .info has almost nothing, so values must come from the fallbacks
  FUND   an ETF
  BANK   a bank (no gross margin, no current ratio)
  NOPAY  pays no dividend and never split
  IPO    listed four months ago
  NOCOV  no analyst coverage
  FAIL   .info fails; everything else works
  SPY    the benchmark, for beta
  ABR-PD a preferred (IBKR: ABR.PRD) whose company facts come from ABR, the issuer
  AAC-UN a SPAC unit (IBKR: AAC.U); AAC, its Class A shares, is a Cayman company
  PEER   a holding of FUND (look-through)
  FUND   also has Yahoo fund data: multiples, costs, sectors, top holdings
  THINTOP a fund whose known holdings cover only 10% of it
  BOND   a bond fund (no stocks)
Every attribute access that would be a network request is counted in CALLS.
quotes() and search() stand in for Yahoo's quote list and ISIN search (common.SymbolMap).
"""
from collections import Counter

import numpy as np
import pandas as pd

TZ = "America/New_York"
END = pd.Timestamp("2026-10-21")
CALLS = Counter()


def _bars(start, seed, drift=0.0004, beta=1.0, market=None, divs=None, splits=None):
    idx = pd.bdate_range(start, END)
    r = np.random.default_rng(seed)
    rets = drift + (beta * market[-len(idx):] if market is not None else r.normal(0, 0.01, len(idx))) \
        + r.normal(0, 0.004, len(idx))
    close = 50 * np.cumprod(1 + rets)
    df = pd.DataFrame({
        "Open": close * 0.995, "High": close * 1.01, "Low": close * 0.99, "Close": close,
        "Volume": (1e6 + 1e5 * r.random(len(idx))).round(),
        "Dividends": 0.0, "Stock Splits": 0.0}, index=idx)
    for d, v in (divs or {}).items():
        df.loc[df.index[df.index >= pd.Timestamp(d)][0], "Dividends"] = v
    for d, v in (splits or {}).items():
        df.loc[df.index[df.index >= pd.Timestamp(d)][0], "Stock Splits"] = v
    df["Adj Close"] = close * adjust_factor(df["Close"], df["Dividends"])
    df.index = df.index.tz_localize(TZ)
    return df


def adjust_factor(close, dividends):
    """Yahoo's dividend adjustment: every close before an ex-date is multiplied by
    (1 - dividend / the close the day before the ex-date)."""
    factor = np.ones(len(close))
    c, d = close.to_numpy(), dividends.to_numpy()
    for i in np.nonzero(d)[0]:
        if i > 0:
            factor[:i] *= 1 - d[i] / c[i - 1]
    return factor


MARKET = np.random.default_rng(99).normal(0.0004, 0.01, len(pd.bdate_range("2000-01-03", END)))
QUARTERLY_DIVS = {f"{y}-{m:02d}-10": 0.25 for y in range(2019, 2027) for m in (2, 5, 8, 11)
                  if pd.Timestamp(f"{y}-{m:02d}-10") <= END}


def _statements():
    q = [pd.Timestamp(d) for d in ("2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30")]
    qi = pd.DataFrame({
        q[0]: [1100, 440, 220, 300, 165, 160, 1.65, 1.70],
        q[1]: [1000, 400, 200, 280, 150, 150, 1.50, 1.55],
        q[2]: [1050, 420, 210, 290, 155, 155, 1.55, 1.60],
        q[3]: [950, 380, 190, 260, 140, 140, 1.40, 1.45],
        q[4]: [1000, 400, 200, 270, 150, 145, 1.50, 1.52],
    }, index=["Total Revenue", "Gross Profit", "Operating Income", "EBITDA", "Net Income",
              "Net Income Common Stockholders", "Diluted EPS", "Basic EPS"]) * 1.0
    qi.loc[["Total Revenue", "Gross Profit", "Operating Income", "EBITDA", "Net Income",
            "Net Income Common Stockholders"]] *= 1e6
    qb = pd.DataFrame({
        q[0]: [10000, 4000, 1500, 1200, 3000, 1000, 300, 800],
        q[1]: [9800, 3900, 1500, 1100, 2900, 950, 280, 790],
        q[2]: [9700, 3850, 1500, 1050, 2800, 900, 270, 780],
        q[3]: [9600, 3800, 1500, 1000, 2700, 900, 260, 770],
        q[4]: [9000, 3600, 1600, 900, 2600, 850, 250, 760],
    }, index=["Total Assets", "Stockholders Equity", "Total Debt", "Cash Cash Equivalents And Short Term Investments",
              "Current Assets", "Current Liabilities", "Inventory", "Cash And Cash Equivalents"]) * 1e6
    qc = pd.DataFrame({d: [200e6, -50e6, 150e6] for d in q[:4]},
                      index=["Operating Cash Flow", "Capital Expenditure", "Free Cash Flow"])
    ai = pd.DataFrame({pd.Timestamp("2025-12-31"): [4000e6, 600e6, 6.0],
                       pd.Timestamp("2024-12-31"): [3800e6, 560e6, 5.6]},
                      index=["Total Revenue", "Net Income", "Diluted EPS"])
    return qi, qb, qc, ai


FULL_INFO = {
    "quoteType": "EQUITY", "longName": "Full Data Inc", "sector": "Technology", "industry": "Software",
    "country": "United States", "city": "Austin", "fullTimeEmployees": 1234, "exchange": "NMS",
    "currency": "USD", "website": "https://example.com", "marketState": "REGULAR",
    "shortPercentOfFloat": 0.05, "sharesShort": 5e6, "sharesShortPriorMonth": 4e6, "shortRatio": 2.5,
    "sharesPercentSharesOut": 0.045, "floatShares": 1e8, "sharesOutstanding": 1.1e8,
    "impliedSharesOutstanding": 1.12e8, "heldPercentInsiders": 0.02, "heldPercentInstitutions": 0.7,
    "dateShortInterest": 1790000000,
    "currentPrice": 101.5, "previousClose": 100.0, "open": 100.5, "dayLow": 99.8, "dayHigh": 102.0,
    "bid": 101.4, "ask": 101.6, "bidSize": 300, "askSize": 400, "preMarketPrice": 100.9,
    "postMarketPrice": 101.7, "regularMarketVolume": 2e6, "averageVolume": 1.5e6,
    "averageVolume10days": 1.6e6, "fiftyTwoWeekLow": 80.0, "fiftyTwoWeekHigh": 120.0,
    "fiftyTwoWeekLowChangePercent": 0.26875, "fiftyTwoWeekHighChangePercent": -0.154,
    "fiftyDayAverage": 98.0, "twoHundredDayAverage": 95.0, "fiftyDayAverageChangePercent": 0.0357,
    "twoHundredDayAverageChangePercent": 0.068, "beta": 1.1,
    "marketCap": 1.1e10, "enterpriseValue": 1.15e10, "trailingPE": 20.0, "forwardPE": 18.0,
    "trailingPegRatio": 1.5, "priceToBook": 3.0, "priceToSalesTrailing12Months": 2.7,
    "enterpriseToRevenue": 2.8, "enterpriseToEbitda": 10.0, "bookValue": 33.0, "trailingEps": 5.0,
    "forwardEps": 5.6, "epsCurrentYear": 5.3, "grossMargins": 0.4, "operatingMargins": 0.2,
    "ebitdaMargins": 0.27, "profitMargins": 0.15, "returnOnAssets": 0.06, "returnOnEquity": 0.16,
    "revenueGrowth": 0.1, "earningsGrowth": 0.12, "earningsQuarterlyGrowth": 0.1, "totalRevenue": 4.1e9,
    "revenuePerShare": 37.0, "grossProfits": 1.6e9, "ebitda": 1.1e9, "netIncomeToCommon": 6e8,
    "operatingCashflow": 8e8, "freeCashflow": 6e8, "totalCash": 1.2e9, "totalCashPerShare": 10.9,
    "totalDebt": 1.5e9, "debtToEquity": 37.5, "currentRatio": 3.0, "quickRatio": 2.7,
    "dividendRate": 1.0, "trailingAnnualDividendRate": 1.0, "trailingAnnualDividendYield": 0.01,
    "fiveYearAvgDividendYield": 1.1, "payoutRatio": 0.2, "exDividendDate": 1790000000,
    "lastDividendValue": 0.25, "lastDividendDate": 1790000000, "recommendationKey": "buy",
    "recommendationMean": 2.0, "averageAnalystRating": "2.0 - Buy", "numberOfAnalystOpinions": 20,
    "targetMeanPrice": 120.0, "targetMedianPrice": 118.0, "targetHighPrice": 150.0, "targetLowPrice": 90.0,
    "earningsTimestamp": 1793000000, "mostRecentQuarter": 1782777600, "lastFiscalYearEnd": 1767139200,
    "nextFiscalYearEnd": 1798675200, "lastSplitFactor": "2:1", "lastSplitDate": 1600000000,
    "firstTradeDateMilliseconds": 946900000000, "overallRisk": 3, "auditRisk": 2, "boardRisk": 4,
    "compensationRisk": 5, "shareHolderRightsRisk": 6,
}

PROFILES = {
    "FULL": dict(info=FULL_INFO, start="2000-01-03", beta=1.1, divs=QUARTERLY_DIVS, splits={"2020-08-31": 4.0}),
    "THIN": dict(info={"quoteType": "EQUITY", "longName": "Thin Data Corp", "sector": "Technology",
                       "industry": "Software", "marketState": "CLOSED", "bid": 0.0, "ask": 0.0,
                       "bidSize": 0, "askSize": 0},
                 start="2015-01-02", beta=1.5, divs=QUARTERLY_DIVS, splits={"2021-06-01": 3.0, "2012-01-01": 2.0}),
    "FUND": dict(info={"quoteType": "ETF", "longName": "Some Index ETF", "category": "Large Blend",
                       "fundFamily": "Example Funds", "yield": 0.0125, "regularMarketPrice": 55.0,
                       "beta3Year": 1.0, "marketState": "REGULAR", "navPrice": 54.9, "totalAssets": 5.49e9,
                       "netExpenseRatio": 0.09, "fundInceptionDate": 1262563200},
                 start="2010-01-04", beta=1.0, divs=QUARTERLY_DIVS,
                 funds=dict(pe=0.04, pb=0.2, ps=0.25, expense=0.0009, stocks=0.98,
                            sectors={"technology": 0.34, "healthcare": 0.12},
                            top=[("FULL", 0.40), ("PEER", 0.20), ("BANK", 0.10), ("NOCOV", 0.05)])),
    "THINTOP": dict(info={"quoteType": "ETF", "longName": "Thin Holdings ETF", "regularMarketPrice": 20.0,
                          "marketState": "REGULAR"},
                    start="2012-01-03", beta=1.0,
                    funds=dict(stocks=0.95, top=[("FULL", 0.10), ("NOCOV", 0.10)])),
    "BOND": dict(info={"quoteType": "ETF", "longName": "Long Treasury Bond ETF", "regularMarketPrice": 80.0,
                       "navPrice": 80.1, "marketState": "REGULAR"},
                 start="2003-01-02", beta=0.0001, divs={f"2026-{m:02d}-01": 0.25 for m in range(1, 11)},
                 funds=dict(stocks=0.0, top=[])),
    "PEER": dict(info={"quoteType": "EQUITY", "longName": "Peer Holding Co", "marketState": "REGULAR",
                       "currentPrice": 40.0, "trailingPE": 10.0, "forwardPE": 9.0, "grossMargins": 0.2,
                       "targetMeanPrice": 44.0},
                 start="2005-01-03", beta=1.0),
    "ABR": dict(info={"quoteType": "EQUITY", "longName": "Arbor Realty Trust, Inc.", "sector": "Real Estate",
                      "industry": "REIT - Mortgage", "country": "United States", "city": "Uniondale",
                      "fullTimeEmployees": 650, "website": "https://arbor.com", "marketState": "REGULAR",
                      "currentPrice": 3.0, "overallRisk": 7, "auditRisk": 5, "boardRisk": 6, "compensationRisk": 8,
                      "shareHolderRightsRisk": 4, "profitMargins": 0.3},
                start="2004-04-07", beta=1.4, divs=QUARTERLY_DIVS),
    "ABR-PD": dict(info={"quoteType": "EQUITY", "longName": "Arbor Realty Trust 6.375% Series D Preferred",
                         "marketState": "REGULAR", "currentPrice": 11.55, "sharesShort": 1687,
                         "dateShortInterest": 1790726400},
                   start="2021-06-01", beta=0.5, divs=QUARTERLY_DIVS),
    "AAC": dict(info={"quoteType": "EQUITY", "longName": "Ares Acquisition Corporation III", "sector": "Financial Services",
                      "industry": "Shell Companies", "country": "Cayman Islands", "marketState": "REGULAR",
                      "currentPrice": 9.95},
                start="2025-03-03", beta=0.1),
    "AAC-UN": dict(info={"quoteType": "EQUITY", "longName": "Ares Acquisition Corporation III Units",
                         "marketState": "REGULAR", "currentPrice": 10.05},
                   start="2025-01-15", beta=0.1),
    # a unit whose base ticker on Yahoo is a different company
    "MIX-UN": dict(info={"quoteType": "EQUITY", "longName": "Mixup Acquisition Corp Units", "marketState": "REGULAR",
                         "currentPrice": 10.0},
                   start="2025-05-01", beta=0.1),
    "MIX": dict(info={"quoteType": "EQUITY", "longName": "Mixed Signals Inc", "sector": "Technology",
                      "country": "United States", "marketState": "REGULAR", "currentPrice": 7.0},
                start="2010-01-04", beta=1.0),
    "BANK": dict(info={"quoteType": "EQUITY", "longName": "Example Bancorp", "sector": "Financial Services",
                       "industry": "Banks - Regional", "marketState": "REGULAR", "currentPrice": 40.0},
                 start="2000-01-03", beta=1.2, divs=QUARTERLY_DIVS, bank=True),
    "NOPAY": dict(info={"quoteType": "EQUITY", "longName": "No Payout Inc", "marketState": "REGULAR",
                        "currentPrice": 30.0, "trailingAnnualDividendRate": 0.0, "payoutRatio": 0.0},
                  start="2012-01-03", beta=1.3),
    "IPO": dict(info={"quoteType": "EQUITY", "longName": "Newly Listed Co", "marketState": "REGULAR"},
                start="2026-06-20", beta=1.0),
    "NOCOV": dict(info={"quoteType": "EQUITY", "longName": "Tiny Cap Inc", "marketState": "REGULAR"},
                  start="2008-01-02", beta=0.8, analysts=False),
    "FAIL": dict(info=None, start="2005-01-03", beta=1.0, divs=QUARTERLY_DIVS),
    "SPY": dict(info={"quoteType": "ETF", "longName": "SPDR S&P 500 ETF Trust", "category": "Large Blend",
                      "fundFamily": "State Street Investment Management", "navPrice": 759.86, "totalAssets": 8.09e11,
                      "netExpenseRatio": 0.0945, "fundInceptionDate": 727660800, "yield": 0.0099,
                      "marketState": "REGULAR"},
                start="2000-01-03", beta=1.0, divs=QUARTERLY_DIVS,
                funds=dict(pe=0.04132, pb=0.19034, ps=0.27459, expense=0.000945, stocks=0.9975,
                           sectors={"technology": 0.34, "financial_services": 0.13, "healthcare": 0.1},
                           top=[("AAPL", 0.0735), ("BRK-B", 0.016), ("FULL", 0.2), ("PEER", 0.1)])),
    # browser-check names (see tools/coverage_report.py for the live version)
    "AAPL": dict(info={**FULL_INFO, "longName": "Apple Inc.", "industry": "Consumer Electronics",
                       "city": "Cupertino", "fullTimeEmployees": 166000, "website": "https://www.apple.com"},
                 start="2000-01-03", beta=1.2, divs=QUARTERLY_DIVS, splits={"2020-08-31": 4.0}),
    "BRK-B": dict(info={"quoteType": "EQUITY", "longName": "Berkshire Hathaway Inc.", "sector": "Financial Services",
                        "industry": "Insurance - Diversified", "country": "United States", "city": "Omaha",
                        "marketState": "REGULAR", "currentPrice": 542.19, "trailingPE": 13.6, "sharesShort": 12370081,
                        "shortPercentOfFloat": 0.0099, "sharesOutstanding": 1408035161, "overallRisk": 9, "auditRisk": 7,
                        "boardRisk": 9, "compensationRisk": 2, "shareHolderRightsRisk": 6, "profitMargins": 0.18},
                  start="2000-01-03", beta=0.9),
    "TCEHY": dict(info={"quoteType": "EQUITY", "longName": "Tencent Holdings Limited", "sector": "Communication Services",
                        "industry": "Internet Content & Information", "country": "China", "exchange": "PNK",
                        "marketState": "REGULAR", "currentPrice": 78.2, "trailingPE": 22.0, "grossMargins": 0.55},
                  start="2008-01-02", beta=0.8, divs={f"{y}-05-20": 0.5 for y in range(2019, 2027)}),
    "SOXL": dict(info={"quoteType": "ETF", "longName": "Direxion Daily Semiconductor Bull 3X Shares",
                       "category": "Trading--Leveraged Equity", "fundFamily": "Direxion Funds", "navPrice": 83.57,
                       "totalAssets": 2.4e10, "netExpenseRatio": 0.75, "fundInceptionDate": 1268265600,
                       "yield": 0.0006, "marketState": "REGULAR"},
                 start="2010-03-11", beta=3.0, divs={"2025-12-20": 0.05, "2026-06-20": 0.02},
                 funds=dict(pe=0.02993, pb=0.09503, ps=0.08311, expense=0.0075, stocks=0.2236,
                            sectors={"technology": 1.0}, top=[])),
}


class FastInfo:
    def __init__(self, t):
        self.t = t

    @property
    def last_price(self):
        CALLS[(self.t.sym, "fast_info")] += 1
        return float(self.t._daily()["Close"].iloc[-1])

    @property
    def previous_close(self):
        return float(self.t._daily()["Close"].iloc[-2])


class FundsData:
    def __init__(self, t):
        self.t, self.f = t, t.p.get("funds") or {}

    def _hit(self):
        CALLS[(self.t.sym, "funds_data")] += 1

    @property
    def fund_overview(self):
        self._hit()
        i = self.t.p["info"]
        return {"categoryName": i.get("category"), "family": i.get("fundFamily"), "legalType": "Exchange Traded Fund"}

    @property
    def fund_operations(self):
        return pd.DataFrame({self.t.sym: [self.f.get("expense"), 0.03, 1000.0]},
                            index=["Annual Report Expense Ratio", "Annual Holdings Turnover", "Total Net Assets"])

    @property
    def equity_holdings(self):
        return pd.DataFrame({self.t.sym: [self.f.get("pe", 0.0), self.f.get("pb", 0.0), self.f.get("ps", 0.0), 0.0, None, None]},
                            index=["Price/Earnings", "Price/Book", "Price/Sales", "Price/Cashflow", "Median Market Cap",
                                   "3 Year Earnings Growth"])

    @property
    def sector_weightings(self):
        return dict(self.f.get("sectors") or {})

    @property
    def asset_classes(self):
        stocks = self.f.get("stocks", 0.0)
        return {"cashPosition": 0.01, "stockPosition": stocks, "bondPosition": max(0.0, 0.99 - stocks)}

    @property
    def top_holdings(self):
        top = self.f.get("top") or []
        return pd.DataFrame({"Name": [f"{s} holding" for s, _ in top], "Holding Percent": [w for _, w in top]},
                            index=pd.Index([s for s, _ in top], name="Symbol"))


class Ticker:
    def __init__(self, sym):
        self.sym = sym
        self.p = PROFILES.get(sym)
        self.history_metadata = {}

    def _daily(self):
        p = self.p
        mk = MARKET if p["beta"] else None
        return _bars(p["start"], sum(map(ord, self.sym)), beta=p["beta"], market=mk,
                     divs=p.get("divs"), splits=p.get("splits"))

    @property
    def info(self):
        CALLS[(self.sym, "info")] += 1
        if self.p is None:
            return {"trailingPegRatio": None}
        if self.p["info"] is None:
            raise RuntimeError("HTTP Error 500: Internal Server Error")
        return dict(self.p["info"])

    @property
    def fast_info(self):
        return FastInfo(self)

    @property
    def funds_data(self):
        if self.p is None or not self.p.get("funds"):
            raise RuntimeError("No Fund data found")
        return FundsData(self)

    def history(self, period="1mo", interval="1d", auto_adjust=True, actions=True, prepost=False, **kw):
        CALLS[(self.sym, f"history:{interval}")] += 1
        if self.p is None:
            return pd.DataFrame()
        if interval == "1d":
            df = self._daily()
            self.history_metadata = {
                "currency": "USD", "exchangeName": "NMS", "instrumentType": "EQUITY" if self.sym != "FUND" else "ETF",
                "regularMarketPrice": float(df["Close"].iloc[-1]),
                "regularMarketTime": pd.Timestamp("2026-10-21 16:00", tz=TZ),
                "firstTradeDate": df.index[0], "exchangeTimezoneName": TZ, "longName": self.p["info"] and
                self.p["info"].get("longName") or "Fail Corp"}
            if auto_adjust:                    # like yfinance: prices become the adjusted ones
                ratio = df["Adj Close"] / df["Close"]
                for col in ("Open", "High", "Low", "Close"):
                    df[col] = df[col] * ratio
                df = df.drop(columns=["Adj Close"])
            return df
        # 5-minute bars with pre- and after-hours for the last two days
        idx = []
        for day in ("2026-10-20", "2026-10-21"):
            idx += list(pd.date_range(f"{day} 04:00", f"{day} 19:55", freq="5min", tz=TZ))
        df = pd.DataFrame({"Close": np.linspace(100, 102, len(idx))}, index=pd.DatetimeIndex(idx))
        self.history_metadata = {"currentTradingPeriod": {"regular": {
            "start": pd.Timestamp("2026-10-21 09:30", tz=TZ), "end": pd.Timestamp("2026-10-21 16:00", tz=TZ)}}}
        return df

    def _stmt(self, i):
        CALLS[(self.sym, "statements")] += 1
        frames = _statements()
        df = frames[i].copy()
        if self.p.get("bank"):
            df = df.drop(index=[r for r in ("Gross Profit", "EBITDA", "Current Assets", "Current Liabilities",
                                            "Inventory") if r in df.index])
        return df

    @property
    def quarterly_income_stmt(self):
        return self._stmt(0)

    @property
    def quarterly_balance_sheet(self):
        return self._stmt(1)

    @property
    def quarterly_cashflow(self):
        return self._stmt(2)

    @property
    def income_stmt(self):
        return self._stmt(3)

    @property
    def analyst_price_targets(self):
        CALLS[(self.sym, "analysts")] += 1
        if self.p.get("analysts") is False:
            return {}
        return {"current": 100.0, "high": 140.0, "low": 80.0, "mean": 110.0, "median": 108.0}

    @property
    def recommendations_summary(self):
        if self.p.get("analysts") is False:
            return pd.DataFrame()
        return pd.DataFrame([{"period": "0m", "strongBuy": 5, "buy": 10, "hold": 4, "sell": 1, "strongSell": 0},
                             {"period": "-1m", "strongBuy": 4, "buy": 10, "hold": 5, "sell": 1, "strongSell": 0}])

    @property
    def earnings_estimate(self):
        if self.p.get("analysts") is False:
            return pd.DataFrame()
        return pd.DataFrame({"avg": [1.7, 1.8, 6.6, 7.4]}, index=pd.Index(["0q", "+1q", "0y", "+1y"], name="period"))

    @property
    def major_holders(self):
        CALLS[(self.sym, "holders")] += 1
        return pd.DataFrame({"Value": [0.031, 0.655, 0.68, 1500]},
                            index=["insidersPercentHeld", "institutionsPercentHeld",
                                   "institutionsFloatPercentHeld", "institutionsCount"])

    @property
    def calendar(self):
        CALLS[(self.sym, "calendar")] += 1
        return {"Earnings Date": [pd.Timestamp("2026-11-05").date()],
                "Ex-Dividend Date": pd.Timestamp("2026-11-10").date()}

    def get_shares_full(self, start=None, end=None):
        CALLS[(self.sym, "shares")] += 1
        return pd.Series([1.05e8, 1.0e8], index=pd.to_datetime(["2026-03-01", "2026-09-01"]))


# IBKR's file entries (name, ISIN; US ISINs are masked like IBKR's), by IBKR ticker
IBKR = {
    "ABR.PRD": {"name": "Arbor Realty Trust Inc", "isin": "XXXXXXX38769"},
    "AAC.U": {"name": "Ares Acquisition Corp Iii", "isin": "KYG3303U1123"},
    "ODD.U": {"name": "Odd Spelling Acquisition", "isin": "KYG8T98W1237"},
    "GONE.U": {"name": "Gone Acquisition Corp", "isin": "KYG5235S1315"},
    "LOST.PRA": {"name": "Lost Preferred Co", "isin": "XXXXXXXX1234"},
    "FUND": {"name": "Some Index Etf", "isin": "XXXXXXX12345"},
    "MIX.U": {"name": "Mixup Acquisition Corp", "isin": "XXXXXXX99999"},
}
SEARCH = {"KYG8T98W1237": [{"symbol": "ODDSU", "quoteType": "EQUITY", "exchange": "NYQ", "shortname": "Odd"}],
          "KYG5235S1315": [{"symbol": "GONE.L", "quoteType": "EQUITY", "exchange": "LSE"},
                           {"symbol": "XYZ", "quoteType": "EQUITY", "exchange": "NYQ"},
                           {"symbol": "XYZW", "quoteType": "EQUITY", "exchange": "NYQ"}]}
EXTRA_QUOTES = {"ODDSU"}           # listed on Yahoo, but only findable by ISIN


def ibkr_entry(sym):
    return IBKR.get(sym)


def quotes(symbols):
    """Yahoo's quote list: real listings get a quote; unknown ones come back empty, like Yahoo's
    "quoteType NONE" answers."""
    CALLS[("*", "quotes")] += 1
    out = {}
    for s in symbols:
        p = PROFILES.get(s)
        if p or s in EXTRA_QUOTES:
            info = (p or {}).get("info") or {}
            out[s] = {"symbol": s, "quoteType": info.get("quoteType", "EQUITY"), "exchange": "NYQ",
                      "regularMarketPrice": 10.0, "longName": info.get("longName", s)}
        else:
            out[s] = {"symbol": s, "quoteType": "NONE", "exchange": None}
    return out


def search(isin):
    CALLS[("*", "search")] += 1
    return list(SEARCH.get(isin, []))


def reset():
    CALLS.clear()
