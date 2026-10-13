"""Releases of a space, their progress and notes, and which issues ship in which.

docs/design/09-restructuring.md §10. A release is `unreleased` until it is released, and may
then be `archived` (models/release.py). An issue's fix versions are rows of
`issue_fix_versions`; an issue may be in several, because a fix is back-ported.

The rules of services/issue.py hold here, plus three of this module's own:

* **Outcomes come from stage categories, never from stage names**: `done` is delivered,
  `canceled` is closed without being delivered, and everything else is still open
  (`utils/workflow_rules.category_class`, which also reads an unknown category as open — an
  issue the rules cannot place is not finished). Progress, the open issues releasing can
  move on, and the notes all use that one mapping.
* **Every change to an issue's fix versions writes its event in the same flush** —
  `fix_version_added` with the release id in `to_value`, `fix_version_removed` with it in
  `from_value` — the way sprint membership records `sprint_added` / `sprint_removed`
  (services/sprint.py), so a history can be replayed from the events alone.
* **Fix versions do not bump `issues.version`.** Like sprint membership, choosing which
  release an issue ships in is planning, not an edit of the issue; someone with the issue
  open must not get a 409 for it.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

from sqlalchemy import and_, func
from sqlalchemy.orm import Query, Session

from models.hierarchy import Space, WorkflowStatus
from models.issue import Issue
from models.release import IssueFixVersion, Release
from services import issue as issue_svc
from utils import workflow_rules as rules
from utils.issue_keys import format_issue_key

#: The most issue cards a release's detail carries. The space backlog's cap (§5.2), for the
#: same reason: past a few hundred cards a page stops being read and starts being scrolled,
#: and the response grows with the release. `progress` always counts every issue.
MAX_DETAIL_ISSUES = 500

#: Unreleased first — what is being worked towards — then what shipped, then the archive.
_STATE_ORDER = {"unreleased": 0, "released": 1, "archived": 2}

#: The order release notes list delivered work in: the outcomes, the user-facing changes,
#: the rest of the work, the fixes, then the parts.
NOTE_TYPES: tuple[str, ...] = ("epic", "story", "task", "bug", "subtask")
NOTE_TITLES: dict[str, str] = {
    "epic": "Epics", "story": "Stories", "task": "Tasks", "bug": "Bugs",
    "subtask": "Subtasks", "canceled": "Canceled", "open": "Still open",
}


def _naive(value: datetime) -> datetime:
    # Read back from the database a DateTime is naive; one created in this session still
    # holds the aware default it was given (models/release._now). Sorting both would raise.
    return value.replace(tzinfo=None)


def outcome(category: str | None) -> str:
    """`done`, `canceled` or `open`."""
    return rules.category_class(category)


def get_release(db: Session, workspace_id: str, release_id: str, *,
                for_update: bool = False) -> Release | None:
    query = db.query(Release).filter(Release.workspace_id == workspace_id,
                                     Release.id == release_id, Release.deleted == 0)
    if for_update:
        # Releasing reads the state and then writes it: two admins pressing Release at once
        # must not both move the open issues. SQLite ignores FOR UPDATE; it has one writer.
        query = query.with_for_update()
    return query.first()


def lock_space(db: Session, workspace_id: str, space_id: str) -> None:
    """Hold the space row until commit, so a name check and the write it guards are atomic.

    Names are unique among a space's live releases ignoring case, which no unique index can
    say on MySQL without a generated column — deleted releases must not count. Creating issues
    and starting sprints already serialise on this row (services/issue.allocate_number,
    services/sprint.lock_space), and releases are created rarely enough to queue behind them.
    """
    (db.query(Space.id)
     .filter(Space.workspace_id == workspace_id, Space.id == space_id)
     .with_for_update()
     .one())


def name_taken(db: Session, workspace_id: str, space_id: str, name: str,
               except_id: str | None = None) -> bool:
    query = db.query(Release.id).filter(
        Release.workspace_id == workspace_id, Release.space_id == space_id,
        Release.deleted == 0, func.lower(Release.name) == name.lower())
    if except_id is not None:
        query = query.filter(Release.id != except_id)
    return query.first() is not None


def _list_order(release: Release) -> tuple[int, bool, int, datetime]:
    """Unreleased soonest-planned first (undated last); released and archived newest first."""
    state = _STATE_ORDER.get(str(release.state), len(_STATE_ORDER))
    created = _naive(release.created_at)
    if release.state == "unreleased":
        planned = release.release_date
        return state, planned is None, planned.toordinal() if planned else 0, created
    shipped = release.release_date or (release.released_at.date() if release.released_at
                                       else None)
    return state, shipped is None, -shipped.toordinal() if shipped else 0, created


def space_releases(db: Session, workspace_id: str, space_id: str,
                   states: Iterable[str] | None = None) -> list[Release]:
    query = db.query(Release).filter(Release.workspace_id == workspace_id,
                                     Release.space_id == space_id, Release.deleted == 0)
    wanted = list(states or [])
    if wanted:
        query = query.filter(Release.state.in_(wanted))
    return sorted(query.all(), key=_list_order)


def _issue_rows(workspace_id: str, query: Query) -> Query:
    """`query`, read from `issue_fix_versions`, joined to the live issues and their stages."""
    return (query.select_from(IssueFixVersion)
            .join(Issue, and_(Issue.id == IssueFixVersion.issue_id,
                              Issue.workspace_id == workspace_id))
            .join(WorkflowStatus, and_(WorkflowStatus.id == Issue.status_id,
                                       WorkflowStatus.workspace_id == workspace_id))
            .filter(IssueFixVersion.workspace_id == workspace_id, Issue.deleted == 0))


def progress(db: Session, workspace_id: str,
             release_ids: Iterable[str]) -> dict[str, dict[str, int]]:
    """Per release, its live issues by outcome: one grouped query, whatever the number of
    releases or issues."""
    ids = list(dict.fromkeys(release_ids))
    totals = {release_id: {"issues": 0, "done": 0, "canceled": 0, "open": 0}
              for release_id in ids}
    if not ids:
        return totals
    rows = (_issue_rows(workspace_id, db.query(IssueFixVersion.release_id,
                                               WorkflowStatus.category, func.count(Issue.id)))
            .filter(IssueFixVersion.release_id.in_(ids))
            .group_by(IssueFixVersion.release_id, WorkflowStatus.category)
            .all())
    for release_id, category, count in rows:
        entry = totals[release_id]
        entry["issues"] += int(count)
        entry[outcome(category)] += int(count)
    return totals


def ref_payload(release: Release) -> dict:
    """`FixVersion`: the release as a chip on an issue."""
    return {"id": release.id, "space_id": release.space_id, "name": release.name,
            "state": release.state, "start_date": release.start_date,
            "release_date": release.release_date, "released_at": release.released_at}


def payloads(db: Session, releases: list[Release]) -> list[dict]:
    """`ReleaseSummary` payloads: one grouped query for the progress of any number of
    releases."""
    if not releases:
        return []
    counts = progress(db, str(releases[0].workspace_id), [r.id for r in releases])
    return [{**ref_payload(r), "description": r.description, "created_at": r.created_at,
             "updated_at": r.updated_at, "progress": counts[r.id]}
            for r in releases]


def detail(db: Session, release: Release) -> dict:
    """`ReleaseDetail`: the summary and its issues as cards, in key order."""
    payload = payloads(db, [release])[0]
    issues = (db.query(Issue)
              .join(IssueFixVersion, and_(IssueFixVersion.issue_id == Issue.id,
                                          IssueFixVersion.workspace_id == release.workspace_id))
              .join(Space, and_(Space.id == Issue.space_id,
                                Space.workspace_id == release.workspace_id))
              .filter(Issue.workspace_id == release.workspace_id,
                      IssueFixVersion.release_id == release.id, Issue.deleted == 0)
              .order_by(Space.key, Issue.number, Issue.id)
              .limit(MAX_DETAIL_ISSUES)
              .all())
    payload.update({"issues": issue_svc.cards(db, issues),
                    "truncated": payload["progress"]["issues"] > len(issues)})
    return payload


def open_issues(db: Session, release: Release) -> list[Issue]:
    """Its live issues whose stage is open — what releasing can move to another release.
    `notin_` the closed categories, so a category the rules do not know counts as open."""
    return (_issue_rows(str(release.workspace_id), db.query(Issue))
            .filter(IssueFixVersion.release_id == release.id,
                    WorkflowStatus.category.notin_(rules.CLOSED_CATEGORIES))
            .order_by(Issue.number, Issue.id)
            .all())


def add_fix_version(db: Session, issue: Issue, release: Release, actor_id: str, *,
                    actor_kind: str = "user") -> bool:
    """Put `issue` in `release`. False when it is already there: a repeated request writes no
    row and no event."""
    present = (db.query(IssueFixVersion.id)
               .filter(IssueFixVersion.workspace_id == issue.workspace_id,
                       IssueFixVersion.issue_id == issue.id,
                       IssueFixVersion.release_id == release.id)
               .first())
    if present is not None:
        return False
    db.add(IssueFixVersion(workspace_id=issue.workspace_id, issue_id=issue.id,
                           release_id=release.id))
    issue_svc.record_event(db, issue, "fix_version_added", actor_id, actor_kind=actor_kind,
                           to_value=str(release.id))
    db.flush()
    return True


def remove_fix_version(db: Session, issue: Issue, release_id: str, actor_id: str, *,
                       actor_kind: str = "user") -> bool:
    """Take `issue` out of the release. The row is deleted — `issue_fix_versions` keeps no
    tombstones; the event is the history. A transfer that moves an issue out of its space
    may call this for the fix versions it leaves behind (`actor_kind="migration"`)."""
    removed = (db.query(IssueFixVersion)
               .filter(IssueFixVersion.workspace_id == issue.workspace_id,
                       IssueFixVersion.issue_id == issue.id,
                       IssueFixVersion.release_id == release_id)
               .delete(synchronize_session=False))
    if not removed:
        return False
    issue_svc.record_event(db, issue, "fix_version_removed", actor_id, actor_kind=actor_kind,
                           from_value=release_id)
    db.flush()
    return True


def _fix_version_order(release: Release) -> tuple[bool, int, str, str]:
    """By release date, undated last, then by name — the order the chips read in."""
    planned = release.release_date
    return (planned is None, planned.toordinal() if planned else 0, str(release.name).lower(),
            str(release.id))


def fix_versions_of(db: Session, workspace_id: str, issue_id: str) -> list[Release]:
    """The issue's live fix versions. A deleted release's rows are kept but never shown: a
    release is deleted only before it ships, so nothing that shipped disappears with it."""
    rows = (db.query(Release)
            .join(IssueFixVersion, and_(IssueFixVersion.release_id == Release.id,
                                        IssueFixVersion.workspace_id == workspace_id))
            .filter(Release.workspace_id == workspace_id, IssueFixVersion.issue_id == issue_id,
                    Release.deleted == 0)
            .all())
    return sorted(rows, key=_fix_version_order)


def releases_by_id(db: Session, workspace_id: str, ids: Iterable[str]) -> dict[str, Release]:
    wanted = set(ids)
    if not wanted:
        return {}
    rows = (db.query(Release)
            .filter(Release.workspace_id == workspace_id, Release.id.in_(wanted),
                    Release.deleted == 0)
            .all())
    return {release.id: release for release in rows}


def replace_fix_versions(db: Session, issue: Issue, wanted: list[str], current: list[Release],
                         adding: dict[str, Release], actor_id: str) -> None:
    """Make the issue's live fix versions exactly `wanted`: removals first, then additions in
    the order asked. `adding` holds every wanted release the issue does not have yet,
    already validated by the caller."""
    keep = set(wanted)
    for release in current:
        if release.id not in keep:
            remove_fix_version(db, issue, str(release.id), actor_id)
    have = {release.id for release in current}
    for release_id in wanted:
        if release_id not in have:
            add_fix_version(db, issue, adding[release_id], actor_id)


def ship(db: Session, release: Release, *, release_date: date, now: datetime,
         move: list[Issue], to: Release | None, actor_id: str) -> list[str]:
    """Release `release` on `release_date`, first moving each of `move` (its open issues,
    already write-checked by the caller) to `to`. Returns the ids moved."""
    moved: list[str] = []
    if to is not None:
        for issue in move:
            remove_fix_version(db, issue, str(release.id), actor_id)
            add_fix_version(db, issue, to, actor_id)
            moved.append(str(issue.id))
    release.state = "released"
    release.release_date = release_date
    release.released_at = now
    db.flush()
    return moved


# ---- notes ----------------------------------------------------------------------------------


def _single_line(text: str) -> str:
    # A title is one line by convention, not by constraint; a newline in one would break the
    # one-issue-per-line shape of the text notes.
    return " ".join(str(text).splitlines())


def _heading(space_name: str, release: Release) -> str:
    if release.released_at is not None:
        when = (f"released {release.release_date.isoformat()}" if release.release_date
                else "released")
    elif release.state == "archived":
        when = "archived without being released"
    elif release.release_date is not None:
        when = f"not released yet, planned for {release.release_date.isoformat()}"
    else:
        when = "not released yet"
    return f"{space_name} {release.name} - {when}"


def render_notes(heading: str, sections: list[dict]) -> str:
    """The plain-text notes: the heading, then each section's title and `KEY title` lines,
    a blank line between. Pure, so its shape is tested without a database."""
    lines = [heading]
    if not sections:
        lines += ["", "No issues are assigned to this release."]
    for section in sections:
        lines += ["", section["title"]]
        lines += [f"{item['key']} {_single_line(item['title'])}" for item in section["issues"]]
    return "\n".join(lines) + "\n"


def notes(db: Session, release: Release) -> dict:
    """`ReleaseNotes`: delivered work by issue type, then what was canceled, then what is
    still open. Three queries — the space's name, the issues with their keys and stages, and
    the progress — whatever the size of the release."""
    workspace_id = str(release.workspace_id)
    space_name = (db.query(Space.name)
                  .filter(Space.workspace_id == workspace_id, Space.id == release.space_id)
                  .scalar()) or ""
    rows = (_issue_rows(workspace_id, db.query(Issue.id, Issue.number, Issue.title, Issue.type,
                                               Space.key, WorkflowStatus.category))
            .join(Space, and_(Space.id == Issue.space_id, Space.workspace_id == workspace_id))
            .filter(IssueFixVersion.release_id == release.id)
            .order_by(Space.key, Issue.number, Issue.id)
            .all())
    items = [{"id": issue_id, "key": format_issue_key(str(key), int(number)), "title": title,
              "type": issue_type, "outcome": outcome(category)}
             for issue_id, number, title, issue_type, key, category in rows]

    sections: list[dict] = []
    seen_types = {item["type"] for item in items}
    # The known types in their order, then — defensively — any other type a row carries.
    for issue_type in (*NOTE_TYPES, *sorted(seen_types - set(NOTE_TYPES))):
        delivered = [item for item in items
                     if item["outcome"] == "done" and item["type"] == issue_type]
        if delivered:
            sections.append({"type": issue_type,
                             "title": NOTE_TITLES.get(issue_type, issue_type.title()),
                             "issues": delivered})
    for kind in ("canceled", "open"):
        rest = [item for item in items if item["outcome"] == kind]
        if rest:
            sections.append({"type": kind, "title": NOTE_TITLES[kind], "issues": rest})
    return {"release": payloads(db, [release])[0],
            "text": render_notes(_heading(space_name, release), sections),
            "sections": sections}
