"""Sprint Planning Poker models: sessions and votes."""
import uuid
from datetime import UTC, datetime

from sqlalchemy import CHAR, Column, DateTime, ForeignKey, Index, Integer, String

from db.session import Base


def _now() -> datetime:
    return datetime.now(UTC)


class PokerSession(Base):
    """A sprint planning poker session.

    Status flows: voting -> revealed -> completed.
    """

    __tablename__ = "poker_sessions"
    __table_args__ = (
        Index("ix_poker_sessions_sprint", "workspace_id", "sprint_id"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    sprint_id = Column(CHAR(36), ForeignKey("sprints.id"), nullable=False)
    active_issue_id = Column(CHAR(36), ForeignKey("issues.id"), nullable=True)
    status = Column(String(20), nullable=False, default="voting")  # voting, revealed, completed
    created_by = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class PokerVote(Base):
    """An individual user's vote on an issue in a poker session."""

    __tablename__ = "poker_votes"

    session_id = Column(CHAR(36), ForeignKey("poker_sessions.id"), primary_key=True)
    user_id = Column(CHAR(36), ForeignKey("users.id"), primary_key=True)
    issue_id = Column(CHAR(36), ForeignKey("issues.id"), primary_key=True)
    point_vote = Column(Integer, nullable=True)  # Fibonacci number or None
    special_vote = Column(String(10), nullable=True)  # '?', 'coffee' or None
    voted_at = Column(DateTime, default=_now, nullable=False)
