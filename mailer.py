"""The access invitation, and the one place email leaves this app.

Sent only when a super admin presses the button on the Access page. Nothing here fires on
a save, on a schedule or on a sign-in: an invitation is a message from a person to a
person, and an app that mails people as a side effect of an edit will eventually mail the
wrong person.

Transport is plain SMTP over STARTTLS, credentials in the environment. No provider SDK,
because one message per new colleague is not a mail pipeline, and the failure mode of a
pipeline nobody watches is worse than the failure mode of a button that says it did not
send.

With no MAIL_HOST configured, `ready()` is False, the button is not offered, and the page
falls back to a mailto: draft the admin sends from their own client — which is where these
should arguably come from anyway.
"""

import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

import config as C

TIMEOUT = 20
BRAND_LABELS = {k: v["label"] for k, v in C.BRANDS.items()}
ROLE_LINE = {
    "super": "full access to every brand, including managing who else gets in",
    "member": "access to the brands below, and can download any table as CSV",
    "viewer": "view-only access to the brands below",
}


def _env(k, d=""):
    return os.environ.get(k, d).strip()


def ready():
    return bool(_env("MAIL_HOST") and _env("MAIL_FROM"))


def dash_url():
    return _env("DASH_URL", "https://postly-cpt-dashboard.onrender.com").rstrip("/")


def _accent(brands):
    """(band, call-to-action). The band is the product's own ink whatever the brand: the
    themes are picked to work as a thin accent on a white app, and Speakeasy's amber as a
    letterhead is a mud-coloured envelope. Brand identity goes on the button and the link,
    where it is a signal rather than a wall.

    The button uses each theme's DARK shade, not its accent -- white text on Speakeasy's
    #F5B301 is unreadable, and an invitation nobody can read the button of is not an
    invitation.
    """
    band = "#1A1C2E"
    if len(brands) == 1 and brands[0] in C.BRANDS:
        return band, C.BRANDS[brands[0]]["theme"]["dark"]
    return band, "#127A42"


def _esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build(email, role, brands, inviter=""):
    """(subject, html, text) for one person's invitation."""
    brands = [b for b in brands if b in C.BRANDS]
    names = [BRAND_LABELS[b] for b in brands]
    who = (", ".join(names[:-1]) + " and " + names[-1]) if len(names) > 1 else \
          (names[0] if names else "the dashboard")
    role_label = {"super": "a super admin", "member": "a member",
                  "viewer": "view-only"}.get(role, role)
    link = dash_url() + "/auth/login"
    dark, light = _accent(brands)
    subject = "You have access to Ads Performance"

    # Inline styles and a table: every mail client strips <style> blocks, and half of them
    # do not lay out flexbox. This is the boring markup that survives Gmail, Outlook and
    # a phone.
    html = f"""\
<!doctype html><html><body style="margin:0;padding:0;background:#F1F3F9">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="background:#F1F3F9;padding:28px 12px">
 <tr><td align="center">
  <table role="presentation" width="560" cellpadding="0" cellspacing="0"
         style="max-width:560px;width:100%;background:#ffffff;border:1px solid #E3E6F0">
   <tr><td style="background:{dark};padding:26px 30px">
     <div style="font:600 15px/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
                 color:rgba(255,255,255,.72);letter-spacing:.02em">Ads Performance</div>
     <div style="font:700 27px/1.25 -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
                 color:#ffffff;margin-top:7px">{_esc(who)}</div>
   </td></tr>
   <tr><td style="padding:30px 30px 34px">
     <div style="font:700 19px/1.3 -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
                 color:{dark};margin:0 0 12px">Your access is ready</div>
     <p style="font:400 15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
               color:#3D4256;margin:0 0 22px">
       You have been given <b>{_esc(role_label)}</b> access to the live ads dashboard —
       spend, trials and cost per trial across Meta and Google, updated through the day.
     </p>
     <table role="presentation" cellpadding="0" cellspacing="0"><tr>
       <td style="background:{light};border-radius:4px">
         <a href="{link}" style="display:inline-block;padding:13px 26px;
            font:600 15px/1 -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
            color:#ffffff;text-decoration:none">Open Ads Performance</a>
       </td></tr></table>
     <p style="font:400 13px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
               color:#6B7086;margin:22px 0 0">
       Sign in with this approved Google account:
       <a href="mailto:{_esc(email)}" style="color:{light};text-decoration:none;
          font-weight:600">{_esc(email)}</a><br>
       No other account will work.
     </p>
   </td></tr>
   <tr><td style="border-top:1px solid #EDEFF6;padding:16px 30px;
                  font:400 12px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
                  color:#9498AB">
     {('Invited by ' + _esc(inviter) + '. ') if inviter else ''}Need a different brand or
     level? Reply to this email.
   </td></tr>
  </table>
 </td></tr></table></body></html>"""

    text = (f"Your access is ready\n\n"
            f"You have been given {role_label} access to the Ads Performance dashboard "
            f"({who}) — spend, trials and cost per trial across Meta and Google.\n\n"
            f"{link}\n\n"
            f"Sign in with this approved Google account: {email}\n"
            f"No other account will work.\n"
            + (f"\nInvited by {inviter}.\n" if inviter else ""))
    return subject, html, text


def send(to, subject, html, text, reply_to=""):
    """(ok, detail). Never raises, and `detail` never carries the password."""
    if not ready():
        return False, "No mail server is configured."
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to or ""):
        return False, "That is not an email address."
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((_env("MAIL_FROM_NAME", "Ads Performance"), _env("MAIL_FROM")))
    msg["To"] = to
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    host, port = _env("MAIL_HOST"), int(_env("MAIL_PORT", "587") or 587)
    user, pw = _env("MAIL_USER") or _env("MAIL_FROM"), os.environ.get("MAIL_PASS", "")
    try:
        if port == 465:
            srv = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT,
                                   context=ssl.create_default_context())
        else:
            srv = smtplib.SMTP(host, port, timeout=TIMEOUT)
            srv.starttls(context=ssl.create_default_context())
        with srv:
            if pw:
                srv.login(user, pw)
            srv.send_message(msg)
        return True, "sent"
    except smtplib.SMTPAuthenticationError:
        return False, ("The mail server rejected the credentials. If this is Gmail, it "
                       "needs an App Password, not the account password.")
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:180]}"
