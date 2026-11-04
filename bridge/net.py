"""HTTPS plumbing shared by the Telegram and AI clients: IPv4 first, and reused connections."""
import http.client
import secrets
import socket
import threading
import time
from typing import Any, Callable

DNS_CACHE_S = 300
_getaddrinfo = socket.getaddrinfo
_dns_cache: dict[tuple[Any, ...], tuple[float, list[Any]]] = {}
_dns_lock = threading.Lock()


def cached_ipv4_first(*args: Any, **kwargs: Any) -> list[Any]:
    """getaddrinfo with a DNS_CACHE_S cache, IPv4 first.
    Hotspots lose DNS packets (each costs a resolver timeout) and some black-hole IPv6."""
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


def multipart(fields: dict[str, str], file_field: str, filename: str, data: bytes) -> tuple[bytes, str]:
    """A multipart/form-data body with text fields and one file, and its Content-Type."""
    boundary = secrets.token_hex(16)
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode() for name, value in fields.items()]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    return b"".join(parts) + data + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


class ConnectionPool:
    """Open HTTPS connections to one host, shared by all threads."""

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
        """Replace idle connections with one freshly opened. Servers drop idle connections, so call this periodically."""
        with self.lock:
            stale, self.idle = self.idle, []
        for conn in stale:
            conn.close()
        conn = http.client.HTTPSConnection(self.host, timeout=timeout)
        try:
            conn.connect()
        except OSError:
            return  # offline: the next request connects on demand
        with self.lock:
            self.idle.append(conn)

    def request(
        self,
        path: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: float,
        on_line: Callable[[bytes], None] | None = None,
        method: str = "POST",
    ) -> tuple[int, Any, bytes]:
        """Send and return (status, headers, body). Retries once if a kept-alive connection went stale.
        With on_line, a successful response is handed over line by line as it arrives (body is then b"")."""
        for attempt in (1, 2):
            conn = self.acquire(timeout)
            streamed = False
            try:
                conn.request(method, path, body, headers)
                response = conn.getresponse()
                if on_line and response.status == 200:
                    for line in response:
                        streamed = True
                        on_line(line)
                    data = b""
                else:
                    data = response.read()
            except Exception as error:
                conn.close()
                # A slow server isn't a stale connection, and a half-delivered answer can't be resent.
                stale = isinstance(error, (http.client.HTTPException, OSError)) and not isinstance(error, TimeoutError)
                if attempt == 2 or not stale or streamed:
                    raise
                continue
            with self.lock:
                self.idle.append(conn)
            return response.status, response.headers, data
        raise AssertionError("unreachable")
