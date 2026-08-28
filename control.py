"""Control layer for the CPT dashboard — self-contained and additive.

This module is owned by the ads-ops side, NOT the data owner. It adds the write
controls (pause / budget / scale / execute-flags / build-triggers) behind an
id+password gate (Google SSO added later). It imports none of the data code and
registers as a Flask blueprint, so the read dashboard and its data pipeline are
completely untouched — `server.py` gains exactly one line to load this.

Nothing secret lives here. Credentials come from env:
  CONTROL_USER / CONTROL_PASS   the shared admin login (control stays locked if unset)
  CONTROL_SECRET (or SECRET_KEY) signs the session cookie
The public repo is safe because this file carries no secrets.
"""
import functools
import hmac
import os
import time

from flask import Blueprint, jsonify, make_response, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

bp = Blueprint("control", __name__)

CONTROL_USER = os.environ.get("CONTROL_USER", "admin")
CONTROL_PASS = os.environ.get("CONTROL_PASS", "")            # empty => control disabled
_SECRET = (os.environ.get("CONTROL_SECRET")
           or os.environ.get("SECRET_KEY")
           or "dev-insecure-secret-change-me")
COOKIE = "cpt_ctrl"
MAX_AGE = 12 * 3600                                          # 12h session
_ser = URLSafeTimedSerializer(_SECRET, salt="cpt-control-v1")


# ------------------------------------------------------------------ session
def _issue(user):
    return _ser.dumps({"u": user, "t": int(time.time())})


def _verify(token):
    try:
        return (_ser.loads(token, max_age=MAX_AGE) or {}).get("u")
    except (BadSignature, SignatureExpired, Exception):
        return None


def current_user():
    tok = request.cookies.get(COOKIE)
    return _verify(tok) if tok else None


def control_required(fn):
    """Gate for every write endpoint added in later phases."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not current_user():
            return jsonify({"error": "control-auth-required"}), 401
        return fn(*a, **kw)
    return wrapper


def _credentials_ok(user, pw):
    # constant-time; and a login is impossible until CONTROL_PASS is set on Render
    return (bool(CONTROL_PASS)
            and hmac.compare_digest(user or "", CONTROL_USER)
            and hmac.compare_digest(pw or "", CONTROL_PASS))


# ------------------------------------------------------------------ routes
@bp.route("/api/control/status")
def status():
    u = current_user()
    return jsonify({"authed": bool(u), "user": u, "enabled": bool(CONTROL_PASS)})


@bp.route("/api/control/login", methods=["POST"])
def login():
    if not CONTROL_PASS:
        return jsonify({"error": "Control isn't enabled yet — no credential is set."}), 503
    body = request.get_json(silent=True) or request.form
    user = (body.get("id") or "").strip()
    pw = body.get("pass") or ""
    if not _credentials_ok(user, pw):
        time.sleep(0.6)                                     # slow brute force
        return jsonify({"error": "Wrong id or password."}), 401
    resp = make_response(jsonify({"authed": True, "user": user}))
    resp.set_cookie(COOKIE, _issue(user), max_age=MAX_AGE, httponly=True,
                    secure=request.is_secure, samesite="Lax")
    return resp


@bp.route("/api/control/logout", methods=["POST"])
def logout():
    resp = make_response(jsonify({"authed": False}))
    resp.delete_cookie(COOKIE)
    return resp
