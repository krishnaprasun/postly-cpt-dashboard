"""The hourly ads update, as posted to a Google Chat space.

Transport is a Chat *incoming webhook*: one secret URL per space, POST a JSON body, no
OAuth and no bot to install. That URL is the whole credential, so it lives in the
environment (`CHAT_WEBHOOK`, or `CHAT_WEBHOOK_<BRAND>` to send one brand somewhere else)
and is read per call -- rotating a space's webhook is an env change, never a deploy. It is
never logged and never echoed back by the endpoint, including on failure.

The numbers come from the same cached payload the dashboard serves, so an hourly push
costs Meta nothing extra: it reads what the page already built, and on a cold instance it
warms the cache the next visitor would have paid for.
"""

import json
import os
import urllib.error
import urllib.request

import config as C
import history as H

TIMEOUT = 20
# Hour-on-hour deltas need a memory, and this process does not have one -- the free
# instance sleeps. So each push appends its own numbers to a per-day doc in the same
# history store everything else uses, and the next push reads the last point back.
STATE_NS = "chatpush"


def webhook(brand=None):
    """The space this brand posts to. One space for everything unless overridden."""
    if brand:
        one = os.environ.get(f"CHAT_WEBHOOK_{brand.upper()}", "").strip()
        if one:
            return one
    return os.environ.get("CHAT_WEBHOOK", "").strip()


# ---- formatting -------------------------------------------------------------
def rs(v):
    """Money at a glance: lakh and crore, because that is how these numbers are read."""
    v = float(v or 0)
    if v >= 1e7:
        return f"₹{v/1e7:.2f}Cr"
    if v >= 1e5:
        return f"₹{v/1e5:.2f}L"
    if v >= 1000:
        return f"₹{v/1000:.1f}k"
    return f"₹{v:,.0f}"


def rs0(v):
    """A per-unit price -- CPT, CPI. Never abbreviated: the digits are the point."""
    return f"₹{float(v or 0):,.0f}"


def num(v):
    v = float(v or 0)
    return f"{v/1000:.1f}k" if v >= 10000 else f"{v:,.0f}"


def signed(v, fmt):
    return ("+" if v >= 0 else "−") + fmt(abs(v))


# ---- one brand --------------------------------------------------------------
def figures(brand, data):
    """The numbers the message needs, pulled out of a built payload.

    Trials are the pro-rata Meta total -- the same figure the summary tiles show, which
    is measured Meta plus Meta's share of the trials Branch could not name. Dividing
    spend by the row sum instead is what made Funda read three times its true CPT.
    """
    B = C.brand(brand)
    ev = (data.get("events") or ["t101"])[0]
    ch = (data.get("channels") or {}).get(ev) or {}
    pr = (data.get("prorata") or {}).get(ev) or {}
    comb = data.get("combined") or {}
    inst = data.get("installs") or {}

    trials = pr.get("meta")
    if trials is None:
        trials = ch.get("meta") or 0.0
    installs = inst.get("meta")
    if installs is None:
        installs = comb.get("inst") or 0.0
    spend = float(comb.get("spend") or 0.0)
    budget = float(comb.get("budget") or 0.0)

    # An account holding real budget and spending essentially nothing is the failure this
    # update exists to catch: on 28 Aug three Funda accounts went to near zero against
    # ~20 lakh a day of live budget and nothing said so for a day.
    stalled = [a for a in (data.get("accounts") or [])
               if float(a.get("budget") or 0) >= 10000
               and float(a.get("spend") or 0) < 0.02 * float(a.get("budget") or 0)]

    return {
        "brand": brand,
        "label": data.get("brand_label") or brand,
        "event": (data.get("event_labels") or {}).get(ev, "Trials"),
        "spend": spend,
        "trials": float(trials or 0.0),
        "installs": float(installs or 0.0),
        "budget": budget,
        "adsets": int(comb.get("active_adsets") or 0),
        "target": B.get("cpt_target"),
        "trials_error": data.get("trials_error"),
        "degraded": data.get("degraded") or [],
        "throttled": bool((data.get("rate_limit") or {}).get("active")),
        "budgets_known": data.get("budgets_known", True),
        "stalled": [{"name": a.get("name"), "spend": float(a.get("spend") or 0),
                     "budget": float(a.get("budget") or 0)} for a in stalled],
        "as_of": data.get("meta_as_of") or "",
    }


