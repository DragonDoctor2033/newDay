"""
Global DNS-over-HTTPS shim for urllib3-based HTTP clients.

Once `install_global_doh()` is called, every new outgoing TCP connect made
through urllib3 (requests, etc.) has its hostname resolved via the configured
DoH resolver instead of the system resolver — so ISP-level DNS blocks don't
apply.

Bypassed (still use system DNS):
  - the DoH server itself (otherwise we'd recurse forever)
  - hostnames that are already IP literals
  - localhost / loopback names

If a DoH lookup fails (resolver down, network glitch), we fall through to
the system resolver instead of failing the connect — better degraded than
broken.
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from urllib3.util import connection as urllib3_connection

try:
    import dns.message
    import dns.rdatatype
    import requests
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False


def _load_resolver_url() -> str:
    """Resolver URL from NEWS_DOH_URL env var, else `doh_resolver_url` in config.json."""
    url = os.environ.get("NEWS_DOH_URL", "").strip()
    if url:
        return url
    cfg = Path(os.environ.get("NEWS_ROOT", Path(__file__).parent)) / "config.json"
    try:
        return str(json.loads(cfg.read_text(encoding="utf-8")).get("doh_resolver_url", "")).strip()
    except (OSError, ValueError):
        return ""


DOH_RESOLVER_URL = _load_resolver_url()
_DOH_HOST = urlparse(DOH_RESOLVER_URL).hostname or ""
_BYPASS_HOSTS = {"localhost", "ip6-localhost", _DOH_HOST}

_cache: dict[str, str] = {}
_installed = False


def _log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] doh: {msg}",
          file=sys.stderr)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _resolve(host: str) -> str | None:
    """Resolve `host` via DoH. Returns IPv4 string or None on failure."""
    if host in _cache:
        return _cache[host]
    try:
        q = dns.message.make_query(host, dns.rdatatype.A)
        r = requests.post(
            DOH_RESOLVER_URL,
            data=q.to_wire(),
            headers={
                "Content-Type": "application/dns-message",
                "Accept":       "application/dns-message",
            },
            timeout=10,
        )
        if r.status_code != 200:
            return None
        resp = dns.message.from_wire(r.content)
        for ans in resp.answer:
            for item in ans.items:
                if item.rdtype == dns.rdatatype.A:
                    _cache[host] = item.address
                    return item.address
    except Exception as e:
        _log(f"resolve {host!r} failed: {e}")
    return None


def install_global_doh() -> bool:
    """
    Install a urllib3 connection hook that DoH-resolves every hostname
    before connect. Idempotent. Returns True if installed (or already
    installed), False if dnspython/requests aren't importable.
    """
    global _installed
    if _installed:
        return True
    if not _AVAILABLE:
        _log("dnspython or requests not installed — DoH disabled")
        return False
    if not DOH_RESOLVER_URL:
        _log("no resolver configured (NEWS_DOH_URL / config.json doh_resolver_url) — DoH disabled")
        return False

    original = urllib3_connection.create_connection

    def patched(address, *args, **kwargs):
        host, port = address
        if host in _BYPASS_HOSTS or _is_ip_literal(host):
            return original(address, *args, **kwargs)
        ip = _resolve(host)
        if ip:
            return original((ip, port), *args, **kwargs)
        return original(address, *args, **kwargs)

    urllib3_connection.create_connection = patched
    _installed = True
    _log(f"installed global DoH via {DOH_RESOLVER_URL}")
    return True
