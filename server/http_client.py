"""Proxy-aware HTTP opener shared by the ASR/LLM/TTS adapters.

The meeting server often runs behind a local HTTP proxy (config key
`realtime_proxy`). urllib's default opener would also pick up socks-style
proxies from environment variables, which it cannot speak, so every outbound
call goes through an explicitly built opener instead. Local/private hosts
(Ollama, intranet MCP or LLM endpoints) always bypass the proxy.

A configured proxy whose app is not running must not take the pipeline down
with ``URLError: [Errno 61] Connection refused``: ``effective_proxy`` probes
the proxy endpoint and falls back to a direct connection while it is down.
"""

import ipaddress
import os
import socket
import threading
import time
import urllib.request
from urllib.parse import urlparse

_PROXY_ENV_KEYS = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY")

_PROBE_TIMEOUT_S = 0.6
_ALIVE_TTL_S = 10.0
_DEAD_TTL_S = 4.0

# Hosts that always go direct: mainland-China endpoints are faster without the
# VPN/proxy (that is usually the whole point of configuring them).
DEFAULT_PROXY_BYPASS = ("volces.com", "volcengine.com", "volcengineapi.com")

_proxy_lock = threading.Lock()
_proxy_cache: dict[str, tuple[float, bool]] = {}


def proxy_url(config: dict | None) -> str:
    candidates = [str((config or {}).get("realtime_proxy", "")).strip()]
    candidates.extend(os.environ.get(key, "").strip() for key in _PROXY_ENV_KEYS)
    for candidate in candidates:
        if candidate.lower().startswith(("http://", "https://")):
            return candidate
    return ""


def _probe_proxy(proxy: str) -> bool:
    parsed = urlparse(proxy)
    host = parsed.hostname or ""
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def proxy_reachable(proxy: str) -> bool:
    """TCP-probe the proxy endpoint, caching results so requests stay cheap."""
    now = time.monotonic()
    with _proxy_lock:
        cached = _proxy_cache.get(proxy)
        if cached:
            cached_at, was_alive = cached
            ttl = _ALIVE_TTL_S if was_alive else _DEAD_TTL_S
            if now - cached_at < ttl:
                return was_alive
    alive = _probe_proxy(proxy)
    with _proxy_lock:
        _proxy_cache[proxy] = (now, alive)
    if not alive:
        print(f"[http_client] 代理 {proxy} 无法连接，暂时直连", flush=True)
    return alive


def effective_proxy(config: dict | None = None) -> str:
    """Proxy to actually use; empty when unset or currently unreachable."""
    proxy = proxy_url(config)
    if proxy and not proxy_reachable(proxy):
        return ""
    return proxy


def proxy_bypass_suffixes(config: dict | None = None) -> tuple[str, ...]:
    """Domain suffixes that bypass the proxy: built-ins + config proxy_bypass."""
    suffixes = list(DEFAULT_PROXY_BYPASS)
    extra = (config or {}).get("proxy_bypass")
    if isinstance(extra, str):
        extra = [s.strip() for s in extra.split(",") if s.strip()]
    if isinstance(extra, (list, tuple)):
        suffixes.extend(str(s).strip().lstrip(".") for s in extra if str(s).strip())
    return tuple(suffixes)


def _host_bypasses_proxy(host: str, config: dict | None = None) -> bool:
    if not host or host == "localhost":
        return host == "localhost"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        lowered = host.lower()
        return any(lowered == suffix or lowered.endswith("." + suffix)
                   for suffix in proxy_bypass_suffixes(config))
    return ip.is_loopback or ip.is_private or ip.is_link_local


def build_opener(config: dict | None = None,
                 url: str = "") -> urllib.request.OpenerDirector:
    """Opener for one request; `url` lets local hosts bypass the proxy."""
    if url and _host_bypasses_proxy(urlparse(url).hostname or "", config):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    proxy = effective_proxy(config)
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    # Empty handler: go direct and ignore any socks proxies in the environment.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))
