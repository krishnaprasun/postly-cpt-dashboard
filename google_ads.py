"""Google Ads: spend, impressions and clicks per campaign and ad group per day.

The other half of a Google CPT. Branch already knows how many trials each Google campaign
and ad group earned -- it fills in the campaign and the ad group even though it leaves the
ad name empty -- so all that is missing is what they cost.

**Never load-bearing.** Exactly like the history store: with no credential, an expired
one, or Google refusing, every function here returns empty and says why. The dashboard is
a Meta dashboard that gains a Google section, not a dashboard that needs Google to render.

The join is on NAMES. Branch carries the campaign and ad group name and no ids, so names
are the only shared key -- the same trade the Meta side already makes at ad-name level.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CREDS_PATH = os.environ.get("GOOGLE_ADS_CREDS",
                            os.path.expanduser("~/.anthropic/google_ads.json"))
API_VERSION = os.environ.get("GOOGLE_ADS_API_VERSION", "v22")
BASE = f"https://googleads.googleapis.com/{API_VERSION}"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TIMEOUT = int(os.environ.get("GOOGLE_ADS_TIMEOUT", "60"))

_lock = threading.Lock()
_tok = {"value": None, "exp": 0}
_last_error = None


def last_error():
    return _last_error


def creds():
    """The credential file, or None. Also accepts the same fields from the environment,
    which is how Render holds them -- there is no file on that filesystem."""
    env = {k: os.environ.get("GOOGLE_ADS_" + k.upper(), "").strip()
           for k in ("client_id", "client_secret", "refresh_token",
                     "developer_token", "login_customer_id")}
    if all(env.get(k) for k in ("client_id", "client_secret", "refresh_token",
                                "developer_token")):
        return env
    try:
        with open(CREDS_PATH) as f:
            c = json.load(f)
    except Exception:
        return None
    return c if c.get("refresh_token") and c.get("developer_token") else None


def available():
    return creds() is not None


def _access_token(force=False):
    """A live access token, cached until a minute before it expires.

    Raises with a plain reason. `invalid_grant` gets named for what it almost always is:
    a refresh token minted while the OAuth consent screen was still in Testing, which
    Google expires after seven days no matter how it is stored.
    """
    global _last_error
    c = creds()
    if not c:
        raise RuntimeError("no Google Ads credentials")
    with _lock:
        if not force and _tok["value"] and time.time() < _tok["exp"]:
            return _tok["value"]
    data = urllib.parse.urlencode({
        "client_id": c["client_id"], "client_secret": c["client_secret"],
        "refresh_token": c["refresh_token"], "grant_type": "refresh_token"}).encode()
    try:
        with urllib.request.urlopen(TOKEN_URL, data=data, timeout=TIMEOUT) as r:
            j = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        if "invalid_grant" in body:
            _last_error = ("Google refused the refresh token (invalid_grant). A token "
                           "minted while the OAuth consent screen is in Testing expires "
                           "after 7 days. Publish the consent screen, then re-run "
                           "tools/google_ads_token.py.")
        else:
            _last_error = f"Google token exchange failed: {body}"
        raise RuntimeError(_last_error)
    except Exception as e:
        _last_error = f"Google token exchange failed: {e}"
        raise RuntimeError(_last_error)
    with _lock:
        _tok["value"] = j["access_token"]
        _tok["exp"] = time.time() + int(j.get("expires_in", 3600)) - 60
    _last_error = None
    return _tok["value"]


def _headers(c, login=None):
    """`login-customer-id` names the MANAGER you are acting through, never the account
    you are reading. Pointing it at an ad account -- which is what the stored credential
    did, at Funda's own id -- makes every single call PERMISSION_DENIED, including calls
    for accounts the user plainly has access to. Omitted entirely for a directly
    accessible account, which is what listAccessibleCustomers returns."""
    h = {"Authorization": "Bearer " + _access_token(),
         "developer-token": c["developer_token"],
         "Content-Type": "application/json"}
    login = (login if login is not None
             else c.get("login_customer_id") or "").replace("-", "")
    if login:
        h["login-customer-id"] = login
    return h


def _post(path, body, c, login=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers=_headers(c, login), method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def _err(e):
    """Google's error body is four levels deep and the useful part is one enum. Dig it
    out: `USER_PERMISSION_DENIED` and `DEVELOPER_TOKEN_NOT_APPROVED` are different
    problems with different fixes and the HTTP code is 403 for both."""
    body = e.read().decode()
    try:
        d = (json.loads(body).get("error", {}).get("details") or [{}])[0]
        first = (d.get("errors") or [{}])[0]
        code = json.dumps(first.get("errorCode", {}))
        return f"Google Ads {e.code} {code}: {first.get('message', '')[:200]}"
    except Exception:
        return f"Google Ads {e.code}: {body[:300]}"


CLIENTS_GAQL = """
SELECT customer_client.id, customer_client.descriptive_name,
       customer_client.manager, customer_client.currency_code
