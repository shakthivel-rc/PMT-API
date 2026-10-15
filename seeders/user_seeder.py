from sqlalchemy.orm import Session
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import uuid
from db.session import SessionLocal
from models.user import User
from models.roles import Role
from models.permissions import Permission
from models.user_roles import UserRole
from models.role_permissions import RolePermission
from dotenv import load_dotenv
from core.permissions import TARGET_PERMISSION_CATALOG
from utils.password import hash_password

load_dotenv()

# The seeded accounts' identities. Overridable, because these are the credentials someone
# signs in with and a hard-coded address in a public repository is both a branding leak and
# a guessable half of a login.
#
# `.local` is deliberate: it is reserved and unroutable, so a default install cannot be
# mistaken for a real mailbox and no verification mail can escape to a domain you do not own.
#
# NOTE FOR EXISTING INSTALLS: the seeder matches on email, so changing this value does not
# rename an account that is already in the database — it creates a second one. If you seeded
# before this changed, either set SUPERADMIN_EMAIL to the address you already have, or update
# the existing row.
DEFAULT_SUPERADMIN_EMAIL = "superadmin@base-project.local"
DEFAULT_MEMBER_EMAIL = "member@base-project.local"

# Default functional roles and their baseline permissions
DEFAULT_ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "Project Manager": (
        "workspaces:read", "workspaces:update", "governance:read",
        "spaces:read", "spaces:create", "spaces:update", "spaces:archive", "spaces:freeze",
        "folders:create", "folders:update", "folders:delete",
        "lists:create", "lists:update", "lists:delete",
        "issues:read", "issues:create", "issues:update", "issues:delete",
        "issues:transition", "issues:assign", "issues:bulk",
        "comments:create", "comments:update", "comments:delete",
        "attachments:upload", "attachments:delete",
        "sprints:read", "sprints:create", "sprints:update", "sprints:delete",
        "sprints:start", "sprints:complete", "backlog:manage",
        "workflow:read", "workflow:create", "workflow:update",
        "initiatives:read", "initiatives:create", "initiatives:update", "initiatives:link",
        "time:read", "time:log", "time:manage", "timesheets:approve",
        "view_reports", "reports:read", "reports:create", "reports:update",
        "reports:export", "reports:schedule",
        "releases:read", "releases:create", "releases:update",
        "ai:chat", "poker:participate", "poker:manage",
        "timeline:read", "timeline:manage",
        "notifications:read", "activity:read",
        "storage:read", "integrations:read",
    ),
    "Product Owner": (
        "workspaces:read", "governance:read",
        "spaces:read", "spaces:create", "spaces:update",
        "folders:create", "folders:update",
        "lists:create", "lists:update",
        "issues:read", "issues:create", "issues:update", "issues:transition", "issues:assign",
        "comments:create", "comments:update",
        "attachments:upload",
        "sprints:read", "sprints:create", "sprints:update",
        "sprints:start", "sprints:complete", "backlog:manage",
        "workflow:read",
        "initiatives:read", "initiatives:create", "initiatives:update",
        "initiatives:delete", "initiatives:link",
        "time:read", "time:log",
        "view_reports", "reports:read", "reports:create", "reports:export",
        "releases:read", "releases:create", "releases:update",
        "ai:chat", "poker:participate", "poker:manage",
        "timeline:read", "timeline:manage",
        "notifications:read", "activity:read",
    ),
    "Developer": (
        "workspaces:read",
        "spaces:read",
        "issues:read", "issues:create", "issues:update", "issues:transition",
        "comments:create", "comments:update",
        "attachments:upload",
        "sprints:read",
        "workflow:read",
        "initiatives:read",
        "time:read", "time:log",
        "releases:read",
        "ai:chat", "poker:participate",
        "timeline:read",
        "notifications:read", "activity:read",
        "storage:read", "integrations:read",
    ),
    "QA Engineer": (
        "workspaces:read",
        "spaces:read",
        "issues:read", "issues:create", "issues:update", "issues:transition",
        "comments:create", "comments:update",
        "attachments:upload",
        "sprints:read",
        "workflow:read",
        "time:read", "time:log",
        "view_reports", "reports:read", "reports:export",
        "releases:read",
        "ai:chat", "poker:participate",
        "timeline:read",
        "notifications:read", "activity:read",
    ),
    "Stakeholder": (
        "workspaces:read",
        "spaces:read",
        "issues:read",
        "sprints:read",
        "initiatives:read",
        "view_reports", "reports:read",
        "timeline:read",
        "notifications:read", "activity:read",
    ),
    "Finance Manager": (
        "workspaces:read", "governance:read",
        "time:read", "time:manage", "timesheets:approve",
        "view_reports", "reports:read", "reports:create", "reports:export", "reports:schedule",
        "notifications:read", "activity:read",
    ),
}


def _get_or_create_role(session: Session, name: str) -> Role:
    role = session.query(Role).filter(Role.name == name).first()
    if role:
        return role
    role = Role(id=str(uuid.uuid4()), name=name)
    session.add(role)
    session.flush()
    return role


def _get_or_create_permission(session: Session, name: str, machine_name: str) -> Permission:
    permission = session.query(Permission).filter(Permission.machine_name == machine_name).first()
    if permission:
        return permission
    permission = Permission(id=str(uuid.uuid4()), name=name, machine_name=machine_name)
    session.add(permission)
    session.flush()
    return permission


