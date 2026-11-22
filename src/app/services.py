import hashlib
import json

from sqlalchemy import select

from app.adapters import get_adapter
from app.adapters.base import DiscoveredArticle
from app.clients import FileClient, IntegrationError, NewsClient, OutcomeUnknown, normalize_image
from app.fetcher import PublicFetcher
from app.models import Article, ArticleImage, Export, now


def export_payload(article, tags=None):
    # A source's publication date is not the publication date in our CMS.
    return {
        "title": article.title,
        "body": article.body,
        "status": "draft",
        "tags": tags or [],
        "published_at": None,
    }


def images_for(db, article_id, active=True):
    stmt = select(ArticleImage).where(ArticleImage.article_id == article_id)
    if active:
        stmt = stmt.where(ArticleImage.active.is_(True))
    return list(db.scalars(stmt.order_by(ArticleImage.position, ArticleImage.id)))


def save_article(db, source_id, parsed):
    digest = hashlib.sha256(
        json.dumps(
            [parsed.title, parsed.body, str(parsed.published_at), parsed.images], ensure_ascii=False
        ).encode()
    ).hexdigest()
    article = db.scalar(
        select(Article).where(Article.source_id == source_id, Article.canonical_url == parsed.url)
    )
    if article is None:
        article = Article(
            source_id=source_id,
            canonical_url=parsed.url,
            title=parsed.title,
            body=parsed.body,
            source_published_at=parsed.published_at,
            content_hash=digest,
        )
        db.add(article)
        db.flush()
    elif not article.manually_edited and article.content_hash != digest:
        article.title, article.body = parsed.title, parsed.body
        article.source_published_at = parsed.published_at
        article.content_hash, article.updated_at = digest, now()
    article.last_seen_at = now()
    existing = {im.source_url: im for im in images_for(db, article.id, active=False)}
    for im in existing.values():
        im.active = im.source_url in parsed.images
    for position, url in enumerate(parsed.images):
        if url in existing:
            existing[url].position = position
            existing[url].active = True
        else:
            db.add(ArticleImage(article_id=article.id, source_url=url, position=position))
    db.commit()
    return article


def store_images(db, article, fetcher, settings, retry_unknown=False):
    errors = []
    client = FileClient(settings)
    for image in images_for(db, article.id):
        if image.file_id:
            continue
        if image.status in {"uploading", "unknown"} and not retry_unknown:
            errors.append(
                {"image_id": image.id, "error": "Upload outcome unknown; reconcile first"}
            )
            continue
        try:
            # Avoid needless downloads when storage credentials are absent.
            if not settings.file_service_token:
                raise IntegrationError("FILE_SERVICE_TOKEN is not configured")
            content, _ = fetcher.get(image.source_url, settings.max_image_download_bytes)
            content = normalize_image(content, settings.max_image_download_bytes)
            if len(content) > settings.max_image_bytes:
                raise ValueError("Normalized image exceeds MAX_IMAGE_BYTES")
            image.status, image.error = "uploading", None
            db.commit()
            image.file_id = client.upload(content)
            image.status = "stored"
        except Exception as exc:
            image.status = "unknown" if isinstance(exc, OutcomeUnknown) else "failed"
            image.error = safe_error(exc)
            errors.append({"image_id": image.id, "error": image.error})
        db.commit()
    return errors


def safe_error(exc):
    # Do not persist tokens, connection strings or response bodies in job results.
    if isinstance(exc, (ValueError, IntegrationError)):
        return str(exc)[:500]
    return type(exc).__name__ + ": operation failed"


def parse_job(db, job, source, settings):
    adapter = get_adapter(source.adapter)
    fetcher = PublicFetcher(settings, adapter.allowed_hosts)
    urls = job.payload.get("urls", [])
    if urls:
        # FSP exposes cover images in the listing, not in the article detail.
        try:
            cards = adapter.discover(fetcher, source.url, 1, 500)
            covers = {c.url: c.cover_url for c in cards}
        except Exception:
            covers = {}
        cards = [DiscoveredArticle(url, covers.get(url)) for url in urls]
    else:
        cards = adapter.discover(
            fetcher, source.url, job.payload["max_pages"], job.payload["limit"]
        )
    result = {"discovered": len(cards), "processed": 0, "article_ids": [], "errors": []}
    for card in cards:
        try:
            parsed = adapter.parse(fetcher.html(card.url), card.url, card.cover_url)
            if len(parsed.images) > settings.max_images:
                raise ValueError("Article exceeds MAX_IMAGES; increase the configured limit")
            article = save_article(db, source.id, parsed)
            result["article_ids"].append(article.id)
            errors = store_images(db, article, fetcher, settings)
            result["errors"].extend({"url": card.url, **e} for e in errors)
            result["processed"] += 1
        except Exception as exc:
            db.rollback()
            result["errors"].append({"url": card.url, "error": safe_error(exc)})
        job.result = dict(result)
        db.commit()
    return result


def export_article(db, article, settings, tags):
    export = db.get(Export, article.id)
    if export is None:
        export = Export(article_id=article.id)
        db.add(export)
        db.commit()
    if export.status == "completed":
        return {"news_id": export.news_id, "already_exported": True}
    if export.status in {"creating", "unknown"}:
        raise IntegrationError("Previous creation outcome unknown; reconcile export first")
    client = NewsClient(settings)
    client.headers()  # Validate configuration before recording an in-flight request.
    try:
        if not export.news_id:
            export.status, export.error = "creating", None
            db.commit()
            try:
                export.news_id = client.create(export_payload(article, tags))
            except OutcomeUnknown:
                export.status = "unknown"
                raise
            except IntegrationError:
                export.status = "failed"
                raise
            export.status = "cover_pending"
            db.commit()
        images = images_for(db, article.id)
        if images:
            cover = images[0]
            if not cover.file_id:
                raise IntegrationError(
                    "News draft created; cover is not yet stored in file_service"
                )
            client.cover(export.news_id, FileClient(settings).download(cover.file_id))
        export.status, export.error, export.updated_at = "completed", None, now()
        db.commit()
        return {"news_id": export.news_id}
    except Exception as exc:
        export.error, export.updated_at = safe_error(exc), now()
        db.commit()
        raise
