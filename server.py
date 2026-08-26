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
import gzip
import hmac
import os
import threading
import time
import traceback
from datetime import datetime, timedelta
from html import escape
from functools import wraps

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request)

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
# Sized to sit just inside the page's 15-minute refresh, on purpose. At 90 seconds every
# open tab that ticked triggered its own rebuild -- Meta insights plus a Branch pull, and
# Branch is the source that throttles. At 13 minutes the first tick past it rebuilds once
# and every other tab, and every other person, is served from that for free. The page is
# no less current: a request past the TTL is answered from the stale copy AND kicks the
# refresh, so the numbers land seconds later either way.
CACHE_TTL = int(os.environ.get("CACHE_TTL", "780"))
# Open by design — no login. Setting ADMIN_PASS turns on a browser password prompt;
# leaving it unset (the default, and how this is deployed) serves the dashboard to
# anyone with the URL. See README "Access" for what that exposes.
ADMIN_USER = os.environ.get("ADMIN_USER", "postly")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")

_cache, _lock = {}, threading.Lock()
_refreshing = set()
_last_error = {}
# Don't write the payload to GCS on every build. A build happens whenever the 90s cache
# expires and someone is looking, and persisting each one would be a steady stream of
# multi-megabyte writes to buy nothing — what matters is that SOMETHING recent survives
# the instance sleeping.
PERSIST_EVERY = int(os.environ.get("PERSIST_EVERY", "600"))
_persisted = {}


def _persist(key, data):
    """Save this payload for the next cold start. Best effort, never blocks a response."""
    if not H.available():
        return
    since, until, brand = key
    now = time.time()
    with _lock:
        if now - _persisted.get(key, 0) < PERSIST_EVERY:
            return
        _persisted[key] = now
    try:
        H.put_payload(brand, since, until, dict(data, _saved_at=now), P.today_ist())
    except Exception:
        # A cache that cannot be saved is not an error anyone needs to hear about; the
        # request already has its numbers.
        traceback.print_exc()


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


def _key():
    """The link key on this request. Accepted from the query string or the path."""
    return (request.args.get("k") or request.view_args or {}).get("k", "") \
        if isinstance(request.view_args, dict) else (request.args.get("k") or "")


def _allowed(key):
    """Brands this key may see, or None if the key is not valid."""
    return C.brands_for(key)


def _gate(key, brand):
    """(brand, error_response, full). Narrows the request to what the key allows.

    `full` gates the two things that make the app SPEND — a hard roster re-read and a
    longevity recompute. Hiding those buttons in the page is presentation; this is the
    part that actually holds, because a hidden button is one edited URL away.
    """
    caps = C.link_caps(key)
    if caps is None:
        return None, (jsonify({"error": "This link is not valid. Ask for your team's "
                               "dashboard link."}), 403), False
    if brand not in caps["brands"]:
        # Not an error worth explaining in detail — a wrong brand on a valid key is
        # either a stale bookmark or someone trying it on, and both get the same answer.
        brand = caps["brands"][0]
    return brand, None, caps["full"]


# ----------------------------------------------------------------- cache ---
def _build_into_cache(key):
    try:
        data = P.build(*key)
        with _lock:
            _cache[key] = {"at": time.time(), "data": data}
            _last_error.pop(key, None)
        _persist(key, data)
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

    # Cold in this process — but the last instance may have left a payload behind. Serving
    # that immediately and rebuilding behind the request is the whole point: a woken
    # instance should show real numbers at once, not spend half a minute proving it can.
    if not (force or hard):
        saved = H.get_payload(brand, since, until)
        # A payload built by an older pro-rata model is not a stale number, it is a
        # wrong one: the page scales every trial by the multiplier the payload carries.
        # Drop it and pay for the rebuild rather than restore it.
        if saved and saved.get("prorata") \
                and saved.get("prorata_model") != P.PRORATA_MODEL:
            saved = None
        if saved and saved.get("combined"):
            age = int(max(0, time.time() - (saved.get("_saved_at") or 0)))
            with _lock:
                if key not in _refreshing:
                    _refreshing.add(key)
                    start = True
                else:
                    start = False
            if start:
                threading.Thread(target=_build_into_cache, args=(key,),
                                 daemon=True).start()
            return _with_live_limits(dict(saved, cached=True, age=age, stale=True,
                                          restored=True))

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
    _persist(key, data)
    return dict(data, cached=False, age=0, stale=False)


