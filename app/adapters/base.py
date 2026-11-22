from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class DiscoveredArticle:
    url: str
    cover_url: str | None = None


@dataclass
class ParsedArticle:
    url: str
    title: str
    body: str
    published_at: datetime | None
    images: list[str] = field(default_factory=list)


class Adapter(Protocol):
    allowed_hosts: set[str]

    def validate_listing(self, url: str) -> str: ...
    def validate_article(self, url: str) -> str: ...
    def discover(
        self, fetcher, url: str, max_pages: int, limit: int
    ) -> list[DiscoveredArticle]: ...
    def parse(self, html: str, url: str, cover_url: str | None = None) -> ParsedArticle: ...
