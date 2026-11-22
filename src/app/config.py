from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./news_parser.db"
    api_key: str = ""
    file_service_url: str = "http://localhost:8001"
    file_public_url: str = "http://localhost:8001"
    file_service_token: str = ""
    news_service_url: str = "http://localhost:8000"
    news_export_enabled: bool = False
    news_user_id: str = ""
    news_user_roles: str = "admin"
    # Only for a trusted internal network. Gateway deployments can use a bearer token instead.
    news_bearer_token: str = ""
    request_timeout: float = Field(20, gt=0, le=120)
    download_timeout: float = Field(120, gt=0, le=300)
    request_interval: float = Field(1, ge=0.1)
    worker_poll_seconds: float = Field(2, ge=0.1)
    max_html_bytes: int = Field(4 * 1024 * 1024, gt=0)
    max_image_bytes: int = Field(5 * 1024 * 1024, gt=0)
    max_image_download_bytes: int = Field(20 * 1024 * 1024, gt=0)
    max_images: int = Field(20, ge=1, le=100)
    user_agent: str = "NewsParser/1.0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
