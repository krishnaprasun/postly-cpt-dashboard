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

import config as C
import history as H
import postly_cpt as P

app = Flask(__name__)
# Jinja caches templates when debug is off; the UI is a single file that gets edited
# in place, so pick up changes on reload rather than needing a server restart.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# 90s: short enough that the figures on screen are never meaningfully behind Meta
# (which lags a few minutes anyway), long enough that clicking between tabs and
# ranges does not re-pull. Past this the cache is served stale and refreshed behind
# the request rather than blocking it.
CACHE_TTL = int(os.environ.get("CACHE_TTL", "90"))
# Open by design — no login. Setting ADMIN_PASS turns on a browser password prompt;
# leaving it unset (the default, and how this is deployed) serves the dashboard to
# anyone with the URL. See README "Access" for what that exposes.
ADMIN_USER = os.environ.get("ADMIN_USER", "postly")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")

_cache, _lock = {}, threading.Lock()
_refreshing = set()
_last_error = {}


def _friendly(exc):
    """Meta's raw error JSON is not something to put in front of someone judging CPT.

    No estimate is invented here. An earlier version of this string promised a throttle
    "usually clears within a few minutes"; it then ran unbroken for over half an hour.
    Meta reports `estimated_time_to_regain_access` on the throttled response itself, so
    either that number is quoted or no time is given at all.
    """
    if isinstance(exc, P.RateLimited):
        return {"kind": "rate_limit",
                "text": "Meta is rate-limiting this ad account, so these are the last "
                        "figures that came through." +
                        (f" Meta puts access back in about {exc.regain_min} min."
                         if exc.regain_min else "")}
    return {"kind": "error", "text": "Could not refresh: " + str(exc)[:200]}


def _with_live_limits(payload):
    """A cached payload carries the rate-limit picture from whenever it was built.

    Serving that verbatim is how a page ends up counting down to a moment that has
    already passed, or claiming all-clear while a refresh is being refused right now.
    The numbers are cached; the throttle state never is.
    """
    accounts = C.brand(payload.get("brand"))["accounts"]
    return dict(payload, rate_limit=P.rate_limit_report(
        {a["id"]: a["name"] for a in accounts}))


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
            _last_error.pop(key, None)
    except Exception as e:
        traceback.print_exc()
        with _lock:
            _last_error[key] = _friendly(e)
    finally:
        with _lock:
            _refreshing.discard(key)


def get_data(since, until, brand, force=False, hard=False):
    """Fresh -> serve. Stale -> serve stale, refresh behind the request. Cold -> block.

    `force` skips this cache. `hard` additionally re-reads Meta's roster — names,
    statuses and BUDGETS — which is otherwise cached for 30-60 minutes. Only the explicit
    Refresh button sets it: the automatic pull must not, because the ads listing is the
    most expensive call here and its long TTL is what keeps the app under Meta's hourly
    request-time limit.
    """
    # The brand is part of the key, not a filter applied afterwards: each brand is a
    # separate set of Meta and Branch calls, and one brand's throttle must never evict
    # or stale another's figures.
    key = (since, until, brand)
    with _lock:
        hit = _cache.get(key)
        age = time.time() - hit["at"] if hit else None
        if hit and not force and age < CACHE_TTL:
            return _with_live_limits(dict(hit["data"], cached=True, age=int(age),
                                          stale=False))
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
        with _lock:
            warn = _last_error.get(key)
        out = _with_live_limits(dict(hit["data"], cached=True, age=int(age),
                                     stale=not warn))
        if warn:
            out["warning"] = warn["text"]
            out["warning_kind"] = warn["kind"]
        return out

    try:
        data = P.build(since, until, brand, force=hard)
    except Exception as e:
        # A throttle or a blip must not blank the dashboard. If any figures were ever
        # fetched for this window, serve those and say how old they are; only a cold
        # cache with nothing to fall back on is a real error.
        with _lock:
            hit = _cache.get(key)
            _last_error[key] = _friendly(e)
        if hit:
            w = _friendly(e)
            return _with_live_limits(
                dict(hit["data"], cached=True, age=int(time.time() - hit["at"]),
                     stale=False, warning=w["text"], warning_kind=w["kind"]))
        raise
    with _lock:
        _cache[key] = {"at": time.time(), "data": data}
        _last_error.pop(key, None)
    return dict(data, cached=False, age=0, stale=False)


