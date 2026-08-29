"""Google sign-in for the dashboard.

Ships dark. With no `GOOGLE_AUTH_CLIENT_ID` the app behaves exactly as it does today --
brand links and nothing else -- so this can be deployed before the OAuth client exists and
switched on by setting three env vars, no code change either way.

What it is: OpenID Connect against Google, restricted to one or more Workspace domains,
with a signed session cookie afterwards. What it is NOT: a user database. There are no
passwords here, no accounts to create and nothing to reset. Google says who you are; a map
in the environment says which brands that address may see.

Why the userinfo endpoint rather than verifying the ID token: the code is exchanged
server-to-server over TLS directly with Google, and the profile is read the same way. That
is the same trust the ID token's signature would establish, without carrying a JWT
verification path -- and a wrong verification is worse than none.
"""

import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

import config as C
import users as U

AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
TIMEOUT = 20
# How long a sign-in lasts before Google is asked again. Long, deliberately: this is a
# read-only dashboard people keep open all day, and a session that dies over lunch trains
# everyone to leave a tab logged in forever somewhere less safe.
MAX_AGE = int(os.environ.get("GOOGLE_AUTH_DAYS", "30")) * 86400


def _env(k, d=""):
    return os.environ.get(k, d).strip()


def client():
    return _env("GOOGLE_AUTH_CLIENT_ID"), _env("GOOGLE_AUTH_CLIENT_SECRET")


def on():
    cid, sec = client()
    return bool(cid and sec)


def domains():
    """Workspace domains allowed to sign in at all. Empty means any Google account, which
    is almost never what anyone wants -- so it is also the one case that refuses."""
    return [d.strip().lower() for d in _env("GOOGLE_AUTH_DOMAIN").split(",") if d.strip()]


def _map():
    """{email: [brands]} from GOOGLE_AUTH_MAP.

    Accepts JSON ({"a@b.com": "all"}) or the flatter `a@b.com=funda,postly; c@b.com=all`,
    because one of those is going to be typed into a Render field by hand.
    """
    raw = _env("GOOGLE_AUTH_MAP")
    if not raw:
        return {}
    out = {}
    try:
        if raw.lstrip().startswith("{"):
            pairs = json.loads(raw).items()
        else:
            pairs = (p.split("=", 1) for p in raw.replace("\n", ";").split(";")
                     if "=" in p)
        for email, brands in pairs:
            out[email.strip().lower()] = _brands(brands)
    except Exception:
        # A malformed map must not lock everyone out AND must not let everyone in. It
        # grants nothing, which the sign-in page reports as "no brands".
        return {}
    return out


def _brands(spec):
    if isinstance(spec, (list, tuple)):
        names = [str(s).strip().lower() for s in spec]
    else:
        names = [s.strip().lower() for s in str(spec).replace(",", " ").split()]
    if "all" in names or "*" in names:
        return list(C.BRANDS)
    return [n for n in names if n in C.BRANDS]


def caps_for(email, hd=""):
    """What this address may see, or None if it may not sign in at all.

    The directory decides, not the domain. The people using this are at two companies that
    have nothing to do with each other, so "anyone at X" was never the rule -- a super
    admin adds an address and that address gets in. GOOGLE_AUTH_DOMAIN survives as an
    optional outer fence for the env-map path, and is normally unset.

    `full` -- the right to make the app SPEND, by forcing a Meta roster re-read or a
    longevity recompute -- belongs to admins, for the same reason a team link is
    read-only.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    caps = U.caps_for(email)
    if caps:
        return dict(caps, via="google")
    # The env map still works, and is checked second: it was how this shipped, and a
    # directory that quietly stopped honouring it would lock someone out mid-week.
    doms = domains()
    dom = email.rpartition("@")[2]
    if doms and dom not in doms and (hd or "").lower() not in doms:
        return None
    brands = _map().get(email)
    if brands is None:
        brands = _brands(_env("GOOGLE_AUTH_DEFAULT"))
    if not brands:
        return None
    return dict(U.caps("super" if len(brands) == len(C.BRANDS) else "member",
                       brands, email), via="google")


# ---- the flow ---------------------------------------------------------------
def redirect_uri(request):
    """Where Google sends the browser back. Must match the OAuth client EXACTLY, so it is
    overridable rather than derived -- Render sits behind a proxy and the scheme it sees
    is not always the scheme the browser used."""
    fixed = _env("GOOGLE_AUTH_REDIRECT")
    if fixed:
        return fixed
    root = request.url_root
    if root.startswith("http://") and not root.startswith("http://127.0.0.1") \
            and not root.startswith("http://localhost"):
        root = "https://" + root[len("http://"):]
    return root.rstrip("/") + "/auth/callback"


def start(request, state):
    """The URL to send someone to. `state` is the CSRF token, held in their session."""
    cid, _ = client()
    p = {"client_id": cid, "redirect_uri": redirect_uri(request),
         "response_type": "code", "scope": "openid email profile",
         "state": state, "prompt": "select_account",
         "access_type": "online",
         # A hint, never a control: Google may still return another account, so the
         # domain is checked again on the way back.
         }
    doms = domains()
    if len(doms) == 1:
        p["hd"] = doms[0]
    return AUTH + "?" + urllib.parse.urlencode(p)


def _post(url, data):
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def finish(request, code):
    """Exchange the code and read the profile. Returns (caps, error)."""
    cid, sec = client()
    try:
        tok = _post(TOKEN, {"code": code, "client_id": cid, "client_secret": sec,
                            "redirect_uri": redirect_uri(request),
                            "grant_type": "authorization_code"})
    except urllib.error.HTTPError as e:
        body = (e.read() or b"")[:300].decode("utf-8", "replace")
        # redirect_uri_mismatch is the one failure worth naming: it is a console setting,
        # not a user error, and "sign-in failed" would send someone hunting in the wrong place.
        if "redirect_uri_mismatch" in body:
            return None, ("This dashboard's redirect URI is not registered on the Google "
                          "OAuth client. Whoever set it up needs to add "
                          + redirect_uri(request))
        return None, "Google refused the sign-in."
    except Exception:
        return None, "Could not reach Google."

    try:
        req = urllib.request.Request(
            USERINFO, headers={"Authorization": "Bearer " + tok.get("access_token", "")})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            info = json.loads(r.read())
    except Exception:
        return None, "Signed in, but Google would not say who you are."

    if not info.get("email_verified"):
        return None, "That Google account has no verified email address."
    caps = caps_for(info.get("email"), info.get("hd"))
    if caps is None:
        doms = domains()
        return None, ("%s is not allowed here." % (info.get("email") or "That account")
                      + (" Sign in with your %s account." % doms[0] if doms else ""))
    caps["name"] = info.get("name") or ""
    return caps, None


def session_caps(session):
    """Caps for the signed-in browser, or None. Re-derived from the map on EVERY request:
    revoking someone's access is an env change that takes effect at once, rather than
    whenever their cookie happens to expire."""
    if not on():
        return None
    email = (session or {}).get("g_email")
    at = (session or {}).get("g_at") or 0
    if not email or time.time() - at > MAX_AGE:
        return None
    return caps_for(email)


def new_state():
    return secrets.token_urlsafe(24)
