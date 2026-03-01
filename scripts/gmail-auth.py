#!/usr/bin/env python3
"""One-time OAuth2 consent flow for Gmail API access.

Usage:
    python scripts/gmail-auth.py --credentials creds.json --token token.json
"""

from __future__ import annotations

import argparse
import sys

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail OAuth2 consent flow")
    parser.add_argument(
        "--credentials",
        required=True,
        help="Path to the OAuth client credentials JSON file (downloaded from Google Cloud Console).",
    )
    parser.add_argument(
        "--token",
        required=True,
        help="Path where the resulting token JSON will be saved.",
    )
    args = parser.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "Missing dependency: pip install google-auth-oauthlib",
            file=sys.stderr,
        )
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(args.credentials, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(args.token, "w") as f:
        f.write(creds.to_json())

    print(f"Token saved to {args.token}")


if __name__ == "__main__":
    main()
