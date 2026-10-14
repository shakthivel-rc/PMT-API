"""PQL search and validation routes."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from controllers import pql_controller as ctl
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_guest
from schemas.errors import COMMON_ERRORS, VALIDATION_ERROR
from schemas.pql import (
    PQLExecuteRequest,
    PQLExecuteResponse,
    PQLValidateRequest,
    PQLValidateResponse,
)
from schemas.response import ErrorResponse, SuccessResponse

router = APIRouter(prefix="/workspaces/{workspace_id}/pql", tags=["Issues"])


@router.post(
    "/validate",
    response_model=SuccessResponse[PQLValidateResponse],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR},
    summary="Validate PQL query syntax and semantic fields",
    operation_id="validate_pql_query",
)
def validate_pql_query(
    body: PQLValidateRequest,
    access: WorkspaceAccess = Depends(as_guest),
    db: Session = Depends(get_db),
):
    return ctl.validate_query(access, body, db)


@router.post(
    "/execute",
    response_model=SuccessResponse[PQLExecuteResponse],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR},
    summary="Execute PQL query and return matching issues",
    operation_id="execute_pql_query",
)
def execute_pql_query(
    body: PQLExecuteRequest,
    access: WorkspaceAccess = Depends(as_guest),
    db: Session = Depends(get_db),
):
    return ctl.execute_query(access, body, db)
