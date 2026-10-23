"""
Record IBKR's borrow fees to the history database, then exit.

GitHub Actions runs this every hour (.github/workflows/record-borrow.yml), because
Render's free plan puts the website to sleep when nobody is visiting. Only changes are
saved (see borrow_history.should_write), and old readings are thinned once a day.

Usage:
    DATABASE_URL=postgresql://... python tools/record_borrow.py
    python tools/record_borrow.py --file usa.txt        # record a saved copy of the file
    python tools/record_borrow.py --prune               # thin old readings now
"""

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import borrow_history as bh  # noqa: E402
from common import download_usa, parse_borrow_text  # noqa: E402


def download(attempts=3):
    for i in range(attempts):
        try:
            return download_usa()
        except Exception as e:
            print(f"Download attempt {i + 1} failed: {type(e).__name__}: {e}", flush=True)
            if i + 1 < attempts:
                time.sleep(10)
    raise RuntimeError("couldn't download IBKR's usa.txt")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", help="record this saved usa.txt instead of downloading it")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--prune", action="store_true", help="thin old readings now")
    g.add_argument("--no-prune", action="store_true", help="don't thin old readings this run")
    args = ap.parse_args(argv)

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("::notice::DATABASE_URL isn't set, so nothing was recorded. "
              "Add it as a repository secret (see README, Borrow fee history).")
        return 0
    store = bh.PgStore(url)
    t0 = time.time()
    try:
        text = Path(args.file).read_text(encoding="utf-8", errors="replace") if args.file else download()
        table, as_of = parse_borrow_text(text)
        if not table:
            raise RuntimeError("the borrow file had no tickers")
    except Exception as e:
        store.record_run(bh.utcnow(), False, 0, f"download: {e}"[:300])
        print(f"::error::{e}")
        return 1
    prune = True if args.prune else False if args.no_prune else None
    n, note = bh.record(store, table, as_of, prune=prune)
    size = store.size_bytes()
    print(f"{note}. File as of {as_of} (New York). Database {bh.storage_text(size)}. "
          f"Took {time.time() - t0:.1f}s.", flush=True)
    if size and size > 0.8 * bh.STORAGE_LIMIT_MB * 1e6:
        print(f"::warning::The borrow history database is at {bh.storage_text(size)}.")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
