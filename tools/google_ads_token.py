#!/usr/bin/env python3
"""Mint a Google Ads refresh token, on this machine, with one command.

    python3 tools/google_ads_token.py

It opens your browser, you approve, and it catches the code itself on a loopback port.
Nothing is pasted anywhere, nothing is sent anywhere but Google, and the result is written
to ~/.anthropic/google_ads.json (mode 600).

It used to use the out-of-band flow, where Google shows you a code to copy. Google
switched that off in 2022 and this client is refused for it -- verified against the live
endpoint, `invalid_request` for OOB and a normal sign-in page for loopback. So this would
have failed at the last step no matter how correct everything else was.

Two things this cannot do for you, because they need a person in a browser signed in as
the account that owns the ads:

  * publish the OAuth consent screen (Google Cloud Console > APIs & Services > OAuth
    consent screen > Publish app). While it is in Testing, Google expires every refresh
    token it issues after SEVEN DAYS, whatever you do with it. That is why the last one
    died.
  * approve the consent, which is the browser window this opens.

Afterwards it runs the same checks as tools/google_ads_check.py, so you find out
immediately whether the developer token is approved for real accounts or only test ones.
"""
import http.server
import json
import os
import socket
import stat
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCOPE = "https://www.googleapis.com/auth/adwords"
DEST = os.path.expanduser("~/.anthropic/google_ads.json")
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
# Fixed rather than random: a Web-application OAuth client only accepts redirect URIs that
# are registered on it, and one predictable port is one line to add. A Desktop client
# accepts any loopback port and does not care.
PORT = int(os.environ.get("GOOGLE_ADS_OAUTH_PORT", "8765"))

_got = {}
_done = threading.Event()


class Catch(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _got.update({k: v[0] for k, v in q.items()})
        _done.set()
        ok = "code" in _got
        body = ("<h2>Done — you can close this tab.</h2>"
                "<p>The token is being written on your machine.</p>") if ok else \
               f"<h2>Google refused.</h2><pre>{_got.get('error','(no code returned)')}</pre>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(("<meta charset=utf-8><style>body{font:16px/1.5 system-ui;"
                          "margin:14vh auto;max-width:32rem;padding:0 1rem}</style>"
                          + body).encode())

    def log_message(self, *a):
        pass                      # the console belongs to the tool, not to http.server


def main():
    have = {}
    if os.path.exists(DEST):
        have = json.load(open(DEST))
    cid = (have.get("client_id") or input("OAuth client id: ")).strip()
    sec = (have.get("client_secret") or input("OAuth client secret: ")).strip()
    dev = (have.get("developer_token") or input("Developer token: ")).strip()
    login = (have.get("login_customer_id")
             or input("Manager (login) customer id: ")).strip()

    try:
        srv = http.server.HTTPServer(("127.0.0.1", PORT), Catch)
    except socket.error as e:
        sys.exit(f"cannot listen on 127.0.0.1:{PORT} ({e}). "
                 f"Set GOOGLE_ADS_OAUTH_PORT to a free port and register it on the "
                 f"OAuth client if it is a Web application client.")
    redirect = f"http://localhost:{PORT}/"
    url = AUTH + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE,
        # Both are required to get a refresh token back at all. `prompt=consent` also
        # forces a NEW refresh token rather than reusing a previous grant.
        "access_type": "offline", "prompt": "consent"})

    print("\nOpening your browser. Sign in as the account that owns the Google Ads "
          "accounts,\nand approve. If nothing opens, paste this in yourself:\n")
    print("   " + url + "\n")
    threading.Thread(target=srv.handle_request, daemon=False).start()
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print(f"waiting on {redirect} …")
    # Blocks on the event rather than spinning on the dict: a busy-wait here would burn a
    # core for however long it takes someone to find their password.
    if not _done.wait(timeout=600):
        srv.server_close()
        sys.exit("\nTimed out after 10 minutes waiting for the browser.")
    srv.server_close()

    if "code" not in _got:
        err = _got.get("error", "(none)")
        if err == "redirect_uri_mismatch":
            sys.exit(f"\nGoogle refused the redirect. Add {redirect} to the OAuth "
                     f"client's\nAuthorised redirect URIs, or make it a Desktop client, "
                     f"then run this again.")
        if err == "access_denied":
            sys.exit("\nConsent was declined, or the app is still in Testing and this "
                     "account\nis not on its test-user list. Publishing the consent "
                     "screen fixes both.")
        sys.exit(f"\nGoogle returned: {err}")

    data = urllib.parse.urlencode({
        "code": _got["code"], "client_id": cid, "client_secret": sec,
        "redirect_uri": redirect, "grant_type": "authorization_code"}).encode()
    try:
        with urllib.request.urlopen(TOKEN, data=data, timeout=60) as r:
            tok = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"\ntoken exchange failed: {e.code} {e.read().decode()[:300]}")

    if "refresh_token" not in tok:
        sys.exit("\nGoogle returned an access token but NO refresh token. That happens "
                 "when\nthis account has already granted the app and Google reuses the "
                 "old grant.\nRevoke it at https://myaccount.google.com/permissions and "
                 "run this again.")

    out = dict(have, client_id=cid, client_secret=sec, developer_token=dev,
               login_customer_id=login, refresh_token=tok["refresh_token"],
               _note=("Google Ads API. mode 600, never committed. "
                      "login_customer_id is the manager/CID."))
    with open(DEST, "w") as f:
        json.dump(out, f, indent=2)
    os.chmod(DEST, stat.S_IRUSR | stat.S_IWUSR)
    print(f"\nwritten to {DEST} (mode 600)\n")

    print("Now checking it end to end — this is where a TEST-level developer token "
          "shows up:\n")
    import importlib
    import google_ads as GA
    importlib.reload(GA)
    from tools import google_ads_check           # noqa: F401
    sys.exit(google_ads_check.main())


if __name__ == "__main__":
    main()
