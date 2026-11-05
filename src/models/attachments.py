from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MemoAttachment(BaseModel):
    id: str
    memoId: str
    fileName: str
    contentType: str
    byteSize: int
    isImage: bool
    status: Literal["uploading", "ready", "failed"]
    createdBy: str
    createdAt: datetime
    updatedAtUtc: datetime


class DownloadAttachmentsRequest(BaseModel):
    attachmentIds: list[str] = Field(min_length=1)


class DeleteAttachmentsRequest(BaseModel):
    attachmentIds: list[str] = Field(min_length=1)


class DeleteAttachmentsResponse(BaseModel):
    deletedIds: list[str]
    deletedCount: int
