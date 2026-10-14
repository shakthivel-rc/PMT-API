"""Autonomous AI Agent models: Daily Standups and Sprint Retrospectives."""
import uuid
from datetime import UTC, datetime

from sqlalchemy import CHAR, JSON, Column, Date, DateTime, ForeignKey, Index, Text

from db.session import Base


def _now() -> datetime:
    return datetime.now(UTC)


class DailyStandup(Base):
    """Automated daily standup briefing synthesized from recent events and active timers."""

    __tablename__ = "daily_standups"
    __table_args__ = (
        Index("ix_daily_standups_ws_date", "workspace_id", "standup_date"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    standup_date = Column(Date, nullable=False)
    summary = Column(Text, nullable=False)
    team_updates = Column(JSON, nullable=True)
    velocity_forecast = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)


class SprintRetrospective(Base):
    """Automated retrospective generated upon sprint closure."""

    __tablename__ = "sprint_retrospectives"
    __table_args__ = (
        Index("ix_sprint_retrospectives_ws_sprint", "workspace_id", "sprint_id"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    sprint_id = Column(CHAR(36), ForeignKey("sprints.id"), nullable=False)
    summary = Column(Text, nullable=False)
    wins = Column(JSON, nullable=True)
    scope_creep = Column(JSON, nullable=True)
    cycle_time_outliers = Column(JSON, nullable=True)
    metrics = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
