"""Sprint Planning Poker services."""
from __future__ import annotations

import statistics
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from models.hierarchy import Space
from models.issue import Issue, IssueEvent
from models.poker import PokerSession, PokerVote
from models.sprint import Sprint
from models.user import User
from models.workspace import WorkspaceMember
from schemas.poker import PokerSessionState, PokerVoterState


def _now() -> datetime:
    return datetime.now(UTC)


def get_active_session(db: Session, workspace_id: str, sprint_id: str) -> PokerSession | None:
    """Get the currently active (voting or revealed) session for this sprint, or the most recent."""
    active = (
        db.query(PokerSession)
        .filter(
            PokerSession.workspace_id == workspace_id,
            PokerSession.sprint_id == sprint_id,
            PokerSession.status.in_(["voting", "revealed"]),
        )
        .order_by(PokerSession.created_at.desc())
        .first()
    )
    if active:
        return active
    return (
        db.query(PokerSession)
        .filter(
            PokerSession.workspace_id == workspace_id,
            PokerSession.sprint_id == sprint_id,
        )
        .order_by(PokerSession.created_at.desc())
        .first()
    )


def start_session(
    db: Session,
    workspace_id: str,
    sprint_id: str,
    issue_id: str,
    user_id: str,
) -> PokerSession:
    """Start or restart a poker round on an issue."""
    # Complete any in-flight active session for this sprint
    active = (
        db.query(PokerSession)
        .filter(
            PokerSession.workspace_id == workspace_id,
            PokerSession.sprint_id == sprint_id,
            PokerSession.status.in_(["voting", "revealed"]),
        )
        .all()
    )
    for s in active:
        s.status = "completed"
        s.updated_at = _now()

    new_session = PokerSession(
        workspace_id=workspace_id,
        sprint_id=sprint_id,
        active_issue_id=issue_id,
        status="voting",
        created_by=user_id,
    )
    db.add(new_session)
    db.commit()
    db.refresh(new_session)
    return new_session


def submit_vote(
    db: Session,
    session: PokerSession,
    user_id: str,
    point_vote: int | None,
    special_vote: str | None,
) -> PokerVote:
    """Record or update a user's vote for the active issue in this session."""
    vote = (
        db.query(PokerVote)
        .filter_by(
            session_id=session.id,
            user_id=user_id,
            issue_id=session.active_issue_id,
        )
        .first()
    )
    if not vote:
        vote = PokerVote(
            session_id=session.id,
            user_id=user_id,
            issue_id=session.active_issue_id,
            point_vote=point_vote,
            special_vote=special_vote,
        )
        db.add(vote)
    else:
        vote.point_vote = point_vote
        vote.special_vote = special_vote
        vote.voted_at = _now()
    session.updated_at = _now()
    db.commit()
    db.refresh(vote)
    return vote


def reveal_votes(db: Session, session: PokerSession) -> PokerSession:
    """Reveal votes to all participants."""
    session.status = "revealed"
    session.updated_at = _now()
    db.commit()
    db.refresh(session)
    return session


def accept_points(
    db: Session,
    session: PokerSession,
    user_id: str,
    story_points: int,
) -> PokerSession:
    """Accept consensus story points, update issue, record event, and complete session."""
    if session.active_issue_id:
        issue = (
            db.query(Issue)
            .filter_by(id=session.active_issue_id, workspace_id=session.workspace_id)
            .first()
        )
        if issue:
            old_points = issue.story_points
            issue.story_points = story_points
            db.add(
                IssueEvent(
                    workspace_id=session.workspace_id,
                    issue_id=issue.id,
                    actor_id=user_id,
                    actor_kind="user",
                    event="points_changed",
                    from_value=str(old_points) if old_points is not None else None,
                    to_value=str(story_points),
                )
            )

    session.status = "completed"
    session.updated_at = _now()
    db.commit()
    db.refresh(session)
    return session


def build_session_state(
    db: Session,
    session: PokerSession,
    caller_user_id: str,
) -> PokerSessionState:
    """Build the session state response, enforcing privacy masking during voting."""
    # 1. Fetch active issue info
    issue_title = None
    issue_key = None
    if session.active_issue_id:
        issue = (
            db.query(Issue)
            .filter_by(id=session.active_issue_id, workspace_id=session.workspace_id)
            .first()
        )
        if issue:
            issue_title = issue.title
            space = db.query(Space).filter_by(id=issue.space_id).first()
            if space:
                issue_key = f"{space.key}-{issue.number}"

    # 2. Fetch all votes for this session
    votes = db.query(PokerVote).filter_by(session_id=session.id).all()
    vote_map: dict[str, PokerVote] = {v.user_id: v for v in votes}

    # 3. Fetch workspace members joined with User
    members = (
        db.query(WorkspaceMember, User)
        .join(User, WorkspaceMember.user_id == User.id)
        .filter(WorkspaceMember.workspace_id == session.workspace_id)
        .all()
    )

    voters: list[PokerVoterState] = []
    seen_users: set[str] = set()

    for wm, u in members:
        seen_users.add(wm.user_id)
        vote = vote_map.get(wm.user_id)
        has_voted = vote is not None
        pt_val = None
        sp_val = None

        if session.status in ("revealed", "completed"):
            if vote:
                pt_val = vote.point_vote
                sp_val = vote.special_vote
        elif session.status == "voting":
            # Voter sees their own selection; others are masked to prevent anchoring bias
            if vote and wm.user_id == caller_user_id:
                pt_val = vote.point_vote
                sp_val = vote.special_vote

        name = f"{u.first_name} {u.last_name}".strip() or u.username
        voters.append(
            PokerVoterState(
                user_id=wm.user_id,
                name=name,
                email=u.email,
                avatar_url=None,
                has_voted=has_voted,
                point_vote=pt_val,
                special_vote=sp_val,
            )
        )

    # Any voter who is not a registered workspace member
    for uid, vote in vote_map.items():
        if uid not in seen_users:
            pt_val = None
            sp_val = None
            if session.status in ("revealed", "completed"):
                pt_val = vote.point_vote
                sp_val = vote.special_vote
            elif session.status == "voting" and uid == caller_user_id:
                pt_val = vote.point_vote
                sp_val = vote.special_vote

            voters.append(
                PokerVoterState(
                    user_id=uid,
                    name=uid,
                    email=None,
                    avatar_url=None,
                    has_voted=True,
                    point_vote=pt_val,
                    special_vote=sp_val,
                )
            )

    # 4. Compute statistics when revealed or completed
    average: float | None = None
    median: float | None = None
    lowest_vote: int | None = None
    highest_vote: int | None = None

    if session.status in ("revealed", "completed"):
        num_votes = [v.point_vote for v in votes if v.point_vote is not None]
        if num_votes:
            average = round(sum(num_votes) / len(num_votes), 1)
            median = float(statistics.median(num_votes))
            lowest_vote = min(num_votes)
            highest_vote = max(num_votes)

    return PokerSessionState(
        id=session.id,
        workspace_id=session.workspace_id,
        sprint_id=session.sprint_id,
        active_issue_id=session.active_issue_id,
        active_issue_title=issue_title,
        active_issue_key=issue_key,
        status=session.status,
        created_by=session.created_by,
        voters=voters,
        average=average,
        median=median,
        lowest_vote=lowest_vote,
        highest_vote=highest_vote,
        total_voters=len(voters),
        voted_count=len(vote_map),
    )
