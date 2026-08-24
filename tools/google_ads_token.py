#!/usr/bin/env python3
"""Mint a Google Ads refresh token, and say plainly whether it will last.

Run this on a machine with a browser:

    python3 tools/google_ads_token.py

It prints a URL, you approve it, you paste the code back, and it writes
~/.anthropic/google_ads.json (mode 600). Nothing is sent anywhere else.

Why this exists rather than a one-off curl: an auth code is single-use, so a
half-finished exchange wastes it, and the token response carries one field that
decides whether the dashboard keeps working — see the warning at the end.
"""
import json
import os
import stat
import sys
import urllib.parse
import urllib.request

SCOPE = "https://www.googleapis.com/auth/adwords"
OOB = "urn:ietf:wg:oauth:2.0:oob"          # falls back to copy/paste if no local server
DEST = os.path.expanduser("~/.anthropic/google_ads.json")


def main():
    have = {}
    if os.path.exists(DEST):
        have = json.load(open(DEST))
    cid = (have.get("client_id") or input("OAuth client id: ")).strip()
    sec = (have.get("client_secret") or input("OAuth client secret: ")).strip()
    dev = (have.get("developer_token") or input("Developer token: ")).strip()
    login = (have.get("login_customer_id") or input("Manager (login) customer id: ")).strip()

    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": OOB, "response_type": "code",
        "scope": SCOPE,
        # both are required to get a refresh token back at all
        "access_type": "offline", "prompt": "consent"})
    print("\n1. Open this and approve:\n\n   " + url + "\n")
    code = input("2. Paste the code here: ").strip()

    data = urllib.parse.urlencode({
        "code": code, "client_id": cid, "client_secret": sec,
        "redirect_uri": OOB, "grant_type": "authorization_code"}).encode()
    with urllib.request.urlopen(
            urllib.request.Request("https://oauth2.googleapis.com/token", data=data),
            timeout=60) as r:
        tok = json.load(r)

    if "refresh_token" not in tok:
        sys.exit("No refresh_token came back. That happens when the account has already "
                 "granted this client and Google reuses the old grant — revoke it at "
                 "https://myaccount.google.com/permissions and run this again.")

    json.dump({"client_id": cid, "client_secret": sec,
               "refresh_token": tok["refresh_token"], "developer_token": dev,
               "login_customer_id": login.replace("-", "")},
              open(DEST, "w"), indent=2)
    os.chmod(DEST, stat.S_IRUSR | stat.S_IWUSR)
    print(f"\nWrote {DEST} (mode 600).")

    # The one thing worth checking every time.
    exp = tok.get("refresh_token_expires_in")
    if exp:
        print(f"\n  WARNING: this refresh token expires in {round(exp/86400)} days.\n"
              "  Google only sets that when the OAuth consent screen is in 'Testing'.\n"
              "  A dashboard on a Testing-mode token breaks every week. Set the consent\n"
              "  screen to 'In production' at console.cloud.google.com/auth/overview,\n"
              "  then run this again — a published app returns no expiry at all.")
    else:
        print("\n  No expiry on this token — the consent screen is published. Good.")


if __name__ == "__main__":
    main()
