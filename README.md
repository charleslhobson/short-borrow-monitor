# Short Borrow Monitor

A public website showing borrow fees, rebates and shares available to short for US
stocks. Borrow data comes from Interactive Brokers' public stock loan file, so no
IBKR login or TWS is needed. Prices come from Yahoo Finance through the free
`yfinance` package, so no API key is needed either.

## What you need

- A free **GitHub** account (github.com) to store the files
- A free **Render** account (render.com) to run the website
## 1. Put the files on GitHub

1. Sign in to GitHub and click **New repository**.
2. Name it `short-borrow-monitor`, leave it **Public** or choose **Private**, and click **Create repository**.
3. Click **uploading an existing file**.
4. Drag in every file from this folder: `app.py`, `etf.py`, `index.html`,
   `etf.html`, `render.yaml`, `requirements.txt` and `README.md`.
5. Click **Commit changes**.

## 2. Put it online with Render

1. Sign in to https://render.com with your GitHub account.
2. Click **New** → **Blueprint**.
3. Pick the `short-borrow-monitor` repository and click **Connect**.
4. Click **Apply**. In a couple of minutes Render shows your site's address, like
   `https://short-borrow-monitor.onrender.com`. Share that link.

## Changing things later

- **Starting tickers for new visitors:** in Render, open the service → **Environment**,
  edit `DEFAULT_TICKERS` (for example `AAPL,TSLA,GME`) and save. The site restarts by itself.
- **Code or page changes:** upload the new file to GitHub. Render updates the site automatically.
- **Updating yfinance:** `requirements.txt` pins the version the site was tested with.
  When Yahoo changes something and a newer yfinance fixes it, raise the version there.

## Metric tabs

Above the stock table, **+ Add tab** opens a menu with two ways to add a tab:

- **Ready-made tabs:** Short interest, Price & trading, Performance, Valuation,
  Profitability, Growth, Financial health, Dividends, Analysts, Earnings & events,
  Company and Governance risk. One click adds the tab.
- **Build your own:** name the tab and tick any of the 127 metrics, mixing Yahoo
  metrics with IBKR borrow data (for example short % of float next to borrow fee).

Each tab shows every stock in your list. Click a column header to sort, use
**Edit columns** to change a tab, and **Download CSV** to export it. Tabs are saved
in each visitor's browser, like their stock list.

### Where the values come from

Each value comes from Yahoo's main quote data when Yahoo has it. When Yahoo leaves
a value out, it's worked out from other Yahoo data instead, one metric at a time:

- **Price history:** price, previous close, day range, volume, 52-week range,
  moving averages, beta (5 years of monthly returns vs SPY, like Yahoo) and every
  performance figure.
- **Financial statements:** revenue, profits, margins, returns, growth, EPS, cash,
  debt, book value and the current and quick ratios. Valuation ratios such as P/E
  and EV/EBITDA are then calculated from those.
- **Dividend and split history:** dividend rate, trailing dividends, 5-year average
  yield, payout ratio, last dividend and last split.
- **Analyst data, holders, earnings calendar and share-count history.**
- **SEC filings** (when `SEC_USER_AGENT` is set): shares outstanding, and revenue,
  net income, operating cash flow and cash from the latest annual report.

Calculated values have a dotted underline. Hover any value to see where it came
from. A cell with no value says why:

| Shows | Means |
|---|---|
| n/a | Doesn't apply: to funds (P/E, margins…), to banks and insurers (gross margin, current ratio), with negative earnings (P/E), or not enough history yet (3-year return for a new listing) |
| none | There isn't one: no dividend, no splits, no analyst coverage, not rated by ISS, no pre-market or after-hours trades yet |
| closed | No live bid/ask outside market hours |
| – | No short-interest report, not in IBKR's borrow file, or no source has it |

Only the data an open tab needs is fetched. Quotes are cached for 15 minutes, price
history for 30, the earnings calendar for 6 hours, and statements, analyst data and
holders for 12 hours, shared across all visitors. Every Yahoo request on the site
goes through one rate limit (`YAHOO_CALLS_PER_MIN`, default 150), and the site
pauses for a minute and a half if Yahoo says it's getting too many requests. The
first time a stock is opened in a tab it can take a few seconds to load.

To check how many metrics get a value for real tickers, and that calculated values
agree with Yahoo's own, run `python tools/metric_report.py` (add `-v` to list every
metric).

## ETF short screen

Open `/etf` on your site (or click **ETF short screen** at the top of any page).
Paste your ETF list into the box (single stocks work too) and each one gets a row with:

- **Quoted fee and availability** from IBKR's stock loan file.
- **Price** (latest from Yahoo, or the last close) and **$ available**: price ×
  the shares IBKR has to lend, so you can see how much could be shorted.
- **Window.** Since inception, 3 years, 1 year, or **Custom** with a date picker for
  any start date. A date before the ETF existed starts at its inception (the page says
  so), and a weekend or holiday moves to the next trading day.
