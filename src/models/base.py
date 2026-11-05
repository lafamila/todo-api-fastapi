from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    name: str
    icon: str
    isSecret: bool
    password: Optional[str] = None
    parentProjectId: Optional[str] = None


class UpdateProjectRequest(BaseModel):
    name: str


class CreateMemoRequest(BaseModel):
    projectId: str
    title: str


class UpdateMemoRequest(BaseModel):
    content: str
    baseRevision: Optional[str] = None


class MoveMemoRequest(BaseModel):
    targetProjectId: str


class DeleteProjectRequest(BaseModel):
    confirmationName: str


class VerifyPasswordRequest(BaseModel):
    password: str


class BulkDeleteMemosRequest(BaseModel):
    memoIds: List[str]


class BulkMoveMemosRequest(BaseModel):
    memoIds: List[str]
    sourceProjectId: str
    targetProjectId: str
    sourceConfirmationName: str
    targetConfirmationName: str


class LoginRequest(BaseModel):
    username: str
    password: str


class InviteMemberRequest(BaseModel):
    userId: str
    role: Literal["editor", "viewer"] = "viewer"
    username: Optional[str] = None
    displayName: Optional[str] = None
    email: Optional[str] = None
    includeAllSubprojects: bool = False
    childProjectIds: List[str] = Field(default_factory=list)


class RegisterRequest(BaseModel):
    username: str
    password: str
    displayName: str


class UpdateAdminRequest(BaseModel):
    isAdmin: bool


class ChangePasswordRequest(BaseModel):
    oldPassword: str
    newPassword: str


class PublishArticleRequest(BaseModel):
    memoId: str


class Article(BaseModel):
    id: str
    memoId: str
    projectId: str
    authorId: str
    authorSlug: str
    title: str
    content: str
    publishedVersion: int
    createdAt: datetime
    publishedAt: datetime
    updatedAt: datetime


class Project(BaseModel):
    id: str
    name: str
    icon: str
    status: int = 0
    isSecret: bool
    parentProjectId: Optional[str] = None
    isGroup: bool = False
    createdAt: datetime
    updatedAt: datetime


class Memo(BaseModel):
    id: str
    projectId: str
    title: str
    content: str
    kind: Literal["memo", "backlog"] = "memo"
    status: int = 0
    createdAt: datetime
    updatedAt: datetime
    revision: str
