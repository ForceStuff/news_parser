from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceCreate(Input):
    name: str = Field(min_length=1, max_length=200)
    adapter: str = Field(min_length=1, max_length=50)
    url: str = Field(max_length=2048)


class SourcePatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None


class ParseCreate(Input):
    source_id: UUID
    urls: list[str] = Field(default_factory=list, max_length=500)
    limit: int = Field(20, ge=1, le=500)
    max_pages: int = Field(1, ge=1, le=50)


class ArticlePatch(Input):
    title: str | None = Field(default=None, min_length=1, max_length=2000)
    body: str | None = Field(default=None, min_length=1, max_length=1_000_000)


class ExportCreate(Input):
    tags: list[UUID] = Field(default_factory=list, max_length=100)


class Reconcile(Input):
    remote_id: UUID | None = None
    confirmed_absent: bool = False

    @model_validator(mode="after")
    def exactly_one(self):
        if bool(self.remote_id) == self.confirmed_absent:
            raise ValueError(
                "Provide remote_id OR confirmed_absent=true after checking the remote service"
            )
        return self


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    adapter: str
    url: str
    enabled: bool
    created_at: datetime


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    kind: str
    source_id: UUID | None
    article_id: UUID | None
    status: Literal["queued", "running", "completed", "partial", "failed"]
    payload: dict
    result: dict
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ImageOut(BaseModel):
    id: UUID
    source_url: str
    position: int
    file_id: UUID | None
    url: str | None
    status: str
    error: str | None


class ExportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    news_id: UUID | None
    status: str
    error: str | None
    updated_at: datetime


class ArticleOut(BaseModel):
    id: UUID
    source_id: UUID
    canonical_url: str
    title: str
    body: str
    body_format: Literal["plain_text"] = "plain_text"
    source_published_at: datetime | None
    manually_edited: bool
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime
    images: list[ImageOut]
    export: ExportOut | None
