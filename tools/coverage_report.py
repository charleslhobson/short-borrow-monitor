"""
How well the Stocks page's metric categories are filled, by kind of ticker
-------------------------------------------------------------------------
Takes a stratified sample of IBKR's US stock loan file (20 tickers of each kind
by default): plain NYSE/Nasdaq stocks, large caps, ADRs, OTC stocks, ETFs,
leveraged ETFs, preferreds, units, share classes and old listings. Runs every
metric for them through the site's own code (metrics.py), live against Yahoo and
FINRA, and reports per kind and category:

  fill %       filled cells out of the cells that apply (cells marked "doesn't
               apply", like EPS for a fund, and rows Yahoo doesn't carry at all
               are left out)
  why blank    every blank cell's reason, counted
  generic      blank cells whose reason is only the generic "Yahoo has no data for this"

It also flags metrics that are blank for every large cap in the sample: a sign
Yahoo renamed or dropped a field.

    python tools/coverage_report.py                    # sample, report
    python tools/coverage_report.py --compare main     # the same sample on main's code too: before/after
    python tools/coverage_report.py --fail-on-gaps     # exit 1 if a metric is blank for every large cap
    python tools/coverage_report.py --timing           # also: 30 tickers, every preset tab, how long to load
    python tools/coverage_report.py --timing-only      # just that
    python tools/coverage_report.py --fake             # dry run on tests/fake_yf.py (no network)

Options: --per-kind N (default 20), --seed S (default: this ISO week, so weekly runs
vary), --json FILE (raw results), --summary FILE (Markdown, e.g. $GITHUB_STEP_SUMMARY),
--minutes M (time budget for Yahoo, default 25).
Needs internet access to Yahoo Finance, FINRA and IBKR's FTP server (not --fake).
"""

import argparse
import inspect
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

KINDS = ["plain", "large cap", "ADR", "OTC", "ETF", "leveraged ETF", "preferred", "unit", "share class", "old listing"]
KIND_LABEL = {"plain": "Plain NYSE/Nasdaq", "large cap": "Large caps ($10B+)", "ADR": "ADRs", "OTC": "OTC",
              "ETF": "ETFs", "leveraged ETF": "Leveraged ETFs", "preferred": "Preferreds", "unit": "Units",
              "share class": "Share classes", "old listing": "Old listings"}
LISTED = {"NYQ", "NMS", "NGM", "NCM", "ASE", "PCX", "BTS", "NAS"}       # NYSE, Nasdaq, NYSE American, Arca, Cboe
OTC = {"PNK", "OQB", "OQX", "OID", "OBB", "OEM", "OGM"}
ADR_RE = re.compile(r"\bADRS?\b|-ADR|\bADS\b|SPON|UNSP|DEPOSITARY", re.I)
LEVERAGED_RE = re.compile(r"\b(\d(\.\d+)?X|ULTRA\w*|LEVERAGED?|BULL|BEAR|INVERSE)\b", re.I)
LARGE_CAP = 10e9
GENERIC = {"no_data"}                     # the reason that explains nothing


# ------------------------------- sampling -----------------------------------
def dotted_kind(sym):
    import common
    if common.listing_kind(sym):
        return "old listing"
    base, dot, suffix = sym.partition(".")
    if not dot:
        return None
    if re.fullmatch(r"PR[A-Z]?", suffix):
        return "preferred"
    if suffix == "U":
        return "unit"
    if re.fullmatch(r"[A-Z]", suffix):
        return "share class"
    return None


def plain_kind(entry, q):
    """The kind of a plain ticker from Yahoo's quote, or None (not on Yahoo, or a mutual fund...)."""
    import common
    if not common.listed_quote(q):
        return None
    qt, ex = q.get("quoteType"), (q.get("exchange") or "").upper()
    name = f"{q.get('longName') or ''} {q.get('shortName') or ''}"
    if qt == "ETF":
        return "leveraged ETF" if LEVERAGED_RE.search(name) else "ETF"
    if qt != "EQUITY":
        return None
    if ex in OTC:
        return "OTC"
    if ex not in LISTED:
        return None
    if ADR_RE.search((entry or {}).get("name") or ""):
        return "ADR"
    return "large cap" if (q.get("marketCap") or 0) >= LARGE_CAP else "plain"


