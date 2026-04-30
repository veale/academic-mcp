"""Session-cookie auth for the webapp.

Cookie signing uses itsdangerous.TimestampSigner keyed by WEBAPP_SESSION_SECRET.
If that env var is absent, the secret is generated once and written to
``/config/webapp_session_secret`` so it survives container restarts.

Login is rate-limited to 5 attempts per minute per IP with a minimum 250 ms
response time on failure.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from pathlib import Path

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from starlette.requests import Request

# ---------------------------------------------------------------------------
# Session secret
# ---------------------------------------------------------------------------

_SESSION_MAX_AGE: int = 7 * 24 * 3600   # 7 days
_COOKIE_NAME: str = "wa_session"
_CONFIG_DIR: Path = Path(os.getenv("CONFIG_DIR", "/config"))


def _load_or_generate_secret() -> str:
    env = os.getenv("WEBAPP_SESSION_SECRET", "").strip()
    if env:
        return env

    secret_file = _CONFIG_DIR / "webapp_session_secret"
    try:
        if secret_file.exists():
            return secret_file.read_text().strip()
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        new_secret = secrets.token_hex(32)
        secret_file.write_text(new_secret)
        return new_secret
    except OSError:
        # /config not writable (dev mode) — use an ephemeral secret; sessions
        # won't survive restarts but the server still starts.
        return secrets.token_hex(32)


_SECRET: str = _load_or_generate_secret()
_SIGNER: TimestampSigner = TimestampSigner(_SECRET, sep=".")


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

def make_session_cookie() -> str:
    """Return a signed session token to set as the cookie value."""
    return _SIGNER.sign(b"ok").decode()


def verify_session_cookie(value: str) -> bool:
    """Return True when *value* is a valid, unexpired session cookie."""
    try:
        _SIGNER.unsign(value, max_age=_SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def get_session_cookie(request: Request) -> str | None:
    return request.cookies.get(_COOKIE_NAME)


# ---------------------------------------------------------------------------
# Login rate-limiter  (in-memory token bucket, per IP)
# ---------------------------------------------------------------------------

_BUCKET_CAPACITY: int = 5
_BUCKET_REFILL_RATE: float = 5 / 60   # tokens per second (5 per minute)
_FAILURE_MIN_DELAY: float = 0.25       # seconds

# {ip: (tokens_float, last_refill_time)}
_buckets: dict[str, tuple[float, float]] = {}


def _consume_login_token(ip: str) -> bool:
    """Consume one login token for *ip*.  Returns True if allowed, False if rate-limited."""
    now = time.monotonic()
    tokens, last = _buckets.get(ip, (float(_BUCKET_CAPACITY), now))
    # Refill
    elapsed = now - last
    tokens = min(float(_BUCKET_CAPACITY), tokens + elapsed * _BUCKET_REFILL_RATE)
    if tokens >= 1.0:
        _buckets[ip] = (tokens - 1.0, now)
        return True
    _buckets[ip] = (tokens, now)
    return False


async def attempt_login(request: Request, password: str) -> bool:
    """Validate *password* against MCP_API_KEY with rate-limiting.

    Returns True on success.  On failure always waits ≥250 ms before returning
    so that brute-force timing is defeated regardless of rate-limit state.
    """
    ip = request.client.host if request.client else "unknown"
    api_key = os.getenv("MCP_API_KEY", "")

    if not _consume_login_token(ip):
        await asyncio.sleep(_FAILURE_MIN_DELAY)
        return False

    ok = bool(api_key) and secrets.compare_digest(password, api_key)
    if not ok:
        await asyncio.sleep(_FAILURE_MIN_DELAY)
    return ok
