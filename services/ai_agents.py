"""Phase 3 Autonomous AI Agents & Workflows Service.

Implements:
1. Autonomous Bug Triage & Deduplication (semantic similarity, duplicate linking, triage comments, assignee & priority recommendation).
2. Autonomous Async Daily Standup & Trajectory Forecast (team activity aggregation, burn rate, carryover risk).
3. Automated Sprint Retrospectives & Backlog Hygiene (wins, scope creep, cycle time outliers, stale issue detection).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import re
from typing import Any

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from models.ai_agents import DailyStandup, SprintRetrospective
from models.hierarchy import Space, WorkflowStatus
from models.issue import Issue, IssueComment, IssueEvent
from models.sprint import Sprint, SprintIssue
from models.time_tracking import TimeEntry
from models.user import User
from models.workspace import WorkspaceMember
from schemas.ai_agents import (
    BacklogHygieneItem,
    BacklogHygieneResponse,
    DailyStandupResponse,
    SprintRetroResponse,
    StandupMemberUpdate,
    StandupVelocityForecast,
    TriageResult,
)
from services import dependencies as dep_svc
from services import issue as issue_svc
from utils.issue_keys import format_issue_key


def _to_naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _tokenize(text: str) -> set[str]:
    """Tokenize text into lowercase alphanumeric words."""
    if not text:
        return set()
    return set(re.findall(r"\b[a-zA-Z0-9_-]{2,}\b", text.lower()))


def _compute_similarity(text1: str, text2: str) -> float:
    """Calculate overlap similarity between two texts based on word tokens."""
    tokens1 = _tokenize(text1)
    tokens2 = _tokenize(text2)
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1.intersection(tokens2)
    min_len = min(len(tokens1), len(tokens2))
    jaccard = len(intersection) / len(tokens1.union(tokens2))
    overlap = len(intersection) / max(1, min_len)
    return 0.7 * overlap + 0.3 * jaccard


def triage_issue(
    db: Session,
    workspace_id: str,
    issue_id: str,
    actor_id: str,
) -> TriageResult:
    """Evaluate an issue for duplicate detection, triage recommendations, and structured reproduction steps."""
    issue = (
        db.query(Issue)
        .filter(Issue.workspace_id == workspace_id, Issue.id == issue_id, Issue.deleted == 0)
        .first()
    )
    if not issue:
        raise ValueError("Issue not found")

    space = (
        db.query(Space)
        .filter(Space.workspace_id == workspace_id, Space.id == issue.space_id)
        .first()
    )
    if not space:
        raise ValueError("Space not found")

    # 1. Deduplication scan against open issues in the same space
    candidates = (
        db.query(Issue)
        .filter(
            Issue.workspace_id == workspace_id,
            Issue.space_id == issue.space_id,
            Issue.id != issue.id,
            Issue.deleted == 0,
        )
        .order_by(Issue.created_at.desc())
        .limit(100)
        .all()
    )

    issue_title = issue.title
    issue_full = f"{issue.title} {issue.description or ''}"
    best_match: Issue | None = None
    best_score: float = 0.0

    for cand in candidates:
        cand_title = cand.title
        cand_full = f"{cand.title} {cand.description or ''}"
        title_score = _compute_similarity(issue_title, cand_title)
        full_score = _compute_similarity(issue_full, cand_full)
        score = max(title_score, 0.6 * title_score + 0.4 * full_score)
        if score > best_score:
            best_score = score
            best_match = cand

    # Threshold for duplicate detection (0.70 or above)
    is_duplicate = best_match is not None and best_score >= 0.70
    duplicate_issue_id = None
    duplicate_issue_key = None
    duplicate_issue_title = None

    if is_duplicate and best_match:
        duplicate_issue_id = str(best_match.id)
        duplicate_issue_key = format_issue_key(space.key, best_match.number)
        duplicate_issue_title = best_match.title

        # Auto-link as related if enabled and not already linked
        if space.ai_triage_enabled:
            existing = dep_svc.existing_link(db, workspace_id, issue.id, best_match.id, "relates")
            if not existing:
                try:
                    dep_svc.create_link(
                        db,
                        source=issue,
                        target=best_match,
                        kind="relates",
                        actor_id=actor_id,
                        actor_kind="automation",
                    )
                except Exception:
                    pass

            # Post automated triage comment
            comment_body = (
                f"⚠️ **Potential Duplicate Detected**: This issue appears {int(best_score * 100)}% "
                f"similar to open defect [{duplicate_issue_key}: {duplicate_issue_title}]. "
                f"Automatically linked as related issue."
            )
            comment = IssueComment(
                workspace_id=workspace_id,
                issue_id=issue.id,
                author_id=actor_id,
                body=comment_body,
            )
            db.add(comment)
            issue_svc.record_event(
                db,
                issue,
                "commented",
                actor_id,
                actor_kind="automation",
                to_value=comment.id,
            )

    # 2. Intelligent Assignee Recommendation (lowest active WIP among workspace members)
    members = (
        db.query(User)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .filter(WorkspaceMember.workspace_id == workspace_id)
        .all()
    )
    suggested_assignee: User | None = None
    if members:
        counts = (
            db.query(Issue.assignee_id, func.count(Issue.id))
            .filter(
                Issue.workspace_id == workspace_id,
                Issue.deleted == 0,
                Issue.assignee_id.in_([m.id for m in members]),
            )
            .group_by(Issue.assignee_id)
            .all()
        )
        counts_dict = {m.id: 0 for m in members}
        for uid, count in counts:
            if uid in counts_dict:
                counts_dict[uid] = count
        lowest_id = min(counts_dict, key=counts_dict.get)  # type: ignore
        for m in members:
            if m.id == lowest_id:
                suggested_assignee = m
                break

    # 3. Priority Recommendation
    desc_lower = (issue.title + " " + (issue.description or "")).lower()
    if any(k in desc_lower for k in ["crash", "critical", "outage", "security", "fatal", "broken"]):
        suggested_priority = "urgent"
    elif any(k in desc_lower for k in ["error", "fail", "bug", "blocker", "exception"]):
        suggested_priority = "high"
    elif any(k in desc_lower for k in ["typo", "minor", "tweak", "cosmetic"]):
        suggested_priority = "low"
    else:
        suggested_priority = "medium"

    # 4. Structured reproduction steps extraction
    steps = (
        f"**Steps to Reproduce:**\n"
        f"1. Navigate to relevant component.\n"
        f"2. Trigger actions matching context: {issue.title}.\n\n"
        f"**Expected Behavior:**\n"
        f"Action completes successfully without unexpected exceptions.\n\n"
        f"**Observed Behavior:**\n"
        f"{issue.description or issue.title}\n\n"
        f"**Recommended Severity:**\n"
        f"{suggested_priority.upper()}"
    )

    db.commit()

    return TriageResult(
        issue_id=str(issue.id),
        potential_duplicate=is_duplicate,
        duplicate_issue_id=duplicate_issue_id,
        duplicate_issue_key=duplicate_issue_key,
        duplicate_issue_title=duplicate_issue_title,
        similarity_score=round(best_score, 2) if is_duplicate else None,
        suggested_assignee_id=str(suggested_assignee.id) if suggested_assignee else None,
        suggested_assignee_name=(
            f"{suggested_assignee.first_name} {suggested_assignee.last_name}".strip()
            if suggested_assignee
            else None
        ),
        suggested_priority=suggested_priority,
        structured_reproduction_steps=steps,
        triage_notes=(
            f"Auto-triage completed for {space.key}-{issue.number}. "
            + (f"Found duplicate {duplicate_issue_key} ({int(best_score * 100)}% match). " if is_duplicate else "No duplicate found. ")
            + f"Suggested priority: {suggested_priority}."
        ),
    )


def generate_daily_standup(
    db: Session,
    workspace_id: str,
    for_date: date | None = None,
) -> DailyStandup:
    """Aggregate team activity across the past 24 hours and forecast active sprint velocity trajectory."""
    today = for_date or datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    window_start = datetime.combine(yesterday, datetime.min.time())

    # 1. Aggregate Time Entries logged on for_date or yesterday
    time_entries = (
        db.query(TimeEntry)
        .filter(
            TimeEntry.workspace_id == workspace_id,
            TimeEntry.work_date >= yesterday,
            TimeEntry.work_date <= today,
            TimeEntry.deleted == 0,
        )
        .all()
    )

    hours_by_user: dict[str, float] = {}
    for entry in time_entries:
        uid = str(entry.user_id)
        hours_by_user[uid] = hours_by_user.get(uid, 0.0) + (entry.duration_seconds / 3600.0)

    # 2. Query workspace members and their issues
    members = (
        db.query(User)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .filter(WorkspaceMember.workspace_id == workspace_id)
        .all()
    )

    done_statuses = (
        db.query(WorkflowStatus.id)
        .filter(
            WorkflowStatus.workspace_id == workspace_id,
            WorkflowStatus.category == "done",
            WorkflowStatus.deleted == 0,
        )
        .all()
    )
    done_status_ids = {s[0] for s in done_statuses}

    team_updates: list[dict[str, Any]] = []
    total_completed = 0
    total_in_flight = 0

    for m in members:
        uid = str(m.id)
        name = f"{m.first_name} {m.last_name}".strip() or m.username

        # Completed yesterday / today
        completed_issues = (
            db.query(Issue, Space.key)
            .join(Space, Space.id == Issue.space_id)
            .filter(
                Issue.workspace_id == workspace_id,
                Issue.assignee_id == uid,
                Issue.status_id.in_(done_status_ids),
                Issue.updated_at >= window_start,
                Issue.deleted == 0,
            )
            .all()
        )
        completed_list = [f"{space_key}-{iss.number}: {iss.title}" for iss, space_key in completed_issues]
        total_completed += len(completed_list)

        # In flight today
        in_flight_issues = (
            db.query(Issue, Space.key)
            .join(Space, Space.id == Issue.space_id)
            .filter(
                Issue.workspace_id == workspace_id,
                Issue.assignee_id == uid,
                ~Issue.status_id.in_(done_status_ids),
                Issue.deleted == 0,
            )
            .limit(5)
            .all()
        )
        in_flight_list = [f"{space_key}-{iss.number}: {iss.title}" for iss, space_key in in_flight_issues]
        total_in_flight += len(in_flight_list)

        # Blockers
        blockers: list[str] = []
        for iss, space_key in in_flight_issues:
            if "block" in (iss.title + " " + (iss.description or "")).lower():
                blockers.append(f"Blocked on {space_key}-{iss.number}: {iss.title}")

        if completed_list or in_flight_list or (hours_by_user.get(uid, 0.0) > 0):
            team_updates.append(
                {
                    "user_id": uid,
                    "user_name": name,
                    "completed_yesterday": completed_list,
                    "in_flight_today": in_flight_list,
                    "blockers": blockers,
                    "hours_logged": round(hours_by_user.get(uid, 0.0), 2),
                }
            )

    # 3. Velocity Forecast for Active Sprint
    active_sprint = (
        db.query(Sprint)
        .filter(
            Sprint.workspace_id == workspace_id,
            Sprint.state == "active",
            Sprint.deleted == 0,
        )
        .first()
    )

    velocity_forecast: dict[str, Any] | None = None
    health_status = "healthy"
    if active_sprint:
        committed = active_sprint.committed_points or 0
        completed = active_sprint.completed_points or 0
        remaining = max(0, committed - completed)

        days_left = 5
        if active_sprint.ends_on:
            days_left = max(1, (active_sprint.ends_on - today).days)

        days_elapsed = 1
        if active_sprint.starts_on:
            days_elapsed = max(1, (today - active_sprint.starts_on).days)

        required_rate = round(remaining / days_left, 1)
        burn_rate = round(completed / days_elapsed, 1)
        carryover_risk = required_rate > (burn_rate * 1.2) if burn_rate > 0 else (remaining > 0)
        projected_carryover = round(
            max(0.0, (remaining - (burn_rate * days_left)) / max(1, committed) * 100), 1
        )

        if projected_carryover > 25.0:
            health_status = "critical"
        elif carryover_risk or projected_carryover > 10.0:
            health_status = "at_risk"
        else:
            health_status = "healthy"

        velocity_forecast = {
            "active_sprint_name": active_sprint.name,
            "remaining_points": remaining,
            "working_days_left": days_left,
            "required_rate": required_rate,
            "current_burn_rate": burn_rate,
            "carryover_risk": carryover_risk,
            "projected_carryover_pct": projected_carryover,
            "health_status": health_status,
        }

    summary = (
        f"Daily Standup Briefing for {today.isoformat()}: "
        f"{len(team_updates)} team members active. {total_completed} issues delivered, "
        f"{total_in_flight} issues in flight. Sprint trajectory: {health_status.upper()}."
    )

    standup = (
        db.query(DailyStandup)
        .filter(
            DailyStandup.workspace_id == workspace_id,
            DailyStandup.standup_date == today,
        )
        .first()
    )
    if standup:
        standup.summary = summary
        standup.team_updates = team_updates
        standup.velocity_forecast = velocity_forecast
    else:
        standup = DailyStandup(
            workspace_id=workspace_id,
            standup_date=today,
            summary=summary,
            team_updates=team_updates,
            velocity_forecast=velocity_forecast,
        )
        db.add(standup)

    db.commit()
    db.refresh(standup)
    return standup


def get_daily_standup(
    db: Session,
    workspace_id: str,
    for_date: date | None = None,
) -> DailyStandup:
    """Retrieve the standup for the date, generating it if none exists."""
    target_date = for_date or datetime.now(UTC).date()
    standup = (
        db.query(DailyStandup)
        .filter(
            DailyStandup.workspace_id == workspace_id,
            DailyStandup.standup_date == target_date,
        )
        .first()
    )
    if not standup:
        return generate_daily_standup(db, workspace_id, target_date)
    return standup


def generate_sprint_retro(
    db: Session,
    workspace_id: str,
    sprint_id: str,
) -> SprintRetrospective:
    """Analyze sprint baseline, scope creep, cycle times, and produce retrospective intelligence."""
    sprint = (
        db.query(Sprint)
        .filter(
            Sprint.workspace_id == workspace_id,
            Sprint.id == sprint_id,
            Sprint.deleted == 0,
        )
        .first()
    )
    if not sprint:
        raise ValueError("Sprint not found")

    space = db.query(Space).filter(Space.workspace_id == workspace_id, Space.id == sprint.space_id).first()
    space_key = space.key if space else "ISSUE"

    sprint_issues = (
        db.query(SprintIssue, Issue)
        .join(Issue, Issue.id == SprintIssue.issue_id)
        .filter(
            SprintIssue.workspace_id == workspace_id,
            SprintIssue.sprint_id == sprint_id,
        )
        .all()
    )

    committed = sprint.committed_points or 0
    completed = sprint.completed_points or 0
    completion_rate = round((completed / max(1, committed)) * 100, 1) if committed > 0 else 100.0

    # 1. Identify Wins
    wins: list[str] = []
    if completion_rate >= 80:
        wins.append(f"High velocity delivery: Achieved {completion_rate}% point completion.")
    if committed > 0 and completed >= committed:
        wins.append(f"Zero carryover: All {committed} committed story points delivered.")

    done_items = [
        f"{space_key}-{iss.number}: {iss.title}"
        for si, iss in sprint_issues
        if iss.story_points and iss.story_points >= 5
    ]
    if done_items:
        wins.append(f"Completed high-impact items: {', '.join(done_items[:3])}")
    if not wins:
        wins.append("Sprint completed with active team engagement and progress across core deliverables.")

    # 2. Scope Creep: Issues added after sprint start
    scope_creep: list[str] = []
    started_at = _to_naive_utc(sprint.started_at)
    if started_at:
        for si, iss in sprint_issues:
            added_at = _to_naive_utc(si.added_at)
            if added_at and added_at > started_at:
                scope_creep.append(
                    f"{space_key}-{iss.number}: {iss.title} (Added mid-sprint with {si.points_at_add or 0} pts)"
                )
    if not scope_creep:
        scope_creep.append("No unplanned mid-sprint scope additions detected.")

    # 3. Cycle Time Outliers (duration from creation to resolution)
    cycle_time_outliers: list[str] = []
    durations: list[tuple[Issue, float]] = []
    for si, iss in sprint_issues:
        c_at = _to_naive_utc(iss.created_at)
        u_at = _to_naive_utc(iss.updated_at)
        if c_at and u_at:
            dur_days = (u_at - c_at).total_seconds() / 86400.0
            durations.append((iss, dur_days))

    if durations:
        avg_days = sum(d for _, d in durations) / len(durations)
        for iss, d in durations:
            if d > max(5.0, avg_days * 2.0):
                cycle_time_outliers.append(
                    f"{space_key}-{iss.number}: {iss.title} took {round(d, 1)} days (space avg: {round(avg_days, 1)}d)"
                )
    if not cycle_time_outliers:
        cycle_time_outliers.append("All issues maintained steady cycle times within expected thresholds.")

    metrics = {
        "committed_points": committed,
        "completed_points": completed,
        "completion_rate_pct": completion_rate,
        "total_issues_count": len(sprint_issues),
        "scope_creep_count": len([s for s in scope_creep if "Added mid-sprint" in s]),
    }

    summary = (
        f"Sprint Retrospective for {sprint.name}: Delivered {completed} of {committed} points "
        f"({completion_rate}%). {len(wins)} key wins highlighted with {metrics['scope_creep_count']} "
        f"unplanned scope additions."
    )

    retro = (
        db.query(SprintRetrospective)
        .filter(
            SprintRetrospective.workspace_id == workspace_id,
            SprintRetrospective.sprint_id == sprint_id,
        )
        .first()
    )
    if retro:
        retro.summary = summary
        retro.wins = wins
        retro.scope_creep = scope_creep
        retro.cycle_time_outliers = cycle_time_outliers
        retro.metrics = metrics
    else:
        retro = SprintRetrospective(
            workspace_id=workspace_id,
            sprint_id=sprint_id,
            summary=summary,
            wins=wins,
            scope_creep=scope_creep,
            cycle_time_outliers=cycle_time_outliers,
            metrics=metrics,
        )
        db.add(retro)

    db.commit()
    db.refresh(retro)
    return retro


def get_sprint_retro(
    db: Session,
    workspace_id: str,
    sprint_id: str,
) -> SprintRetrospective:
    """Retrieve retrospective for a sprint, generating it if none exists."""
    retro = (
        db.query(SprintRetrospective)
        .filter(
            SprintRetrospective.workspace_id == workspace_id,
            SprintRetrospective.sprint_id == sprint_id,
        )
        .first()
    )
    if not retro:
        return generate_sprint_retro(db, workspace_id, sprint_id)
    return retro


def scan_backlog_hygiene(
    db: Session,
    workspace_id: str,
    space_id: str,
    threshold_days: int = 90,
) -> BacklogHygieneResponse:
    """Scan backlog for stale issues inactive for >= threshold_days and provide hygiene recommendations."""
    now = _now()
    cutoff = now - timedelta(days=threshold_days)

    space = db.query(Space).filter(Space.workspace_id == workspace_id, Space.id == space_id).first()
    if not space:
        raise ValueError("Space not found")

    done_statuses = (
        db.query(WorkflowStatus.id)
        .filter(
            WorkflowStatus.workspace_id == workspace_id,
            WorkflowStatus.category.in_(["done", "canceled"]),
            WorkflowStatus.deleted == 0,
        )
        .all()
    )
    done_status_ids = {s[0] for s in done_statuses}

    stale_issues = (
        db.query(Issue)
        .filter(
            Issue.workspace_id == workspace_id,
            Issue.space_id == space_id,
            Issue.deleted == 0,
            ~Issue.status_id.in_(done_status_ids),
            Issue.updated_at <= cutoff,
        )
        .order_by(Issue.updated_at.asc())
        .limit(100)
        .all()
    )

    items: list[BacklogHygieneItem] = []

    for iss in stale_issues:
        upd = _to_naive_utc(iss.updated_at)
        days_inactive = (now - upd).days if upd else threshold_days
        priority = iss.priority or "medium"
        recommendation = (
            "Recommend archiving or moving to Icebox."
            if days_inactive >= 90
            else "Needs team review for relevancy and priority re-evaluation."
        )

        items.append(
            BacklogHygieneItem(
                issue_id=str(iss.id),
                key=format_issue_key(space.key, iss.number),
                title=iss.title,
                days_inactive=days_inactive,
                story_points=iss.story_points,
                priority=priority,
                recommendation=recommendation,
            )
        )

    return BacklogHygieneResponse(
        space_id=space_id,
        stale_count=len(items),
        threshold_days=threshold_days,
        items=items,
    )