def stratified_sample(table, per_kind, seed, quotes, log=print):
    """{kind: [tickers]}, per_kind of each (fewer when IBKR's file has fewer)."""
    rng = random.Random(seed)
    syms = sorted(table)
    rng.shuffle(syms)
    out = {k: [] for k in KINDS}
    plain, checked, missing = [], 0, 0
    for s in syms:
        k = dotted_kind(s)
        if k and len(out[k]) < per_kind:
            out[k].append(s)
        elif "." not in s:
            plain.append(s)
    need = [k for k in ("plain", "large cap", "ADR", "OTC", "ETF", "leveraged ETF")]
    for i in range(0, len(plain), 50):
        if all(len(out[k]) >= per_kind for k in need) or checked >= 4000:
            break
        chunk = plain[i:i + 50]
        q = quotes(chunk)
        checked += len(chunk)
        for s in chunk:
            k = plain_kind(table.get(s), q.get(s))
            if k is None:
                missing += 1 if not (q.get(s) or {}).get("exchange") else 0
                continue
            if len(out[k]) < per_kind:
                out[k].append(s)
    log(f"Sampled from {len(table):,} IBKR tickers (seed {seed}); checked {checked:,} plain tickers on Yahoo, "
        f"{missing:,} of them ({missing / max(checked, 1):.0%}) aren't on Yahoo at all.")
    return out


# ------------------------------- running ------------------------------------
def make_service(table, fake=False):
    """metrics.MetricService set up like the site's (whichever copy of the code is on sys.path)."""
    import common
    import metrics
    if fake:
        sys.path.insert(0, str(ROOT / "tests"))
        import fake_yf as yf
    else:
        import yfinance as yf
    try:
        import etf
        sec, sec_on = etf.SEC, bool(etf.SEC_USER_AGENT)
    except Exception:
        etf, sec, sec_on = None, None, False
    params = inspect.signature(metrics.MetricService.__init__).parameters
    kw = {}
    if "ibkr" in params:
        kw["ibkr"] = table.get
    if fake and "symbols" in params:
        common.SYMBOLS = common.SymbolMap(quotes=yf.quotes, search=yf.search)
        kw["symbols"] = common.SYMBOLS
    if "finra" in params:
        import finra
        if fake:
            sys.path.insert(0, str(ROOT / "tests"))
            from test_coverage import fake_finra_get
            fin = finra.FinraShortInterest(get=fake_finra_get, today=lambda: date(2026, 10, 24))
        else:
            fin = finra.FinraShortInterest()
        fin.ensure(background=False)
        kw["finra"] = fin
    if "holdings" in params and etf is not None and hasattr(etf.EtfService, "lookthrough_weights"):
        class Borrow:
            def ensure_fresh(self):
                pass

            def snapshot(self):
                return table, [], "", "", False
        kw["holdings"] = etf.EtfService(yf, Borrow()).lookthrough_weights
    return metrics.MetricService(yf, sec, sec_on, **kw)


def run_rows(symbols, table, keys, fake=False, minutes=25, log=print):
    """Every metric for these tickers, waiting (up to `minutes`) until nothing is loading."""
    import metrics
    svc = make_service(table, fake)
    keys = [k for k in keys if k in metrics.STRATEGIES]
    out, chunks = {}, [symbols[i:i + 30] for i in range(0, len(symbols), 30)]
    deadline = time.time() + minutes * 60
    t0 = time.time()
    for n, chunk in enumerate(chunks, 1):
        chunk_deadline = t0 + (deadline - t0) * n / len(chunks)
        while True:
            rows, pending = svc.get(chunk, keys)
            if not pending or time.time() > chunk_deadline:
                break
            time.sleep(2)
        for r in rows:
            out[r["symbol"]] = {"data": sorted(r["data"]), "why": r["why"], "loading": r["loading"],
                                "error": r.get("error"), "error_kind": r.get("error_kind"), "yahoo": r.get("yahoo"),
                                "large": isinstance(r["data"].get("marketCap"), (int, float))
                                and r["data"]["marketCap"] >= LARGE_CAP}
        log(f"  {n}/{len(chunks)} groups of tickers done ({time.time() - t0:.0f} s)")
    return out, keys


# ------------------------------- summarizing --------------------------------
def kind_of_reason(code):
    import metrics
    r = metrics.REASONS.get(code)
    return r[0] if r else "none"