FROM customer_client WHERE customer_client.status = 'ENABLED'
"""


def manager_clients(manager_id):
    """[{id, name, manager}] under a manager account, or [].

    listAccessibleCustomers answers with what the USER can reach directly, which for a
    normal setup is one manager and maybe a stray account -- not the twenty ad accounts
    beneath it. This is the step that turns the first into the second.
    """
    global _last_error
    c = creds()
    if not c:
        _last_error = "no Google Ads credentials"
        return []
    mid = str(manager_id).replace("-", "")
    try:
        j = _post(f"/customers/{mid}/googleAds:searchStream",
                  {"query": CLIENTS_GAQL}, c, login=mid)
    except urllib.error.HTTPError as e:
        _last_error = _err(e)
        return []
    except Exception as e:
        _last_error = f"Google Ads: {str(e)[:200]}"
        return []
    out = []
    for chunk in (j if isinstance(j, list) else [j]):
        for row in (chunk.get("results") or []):
            cc = row.get("customerClient") or {}
            if cc.get("id"):
                out.append({"id": str(cc["id"]), "name": cc.get("descriptiveName") or "",
                            "manager": bool(cc.get("manager")),
                            "currency": cc.get("currencyCode") or ""})
    _last_error = None
    return out


def all_customers():
    """Every non-manager account this credential can read, expanded through managers."""
    seen, out = set(), []
    for cid in accessible_customers():
        try:
            j = _post(f"/customers/{cid}/googleAds:searchStream",
                      {"query": "SELECT customer.id, customer.descriptive_name, "
                                "customer.manager FROM customer"}, creds(), login=None)
        except Exception:
            continue
        rows = [r for ch in (j if isinstance(j, list) else [j])
                for r in (ch.get("results") or [])]
        for r in rows:
            cu = r.get("customer") or {}
            if cu.get("manager"):
                for kid in manager_clients(cid):
                    if not kid["manager"] and kid["id"] not in seen:
                        seen.add(kid["id"]); out.append(dict(kid, via=cid))
            elif str(cu.get("id")) not in seen:
                seen.add(str(cu.get("id")))
                out.append({"id": str(cu.get("id")),
                            "name": cu.get("descriptiveName") or "",
                            "manager": False, "via": None})
    return out


def accessible_customers():
    """[customer_id] the credential can see, or [] with last_error() set.

    Discovery rather than configuration: the ids are a property of the Google account,
    and one hard-coded list that silently goes stale is how a report ends up quietly
    missing an account.
    """
    global _last_error
    c = creds()
    if not c:
        _last_error = "no Google Ads credentials"
        return []
    try:
        req = urllib.request.Request(
            BASE + "/customers:listAccessibleCustomers", headers=_headers(c))
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            j = json.loads(r.read().decode())
        _last_error = None
        return [n.rsplit("/", 1)[-1] for n in (j.get("resourceNames") or [])]
    except urllib.error.HTTPError as e:
        _last_error = f"Google Ads {e.code}: {e.read().decode()[:300]}"
        return []
    except Exception as e:
        _last_error = f"Google Ads: {str(e)[:200]}"
        return []


GAQL_DAILY = """
SELECT segments.date, campaign.id, campaign.name, ad_group.id, ad_group.name,
       metrics.cost_micros, metrics.impressions, metrics.clicks
