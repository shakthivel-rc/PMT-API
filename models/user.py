from sqlalchemy import Column, Integer, String, DateTime, Boolean
from sqlalchemy.orm import relationship, validates
from sqlalchemy.dialects.mysql import CHAR
from db.session import Base
from datetime import datetime, timezone
import uuid
from models.user_roles import UserRole  # noqa: F401 — registers the mapped class; the relationship() strings below resolve through SQLAlchemy's registry, not this name
from models.roles import Role  # noqa: F401 — registers the mapped class; the relationship() strings below resolve through SQLAlchemy's registry, not this name


class User(Base):
    __tablename__ = "users"
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    email = Column(String(100), unique=True, nullable=False)
    username = Column(String(100), unique=True, nullable=False)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    organization = Column(String(100), nullable=False)
    department = Column(String(100), nullable=False)
    password = Column(String(512), nullable=True)
    status = Column(Boolean, nullable=False, default=False)
    fp_token = Column(String(255), nullable=True)
    fp_token_created_at = Column(DateTime, nullable=True)
    verify_token = Column(String(255), nullable=True)
    otp_code = Column(String(255), nullable=True)
    otp_created_at = Column(DateTime, nullable=True)
    failed_login_attempts = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
    last_failed_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)
    deleted_at = Column(DateTime, nullable=True)  # Allow NULL values for soft deletion
    deleted = Column(Integer, nullable=False, default=0)  # 0 for active, 1 for deleted

    # A soft-deleted role must stop granting. `dependencies/auth.permission_slugs` walks this
    # collection and then each role's permissions, so an unfiltered relationship means a role
    # withdrawn by setting `roles.deleted = 1` keeps handing every permission it held to every
    # user it was assigned to, on every request, indefinitely.
    #
    # docs/production-readiness-plan.md (SEC-3) prefers deleting the `user_roles` rows over
    # filtering them — "a filter is one place to forget, an absent row is nowhere" — and for
    # the one deletion path that exists that guarantee already holds: `DELETE /roles/{id}`
    # refuses while any user still holds the role and then hard-deletes the row
    # (`controllers/roles.remove_role`), so it cannot leave a grant behind. This filter is for
    # every *other* route to `roles.deleted = 1`. The column and its `@validates` hook exist
    # on `Role`, nothing in the service sets them today, and a support query, a migration or a
    # future "retire this role" screen that did would otherwise change nothing at all about
    # who can do what. That is the same delegated-safety shape as SEC-2 and it gets the same
    # answer: make the guarantee structural rather than procedural.
    #
    # `viewonly=True` because nothing writes through this collection — a grant is written as a
    # `UserRole` row by `services/user.assign_role_for_user` and
    # `services/user_role.add_roles_to_user` — and a `secondaryjoin` carrying a filter is not
    # something SQLAlchemy can persist through anyway.
    #
    # `Role.permissions` is deliberately NOT filtered the same way: it is what the RBAC screens
    # render (`controllers/roles.fetch_role`), permissions are hard-deleted and refused while
    # still assigned to a role (`controllers/permissions.delete_permission_by_id`), so
    # `permissions.deleted` is never 1 and a filter there could only ever hide a row from the
    # screen whose job is to manage it.
    roles = relationship(
        "Role",
        secondary="user_roles",
        primaryjoin="User.id == UserRole.user_id",
        secondaryjoin="and_(Role.id == UserRole.role_id, Role.deleted != 1)",
        back_populates="users",
        viewonly=True,
    )

    @validates('deleted')
    def validate_deleted(self, key, value):
        if value == 1:
            self.deleted_at = datetime.now(timezone.utc)
        elif value == 0:
            self.deleted_at = None  # Reset deleted_at if user is marked active again
        return value


# Event listener to automatically update the updated_at column
from sqlalchemy import event

@event.listens_for(User, 'before_update')
def receive_before_update(mapper, connection, target):
    target.updated_at = datetime.now(timezone.utc)