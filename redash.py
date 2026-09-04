#!/usr/bin/env python3
"""Redash, shaped exactly like the Branch and AppsFlyer readers beside it.

PrepShots' trials come from the product database through a Redash query rather than from
an attribution vendor. The reason is quota: AppsFlyer answers the current day only from
its raw in-app events export, that export is capped per app per day, and three unsettled
days re-read every few hours exhausted the allowance before noon — leaving today's trials
at zero, which reads as "no trials happened" rather than "we could not ask". The product
DB has no such cap and knows today.

Same contract as the other two readers:

    {date: {event_key: {ad_name: count}}}

so the ad-name join, the pro-rata split, the day store and every view stay ignorant of
where a brand's trials came from.

Two honest differences from the vendor readers:

* **Counts are mandates, not device-attributed unique users.** Measured against AppsFlyer
  over the same days: 512 vs 426 on 1 Sept, 669 vs 618 on 2 Sept — the same shape running
  8-20% higher, which is what a backend count does against device attribution. A day
  answered by this reader is therefore not comparable with a day the same brand had from
  AppsFlyer, which is why the switch is forward-only rather than a backfill.
* **The window is the query's, not the caller's.** A Redash query carries its own date
  range in its SQL; this asks for the whole result and keeps the days it was asked for.
  A day outside the query's window comes back absent, never as zero.
"""
import csv
import io
import os
import time
import urllib.error
import urllib.request

TIMEOUT = int(os.environ.get("REDASH_TIMEOUT", "180"))
TRIES = int(os.environ.get("REDASH_TRIES", "3"))
# Identical to the AppsFlyer and Branch readers', so an unattributed trial buckets the
# same way downstream whichever vendor produced it.
NONE_PREFIX = "~none~"

DATE_COL = "Date"
AD_COL = "Ad Name"
SRC_COL = "Media Source"


def _slug(src):
    return NONE_PREFIX + (src or "").strip()


def _ad(raw, src):
    """The ad name, or the partner bucket when there is none.

    Redash writes the literal string "null" where an ad name is absent — Google rows
    always do, because the mandate is attributed to a campaign and never to a creative.
    Taken at face value that becomes an ad called "null" carrying two thousand mandates,
    sitting above every real creative in the tables.
    """
    ad = (raw or "").strip()
    return ad if ad and ad.lower() not in ("none", "null", "n/a") else _slug(src)


def _fetch(host, qid, key):
    url = f"https://{host}/api/queries/{qid}/results.csv?api_key={key}"
    last = None
    for n in range(TRIES):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and n < TRIES - 1:
                time.sleep(5 * (n + 1))
                last = f"HTTP {e.code}: {body}"
                continue
            raise RuntimeError(f"Redash HTTP {e.code} on query {qid}: {body}")
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            if n < TRIES - 1:
                time.sleep(5 * (n + 1))
                continue
            raise
    raise RuntimeError(f"Redash failed on query {qid}: {last}")


def trials_daily(host, query, since, until, events):
    """{date: {event_key: {ad_name: count}}} for one brand.

    `events` maps this dashboard's event keys to CSV column names — {"t101": "Mandates"} —
    the same way it maps them to Branch and AppsFlyer event names elsewhere.
    """
    qid, key = query
    rows = csv.DictReader(io.StringIO(
        _fetch(host, qid, key).decode("utf-8", "replace")))
    out = {}
    for r in rows:
        day = (r.get(DATE_COL) or "")[:10]
        if not (since <= day <= until):
            continue
        ad = _ad(r.get(AD_COL), r.get(SRC_COL))
        for ek, col in events.items():
            raw = (r.get(col) or "").strip()
            if not raw:
                continue
            try:
                n = int(float(raw))
            except ValueError:
                continue
            if n:
                dst = out.setdefault(day, {}).setdefault(ek, {})
                dst[ad] = dst.get(ad, 0) + n
    return out
