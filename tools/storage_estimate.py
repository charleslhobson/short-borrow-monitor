"""
How fast will the borrow history database grow? Applies the history's write rules
(borrow_history.should_write) to real IBKR files and projects MB per month.

Usage:
    python tools/storage_estimate.py usa_1.txt usa_2.txt usa_3.txt   # saved copies, oldest first
    python tools/storage_estimate.py --watch 3 --every 60            # download 3 copies an hour apart
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import borrow_history as bh  # noqa: E402
from common import download_usa, parse_borrow_text  # noqa: E402

BYTES_PER_ROW = 123        # measured on Postgres 16, primary key index included


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*")
    ap.add_argument("--watch", type=int, default=0, help="download this many copies instead")
    ap.add_argument("--every", type=float, default=60, help="minutes between downloads (default 60)")
    args = ap.parse_args(argv)
    tables = []
    if args.watch:
        for i in range(args.watch):
            if i:
                time.sleep(args.every * 60)
            tables.append(parse_borrow_text(download_usa()))
            print(f"downloaded copy {i + 1}: {len(tables[-1][0]):,} tickers, as of {tables[-1][1]}", flush=True)
    else:
        tables = [parse_borrow_text(Path(f).read_text(encoding="utf-8", errors="replace")) for f in args.files]
    if len(tables) < 2:
        ap.error("need at least two copies of the file")
    store = bh.MemoryStore()
    counts = []
    for i, (table, as_of) in enumerate(tables):
        rows = bh.changes(store.latest(), table, datetime(2026, 1, 1, i, tzinfo=timezone.utc))
        store.insert_points(rows)
        counts.append(len(rows))
        print(f"copy {i + 1} (as of {as_of}): {len(rows):,} points would be saved")
    per_run = sum(counts[1:]) / (len(counts) - 1)
    hours = args.every / 60 if args.watch else 1.0
    per_month = per_run / hours * 24 * 30.4
    print(f"\nFirst run: {counts[0]:,} points ({counts[0] * BYTES_PER_ROW / 1e6:.1f} MB).")
    print(f"After that: about {per_run:,.0f} points per run" + ("" if args.watch else " (assuming copies an hour apart)")
          + f" = {per_month:,.0f} points, {per_month * BYTES_PER_ROW / 1e6:,.0f} MB per month.")


if __name__ == "__main__":
    sys.exit(main())
