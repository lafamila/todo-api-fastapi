from typing import Optional

from pydantic import BaseModel, Field


class SessionLoginRequest(BaseModel):
    loginId: str = Field(min_length=1)
    password: str = Field(min_length=1)


class ServiceApplicationRequest(BaseModel):
    message: Optional[str] = ""


class LiveKitTokenRequest(BaseModel):
    roomName: str = Field(min_length=1)
