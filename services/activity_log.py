"""The audit trail for things a *user* did.

`activity_logs.user_id` is `NOT NULL` and carries a foreign key to `users.id`, which is the
right shape for the question this table answers — who did that. Work with no local user
behind it (a scheduled job, a system process, an inbound integration) has no row here and
must keep its own audit record; inventing a user to satisfy the column would attribute
automated work to a real person.
"""
import logging

from sqlalchemy.orm import Session

from models.activity_log import ActivityLog

logger = logging.getLogger("base_project.activity")


def log_activity(
    db: Session,
    user_id: str,
    user_name: str,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    details: str | None = None,
    ip_address: str | None = None,
):
    """Log an activity entry. Fire-and-forget — a failure here never breaks the caller.

    The savepoint is what makes "never breaks the caller" true rather than aspirational.
    Swallowing the exception alone is not enough: a failed flush leaves the *session* in a
    rolled-back state, and the caller's next `commit()` then raises `PendingRollbackError`
    from a line that has nothing to do with logging. Rolling back to a savepoint discards
    only this insert.
    """
    if not user_id:
        # No local user, so there is nothing this table can honestly record. Automated work
        # keeps its own audit record.
        return
    try:
        with db.begin_nested():
            db.add(ActivityLog(
                user_id=user_id,
                user_name=user_name,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                details=details,
                ip_address=ip_address,
            ))
    except Exception:  # logging must never break business logic
        logger.warning(
            "could not write an activity log entry",
            extra={"extra_fields": {"action": action, "entity_type": entity_type,
                                    "entity_id": entity_id}},
        )
