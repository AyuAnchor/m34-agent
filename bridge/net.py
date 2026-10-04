"""HTTPS plumbing shared by the Telegram and AI clients: IPv4 first, and reused connections."""
import http.client
import socket
import threading
import time
from typing import Any

DNS_CACHE_S = 300
_getaddrinfo = socket.getaddrinfo
_dns_cache: dict[tuple[Any, ...], tuple[float, list[Any]]] = {}
_dns_lock = threading.Lock()


def cached_ipv4_first(*args: Any, **kwargs: Any) -> list[Any]:
    """getaddrinfo with a 5-minute cache, IPv4 first.
    Cache: a lost DNS packet on a hotspot costs a resolver timeout (seconds), so look names up rarely.
    IPv4 first: mobile hotspots sometimes black-hole IPv6, stalling new connections ~35s."""
    key = (*args, *sorted(kwargs.items()))
    now = time.time()
    with _dns_lock:
        hit = _dns_cache.get(key)
    if hit and now - hit[0] < DNS_CACHE_S:
        return hit[1]
    result = sorted(_getaddrinfo(*args, **kwargs), key=lambda info: info[0] != socket.AF_INET)
    with _dns_lock:
        _dns_cache[key] = (now, result)
    return result


socket.getaddrinfo = cached_ipv4_first


class ConnectionPool:
    """Open HTTPS connections to one host, shared by all threads. A warm connection answers in
    ~0.2-1s; a new one costs DNS + TCP + TLS, 1-6s on a mobile hotspot."""

    def __init__(self, host: str) -> None:
        self.host = host
        self.idle: list[http.client.HTTPSConnection] = []
        self.lock = threading.Lock()

    def acquire(self, timeout: float) -> http.client.HTTPSConnection:
        with self.lock:
            conn = self.idle.pop() if self.idle else None
        if conn is None:
            conn = http.client.HTTPSConnection(self.host, timeout=timeout)
        conn.timeout = timeout
        if conn.sock:
            conn.sock.settimeout(timeout)
        return conn

    def refresh(self, timeout: float = 30) -> None:
        """Swap idle connections for one freshly opened, so the next request doesn't pay the setup cost.
        Servers drop idle connections after a while; calling this every couple of minutes keeps one warm."""
        with self.lock:
            stale, self.idle = self.idle, []
        for conn in stale:
            conn.close()
        conn = http.client.HTTPSConnection(self.host, timeout=timeout)
        try:
            conn.connect()
        except OSError:
            return  # offline right now; the next request will connect on demand
        with self.lock:
            self.idle.append(conn)

    def request(self, path: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, Any, bytes]:
        """POST and return (status, headers, body). Retries once if a kept-alive connection went stale."""
        for attempt in (1, 2):
            conn = self.acquire(timeout)
            try:
                conn.request("POST", path, body, headers)
                response = conn.getresponse()
                data = response.read()
            except (http.client.HTTPException, OSError):
                conn.close()
                if attempt == 2:
                    raise
                continue
            with self.lock:
                self.idle.append(conn)
            return response.status, response.headers, data
        raise AssertionError("unreachable")
