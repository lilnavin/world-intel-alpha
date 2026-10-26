"""One-shot: seed the reconciliation pool from the archived briefs.

Run ONCE at deployment, then delete this file. It exists because the pool is
written going forward but the filings it reconciles against were already filed:
at the time of writing, 203 of the 243 companies published in the previous 31
briefs had filed a 10-Q or 10-K, and 191 of those filings were never read.
Without a seed those 191 stay unread, because a company only enters the pool on
the day it is published and none of them will be published again.

The brief HTML is the only surviving record of who appeared -- the pipeline
ledger prunes at 14 days and metrics stores counts, not tickers -- so this
parses the archive. That is exactly why it is throwaway: parsing rendered HTML
back into data is not a thing to depend on, and after this runs, nothing does.

Backfilled entries carry no `snapshot`: the beat-1 numbers were never persisted
for them. A block built from one therefore renders without the recalled
"as reported" line. Self-correcting within a week as real entries replace them.

    python backfill_pool.py [--days 30] [--dry]
"""
from worldintel import config as wi_config
import argparse
import datetime
import os
import re
import sys

import requests  # noqa: E402

from worldintel.core import earnings_report  # noqa: E402
from worldintel.core import post_earnings_pool  # noqa: E402


# A write-up card's ticker sits in the <h3> of an `article class='card pe'`; the
# also-reported names are "TICKER &mdash; note" pairs in one paragraph. Both are
# matched narrowly so a ticker-looking string elsewhere on the page cannot leak
# into the pool.
_CARD_RE = re.compile(r'<h3>.*?<span class="ticker">([A-Z0-9.\-]+)</span>')
_ALSO_BLOCK_RE = re.compile(r'<p class="muted pe-also">Also reported:(.*?)</p>', re.S)
_ALSO_TICKER_RE = re.compile(r"(?:^|;\s)([A-Z0-9.\-]{1,6})\s+&mdash;")


def _read_brief(path: str):
    """(card_tickers, also_tickers) for one archived brief."""
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    cards = _CARD_RE.findall(html)
    block = _ALSO_BLOCK_RE.search(html)
    also = _ALSO_TICKER_RE.findall(block.group(1)) if block else []
    return cards, also


def _plain_ua(note: str = "backfill") -> str:
    return wi_config.get().plain_user_agent(note)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=post_earnings_pool.POOL_WINDOW_DAYS)
    ap.add_argument("--dry", action="store_true", help="report, write nothing")
    args = ap.parse_args()

    cfg = wi_config.get()
    kb_root, media_dir = cfg.data_root, cfg.media_dir
    today = datetime.datetime.now(cfg.timezone).date()
    session = requests.Session()
    session.headers.update({"User-Agent": _plain_ua()})
    ciks = earnings_report._load_cik_map(session)

    pool = post_earnings_pool.read_pool(kb_root)
    before = len(pool)
    briefs = seeded = no_cik = 0

    # Oldest first, so `first_seen` is the EARLIEST brief a ticker appeared in
    # and the window ages it out on the right day. Reversed, a name published
    # twice would look newer than it is and linger.
    for n in range(args.days, -1, -1):
        day = (today - datetime.timedelta(days=n)).isoformat()
        path = os.path.join(media_dir, f"brief-{day}.html")
        if not os.path.exists(path):
            continue
        try:
            cards, also = _read_brief(path)
        except wi_config.ConfigError:
            raise
        except Exception as exc:
            print(f"  {day}: unreadable ({type(exc).__name__}), skipped")
            continue
        briefs += 1

        for ticker, origin in ([(t, "card") for t in cards]
                               + [(t, "also") for t in also]):
            ticker = ticker.upper()
            cik = ciks.get(ticker, "")
            if not cik:
                no_cik += 1
            # The report date is unrecoverable from the HTML, so the brief date
            # stands in. It only has to make the key unique and stable; the join
            # is on CIK, and `build_fundamentals` finds the anchor itself.
            key = f"{ticker}:{day}"
            if key in pool:
                continue
            pool[key] = {
                "ticker": ticker, "cik": cik, "name": "", "origin": origin,
                "first_seen": day, "snapshot": {},
                "line_shown": None, "block_shown": None,
            }
            seeded += 1

    pool = post_earnings_pool.prune(pool, today.isoformat())
    print(f"\n{briefs} briefs read, {seeded} entries seeded "
          f"({no_cik} with no CIK, which can never match)")
    print(f"pool: {before} -> {len(pool)} after prune")
    if args.dry:
        print("--dry: nothing written")
        return 0
    post_earnings_pool.write_pool(kb_root, pool)
    print(f"written to {os.path.join(kb_root, post_earnings_pool.POOL_FILE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
