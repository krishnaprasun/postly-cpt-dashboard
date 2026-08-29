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
def figures(brand, data, goog=None):
    """The numbers the message needs, split by channel, out of the built payloads.

    Meta's trials are the PRO-RATA total -- measured Meta plus Meta's share of the trials
    Branch could not name -- because that is what the dashboard's own summary shows, and
    dividing spend by the row sum instead is what once made Funda read three times its
    true CPT. Google's are its window total, Branch-attributed, exactly as the Google tab
    reads them. Adding those two is the same blend the Both-channels view does.

    `goog` may be None, or may carry no usable trial count. That is UNKNOWN, never zero:
    a blend that quietly dropped Google's side would read as a real, smaller number.
    """
    B = C.brand(brand)
    ev = (data.get("events") or ["t101"])[0]
    ch = (data.get("channels") or {}).get(ev) or {}
    pr = (data.get("prorata") or {}).get(ev) or {}
    comb = data.get("combined") or {}
    inst = data.get("installs") or {}

    m_tr = pr.get("meta")
    if m_tr is None:
        m_tr = ch.get("meta") or 0.0
    m_in = inst.get("meta")
    if m_in is None:
        m_in = comb.get("inst") or 0.0
    meta = {"spend": float(comb.get("spend") or 0.0),
            "trials": None if data.get("trials_error") else float(m_tr or 0.0),
            "installs": float(m_in or 0.0),
            "budget": float(comb.get("budget") or 0.0),
            "adsets": int(comb.get("active_adsets") or 0)}

    # Same shape as the page's googTotals(): Branch refusing is not zero trials, and
    # `trial_days` counting nought means the number never arrived.
    g = goog or {}
    gt = g.get("totals") or {}
    g_known = bool(g) and not g.get("trials_error") and (g.get("trial_days") or 0) > 0
    google = {"spend": float(gt.get("spend") or 0.0) if g.get("spend_ok") else None,
              "trials": float(gt.get(ev) or 0.0) if g_known else None,
              "installs": float(gt.get("inst") or 0.0) if g_known else None,
              # Google's own count of the same event, under Google's own attribution --
              # from the click it saw, where Branch counts from the install it saw. On
              # Postly the two differ three-fold, and a single number in that gap reads
              # as a CPT crisis rather than as two ways of counting. Neither is the
              # other's error, so both are shown and neither is averaged away.
              "gconv": float(gt.get("gconv") or 0.0) if g.get("conv_ok") else None,
              "why": g.get("trials_error") or g.get("spend_error") or
                     ("" if g else "Google figures did not load")}

    def add(a, b):
        return None if (a is None or b is None) else a + b

    total = {"spend": add(meta["spend"], google["spend"]),
             "trials": add(meta["trials"], google["trials"]),
             "installs": add(meta["installs"], google["installs"])}

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
        "meta": meta, "google": google, "total": total,
        "target": B.get("cpt_target"),
        "trials_error": data.get("trials_error"),
        "degraded": data.get("degraded") or [],
        "throttled": bool((data.get("rate_limit") or {}).get("active")),
        "budgets_known": data.get("budgets_known", True),
        "stalled": [{"name": a.get("name"), "spend": float(a.get("spend") or 0),
                     "budget": float(a.get("budget") or 0)} for a in stalled],
    }


def cpt(spend, trials):
    """Cost per trial, or None when either side is unknown or there is nothing to divide."""
    if spend is None or not trials:
        return None
    return spend / trials


def mark(v, target):
    """Green under target, amber to half again, red beyond. A nudge, not a verdict --
    early in the day a thin trial count moves CPT around on its own."""
    if not target or not v:
        return ""
    return (" \U0001f7e2" if v <= target
            else " \U0001f7e1" if v <= 1.5 * target else " \U0001f534")


def delta(now, was):
    """"+₹48.0k, +131 trials" for the hour just gone, or "" when there is no hour to
    compare or nothing moved in it. Two pushes inside one cache window produce identical
    numbers, and printing that anyway reads as an hour in which nothing happened rather
    than an hour that was not measured."""
    if not was or now.get("spend") is None or now.get("trials") is None:
        return ""
    d_sp = now["spend"] - (was.get("spend") or 0)
    d_tr = now["trials"] - (was.get("trials") or 0)
    if abs(d_sp) < 1 and abs(d_tr) < 1:
        return ""
    out = f"{signed(d_sp, rs)}, {signed(d_tr, num)} this hour"
    if d_tr >= 1 and d_sp > 0:
        out += f" at {rs0(d_sp/d_tr)}"
    return out


def chan_line(name, cur, was, event, target=None):
    """One channel's line. Unknown prints as a dash and says why -- never as a zero."""
    c = cpt(cur["spend"], cur["trials"])
    sp = "—" if cur["spend"] is None else rs(cur["spend"])
    tr = "—" if cur["trials"] is None else num(cur["trials"])
    bits = [f"*{name}* {sp} · {tr} {event.lower()}",
            f"CPT {rs0(c) if c else '—'}{mark(c, target)}"]
    d = delta(cur, was)
    if d:
        bits.append(d)
    line = "   " + " · ".join(bits)
    gc = cur.get("gconv")
    if gc and cur["spend"]:
        line += f"\n      _Google's own count: {num(gc)} → {rs0(cur['spend']/gc)}_"
    return line


