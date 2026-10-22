"""URL and network safety helpers for untrusted feed URLs."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


class UnsafeURLError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ValidatedURL:
    url: str
    hostname: str
    port: int
    pinned_ips: tuple[str, ...]

    def curl_resolve(self) -> str:
        if not self.pinned_ips or _literal_ip(self.hostname) is not None:
            return ""
        # CURLOPT_RESOLVE accepts comma-separated addresses for a host/port key.
        addresses = ",".join(f"[{ip}]" if ":" in ip else ip for ip in self.pinned_ips)
        return f"+{self.hostname}:{self.port}:{addresses}"


def _public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (
        ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved
    )


def _literal_ip(value: str):
    try:
        return ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None


async def validate_public_url(url: str, resolve_dns: bool = True) -> ValidatedURL:
    """Validate a public HTTP(S) URL and resolve every address before pinning it.

    The caller must apply ``ValidatedURL.curl_resolve()`` to the request itself;
    a prior DNS check without pinning would remain vulnerable to rebinding.
    Redirects must be validated independently.
    """
    if not isinstance(url, str) or any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise UnsafeURLError("URL contains control characters")
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme.lower() == "https" else 80)
        host = parsed.hostname
    except (ValueError, AttributeError) as exc:
        raise UnsafeURLError("invalid URL") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise UnsafeURLError("only credential-free HTTP(S) URLs are allowed")
    if port not in (80, 443):
        raise UnsafeURLError("non-standard ports are not allowed")
    try:
        literal = _literal_ip(host.rstrip("."))
        host = str(literal) if literal is not None else host.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError) as exc:
        raise UnsafeURLError("invalid hostname") from exc
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal", ".test")):
        raise UnsafeURLError("local hostnames are not allowed")
    literal = _literal_ip(host)
    if literal is not None:
        ips = (str(literal),)
    elif resolve_dns:
        try:
            records = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeURLError("DNS resolution failed") from exc
        ips = tuple(dict.fromkeys(record[4][0].split("%", 1)[0] for record in records))
        if not ips:
            raise UnsafeURLError("DNS returned no addresses")
    else:
        ips = ()
    if any(not _public_ip(ip) for ip in ips):
        raise UnsafeURLError("URL resolves to a non-public address")
    netloc_host = f"[{host}]" if isinstance(literal, ipaddress.IPv6Address) else host
    explicit_port = urlsplit(url.strip()).port
    netloc = netloc_host + (f":{explicit_port}" if explicit_port is not None else "")
    normalized = urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))
    return ValidatedURL(normalized, host, port, ips)