- **Returns and the cash rate.** Annualized total returns for the ETF and SPY over the
  window, and the **cash rate** short proceeds earn over the same dates: the T-bill
  rate (Yahoo `^IRX`, or FRED's 3-month bill rate as a fallback) minus the spread you
  set (**Interest on short proceeds**, default 1%), never below zero on any day.
- **Beta, two ways.** This window's beta (from returns over the same dates) is used in
  the break-even fees and the verdict. Last year's beta is shown for sizing a hedge
  today: short 1 ÷ beta dollars of the ETF for each dollar of SPY. "β changed" flags
  when they differ by more than 25%. Funds whose stocks mostly trade outside the US
  use weekly returns for both, because daily returns understate their beta.
- **Beta-adjusted break-even fees.** With cash rate `r`, shorting SPY costs
  `r − SPY return` a year per −1.00 beta. Shorting the ETF at fee `f`, scaled to
  −1.00 beta, costs `(r − ETF return − f) ÷ beta` (the interest scales too).
  - *Short profits, after the cash rate:* `f = r − ETF return`.
  - *Beta-adjusted vs shorting SPY:* `f = beta × (SPY return − r) − (ETF return − r)`.
  - Example: SPY +10%, T-bill 4%, spread 1% → cash 3%, SPY costs −7% per −1.00 beta.
    An ETF with beta 0.2 returning +5% costs −2% at no fee, −10% levered ×5, so its
    break-even fee is 0.2 × 7% − 2% = −0.6%: not worthwhile at any fee. With no
    spread it would be +0.2%.
- **Hedge cost per −1.00 beta** at the quoted fee, next to SPY's.
- **Verdict.** Worthwhile when the quoted fee is at or under the beta-adjusted
  break-even. Betas under 0.1 are marked "Low beta: not a SPY hedge".
  Click an ETF to see the full calculation, plus the previous (unadjusted) break-evens.
- **Theoretical fee from holdings.** Every position's exposure (as a share of net
  assets, not re-scaled) times its borrow fee, summed:
  - stocks and ETFs: their IBKR fee, matched by ISIN, CUSIP, ticker, IBKR's non-US
    country files, or a unique exact company name;
  - swaps, futures and options: looked through to the stock or index they track, using
    notional (or delta-adjusted) exposure, so a 2x single-stock ETF comes out at about
    2 × that stock's fee. Index swaps use the components in the filing, the fund's own
    stock holdings, or an ETF that holds the index (edit `INDEX_PROXIES` in `etf.py`);
  - short exposure: 0%;
  - cash, T-bills, Treasuries, money market funds and repo: the **fee for cash &
    T-bills** setting (0% by default);
  - bonds and anything unmatched: the **fee for unmatched holdings** setting.

  Click an ETF to see every position with its fee and how it was matched, or download
  them as a CSV. `/api/etf/holdings?symbol=TSLL` returns the same list as JSON.

The method is written out at the bottom of the page.

### Turning on full holdings from SEC filings (recommended)

Without this, the theoretical fee uses only Yahoo's top 10 holdings. With it, the
site reads each ETF's latest N-PORT filing at the SEC, which lists every holding.

SEC asks every program that downloads its files to identify itself with a name and
email address. In Render, open your service → **Environment** → **Add Environment
Variable**:

- Key: `SEC_USER_AGENT`
- Value: your name and email, for example `Jane Smith jane@example.com`

Save, and the site restarts with SEC holdings turned on. N-PORT filings are
quarterly and published up to 60 days after the quarter ends, so the "Holdings as
of" date in each ETF's details can be a few months old.

### Other settings

- `DEFAULT_ETFS`: the starting list for new visitors.
- `GC_RATE`: the starting "fee for unmatched holdings" (default `0.25`).
- `CASH_RATE`: the starting "fee for cash & T-bills" (default `0`).
- `RFR_SPREAD`: the starting spread below the T-bill rate for interest on short
  proceeds (default `1.0`, % per year).

### Checking the holdings match

With internet access and `SEC_USER_AGENT` set, this prints how much of each ETF's
holdings get an IBKR fee, under the original code and the current one:

```
python tools/match_report.py TSLL NVDL CONL MSTU SOXL IVV IWM EFA HYG TSLA
```

### Tests

`python -m unittest discover -s tests` runs the tests. They use small sample files in
`tests/fixtures`, so they don't need internet access.

## Good to know

- **Free plan sleeps.** Render's free plan pauses the site when nobody has visited
  for a while, so the next visitor waits up to about a minute while it wakes. A paid
  plan (about $7/month) keeps it awake.
- **Borrow data refreshes about every 15 minutes**, not tick by tick.
- **Availability is estimated from the share count:** Available is 10,000 or more
  shares, Limited is fewer than 10,000, None is zero. It isn't IBKR's own
  easy-to-borrow / hard-to-borrow flag.
- **Each visitor has their own list**, saved in their browser. The "Most expensive to
  borrow" table is the same for everyone.
- **If Yahoo blocks the server,** prices show as "–" while everything else keeps
  working. Yahoo sometimes rate-limits cloud servers. To hide the price columns
  entirely, set `SHOW_PRICES` to `off` in Render's **Environment** tab.

## Before you share it publicly

- **Price data rights:** yfinance is an unofficial tool, not a Yahoo product, and
  Yahoo's terms only allow personal use of its data. Showing Yahoo prices on a public
  site goes against those terms. Running with `SHOW_PRICES` set to `off` avoids this.
- **IBKR's borrow file:** it's publicly downloadable, but I couldn't find terms that
  say whether it may be republished. If the site gets real traffic, ask IBKR.

## Test on your own computer first (optional)

Double-click `Test Locally.bat` (it installs yfinance the first time) and the site opens at
http://localhost:8000. If your home network blocks FTP (as it did with the Excel
version), the borrow columns will stay empty locally but will work once the site
is on Render.
