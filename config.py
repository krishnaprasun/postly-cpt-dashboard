"""Config for the CPT dashboard.

Deliberately does NOT depend on ~/Desktop/Postly Ads Management/postly_config.py at
serve time: macOS protects ~/Desktop and the process that serves this dashboard is not
granted access to it, so that import fails at runtime even though it works from a shell.

No credential is hardcoded here. Resolution order (first hit wins):

  META_TOKEN     env META_TOKEN  ->  ~/.anthropic/meta_token  ->  Desktop toolkit import
  BRANCH creds   env BRANCH_KEY / BRANCH_SECRET  ->  ~/.anthropic/branch_creds.json
                 ->  Desktop toolkit import
  CLASSPLUS      env CLASSPLUS_QUERIES ("id:key,id:key") / CLASSPLUS_HOST
                 ->  ~/.anthropic/classplus_creds.json          (OPTIONAL)

Keep ~/.anthropic/ current — it is the one location every process here can read.
Account and campaign ids below mirror postly_config.py; they are stable identifiers.
"""
import json
import os
import sys

GRAPH = "https://graph.facebook.com/v21.0"

AD_ACCOUNT = "act_964790132585820"        # "Postly"
INSTALL_ACCOUNT = "act_2383113182218548"  # "Postly Install" — the second, easily missed one

IST_OFFSET_MIN = 330

_TOOLKIT = "/Users/krishnaprasun/Desktop/Postly Ads Management"


def _toolkit():
    """postly_config module if this process can actually read ~/Desktop, else None."""
    try:
        if _TOOLKIT not in sys.path:
            sys.path.insert(0, _TOOLKIT)
        import postly_config
        return postly_config
    except Exception:
        return None


def _read(path):
    try:
        with open(os.path.expanduser(path)) as f:
            return f.read().strip()
    except OSError:
        return ""


def _branch_creds():
    """{brand: (key, secret)} — one Branch app per brand, each with its own pair.

    Postly's pair keeps the unprefixed names it has always had so nothing that already
    sets them breaks; the others are prefixed. A brand with no pair is not an error —
    its Meta side (spend, budgets, statuses) works regardless and the trial columns
    simply stay hidden until a key turns up.

      env    BRANCH_KEY / BRANCH_SECRET                  -> postly
             SPEAKEASY_BRANCH_KEY / SPEAKEASY_BRANCH_SECRET
             FUNDA_BRANCH_KEY / FUNDA_BRANCH_SECRET
      file   ~/.anthropic/branch_creds.json
             {"postly": {"branch_key": ..., "branch_secret": ...}, "speakeasy": {...}}
             or the older flat {"branch_key": ..., "branch_secret": ...} = postly
    """
    out = {}
    try:
        j = json.loads(_read("~/.anthropic/branch_creds.json") or "{}")
    except ValueError:
        j = {}
    if j.get("branch_key"):                      # older flat file
        j = {"postly": j}
    for brand in ("postly", "speakeasy", "funda"):
        pre = "" if brand == "postly" else brand.upper() + "_"
        blk = j.get(brand) or {}
        key = (os.environ.get(pre + "BRANCH_KEY", "").strip()
               or blk.get("branch_key", "")).strip()
        sec = (os.environ.get(pre + "BRANCH_SECRET", "").strip()
               or blk.get("branch_secret", "")).strip()
        if key and sec:
            out[brand] = (key, sec)
    return out


def _resolve():
    token = os.environ.get("META_TOKEN", "").strip() or _read("~/.anthropic/meta_token")
    branch = _branch_creds()
    if not (token and branch.get("postly")):
        pc = _toolkit()
        if pc:
            token = token or getattr(pc, "META_TOKEN", "")
            k = getattr(pc, "BRANCH_KEY", "")
            s = getattr(pc, "BRANCH_SECRET", "")
            if k and s:
                branch.setdefault("postly", (k, s))
    # Meta is the one hard requirement: it is the only source of spend, and spend is
    # what every other number on the page divides by. Branch being absent degrades a
    # brand to Meta-only, which is a legible state; Meta being absent is not.
    if not token:
        raise RuntimeError(
            "missing credentials: META_TOKEN — set it in the environment or in "
            "~/.anthropic/meta_token")
    return token, branch


META_TOKEN, BRANCH = _resolve()


