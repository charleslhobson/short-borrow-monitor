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
4. Drag in every file and folder from this folder (the `.py` and `.html` files,
   `render.yaml`, `requirements.txt`, `README.md`, and the `static`, `tools`, `tests`
   and `.github` folders).
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
- **Build your own:** name the tab and tick any of the 135 metrics, mixing Yahoo
  metrics with IBKR borrow data (for example short % of float next to borrow fee).

Each tab shows every stock in your list. Click a column header to sort, use
**Edit columns** to change a tab, and **Download CSV** to export it. Tabs are saved
in each visitor's browser, like their stock list. A line above each table counts
what's filled and why the rest is blank, for example "312 of 360 cells filled · 36
don't apply to funds · 12 not on Yahoo".

### Tickers: IBKR's spelling and Yahoo's

The site always shows IBKR's spelling and asks Yahoo for its own (hover a ticker to
see it): share classes `BRK.B` → `BRK-B`, preferreds `ABR.PRD` → `ABR-PD` (and
`DCOM.PR` → `DCOM-P`), units `AAC.U` → `AAC-UN`, warrants `.WS` → `-WT`, rights
`.RT` → `-RI`, when-issued `.WI` → `-WI`. Dotted tickers are checked against Yahoo
once; when the rule finds nothing and IBKR's file has a full ISIN (IBKR masks US
ones), Yahoo's ISIN search is tried, accepting only an exact match. Answers, good and
bad, are remembered for a week (in the database too, when `DATABASE_URL` is set).
Typing Yahoo's spelling (`BRK-B`, `BAC-PD`, `AAC-UN`) adds IBKR's (`BRK.B`,
`BAC.PRD`, `AAC.U`). Old or renamed IBKR listings (`.OLD`, `.OLD1`, `.RS`, `.RES`…)
and IBKR-only lines (restricted, contra, temporary or fractional shares) are never
looked up: their row says so and still shows IBKR's borrow data, as does any ticker
Yahoo doesn't carry ("Not on Yahoo Finance; tried X, Y and ISIN Z").

### Where the values come from

Each value comes from Yahoo's main quote data when Yahoo has it. When Yahoo leaves
a value out, it's worked out from other data instead, one metric at a time:

- **Price history:** price, previous close, day range, volume, 52-week range,
  moving averages, beta (5 years of monthly returns vs SPY, like Yahoo) and every
  performance figure.
- **Financial statements:** revenue, profits, margins, returns, growth, EPS, cash,
  debt, book value and the current and quick ratios. Valuation ratios such as P/E
  and EV/EBITDA are then calculated from those.
- **Dividend and split history:** dividend rate, trailing dividends, 5-year average
  yield, payout ratio, last dividend and last split.
- **Analyst data, holders, earnings calendar and share-count history.**
- **FINRA short interest:** shares short, the prior month's, days to cover and the
  report date, from FINRA's free twice-monthly file of every exchange-listed and OTC
  stock and ETF (Yahoo leaves short interest out for ETFs). Marked "FINRA short
  interest, as of" the report's date.
- **The issuer's common stock**, for preferreds, units, warrants and rights:
  sector, location, governance scores, margins and other company-wide figures.
- **SEC filings** (when `SEC_USER_AGENT` is set): shares outstanding, and revenue,
  net income, operating cash flow and cash from the latest annual report.

