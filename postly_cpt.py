#!/usr/bin/env python3
"""Live CPT data layer: Meta spend (both ad accounts) x Branch trials (by ad name).

Join strategy
-------------
Branch only exposes `~ad_name` / `~ad_set_name` as attribution dimensions -- campaign and
account are NOT populated (see postly-branch-api memory). Ad-set names also merge across
the Testing and Trial campaigns, which corrupts an ad-set-name join.

So everything is joined ONCE at ad-name level and then rolled UP:
    ad -> ad set -> campaign -> account -> combined
Ad names do not collide between the two accounts (verified 2026-08-20). Where a name is
reused by several ads inside an account, the trials are split across them in proportion to
spend, so no rollup double counts.

Classplus (Redash) joins on the SAME key: signups carry `deviceInfo.branchData.adName`,
which is the Meta ad name. 99.4% of its signups and 99.5% of its mandates matched a live
Meta ad name on 2026-08-20, so it rolls up through the identical path.

Read-only. This module never writes to Meta.
"""
import json, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import config as C

IST = timezone(timedelta(minutes=330))
BRANCH_URL = "https://api2.branch.io/v1/query/analytics"
BRANCH_MAX_SPAN = 7          # Branch Query API caps a request at 7 days

ACCOUNTS = [
    {"id": C.AD_ACCOUNT,      "name": "Postly"},
    {"id": C.INSTALL_ACCOUNT, "name": "Postly Install"},
]
EVENTS = {
    "t101": "postly_trial_started_backend",            # the daily report's CPT numerator
    "t10m": "postly_trial_nc_after10min_backend",      # the 10-min campaigns' own event
}


def today_ist():
    return datetime.now(IST).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- Meta -----
class RateLimited(RuntimeError):
    """Meta code 4/17/613. Distinct from a real failure: the data is fine, we are just
    not allowed to ask right now, so callers fall back to cached numbers.

    Carries Meta's own `estimated_time_to_regain_access` so nothing downstream has to
    guess when it clears. Guessing is how this got misreported once already — the first
    version of this dashboard told people a throttle "usually clears within a few
    minutes" and it then ran for over half an hour.
    """

    def __init__(self, msg, account=None, regain_min=0, usage=None):
        super().__init__(msg)
        self.account, self.regain_min, self.usage = account, regain_min, usage or {}


# Meta reports its own budget on every response, throttled or not, in
# x-business-use-case-usage: call_count / total_cputime / total_time as percentages of
# the hourly allowance, estimated_time_to_regain_access in minutes, and the access tier.
# total_time is the one that bites this app (108% when it first tripped, call_count 1%),
# so it is worth watching on the way UP rather than only reporting the crash.
_usage_lock = threading.Lock()
_usage = {}                       # account id -> latest usage snapshot
_throttle = {}                    # (account id, listing) -> {"since","until","regain_min"}


def _note_usage(headers, acct):
    raw = headers.get("x-business-use-case-usage")
    if not raw:
        return {}
    try:
        j = json.loads(raw)
    except ValueError:
        return {}
    out = {}
    for entries in j.values():
        for e in entries or []:
            for k in ("call_count", "total_cputime", "total_time"):
                out[k] = max(out.get(k, 0), int(e.get(k) or 0))
            out["regain_min"] = max(out.get("regain_min", 0),
                                    int(e.get("estimated_time_to_regain_access") or 0))
            if e.get("ads_api_access_tier"):
                out["tier"] = e["ads_api_access_tier"]
    if out:
        with _usage_lock:
            _usage[acct] = dict(out, at=time.time())
    return out


def _acct_of(path):
    head = path.split("/")[0]
    return head if head.startswith("act_") else ""


def _mark_throttle(acct, edge, regain_min):
    now = time.time()
    with _usage_lock:
        prev = _throttle.get((acct, edge))
        _throttle[(acct, edge)] = {
            # keep the original start across repeated failures, so the page can say how
            # long this has been going on rather than restarting the clock each attempt
            "since": prev["since"] if prev else now,
            "until": now + max(regain_min, 1) * 60,
            "regain_min": regain_min,
        }


