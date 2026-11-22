"""Verify an existing parse job against the real file_service, without mocks."""

import argparse
import hashlib
import json
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

from app.config import get_settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    settings = get_settings()
    verified = []
    with httpx.Client(trust_env=False, timeout=30) as client:
        headers = {"X-API-Key": settings.api_key}
        job_response = client.get(f"http://127.0.0.1:8002/api/jobs/{args.job_id}", headers=headers)
        job_response.raise_for_status()
        job = job_response.json()
        assert job["status"] == "completed", job["status"]
        for article_id in job["result"]["article_ids"]:
            response = client.get(
                f"http://127.0.0.1:8002/api/articles/{article_id}", headers=headers
            )
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
    result = {
        "job_id": args.job_id,
        "articles": len(job["result"]["article_ids"]),
        "verified_images": len(verified),
        "files": verified,
    }
    Path(".scratch/runtime/files-verification.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {"articles": result["articles"], "verified_images": len(verified), "status": "passed"}
        )
    )


if __name__ == "__main__":
    main()