def summarize(sample, results, keys):
    """{kind: {category: {"filled", "applies", "why": {code: n}, "unexplained"}}}"""
    import metrics
    groups = [g for g in metrics.CATALOG if g["id"] != "borrow"]
    out = {}
    for kind, syms in sample.items():
        per = {}
        for g in groups:
            gkeys = [m["key"] for m in g["metrics"] if m["key"] in keys]
            st = {"filled": 0, "applies": 0, "why": {}, "unexplained": 0, "rows_off": 0}
            for s in syms:
                r = results.get(s)
                if r is None:
                    continue
                if r.get("error"):
                    if r.get("error_kind") == "listing":       # Yahoo doesn't carry it, and says so
                        st["rows_off"] += len(gkeys)
                        st["why"]["not on Yahoo"] = st["why"].get("not on Yahoo", 0) + len(gkeys)
                    else:
                        st["applies"] += len(gkeys)
                        st["unexplained"] += len(gkeys) if "has no data" in (r["error"] or "") else 0
                        st["why"][r["error"]] = st["why"].get(r["error"], 0) + len(gkeys)
                    continue
                filled = set(r["data"])
                for k in gkeys:
                    if k in filled:
                        st["filled"] += 1
                        st["applies"] += 1
                        continue
                    code = r["why"].get(k) or ("loading" if k in r["loading"] else "no_data")
                    st["why"][code] = st["why"].get(code, 0) + 1
                    if code == "loading" or kind_of_reason(code) != "na":
                        st["applies"] += 1
                    if code in GENERIC:
                        st["unexplained"] += 1
            per[g["id"]] = st
        out[kind] = per
    return out


def most_blank(syms, results, keys, n=6):
    """[(metric, blank count, commonest reason)] for blank cells that apply, most often blank first."""
    counts = {}
    for s in syms:
        r = results.get(s)
        if not r or r.get("error"):
            continue
        for k in keys:
            if k in r["data"]:
                continue
            code = r["why"].get(k) or "loading"
            if code != "loading" and kind_of_reason(code) == "na":
                continue
            counts.setdefault(k, {}).setdefault(code, 0)
            counts[k][code] += 1
    ranked = sorted(((k, sum(c.values()), max(c, key=c.get)) for k, c in counts.items()), key=lambda x: -x[1])
    return ranked[:n]


def pct(st):
    return None if not st["applies"] else st["filled"] / st["applies"]


def fmt_pct(v):
    return "–" if v is None else f"{v:.0%}"


def large_cap_gaps(results, keys, sample):
    """Metrics no large-cap stock in the sample has, for reasons other than "doesn't apply" or "retrying"."""
    stocks = {s for k in ("plain", "large cap", "ADR", "share class") for s in sample.get(k, [])}
    large = {s: r for s, r in results.items() if s in stocks and r.get("large") and not r.get("error")}
    if len(large) < 3:
        return [], len(large)
    gaps = []
    for k in keys:
        if any(k in r["data"] for r in large.values()):
            continue
        codes = [r["why"].get(k, "loading") for r in large.values()]
        if all(c != "loading" and kind_of_reason(c) in ("na", "temp") for c in codes):
            continue
        counts = {}
        for c in codes:
            counts[c] = counts.get(c, 0) + 1
        gaps.append((k, counts))
    return gaps, len(large)


