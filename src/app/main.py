from datetime import datetime
from hmac import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import APIKeyHeader
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters import ADAPTERS, get_adapter
from app.clients import FileClient, IntegrationError, NewsClient
from app.config import get_settings
from app.db import get_db
from app.models import Article, ArticleImage, Export, Job, Source, now
from app.schemas import (
    ArticleOut,
    ArticlePatch,
    ExportCreate,
    JobOut,
    ParseCreate,
    Reconcile,
    SourceCreate,
    SourceOut,
    SourcePatch,
)
from app.services import export_payload, images_for

app = FastAPI(
    title="News Parser",
    version="1.0.0",
    description=(
        "Independent news collection API. Article bodies are plain text. "
        "Jobs are processed by a separate worker; files are stored in file_service."
    ),
)
key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def authorize(key: str | None = Depends(key_header)):
    expected = get_settings().api_key
    if not expected:
        raise HTTPException(503, "API_KEY is not configured")
    if not key or not compare_digest(key.encode(), expected.encode()):
        raise HTTPException(401, "Invalid API key")


DB = Annotated[Session, Depends(get_db)]
AUTH = [Depends(authorize)]


def require(db, model, object_id):
    record = db.get(model, str(object_id))
    if record is None:
        raise HTTPException(404, "Not found")
    return record


def article_data(db, article):
    settings = get_settings()
    return {
        **{c.name: getattr(article, c.name) for c in Article.__table__.columns},
        "images": [
            {
                "id": im.id,
                "source_url": im.source_url,
                "position": im.position,
                "file_id": im.file_id,
                "status": im.status,
                "error": im.error,
                "url": f"{settings.file_public_url.rstrip('/')}/files/{im.file_id}"
                if im.file_id
                else None,
            }
            for im in images_for(db, article.id)
        ],
        "export": db.get(Export, article.id),
    }


def enqueue(db, job):
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


@app.get("/health")
def health():
    return {"status": "ok", "service": "news_parser"}


@app.get("/ready")
def ready(db: DB):
    try:
        db.execute(text("SELECT 1 FROM alembic_version"))
        db.execute(select(Source.id).limit(1))
    except Exception:
        raise HTTPException(503, "Database unavailable or migrations not applied") from None
    if not get_settings().api_key:
        raise HTTPException(503, "API_KEY is not configured")
    return {"status": "ok"}


@app.get("/api/adapters", dependencies=AUTH)
def adapters():
    return {"items": list(ADAPTERS)}


@app.get("/api/sources", response_model=list[SourceOut], dependencies=AUTH)
def sources(db: DB):
    return list(db.scalars(select(Source).order_by(Source.created_at)))


@app.post("/api/sources", response_model=SourceOut, status_code=201, dependencies=AUTH)
def create_source(data: SourceCreate, db: DB):
    try:
        url = get_adapter(data.adapter).validate_listing(data.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    source = Source(name=data.name, adapter=data.adapter, url=url)
    db.add(source)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Source already exists") from None
    return source


@app.patch("/api/sources/{source_id}", response_model=SourceOut, dependencies=AUTH)
def patch_source(source_id: UUID, data: SourcePatch, db: DB):
    source = require(db, Source, source_id)
    for key, value in data.model_dump(exclude_none=True).items():
        setattr(source, key, value)
    db.commit()
    return source


@app.post("/api/parse-jobs", response_model=JobOut, status_code=202, dependencies=AUTH)
def create_parse_job(data: ParseCreate, db: DB):
    source = require(db, Source, data.source_id)
    if not source.enabled:
        raise HTTPException(409, "Source disabled")
    adapter = get_adapter(source.adapter)
    try:
        urls = list(dict.fromkeys(adapter.validate_article(url) for url in data.urls))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return enqueue(
        db,
        Job(
            kind="parse",
            source_id=source.id,
            payload={"urls": urls, "limit": data.limit, "max_pages": data.max_pages},
        ),
    )


@app.get("/api/parse-jobs/{job_id}", response_model=JobOut, dependencies=AUTH)
@app.get("/api/jobs/{job_id}", response_model=JobOut, dependencies=AUTH)
def get_job(job_id: UUID, db: DB):
    return require(db, Job, job_id)


@app.get("/api/jobs", dependencies=AUTH)
def list_jobs(db: DB, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)):
    total = db.scalar(select(func.count()).select_from(Job))
    rows = db.scalars(
        select(Job).order_by(Job.created_at.desc(), Job.id).offset((page - 1) * size).limit(size)
    )
    return {
        "items": [JobOut.model_validate(row) for row in rows],
        "total": total,
        "page": page,
        "size": size,
    }


@app.post("/api/jobs/{job_id}/retry", response_model=JobOut, status_code=202, dependencies=AUTH)
def retry_job(job_id: UUID, db: DB):
    old = require(db, Job, job_id)
    if old.status not in {"failed", "partial"}:
        raise HTTPException(409, "Only failed or partial jobs can be retried")
    return enqueue(
        db,
        Job(kind=old.kind, source_id=old.source_id, article_id=old.article_id, payload=old.payload),
    )


