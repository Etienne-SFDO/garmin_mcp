"""Low-footprint Garmin Connect login that saves DI OAuth tokens for garmin-mcp.

Why not `garmin-mcp-auth`? Its login chain tries 5 strategies x several TLS
fingerprints (~8 SSO login sequences per run), which feeds Cloudflare's rate
limiter. This script makes at most ONE login attempt and never retries.

Modes (pick one with --mode):

  browser (default)  NO automated credential POST at all. You sign in (incl.
                     the emailed MFA code) in your normal browser; Garmin
                     redirects to a URL carrying a one-time service ticket
                     (ST-...). Paste that URL here and the script exchanges
                     the ticket for DI OAuth tokens at diauth.garmin.com.
                     Tickets are single-use and short-lived: paste promptly.

  mobile             One iOS-app style login (garminconnect>=0.3.20 loads the
                     sign-in page first, which avoids the cookie-less 429).

  widget             One SSO HTML-widget login (browser-style form flow).

Usage:  uv run python deploy/one_shot_login.py [--mode browser|mobile|widget]
Writes ~/.garminconnect/garmin_tokens.json (or $GARMINTOKENS), mode 0600,
then verifies with a single profile read.
"""
import argparse
import getpass
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

import garminconnect.client as gcc
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)
from garminconnect.client.sso import _MFARequired

SSO = "https://sso.garmin.com"
# service URL -> sign-in page that issues a ticket for it
SERVICES = {
    # iOS app service: the same one the mobile flow exchanges for DI tokens.
    # The final redirect host does not resolve in a desktop browser - that is
    # expected; the ticket is in the address bar of the error page.
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
TICKET_RE = re.compile(r"(ST-[A-Za-z0-9._-]+)")


def browser_mode(c, service_key: str) -> None:
    service_url, page, extra = SERVICES[service_key]
    url = f"{page}?{urlencode({**extra, 'service': service_url})}"
    print(
        "\n1. Open this URL in your normal browser (private window is best):\n\n"
        f"   {url}\n\n"
        "2. Sign in and enter the emailed MFA code.\n"
        "3. You will land on a page (possibly a 'can't reach this site' error).\n"
        "   Copy the FULL address-bar URL (it contains ticket=ST-...).\n"
        "   If the URL has no ticket, open DevTools > Network, find the request\n"
        "   whose response/redirect contains 'ST-', and copy that value.\n"
    )
    raw = input("4. Paste the URL or ticket here: ").strip()
    m = TICKET_RE.search(raw)
    if not m:
        raise SystemExit("No ST-... ticket found in what you pasted. Nothing sent.")
    print("Exchanging ticket for DI OAuth tokens...")
    c._exchange_service_ticket(m.group(1), service_url=service_url)


def credential_mode(c, mode: str) -> None:
    email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password (hidden): ")
    try:
        if mode == "widget":
            print("Widget flow: pauses a few seconds before submitting (anti-WAF)...")
            c._widget_web_login(email, password)
        else:
            sess = gcc.cffi_requests.Session(impersonate="safari_ios")
            print("Mobile flow: loads sign-in page, pauses ~2s, then submits...")
            c._do_mobile_login(sess, email, password)
    except _MFARequired:
        method = getattr(c, "_mfa_method", "email")
        code = input(f"MFA code sent via {method} - enter it: ").strip()
        c._complete_mfa(code)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=("browser", "mobile", "widget"), default="browser")
    ap.add_argument("--service", choices=tuple(SERVICES), default="ios",
                    help="browser mode only: which service to request a ticket for")
    args = ap.parse_args()

    token_dir = Path(os.path.expanduser(os.getenv("GARMINTOKENS") or "~/.garminconnect"))
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
        print(f"\nAUTH FAILED: {e}\nDo not loop retries - tell Claude the message.", file=sys.stderr)
        return 4
    except Exception as e:  # surface anything else verbatim, no retry
        print(f"\nFAILED ({type(e).__name__}): {e}\nNo retry made.", file=sys.stderr)
        return 6

    if not getattr(c, "di_token", None):
        # A JWT_WEB browser cookie may have been issued instead; it is short-lived
        # and not persisted by dump(), so it is useless for the server.
        print("Garmin did not issue DI OAuth tokens (only a short-lived web "
              "cookie, or nothing). Nothing saved - tell Claude.", file=sys.stderr)
        return 5

    token_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(token_dir, 0o700)
    c.dump(str(token_dir))
    token_file = token_dir / "garmin_tokens.json"
    os.chmod(token_file, 0o600)
    print(f"\nOK - tokens saved to {token_file}")

    try:
        prof = c.connectapi("/userprofile-service/socialProfile")
        print(f"Verified: signed in as {prof.get('displayName')!r}")
    except Exception as e:
        print(f"Tokens saved, but the verification read failed: {e}\n"
              "Do not log in again - tell Claude.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
