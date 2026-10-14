"""PQL request and response schemas."""
from pydantic import BaseModel, Field

from schemas.responses.issues import IssueCard


class PQLValidateRequest(BaseModel):
    query: str = Field(description="The PQL query expression to validate.")


class PQLValidateResponse(BaseModel):
    valid: bool = Field(description="Whether the PQL query syntax and fields are valid.")
    error: str | None = Field(default=None, description="Syntax or semantic error message if invalid.")


class PQLExecuteRequest(BaseModel):
    query: str = Field(description="The PQL query expression to execute.")
    limit: int = Field(default=50, ge=1, le=100, description="Max issues to return.")
    page: int = Field(default=1, ge=1, description="Page number, 1-indexed.")


class PQLExecuteResponse(BaseModel):
    records: list[IssueCard] = Field(description="Matching issue cards.")
    total: int = Field(description="Total issues matching the query across all pages.")
    page: int = Field(description="Current page.")
    limit: int = Field(description="Items per page.")
