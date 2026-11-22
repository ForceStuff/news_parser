"""One durable queue consumer. PostgreSQL advisory lock prevents duplicate workers."""

import argparse
import logging
import time
from contextlib import closing, contextmanager
from datetime import timedelta

from sqlalchemy import select, text

from app.adapters import get_adapter
from app.clients import NewsImportClient
from app.config import get_settings
from app.db import SessionLocal, engine
from app.fetcher import PublicFetcher
from app.models import Article, ArticleImage, Export, Job, Source, now
from app.services import export_article, parse_job, safe_error, store_images

log = logging.getLogger(__name__)


@contextmanager
def worker_lock():
    if engine.dialect.name == "postgresql":
        # Keep the lock on the same connection for the entire worker lifetime.
        with engine.connect() as connection:
            if not connection.scalar(text("SELECT pg_try_advisory_lock(72590131)")):
                raise RuntimeError("Another news_parser worker is already running")
            try:
                yield connection
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(72590131)"))
    else:
        # SQLite is local-development only. OS lock is released automatically after a crash.
        from pathlib import Path

        path = Path(engine.url.database).resolve().with_suffix(".worker.lock")
        with path.open("a+b") as lock:
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            import os

            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield None


def recover(db):
    for image in db.scalars(select(ArticleImage).where(ArticleImage.status == "uploading")):
        image.status, image.error = "unknown", "Worker stopped during upload; reconcile first"
    for export in db.scalars(select(Export).where(Export.status == "creating")):
        export.status = "unknown"
    for job in db.scalars(select(Job).where(Job.status == "running")):
        job.status = "failed" if job.kind == "export" else "queued"
        job.error = (
            "Worker restarted; inspect export state before retry" if job.kind == "export" else None
        )
    db.commit()


def enqueue_due_imports(db, settings):
    if not settings.auto_import_enabled:
        return
    cutoff = now() - timedelta(seconds=settings.auto_import_interval_seconds)
    for source_id in db.scalars(select(Source.id).where(Source.enabled.is_(True))):
        last_job = db.scalar(
            select(Job)
            .where(Job.kind == "auto_import", Job.source_id == source_id)
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(1)
        )
        if last_job and (
            last_job.status in {"queued", "running"}
            or (last_job.started_at or last_job.created_at) > cutoff
        ):
            continue
        db.add(Job(kind="auto_import", source_id=source_id))
    db.commit()


def run_one(db, settings):
    job = db.scalar(
        select(Job).where(Job.status == "queued").order_by(Job.created_at, Job.id).limit(1)
    )
    if not job:
        return False
    job.status, job.started_at = "running", now()
    db.commit()
    try:
        if job.kind in {"parse", "auto_import"}:
            source = db.get(Source, job.source_id)
            if not source or not source.enabled:
                raise ValueError("Source is disabled or missing")
            if job.kind == "auto_import":
                if not settings.auto_import_enabled or not settings.news_parser_token:
                    raise ValueError(
                        "Automatic import requires AUTO_IMPORT_ENABLED and NEWS_PARSER_TOKEN"
                    )
                with closing(NewsImportClient(settings)) as client:
                    result = parse_job(db, job, source, settings, import_client=client)
            else:
                result = parse_job(db, job, source, settings)
        elif job.kind == "images":
            article = db.get(Article, job.article_id)
            source = db.get(Source, article.source_id)
            fetcher = PublicFetcher(settings, get_adapter(source.adapter).allowed_hosts)
            result = {"errors": store_images(db, article, fetcher, settings)}
        elif job.kind == "export":
            result = export_article(
                db, db.get(Article, job.article_id), settings, job.payload.get("tags")
            )
        else:
            raise ValueError("Unknown job kind")
        job.result = result
        job.status = "partial" if result.get("errors") else "completed"
        if job.kind == "parse" and not result["processed"]:
            job.status = "failed"
        if job.kind == "auto_import" and not (result["processed"] or result["article_ids"]):
            job.status = "failed"
    except Exception as exc:
        db.rollback()
        job.status, job.error = "failed", safe_error(exc)
        log.warning("Job %s failed: %s", job.id, job.error)
    job.finished_at = now()
    db.commit()
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Process at most one job and exit")
    args = parser.parse_args()
    settings = get_settings()
    if settings.auto_import_enabled and not settings.news_parser_token:
        parser.error("NEWS_PARSER_TOKEN is required when AUTO_IMPORT_ENABLED is true")
    logging.basicConfig(level=logging.INFO)
    with worker_lock() as connection:
        with SessionLocal() as db:
            recover(db)
        while True:
            if connection is not None:
                connection.execute(text("SELECT 1"))  # Fail closed if the lock connection died.
            with SessionLocal() as db:
                enqueue_due_imports(db, settings)
                worked = run_one(db, settings)
            if args.once:
                return
            if not worked:
                time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