def resolve_range(rng, since, until):
    today = P.today_ist()
    if since and until:
        return since, until
    if rng == "yesterday":
        d = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        return d, d
    back = {"3d": 2, "7d": 6, "30d": 29}.get(rng)
    if back is not None:
        d = (datetime.strptime(today, "%Y-%m-%d")
             - timedelta(days=back)).strftime("%Y-%m-%d")
        return d, today
    return today, today


# ----------------------------------------------------------------- routes --
@app.route("/")
@protected
def index():
    # The brand list is server-rendered so the switcher and the loading veil are correct
    # on the very first paint, before any data has been fetched.
    brand = request.args.get("brand", C.DEFAULT_BRAND)
    if brand not in C.BRANDS:
        brand = C.DEFAULT_BRAND
    return render_template(
        "index.html",
        brands=[{"key": k, "label": v["label"]} for k, v in C.BRANDS.items()],
        default_brand=brand,
        brand_logo=C.BRANDS[brand]["logo"],
        brand_themes=[dict(t, key=k) for k, t in
                      ((k, v["theme"]) for k, v in C.BRANDS.items())],
        brand_meta={k: {"label": v["label"], "accounts": len(v["accounts"]),
                        "branch": bool(C.BRAND_HAS_BRANCH(k)),
                        "classplus": bool(v["classplus"]),
                        "logo": v["logo"]}
                    for k, v in C.BRANDS.items()})


@app.route("/api/data")
@protected
def api_data():
    since, until = resolve_range(request.args.get("range", "today"),
                                 request.args.get("since"), request.args.get("until"))
    brand = request.args.get("brand", C.DEFAULT_BRAND)
    if brand not in C.BRANDS:
        brand = C.DEFAULT_BRAND
    try:
        hard = request.args.get("hard") == "1"
        return jsonify(get_data(since, until, brand,
                                force=hard or request.args.get("force") == "1", hard=hard))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)[:500]}), 500


@app.route("/api/longevity")
@protected
def api_longevity():
    """Per ad set: when it went live, how long it ran, what it spent and returned.

    Separate from /api/data rather than folded into it: it reads a much wider window and
    costs far more, so making every page load pay for it would be the wrong trade for a
    view most people open occasionally.
    """
    brand = request.args.get("brand", C.DEFAULT_BRAND)
    if brand not in C.BRANDS:
        brand = C.DEFAULT_BRAND
    today = P.today_ist()
    days = max(7, min(int(request.args.get("days", "90")), 370))
    since = request.args.get("since") or (
        datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    until = request.args.get("until") or today
    force = request.args.get("force") == "1"
    try:
        # The fast path serves a nightly-precomputed fold of the settled days and fetches
        # only the unsettled tail. It is used whenever the caller asks for a plain window
        # rather than explicit dates, which is what the page does.
        if not (request.args.get("since") or request.args.get("until")):
            return jsonify(P.longevity_fast(brand, days, force=force))
        return jsonify(P.longevity(brand, since, until, force=force))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)[:500]}), 500


