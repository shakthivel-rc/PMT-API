"""Liveness, readiness and a dependency report — three different questions.

They used to be two identical `SELECT 1`s and a third one, which meant a load balancer
asking "can this instance serve traffic" and an operator asking "what is wrong with it"
got the same answer.

| Endpoint | Question | Touches the database | Fails when |
|---|---|---|---|
| `/live` | is the process up | no | never — a 200 here means the interpreter is running |
| `/ready` | can it serve a request | yes | the database is unreachable |
| `/dependencies` | what state is each dependency in | yes | never; it reports, it does not judge |

Today the only dependency is the database. One added later (a cache, a queue, an upstream
API) is reported beside `database` and folded into `degraded`.

`/live` deliberately does no work. A liveness probe that queries the database restarts a
healthy container every time the database blinks, which turns a brief outage into a
restart loop.

All three are unauthenticated (see `tests/unit/test_route_inventory.py`, which requires
every public route to say why). They disclose no configuration: `/dependencies` reports each
dependency's state (`ok`, or `error: <ExceptionClass>`) and the `degraded` flag, never its
address, its DSN or its credential.
"""
from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from db.session import get_db
from schemas.response import SuccessResponse


router = APIRouter(prefix="/api/v1/health", tags=["Health"])


@router.get("/live")
def live():
    """Up. Nothing else is claimed, and nothing else is checked."""
    return SuccessResponse(status_code=200, message="Base Project API is live",
                           data={"status": "live"})


@router.get("/ready")
def ready(db: Session = Depends(get_db)):
    """Serving. One trivial query, which is what "ready" actually depends on."""
    db.execute(text("SELECT 1"))
    return SuccessResponse(status_code=200, message="Base Project API is ready",
                           data={"database": "ok"})


@router.get("/dependencies")
def dependencies(response: Response, db: Session = Depends(get_db)):
    """Everything an operator wants before they start reading logs.

    Returns 200 with `"degraded": true` rather than a 5xx when something is wrong: this is
    a diagnostic, and a diagnostic that itself fails to answer is the least useful thing it
    could do. The status is in the body, where a human and a dashboard can both read it.
    """
    database = "ok"
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        # The class name, never the message: a driver's connection error quotes the DSN,
        # and the DSN carries the password.
        database = f"error: {exc.__class__.__name__}"

    degraded = database != "ok"
    # So a probe or a scrape can alert on the header without parsing the body.
    response.headers["X-Health-Degraded"] = "true" if degraded else "false"

    return SuccessResponse(
        status_code=200,
        message="Dependencies degraded" if degraded else "Dependencies are healthy",
        data={"database": database, "degraded": degraded},
    )