# ------------------------------------------------------- Classplus (Redash) ---
# Product-side truth: signups and trial mandates per ad name, straight out of the
# Classplus DB. OPTIONAL on purpose — if the key is absent the dashboard runs exactly
# as it did before, just without those columns. Meta and Branch are what CPT is built
# on and must never be blocked by a third source being down or unconfigured.
def _classplus():
    """(host, [(query_id, api_key), ...]) — sources are tried in the order given.

    More than one is allowed because each Redash query answers a different window and
    each carries its own result key. A query that reports a signup date can serve any
    day inside its range; one that does not can only serve the exact window written
    into its SQL. Listing both lets whichever can answer the requested window answer it.

      CLASSPLUS_QUERIES  "19695:key" — or "id:key,id:key" for several  (preferred)
      CLASSPLUS_QUERY_ID / CLASSPLUS_API_KEY          (single source, still honoured)
      ~/.anthropic/classplus_creds.json  {"queries": [{"id": ..., "key": ...}]}
                                         or the older {"query_id": ..., "api_key": ...}
    """
    host = os.environ.get("CLASSPLUS_HOST", "").strip()
    out = []

    def add(qid, key):
        qid, key = str(qid or "").strip(), str(key or "").strip()
        if qid and key and not any(q == qid for q, _ in out):
            out.append((qid, key))

    for pair in os.environ.get("CLASSPLUS_QUERIES", "").split(","):
        if ":" in pair:
            qid, _, key = pair.partition(":")
            add(qid, key)
    add(os.environ.get("CLASSPLUS_QUERY_ID", ""), os.environ.get("CLASSPLUS_API_KEY", ""))

    raw = _read("~/.anthropic/classplus_creds.json")
    if raw:
        try:
            j = json.loads(raw)
        except ValueError:
            j = {}
        host = host or j.get("host", "")
        for q in j.get("queries") or []:
            add(q.get("id"), q.get("key"))
        add(j.get("query_id"), j.get("api_key"))
    return host or "data.classplus.co", out


CLASSPLUS_HOST, CLASSPLUS_QUERIES = _classplus()
CLASSPLUS_ON = bool(CLASSPLUS_QUERIES)


# ------------------------------------------------------------- brands ------
# Three brands share this dashboard because they share everything that is hard about
# it: Meta's rate limits, the roster caching, the ad-name join, the degradation rules.
# What differs per brand is only this table — which ad accounts, which Branch app,
# which two events, and what "good" costs.
#
# `events` maps the page's two fixed slots to each brand's own Branch event names:
#   t101  the headline count, the CPT numerator
#   t10m  the secondary count shown beside it
# t101 is TRIAL STARTED for every brand, deliberately and by the owner's instruction
# (2026-08-22): one definition of "a trial" across all three, so CPT means the same
# thing everywhere and the brands can be compared. The no-cancel-after-10-minutes
# variant stays visible in t10m but never drives CPT.
# The slot keys stay the same across brands so the page needs no per-brand branching;
# only the labels and the underlying event names change.
#
# `cpt_target` may be None, which means "show the number, do not colour it" — a target
# nobody has agreed on is worse than no target, because a red cell is an instruction.
#
# `testing_re` splits spend into the two things it is actually buying. A testing campaign
# is looking for a creative that works and is expected to cost more per trial; a trial
# campaign is scaling one that already does. Blending them produces a CPT that describes
# neither, and judging a testing ad set against the trial target would kill the pipeline
# that feeds it. Every brand names testing campaigns with the word "testing" — verified
# 2026-08-24 across all seven ad accounts — and everything else is trial. Per-brand so a
# brand that renames its campaigns can be corrected without touching the others.
TESTING_RE_DEFAULT = r"(?i)testing"
BRANDS = {
    "postly": {
        "label": "Postly",
        "testing_re": TESTING_RE_DEFAULT,
        "accounts": [{"id": AD_ACCOUNT, "name": "Postly"},
                     {"id": INSTALL_ACCOUNT, "name": "Postly Install"}],
        "events": {"t101": "postly_trial_started_backend",
                   "t10m": "postly_trial_nc_after10min_backend"},
        "labels": {"t101": "Trials", "t10m": "NC 10m"},
        "event_note": {"t101": "postly_trial_started_backend",
                       "t10m": "postly_trial_nc_after10min_backend"},
        "cpt_target": 150,
        "classplus": True,
        "logo": "brand/postly.svg",
        # Chrome only — active tab, hover, focus ring, hero wash, spinner. The
        # good/warn/bad colours are NEVER themed: a "good" CPT has to stay green on
        # every brand or the one colour anyone acts on stops meaning one thing.
        "theme": {"accent": "#20A75D", "dark": "#127A42", "light": "#EAF7F0"},
    },
    "speakeasy": {
        "label": "Speakeasy",
        "testing_re": TESTING_RE_DEFAULT,
        "accounts": [{"id": "act_874500498817876", "name": "SpeakEasy"},
                     {"id": "act_909676394829541", "name": "SpeakEasy Install"}],
        "events": {"t101": "speakeasy_trial_started",
                   "t10m": "SE_trial_nc_after_10mins"},
        "labels": {"t101": "Trials", "t10m": "NC 10m"},
        "event_note": {"t101": "speakeasy_trial_started",
                       "t10m": "SE_trial_nc_after_10mins"},
        # No agreed target yet, so CPT is shown uncoloured rather than judged.
        "cpt_target": 275,
        "classplus": False,
        "logo": "brand/speakeasy.svg",
        # Their black-on-gold identity. `dark` is a deep bronze rather than the logo's
        # gold so it never reads as the amber "warn" colour in body text.
        "theme": {"accent": "#F5B301", "dark": "#6E4A00", "light": "#FFF6DF"},
    },
    "funda": {
        "label": "Funda",
        "testing_re": TESTING_RE_DEFAULT,
        "accounts": [{"id": "act_1415034359774559", "name": "Funda"},
                     {"id": "act_1662727118397158", "name": "Funda Earning App"},
                     {"id": "act_826851770432701", "name": "Funda 3"}],
        # Funda's Branch events are named exactly like Postly's minus the prefix.
        # 99.7% of its attributed trials matched a live Meta ad name (2026-08-21),
        # the cleanest join of the three brands.
        "events": {"t101": "trial_started_backend",
                   "t10m": "trial_nc_after10min_backend"},
        "labels": {"t101": "Trials", "t10m": "NC 10m"},
        "event_note": {"t101": "trial_started_backend",
                       "t10m": "trial_nc_after10min_backend"},
        "cpt_target": 180,
        "classplus": False,
        "logo": "brand/funda.png",
        # The violet end of their play-button gradient; the orange end is too close to
        # the warn amber to use as chrome.
        "theme": {"accent": "#6A4BD8", "dark": "#4A32A6", "light": "#F1EDFD"},
    },
}
DEFAULT_BRAND = "postly"

