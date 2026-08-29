"""Who may open this dashboard, and which brands they see.

The list lives in the history store, not in the environment: adding a colleague should be
something the person running this does at 9pm from a phone, not a Render env edit and a
redeploy. Env still carries the BOOTSTRAP super admins, because a directory you can lock
yourself out of is a directory you will eventually lock yourself out of.

Access is by ADDRESS, not by domain. The people using this are at Classplus and at Meta —
two domains that have nothing to do with each other — so "anyone at company X" was never
going to be the rule. A super admin adds an address; that address gets in. Nobody else
does, whatever they are signed in as.

Roles, and only two on purpose:
  super  — sees every brand, may manage this list, and holds the rights that let the app
           SPEND (a forced Meta roster re-read, a longevity recompute).
  member — sees the brands they were given, and is read-only in that sense.

A third role would need a reason. "Can see two brands" is not a role, it is two brands.
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
_cache = {"at": 0.0, "users": None, "ok": False}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_email(e):
    e = (e or "").strip().lower()
    return e if EMAIL_RE.match(e) else ""


def supers():
    """Bootstrap super admins, from the environment. Always present, never removable."""
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
        users[e] = {"email": e, "brands": _brands(rec.get("brands")),
                    "role": "super" if rec.get("role") == "super" else "member",
                    "note": str(rec.get("note") or "")[:120],
                    "by": rec.get("by", ""), "at": rec.get("at", 0)}
    if ok:
        _cache.update({"at": now, "users": users, "ok": True})
    return users, ok


def listing():
    """Everyone, bootstrap supers included, sorted for display."""
    users, ok = load()
    out = dict(users)
    for e in supers():
        # The env supers outrank whatever the store says about them, so nobody can be
        # demoted out of their own dashboard by an edit.
        out[e] = dict(out.get(e, {"note": "", "by": "environment", "at": 0}),
                      email=e, brands=list(C.BRANDS), role="super", bootstrap=True)
    rows = sorted(out.values(), key=lambda r: (r["role"] != "super", r["email"]))
    return rows, ok


def save(users, by):
    """Replace the directory. Returns (ok, error). Never writes on a failed read."""
    if not H.available():
        return False, "The history store is not configured, so there is nowhere to save."
    _, ok = load(force=True)
    if not ok:
        return False, ("Could not read the current list, so saving would risk deleting "
                       "it. Try again in a moment.")
    clean = []
    for rec in users:
        e = _clean_email(rec.get("email"))
        if not e:
            continue
        role = "super" if rec.get("role") == "super" else "member"
        brands = list(C.BRANDS) if role == "super" else _brands(rec.get("brands"))
        if not brands:
            # An account with no brands can sign in and see nothing, which reads as a
            # broken dashboard rather than as an access decision. Refuse it here.
            return False, f"{e} has no brands. Give at least one, or remove the address."
        clean.append({"email": e, "brands": brands, "role": role,
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
        return {"brands": list(C.BRANDS), "full": True, "email": e, "role": "super"}
    users, ok = load()
    rec = users.get(e)
    if rec is None:
        # Store unreadable: only the bootstrap supers get in. Better a dashboard the
        # owner can still open than one that lets everyone in because a network call
        # timed out.
        return None
    if not rec["brands"]:
        return None
    return {"brands": rec["brands"], "full": rec["role"] == "super",
            "email": e, "role": rec["role"]}