def resolve_range(rng, since, until):
    today = P.today_ist()
    if since and until:
        return since, until
    if rng == "yesterday":
        d = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        return d, d
    back = {"3d": 2, "7d": 6, "14d": 13, "30d": 29, "60d": 59}.get(rng)
    if back is not None:
        d = (datetime.strptime(today, "%Y-%m-%d")
             - timedelta(days=back)).strftime("%Y-%m-%d")
        return d, today
    return today, today


# ----------------------------------------------------------------- routes --
@app.route("/b/<key>")
@protected
def branded(key):
    """One team's link. Locks the page to that brand and hides the switcher."""
    return index(key=key)


@app.route("/")
@protected
def index(key=None):
    # The brand list is server-rendered so the switcher and the loading veil are correct
    # on the very first paint, before any data has been fetched.
    key = key or request.args.get("k", "")
    caps = C.link_caps(key)
    allowed = caps["brands"] if caps else None
    if allowed is None:
        # No hint about which links exist or how many — a wrong key learns nothing.
        return Response(
            "<!doctype html><meta charset=utf-8><title>Ads Performance</title>"
            "<style>body{font:15px/1.6 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
            "background:#FDFCF7;color:#1A1C2E;display:grid;place-items:center;height:100vh;"
            "margin:0}div{max-width:380px;padding:0 24px}</style>"
            "<div><h1 style='font-size:18px;margin:0 0 8px'>Ads Performance</h1>"
            "<p style='color:#787E91'>This dashboard is reached through your team's own "
            "link. Ask whoever set it up for yours.</p></div>", 403,
            mimetype="text/html")

    brand = request.args.get("brand", allowed[0])
    if brand not in allowed:
        brand = allowed[0]
    return render_template(
        "index.html",
        app_version=APP_VERSION,
        link_key=key,
        can_act=caps["full"],
        # Only the brands this link may see. A switcher listing brands the key cannot
        # open would be a list of things to go looking for.
        brands=[{"key": k, "label": C.BRANDS[k]["label"]} for k in allowed],
        default_brand=brand,
        brand_logo=C.BRANDS[brand]["logo"],
        brand_themes=[dict(t, key=k) for k, t in
                      ((k, v["theme"]) for k, v in C.BRANDS.items())],
        brand_meta={k: {"label": C.BRANDS[k]["label"],
                        "accounts": len(C.BRANDS[k]["accounts"]),
                        "branch": bool(C.BRAND_HAS_BRANCH(k)),
                        "classplus": bool(C.BRANDS[k]["classplus"]),
                        "logo": C.BRANDS[k]["logo"]}
                    for k in allowed})


@app.route("/api/data")
@protected
def api_data():
    since, until = resolve_range(request.args.get("range", "today"),
                                 request.args.get("since"), request.args.get("until"))
    brand, err, full = _gate(request.args.get("k", ""),
                             request.args.get("brand", C.DEFAULT_BRAND))
    if err:
        return err
    try:
        # A read-only link cannot force a hard roster re-read. Ignored rather than
        # refused: the request is a perfectly good read, it just does not get to spend
        # Meta's request-time budget, and the page still refreshes on its own cadence.
        hard = full and request.args.get("hard") == "1"
        force = full and (hard or request.args.get("force") == "1")
        return jsonify(get_data(since, until, brand, force=force, hard=hard))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)[:500]}), 500


