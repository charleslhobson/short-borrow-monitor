"""
Live check of iBorrowDesk's data feed, which the borrow fee charts use for history
from before this site started recording.

For each ticker it shows the fields iBorrowDesk sent, how many daily and intraday
readings there are, the first and last few, and compares iBorrowDesk's latest fee with
IBKR's own file, to confirm both are in percent per year.

Usage:
    python tools/check_iborrowdesk.py                 # GME and AAPL
    python tools/check_iborrowdesk.py TSLL SOXL --save tests/fixtures
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import borrow_history as bh  # noqa: E402
from common import download_usa, normalize, parse_borrow_text  # noqa: E402


def show(rows, label):
    print(f"  {label}: {len(rows)} readings")
    for ts, fee, reb, sh in rows[:2] + ([None] if len(rows) > 4 else []) + rows[-2:]:
        if ts is None:
            print("    ...")
        else:
            print(f"    {ts:%Y-%m-%d %H:%M} UTC  fee {fee}  rebate {reb}  shares {sh}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", default=["GME", "AAPL"])
    ap.add_argument("--save", help="folder to save each raw reply in, as iborrowdesk_<SYM>.json")
    args = ap.parse_args(argv)
    try:
        ibkr, as_of = parse_borrow_text(download_usa())
        print(f"IBKR file as of {as_of}: {len(ibkr):,} tickers")
    except Exception as e:
        ibkr = {}
        print(f"Couldn't download IBKR's file ({e}); skipping the unit check.")
    bad = 0
    for raw_sym in args.symbols:
        sym = normalize(raw_sym)
        print(f"\n{sym}")
        try:
            raw = bh.fetch_iborrowdesk(sym)
        except Exception as e:
            print(f"  FAILED to fetch: {type(e).__name__}: {e}")
            bad += 1
            continue
        if args.save:
            path = Path(args.save) / f"iborrowdesk_{sym}.json"
            path.write_text(json.dumps(raw, indent=1))
            print(f"  saved {path}")
        if isinstance(raw, dict):
            print(f"  top-level fields: {sorted(raw)}")
            for key in ("daily", "real_time"):
                items = raw.get(key)
                if isinstance(items, list) and items:
                    print(f"  {key} fields: {sorted(items[0]) if isinstance(items[0], dict) else type(items[0])}")
        try:
            p = bh.parse_iborrowdesk(raw)
        except ValueError as e:
            print(f"  FAILED to parse: {e}")
            bad += 1
            continue
        show(p["daily"], "daily")
        show(p["real_time"], "intraday")
        live = ibkr.get(sym)
        if live:
            k = bh.unit_scale(p, live["fee"])
            last = (p["real_time"] or p["daily"] or [(None, None)])[-1]
            print(f"  IBKR fee now {live['fee']}; iBorrowDesk latest {last[1]} -> "
                  + ("same units" if k == 1.0 else f"would be rescaled by {k}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
