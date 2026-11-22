import warnings
from io import BytesIO
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import httpx
from PIL import Image, ImageOps

from app.fetcher import PublicFetcher


class IntegrationError(RuntimeError):
    pass


class OutcomeUnknown(IntegrationError):
    """A non-idempotent upstream request may have succeeded. Do not retry blindly."""


def normalize_image(data: bytes, max_bytes: int) -> bytes:
    try:
        return _normalize_image(data, max_bytes)
    except (
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("Invalid or unsafe image") from exc


def _normalize_image(data: bytes, max_bytes: int) -> bytes:
    if not data or len(data) > max_bytes:
        raise ValueError("Image is empty or exceeds the size limit")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as source:
            if source.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("Only JPEG, PNG and WebP images are supported")
            if getattr(source, "n_frames", 1) != 1:
                raise ValueError("Animated images are not supported")
            source.load()
            image = ImageOps.exif_transpose(source)
            image.thumbnail((1920, 1080))
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            output = BytesIO()
            image.save(output, format="WEBP", quality=85)
    if output.tell() > max_bytes:
        raise ValueError("Normalized image exceeds the size limit")
    return output.getvalue()


class FileClient:
    def __init__(self, settings):
        self.settings = settings

    def upload(self, content: bytes) -> str:
        if not self.settings.file_service_token:
            raise IntegrationError("FILE_SERVICE_TOKEN is not configured")
        try:
            with httpx.Client(timeout=self.settings.request_timeout, trust_env=False) as client:
                response = client.post(
                    self.settings.file_service_url.rstrip("/") + "/internal/upload",
                    headers={"X-Internal-Token": self.settings.file_service_token},
                    data={"is_public": "true"},
                    files={"file": ("image.webp", content, "image/webp")},
                )
        except httpx.TransportError as exc:
            raise OutcomeUnknown(
                "File upload outcome unknown; check file_service before retry"
            ) from exc
        if response.status_code >= 500:
            raise OutcomeUnknown("File upload outcome unknown: upstream server error")
        if response.status_code != 201:
            raise IntegrationError(f"File upload rejected: HTTP {response.status_code}")
        try:
            return str(UUID(response.json()["file_id"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise OutcomeUnknown("File service returned an invalid upload response") from exc

    def download(self, file_id: str) -> bytes:
        # Public file reads carry no internal token, including redirects to object storage.
        with httpx.Client(timeout=self.settings.request_timeout, trust_env=False) as client:
            with client.stream(
                "GET", self.settings.file_service_url.rstrip("/") + f"/files/{UUID(file_id)}"
            ) as response:
                if response.is_redirect:
                    target = urljoin(str(response.url), response.headers.get("location", ""))
                    host = urlsplit(target).hostname
                    fetcher = PublicFetcher(self.settings, {host})
                    content, _ = fetcher.get(target, self.settings.max_image_bytes)
                    return content
                if response.status_code != 200:
                    raise IntegrationError(f"File download rejected: HTTP {response.status_code}")
                data = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    data.extend(chunk)
                    if len(data) > self.settings.max_image_bytes:
                        raise IntegrationError("Stored image exceeds the size limit")
                return bytes(data)


class NewsClient:
    def __init__(self, settings):
        self.settings = settings

    def headers(self):
        if not self.settings.news_export_enabled:
            raise IntegrationError("News export is disabled; PUBLIC_MVP does not allow writes")
        if self.settings.news_bearer_token:
            return {"Authorization": f"Bearer {self.settings.news_bearer_token}"}
        try:
            user_id = str(UUID(self.settings.news_user_id))
        except ValueError as exc:
            raise IntegrationError("NEWS_USER_ID is not configured") from exc
        return {"X-User-Id": user_id, "X-User-Roles": self.settings.news_user_roles}

    def create(self, payload: dict) -> str:
        headers = self.headers()
        try:
            with httpx.Client(timeout=self.settings.request_timeout, trust_env=False) as client:
                response = client.post(
                    self.settings.news_service_url.rstrip("/") + "/api/news",
                    headers=headers,
                    json=payload,
                )
        except httpx.TransportError as exc:
            raise OutcomeUnknown("News creation outcome unknown; reconcile before retry") from exc
        if response.status_code >= 500:
            raise OutcomeUnknown("News creation outcome unknown: upstream server error")
        if response.status_code != 201:
            raise IntegrationError(
                f"News creation rejected: HTTP {response.status_code}; "
                "PUBLIC_MVP returns 404 for writes"
            )
        try:
            return str(UUID(response.json()["id"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise OutcomeUnknown("News service returned an invalid creation response") from exc

    def cover(self, news_id: str, content: bytes):
        with httpx.Client(timeout=self.settings.request_timeout, trust_env=False) as client:
            response = client.put(
                self.settings.news_service_url.rstrip("/") + f"/api/news/{UUID(news_id)}/cover",
                headers=self.headers(),
                files={"file": ("cover.webp", content, "image/webp")},
            )
        if response.status_code != 200:
            raise IntegrationError(f"Cover upload failed: HTTP {response.status_code}")

    def get(self, news_id: str):
        with httpx.Client(timeout=self.settings.request_timeout, trust_env=False) as client:
            response = client.get(
                self.settings.news_service_url.rstrip("/") + f"/api/news/{UUID(news_id)}",
                headers=self.headers(),
            )
        if response.status_code != 200:
            raise IntegrationError(f"Cannot verify news: HTTP {response.status_code}")
        return response.json()
