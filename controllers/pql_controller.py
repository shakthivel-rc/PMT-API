"""Controller for PQL query validation and execution."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from schemas.response import SuccessResponse, error_json
from dependencies.workspace import WorkspaceAccess
from schemas.pql import PQLExecuteRequest, PQLValidateRequest
from services import issue as issue_svc
from utils.pql import PQLError, compile_pql_to_query, validate_pql


def validate_query(access: WorkspaceAccess, body: PQLValidateRequest, db: Session):
    is_valid, err = validate_pql(body.query)
    return SuccessResponse(
        status_code=200,
        message="PQL validated",
        data={"valid": is_valid, "error": err},
    )


def execute_query(access: WorkspaceAccess, body: PQLExecuteRequest, db: Session):
    try:
        stmt = compile_pql_to_query(
            body.query,
            workspace_id=access.workspace_id,
            current_user_id=access.user_id,
        )
    except PQLError as err:
        return error_json(422, str(err))

    # Calculate total count from the same filter criteria
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = db.scalar(count_stmt) or 0

    # Paginate
    paged_stmt = stmt.offset((body.page - 1) * body.limit).limit(body.limit)
    rows = db.scalars(paged_stmt).all()

    cards = issue_svc.cards(db, rows)
    return SuccessResponse(
        status_code=200,
        message="PQL executed",
        data={
            "records": cards,
            "total": total,
            "page": body.page,
            "limit": body.limit,
        },
    )
