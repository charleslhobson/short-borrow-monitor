"""
Theoretical borrow fee check, live: for each ETF, the estimate from this code ("after")
next to the live site's ("before", whatever is deployed), with checks that fail the
run when an estimate looks wrong.

Needs internet access to IBKR's FTP site, SEC EDGAR, OpenFIGI and Yahoo, and
SEC_USER_AGENT set to your name and email (SEC refuses requests without one):

    export SEC_USER_AGENT="Jane Smith jane@example.com"
    export OPENFIGI_API_KEY=...            # optional, makes identifying holdings faster
    python tools/theo_report.py                         # the default list of ETFs
    python tools/theo_report.py RYLD JEPI --strict      # exit 1 when a check fails
    python tools/theo_report.py --summary out.md        # also write the tables as Markdown

Checks (--strict fails the run on any of them):
  - RYLD (Russell 2000 through RSSL) between 0.3% and 1.5%, and no single holding
    supplying most of it;
  - single-stock funds within 0.05 points of their stock exposure x the stock's fee;
  - funds tracking the same index within 0.05 points of each other;
  - US stock funds: at least 98% of stock exposure identified and 95% matched to an
    IBKR fee;
  - every position has a named source, and no fund carries a warning.
"""

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf  # noqa: E402
import ident  # noqa: E402
from match_report import Static, download_ibkr  # noqa: E402

SITE = "https://short-borrow-monitor.onrender.com"
DEFAULT = ["RYLD", "QYLD", "XYLD", "JEPI", "JEPQ", "TSLL", "NVDL", "CONL", "MSTU", "SOXL", "TQQQ", "SPY", "IVV",
           "VOO", "QQQ", "QQQM", "DIA", "IWM", "EEM", "VWO", "EFA", "KWEB", "FXI", "INDA", "EWT", "EWY", "EWZ",
           "ARKK", "SDTY", "SMH", "XLE", "HYG"]
US_STOCK_FUNDS = ["JEPI", "QYLD", "XYLD", "ARKK", "IWM", "IVV", "SPY", "QQQ", "SMH", "XLE"]
SINGLE_STOCK = {"TSLL": "TSLA", "NVDL": "NVDA", "CONL": "COIN", "MSTU": "MSTR"}
SAME_INDEX = [("SPY", "IVV", "VOO"), ("QQQ", "QQQM")]
SOURCES = {"isin", "figi", "ticker_filing", "ticker_openfigi", "ticker_sec_ftd", "ticker_yahoo", "ticker_fund_name",
           "isin_suffix_name", "name", "secondary_listing", "us_line", "country_median", "same_index_fund",
           "fund_lookthrough", "fund_own_fee", "stock_lookthrough", "index_components", "fund_sleeve",
           "stated_exposure", "eln", "assumed", "not_lendable", "cash", "bond", "short", "nonequity", "excluded"}


def pct(x, digits=2):
    return "–" if x is None else f"{x:.{digits}f}%"


def share(x, digits=1):
    return "–" if x is None else f"{x * 100:.{digits}f}%"


