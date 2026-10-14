"""Version control: which repositories feed a workspace, and what they link to.

Phase 1 is **inbound only** — GitHub tells us about pull requests and we link them to the
issues their branch, title or body names. Nothing here holds a credential: the webhook is
authenticated by an HMAC shared secret that lives in the environment
(`GITHUB_WEBHOOK_SECRET`), not in a row. Outbound calls (posting a status back to GitHub,
creating a branch from an issue) need an installation token, which is the point at which
CLAUDE.md §7 rule 3 — a separate encryption key for stored credentials — starts to apply.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import CHAR, JSON, Column, DateTime, ForeignKey, Index, String, UniqueConstraint

from db.session import Base

VCS_PROVIDERS: tuple[str, ...] = ("github", "gitlab", "bitbucket")
#: What became of a delivery (docs/design/09-restructuring.md §6.3). `deferred`: it named an
#: issue in a space frozen for a migration, so it was kept, payload and all, to be applied
#: when the space is unfrozen; `replayed` once it has been; `failed` if the replay raised.
DELIVERY_STATUSES: tuple[str, ...] = ("processed", "deferred", "replayed", "failed")
LINK_KINDS: tuple[str, ...] = ("pull_request", "branch", "commit")
PULL_REQUEST_STATES: tuple[str, ...] = ("open", "closed", "merged")


def _now() -> datetime:
    return datetime.now(UTC)


class VcsRepository(Base):
    """A repository whose events a workspace accepts.

    Unique per provider: one repository feeds one workspace. Two workspaces claiming the
    same repository would let whoever linked it second read the other's branch names and
    PR titles through the issues they link to — a disclosure across tenants.
    """

    __tablename__ = "vcs_repositories"
    __table_args__ = (
        UniqueConstraint("provider", "full_name", name="uq_vcs_repositories_provider_name"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False, index=True)
    provider = Column(String(20), nullable=False, default="github")
    # `owner/name`, as GitHub spells it in `repository.full_name`. Compared lower-cased:
    # GitHub treats repository names case-insensitively and so must the lookup.
    full_name = Column(String(200), nullable=False)
    created_by = Column(CHAR(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)


class VcsLink(Base):
    """A pull request, branch or commit attached to an issue."""

    __tablename__ = "vcs_links"
    __table_args__ = (
        UniqueConstraint("issue_id", "provider", "kind", "external_id",
                         name="uq_vcs_links_issue_external"),
    )

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=False)
    issue_id = Column(CHAR(36), ForeignKey("issues.id"), nullable=False, index=True)
    repository_id = Column(CHAR(36), ForeignKey("vcs_repositories.id"), nullable=False)
    provider = Column(String(20), nullable=False, default="github")
    kind = Column(String(20), nullable=False)
    # Stable across renames: `owner/name#123` for a pull request.
    external_id = Column(String(255), nullable=False)
    url = Column(String(500), nullable=False)
    title = Column(String(500), nullable=True)
    state = Column(String(20), nullable=True)
    author_login = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class WebhookDelivery(Base):
    """Every delivery id we have processed, so a redelivery is a no-op.

    GitHub delivers at least once and redelivers on a timeout or on request from its UI. The
    handlers are idempotent on their own for links (an upsert), but not for side effects
    like "moved to Done" events, so the delivery id is checked first. The primary key is
    GitHub's `X-GitHub-Delivery` GUID; an insert that collides is the duplicate.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # The replay on unfreeze: one workspace's deferred deliveries, oldest first.
        Index("ix_webhook_deliveries_workspace_status", "workspace_id", "status",
              "received_at"),
    )

    id = Column(String(64), primary_key=True)
    provider = Column(String(20), nullable=False)
    event = Column(String(50), nullable=False)
    received_at = Column(DateTime, default=_now, nullable=False)
    status = Column(String(20), nullable=False, default="processed",
                    server_default="processed")
    # The repository's workspace, when the delivery got far enough to know it.
    workspace_id = Column(CHAR(36), ForeignKey("workspaces.id"), nullable=True)
    # Kept only while a delivery is deferred: it is the only copy of what GitHub said, and
    # GitHub was told 200, so it will not send it again. Cleared once replayed.
    payload = Column(JSON, nullable=True)
    processed_at = Column(DateTime, nullable=True)
    note = Column(String(255), nullable=True)
