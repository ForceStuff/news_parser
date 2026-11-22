"""Opt-in live read-only check. Does not call news_service or file_service."""

import argparse
import json

from app.adapters.fsp import FSP_URL, FspAdapter
from app.clients import normalize_image
from app.config import get_settings
from app.fetcher import PublicFetcher


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=2, choices=range(1, 6))
    args = parser.parse_args()
    settings = get_settings()
    adapter = FspAdapter()
    fetcher = PublicFetcher(settings, adapter.allowed_hosts)
    cards = adapter.discover(fetcher, FSP_URL, 1, args.limit)
    result = []
    for card in cards:
        article = adapter.parse(fetcher.html(card.url), card.url, card.cover_url)
        images = []
        for url in article.images:
            raw, _ = fetcher.get(url, settings.max_image_download_bytes)
            normalized = normalize_image(raw, settings.max_image_download_bytes)
            assert len(normalized) <= settings.max_image_bytes
            images.append({"downloaded_bytes": len(raw), "webp_bytes": len(normalized)})
        result.append(
            {
                "url": article.url,
                "title": article.title,
                "published_at": str(article.published_at),
                "body_characters": len(article.body),
                "images": images,
            }
        )
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
