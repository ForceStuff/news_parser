import hashlib
import json

from sqlalchemy import or_, select

from app.adapters import get_adapter
from app.adapters.base import DiscoveredArticle
from app.clients import FileClient, IntegrationError, NewsClient, OutcomeUnknown, normalize_image
from app.fetcher import PublicFetcher
from app.models import Article, ArticleImage, ArticleImport, Export, now


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


def save_article(db, source_id, parsed, *, queue_import=False):
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
    if queue_import and db.get(ArticleImport, article.id) is None:
        db.add(ArticleImport(article_id=article.id))
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


def parse_job(db, job, source, settings, import_client=None):
    adapter = get_adapter(source.adapter)
    fetcher = PublicFetcher(settings, adapter.allowed_hosts)
    urls = job.payload.get("urls", [])
    if import_client is not None:
        cards = adapter.archive(fetcher, source.url)
    elif urls:
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
    result = {
        "discovered": len(cards) if import_client is None else 0,
        "processed": 0,
        "article_ids": [],
        "errors": [],
    }
    seen = set()

    def process(article):
        if import_client is None:
            result["article_ids"].append(article.id)
            errors = store_images(db, article, fetcher, settings)
        else:
            seen.add(article.id)
            confirmed, errors = publish_and_store_images(
                db, article, source.adapter, import_client, fetcher, settings
            )
            if confirmed:
                result["article_ids"].append(article.id)
        result["errors"].extend({"url": article.canonical_url, **error} for error in errors)

    try:
        for card in cards:
            if import_client is not None:
                result["discovered"] += 1
            try:
                parsed = adapter.parse(fetcher.html(card.url), card.url, card.cover_url)
                if len(parsed.images) > settings.max_images:
                    if import_client is None:
                        raise ValueError("Article exceeds MAX_IMAGES; increase the configured limit")
                    parsed.images = parsed.images[:settings.max_images]
                article = save_article(db, source.id, parsed, queue_import=import_client is not None)
                process(article)
                result["processed"] += 1
            except Exception as exc:
                db.rollback()
                result["errors"].append({"url": card.url, "error": safe_error(exc)})
            if import_client is None:
                job.result = dict(result)
                db.commit()
    except Exception as exc:
        if import_client is None:
            raise
        result["errors"].append({"url": source.url, "error": safe_error(exc)})

    if import_client is not None:
        has_images = select(ArticleImage.id).where(
            ArticleImage.article_id == Article.id, ArticleImage.active.is_(True)
        ).exists()
        pending = db.scalars(
            select(Article).join(ArticleImport).where(
                Article.source_id == source.id,
                or_(ArticleImport.news_id.is_(None), ~ArticleImport.cover_attached & has_images),
            )
        ).all()
        for article in pending:
            if article.id in seen:
                continue
            try:
                process(article)
            except Exception as exc:
                db.rollback()
                result["errors"].append({"url": article.canonical_url, "error": safe_error(exc)})
    return result


def publish_and_store_images(db, article, source, client, fetcher, settings):
    errors = []
    record = db.get(ArticleImport, article.id)
    confirmed = False
    if record.news_id is None:
        try:
            news_id = client.create({
                "source": source,
                "canonical_url": article.canonical_url,
                "title": article.title,
                "body": article.body,
                "source_published_at": (
                    article.source_published_at.isoformat() if article.source_published_at else None
                ),
            })
        except IntegrationError as exc:
            errors.append({"stage": "import", "error": safe_error(exc)})
        else:
            record.news_id = news_id
            db.commit()
            confirmed = True

    errors.extend(store_images(db, article, fetcher, settings))
    if record.news_id and not record.cover_attached:
        images = images_for(db, article.id)
        if images and images[0].file_id:
            try:
                client.cover(record.news_id, FileClient(settings).download(images[0].file_id))
            except Exception as exc:
                errors.append({"stage": "cover", "error": safe_error(exc)})
            else:
                record.cover_attached = True
                db.commit()
    return confirmed, errors


def export_article(db, article, settings, tags):
    automatic = db.get(ArticleImport, article.id)
    if automatic is not None and automatic.news_id:
        return {"news_id": automatic.news_id, "already_exported": True}
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