FROM ad_group
WHERE segments.date BETWEEN '{since}' AND '{until}'
"""


def spend_daily(customer_id, since, until, login=None):
    """[{date, campaign, ad_group, spend, imp, clk}] for one customer.

    `ad_group` is the level a Google campaign is actually steered at, and the level
    Branch reports trials at, so it is the level pulled -- campaign totals are the sum of
    its ad groups and never need a second query.
    """
    global _last_error
    c = creds()
    if not c:
        _last_error = "no Google Ads credentials"
        return []
    cid = str(customer_id).replace("-", "")
    body = {"query": GAQL_DAILY.format(since=since, until=until)}
    try:
        j = _post(f"/customers/{cid}/googleAds:searchStream", body, c, login=login)
    except urllib.error.HTTPError as e:
        _last_error = _err(e)
        return []
    except Exception as e:
        _last_error = f"Google Ads: {str(e)[:200]}"
        return []
    out = []
    # searchStream answers with a LIST of response chunks, each holding results.
    for chunk in (j if isinstance(j, list) else [j]):
        for row in (chunk.get("results") or []):
            m = row.get("metrics") or {}
            out.append({
                "date": (row.get("segments") or {}).get("date"),
                "customer_id": cid,
                "campaign_id": str((row.get("campaign") or {}).get("id") or ""),
                "campaign": (row.get("campaign") or {}).get("name") or "",
                "ad_group_id": str((row.get("adGroup") or {}).get("id") or ""),
                "ad_group": (row.get("adGroup") or {}).get("name") or "",
                # Google reports money in micros of the account currency.
                "spend": int(m.get("costMicros") or 0) / 1_000_000,
                "imp": int(m.get("impressions") or 0),
                "clk": int(m.get("clicks") or 0)})
    _last_error = None
    return out


GAQL_CONV = """
SELECT segments.date, segments.conversion_action_name,
       campaign.id, campaign.name, ad_group.id, ad_group.name,
       metrics.conversions
FROM ad_group
WHERE segments.date BETWEEN '{since}' AND '{until}'
  AND segments.conversion_action_name LIKE '%{suffix}%'
"""


# Google appends the creation timestamp to a conversion action's name when the same event
# is imported twice -- "... postly_trial_started_backend 2026-07-10T09:56:11.245". Two of
# the three brands are named that way, so an endswith test against the raw name matches
# nothing at all, which reads on the page as "Google reported no conversions".
_TS_SUFFIX = re.compile(r"\s+\d{4}-\d{2}-\d{2}T[\d:.]+$")


def _action_is(action, event):
    """Whether this conversion action IS the brand's trial event.

    The GAQL filter is a `contains`, because the event sits in the middle of the name once
    a timestamp is appended. Contains alone is too loose -- an event that is a prefix of a
    longer one (`trial_started` inside `trial_started_backend`) would swallow it -- so the
    real test is done here: strip the timestamp, then require the name to END with the
    event.
    """
    name = _TS_SUFFIX.sub("", action or "").strip()
    return name.endswith(event)


def conv_daily(customer_id, since, until, suffix, login=None):
    """[{date, campaign, ad_group, conv}] -- what GOOGLE says the trial event earned.

    The other half of a bifurcated CPT. Branch answers "which Google ad group earned this
    trial" from its own attribution; Google answers the same question from its own, over
    its own lookback windows, and the two do not agree. Neither is wrong; they are
    different questions, and the page shows both rather than picking a winner.

    Matched by NAME SUFFIX, not by conversion action id. A brand's Branch event
    (`trial_started_backend`) arrives in Google as a conversion action called
    "Funda: Daily learning in 1 Min (Android) trial_started_backend" -- the app prefixed
    onto the event -- and the prefix differs per app, per feed and per account, while the
    event name is exactly the thing this dashboard already keys everything else on.

    **`metrics.conversions`, never `all_conversions`.** The same event usually arrives
    twice, from two feeds: a third-party-analytics action (Branch) and a Firebase one.
    Postly has both, and both end with `postly_trial_started_backend`. Only one is marked
    primary, so `conversions` counts it once (508 and 0) while `all_conversions` would
    count it twice (694 and 403) and report a CPT ~40% too low. `conversions` is also the
    column Google itself bids on, which is what makes it the number worth comparing.
    """
    global _last_error
    c = creds()
    if not c:
        _last_error = "no Google Ads credentials"
        return []
    # The suffix goes into a GAQL string literal. Event names are identifiers, but a
    # quote or a backslash arriving from config must never be able to close it.
    suffix = str(suffix or "").replace("\\", "").replace("'", "")
    if not suffix:
        return []
    cid = str(customer_id).replace("-", "")
    body = {"query": GAQL_CONV.format(since=since, until=until, suffix=suffix)}
    try:
        j = _post(f"/customers/{cid}/googleAds:searchStream", body, c, login=login)
    except urllib.error.HTTPError as e:
        _last_error = _err(e)
        return []
    except Exception as e:
        _last_error = f"Google Ads: {str(e)[:200]}"
        return []
    out = []
    for chunk in (j if isinstance(j, list) else [j]):
        for row in (chunk.get("results") or []):
            m = row.get("metrics") or {}
            action = (row.get("segments") or {}).get("conversionActionName") or ""
            if not _action_is(action, suffix):
                continue
            out.append({
                "date": (row.get("segments") or {}).get("date"),
                "customer_id": cid,
                "campaign": (row.get("campaign") or {}).get("name") or "",
                "ad_group": (row.get("adGroup") or {}).get("name") or "",
                "action": action,
                # Google reports fractional conversions; they are summed and rounded once
                # at the top rather than per row.
                "conv": float(m.get("conversions") or 0)})
    _last_error = None
    return out


GAQL_ASSETS = """
SELECT campaign.name, ad_group.name, asset.type,
       ad_group_ad_asset_view.performance_label, ad_group_ad_asset_view.enabled
