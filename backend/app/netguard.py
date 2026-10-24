"""In-process egress guard (defense in depth, spec section 4).

The API process holds patient data and must never open internet connections.
A Python audit hook (which cannot be removed once installed) rejects
``socket.connect`` to any address that is not loopback or explicitly
allowlisted (e.g. a private Ollama route). The evidence gateway runs as a
separate process and does not install this guard.

This complements, and does not replace, egress denial at the OS / container
boundary.
"""
from __future__ import annotations

import ipaddress
import socket
import sys
from urllib.parse import urlparse

_allowed: set[str] = set()
_installed = False


class EgressBlocked(PermissionError):
    pass


def _is_local(host: str) -> bool:
    if host in _allowed:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or str(ip) in _allowed


def _hook(event: str, args: tuple) -> None:
    if event != "socket.connect":
        return
    sock, addr = args[0], args[1]
    if getattr(sock, "family", None) == socket.AF_UNIX:
        return
    host = addr[0] if isinstance(addr, tuple) else str(addr)
    if not _is_local(host):
        raise EgressBlocked(f"egress to non-local address blocked in API process: {host}")


def install(*urls: str) -> None:
    global _installed
    for u in urls:
        h = urlparse(u).hostname
        if not h:
            continue
        _allowed.add(h)
        try:
            for info in socket.getaddrinfo(h, None):
                _allowed.add(info[4][0])
        except OSError:
            pass
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True
