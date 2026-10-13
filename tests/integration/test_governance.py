"""The governance settings document, `workspaces.settings["governance"]` — §3.3.

How strictly a workspace applies the restructuring plan's creation rules. Shaped like the
time-tracking and report settings: a guest reads them, an admin changes them field by field,
and each change is audited. The one rule of its own: `default_workflow_template` must name a
template that exists here and can be applied, or every space created without naming one
would be refused.
"""
import pytest

from models.activity_log import ActivityLog
from models.hierarchy import WorkflowTemplate
from models.workspace import Workspace
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration

DEFAULTS = {"require_space_charter": False, "require_epic_owner_and_criteria": True,
            "default_workflow_template": "default", "inactive_after_days": 90}


def _path(ws: str) -> str:
    return f"/workspaces/{ws}/settings/governance"


def _setup(client, db):
    owner = pm.person(db, "owner-1")
    ws = pm.workspace(client, owner)
    member = pm.person(db, "member-1")
    pm.join(db, ws, "member-1", "member")
    guest = pm.person(db, "guest-1")
    pm.join(db, ws, "guest-1", "guest")
    return owner, member, guest, ws


def test_a_guest_reads_the_defaults(client, db):
    """Every workspace starts with the §3.3 defaults — the epic rule on, the space checklist
    off — and reading them is open to everyone in the workspace."""
    _owner, _member, guest, ws = _setup(client, db)
    assert pm.ok(client.get(_path(ws), headers=guest)) == DEFAULTS


def test_an_admin_changes_them_and_they_round_trip(client, db):
    """What PATCH answers is what GET reads back and what is stored, and the other features'
    settings in the same JSON document survive the write."""
    owner, _member, _guest, ws = _setup(client, db)
    pm.ok(client.patch(f"/workspaces/{ws}/settings/time-tracking", json={"hours_per_day": 7},
                       headers=owner))

    saved = pm.ok(client.patch(_path(ws), json={
        "require_space_charter": True, "require_epic_owner_and_criteria": False,
        "default_workflow_template": "delivery", "inactive_after_days": 30}, headers=owner))
    assert saved == {"require_space_charter": True, "require_epic_owner_and_criteria": False,
                     "default_workflow_template": "delivery", "inactive_after_days": 30}
    assert pm.ok(client.get(_path(ws), headers=owner)) == saved

    stored = db.query(Workspace.settings).filter(Workspace.id == ws).scalar()
    assert stored["governance"] == saved
    assert stored["time_tracking"]["hours_per_day"] == 7


def test_only_the_fields_present_change(client, db):
    """A partial update is partial: one field in, the other three untouched."""
    owner, _member, _guest, ws = _setup(client, db)
    pm.ok(client.patch(_path(ws), json={"inactive_after_days": 60}, headers=owner))
    after = pm.ok(client.patch(_path(ws), json={"require_space_charter": True}, headers=owner))
    assert after == {**DEFAULTS, "inactive_after_days": 60, "require_space_charter": True}


def test_only_admins_change_them_and_strangers_see_nothing(client, db):
    """Members and guests are refused with 403; someone outside the workspace gets the
    workspace's 404 for both verbs."""
    owner, member, guest, ws = _setup(client, db)
    stranger = pm.person(db, "stranger")
    for headers in (member, guest):
        assert client.patch(_path(ws), json={"require_space_charter": True},
                            headers=headers).status_code == 403
    assert client.get(_path(ws), headers=stranger).status_code == 404
    assert client.patch(_path(ws), json={"require_space_charter": True},
                        headers=stranger).status_code == 404
    assert pm.ok(client.get(_path(ws), headers=owner)) == DEFAULTS


def test_the_default_template_must_exist_here_and_apply(client, db):
    """Built-in keys, with or without `builtin:`, and this workspace's live templates are
    accepted. A made-up key, another workspace's template, a deleted one and one that cannot
    be applied are 422 — and leave the stored default as it was."""
    owner, _member, _guest, ws = _setup(client, db)
    other = pm.person(db, "owner-2")
    other_ws = pm.workspace(client, other, "bravo")
    good = {"stages": [{"name": "Open", "category": "todo"},
                       {"name": "Done", "category": "done"}], "initial": 0, "merge_target": 1}

    def template(workspace_id, definition, created_by="owner-1", deleted=0):
        row = WorkflowTemplate(workspace_id=workspace_id, name="T", definition=definition,
                               created_by=created_by, deleted=deleted)
        db.add(row)
        db.commit()
        return row.id

    ours = template(ws, good)
    for ref in ("builtin:delivery", "support", ours):
        assert pm.ok(client.patch(_path(ws), json={"default_workflow_template": ref},
                                  headers=owner))["default_workflow_template"] == ref

    refused = ("kanban-pro", "builtin:nope", template(other_ws, good, "owner-2"),
               template(ws, good, deleted=1), template(ws, {"stages": []}))
    for ref in refused:
        response = client.patch(_path(ws), json={"default_workflow_template": ref},
                                headers=owner)
        assert response.status_code == 422, ref
    assert pm.ok(client.get(_path(ws), headers=owner))["default_workflow_template"] == ours


def test_null_out_of_range_and_unknown_fields_are_refused(client, db):
    """Settings cannot be cleared to nothing, `inactive_after_days` stays within 7-3650, and
    a field the document does not have is a 422, not silently dropped."""
    owner, _member, _guest, ws = _setup(client, db)
    for body in ({"require_space_charter": None}, {"inactive_after_days": 3},
                 {"inactive_after_days": 4000}, {"default_workflow_template": ""},
                 {"require_space_charter_for_epics": True}):
        assert client.patch(_path(ws), json=body, headers=owner).status_code == 422, body
    assert pm.ok(client.get(_path(ws), headers=owner)) == DEFAULTS


def test_a_change_is_audited(client, db):
    """Who changed how strictly the workspace applies its creation rules, and to what — the
    audit trail is where that question is answered."""
    owner, _member, _guest, ws = _setup(client, db)
    pm.ok(client.patch(_path(ws), json={"require_space_charter": True}, headers=owner))
    entry = (db.query(ActivityLog)
             .filter(ActivityLog.entity_type == "workspace_settings",
                     ActivityLog.entity_id == ws)
             .one())
    assert entry.user_id == "owner-1" and entry.action == "UPDATE"
    assert "Governance settings" in entry.details
    assert "require_space_charter=True" in entry.details


def test_a_stored_document_that_no_longer_validates_reads_as_the_defaults(client, db):
    """A bad document must not fail every route that consults the settings
    (services/governance.settings_for); it reads as the defaults until an admin saves."""
    owner, _member, _guest, ws = _setup(client, db)
    db.query(Workspace).filter(Workspace.id == ws).update(
        {"settings": {"governance": {"inactive_after_days": 1}}})
    db.commit()
    assert pm.ok(client.get(_path(ws), headers=owner)) == DEFAULTS
    saved = pm.ok(client.patch(_path(ws), json={"inactive_after_days": 14}, headers=owner))
    assert saved == {**DEFAULTS, "inactive_after_days": 14}
