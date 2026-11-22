import httpx
import pytest

from app.fetcher import FetchError, PublicFetcher


def transport(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr("app.fetcher.public_address", lambda host: "93.184.216.34")
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler))
    )


def test_connection_pins_dns_but_preserves_host_and_sni(settings, monkeypatch):
    def handler(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "fsp-russia.ru"
        assert request.extensions["sni_hostname"] == "fsp-russia.ru"
        return httpx.Response(200, text="<h1>ok</h1>", headers={"Content-Type": "text/html"})

    transport(monkeypatch, handler)
    assert (
        PublicFetcher(settings, {"fsp-russia.ru"}).html("https://fsp-russia.ru/") == "<h1>ok</h1>"
    )


def test_redirect_revalidates_destination(settings, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/secret"})

    transport(monkeypatch, handler)
    with pytest.raises(FetchError):
        PublicFetcher(settings, {"fsp-russia.ru"}).get("https://fsp-russia.ru/", 100)
    assert len(calls) == 1


def test_download_size_limit(settings, monkeypatch):
    transport(monkeypatch, lambda req: httpx.Response(200, content=b"x" * 101))
    with pytest.raises(FetchError, match="size"):
        PublicFetcher(settings, {"fsp-russia.ru"}).get("https://fsp-russia.ru/", 100)


def test_bounded_get_retry(settings, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) < 3 else 200, content=b"ok")

    transport(monkeypatch, handler)
    monkeypatch.setattr("app.fetcher.time.sleep", lambda n: None)
    assert PublicFetcher(settings, {"fsp-russia.ru"}).get("https://fsp-russia.ru/", 100)[0] == b"ok"
    assert len(calls) == 3