FROM ad_group_ad_asset_view
WHERE ad_group.status != 'REMOVED' AND campaign.status != 'REMOVED'
"""

# What counts as a creative. Headlines and descriptions are assets too, but nobody means
# them by "how many ads are in this group" -- the video or the image is the ad.
_CREATIVE_TYPES = ("YOUTUBE_VIDEO", "IMAGE", "MEDIA_BUNDLE")


def assets_by_group(customer_id, login=None):
    """{(campaign, ad_group): {...counts}} -- how many creatives each ad group is running.

    **A Google ad group has exactly ONE ad.** Every one of these accounts is App campaigns
    (UAC), where the ad group holds a single `APP_AD` and the actual creative variety lives
    in the ASSETS attached to it -- videos, images, headlines, descriptions. So "how many
    ads" answers 1 for every row and means nothing; the number people are asking for is the
    count of video and image assets, which is what this returns.

    Google labels each asset BEST / GOOD / LOW / LEARNING / PENDING, its own verdict on
    the creative, and those are carried through too: a group with 90 creatives of which 40
    are LOW is a different situation from one with 90 that are mostly GOOD.

    **This is CURRENT state, not windowed.** Assets carry no date here, so the count is
    what the group holds now -- it cannot say what was running three weeks ago. The column
    says so rather than letting it be read as a figure for the selected window.
    """
    global _last_error
    c = creds()
    if not c:
        _last_error = "no Google Ads credentials"
        return {}
    cid = str(customer_id).replace("-", "")
    try:
        j = _post(f"/customers/{cid}/googleAds:searchStream", {"query": GAQL_ASSETS},
                  c, login=login)
    except urllib.error.HTTPError as e:
        _last_error = _err(e)
        return {}
    except Exception as e:
        _last_error = f"Google Ads: {str(e)[:200]}"
        return {}
    out = {}
    for chunk in (j if isinstance(j, list) else [j]):
        for row in (chunk.get("results") or []):
            v = row.get("adGroupAdAssetView") or {}
            key = ((row.get("campaign") or {}).get("name") or "",
                   (row.get("adGroup") or {}).get("name") or "")
            rec = out.get(key)
            if rec is None:
                rec = out[key] = {"cre": 0, "cre_off": 0, "txt": 0,
                                  "best": 0, "good": 0, "low": 0, "learning": 0,
                                  "pending": 0}
            if (row.get("asset") or {}).get("type") not in _CREATIVE_TYPES:
                rec["txt"] += 1
                continue
            # `enabled` false means the asset is attached but no longer serving. Counting
            # it as live would say a group is running creatives it has already retired.
            if not v.get("enabled"):
                rec["cre_off"] += 1
                continue
            rec["cre"] += 1
            lbl = (v.get("performanceLabel") or "").lower()
            if lbl in rec:
                rec[lbl] += 1
    _last_error = None
    return out


def status():
    """A one-shot health read for the page and for the ops endpoint."""
    c = creds()
    if not c:
        return {"configured": False, "ok": False,
                "error": "no Google Ads credentials on this instance"}
    try:
        _access_token(force=True)
    except Exception as e:
        return {"configured": True, "ok": False, "error": str(e)[:400]}
    ids = accessible_customers()
    kids = all_customers() if ids else []
    return {"configured": True, "ok": bool(kids), "accessible": ids,
            "customers": [{"id": k["id"], "name": k["name"]} for k in kids],
            "login_customer_id": (creds() or {}).get("login_customer_id"),
            "error": None if kids else last_error(),
            "api_version": API_VERSION}
