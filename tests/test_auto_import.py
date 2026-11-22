from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.clients import FileClient, IntegrationError, NewsImportClient
from app.models import ArticleImport, Job
from app.worker import enqueue_due_imports, run_one

FIXTURES = Path(__file__).parent / "fixtures"


def test_scheduled_archive_publishes_text_and_retries_only_pending_work(
    db, source, settings, monkeypatch
):
    settings.auto_import_enabled = True
    settings.news_parser_token = "test-token"
    listing = (FIXTURES / "listing.html").read_text(encoding="utf-8")
    article_html = (FIXTURES / "article.html").read_text(encoding="utf-8")
    page2 = source.url + "?PAGEN_1=2"
    second_article = source.url + "older-story/"
    archive = {
        source.url: listing + '<a href="?PAGEN_1=2">2</a>',
        page2: listing.replace("test-story", "older-story"),
    }
    fetched, creates, covers = [], [], []

    def html(_, url):
        fetched.append(url)
        if url in archive:
            return archive[url]
        return article_html

    def create(_, data):
        creates.append(data["canonical_url"])
        if data["canonical_url"] == second_article and creates.count(second_article) == 1:
            raise IntegrationError("temporary failure")
        return str(uuid4())

    def cover(_, news_id, content):
        covers.append(news_id)

    current_time = datetime.now(timezone.utc)
    monkeypatch.setattr("app.worker.now", lambda: current_time)
    monkeypatch.setattr("app.fetcher.PublicFetcher.html", html)
    monkeypatch.setattr(NewsImportClient, "create", create)
    monkeypatch.setattr(NewsImportClient, "cover", cover)

    enqueue_due_imports(db, settings)
    enqueue_due_imports(db, settings)
    assert run_one(db, settings)
    job = db.scalars(select(Job)).one()
    assert job.status == "partial"
    assert job.result["discovered"] == 2
    assert len(job.result["article_ids"]) == 1
    assert page2 in fetched
    imported = db.scalars(select(ArticleImport).where(ArticleImport.news_id.is_not(None))).one()
    assert not imported.cover_attached
    assert not covers

    current_time += timedelta(minutes=29)
    enqueue_due_imports(db, settings)
    assert not run_one(db, settings)

    current_time += timedelta(minutes=1)
    settings.file_service_token = "test-file-token"
    monkeypatch.setattr("app.fetcher.PublicFetcher.get", lambda *args: (b"cover", "image/webp"))
    monkeypatch.setattr("app.services.normalize_image", lambda content, _: content)
    monkeypatch.setattr(FileClient, "upload", lambda *args: str(uuid4()))
    monkeypatch.setattr(FileClient, "download", lambda *args: b"cover")
    # Pending delivery is retried even if a previously fetched article disappears from the archive.
    archive[source.url] = listing
    enqueue_due_imports(db, settings)
    assert run_one(db, settings)
    assert fetched.count(source.url) == 2
    assert creates == [source.url + "test-story/", second_article, second_article]
    assert set(covers) == {record.news_id for record in db.scalars(select(ArticleImport))}
    assert imported.cover_attached

    current_time += timedelta(minutes=30)
    enqueue_due_imports(db, settings)
    assert run_one(db, settings)
    assert len(creates) == 3
    assert len(covers) == 2
