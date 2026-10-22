"""
Before/after table of how much of each ETF's holdings get an IBKR borrow fee.

Needs internet access to IBKR's FTP site, SEC EDGAR and Yahoo, and SEC_USER_AGENT
set to your name and email (without it, only Yahoo's top 10 holdings are used):

    set SEC_USER_AGENT=Jane Smith jane@example.com          (Windows)
    export SEC_USER_AGENT="Jane Smith jane@example.com"     (Mac / Linux)
    python tools/match_report.py TSLL NVDL CONL MSTU SOXL IVV IWM EFA HYG TSLA

"Before" re-runs the original holdings parser and fee calculation (copied below)
on the same SEC filing and IBKR US file. "After" is the current code.
"""

import ftplib
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import etf  # noqa: E402

DEFAULT = ["TSLL", "NVDL", "CONL", "MSTU", "SOXL", "IVV", "IWM", "EFA", "HYG", "TSLA"]


# ---------------- the original code, kept only for the "before" column ----------------
def legacy_parse_nport(xml_bytes):
    root = ET.fromstring(xml_bytes)
    holdings = []
    for el in root.iter():
        if etf._local(el.tag) != "invstOrSec":
            continue
        h = {}
        for c in el.iter():
            cn = etf._local(c.tag)
            if cn in ("name", "title", "cusip", "pctVal", "assetCat", "payoffProfile") and cn not in h:
                h[cn] = (c.text or "").strip()
            elif cn in ("isin", "ticker") and cn not in h:
                h[cn] = (c.get("value") or "").strip()
        w = etf.to_float(h.get("pctVal"))
        if w is None or w <= 0 or h.get("payoffProfile", "").lower() == "short":
            continue
        cusip = (h.get("cusip") or "").strip().upper()
        isin = h.get("isin") or (etf.cusip_to_isin(cusip) if re.fullmatch(r"[0-9A-Z]{9}", cusip) else None)
        holdings.append({"ticker": (h.get("ticker") or "").upper() or None,
                         "isin": (isin or "").upper() or None, "w": w / 100.0})
    return holdings


def legacy_theoretical_fee(holdings, by_sym, by_isin, gc):
    total = sum(h["w"] for h in holdings)
    if total <= 0:
        return None, None
    acc, matched = 0.0, 0.0
    for h in holdings:
        loan = (by_isin.get(h["isin"]) if h.get("isin") else None) or \
               (by_sym.get(h["ticker"]) if h.get("ticker") else None)
        fee = loan["fee"] if loan and loan.get("fee") is not None else None
        w = h["w"] / total
        acc += w * (fee if fee is not None else gc)
        matched += w if fee is not None else 0.0
    return acc, matched


# ----------------------------------- data -----------------------------------
def download_ibkr():
    with ftplib.FTP(app.FTP_HOST, timeout=60) as ftp:
        ftp.login(app.FTP_USER, "")
        us, _ = app.parse_borrow_text(app.ftp_download(ftp, app.FTP_FILE))
        foreign = {}
        for name in sorted({n.rsplit("/", 1)[-1] for n in ftp.nlst()}):
            low = name.lower()
            if low.endswith(".txt") and low != app.FTP_FILE:
                table, _ = app.parse_borrow_text(app.ftp_download(ftp, name))
                if table:
                    foreign[low[:-4]] = table
    return us, foreign


class Static:
    def __init__(self, table=None, tables=None):
        self.table, self.tables = table, tables

    def ensure_fresh(self):
        pass

    def snapshot(self):
        if self.tables is not None:
            return self.tables, False, ""
        return self.table, [], "", "", False


def pct(x, digits=1):
    return "–" if x is None else f"{x * 100:.{digits}f}%"


def main(symbols):
    import yfinance as yf
    if not etf.SEC_USER_AGENT:
        print("Note: SEC_USER_AGENT isn't set, so holdings come from Yahoo's top 10 only.\n")
    print("Downloading IBKR's stock loan files…", flush=True)
    us, foreign = download_ibkr()
    print(f"  US: {len(us):,} rows; other countries: {', '.join(sorted(foreign)) or 'none'}\n", flush=True)
    by_isin = {e["isin"]: e for e in us.values() if e.get("isin")}
    etf.WAIT_SEC = 120
    svc = etf.EtfService(yf, Static(table=us), Static(tables=foreign))

    head = ("ETF", "Holdings", "Before: theo fee", "Before: % matched", "After: theo fee",
            "After: % non-cash matched", "Gross long stock", "Assumed-rate weight", "Positions")
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for sym in symbols:
        d = svc.holdings_detail(sym, etf.GC_DEFAULT, etf.CASH_DEFAULT)
        if d.get("single_stock"):
            print(f"| {sym} | single stock | – | – | – | – | – | – | – |", flush=True)
            continue
        if d.get("error") or d.get("pending"):
            print(f"| {sym} | {d.get('error') or 'still loading'} | – | – | – | – | – | – | – |", flush=True)
            continue
        before_fee = before_matched = None
        if d.get("source") == "SEC N-PORT" and d.get("url"):
            old = legacy_parse_nport(etf.SEC.get(d["url"] + "primary_doc.xml"))
            before_fee, before_matched = legacy_theoretical_fee(old, us, by_isin, etf.GC_DEFAULT)
        t = d["theo"]
        print(f"| {sym} | {d['source']} {d.get('as_of') or ''} | {pct(before_fee and before_fee / 100, 2)} | "
              f"{pct(before_matched)} | {pct(t['fee'] / 100, 2)} | {pct(t['coverage'])} | "
              f"{pct(t['gross_long_equity'], 0)} | {pct(t['assumed_weight'])} | {t['count']} |", flush=True)
        for u in t["unmatched"][:5]:
            print(f"|   ↳ assumed: {u['name']} ({pct(u['weight'], 2)}) | {u['detail'][:120]} |||||||", flush=True)


if __name__ == "__main__":
    main([s.upper() for s in sys.argv[1:]] or DEFAULT)
