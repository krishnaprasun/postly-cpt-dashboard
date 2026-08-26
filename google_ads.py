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
