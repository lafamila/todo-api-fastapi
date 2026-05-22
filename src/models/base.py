from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel


class CreateProjectRequest(BaseModel):
    name: str
    icon: str
    isSecret: bool
    password: Optional[str] = None


class CreateMemoRequest(BaseModel):
    projectId: str
    title: str


class UpdateMemoRequest(BaseModel):
    content: str


class VerifyPasswordRequest(BaseModel):
    password: str


class BulkDeleteMemosRequest(BaseModel):
    memoIds: List[str]


class LoginRequest(BaseModel):
    username: str
    password: str


class InviteMemberRequest(BaseModel):
    userId: str
    role: Literal["editor", "viewer"] = "viewer"
    username: Optional[str] = None
    displayName: Optional[str] = None
    email: Optional[str] = None


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
    isSecret: bool
    createdAt: datetime
    updatedAt: datetime


class Memo(BaseModel):
    id: str
    projectId: str
    title: str
    content: str
    createdAt: datetime
    updatedAt: datetime