# ---- daily series -----------------------------------------------------------
# Same stale-while-revalidate shape as /api/data, for the same reason: a fourteen-day
# fold over a wide dimension is a real Meta and Branch cost, and nobody should watch a
# spinner for it when perfectly good numbers from ten minutes ago are on hand.
_series_refreshing = set()


def _series_into_cache(args):
    try:
        P.series(*args, force=True)
    except Exception:
        traceback.print_exc()
    finally:
        with _lock:
            _series_refreshing.discard(args)


@app.route("/api/series")
@protected
def api_series():
    """Per-day spend, trials and installs for every row of one dimension.

    Feeds both the Trends chart and the date matrix — they are two renderings of one
    answer, so they share a request rather than each paying for their own fold.
    """
    brand, err, full = _gate(request.args.get("k", ""),
                             request.args.get("brand", C.DEFAULT_BRAND))
    if err:
        return err
    # These two views own their window rather than following the page's range picker.
    # A trend line and a date grid over a single day are not a smaller version of the
    # answer, they are no answer at all -- and "Today", the picker's default, is exactly
    # that. Longevity already works this way for the same reason.
    if request.args.get("since") and request.args.get("until"):
        since, until = request.args["since"], request.args["until"]
    else:
        try:
            days = int(request.args.get("days", P.SERIES_DEFAULT_DAYS))
        except ValueError:
            days = P.SERIES_DEFAULT_DAYS
        since, until = P.series_window(days)
    dim = request.args.get("dim", "script")
    force = full and request.args.get("force") == "1"
    args = (brand, since, until, dim)
    try:
        out = P.series(*args, force=force)
        # Served from cache and old enough to be worth refreshing: hand back what we have
        # and rebuild behind the request, exactly as the main payload does.
        if out.get("cached") and out.get("age_min", 0) >= 5 and not force:
            with _lock:
                start = args not in _series_refreshing
                if start:
                    _series_refreshing.add(args)
            if start:
                threading.Thread(target=_series_into_cache, args=(args,),
                                 daemon=True).start()
            out = dict(out, stale=True)
        return jsonify(out)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)[:500]}), 500


@app.route("/api/prior")
@protected
def api_prior():
    """Totals for the window immediately BEFORE the one on screen, from the store only.

    Its own endpoint, and store-only on purpose. A trend needs a comparison, but the
    comparison must not cost what the page costs -- pulling Meta and Branch again for a
    second window would double the request budget on every load, for a subtitle. Settled
    days are already sitting in the store, so this reads them and says how many it found.
    Incomplete is reported, never quietly averaged over fewer days.
    """
    brand, err, _full = _gate(request.args.get("k", ""),
                              request.args.get("brand", C.DEFAULT_BRAND))
    if err:
        return err
    since, until = resolve_range(request.args.get("range", "7d"),
                                 request.args.get("since"), request.args.get("until"))
    try:
        return jsonify(P.prior_window(brand, since, until))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)[:300]}), 500


@app.route("/api/google")
@protected
def api_google():
    """Google campaigns and ad groups: trials, installs, and spend when it is available.

    Its own endpoint, not part of /api/data, for the same reason Longevity and the series
    have their own: it is a different question with a different cost, and the Meta page
    must not get slower because a Google tab exists.
    """
    brand, err, full = _gate(request.args.get("k", ""),
                             request.args.get("brand", C.DEFAULT_BRAND))
    if err:
        return err
    since, until = resolve_range(request.args.get("range", "7d"),
                                 request.args.get("since"), request.args.get("until"))
    try:
        return jsonify(P.google_window(brand, since, until,
                                       force=full and request.args.get("force") == "1"))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)[:500]}), 500


