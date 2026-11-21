"""Single, low-footprint Garmin Connect login that saves tokens for garmin-mcp.

Why not `garmin-mcp-auth`? Its login chain tries 5 strategies x several TLS
fingerprints (~8 SSO login sequences per run), which feeds Cloudflare's rate
limiter. This script makes exactly ONE login POST (iOS mobile flow, one
fingerprint), plus the MFA verify if Garmin asks for one. No retries.

Usage:  uv run python deploy/one_shot_login.py [--impersonate safari_ios]
Prompts for email, password (hidden) and the emailed MFA code.
Writes ~/.garminconnect/garmin_tokens.json (or $GARMINTOKENS), mode 0600.
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

from garminconnect import client as gc
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--impersonate", default="safari_ios",
                    help="curl_cffi TLS profile (default safari_ios)")
    args = ap.parse_args()

    token_dir = Path(os.path.expanduser(os.getenv("GARMINTOKENS") or "~/.garminconnect"))
    if not gc.HAS_CFFI:
        print("curl_cffi is not installed; aborting rather than falling back.", file=sys.stderr)
        return 2

    email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password (hidden): ")

    c = gc.Client()
    sess = gc.cffi_requests.Session(impersonate=args.impersonate)
    try:
        try:
            c._do_mobile_login(sess, email, password)
        except gc._MFARequired:
            method = getattr(c, "_mfa_method", "email")
            code = input(f"MFA code sent via {method} - enter it: ").strip()
            c._complete_mfa(code)
    except GarminConnectTooManyRequestsError as e:
        print(f"\nRATE LIMITED (429): {e}\nSTOP. Do not retry for 12-24h.", file=sys.stderr)
        return 3
    except GarminConnectAuthenticationError as e:
        print(f"\nAUTH FAILED: {e}\nCheck email/password/MFA code. Do not loop retries.", file=sys.stderr)
        return 4

    if not c.is_authenticated:
        print("Login finished but no tokens were issued; aborting.", file=sys.stderr)
        return 5

    token_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(token_dir, 0o700)
    c.dump(str(token_dir))
    os.chmod(token_dir / "garmin_tokens.json", 0o600)
    print(f"\nOK - tokens saved to {token_dir / 'garmin_tokens.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