def get_json(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "short-borrow-monitor theo_report"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def before(symbols, site, gc, deadline=600):
    """The live site's theoretical fees: {sym: (fee, coverage)}; {} when it can't be reached."""
    out = {}
    for i in range(0, len(symbols), 30):
        chunk = symbols[i:i + 30]
        url = f"{site}/api/etf?symbols={','.join(chunk)}&gc={gc}&cash=0"
        t0 = time.time()
        while True:
            try:
                d = get_json(url)
            except Exception as e:
                print(f"  live site: {type(e).__name__}; trying again", flush=True)
                d = None
            if d is not None:
                for r in d.get("rows", []):
                    t = (r.get("holdings") or {}).get("theo") or {}
                    out[r["symbol"]] = (t.get("fee"), t.get("coverage"))
                if not d.get("pending"):
                    break
            if time.time() - t0 > deadline:
                break
            time.sleep(15)
    return out


def detail(svc, sym, gc, ids, wait_ids):
    d = svc.holdings_detail(sym, gc, 0.0)
    t0 = time.time()
    while (d.get("theo") or {}).get("identifying") or (d.get("theo") or {}).get("pending"):
        if time.time() - t0 > wait_ids:
            break
        ids.drain(timeout=60)
        d = svc.holdings_detail(sym, gc, 0.0)
    return d


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--site", default=SITE, help="the live site, for the 'before' column ('' to skip)")
    ap.add_argument("--strict", action="store_true", help="exit 1 when a check fails")
    ap.add_argument("--summary", help="also write the report as Markdown to this file")
    ap.add_argument("--wait-ids", type=float, default=900, help="seconds to wait for holdings to be identified")
    args = ap.parse_args(argv)
    symbols = [s.upper() for s in args.symbols] or DEFAULT
    if not etf.SEC_USER_AGENT:
        print("SEC_USER_AGENT isn't set, so the SEC filings this check needs can't be downloaded.\n"
              "On GitHub: Settings -> Secrets and variables -> Actions -> New repository secret,\n"
              "name SEC_USER_AGENT, value your name and email (for example: Jane Smith jane@example.com).")
        return 2
    gc = etf.GC_DEFAULT
    out = []

    def say(line=""):
        print(line, flush=True)
        out.append(line)

    print("Downloading IBKR's stock loan files…", flush=True)
    us, foreign = download_ibkr()
    print(f"  US: {len(us):,} rows; other countries: {', '.join(sorted(foreign))}", flush=True)
    etf.WAIT_SEC = 180
    ids = ident.Identifier()
    ids.ftd.get = etf.SEC.get
    import yfinance as yf
    svc = etf.EtfService(yf, Static(table=us), Static(tables=foreign), ids=ids)
    print("Live site (before)…", flush=True)
    old = before(symbols, args.site, gc) if args.site else {}

    results, problems = {}, []
    for sym in symbols:
        print(f"Pricing {sym}…", flush=True)
        try:
            results[sym] = detail(svc, sym, gc, ids, args.wait_ids)
        except Exception as e:
            results[sym] = {"error": f"{type(e).__name__}: {e}"}

    say("## Theoretical borrow fee, before and after")
    say()
    say(f"Fees in % a year. Holdings nothing identifies are charged {gc}%; cash 0%. Before = the live site.")
    say()
    head = ["ETF", "Holdings", "ETF's own IBKR fee", "Before", "Before: matched", "After", "After: matched",
            "Identified", "Stand-in", "Assumed", "Not lendable", "Warnings"]
    say("| " + " | ".join(head) + " |")
    say("|" + "---|" * len(head))
    for sym in symbols:
        d = results[sym]
        quoted = (us.get(sym) or {}).get("fee")
        b = old.get(sym, (None, None))
        if d.get("single_stock") or d.get("error") or d.get("pending"):
            say(f"| {sym} | {d.get('error') or ('single stock' if d.get('single_stock') else 'still loading')} | "
                f"{pct(quoted)} | {pct(b[0])} | {share(b[1])} | – | – | – | – | – | – | |")
            if not d.get("single_stock"):
                problems.append(f"{sym}: no estimate ({d.get('error') or 'still loading'})")
            continue
        t = d["theo"]
        warn = "; ".join(t.get("warnings") or [])
        say(f"| {sym} | {d['source']} {d.get('as_of') or ''} | {pct(quoted)} | {pct(b[0])} | {share(b[1])} | "
            f"{pct(t['fee'])} | {share(t['coverage'])} | {share(t.get('identified'))} | {share(t.get('proxy_weight'))} | "
            f"{share(t.get('assumed_weight'))} | {share(t.get('notlendable_weight'))} | {warn} |")

    say()
    say("## Still assumed or priced from a stand-in (top 5 per ETF)")
    say()
    for sym in symbols:
        d = results[sym]
        if "theo" not in d:
            continue
        rows = [r for r in d["rows"] if r["group"] in ("unmatched", "proxy") and r["exposure"] > 0]
        rows.sort(key=lambda r: -r["exposure"])
        if not rows:
            continue
        say(f"**{sym}**")
        say()
        for r in rows[:5]:
            say(f"- {r['name']} ({share(r['exposure'], 2)}): {r['how']} at {pct(r['fee'])}. {r['detail'][:220]}")
        say()

    say("## Market medians used, and stand-in lines at 2% or more")
    say()
    idx = svc.match_index(us)
    for sym in symbols:
        d = results[sym]
        if "theo" not in d:
            continue
        meds = sorted({(r["how"], round(r["fee"], 4)) for r in d["rows"] if r.get("source") == "country_median"})
        dear = [r for r in d["rows"] if r.get("source") in ("us_line", "secondary_listing") and (r.get("fee") or 0) >= 2]
        if not meds and not dear:
            continue
        parts = [f"{how} {fee:.2f}%" for how, fee in meds]
        for r in dear:
            lines = etf.country_lines(idx, r.get("home"), d["rows"])
            med = sorted(lines.values())[len(lines) // 2] if lines else None
            parts.append(f"{r['name']} via {r.get('symbol')} at {r['fee']:.2f}% ({len(lines)} lines for "
                         f"{etf.market_label(r.get('home'))}, median about {pct(med)})")
        say(f"- {sym}: " + "; ".join(parts))
    say()

    say("## Weight by source")
    say()
    for sym in symbols:
        d = results[sym]
        if "theo" in d:
            src = sorted(d["theo"].get("sources", {}).items(), key=lambda kv: -kv[1])
            say(f"- {sym}: " + ", ".join(f"{k} {share(v)}" for k, v in src if v > 0.0005))
    say()

    # ---- checks ----
    def theo(sym):
        d = results.get(sym) or {}
        return d.get("theo") if "theo" in d else None

    r = theo("RYLD")
    if r is not None:
        if not 0.3 <= r["fee"] <= 1.5:
            problems.append(f"RYLD: {r['fee']:.2f}% is outside 0.3%–1.5%")
        top = max(results["RYLD"]["rows"], key=lambda x: x["contrib"])
        if r["fee"] > 0 and top["contrib"] > 0.5 * r["fee"] and top.get("source") not in ("fund_lookthrough",
                                                                                          "same_index_fund"):
            problems.append(f"RYLD: {top['name']} supplies most of the estimate ({top['how']})")
    for fund, stock in SINGLE_STOCK.items():
        t = theo(fund)
        if t is None or (us.get(stock) or {}).get("fee") is None:
            continue
        want = t["gross_long_equity"] * us[stock]["fee"]
        if abs(t["fee"] - want) > 0.05:
            problems.append(f"{fund}: {t['fee']:.2f}%, but {t['gross_long_equity']:.2f}x exposure x {stock}'s "
                            f"{us[stock]['fee']:.2f}% is {want:.2f}%")
        if t["gross_long_equity"] < 1.5:
            problems.append(f"{fund}: only {t['gross_long_equity']:.2f}x stock exposure found for a 2x fund")
    for group in SAME_INDEX:
        fees = {s: theo(s)["fee"] for s in group if theo(s)}
        if len(fees) > 1 and max(fees.values()) - min(fees.values()) > 0.05:
            problems.append(f"{' / '.join(group)} differ: " + ", ".join(f"{s} {f:.2f}%" for s, f in fees.items()))
    for sym in US_STOCK_FUNDS:
        t = theo(sym)
        if t is None:
            continue
        if (t.get("identified") or 0) < 0.98:
            problems.append(f"{sym}: only {share(t.get('identified'))} of stock exposure identified")
        if (t.get("coverage") or 0) < 0.95:
            problems.append(f"{sym}: only {share(t.get('coverage'))} matched to an IBKR fee")
    for sym in symbols:
        d = results[sym]
        if "theo" not in d:
            continue
        bad = [x for x in d["rows"] if x.get("source") not in SOURCES]
        if bad:
            problems.append(f"{sym}: {len(bad)} positions without a named source ({bad[0]['name']})")
        for w in d["theo"].get("warnings") or []:
            problems.append(f"{sym}: {w}")

    say("## Checks")
    say()
    if problems:
        for p in problems:
            say(f"- ❌ {p}")
    else:
        say("- ✅ all checks passed")
    if args.summary:
        Path(args.summary).write_text("\n".join(out) + "\n")
    return 1 if problems and args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
