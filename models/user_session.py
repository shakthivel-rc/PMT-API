import hashlib
import uuid

from sqlalchemy import CHAR, Column, DateTime, String
from sqlalchemy.sql import func

from db.session import Base


def hash_session_token(token: str) -> str:
    """The value `access_token_hash` and `refresh_token_hash` hold for `token`.

    It lives beside the columns rather than in `utils/` because it is not a general-purpose
    hash: it defines what those two columns *contain*. A second implementation that
    disagreed by so much as an encoding would fail nowhere — it would simply never match a
    row, and every authenticated request would answer "Invalid session or token revoked"
    with nothing in any log to say why. Both writers (`controllers/auth_controller.py`) and
    both readers (that module's logout, and `routes/auth_middleware.py`) already import
    `UserSession` from here, so putting it here adds no import edge to anything.

    Plain SHA-256, not bcrypt, and deliberately. The input is a 200-plus character JWT
    signed with `SECRET_KEY`, not a human-chosen password: there is no dictionary to run
    against it and no rainbow table that could hold it, so the work factor bcrypt exists to
    impose would buy nothing here and would be paid on the single highest-frequency query in
    the service. Passwords are a different problem and go through `utils/password.py`.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), nullable=False)
    # String(36), not a bare String: this is a users.id UUID, and an unbounded VARCHAR is
    # not a type MySQL can create a column from — a fresh `alembic upgrade head` built from
    # this metadata would fail on it.
    user_id = Column(String(36), nullable=False, index=True)
    # SHA-256 of the access token, hex. The token itself is not stored anywhere.
    #
    # Two findings, one column, closed together on 2026-08-27 (SEC-5 and P-1).
    #
    # Security: this was `access_token VARCHAR(1024)` holding the raw JWT, while the refresh
    # token beside it was already stored hashed. A read of `user_sessions` — a backup, a
    # replica, a support query, an injection anywhere in the service — was therefore a set
    # of tokens that authenticate immediately, with no password. The hashed pattern was
    # already here and working; the access token had simply never adopted it.
    #
    # Performance: `assert_session_is_live` looks a session up by token on *every*
    # authenticated request, and an index could not be added where the column stood — MySQL
    # counts a utf8mb4 VARCHAR(1024) as 4096 bytes against InnoDB's 3072-byte key limit
    # (CLAUDE.md §17.5), so it is not indexable at any length that would cover it. The one
    # fix closes both: hash first, then index the hash.
    #
    # CHAR(64) because a hex SHA-256 digest is exactly 64 characters, always — 256 bytes as
    # utf8mb4, comfortably inside the key limit, and CHAR rather than VARCHAR because a
    # fixed-width value gains nothing from a length prefix.
    access_token_hash = Column(CHAR(64), nullable=False, index=True)
    refresh_token_hash = Column(String(128), nullable=True, index=True)
    refresh_token_expires_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    user_agent = Column(String(512), nullable=True)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
