"""
Mint a test access token.

Supabase signs its access tokens with the project JWT secret using HS256. This
backend only verifies that signature -- it never issues a token and never sees
a password (FR-ONB-001). Which means anything holding the secret can produce a
token the backend will accept, and during development that is useful: the whole
onboarding flow can be exercised before Google sign-in is wired up.

    python scripts/make_test_token.py
    python scripts/make_test_token.py --subject user-two --provider apple

DEVELOPMENT ONLY. This exists because the secret is in your local `.env`. In
production the secret lives in the deployment environment and nobody runs this.
Never commit a token it produces.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

import jwt

sys.path.insert(0, ".")

from app.core.config import get_settings  # noqa: E402


def make_token(subject: str, provider: str, email: str, hours: int) -> str:
    settings = get_settings()
    if not settings.jwt_secret:
        raise SystemExit("JWT_SECRET is not set in .env")

    now = datetime.now(timezone.utc)
    payload = {
        # The claims app/core/security.py actually reads.
        "sub": subject,
        "aud": settings.jwt_audience,
        "exp": now + timedelta(hours=hours),
        "iat": now,
        "email": email,
        # Supabase nests the identity provider here.
        "app_metadata": {"provider": provider},
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint a development access token")
    parser.add_argument(
        "--subject",
        default="test-user-1",
        help="becomes User.auth_id -- change it to simulate a different account",
    )
    parser.add_argument("--provider", default="google", choices=["google", "apple"])
    parser.add_argument("--email", default="test@example.com")
    parser.add_argument("--hours", type=int, default=12, help="token lifetime")
    args = parser.parse_args()

    token = make_token(args.subject, args.provider, args.email, args.hours)

    print()
    print(f"subject : {args.subject}")
    print(f"provider: {args.provider}")
    print(f"expires : in {args.hours}h")
    print()
    print("Paste this into the Authorize box in /docs:")
    print()
    print(token)
    print()


if __name__ == "__main__":
    main()