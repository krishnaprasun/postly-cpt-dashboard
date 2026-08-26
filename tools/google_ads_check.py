#!/usr/bin/env python3
"""Say exactly which piece of the Google Ads credential is wrong.

There are four independent things that must all be true, and they fail in ways that look
alike from the outside -- an empty result can mean "no spend", "test access only", "wrong
account" or "token expired". This checks them in order and stops at the first that fails,
so the answer is one specific thing to fix rather than a shrug.

    python3 tools/google_ads_check.py
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import google_ads as GA          # noqa: E402

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "


def line(mark, what, detail=""):
    print(f"[{mark}] {what}" + (f"\n         {detail}" if detail else ""))


def main():
    c = GA.creds()
    if not c:
        line(BAD, "credentials present",
             f"nothing readable at {GA.CREDS_PATH}, and no GOOGLE_ADS_* env vars")
        return 1
    missing = [k for k in ("client_id", "client_secret", "refresh_token",
                           "developer_token") if not c.get(k)]
    if missing:
        line(BAD, "credentials complete", "missing: " + ", ".join(missing))
        return 1
    line(OK, "credentials present",
         f"developer token {c['developer_token'][:6]}…{c['developer_token'][-4:]} "
         f"({len(c['developer_token'])} chars), "
         f"login customer {c.get('login_customer_id') or '(none)'}")
    if len(c["developer_token"]) != 22:
        line(WARN, "developer token length",
             "Google Ads developer tokens are 22 characters; this one is "
             f"{len(c['developer_token'])}")

    # 1. OAuth -- is the refresh token still good?
    try:
        GA._access_token(force=True)
        line(OK, "OAuth refresh token", "exchanged for an access token")
    except Exception as e:
        line(BAD, "OAuth refresh token", str(e))
        print("\n  Fix: publish the OAuth consent screen in the project owning the client\n"
              "       id above (Testing-mode tokens expire after 7 days), then run\n"
              "       python3 tools/google_ads_token.py")
        return 1

    # 2. Developer token -- is it accepted at all, and at what access level?
    ids = GA.accessible_customers()
    if not ids:
        err = GA.last_error() or ""
        line(BAD, "developer token accepted", err[:400])
        if "DEVELOPER_TOKEN_NOT_APPROVED" in err:
            print("\n  Fix: the token works but has TEST access only, so it can reach test\n"
                  "       accounts and nothing else. Apply for Basic access in the Google\n"
                  "       Ads manager account: Tools > API Center.")
        elif "DEVELOPER_TOKEN_PROHIBITED" in err or "invalid" in err.lower():
            print("\n  Fix: the developer token is not valid for this login customer.\n"
                  "       It must come from the manager account named in\n"
                  "       login_customer_id, and that account must own the ad accounts.")
        return 1
    line(OK, "developer token accepted", f"{len(ids)} account(s) reachable: "
         + ", ".join(ids[:8]) + ("…" if len(ids) > 8 else ""))

    # 3. Can it actually read spend? Test access answers here, not above.
    import datetime
    until = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    since = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    total, rows, reached = 0.0, 0, []
    for cid in ids:
        got = GA.spend_daily(cid, since, until)
        if got:
            reached.append(cid)
            rows += len(got)
            total += sum(r["spend"] for r in got)
        elif GA.last_error():
            line(WARN, f"reading customer {cid}", GA.last_error()[:300])
    if not rows:
        line(BAD, "spend readable",
             "every account returned nothing for the last 7 days. If these are live "
             "accounts that spent, this is usually TEST-level developer access.")
        return 1
    line(OK, "spend readable",
         f"{rows} ad-group-days, {total:,.0f} in account currency, "
         f"{since} to {until}, from {len(reached)} account(s)")
    print("\nEverything the dashboard needs is in place. Set the same values on Render\n"
          "(GOOGLE_ADS_CLIENT_ID / _CLIENT_SECRET / _REFRESH_TOKEN / _DEVELOPER_TOKEN /\n"
          "_LOGIN_CUSTOMER_ID) and the Google tab fills in its spend, CPT and CPI columns.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
