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


def meta_insights(acct, since, until):
    """Ad-level spend for the window. Re-pulled on every refresh; this is the number."""
    return _graph(f"{acct}/insights", {
        "level": "ad", "time_range": json.dumps({"since": since, "until": until}),
        "fields": "ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,spend"})


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


def meta_roster(acct, force=False):
    """(campaigns, active ad sets, active ads, ok_flags) — each piece independently
    cached and independently allowed to fail.

    `force` is for an explicit Refresh only. The automatic 30-minute pull must NOT set it:
    the ads listing is the single most expensive call the dashboard makes and its 60-minute
    TTL exists to keep it off the hourly time budget.
    """
    camps, ok_c = _part(acct, "campaigns", lambda: _graph(
        f"{acct}/campaigns", {"fields": "id,name,effective_status,daily_budget"},
        rl_retries=0), ROSTER_TTL, force)
    sets, ok_s = _part(acct, "adsets", lambda: _graph(
        f"{acct}/adsets", {"fields": "id,name,effective_status,daily_budget,campaign_id,"
                                     "created_time",
                           "effective_status": json.dumps(["ACTIVE"])},
        rl_retries=0), ROSTER_TTL, force)
    ads_, ok_a = _part(acct, "ads", lambda: _graph(
        f"{acct}/ads", {"fields": "id,name,effective_status,adset_id,campaign_id",
                        "effective_status": json.dumps(["ACTIVE"])},
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
    return _graph(f"{acct}/insights", {
        "level": "ad", "time_increment": 1,
        "time_range": json.dumps({"since": since, "until": until}),
        "fields": "ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,spend"})


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


def window_data(since, until, B, today=None):
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
    if live_since:
        for a in B["accounts"]:
            meta[a["id"]] += meta_insights_daily(a["id"], live_since, live_until)
        # Branch failing must not take the page down with it. Spend, budgets, statuses and
        # the whole testing/trial split come from Meta and are perfectly good without it —
        # a Branch throttle used to 500 the entire dashboard, which is how a backfill
        # competing for Branch quota made SpeakEasy unreachable rather than merely
        # trial-less. Reported as an explicit failure, never as zero: a zero here would
        # read as "no trials happened", which is a different and much worse claim.
        try:
            for day, per_ev in branch_trials_daily(live_since, live_until, B).items():
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
    with _have_lock:
        _have_cache.pop(brand, None)
    return {"ok": ok, "date": date, "brand": brand,
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
# 0 means "every row". The Matrix paginates, so there is no longer a screen-sized reason
# to truncate, and the truncation was hiding real money: on Postly's 30-day script fold
# the top 60 rows are only 40% of the spend, and rank 61 had still spent Rs24,000.
# The ceiling is a safety net against a dimension nobody has tried yet, not a view limit.
SERIES_TOP = int(os.environ.get("SERIES_TOP", "0"))
SERIES_MAX_ROWS = int(os.environ.get("SERIES_MAX_ROWS", "20000"))
# Bumped when the SHAPE of a folded row changes. Checked alongside dates and row_cap
# before a stored fold is reused, for the same reason: a fold from before rows carried
# their account gives the Matrix a grid with no links and no way to tell why.
SERIES_SHAPE = 2
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
                k = lbl = r.get("ad_name") or ""
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
                               **dict(blank)}
            if sp > e["acct_spend"]:
                e["acct"], e["acct_spend"] = acct, sp
            e["spend"] += sp
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
                     if (c.get("effective_status") or "") == "ACTIVE"}
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


def series(brand, since, until, dim="script", force=False):
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
    live_days = [x for x in dates if x not in stored]
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
                                 "total_spend": 0.0, "days": {},
                                 **{"total_" + x: 0.0 for x in keys}}
            row["label"] = row["label"] or e["label"]
            row["acct"] = row["acct"] or e.get("acct")
            row["stage"] = row["stage"] or e["stage"]
            row["total_spend"] += e["spend"]
            for x in keys:
                row["total_" + x] += e[x]
            row["days"][day] = {"spend": round(e["spend"], 2),
                                **{x: round(e[x], 2) for x in keys}}

    cap = SERIES_TOP if SERIES_TOP > 0 else SERIES_MAX_ROWS
    out_rows = sorted(rows.values(), key=lambda r: -r["total_spend"])[:cap]
    for r in out_rows:
        r["total_spend"] = round(r["total_spend"], 2)
        for x in keys:
            r["total_" + x] = round(r["total_" + x], 2)

    data = {"brand": brand, "brand_label": B["label"], "since": dates[0],
            "until": dates[-1], "dim": dim, "dim_labels": DIM_LABELS,
            "dates": dates, "keys": keys, "install_key": INSTALL_KEY,
            "event_labels": B["labels"], "cpt_target": B["cpt_target"],
            "rows": out_rows, "truncated": len(rows) > len(out_rows), "row_cap": cap,
            "shape": SERIES_SHAPE,
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
        row["active"] = None if unknown else (status.get(sid) == "ACTIVE")
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
        row["active"] = None if unknown else (st.get(sid) == "ACTIVE")
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
                row["active"] = None if unknown else (st.get(row["id"]) == "ACTIVE")
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


def build(since, until, brand=C.DEFAULT_BRAND, force=False):
    started = time.time()
    B = C.brand(brand)
    EVENTS = B["events"]
    ACCOUNTS = B["accounts"]
    degraded = []
    budgets_known = True
    # Settled days out of the store, the rest live. The shapes are identical to what the
    # two direct fetches used to return, which is why nothing below this line changed.
    insights_by_acct, trials, chan_days, prov = window_data(since, until, B)

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
    for name, n in trials.get(INSTALL_KEY, {}).items():
        nm = name if isinstance(name, str) else ""
        if not nm or nm == "null" or nm.startswith(NONE_PREFIX) or nm not in by_name:
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
            if x["active"]:
                s["active_ads"] += 1
    for s in adsets.values():
        c = campaigns.get(s["campaign_id"])
        if c:
            c["spend"] += s["spend"]; c["t101"] += s["t101"]; c["t10m"] += s["t10m"]
            c[INSTALL_KEY] += s[INSTALL_KEY]
            for k in CP_KEYS:
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
            a["budget"] += c["budget"]

    combined = {"spend": sum(a["spend"] for a in accounts.values()),
                "budget": sum(a["budget"] for a in accounts.values()),
                "t101": sum(a["t101"] for a in accounts.values()),
                "t10m": sum(a["t10m"] for a in accounts.values()),
                INSTALL_KEY: sum(a[INSTALL_KEY] for a in accounts.values()),
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
                     "matched": round(inst_matched, 1)},
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
