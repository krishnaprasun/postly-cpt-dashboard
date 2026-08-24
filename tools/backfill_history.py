#!/usr/bin/env python3
"""Fill the ads-history store with settled days.

    python3 tools/backfill_history.py --days 60
    python3 tools/backfill_history.py --brand funda --since 2026-07-01 --until 2026-08-21
    python3 tools/backfill_history.py --days 60 --dry-run

Already-stored days are skipped, so this is safe to re-run and safe to interrupt: it
picks up where it left off. Days that have not settled yet are refused rather than
written, because a day stored while its numbers are still moving stays wrong forever —
the point of the store is that nothing checks it again.

It goes one day at a time on purpose. The alternative, pulling a wide range and slicing
it, is fewer requests but pays Meta's per-request TIME budget in one long call, and time
is the quota that actually trips code 17 on this app's development-access tier.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as C          # noqa: E402
import history as H         # noqa: E402
import postly_cpt as P      # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", action="append",
                    help="repeatable; default is every configured brand")
    ap.add_argument("--days", type=int, default=30,
                    help="how far back from the newest settled day (default 30)")
    ap.add_argument("--since"); ap.add_argument("--until")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=1.0,
                    help="pause between days, to stay clear of Meta's time budget")
    a = ap.parse_args()

    if not H.available():
        sys.exit("No history store configured. Set HISTORY_URL (and HISTORY_TOKEN, or "
                 "leave it in ~/.anthropic/ads_history_token).")

    today = P.today_ist()
    last = a.until or H.settled_through(today)
    if last > H.settled_through(today):
        last = H.settled_through(today)
        print(f"  --until trimmed to {last}: newer days have not settled "
              f"({H.SETTLE_DAYS}-day window).")
    first = a.since or (datetime.strptime(last, "%Y-%m-%d")
                        - timedelta(days=a.days - 1)).strftime("%Y-%m-%d")
    brands = a.brand or list(C.BRANDS)

    dates = []
    d = datetime.strptime(first, "%Y-%m-%d").date()
    while d.strftime("%Y-%m-%d") <= last:
        dates.append(d.strftime("%Y-%m-%d")); d += timedelta(days=1)

    print(f"window {first} → {last}  ({len(dates)} days)  brands: {', '.join(brands)}")
    grand = 0
    for brand in brands:
        have = set(H.have(brand))
        todo = [x for x in dates if x not in have]
        print(f"\n{brand}: {len(have & set(dates))} already stored, {len(todo)} to fetch")
        if a.dry_run:
            continue
        for i, day in enumerate(todo, 1):
            t0 = time.time()
            try:
                r = P.snapshot(brand, day)
            except P.BranchThrottled as ex:
                # Ploughing on is what turned one throttle into an eight-day hole: every
                # further day is another request against the limiter holding the door
                # shut. Stop this brand, say exactly what is missing, and let a re-run
                # pick it up — the backfill skips whatever is already stored.
                left = todo[i - 1:]
                print(f"  {day}  Branch is rate-limiting — STOPPING {brand}. {ex}")
                print(f"  {len(left)} day(s) still missing for {brand}: "
                      f"{left[0]} → {left[-1]}. Re-run this command later to fill them.")
                break
            except Exception as ex:
                print(f"  {day}  FAILED  {type(ex).__name__}: {str(ex)[:120]}")
                continue
            if not r.get("ok"):
                print(f"  {day}  skipped — {r.get('reason') or r.get('error')}")
                continue
            grand += 1
            tr = " ".join(f"{k}={v:,}" for k, v in sorted(r["trials"].items()))
            print(f"  {day}  {r['ads']:>5} ad-rows  spend={r['spend']:>12,.2f}  {tr}"
                  f"  ({time.time()-t0:.1f}s)  [{i}/{len(todo)}]")
            time.sleep(a.sleep)
    print(f"\nwrote {grand} day(s).")


if __name__ == "__main__":
    main()
