"""Who may open this dashboard, and which brands they see.

The list lives in the history store, not in the environment: adding a colleague should be
something the person running this does at 9pm from a phone, not a Render env edit and a
redeploy. Env still carries the BOOTSTRAP admins, because a directory you can lock
yourself out of is a directory you will eventually lock yourself out of.

Access is by ADDRESS, not by domain. The people using this are at Classplus and at
Testbook -- two domains that have nothing to do with each other -- so "anyone at company X"
was never going to be the rule. An admin adds an address; that address gets in. Nobody else
does, whatever they are signed in as. (Not to be confused with Meta, which throughout this
codebase means the ad platform.)

Three roles, and each one earns its place by what it may DO, never by what it may see:
  admin  — every brand, manages this list, and holds the rights that make the app SPEND
           (a forced Meta roster re-read, a longevity recompute).
  member — the brands they were given, and may download the tables as CSV.
  viewer — the brands they were given. Reads the page and nothing else.

"Can see two brands" is not a fourth role, it is two brands.
"""

import os
import re
import time

import config as C
import history as H

NS = "dashusers"
# The store is a network call, and the gate runs on every request. A minute of staleness
# buys not paying for that on each one; the cost is that removing someone takes up to a
# minute to bite. Saving clears it, so the person doing the removing sees it at once.
TTL = 60
# The last directory that was read SUCCESSFULLY, kept far longer than the cache. When the
# store goes down, refusing everyone is the safe answer for a WRITE and the wrong one for
# a READ: on 30 Aug the project's billing lapsed, the store began answering 503, and every
# person except the env-set admin was locked out of a dashboard whose list had not changed.
# Reads now degrade to the last known list; writes still refuse outright.
GOOD_FOR = 24 * 3600
_cache = {"at": 0.0, "users": None, "ok": False, "good": None, "good_at": 0.0}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ROLES = ("super", "member", "viewer")
# The stored key stays "super" — it is in every saved record, and renaming a stored
# value to change a word on screen is how a directory quietly loses its admins.
ROLE_LABELS = {"super": "Admin", "member": "Member", "viewer": "View only"}


def _role(r):
    """Anything unrecognised reads as the LEAST privileged role, never the most. A typo
    in a stored record should cost someone an export button, not hand them the keys."""
    r = str(r or "").strip().lower()
    return r if r in ROLES else "viewer"


def _clean_email(e):
    e = (e or "").strip().lower()
    return e if EMAIL_RE.match(e) else ""


def supers():
    """Bootstrap admins, from the environment. Always present, never removable."""
    raw = os.environ.get("GOOGLE_AUTH_SUPERS", "")
    return [e for e in (_clean_email(x) for x in raw.replace(",", " ").split()) if e]


def _brands(spec):
    if isinstance(spec, (list, tuple)):
        names = [str(s).strip().lower() for s in spec]
    else:
        names = [s.strip().lower() for s in str(spec or "").replace(",", " ").split()]
    if "all" in names or "*" in names:
        return list(C.BRANDS)
    seen, out = set(), []
    for n in names:
        if n in C.BRANDS and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def load(force=False):
    """({email: record}, ok). `ok` is False only when the store could not be READ.

    The two are not the same and the difference is destructive: a failed read treated as
    an empty directory would, on the next save, write back one entry and delete everyone
    else. So a writer refuses on ok=False, and a reader falls back to the bootstrap supers
    rather than to nobody.
    """
    now = time.time()
    if not force and _cache["users"] is not None and now - _cache["at"] < TTL:
        return _cache["users"], _cache["ok"]
    got, ok = (None, False)
    if H.available():
        try:
            got, ok = H.get_agg_raw(NS)
        except Exception:
            got, ok = None, False
    users = {}
    for rec in ((got or {}).get("users") or []):
        e = _clean_email(rec.get("email"))
        if not e:
            continue
        users[e] = {"email": e, "name": str(rec.get("name") or "")[:80],
                    "brands": _brands(rec.get("brands")),
                    "role": _role(rec.get("role")),
                    "note": str(rec.get("note") or "")[:120],
                    "by": rec.get("by", ""), "at": rec.get("at", 0)}
    if ok:
        _cache.update({"at": now, "users": users, "ok": True,
                       "good": users, "good_at": now})
        return users, True
    # Unreadable. Hand back the last good list so people keep the access they were
    # given, and say `ok=False` so nothing writes over a list it could not read.
    good = _cache.get("good")
    if good is not None and now - _cache.get("good_at", 0) < GOOD_FOR:
        return good, False
    return users, False


