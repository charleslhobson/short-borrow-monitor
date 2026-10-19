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

Company data from Yahoo is cached for 15 minutes and performance figures for 30,
shared across all visitors. The first time a stock is opened in a tab it can take
a few seconds to load.

## ETF short screen

Open `/etf` on your site (or click **ETF short screen** at the top of any page).
Paste your ETF list into the box and each ETF gets one row with:

- **Quoted fee and availability** from IBKR's stock loan file.
- **Break-even fees from history.** Using Yahoo's total-return prices, the fee at
  which a short held since inception (or over 3 or 1 years, using the buttons)
  would have broken even, and the fee at which it would have earned as much as
  owning SPY over the same dates. **Verdict** compares these with the quoted fee.
  Click an ETF to see the full calculation, including a short-ETF/long-SPY pair.
- **Theoretical fee from holdings.** Each holding's weight times its IBKR borrow
  fee, summed, next to the quoted fee and the gap between them. Holdings without
  an IBKR fee (cash, Treasuries, swaps) are charged the "fee for unmatched
  holdings" rate, which you can change on the page.

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