def _assign_permission(session: Session, role_id: str, permission_id: str) -> None:
    exists = session.query(RolePermission).filter(
        RolePermission.role_id == role_id,
        RolePermission.permission_id == permission_id,
    ).first()
    if not exists:
        session.add(RolePermission(id=str(uuid.uuid4()), role_id=role_id, permission_id=permission_id))


def _assign_role(session: Session, user_id: str, role_id: str) -> None:
    exists = session.query(UserRole).filter(
        UserRole.user_id == user_id,
        UserRole.role_id == role_id,
    ).first()
    if not exists:
        session.add(UserRole(id=str(uuid.uuid4()), user_id=user_id, role_id=role_id))

def seed_database() -> bool:
    """Seed the roles, the permission catalogue and the two default accounts; True if committed.

    Errors are caught, rolled back and printed rather than raised, so a caller in-process
    (the tests) reads the printed line; the return value is what the command line turns into
    an exit status.
    """
    session: Session = SessionLocal()

    try:
        admin_role = _get_or_create_role(session, "Admin")
        member_role = _get_or_create_role(session, "Member")

        # `manage_users` is the one slug the kept routes check (`check_permissions` on every
        # write under /users, /roles and /permissions, and the all-users view of
        # /activity-logs). It is a legacy coarse slug rather than a catalogue entry — its
        # implications live in `core.permissions.LEGACY_PERMISSION_IMPLICATIONS` — so it is
        # seeded here by name; nothing else would create it.
        manage_users_permission = _get_or_create_permission(session, "Manage Users", "manage_users")
        target_permissions = [
            _get_or_create_permission(session, name, machine_name)
            for name, machine_name in TARGET_PERMISSION_CATALOG
        ]

        # Seed Users
        default_password = os.getenv("SUPERADMIN_PASSWORD")
        if not default_password:
            raise RuntimeError("SUPERADMIN_PASSWORD is required to seed default users")

        # `or`, not os.getenv's default argument: .env.example ships these as blank
        # placeholders, and a key that is present-but-empty makes getenv return "" rather
        # than fall back — which would seed an account with no address at all. This is the
        # same distinction between "set" and "has a value" that broke scripts/bootstrap.sh.
        admin_email = os.getenv("SUPERADMIN_EMAIL", "").strip() or DEFAULT_SUPERADMIN_EMAIL
        member_email = os.getenv("SEED_MEMBER_EMAIL", "").strip() or DEFAULT_MEMBER_EMAIL

        admin_user = session.query(User).filter(User.email == admin_email).first()
        if not admin_user:
            admin_user = User(
                id=str(uuid.uuid4()),
                email=admin_email,
                username=admin_email,
                first_name="Base Project",
                last_name="Admin",
                organization="Base Project",
                department="admin",
                password=hash_password(default_password),
                status=1,
            )
            session.add(admin_user)
            session.flush()

        member_user = session.query(User).filter(User.email == member_email).first()
        if not member_user:
            member_user = User(
                id=str(uuid.uuid4()),
                email=member_email,
                username=member_email,
                first_name="Base Project",
                last_name="Member",
                organization="Base Project",
                department="general",
                password=hash_password(default_password),
                status=1,
            )
            session.add(member_user)
            session.flush()

        _assign_role(session, admin_user.id, admin_role.id)
        _assign_role(session, member_user.id, member_role.id)

        _assign_permission(session, admin_role.id, manage_users_permission.id)
        for permission in target_permissions:
            _assign_permission(session, admin_role.id, permission.id)

        # Seed additional functional roles and their mapped permissions
        perm_by_slug = {p.machine_name: p for p in target_permissions}
        perm_by_slug[manage_users_permission.machine_name] = manage_users_permission

        for role_name, perms in DEFAULT_ROLE_PERMISSIONS.items():
            functional_role = _get_or_create_role(session, role_name)
            for p_slug in perms:
                perm_obj = perm_by_slug.get(p_slug)
                if perm_obj:
                    _assign_permission(session, functional_role.id, perm_obj.id)

        # Member is deliberately granted nothing. It is the least-privileged role — also the
        # one `set_password_after_registration_controller` gives every self-registered
        # account — so it can do exactly what any authenticated caller can do without a
        # permission check and nothing more. That is more than "itself": besides its own
        # profile, password and activity, the read routes that declare no check (GET
        # /users, /users/{id}, /roles, /permissions) answer any valid token, so a Member can
        # read the user directory and the RBAC catalogue. A project built on this base
        # grants Member its own feature slugs as it adds them; seeding a catalogue slug here
        # would silently widen every self-registration.
        #
        # One consequence to know about: `PUT /roles/{id}` refuses an empty permission list,
        # so editing Member in the RBAC screen means giving it at least one permission.

        # Commit all changes
        session.commit()
        print("Database seeding completed successfully!")
        return True

    except Exception as e:
        session.rollback()
        print(f"Error seeding database: {e}")
        return False

    finally:
        session.close()


if __name__ == "__main__":
    # The exit status is the only thing the two scripted callers — the container entrypoint
    # and native setup — can tell a failed seed from a finished one by. Exiting 0 after
    # printing an error made both report success over a database with no account in it.
    # A re-run over an already seeded database changes nothing and still exits 0.
    sys.exit(0 if seed_database() else 1)