def listing():
    """Everyone, bootstrap supers included, sorted for display."""
    users, ok = load()
    out = dict(users)
    for e in supers():
        # The env supers outrank whatever the store says about their ROLE and BRANDS, so
        # nobody can be demoted out of their own dashboard by an edit. Their name is not
        # a permission, so a stored one is kept.
        out[e] = dict(out.get(e, {"name": "", "note": "", "by": "environment", "at": 0}),
                      email=e, brands=list(C.BRANDS), role="super", bootstrap=True)
    rows = sorted(out.values(), key=lambda r: (r["role"] != "super", r["email"]))
    return rows, ok


def save(users, by):
    """Replace the directory. Returns (ok, error). Never writes on a failed read."""
    if not H.available():
        return False, "The history store is not configured, so there is nowhere to save."
    # force=True so this is a fresh read, never the degraded copy: saving on top of a
    # list we could not verify is how a directory gets silently truncated.
    _, ok = load(force=True)
    if not ok:
        return False, ("Could not read the current list, so saving would risk deleting "
                       "it. Try again in a moment.")
    clean = []
    for rec in users:
        e = _clean_email(rec.get("email"))
        if not e:
            continue
        role = _role(rec.get("role"))
        # An env super cannot be demoted by an edit to this list — their row is here so
        # their NAME can be set, nothing more.
        if e in supers():
            role = "super"
        brands = list(C.BRANDS) if role == "super" else _brands(rec.get("brands"))
        if not brands:
            # An account with no brands can sign in and see nothing, which reads as a
            # broken dashboard rather than as an access decision. Refuse it here.
            return False, f"{e} has no brands. Give at least one, or remove the address."
        clean.append({"email": e, "name": str(rec.get("name") or "").strip()[:80],
                      "brands": brands, "role": role,
                      "note": str(rec.get("note") or "")[:120],
                      "by": by, "at": int(time.time())})
    wrote = H.put_agg(NS, time.strftime("%Y-%m-%d"), {"users": clean, "by": by,
                                                      "at": int(time.time())})
    if not wrote:
        return False, "The store refused the write. Nothing was changed."
    _cache.update({"at": 0.0, "users": None, "ok": False})   # next read is fresh
    return True, ""


def caps_for(email):
    """What this address may see, or None. The one place access is decided."""
    e = _clean_email(email)
    if not e:
        return None
    if e in supers():
        return caps("super", list(C.BRANDS), e)
    users, _ok = load()
    rec = users.get(e)
    if rec is None:
        # Either not on the list, or the store is down and this process never held a
        # copy — a cold instance during an outage. Both refuse: an unknown address must
        # never be admitted because a network call failed.
        return None
    if not rec["brands"]:
        return None
    return caps(rec["role"], rec["brands"], e)


def caps(role, brands, email):
    """One place where a role becomes rights, so the page and the server cannot disagree.

    `full` is the right to make the app SPEND — forcing a Meta roster re-read or a
    longevity recompute — and it stays with admins. Meta's hourly request-TIME limit
    is the scarcest thing this app has, and it is shared by everyone looking at it, so a
    button that consumes it is not a viewing preference.
    """
    role = _role(role)
    return {"brands": list(brands), "email": email, "role": role,
            "full": role == "super",
            "export": role in ("super", "member")}
