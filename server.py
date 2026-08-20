#!/usr/bin/env python3
"""Postly live CPT dashboard — Flask server.

Read-only: serves one page plus /api/data. Nothing here writes to Meta.

Two things exist for the free Render plan specifically:
  * stale-while-revalidate caching, so a woken instance answers immediately with the
    last numbers and refreshes behind the request instead of showing a blank page for
    the ~15-30s a full Meta+Branch pull takes on a shared CPU;
  * no login. The URL is the only thing between the public and live spend figures, so
    the app sends noindex headers and a disallow-all robots.txt to keep it out of
    search results. An optional ADMIN_PASS turns on a password prompt if ever wanted.
"""
import hmac
import os
import threading
import time
import traceback
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, Response, jsonify, render_template, request

import postly_cpt as P

app = Flask(__name__)
# Jinja caches templates when debug is off; the UI is a single file that gets edited
# in place, so pick up changes on reload rather than needing a server restart.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

CACHE_TTL = int(os.environ.get("CACHE_TTL", "180"))
# Open by design — no login. Setting ADMIN_PASS turns on a browser password prompt;
# leaving it unset (the default, and how this is deployed) serves the dashboard to
# anyone with the URL. See README "Access" for what that exposes.
ADMIN_USER = os.environ.get("ADMIN_USER", "postly")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")

_cache, _lock = {}, threading.Lock()
_refreshing = set()


# ------------------------------------------------------------------ auth ---
def _ok(user, pw):
    return (hmac.compare_digest(user or "", ADMIN_USER)
            and hmac.compare_digest(pw or "", ADMIN_PASS))


def protected(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not ADMIN_PASS:            # the default: open, no prompt
            return fn(*a, **kw)
        auth = request.authorization
        if not auth or not _ok(auth.username, auth.password):
            return Response("Authentication required.", 401,
                            {"WWW-Authenticate": 'Basic realm="Postly CPT"'})
        return fn(*a, **kw)
    return wrapper


# ----------------------------------------------------------------- cache ---
def _build_into_cache(key):
    try:
        data = P.build(*key)
        with _lock:
            _cache[key] = {"at": time.time(), "data": data}
    except Exception:
        traceback.print_exc()
    finally:
        with _lock:
            _refreshing.discard(key)


def get_data(since, until, force=False):
    """Fresh -> serve. Stale -> serve stale, refresh behind the request. Cold -> block."""
    key = (since, until)
    with _lock:
        hit = _cache.get(key)
        age = time.time() - hit["at"] if hit else None
        if hit and not force and age < CACHE_TTL:
            return dict(hit["data"], cached=True, age=int(age), stale=False)
        if hit and not force:
            start_bg = key not in _refreshing
            if start_bg:
                _refreshing.add(key)
        else:
            start_bg = False
    if hit and not force:
        if start_bg:
            threading.Thread(target=_build_into_cache, args=(key,), daemon=True).start()
        # stale=True tells the page to check back shortly for the refreshed numbers
        return dict(hit["data"], cached=True, age=int(age), stale=True)

    data = P.build(since, until)
    with _lock:
        _cache[key] = {"at": time.time(), "data": data}
    return dict(data, cached=False, age=0, stale=False)


def resolve_range(rng, since, until):
    today = P.today_ist()
    if since and until:
        return since, until
    if rng == "yesterday":
        d = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        return d, d
    if rng == "7d":
        d = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
        return d, today
    if rng == "3d":
        d = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
        return d, today
    return today, today


# ----------------------------------------------------------------- routes --
@app.route("/")
@protected
def index():
    return render_template("index.html")


@app.route("/api/data")
@protected
def api_data():
    since, until = resolve_range(request.args.get("range", "today"),
                                 request.args.get("since"), request.args.get("until"))
    try:
        return jsonify(get_data(since, until, force=request.args.get("force") == "1"))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)[:500]}), 500


@app.after_request
def _noindex(resp):
    """Nothing here should ever turn up in a search result."""
    resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return resp


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@app.route("/healthz")
def healthz():
    """Public and cheap on purpose — Render's health check must not trigger a pull."""
    return jsonify({"ok": True, "ist": P.today_ist(), "cached_windows": len(_cache)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    print(f"\n  Postly CPT dashboard  ->  http://127.0.0.1:{port}"
          f"{'  (password required)' if ADMIN_PASS else ''}\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