def report(sample, after, keys, before=None, before_keys=None, timing=None):
    """Markdown text."""
    import metrics
    groups = [g for g in metrics.CATALOG if g["id"] != "borrow"]
    a = summarize(sample, after, keys)
    b = summarize(sample, before, before_keys) if before is not None else None
    lines = ["## Stocks page coverage", "",
             "Fill % = filled cells out of the cells that apply (\"doesn't apply\" cells such as EPS for a fund, and "
             "rows Yahoo doesn't carry, are left out). Every blank cell has a reason; the last column counts the "
             "ones whose reason is only the generic \"Yahoo has no data for this\"." + (" Each cell: before → after." if b else ""), ""]
    head = "| Kind | n | " + " | ".join(g["label"] for g in groups) + " | Generic \"no data\" blanks |"
    lines += [head, "|" + "---|" * (len(groups) + 3)]
    for kind in KINDS:
        syms = sample.get(kind) or []
        if not syms:
            continue
        cells = []
        for g in groups:
            v = fmt_pct(pct(a[kind][g["id"]]))
            if b:
                v = f"{fmt_pct(pct(b[kind][g['id']]))} → {v}"
            cells.append(v)
        un_a = sum(st["unexplained"] for st in a[kind].values())
        un = f"{sum(st['unexplained'] for st in b[kind].values())} → {un_a}" if b else str(un_a)
        lines.append(f"| {KIND_LABEL[kind]} | {len(syms)} | " + " | ".join(cells) + f" | {un} |")
    lines += ["", "### Why cells are blank (after)", ""]
    for kind in KINDS:
        if not sample.get(kind):
            continue
        tally = {}
        for st in a[kind].values():
            for code, n in st["why"].items():
                tally[code] = tally.get(code, 0) + n
        text = ", ".join(f"{(metrics.REASONS.get(c) or (None, c))[1]} ×{n}" for c, n in sorted(tally.items(), key=lambda x: -x[1]))
        lines.append(f"- **{KIND_LABEL[kind]}** ({', '.join(sample[kind][:8])}{'…' if len(sample[kind]) > 8 else ''}): "
                     f"{text or 'nothing blank'}")
    lines += ["", "### Most often blank where they apply (after)", ""]
    for kind in KINDS:
        top = most_blank(sample.get(kind) or [], after, keys)
        if top:
            lines.append(f"- **{KIND_LABEL[kind]}**: " + ", ".join(
                f"{k} {n}/{len(sample[kind])} ({(metrics.REASONS.get(c) or (None, c))[1]})" for k, n, c in top))
    gaps, n_large = large_cap_gaps(after, keys, sample)
    lines += ["", f"### Metrics blank for every large cap ({n_large} in the sample)", ""]
    if n_large < 3:
        lines.append("Too few large caps in the sample to check.")
    elif not gaps:
        lines.append("None: every metric has a value for at least one large cap.")
    for k, counts in gaps:
        why = ", ".join(f"{(metrics.REASONS.get(c) or (None, c))[1]} ×{n}" for c, n in counts.items())
        lines.append(f"- **{k}** ({metrics.GROUP_OF.get(k)}): {why}")
    if timing:
        lines += ["", "### Load time: 30 tickers, every preset tab", "", timing]
    return "\n".join(lines) + "\n", gaps, n_large


# ------------------------------- timing -------------------------------------
def timing_run(symbols, table, fake=False, log=print):
    """Like the page: the open tab first (polled every 4 s), then the other tabs fetched ahead with
    spare Yahoo capacity, from cold caches. Returns (Markdown lines, total seconds)."""
    import metrics
    tabs = [g for g in metrics.CATALOG if g.get("preset")]
    svc = make_service(table, fake)
    t0 = time.time()
    out = []
    first = [m["key"] for m in tabs[0]["metrics"]]
    while svc.get(symbols, first)[1]:
        time.sleep(4)
    out.append(f"- {tabs[0]['label']} (the open tab): {time.time() - t0:.0f} s")
    rest = [m["key"] for g in tabs[1:] for m in g["metrics"]]
    while svc.get(symbols, rest, prefetch=True)[1]:        # the other tabs in one request, like the page
        time.sleep(2)
    out.append(f"- The other {len(tabs) - 1} tabs (fetched ahead while idle): ready at {time.time() - t0:.0f} s")
    total = time.time() - t0
    left = sum(svc.get(symbols, [m["key"] for m in g["metrics"]])[1] for g in tabs)
    out.append(f"- **All {len(tabs)} tabs for {len(symbols)} tickers: {total:.0f} s** "
               f"({'every tab then opens with nothing left to load' if not left else f'{left} rows still loading'})")
    log("\n".join(out))
    return "\n".join(out), total


TIMING_SET = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "XOM", "JNJ", "PG", "KO", "WMT", "DIS",
              "GME", "AMC", "BYND", "PLTR", "RIVN", "SOFI", "BRK.B", "ABR.PRD", "AAC.U", "TCEHY", "BABA",
              "SPY", "QQQ", "SOXL", "TSLL", "JEPI"]


# ------------------------------- main ---------------------------------------
def load_table(fake):
    if fake:
        sys.path.insert(0, str(ROOT / "tests"))
        import fake_yf
        t = {}
        for ysym in fake_yf.PROFILES:
            import common
            t[common.ibkr_symbol(ysym)] = {"name": (fake_yf.PROFILES[ysym]["info"] or {}).get("longName") or ysym,
                                           "isin": None, "fee": 1.0, "shares": 1000}
        t.update({k: dict(v, fee=1.0, shares=1000) for k, v in fake_yf.IBKR.items()})
        t["LU.OLD"] = {"name": "Lufax Holding Ltd-Adr", "isin": None}
        return t
    from common import download_usa, parse_borrow_text
    return parse_borrow_text(download_usa(timeout=90))[0]


