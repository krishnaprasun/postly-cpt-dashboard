#!/usr/bin/env python3
"""AppsFlyer, shaped exactly like the Branch reader beside it.

PrepShots measures on AppsFlyer where the other brands measure on Branch. Everything
downstream — the ad-name join, the pro-rata split between Meta and Google, the day store,
every view — is written against one shape:

    {date: {event_key: {ad_name: unique_users}}}

so this module's whole job is to produce that shape from AppsFlyer and let the rest of the
dashboard stay ignorant of which vendor a brand uses.

Two sources, chosen deliberately:

* **Trials come from the raw in-app events export**, because it carries each event's own
  timestamp. AppsFlyer's aggregate API is far cheaper but groups by INSTALL date — "people
  who installed on the 1st and later started a trial" — which is a cohort figure. Every CPT
  on this dashboard is a day's spend over that day's trials, and mixing the two definitions
  would make one brand's CPT quietly incomparable with the rest. The cost is about 6 MB of
  CSV per day instead of a few KB.
* **Installs come from the aggregate API**, where grouping by install date IS the event day,
  so the cheap source is also the correct one.

Unique users, not rows: Branch reports `unique_count`, so a person who fires the trial event
twice in a day counts once here too.
"""
import csv
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

BASE = "https://hq1.appsflyer.com/api"
TIMEOUT = int(os.environ.get("AF_TIMEOUT", "300"))
TRIES = int(os.environ.get("AF_TRIES", "3"))
# The prefix the rest of the app uses for a trial with no ad name behind it. Kept identical
# to the Branch reader's so the two are indistinguishable downstream.
NONE_PREFIX = "~none~"


def token():
    t = os.environ.get("APPSFLYER_TOKEN", "").strip()
    if t:
        return t
    try:
        with open(os.path.expanduser("~/.anthropic/appsflyer_token")) as f:
            return f.read().strip()
    except OSError:
        return ""


def available():
    return bool(token())


def _get(path, params, accept="application/json", tries=TRIES):
    url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
    last = None
    for n in range(tries):
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + token(), "accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode("utf-8", "replace")
            # 429 and 5xx are worth another go; a 4xx about fields or dates is not.
            if e.code in (429, 500, 502, 503, 504) and n < tries - 1:
                time.sleep(5 * (n + 1))
                last = f"HTTP {e.code}: {body}"
                continue
            raise RuntimeError(f"AppsFlyer HTTP {e.code} on {path}: {body}")
        except Exception as e:                       # network blips
            last = f"{type(e).__name__}: {e}"
            if n < tries - 1:
                time.sleep(5 * (n + 1))
                continue
            raise
    raise RuntimeError(f"AppsFlyer failed on {path}: {last}")


def _days(since, until):
    d = datetime.strptime(since, "%Y-%m-%d").date()
    end = datetime.strptime(until, "%Y-%m-%d").date()
    while d <= end:
        yield d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def _slug(src):
    """Nameless trials keep the partner that earned them, exactly as Branch's do."""
    return NONE_PREFIX + (src or "").strip()


def _ad(raw, src):
    """The ad name, or the partner bucket when there is none.

    The aggregate API writes the STRING "None" where the raw export leaves the field
    empty; taken literally that becomes an ad called None carrying two thousand installs,
    sitting in the tables above every real creative.
    """
    ad = (raw or "").strip()
    return ad if ad and ad.lower() not in ("none", "null", "n/a") else _slug(src)


QUOTA_MARK = "maximum number of"


def _raw_day(app_id, day, want):
    """One day of trials counted on the day the EVENT happened. Costs one raw export."""
    raw = _get(f"raw-data/export/app/{app_id}/in_app_events_report/v5",
               {"from": day, "to": day}, accept="text/csv")
    seen = {}
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))):
        key = want.get(row.get("Event Name"))
        if not key:
            continue
        # The event's OWN day, not the install's. A row decides which day it belongs to
        # rather than the request, so an event just after midnight lands correctly.
        d = (row.get("Event Time") or "")[:10] or day
        ad = _ad(row.get("Ad"), row.get("Media Source"))
        uid = row.get("AppsFlyer ID") or row.get("Customer User ID") or ""
        seen.setdefault(d, {}).setdefault(key, {}).setdefault(ad, set()).add(uid)
    out = {}
    for d, per_key in seen.items():
        for key, per_ad in per_key.items():
            dst = out.setdefault(d, {}).setdefault(key, {})
            for ad, users in per_ad.items():
                dst[ad] = dst.get(ad, 0) + len(users)
    return out


