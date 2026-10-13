"""Shared builders for the test suite.

Kept out of `conftest.py` because these are *constructors*, not fixtures — they take
arguments, and a test that needs a user holding exactly two slugs should say so at the call
site rather than through a fixture whose name encodes it.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Iterable

import jwt
from sqlalchemy.orm import Session

from core.config import get_settings
from core.permissions import TARGET_PERMISSION_CATALOG
from models.permissions import Permission
from models.role_permissions import RolePermission
from models.roles import Role
from models.user import User
from models.user_roles import UserRole
from models.user_session import UserSession, hash_session_token
from utils.password import hash_password


def access_token(sub: str) -> str:
    settings = get_settings()
    payload = {"sub": sub, "type": "access", "exp": int(time.time()) + 3600}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def seed_user(db: Session, user_id: str, slugs: Iterable[str]) -> str:
    """A signed-in user holding exactly `slugs` and nothing else."""
    db.add(User(
        id=user_id,
        email=f"{user_id}@example.com",
        username=user_id,
        first_name="Test",
        last_name="User",
        organization="Org",
        department="Ops",
        password=hash_password("Abcdef123!@#"),
        status=True,
        deleted=0,
    ))
    role = Role(id=str(uuid.uuid4()), name=f"role-{user_id}")
    db.add(role)
    db.flush()

    catalog = {machine: name for name, machine in TARGET_PERMISSION_CATALOG}
    for slug in slugs:
        # `permissions.machine_name` is unique, so a second user wanting the same slug has
        # to reuse the row rather than insert another — exactly what the seeder does.
        permission = db.query(Permission).filter(Permission.machine_name == slug).first()
        if permission is None:
            permission = Permission(id=str(uuid.uuid4()), name=catalog.get(slug, slug),
                                    machine_name=slug)
            db.add(permission)
            db.flush()
        db.add(RolePermission(id=str(uuid.uuid4()), role_id=role.id,
                              permission_id=permission.id))
    db.add(UserRole(id=str(uuid.uuid4()), user_id=user_id, role_id=role.id))

    token = access_token(user_id)
    db.add(UserSession(user_id=user_id, access_token_hash=hash_session_token(token)))
    db.commit()
    return token