@app.get("/api/articles", dependencies=AUTH)
def list_articles(
    db: DB,
    source_id: UUID | None = None,
    q: str | None = Query(None, max_length=200),
    published_from: datetime | None = None,
    published_to: datetime | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    stmt = select(Article)
    if source_id:
        stmt = stmt.where(Article.source_id == str(source_id))
    if q:
        stmt = stmt.where(Article.title.icontains(q, autoescape=True))
    if published_from:
        stmt = stmt.where(Article.source_published_at >= published_from)
    if published_to:
        stmt = stmt.where(Article.source_published_at <= published_to)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(
        stmt.order_by(Article.created_at.desc(), Article.id).offset((page - 1) * size).limit(size)
    )
    return {
        "items": [ArticleOut.model_validate(article_data(db, row)) for row in rows],
        "total": total,
        "page": page,
        "size": size,
    }


@app.get("/api/articles/{article_id}", response_model=ArticleOut, dependencies=AUTH)
def get_article(article_id: UUID, db: DB):
    return article_data(db, require(db, Article, article_id))


@app.patch("/api/articles/{article_id}", response_model=ArticleOut, dependencies=AUTH)
def patch_article(article_id: UUID, data: ArticlePatch, db: DB):
    article = require(db, Article, article_id)
    changes = data.model_dump(exclude_none=True)
    for key, value in changes.items():
        if not value.strip():
            raise HTTPException(422, "Text cannot be blank")
        setattr(article, key, value)
    if changes:
        article.manually_edited, article.updated_at = True, now()
    db.commit()
    return article_data(db, article)


@app.post(
    "/api/articles/{article_id}/images/retry",
    response_model=JobOut,
    status_code=202,
    dependencies=AUTH,
)
def retry_images(article_id: UUID, db: DB):
    article = require(db, Article, article_id)
    return enqueue(db, Job(kind="images", article_id=article.id))


@app.get("/api/articles/{article_id}/export", dependencies=AUTH)
def export_preview(article_id: UUID, db: DB):
    article = require(db, Article, article_id)
    data = ArticleOut.model_validate(article_data(db, article))
    return {
        "news_create": export_payload(article),
        "source_url": article.canonical_url,
        "source_published_at": article.source_published_at,
        "images": data.images,
        "cover_upload_endpoint": "PUT /api/news/{news_id}/cover",
        "export": data.export,
    }


@app.post(
    "/api/articles/{article_id}/export", response_model=JobOut, status_code=202, dependencies=AUTH
)
def create_export(article_id: UUID, data: ExportCreate, db: DB):
    article = require(db, Article, article_id)
    try:
        NewsClient(get_settings()).headers()
    except IntegrationError as exc:
        raise HTTPException(409, str(exc)) from exc
    return enqueue(
        db,
        Job(
            kind="export", article_id=article.id, payload={"tags": [str(tag) for tag in data.tags]}
        ),
    )


def no_active_job(db, article_id):
    if db.scalar(
        select(Job.id)
        .where(Job.article_id == article_id, Job.status.in_(["queued", "running"]))
        .limit(1)
    ):
        raise HTTPException(409, "Wait for active article jobs to finish")
    article = require(db, Article, article_id)
    if db.scalar(
        select(Job.id)
        .where(Job.source_id == article.source_id, Job.status.in_(["queued", "running"]))
        .limit(1)
    ):
        raise HTTPException(409, "Wait for active source jobs to finish")


@app.post("/api/articles/{article_id}/export/reconcile", dependencies=AUTH)
def reconcile_export(article_id: UUID, data: Reconcile, db: DB):
    article = require(db, Article, article_id)
    no_active_job(db, article.id)
    export = require(db, Export, article.id)
    if export.status not in {"unknown", "creating"}:
        raise HTTPException(409, "Only uncertain creation can be reconciled")
    if data.remote_id:
        try:
            NewsClient(get_settings()).get(str(data.remote_id))
        except Exception:
            raise HTTPException(502, "Could not verify remote news") from None
    export.news_id = str(data.remote_id) if data.remote_id else None
    export.status = "cover_pending" if data.remote_id else "pending"
    export.error, export.updated_at = None, now()
    db.commit()
    return {"status": export.status, "news_id": export.news_id}


@app.post("/api/images/{image_id}/reconcile", dependencies=AUTH)
def reconcile_image(image_id: UUID, data: Reconcile, db: DB):
    image = require(db, ArticleImage, image_id)
    no_active_job(db, image.article_id)
    if image.status not in {"unknown", "uploading"}:
        raise HTTPException(409, "Only uncertain upload can be reconciled")
    if data.remote_id:
        try:
            FileClient(get_settings()).download(str(data.remote_id))
        except Exception:
            raise HTTPException(502, "Could not verify remote image") from None
    image.file_id = str(data.remote_id) if data.remote_id else None
    image.status, image.error = "stored" if data.remote_id else "pending", None
    db.commit()
    return {"status": image.status, "file_id": image.file_id}
