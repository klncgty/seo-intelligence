"""SSRF protection.

Two layers:
1. ``check_url`` — cheap string-level policy (scheme, port, host allowlist, obvious internal names).
2. ``SafeNetworkBackend`` — the authoritative check. It resolves the hostname itself, rejects the
   connection if ANY resolved address is non-public, and then connects to that exact validated
   address. Because validation and connection use the same resolved IP there is no
   time-of-check/time-of-use gap and DNS rebinding cannot redirect the socket to an internal host.
   TLS still uses the original hostname for SNI/certificate verification (httpcore passes
   ``server_hostname`` separately to ``start_tls``).

Every redirect hop goes through both layers again (see fetcher.py).
"""

import asyncio
import ipaddress
import socket
from collections.abc import Iterable

import httpcore

from app.services.crawler.urls import host_of

BLOCKED_HOSTNAMES = {"localhost", "metadata", "metadata.google.internal", "instance-data"}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home.arpa")
ALLOWED_PORTS = {80, 443}


class BlockedDestination(Exception):
    """Raised when a URL or resolved address is not allowed."""


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return is_public_ip(ip.ipv4_mapped)
        if ip.sixtofour:  # 6to4 relays are blocked outright (spec §9)
            return False
        if ip.teredo:
            return False
        if ip in ipaddress.ip_network("64:ff9b::/96") or ip in ipaddress.ip_network("64:ff9b:1::/48"):
            return False  # NAT64 translation prefixes are blocked outright (spec §9)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def check_url(url: str, allowed_hosts: Iterable[str], allow_private: bool = False) -> None:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedDestination(f"scheme not allowed: {parts.scheme}")
    host = host_of(url)
    if not host:
        raise BlockedDestination("missing host")
    allowed = {h.lower().rstrip(".") for h in allowed_hosts}
    if host not in allowed:
        raise BlockedDestination(f"host not in allowlist: {host}")
    if allow_private:
        return
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise BlockedDestination(f"port not allowed: {port}")
    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        raise BlockedDestination(f"internal hostname: {host}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if not is_public_ip(ip):
        raise BlockedDestination(f"non-public address: {ip}")


async def resolve_public(host: str, port: int, allow_private: bool = False) -> list[str]:
    """Resolve host and return addresses; raise if any address is non-public."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedDestination(f"DNS resolution failed for {host}") from exc
    addrs: list[str] = []
    for *_, sockaddr in infos:
        addr = sockaddr[0].split("%")[0]
        ip = ipaddress.ip_address(addr)
        if not allow_private and not is_public_ip(ip):
            # Reject the whole host: mixed public/private answers are a rebinding signal.
            raise BlockedDestination(f"{host} resolves to non-public address {ip}")
        if addr not in addrs:
            addrs.append(addr)
    if not addrs:
        raise BlockedDestination(f"no addresses for {host}")
    return addrs


class SafeNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, allow_private: bool = False, inner: httpcore.AsyncNetworkBackend | None = None):
        self.allow_private = allow_private
        self.inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addrs = await resolve_public(host, port, self.allow_private)
        last_exc: Exception | None = None
        for addr in addrs:
            try:
                return await self.inner.connect_tcp(addr, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last_exc = exc
        raise httpcore.ConnectError(f"could not connect to {host}") from last_exc

    async def connect_unix_socket(self, *args, **kwargs):
        raise BlockedDestination("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)