# ---------------------------------------------------------- brand links ----
# One unguessable link per brand, because each brand is a different team and a team
# should land on its own numbers and not wander into another brand's spend.
#
# The link IS the credential. That is not a new idea here — the dashboard has always been
# "anyone with the URL", by the owner's explicit decision — this only narrows it from one
# secret covering everything to one secret per team. It is NOT a login: a link that gets
# forwarded grants what it grants, and anyone holding it keeps access until it is rotated.
# Rotating is changing one env var.
#
# Resolution order matches everything else here: env first, then the file, so Render can
# hold the real values while a laptop reads them from ~/.anthropic.
def _brand_links():
    out = {}
    try:
        j = json.loads(_read("~/.anthropic/brand_links.json") or "{}")
    except ValueError:
        j = {}
    for b in BRANDS:
        v = (os.environ.get("BRAND_LINK_" + b.upper(), "").strip()
             or (j.get(b) or "").strip())
        if v:
            out[v] = b
    master = (os.environ.get("BRAND_LINK_ALL", "").strip()
              or (j.get("all") or "").strip())
    return out, master


BRAND_LINKS, MASTER_LINK = _brand_links()
# Whether the bare URL still works. Default ON, and that default is not laziness: gating
# it is what takes the link everybody already has and turns it into a 403, which is fine
# only AFTER every team is holding its own link. Doing it before — as this first shipped —
# locks out the people it is meant to serve. Set ROOT_OPEN=0 the day the handover is done;
# until then the bare URL behaves exactly as it always has, all brands and full controls.
ROOT_OPEN = os.environ.get("ROOT_OPEN", "1").strip() not in ("0", "false", "no")
# With no links configured the app behaves exactly as it always did: open, all brands.
# So this ships dark and is switched on by setting the env vars, and switching it off is
# unsetting them — no deploy either way.
LINKS_ON = bool(BRAND_LINKS or MASTER_LINK)


def link_caps(key):
    """What this link may do. None means the key is not valid at all.

    Two capabilities, deliberately not a permission system: `brands` is what it may see,
    `full` is whether it may make the app SPEND — force a hard Meta roster re-read, or
    recompute longevity. A team link is read-only in that sense; the master link is not.
    """
    if not LINKS_ON:
        return {"brands": list(BRANDS), "full": True}
    key = (key or "").strip()
    if not key and ROOT_OPEN:
        return {"brands": list(BRANDS), "full": True}
    if MASTER_LINK and key == MASTER_LINK:
        return {"brands": list(BRANDS), "full": True}
    b = BRAND_LINKS.get(key)
    return {"brands": [b], "full": False} if b else None


def brands_for(key):
    """Which brands this link may see. None means 'not a valid link'."""
    caps = link_caps(key)
    return caps["brands"] if caps else None


def brand(name):
    """One brand's config, with its Branch pair resolved. Unknown name -> default."""
    b = dict(BRANDS.get(name) or BRANDS[DEFAULT_BRAND])
    b["key"] = name if name in BRANDS else DEFAULT_BRAND
    b["branch"] = BRANCH.get(b["key"])
    if not b["branch"]:
        b["events"], b["labels"], b["event_note"] = {}, {}, {}
    return b


def BRAND_HAS_BRANCH(name):
    """Whether this brand can produce trial counts at all — used by the page to decide
    what to promise while loading, before any data has come back."""
    return bool(BRANCH.get(name) and BRANDS.get(name, {}).get("events"))
