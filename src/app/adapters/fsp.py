import re
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from app.adapters.base import DiscoveredArticle, ParsedArticle
from app.fetcher import validate_url

FSP_URL = "https://fsp-russia.ru/press/news/"


class FspAdapter:
    allowed_hosts = {"fsp-russia.ru", "www.fsp-russia.ru"}

    def validate_listing(self, url: str) -> str:
        validate_url(url, self.allowed_hosts)
        if urlsplit(url).path.rstrip("/") != "/press/news":
            raise ValueError("FSP listing must be /press/news/")
        return FSP_URL

    def validate_article(self, url: str) -> str:
        validate_url(url, self.allowed_hosts)
        path = urlsplit(url).path
        if not re.fullmatch(r"/press/news/[a-zA-Z0-9_\-]+/?", path):
            raise ValueError("Expected an FSP news article URL")
        return "https://fsp-russia.ru" + path.rstrip("/") + "/"

    def listing(self, html: str, url: str):
        soup = BeautifulSoup(html, "html.parser")
        articles = {}
        for link in soup.select(".archive_item a[href]"):
            try:
                target = self.validate_article(urljoin(url, link["href"]))
            except ValueError:
                continue
            img = link.select_one("img")
            cover = urljoin(url, img.get("data-src") or img.get("src", "")) if img else None
            articles.setdefault(target, DiscoveredArticle(target, cover))
        if not articles:
            raise ValueError("FSP listing layout changed or no article cards found")
        next_url = None
        # Follow only real pagination links; the site's empty 'more' button is not pagination.
        current = int(parse_qs(urlsplit(url).query).get("PAGEN_1", ["1"])[0])
        for link in soup.select("a[href]"):
            target = urljoin(url, link["href"])
            parts = urlsplit(target)
            pages = parse_qs(parts.query).get("PAGEN_1", [])
            if (
                parts.hostname in self.allowed_hosts
                and parts.path == "/press/news/"
                and pages
                and pages[0].isdigit()
                and int(pages[0]) == current + 1
            ):
                next_url = urlunsplit(
                    (
                        "https",
                        "fsp-russia.ru",
                        "/press/news/",
                        urlencode({"PAGEN_1": current + 1}),
                        "",
                    )
                )
                break
        return list(articles.values()), next_url

    def discover(self, fetcher, url: str, max_pages: int, limit: int):
        url = self.validate_listing(url)
        result = {}
        visited = set()
        for _ in range(max_pages):
            if not url or url in visited:
                break
            visited.add(url)
            cards, next_url = self.listing(fetcher.html(url), url)
            before = len(result)
            for card in cards:
                result.setdefault(card.url, card)
            if len(result) >= limit or len(result) == before:
                break
            url = next_url
        return list(result.values())[:limit]

    def parse(self, html: str, url: str, cover_url: str | None = None):
        url = self.validate_article(url)
        soup = BeautifulSoup(html, "html.parser")
        section = soup.select_one("section.calendar_detail")
        title = section.select_one("h1") if section else None
        content = section.select_one(".opis") if section else None
        if not title or not content:
            raise ValueError("FSP article layout changed: title or body missing")
        date = section.select_one(".item_cal.date")
        published = None
        if date:
            match = re.search(r"\d{2}\.\d{2}\.\d{4}", date.get_text())
            if match:
                published = datetime.strptime(match[0], "%d.%m.%Y").replace(
                    tzinfo=ZoneInfo("Europe/Moscow")
                )
        images = []
        candidates = [cover_url] if cover_url else []
        # Content images only: exclude sponsor logos and unrelated press cards.
        candidates.extend(img.get("data-src") or img.get("src") for img in content.select("img"))
        for candidate in candidates:
            if not candidate:
                continue
            target = urljoin(url, candidate)
            validate_url(target, self.allowed_hosts)
            if target not in images:
                images.append(target)
        for tag in content.select("script, style, iframe, form, noscript"):
            tag.decompose()
        for tag in content.select("br"):
            tag.replace_with("\n")
        for tag in content.select("p, div, h2, h3, h4, li"):
            tag.insert_before("\n")
            tag.insert_after("\n")
        body = "\n\n".join(line.strip() for line in content.get_text().splitlines() if line.strip())
        heading = title.get_text(" ", strip=True)
        if not heading or not body:
            raise ValueError("Empty FSP article")
        return ParsedArticle(url, heading, body, published, images)
