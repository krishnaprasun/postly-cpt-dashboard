#!/usr/bin/env python3
"""Add the Branch install series to days that were stored before installs were pulled.

Installs live in the stored day under the same {event: {ad_name: n}} shape as the two
trial events, keyed `inst`. Days written before that carry trials only, so the Trends and
Matrix views would show a hole where the install metrics should be.

Cheap on purpose. Only the install series is fetched -- one Branch query per 7-day chunk
per brand rather than a full re-pull of every event -- so the whole history costs on the
order of forty requests, not a thousand. That distinction is the difference between a
backfill nobody notices and the one that exhausted Branch's burst limit and took
SpeakEasy's dashboard down.

Nothing already stored is touched: trials, the channel tags and the entire Meta side are
written back exactly as they were, and only the `inst` key is added.

Usage
  python3 tools/backfill_installs.py --dry-run
  python3 tools/backfill_installs.py --brands postly --limit 20
  python3 tools/backfill_installs.py
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as C           # noqa: E402
import history as H          # noqa: E402
import postly_cpt as P       # noqa: E402


def chunks(dates, n=P.BRANCH_MAX_SPAN):
    out, cur = [], []
    for d in dates:
        dd = datetime.strptime(d, "%Y-%m-%d").date()
        if cur and (len(cur) >= n or
                    dd != datetime.strptime(cur[-1], "%Y-%m-%d").date() + timedelta(1)):
            out.append(cur); cur = []
        cur.append(d)
    if cur:
        out.append(cur)
    return out


def installs_daily(since, until, B, tries=P.BRANCH_BACKFILL_TRIES):
    """{date: {ad_name_key: n}} for installs only."""
    bkey, bsecret = B["branch"]
    out = {}
    d = datetime.strptime(since, "%Y-%m-%d").date()
    endd = datetime.strptime(until, "%Y-%m-%d").date()
    AD = "last_attributed_touch_data_tilde_ad_name"
    PAR = "last_attributed_touch_data_tilde_advertising_partner_name"
    while d <= endd:
        ce = min(d + timedelta(days=P.BRANCH_MAX_SPAN - 1), endd)
        rows, _tc = P._branch_pages({
            "branch_key": bkey, "branch_secret": bsecret,
            "start_date": d.strftime("%Y-%m-%d"), "end_date": ce.strftime("%Y-%m-%d"),
            "dimensions": [AD, PAR], "granularity": "day",
            "aggregation": "unique_count",
            "data_source": P.INSTALL_SOURCE}, tries=tries)
        for row in rows:
            day = (row.get("timestamp") or "")[:10]
            if not day:
                continue
            res = row.get("result", {})
            name = res.get(AD) or P._nameless_key(res.get(PAR))
            out.setdefault(day, {})
            out[day][name] = out[day].get(name, 0) + res.get("unique_count", 0)
        d = ce + timedelta(days=1)
    return out


def repair_brand(brand, limit=None, dry=False, since=None, pause=2.0):
    B = C.brand(brand)
    if not B.get("branch"):
        print(f"  {brand}: no Branch app configured — nothing to do")
        return 0, 0
    stored = sorted(d for d in H.have(brand) if not since or d >= since)
    if not stored:
        print(f"  {brand}: nothing stored")
        return 0, 0

    need, raw_all = [], {}
    for i in range(0, len(stored), 15):
        raw = H.fetch_raw(brand, stored[i:i + 15])
        raw_all.update(raw)
        for d, day in raw.items():
            if not (day.get("branch") or {}).get(P.INSTALL_KEY):
                need.append(d)
    need.sort()
    print(f"  {brand}: {len(stored)} stored, {len(need)} without installs"
          + (f" (taking the oldest {limit})" if limit and limit < len(need) else ""))
    if limit:
        need = need[:limit]
    if not need:
        return 0, 0

    done = failed = 0
    for run in chunks(need):
        try:
            got = installs_daily(run[0], run[-1], B)
        except P.BranchThrottled:
            left = need[need.index(run[0]):]
            print(f"  {brand}: Branch is throttling — stopping. Not filled: "
                  f"{left[0]}..{left[-1]} ({len(left)} day(s))")
            return done, failed + len(left)
        except Exception as ex:
            print(f"  {brand} {run[0]}..{run[-1]}: {type(ex).__name__}: {ex}")
            failed += len(run)
            continue

        for d in run:
            day = raw_all.get(d) or {}
            per_name = got.get(d)
            if not per_name:
                print(f"    {d}  Branch returned no installs — left alone")
                continue
            branch = {ev: dict(v) for ev, v in (day.get("branch") or {}).items()}
            branch[P.INSTALL_KEY] = per_name
            total = sum(per_name.values())
            print(f"    {d}  installs {total:,} across {len(per_name)} name(s)")
            if dry:
                done += 1
                continue
            if H.put(brand, d, day.get("meta") or {}, branch):
                # keep the per-day channel index in step: it now has an install row too
                P.chan_index_add(brand, d, branch)
                done += 1
            else:
                failed += 1
                print(f"    {d}  WRITE FAILED: {H.last_error()}")
        time.sleep(pause)
    return done, failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brands", default=",".join(C.BRANDS))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--since", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pause", type=float, default=2.0)
    a = ap.parse_args()
    if not H.available():
        sys.exit("HISTORY_URL / HISTORY_TOKEN not set — nothing to fill")
    print(("DRY RUN — " if a.dry_run else "") + "install backfill")
    td = tf = 0
    for b in [x.strip() for x in a.brands.split(",") if x.strip()]:
        d, f = repair_brand(b, a.limit, a.dry_run, a.since, a.pause)
        td += d; tf += f
    print(f"\ndone: {td} day(s) {'would be ' if a.dry_run else ''}filled, {tf} failed")


if __name__ == "__main__":
    main()
