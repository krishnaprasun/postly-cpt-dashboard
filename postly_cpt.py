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

Read-only. This module never writes to Meta.
"""
import json, sys, time, urllib.error, urllib.parse, urllib.request
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
def _graph(path, params, tries=6):
    pr = dict(params); pr["access_token"] = C.META_TOKEN; pr.setdefault("limit", "500")
    url = f"{C.GRAPH}/{path}?" + urllib.parse.urlencode(pr)
    out = []
    while url:
        for i in range(tries):
            try:
                with urllib.request.urlopen(url, timeout=120) as r:
                    j = json.load(r)
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode()
                # code 4 / 17 = rate limit. Back off briefly; the dashboard must stay
                # responsive, so this is a short retry rather than the 35-min cooldown
                # the batch scripts use.
                if ('"code":17' in body or '"code":4' in body) and i < tries - 1:
                    time.sleep(8 * (i + 1)); continue
                raise RuntimeError(f"Meta {path}: {body[:300]}")
            except Exception as ex:
                if i < tries - 1:
                    time.sleep(4 * (i + 1)); continue
                raise RuntimeError(f"Meta {path}: {ex}")
        out += j.get("data", [])
        url = j.get("paging", {}).get("next")
    return out


def meta_account(acct, since, until):
    """Ad-level spend for the window + the ACTIVE roster (names, status, budgets)."""
    tr = json.dumps({"since": since, "until": until})
    insights = _graph(f"{acct}/insights", {
        "level": "ad", "time_range": tr,
        "fields": "ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,spend"})
    campaigns = _graph(f"{acct}/campaigns",
                       {"fields": "id,name,effective_status,daily_budget"})
    adsets = _graph(f"{acct}/adsets", {
        "fields": "id,name,effective_status,daily_budget,campaign_id",
        "effective_status": json.dumps(["ACTIVE"])})
    ads = _graph(f"{acct}/ads", {
        "fields": "id,name,effective_status,adset_id,campaign_id",
        "effective_status": json.dumps(["ACTIVE"])})
    return insights, campaigns, adsets, ads


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


# ---------------------------------------------------------------- build ----
def build(since, until):
    started = time.time()
    trials = branch_trials_by_ad(since, until)

    ads, adsets, campaigns, accounts = {}, {}, {}, {}

    for a in ACCOUNTS:
        insights, camps, live_sets, live_ads = meta_account(a["id"], since, until)
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
                                  "status": "INACTIVE", "account": a["name"],
                                  "account_id": a["id"], "spend": 0.0, "budget": 0.0,
                                  "t101": 0.0, "t10m": 0.0,
                                  "active_adsets": 0, "active_ads": 0}
            if sid and sid not in adsets:
                adsets[sid] = {"id": sid, "name": r.get("adset_name", ""),
                               "status": "INACTIVE", "active": False, "budget": 0.0,
                               "campaign_id": cid, "campaign": r.get("campaign_name", ""),
                               "account": a["name"], "account_id": a["id"],
                               "spend": 0.0, "t101": 0.0, "t10m": 0.0, "active_ads": 0}
            if aid and aid not in ads:
                ads[aid] = {"id": aid, "name": r.get("ad_name", ""), "status": "INACTIVE",
                            "active": False, "adset_id": sid, "adset": r.get("adset_name", ""),
                            "campaign_id": cid, "campaign": r.get("campaign_name", ""),
                            "account": a["name"], "account_id": a["id"],
                            "spend": 0.0, "t101": 0.0, "t10m": 0.0}
            if aid:
                ads[aid]["spend"] += sp
                ads[aid]["adset"] = ads[aid]["adset"] or r.get("adset_name", "")
                ads[aid]["campaign"] = ads[aid]["campaign"] or r.get("campaign_name", "")

        accounts[a["id"]]["active_adsets"] = len(live_set_ids)
        accounts[a["id"]]["active_ads"] = len(live_ad_ids)

    # fill parent names for roster ads that never spent
    for x in ads.values():
        if not x["adset"] and x["adset_id"] in adsets:
            x["adset"] = adsets[x["adset_id"]]["name"]
        if not x["campaign"] and x["campaign_id"] in campaigns:
            x["campaign"] = campaigns[x["campaign_id"]]["name"]

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

    # ---- roll up ad -> adset -> campaign -> account -------------------------
    for x in ads.values():
        s = adsets.get(x["adset_id"])
        if s:
            s["spend"] += x["spend"]; s["t101"] += x["t101"]; s["t10m"] += x["t10m"]
            if x["active"]:
                s["active_ads"] += 1
    for s in adsets.values():
        c = campaigns.get(s["campaign_id"])
        if c:
            c["spend"] += s["spend"]; c["t101"] += s["t101"]; c["t10m"] += s["t10m"]
            c["active_ads"] += s["active_ads"]
            if s["active"]:
                c["active_adsets"] += 1; c["budget"] += s["budget"]
    for c in campaigns.values():
        a = accounts.get(c["account_id"])
        if a:
            a["spend"] += c["spend"]; a["t101"] += c["t101"]; a["t10m"] += c["t10m"]
            a["budget"] += c["budget"]

    combined = {"spend": sum(a["spend"] for a in accounts.values()),
                "budget": sum(a["budget"] for a in accounts.values()),
                "t101": sum(a["t101"] for a in accounts.values()),
                "t10m": sum(a["t10m"] for a in accounts.values()),
                "active_adsets": sum(a["active_adsets"] for a in accounts.values()),
                "active_ads": sum(a["active_ads"] for a in accounts.values())}

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
    }


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else today_ist()
    u = sys.argv[2] if len(sys.argv) > 2 else d
    r = build(d, u)
    print(json.dumps({k: v for k, v in r.items()
                      if k not in ("adsets", "ads", "campaigns", "accounts")}, indent=1))
    print(f"adsets={len(r['adsets'])} ads={len(r['ads'])} campaigns={len(r['campaigns'])}")