def child(args, code_dir, sample_file, json_file):
    """Run the same sample on another copy of the code (e.g. main) in a separate process."""
    cmd = [sys.executable, str(Path(__file__).resolve()), "--code-dir", str(code_dir), "--sample-file", str(sample_file),
           "--json", str(json_file), "--minutes", str(args.minutes)] + (["--fake"] if args.fake else [])
    subprocess.run(cmd, check=True)
    return json.loads(Path(json_file).read_text())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--per-kind", type=int, default=20)
    ap.add_argument("--seed", default=None)
    ap.add_argument("--compare", metavar="REF")
    ap.add_argument("--fail-on-gaps", action="store_true")
    ap.add_argument("--timing", action="store_true")
    ap.add_argument("--timing-only", action="store_true")
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--json")
    ap.add_argument("--summary")
    ap.add_argument("--minutes", type=float, default=25)
    ap.add_argument("--code-dir")                 # internal: run with another copy of the code
    ap.add_argument("--sample-file")              # internal: the sample to run
    args = ap.parse_args()

    if args.code_dir:                             # child process for --compare
        sys.path.insert(0, args.code_dir)
        sample = json.loads(Path(args.sample_file).read_text())
        table = sample.pop("_table")
        import metrics
        syms = [s for k in KINDS for s in sample.get(k, [])]
        rows, keys = run_rows(syms, table, metrics.YAHOO_KEYS, fake=args.fake, minutes=args.minutes)
        Path(args.json).write_text(json.dumps({"rows": rows, "keys": keys}))
        return 0

    sys.path.insert(0, str(ROOT))
    import common
    import metrics
    table = load_table(args.fake)
    if args.fake:
        sys.path.insert(0, str(ROOT / "tests"))
        import fake_yf
        common.SYMBOLS = common.SymbolMap(quotes=fake_yf.quotes, search=fake_yf.search)
        quotes = fake_yf.quotes

        def fake_quotes(syms):
            q = quotes(syms)
            for s, v in q.items():
                p = fake_yf.PROFILES.get(s)
                if p and p.get("info"):
                    v.update({k: p["info"][k] for k in ("marketCap", "exchange") if k in p["info"]})
            return q
        quote_fn = fake_quotes
    else:
        quote_fn = common.yahoo_quotes
    if args.timing_only:
        tsyms = [s for s in TIMING_SET if s in table] if not args.fake else sorted(table)[:30]
        text, _ = timing_run(tsyms, table, fake=args.fake)
        if args.summary:
            with open(args.summary, "a") as f:
                f.write("### Load time: 30 tickers, every preset tab\n\n" + text + "\n")
        return 0
    seed = args.seed or f"{date.today().isocalendar()[0]}-W{date.today().isocalendar()[1]:02d}"
    sample = stratified_sample(table, args.per_kind, seed, quote_fn)
    for k in KINDS:
        print(f"  {KIND_LABEL[k]:20} {len(sample[k]):3}  {', '.join(sample[k][:10])}")
    syms = [s for k in KINDS for s in sample[k]]

    before = before_keys = None
    if args.compare:
        tmp = Path(tempfile.mkdtemp(prefix="coverage-"))
        wt = tmp / "code"
        subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", str(wt), args.compare], check=True,
                       stdout=subprocess.DEVNULL)
        try:
            sf = tmp / "sample.json"
            sf.write_text(json.dumps(dict(sample, _table={s: table[s] for s in syms if s in table})))
            print(f"\nRunning the sample on {args.compare}'s code…", flush=True)
            got = child(args, wt, sf, tmp / "before.json")
            before, before_keys = got["rows"], got["keys"]
        finally:
            subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(wt)], check=False)
    print("\nRunning the sample on this code…", flush=True)
    after, keys = run_rows(syms, table, metrics.YAHOO_KEYS, fake=args.fake, minutes=args.minutes)

    timing = None
    if args.timing:
        print("\nTiming 30 tickers on every preset tab (cold caches)…", flush=True)
        time.sleep(61)                    # a fresh minute of the shared Yahoo rate limit
        tsyms = [s for s in TIMING_SET if s in table] if not args.fake else sorted(table)[:30]
        timing, _ = timing_run(tsyms, table, fake=args.fake)

    text, gaps, n_large = report(sample, after, keys, before, before_keys, timing)
    print("\n" + text)
    if args.summary:
        with open(args.summary, "a") as f:
            f.write(text)
    if args.json:
        Path(args.json).write_text(json.dumps({"sample": sample, "after": after, "before": before, "keys": keys}))
    if args.fail_on_gaps and gaps:
        print(f"FAILED: {len(gaps)} metric(s) are blank for every one of the {n_large} large caps sampled: "
              + ", ".join(k for k, _ in gaps) + ". Yahoo may have renamed or dropped these fields; see the table above.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