def brand_block(f, prev):
    """One brand: the blended headline, then Meta and Google under it."""
    prev = prev or {}
    ev = f["event"]
    t = f["total"]
    # A channel that did not answer makes the BLEND unknown, not smaller. Rather than
    # print a headline of dashes, fall back to the channel that did answer and say so --
    # a Google outage should cost the message its Google line, not its headline.
    whole = t["spend"] is not None and t["trials"] is not None
    t = t if whole else f["meta"]
    tc = cpt(t["spend"], t["trials"])
    head = (f"*{f['label']}* — CPT *{rs0(tc) if tc else '—'}*"
            + (f" vs {rs0(f['target'])}{mark(tc, f['target'])}" if f["target"] else "")
            + f" · {'—' if t['spend'] is None else rs(t['spend'])}"
            + f" · {'—' if t['trials'] is None else num(t['trials'])} {ev.lower()}"
            + ("" if whole else "  _(Meta only)_"))

    lines = [head,
             chan_line("Meta", f["meta"], prev.get("meta"), ev, f["target"]),
             chan_line("Google", f["google"], prev.get("google"), ev, f["target"])]

    tail = []
    if f["meta"]["budget"] and f["budgets_known"]:
        tail.append(f"{100*f['meta']['spend']/f['meta']['budget']:.0f}%"
                    f" of {rs(f['meta']['budget'])} Meta budget")
    tail.append(f"{f['meta']['adsets']:,} live ad sets")
    ins = t["installs"]
    tail.append(f"{'—' if ins is None else num(ins)} installs"
                + ("" if whole else ", Meta"))
    lines.append("   _" + " · ".join(tail) + "_")

    for a in f["stalled"]:
        lines.append(f"   ⚠️ *{a['name']}* has spent {rs(a['spend'])} against "
                     f"{rs(a['budget'])} of live budget")
    if f["trials_error"]:
        lines.append(f"   ⚠️ Meta trial counts unavailable — {f['trials_error']}")
    if f["google"]["trials"] is None and f["google"]["why"]:
        lines.append(f"   ⚠️ Google not counted — {f['google']['why']}")
    if f["degraded"]:
        lines.append("   ⚠️ partial data: " + ", ".join(str(d) for d in f["degraded"]))
    if f["throttled"]:
        lines.append("   ⚠️ Meta is throttling this account — figures may lag")
    return "\n".join(lines)


def hour_strip(points, rows):
    """The day so far, hour by hour, as the gap between consecutive pushes.

    Stored points are cumulative today-so-far, so an hour is a subtraction. The first
    point of the day has nothing before it -- it covers everything since midnight, which
    is not an hour and is not shown as one.
    """
    if len(points) < 2:
        return ""
    want = [f["brand"] for f in rows]

    def tot(p, k):
        s = 0.0
        for b in want:
            v = ((p.get("brands") or {}).get(b) or {}).get(k)
            if v is None:
                return None
            s += v
        return s

    out = []
    for a, b in zip(points, points[1:]):
        sp, tr = tot(a, "spend"), tot(b, "spend")
        ta, tb = tot(a, "trials"), tot(b, "trials")
        if sp is None or tr is None or ta is None or tb is None:
            continue
        d_sp, d_tr = tr - sp, tb - ta
        if d_sp < 1 and d_tr < 1:
            continue
        out.append(f"{b.get('at', '')} {rs(d_sp)}·{num(d_tr)}")
    if not out:
        return ""
    # Six is a screenful on a phone; the rest of the day is on the dashboard.
    return ("_Hour by hour, all brands, at the hour it ended (spend · trials):_ "
            + "  ".join(out[-6:]))


def compose(rows, prev_by_brand, when, points=None, link=None):
    """The whole message. `rows` are figures() dicts, in the order they should read."""
    head = f"*Ads — today so far* · {when} IST"
    # A blank line between brands: on a phone these blocks are five or six wrapped lines
    # each, and run together they read as one paragraph about nothing.
    body = "\n\n".join(brand_block(f, prev_by_brand.get(f["brand"])) for f in rows)
    out = [head, "", body]
    strip = hour_strip(points or [], rows)
    if strip:
        out += ["", strip]
    if link:
        out += ["", f"<{link}|Open the dashboard>"]
    return "\n".join(out)


# ---- state ----------------------------------------------------------------
def day_points(day):
    """Today's pushes, oldest first. Each is cumulative today-so-far, not an hour."""
    if not H.available():
        return []
    try:
        got, ok = H.get_day_raw(STATE_NS, day)
    except Exception:
        return []
    return ((got or {}).get("points") or []) if ok else []


def last_point(pts):
    """The most recent push's numbers, {brand: {meta:{...}, google:{...}}}, or {}."""
    return ((pts[-1] or {}).get("brands") or {}) if pts else {}


def _point(f):
    """What one brand contributes to a stored point. None stays None -- a channel that
    did not answer must not be recorded as a zero and then subtracted from the next
    hour as if it had."""
    def side(d):
        return {k: (None if d.get(k) is None else round(d[k], 2))
                for k in ("spend", "trials", "installs")}
    return {"meta": side(f["meta"]), "google": side(f["google"]),
            # Kept flat as well so the hour strip can sum a brand without knowing which
            # channels were readable at the time.
            "spend": None if f["total"]["spend"] is None else round(f["total"]["spend"], 2),
            "trials": None if f["total"]["trials"] is None
                      else round(f["total"]["trials"], 1)}


def record(day, when, rows):
    """Append this push to today's doc. Best effort: a failed write must not lose a post."""
    if not H.available():
        return False
    try:
        got, ok = H.get_day_raw(STATE_NS, day)
        doc = got if (ok and isinstance(got, dict)) else {}
        pts = doc.get("points") or []
        pts.append({"at": when, "brands": {f["brand"]: _point(f) for f in rows}})
        # A day is at most 24 pushes; the cap is only there so a runaway scheduler cannot
        # grow one document without bound.
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
