from datetime import timedelta
from pathlib import Path

import pytest

from app.adapters.fsp import FSP_URL, FspAdapter
from app.fetcher import FetchError, public_address, validate_url

FIXTURES = Path(__file__).parent / "fixtures"


def test_listing_deduplicates_and_ignores_fake_more():
    cards, next_page = FspAdapter().listing((FIXTURES / "listing.html").read_text("utf-8"), FSP_URL)
    assert len(cards) == 1
    assert cards[0].url == FSP_URL + "test-story/"
    assert cards[0].cover_url == "https://fsp-russia.ru/upload/test.jpg"
    assert next_page is None


def test_real_pagination():
    html = (FIXTURES / "listing.html").read_text("utf-8") + '<a href="?PAGEN_1=2">2</a>'
    assert FspAdapter().listing(html, FSP_URL)[1] == FSP_URL + "?PAGEN_1=2"


def test_article_content_date_images():
    parsed = FspAdapter().parse(
        (FIXTURES / "article.html").read_text("utf-8"), FSP_URL + "test-story/", "/upload/test.jpg"
    )
    assert parsed.title == "Тестовая новость & соревнования"
    assert parsed.body == "Первый абзац.\n\nВторой абзац."
    assert parsed.published_at.utcoffset() == timedelta(hours=3)
    assert parsed.images == [
        "https://fsp-russia.ru/upload/test.jpg",
        "https://fsp-russia.ru/upload/inside.png",
    ]


def test_layout_changes_fail_visibly():
    with pytest.raises(ValueError, match="layout changed"):
        FspAdapter().parse("<h1>Error page</h1>", FSP_URL + "test-story/")


@pytest.mark.parametrize(
    "url",
    [
        "http://fsp-russia.ru/a",
        "https://127.0.0.1/",
        "https://fsp-russia.ru.evil.com/",
        "https://u:p@fsp-russia.ru/",
        "https://fsp-russia.ru:8443/",
        "file:///etc/passwd",
    ],
)
def test_ssrf_urls(url):
    with pytest.raises(FetchError):
        validate_url(url, FspAdapter.allowed_hosts)


def test_ssrf_private_dns(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(FetchError, match="Non-public"):
        public_address("fsp-russia.ru")