def _agg_day(app_id, day, events):
    """One day from the aggregate API: one small JSON call, no raw-export quota spent.

    AppsFlyer aggregates this by INSTALL date, so it answers "people who installed that
    day and went on to start a trial" where the raw export answers "trials that happened
    that day". Measured on PrepShots for 1 Sept: 426 against 456, about 7% apart. Close,
    not the same, which is why this is only used where a raw export cannot be.
    """
    kpis = ",".join(["installs"] + [f"unique_users_{n}" for n in events.values() if n])
    rows = json.loads(_get(f"master-agg-data/v4/app/{app_id}",
                           {"from": day, "to": day, "format": "json",
                            "groupings": "pid,af_ad", "kpis": kpis}))
    out = {}
    for r in rows:
        ad = _ad(r.get("Ad"), r.get("Media Source"))
        for key, name in events.items():
            n = int(r.get(f"Unique Users - {name}") or 0)
            if n:
                d = out.setdefault(key, {})
                d[ad] = d.get(ad, 0) + n
    return out, rows


def trials_daily(app_id, since, until, events, install_key="inst", raw_until=None,
                 source=None):
    """{date: {event_key: {ad_name: unique_users}}} for one app.

    Two sources, split at the settle line. A day that is settled is written to the store
    once and never asked for again, so it can afford the raw export and gets the same
    event-day meaning Branch gives every other brand. Today and the days behind it are
    re-read every hour, which raw data cannot survive — AppsFlyer caps raw exports per app
    per day and an hourly job would exhaust it before lunch — so those come from the
    aggregate API and shift slightly when the day finally settles. Branch brands already
    move for the same reason: late events land after the fact.

    `raw_until` is that settle line. Without one, everything uses the aggregate API.
    """
    want = {name: key for key, name in events.items() if name}
    out, used = {}, {}
    for day in _days(since, until):
        pick = source or ("raw" if (raw_until and day <= raw_until) else "agg")
        if pick == "raw":
            try:
                for d, per in _raw_day(app_id, day, want).items():
                    for key, ads in per.items():
                        dst = out.setdefault(d, {}).setdefault(key, {})
                        for ad, n in ads.items():
                            dst[ad] = dst.get(ad, 0) + n
                used[day] = "raw"
                continue
            except RuntimeError as e:
                # The daily export allowance is a hard stop, not a blip. Fall back to the
                # aggregate rather than leaving the day with no trials at all, and say so.
                if QUOTA_MARK not in str(e):
                    raise
                used[day] = "agg (raw quota spent)"
        per, _rows = _agg_day(app_id, day, events)
        for key, ads in per.items():
            dst = out.setdefault(day, {}).setdefault(key, {})
            for ad, n in ads.items():
                dst[ad] = dst.get(ad, 0) + n
        used.setdefault(day, "agg")

    # Installs are aggregated by install date, which for an install IS the event day —
    # so here the cheap source is also the correct one, whichever way the trials came.
    if install_key:
        for day in _days(since, until):
            rows = json.loads(_get(f"master-agg-data/v4/app/{app_id}",
                                   {"from": day, "to": day, "format": "json",
                                    "groupings": "pid,af_ad", "kpis": "installs"}))
            dst = out.setdefault(day, {}).setdefault(install_key, {})
            for r in rows:
                n = int(r.get("Installs") or 0)
                if not n:
                    continue
                ad = _ad(r.get("Ad"), r.get("Media Source"))
                dst[ad] = dst.get(ad, 0) + n
    out["_sources"] = used
    return out


def events_seen(app_id, day):
    """Every event name the app recorded on one day, with counts. For wiring a new brand."""
    raw = _get(f"raw-data/export/app/{app_id}/in_app_events_report/v5",
               {"from": day, "to": day}, accept="text/csv")
    out = {}
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))):
        n = row.get("Event Name")
        out[n] = out.get(n, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
