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
import html
import json, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import config as C
import google_ads as GA
import history as H

IST = timezone(timedelta(minutes=330))
BRANCH_URL = "https://api2.branch.io/v1/query/analytics"
BRANCH_MAX_SPAN = 7          # Branch Query API caps a request at 7 days

# Which accounts, which Branch app, which two events and what "good" costs are the only
# things that differ per brand; everything below this line is brand-agnostic. `EVENTS`
# is therefore no longer a module constant — it is read off the brand being built, and
# the two slot keys (t101 headline, t10m secondary) stay fixed so nothing downstream
# has to know which brand it is looking at.
ALL_ACCOUNTS = [a for b in C.BRANDS.values() for a in b["accounts"]]


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


# Which usage headers Meta returns depends on the EDGE. Verified 2026-08-21 against both
# accounts, because assuming otherwise produced a wrong claim once already:
#
#   /campaigns, /insights, single-object reads -> x-business-use-case-usage
#   /adsets, /ads                              -> x-ad-account-usage + x-app-usage ONLY
#
# So the ad set and ad listings — the two that actually get throttled here — never carry
# `estimated_time_to_regain_access`, and their code-17 response reports acc_id_util_pct 0
# and reset_time_duration 0. There is no recovery estimate to be had for those. The page
# says so and falls back to its own re-check interval rather than inventing a time.
#
# Each business-use-case entry is a SEPARATE quota keyed by type (ads_management for the
# roster, ads_insights for spend), so they are kept per type instead of collapsed into one
# number — they throttle independently.
_usage_lock = threading.Lock()
_usage = {}                       # account id -> {"types": {...}, "tier", "acct_pct", ...}
_throttle = {}                    # (account id, edge) -> {"since","until","regain_min"}


def _note_usage(headers, acct):
    """Fold this response's usage headers into the account picture. Returns what it
    learned about recovery time, which is usually nothing."""
    if not acct:
        return {}
    now, found, regain = time.time(), {}, 0
    raw = headers.get("x-business-use-case-usage")
    if raw:
        try:
            j = json.loads(raw)
        except ValueError:
            j = {}
        for entries in j.values():
            for e in entries or []:
                r = int(e.get("estimated_time_to_regain_access") or 0)
                regain = max(regain, r)
                found.setdefault("types", {})[e.get("type") or "unknown"] = {
                    "calls_pct": int(e.get("call_count") or 0),
                    "cpu_pct": int(e.get("total_cputime") or 0),
                    "time_pct": int(e.get("total_time") or 0),
                    "regain_min": r, "at": now}
                if e.get("ads_api_access_tier"):
                    found["tier"] = e["ads_api_access_tier"]
    raw = headers.get("x-ad-account-usage")
    if raw:
        try:
            j = json.loads(raw)
        except ValueError:
            j = {}
        found["acct_pct"] = float(j.get("acc_id_util_pct") or 0)
        found["reset_sec"] = int(j.get("reset_time_duration") or 0)
        if j.get("ads_api_access_tier"):
            found.setdefault("tier", j["ads_api_access_tier"])
    if not found:
        return {}
    types = found.pop("types", {})
    with _usage_lock:
        cur = _usage.setdefault(acct, {"types": {}})
        # merge, never replace: an /adsets response carries no business-use-case numbers
        # and would otherwise wipe the ones /campaigns just supplied
        cur["types"].update(types)
        cur.update(found)
        cur["at"] = now
    return {"regain_min": regain, "reset_sec": found.get("reset_sec", 0)}


def _acct_of(path):
    head = path.split("/")[0]
    return head if head.startswith("act_") else ""


def _mark_throttle(acct, edge, regain_min):
    """regain_min of 0 means Meta gave no estimate — which is the normal case for the
    listing edges. Then the deadline is OUR re-check interval, and it is labelled as
    such so the page never presents it as a promise from Meta."""
    now = time.time()
    with _usage_lock:
        prev = _throttle.get((acct, edge))
        _throttle[(acct, edge)] = {
            # keep the original start across repeated failures, so the page can say how
            # long this has been going on rather than restarting the clock each attempt
            "since": prev["since"] if prev else now,
            "until": now + (regain_min * 60 if regain_min else ROSTER_RETRY),
            "regain_min": regain_min,
        }


def _clear_throttle(acct, edge):
    with _usage_lock:
        _throttle.pop((acct, edge), None)


def _graph(path, params, tries=6, rl_retries=2, raw=False):
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
                    regain = usage.get("regain_min", 0) or usage.get("reset_sec", 0) // 60
                    _mark_throttle(acct, edge, regain)
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
        if raw:
            # A single object read has no `data` and no paging; accumulating it the
            # normal way would quietly return an empty list.
            return j
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
# These MUST stay clear of the page's 30-minute auto-refresh rather than matching it.
# A TTL equal to the refresh interval aliases against it: the roster's age is stamped
# when its fetch RETURNS, so when the next tick asks, the entry is a few seconds short
# of expiry, is served from cache, and the refresh does nothing. The listing then only
# actually refreshes every OTHER tick — budgets moved on Meta took up to an hour to
# appear rather than the half hour the cadence promises. The slack absorbs the fetch
# itself, build time, and the cold-start delay off a sleeping instance.
ROSTER_TTL = int(os.environ.get("ROSTER_TTL", "1500"))        # < 30 min refresh tick
ADS_ROSTER_TTL = int(os.environ.get("ADS_ROSTER_TTL", "3300"))  # < 2 ticks
# After a roster fetch fails, stop asking for a while. Retrying a throttled endpoint on
# every refresh both feeds the rate limit that caused it and costs the caller the full
# back-off sleep on each build (~15s), so a failure is cached almost as deliberately as
# a success.
ROSTER_RETRY = int(os.environ.get("ROSTER_RETRY", "300"))
_roster_cache, _roster_lock = {}, threading.Lock()
_roster_fail_until = {}


# ---- ad creative preview ---------------------------------------------------
# The rendered ad -- the actual image or video with its copy, as it runs. Resolved ON
# DEMAND rather than folded into the payload, for three reasons: the signed URL expires,
# so a payload restored from the store hours later would carry dead links; it is ~530
# bytes per ad, which is another 1 MB on a 1,866-ad brand; and it is a credential-bearing
# URL, so it should not sit in the page for every ad whether or not anyone looks.
PREVIEW_FORMATS = ("MOBILE_FEED_STANDARD", "INSTAGRAM_STANDARD", "INSTAGRAM_STORY",
                   "INSTAGRAM_REELS", "DESKTOP_FEED_STANDARD", "FACEBOOK_STORY_MOBILE")
PREVIEW_TTL = int(os.environ.get("PREVIEW_TTL", "900"))
_preview_cache, _preview_lock = {}, threading.Lock()


def ad_preview(ad_id, fmt="MOBILE_FEED_STANDARD"):
    """(preview_url, account_id) for one ad, or (None, account_id) if Meta renders none.

    The account comes back on the same request so the caller can check the ad actually
    belongs to the brand whose link was used. Without that check any valid team link
    would preview any ad the token can see, including another brand's.
    """
    fmt = fmt if fmt in PREVIEW_FORMATS else PREVIEW_FORMATS[0]
    key = (str(ad_id), fmt)
    with _preview_lock:
        hit = _preview_cache.get(key)
        if hit and time.time() - hit["at"] < PREVIEW_TTL:
            return hit["url"], hit["acct"]
    j = _graph(str(ad_id), {"fields": f"account_id,previews.ad_format({fmt})"},
               rl_retries=0, raw=True)
    acct = j.get("account_id") or ""
    body = ((j.get("previews") or {}).get("data") or [{}])[0].get("body") or ""
    m = re.search(r'src="([^"]+)"', body)
    url = html.unescape(m.group(1)) if m else None
    with _preview_lock:
        _preview_cache[key] = {"at": time.time(), "url": url, "acct": acct}
        if len(_preview_cache) > 600:
            for k in sorted(_preview_cache,
                            key=lambda k: _preview_cache[k]["at"])[:200]:
                _preview_cache.pop(k, None)
    return url, acct


# ---- testing -> trial cohorts ----------------------------------------------
# The funnel this whole operation runs on: creatives start in a testing campaign
# optimised for installs, the ones that work are graduated into a trial campaign, and a
# few of those go on to carry real spend. Read by the day a creative FIRST went live in
# testing, so each row is a cohort and the columns are what became of it.
#
# The unit is the ad NAME, not the ad id, because graduation clones the creative into a
# new campaign under the same name — by id, every graduate would look like a brand new
# ad that had never been tested.
WINNER_SPEND = float(os.environ.get("WINNER_SPEND", "5000"))
SUPER_SPEND = float(os.environ.get("SUPER_SPEND", "8000"))
# How far before the window to look, to tell "went live today" from "was already
# running". Without it every ad alive on day one of the window counts as new that day.
COHORT_LOOKBACK = int(os.environ.get("COHORT_LOOKBACK", "45"))
COHORT_TTL = int(os.environ.get("COHORT_TTL", "1800"))
# The stored artifact covers this many days of cohorts in one document, and every window
# the page offers is a slice of it. One build a night, one small read per view — the same
# bargain the longevity folds and the daily series already make with the store.
GRAD_STORE_DAYS = int(os.environ.get("GRAD_STORE_DAYS", "120"))
# Bumped when the shape of a stored cohort row changes, so a new deploy rejects the old
# artifact instead of rendering it with a column missing.
GRAD_SHAPE = 2
_cohort_cache, _cohort_lock = {}, threading.Lock()


def _grad_ns(brand):
    return H.agg_ns(brand, "grad", 0)


def _cohort_scan(brand, since, until):
    """One pass over the stored raw days -> a record per creative.

    A record, not a per-day total, because every column on the Graduation view has to be
    recomputable under a filter: "the ad sets uploaded on the 20th whose testing CPI came
    in under Rs8" is a different set from the day's total, and a stored aggregate can only
    answer the question it was aggregated for. Nothing here asks Meta or Branch — the raw
    daily documents already hold it, which is why this folds nightly and reads back as one
    document.
    """
    B = C.brand(brand)
    testing_re = re.compile(B.get("testing_re") or C.TESTING_RE_DEFAULT)
    look = (datetime.strptime(since, "%Y-%m-%d").date()
            - timedelta(days=COHORT_LOOKBACK)).strftime("%Y-%m-%d")
    raw = H.fetch_raw(brand, date_range(look, until)) if H.available() else {}
    stored = sorted(d for d in raw if raw[d])

    first_test, first_trial = {}, {}
    test_spend, test_inst = defaultdict(float), defaultdict(float)
    trial_spend, trial_trials = defaultdict(float), defaultdict(float)
    # What the testing campaigns spent ON each day, as opposed to what a cohort went on to
    # spend. A day with testing spend and no new name means the pipeline shipped nothing
    # that day; a day with neither means the brand was not testing at all.
    day_test_spend = defaultdict(float)
    ev = next(iter(B["events"]), "t101")
    for d in stored:
        day = raw[d] or {}
        stage_of = {}
        for _acct, rows in (day.get("meta") or {}).items():
            for r in rows:
                n = r.get("ad_name") or ""
                if not n:
                    continue
                st = ("testing" if testing_re.search(r.get("campaign_name") or "")
                      else "trial")
                # A name running in both on one day counts as trial for that day's events:
                # the trial campaign is the one whose result is being judged.
                if stage_of.get(n) != "trial":
                    stage_of[n] = st
                sp = float(r.get("spend") or 0)
                if st == "trial":
                    trial_spend[n] += sp
                    first_trial.setdefault(n, d)
                else:
                    test_spend[n] += sp
                    first_test.setdefault(n, d)
                    day_test_spend[d] += sp
        branch = day.get("branch") or {}
        for n, v in (branch.get(ev) or {}).items():
            if stage_of.get(n) == "trial":
                trial_trials[n] += float(v or 0)
        # Installs while the creative was still in TESTING. That is the denominator of the
        # testing CPI the graduation decision is made on — installs it earns later, in a
        # trial campaign, say nothing about how it tested.
        for n, v in (branch.get("inst") or {}).items():
            if stage_of.get(n) == "testing":
                test_inst[n] += float(v or 0)

    # ---- the unsettled tail -------------------------------------------------------
    # The raw day store deliberately stops three days back: Meta bills late and Branch
    # backfills, so a day is not written until it has stopped moving. That left this view
    # ending on the 29th while people were asking about the 31st. The daily series fold is
    # already refreshed hourly and already carries the tail, keyed by ad name AND stage
    # with spend, trials and installs per day — everything this scan needs. Take the days
    # the raw store does not have from there, and mark them provisional so nobody reads a
    # half-settled day as final.
    last_raw = stored[-1] if stored else None
    provisional = []
    sart = H.get_agg(_series_ns(brand, "script")) if H.available() else None
    if sart and sart.get("shape") == SERIES_SHAPE:
        tail = sorted(d for d in (sart.get("dates") or [])
                      if (last_raw is None or d > last_raw) and d <= until)
        if tail:
            provisional = tail
            for r in sart.get("rows") or []:
                n = r.get("label") or ""
                if not n:
                    continue
                testing = (r.get("stage") or "") == "testing"
                for d in tail:
                    v = (r.get("days") or {}).get(d)
                    if not v:
                        continue
                    sp = float(v.get("spend") or 0)
                    if testing:
                        if sp:
                            test_spend[n] += sp
                            day_test_spend[d] += sp
                            first_test.setdefault(n, d)
                        test_inst[n] += float(v.get("inst") or 0)
                    else:
                        if sp:
                            trial_spend[n] += sp
                            first_trial.setdefault(n, d)
                        trial_trials[n] += float(v.get(ev) or 0)

    out = []
    for n, d in first_test.items():
        g = first_trial.get(n)
        out.append({"n": n, "d": d,
                    # Graduated means it turned up in a trial campaign on or after the day
                    # it first ran in testing. Before is a different creative that happens
                    # to share a name.
                    "g": g if (g and g >= d) else None,
                    "ts": round(test_spend[n], 2), "ti": round(test_inst[n], 1),
                    "xs": round(trial_spend[n], 2), "xt": round(trial_trials[n], 1)})
    out.sort(key=lambda r: (r["d"], r["n"]))
    dates = stored + provisional
    last_test = max((d for d in dates if day_test_spend.get(d, 0) > 0), default=None)
    return out, {"stored_days": len(stored), "lookback_from": look,
                 "last_test_day": last_test, "event": ev,
                 "event_label": B["labels"].get(ev, "Trials"),
                 "days": {d: round(day_test_spend.get(d, 0.0), 2) for d in dates},
                 "dates": dates, "provisional": provisional}


def cohort_build(brand, until=None):
    """The whole retained range as one document, ready to store and to slice.

    Runs to TODAY rather than to the settle line: the last few days come from the daily
    series fold instead of the raw store, and are labelled as such.
    """
    until = until or today_ist()
    since = (datetime.strptime(until, "%Y-%m-%d").date()
             - timedelta(days=GRAD_STORE_DAYS - 1)).strftime("%Y-%m-%d")
    recs, meta = _cohort_scan(brand, since, until)
    dates = [d for d in meta["dates"] if d >= since]
    # `built_for` is the settled day the fold was asked about; `until` is the last day the
    # store actually held when it ran, usually a day behind it. The reader compares
    # against the first: matching on the second rejects every artifact for being one day
    # short of a day that does not exist yet.
    return {"shape": GRAD_SHAPE, "brand": brand, "since": since, "built_for": until,
            "until": dates[-1] if dates else until, "dates": dates,
            "day_test_spend": {d: meta["days"][d] for d in dates},
            "provisional": [d for d in (meta.get("provisional") or []) if d >= since],
            "creatives": [r for r in recs if r["d"] >= since],
            "last_test_day": meta["last_test_day"], "event": meta["event"],
            "event_label": meta["event_label"], "stored_days": meta["stored_days"],
            "lookback_from": meta["lookback_from"], "generated_at": now_ist_str()}


def precompute_cohorts(brand):
    """Fold the creative ledger for one brand and store it. Nightly."""
    art = cohort_build(brand)
    ok = H.put_agg(_grad_ns(brand), today_ist(), art)
    return {"ok": ok, "brand": brand, "days": len(art["dates"]),
            "creatives": len(art["creatives"]), "since": art["since"],
            "until": art["until"], "error": None if ok else H.last_error()}


# The two trial campaigns a creative can graduate into are chosen by what it cost to buy
# an install while it was testing, so that is the cut the view filters on.
CPI_BANDS = {"lt8": ("under Rs8", lambda c: c is not None and c < 8),
             "8to12": ("Rs8 to Rs12", lambda c: c is not None and 8 <= c <= 12),
             "gt12": ("over Rs12", lambda c: c is not None and c > 12),
             "none": ("no installs", lambda c: c is None)}


def _cpi(rec):
    """Testing CPI: what the creative paid per install while it was still in testing.
    None when it never got an install — a rate with no denominator is not a zero."""
    return (rec["ts"] / rec["ti"]) if rec.get("ti") else None


def live_trial_names(brand):
    """Ad-set names live in a TRIAL campaign right now, or None if Meta would not say.

    The one figure on this view that cannot come out of the store: "still live" is a fact
    about this minute. None rather than an empty set when a roster is degraded — zero live
    ad sets and "we could not ask" must not render as the same thing.
    """
    B = C.brand(brand)
    testing_re = re.compile(B.get("testing_re") or C.TESTING_RE_DEFAULT)
    names = set()
    for a in B["accounts"]:
        camps, live_sets, _live_ads, ok = meta_roster(a["id"], False)
        if not ok["adsets"] or not ok["campaigns"]:
            return None
        testing_ids = {c["id"] for c in camps
                       if testing_re.search(c.get("name") or "")}
        for s in live_sets:
            if s.get("campaign_id") not in testing_ids and s.get("name"):
                names.add(s["name"])
    return names


def _cohort_rows(art, since, until, band=None, live=None):
    """Fold the ledger into one row per day, under whichever CPI band is asked for."""
    keep = CPI_BANDS.get(band, (None, None))[1] if band and band != "all" else None
    prov = set(art.get("provisional") or [])
    rows = {d: {"date": d, "live": 0, "grad": 0, "win": 0, "sup": 0, "onair": 0,
                "d1": 0, "d2": 0, "d3": 0, "d4": 0, "d5": 0,
                "test_spend": 0.0, "test_inst": 0.0, "trial_spend": 0.0, "trials": 0.0,
                "day_test_spend": (art.get("day_test_spend") or {}).get(d, 0.0),
                "prov": d in prov}
            for d in art.get("dates") or [] if since <= d <= until}
    for rec in art.get("creatives") or []:
        r = rows.get(rec["d"])
        if r is None:
            continue
        # What was uploaded that day is a fact about the day, not about the band: a
        # creative's testing CPI is what decides which trial campaign it graduates INTO,
        # so the filter narrows what happened next and leaves the day's uploads alone.
        # Filtering the uploads too made the first column move under the reader every
        # time they changed the band, which is not what the band means.
        r["live"] += 1
        r["test_spend"] += rec["ts"]
        r["test_inst"] += rec["ti"]
        if not rec.get("g"):
            continue
        if keep is not None and not keep(_cpi(rec)):
            continue
        r["grad"] += 1
        r["trial_spend"] += rec["xs"]
        r["trials"] += rec["xt"]
        # Exclusive buckets by how long the creative took to graduate. Day 1 holds the
        # same-day graduations too: a creative moved before its first testing day closed
        # was still moved on day one, and a bucket nobody can reach is worse than a
        # bucket that reads a shade wide.
        lag = (datetime.strptime(rec["g"], "%Y-%m-%d")
               - datetime.strptime(rec["d"], "%Y-%m-%d")).days
        r["d1" if lag <= 1 else "d2" if lag == 2 else "d3" if lag == 3
          else "d4" if lag == 4 else "d5"] += 1
        if live is not None and rec["n"] in live:
            r["onair"] += 1
        if rec["xs"] > WINNER_SPEND:
            r["win"] += 1
        if rec["xs"] > SUPER_SPEND:
            r["sup"] += 1
    out = [rows[d] for d in sorted(rows)]
    for r in out:
        for k in ("test_spend", "trial_spend", "day_test_spend"):
            r[k] = round(r[k], 2)
        for k in ("trials", "test_inst"):
            r[k] = round(r[k], 1)
    return out


def _cohort_totals(rows):
    return {k: round(sum(r[k] for r in rows), 2)
            for k in ("live", "grad", "win", "sup", "onair", "d1", "d2", "d3", "d4",
                      "d5", "test_spend", "test_inst", "trial_spend", "trials")}


def cohorts(brand, since, until, band=None, force=False):
    """Per day: creatives uploaded into testing, and what became of them.

    Served from the nightly ledger whenever one covers the window — the scan behind it
    reads two and a half months of raw daily documents and takes ten seconds, which is not
    a thing to do inside a page load. A missing or stale artifact falls back to scanning
    live, so a night the job did not run costs latency and not the view.
    """
    band = band if band in CPI_BANDS else "all"
    key = (brand, since, until, band)
    if not force:
        with _cohort_lock:
            hit = _cohort_cache.get(key)
        if hit and time.time() - hit["at"] < COHORT_TTL:
            return dict(hit["data"], cached=True,
                        age_min=int((time.time() - hit["at"]) / 60))

    art, stored = None, False
    if not force:
        got = H.get_agg(_grad_ns(brand))
        if got and got.get("shape") == GRAD_SHAPE \
                and got.get("since", "9") <= since \
                and got.get("built_for", "") >= until:
            art, stored = got, True
    if art is None:
        recs, meta = _cohort_scan(brand, since, until)
        art = {"brand": brand, "since": since, "until": until,
               "dates": meta["dates"], "day_test_spend": meta["days"],
               "provisional": meta.get("provisional") or [],
               "creatives": recs, "last_test_day": meta["last_test_day"],
               "event": meta["event"], "event_label": meta["event_label"],
               "stored_days": meta["stored_days"], "lookback_from": meta["lookback_from"],
               "generated_at": now_ist_str()}

    # "Still live" is a fact about this minute, so it is read now rather than folded — and
    # it is allowed to fail without taking the rest of the view with it.
    try:
        live = live_trial_names(brand)
    except Exception:
        live = None
    rows = _cohort_rows(art, since, until, band, live)
    data = {"brand": brand,
            "since": rows[0]["date"] if rows else since,
            "until": rows[-1]["date"] if rows else until,
            "rows": rows, "totals": _cohort_totals(rows),
            "band": band, "bands": {k: v[0] for k, v in CPI_BANDS.items()},
            "live_known": live is not None,
            "winner_spend": WINNER_SPEND, "super_spend": SUPER_SPEND,
            "event": art.get("event"), "event_label": art.get("event_label"),
            "last_test_day": art.get("last_test_day"),
            "stored_days": art.get("stored_days"),
            "lookback_from": art.get("lookback_from"),
            "creatives": len(art.get("creatives") or []),
            "provisional": [d for d in (art.get("provisional") or [])
                            if since <= d <= until],
            "stored": stored, "built_at": art.get("generated_at") if stored else None,
            "cached": False, "age_min": 0, "generated_at": now_ist_str()}
    with _cohort_lock:
        _cohort_cache[key] = {"at": time.time(), "data": data}
        if len(_cohort_cache) > 16:
            for k in sorted(_cohort_cache, key=lambda k: _cohort_cache[k]["at"])[:4]:
                _cohort_cache.pop(k, None)
    return data


