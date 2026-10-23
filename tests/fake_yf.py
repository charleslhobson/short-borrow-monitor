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
Every attribute access that would be a network request is counted in CALLS.
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
                       "beta3Year": 1.0, "marketState": "REGULAR"},
                 start="2010-01-04", beta=1.0, divs=QUARTERLY_DIVS),
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
    "SPY": dict(info={"quoteType": "ETF", "longName": "SPDR S&P 500 ETF Trust"}, start="2000-01-03", beta=1.0),
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
            raise RuntimeError("HTTP Error 404: quote not found")
        return dict(self.p["info"])

    @property
    def fast_info(self):
        return FastInfo(self)

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


def reset():
    CALLS.clear()
