from typing import Optional

from pydantic import BaseModel, Field


class SessionOidcStartRequest(BaseModel):
    returnTo: Optional[str] = None


class ServiceApplicationRequest(BaseModel):
    message: Optional[str] = ""


class LiveKitTokenRequest(BaseModel):
    roomName: str = Field(min_length=1)
