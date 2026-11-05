"""Resolve the real client address behind the single trusted reverse proxy.

Production traffic always arrives through Caddy, so ``request.client.host`` is
the proxy's container address for everyone. Caddy (configured without its own
``trusted_proxies``) discards any client-supplied ``X-Forwarded-For`` and sets
it to the address it accepted the connection from. The header is therefore
only honoured when the direct peer is a configured trusted proxy, and only its
rightmost entry (the one that proxy wrote) is used; direct callers cannot
spoof it and left-hand values are never trusted.
"""

from __future__ import annotations

import ipaddress

from fastapi import Request


def client_source(request: Request) -> str:
    peer = request.client.host if request.client else ""
    settings = getattr(request.app.state, "settings", None)
    networks = getattr(settings, "trusted_proxies", ()) or ()
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return peer or "unknown"
    if not any(peer_address in network for network in networks):
        return str(peer_address)
    hops = [hop.strip() for hop in request.headers.get("x-forwarded-for", "").split(",") if hop.strip()]
    if not hops:
        return str(peer_address)
    try:
        # 只信任一跳代理：最右一项由 Caddy 写入，即它实际看到的连接来源。
        return str(ipaddress.ip_address(hops[-1]))
    except ValueError:
        return str(peer_address)
