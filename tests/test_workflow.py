from io import BytesIO
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from sqlalchemy import func, select

from app.adapters.base import ParsedArticle
from app.clients import FileClient, IntegrationError, NewsClient, OutcomeUnknown, normalize_image
from app.models import Article, Export, Job
from app.services import export_article, images_for, save_article, store_images
from app.worker import recover, run_one

FIXTURES = Path(__file__).parent / "fixtures"


def parsed(images=None):
    return ParsedArticle(
        "https://fsp-russia.ru/press/news/test-story/", "Title", "Body", None, images or []
    )


def image_bytes():
    buffer = BytesIO()
    Image.new("RGB", (20, 10), "red").save(buffer, "PNG")
    return buffer.getvalue()


def test_api_auth_validation_and_pagination(client, source):
    assert client.get("/api/articles", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/articles?size=101").status_code == 422
    assert client.get("/api/articles/not-a-uuid").status_code == 422
    assert client.get("/api/articles/" + str(uuid4())).status_code == 404
    assert (
        client.post(
            "/api/parse-jobs", json={"source_id": source.id, "urls": ["http://127.0.0.1/"]}
        ).status_code
        == 422
    )


def test_parse_api_worker_dedupe_and_file_retry(client, db, settings, source, monkeypatch):
    def html(self, url):
        name = "listing.html" if url.rstrip("/").endswith("news") else "article.html"
        return (FIXTURES / name).read_text("utf-8")

    monkeypatch.setattr("app.fetcher.PublicFetcher.html", html)
    response = client.post("/api/parse-jobs", json={"source_id": source.id})
    assert response.status_code == 202
    job_id = response.json()["id"]
    assert run_one(db, settings)
    job = client.get("/api/jobs/" + job_id).json()
    assert job["status"] == "partial"  # Text persists even without a file_service token.
    article_id = job["result"]["article_ids"][0]
    assert client.get("/api/articles").json()["total"] == 1
    assert client.get("/api/articles/" + article_id).json()["images"][0]["status"] == "failed"
    client.post("/api/jobs/" + job_id + "/retry")
    run_one(db, settings)
    assert db.scalar(select(func.count()).select_from(Article)) == 1
    settings.file_service_token = "allowed-test-token"
    monkeypatch.setattr("app.fetcher.PublicFetcher.get", lambda *a: (image_bytes(), "image/png"))
    monkeypatch.setattr(FileClient, "upload", lambda *a: str(uuid4()))
    client.post(f"/api/articles/{article_id}/images/retry")
    run_one(db, settings)
    detail = client.get("/api/articles/" + article_id).json()
    assert all(im["file_id"] and im["status"] == "stored" for im in detail["images"])
    assert detail["source_published_at"].endswith("Z")


def test_edit_preserved_and_export_contract(client, db, source):
    article = save_article(db, source.id, parsed())
    assert client.patch(f"/api/articles/{article.id}", json={"title": "Edited"}).status_code == 200
    save_article(db, source.id, parsed())
    assert db.get(Article, article.id).title == "Edited"
    preview = client.get(f"/api/articles/{article.id}/export").json()
    assert preview["news_create"] == {
        "title": "Edited",
        "body": "Body",
        "status": "draft",
        "tags": [],
        "published_at": None,
    }
    assert client.post(f"/api/articles/{article.id}/export", json={}).status_code == 409


def test_partial_export_retries_cover_without_recreating_news(db, source, settings, monkeypatch):
    article = save_article(db, source.id, parsed(["https://fsp-russia.ru/image.jpg"]))
    settings.news_export_enabled, settings.news_user_id = True, str(uuid4())
    calls = []
    news_id = str(uuid4())
    monkeypatch.setattr(NewsClient, "create", lambda *args: calls.append(args) or news_id)
    with pytest.raises(IntegrationError, match="cover"):
        export_article(db, article, settings, [])
    assert db.get(Export, article.id).news_id == news_id
    images_for(db, article.id)[0].file_id = str(uuid4())
    db.commit()
    monkeypatch.setattr(FileClient, "download", lambda *a: image_bytes())
    monkeypatch.setattr(NewsClient, "cover", lambda *a: None)
    assert export_article(db, article, settings, [])["news_id"] == news_id
    export_article(db, article, settings, [])
    assert len(calls) == 1


def test_uncertain_export_requires_reconcile(db, source, settings, monkeypatch, client):
    article = save_article(db, source.id, parsed())
    settings.news_export_enabled, settings.news_user_id = True, str(uuid4())

    def fail(*a):
        raise OutcomeUnknown("timeout")

    monkeypatch.setattr(NewsClient, "create", fail)
    with pytest.raises(OutcomeUnknown):
        export_article(db, article, settings, [])
    with pytest.raises(IntegrationError, match="reconcile"):
        export_article(db, article, settings, [])
    remote_id = str(uuid4())
    monkeypatch.setattr(NewsClient, "get", lambda *a: {"id": remote_id})
    response = client.post(
        f"/api/articles/{article.id}/export/reconcile", json={"remote_id": remote_id}
    )
    assert response.status_code == 200
    assert export_article(db, article, settings, [])["news_id"] == remote_id


def test_recovery_does_not_repeat_uncertain_upload(db, source, settings):
    article = save_article(db, source.id, parsed(["https://fsp-russia.ru/image.jpg"]))
    image = images_for(db, article.id)[0]
    image.status = "uploading"
    job = Job(kind="parse", source_id=source.id, status="running")
    db.add(job)
    db.commit()
    recover(db)
    assert job.status == "queued"
    assert image.status == "unknown"
    assert store_images(db, article, None, settings)[0]["image_id"] == image.id


def test_image_validation():
    normalized = normalize_image(image_bytes(), 10000)
    assert Image.open(BytesIO(normalized)).format == "WEBP"
    with pytest.raises(ValueError):
        normalize_image(b"<svg>unsafe</svg>", 10000)


def test_file_upload_contract(settings, monkeypatch):
    file_id = str(uuid4())
    settings.file_service_token = "test-token"
    original_client = httpx.Client

    def handler(request):
        assert request.url.path == "/internal/upload"
        assert request.headers["X-Internal-Token"] == "test-token"
        body = request.read()
        assert b'name="is_public"\r\n\r\ntrue' in body
        assert b"image/webp" in body
        return httpx.Response(201, json={"file_id": file_id})

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(handler))
    )
    assert FileClient(settings).upload(b"test-webp") == file_id


def test_news_public_mvp_rejection_is_not_uncertain(settings, monkeypatch):
    settings.news_export_enabled, settings.news_user_id = True, str(uuid4())
    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original_client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
    )
    with pytest.raises(IntegrationError, match="PUBLIC_MVP") as exc:
        NewsClient(settings).create({})
    assert not isinstance(exc.value, OutcomeUnknown)
