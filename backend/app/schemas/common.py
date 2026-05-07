"""Common response schemas."""
from typing import Any, Optional
from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    version: str
    db: str


class PaginatedResponse(BaseModel):
    items: list[Any]
    total: int
    page: int
    page_size: int
    has_more: bool


class MessageResponse(BaseModel):
    message: str
    success: bool = True