def _clear_throttle(acct, edge):
    with _usage_lock:
        _throttle.pop((acct, edge), None)


def _graph(path, params, tries=6, rl_retries=2):
    pr = dict(params); pr["access_token"] = C.META_TOKEN; pr.setdefault("limit", "500")
    url = f"{C.GRAPH}/{path}?" + urllib.parse.urlencode(pr)
    acct, edge = _acct_of(path), path.split("/")[-1]
    out = []
    while url:
        for i in range(tries):
            try:
                with urllib.request.urlopen(url, timeout=120) as r:
                    j = json.load(r)
                    _note_usage(r.headers, acct)
                    _clear_throttle(acct, edge)
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode()
                usage = _note_usage(e.headers, acct)
                # Parse the code rather than substring-matching the body: '"code":1'
                # also matches 17 and 100, which is exactly the kind of bug that makes a
                # rate limit look like something else.
                try:
                    code = json.loads(body).get("error", {}).get("code")
                except Exception:
                    code = None
                # 4 / 17 / 613 = rate limit. Meta holds these for minutes, far longer than
                # a web request can wait, and hammering makes it worse. Two short retries,
                # then give up so the caller can serve the last good numbers instead.
                if code in (4, 17, 613):
                    # rl_retries=0 for the roster listings: the caller degrades to
                    # cached-or-insights-only anyway, so waiting 15s to be told "no"
                    # again just makes a throttled cold start feel broken.
                    if i < rl_retries:
                        time.sleep(5 * (i + 1)); continue
                    regain = usage.get("regain_min", 0)
                    _mark_throttle(acct, edge, regain or ROSTER_RETRY // 60)
                    raise RateLimited(f"Meta rate limit on {edge}", account=acct,
                                      regain_min=regain, usage=usage)
                # 1 / 2 = Meta-side transient ("Service temporarily unavailable"). Short
                # lived and common; retrying is right. Not retrying these turned a blip
                # into a 500 on a cold cache, which is how this branch got written.
                if code in (1, 2) and i < tries - 1:
                    time.sleep(3 * (i + 1)); continue
                raise RuntimeError(f"Meta {path}: {body[:300]}")
            except Exception as ex:
                if i < tries - 1:
                    time.sleep(4 * (i + 1)); continue
                raise RuntimeError(f"Meta {path}: {ex}")
        out += j.get("data", [])
        url = j.get("paging", {}).get("next")
    return out


# The Meta app is on the **development_access** ads-API tier, whose per-account call
# ceiling is low enough that a naive refresh loop trips code 17 within an afternoon.
# The roster (names, statuses, budgets) is ~70% of the calls a refresh would make and
# changes on the timescale of ad-ops decisions, not seconds — so it gets its own long
# cache and the per-refresh cost drops to the insights call alone.
# Meta's limit that actually bites here is total_time, not call_count: the usage header
# read 108% time / 1% calls when this first tripped. So the cost to minimise is expensive
# LISTINGS, not the number of requests. Listing every active ad is the priciest thing the
# dashboard does and the least urgent, hence its own much longer TTL.
ROSTER_TTL = int(os.environ.get("ROSTER_TTL", "1800"))
ADS_ROSTER_TTL = int(os.environ.get("ADS_ROSTER_TTL", "3600"))
# After a roster fetch fails, stop asking for a while. Retrying a throttled endpoint on
# every refresh both feeds the rate limit that caused it and costs the caller the full
# back-off sleep on each build (~15s), so a failure is cached almost as deliberately as
# a success.
ROSTER_RETRY = int(os.environ.get("ROSTER_RETRY", "300"))
_roster_cache, _roster_lock = {}, threading.Lock()
_roster_fail_until = {}


def meta_insights(acct, since, until):
    """Ad-level spend for the window. Re-pulled on every refresh; this is the number."""
    return _graph(f"{acct}/insights", {
        "level": "ad", "time_range": json.dumps({"since": since, "until": until}),
        "fields": "ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,spend"})


def _part(acct, kind, fetch, ttl):
    """One cached piece of the roster. Returns (data, ok).

    Each piece stands alone deliberately. Fetching all three as a unit meant one
    throttled listing threw away the other two — losing every budget because the *ads*
    listing failed, when the ad set listing had answered perfectly well.
    """
    key, now = (acct, kind), time.time()
    with _roster_lock:
        hit = _roster_cache.get(key)
        blocked_until = _roster_fail_until.get(key, 0)
    if hit and now - hit["at"] < ttl:
        return hit["data"], True
    if now < blocked_until:
        return (hit["data"], True) if hit else (None, False)
    try:
        data = fetch()
        with _roster_lock:
            _roster_cache[key] = {"at": time.time(), "data": data}
            _roster_fail_until.pop(key, None)
        return data, True
    except Exception as ex:
        now = time.time()
        # Meta says how long it will be. Wait exactly that long rather than the generic
        # back-off: asking early does not work and blocked attempts still count against
        # the window, so an eager retry loop keeps the throttle alive. That is not
        # theoretical — a 60s poller here held one open for half an hour.
        wait = ROSTER_RETRY
        if isinstance(ex, RateLimited):
            wait = max(ROSTER_RETRY, (ex.regain_min or 0) * 60)
        with _roster_lock:
            _roster_fail_until[key] = now + wait
        # An expired copy beats nothing; nothing beats failing the whole build.
        return (hit["data"], True) if hit else (None, False)


def meta_roster(acct):
    """(campaigns, active ad sets, active ads, ok_flags) — each piece independently
    cached and independently allowed to fail."""
    camps, ok_c = _part(acct, "campaigns", lambda: _graph(
        f"{acct}/campaigns", {"fields": "id,name,effective_status,daily_budget"},
        rl_retries=0), ROSTER_TTL)
    sets, ok_s = _part(acct, "adsets", lambda: _graph(
        f"{acct}/adsets", {"fields": "id,name,effective_status,daily_budget,campaign_id",
                           "effective_status": json.dumps(["ACTIVE"])},
        rl_retries=0), ROSTER_TTL)
    ads_, ok_a = _part(acct, "ads", lambda: _graph(
        f"{acct}/ads", {"fields": "id,name,effective_status,adset_id,campaign_id",
                        "effective_status": json.dumps(["ACTIVE"])},
        rl_retries=0), ADS_ROSTER_TTL)
    return (camps or [], sets or [], ads_ or [],
            {"campaigns": ok_c, "adsets": ok_s, "ads": ok_a})


# -------------------------------------------------------------- Branch -----
def _branch(body, tries=5):
    for i in range(tries):
        req = urllib.request.Request(BRANCH_URL, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            b = e.read().decode()
            if e.code in (429, 500, 502, 503) and i < tries - 1:
                time.sleep(10 * (i + 1)); continue
            raise RuntimeError(f"Branch {e.code}: {b[:300]}")
        except Exception as ex:
            if i < tries - 1:
                time.sleep(6 * (i + 1)); continue
            raise RuntimeError(f"Branch: {ex}")


def branch_trials_by_ad(since, until):
    """{event_key: {ad_name_or_None: unique_count}} over the window, 7-day chunked."""
    out = {k: defaultdict(int) for k in EVENTS}
    d = datetime.strptime(since, "%Y-%m-%d").date()
    endd = datetime.strptime(until, "%Y-%m-%d").date()
    while d <= endd:
        ce = min(d + timedelta(days=BRANCH_MAX_SPAN - 1), endd)
        for key, ev in EVENTS.items():
            j = _branch({
                "branch_key": C.BRANCH_KEY, "branch_secret": C.BRANCH_SECRET,
                "start_date": d.strftime("%Y-%m-%d"), "end_date": ce.strftime("%Y-%m-%d"),
                "dimensions": ["last_attributed_touch_data_tilde_ad_name"],
                "granularity": "all", "aggregation": "unique_count",
                "data_source": "eo_custom_event", "filters": {"name": [ev]}})
            for row in j.get("results", []):
                res = row.get("result", {})
                out[key][res.get("last_attributed_touch_data_tilde_ad_name")] += \
                    res.get("unique_count", 0)
        d = ce + timedelta(days=1)
    return out



# ------------------------------------------------------------ Classplus ----
# A Redash query over the Classplus product DB, keyed by ad name. It gives what Meta and
# Branch cannot: signups, and how many of those signups actually put a trial mandate in
# place, per ad.
#
# Two things about it shape the code below.
#
# 1. The window is BAKED INTO THE SQL as literal dates — the query takes no parameters.
#    So the window it covers is read back out of the SQL text and the numbers are only
#    attached when the dashboard is looking at exactly that window. Labelling another
#    day's figures as today's would be worse than showing nothing.
# 2. It is a SIGNUP-COHORT measure, not an event measure: `trial_mandates` counts trials
#    taken by people who *signed up inside the window*. Branch trials count trial events
#    inside the window whenever the user signed up. The two agree closely but they are
#    not the same question, which is why they stay in separate columns.
CP_TTL = int(os.environ.get("CP_TTL", "600"))            # insist the result is this fresh
CP_POLL_BUDGET = int(os.environ.get("CP_POLL_BUDGET", "20"))
CP_KEYS = ("cp_signups", "cp_mandates", "cp_d0a", "cp_d0c")


def _cp_url(path):
    return (f"https://{C.CLASSPLUS_HOST}/api/{path}"
            f"{'&' if '?' in path else '?'}api_key={urllib.parse.quote(C.CLASSPLUS_KEY)}")


def _cp_call(path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        _cp_url(path), data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _cp_window(sql):
    """The two literal IST bounds out of the SQL -> inclusive (since, until) dates."""
    m = re.findall(r"CONVERT_TZ\('(\d{4}-\d{2}-\d{2}) [\d:]+', '\+05:30'", sql or "")
    if len(m) < 2:
        return None
    start = datetime.strptime(m[0], "%Y-%m-%d").date()
    end = datetime.strptime(m[1], "%Y-%m-%d").date() - timedelta(days=1)  # end is exclusive
    if end < start:
        return None
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _cp_parse(qr):
    cols = {c["name"] for c in qr["data"]["columns"]}
    need = {"ad_name", "signups", "trial_mandates"}
    if not need <= cols:
        raise RuntimeError(f"Classplus query is missing {sorted(need - cols)}")
    win = _cp_window(qr.get("query"))
    if not win:
        raise RuntimeError("Classplus query has no readable date bounds")
    by_ad, organic = {}, {"cp_signups": 0, "cp_mandates": 0, "cp_d0a": 0, "cp_d0c": 0}
    for r in qr["data"]["rows"]:
        rec = {"cp_signups": int(r.get("signups") or 0),
               "cp_mandates": int(r.get("trial_mandates") or 0),
               "cp_d0a": int(r.get("d0_active") or 0),
               "cp_d0c": int(r.get("d0_cancelled") or 0)}
        name = r.get("ad_name")
        if not name or name == "Organic / Unknown":
            for k in CP_KEYS:
                organic[k] += rec[k]
            continue
        prev = by_ad.setdefault(name, {k: 0 for k in CP_KEYS})
        for k in CP_KEYS:
            prev[k] += rec[k]
    ts = qr.get("retrieved_at", "")
    try:
        at = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        age = int((datetime.now(timezone.utc) - at).total_seconds() // 60)
    except ValueError:
        at, age = None, None
    return {"window": win, "by_ad": by_ad, "organic": organic,
            "retrieved_at": at.astimezone(IST).strftime("%H:%M IST") if at else "",
            "age_min": age,
            "totals": {k: sum(v[k] for v in by_ad.values()) + organic[k] for k in CP_KEYS}}


def classplus_fetch():
    """Latest result, refreshing the query if the cached one is older than CP_TTL.

    Redash answers the POST either with a result (cache was fresh enough) or with a job.
    A job is polled, but only within a budget: the query takes ~13s and the dashboard is
    not going to sit on a cold page waiting for it. If the budget runs out the last
    result is served instead — still real data, just a few minutes old, and the job it
    kicked off means the next refresh gets the new figures. Its age is reported so the
    page can say how old it is rather than implying it is live.
    """
    qid = C.CLASSPLUS_QUERY_ID
    # POST decides freshness; it answers with a result if the cache is young enough,
    # otherwise with a job. Its payload is trimmed and carries no SQL, and the SQL is
    # the only place the covered window is written down — so the numbers are always
    # read back from results.json, which returns the full record.
    j = _cp_call(f"queries/{qid}/results", {"max_age": CP_TTL}, timeout=60)
    job = (j.get("job") or {}).get("id") if "query_result" not in j else None
    deadline = time.time() + CP_POLL_BUDGET
    while job and time.time() < deadline:
        time.sleep(2)
        # 1 pending, 2 started, 3 success, 4 failure, 5 cancelled
        if (_cp_call(f"queries/{qid}/jobs/{job}").get("job") or {}).get("status") in (3, 4, 5):
            break
    return _cp_parse(_cp_call(f"queries/{qid}/results.json", timeout=60)["query_result"])


def classplus(since, until):
    """(data, note) — data is None whenever it cannot be trusted for THIS window."""
    if not C.CLASSPLUS_ON:
        return None, None
    d, ok = _part("classplus", "query", classplus_fetch, CP_TTL)
    if not ok or not d:
        return None, "Classplus is not responding — signup and mandate columns are hidden."
    if tuple(d["window"]) != (since, until):
        w = d["window"][0] if d["window"][0] == d["window"][1] else " → ".join(d["window"])
        return None, (f"Classplus covers {w}, not this window — its query has the dates "
                      f"written into the SQL, so signups and mandates are hidden here.")
    return d, None


# ---------------------------------------------------------------- build ----
LISTING_LABEL = {"campaigns": "campaigns", "adsets": "ad sets", "ads": "ads",
                 "insights": "spend"}


def rate_limit_report(accounts):
    """What Meta is currently refusing, and when it said it would stop.

    accounts: {account id: display name}. Returns a dict the page renders directly —
    everything it needs to state the situation without inferring anything.
    """
    now = time.time()
    with _usage_lock:
        live = {k: v for k, v in _throttle.items() if v["until"] > now}
        usage = {a: dict(u) for a, u in _usage.items()}
    per = {}
    for (acct, kind), t in live.items():
        e = per.setdefault(acct, {"account": accounts.get(acct, acct), "id": acct,
                                  "listings": [], "until": 0, "since": t["since"]})
        e["listings"].append(LISTING_LABEL.get(kind, kind))
        e["until"] = max(e["until"], t["until"])
        e["since"] = min(e["since"], t["since"])
    out = []
    for e in per.values():
        e["listings"].sort()
        e["eta_sec"] = int(e["until"] - now)
        e["until_ist"] = datetime.fromtimestamp(e["until"], IST).strftime("%H:%M")
        e["since_ist"] = datetime.fromtimestamp(e["since"], IST).strftime("%H:%M")
        e["held_min"] = int((now - e["since"]) // 60)
        out.append(e)
    out.sort(key=lambda e: -e["eta_sec"])
    # Budget headroom, reported whether or not anything is throttled: total_time is the
    # limit that actually binds here, so seeing it climb is the warning that matters.
    budget = [{"account": accounts.get(a, a), "id": a,
               "time_pct": u.get("total_time", 0), "calls_pct": u.get("call_count", 0),
               "cpu_pct": u.get("total_cputime", 0), "tier": u.get("tier", "")}
              for a, u in usage.items() if a in accounts]
    budget.sort(key=lambda b: -b["time_pct"])
    return {
        "active": bool(out),
        "accounts": out,
        # absolute deadline for the page to schedule its own retry against
        "retry_in_sec": max((e["eta_sec"] for e in out), default=0),
        "retry_at_ist": max(out, key=lambda e: e["eta_sec"])["until_ist"] if out else "",
        "budget": budget,
        "worst_time_pct": max((b["time_pct"] for b in budget), default=0),
    }


def build(since, until):
    started = time.time()
    degraded = []
    budgets_known = True
    trials = branch_trials_by_ad(since, until)

    ads, adsets, campaigns, accounts = {}, {}, {}, {}

    for a in ACCOUNTS:
        insights = meta_insights(a["id"], since, until)
        camps, live_sets, live_ads, ok = meta_roster(a["id"])
        missing = [lbl for lbl, k in (("campaigns", "campaigns"), ("ad sets", "adsets"),
                                      ("ads", "ads")) if not ok[k]]
        if missing:
            degraded.append({"account": a["name"], "missing": missing})
        if not ok["adsets"]:
            budgets_known = False
        # Whatever listing is missing, nothing in it can be known to be paused, and
        # defaulting to paused would empty the tables behind the "active only" filter.
        # Anything that spent in the window was live for part of it: treat it as live
        # and label the status unknown rather than asserting something false.
        set_status = "INACTIVE" if ok["adsets"] else "UNKNOWN"
        set_active = not ok["adsets"]
        ad_status = "INACTIVE" if ok["ads"] else "UNKNOWN"
        ad_active = not ok["ads"]
        accounts[a["id"]] = {"id": a["id"], "name": a["name"], "spend": 0.0,
                             "budget": 0.0, "t101": 0.0, "t10m": 0.0,
                             "active_adsets": 0, "active_ads": 0}
        cstat = {c["id"]: c for c in camps}
        live_set_ids = {s["id"] for s in live_sets}
        live_ad_ids = {x["id"] for x in live_ads}

        # roster first, so ACTIVE objects with zero spend still show up
        for c in camps:
            campaigns[c["id"]] = {
                "id": c["id"], "name": c["name"], "status": c.get("effective_status", ""),
                "account": a["name"], "account_id": a["id"], "spend": 0.0, "budget": 0.0,
                "t101": 0.0, "t10m": 0.0, "active_adsets": 0, "active_ads": 0}
        for s in live_sets:
            adsets[s["id"]] = {
                "id": s["id"], "name": s["name"], "status": s.get("effective_status", ""),
                "active": True, "budget": int(s.get("daily_budget") or 0) / 100,
                "campaign_id": s.get("campaign_id"),
                "campaign": (cstat.get(s.get("campaign_id")) or {}).get("name", ""),
                "account": a["name"], "account_id": a["id"],
                "spend": 0.0, "t101": 0.0, "t10m": 0.0, "active_ads": 0}
        for x in live_ads:
            ads[x["id"]] = {
                "id": x["id"], "name": x["name"], "status": x.get("effective_status", ""),
                "active": True, "adset_id": x.get("adset_id"), "adset": "",
                "campaign_id": x.get("campaign_id"), "campaign": "",
                "account": a["name"], "account_id": a["id"],
                "spend": 0.0, "t101": 0.0, "t10m": 0.0}

        # spend; also picks up objects that spent in the window but are no longer active
        for r in insights:
            sp = float(r.get("spend") or 0)
            aid, sid, cid = r.get("ad_id"), r.get("adset_id"), r.get("campaign_id")
            if cid and cid not in campaigns:
                campaigns[cid] = {"id": cid, "name": r.get("campaign_name", ""),
                                  "status": set_status, "account": a["name"],
                                  "account_id": a["id"], "spend": 0.0, "budget": 0.0,
                                  "t101": 0.0, "t10m": 0.0,
                                  "active_adsets": 0, "active_ads": 0}
            if sid and sid not in adsets:
                adsets[sid] = {"id": sid, "name": r.get("adset_name", ""),
                               "status": set_status, "active": set_active, "budget": 0.0,
                               "campaign_id": cid, "campaign": r.get("campaign_name", ""),
                               "account": a["name"], "account_id": a["id"],
                               "spend": 0.0, "t101": 0.0, "t10m": 0.0, "active_ads": 0}
            if aid and aid not in ads:
                ads[aid] = {"id": aid, "name": r.get("ad_name", ""), "status": ad_status,
                            "active": ad_active, "adset_id": sid, "adset": r.get("adset_name", ""),
                            "campaign_id": cid, "campaign": r.get("campaign_name", ""),
                            "account": a["name"], "account_id": a["id"],
                            "spend": 0.0, "t101": 0.0, "t10m": 0.0}
            if aid:
                ads[aid]["spend"] += sp
                ads[aid]["adset"] = ads[aid]["adset"] or r.get("adset_name", "")
                ads[aid]["campaign"] = ads[aid]["campaign"] or r.get("campaign_name", "")

        accounts[a["id"]]["active_adsets"] = (len(live_set_ids) if ok["adsets"] else
            sum(1 for x in adsets.values() if x["account_id"] == a["id"]))
        accounts[a["id"]]["active_ads"] = (len(live_ad_ids) if ok["ads"] else
            sum(1 for x in ads.values() if x["account_id"] == a["id"]))

    # fill parent names for roster ads that never spent
    for x in ads.values():
        if not x["adset"] and x["adset_id"] in adsets:
            x["adset"] = adsets[x["adset_id"]]["name"]
        if not x["campaign"] and x["campaign_id"] in campaigns:
            x["campaign"] = campaigns[x["campaign_id"]]["name"]

    for coll in (ads, adsets, campaigns, accounts):
        for o in coll.values():
            for k in CP_KEYS:
                o[k] = 0.0

    # ---- attach Branch trials to ads by NAME -------------------------------
    by_name = defaultdict(list)
    for x in ads.values():
        by_name[x["name"]].append(x)
    dup_names = sum(1 for v in by_name.values() if len(v) > 1)

    matched = {k: 0.0 for k in EVENTS}
    for key in EVENTS:
        for name, n in trials[key].items():
            if not name or name not in by_name:
                continue
            group = by_name[name]
            matched[key] += n
            if len(group) == 1:
                group[0][key] += n
                continue
            # same name on several ads: split by spend so no rollup double counts
            tot = sum(g["spend"] for g in group)
            for g in group:
                g[key] += n * (g["spend"] / tot) if tot else n / len(group)
                g["shared_name"] = True

    # ---- attach Classplus signups/mandates to ads by the SAME name key ------
    cp, cp_note = classplus(since, until)
    cp_matched = {k: 0.0 for k in CP_KEYS}
    if cp:
        for name, rec in cp["by_ad"].items():
            group = by_name.get(name)
            if not group:
                continue
            for k in CP_KEYS:
                cp_matched[k] += rec[k]
            if len(group) == 1:
                for k in CP_KEYS:
                    group[0][k] += rec[k]
                continue
            tot = sum(g["spend"] for g in group)
            for g in group:
                share = (g["spend"] / tot) if tot else 1 / len(group)
                for k in CP_KEYS:
                    g[k] += rec[k] * share
                g["shared_name"] = True

    # ---- roll up ad -> adset -> campaign -> account -------------------------
    for x in ads.values():
        s = adsets.get(x["adset_id"])
        if s:
            s["spend"] += x["spend"]; s["t101"] += x["t101"]; s["t10m"] += x["t10m"]
            for k in CP_KEYS:
                s[k] += x[k]
            if x["active"]:
                s["active_ads"] += 1
    for s in adsets.values():
        c = campaigns.get(s["campaign_id"])
        if c:
            c["spend"] += s["spend"]; c["t101"] += s["t101"]; c["t10m"] += s["t10m"]
            for k in CP_KEYS:
                c[k] += s[k]
            c["active_ads"] += s["active_ads"]
            if s["active"]:
                c["active_adsets"] += 1; c["budget"] += s["budget"]
    for c in campaigns.values():
        a = accounts.get(c["account_id"])
        if a:
            a["spend"] += c["spend"]; a["t101"] += c["t101"]; a["t10m"] += c["t10m"]
            for k in CP_KEYS:
                a[k] += c[k]
            a["budget"] += c["budget"]

    combined = {"spend": sum(a["spend"] for a in accounts.values()),
                "budget": sum(a["budget"] for a in accounts.values()),
                "t101": sum(a["t101"] for a in accounts.values()),
                "t10m": sum(a["t10m"] for a in accounts.values()),
                "active_adsets": sum(a["active_adsets"] for a in accounts.values()),
                "active_ads": sum(a["active_ads"] for a in accounts.values())}
    for k in CP_KEYS:
        combined[k] = sum(a[k] for a in accounts.values())

    branch_totals = {k: sum(trials[k].values()) for k in EVENTS}
    # Branch trials whose ad name matches nothing in either account: organic, other
    # channels, or ads deleted out of Meta. Shown so the combined CPT is honest about
    # what it does and does not cover.
    unattributed = {k: branch_totals[k] - matched[k] for k in EVENTS}

    return {
        "since": since, "until": until,
        "generated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "took": round(time.time() - started, 1),
        "cpt_target": C.CPT_TARGET,
        "combined": combined,
        "accounts": sorted(accounts.values(), key=lambda r: -r["spend"]),
        "campaigns": sorted(campaigns.values(), key=lambda r: -r["spend"]),
        "adsets": sorted(adsets.values(), key=lambda r: -r["spend"]),
        "ads": sorted(ads.values(), key=lambda r: -r["spend"]),
        "branch_totals": branch_totals,
        "matched": {k: round(v, 1) for k, v in matched.items()},
        "unattributed": {k: round(v, 1) for k, v in unattributed.items()},
        "duplicate_ad_names": dup_names,
        # per-account list of which roster listings Meta would not return. Spend,
        # trials and CPT stay correct regardless; only statuses and budgets are affected.
        "degraded": degraded,
        "budgets_known": budgets_known,
        "rate_limit": rate_limit_report({a["id"]: a["name"] for a in ACCOUNTS}),
        # Signups / trial mandates per ad from the Classplus DB, or why they are absent.
        # `unmatched` is Classplus signups whose ad name is organic or matches no live
        # Meta ad — kept visible so the per-ad columns are never mistaken for the total.
        "classplus": ({
            "available": True,
            "window": cp["window"],
            "retrieved_at": cp["retrieved_at"],
            "age_min": cp["age_min"],
            "totals": cp["totals"],
            "organic": cp["organic"],
            "matched": {k: round(v, 1) for k, v in cp_matched.items()},
            "unmatched": {k: round(cp["totals"][k] - cp_matched[k], 1) for k in CP_KEYS},
        } if cp else {"available": False, "note": cp_note}),
    }


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else today_ist()
    u = sys.argv[2] if len(sys.argv) > 2 else d
    r = build(d, u)
    print(json.dumps({k: v for k, v in r.items()
                      if k not in ("adsets", "ads", "campaigns", "accounts")}, indent=1))
    print(f"adsets={len(r['adsets'])} ads={len(r['ads'])} campaigns={len(r['campaigns'])}")
