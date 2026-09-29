"""
Create a Postman environment for the API collection.

    python scripts/make_postman_env.py
    npx newman run postman/skincare-advisor.postman_collection.json \
        -e postman/local.postman_environment.json

Mints tokens for three brand-new test users (random UUID subjects) and writes
postman/local.postman_environment.json, plus a small grey test image:

  userA -- ordinary adult: full onboarding, a scan, account deletion
  userB -- answers yes to safety question 3: declared referral, no scan allowed
  userC -- under 13: scan access blocked

The collection deletes all three at the end, so running it leaves nothing behind.

The environment file holds working tokens and is git-ignored. Re-run this script
before each collection run: tokens expire, and each run needs fresh users
because date of birth and skin type can only be set once.

DEVELOPMENT ONLY -- uses the JWT secret from your local .env, like
make_test_token.py.
"""

from __future__ import annotations

import json
import struct
import sys
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt

sys.path.insert(0, ".")

from app.core.config import get_settings  # noqa: E402

OUT_DIR = Path("postman")


def token(subject: str) -> str:
    settings = get_settings()
    if not settings.jwt_secret:
        raise SystemExit("JWT_SECRET is not set in .env")
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": subject,
            "aud": settings.jwt_audience,
            "exp": now + timedelta(hours=2),
            "iat": now,
            "email": f"postman-{subject[:8]}@example.com",
            "app_metadata": {"provider": "email"},
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def grey_png(path: Path, size: int = 256) -> None:
    """A plain grey PNG. Not a face -- the analysis should call it unusable."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    row = b"\x00" + b"\x80\x80\x80" * size
    raw = zlib.compress(row * size)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", raw)
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def main() -> None:
    settings = get_settings()
    OUT_DIR.mkdir(exist_ok=True)
    image = (OUT_DIR / "grey-test-image.png").resolve()
    grey_png(image)

    users = {name: str(uuid.uuid4()) for name in ("A", "B", "C")}
    values = [
        {"key": "baseUrl", "value": "http://localhost:8000"},
        {"key": "consentVersion", "value": settings.consent_statement_version},
        {"key": "supportEmail", "value": settings.support_email},
        {"key": "imagePath", "value": str(image)},
    ]
    for name, subject in users.items():
        values.append({"key": f"subject{name}", "value": subject})
        values.append({"key": f"token{name}", "value": token(subject), "type": "secret"})

    env = {"name": "Skincare Advisor - local", "values": values}
    out = OUT_DIR / "local.postman_environment.json"
    out.write_text(json.dumps(env, indent=2), encoding="utf-8")
    print(f"wrote {out} (tokens valid 2h) and {image.name}")


if __name__ == "__main__":
    main()
