"""Reading what the model will see out of the database.

Used twice with the same result by construction: by the API, to compute the cache key when
someone asks for a summary, and by the worker, to build the text it actually sends. Both go
through `issue_thread`, so "the thread has not changed" means exactly "the model would be
sent the same bytes".

Everything here is scoped by workspace id, including the comment authors' names — a
summary is readable by every member of the workspace, so it must be built only from what
every member could already read.
"""
from sqlalchemy.orm import Session

from ai.prompts import IssueThread, ThreadComment, ThreadPullRequest
from models.hierarchy import Space, WorkflowStatus
from models.issue import Issue
from models.vcs import VcsLink
from services import issue as issue_service
from utils.issue_keys import format_issue_key


def issue_thread(db: Session, workspace_id: str, issue: Issue) -> IssueThread:
    space_key = db.query(Space.key).filter(Space.id == issue.space_id).scalar() or "?"
    status_name = (
        db.query(WorkflowStatus.name)
        .filter(WorkflowStatus.workspace_id == workspace_id,
                WorkflowStatus.id == issue.status_id)
        .scalar()
    ) or "unknown"
    rows = issue_service.comments(db, workspace_id, issue.id)
    payloads = issue_service.comment_payloads(db, rows)
    links = (
        db.query(VcsLink)
        .filter(VcsLink.workspace_id == workspace_id, VcsLink.issue_id == issue.id,
                VcsLink.kind == "pull_request")
        .order_by(VcsLink.created_at)
        .all()
    )
    return IssueThread(
        key=format_issue_key(space_key, issue.number),
        issue_type=issue.type,
        title=issue.title,
        status=status_name,
        description=issue.description,
        comments=[
            ThreadComment(author=payload["author"]["display_name"] or "Unknown",
                          created_at=payload["created_at"], body=payload["body"])
            for payload in payloads
        ],
        pull_requests=[
            ThreadPullRequest(external_id=link.external_id, title=link.title, state=link.state)
            for link in links
        ],
    )
