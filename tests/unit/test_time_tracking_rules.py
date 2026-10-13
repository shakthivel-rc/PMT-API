"""Who may do what with time, and how settings are read — without a database.

These are the decisions `controllers/time_tracking_controller.py` turns into 403s and 404s.
They are pure functions of a `WorkspaceAccess` and the settings, so they are pinned here as
unit tests: a regression in one is a privacy incident (a member reading a colleague's hours
in a workspace that switched that off), and the unit subset is what the coverage floor
measures (pyproject.toml).
"""
from datetime import datetime

import pytest
from pydantic import ValidationError

from dependencies.workspace import WorkspaceAccess
from models.workspace import Workspace
from schemas.time_tracking import TimeEntryCreate, TimeTrackingSettings
from services import time_tracking as svc

pytestmark = pytest.mark.unit

EVERYONE = TimeTrackingSettings()
OWN = TimeTrackingSettings(visibility="own")
OFF = TimeTrackingSettings(enabled=False)


def _as(role: str, user_id: str = "me") -> WorkspaceAccess:
    return WorkspaceAccess(workspace_id="w1", user_id=user_id, role=role)


class TestCapabilities:
    @pytest.mark.parametrize("role, logs, for_others", [
        ("guest", False, False),
        ("member", True, False),
        ("admin", True, True),
        ("owner", True, True),
    ])
    def test_who_logs_time(self, role, logs, for_others):
        assert svc.can_log(_as(role), EVERYONE) is logs
        assert svc.can_log_for_others(_as(role), EVERYONE) is for_others

    def test_switched_off_nobody_logs(self):
        assert svc.can_log(_as("owner"), OFF) is False
        assert svc.can_log_for_others(_as("owner"), OFF) is False

    def test_a_member_edits_their_own_and_an_admin_anyones(self):
        assert svc.can_edit(_as("member"), "me") is True
        assert svc.can_edit(_as("member"), "someone-else") is False
        assert svc.can_edit(_as("admin"), "someone-else") is True

    def test_a_member_demoted_to_guest_can_no_longer_edit_their_old_time(self):
        """Editing is logging after the fact; a guest does neither."""
        assert svc.can_edit(_as("guest"), "me") is False


class TestVisibility:
    def test_everyone_sees_everyone_by_default(self):
        assert svc.visible_user_id(_as("guest"), EVERYONE) is None
        assert svc.visible_user_id(_as("member"), EVERYONE) is None

    def test_own_restricts_members_and_guests_to_themselves_but_not_admins(self):
        assert svc.visible_user_id(_as("member"), OWN) == "me"
        assert svc.visible_user_id(_as("guest"), OWN) == "me"
        assert svc.visible_user_id(_as("admin"), OWN) is None
        assert svc.visible_user_id(_as("owner"), OWN) is None


class TestSettingsDocument:
    def test_a_workspace_that_never_saved_settings_gets_the_defaults(self):
        assert svc.settings_for(Workspace(settings=None)) == EVERYONE
        assert svc.settings_for(None) == EVERYONE

    def test_saved_values_are_read_back(self):
        workspace = Workspace(settings={"time_tracking": {"visibility": "own",
                                                          "hours_per_day": 6}})
        settings = svc.settings_for(workspace)
        assert (settings.visibility, settings.hours_per_day) == ("own", 6)

    def test_a_document_that_no_longer_validates_falls_back_rather_than_failing(self):
        """Written by hand or by an older release, it must not break every read."""
        broken = Workspace(settings={"time_tracking": {"hours_per_day": 99}})
        assert svc.settings_for(broken) == EVERYONE
        assert svc.settings_for(Workspace(settings=["not", "a", "dict"])) == EVERYONE

    def test_saving_keeps_other_features_keys_and_assigns_a_new_document(self):
        """A JSON column only notices reassignment; mutating in place is silently lost."""
        original = {"ai": {"enabled": True}}
        workspace = Workspace(settings=original)
        svc.save_settings(workspace, OWN)
        assert workspace.settings is not original
        assert workspace.settings["ai"] == {"enabled": True}
        assert workspace.settings["time_tracking"]["visibility"] == "own"


class TestRequestSchemas:
    def test_a_naive_start_is_refused_rather_than_guessed(self):
        with pytest.raises(ValidationError):
            TimeEntryCreate(duration_seconds=60, work_date="2026-10-01",
                            started_at=datetime(2026, 10, 1, 10, 0))

    @pytest.mark.parametrize("field, value", [("hours_per_day", 0.5), ("hours_per_day", 25),
                                              ("days_per_week", 0), ("days_per_week", 8),
                                              ("visibility", "managers")])
    def test_settings_out_of_range_are_refused(self, field, value):
        with pytest.raises(ValidationError):
            TimeTrackingSettings(**{field: value})


@pytest.mark.parametrize("note, expected", [(None, None), ("", None), ("   ", None),
                                            ("  fixed it \n", "fixed it")])
def test_a_note_is_trimmed_and_an_empty_one_is_no_note(note, expected):
    assert svc.clean_note(note) == expected