**Funds** get fund equivalents, marked with a small *f*: P/E, price/book and
price/sales of the holdings (Yahoo's fund data), net assets for market cap, the
premium or discount to NAV, distribution rate, fund family, category, inception date,
expense ratio, net assets and number of holdings (from SEC filings). For the other
valuation, profitability, growth, financial health and analyst columns, a
**look-through** value averages the fund's biggest holdings (up to 15) by weight,
labelled for example "Look-through: weighted by 64% of holdings". It's shown only
when the holdings with data cover at least 30% of the fund's stock exposure; below
that the cell says "holdings unavailable". With `SEC_USER_AGENT` set, holdings come
from the fund's SEC filing, with swaps looked through to the stock or index they
track (so leveraged funds like SOXL or TSLL get look-through values); without it,
from Yahoo's top 10.

Calculated values have a dotted underline. Hover any value to see where it came
from. A cell with no value says why, in one of three kinds:

| Shows | Means |
|---|---|
| n/a (grey) | Doesn't apply: to funds (EPS, total debt…), only to funds (expense ratio for a stock), to banks and insurers (gross margin, current ratio), negative earnings (P/E), no dividend, never split, market closed (bid/ask), not enough history yet, or ISS rates only US-listed companies |
| – | Yahoo has none: no analyst coverage, not rated by ISS, no short-interest report from Yahoo or FINRA, holdings unavailable, or no source has it |
| retrying… | Temporarily unavailable: Yahoo was busy or unreachable, and the site is trying again |

A row still loading after five checks (about 20 seconds) switches to "retrying…" and
gets a **Retry** link. When Yahoo answers "too many requests", the line above the
table says "Yahoo is busy; retrying in N s".

Only the data an open tab needs is fetched, and it's fetched first: once the open tab
is complete, the page fetches your other tabs ahead using only Yahoo capacity to spare,
so switching tabs is instant. Quotes are cached for 15 minutes, price history for 30,
the earnings calendar for 6 hours, and statements, analyst and fund data and holders
for 12 hours, shared across all visitors. Every Yahoo request on the site goes through
one rate limit (`YAHOO_CALLS_PER_MIN`, default 150), and the site pauses for a minute
and a half if Yahoo says it's getting too many requests.

### Checking coverage

`python tools/coverage_report.py` samples IBKR's file (20 each of plain stocks, large
caps, ADRs, OTC stocks, ETFs, leveraged ETFs, preferreds, units, share classes and old
listings), runs every metric live, and prints fill % per kind and category, why the
rest is blank, and any metric that's blank for every large cap. `--compare main` runs
the same sample on another branch's code for a before/after table, `--timing` adds how
long 30 tickers take on every tab. The **Metric coverage** GitHub workflow runs it every
Monday (and on demand from the Actions tab) and fails, with the table in its summary,
when a metric goes blank for every large cap, which usually means Yahoo renamed or
dropped a field.

To check how many metrics get a value for particular tickers, and that calculated
values agree with Yahoo's own, run `python tools/metric_report.py AAPL SPY` (add `-v`
to list every metric).

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
  assets, not re-scaled) times its borrow fee, summed. Every position says where its
  fee came from:
  - US stocks: IBKR's fee for the holding's own line. IBKR's US file hides most of
    each US ISIN (Apple is `XXXXXXX31005`) but keeps its last 5 characters, the
    composite FIGI and every non-US ISIN, so holdings are matched by FIGI, full ISIN,
    a ticker found from the ISIN (OpenFIGI, SEC's fails-to-deliver data, Yahoo; see
    `ident.py`), the filing's ticker, the ISIN's last 5 characters plus a matching
    name, or a unique company name. Names are compared without "Inc", "ADR" and the
    like, HTML entities are decoded ("JOHNSON &amp; JOHNSON") and IBKR's old names
    count (`RENAMES` in `etf.py`: it still calls Meta "Facebook").
  - The holding's currency decides its market: Medtronic, Irish but held in dollars,
    uses its US line, not IBKR's Austria file.
  - Non-US stocks: the line in IBKR's file for their market; where IBKR has none
    (Korea, Taiwan, India, Brazil...), the same company's IBKR US line (an ADR), its
    listing in another country's file, or the median fee of IBKR's US lines for that
    market's companies (`COUNTRY_LINES` in `etf.py`). Lines that aren't the holding's
    own aren't used when they're expensive (5%+) and thin (under 100,000 shares), or cost
    2%+ and over 4× the median of other lines for that market's companies (an unsponsored
    ADR at 8.94% says little about borrowing the bank's shares in Singapore).
  - Funds held by the fund: priced from their own holdings, two levels deep, else
    from an ETF tracking the same index, and only as a last resort at their own (never
    thin) IBKR fee. RYLD holds the Russell 2000 through RSSL, whose own IBKR fee was
    40% on 9,000 shares; it now comes out near IWM's rate.
  - Swaps, futures and options: looked through to the stock or index they track, using
    notional (or delta-adjusted) exposure, so a 2x single-stock ETF comes out at about
    2 × that stock's fee. When the filing names the reference "N/A" (TSLL), the swap's
    own name and the fund's name say which stock. Index swaps use the components in the
    filing, the fund's own stock holdings, or an ETF that holds the index (edit
    `INDEX_PROXIES` in `etf.py`). Equity-linked notes (JEPI) are priced like their index.
  - Stated exposure: when a fund's filing shows far less stock exposure than its name
    promises (MSTU's swaps add up to 0.5% of a 2x fund; SDTY holds only T-bills and an
    option with no delta), the missing exposure is added as a labelled row.
  - Short exposure (written calls too, even when filed as rate derivatives): 0%.
  - Cash, T-bills, Treasuries, money market funds and repo: the **fee for cash &
    T-bills** setting (0% by default).
  - Private shares, warrants, rights and CVRs: "not lendable", adding nothing.
  - Bonds and holdings nothing identifies: the **fee when nothing else is known**
    setting. Each ETF's details list them.

  An ETF is marked "⚠ check" when one holding drives most of its estimate at a high
  fee, when the estimate is over 3× the ETF's own IBKR fee, or when more than half of it
  is charged the assumed rate. Click an ETF to see every position with its fee and
  source, or download them as a CSV. `/api/etf/holdings?symbol=TSLL` returns the same
  list as JSON.
