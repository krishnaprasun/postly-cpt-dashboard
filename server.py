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
                   request, session, url_for)

import chat as CH
import config as C
import gauth as GA
import users as U
import history as H
import kv as KV
import postly_cpt as P

app = Flask(__name__)
# Jinja caches templates when debug is off; the UI is a single file that gets edited
# in place, so pick up changes on reload rather than needing a server restart.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# Control layer (ads-ops write actions) lives in its own module `control.py` and its own
# `static/control.js`; it registers here as a blueprint so the read dashboard and its data
# pipeline are untouched. Wrapped: control is optional and must never block the read app.
try:
    from control import bp as _control_bp
    app.register_blueprint(_control_bp)
except Exception:
    traceback.print_exc()

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

# Signs the session cookie that Google sign-in leaves behind. A generated fallback keeps
# the app running without it, at the cost of signing everyone out whenever an instance
# restarts -- which on the free plan is often. Set SESSION_SECRET the moment sign-in is
# switched on.
app.secret_key = os.environ.get("SESSION_SECRET", "") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Off only where there is no TLS to require: a local run on 127.0.0.1. Anywhere else
    # a cookie that would travel in the clear is worse than no cookie.
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_INSECURE", "") != "1",
    PERMANENT_SESSION_LIFETIME=GA.MAX_AGE)

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


def _caps(key):
    """What this request may see: the link it carries, or failing that whoever is signed in.

    An EXPLICIT link wins over the session — a team link opened by someone signed in as
    the owner still shows that team's brand and nothing more, because the link is a
    statement about the view and two credentials on one request should narrow, never widen.

    The empty key is not such a statement. Under ROOT_OPEN it is merely the door being
    unlocked, and letting it win made a signed-in admin read as an anonymous
    visitor: no name in the header, and no Access link to the page they are the only
    person allowed to open.
    """
    key = (key or "").strip()
    # Where sign-in is configured, "no links are configured" means links are RETIRED —
    # never "open to everyone", which is what config.link_caps answers and how this app
    # behaved before links existed. It answers it for ANY key, so this check has to come
    # BEFORE link_caps is consulted at all: guarding only the empty-key path left
    # /b/<anything> serving the whole dashboard, which is exactly what happened.
    links_dead = GA.on() and not C.LINKS_ON
    if key and not links_dead:
        caps = C.link_caps(key)
        if caps is not None:
            return caps
    signed = GA.session_caps(session)
    if signed:
        return signed
    return None if links_dead else C.link_caps(key)


