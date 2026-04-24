from typing import Optional

from pydantic import BaseModel, field_validator
import re


class CreateTaskTypeRequest(BaseModel):
    name: str
    icon: str = ""
    color: str = "#3994ef"


class UpdateTaskTypeRequest(BaseModel):
    name: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    isActive: Optional[bool] = None
    displayOrder: Optional[int] = None


class CompleteTaskRequest(BaseModel):
    taskTypeId: str
    completedDate: str

    @field_validator("completedDate")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("completedDate must be YYYY-MM-DD format")
        return v
