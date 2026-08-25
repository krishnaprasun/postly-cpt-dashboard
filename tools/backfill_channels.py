#!/usr/bin/env python3
"""Fill in the channel a stored trial came from, for days written before we kept it.

Every Branch trial now carries the partner that earned it, tagged into its ad-name key.
Days stored before that carry one bare `null` key holding the whole nameless pool, so the
dashboard reports them as "channel not recorded" rather than guessing. This resolves them.

Why it is not simply the normal backfill re-run
-----------------------------------------------
The ad-name query returns one row per ad name per day — hundreds. The partner query
returns four or five. Re-running the full pull over a hundred stored days is exactly the
traffic that exhausted Branch's per-app burst limit and made SpeakEasy's dashboard
unreachable; this asks Branch for about one hundredth as much.

What it does to a day
---------------------
Only the null key is touched. Named ad rows and the whole Meta side are written back
byte-identical, and the day's TOTAL for each event is unchanged — the stored nameless
count is redistributed across partners, never replaced by the second query's own total.
The split comes from the partner query; only the rounding is imposed (see `apportion`).

Meta's share of the nameless pool is derived, not assumed: it is the partner query's
Facebook count minus the trials the day already has under real ad names, floored at zero.
On every day measured that came out at zero, which is the finding this whole change rests
on — Branch fills in an ad name for Meta and leaves it empty for Google.

Usage
  python3 tools/backfill_channels.py --dry-run
  python3 tools/backfill_channels.py --brands funda,speakeasy --limit 20
  python3 tools/backfill_channels.py                       # every brand, every stored day
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as C           # noqa: E402
import history as H          # noqa: E402
import postly_cpt as P       # noqa: E402


def chunks(dates, n=P.BRANCH_MAX_SPAN):
    """Contiguous runs of at most n days, so one Branch call covers each run."""
    from datetime import datetime, timedelta
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


def repair_brand(brand, limit=None, dry=False, since=None, pause=2.0):
    B = C.brand(brand)
    if not (B.get("branch") and B.get("events")):
        print(f"  {brand}: no Branch app configured — nothing to do")
        return 0, 0
    stored = sorted(d for d in H.have(brand) if not since or d >= since)
    if not stored:
        print(f"  {brand}: nothing stored")
        return 0, 0

    # Which stored days still hold a bare null key. Read raw in the same 15-day chunks
    # history.fetch_raw uses, so this stays one modest response at a time.
    need, raw_all = [], {}
    for i in range(0, len(stored), 15):
        part = stored[i:i + 15]
        raw = H.fetch_raw(brand, part)
        raw_all.update(raw)
        for d, day in raw.items():
            if any(P.legacy_nameless(v) for v in (day.get("branch") or {}).values()):
                need.append(d)
    need.sort()
    print(f"  {brand}: {len(stored)} stored, {len(need)} need a channel split"
          + (f" (taking the oldest {limit})" if limit and limit < len(need) else ""))
    if limit:
        need = need[:limit]
    if not need:
        return 0, 0

    done = failed = 0
    for run in chunks(need):
        try:
            partners = P.branch_partners_daily(run[0], run[-1], B)
        except P.BranchThrottled:
            print(f"  {brand}: Branch is throttling — stopping here. "
                  f"Not repaired: {', '.join(need[need.index(run[0]):])}")
            return done, failed + len(need) - need.index(run[0])
        except Exception as ex:
            print(f"  {brand} {run[0]}..{run[-1]}: {type(ex).__name__}: {ex}")
            failed += len(run)
            continue

        for d in run:
            day = raw_all.get(d) or {}
            branch = {ev: dict(v) for ev, v in (day.get("branch") or {}).items()}
            per_ev = partners.get(d) or {}
            changed, lines = False, []
            for ev, by_name in branch.items():
                pool = P.legacy_nameless(by_name)
                if not pool:
                    continue
                pt = per_ev.get(ev)
                if not pt:
                    lines.append(f"{ev}: no partner data for this day — left as unknown")
                    continue
                named = P.named_total(by_name)
                # Weights: everything that is not Facebook is nameless in full; Facebook's
                # nameless part is whatever it has beyond the named rows already stored.
                w = {}
                for par, n in pt.items():
                    if P.partner_slug(par) == "meta":
                        n = max(0, n - named)
                    if n > 0:
                        w[par or ""] = w.get(par or "", 0) + n
                split = P.apportion(pool, w)
                if not split:
                    lines.append(f"{ev}: partner query returned nothing usable — left alone")
                    continue
                for k in [k for k in by_name
                          if not isinstance(k, str) or not k or k == "null"]:
                    by_name.pop(k)
                for par, n in split.items():
                    key = P._nameless_key(par)
                    by_name[key] = by_name.get(key, 0) + n
                changed = True
                lines.append(f"{ev}: {pool:,} → " + ", ".join(
                    f"{P.partner_slug(par)} {n:,}" for par, n in
                    sorted(split.items(), key=lambda kv: -kv[1])))
            if not changed:
                continue
            print(f"    {d}  " + " | ".join(lines))
            if dry:
                done += 1
                continue
            if H.put(brand, d, day.get("meta") or {}, branch):
                done += 1
            else:
                failed += 1
                print(f"    {d}  WRITE FAILED: {H.last_error()}")
        time.sleep(pause)     # the burst limiter is per app key, and shared with the site
    return done, failed


def audit(brand):
    """Report the channel index against the stored days. Reads only, writes nothing.

    Exists because the failure it looks for is silent by construction: an index that has
    quietly lost a day, or an entry written before its day gained a series, looks exactly
    like a correct one from the outside. The pro-rata view would just cover fewer days
    and print a smaller uplift, with nothing anywhere saying why.
    """
    idx, ok = P.chan_index_read(brand)
    if not ok:
        print(f"  {brand}: could not read the channel index — {H.last_error()}")
        return False
    stored = sorted(H.have(brand))
    want = {"inst"} | set(C.brand(brand).get("events") or {})
    missing = [d for d in stored if d not in idx]
    extra = [d for d in idx if d not in stored]
    partial = {d: sorted(want - set(idx[d] or {})) for d in sorted(idx)
               if want - set(idx[d] or {})}
    clean = not (missing or extra or partial)
    print(f"  {brand}: index {len(idx)} day(s), stored {len(stored)} — "
          + ("consistent" if clean else "PROBLEMS"))
    if missing:
        print(f"     {len(missing)} stored day(s) absent from the index: "
              f"{missing[:4]}{' …' if len(missing) > 4 else ''}")
    if extra:
        print(f"     {len(extra)} index day(s) not in the store: {extra[:4]}")
    for d, ks in list(partial.items())[:8]:
        print(f"     {d} has no {', '.join(ks)} row")
    if len(partial) > 8:
        print(f"     … and {len(partial) - 8} more")
    if not clean:
        print(f"     fix: python3 tools/backfill_channels.py --brands {brand} "
              f"--index-only --reindex")
    return clean


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brands", default=",".join(C.BRANDS))
    ap.add_argument("--limit", type=int, default=None, help="days per brand")
    ap.add_argument("--since", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pause", type=float, default=2.0)
    ap.add_argument("--index-only", action="store_true",
                    help="skip the repair, just (re)build the per-day channel index")
    ap.add_argument("--reindex", action="store_true",
                    help="recompute every stored day, not only the ones missing")
    ap.add_argument("--audit", action="store_true",
                    help="report the index against the stored days; write nothing")
    a = ap.parse_args()
    if not H.available():
        sys.exit("HISTORY_URL / HISTORY_TOKEN not set — nothing to repair")
    if a.audit:
        print("channel index audit — reads only")
        allclean = True
        for b in [x.strip() for x in a.brands.split(",") if x.strip()]:
            allclean = audit(b) and allclean
        sys.exit(0 if allclean else 1)
    print(("DRY RUN — " if a.dry_run else "") + "channel backfill")
    tot_d = tot_f = 0
    for b in [x.strip() for x in a.brands.split(",") if x.strip()]:
        if not a.index_only:
            d, f = repair_brand(b, a.limit, a.dry_run, a.since, a.pause)
            tot_d += d; tot_f += f
        if a.dry_run:
            continue
        # The index is what the pro-rata view reads day by day. Rebuilt AFTER the repair
        # so it picks up the channels the repair just wrote, never before.
        #
        # --reindex recomputes every stored day rather than wiping first: writing an
        # empty index and then filling it means a failure part-way leaves nothing behind,
        # and the empty one would be the newest artifact and win every read.
        try:
            built, total = P.chan_index_build(b, log=print, rebuild=a.reindex)
            print(f"  {b}: channel index {built} day(s) written, "
                  f"{total} stored day(s) total")
        except Exception as ex:
            tot_f += 1
            print(f"  {b}: channel index NOT updated — {type(ex).__name__}: {ex}")
    print(f"\ndone: {tot_d} day(s) {'would be ' if a.dry_run else ''}repaired, "
          f"{tot_f} failed")


if __name__ == "__main__":
    main()