# ---- hourly ----------------------------------------------------------------
# Meta breaks insights down by hour of the ACCOUNT's timezone, retroactively, for any day
# — 96 rows for four days of one account, in under two seconds. Branch does not: its query
# API accepts granularity "day" and answers 500 to "hour". So spend can be charted by the
# hour for any past day and trials cannot, which is why the hourly view is about pacing
# (when the budget burns, whether a pause landed) and not about CPT.
HOURLY_DAYS = int(os.environ.get("HOURLY_DAYS", "7"))
HOURLY_TTL = int(os.environ.get("HOURLY_TTL", "600"))
_hourly_cache, _hourly_lock = {}, threading.Lock()


def _hour_of(row):
    """Meta gives '13:00:00 - 13:59:59'. The hour is the only part that carries meaning."""
    v = row.get("hourly_stats_aggregated_by_advertiser_time_zone") or ""
    try:
        return int(v[:2])
    except ValueError:
        return None


def hourly_spend(brand, days=None, force=False):
    """{days: [...], hours: {date: {hour: {spend, imp, clk}}}} for the last `days` days.

    Every account for the brand, summed. The account timezone is Asia/Kolkata for all of
    them, so "hour of the advertiser's timezone" is the hour people mean.
    """
    days = days or HOURLY_DAYS
    B = C.brand(brand)
    key = (brand, days)
    if not force:
        with _hourly_lock:
            hit = _hourly_cache.get(key)
        if hit and time.time() - hit["at"] < HOURLY_TTL:
            return dict(hit["data"], cached=True,
                        age_min=int((time.time() - hit["at"]) / 60))

    today = datetime.strptime(today_ist(), "%Y-%m-%d").date()
    since = (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    until = today.strftime("%Y-%m-%d")
    dates = date_range(since, until)
    hours = {d: {} for d in dates}
    err = None
    for a in B["accounts"]:
        try:
            rows = _graph(f"{a['id']}/insights", {
                "level": "account",
                "breakdowns": "hourly_stats_aggregated_by_advertiser_time_zone",
                "time_range": json.dumps({"since": since, "until": until}),
                "time_increment": "1",
                "fields": "spend,impressions,clicks,date_start"})
        except Exception as ex:
            # One account failing must not empty the chart: the others are still true,
            # and a partial answer that SAYS it is partial beats no answer.
            err = str(ex)[:160]
            continue
        for r in rows:
            d, h = r.get("date_start"), _hour_of(r)
            if d not in hours or h is None:
                continue
            e = hours[d].setdefault(h, {"spend": 0.0, "imp": 0.0, "clk": 0.0})
            e["spend"] += float(r.get("spend") or 0)
            e["imp"] += _num(r.get("impressions"))
            e["clk"] += _num(r.get("clicks"))
    for d in hours:
        for h in hours[d]:
            hours[d][h]["spend"] = round(hours[d][h]["spend"], 2)
    out = {"brand": brand, "days": dates, "hours": hours, "error": err,
            "accounts": len(B["accounts"]), "generated_at": now_ist_str(),
            "cached": False, "age_min": 0}
    with _hourly_lock:
        _hourly_cache[key] = {"at": time.time(), "data": out}
        if len(_hourly_cache) > 12:
            for k in sorted(_hourly_cache, key=lambda k: _hourly_cache[k]["at"])[:4]:
                _hourly_cache.pop(k, None)
    return out


# Trials have no hourly source, so the only way to ever chart them by hour is to write
# down where the running total stood on each hour and subtract. That is what this records:
# one point an hour, per brand, kept for the day. It is deliberately NOT a side effect of
# the Chat message — that runs 9am to 11pm and can fail on its own — because a series with
# holes in it is worth much less than one without.
def hour_ns(brand):
    return f"{brand}hrs"


def hour_snapshot(brand, trials, spend, installs=None):
    """Append where the day's running totals stand right now. Best effort, never raises."""
    if not H.available():
        return False
    day = today_ist()
    try:
        got, ok = H.get_day_raw(hour_ns(brand), day)
        if not ok:
            return False
        doc = got if isinstance(got, dict) else {}
        pts = doc.get("points") or []
        now = datetime.now(IST)
        pts = [p for p in pts if p.get("h") != now.hour]      # one point per hour, latest wins
        pts.append({"h": now.hour, "at": now.strftime("%H:%M"), "ts": int(time.time()),
                    "trials": round(float(trials or 0), 1),
                    "spend": round(float(spend or 0), 2),
                    "installs": None if installs is None else round(float(installs), 1)})
        doc["points"] = sorted(pts, key=lambda p: p["h"])[-30:]
        return bool(H.put_agg(hour_ns(brand), day, doc))
    except Exception:
        return False


def hour_points(brand, dates):
    """{date: [points]} for whichever of these days were recorded."""
    if not (H.available() and dates):
        return {}
    out = {}
    for d in dates:
        try:
            got, ok = H.get_day_raw(hour_ns(brand), d)
        except Exception:
            continue
        if ok and isinstance(got, dict) and got.get("points"):
            out[d] = got["points"]
    return out


# ---- daily budget history --------------------------------------------------
# Meta exposes a budget only as it is RIGHT NOW. Insights carry spend per day but never
# the budget that produced it, and this app's activity-log access retains one day. So
# budget history cannot be reconstructed backwards -- it can only be recorded forward,
# starting the first time this runs. Nothing here pretends otherwise: a day with no
# snapshot is absent, never zero.
# ---- reach backfill --------------------------------------------------------
# Re-fetching a stored day with impressions and clicks costs a full ad-level insights
# pull, 5-7 seconds, against a limit that binds on TIME. Ninety-odd days across three
# brands is half an hour of it, which is far too long for one request and far too rude
# to do in one burst -- so the work is done in bounded batches and the batch is what gets
# scheduled. A run that finds nothing to do costs one cheap call and says so, which is
# what lets the schedule sit there harmlessly after it has finished.
def _day_has_reach(day):
    """True when every Meta row in a stored day carries impressions AND video.

    Both, because they arrived a day apart: days settled between IMP_FROM and VID_FROM
    hold impressions and no hook rate. Requiring only impressions would call those days
    finished and leave the video columns permanently blank on them.
    """
    rows = [r for rs in ((day or {}).get("meta") or {}).values() for r in rs]
    return bool(rows) and all(has_imp(r) and has_vid(r) for r in rows)


def reach_ns(brand):
    # v2: the marker names WHAT was backfilled, and the answer changed when video joined
    # impressions. Reusing the old namespace would hand back a completed list and the
    # backfill would skip every day it now has more to fetch for.
    return f"{brand}reachdone2"


def reach_done(brand):
    """(set of dates known to carry impressions, ok). A tiny artifact -- a list of date
    strings -- kept so a batch does not have to re-read ninety days of raw rows just to
    work out what is left. Without it every call, including the ones that find nothing to
    do, paid fifteen seconds to prove it; with it the steady state is two cheap reads."""
    art, ok = H.get_agg_raw(reach_ns(brand))
    return set((art or {}).get("days") or []), ok


def reach_backfill(brand, budget_s=90, max_days=0, dry=False):
    """Re-fetch as many pending days as fit in the budget. Never raises.

    Newest first on purpose: if the run is cut short, the days people actually look at
    are the ones that landed.
    """
    started = time.time()
    have = sorted(H.have(brand) or [], reverse=True)
    done, ok = reach_done(brand)
    out = {"brand": brand, "stored": len(have), "written": 0, "failed": 0,
           "days": [], "throttled": False, "marker_ok": ok}
    todo = [d for d in have if d not in done]
    out["pending_before"] = len(todo)
    if not todo:
        out["pending_after"] = 0
        out["took"] = round(time.time() - started, 1)
        return out

    B = C.brand(brand)
    # Only the slice this batch could possibly reach, so the scan cost is bounded by the
    # budget rather than by how much history exists.
    slice_ = todo[:max_days] if max_days else todo[:24]
    raw = H.fetch_raw(brand, slice_)
    fresh_done = set()
    for d in slice_:
        if time.time() - started > budget_s:
            break
        stored_day = raw.get(d)
        # Already good -- a day stored after the fields were added, or one this ran on
        # before the marker existed. Mark it and move on rather than paying for it again.
        if _day_has_reach(stored_day):
            fresh_done.add(d)
            out["days"].append({"date": d, "skipped": "already has impressions and video"})
            continue
        t = time.time()
        try:
            got = {a["id"]: meta_insights_daily(a["id"], d, d) for a in B["accounts"]}
        except RateLimited:
            # Meta saying stop. Feeding it makes the wait longer and the run resumes on
            # the next tick anyway, so stopping costs nothing but time.
            out["throttled"] = True
            break
        except Exception as ex:
            out["failed"] += 1
            out["days"].append({"date": d, "error": str(ex)[:120]})
            continue
        n = sum(len(v) for v in got.values())
        imp = sum(_num(r.get("impressions")) for v in got.values() for r in v)
        # The stored day's Branch half is written back exactly as it was. Losing a day's
        # trials to a reach backfill would be an absurd trade.
        wrote = True if dry else H.put(brand, d, got,
                                       (stored_day or {}).get("branch") or {})
        if wrote:
            out["written"] += 1
            if not dry:
                fresh_done.add(d)
        else:
            out["failed"] += 1
        out["days"].append({"date": d, "rows": n, "impressions": int(imp),
                            "took": round(time.time() - t, 1), "stored": bool(wrote)})

    # One write at the end, and only when the read that produced `done` succeeded --
    # otherwise this would stamp a fresh marker over an existing one and lose every day
    # already recorded. Same rule as the channel index.
    if fresh_done and not dry:
        if ok:
            H.put_agg(reach_ns(brand), today_ist(),
                      {"days": sorted(done | fresh_done)})
        else:
            out["marker_write_skipped"] = "could not read the marker; not overwriting it"
    out["pending_after"] = max(0, len(todo) - len(fresh_done))
    out["took"] = round(time.time() - started, 1)
    return out


def budget_ns(brand):
    return f"{brand}budg"


# Three snapshots a day are taken and, until now, all three were written under the same
# date — so 09:00 was overwritten by 15:00 and 15:00 by 23:00, leaving one reading a day.
# That is enough to answer "what changed since yesterday" and useless for "why did the
# budget climb after six", which is the question actually asked of it. Each slot now also
# gets its own namespace, so the day keeps all three. The date-keyed copy stays exactly as
# it was: the dashboard's budget history reads it, and this must not move under it.
def budget_slot_ns(brand, hour):
    return f"{brand}budg{hour:02d}"


def budget_slots(brand, date):
    """{hour: snapshot} for whichever of the day's slots were recorded."""
    if not H.available():
        return {}
    out = {}
    for hour in BUDGET_SLOTS:
        try:
            got, ok = H.get_day_raw(budget_slot_ns(brand, hour), date)
        except Exception:
            continue
        if ok and isinstance(got, dict) and got.get("adsets"):
            out[hour] = got
    return out


# The hours the scheduler actually runs at. A snapshot taken at any other time rounds to
# the nearest of these rather than inventing a fourth slot nobody reads.
BUDGET_SLOTS = (9, 15, 23)
_open_cache, _open_lock = {}, threading.Lock()


def budget_open(brand, day):
    """What the day's budget was when it started, from the first slot recorded.

    The live budget is the budget of what is switched on RIGHT NOW, and dividing a whole
    day's spend by it is not a percentage of anything: Postly spent Rs1.93L on 1 Sept
    across 181 ad sets, then paused 166 of them, leaving Rs52,693 live and a tile that
    read 366% used. The money was spent against the budget that was in force while it
    was spending, and the 09:00 snapshot is the closest record of that.
    """
    key = (brand, day)
    with _open_lock:
        hit = _open_cache.get(key)
    if hit and time.time() - hit["at"] < 900:
        return hit["val"]
    val = None
    if H.available():
        for hour in BUDGET_SLOTS:
            try:
                got, ok = H.get_day_raw(budget_slot_ns(brand, hour), day)
            except Exception:
                continue
            if ok and isinstance(got, dict) and got.get("total"):
                val = {"total": round(float(got["total"]), 2),
                       "at": (got.get("at") or "")[11:16] or f"{hour:02d}:00",
                       "slot": hour}
                break
    with _open_lock:
        _open_cache[key] = {"at": time.time(), "val": val}
    return val


def _nearest_slot(hour):
    return min(BUDGET_SLOTS, key=lambda h: abs(h - hour))


def budget_diff(older, newer):
    """What moved between two snapshots of the same day: added, removed, changed.

    Separated deliberately. A total that rose because 51 ad sets were created is a
    different fact from one that rose because existing budgets were raised, and the
    first is invisible in a `moved` list — which only sees ad sets present in both.
    """
    a = (older or {}).get("adsets") or {}
    b = (newer or {}).get("adsets") or {}
    bud = lambda d, k: (d[k].get("b") or 0)
    added = [{"id": k, "n": b[k].get("n", ""), "b": bud(b, k)} for k in b if k not in a]
    gone = [{"id": k, "n": a[k].get("n", ""), "b": bud(a, k)} for k in a if k not in b]
    changed = [{"id": k, "n": b[k].get("n", ""), "from": bud(a, k), "to": bud(b, k)}
               for k in b if k in a and bud(a, k) != bud(b, k)]
    return {"added": sorted(added, key=lambda r: -r["b"]),
            "removed": sorted(gone, key=lambda r: -r["b"]),
            "changed": sorted(changed, key=lambda r: -(r["to"] - r["from"])),
            "added_total": round(sum(r["b"] for r in added), 2),
            "removed_total": round(sum(r["b"] for r in gone), 2),
            "changed_total": round(sum(r["to"] - r["from"] for r in changed), 2)}


def _rupees(v):
    """Meta returns money in the account's minor unit."""
    try:
        return round(int(v) / 100, 2)
    except (TypeError, ValueError):
        return 0.0


def _prior_budget_day(brand, day):
    """(snapshot, date) for the most recent recorded day BEFORE `day`, or (None, None)."""
    try:
        dates = [d for d in (H.have(budget_ns(brand)) or []) if d < day]
    except Exception:
        return None, None
    if not dates:
        return None, None
    d = max(dates)
    got, ok = H.get_day_raw(budget_ns(brand), d)
    return (got, d) if ok else (None, None)


def _adsets_by_id(ids):
    """{id: fields} for specific ad sets, whatever their status.

    Listing every ad set including paused ones costs 78 seconds of Meta request time
    across these accounts -- 24,717 rows against the 980 that are live -- and Meta's limit
    here binds on TIME. Fetching by id is how a paused ad set keeps its budget history
    without paying for twenty-four thousand dead ones every snapshot.
    """
    out = {}
    ids = [i for i in ids if i]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            j = _graph("", {"ids": ",".join(chunk),
                            "fields": "id,name,effective_status,daily_budget,"
                                      "lifetime_budget,campaign_id,account_id"},
                       rl_retries=0, raw=True)
        except Exception:
            continue
        for k, v in (j or {}).items():
            if isinstance(v, dict) and v.get("id"):
                out[k] = v
    return out


def budget_snapshot(brand, force=False, store=True):
    """Record every level's budget as it stands now, under today's date.

    Only ACTIVE ad sets count towards a campaign or an account, which is the same rule
    the dashboard's Budget/day tile uses: a paused ad set's number is not money that will
    be spent, and adding it would make the stored total disagree with the page.
    """
    B = C.brand(brand)
    day = today_ist()
    sets_, camps_, accts_, degraded, live_accounts = {}, {}, {}, [], []
    for a in B["accounts"]:
        camps, live_sets, _ads, ok = meta_roster(a["id"], force)
        if not (ok["campaigns"] and ok["adsets"]):
            # A partial snapshot would look like a budget cut that never happened. Record
            # the account as degraded and leave its rows out entirely.
            degraded.append(a["id"])
            continue
        acct_total = 0.0
        # EVERY campaign, with its status. The campaigns listing carries no status filter,
        # so recording the paused ones is free -- and a campaign that goes to zero because
        # it was paused is exactly what a budget history is for.
        for c in camps:
            st = c.get("effective_status") or ""
            b = _rupees(c.get("daily_budget"))
            camps_[c["id"]] = {"n": c.get("name", ""), "b": b,
                               "lt": _rupees(c.get("lifetime_budget")),
                               "st": st, "a": a["id"]}
            if is_live(st):
                acct_total += b
        for x in live_sets:
            b = _rupees(x.get("daily_budget"))
            sets_[x["id"]] = {"n": x.get("name", ""), "b": b,
                              "lt": _rupees(x.get("lifetime_budget")),
                              # The listing is filtered to the states that still deliver,
                              # so record which one it actually was rather than flattening
                              # IN_PROCESS and PENDING_REVIEW into ACTIVE.
                              "st": x.get("effective_status") or "ACTIVE",
                              "c": x.get("campaign_id", ""), "a": a["id"]}
            acct_total += b
        accts_[a["id"]] = {"n": a["name"], "b": round(acct_total, 2)}
        live_accounts.append(a["id"])

    # ---- ad sets that were here yesterday and are not today ------------------
    # Without this an ad set does not read as paused, it VANISHES -- and a row that
    # disappears is indistinguishable from one that never existed. Only the ones we were
    # already tracking are chased, by id, so the cost is tens of lookups and not a sweep.
    prev_snap, prev_date = _prior_budget_day(brand, day)
    lapsed = 0
    if prev_snap and live_accounts:
        gone = [k for k, v in (prev_snap.get("adsets") or {}).items()
                if k not in sets_ and v.get("a") in live_accounts]
        for k, v in _adsets_by_id(gone).items():
            sets_[k] = {"n": v.get("name", ""), "b": _rupees(v.get("daily_budget")),
                        "lt": _rupees(v.get("lifetime_budget")),
                        "st": v.get("effective_status") or "UNKNOWN",
                        "c": v.get("campaign_id", ""),
                        "a": "act_" + str(v.get("account_id") or "").replace("act_", "")}
            lapsed += 1

    snap = {"brand": brand, "date": day, "at": now_ist_str(),
            "lapsed": lapsed, "since_day": prev_date,
            "adsets": sets_, "campaigns": camps_, "accounts": accts_,
            # Still the ACTIVE-only figure, so it keeps matching the Budget/day tile:
            # a paused ad set's number is not money that will be spent.
            "total": round(sum(v["b"] for v in accts_.values()), 2),
            "degraded": degraded, "samples": 1}

    if not store or not H.available():
        return snap
    prev, ok = H.get_day_raw(budget_ns(brand), day)
    if not ok:
        # Refusing beats overwriting: a failed read that we treated as "nothing there"
        # would drop today's earlier samples and the record of what moved.
        snap["stored"] = False
        snap["store_error"] = "could not read today's snapshot; not overwriting it"
        return snap
    if prev:
        snap["first_at"] = prev.get("first_at") or prev.get("at")
        snap["samples"] = int(prev.get("samples") or 1) + 1
        # What actually moved TODAY, so a budget change made at noon is not invisible
        # just because the day only keeps one row.
        moved = list(prev.get("moved") or [])
        for lvl, cur in (("adsets", sets_), ("campaigns", camps_)):
            was = prev.get(lvl) or {}
            for k, v in cur.items():
                o = was.get(k)
                if o is None:
                    continue
                ob = round(float(o.get("b") or 0), 2)
                # Status counts as a change even when the number does not: an ad set
                # paused at its own budget still stopped spending, and a history that
                # only watched the rupees would call that day identical to the last.
                ost, nst = o.get("st") or "", v.get("st") or ""
                if ob != v["b"] or (ost and nst and ost != nst):
                    moved.append({"lvl": lvl[:-1], "id": k, "n": v.get("n", ""),
                                  "from": ob, "to": v["b"],
                                  "from_st": ost, "to_st": nst, "at": snap["at"]})
        snap["moved"] = moved[-500:]
    else:
        snap["first_at"] = snap["at"]
        snap["moved"] = []
    snap["stored"] = H.put_agg(budget_ns(brand), day, snap)
    # And under its own slot, so the day keeps every reading rather than only the last.
    slot = _nearest_slot(datetime.now(IST).hour)
    snap["slot"] = slot
    snap["slot_stored"] = H.put_agg(budget_slot_ns(brand, slot), day, snap)
    return snap


def budget_days(brand, dates):
    """{date: snapshot} for the dates that have one. Missing days are simply absent."""
    if not (H.available() and dates):
        return {}
    return H.fetch_raw(budget_ns(brand), list(dates))


# ---- video: hook rate and ThruPlay % ---------------------------------------
# Hook rate = 3-second video plays / impressions -- of the people this reached, how many
# stopped scrolling. ThruPlay % = ThruPlays / impressions -- how many actually watched it
# (to the end, or 15s for a longer video).
#
# The 3-second count has no field of its own any more: `video_3_sec_watched_actions` was
# removed and v21 rejects it. It survives only as the `video_view` entry inside `actions`,
# and `actions` unfiltered is 27 action types per row and 346 KB a page. The `filtering`
# parameter narrows it to the one type -- 112 KB, and verified NOT to drop rows: filtered
# and unfiltered both return 604 rows totalling 258,000.84 exactly. Getting that wrong
# would have quietly deleted spend, so it was checked before being relied on.
#
# Both fields are ABSENT when the count is zero, not zero -- 129 of 604 rows had plays and
# no ThruPlay key at all. So absence means nothing was watched, and `_vid` reads it as 0.
VIDEO_FILTER = json.dumps([{"field": "action_type", "operator": "IN",
                            "value": ["video_view"]}])
VIDEO_FIELDS = ",video_thruplay_watched_actions,actions"


def _action(r, key, action_type):
    """One action total off an insights row, or None when the row does not carry it."""
    v = r.get(key)
    if not isinstance(v, list):
        return None
    for a in v:
        if a.get("action_type") == action_type:
            return _num(a.get("value"))
    return None


def _vid(rows):
    """Flatten Meta's action lists into plain `vv` and `tp` numbers, in place.

    Done at the fetch boundary so nothing downstream -- the rollups, the stored day, the
    series -- ever has to know what shape Meta returned. A row that reports impressions
    reports video too (both keys are simply omitted when nothing was watched), so `vv` is
    set whenever the row is a measured one, and stays absent on rows from a day stored
    before this shipped. That absence is what `has_vid` reads.
    """
    for r in rows:
        if r.get("impressions") is None:
            continue
        r["vv"] = _action(r, "actions", "video_view") or 0.0
        r["tp"] = _action(r, "video_thruplay_watched_actions", "video_view") or 0.0
        r.pop("actions", None)
        r.pop("video_thruplay_watched_actions", None)
    return rows


def meta_insights(acct, since, until):
    """Ad-level spend for the window. Re-pulled on every refresh; this is the number."""
    return _vid(_graph(f"{acct}/insights", {
        "level": "ad", "time_range": json.dumps({"since": since, "until": until}),
        "filtering": VIDEO_FILTER,
        "fields": "ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,spend,"
                  "impressions,clicks" + VIDEO_FIELDS}))


# ---- impressions and clicks ------------------------------------------------
# Added to the insights call on 2026-08-26. Every day stored BEFORE that carries spend and
# nothing else, and the difference matters: a stored row has no `impressions` key at all,
# while a live row that genuinely got none has the key set to zero. Reading the first as
# the second would divide real spend by no impressions and print a CPM of infinity.
#
# So the rule everywhere below is: a row counts towards CTR and CPM only if it actually
# REPORTS impressions, and the spend that goes with it is accumulated separately as
# `imp_spend`. CPM is imp_spend / imp, never total spend / imp. The figure is then correct
# on day one over whatever fraction of the window is measured, and `imp_cov` says what
# fraction that is instead of leaving the reader to guess.
# The first day fetched with impressions and clicks. Days before it carry spend only, and
# nothing can be inferred about their CTR -- but they CAN be re-fetched, because insights
# are not retention-limited the way the activity log is. See tools/backfill_reach.py.
IMP_FROM = os.environ.get("IMP_FROM", "2026-08-26")


def has_imp(r):
    return r.get("impressions") is not None


# Video needs its OWN gate and its own denominator, and this is the trap in it: every day
# stored between 2026-08-26 and today carries impressions but NO video, so gating video on
# has_imp() would put those impressions under a zero numerator and report a hook rate
# diluted by exactly the unmeasured fraction. `vimp` is the impressions of rows that
# actually reported video, and every rate below divides by that, never by `imp`.
VID_FROM = os.environ.get("VID_FROM", "2026-08-27")

# The shape of the built payload. Bump it whenever a FIELD is added or removed, because
# the payload is persisted to GCS for cold starts and a restored one is served whole --
# so the shipped code reads new fields off an old payload and finds nothing. That is not
# staleness, it is a page rendering blanks for up to twelve hours after a deploy.
# Every other stored artifact already carries a stamp (PRORATA_MODEL, SERIES_SHAPE,
# row_cap); the payload was the one that did not, and adding video is what found it.
#   2 - hook rate and ThruPlay (vv / tp / vimp) at every level
PAYLOAD_SHAPE = 5


def has_vid(r):
    return r.get("vv") is not None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _as_of_str(ts):
    """'HH:MM:SS' in IST for an epoch, or None. None is not a failure -- it means no live
    pull happened, because every day asked for was settled history."""
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S") if ts else None


def _age_of(ts):
    return int(max(0, time.time() - ts)) if ts else None


def roster_age(acct, kind):
    """Seconds since this listing was last read from Meta, or None if never."""
    with _roster_lock:
        hit = _roster_cache.get((acct, kind))
    return (time.time() - hit["at"]) if hit else None


def _part(acct, kind, fetch, ttl, force=False):
    """One cached piece of the roster. Returns (data, ok).

    Each piece stands alone deliberately. Fetching all three as a unit meant one
    throttled listing threw away the other two — losing every budget because the *ads*
    listing failed, when the ad set listing had answered perfectly well.
    """
    key, now = (acct, kind), time.time()
    with _roster_lock:
        hit = _roster_cache.get(key)
        blocked_until = _roster_fail_until.get(key, 0)
    if hit and not force and now - hit["at"] < ttl:
        return hit["data"], True
    # `force` overrides the TTL but NEVER an active throttle window. Letting a Refresh
    # button hammer a throttled endpoint is precisely what keeps a throttle open.
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


# Meta's `effective_status` is not a two-state flag, and reading it as one cost this
# dashboard a whole page of wrong numbers. An ad set Meta reports as IN_PROCESS has its
# own status ACTIVE and is spending money: on 1 Sept, Postly had 125 of them carrying
# Rs2,53,529 of daily budget, and they spent Rs1,57,435 that day while the roster call —
# which asked only for effective_status ACTIVE — returned 35 ad sets worth Rs52,693. The
# live budget read as a seventh of itself, the LIVE tile counted a fifth of the ad sets,
# and "active only" hid the rest of the account as though it were paused.
#
# So the question is not "does Meta say ACTIVE" but "has Meta stopped it". These are the
# states where it has not: a new entity still being processed, one waiting on review, one
# approved ahead of its start, and one running with a placement complaint against it.
# Everything else — paused at any level, archived, disapproved, out of billing — is off.
LIVE_STATUSES = ["ACTIVE", "IN_PROCESS", "PENDING_REVIEW", "PREAPPROVED", "WITH_ISSUES"]
_LIVE_SET = set(LIVE_STATUSES)


def is_live(status):
    """True when Meta has not stopped this entity. Unknown statuses count as stopped:
    a new state Meta invents is more likely to be a way of NOT delivering."""
    return (status or "") in _LIVE_SET


def meta_roster(acct, force=False):
    """(campaigns, active ad sets, active ads, ok_flags) — each piece independently
    cached and independently allowed to fail.

    `force` is for an explicit Refresh only. The automatic 30-minute pull must NOT set it:
    the ads listing is the single most expensive call the dashboard makes and its 60-minute
    TTL exists to keep it off the hourly time budget.
    """
    camps, ok_c = _part(acct, "campaigns", lambda: _graph(
        f"{acct}/campaigns", {"fields": "id,name,effective_status,daily_budget,"
                                         "lifetime_budget"},
        rl_retries=0), ROSTER_TTL, force)
    sets, ok_s = _part(acct, "adsets", lambda: _graph(
        f"{acct}/adsets", {"fields": "id,name,effective_status,daily_budget,campaign_id,"
                                     "created_time,lifetime_budget",
                           "effective_status": json.dumps(LIVE_STATUSES)},
        rl_retries=0), ROSTER_TTL, force)
    ads_, ok_a = _part(acct, "ads", lambda: _graph(
        f"{acct}/ads", {"fields": "id,name,effective_status,adset_id,campaign_id",
                        "effective_status": json.dumps(LIVE_STATUSES)},
        rl_retries=0), ADS_ROSTER_TTL, force)
    return (camps or [], sets or [], ads_ or [],
            {"campaigns": ok_c, "adsets": ok_s, "ads": ok_a})


# -------------------------------------------------------------- Branch -----
class BranchThrottled(RuntimeError):
    """Branch is refusing on rate limit. Distinct from a generic failure because the
    caller's right response is to STOP, not to move on to the next day."""


def _branch(body, tries=5, url=None):
    for i in range(tries):
        req = urllib.request.Request(url or BRANCH_URL, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            b = e.read().decode()
            if e.code in (429, 500, 502, 503) and i < tries - 1:
                # 10/20/30/40s totalled 100s, which was not enough: a backfill hit a 429
                # that outlasted it and then lost eight consecutive days, each attempt
                # feeding the limiter that caused the last one. Back off properly, and
                # believe Retry-After when Branch sends it.
                wait = 15 * (2 ** i)                      # 15, 30, 60, 120
                try:
                    ra = int(e.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    ra = 0
                time.sleep(max(wait, min(ra, 300)))
                continue
            if e.code == 429:
                raise BranchThrottled(f"Branch 429 after {tries} attempts")
            raise RuntimeError(f"Branch {e.code}: {b[:300]}")
        except Exception as ex:
            if i < tries - 1:
                time.sleep(6 * (i + 1)); continue
            raise RuntimeError(f"Branch: {ex}")


# Branch caps a response at 1000 rows and reports the real size in
# `paging.total_count`, with `paging.next_url` carrying the offset. Taking the first page
# only is a SILENT under-count — no error, no flag, just fewer trials and therefore a CPT
# that reads too high, which is the direction that gets a working ad set killed. Measured
# 2026-08-24 on a 7-day window: Funda 816 rows (82% of the cap), Postly 664, SpeakEasy 273.
# Nothing was truncated yet; Funda was one busy week away.
def _branch_pages(body, cap=40, tries=5):
    """Every row for this query, following Branch's paging. Returns (rows, total_count).

    `cap` is a runaway guard, not a limit anyone should hit: 40 pages is 40,000 rows of
    distinct ad names in one window.
    """
    rows, total, url = [], None, BRANCH_URL
    for _ in range(cap):
        j = _branch(body, tries=tries, url=url)
        rows += j.get("results", [])
        pg = j.get("paging") or {}
        if total is None:
            total = pg.get("total_count")
        nxt = pg.get("next_url")
        if not nxt or len(rows) >= (total or 0):
            break
        # next_url is a path on the same host, e.g. /v1/query/analytics?limit=1000&after=1000
        url = urllib.parse.urljoin(BRANCH_URL, nxt)
    return rows, (total if total is not None else len(rows))


# ------------------------------------- where a nameless trial came from -----
# Branch fills in `~ad_name` for Facebook ads and, in practice, for nothing else: Google
# populates the channel and campaign fields but leaves the ad name empty. So the pool this
# dashboard used to lump together as "not matched to an ad" was never a pool of
# untraceable trials -- Branch knows exactly which partner earned every one of them.
# Cross-tabbed on 2026-08-20, not one nameless trial belonged to Facebook:
#
#              named (has ~ad_name)      nameless
#   postly       1,552  all Facebook          40  =     14 Google AdWords +  26 organic
#   funda       13,628  all Facebook      17,070  = 16,322 Google AdWords + 748 organic
#   speakeasy    1,180  all Facebook         747  =    689 Google AdWords +  58 organic
#
# Which is why these rows are tagged with the partner Branch names, and NOT divided
# between Meta and Google in proportion to their attributed trials. A proportional split
# would have credited Meta with about 7,700 of Funda's 17,070 nameless trials that day and
# printed a Meta CPT of Rs 133 where the measured figure was Rs 209 -- an error in the
# direction that keeps a losing ad set alive, against a Rs 150 target.
#
# The tag rides inside the ad-name key so the store, its aggregator and the payload cache
# need no format change and no migration: a key under this prefix is a nameless row, and
# whatever follows the prefix is Branch's partner name ("" = no partner at all, organic).
# Days stored before this existed carry a bare None/"null" key and are reported as
# "channel not recorded" rather than guessed at; tools/backfill_channels.py fills them in.
NONE_PREFIX = "~none~"

# Bumped whenever the pro-rata arithmetic changes. Stamped into every payload, and
# checked before a saved one is restored: a woken instance must not serve a payload
# built by the previous model under this model's badge. Model 1 put Google's own trials
# in the shared pool and printed a Funda uplift of 1.57 where model 2 prints 1.03 --
# restoring one of those silently would be a 57% error wearing a "Pro rata" chip.
PRORATA_MODEL = 2

# Installs ride alongside the two trial events as a third pseudo-event so that the store,
# its aggregator, the channel index and the ad-name join all take them with no format
# change. It is not a custom event -- Branch keeps installs in their own data source and
# they carry no event name -- so only the query differs, never the shape of the answer.
INSTALL_KEY = "inst"
INSTALL_SOURCE = "eo_install"

# Meta is "meta" and not "facebook" because the dashboard's own vocabulary is Meta, and
# Branch's is Facebook. Anything Branch names that is neither is kept as "other" rather
# than folded into organic: an unrecognised paid partner is a fact worth seeing, and
# calling it organic would quietly credit it to nobody.
CHANNELS = ("meta", "google", "organic", "other")
CHANNEL_LABELS = {"meta": "Meta", "google": "Google Ads",
                  "organic": "Organic / direct", "other": "Other partners"}


def _event_query(key, ev):
    """The data-source half of a Branch query for one event key."""
    if key == INSTALL_KEY:
        return {"data_source": INSTALL_SOURCE}
    return {"data_source": "eo_custom_event", "filters": {"name": [ev]}}


def _with_installs(events):
    """[(key, branch_event_name)] for every series worth pulling, installs last.

    Installs are pulled on the same trip as the trials because they are wanted at the
    same grain by the same views, and a separate pass over the same days would double
    the number of Branch requests for nothing.
    """
    return list(events.items()) + [(INSTALL_KEY, None)]


def _nameless_key(partner):
    return NONE_PREFIX + (partner or "")


def partner_slug(raw):
    p = (raw or "").strip().lower()
    if not p:
        return "organic"
    if "facebook" in p or "meta" in p or "instagram" in p:
        return "meta"
    if "google" in p or "adwords" in p or "youtube" in p or "doubleclick" in p:
        return "google"
    return "other"


def branch_trials_by_ad(since, until, B):
    """{event_key: {ad_name_or_None: unique_count}} over the window, 7-day chunked.

    A brand with no Branch app, or none of its events named yet, yields an empty map
    rather than raising — its Meta figures are unaffected and the page hides the
    columns instead of showing zeros that would read as "no trials".
    """
    events, creds = B["events"], B["branch"]
    if not (events and creds):
        return {}
    bkey, bsecret = creds
    out = {k: defaultdict(int) for k, _ in _with_installs(events)}
    d = datetime.strptime(since, "%Y-%m-%d").date()
    endd = datetime.strptime(until, "%Y-%m-%d").date()
    while d <= endd:
        ce = min(d + timedelta(days=BRANCH_MAX_SPAN - 1), endd)
        for key, ev in _with_installs(events):
            rows, _tc = _branch_pages({
                "branch_key": bkey, "branch_secret": bsecret,
                "start_date": d.strftime("%Y-%m-%d"), "end_date": ce.strftime("%Y-%m-%d"),
                "dimensions": ["last_attributed_touch_data_tilde_ad_name",
                               "last_attributed_touch_data_tilde_advertising_partner_name"],
                "granularity": "all", "aggregation": "unique_count",
                **_event_query(key, ev)})
            for row in rows:
                res = row.get("result", {})
                name = res.get("last_attributed_touch_data_tilde_ad_name")
                if not name:
                    name = _nameless_key(res.get(
                        "last_attributed_touch_data_tilde_advertising_partner_name"))
                out[key][name] += res.get("unique_count", 0)
        d = ce + timedelta(days=1)
    return out



# ------------------------------------------------------------ Classplus ----
# A Redash query over the Classplus product DB, keyed by ad name. It gives what Meta and
# Branch cannot: signups, and how many of those signups actually put a trial mandate in
# place, per ad.
#
# Two things about it shape the code below.
#
# 1. The window is BAKED INTO THE SQL — the queries take no parameters (the result key
#    is read-only; `parameters` in the POST body is ignored). So the window is read back
#    out of the SQL text, and figures are attached only when the query can genuinely
#    answer the window on screen. Labelling one day's numbers as another day's would be
#    worse than showing nothing. Two shapes exist and both are parsed:
#      - literal dates    -> covers exactly that window
#      - UTC_TIMESTAMP() +/- INTERVAL n DAY -> a window that rolls with today
#    Orthogonally, a query may GROUP BY the signup date and select it. One that does
#    becomes a per-day table and can answer any window inside its range; one that does
#    not is a single block and can only answer its own window. Several queries may be
#    configured; whichever can answer the requested window does.
# 2. It is a SIGNUP-COHORT measure, not an event measure: `trial_mandates` counts trials
#    taken by people who *signed up inside the window*. Branch trials count trial events
#    inside the window whenever the user signed up. The two agree closely but they are
#    not the same question, which is why they stay in separate columns.
CP_TTL = int(os.environ.get("CP_TTL", "600"))            # insist the result is this fresh
CP_POLL_BUDGET = int(os.environ.get("CP_POLL_BUDGET", "20"))
CP_KEYS = ("cp_signups", "cp_mandates", "cp_d0a", "cp_d0c")


def _cp_url(path, key):
    return (f"https://{C.CLASSPLUS_HOST}/api/{path}"
            f"{'&' if '?' in path else '?'}api_key={urllib.parse.quote(key)}")


def _cp_call(path, key, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        _cp_url(path, key), data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _cp_bound(seg, today):
    """One IST bound out of its slice of the `bounds` CTE, literal or rolling."""
    lit = re.search(r"'(\d{4}-\d{2}-\d{2})[ ']", seg)
    if lit:
        return datetime.strptime(lit.group(1), "%Y-%m-%d").date()
    if "UTC_TIMESTAMP" not in seg.upper():
        return None
    days = sum(int(f"{sign}1") * int(n)
               for sign, n in re.findall(r"([-+])\s*INTERVAL\s+(\d+)\s+DAY", seg, re.I))
    return today + timedelta(days=days)


def _cp_window(sql, today=None):
    """The query's own IST bounds -> inclusive (since, until) dates, or None.

    Handles both shapes the Classplus queries use: literal dates written into the SQL,
    and a rolling window expressed as UTC_TIMESTAMP() +/- INTERVAL n DAY. The bounds
    are read positionally from the `bounds` CTE — the text up to `AS start_utc` gives
    the start, the text between the two aliases gives the end.
    """
    sql = sql or ""
    a, b = sql.find("AS start_utc"), sql.find("AS end_utc")
    if a < 0 or b < a:
        return None
    today = today or datetime.strptime(today_ist(), "%Y-%m-%d").date()
    start, end = _cp_bound(sql[:a], today), _cp_bound(sql[a:b], today)
    if not start or not end:
        return None
    end -= timedelta(days=1)                       # the SQL end bound is exclusive
    if end < start:
        return None
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


# Any of these, if the query selects one, turns the result from a single block into a
# per-day table — which is what lets one query answer every window the page offers.
CP_DATE_COLS = ("signup_date", "signup_date_ist", "date", "day", "dt")


def _cp_blank():
    return {k: 0 for k in CP_KEYS}


def _cp_parse(qr, qid=""):
    cols = {c["name"] for c in qr["data"]["columns"]}
    need = {"ad_name", "signups", "trial_mandates"}
    if not need <= cols:
        raise RuntimeError(f"Classplus query {qid} is missing {sorted(need - cols)}")
    win = _cp_window(qr.get("query"))
    if not win:
        raise RuntimeError(f"Classplus query {qid} has no readable date bounds")
    datecol = next((c for c in CP_DATE_COLS if c in cols), None)

    by_day = {}
    for r in qr["data"]["rows"]:
        rec = {"cp_signups": int(r.get("signups") or 0),
               "cp_mandates": int(r.get("trial_mandates") or 0),
               "cp_d0a": int(r.get("d0_active") or 0),
               "cp_d0c": int(r.get("d0_cancelled") or 0)}
        day = str(r.get(datecol) or "")[:10] if datecol else ""
        name = r.get("ad_name") or "Organic / Unknown"
        slot = by_day.setdefault(day, {}).setdefault(name, _cp_blank())
        for k in CP_KEYS:
            slot[k] += rec[k]

    ts = qr.get("retrieved_at", "")
    try:
        at = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        age = int((datetime.now(timezone.utc) - at).total_seconds() // 60)
    except ValueError:
        at, age = None, None
    return {"qid": qid, "window": win, "daily": bool(datecol), "by_day": by_day,
            "retrieved_at": at.astimezone(IST).strftime("%H:%M IST") if at else "",
            "age_min": age}


def _cp_slice(src, since, until):
    """The source's rows for [since, until] folded into the shape build() consumes."""
    days = [d for d in src["by_day"]
            if not src["daily"] or since <= d <= until]
    by_ad, organic = {}, _cp_blank()
    for d in days:
        for name, rec in src["by_day"][d].items():
            tgt = organic if name == "Organic / Unknown" else \
                by_ad.setdefault(name, _cp_blank())
            for k in CP_KEYS:
                tgt[k] += rec[k]
    return {"window": [since, until], "by_ad": by_ad, "organic": organic,
            "retrieved_at": src["retrieved_at"], "age_min": src["age_min"],
            "totals": {k: sum(v[k] for v in by_ad.values()) + organic[k]
                       for k in CP_KEYS}}


def classplus_fetch(qid, key):
    """Latest result for one query, refreshed if the cached one is older than CP_TTL.

    Redash answers the POST either with a result (cache was fresh enough) or with a job.
    A job is polled, but only within a budget: the query takes ~15s and the dashboard is
    not going to sit on a cold page waiting for it. If the budget runs out the last
    result is served instead — still real data, just a few minutes old, and the job it
    kicked off means the next refresh gets the new figures. Its age is reported so the
    page can say how old it is rather than implying it is live.
    """
    # POST decides freshness; it answers with a result if the cache is young enough,
    # otherwise with a job. Its payload is trimmed and carries no SQL, and the SQL is
    # the only place the covered window is written down — so the numbers are always
    # read back from results.json, which returns the full record.
    j = _cp_call(f"queries/{qid}/results", key, {"max_age": CP_TTL}, timeout=60)
    job = (j.get("job") or {}).get("id") if "query_result" not in j else None
    deadline = time.time() + CP_POLL_BUDGET
    while job and time.time() < deadline:
        time.sleep(2)
        # 1 pending, 2 started, 3 success, 4 failure, 5 cancelled
        st = (_cp_call(f"queries/{qid}/jobs/{job}", key).get("job") or {}).get("status")
        if st in (3, 4, 5):
            break
    qr = _cp_call(f"queries/{qid}/results.json", key, timeout=60)["query_result"]
    return _cp_parse(qr, qid)


def _cp_covers(src, since, until):
    """Can this source answer exactly this window?

    A per-day source can, for any window inside its range. A whole-block source can
    only answer its own window: labelling one day's figures as another day's would be
    worse than showing nothing.
    """
    lo, hi = src["window"]
    if src["daily"]:
        return lo <= since and until <= hi
    return (lo, hi) == (since, until)


def classplus(since, until):
    """(data, note) — data is None whenever it cannot be trusted for THIS window."""
    if not C.CLASSPLUS_ON:
        return None, None
    seen, dead = [], 0
    for qid, key in C.CLASSPLUS_QUERIES:
        src, ok = _part("classplus", f"q{qid}", lambda q=qid, k=key: classplus_fetch(q, k),
                        CP_TTL)
        if not ok or not src:
            dead += 1
            continue
        if _cp_covers(src, since, until):
            return _cp_slice(src, since, until), None
        seen.append(src)
    if not seen:
        return None, "Classplus is not responding — signup and mandate columns are hidden."
    # Say which query holds what, rather than merging every window into one phrase:
    # the answer to "why is today blank" is different for each of them.
    bits = []
    for s in seen:
        lo, hi = s["window"]
        span = lo if lo == hi else f"{lo} → {hi}"
        bits.append(f"query {s['qid']} covers {span}"
                    + ("" if s["daily"] else " as a single total, not day by day"))
    fix = ("" if any(s["daily"] for s in seen) else
           " Selecting a signup-date column in the 30-day query would let it answer"
           " any day in its range.")
    return None, ("Classplus has no figures for this window — "
                  + "; ".join(bits) + f".{fix}")



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
                                  "listings": [], "until": 0, "since": t["since"],
                                  "regain_min": 0})
        e["listings"].append(LISTING_LABEL.get(kind, kind))
        e["until"] = max(e["until"], t["until"])
        e["since"] = min(e["since"], t["since"])
        e["regain_min"] = max(e["regain_min"], t.get("regain_min", 0))
    out = []
    for e in per.values():
        e["listings"].sort()
        e["eta_sec"] = int(e["until"] - now)
        # 'meta' = Meta's own estimated_time_to_regain_access. 'recheck' = our own retry
        # interval, because the listing edges give no estimate at all. The page must not
        # present the second as though it were the first.
        e["eta_source"] = "meta" if e.get("regain_min") else "recheck"
        e["until_ist"] = datetime.fromtimestamp(e["until"], IST).strftime("%H:%M")
        e["since_ist"] = datetime.fromtimestamp(e["since"], IST).strftime("%H:%M")
        e["held_min"] = int((now - e["since"]) // 60)
        out.append(e)
    out.sort(key=lambda e: -e["eta_sec"])
    # Budget headroom, reported whether or not anything is throttled: total_time is the
    # limit that actually binds here, so seeing it climb is the warning that matters.
    budget = []
    for a, u in usage.items():
        if a not in accounts:
            continue
        rows = sorted(({"quota": t, **v} for t, v in (u.get("types") or {}).items()),
                      key=lambda r: -r["time_pct"])
        top = rows[0] if rows else {}
        budget.append({"account": accounts[a], "id": a, "tier": u.get("tier", ""),
                       "quota": top.get("quota", ""), "time_pct": top.get("time_pct", 0),
                       "calls_pct": top.get("calls_pct", 0),
                       "cpu_pct": top.get("cpu_pct", 0),
                       "acct_pct": u.get("acct_pct", 0), "quotas": rows})
    budget.sort(key=lambda b: -b["time_pct"])
    return {
        "active": bool(out),
        "accounts": out,
        # absolute deadline for the page to schedule its own retry against
        "retry_in_sec": max((e["eta_sec"] for e in out), default=0),
        "retry_at_ist": max(out, key=lambda e: e["eta_sec"])["until_ist"] if out else "",
        # 'meta' only when Meta actually supplied an estimate for one of them
        "retry_source": ("meta" if any(e["eta_source"] == "meta" for e in out)
                         else "recheck"),
        "recheck_min": ROSTER_RETRY // 60,
        "budget": budget,
        "worst_time_pct": max((b["time_pct"] for b in budget), default=0),
    }


# ------------------------------------------------------- window assembly ----
# Everything below exists so that a closed day is asked for once, ever, instead of on
# every view. See history.py for why a day is only stored once it has settled.
_have_cache, _have_lock = {}, threading.Lock()
HAVE_TTL = int(os.environ.get("HAVE_TTL", "300"))


def _have(brand):
    """Dates already in the store, cached briefly — one listing per brand, not per view."""
    with _have_lock:
        hit = _have_cache.get(brand)
        if hit and time.time() - hit["at"] < HAVE_TTL:
            return hit["dates"]
    dates = set(H.have(brand))
    with _have_lock:
        _have_cache[brand] = {"at": time.time(), "dates": dates}
    return dates


def meta_insights_daily(acct, since, until):
    """Ad-level spend, ONE ROW PER AD PER DAY.

    build() accumulates spend per ad (`+=`) and takes names off whichever row it sees, so
    per-day rows drop straight into it with no change. Verified against the aggregate
    call it replaces: 933 aggregate rows and 1697 per-day rows over the same 3 days both
    total 828,048.30 exactly.
    """
    return _vid(_graph(f"{acct}/insights", {
        "level": "ad", "time_increment": 1,
        "time_range": json.dumps({"since": since, "until": until}),
        "filtering": VIDEO_FILTER,
        "fields": "ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,spend,"
                  "impressions,clicks" + VIDEO_FIELDS}))


# Retry budget for the LIVE page vs the backfill. The long backoff that lets a backfill
# ride out a throttle is exactly wrong in front of a person: a throttled Branch made every
# SpeakEasy page load hang for 105 seconds and then fail, when failing in 15 and rendering
# the Meta half would have been far more useful. The backfill has nobody waiting on it and
# keeps the patient budget.
BRANCH_LIVE_TRIES = int(os.environ.get("BRANCH_LIVE_TRIES", "2"))
BRANCH_BACKFILL_TRIES = int(os.environ.get("BRANCH_BACKFILL_TRIES", "5"))


def branch_trials_daily(since, until, B, tries=BRANCH_LIVE_TRIES):
    """{date: {event_key: {ad_name: unique_count}}}, 7-day chunked and fully paged.

    Branch reports the day in `timestamp` as IST (+05:30), which is the same day boundary
    Meta uses for these accounts, so no shifting is needed.

    One honest caveat: summing days does not exactly reproduce a single multi-day query.
    Measured across all three brands over a fully settled week (2026-08-15 → 21), the
    brand total came out 0.00% to 0.37% LOW by day-sum.

    Where that difference sits is the part that matters, and it is not spread evenly.
    Broken down for Funda, the worst case:

        trials matched to an ad name   84,750 window  vs  84,772 day-sum   +0.026%
        trials with no ad name         99,768 window  vs  99,301 day-sum     -467

    The whole discrepancy is in the unattributed bucket. Every CPT on the page is spend
    over MATCHED trials, so no ad, ad set, campaign or account figure moves; only the
    brand-level "not matched to an ad" count does, which the page already shows on its
    own. Meta spend, by contrast, reproduces exactly — 0.00 on every brand.
    """
    events, creds = B["events"], B["branch"]
    out = {}
    if not (events and creds):
        return out
    bkey, bsecret = creds
    d = datetime.strptime(since, "%Y-%m-%d").date()
    endd = datetime.strptime(until, "%Y-%m-%d").date()
    while d <= endd:
        ce = min(d + timedelta(days=BRANCH_MAX_SPAN - 1), endd)
        for key, ev in _with_installs(events):
            rows, _tc = _branch_pages({
                "branch_key": bkey, "branch_secret": bsecret,
                "start_date": d.strftime("%Y-%m-%d"), "end_date": ce.strftime("%Y-%m-%d"),
                "dimensions": ["last_attributed_touch_data_tilde_ad_name",
                               "last_attributed_touch_data_tilde_advertising_partner_name"],
                "granularity": "day", "aggregation": "unique_count",
                **_event_query(key, ev)},
                tries=tries)
            for row in rows:
                day = (row.get("timestamp") or "")[:10]
                res = row.get("result", {})
                name = res.get("last_attributed_touch_data_tilde_ad_name")
                if not name:
                    # Nameless, but not unknown: keep the partner Branch names for it.
                    name = _nameless_key(res.get(
                        "last_attributed_touch_data_tilde_advertising_partner_name"))
                if not day:
                    continue
                out.setdefault(day, {}).setdefault(key, {})
                out[day][key][name] = out[day][key].get(name, 0) + \
                    res.get("unique_count", 0)
        d = ce + timedelta(days=1)
    return out


# ---- Google: the trials half -----------------------------------------------
# Branch fills in no ad NAME for a Google trial, which is why they show up on this
# dashboard as a bucket with no cost beside them. What it DOES fill in is the Google
# campaign and the ad group -- and those are the same names Google Ads reports spend
# against, so a real Google CPT is joinable at the level buying decisions are made at.
# Measured on 2026-08-20: Funda 16,328 Google trials across 11 campaigns and 23 ad
# groups; SpeakEasy 689 across 5 and 9; Postly 14, which is noise.
#
# A separate query rather than another dimension on the main one: adding campaign and ad
# group there would multiply every Meta row by two dimensions it already implies, for no
# gain, and push far more days into Branch's 1000-row paging.
GOOGLE_PARTNERS = ("google adwords", "google ads", "googleadwords", "adwords")


def is_google(partner):
    return (partner or "").strip().lower() in GOOGLE_PARTNERS


def google_trials_daily(since, until, B, tries=BRANCH_LIVE_TRIES):
    """{date: {event_key: {(campaign, ad_group): unique_count}}} for Google only.

    Names, not ids, because names are all Branch carries -- the same join this dashboard
    already makes for Meta at ad-name level, one rung up. Rows Branch gives a partner of
    Google but no campaign are kept under a campaign of "" so they are counted and
    visible rather than silently dropped.
    """
    events, creds = B["events"], B["branch"]
    out = {}
    if not (events and creds):
        return out
    bkey, bsecret = creds
    d = datetime.strptime(since, "%Y-%m-%d").date()
    endd = datetime.strptime(until, "%Y-%m-%d").date()
    while d <= endd:
        ce = min(d + timedelta(days=BRANCH_MAX_SPAN - 1), endd)
        for key, ev in _with_installs(events):
            rows, _tc = _branch_pages({
                "branch_key": bkey, "branch_secret": bsecret,
                "start_date": d.strftime("%Y-%m-%d"), "end_date": ce.strftime("%Y-%m-%d"),
                "dimensions": [
                    "last_attributed_touch_data_tilde_advertising_partner_name",
                    "last_attributed_touch_data_tilde_campaign",
                    "last_attributed_touch_data_tilde_ad_set_name"],
                "granularity": "day", "aggregation": "unique_count",
                **_event_query(key, ev)},
                tries=tries)
            for row in rows:
                day = (row.get("timestamp") or "")[:10]
                res = row.get("result", {})
                if not day or not is_google(res.get(
                        "last_attributed_touch_data_tilde_advertising_partner_name")):
                    continue
                k = (res.get("last_attributed_touch_data_tilde_campaign") or "",
                     res.get("last_attributed_touch_data_tilde_ad_set_name") or "")
                bucket = out.setdefault(day, {}).setdefault(key, {})
                bucket[k] = bucket.get(k, 0) + res.get("unique_count", 0)
        d = ce + timedelta(days=1)
    return out


GOOGLE_DIMS = {"gcampaign": "Campaign", "gadgroup": "Ad group"}
# Two caches, because the two halves cost completely different things. Google Ads spend is
# cheap and not rate-limited here; Branch is the source that throttles, and the trials for
# a window are IDENTICAL for both dimensions -- Campaign is the Ad group fold summed one
# level up. Caching the trials by window alone means opening Campaign after Ad group costs
# no Branch call at all, where before it cost a second full pull and usually got a 429.
_gtrials_cache, _gseries_cache, _gwin_cache = {}, {}, {}
_gcache_lock = threading.Lock()
GSERIES_TTL = int(os.environ.get("GSERIES_TTL", "900"))
# A FAILED pull is cached too, briefly. Not caching it at all sounded principled -- do not
# let a transient look permanent -- but it meant that while Branch was throttling, every
# single interaction paid the full twenty-second failure again. Sixty seconds is short
# enough that a recovery is picked up almost at once and long enough that clicking around
# a throttled window is instant instead of unusable.
GERR_TTL = int(os.environ.get("GERR_TTL", "60"))


def _gcache_get(store, key, ttl):
    with _gcache_lock:
        hit = store.get(key)
    if hit and time.time() - hit["at"] < (GERR_TTL if hit.get("err") else ttl):
        return hit["v"], int((time.time() - hit["at"]) // 60)
    return None, None


def _gcache_put(store, key, v, keep=8, err=False):
    with _gcache_lock:
        store[key] = {"at": time.time(), "v": v, "err": err}
        if len(store) > keep:
            for k in sorted(store, key=lambda k: store[k]["at"])[:len(store) - keep]:
                store.pop(k, None)


def google_trials_window(brand, dates, force=False):
    """({date: {event: {(campaign, ad_group): n}}}, error) for a window, cached.

    Stored days come from the store and never expire; the rest are one Branch pull shared
    by every dimension and every view that asks within the TTL.
    """
    key = (brand, dates[0], dates[-1])
    if not force:
        hit, _age = _gcache_get(_gtrials_cache, key, GSERIES_TTL)
        if hit is not None:
            return hit
    stored = google_trials_read(brand, dates) if H.available() else {}
    missing = [d for d in dates if d not in stored]
    err = None
    if missing:
        try:
            live = google_trials_daily(missing[0], missing[-1], C.brand(brand))
            stored.update({d: v for d, v in live.items() if d in set(missing)})
        except Exception as ex:
            err = str(ex)[:160]
    out = (stored, err)
    # A throttled pull is not cached: caching it would hold an empty answer for fifteen
    # minutes and make a transient look permanent.
    _gcache_put(_gtrials_cache, key, out, err=bool(err))
    return out


def google_series(brand, since, until, dim="gadgroup", force=False):
    """See _google_series_build. Wrapped so a repeat view is free."""
    key = (brand, since, until, dim)
    if not force:
        hit, age = _gcache_get(_gseries_cache, key, GSERIES_TTL)
        if hit is not None:
            return dict(hit, cached=True, age_min=age)
    out = _google_series_build(brand, since, until, dim=dim, force=force)
    _gcache_put(_gseries_cache, key, out, err=bool(out.get("trials_error")))
    return dict(out, cached=False, age_min=0)


def _google_series_build(brand, since, until, dim="gadgroup", force=False):
    """Per-day Google spend, trials and installs by campaign or ad group.

    Deliberately the SAME SHAPE as series(): `{dates, rows:[{key,label,days:{date:{...}}}]}`.
    Trends and Matrix read that shape and nothing else, so producing it here gets the
    chart, the date grid, paging, per-day sorting, the hover read-out and the CSV export
    for Google without a second implementation of any of them.
    """
    dim = dim if dim in GOOGLE_DIMS else "gadgroup"
    B = C.brand(brand)
    dates, partial_today = _series_dates(since, until)
    if not dates:
        return {"brand": brand, "since": since, "until": until, "dim": dim,
                "dates": [], "rows": [], "generated_at": now_ist_str()}
    ev_keys = [k for k, _ in _with_installs(B["events"])]

    rows = {}

    def cell(camp, group):
        k = camp if dim == "gcampaign" else camp + "\t" + group
        r = rows.get(k)
        if r is None:
            r = rows[k] = {
                "key": k,
                "label": camp if dim == "gcampaign" else (group or "(no ad group)"),
                "stage": "" if dim == "gcampaign" else camp,
                "platform": "Google", "acct": None, "ad": None,
                "total_spend": 0.0, "days": {},
                **{"total_" + x: 0.0 for x in ev_keys}}
        return r

    def day_of(r, d):
        rec = r["days"].get(d)
        if rec is None:
            rec = r["days"][d] = {"spend": 0.0, **{x: 0.0 for x in ev_keys}}
        return rec

    # ---- trials, from the store where a day has one and Branch for the rest ----
    per_day, trials_err = google_trials_window(brand, dates, force=force)
    stored = per_day
    trial_days = 0
    for d in dates:
        per_ev = per_day.get(d)
        if not per_ev:
            continue
        trial_days += 1
        for k, by_key in per_ev.items():
            if k not in ev_keys:
                continue
            for (camp, group), n in by_key.items():
                r = cell(camp, group)
                day_of(r, d)[k] += n
                r["total_" + k] += n

    # ---- spend, straight from Google Ads, already per day ---------------------
    spend_days, spend_err = set(), None
    conv_days, conv_err = set(), None
    cust, how = ([], "no credentials")
    if GA.available():
        cust, how = google_customers(brand)
        for cid in cust:
            for x in GA.spend_daily(cid, dates[0], dates[-1]):
                d = x["date"]
                if d not in r_dates_set(dates):
                    continue
                r = cell(x["campaign"], x["ad_group"])
                rec = day_of(r, d)
                rec["spend"] += x["spend"]
                rec["imp"] = rec.get("imp", 0) + x["imp"]
                rec["clk"] = rec.get("clk", 0) + x["clk"]
                rec["isp"] = round(rec.get("isp", 0) + x["spend"], 2)
                r["total_spend"] += x["spend"]
                r["total_imp"] = (r.get("total_imp") or 0) + x["imp"]
                r["total_clk"] = (r.get("total_clk") or 0) + x["clk"]
                r["total_isp"] = round((r.get("total_isp") or 0) + x["spend"], 2)
                spend_days.add(d)
        spend_err = GA.last_error() or (None if cust else
                                        "no Google Ads account is mapped to this brand")
        # Google's own count of the same event, per day, so the trend and the grid can
        # show both attributions rather than only the Branch one the Overview compares.
        ev_name = B["events"].get(ev_keys[0]) if ev_keys else None
        if ev_name:
            keep = r_dates_set(dates)
            for cid in cust:
                for x in GA.conv_daily(cid, dates[0], dates[-1], ev_name):
                    if x["date"] not in keep:
                        continue
                    r = cell(x["campaign"], x["ad_group"])
                    rec = day_of(r, x["date"])
                    rec["gconv"] = round(rec.get("gconv", 0) + x["conv"], 2)
                    r["total_gconv"] = round((r.get("total_gconv") or 0) + x["conv"], 2)
                    conv_days.add(x["date"])
            conv_err = GA.last_error()
        else:
            conv_err = "this brand has no trial event configured"
    else:
        spend_err = "no Google Ads credentials on this instance"
        conv_err = spend_err

    out_rows = sorted(rows.values(), key=lambda r: -r["total_spend"])
    for r in out_rows:
        r["total_spend"] = round(r["total_spend"], 2)
        for x in ev_keys:
            r["total_" + x] = round(r["total_" + x], 2)

    return {"brand": brand, "brand_label": B["label"], "since": dates[0],
            "until": dates[-1], "dim": dim, "dim_labels": GOOGLE_DIMS,
            "dates": dates, "keys": ev_keys, "install_key": INSTALL_KEY,
            "event_labels": B["labels"], "cpt_target": B["cpt_target"],
            "rows": out_rows, "truncated": False, "row_cap": 0,
            "shape": SERIES_SHAPE, "channel": "google",
            "partial_today": partial_today, "excluded_today": today_ist(),
            "total_rows": len(out_rows), "stored_days": len(stored),
            "trial_days": trial_days, "trials_error": trials_err,
            "spend_days": len(spend_days), "spend_error": spend_err,
            "conv_days": len(conv_days), "conv_error": conv_err,
            "conv_event": (B["events"].get(ev_keys[0]) if ev_keys else None),
            "customers": cust, "customers_how": how,
            # Google has no budget history here and no live/paused flag on an ad group,
            # so the controls that depend on those are told plainly rather than left to
            # render an empty grid.
            "budget_dim": False, "budget_days": 0,
            "active_dim": False, "active_known": False,
            "imp_days": len(spend_days), "imp_from": IMP_FROM,
            "generated_at": now_ist_str()}


def r_dates_set(dates, _cache={}):
    k = (dates[0], dates[-1], len(dates))
    if k not in _cache:
        _cache.clear()
        _cache[k] = set(dates)
    return _cache[k]


def google_backfill(brand, budget_s=90, max_days=0, dry=False):
    """Store Google's per-day campaign/ad-group trials for settled days that lack them.

    Same shape and the same reasons as the reach backfill: bounded batches so it can be
    scheduled and left, newest first so a short run lands the days people look at, and a
    throttle stops it rather than feeding it. Without this every Trends or Matrix view on
    the Google channel re-pulls Branch for the whole window, which is both slow and the
    thing that gets the app rate-limited.
    """
    started = time.time()
    B = C.brand(brand)
    have = sorted(H.have(brand) or [], reverse=True)          # days the store has at all
    done = set(H.have(google_ns(brand)) or [])
    todo = [d for d in have if d not in done]
    out = {"brand": brand, "stored": len(have), "pending_before": len(todo),
           "written": 0, "failed": 0, "days": [], "throttled": False}
    if not todo:
        out["pending_after"] = 0
        out["took"] = round(time.time() - started, 1)
        return out
    for d in (todo[:max_days] if max_days else todo):
        if time.time() - started > budget_s:
            break
        t = time.time()
        try:
            g = google_trials_daily(d, d, B, tries=BRANCH_BACKFILL_TRIES)
        except BranchThrottled:
            out["throttled"] = True
            break
        except Exception as ex:
            out["failed"] += 1
            out["days"].append({"date": d, "error": str(ex)[:120]})
            continue
        per_ev = g.get(d) or {}
        # A day with no Google rows is still a stored answer -- "Google earned nothing
        # here" is a fact, and without writing it the day stays pending for ever.
        ok = True if dry else google_trials_store(brand, d, per_ev)
        out["written"] += 1 if ok else 0
        out["failed"] += 0 if ok else 1
        n = sum(sum(v.values()) for v in per_ev.values())
        out["days"].append({"date": d, "trials": n, "rows": sum(len(v) for v in per_ev.values()),
                            "took": round(time.time() - t, 1), "stored": bool(ok)})
    out["pending_after"] = max(0, len(todo) - out["written"])
    out["took"] = round(time.time() - started, 1)
    return out


def google_spend_only(brand, since, until):
    """{spend, imp, clk, days} for a window -- Google Ads ONLY, no Branch call.

    Exists so the Meta view can show what Google costs beside its own CPT without paying
    for a Branch query it does not need: the Meta payload already carries the measured
    Google trial count for this window in `channels`, so the only missing half is spend,
    and Google Ads is not the rate-limited source here.
    """
    out = {"spend": 0.0, "imp": 0, "clk": 0, "days": 0, "ok": False, "error": None}
    if not GA.available():
        out["error"] = "no Google Ads credentials on this instance"
        return out
    cust, how = google_customers(brand)
    if not cust:
        out["error"] = "no Google Ads account is mapped to this brand"
        return out
    seen = set()
    for cid in cust:
        for r in GA.spend_daily(cid, since, until):
            out["spend"] += r["spend"]; out["imp"] += r["imp"]; out["clk"] += r["clk"]
            seen.add(r["date"])
    out["spend"] = round(out["spend"], 2)
    out["days"] = len(seen)
    out["ok"] = bool(seen)
    out["error"] = None if seen else (GA.last_error() or "Google returned no rows")
    out["customers"] = cust
    return out


def prior_window(brand, since, until):
    """{spend, trials, days, days_covered, complete} for the window before this one.

    Read from the STORE only. A comparison is worth having and is not worth doubling the
    Meta and Branch cost of every page load to get, and the days it needs are settled by
    definition -- they are older than the ones on screen. Where the store is short, the
    figure is marked incomplete rather than divided by the days that happened to be there.
    """
    B = C.brand(brand)
    n = len(date_range(since, until))
    end = datetime.strptime(since, "%Y-%m-%d").date() - timedelta(days=1)
    start = end - timedelta(days=n - 1)
    dates = date_range(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    out = {"since": dates[0], "until": dates[-1], "days": n,
           "days_covered": 0, "complete": False, "spend": 0.0,
           "trials": {k: 0.0 for k in B["events"]}}
    if not H.available():
        out["note"] = "no history store configured"
        return out
    meta, _branch, got, _missing = H.fetch(brand, dates)
    out["days_covered"] = len(got)
    out["complete"] = len(got) == n
    out["spend"] = round(sum(float(r.get("spend") or 0)
                             for rows in meta.values() for r in rows), 2)
    # Trials come from the CHANNEL INDEX, not from summing every stored ad name. The
    # stored names include Google's and organic's, so summing them compares this window's
    # META trials against the prior window's ALL-CHANNEL trials -- which on Funda made the
    # prior week look like 168,836 trials against 82,528, and a CPT half the real one.
    idx, ok = chan_index_read(brand)
    if ok:
        for ev in B["events"]:
            out["trials"][ev] = round(sum(
                ((idx.get(d) or {}).get(ev) or {}).get("meta", 0) for d in dates), 1)
            out["google_trials"] = round(sum(
                ((idx.get(d) or {}).get(ev) or {}).get("google", 0) for d in dates), 1)
        out["trials_from"] = "channel index"
    else:
        out["trials"] = None
        out["note"] = "channel index unreadable — no prior trial count"
    out["generated_at"] = now_ist_str()
    return out


def google_ns(brand):
    return f"{brand}gtri"


def google_trials_store(brand, day, per_ev):
    """Persist one day's Google trials. Tuples are not JSON keys, so they are joined
    with a tab -- neither a campaign nor an ad group name may contain one."""
    flat = {ev: {"\t".join(k): v for k, v in rows.items()}
            for ev, rows in (per_ev or {}).items()}
    return H.put_agg(google_ns(brand), day, {"date": day, "events": flat})


def google_trials_read(brand, dates):
    """{date: {event: {(campaign, ad_group): n}}} for whichever of `dates` are stored."""
    out = {}
    for day, art in (H.fetch_raw(google_ns(brand), list(dates)) or {}).items():
        out[day] = {ev: {tuple(k.split("\t", 1)): v for k, v in rows.items()}
                    for ev, rows in ((art or {}).get("events") or {}).items()}
    return out


GOOGLE_CUSTOMERS = {}          # {brand: [customer_id]}, discovered and cached per run
_gcust_lock = threading.Lock()


def google_customers(brand):
    """(ids, how) — which Google Ads accounts belong to THIS brand.

    Never "all of them". The manager account here holds eighteen accounts across brands
    that have nothing to do with each other -- Testbook, PrepShots, UPSC, The Legal School
    -- and summing them into one brand's CPT would not look wrong, it would just be wrong.
    So: an explicit list if one is configured, otherwise accounts whose Google name
    matches the brand's label, and otherwise NOTHING, said out loud.
    """
    env = os.environ.get("GOOGLE_CUSTOMERS_" + brand.upper(), "").strip()
    if env:
        return [x.strip().replace("-", "") for x in env.split(",") if x.strip()], "configured"
    with _gcust_lock:
        hit = GOOGLE_CUSTOMERS.get(brand)
    if hit is not None:
        return hit[0], hit[1]
    label = (C.brand(brand)["label"] or brand).strip().lower()
    # "Funda" must take "Funda" and "Funda 2" and not "Fundamentals of X"; a word-boundary
    # prefix does that, where a bare `in` would take anything containing the name.
    pat = re.compile(r"^" + re.escape(label) + r"(\b|$)", re.I)
    ids = [a["id"] for a in GA.all_customers() if pat.match((a.get("name") or "").strip())]
    how = "matched by name" if ids else "no account matched this brand"
    with _gcust_lock:
        GOOGLE_CUSTOMERS[brand] = (ids, how)
    return ids, how


def google_window(brand, since, until, force=False):
    """See _google_window_build. Cached, because switching channel should not re-pull."""
    key = (brand, since, until)
    if not force:
        hit, age = _gcache_get(_gwin_cache, key, GSERIES_TTL)
        if hit is not None:
            return dict(hit, cached=True, age_min=age)
    out = _google_window_build(brand, since, until, force=force)
    _gcache_put(_gwin_cache, key, out, err=bool(out.get("trials_error")))
    return dict(out, cached=False, age_min=0)


def _google_window_build(brand, since, until, force=False):
    """Google campaigns and ad groups for a window: trials, installs, spend, CPT.

    Trials come from the store for days that have one and from Branch for the rest --
    the same split the Meta side makes, for the same reason. Spend comes from Google Ads
    if the credential works; when it does not, every row still carries its trials and
    installs and simply has no cost beside it, which is strictly better than the nothing
    the page shows today.
    """
    B = C.brand(brand)
    dates = date_range(since, until)
    ev_keys = [k for k, _ in _with_installs(B["events"])]
    # The same cached, window-keyed pull the series uses. Opening the Google tab and then
    # Trends now costs ONE Branch pull between them instead of one each.
    per_day, err = google_trials_window(brand, dates, force=force)
    if err:
        err = f"Branch: {err}"
    stored = per_day
    live = {}

    rows = {}

    def cell(camp, group):
        return rows.setdefault((camp, group), {
            "campaign": camp or "(no campaign)", "ad_group": group or "(no ad group)",
            "spend": 0.0, "imp": 0.0, "clk": 0.0, "gconv": 0.0,
            # None, not 0: an ad group the asset pull never answered for has an UNKNOWN
            # creative count, and unknown must never render as a confident zero.
            "cre": None, "cre_off": 0, "best": 0, "good": 0, "low": 0,
            **{k: 0.0 for k in ev_keys}})

    trial_days = 0
    for d in dates:
        per_ev = stored.get(d) or live.get(d)
        if not per_ev:
            continue
        trial_days += 1
        for k, by_key in per_ev.items():
            if k not in ev_keys:
                continue
            for (camp, group), n in by_key.items():
                cell(camp, group)[k] += n

    # ---- spend ---------------------------------------------------------------
    spend_days, spend_err = 0, None
    conv_days, conv_err = set(), None
    asset_groups, asset_err = set(), None
    cust, how = ([], "no credentials")
    if GA.available():
        cust, how = google_customers(brand)
        seen_days = set()
        for cid in cust:
            for r in GA.spend_daily(cid, since, until):
                c = cell(r["campaign"], r["ad_group"])
                c["spend"] += r["spend"]; c["imp"] += r["imp"]; c["clk"] += r["clk"]
                c["customer_id"] = r["customer_id"]
                c["campaign_id"] = r["campaign_id"]
                c["ad_group_id"] = r["ad_group_id"]
                seen_days.add(r["date"])
        spend_days = len(seen_days)
        spend_err = GA.last_error() or (
            None if cust else "no Google Ads account is mapped to this brand")
        # ---- and what Google itself says the same event earned -----------------
        # A second, cheap query (tens of rows, ~2s) against the same customers. It is
        # kept separate from spend so a conversion-side failure can never take the cost
        # column down with it -- one is what we paid, which is not in doubt; the other is
        # one attribution model's opinion of what it bought.
        ev_name = B["events"].get(ev_keys[0]) if ev_keys else None
        if ev_name:
            for cid in cust:
                for r in GA.conv_daily(cid, since, until, ev_name):
                    cell(r["campaign"], r["ad_group"])["gconv"] += r["conv"]
                    conv_days.add(r["date"])
            conv_err = GA.last_error()
        else:
            conv_err = "this brand has no trial event configured"
        # ---- how many creatives each ad group is actually running -------------
        # Current state, not windowed: assets carry no date here. Cheap (1-4s), one call
        # per customer, so it rides along rather than becoming a tab of its own.
        for cid in cust:
            for (camp, group), a in GA.assets_by_group(cid).items():
                c = rows.get((camp, group))
                if c is None:
                    # An ad group holding creatives but with no spend and no trials in
                    # this window is not a row on this page; adding one would put a line
                    # with no cost and no result into a cost-per-trial table.
                    continue
                c["cre"] = (c["cre"] or 0) + a["cre"]
                for k in ("cre_off", "best", "good", "low"):
                    c[k] += a[k]
                asset_groups.add((camp, group))
        asset_err = GA.last_error()
    else:
        spend_err = GA.last_error() or "no Google Ads credentials on this instance"
        conv_err = spend_err
        asset_err = spend_err

    out_rows = sorted(rows.values(),
                      key=lambda r: (-r["spend"], -r.get(ev_keys[0], 0)))
    camps = {}
    for r in out_rows:
        c = camps.setdefault(r["campaign"], {
            "campaign": r["campaign"], "ad_groups": 0, "spend": 0.0,
            "imp": 0.0, "clk": 0.0, "gconv": 0.0, "cre": None, "cre_off": 0,
            "best": 0, "good": 0, "low": 0, **{k: 0.0 for k in ev_keys}})
        c["ad_groups"] += 1
        for k in ("spend", "imp", "clk", "gconv", *ev_keys):
            c[k] += r[k]
        for k in ("cre_off", "best", "good", "low"):
            c[k] += r[k]
        # A campaign's creative count is the sum of the ad groups that ANSWERED. If none
        # did it stays None: summing unknowns into 0 would claim a campaign runs no
        # creatives, which is the one thing it certainly does.
        if r["cre"] is not None:
            c["cre"] = (c["cre"] or 0) + r["cre"]
    tot = {k: round(sum(r[k] for r in out_rows), 2)
           for k in ("spend", "imp", "clk", "gconv", *ev_keys)}
    _cre = [r["cre"] for r in out_rows if r["cre"] is not None]
    tot["cre"] = sum(_cre) if _cre else None
    for k in ("cre_off", "best", "good", "low"):
        tot[k] = sum(r[k] for r in out_rows)
    return {
        "brand": brand, "since": since, "until": until,
        "events": list(B["events"]), "event_labels": B["labels"],
        "install_key": INSTALL_KEY,
        "campaigns": sorted(camps.values(), key=lambda c: -c["spend"] or 0),
        "ad_groups": out_rows,
        "totals": tot,
        "trial_days": trial_days, "days": len(dates),
        "spend_days": spend_days,
        "customers": cust, "customers_how": how,
        "spend_ok": bool(spend_days),
        "spend_error": None if spend_days else spend_err,
        # Google-reported conversions are a SECOND reading of the same window, so they
        # get their own ok/error pair. Zero conversion days means the number is unknown,
        # not zero -- the same rule the Branch side has always followed.
        "conv_days": len(conv_days),
        "conv_event": (B["events"].get(ev_keys[0]) if ev_keys else None),
        "conv_ok": bool(conv_days),
        "conv_error": None if conv_days else conv_err,
        "asset_groups": len(asset_groups),
        "assets_ok": bool(asset_groups),
        "asset_error": None if asset_groups else asset_err,
        "trials_error": err,
        "stored_days": len(stored),
        "generated_at": now_ist_str(),
    }


def branch_partners_daily(since, until, B, tries=BRANCH_BACKFILL_TRIES):
    """{date: {event_key: {branch_partner_name_or_None: unique_count}}}.

    The cheap query: one dimension, a handful of rows a day rather than one per ad name.
    It exists for repairing days that were stored before trials carried their partner —
    re-running the full ad-name pull for a hundred days is the thing that took SpeakEasy
    off the air once already, and this asks for a hundredth of the data.
    """
    events, creds = B["events"], B["branch"]
    out = {}
    if not (events and creds):
        return out
    bkey, bsecret = creds
    d = datetime.strptime(since, "%Y-%m-%d").date()
    endd = datetime.strptime(until, "%Y-%m-%d").date()
    while d <= endd:
        ce = min(d + timedelta(days=BRANCH_MAX_SPAN - 1), endd)
        for key, ev in events.items():
            rows, _tc = _branch_pages({
                "branch_key": bkey, "branch_secret": bsecret,
                "start_date": d.strftime("%Y-%m-%d"), "end_date": ce.strftime("%Y-%m-%d"),
                "dimensions": [
                    "last_attributed_touch_data_tilde_advertising_partner_name"],
                "granularity": "day", "aggregation": "unique_count",
                "data_source": "eo_custom_event", "filters": {"name": [ev]}},
                tries=tries)
            for row in rows:
                day = (row.get("timestamp") or "")[:10]
                if not day:
                    continue
                res = row.get("result", {})
                par = res.get(
                    "last_attributed_touch_data_tilde_advertising_partner_name")
                out.setdefault(day, {}).setdefault(key, {})
                out[day][key][par] = out[day][key].get(par, 0) + \
                    res.get("unique_count", 0)
        d = ce + timedelta(days=1)
    return out


def legacy_nameless(by_name):
    """Trials stored under a bare null key — nameless, and from before partners were kept."""
    return sum(n for k, n in (by_name or {}).items()
               if not isinstance(k, str) or not k or k == "null")


def named_total(by_name):
    """Trials stored against a real ad name (so, per Branch's behaviour, Meta's)."""
    return sum(n for k, n in (by_name or {}).items()
               if isinstance(k, str) and k and k != "null"
               and not k.startswith(NONE_PREFIX))


def apportion(total, weights):
    """Split integer `total` across {key: weight} so the parts sum to it exactly.

    Largest remainder. The weights come from a SECOND Branch query whose unique_count
    dedupes very slightly differently from the first (0.2% on the days measured), so the
    shape of the split is measured and only the rounding is imposed. Normalising to the
    stored total rather than replacing it keeps every day's total exactly what it was
    when it was written, which is the one property a repair pass must not break.
    """
    total = int(round(total))
    tot_w = sum(weights.values())
    if total <= 0 or tot_w <= 0:
        return {}
    raw = {k: total * w / tot_w for k, w in weights.items() if w > 0}
    out = {k: int(v) for k, v in raw.items()}
    left = total - sum(out.values())
    for k, _ in sorted(raw.items(), key=lambda kv: -(kv[1] - int(kv[1]))):
        if left <= 0:
            break
        out[k] += 1
        left -= 1
    return {k: v for k, v in out.items() if v}


def _stored_prefix(brand, dates):
    """The longest run of `dates`, from the start, that the store actually holds.

    A prefix and not a subset: the live half is fetched as ONE range because Meta and
    Branch both cost more per call than per day, so a gap in the middle is cheaper to
    re-fetch than to work around. Backfill closes the gaps; this just refuses to
    pretend one is not there.
    """
    if not (H.available() and dates):
        return []
    got = _have(brand)
    out = []
    for d in dates:
        if d not in got:
            break
        out.append(d)
    return out


# ------------------------------------------- per-day channel totals --------
# The pro-rata model has to be applied DAY BY DAY and then summed, because the Meta /
# Google mix moves: doing it once on a window aggregate silently uses one blended ratio
# for a month, and on a month where Google's share doubled that is a different number.
#
# What a day needs for it is four integers per event, and none of them depend on the
# current ad roster -- "named" means Facebook regardless of whether that ad still exists.
# So the per-day totals can be computed once, cached in the store, and reused forever,
# which is what CHAN_NS holds. Reading raw stored days instead would be tens of thousands
# of rows to recover twenty numbers.
CHAN_NS = "chan"
_chan_cache, _chan_lock = {}, threading.Lock()
CHAN_TTL = int(os.environ.get("CHAN_TTL", "900"))


def date_range(since, until):
    """Every date from since to until inclusive, as YYYY-MM-DD strings."""
    out = []
    d = datetime.strptime(since, "%Y-%m-%d").date()
    endd = datetime.strptime(until, "%Y-%m-%d").date()
    while d <= endd:
        out.append(d.strftime("%Y-%m-%d")); d += timedelta(days=1)
    return out


def chan_of_day(by_name):
    """{channel: n} for one day's {ad_name: count} map.

    `meta` is every trial with a real ad name plus any nameless row Branch attributed to
    Facebook (rare, but it happens and it is Meta's). `unknown` is a day stored before
    partners were recorded -- reported, never apportioned.
    """
    out = {c: 0 for c in CHANNELS}
    out["unknown"] = 0
    for k, n in (by_name or {}).items():
        nm = k if isinstance(k, str) else ""
        if nm.startswith(NONE_PREFIX):
            out[partner_slug(nm[len(NONE_PREFIX):])] += n
        elif not nm or nm == "null":
            out["unknown"] += n
        else:
            out["meta"] += n
    return out


def prorata_day(ch):
    """(meta_allocation, google_allocation, pool) for one day under the pro-rata model.

        pool         = organic + other + unrecorded      (nobody claims these)
        share_meta   = meta   / (meta + google)          (of the ATTRIBUTED volume)
        share_google = google / (meta + google)
        meta   += pool * share_meta
        google += pool * share_google

    Google's own trials are NOT in the pool. Branch names Google as the partner on them,
    so they are attributed, not unattributed -- and putting them in the pool made the
    split circular: Google's count set the ratio that then decided how much of Google's
    own count Google kept. On Funda that handed Meta 7,839 of the 16,106 trials Branch
    said Google earned, on one day. The pool is what nobody can claim; the ratio is what
    the two claimants measurably earned.

    On a day with no attributed volume at all there is no ratio to apply, so BOTH
    allocations are zero and the pool stays unclaimed. Returning it to either side would
    be asserting that a channel earned trials on a day it measurably earned none, which
    is the same mistake as the circular split, just quieter.
    """
    meta, google = ch.get("meta", 0), ch.get("google", 0)
    pool = ch.get("organic", 0) + ch.get("other", 0) + ch.get("unknown", 0)
    denom = meta + google
    if not pool or not denom:
        return 0.0, 0.0, pool
    return pool * (meta / denom), pool * (google / denom), pool


def chan_index_read(brand):
    """({date: {event: {channel: n}}}, ok). `ok` is False if the store could not be read.

    Every writer below goes through this rather than chan_index(), because a writer that
    cannot tell "empty" from "unreachable" will overwrite the whole index with whatever
    single entry it was adding.
    """
    got, ok = H.get_agg_raw(H.agg_ns(brand, CHAN_NS, 0))
    if not ok:
        return {}, False
    return {k: v for k, v in (got or {}).items() if not k.startswith("_")}, True


def chan_index(brand, force=False):
    """{date: {event: {channel: n}}} for every stored day of this brand. {} if absent.

    The reader's view: a failed read degrades to whatever was last cached, or to nothing,
    and is NOT cached as an empty index -- caching a failure would take the pro-rata view
    down to zero covered days for the whole TTL.
    """
    with _chan_lock:
        hit = _chan_cache.get(brand)
        if hit and not force and time.time() - hit[0] < CHAN_TTL:
            return hit[1]
    idx, ok = chan_index_read(brand)
    if not ok:
        with _chan_lock:
            stale = _chan_cache.get(brand)
        return stale[1] if stale else {}
    with _chan_lock:
        _chan_cache[brand] = (time.time(), idx)
    return idx


def chan_index_put(brand, idx):
    ok = H.put_agg(H.agg_ns(brand, CHAN_NS, 0), today_ist(), idx)
    if ok:
        with _chan_lock:
            _chan_cache[brand] = (time.time(), idx)
    return ok


def chan_index_add(brand, date, by_event):
    """Fold one freshly stored day into the index. Returns True only if it landed.

    Called from snapshot() so the nightly job keeps the index current without anyone
    having to remember to rebuild it. Refuses to write when the read failed: a truncated
    index would then be the NEWEST artifact and would win every subsequent read.
    """
    try:
        idx, ok = chan_index_read(brand)
        if not ok:
            return False
        idx[date] = {ev: chan_of_day(bn) for ev, bn in (by_event or {}).items()}
        return chan_index_put(brand, idx)
    except Exception:
        return False


def chan_index_build(brand, dates=None, log=None, rebuild=False):
    """(built, total) — bring the channel index in line with the stored days.

    One read and ONE write, not one of each per day. The per-day version did 286
    read-modify-write round trips during the install backfill and lost an update: any
    single failed read or write in that chain vanishes silently, because nothing checks.

    `rebuild` recomputes every stored day rather than only the ones missing, which is how
    an entry that went stale -- present, but written before the day gained a series --
    gets corrected. It never writes an empty index first: the new one is assembled in
    memory and swapped in, so a failure part-way leaves the old one intact.
    """
    idx, ok = chan_index_read(brand)
    if not ok:
        raise RuntimeError("could not read the existing channel index — refusing to "
                           "rebuild over it")
    stored = sorted(dates or H.have(brand))
    todo = stored if rebuild else [d for d in stored if d not in idx]
    if not todo:
        return 0, len(stored)
    for i in range(0, len(todo), 15):
        part = todo[i:i + 15]
        raw = H.fetch_raw(brand, part)
        if len(raw) < len(part) and log:
            log(f"    {part[0]}..{part[-1]}  WARNING: read {len(raw)} of {len(part)} day(s)")
        for d, day in raw.items():
            idx[d] = {ev: chan_of_day(bn)
                      for ev, bn in (day.get("branch") or {}).items()}
        if log:
            log(f"    {part[0]}..{part[-1]}  {len(raw)} day(s)")
    if not chan_index_put(brand, idx):
        raise RuntimeError(f"channel index write failed: {H.last_error()}")
    # Read it back. An index that silently lost days is the failure this whole change is
    # about, and the only way to know it did not happen is to look.
    back, ok = chan_index_read(brand)
    if not ok:
        raise RuntimeError("wrote the channel index but could not read it back")
    lost = [d for d in idx if d not in back]
    if lost:
        raise RuntimeError(f"channel index came back missing {len(lost)} day(s): "
                           f"{lost[:3]}")
    return len(todo), len(stored)


# The live half of a window, kept so ONE source can be refreshed without re-pulling the
# other. A person watching spend wants Meta re-read; a person watching trials wants
# Branch. Making either wait for both spends quota nobody asked to spend, and on a long
# window it is the difference between ten seconds and two minutes.
#
# Only ever a fallback for a targeted refresh: a normal build ignores it and fetches
# both, so nothing here can make the page show a stale figure it did not ask for.
_live_cache, _live_lock = {}, threading.Lock()
LIVE_TTL = int(os.environ.get("LIVE_TTL", "3600"))


def _live_get(key, part):
    with _live_lock:
        hit = _live_cache.get(key)
    if not hit or time.time() - hit["at"] > LIVE_TTL:
        return None
    return hit.get(part)


def _live_put(key, part, value):
    with _live_lock:
        cur = _live_cache.get(key) or {}
        cur[part] = value
        cur["at"] = time.time()
        _live_cache[key] = cur
        if len(_live_cache) > 24:
            for k in sorted(_live_cache, key=lambda k: _live_cache[k]["at"])[:8]:
                _live_cache.pop(k, None)


def window_data(since, until, B, today=None, only=None):
    """(insights_by_account, {event: {ad_name: n}}, {date: {event: {channel: n}}}, prov).

    Settled days come from the store, the rest from Meta and Branch live.

    The third element is the per-day channel split, kept separate from the aggregated
    trials because the pro-rata model needs each day on its own and the aggregate has
    already thrown the days away. Stored days come from the index; live days are folded
    in as they arrive, which costs nothing -- branch_trials_daily returns per-day data
    and the aggregation was discarding it.
    """
    brand, events = B["key"], B["events"]
    today = today or today_ist()
    meta = defaultdict(list)
    trials = {k: defaultdict(int) for k, _ in _with_installs(events)}
    chan_days = {}
    prov = {"stored_days": 0, "live_since": since, "live_until": until,
            "store": "off" if not H.available() else "on", "note": ""}

    dates, live_since, live_until = (H.split(since, until, today)
                                     if H.available() else ([], since, until))
    use = _stored_prefix(brand, dates)
    if len(use) < len(dates):
        # Everything from the first gap onwards has to come live, so the window stays
        # one contiguous range.
        live_since = dates[len(use)] if use or dates else since
        live_until = until
        if H.available() and dates:
            prov["note"] = (f"{len(dates) - len(use)} settled day(s) not in the store yet"
                            " — fetched live; run the backfill to stop paying for them.")

    if use:
        m, b, got, missing = H.fetch(brand, use)
        for acct, rows in m.items():
            meta[acct] += rows
        for ev, by_name in b.items():
            if ev not in trials:
                continue
            for name, n in by_name.items():
                trials[ev][name] += n
        prov["stored_days"] = len(got)
        idx = chan_index(brand)
        for d in got:
            if d in idx:
                chan_days[d] = idx[d]

    prov["live_since"], prov["live_until"] = live_since, live_until
    # When the numbers were actually PULLED, as opposed to when this dict was assembled.
    # Those are different things and the page was only ever shown the second one, so
    # opening it always read as "just refreshed" no matter how old the figures were.
    # None means no pull happened: every day in the window was settled history.
    prov["meta_at"] = prov["branch_at"] = None
    if live_since:
        lk = (brand, live_since, live_until)
        # `only` names the ONE source this build is refreshing. The other is reused from
        # the last live pull when there is one, and fetched normally when there is not —
        # a targeted refresh must never be able to produce a page with a hole in it.
        reuse = _live_get(lk, "meta") if only and only != "meta" else None
        if reuse is not None:
            for acct, rows in reuse.items():
                meta[acct] += rows
            prov["meta_at"] = _live_cache.get(lk, {}).get("meta_at")
            prov["meta_reused"] = True
        else:
            fresh = {}
            for a in B["accounts"]:
                fresh[a["id"]] = meta_insights_daily(a["id"], live_since, live_until)
                meta[a["id"]] += fresh[a["id"]]
            prov["meta_at"] = time.time()
            _live_put(lk, "meta", fresh)
            with _live_lock:
                _live_cache[lk]["meta_at"] = prov["meta_at"]
        # Branch failing must not take the page down with it. Spend, budgets, statuses and
        # the whole testing/trial split come from Meta and are perfectly good without it —
        # a Branch throttle used to 500 the entire dashboard, which is how a backfill
        # competing for Branch quota made SpeakEasy unreachable rather than merely
        # trial-less. Reported as an explicit failure, never as zero: a zero here would
        # read as "no trials happened", which is a different and much worse claim.
        try:
            reuse_b = _live_get(lk, "branch") if only and only != "trials" else None
            if reuse_b is not None:
                daily = reuse_b
                prov["branch_at"] = _live_cache.get(lk, {}).get("branch_at")
                prov["trials_reused"] = True
            else:
                daily = branch_trials_daily(live_since, live_until, B)
                prov["branch_at"] = time.time()
                _live_put(lk, "branch", daily)
                with _live_lock:
                    _live_cache[lk]["branch_at"] = prov["branch_at"]
            for day, per_ev in daily.items():
                for ev, by_name in per_ev.items():
                    for name, n in by_name.items():
                        trials[ev][name] += n
                chan_days[day] = {ev: chan_of_day(bn) for ev, bn in per_ev.items()}
        except Exception as ex:
            kind = ("Branch is rate-limiting this app"
                    if isinstance(ex, BranchThrottled) else str(ex)[:160])
            prov["trials_error"] = kind
            trials = {k: defaultdict(int) for k, _ in _with_installs(events)}
            chan_days = {}
    return meta, trials, chan_days, prov


def snapshot(brand, date):
    """Fetch one settled day from source and write it to the store. Returns a status dict.

    Deliberately NOT called from a page view. A view that discovers a missing day and
    stops to fetch it turns one slow page into a rate-limit incident on a 30-day window;
    the backfill and the nightly job own writing, and views only ever read.
    """
    B = C.brand(brand)
    if date > H.settled_through(today_ist()):
        return {"ok": False, "date": date, "reason": "not settled yet"}
    meta = {a["id"]: meta_insights_daily(a["id"], date, date) for a in B["accounts"]}
    daily = branch_trials_daily(date, date, B, tries=BRANCH_BACKFILL_TRIES)
    branch = {ev: dict(by_name) for ev, by_name in (daily.get(date) or {}).items()}

    # A day where BOTH sources return nothing is refused, never stored. "No spend that
    # day" and "that day is past the source's retention" look identical from here, and
    # the second one written down becomes a permanent zero that nothing ever rechecks —
    # the exact failure the settle window exists to prevent, arriving from the other end.
    # Meta and Branch both answer for 2026-05-26 and neither answers for 2026-02-24, so
    # the boundary is real and somewhere in between.
    spend = sum(float(r.get("spend") or 0) for v in meta.values() for r in v)
    trials = sum(sum(v.values()) for v in branch.values())
    if not spend and not trials:
        return {"ok": False, "date": date, "brand": brand,
                "reason": "both sources returned nothing — refusing to store a zero "
                          "that may just be past retention"}

    ok = H.put(brand, date, meta, branch)
    chan_ok = None
    if ok:
        # Fold the day into the channel index in the same breath. Doing it here rather
        # than in a separate nightly job means the index cannot silently fall behind the
        # store, which would make the pro-rata view quietly skip days. Reported, not
        # assumed: this is the step that failed once already.
        chan_ok = chan_index_add(brand, date, branch)
    # ...and Google's campaigns and ad groups, in the same breath and for the same
    # reason. It is a second Branch query, but a cheap one -- tens of rows a day against
    # thousands -- and a day stored without it can never gain it later without a second
    # pass over the whole history, which is the position the reach fields left us in.
    goog_ok = None
    if ok:
        try:
            g = google_trials_daily(date, date, B, tries=BRANCH_BACKFILL_TRIES)
            per_ev = g.get(date) or {}
            # Nothing to store is not a failure: a brand may simply not buy on Google.
            goog_ok = google_trials_store(brand, date, per_ev) if per_ev else True
        except Exception:
            goog_ok = False
    with _have_lock:
        _have_cache.pop(brand, None)
    return {"ok": ok, "date": date, "brand": brand, "google": goog_ok,
            "ads": sum(len(v) for v in meta.values()),
            "spend": round(sum(float(r.get("spend") or 0)
                               for v in meta.values() for r in v), 2),
            "trials": {ev: sum(v.values()) for ev, v in branch.items()},
            "chan_index": chan_ok,
            "error": H.last_error() if not (ok and chan_ok is not False) else
                     H.last_error()}


# ---------------------------------------------------------- longevity ------
# "Which creatives keep spending, and for how long" is a different question from every
# other one this app answers, and the only one that needs history rather than a window.
# It is also the question the store was worth building for.
LONGEVITY_TTL = int(os.environ.get("LONGEVITY_TTL", "1800"))
DAILY_SERIES_TOP = int(os.environ.get("DAILY_SERIES_TOP", "400"))
_long_cache, _long_lock = {}, threading.Lock()


def now_ist_str():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _adset_day(meta_rows, branch_day, events):
    """One day folded to {adset_id: {name, campaign, account, spend, t101, t10m}}.

    The ad-name join is the same one build() does and for the same reason: Branch knows
    an ad NAME and nothing else, so a name shared by several ad sets has its trials split
    by that day's spend share. Splitting by the window's share instead would put trials
    on a day the ad set did not spend.
    """
    sets, by_name = {}, defaultdict(list)
    for acct, rows in (meta_rows or {}).items():
        for r in rows:
            sid = r.get("adset_id")
            if not sid:
                continue
            e = sets.get(sid)
            if e is None:
                e = sets[sid] = {"name": r.get("adset_name", ""),
                                 "campaign": r.get("campaign_name", ""),
                                 "campaign_id": r.get("campaign_id", ""),
                                 "account": acct, "spend": 0.0,
                                 **{k: 0.0 for k in events}}
            e["spend"] += float(r.get("spend") or 0)
            if r.get("ad_name"):
                by_name[r["ad_name"]].append((sid, float(r.get("spend") or 0)))
    for ev in events:
        for name, n in ((branch_day or {}).get(ev) or {}).items():
            group = by_name.get(name)
            if not group:
                continue
            tot = sum(sp for _, sp in group)
            for sid, sp in group:
                if sid in sets:
                    sets[sid][ev] += n * (sp / tot) if tot else n / len(group)
    return sets


# ----------------------------------------------------------- daily series ----
# Two views want the same thing and neither existed before: one number per row per DAY.
# The window tables answer "what did this cost over the period" and deliberately collapse
# the days; Longevity answers "how long did this ad set keep going" and only tracks
# spend. Neither can draw a trend line or fill a date grid.
#
# The fold is the one _adset_day already does, generalised to any grouping. Grouping by
# ad NAME rather than ad id is deliberate for the Script dimension: the same creative is
# rebuilt into new ads across ad sets and builds, and the name is what Branch attributes
# to, so a name is the closest thing in this data to a script.
SERIES_TTL = int(os.environ.get("SERIES_TTL", "900"))
SERIES_MAX_DAYS = int(os.environ.get("SERIES_MAX_DAYS", "62"))
# The window Trends and Matrix open on. ONE definition, because the nightly warm and the
# endpoint's default have to agree exactly: a stored fold is reused only on an exact date
# match, so a warm one day wider than the request is a fold nobody will ever ask for.
# They drifted -- the tabs moved to 30 days and the warm stayed at 15 -- and the symptom
# was invisible: every cold open still paid the full 15-25s, and the nightly job still
# reported success.
SERIES_DEFAULT_DAYS = int(os.environ.get("SERIES_DEFAULT_DAYS", "30"))


def series_window(days=None, today=None):
    """(since, until) for a days-long window ending today, clamped to the allowed range."""
    d = max(2, min(int(days or SERIES_DEFAULT_DAYS), SERIES_MAX_DAYS))
    t = today or today_ist()
    return ((datetime.strptime(t, "%Y-%m-%d") - timedelta(days=d - 1))
            .strftime("%Y-%m-%d"), t)
# 0 means "every row". The Matrix paginates, so there is no longer a screen-sized reason
# to truncate, and the truncation was hiding real money: on Postly's 30-day script fold
# the top 60 rows are only 40% of the spend, and rank 61 had still spent Rs24,000.
# The ceiling is a safety net against a dimension nobody has tried yet, not a view limit.
SERIES_TOP = int(os.environ.get("SERIES_TOP", "0"))
SERIES_MAX_ROWS = int(os.environ.get("SERIES_MAX_ROWS", "20000"))
# Bumped when the SHAPE of a folded row changes. Checked alongside dates and row_cap
# before a stored fold is reused, for the same reason: a fold from before rows carried
# their account gives the Matrix a grid with no links and no way to tell why.
SERIES_SHAPE = 6
# The levels a budget belongs to. A script is an ad name and a stage is a bucket; neither
# is a thing Meta holds a budget against.
BUDGET_DIMS = {"adset": "adsets", "campaign": "campaigns", "account": "accounts"}
# Folds are now 1.5-2 MB each, so an unbounded dict of them is an OOM on a 512 MB
# instance. Keep the few most recently used and let the store serve the rest.
SERIES_CACHE_MAX = int(os.environ.get("SERIES_CACHE_MAX", "8"))
_series_cache, _series_lock = {}, threading.Lock()


def _series_cache_put(key, data):
    """Insert under the lock and evict the least recently touched. Caller holds nothing."""
    with _series_lock:
        _series_cache[key] = {"at": time.time(), "data": data}
        if len(_series_cache) > SERIES_CACHE_MAX:
            for k in sorted(_series_cache, key=lambda k: _series_cache[k]["at"]
                            )[:len(_series_cache) - SERIES_CACHE_MAX]:
                _series_cache.pop(k, None)

def _series_ns(brand, dim):
    """Store namespace for a folded series. Alphanumeric, which the service requires."""
    return f"{brand}ser{dim}"


def _series_dates(since, until, today=None):
    """(dates, partial_today) for a series window.

    One function so the fold and the stored-artifact check can never disagree about which
    days a window means -- if they did, a restore would answer a slightly different
    question than the one asked, and silently.
    """
    today = today or today_ist()
    dates = date_range(since, min(until, today))[-SERIES_MAX_DAYS:]
    if len(dates) > 1 and dates[-1] == today:
        # Drop today and take one more day at the far end, so "last 14 days" is fourteen
        # whole days rather than thirteen and a fragment. Today is a partial day: spend
        # is minutes behind and Branch trials land all evening, and on a trend line that
        # draws as a collapse that never happened.
        back = (datetime.strptime(dates[0], "%Y-%m-%d").date()
                - timedelta(days=1)).strftime("%Y-%m-%d")
        return [back] + dates[:-1], False
    return dates, bool(dates and dates[-1] == today)


DIMS = ("script", "adset", "campaign", "account", "stage", "platform")
DIM_LABELS = {"script": "Script (ad name)", "adset": "Ad set", "campaign": "Campaign",
              "account": "Ad account", "stage": "Stage", "platform": "Platform"}


def _dim_day(meta_rows, branch_day, keys, dim, testing_re, acct_names):
    """One day folded to {row_key: {label, stage, platform, spend, <keys...>}}.

    Branch attributes to an ad NAME and nothing else, so a name carried by several ads
    has its trials and installs split by that day's spend share -- the same rule build()
    and _adset_day use, applied on the day rather than on the window, so nothing lands on
    a day the ad did not spend.
    """
    rows, by_name = {}, defaultdict(list)
    blank = {k: 0.0 for k in keys}

    for acct, mrows in (meta_rows or {}).items():
        for r in mrows:
            camp = r.get("campaign_name") or ""
            stage = "testing" if testing_re.search(camp) else "trial"
            sp = float(r.get("spend") or 0)
            if dim == "script":
                # Keyed by name AND stage, not by name alone. The same creative runs in a
                # testing campaign and, once it graduates, in a trial one — 126 names on
                # Funda in a single week. Keyed by name only, those two lives merge into
                # one row whose spend, trials and CPT are a blend of an install-optimised
                # campaign and a trial-optimised one, and whose Stage column shows
                # whichever happened to be seen first. Two rows now, one per stage, which
                # is what the Stage column and the "include testing" filter both assume.
                lbl = r.get("ad_name") or ""
                k = lbl + "\x1f" + stage
            elif dim == "adset":
                k, lbl = r.get("adset_id"), r.get("adset_name") or ""
            elif dim == "campaign":
                k, lbl = r.get("campaign_id"), camp
            elif dim == "account":
                k, lbl = acct, acct_names.get(acct, acct)
            elif dim == "stage":
                k, lbl = stage, stage
            else:
                k, lbl = "meta", "Meta"
            if not k:
                continue
            e = rows.get(k)
            if e is None:
                # `acct` rides along so the Matrix can link a row to Ads Manager, which
                # resolves an object id only inside an act=. Kept as the account that
                # spent MOST on the row, because a script name can appear in both
                # accounts and a link into the wrong one is worse than none.
                e = rows[k] = {"label": lbl, "stage": stage, "platform": "Meta",
                               "spend": 0.0, "acct": acct, "acct_spend": 0.0,
                               "ad": None, "ad_spend": 0.0,
                               "imp": 0.0, "clk": 0.0, "isp": 0.0, "impn": 0,
                               "vv": 0.0, "tp": 0.0, "vimp": 0.0,
                               **dict(blank)}
            if sp > e["acct_spend"]:
                e["acct"], e["acct_spend"] = acct, sp
            # The biggest-spending ad behind this row, so a Script row can open the
            # creative it is actually about. A name can be carried by several ads; the
            # one that spent most is the one the row's numbers are mostly of.
            if r.get("ad_id") and sp > e["ad_spend"]:
                e["ad"], e["ad_spend"] = r["ad_id"], sp
            e["spend"] += sp
            # `impn` counts rows that actually reported impressions. Zero of them means
            # this day was stored before the fields were fetched, which is not the same
            # statement as "this row got no impressions" -- so the day is left BLANK.
            if has_imp(r):
                e["impn"] += 1
                e["imp"] += _num(r.get("impressions"))
                e["clk"] += _num(r.get("clicks"))
                e["isp"] += sp
            if has_vid(r):
                e["vv"] += _num(r.get("vv"))
                e["tp"] += _num(r.get("tp"))
                e["vimp"] += _num(r.get("impressions"))
            e["label"] = e["label"] or lbl
            # An ad name that appears under both stages is reported under the stage that
            # spent more on the day, rather than silently taking whichever row came last.
            if dim in ("script",) and stage != e["stage"] and sp > e["spend"] / 2:
                e["stage"] = stage
            if r.get("ad_name"):
                by_name[r["ad_name"]].append((k, sp))

    for ev in keys:
        for name, n in ((branch_day or {}).get(ev) or {}).items():
            nm = name if isinstance(name, str) else ""
            if dim == "platform" and (nm.startswith(NONE_PREFIX) or not nm or nm == "null"):
                # The only place a non-Meta row can exist: Branch knows these belong to
                # Google or to organic, and this dashboard reads no spend for either, so
                # they appear with trials and installs and no cost beside them.
                slug = partner_slug(nm[len(NONE_PREFIX):]) if nm.startswith(NONE_PREFIX) \
                    else "unknown"
                if slug == "meta":
                    slug = "meta"
                e = rows.get(slug)
                if e is None:
                    e = rows[slug] = {"label": CHANNEL_LABELS.get(slug, slug.title()),
                                      "stage": "", "platform": CHANNEL_LABELS.get(
                                          slug, slug.title()),
                                      "spend": 0.0, **dict(blank)}
                e[ev] += n
                continue
            group = by_name.get(nm)
            if not group:
                continue
            tot = sum(sp for _, sp in group)
            for k, sp in group:
                if k in rows:
                    rows[k][ev] += n * (sp / tot) if tot else n / len(group)
    return rows


# Which dimensions are a thing that can BE paused. A stage or a platform is a bucket, and
# an ad account does not get switched off the way an ad set does.
ACTIVE_DIMS = ("adset", "campaign", "script")


def _series_active(B, dim):
    """(set of keys still running, ok) for one dimension.

    `ok` False means the roster could not be read for at least one account. The caller
    must then treat active as UNKNOWN and refuse to filter -- an empty set would read as
    "nothing is running", which on a page listing what is running is the worst possible
    way to be wrong.
    """
    if dim not in ACTIVE_DIMS:
        return None, True
    keys, ok_all = set(), True
    for a in B["accounts"]:
        camps, live_sets, live_ads, ok = meta_roster(a["id"], False)
        if dim == "adset":
            # The adsets listing is already filtered to ACTIVE by the request itself.
            if not ok["adsets"]:
                ok_all = False
                continue
            keys |= {x["id"] for x in live_sets}
        elif dim == "campaign":
            if not ok["campaigns"]:
                ok_all = False
                continue
            keys |= {c["id"] for c in camps
                     if is_live(c.get("effective_status"))}
        else:
            # A script is an ad NAME, so it is still running if any live ad carries it.
            if not ok["ads"]:
                ok_all = False
                continue
            keys |= {x.get("name") or "" for x in live_ads if x.get("name")}
    return keys, ok_all


def _with_active(data, brand):
    """Stamp `active` on every row at SERVE time, never into the artifact.

    Whether an ad set is running is a property of now; the fold is cached for fifteen
    minutes and persisted to the store for far longer. Baking it in is the same mistake
    the Longevity tab made with its last-spend date -- see _overlay_today.
    """
    dim = data.get("dim")
    if dim not in ACTIVE_DIMS:
        return dict(data, active_known=False, active_dim=False)
    try:
        keys, ok = _series_active(C.brand(brand), dim)
    except Exception:
        keys, ok = None, False
    if not ok or keys is None:
        return dict(data, active_known=False, active_dim=True)
    return dict(data, active_known=True, active_dim=True,
                active_rows=sum(1 for r in data.get("rows") or []
                                if r.get("key") in keys),
                rows=[dict(r, active=(r.get("key") in keys))
                      for r in (data.get("rows") or [])])


def series(brand, since, until, dim="script", force=False, store_only=False):
    """Per-day spend, trials and installs for every row of one dimension.

    Same source split as everything else: settled days out of the store, the rest live.
    Cached, because a fourteen-day fold is a real cost and both views re-ask for it every
    time somebody changes a metric or a dimension.
    """
    dim = dim if dim in DIMS else "script"
    key = (brand, since, until, dim)
    with _series_lock:
        hit = _series_cache.get(key)
        if hit and not force and time.time() - hit["at"] < SERIES_TTL:
            return _with_active(dict(hit["data"], cached=True,
                                     age_min=int((time.time() - hit["at"]) // 60)), brand)

    want, _partial = _series_dates(since, until)
    if not force and want:
        # Already folded by the nightly warm or by another instance. The dates must match
        # EXACTLY: a window that has rolled forward by a day is a different question, and
        # answering it with yesterday's fold would be wrong without looking wrong.
        art = H.get_agg(_series_ns(brand, dim))
        # The row cap has to match as well as the dates. A fold stored under the old
        # top-60 cap has the right dates and the wrong rows, and restoring it would put
        # the truncation back silently -- which is exactly what happened to SpeakEasy on
        # the first deploy of the uncapped fold.
        if art and art.get("dates") == want \
                and art.get("row_cap") == (SERIES_TOP if SERIES_TOP > 0 else SERIES_MAX_ROWS) \
                and art.get("shape") == SERIES_SHAPE:
            _series_cache_put(key, art)
            return _with_active(dict(art, cached=True, restored=True, age_min=0), brand)

    B = C.brand(brand)
    events = B["events"]
    keys = [k for k, _ in _with_installs(events)]
    testing_re = re.compile(B.get("testing_re") or C.TESTING_RE_DEFAULT)
    acct_names = {a["id"]: a["name"] for a in B["accounts"]}
    today = today_ist()
    dates, partial_today = want, _partial
    if not dates:
        return {"brand": brand, "since": since, "until": until, "dim": dim,
                "dates": [], "rows": [], "generated_at": now_ist_str()}

    stored = H.fetch_raw(brand, dates) if H.available() else {}
    # `store_only` is how a browser asks for this fold on an hourly-refresh deployment:
    # fold what the store holds and leave the tail to the scheduled warm, rather than
    # every open tab paying Meta for the same two days.
    live_days = [] if store_only else [x for x in dates if x not in stored]
    live = {}
    # Contiguous runs only: Meta and Branch both cost far more per call than per day, so
    # one range beats a call per day, and a gap in the middle is cheaper to re-fetch than
    # to work around.
    runs, cur = [], []
    for d in live_days:
        dd = datetime.strptime(d, "%Y-%m-%d").date()
        if cur and dd != datetime.strptime(cur[-1], "%Y-%m-%d").date() + timedelta(1):
            runs.append(cur); cur = []
        cur.append(d)
    if cur:
        runs.append(cur)
    for run in runs:
        lo, hi = run[0], run[-1]
        by_day = {}
        for a in B["accounts"]:
            for r in meta_insights_daily(a["id"], lo, hi):
                by_day.setdefault(r.get("date_start"), {}).setdefault(
                    a["id"], []).append(r)
        try:
            bd = branch_trials_daily(lo, hi, B)
        except Exception:
            bd = {}
        for day in run:
            live[day] = {"meta": by_day.get(day, {}), "branch": bd.get(day, {})}

    rows = {}
    for day in dates:
        src = stored.get(day) or live.get(day)
        if not src:
            continue
        for k, e in _dim_day(src.get("meta"), src.get("branch"), keys, dim,
                             testing_re, acct_names).items():
            row = rows.get(k)
            if row is None:
                row = rows[k] = {"key": k, "label": e["label"], "stage": e["stage"],
                                 "platform": e["platform"], "acct": e.get("acct"),
                                 "ad": None, "ad_spend": 0.0,
                                 "total_spend": 0.0, "days": {},
                                 **{"total_" + x: 0.0 for x in keys}}
            row["label"] = row["label"] or e["label"]
            row["acct"] = row["acct"] or e.get("acct")
            # Across the window, not just within a day: the ad that carried this name
            # for most of the money is the one worth opening.
            if e.get("ad") and e.get("ad_spend", 0) > row.get("ad_spend", 0):
                row["ad"], row["ad_spend"] = e["ad"], e["ad_spend"]
            row["stage"] = row["stage"] or e["stage"]
            row["total_spend"] += e["spend"]
            for x in keys:
                row["total_" + x] += e[x]
            rec = row["days"][day] = {"spend": round(e["spend"], 2),
                                      **{x: round(e[x], 2) for x in keys}}
            if e.get("impn"):
                rec["imp"] = e["imp"]
                rec["clk"] = e["clk"]
                rec["isp"] = round(e["isp"], 2)
                row["total_imp"] = (row.get("total_imp") or 0) + e["imp"]
                row["total_clk"] = (row.get("total_clk") or 0) + e["clk"]
                row["total_isp"] = round((row.get("total_isp") or 0) + e["isp"], 2)
            # Written only when the day carried video, and keyed separately from `imp` so
            # a day with impressions and no video stays BLANK on the two video metrics
            # rather than plotting a zero hook rate it never measured.
            if e.get("vimp"):
                rec["vv"] = e["vv"]
                rec["tp"] = e["tp"]
                rec["vimp"] = e["vimp"]
                row["total_vv"] = (row.get("total_vv") or 0) + e["vv"]
                row["total_tp"] = (row.get("total_tp") or 0) + e["tp"]
                row["total_vimp"] = (row.get("total_vimp") or 0) + e["vimp"]

    cap = SERIES_TOP if SERIES_TOP > 0 else SERIES_MAX_ROWS
    # ---- day-on-day budgets -------------------------------------------------
    # Baked into the fold rather than applied at serve time, unlike `active`: a past day's
    # budget is a fact that cannot change, and series windows end yesterday. A day with no
    # snapshot stays ABSENT -- never zero, which would draw as a budget cut to nothing on
    # every day before this feature existed.
    lvl = BUDGET_DIMS.get(dim)
    bud_days = 0
    if lvl:
        for day, snap in (budget_days(brand, dates) or {}).items():
            entries = (snap or {}).get(lvl) or {}
            if not entries:
                continue
            bud_days += 1
            for k, v in entries.items():
                row = rows.get(k)
                if row is None:
                    # Budgeted but never spent in this window. It still has a budget
                    # history, and leaving it out would make "every ad set" untrue.
                    row = rows[k] = {"key": k, "label": v.get("n", ""), "stage": "",
                                     "platform": "Meta", "acct": v.get("a"),
                                     "total_spend": 0.0, "days": {},
                                     **{"total_" + x: 0.0 for x in keys}}
                row["label"] = row["label"] or v.get("n", "")
                row["acct"] = row["acct"] or v.get("a")
                day_rec = row["days"].get(day)
                if day_rec is None:
                    day_rec = row["days"][day] = {"spend": 0.0,
                                                  **{x: 0.0 for x in keys}}
                day_rec["bud"] = v.get("b") or 0.0
                row["total_bud"] = round((row.get("total_bud") or 0.0)
                                         + (v.get("b") or 0.0), 2)

    out_rows = sorted(rows.values(), key=lambda r: -r["total_spend"])[:cap]
    for r in out_rows:
        r.pop("ad_spend", None)      # a working figure, not something the page needs
        r["total_spend"] = round(r["total_spend"], 2)
        for x in keys:
            r["total_" + x] = round(r["total_" + x], 2)

    data = {"brand": brand, "brand_label": B["label"], "since": dates[0],
            "until": dates[-1], "dim": dim, "dim_labels": DIM_LABELS,
            "dates": dates, "keys": keys, "install_key": INSTALL_KEY,
            "event_labels": B["labels"], "cpt_target": B["cpt_target"],
            "rows": out_rows, "truncated": len(rows) > len(out_rows), "row_cap": cap,
            "shape": SERIES_SHAPE,
            # How many of the window's days have a budget snapshot at all. The page needs
            # this to say "recording started on the 26th" instead of drawing a cliff.
            # How many of the window's days carry impressions at all. Without this the
            # page cannot tell a genuinely click-free day from one stored before the
            # fields existed, and would draw the second as a cliff to zero.
            "imp_days": sum(1 for d in dates
                            if any((r["days"].get(d) or {}).get("imp") is not None
                                   for r in out_rows)),
            "imp_from": IMP_FROM,
            "budget_dim": bool(lvl), "budget_days": bud_days,
            "budget_from": (min((d for d in dates
                                 if any((r["days"].get(d) or {}).get("bud") is not None
                                        for r in out_rows)), default=None)
                            if lvl else None),
            "partial_today": partial_today, "excluded_today": today,
            "total_rows": len(rows), "stored_days": len(stored),
            "generated_at": now_ist_str()}
    _series_cache_put(key, data)
    # And to the store, so a woken instance does not re-fold from scratch. The in-process
    # cache dies with the process, which on the free plan is every fifteen idle minutes --
    # exactly the gap the nightly warm was supposed to close.
    H.put_agg(_series_ns(brand, dim), today, data)
    return _with_active(dict(data, cached=False, age_min=0), brand)


def longevity(brand, since, until, force=False):
    """Per ad set across the whole window: when it first spent, how long it kept going.

    Deliberately NOT built from build() per day — that would be one Meta and Branch pull
    per day and defeat the store entirely. Stored days come back raw and are folded here.
    """
    key = (brand, since, until)
    with _long_lock:
        hit = _long_cache.get(key)
        if hit and not force and time.time() - hit["at"] < LONGEVITY_TTL:
            return dict(hit["data"], cached=True,
                        age_min=int((time.time() - hit["at"]) // 60))

    B = C.brand(brand)
    events = B["events"]
    today = today_ist()
    dates = []
    d = datetime.strptime(since, "%Y-%m-%d").date()
    endd = datetime.strptime(min(until, today), "%Y-%m-%d").date()
    while d <= endd:
        dates.append(d.strftime("%Y-%m-%d")); d += timedelta(days=1)

    stored = H.fetch_raw(brand, dates) if H.available() else {}
    live_days = [x for x in dates if x not in stored]
    # The unstored tail is normally today plus the settle window. If the store is empty
    # this becomes the whole range, which is legitimate but slow — the caller is told how
    # many days had to be fetched live so a long wait is explained rather than mysterious.
    live = {}
    # Fetch the unstored days as CONTIGUOUS RUNS, not as one min..max span. A single gap
    # in the middle of the store — eight days lost to a Branch throttle, in the case that
    # found this — would otherwise stretch the live range from that gap all the way to
    # today and re-pull two months from Meta and Branch, which is precisely what the
    # store exists to avoid. Measured: 59 live days instead of 3.
    runs = []
    for day in live_days:
        if runs and (datetime.strptime(day, "%Y-%m-%d")
                     - datetime.strptime(runs[-1][-1], "%Y-%m-%d")).days == 1:
            runs[-1].append(day)
        else:
            runs.append([day])
    for run in runs:
        lo, hi = run[0], run[-1]
        by_day = {}
        for a in B["accounts"]:
            for r in meta_insights_daily(a["id"], lo, hi):
                by_day.setdefault(r.get("date_start"), {}).setdefault(a["id"], []).append(r)
        try:
            btrials = branch_trials_daily(lo, hi, B)
        except Exception:
            btrials = {}
        for day in run:
            live[day] = {"meta": by_day.get(day, {}), "branch": btrials.get(day, {})}

    per_set, series = {}, {}
    for day in dates:
        src = stored.get(day) or live.get(day)
        if not src:
            continue
        for sid, e in _adset_day(src.get("meta"), src.get("branch"), events).items():
            row = per_set.get(sid)
            if row is None:
                row = per_set[sid] = {
                    "id": sid, "name": e["name"], "campaign": e["campaign"],
                    "campaign_id": e["campaign_id"], "account_id": e["account"],
                    "first": day, "last": day, "days": 0, "spend": 0.0,
                    **{k: 0.0 for k in events}}
                series[sid] = {}
            row["name"] = row["name"] or e["name"]
            row["campaign"] = row["campaign"] or e["campaign"]
            if e["spend"] > 0:
                row["first"] = min(row["first"], day)
                row["last"] = max(row["last"], day)
                row["days"] += 1
            row["spend"] += e["spend"]
            for k in events:
                row[k] += e[k]
            series[sid][day] = round(e["spend"], 2)

    # Current status and creation date, for the two things history cannot answer: whether
    # an ad set is live RIGHT NOW, and when it was made if it predates the store.
    # An account whose ad set listing Meta refused tells us nothing about what is live in
    # it. Reporting those ad sets as "not active" would be asserting something false — the
    # same trap build() already sidesteps by labelling them UNKNOWN — and here it would be
    # worse, because the whole view is a list of what is still running. Ad sets in a
    # degraded account get active=None, which the page renders as "unknown", not "paused".
    status, created, degraded_accts = {}, {}, set()
    for a in B["accounts"]:
        camps, live_sets, live_ads, ok = meta_roster(a["id"], False)
        if not ok["adsets"]:
            degraded_accts.add(a["id"])
            continue
        for x in live_sets:
            status[x["id"]] = x.get("effective_status", "ACTIVE")
            if x.get("created_time"):
                created[x["id"]] = x["created_time"][:10]

    covered = sorted(set(stored) | set(live))
    floor = covered[0] if covered else since
    # Daily series only for the heaviest spenders. All 4,403 ad sets by 51 days is a
    # quarter of a million numbers to ship so a sparkline can be drawn on the handful of
    # rows anyone actually reads; the summary for every ad set costs a fraction of that.
    top = sorted(per_set.values(), key=lambda r: -r["spend"])[:DAILY_SERIES_TOP]
    with_series = {r["id"] for r in top}
    for sid, row in per_set.items():
        row["spend"] = round(row["spend"], 2)
        for k in events:
            row[k] = round(row[k], 1)
        unknown = row["account_id"] in degraded_accts
        row["active"] = None if unknown else is_live(status.get(sid))
        row["status"] = "UNKNOWN" if unknown else (status.get(sid) or "INACTIVE")
        row["created"] = created.get(sid, "")
        # An ad set already spending on the first day we can see did not necessarily
        # start there. Saying "went live on the earliest day I happen to have" would be a
        # made-up launch date, and the whole point of this view is launch dates.
        row["censored"] = row["first"] <= floor
        row["span"] = (datetime.strptime(row["last"], "%Y-%m-%d")
                       - datetime.strptime(row["first"], "%Y-%m-%d")).days + 1
        # Aligned to `dates` rather than a date->spend map: the map repeats a 10-character
        # key for every value and is several times the size for the same information.
        row["daily"] = ([int(series[sid].get(d, 0)) for d in dates]
                        if sid in with_series else None)

    out = {"brand": brand, "since": since, "until": until, "dates": dates,
            "covered_from": floor, "covered_days": len(covered),
            "stored_days": len(stored), "live_days": len(live_days),
            "live_runs": len(runs),
            "events": list(events), "cpt_target": B["cpt_target"],
            "series_top": DAILY_SERIES_TOP,
            # Named so the page can say WHICH accounts' live/paused state is unknown,
            # rather than leaving a column of "unknown" unexplained.
            "status_unknown": sorted(
                a["name"] for a in B["accounts"] if a["id"] in degraded_accts),
            "adsets": sorted(per_set.values(), key=lambda r: -r["spend"]),
            "cached": False, "age_min": 0}
    with _long_lock:
        _long_cache[key] = {"at": time.time(), "data": out}
    return out


# ---- precomputed longevity -------------------------------------------------
# Folding 90 stored days takes ~30s, which is a long time to sit in front of a table. The
# fold is over SETTLED days, which by definition cannot change — so it is done once by a
# nightly job and stored, and a request pays only for the handful of unsettled days on the
# end. Everything the fold produces is additive, which is what makes the merge exact
# rather than an approximation: spend and trials sum, `days` counts, `first` is a min,
# `last` a max, and `span` is recomputed from the two.
LONG_WINDOWS = tuple(int(x) for x in
                     os.environ.get("LONG_WINDOWS", "30,60,90,180").split(","))


def _fold_days(brand, dates, B):
    """{adset_id: row} over `dates`, taking whatever the store has and nothing else."""
    events = B["events"]
    stored = H.fetch_raw(brand, dates) if H.available() else {}
    per_set, series = {}, {}
    for day in dates:
        src = stored.get(day)
        if not src:
            continue
        for sid, e in _adset_day(src.get("meta"), src.get("branch"), events).items():
            row = per_set.get(sid)
            if row is None:
                row = per_set[sid] = {
                    "id": sid, "name": e["name"], "campaign": e["campaign"],
                    "campaign_id": e["campaign_id"], "account_id": e["account"],
                    "first": day, "last": day, "days": 0, "spend": 0.0,
                    **{k: 0.0 for k in events}}
                series[sid] = {}
            row["name"] = row["name"] or e["name"]
            row["campaign"] = row["campaign"] or e["campaign"]
            if e["spend"] > 0:
                row["first"] = min(row["first"], day)
                row["last"] = max(row["last"], day)
                row["days"] += 1
            row["spend"] += e["spend"]
            for k in events:
                row[k] += e[k]
            series[sid][day] = round(e["spend"], 2)
    return per_set, series, sorted(stored)


def _tail_days(brand, B, after, today):
    """Per-day folded rows for the days the store does not cover yet. Fetched ONCE.

    This is the expensive half of longevity and the reason the first version of the
    precompute bought nothing: measured on Postly, folding 26 stored days took 0.6s while
    fetching the 4 unsettled days took 17.9s from Meta plus 4.5s from Branch. Optimising
    the fold was optimising 2% of the work. So the tail is fetched once per brand and
    shared across every window, and it goes INTO the artifact rather than being re-fetched
    on the way out.
    """
    days = []
    d = datetime.strptime(after, "%Y-%m-%d").date() + timedelta(days=1)
    while d <= datetime.strptime(today, "%Y-%m-%d").date():
        days.append(d.strftime("%Y-%m-%d")); d += timedelta(days=1)
    if not days:
        return {}
    lo, hi = days[0], days[-1]
    by_day = {}
    for a in B["accounts"]:
        for r in meta_insights_daily(a["id"], lo, hi):
            by_day.setdefault(r.get("date_start"), {}).setdefault(a["id"], []).append(r)
    try:
        btrials = branch_trials_daily(lo, hi, B)
    except Exception:
        btrials = {}
    return {day: _adset_day(by_day.get(day, {}), btrials.get(day, {}), B["events"])
            for day in days if day in by_day}


def _roster_status(B):
    """(status, created, degraded account ids) as of now."""
    status, created, degraded = {}, {}, set()
    for a in B["accounts"]:
        camps, live_sets, live_ads, ok = meta_roster(a["id"], False)
        if not ok["adsets"]:
            degraded.add(a["id"]); continue
        for x in live_sets:
            status[x["id"]] = x.get("effective_status", "ACTIVE")
            if x.get("created_time"):
                created[x["id"]] = x["created_time"][:10]
    return status, created, degraded


def _roster_names(B):
    """{adset_id: {name, campaign, campaign_id, account_id, created}} from the cache.

    Only so an ad set that started spending TODAY, and therefore appears in no folded
    window yet, can still be named on the Longevity tab instead of being absent from it.
    """
    out = {}
    for a in B["accounts"]:
        camps, live_sets, live_ads, ok = meta_roster(a["id"], False)
        if not ok["adsets"]:
            continue
        cname = {c["id"]: c.get("name", "") for c in camps}
        for x in live_sets:
            out[x["id"]] = {
                "name": x.get("name", ""), "campaign_id": x.get("campaign_id", ""),
                "campaign": cname.get(x.get("campaign_id"), ""),
                "account_id": a["id"],
                "created": (x.get("created_time") or "")[:10]}
    return out


# How long today's spend may be reused before it is re-read. Today's figure moves all day,
# so this is short -- but not so short that opening the tab repeatedly costs a Meta call
# each time. The roster beside it is cached for 25 minutes and this is deliberately
# tighter, because "did it spend today" changes faster than "does it still exist".
TODAY_SPEND_TTL = int(os.environ.get("TODAY_SPEND_TTL", "600"))
_today_spend_cache, _today_spend_lock = {}, threading.Lock()


def today_adset_spend(B, today=None, force=False):
    """{adset_id: spend} for today, per brand. {} if Meta will not answer.

    Deliberately level=adset and two fields wide: the ad-level pull this file uses
    everywhere else returns hundreds of rows per account, and nothing here needs them.
    """
    today = today or today_ist()
    key = (B["key"], today)
    with _today_spend_lock:
        hit = _today_spend_cache.get(key)
        if hit and not force and time.time() - hit[0] < TODAY_SPEND_TTL:
            return hit[1]
    out = {}
    for a in B["accounts"]:
        try:
            rows = _graph(f"{a['id']}/insights", {
                "level": "adset", "fields": "adset_id,spend",
                "time_range": json.dumps({"since": today, "until": today})},
                rl_retries=0)
        except Exception:
            # A throttled account means "unknown", never "spent nothing" -- the whole
            # point of this overlay is to stop the tab asserting a stale last-spend date,
            # and asserting a wrong one instead would be worse.
            continue
        for r in rows:
            sp = float(r.get("spend") or 0)
            if sp > 0 and r.get("adset_id"):
                out[r["adset_id"]] = out.get(r["adset_id"], 0.0) + sp
    with _today_spend_lock:
        _today_spend_cache[key] = (time.time(), out)
    return out


def precompute_longevity(brand, days, tail=None, status=None):
    """Fold one window COMPLETE — stored days plus the live tail — and store it.

    Complete on purpose. The artifact is what gets served, so anything left out of it is
    something a request has to pay Meta for, which is the whole thing being avoided.
    `tail` and `status` are passed in so one brand's expensive fetches are shared across
    all four windows instead of repeated four times.
    """
    B = C.brand(brand)
    today = today_ist()
    start = (datetime.strptime(today, "%Y-%m-%d")
             - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    end = H.settled_through(today)
    dates = []
    d = datetime.strptime(start, "%Y-%m-%d").date()
    while d <= datetime.strptime(end, "%Y-%m-%d").date():
        dates.append(d.strftime("%Y-%m-%d")); d += timedelta(days=1)

    per_set, series, covered = _fold_days(brand, dates, B)
    if tail is None:
        tail = _tail_days(brand, B, covered[-1] if covered else end, today)
    if status is None:
        status = _roster_status(B)
    st, created, degraded = status

    for day in sorted(tail):
        for sid, e in tail[day].items():
            row = per_set.get(sid)
            if row is None:
                row = per_set[sid] = {
                    "id": sid, "name": e["name"], "campaign": e["campaign"],
                    "campaign_id": e["campaign_id"], "account_id": e["account"],
                    "first": day, "last": day, "days": 0, "spend": 0.0,
                    **{k: 0.0 for k in B["events"]}}
                series[sid] = {}
            if e["spend"] > 0:
                row["first"] = min(row["first"], day)
                row["last"] = max(row["last"], day)
                row["days"] += 1
            row["spend"] += e["spend"]
            for k in B["events"]:
                row[k] += e[k]
            series[sid][day] = round(e["spend"], 2)

    all_days = sorted(set(covered) | set(tail))
    if not all_days:
        return {"ok": False, "brand": brand, "days": days,
                "reason": "nothing stored or fetchable for this window"}
    floor = all_days[0]
    span_dates = []
    d = datetime.strptime(floor, "%Y-%m-%d").date()
    while d <= datetime.strptime(all_days[-1], "%Y-%m-%d").date():
        span_dates.append(d.strftime("%Y-%m-%d")); d += timedelta(days=1)

    top = sorted(per_set.values(), key=lambda r: -r["spend"])[:DAILY_SERIES_TOP]
    keep = {r["id"] for r in top}
    for sid, row in per_set.items():
        row["spend"] = round(row["spend"], 2)
        for k in B["events"]:
            row[k] = round(row[k], 1)
        unknown = row["account_id"] in degraded
        row["active"] = None if unknown else is_live(st.get(sid))
        row["status"] = "UNKNOWN" if unknown else (st.get(sid) or "INACTIVE")
        row["created"] = created.get(sid, "")
        row["censored"] = row["first"] <= floor
        row["span"] = (datetime.strptime(row["last"], "%Y-%m-%d")
                       - datetime.strptime(row["first"], "%Y-%m-%d")).days + 1
        row["daily"] = ([int(series[sid].get(x, 0)) for x in span_dates]
                        if sid in keep else None)

    art = {"v": 2, "brand": brand, "window_days": days,
           "since": floor, "until": all_days[-1], "dates": span_dates,
           "covered_from": floor, "covered_days": len(all_days),
           "stored_days": len(covered), "live_days": len(tail), "live_runs": 1 if tail else 0,
           "events": list(B["events"]), "cpt_target": B["cpt_target"],
           "series_top": DAILY_SERIES_TOP,
           "status_unknown": sorted(a["name"] for a in B["accounts"]
                                    if a["id"] in degraded),
           "generated_at": now_ist_str(),
           "adsets": sorted(per_set.values(), key=lambda r: -r["spend"])}
    ok = H.put_agg(H.agg_ns(brand, "long", days), today, art)
    return {"ok": ok, "brand": brand, "days": days, "adsets": len(art["adsets"]),
            "covered": len(all_days), "since": art["since"], "until": art["until"],
            "error": None if ok else H.last_error()}


def precompute_brand(brand, windows=None):
    """Every window for one brand, sharing a single tail fetch and roster read."""
    B = C.brand(brand)
    today = today_ist()
    tail = _tail_days(brand, B, H.settled_through(today), today)
    status = _roster_status(B)
    return [precompute_longevity(brand, w, tail=tail, status=status)
            for w in (windows or LONG_WINDOWS)]


def _overlay_today(art, B):
    """Bring `last` and `days` up to today from a live read. Mutates `art`.

    Last-spend is a property of NOW, exactly like status, and for the same reason it
    cannot be taken from the artifact: the artifact is folded twice a day, so between
    folds it says an ad set last spent yesterday even while it is spending. Worse, the
    early fold runs at 04:15 -- before most ad sets have spent anything today -- so for
    most of the day the answer would be yesterday no matter how fresh the artifact was.

    What is NOT overlaid is `spend`, and therefore CPT. Today's trials are not fetched
    here (that is the Branch pull this precompute exists to avoid), and adding today's
    spend without today's trials would inflate every CPT on the tab for exactly the ad
    sets that are running now -- the direction that gets a working ad set killed. Today's
    spend is carried in its own field instead, and the note says which columns cover
    what. `days` IS incremented, because otherwise `span` grows while `days` does not and
    the tab reads that as an ad set that stopped and restarted.
    """
    today = today_ist()
    sp = today_adset_spend(B, today)
    if not sp:
        art["today_known"] = False
        return art
    art["today_known"] = True
    art["today"] = today
    seen, moved = set(), 0
    for row in art.get("adsets") or []:
        seen.add(row["id"])
        amt = sp.get(row["id"])
        if not amt:
            continue
        row["today_spend"] = round(amt, 2)
        if row.get("last", "") < today:
            row["last"] = today
            row["days"] = (row.get("days") or 0) + 1
            moved += 1
    # An ad set that first spent today is in no folded window yet. Absent is the one
    # answer that is certainly wrong on a tab whose question is "what is running".
    names = _roster_names(B)
    fresh = 0
    for sid, amt in sp.items():
        if sid in seen:
            continue
        n = names.get(sid) or {}
        art.setdefault("adsets", []).append({
            "id": sid, "name": n.get("name", ""), "campaign": n.get("campaign", ""),
            "campaign_id": n.get("campaign_id", ""),
            "account_id": n.get("account_id", ""),
            "first": today, "last": today, "days": 1, "spend": 0.0,
            "today_spend": round(amt, 2), "new_today": True, "censored": False,
            "created": n.get("created", ""),
            "active": True, "status": "ACTIVE",
            **{k: 0.0 for k in B["events"]}})
        fresh += 1
    art["today_moved"] = moved
    art["today_new"] = fresh
    art["today_spending"] = len(sp)
    return art


def longevity_fast(brand, days, force=False):
    """The precomputed artifact, served as-is. This is the fast path and it does no work.

    Freshness is reported, never assumed: `generated_at` says when the numbers were
    folded, and `stale_hours` how long ago that was. The page shows it and offers a
    refresh rather than silently presenting yesterday as today.
    """
    B = C.brand(brand)
    if not force:
        art = H.get_agg(H.agg_ns(brand, "long", days))
        if art and art.get("adsets"):
            age = None
            try:
                gen = datetime.strptime(art["generated_at"][:19], "%Y-%m-%d %H:%M:%S")
                age = round((datetime.now(IST).replace(tzinfo=None) - gen)
                            .total_seconds() / 3600, 1)
            except Exception:
                pass
            # Whether an ad set is running is a property of NOW, not of the window that
            # was folded — so it is applied here, never taken from the artifact. Freezing
            # it was a real bug: a precompute that ran while Meta was throttling one
            # account baked UNKNOWN into 2,529 rows, and they stayed unknown for hours
            # after the throttle cleared. Everything else in the artifact describes a
            # closed period and cannot change; this one field can, so it is re-read.
            # meta_roster is cached for 25 minutes, so this is usually free.
            st, created, degraded = _roster_status(B)
            for row in art["adsets"]:
                unknown = row.get("account_id") in degraded
                row["active"] = None if unknown else is_live(st.get(row["id"]))
                row["status"] = ("UNKNOWN" if unknown
                                 else (st.get(row["id"]) or "INACTIVE"))
                row["created"] = created.get(row["id"], row.get("created", ""))
            art["status_unknown"] = sorted(a["name"] for a in B["accounts"]
                                           if a["id"] in degraded)
            _overlay_today(art, B)
            return dict(art, precomputed=True, stale_hours=age,
                        artifact_written=art.get("_written", ""),
                        cached=False, age_min=0)

    # No artifact yet (a brand whose first precompute has not run), or an explicit
    # refresh. Same work the nightly job does, and it stores the result on the way out so
    # the next reader gets it free.
    today = today_ist()
    tail = _tail_days(brand, B, H.settled_through(today), today)
    st = precompute_longevity(brand, days, tail=tail, status=_roster_status(B))
    art = H.get_agg(H.agg_ns(brand, "long", days)) if st.get("ok") else None
    if art:
        return dict(art, precomputed=True, stale_hours=0.0, cached=False, age_min=0)
    since = (datetime.strptime(today, "%Y-%m-%d")
             - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    out = longevity(brand, since, today, force=True)
    out["precomputed"] = False
    return out


def build(since, until, brand=C.DEFAULT_BRAND, force=False, only=None):
    started = time.time()
    B = C.brand(brand)
    EVENTS = B["events"]
    ACCOUNTS = B["accounts"]
    degraded = []
    budgets_known = True
    # Settled days out of the store, the rest live. The shapes are identical to what the
    # two direct fetches used to return, which is why nothing below this line changed.
    insights_by_acct, trials, chan_days, prov = window_data(since, until, B, only=only)

    ads, adsets, campaigns, accounts = {}, {}, {}, {}

    for a in ACCOUNTS:
        insights = insights_by_acct.get(a["id"], [])
        camps, live_sets, live_ads, ok = meta_roster(a["id"], force)
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
                             "imp": 0.0, "clk": 0.0, "imp_spend": 0.0,
                "vv": 0.0, "tp": 0.0, "vimp": 0.0,
                             "active_adsets": 0, "active_ads": 0}
        cstat = {c["id"]: c for c in camps}
        live_set_ids = {s["id"] for s in live_sets}
        live_ad_ids = {x["id"] for x in live_ads}

        # roster first, so ACTIVE objects with zero spend still show up
        for c in camps:
            campaigns[c["id"]] = {
                "id": c["id"], "name": c["name"], "status": c.get("effective_status", ""),
                "account": a["name"], "account_id": a["id"], "spend": 0.0, "budget": 0.0,
                "t101": 0.0, "t10m": 0.0, "imp": 0.0, "clk": 0.0, "imp_spend": 0.0,
                "vv": 0.0, "tp": 0.0, "vimp": 0.0,
                "active_adsets": 0, "active_ads": 0}
        for s in live_sets:
            adsets[s["id"]] = {
                "id": s["id"], "name": s["name"], "status": s.get("effective_status", ""),
                "active": True, "budget": int(s.get("daily_budget") or 0) / 100,
                "campaign_id": s.get("campaign_id"),
                "campaign": (cstat.get(s.get("campaign_id")) or {}).get("name", ""),
                "account": a["name"], "account_id": a["id"],
                "spend": 0.0, "t101": 0.0, "t10m": 0.0,
                "imp": 0.0, "clk": 0.0, "imp_spend": 0.0,
                "vv": 0.0, "tp": 0.0, "vimp": 0.0, "active_ads": 0}
        for x in live_ads:
            ads[x["id"]] = {
                "id": x["id"], "name": x["name"], "status": x.get("effective_status", ""),
                "active": True, "adset_id": x.get("adset_id"), "adset": "",
                "campaign_id": x.get("campaign_id"), "campaign": "",
                "account": a["name"], "account_id": a["id"],
                "spend": 0.0, "t101": 0.0, "t10m": 0.0,
                "imp": 0.0, "clk": 0.0, "imp_spend": 0.0,
                "vv": 0.0, "tp": 0.0, "vimp": 0.0}

        # spend; also picks up objects that spent in the window but are no longer active
        for r in insights:
            sp = float(r.get("spend") or 0)
            aid, sid, cid = r.get("ad_id"), r.get("adset_id"), r.get("campaign_id")
            if cid and cid not in campaigns:
                campaigns[cid] = {"id": cid, "name": r.get("campaign_name", ""),
                                  "status": set_status, "account": a["name"],
                                  "account_id": a["id"], "spend": 0.0, "budget": 0.0,
                                  "t101": 0.0, "t10m": 0.0, "imp": 0.0, "clk": 0.0,
                                  "imp_spend": 0.0,
                                  "active_adsets": 0, "active_ads": 0}
            if sid and sid not in adsets:
                adsets[sid] = {"id": sid, "name": r.get("adset_name", ""),
                               "status": set_status, "active": set_active, "budget": 0.0,
                               "campaign_id": cid, "campaign": r.get("campaign_name", ""),
                               "account": a["name"], "account_id": a["id"],
                               "spend": 0.0, "t101": 0.0, "t10m": 0.0,
                               "imp": 0.0, "clk": 0.0, "imp_spend": 0.0,
                "vv": 0.0, "tp": 0.0, "vimp": 0.0,
                               "active_ads": 0}
            if aid and aid not in ads:
                ads[aid] = {"id": aid, "name": r.get("ad_name", ""), "status": ad_status,
                            "active": ad_active, "adset_id": sid, "adset": r.get("adset_name", ""),
                            "campaign_id": cid, "campaign": r.get("campaign_name", ""),
                            "account": a["name"], "account_id": a["id"],
                            "spend": 0.0, "t101": 0.0, "t10m": 0.0,
                            "imp": 0.0, "clk": 0.0, "imp_spend": 0.0,
                "vv": 0.0, "tp": 0.0, "vimp": 0.0}
            if aid:
                ads[aid]["spend"] += sp
                # Only rows that actually report impressions feed CTR and CPM, and their
                # spend is kept apart so CPM divides like by like. A day stored before
                # these fields were fetched contributes to spend and to nothing else.
                if has_imp(r):
                    ads[aid]["imp"] += _num(r.get("impressions"))
                    ads[aid]["clk"] += _num(r.get("clicks"))
                    ads[aid]["imp_spend"] += sp
                # Video keeps its own impression base: a day stored with impressions but
                # no video must not land under a zero numerator.
                if has_vid(r):
                    ads[aid]["vv"] += _num(r.get("vv"))
                    ads[aid]["tp"] += _num(r.get("tp"))
                    ads[aid]["vimp"] += _num(r.get("impressions"))
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

    # ---- testing vs trial ---------------------------------------------------
    # The split is decided ONCE, on the campaign, and inherited downwards. Matching the
    # pattern against ad set or ad names instead would be a second source of truth that
    # could disagree with the first — and it would, because ad set names travel with a
    # creative when it graduates from testing into trial, while the campaign it sits in
    # is the thing that actually changed.
    testing_re = re.compile(B.get("testing_re") or C.TESTING_RE_DEFAULT)
    for c in campaigns.values():
        c["seg"] = "testing" if testing_re.search(c["name"] or "") else "trial"
    for x in adsets.values():
        x["seg"] = (campaigns.get(x["campaign_id"]) or {}).get("seg", "trial")
    for x in ads.values():
        x["seg"] = (campaigns.get(x["campaign_id"]) or {}).get("seg", "trial")

    for coll in (ads, adsets, campaigns, accounts):
        for o in coll.values():
            for k in CP_KEYS + (INSTALL_KEY,):
                o[k] = 0.0

    # ---- attach Branch trials to ads by NAME -------------------------------
    by_name = defaultdict(list)
    for x in ads.values():
        by_name[x["name"]].append(x)
    dup_names = sum(1 for v in by_name.values() if len(v) > 1)

    matched = {k: 0.0 for k in EVENTS}
    # Every Branch row lands in exactly one channel bucket, and the buckets sum back to
    # the Branch total. Three of them are worth naming:
    #   meta_matched  a named ad that is still live in one of our accounts -- the only
    #                 bucket any CPT on this page is allowed to divide by
    #   meta_orphan   a named ad that is not. Named means Facebook (see NONE_PREFIX), so
    #                 the trial is Meta's; there is simply no ad row left to hang it on
    #   unknown       stored before trials carried their partner. Reported as unknown,
    #                 never apportioned -- run tools/backfill_channels.py to resolve it
    chan = {k: {c: 0.0 for c in CHANNELS} for k in EVENTS}
    orphan = {k: 0.0 for k in EVENTS}
    unknown_chan = {k: 0.0 for k in EVENTS}
    for key in EVENTS:
        for name, n in trials[key].items():
            nm = name if isinstance(name, str) else ""
            if nm.startswith(NONE_PREFIX):
                chan[key][partner_slug(nm[len(NONE_PREFIX):])] += n
                continue
            if not nm or nm == "null":
                unknown_chan[key] += n
                continue
            if nm not in by_name:
                chan[key]["meta"] += n
                orphan[key] += n
                continue
            group = by_name[name]
            matched[key] += n
            chan[key]["meta"] += n
            if len(group) == 1:
                group[0][key] += n
                continue
            # same name on several ads: split by spend so no rollup double counts
            tot = sum(g["spend"] for g in group)
            for g in group:
                g[key] += n * (g["spend"] / tot) if tot else n / len(group)
                g["shared_name"] = True

    # ---- attach Branch installs by the same ad-name join --------------------
    # Kept out of the loop above rather than folded into it because installs must not
    # touch `matched`, `chan` or `unattributed`: those describe trial attribution, which
    # is what every CPT on the page divides by, and quietly adding a third series to them
    # would change the attribution figure into something nobody asked for.
    inst_matched = 0.0
    inst_meta = 0.0
    for name, n in trials.get(INSTALL_KEY, {}).items():
        nm = name if isinstance(name, str) else ""
        if not nm or nm == "null" or nm.startswith(NONE_PREFIX):
            continue
        # A name means Meta (see NONE_PREFIX), whether or not that ad still has a row in
        # this window. The summary tiles divide by this; the ad rows can only carry the
        # matched part, exactly as with trials.
        inst_meta += n
        if nm not in by_name:
            continue
        group = by_name[nm]
        inst_matched += n
        if len(group) == 1:
            group[0][INSTALL_KEY] += n
            continue
        tot = sum(g["spend"] for g in group)
        for g in group:
            g[INSTALL_KEY] += n * (g["spend"] / tot) if tot else n / len(group)

    # ---- attach Classplus signups/mandates to ads by the SAME name key ------
    cp, cp_note = classplus(since, until) if B["classplus"] else (None, None)
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
            s[INSTALL_KEY] += x[INSTALL_KEY]
            for k in CP_KEYS:
                s[k] += x[k]
            for k in ("imp", "clk", "imp_spend", "vv", "tp", "vimp"):
                s[k] += x[k]
            if x["active"]:
                s["active_ads"] += 1
    for s in adsets.values():
        c = campaigns.get(s["campaign_id"])
        if c:
            c["spend"] += s["spend"]; c["t101"] += s["t101"]; c["t10m"] += s["t10m"]
            c[INSTALL_KEY] += s[INSTALL_KEY]
            for k in CP_KEYS:
                c[k] += s[k]
            for k in ("imp", "clk", "imp_spend", "vv", "tp", "vimp"):
                c[k] += s[k]
            c["active_ads"] += s["active_ads"]
            if s["active"]:
                c["active_adsets"] += 1; c["budget"] += s["budget"]
    for c in campaigns.values():
        a = accounts.get(c["account_id"])
        if a:
            a["spend"] += c["spend"]; a["t101"] += c["t101"]; a["t10m"] += c["t10m"]
            a[INSTALL_KEY] += c[INSTALL_KEY]
            for k in CP_KEYS:
                a[k] += c[k]
            for k in ("imp", "clk", "imp_spend", "vv", "tp", "vimp"):
                a[k] += c[k]
            a["budget"] += c["budget"]

    combined = {"spend": sum(a["spend"] for a in accounts.values()),
                "budget": sum(a["budget"] for a in accounts.values()),
                "t101": sum(a["t101"] for a in accounts.values()),
                "t10m": sum(a["t10m"] for a in accounts.values()),
                INSTALL_KEY: sum(a[INSTALL_KEY] for a in accounts.values()),
                "imp": sum(a["imp"] for a in accounts.values()),
                "clk": sum(a["clk"] for a in accounts.values()),
                "imp_spend": sum(a["imp_spend"] for a in accounts.values()),
                "vv": sum(a["vv"] for a in accounts.values()),
                "tp": sum(a["tp"] for a in accounts.values()),
                "vimp": sum(a["vimp"] for a in accounts.values()),
                "active_adsets": sum(a["active_adsets"] for a in accounts.values()),
                "active_ads": sum(a["active_ads"] for a in accounts.values())}
    for k in CP_KEYS:
        combined[k] = sum(a[k] for a in accounts.values())

    # Per-segment account and combined rows. Campaigns already carry everything rolled up
    # from their ad sets and ads, so summing campaigns per account reproduces the account
    # row exactly — for spend, budget, trials and Classplus alike. The campaign/ad set/ad
    # tables are filtered client-side by the `seg` tag instead, because those rows exist
    # already and shipping three copies of them would treble the payload.
    NUM = (("spend", "budget", "t101", "t10m", INSTALL_KEY,
            "imp", "clk", "imp_spend", "vv", "tp", "vimp",
            "active_adsets", "active_ads") + CP_KEYS)

    def _acct_rows(seg):
        out = {}
        for c in campaigns.values():
            if c["seg"] != seg:
                continue
            a = accounts.get(c["account_id"])
            if not a:
                continue
            row = out.setdefault(c["account_id"], {
                "id": a["id"], "name": a["name"], "seg": seg,
                **{k: 0.0 for k in NUM}})
            for k in NUM:
                row[k] += c.get(k, 0) or 0
        return sorted(out.values(), key=lambda r: -r["spend"])

    accounts_by_seg = {sg: _acct_rows(sg) for sg in ("trial", "testing")}
    segments = {sg: {k: sum(r[k] for r in rows) for k in NUM}
                for sg, rows in accounts_by_seg.items()}

    # Budgets and statuses come off the cached ad set listing, not off the insights call
    # that runs every refresh — so they can legitimately be up to ROSTER_TTL old while
    # spend beside them is a minute old. Report that age instead of letting the two look
    # equally live.
    ages = [roster_age(a["id"], "adsets") for a in ACCOUNTS]
    ages = [x for x in ages if x is not None]
    budget_age = int(max(ages)) if ages else None

    branch_totals = {k: sum(trials[k].values()) for k in EVENTS}
    # Everything the per-ad CPT does not cover. Kept as a single figure because that is
    # what "attribution %" is measured against, but no longer the end of the story --
    # `channels` below says which partner each of these trials actually belongs to.
    unattributed = {k: branch_totals[k] - matched[k] for k in EVENTS}
    channels = {k: {"meta_matched": round(matched[k], 1),
                    "meta_orphan": round(orphan[k], 1),
                    "meta": round(chan[k]["meta"], 1),
                    "google": round(chan[k]["google"], 1),
                    "organic": round(chan[k]["organic"], 1),
                    "other": round(chan[k]["other"], 1),
                    "unknown": round(unknown_chan[k], 1)} for k in EVENTS}

    # ---- pro rata -----------------------------------------------------------
    # A second, MODELLED reading of the same data, asked for explicitly and shipped
    # alongside the measured one rather than instead of it. The trials Branch could
    # attribute to NOBODY are shared between Meta and Google in proportion to each one's
    # share of the attributed volume -- computed for each day separately and then summed,
    # because the mix moves and one blended ratio over a month would be a different
    # number. Google's measured trials stay Google's; see prorata_day.
    #
    # It reaches the tables as a single scalar per event: the whole page divides spend by
    # trials, so multiplying every trial count by the same uplift is exactly equivalent to
    # re-deriving each row, and it keeps every rollup exact instead of leaving ad sets
    # that no longer sum to their campaign. What it CANNOT do is move trials between ads:
    # it lifts them all by the window's factor, so the split across rows stays the
    # measured one. Ad-level accuracy is not what this model is for.
    ndays = len(date_range(since, until))
    prorata = {}
    for k in EVENTS:
        alloc, g_alloc, pool_tot, covered = 0.0, 0.0, 0.0, 0
        for d, per_ev in chan_days.items():
            ch = (per_ev or {}).get(k)
            if not ch:
                continue
            a, g, pl = prorata_day(ch)
            alloc += a
            g_alloc += g
            pool_tot += pl
            covered += 1
        # Meta's allocation is earned by Meta's WHOLE measured bucket, but only the
        # `matched` part of it has ad rows to carry it -- the orphans (named ads no
        # longer live) have nowhere to land, exactly as in the measured view, where the
        # tables sum to `matched` and not to `meta`. So the rows take the matched share
        # of the allocation, which makes the uplift 1 + alloc/meta rather than
        # 1 + alloc/matched. Dumping the whole allocation on the matched rows would
        # credit them with trials the orphans earned and quietly cut CPT further.
        m, meta_all = matched[k], chan[k]["meta"]
        row_alloc = alloc * (m / meta_all) if meta_all else 0.0
        meta_pro = meta_all + alloc
        prorata[k] = {
            "uplift": round((m + row_alloc) / m, 6) if m else 1.0,
            "allocated": round(alloc, 1),
            "row_allocated": round(row_alloc, 1),
            "pool": round(chan[k]["organic"] + chan[k]["other"] + unknown_chan[k], 1),
            "matched": round(m, 1),
            "trials": round(m + row_alloc, 1),
            "meta": round(meta_pro, 1),
            # Google's own count plus Google's SHARE of the pool -- computed, not taken as
            # the remainder of branch_total. The two agree on every real day, but on a day
            # with no attributed volume the remainder would silently hand Google the whole
            # unclaimed pool, which is a claim the data does not support.
            "google": round(chan[k]["google"] + g_alloc, 1),
            "google_allocated": round(g_alloc, 1),
            "unallocated": round(pool_tot - alloc - g_alloc, 1),
            "days_covered": covered,
            "days_total": ndays,
        }

    return {
        "since": since, "until": until,
        "generated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "took": round(time.time() - started, 1),
        "brand": B["key"],
        "brand_label": B["label"],
        "brands": [{"key": k, "label": v["label"]} for k, v in C.BRANDS.items()],
        "events": list(EVENTS),
        "event_labels": B["labels"],
        "event_note": B["event_note"],
        # None means "show the number, do not colour it": a target nobody has agreed
        # on is worse than none, because a red cell reads as an instruction.
        "cpt_target": B["cpt_target"],
        "combined": combined,
        "accounts": sorted(accounts.values(), key=lambda r: -r["spend"]),
        "campaigns": sorted(campaigns.values(), key=lambda r: -r["spend"]),
        "adsets": sorted(adsets.values(), key=lambda r: -r["spend"]),
        "ads": sorted(ads.values(), key=lambda r: -r["spend"]),
        # Testing and trial buy different things and must not share a CPT. Campaign,
        # ad set and ad rows carry a `seg` tag and are filtered in the browser; account
        # and combined totals cannot be filtered, so both segments are computed here.
        "segments": segments,
        "accounts_by_seg": accounts_by_seg,
        "segment_rule": B.get("testing_re") or C.TESTING_RE_DEFAULT,
        "segment_campaigns": {
            sg: sorted((c["name"] for c in campaigns.values()
                        if c["seg"] == sg and c["spend"] > 0))
            for sg in ("trial", "testing")},
        "branch_totals": branch_totals,
        "matched": {k: round(v, 1) for k, v in matched.items()},
        "unattributed": {k: round(v, 1) for k, v in unattributed.items()},
        # Which partner earned each trial, straight out of Branch rather than modelled.
        # Meta spend is the only spend this dashboard reads, so Google's trials are shown
        # as a count with no CPT beside them until the Google Ads pull lands.
        "channels": channels,
        "channel_labels": CHANNEL_LABELS,
        # Branch installs, joined to ads by the same name key as trials. Reported
        # separately from `matched` because installs are not what CPT divides by.
        "installs": {"branch_total": round(sum(trials.get(INSTALL_KEY, {}).values()), 1),
                     "matched": round(inst_matched, 1),
                     # Meta's whole bucket and the part of it no ad row can carry, so the
                     # summary can divide by the same thing the trial tiles do.
                     "meta": round(inst_meta, 1),
                     "orphan": round(inst_meta - inst_matched, 1)},
        # The modelled view. `uplift` is the multiplier the page applies to every trial
        # count when pro-rata mode is on; 1.0 means the model changes nothing.
        "prorata": prorata,
        "prorata_model": PRORATA_MODEL,
        "duplicate_ad_names": dup_names,
        # per-account list of which roster listings Meta would not return. Spend,
        # trials and CPT stay correct regardless; only statuses and budgets are affected.
        "degraded": degraded,
        # Set when Branch would not answer. Distinct from a brand having no Branch app:
        # that one has no trials to show, this one has trials it could not read.
        "trials_error": prov.get("trials_error"),
        "budgets_known": budgets_known,
        # The two real pulls, each with its own clock. Spend moves minute to minute,
        # trials arrive all evening, and budgets come off a listing cached for up to an
        # hour -- one "last refreshed" for all three was always a fiction.
        # What fraction of the window's spend has impressions behind it. Below 1.0 the
        # page must say so: CTR and CPM are then true of that fraction, not of the window.
        # What fraction of this window's video rate is actually measured, so a reader is
        # never left guessing whether a low hook rate is the creative or the coverage.
        "vid_coverage": (round(combined["vimp"] / combined["imp"], 4)
                         if combined.get("imp") else None),
        "vid_from": VID_FROM,
        "payload_shape": PAYLOAD_SHAPE,
        "imp_coverage": (round(combined["imp_spend"] / combined["spend"], 4)
                         if combined["spend"] else None),
        "imp_from": IMP_FROM,
        "meta_as_of": _as_of_str(prov.get("meta_at")),
        "meta_age_sec": _age_of(prov.get("meta_at")),
        "trials_as_of": _as_of_str(prov.get("branch_at")),
        "trials_age_sec": _age_of(prov.get("branch_at")),
        "all_stored": prov.get("meta_at") is None,
        # What the day's budget was when the day started, so a whole day's spend has a
        # denominator that was actually in force while it was being spent. Only for a
        # window that IS today: over a longer window the tile shows a per-day average and
        # this would answer a question nobody asked.
        "budget_open": (budget_open(brand, until)
                        if since == until == today_ist() else None),
        "budget_age_sec": budget_age,
        "budget_as_of": (datetime.fromtimestamp(time.time() - budget_age, IST)
                         .strftime("%H:%M") if budget_age is not None else ""),
        "rate_limit": rate_limit_report({a["id"]: a["name"] for a in ACCOUNTS}),
        # Where these numbers came from. Shown rather than kept internal: "27 of 30 days
        # from the store" is the difference between a figure that was just checked and
        # one that was checked days ago, and the reader is entitled to know which.
        "source": prov,
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
