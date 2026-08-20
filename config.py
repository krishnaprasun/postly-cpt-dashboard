"""Config for the CPT dashboard.

Deliberately does NOT depend on ~/Desktop/Postly Ads Management/postly_config.py at
serve time: macOS protects ~/Desktop and the process that serves this dashboard is not
granted access to it, so that import fails at runtime even though it works from a shell.

No credential is hardcoded here. Resolution order (first hit wins):

  META_TOKEN     env META_TOKEN  ->  ~/.anthropic/meta_token  ->  Desktop toolkit import
  BRANCH creds   env BRANCH_KEY / BRANCH_SECRET  ->  ~/.anthropic/branch_creds.json
                 ->  Desktop toolkit import
  CLASSPLUS      env CLASSPLUS_API_KEY / CLASSPLUS_QUERY_ID / CLASSPLUS_HOST
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

CPT_TARGET = 150
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


def _resolve():
    token = os.environ.get("META_TOKEN", "").strip() or _read("~/.anthropic/meta_token")
    key = os.environ.get("BRANCH_KEY", "").strip()
    secret = os.environ.get("BRANCH_SECRET", "").strip()
    if not (key and secret):
        raw = _read("~/.anthropic/branch_creds.json")
        if raw:
            try:
                j = json.loads(raw)
                key = key or j.get("branch_key", "")
                secret = secret or j.get("branch_secret", "")
            except ValueError:
                pass
    if not (token and key and secret):
        pc = _toolkit()
        if pc:
            token = token or getattr(pc, "META_TOKEN", "")
            key = key or getattr(pc, "BRANCH_KEY", "")
            secret = secret or getattr(pc, "BRANCH_SECRET", "")
    missing = [n for n, v in (("META_TOKEN", token), ("BRANCH_KEY", key),
                              ("BRANCH_SECRET", secret)) if not v]
    if missing:
        raise RuntimeError(
            "missing credentials: " + ", ".join(missing) +
            " — set them in the environment or in ~/.anthropic/ "
            "(meta_token, branch_creds.json)")
    return token, key, secret


META_TOKEN, BRANCH_KEY, BRANCH_SECRET = _resolve()


# ------------------------------------------------------- Classplus (Redash) ---
# Product-side truth: signups and trial mandates per ad name, straight out of the
# Classplus DB. OPTIONAL on purpose — if the key is absent the dashboard runs exactly
# as it did before, just without those columns. Meta and Branch are what CPT is built
# on and must never be blocked by a third source being down or unconfigured.
def _classplus():
    host = os.environ.get("CLASSPLUS_HOST", "").strip()
    qid = os.environ.get("CLASSPLUS_QUERY_ID", "").strip()
    key = os.environ.get("CLASSPLUS_API_KEY", "").strip()
    if not (qid and key):
        raw = _read("~/.anthropic/classplus_creds.json")
        if raw:
            try:
                j = json.loads(raw)
                host = host or j.get("host", "")
                qid = qid or str(j.get("query_id", ""))
                key = key or j.get("api_key", "")
            except ValueError:
                pass
    return host or "data.classplus.co", qid, key


CLASSPLUS_HOST, CLASSPLUS_QUERY_ID, CLASSPLUS_KEY = _classplus()
CLASSPLUS_ON = bool(CLASSPLUS_QUERY_ID and CLASSPLUS_KEY)
