"""Low-footprint Garmin Connect login that saves DI OAuth tokens for garmin-mcp.

Why not `garmin-mcp-auth`? Its login chain tries 5 strategies x several TLS
fingerprints (~8 SSO login sequences per run), which feeds Cloudflare's rate
limiter. This script makes at most ONE login attempt and never retries the
credentials.

Modes (pick one with --mode):

  browser (default)  NO automated credential POST at all. You sign in (incl.
                     the emailed MFA code) in your normal browser, with a
                     DevTools rule blocking the post-login redirect so the
                     one-time service ticket (ST-...) is NOT consumed. Paste
                     the blocked URL here and the script exchanges the ticket
                     for DI OAuth tokens at diauth.garmin.com.

  mobile             One iOS-app style login (garminconnect>=0.3.20 loads the
                     sign-in page first, which avoids the cookie-less 429).

  widget             One SSO HTML-widget login (browser-style form flow).

Usage:  uv run python deploy/one_shot_login.py [--mode browser|mobile|widget]
Writes ~/.garminconnect/garmin_tokens.json (or $GARMINTOKENS), mode 0600,
then verifies with a single profile read.

Exit codes: 0 ok+verified, 2 setup problem (nothing sent), 3 rate limited,
4 auth failed, 5 no DI tokens issued, 6 other error, 7 saved but verify failed,
8 tokens obtained but could not be saved (fallback file printed).
"""
import argparse
import getpass
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlencode

import garminconnect.client as gcc
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)
from garminconnect.client.sso import _MFARequired

# Surface the library's own WARNING lines (e.g. the real reason a DI exchange
# failed) instead of hiding them behind a generic error.
logging.basicConfig(level=logging.WARNING, format="  [garminconnect] %(message)s")

SSO = "https://sso.garmin.com"
# service URL -> sign-in page that issues a ticket for it
SERVICES = {
    # iOS app service: the one the library's mobile flow exchanges for DI tokens.
    "ios": (
        "https://mobile.integration.garmin.com/gcm/ios",
        f"{SSO}/mobile/sso/en_US/sign-in",
        {"clientId": "GCM_IOS_DARK"},
    ),
    # SSO embed widget service (what the widget strategy uses).
    "embed": (
        f"{SSO}/sso/embed",
        f"{SSO}/sso/signin",
        {
            "id": "gauth-widget",
            "embedWidget": "true",
            "gauthHost": f"{SSO}/sso/embed",
            "source": f"{SSO}/sso/embed",
            "redirectAfterAccountLoginUrl": f"{SSO}/sso/embed",
            "redirectAfterAccountCreationUrl": f"{SSO}/sso/embed",
        },
    ),
}
# Most specific first: ?ticket=/&ticket= query, then JSON serviceTicketId,
# then a bare ST- token with a left word boundary.
TICKET_PATTERNS = (
    re.compile(r"[?&]ticket=(ST-[^\"&\s#]+)"),
    re.compile(r"\"serviceTicketId\"\s*:\s*\"(ST-[^\"]+)\""),
    re.compile(r"(?<![A-Za-z0-9])(ST-[A-Za-z0-9-]+)"),
)
# Chrome's Request conditions panel takes URLPattern syntax; block the whole
# redirect host(s) so the ticket-carrying navigation never leaves the browser.
BLOCK_PATTERNS = {
    "ios": "*://mobile.integration.garmin.com/*",
    "embed": "*://sso.garmin.com/sso/embed*",
}
MFA_TRIES = 3  # re-entering a mistyped code reuses the same SSO session


def extract_ticket(raw: str) -> str | None:
    raw = unquote(raw.strip())
    for pat in TICKET_PATTERNS:
        m = pat.search(raw)
        if m:
            return m.group(1).rstrip(".")
    return None


def browser_mode(c, service_key: str) -> None:
    service_url, page, extra = SERVICES[service_key]
    url = f"{page}?{urlencode({**extra, 'service': service_url})}"
    print(f"""
BEFORE signing in - set up Chrome so the ticket is not used up:
  a. Open a new Chrome window (Incognito is fine) on a blank tab.
  b. Open DevTools (Cmd+Opt+I) > Network tab. Tick "Preserve log".
  c. Cmd+Shift+P > type "request conditions" > Enter (older Chrome:
     "Show Network request blocking"). Tick "Enable blocking and throttling",
     click "+", enter exactly:
         {BLOCK_PATTERNS[service_key]}
     set it to Block, click elsewhere, and check it has NO red outline
     (red = "failed to parse" = not blocking).
  d. Leave DevTools open on that tab.

THEN:
  1. Paste this URL into that tab's address bar:

     {url}

  2. Sign in and enter the emailed MFA code.
  3. FASTEST: as soon as the code is accepted, type verifyCode in the Network
     filter box, click that request > Response, copy the serviceTicketId
     value (ST-...). (No MFA prompt? use the 'login' request instead.)
     The blocked redirect also shows red ('... affected' goes to 1) and its
     Request URL contains the same ticket.
""")
    raw = input("  4. Paste the URL or ticket here (promptly - it expires): ")
    ticket = extract_ticket(raw)
    if not ticket:
        raise SystemExit("No ST-... ticket found in what you pasted. Nothing sent.")
    print(f"Exchanging ticket {ticket[:10]}... for DI OAuth tokens...")
    # The library only logs the HTTP status at DEBUG; show status + error body
    # for failed attempts (failure bodies carry no tokens) so a rejection is
    # diagnosable without another login.
    orig = c._di_token_post

    def traced(client_id, accept, data):
        r = orig(client_id, accept, data)
        if not r.ok:
            body = (r.text or "").replace("\n", " ")[:300]
            print(f"  diauth {client_id}: HTTP {r.status_code} {body}")
        return r

    c._di_token_post = traced
    c._exchange_service_ticket(ticket, service_url=service_url)


