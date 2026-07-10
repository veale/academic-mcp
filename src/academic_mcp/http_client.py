"""Process-wide pooled httpx clients.

Every external API call used to build its own ``httpx.AsyncClient``, which
means a fresh TCP + TLS handshake to Semantic Scholar, OpenAlex, Crossref,
Primo and Scite on *every* call — typically 100–300 ms each, paid again on
the next search. These pooled clients keep connections warm for the life of
the process.

Two clients are kept: a direct one and (when ``GOST_PROXY_URL`` is set) a
proxied one. Per-request headers are passed at call time rather than baked
into the client, so a single pool serves callers with different auth.

HTTP/2 is enabled when the ``h2`` package is importable; without it httpx
raises on ``http2=True``, so the flag tracks availability rather than intent.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from .config import config

logger = logging.getLogger(__name__)

try:  # h2 ships with httpx[http2]; degrade to HTTP/1.1 when absent.
    import h2  # noqa: F401

    _HTTP2_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on install extras
    _HTTP2_AVAILABLE = False


_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_LIMITS = httpx.Limits(
    max_connections=32,
    max_keepalive_connections=16,
    keepalive_expiry=90.0,
)

# proxied -> (owning event loop, client). A client is bound to the loop that
# created it, so the loop is stored alongside and the pair is rebuilt whenever
# we find ourselves on a different (or closed) loop — as happens under pytest.
_clients: dict[bool, tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}


def get_client(*, proxied: bool = False) -> httpx.AsyncClient:
    """Return the shared client for this event loop.

    ``proxied=True`` routes through ``GOST_PROXY_URL`` when configured; with
    no proxy configured it returns the same client as ``proxied=False``.
    """
    if proxied and not config.gost_proxy_url:
        proxied = False

    loop = asyncio.get_running_loop()
    entry = _clients.get(proxied)
    if entry is not None:
        prev_loop, client = entry
        if prev_loop is loop and not client.is_closed:
            return client
        # Stale: belongs to a loop that has gone away. Drop it — closing needs
        # its own loop, which is already gone, so the sockets die with it.
        _clients.pop(proxied, None)

    kwargs: dict = {
        "timeout": _TIMEOUT,
        "follow_redirects": True,
        "limits": _LIMITS,
        "http2": _HTTP2_AVAILABLE,
    }
    if proxied:
        kwargs["proxy"] = config.gost_proxy_url

    client = httpx.AsyncClient(**kwargs)
    _clients[proxied] = (loop, client)
    return client


async def aclose_all() -> None:
    """Close every pooled client owned by the running loop."""
    loop = asyncio.get_running_loop()
    for proxied, (owner, client) in list(_clients.items()):
        if owner is not loop:
            continue
        _clients.pop(proxied, None)
        try:
            await client.aclose()
        except Exception as e:  # pragma: no cover - shutdown best effort
            logger.debug("Closing pooled client failed: %s", e)
