#!/usr/bin/env python3
"""Re-fetch stored days so they carry impressions and clicks as well as spend.

Days settled before 2026-08-26 were stored with `spend` and nothing else, so CTR and CPM
are blank for them. Unlike budgets this IS recoverable: Meta's insights are not
retention-limited the way the activity log is, so the same days can be asked for again
with the extra fields.

It is not free -- one brand-day is a full ad-level insights pull, 5 to 7 seconds, against
a limit that binds on request TIME. This drives the same bounded batch the scheduled
endpoint uses (`P.reach_backfill`), in a loop, so the two can never drift apart.

    python3 tools/backfill_reach.py --brand postly --dry-run
    python3 tools/backfill_reach.py --all --budget 90
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as C          # noqa: E402
import history as H         # noqa: E402
import postly_cpt as P      # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--budget", type=float, default=90, help="seconds per batch")
    ap.add_argument("--rounds", type=int, default=50, help="max batches per brand")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    brands = list(C.BRANDS) if a.all else [a.brand]
    if not brands or brands == [None]:
        ap.error("give --brand or --all")
    if not H.available():
        sys.exit("no history store configured (set HISTORY_URL)")

    for b in brands:
        for i in range(a.rounds):
            r = P.reach_backfill(b, budget_s=a.budget, dry=a.dry_run)
            for d in r["days"]:
                if d.get("skipped"):
                    print(f"   {d['date']}  {d['skipped']}")
                elif d.get("error"):
                    print(f"   {d['date']}  FAILED {d['error']}")
                else:
                    print(f"   {d['date']}  {d['rows']:5} rows  "
                          f"{d['impressions']:12,} impressions  {d['took']:5.1f}s")
            print(f"{b}: batch {i+1} wrote {r['written']}, failed {r['failed']}, "
                  f"{r['pending_after']} left  ({r['took']}s)")
            if r["throttled"]:
                print(f"{b}: Meta is rate-limiting. Stopping; re-run later.")
                break
            if not r["pending_after"] or a.dry_run:
                break
            time.sleep(2)
        print()


if __name__ == "__main__":
    main()
