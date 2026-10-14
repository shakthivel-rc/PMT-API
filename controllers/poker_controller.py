"""Controller for Sprint Planning Poker sessions."""
from __future__ import annotations

from sqlalchemy.orm import Session

from schemas.response import SuccessResponse, error_json
from dependencies.workspace import WorkspaceAccess
from models.issue import Issue
from models.sprint import Sprint
from schemas.poker import PokerAcceptRequest, PokerStartRequest, PokerVoteRequest
from services import poker as svc


def _get_sprint(db: Session, workspace_id: str, sprint_id: str) -> Sprint | None:
    return (
        db.query(Sprint)
        .filter(
            Sprint.id == sprint_id,
            Sprint.workspace_id == workspace_id,
            Sprint.deleted == 0,
        )
        .first()
    )


def start_session(
    access: WorkspaceAccess,
    sprint_id: str,
    body: PokerStartRequest,
    db: Session,
):
    sprint = _get_sprint(db, access.workspace_id, sprint_id)
    if not sprint:
        return error_json(404, "Sprint not found")

    issue = (
        db.query(Issue)
        .filter(Issue.id == body.issue_id, Issue.workspace_id == access.workspace_id)
        .first()
    )
    if not issue:
        return error_json(404, "Issue not found")

    session = svc.start_session(
        db,
        workspace_id=access.workspace_id,
        sprint_id=sprint_id,
        issue_id=body.issue_id,
        user_id=access.user_id,
    )
    state = svc.build_session_state(db, session, access.user_id)
    return SuccessResponse(
        status_code=201,
        message="Poker session started",
        data=state,
    )


def submit_vote(
    access: WorkspaceAccess,
    sprint_id: str,
    body: PokerVoteRequest,
    db: Session,
):
    sprint = _get_sprint(db, access.workspace_id, sprint_id)
    if not sprint:
        return error_json(404, "Sprint not found")

    session = svc.get_active_session(db, access.workspace_id, sprint_id)
    if not session or session.status != "voting":
        return error_json(409, "No active voting round for this sprint")

    svc.submit_vote(
        db,
        session=session,
        user_id=access.user_id,
        point_vote=body.point_vote,
        special_vote=body.special_vote,
    )
    state = svc.build_session_state(db, session, access.user_id)
    return SuccessResponse(
        status_code=200,
        message="Vote recorded",
        data=state,
    )


def reveal_votes(
    access: WorkspaceAccess,
    sprint_id: str,
    db: Session,
):
    sprint = _get_sprint(db, access.workspace_id, sprint_id)
    if not sprint:
        return error_json(404, "Sprint not found")

    session = svc.get_active_session(db, access.workspace_id, sprint_id)
    if not session or session.status != "voting":
        return error_json(409, "Session is not in voting status")

    session = svc.reveal_votes(db, session)
    state = svc.build_session_state(db, session, access.user_id)
    return SuccessResponse(
        status_code=200,
        message="Votes revealed",
        data=state,
    )


def accept_points(
    access: WorkspaceAccess,
    sprint_id: str,
    body: PokerAcceptRequest,
    db: Session,
):
    sprint = _get_sprint(db, access.workspace_id, sprint_id)
    if not sprint:
        return error_json(404, "Sprint not found")

    session = svc.get_active_session(db, access.workspace_id, sprint_id)
    if not session or session.status != "revealed":
        return error_json(409, "Session votes must be revealed before accepting points")

    session = svc.accept_points(db, session, access.user_id, body.story_points)
    state = svc.build_session_state(db, session, access.user_id)
    return SuccessResponse(
        status_code=200,
        message="Consensus story points assigned",
        data=state,
    )


def get_state(
    access: WorkspaceAccess,
    sprint_id: str,
    db: Session,
):
    sprint = _get_sprint(db, access.workspace_id, sprint_id)
    if not sprint:
        return error_json(404, "Sprint not found")

    session = svc.get_active_session(db, access.workspace_id, sprint_id)
    if not session:
        return error_json(404, "No poker session found for this sprint")

    state = svc.build_session_state(db, session, access.user_id)
    return SuccessResponse(
        status_code=200,
        message="Session state retrieved",
        data=state,
    )
