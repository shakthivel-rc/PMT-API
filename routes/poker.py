"""Sprint Planning Poker routes."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from controllers import poker_controller as ctl
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_guest, as_member
from routes.workspaces import NOT_A_MEMBER
from schemas.errors import COMMON_ERRORS, VALIDATION_ERROR
from schemas.poker import (
    PokerAcceptRequest,
    PokerSessionState,
    PokerStartRequest,
    PokerVoteRequest,
)
from schemas.response import ErrorResponse, SuccessResponse

router = APIRouter(
    prefix="/workspaces/{workspace_id}/sprints/{sprint_id}/poker",
    tags=["Sprints"],
)

POKER_ERRORS = {**COMMON_ERRORS, **NOT_A_MEMBER, **VALIDATION_ERROR}


@router.post(
    "/start",
    response_model=SuccessResponse[PokerSessionState],
    status_code=201,
    responses=POKER_ERRORS,
    summary="Start or restart a sprint planning poker round for an issue",
    operation_id="start_poker_session",
)
def start_poker_session(
    sprint_id: str,
    body: PokerStartRequest,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    return ctl.start_session(access, sprint_id, body, db)


@router.post(
    "/vote",
    response_model=SuccessResponse[PokerSessionState],
    responses=POKER_ERRORS,
    summary="Submit or change a planning poker vote for the active issue",
    operation_id="submit_poker_vote",
)
def submit_poker_vote(
    sprint_id: str,
    body: PokerVoteRequest,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    return ctl.submit_vote(access, sprint_id, body, db)


@router.post(
    "/reveal",
    response_model=SuccessResponse[PokerSessionState],
    responses=POKER_ERRORS,
    summary="Reveal all participant votes in the active planning poker round",
    operation_id="reveal_poker_votes",
)
def reveal_poker_votes(
    sprint_id: str,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    return ctl.reveal_votes(access, sprint_id, db)


@router.post(
    "/accept",
    response_model=SuccessResponse[PokerSessionState],
    responses=POKER_ERRORS,
    summary="Accept consensus story points and update the estimated issue",
    operation_id="accept_poker_consensus",
)
def accept_poker_consensus(
    sprint_id: str,
    body: PokerAcceptRequest,
    access: WorkspaceAccess = Depends(as_member),
    db: Session = Depends(get_db),
):
    return ctl.accept_points(access, sprint_id, body, db)


@router.get(
    "/state",
    response_model=SuccessResponse[PokerSessionState],
    responses=POKER_ERRORS,
    summary="Get current state of the sprint planning poker session",
    operation_id="get_poker_state",
)
def get_poker_state(
    sprint_id: str,
    access: WorkspaceAccess = Depends(as_guest),
    db: Session = Depends(get_db),
):
    return ctl.get_state(access, sprint_id, db)
