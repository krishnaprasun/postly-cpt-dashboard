#!/usr/bin/env python3
"""Client for the ads-history store — settled days served from storage, not from Meta.

The dashboard re-asked Meta and Branch for the same closed days on every single view: a
30-day window meant a full insights pull per ad account plus five Branch chunks per event,
every time, for days whose numbers stopped moving long ago. That is most of the cold-start
latency and most of the rate-limit exposure that trips Meta's code 17.

Two rules shape everything here.

**A closed day is not immediately a settled day.** Meta applies billing corrections and
Branch backfills late-arriving events, mostly inside 48 hours. So a day only becomes
storable once it is SETTLE_DAYS old; today and the days behind it are always fetched live.
Storing a day too early would freeze a number that was still moving, and no amount of
later correctness would undo it — the whole point of a store is that you stop checking.

**The store is never load-bearing.** With no HISTORY_URL configured, or the service down,
slow, or answering nonsense, every function here degrades to "nothing stored" and the
caller pulls live exactly as it always did. A history cache that can take the dashboard
down with it is worse than no history cache.

Auth is a bearer token rather than Google IAM because the org policy
`iam.disableServiceAccountKeyCreation` forbids service-account keys and Render issues no
OIDC identity Google would accept. The token is not a Google credential and reaches
nothing but this one bucket.
"""
import gzip
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

# How old a day must be before its numbers are treated as final. Meta bills late and
# Branch backfills; 3 days is comfortably past both. Raising this costs live pulls,
# lowering it risks freezing a figure that had not stopped moving.
SETTLE_DAYS = int(os.environ.get("HISTORY_SETTLE_DAYS", "3"))
# Short on purpose. This is an optimisation; if it cannot answer quickly there is a
# perfectly good live path waiting, and blocking a page render on it defeats the point.
TIMEOUT = int(os.environ.get("HISTORY_TIMEOUT", "20"))


def _token():
    t = os.environ.get("HISTORY_TOKEN", "").strip()
    if t:
        return t
    try:
        with open(os.path.expanduser("~/.anthropic/ads_history_token")) as f:
            return f.read().strip()
    except OSError:
        return ""


URL = os.environ.get("HISTORY_URL", "").strip().rstrip("/")
TOKEN = _token()
# Set by the first failure so a dead store is not retried on every build within a render.
_last_error = None


def available():
    return bool(URL and TOKEN)


def last_error():
    return _last_error


def _call(method, path, params, body=None):
    global _last_error
    url = f"{URL}{path}?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + TOKEN,
        "Accept-Encoding": "gzip",
        **({"Content-Type": "application/json"} if data else {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        _last_error = None
        return json.loads(raw)


def settled_through(today):
    """The newest date whose numbers are treated as final."""
    d = datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=SETTLE_DAYS)
    return d.strftime("%Y-%m-%d")


def split(since, until, today):
    """(dates eligible for the store, live_since, live_until).

    live_since/live_until are None when the whole window is settled. The split is a
    single cut, never a set of holes: Meta and Branch are cheaper to ask for one range
    than for scattered days, so the live half stays contiguous even if that means
    re-fetching a stored day or two at the boundary.
    """
    cut = settled_through(today)
    d0 = datetime.strptime(since, "%Y-%m-%d").date()
    d1 = datetime.strptime(until, "%Y-%m-%d").date()
    dc = datetime.strptime(cut, "%Y-%m-%d").date()
    if d0 > dc:                                   # nothing old enough
        return [], since, until
    last = min(d1, dc)
    dates, d = [], d0
    while d <= last:
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    if last >= d1:                                # whole window settled
        return dates, None, None
    return dates, (last + timedelta(days=1)).strftime("%Y-%m-%d"), until


def fetch(brand, dates):
    """Aggregated stored data for these dates: (meta_by_account, branch_by_event, got, missing).

    Returns empty structures rather than raising on any failure — see the module note.
    """
    global _last_error
    if not (available() and dates):
        return {}, {}, [], list(dates)
    try:
        j = _call("GET", "/v1/history",
                  {"brand": brand, "dates": ",".join(dates), "aggregate": "1"})
    except Exception as ex:
        _last_error = f"{type(ex).__name__}: {str(ex)[:160]}"
        return {}, {}, [], list(dates)
    return (j.get("meta") or {}, j.get("branch") or {},
            j.get("days") or [], j.get("missing") or list(dates))


def put(brand, date, meta_by_account, branch_by_event):
    """Store one settled day. Returns True on success; never raises."""
    global _last_error
    if not available():
        return False
    try:
        _call("PUT", "/v1/history", {"brand": brand, "date": date},
              body={"v": 1, "brand": brand, "date": date,
                    "written_at": datetime.utcnow().isoformat() + "Z",
                    "meta": meta_by_account, "branch": branch_by_event})
        return True
    except Exception as ex:
        _last_error = f"{type(ex).__name__}: {str(ex)[:160]}"
        return False


def have(brand):
    """Dates already stored for a brand, for the backfill to skip. [] on failure."""
    try:
        return _call("GET", "/v1/have", {"brand": brand}).get("dates", [])
    except Exception:
        return []
