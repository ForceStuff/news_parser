"""Verify an existing parse job against the real file_service, without mocks."""

import hashlib
import os
from io import BytesIO

import httpx
import pytest
from PIL import Image

from app.clients import FileClient, normalize_image
from app.config import get_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_FILE_SERVICE_TESTS") != "1", reason="Set RUN_FILE_SERVICE_TESTS=1"
    ),
]


def test_job_images_are_stored_in_file_service():
    job_id = os.getenv("PARSER_TEST_JOB_ID")
    assert job_id, "Set PARSER_TEST_JOB_ID to a completed parse job UUID"
    api_url = os.getenv("PARSER_API_URL", "http://127.0.0.1:8002").rstrip("/")
    settings = get_settings()
    verified = []
    with httpx.Client(trust_env=False, timeout=30) as client:
        headers = {"X-API-Key": settings.api_key}
        job_response = client.get(f"{api_url}/api/jobs/{job_id}", headers=headers)
        job_response.raise_for_status()
        job = job_response.json()
        assert job["status"] == "completed", job["status"]
        for article_id in job["result"]["article_ids"]:
            response = client.get(f"{api_url}/api/articles/{article_id}", headers=headers)
            response.raise_for_status()
            for image in response.json()["images"]:
                assert image["status"] == "stored" and image["file_id"]
                file_url = settings.file_service_url.rstrip("/")
                metadata = client.get(
                    f"{file_url}/internal/files/{image['file_id']}",
                    headers={"X-Internal-Token": settings.file_service_token},
                )
                metadata.raise_for_status()
                download = client.get(image["url"])
                download.raise_for_status()
                assert len(download.content) == metadata.json()["size_bytes"]
                assert download.headers["content-type"].startswith("image/webp")
                with Image.open(BytesIO(download.content)) as decoded:
                    decoded.load()
                    assert decoded.format == "WEBP"
                    assert decoded.width <= 1920 and decoded.height <= 1080
                assert (
                    client.get(f"{file_url}/internal/files/{image['file_id']}").status_code == 401
                )
                verified.append(
                    {
                        "file_id": image["file_id"],
                        "bytes": len(download.content),
                        "sha256": hashlib.sha256(download.content).hexdigest(),
                    }
                )
    assert job["result"]["article_ids"] and verified, "No stored images were verified"


def test_upload_read_delete_roundtrip():
    """Upload a new test image and remove only that image after verification."""
    settings = get_settings()
    source = BytesIO()
    Image.new("RGB", (32, 24), "blue").save(source, "PNG")
    content = normalize_image(source.getvalue(), settings.max_image_bytes)
    file_id = FileClient(settings).upload(content)
    base = settings.file_service_url.rstrip("/")
    headers = {"X-Internal-Token": settings.file_service_token}
    with httpx.Client(trust_env=False, timeout=30) as client:
        try:
            response = client.get(f"{base}/files/{file_id}")
            response.raise_for_status()
            assert response.content == content
            metadata = client.get(f"{base}/internal/files/{file_id}", headers=headers)
            metadata.raise_for_status()
            assert metadata.json()["size_bytes"] == len(content)
        finally:
            deleted = client.delete(f"{base}/internal/files/{file_id}", headers=headers)
            deleted.raise_for_status()
        assert client.get(f"{base}/files/{file_id}").status_code == 404