def _gate(key, brand):
    """(brand, error_response, full). Narrows the request to what the key allows.

    `full` gates the two things that make the app SPEND — a hard roster re-read and a
    longevity recompute. Hiding those buttons in the page is presentation; this is the
    part that actually holds, because a hidden button is one edited URL away.
    """
    caps = _caps(key)
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
        # Same argument, one level up: a payload built before a field existed cannot grow
        # it, and the page would render blanks off it until the artifact aged out. Cheaper
        # to rebuild once than to serve a shape the shipped code no longer matches.
        if saved and saved.get("payload_shape") != P.PAYLOAD_SHAPE:
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
# ------------------------------------------------------------ google sign-in ---
# A page, three routes and no user table. Google says who you are; GOOGLE_AUTH_MAP says
# which brands that address may see. Switched on by setting the client id and secret —
# with them unset, none of this is reachable and the app is exactly what it was.
# The sign-in page is the whole product for anyone who has not signed in yet — a stranger,
# a colleague on their first day, or you on a phone. It gets a card, a mark and the
# dashboard's own palette rather than three lines of text adrift in the viewport.
SIGNIN_CSS = """
:root{--ink:#1A1C2E;--muted:#787E91;--faint:#A2A7B6;--line:#E6E3DA;--line2:#F0EDE4;
  --white:#fff;--bg:#FDFCF7;--panel:#F7F5EE;--accent:#20A75D;--accent-dk:#127A42;
  --accent-lt:#EAF7F0;--bad:#B3261E;--badlt:#FCEEEC}
*{box-sizing:border-box}
/* Light only, on purpose. This page is the front door and it should look the same to
   everyone who arrives at it, whatever their laptop is set to. */
html{color-scheme:light}
/* Flex, not grid, to centre this. A grid track under `place-items:center` is sized to
   the item's max-content, so a percentage width on the card resolves against that rather
   than against the viewport. Flex has no such trap. */
body{font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);
  color:var(--ink);margin:0;min-height:100vh;display:flex;align-items:center;
  justify-content:center;padding:24px;-webkit-font-smoothing:antialiased}
.shell{width:100%;max-width:720px;background:var(--white);border:1px solid var(--line);
  border-radius:18px;overflow:hidden;display:grid;grid-template-columns:1fr 1fr;
  box-shadow:0 1px 2px rgba(26,28,46,.04),0 18px 50px -20px rgba(26,28,46,.18)}
.about{background:var(--panel);border-right:1px solid var(--line);padding:34px 30px}
.mark{width:42px;height:42px;border-radius:12px;background:var(--ink);color:var(--white);
  display:grid;place-items:center;margin:0 0 16px}
h1{font-size:19px;margin:0 0 8px;letter-spacing:-.015em}
.blurb{color:var(--muted);font-size:13.5px;margin:0 0 20px;line-height:1.55}
ul{list-style:none;margin:0;padding:0}
li{display:flex;gap:9px;align-items:flex-start;font-size:13px;color:var(--ink);
  padding:6px 0;line-height:1.45}
li svg{flex:none;margin-top:3px;color:var(--accent)}
.side{padding:34px 30px;display:flex;flex-direction:column;justify-content:center}
h2{font-size:16px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13.5px;margin:0 0 20px}
.note{border-radius:10px;padding:9px 13px;margin:0 0 16px;font-size:13px;line-height:1.5}
.note.err{color:var(--bad);background:var(--badlt)}
.note.ok{color:var(--accent-dk);background:var(--accent-lt)}
.note.info{color:var(--muted);background:var(--panel)}
a.g{display:flex;align-items:center;justify-content:center;gap:10px;text-decoration:none;
  background:var(--white);color:var(--ink);border:1px solid var(--line);border-radius:11px;
  padding:12px 18px;font-weight:600;font-size:14.5px;transition:.14s}
a.g:hover{border-color:var(--accent);background:var(--accent-lt);color:var(--accent-dk)}
.foot{color:var(--faint);font-size:12px;margin:18px 0 0;line-height:1.55}
@media(max-width:620px){
  .shell{grid-template-columns:1fr}
  .about{border-right:0;border-bottom:1px solid var(--line);padding:26px 24px 22px}
  .side{padding:24px}
  ul{display:none}          /* the pitch is for a desktop first visit, not a phone */
}
"""
# A rising bar chart, drawn rather than picked from a brand: this tool serves three of
# them and wearing one brand's logo on the way in would name the wrong owner.
# currentColor, not #fff -- see .mark, which owns the pairing.
MARK = ('<svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" fill="none">'
        '<rect x="3" y="13" width="4" height="8" rx="1.4" fill="currentColor" opacity=".5"/>'
        '<rect x="10" y="8" width="4" height="13" rx="1.4" fill="currentColor" opacity=".75"/>'
        '<rect x="17" y="3" width="4" height="18" rx="1.4" fill="currentColor"/></svg>')
TICK = ('<svg width="13" height="13" viewBox="0 0 16 16" aria-hidden="true">'
        '<path d="M2.5 8.5l3.5 3.5 7.5-8" fill="none" stroke="currentColor"'
        ' stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/></svg>')
# What the tool is, in the three lines someone needs before deciding this is the right
# tab. Deliberately no brand names and no figures: this page is public.
SIGNIN_POINTS = ("Spend, trials and cost per trial, updated through the day",
                 "Meta and Google side by side, or blended",
                 "Every campaign, ad set and ad, with daily history")