@app.route("/api/google/series")
@protected
def api_google_series():
    """Per-day Google spend and trials by campaign or ad group.

    Same shape as /api/series on purpose — Trends and Matrix read that shape and nothing
    else, so Google gets both views without a second implementation of either.
    """
    brand, err, full = _gate(request.args.get("k", ""),
                             request.args.get("brand", C.DEFAULT_BRAND))
    if err:
        return err
    if request.args.get("since") and request.args.get("until"):
        since, until = request.args["since"], request.args["until"]
    else:
        try:
            days = int(request.args.get("days", P.SERIES_DEFAULT_DAYS))
        except ValueError:
            days = P.SERIES_DEFAULT_DAYS
        since, until = P.series_window(days)
    try:
        return jsonify(P.google_series(brand, since, until,
                                       dim=request.args.get("dim", "gadgroup"),
                                       force=full and request.args.get("force") == "1"))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)[:500]}), 500


@app.route("/api/google/spend")
@protected
def api_google_spend():
    """Google spend for a window and nothing else — no Branch, so it is cheap enough for
    the Meta view to ask for it just to print a comparison."""
    brand, err, _full = _gate(request.args.get("k", ""),
                              request.args.get("brand", C.DEFAULT_BRAND))
    if err:
        return err
    since, until = resolve_range(request.args.get("range", "7d"),
                                 request.args.get("since"), request.args.get("until"))
    try:
        return jsonify(dict(P.google_spend_only(brand, since, until),
                            since=since, until=until))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)[:300]}), 500


@app.route("/api/google/status")
def api_google_status():
    """Is the Google credential alive, and if not, exactly why. Public and cheap: it
    returns no numbers, only whether the door is open."""
    import google_ads as GA
    return jsonify(GA.status())


