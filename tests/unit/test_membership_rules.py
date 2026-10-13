"""Who may change whose standing in a workspace, and what the two new bodies accept.

`utils/membership_rules.py` decides every 403 and the last-owner 409 of the member routes
without a database, so the whole matrix is here; tests/integration/test_workspace_admin.py
checks that the routes ask it, and everything that needs rows. A mistake in either direction
is costly: too strict and an owner cannot hand the workspace over, too loose and an admin
can promote themselves to owner — and then delete the workspace.
"""
import pytest
from pydantic import ValidationError

from schemas.workspaces import MemberUpdate, WorkspaceCreate, WorkspaceUpdate
from utils import membership_rules as rules

pytestmark = pytest.mark.unit

ROLES = ("guest", "member", "admin", "owner")


def change(caller: str, current: str, new: str, owners: int = 2):
    return rules.role_change_problem(caller_role=caller, current_role=current, new_role=new,
                                     live_owners=owners)


def remove(caller: str, target: str, *, leaving: bool = False, owners: int = 2):
    return rules.removal_problem(caller_role=caller, leaving=leaving, target_role=target,
                                 live_owners=owners)


class TestRoleChange:
    @pytest.mark.parametrize("caller", ["admin", "owner"])
    @pytest.mark.parametrize("role", ROLES)
    def test_the_role_someone_already_has_is_never_refused(self, caller, role):
        """Even the last owner "becoming" an owner, and an admin naming an owner's own role:
        nobody is made an owner and no owner's role changes, so it is a no-op, not a 403."""
        assert change(caller, role, role, owners=1) is None

    @pytest.mark.parametrize("current,new", [(a, b) for a in ("guest", "member", "admin")
                                             for b in ("guest", "member", "admin") if a != b])
    def test_an_admin_moves_people_between_guest_member_and_admin(self, current, new):
        assert change("admin", current, new) is None

    @pytest.mark.parametrize("current", ["guest", "member", "admin"])
    def test_only_an_owner_makes_someone_an_owner(self, current):
        assert change("admin", current, "owner") == (403, rules.MAKE_OWNER)
        assert change("owner", current, "owner") is None

    @pytest.mark.parametrize("new", ["guest", "member", "admin"])
    def test_only_an_owner_changes_an_owners_role(self, new):
        assert change("admin", "owner", new) == (403, rules.CHANGE_OWNER)
        assert change("owner", "owner", new, owners=2) is None

    @pytest.mark.parametrize("new", ["guest", "member", "admin"])
    def test_the_last_owner_cannot_be_demoted(self, new):
        assert change("owner", "owner", new, owners=1) == (409, rules.LAST_OWNER)

    def test_the_403_is_decided_before_the_last_owner_count(self):
        """An admin trying to demote the only owner is told it is not theirs to do — not that
        the owner is the last one, which is true but not the reason."""
        assert change("admin", "owner", "member", owners=1) == (403, rules.CHANGE_OWNER)

    def test_the_wording_is_the_contracts(self):
        assert rules.LAST_OWNER == ("A workspace needs at least one owner. Make someone else "
                                    "an owner first.")


class TestRemoval:
    @pytest.mark.parametrize("role", ["guest", "member", "admin"])
    def test_anyone_may_leave(self, role):
        assert remove(role, role, leaving=True) is None

    def test_an_owner_may_leave_while_another_owner_stays(self):
        assert remove("owner", "owner", leaving=True, owners=2) is None

    def test_the_last_owner_cannot_leave(self):
        assert remove("owner", "owner", leaving=True, owners=1) == (409, rules.LAST_OWNER)

    @pytest.mark.parametrize("caller", ["guest", "member"])
    @pytest.mark.parametrize("target", ROLES)
    def test_removing_someone_else_needs_admin_or_owner(self, caller, target):
        assert remove(caller, target) == (403, rules.REMOVE_SOMEONE_ELSE)

    @pytest.mark.parametrize("caller", ["admin", "owner"])
    @pytest.mark.parametrize("target", ["guest", "member", "admin"])
    def test_admins_and_owners_remove_guests_members_and_admins(self, caller, target):
        assert remove(caller, target) is None

    def test_only_an_owner_removes_an_owner(self):
        assert remove("admin", "owner") == (403, rules.REMOVE_OWNER)
        assert remove("owner", "owner", owners=2) is None


class TestBodies:
    """The request bodies: what a 422 is decided on before any rule runs."""

    @pytest.mark.parametrize("body", [
        {},                                   # nothing to change
        {"name": None}, {"slug": None},       # a workspace keeps a name and an address
        {"name": ""}, {"name": "   "},        # blank, also after trimming
        {"name": "x" * 101},                  # `workspaces.name` is VARCHAR(100)
        {"slug": "Acme Eng"}, {"slug": "a"}, {"slug": "-acme"}, {"slug": "acme-"},
        {"slug": "x" * 51},                   # `workspaces.slug` is VARCHAR(50)
        {"name": "Acme", "colour": "teal"},   # a misspelt field is a 422, not ignored
    ])
    def test_a_workspace_update_that_cannot_be_applied_is_refused(self, body):
        with pytest.raises(ValidationError):
            WorkspaceUpdate.model_validate(body)

    def test_only_the_fields_sent_are_set_and_the_name_is_trimmed(self):
        update = WorkspaceUpdate.model_validate({"name": "  Acme Platform  "})
        assert update.name == "Acme Platform"
        assert update.model_fields_set == {"name"}

    @pytest.mark.parametrize("slug", ["ab", "acme-eng", "a1-b2-c3", "x" * 50, "a--b", "007"])
    def test_create_and_update_accept_the_same_addresses(self, slug):
        """One pattern for both bodies (`WORKSPACE_SLUG_PATTERN`): an address a workspace can
        be created with is one it can be renamed to, and the other way round."""
        assert WorkspaceUpdate.model_validate({"slug": slug}).slug == slug
        assert WorkspaceCreate.model_validate({"name": "Acme", "slug": slug}).slug == slug

    @pytest.mark.parametrize("body", [{}, {"role": None}, {"role": "boss"},
                                      {"role": "admin", "until": "2027-01-01"}])
    def test_a_member_update_names_one_known_role(self, body):
        with pytest.raises(ValidationError):
            MemberUpdate.model_validate(body)

    @pytest.mark.parametrize("role", ROLES)
    def test_every_role_can_be_given(self, role):
        assert MemberUpdate.model_validate({"role": role}).role == role

    def test_the_documented_example_bodies_are_accepted(self):
        """Would catch: an example in `/docs` that the API itself refuses."""
        for model in (WorkspaceUpdate, MemberUpdate):
            for example in model.model_config["json_schema_extra"]["examples"]:
                model.model_validate(example)