- **Price vs total return chart** (in each ETF's details). Two lines over the same
  dates as the table's window:
  - **Market price**: what the shares trade at (adjusted for splits only);
  - **Total return**: the value of one share bought at the window's start with every
    distribution reinvested (Yahoo's adjusted close, the same series the table's
    returns use). Both start at the same price.

  The shaded gap between them is what the distributions added. A fund can show a
  falling price and a rising total return at the same time: high-payout funds (covered
  call, "income" and many leveraged funds) pay out a lot, including return of capital,
  and each payout lowers the share price by about the amount paid. A short owes every
  one of those payouts, which is why the screen's break-evens use total return.
  Switch to **% change** to compare both with SPY's total return; small marks along the
  bottom are ex-distribution dates. Download the chart's data as a CSV.
  `/api/etf/chart?symbol=TSLL&win=1y` returns it as JSON (`win` is `inception`, `3y`,
  `1y`, or `custom` with `&start=YYYY-MM-DD`).
- **Borrow fee history chart** (in each ETF's details), the same as on the Stocks page:
  see [Borrow fee history](#borrow-fee-history).

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
of" date in each ETF's details can be a few months old. SPY, DIA and MDY are unit
trusts that don't file N-PORT: their holdings come from State Street's daily file, and
QQQ uses QQQM's filing.

Holdings IBKR's own fields can't place are looked up at OpenFIGI (free). It allows 25
requests of 10 holdings a minute; a free key from openfigi.com raises that to 25
requests of 100 every 6 seconds. To use one, add `OPENFIGI_API_KEY` in Render →
Environment. With `DATABASE_URL` set, every answer is saved (the `security_ids` table)
for 30 days, so it's looked up once.

### Other settings

- `DEFAULT_ETFS`: the starting list for new visitors.
- `GC_RATE`: the starting "fee when nothing else is known" (default `0.25`).
- `OPENFIGI_API_KEY`: optional, see above.
- `CASH_RATE`: the starting "fee for cash & T-bills" (default `0`).
- `RFR_SPREAD`: the starting spread below the T-bill rate for interest on short
  proceeds (default `1.0`, % per year).
- `DATABASE_URL`, `STORAGE_LIMIT_MB`, `SHARES_CHANGE_PCT`, `IBORROWDESK`: see
  [Borrow fee history](#borrow-fee-history).

### Checking the theoretical fees

With internet access and `SEC_USER_AGENT` set, this prices 32 ETFs (RYLD, QYLD, JEPI,
TSLL, MSTU, SPY, IWM, EEM, KWEB, SDTY and more) with the current code, next to the live
site's numbers, and lists what's still assumed or priced from a stand-in and why:

```
python tools/theo_report.py                 # or: python tools/theo_report.py RYLD JEPI --strict
```

It checks that RYLD lands between 0.3% and 1.5%, single-stock funds at their exposure ×
the stock's fee, SPY / IVV / VOO (and QQQ / QQQM) within 0.05 points of each other, US
stock funds at least 98% identified and 95% matched, and no fund with a warning. The
**Theoretical fee check** GitHub workflow runs it every Monday (and on demand from the
Actions tab) and fails when a check does. It needs the `SEC_USER_AGENT` repository
secret (Settings → Secrets and variables → Actions → New repository secret, value your
name and email); `OPENFIGI_API_KEY` there is optional.

`python tools/match_report.py TSLL NVDL CONL MSTU SOXL IVV IWM EFA HYG TSLA` compares
the share of holdings matched under the original code and the current one.

### Tests

`python -m unittest discover -s tests` runs the tests. They use small sample files in
`tests/fixtures`, so they don't need internet access. The iBorrowDesk samples follow
the shape of its public data feed with made-up values. To also test the database code
against a real Postgres, set `TEST_DATABASE_URL` to an empty test database (its
tables are dropped and recreated).

## Borrow fee history

Click the ▶ next to any ticker on the Stocks page (or open an ETF on the ETF screen)
to see its borrow fee and shares available over time:

- **Borrow fee** as a step line (left axis), **shares available** as a light area
  (right axis). Hover for the exact fee, rebate, shares and where the reading came from.
- **1M, 3M, 6M, 1Y, 3Y, All**, a log scale for the fee, and **Download CSV**.
- **Where the data comes from:**
  - **This site's own readings** of IBKR's file, every hour, from the day you set it up.
  - **Older history from [iBorrowDesk](https://iborrowdesk.com)**, which has logged
    IBKR's file for years. It's fetched once per ticker, the first time anyone opens
    that ticker's chart (that first chart takes a few seconds), and saved. A dotted
    line marks where iBorrowDesk's history ends and this site's readings begin.
    **As of October 2026 iBorrowDesk turns away requests from cloud servers** (Render's
    and GitHub's both get "403 Forbidden" or a dropped connection), so charts start on
    the day this site began recording and the chart says so. The site asks iBorrowDesk
    again once a day; if it ever allows the request, older history fills in by itself.
  - **Shaded "not recorded"** stretches are 3+ hours when this site's recorder didn't
    run. iBorrowDesk's data fills them where it can.
- Only changes are saved: a new reading is stored when the fee changes, the rebate
  moves more than 0.25 points, or shares available move 20% or more (or to or from
  zero). The chart carries each value forward until the next change.

Without a database the charts still work, using iBorrowDesk's data only.

### Setting it up (about 10 minutes, free)

1. **Create the database.** Sign up at https://neon.tech (the free plan is enough) and
   create a project. Pick the region closest to your Render service (Render shows its
   region on the service's page). On the project's dashboard click **Connect**, leave
   **Connection pooling** on, and copy the connection string. It starts with
   `postgresql://` and ends with `sslmode=require`. Treat it like a password: never
   put it in a file in the repository.
2. **Give it to the website.** In Render, open your service → **Environment** →
   **Add Environment Variable**: key `DATABASE_URL`, value the connection string.
   Save; the site restarts and creates its tables.
3. **Give it to the hourly recorder.** The website sleeps on Render's free plan, so a
   GitHub Actions job (`.github/workflows/record-borrow.yml`) records IBKR's file every
   hour instead. In GitHub, open the repository → **Settings** → **Secrets and
   variables** → **Actions** → **New repository secret**: name `DATABASE_URL`, value
   the same connection string.
4. **Run it once by hand.** In GitHub, open **Actions** (enable workflows if asked) →
   **Record borrow** → **Run workflow**. Open the run and check the log ends with a
   line like `14,812 tickers in file, 14,812 points saved... Database 9.3 MB of 500 MB`.
   After that it runs by itself every hour at 17 minutes past.

The website also records every 30 minutes while it's awake, so readings keep coming
while people use it. Two recorders can't save the same reading twice.

### Good to know about the history

- **Also stored:** a small `yahoo_symbols` table remembering Yahoo's spelling of
  dotted IBKR tickers (see "Tickers: IBKR's spelling and Yahoo's"), a few kilobytes.
- **Storage.** Neon's free plan holds 0.5 GB. Each saved reading takes about 120
  bytes. The first run saves one reading per ticker (about 2 MB); after that, only
  changes. My estimate is around 50 MB a month, but it depends on how often IBKR's
  numbers move, so check the log's database size after a few days. After 180 days,
  old readings are thinned to the last one of each day.
  - `STORAGE_LIMIT_MB` (default `500`): the site and the recorder warn when the
    database passes 80% of this.
  - `SHARES_CHANGE_PCT` (default `20`): raise it (say to `40`) to save fewer
    shares-available readings if the database grows too fast.
  - `python tools/storage_estimate.py --watch 3 --every 60` downloads IBKR's file three
    times an hour apart and projects the growth from real changes.
- **GitHub Actions minutes.** Free for public repositories. Private repositories get
  2,000 free minutes a month; the hourly recorder uses about 720.
- **GitHub pauses scheduled jobs** in public repositories after 60 days with no
  commits. If the charts show a long "not recorded" stretch, open **Actions** →
  **Record borrow** and re-enable it. GitHub can also start scheduled runs late when
  it's busy.
- **iBorrowDesk is unofficial.** It's a free site that publishes IBKR's data, with no
  documented API, and it currently blocks cloud servers (see above). This site asks it
  for at most one ticker every 2 seconds, only when someone opens a chart. When it
  refuses the server, the site stops asking for 24 hours; a failure for one ticker is
  retried after an hour. Set `IBORROWDESK` to `off` to stop asking altogether.
  `python tools/check_iborrowdesk.py GME AAPL` checks it from your own computer and
  compares its fees with IBKR's file.
- `/api/borrow/history?symbol=GME&range=1y` returns a chart's data as JSON.

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
- **iBorrowDesk's history:** the charts credit and link to iBorrowDesk. If the site
  gets real traffic, ask its owner whether that use is welcome.

## Test on your own computer first (optional)

Double-click `Test Locally.bat` (it installs yfinance the first time) and the site opens at
http://localhost:8000. If your home network blocks FTP (as it did with the Excel
version), the borrow columns will stay empty locally but will work once the site
is on Render.