@app.route("/api/longevity")
@protected
def api_longevity():
    """Per ad set: when it went live, how long it ran, what it spent and returned.

    Separate from /api/data rather than folded into it: it reads a much wider window and
    costs far more, so making every page load pay for it would be the wrong trade for a
    view most people open occasionally.
    """
    brand, err, full = _gate(request.args.get("k", ""),
                             request.args.get("brand", C.DEFAULT_BRAND))
    if err:
        return err
    today = P.today_ist()
    days = max(7, min(int(request.args.get("days", "90")), 370))
    since = request.args.get("since") or (
        datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    until = request.args.get("until") or today
    # Recomputing longevity is a ~30s Meta and Branch pull. Read-only links get the
    # precomputed artifact and nothing else.
    force = full and request.args.get("force") == "1"
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


@app.route("/api/backfill/google", methods=["POST", "GET"])
def api_backfill_google():
    """Store Google's per-day campaign/ad-group trials for settled days that lack them.

    Token-gated and bounded like the reach backfill, and for the same reason: without it,
    every Google Trends or Matrix view re-pulls Branch for its whole window.
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
    try:
        budget = max(10, min(int(request.args.get("budget", "90")), 240))
    except ValueError:
        budget = 90
    out = []
    for b in brands:
        try:
            out.append(P.google_backfill(b, budget_s=budget,
                                         dry=request.args.get("dry") == "1"))
        except Exception as e:
            traceback.print_exc()
            out.append({"brand": b, "error": str(e)[:200]})
    return jsonify({"pending": sum(r.get("pending_after") or 0 for r in out),
                    "results": out})


@app.route("/api/backfill/reach", methods=["POST", "GET"])
def api_backfill_reach():
    """Re-fetch stored days so they carry impressions and clicks, in bounded batches.

    Token-gated: it spends real Meta request time, which is the budget this app is
    rate-limited on. One call does as much as fits in `budget` seconds and reports what
    is left, so the schedule can simply fire until `pending_after` reaches zero -- after
    which every call is one cheap store read that changes nothing.

    ?brand=postly   one brand (default: all)
    ?budget=90      seconds of work per call
    ?max=0          hard cap on days per call, 0 for none
    ?dry=1          fetch and report, write nothing
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
    try:
        budget = max(10, min(int(request.args.get("budget", "90")), 240))
    except ValueError:
        budget = 90
    try:
        cap = max(0, int(request.args.get("max", "0")))
    except ValueError:
        cap = 0
    dry = request.args.get("dry") == "1"
    out = []
    for b in brands:
        try:
            out.append(P.reach_backfill(b, budget_s=budget, max_days=cap, dry=dry))
        except Exception as e:
            traceback.print_exc()
            out.append({"brand": b, "error": str(e)[:200]})
    return jsonify({"dry": dry, "budget_s": budget,
                    "pending": sum(r.get("pending_after") or 0 for r in out),
                    "results": out})


@app.route("/api/budgets/snapshot", methods=["POST", "GET"])
def api_budget_snapshot():
    """Record every level's budget as it stands now, under today's date.

    Token-gated like the other writing endpoints -- it spends Meta quota and writes to the
    store. Cheap enough to run several times a day: it reads the roster, which is cached,
    so most calls cost nothing at all.

    Budget history can only run FORWARD. Meta reports a budget as it is now, insights
    never carry the budget behind a day's spend, and this app's activity-log access
    retains one day -- so there is nothing to backfill and this endpoint does not pretend
    there is.
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
    out = []
    for b in brands:
        try:
            snap = P.budget_snapshot(b)
            out.append({"brand": b, "date": snap["date"], "at": snap["at"],
                        "adsets": len(snap["adsets"]), "campaigns": len(snap["campaigns"]),
                        "accounts": len(snap["accounts"]), "total": snap["total"],
                        "samples": snap.get("samples"), "moved": len(snap.get("moved") or []),
                        "degraded": snap.get("degraded") or [],
                        "stored": snap.get("stored"),
                        "error": snap.get("store_error")})
        except Exception as e:
            traceback.print_exc()
            out.append({"brand": b, "stored": False, "error": str(e)[:200]})
    return jsonify({"results": out})


@app.route("/api/budgets")
@protected
def api_budgets():
    """The stored budget history for one brand: {date: {level: {id: {...}}}}.

    Read-only and link-gated like the rest of the page. Days with no snapshot are absent
    from `dates` rather than present with zeros.
    """
    brand, err, _full = _gate(request.args.get("k", ""),
                              request.args.get("brand", C.DEFAULT_BRAND))
    if err:
        return err
    try:
        days = max(1, min(int(request.args.get("days", "30")), 400))
    except ValueError:
        days = 30
    today = P.today_ist()
    since = (datetime.strptime(today, "%Y-%m-%d")
             - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    dates = P.date_range(since, today)
    got = P.budget_days(brand, dates) or {}
    have = sorted(got)
    return jsonify({
        "brand": brand, "since": since, "until": today,
        "dates": have, "missing": [d for d in dates if d not in got],
        "days": got,
        "recording_from": have[0] if have else None,
        "note": ("Budget history only runs forward from the first snapshot. Days before "
                 "that were never recorded and are absent, not zero."),
    })


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
    # Warm the daily series for the combination the Trends and Matrix tabs open on.
    # The fold is 15-25s cold, which on a woken free instance is long enough that someone
    # concludes the tab is broken. Warming it here costs the nightly job a few seconds and
    # nobody else anything. Only the default window and dimension: warming every
    # combination would be spending Meta quota on views that may never be opened.
    warmed = []
    if request.args.get("series", "1") != "0":
        since, until = P.series_window()
        for b in brands:
            try:
                r = P.series(b, since, until, dim="script", force=True)
                warmed.append({"brand": b, "rows": r.get("total_rows"),
                               "days": len(r.get("dates") or []),
                               "window": f"{since}..{until}"})
            except Exception as e:
                traceback.print_exc()
                warmed.append({"brand": b, "error": str(e)[:200]})
    return jsonify({"wrote": sum(1 for r in out if r.get("ok")), "results": out,
                    "series_warmed": warmed})


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


# A full series fold is 1.5-2 MB of JSON now that the Matrix shows every row, and it is
# almost all digits and repeated keys -- it compresses about eight to one. Done here
# rather than with a dependency, because the whole app is Flask and gunicorn and this is
# the only thing on it big enough to care.
GZIP_MIN = 4096


def _app_version():
    """A short stamp that changes whenever this app's code does.

    The browser cache holds payloads for up to twelve hours, and a payload is shaped by
    the code that produced it. Without this, a deploy that adds a field leaves everyone
    reading yesterday's shape until it expires -- which is how a CPM column came out as a
    row of dashes on a matrix that had every number it needed. Folded into the cache key,
    so a deploy retires the old entries instead of serving them.
    """
    h = 0
    for f in ("postly_cpt.py", "server.py", "config.py", "history.py",
              "templates/index.html"):
        try:
            h ^= int(os.stat(os.path.join(os.path.dirname(__file__), f)).st_mtime)
        except OSError:
            pass
    return format(h & 0xffffffff, "x")


APP_VERSION = _app_version()


@app.after_request
def _gzip(resp):
    if resp.direct_passthrough or resp.status_code >= 300:
        return resp
    if "gzip" not in (request.headers.get("Accept-Encoding") or ""):
        return resp
    if resp.headers.get("Content-Encoding"):
        return resp
    ct = (resp.headers.get("Content-Type") or "")
    if not (ct.startswith("application/json") or ct.startswith("text/")):
        return resp
    body = resp.get_data()
    if len(body) < GZIP_MIN:
        return resp
    packed = gzip.compress(body, 6)
    if len(packed) >= len(body):
        return resp
    resp.set_data(packed)
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Content-Length"] = str(len(packed))
    resp.headers.add("Vary", "Accept-Encoding")
    return resp


@app.route("/api/preview")
@protected
def api_preview():
    """Redirect to the rendered ad -- the creative as it actually runs.

    A redirect rather than an embedded URL: Meta's preview link is signed and expires, so
    resolving it at click time is the only way the link is never dead. It also keeps a
    credential-bearing URL out of a page that lists two thousand ads.
    """
    brand, err, _full = _gate(request.args.get("k", ""),
                              request.args.get("brand", C.DEFAULT_BRAND))
    if err:
        return err
    ad = (request.args.get("ad") or "").strip()
    if not ad.isdigit():
        return _preview_problem("That is not an ad id.", None)
    fmt = request.args.get("format") or P.PREVIEW_FORMATS[0]
    try:
        url, acct = P.ad_preview(ad, fmt)
    except Exception as e:
        traceback.print_exc()
        return _preview_problem(_friendly(e)["text"], ad)
    # A valid team link must not become a way to read another brand's creatives. The
    # account comes from Meta on the same request, so this cannot be spoofed by the
    # caller -- and an ad in no account this brand owns is refused, not previewed.
    allowed = {a["id"].replace("act_", "") for a in C.brand(brand)["accounts"]}
    if not acct or str(acct).replace("act_", "") not in allowed:
        return _preview_problem(
            "That ad is not in this brand's ad accounts.", None), 403
    if not url:
        return _preview_problem(
            "Meta returned no preview for this ad in that placement. It may render in a "
            "different one, or the creative may have been deleted.", ad)
    resp = redirect(url, code=302)
    # Without this the browser hands business.facebook.com a Referer containing this
    # request's URL -- and this request's URL contains the team's link key. The whole
    # point of a per-team secret link is that it does not travel to third parties.
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _preview_problem(msg, ad):
    """A plain page saying what went wrong, with a way onward. Better than a blank tab."""
    link = (f'<p><a href="https://adsmanager.facebook.com/adsmanager/manage/ads?'
            f'selected_ad_ids={ad}">Open it in Ads Manager instead</a></p>') if ad else ""
    return Response(
        "<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Preview unavailable</title>"
        "<style>body{font:15px/1.55 -apple-system,system-ui,sans-serif;margin:12vh auto;"
        "max-width:34rem;padding:0 1.2rem;color:#1c1e21}h1{font-size:17px}"
        "a{color:#1877f2}</style>"
        f"<h1>Preview unavailable</h1><p>{escape(msg)}</p>{link}",
        mimetype="text/html")


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
