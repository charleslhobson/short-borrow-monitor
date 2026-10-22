"""
How many of the Stocks page's metrics have a value, per ticker, before and after
the fallbacks, plus a check that calculated values agree with Yahoo's own.

Needs internet access to Yahoo Finance (and SEC, if SEC_USER_AGENT is set):

    python tools/metric_report.py                       # the standard test list
    python tools/metric_report.py AAPL JPM SPY -v       # your own list, every metric

"Before" counts what the old code showed: Yahoo's .info fields, plus performance
calculated from price history. "After" is the current code. -v prints each
metric's value, where it came from or why it's missing, and the .info keys Yahoo
actually returned (to spot renamed fields).
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import metrics  # noqa: E402

DEFAULT = ["AAPL", "MSFT", "JPM", "XOM", "BRK.B", "TSLA", "GME", "AMC", "BYND", "TSM", "SPY", "TSLL"]
# Calculated fallbacks worth checking against Yahoo's value, with the gap that's fine
COMPARE = {"fiftyDayAverage": 0.02, "twoHundredDayAverage": 0.02, "averageVolume": 0.05,
           "averageVolume10days": 0.05, "fiftyTwoWeekHigh": 0.02, "fiftyTwoWeekLow": 0.02,
           "beta": 0.15, "marketCap": 0.03, "trailingEps": 0.05, "totalRevenue": 0.03,
           "grossMargins": 0.02, "operatingMargins": 0.03, "profitMargins": 0.02, "currentRatio": 0.05,
           "debtToEquity": 0.05, "dividendRate": 0.05, "trailingAnnualDividendRate": 0.05,
           "recommendationMean": 0.1, "sharesOutstanding": 0.03, "bookValue": 0.05, "forwardEps": 0.05}
ABSOLUTE = {"beta", "grossMargins", "operatingMargins", "profitMargins", "recommendationMean"}


def before_count(row, info):
    old = {k for k in metrics.YAHOO_KEYS if info.get(k) not in (None, "")}
    old |= {k for k in metrics.PERF_KEYS if k in row["data"]}
    if info.get("sharesShort") and info.get("sharesShortPriorMonth"):
        old.add("short_change")
    if info.get("dividendRate") and (info.get("currentPrice") or info.get("regularMarketPrice")):
        old.add("yield_calc")
    if info.get("targetMeanPrice") and (info.get("currentPrice") or info.get("regularMarketPrice")):
        old.add("target_upside")
    return len(old)


def main(symbols, verbose, fake=False):
    if fake:                                  # dry run on the test data in tests/fake_yf.py
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
        import fake_yf as yf
    else:
        import yfinance as yf
    import etf
    svc = metrics.MetricService(yf, etf.SEC, bool(etf.SEC_USER_AGENT))
    metrics.WAIT_SEC = 120
    print(f"{'Ticker':8} {'Before':>7} {'After':>7}  {'No value (reasons)':s}")
    for sym in symbols:
        t0 = time.time()
        rows, _ = svc.get([sym], metrics.YAHOO_KEYS)
        r = rows[0]
        info = ((svc._lookup("info", sym)[0] or {}).get("info")) or {}
        reasons = {}
        for code in r["why"].values():
            reasons[code] = reasons.get(code, 0) + 1
        print(f"{sym:8} {before_count(r, info):>4}/{len(metrics.YAHOO_KEYS)} {len(r['data']):>4}/{len(metrics.YAHOO_KEYS)}  "
              f"{', '.join(f'{metrics.REASONS[c][1]} ×{n}' for c, n in sorted(reasons.items())) or '–'}"
              f"{'  (still loading: ' + ', '.join(r['loading']) + ')' if r['loading'] else ''}"
              f"  [{time.time() - t0:.1f}s]", flush=True)
        if r["error"]:
            print(f"         {r['error']}")
        if verbose:
            for k in metrics.YAHOO_KEYS:
                if k in r["data"]:
                    print(f"         {k:34} {str(r['data'][k])[:28]:28} {metrics.SOURCES[r['src'][k]][0]}"
                          f"{'  (' + r['note'][k] + ')' if k in r['note'] else ''}")
                elif k in r["why"]:
                    print(f"         {k:34} {'':28} WHY: {metrics.REASONS[r['why'][k]][1]}"
                          f"{'  (' + r['note'][k] + ')' if k in r['note'] else ''}")
            print(f"         .info keys Yahoo returned ({len(info)}): {', '.join(sorted(info))}")

        # where Yahoo has a value, does the fallback agree?
        for source in ("statements", "analysts", "holders", "shares"):
            svc._ensure(source, sym)
        svc._ensure("hist", "SPY")
        deadline = time.time() + 60
        while time.time() < deadline and any(svc._lookup(s, sym)[0] is None for s in ("statements", "analysts", "holders", "shares")):
            time.sleep(0.5)
        for key, tol in COMPARE.items():
            yahoo = info.get(key)
            if not isinstance(yahoo, (int, float)) or not yahoo:
                continue
            ctx_sources = svc._sources(sym)
            stripped = {k: v for k, v in info.items() if k not in metrics_info_names(key)}
            ctx_sources["info"] = {"info": stripped, "empty": False}
            ctx = metrics.Ctx(sym, ctx_sources, spy=svc._lookup("hist", "SPY")[0], sec_enabled=svc.sec_enabled)
            ours = ctx.v(key)
            if ours is None:
                continue
            gap = abs(ours - yahoo) if key in ABSOLUTE else abs(ours / yahoo - 1)
            flag = "ok " if gap <= tol else "CHECK"
            print(f"         {flag} {key:28} Yahoo {yahoo:<16.6g} calculated {ours:<16.6g} "
                  f"({metrics.SOURCES[ctx.memo[key][1]][0]})")


def metrics_info_names(key):
    """The .info field names a metric's Yahoo step reads."""
    names = {key}
    for src, fn in metrics.STRATEGIES[key]:
        names |= set(getattr(fn, "info_names", ()))
    return names


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    main([a.upper() for a in args] or DEFAULT, "-v" in sys.argv, "--fake" in sys.argv)