def credential_mode(c, mode: str) -> None:
    email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password (hidden): ")
    try:
        if mode == "widget":
            print("Widget flow: pauses a few seconds before submitting (anti-WAF)...")
            c._widget_web_login(email, password)
        else:
            sess = gcc.cffi_requests.Session(impersonate="safari_ios")
            print("Mobile flow: loads sign-in page, pauses 2-5s, then submits...")
            c._do_mobile_login(sess, email, password)
    except _MFARequired:
        method = getattr(c, "_mfa_method", "email")
        for attempt in range(1, MFA_TRIES + 1):
            code = input(f"MFA code sent via {method} - enter it: ").strip()
            try:
                c._complete_mfa(code)
                return
            except GarminConnectAuthenticationError as e:
                if attempt == MFA_TRIES:
                    raise
                print(f"  Code rejected ({e}). Re-enter it (no new login is sent).")


def preflight_token_path(token_dir: Path) -> Path:
    """Fail BEFORE any network call if the token file can't be written."""
    token_file = gcc.token_file_path(str(token_dir))
    token_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    probe = token_file.parent / ".write_probe"
    probe.write_text("ok")
    probe.unlink()
    return token_file


def save(c, token_dir: Path, token_file: Path) -> bool:
    try:
        c.dump(str(token_dir))
        os.chmod(token_file, 0o600)
        # So a refresh during verification is persisted to the same file.
        c._tokenstore_path = str(token_dir)
        return True
    except Exception as e:
        fallback = Path.home() / "garmin_tokens.RESCUE.json"
        fd = os.open(fallback, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(c.dumps())
        print(f"\nCould not save to {token_file}: {e}\n"
              f"Tokens rescued to {fallback} - do NOT log in again; tell Claude.",
              file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=("browser", "mobile", "widget"), default="browser")
    ap.add_argument("--service", choices=tuple(SERVICES), default="ios",
                    help="browser mode only: which service to request a ticket for")
    args = ap.parse_args()

    token_dir = Path(os.path.expanduser(os.getenv("GARMINTOKENS") or "~/.garminconnect"))
    try:
        token_file = preflight_token_path(token_dir)
    except Exception as e:
        print(f"Token path {token_dir} is not usable: {e}. Nothing sent.", file=sys.stderr)
        return 2
    if args.mode != "browser" and not gcc.HAS_CFFI:
        print("curl_cffi is not installed; aborting rather than falling back.", file=sys.stderr)
        return 2

    c = gcc.Client()
    try:
        if args.mode == "browser":
            browser_mode(c, args.service)
        else:
            credential_mode(c, args.mode)
    except GarminConnectTooManyRequestsError as e:
        print(f"\nRATE LIMITED (429): {e}\nSTOP. Do not retry for 12-24h.", file=sys.stderr)
        return 3
    except GarminConnectAuthenticationError as e:
        print(f"\nAUTH FAILED: {e}\n(See any [garminconnect] lines above for the real "
              "cause.) Do not loop retries - tell Claude the message.", file=sys.stderr)
        return 4
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        return 6
    except Exception as e:  # surface anything else verbatim, no retry
        print(f"\nFAILED ({type(e).__name__}): {e}\nNo retry made.", file=sys.stderr)
        return 6

    if not getattr(c, "di_token", None):
        # A JWT_WEB browser cookie may have been issued instead; it is short-lived
        # and not persisted by dump(), so it is useless for the server.
        print("Garmin did not issue DI OAuth tokens (only a short-lived web "
              "cookie, or nothing). Nothing saved - tell Claude.", file=sys.stderr)
        return 5

    if not save(c, token_dir, token_file):
        return 8
    print(f"\nOK - tokens saved to {token_file}")

    try:
        prof = c.connectapi("/userprofile-service/socialProfile")
        print(f"Verified: signed in as {prof.get('displayName')!r}")
    except Exception as e:
        print(f"Tokens saved, but the verification read failed: {e}\n"
              "Do not log in again - tell Claude.", file=sys.stderr)
        return 7
    return 0


if __name__ == "__main__":
    sys.exit(main())
