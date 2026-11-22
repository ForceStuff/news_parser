"""Bounded public HTTP downloads. DNS is validated and pinned for each hop."""

import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit

import httpx


class FetchError(ValueError):
    pass


def validate_url(url: str, allowed_hosts: set[str]) -> str:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in allowed_hosts
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
        ):
            raise ValueError()
        if "\\" in url or any(ord(c) < 32 for c in url):
            raise ValueError()
    except ValueError as exc:
        raise FetchError("URL must use HTTPS and an adapter-approved host") from exc
    return url


def public_address(host: str) -> str:
    addresses = list(
        dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
    )
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise FetchError("Non-public destination blocked")
    return addresses[0]


class PublicFetcher:
    def __init__(self, settings, allowed_hosts: set[str]):
        self.settings = settings
        self.allowed_hosts = allowed_hosts
        self.last_request = 0.0

    def get(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        with httpx.Client(timeout=self.settings.request_timeout, trust_env=False) as client:
            for _ in range(6):
                validate_url(url, self.allowed_hosts)
                host = urlsplit(url).hostname
                ip = public_address(host)
                pinned = httpx.URL(url).copy_with(host=ip)
                for attempt in range(3):
                    time.sleep(
                        max(
                            0,
                            self.settings.request_interval - (time.monotonic() - self.last_request),
                        )
                    )
                    self.last_request = time.monotonic()
                    try:
                        with client.stream(
                            "GET",
                            pinned,
                            headers={"Host": host, "User-Agent": self.settings.user_agent},
                            extensions={"sni_hostname": host},
                        ) as response:
                            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                                time.sleep(2**attempt)
                                continue
                            if response.is_redirect:
                                url = urljoin(url, response.headers.get("location", ""))
                                break
                            response.raise_for_status()
                            data = bytearray()
                            deadline = time.monotonic() + self.settings.download_timeout
                            for chunk in response.iter_bytes(chunk_size=65536):
                                data.extend(chunk)
                                if len(data) > max_bytes:
                                    raise FetchError("Download exceeded size limit")
                                if time.monotonic() > deadline:
                                    raise FetchError("Download exceeded time limit")
                            return bytes(data), response.headers.get("content-type", "")
                    except httpx.TransportError:
                        if attempt == 2:
                            raise FetchError("Public HTTP download failed") from None
                else:
                    raise FetchError("Download failed")
            raise FetchError("Too many redirects")

    def html(self, url: str) -> str:
        data, content_type = self.get(url, self.settings.max_html_bytes)
        if "html" not in content_type.lower():
            raise FetchError("Expected HTML")
        return data.decode("utf-8-sig", errors="replace")
