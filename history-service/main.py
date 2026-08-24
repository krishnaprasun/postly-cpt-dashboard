#!/usr/bin/env python3
"""Ads history store — a thin authenticated front door to one GCS bucket.

Why this exists rather than the dashboard talking to GCS directly: the org policy
`iam.disableServiceAccountKeyCreation` forbids downloadable service-account keys, and
Render cannot do keyless Google auth (it issues no OIDC token to identify itself). So no
Google credential can safely live on Render at all. This service runs on Cloud Run with
*ambient* credentials, and Render authenticates to it with a bearer token that is not a
Google credential and grants nothing beyond this one bucket.

It stores days and returns days. It deliberately knows nothing about CPT, joins or
rollups — that logic lives in the dashboard and must have exactly one home.
"""
import gzip
import hmac
import json
import os

from flask import Flask, Response, jsonify, request
from google.cloud import storage

BUCKET = os.environ["HISTORY_BUCKET"]
TOKEN = os.environ["HISTORY_TOKEN"]
PREFIX = os.environ.get("HISTORY_PREFIX", "v1")

app = Flask(__name__)
_gcs = storage.Client()
_bucket = _gcs.bucket(BUCKET)


def _authed():
    got = request.headers.get("Authorization", "")
    want = "Bearer " + TOKEN
    # compare_digest, not ==: a plain comparison leaks the token a character at a time
    # to anyone who can time the responses.
    return hmac.compare_digest(got, want)


def _path(brand, date):
    # Brand and date are interpolated into an object path, so they are validated rather
    # than trusted: "../" in a brand name would otherwise walk out of the prefix.
    if not brand.isalnum() or len(date) != 10 or date.count("-") != 2:
        raise ValueError("bad brand or date")
    return f"{PREFIX}/{brand}/{date}.json.gz"


def _read(brand, date):
    blob = _bucket.blob(_path(brand, date))
    if not blob.exists():
        return None
    return json.loads(gzip.decompress(blob.download_as_bytes()))


@app.before_request
def _gate():
    if request.path == "/healthz":
        return None
    if not _authed():
        return jsonify({"error": "unauthorized"}), 401


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "bucket": BUCKET})


@app.get("/v1/history")
def history():
    """Days for one brand. `aggregate=1` sums them into the shape the dashboard wants.

    Aggregating here rather than in the dashboard is not logic leaking across the
    boundary — it is only addition, and it turns a 30-day answer from tens of thousands
    of per-day rows into a couple of thousand per-ad rows. That is the difference
    between an 8 MB response and a small one, over a link between two clouds.
    """
    brand = request.args.get("brand", "")
    dates = [d for d in request.args.get("dates", "").split(",") if d]
    if not brand or not dates:
        return jsonify({"error": "brand and dates are required"}), 400
    if len(dates) > 400:
        return jsonify({"error": "too many dates"}), 400

    days, missing = {}, []
    for d in dates:
        try:
            got = _read(brand, d)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if got is None:
            missing.append(d)
        else:
            days[d] = got

    if request.args.get("aggregate") != "1":
        body = {"days": days, "missing": missing}
    else:
        meta, branch = {}, {}
        for d in sorted(days):
            for acct, rows in (days[d].get("meta") or {}).items():
                # one row per ad per day -> one row per ad, spend summed
                keep = meta.setdefault(acct, {})
                for r in rows:
                    aid = r.get("ad_id")
                    if not aid:
                        continue
                    cur = keep.get(aid)
                    if cur is None:
                        cur = keep[aid] = dict(r)
                        cur["spend"] = 0.0
                    cur["spend"] += float(r.get("spend") or 0)
                    # A renamed ad keeps whichever name was most recent in the window.
                    for f in ("ad_name", "adset_name", "campaign_name",
                              "adset_id", "campaign_id"):
                        if r.get(f):
                            cur[f] = r[f]
            for ev, by_name in (days[d].get("branch") or {}).items():
                tgt = branch.setdefault(ev, {})
                for name, n in by_name.items():
                    tgt[name] = tgt.get(name, 0) + n
        body = {"meta": {a: list(v.values()) for a, v in meta.items()},
                "branch": branch, "days": sorted(days), "missing": missing}

    # gzip on the way out: this is cross-cloud and the payload is highly repetitive JSON.
    raw = gzip.compress(json.dumps(body).encode(), 6)
    return Response(raw, mimetype="application/json",
                    headers={"Content-Encoding": "gzip"})


@app.put("/v1/history")
def put_day():
    brand, date = request.args.get("brand", ""), request.args.get("date", "")
    try:
        path = _path(brand, date)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "body must be a JSON object"}), 400
    blob = _bucket.blob(path)
    data = gzip.compress(json.dumps(payload).encode(), 6)
    blob.upload_from_string(data, content_type="application/json")
    return jsonify({"ok": True, "path": path, "bytes": len(data)})


@app.get("/v1/have")
def have():
    """Which dates are already stored for a brand — for the backfill to skip them."""
    brand = request.args.get("brand", "")
    if not brand.isalnum():
        return jsonify({"error": "bad brand"}), 400
    got = sorted(b.name.split("/")[-1][:10]
                 for b in _gcs.list_blobs(BUCKET, prefix=f"{PREFIX}/{brand}/"))
    return jsonify({"brand": brand, "dates": got})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
