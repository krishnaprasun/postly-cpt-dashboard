#!/usr/bin/env python3
"""Re-fetch stored days so they carry impressions and clicks as well as spend.

Days settled before 2026-08-26 were stored with `spend` and nothing else, so CTR and CPM
are blank for them. Unlike budgets, this IS recoverable: Meta's insights are not
retention-limited the way the activity log is, so the same days can simply be asked for
again with the extra fields.

It is not free. One brand-day is a full ad-level insights pull -- 8 to 12 seconds and a
real slice of the request-time budget Meta rate-limits on -- so 97 days across three
brands is the better part of an hour of Meta time. Hence: resumable, one day at a time,
and it stops on a throttle rather than feeding it.

    python3 tools/backfill_reach.py --brand postly --days 30
    python3 tools/backfill_reach.py --all --dry-run

Branch trials in the stored day are read and written back UNTOUCHED. This rewrites the
Meta half only; losing a day's trials to a reach backfill would be an absurd trade.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as C          # noqa: E402
import history as H         # noqa: E402
import postly_cpt as P      # noqa: E402


def already_has_reach(day):
    """True if every account's rows in this stored day already report impressions."""
    meta = (day or {}).get("meta") or {}
    rows = [r for rows_ in meta.values() for r in rows_]
    return bool(rows) and all(P.has_imp(r) for r in rows)


def one_day(brand, date, stored, dry):
    B = C.brand(brand)
    fresh = {}
    for a in B["accounts"]:
        fresh[a["id"]] = P.meta_insights_daily(a["id"], date, date)
    n = sum(len(v) for v in fresh.values())
    imp = sum(P._num(r.get("impressions")) for v in fresh.values() for r in v)
    if dry:
        return True, n, imp
    # The stored day's Branch half is carried over exactly as it was.
    ok = H.put(brand, date, fresh, (stored or {}).get("branch") or {})
    return ok, n, imp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--days", type=int, default=0,
                    help="only the N most recent stored days (default: all of them)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=1.0)
    a = ap.parse_args()
    brands = list(C.BRANDS) if a.all else [a.brand]
    if not brands or brands == [None]:
        ap.error("give --brand or --all")
    if not H.available():
        sys.exit("no history store configured (set HISTORY_URL)")

    for b in brands:
        have = sorted(H.have(b) or [])
        if a.days:
            have = have[-a.days:]
        if not have:
            print(f"{b}: nothing stored"); continue
        raw = H.fetch_raw(b, have)
        todo = [d for d in have if not already_has_reach(raw.get(d))]
        print(f"{b}: {len(have)} stored, {len(todo)} without impressions"
              f"{' (dry run)' if a.dry_run else ''}")
        done = failed = 0
        for i, d in enumerate(todo, 1):
            t = time.time()
            try:
                ok, n, imp = one_day(b, d, raw.get(d), a.dry_run)
            except Exception as ex:
                # A throttle here is Meta saying stop. Feeding it makes the wait longer,
                # and the run is resumable, so stopping costs nothing but time.
                print(f"   {d}  FAILED {type(ex).__name__}: {str(ex)[:120]}")
                failed += 1
                if "rate limit" in str(ex).lower() or "RateLimited" in type(ex).__name__:
                    print("   stopping: Meta is rate-limiting. Re-run later; "
                          "days already written are skipped.")
                    break
                continue
            done += ok
            print(f"   {d}  {n:5} rows  {imp:12,.0f} impressions  "
                  f"{'written' if ok and not a.dry_run else 'would write'}  "
                  f"({time.time()-t:.1f}s)  [{i}/{len(todo)}]")
            time.sleep(a.sleep)
        print(f"{b}: {done} written, {failed} failed\n")


if __name__ == "__main__":
    main()
