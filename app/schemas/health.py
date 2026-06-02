"""Response schemas for health/diagnostic endpoints."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str


class DBHealthResponse(BaseModel):
    status: str
    database: str


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    """Matches the project-wide error envelope.

    {"error": {"code": "...", "message": "..."}}
    """

    error: ErrorBody
