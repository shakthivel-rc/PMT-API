"""Who may change whose standing in a workspace: the role rules of the member routes.

Pure and type-checked, like `utils/hierarchy_rules.py`, for the same reason: each question
takes a handful of facts — two or three roles, whether the caller is acting on themselves,
how many owners there are — and needs no database to answer, so every combination is a unit
test (tests/unit/test_membership_rules.py) instead of a round trip.

What is written here (2026-10-06, the edit-and-delete change for projects and members):

* **Owners are made and unmade by owners.** An admin manages guests, members and other
  admins. Making someone an owner, changing an owner's role and removing an owner are an
  owner's acts — the line `controllers/workspace_controller.add_member` already drew when it
  refused an admin who grants `owner`.
* **A workspace always keeps an owner.** Only an owner can delete a workspace or make
  someone else an owner, so one without an owner could never be deleted, and nobody could
  ever become its owner again. The caller counts **live** owners: a soft-deleted account
  cannot sign in (`DELETE /users/{id}` revokes its sessions), so it holds the role and can
  exercise none of it, and counting it would let the last real owner step down.
* **Anyone may leave; removing someone else is an admin's.** Leaving is not a power over
  anybody else, so it needs no role — but the last owner cannot leave either.

What a removal does to the person's *work* — the spaces they own, their running timer, the
report schedules that run as them — is not a role question and is not here:
`services/workspace.member_removal_refusal` asks it of the database.
"""
from __future__ import annotations

#: The roles that manage a workspace's membership. Owners also manage owners.
MANAGING_ROLES: frozenset[str] = frozenset({"admin", "owner"})

LAST_OWNER = "A workspace needs at least one owner. Make someone else an owner first."
MAKE_OWNER = "Only an owner can make someone an owner."
CHANGE_OWNER = "Only an owner can change an owner's role."
REMOVE_OWNER = "Only an owner can remove an owner."
REMOVE_SOMEONE_ELSE = ("Only an admin or an owner can remove someone else from this "
                       "workspace. You can remove yourself.")


def role_change_problem(*, caller_role: str, current_role: str, new_role: str,
                        live_owners: int) -> tuple[int, str] | None:
    """Why someone holding `caller_role` may not move a member from `current_role` to
    `new_role`, as `(status, message)`, or None when they may.

    The route admits admins and owners only, so `caller_role` is one of them. A change to
    the role someone already has changes nothing and is never refused — not even an admin
    "making" an owner an owner, which neither makes anyone an owner nor changes an owner's
    role. `live_owners` counts the target too when they are an owner.
    """
    if new_role == current_role:
        return None
    if caller_role != "owner":
        if new_role == "owner":
            return 403, MAKE_OWNER
        if current_role == "owner":
            return 403, CHANGE_OWNER
    if current_role == "owner" and live_owners <= 1:
        return 409, LAST_OWNER
    return None


def removal_problem(*, caller_role: str, leaving: bool, target_role: str,
                    live_owners: int) -> tuple[int, str] | None:
    """Why someone holding `caller_role` may not remove a member who holds `target_role` —
    themselves, when `leaving` — as `(status, message)`, or None when they may.

    Any member reaches the route, because anyone may leave. When they are leaving,
    `target_role` is their own role, so "only an owner removes an owner" lets an owner go
    and the last-owner rule is what stops the last one. `live_owners` counts the target too
    when they are an owner.
    """
    if not leaving and caller_role not in MANAGING_ROLES:
        return 403, REMOVE_SOMEONE_ELSE
    if target_role == "owner" and caller_role != "owner":
        return 403, REMOVE_OWNER
    if target_role == "owner" and live_owners <= 1:
        return 409, LAST_OWNER
    return None