# Google's mark, inline: the CSP on this app allows no external images, and a sign-in
# button with a broken icon looks like a phishing page.
GOOGLE_G = (
    '<svg width="17" height="17" viewBox="0 0 48 48" aria-hidden="true">'
    '<path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9 3.6l6.7-6.7C35.6 2.6 30.1 0 24 0'
    ' 14.6 0 6.4 5.4 2.5 13.2l7.8 6.1C12.2 13.2 17.6 9.5 24 9.5z"/>'
    '<path fill="#4285F4" d="M46.1 24.6c0-1.6-.1-2.8-.4-4.1H24v7.8h12.7c-.3 2.1-1.6 5.3-4.7'
    ' 7.4l7.6 5.9c4.5-4.2 7.1-10.3 7.1-17z"/>'
    '<path fill="#FBBC05" d="M10.3 28.7c-.5-1.5-.8-3-.8-4.7s.3-3.2.8-4.7l-7.8-6.1C.9 16.5 0'
    ' 20.1 0 24s.9 7.5 2.5 10.8l7.8-6.1z"/>'
    '<path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.6-5.9c-2 1.4-4.8 2.4-8.3 2.4'
    '-6.4 0-11.8-3.7-13.7-9.8l-7.8 6.1C6.4 42.6 14.6 48 24 48z"/></svg>')


def _safe_next(path):
    """Where to land after signing in. A path on this site, and never an /auth/ one.

    The sign-in page remembers where you were so it can send you back — and after a
    logout that was /auth/logout, so signing in redirected straight back into logging
    out. It looked like sign-in was broken; it was sign-in working perfectly and being
    aimed at the door.
    """
    p = (path or "/").strip()
    if not p.startswith("/") or p.startswith("//") or p.startswith("/auth/"):
        return "/"
    return p


def _signin_page(note="", status=200, kind="err"):
    """The way in, and for anyone not yet signed in it is the whole product.

    `kind` separates a refusal from a plain statement of fact — signing out is not an
    error, and colouring it like one tells people something went wrong.
    """
    doms = GA.domains()
    who = (f"Use your <b>{escape(doms[0])}</b> account."
           if len(doms) == 1 else "Use your work Google account.")
    login = url_for("auth_login",
                    next=_safe_next(request.args.get("next")
                                    or request.full_path.rstrip("?")))
    points = "".join(f"<li>{TICK}<span>{escape(p)}</span></li>" for p in SIGNIN_POINTS)
    return Response(
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<title>Sign in \u00b7 Ads Performance</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<style>{SIGNIN_CSS}</style>"
        '<div class="shell">'
        '<div class="about">'
        f'<div class="mark">{MARK}</div>'
        "<h1>Ads Performance</h1>"
        '<p class="blurb">One live view of what the ads are spending and what they are '
        'bringing back \u2014 read straight from Meta, Google and Branch.</p>'
        f"<ul>{points}</ul>"
        "</div>"
        '<div class="side">'
        "<h2>Sign in</h2>"
        f'<p class="sub">{who}</p>'
        + (f'<div class="note {kind}">{escape(note)}</div>' if note else "")
        + f'<a class="g" href="{escape(login)}">{GOOGLE_G}Sign in with Google</a>'
        + '<p class="foot">Access is by invitation. If you cannot get in, ask whoever '
          'runs this dashboard to add you.</p>'
        "</div></div>",
        status, mimetype="text/html")


@app.route("/auth/login")
def auth_login():
    if not GA.on():
        return redirect("/")
    state = GA.new_state()
    session["g_state"] = state
    # Only a path on this site, and never an /auth/ one. An open redirect on a sign-in
    # route is how a phishing link borrows someone else's domain.
    session["g_next"] = _safe_next(request.args.get("next"))
    return redirect(GA.start(request, state))


@app.route("/auth/callback")
def auth_callback():
    if not GA.on():
        return redirect("/")
    if request.args.get("error"):
        return _signin_page("Sign-in was cancelled.", 200, kind="info")
    want = session.pop("g_state", None)
    if not want or request.args.get("state") != want:
        # Either a stale tab or a forged callback, and the honest answer to both is to
        # start again rather than to guess which.
        return _signin_page("That sign-in link had expired. Try again.", 400)
    caps, err = GA.finish(request, request.args.get("code", ""))
    if err:
        return _signin_page(err, 403)
    session.permanent = True
    session["g_email"] = caps["email"]
    session["g_at"] = int(time.time())
    return redirect(_safe_next(session.pop("g_next", "/")))


