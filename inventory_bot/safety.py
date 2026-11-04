"""Validation shared by private inventory and public catalogue."""
import http.client
import ipaddress
import math
import re
import socket
import ssl
from urllib.parse import urlsplit, urljoin


def quantity(value, *, positive=False):
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > 1e12 or (positive and result == 0):
        raise ValueError("Количество должно быть конечным неотрицательным числом")
    return result


def item_id_valid(value):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value or ""))


def web_url(value):
    try:
        p = urlsplit(value)
        return bool(p.scheme in ("http", "https") and p.hostname and not p.username
                    and not p.password and not any(ord(c) < 32 for c in value))
    except (TypeError, ValueError):
        return False


def download_public(url, *, limit=6_000_000, redirects=3):
    """Bounded download, no proxies, connect only to a validated public IP.

    Pin the socket to the resolved address (TLS still checks the original host),
    and validate every redirect. This also prevents a DNS rebinding race.
    """
    if not web_url(url):
        raise ValueError("Ожидается публичная HTTP(S) ссылка")
    p = urlsplit(url)
    port = p.port or (443 if p.scheme == "https" else 80)
    if port not in (80, 443):
        raise ValueError("Недопустимый порт")
    addresses = socket.getaddrinfo(p.hostname, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Локальные адреса запрещены")
    address = addresses[0][4]
    conn = http.client.HTTPConnection(p.hostname, port, timeout=20)
    sock = socket.create_connection(address[:2], timeout=20)
    if p.scheme == "https":
        try:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=p.hostname)
        except Exception:
            sock.close()
            raise
    conn.sock = sock
    try:
        conn.request("GET", (p.path or "/") + ("?" + p.query if p.query else ""),
                     headers={"User-Agent": "Zavhoz/1.0", "Accept-Encoding": "identity"})
        response = conn.getresponse()
        if response.status in (301, 302, 303, 307, 308):
            if redirects <= 0:
                raise ValueError("Слишком много перенаправлений")
            target = urljoin(url, response.getheader("Location", ""))
            conn.close()
            return download_public(target, limit=limit, redirects=redirects - 1)
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Слишком большой ответ")
        return data, response.getheader("Content-Type", "").split(";")[0]
    finally:
        conn.close()


def image_extension(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("Неизвестный формат изображения")
