"""A small key-value store over HTTPS, for the handful of things that must survive
everything else being down.

Upstash Redis, spoken through its REST API: one endpoint, one token, no client library
and no connection pool. That matters more than it sounds -- this module exists because the
access directory was living in the same GCS-backed service as a year of ad history, and
when that project's billing lapsed on 30 Aug the whole team lost access to a list that had
not changed in a day.

So this is deliberately NOT where analytics data goes. It holds small documents that gate
or configure the app, and it is chosen for having no dependency on the same account,
project or provider as anything else here.

Unconfigured (`KV_URL` unset) it reports unavailable and every caller falls through to
whatever it used before, which is how this ships without breaking anything.
"""

import json
import os
import urllib.error
import urllib.request

TIMEOUT = 12


def _env(k):
    return os.environ.get(k, "").strip()


def url():
    return _env("KV_URL").rstrip("/")


def available():
    return bool(url() and _env("KV_TOKEN"))


def _cmd(*parts):
    """Run one Redis command. Returns (result, ok) -- `ok` False means the store could
    not be reached or refused, which is NOT the same as a key that is not there."""
    if not available():
        return None, False
    try:
        req = urllib.request.Request(
            url(), data=json.dumps(list(parts)).encode(),
            headers={"Authorization": "Bearer " + _env("KV_TOKEN"),
                     "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = json.loads(r.read() or b"{}")
        if isinstance(body, dict) and "error" in body:
            return None, False
        return (body or {}).get("result"), True
    except Exception:
        return None, False


def get_json(key):
    """(value, ok). A missing key is (None, True) -- absent is an answer, unreachable is not."""
    raw, ok = _cmd("GET", key)
    if not ok:
        return None, False
    if raw in (None, ""):
        return None, True
    try:
        return json.loads(raw), True
    except Exception:
        # Stored but unreadable. Treated as a failed read, never as an empty document:
        # the one thing that must not happen is overwriting a list we could not parse.
        return None, False


def put_json(key, value):
    """True when the store confirmed the write."""
    _res, ok = _cmd("SET", key, json.dumps(value, separators=(",", ":")))
    return ok


def ping():
    """(ok, detail) -- for a health check that says which half is wrong."""
    if not url():
        return False, "KV_URL is not set"
    if not _env("KV_TOKEN"):
        return False, "KV_TOKEN is not set"
    res, ok = _cmd("PING")
    return ok, (str(res) if ok else "the store did not answer")