@app.route("/auth/logout")
def auth_logout():
    for k in ("g_email", "g_at", "g_state", "g_next"):
        session.pop(k, None)
    return _signin_page("You are signed out.", kind="info")


@app.route("/auth/whoami")
def auth_whoami():
    """What this browser is, as the server sees it. For checking a rollout, and for
    anyone wondering why they can see one brand and not another."""
    caps = _caps(request.args.get("k", ""))
    return jsonify({"signed_in": bool(caps and caps.get("email")),
                    "email": (caps or {}).get("email", ""),
                    "brands": (caps or {}).get("brands", []),
                    "full": bool(caps and caps.get("full")),
                    "google_auth": GA.on(),
                    "domains": GA.domains()})


# ---------------------------------------------------------------- user admin ---
# Managing who gets in is a thing the person running this does at 9pm from a phone, so it
# is a page in the app rather than an environment variable and a redeploy. Only a super
# admin can open it, and only an admin can save.
def _me():
    """The signed-in caps for this request, or None. Sessions only -- a link is not a
    person and must never be able to edit who the people are."""
    return GA.session_caps(session)


def _require_super():
    me = _me()
    if not me:
        return None, (jsonify({"error": "Sign in first."}), 401)
    if me.get("role") != "super":
        return None, (jsonify({"error": "Only an admin can manage access."}), 403)
    return me, None


@app.route("/api/users", methods=["GET"])
def api_users():
    me, err = _require_super()
    if err:
        return err
    rows, ok = U.listing()
    kv_ok, kv_why = KV.ping()
    return jsonify({"users": rows, "store_ok": ok, "brands": list(C.BRANDS),
                    # Which store answered, so an outage is diagnosable from the page
                    # rather than from a log nobody is looking at.
                    "stores": {"kv": kv_ok, "kv_detail": kv_why, "history": H.available()},
                    "brand_labels": {k: v["label"] for k, v in C.BRANDS.items()},
                    "me": me["email"],
                    "store": bool(H.available())})


@app.route("/api/users", methods=["PUT"])
def api_users_save():
    me, err = _require_super()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    rows = body.get("users")
    if not isinstance(rows, list):
        return jsonify({"error": "Expected a list of users."}), 400
    # Bootstrap supers ARE kept, so their display name can be set here; U.save forces
    # their role back to super, so the list cannot demote them however it is edited.
    boot = set(U.supers())
    if not any(r.get("role") == "super" for r in rows) and not boot:
        return jsonify({"error": "That would leave nobody able to manage access."}), 400
    ok, msg = U.save(rows, me["email"])
    if not ok:
        return jsonify({"error": msg}), 502
    fresh, store_ok = U.listing()
    return jsonify({"saved": True, "users": fresh, "store_ok": store_ok})


@app.route("/admin/users")
def admin_users():
    me = _me()
    if not me:
        return (_signin_page("Sign in to manage access.", kind="info")
                if GA.on() else redirect("/"))
    if me.get("role") != "super":
        return Response("<!doctype html><meta charset=utf-8><title>Access</title>"
                        f"<style>{SIGNIN_CSS}</style><div class=card><h1>Access</h1>"
                        "<p>Only an admin can manage who gets in.</p>"
                        "<p class=foot><a href='/'>Back to the dashboard</a></p></div>",
                        403, mimetype="text/html")
    return render_template("users.html", me=me["email"],
                           roles=[{"key": k, "label": U.ROLE_LABELS[k]} for k in U.ROLES],
                           brands=[{"key": k, "label": C.BRANDS[k]["label"]}
                                   for k in C.BRANDS])


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
    caps = _caps(key)
    allowed = caps["brands"] if caps else None
    if allowed is None and GA.on():
        return _signin_page()
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
        # Export is its own right, not a shade of `full`. Downloading a table costs
        # nothing and answers a real need; forcing a Meta roster re-read spends the one
        # budget everyone looking at this page shares. A link, which is nobody in
        # particular, gets neither.
        can_export=caps.get("export", caps["full"]),
        # Who is looking, when that is a person rather than a link. The page shows it
        # beside a sign-out, so a shared screen is never a mystery.
        signed_in=caps.get("email", ""),
        # Whether signing in is even possible here. While the front door is open, being
        # signed in and not being signed in look exactly alike — which is how someone can
        # believe they signed in, wonder where their Access link went, and be right to.
        google_auth=GA.on(),
        role_label=U.ROLE_LABELS.get(caps.get("role", ""), ""),
        is_super=caps.get("role") == "super",
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