def brand_block(f, prev):
    """One brand's two lines, plus its own warnings."""
    cpt = f["spend"] / f["trials"] if f["trials"] else 0
    cpi = f["spend"] / f["installs"] if f["installs"] else 0
    tgt = f["target"]
    # Green under target, amber up to half again, red beyond. Colour is a nudge, not a
    # verdict -- early in the day a thin trial count moves CPT around on its own.
    mark = "" if not tgt or not cpt else (
        " \U0001f7e2" if cpt <= tgt else " \U0001f7e1" if cpt <= 1.5 * tgt
        else " \U0001f534")
    head = (f"*{f['label']}* — {rs(f['spend'])} · "
            f"{num(f['trials'])} {f['event'].lower()} · CPT *{rs0(cpt)}*"
            + (f" (target {rs0(tgt)}){mark}" if tgt else ""))

    bits = []
    d_sp = f["spend"] - prev.get("spend", 0) if prev else 0
    d_tr = f["trials"] - prev.get("trials", 0) if prev else 0
    # "+₹0, +0 trials" is not a change, it is the same message twice -- which is what two
    # pushes inside one cache window produce. Say nothing rather than say nothing happened.
    if prev and (abs(d_sp) >= 1 or abs(d_tr) >= 1):
        hour = f"{signed(d_sp, rs)}, {signed(d_tr, num)} {f['event'].lower()}"
        # CPT of the hour just gone, which is the number that actually moves first.
        if d_tr >= 1 and d_sp > 0:
            hour += f" — CPT {rs0(d_sp/d_tr)} this hour"
        bits.append(hour)
    bits.append(f"{num(f['installs'])} installs, CPI {rs0(cpi)}")
    if f["budget"] and f["budgets_known"]:
        bits.append(f"{100*f['spend']/f['budget']:.0f}% of {rs(f['budget'])} budget")
    bits.append(f"{f['adsets']:,} live ad sets")
    lines = [head, "   " + " · ".join(bits)]

    for a in f["stalled"]:
        lines.append(f"   ⚠️ *{a['name']}* has spent {rs(a['spend'])} against "
                     f"{rs(a['budget'])} of live budget")
    if f["trials_error"]:
        lines.append(f"   ⚠️ trial counts unavailable — {f['trials_error']}")
    if f["degraded"]:
        lines.append("   ⚠️ partial data: " + ", ".join(str(d) for d in f["degraded"]))
    if f["throttled"]:
        lines.append("   ⚠️ Meta is throttling this account — figures may lag")
    return "\n".join(lines)


def compose(rows, prev_by_brand, when, link=None):
    """The whole message. `rows` are figures() dicts, in the order they should read."""
    head = f"*Ads — today so far* · {when} IST"
    if prev_by_brand:
        head += "  _(change is since the last update)_"
    # A blank line between brands: on a phone these blocks are three or four wrapped
    # lines each, and run together they read as one paragraph about nothing.
    body = "\n\n".join(brand_block(f, prev_by_brand.get(f["brand"])) for f in rows)
    out = [head, "", body]
    if link:
        out += ["", f"<{link}|Open the dashboard>"]
    return "\n".join(out)


# ---- state ------------------------------------------------------------------
def last_point(day):
    """The most recent push's numbers, {brand: {...}}, or {} if there is none today."""
    if not H.available():
        return {}
    try:
        got, ok = H.get_day_raw(STATE_NS, day)
    except Exception:
        return {}
    pts = (got or {}).get("points") or [] if ok else []
    return (pts[-1] or {}).get("brands") or {} if pts else {}


def record(day, when, rows):
    """Append this push to today's doc. Best effort: a failed write must not lose a post."""
    if not H.available():
        return False
    try:
        got, ok = H.get_day_raw(STATE_NS, day)
        doc = got if (ok and isinstance(got, dict)) else {}
        pts = doc.get("points") or []
        pts.append({"at": when,
                    "brands": {f["brand"]: {"spend": round(f["spend"], 2),
                                            "trials": round(f["trials"], 1),
                                            "installs": round(f["installs"], 1)}
                               for f in rows}})
        # A day is 24 pushes; the cap is only there so a runaway scheduler cannot grow
        # one document without bound.
        doc["points"] = pts[-64:]
        return bool(H.put_agg(STATE_NS, day, doc))
    except Exception:
        return False


# ---- send -------------------------------------------------------------------
def send(text, url):
    """POST to the space. Returns (ok, detail) -- the URL is never part of `detail`."""
    if not url:
        return False, "no webhook configured"
    req = urllib.request.Request(
        url, data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=UTF-8"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return True, f"http {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"http {e.code}: {(e.read() or b'')[:200].decode('utf-8', 'replace')}"
    except Exception as e:
        return False, str(e)[:200]