@app.route("/api/precompute", methods=["POST", "GET"])
def api_precompute():
    """Fold the settled days for each longevity window and store the result.

    Token-gated for the same reason as /api/snapshot: it spends Meta and Branch quota.
    Run nightly, after the snapshot jobs have written the day this depends on.
    """
    want = H.TOKEN
    if not want or not hmac.compare_digest(request.headers.get("Authorization", ""),
                                           "Bearer " + want):
        return jsonify({"error": "unauthorized"}), 401
    if not H.available():
        return jsonify({"error": "no history store configured"}), 503
    brands = [request.args.get("brand")] if request.args.get("brand") else list(C.BRANDS)
    bad = [b for b in brands if b not in C.BRANDS]
    if bad:
        return jsonify({"error": f"unknown brand(s): {bad}"}), 400
    wins = ([int(request.args["days"])] if request.args.get("days")
            else list(P.LONG_WINDOWS))
    out = []
    for b in brands:
        for w in wins:
            try:
                out.append(P.precompute_longevity(b, w))
            except Exception as e:
                traceback.print_exc()
                out.append({"ok": False, "brand": b, "days": w, "error": str(e)[:200]})
    return jsonify({"wrote": sum(1 for r in out if r.get("ok")), "results": out})


@app.route("/api/snapshot", methods=["POST", "GET"])
def api_snapshot():
    """Write settled days to the history store. For the nightly job, not for browsers.

    Token-gated even though the rest of the app is open, and for a different reason than
    privacy: this endpoint SPENDS. It pulls Meta and Branch for every brand, so leaving it
    open on a public URL would hand anyone a button that burns the request-time budget
    this app is already rate-limited against.

    ?date=YYYY-MM-DD  one specific settled day (default: the newest settled day)
    ?brand=postly     one brand (default: all of them)
    ?days=N           N days back from the newest settled day, skipping stored ones
    """
    want = H.TOKEN
    got = request.headers.get("Authorization", "")
    if not want or not hmac.compare_digest(got, "Bearer " + want):
        return jsonify({"error": "unauthorized"}), 401
    if not H.available():
        return jsonify({"error": "no history store configured"}), 503

    brands = [request.args.get("brand")] if request.args.get("brand") else list(C.BRANDS)
    bad = [b for b in brands if b not in C.BRANDS]
    if bad:
        return jsonify({"error": f"unknown brand(s): {bad}"}), 400

    newest = H.settled_through(P.today_ist())
    if request.args.get("date"):
        dates = [request.args["date"]]
    else:
        n = max(1, min(int(request.args.get("days", "1")), 120))
        dates = [(datetime.strptime(newest, "%Y-%m-%d") - timedelta(days=i))
                 .strftime("%Y-%m-%d") for i in range(n)][::-1]

    # `limit` caps how many days one call will actually FETCH, skips excluded. It is what
    # lets a 90-day backfill run as many short calls instead of one long one: each stays
    # well inside gunicorn's 180s timeout, and the gaps between calls are what stop this
    # from crowding out the live page's Branch quota the way a continuous backfill did.
    limit = max(1, min(int(request.args.get("limit", "365")), 365))

    out, wrote, fetched = [], 0, 0
    for b in brands:
        have = set(H.have(b))
        for d in dates:
            if d in have:
                continue                       # silent: a full listing helps nobody here
            if fetched >= limit:
                out.append({"brand": b, "date": d, "ok": True,
                            "skipped": "limit reached — next call continues"})
                break
            fetched += 1
            try:
                r = P.snapshot(b, d)
            except P.BranchThrottled as e:
                # Same rule as the CLI backfill: stop, do not keep knocking. Every extra
                # attempt feeds the limiter that is holding the door shut.
                out.append({"brand": b, "ok": False, "date": d,
                            "error": "Branch rate-limiting — stopped", "stopped": True})
                break
            except Exception as e:
                traceback.print_exc()
                r = {"ok": False, "brand": b, "date": d, "error": str(e)[:200]}
                out.append(r)
                continue
            wrote += 1 if r.get("ok") else 0
            out.append(r)
    remaining = {}
    for b in brands:
        have = set(H.have(b))
        remaining[b] = len([d for d in dates if d not in have])
    return jsonify({"wrote": wrote, "fetched": fetched,
                    "remaining": remaining, "results": out})


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