@app.route("/api/chat/hourly", methods=["POST", "GET"])
def api_chat_hourly():
    """Post the hourly ads update to a Google Chat space.

    Token-gated like every other scheduler endpoint, and here for two reasons rather than
    one: it reaches OUT of this app to a third party, and an open URL that posts into the
    team's space is a megaphone anyone can pick up.

    It reads the same cached payload the page serves, so the push costs Meta nothing on a
    warm instance and warms a cold one.

    ?brand=funda   just one brand (default: all of them, in config order)
    ?dry=1         compose and return the message WITHOUT posting
    """
    want = H.TOKEN
    if not want or not hmac.compare_digest(request.headers.get("Authorization", ""),
                                           "Bearer " + want):
        return jsonify({"error": "unauthorized"}), 401
    brands = [request.args.get("brand")] if request.args.get("brand") else list(C.BRANDS)
    bad = [b for b in brands if b not in C.BRANDS]
    if bad:
        return jsonify({"error": f"unknown brand(s): {bad}"}), 400
    dry = request.args.get("dry") == "1"

    today = P.today_ist()
    rows, failed = [], []
    for b in brands:
        try:
            # Google is its own pull, as it is for the page — and its own failure. A
            # Google outage must cost the message its Google line, never the whole push.
            try:
                goog = P.google_window(b, today, today)
            except Exception:
                traceback.print_exc()
                goog = None
            rows.append(CH.figures(b, get_data(today, today, b), goog))
        except Exception as e:
            traceback.print_exc()
            failed.append({"brand": b, "error": str(e)[:200]})
    if not rows:
        return jsonify({"sent": False, "error": "no brand produced figures",
                        "failed": failed}), 502

    pts = CH.day_points(today)
    when = datetime.now(P.IST).strftime("%d %b, %-I:%M %p")
    text = CH.compose(rows, CH.last_point(pts), when, points=pts,
                      link=os.environ.get("CHAT_LINK", "").strip() or None)
    # A brand that failed is said out loud rather than silently missing: a shorter
    # message that looks complete is the one way this can mislead.
    if failed:
        text += "\n\n⚠️ No figures for " + ", ".join(f["brand"] for f in failed)
    if dry:
        return jsonify({"sent": False, "dry": True, "text": text,
                        "configured": bool(CH.webhook()), "failed": failed})

    ok, detail = CH.send(text, CH.webhook())
    # Only a delivered message becomes the baseline. Recording a failed push would make
    # the next update's "since the last update" cover an hour nobody ever saw.
    if ok:
        CH.record(today, when, rows)
    return jsonify({"sent": ok, "detail": detail, "at": when,
                    "brands": [f["brand"] for f in rows], "failed": failed}), (
        200 if ok else 502)


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@app.route("/healthz")
def healthz():
    """Public and cheap on purpose — Render's health check must not trigger a pull.

    It also names the commit it is running, because "the URL answered 200" does not mean
    the deploy landed: during a roll the OLD instance is still serving, so a health check
    alone cannot tell a finished deploy from one that has not started. Render injects
    RENDER_GIT_COMMIT; the deploy workflow compares it with the sha it pushed and only
    then calls the deploy done. Empty off Render, which is a fine answer locally.
    """
    return jsonify({"ok": True, "ist": P.today_ist(), "cached_windows": len(_cache),
                    "commit": os.environ.get("RENDER_GIT_COMMIT", "")[:40]})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    print(f"\n  Postly CPT dashboard  ->  http://127.0.0.1:{port}"
          f"{'  (password required)' if ADMIN_PASS else ''}\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
