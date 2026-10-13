"""The reports settings document, `workspaces.settings["reports"]`, read with defaults.

The same shape as `services/time_tracking.settings_for`: a stored document that no longer
validates falls back to the defaults rather than failing every report in the tenant.
"""
from sqlalchemy.orm import Session

from models.workspace import Workspace
from schemas.reports import ReportsSettings

SETTINGS_KEY = "reports"


def settings_for(workspace: Workspace | None) -> ReportsSettings:
    document = (workspace.settings or {}) if workspace is not None else {}
    stored = document.get(SETTINGS_KEY) if isinstance(document, dict) else None
    try:
        return ReportsSettings.model_validate(stored or {})
    except ValueError:
        return ReportsSettings()


def load_settings(db: Session, workspace_id: str) -> ReportsSettings:
    return settings_for(db.query(Workspace).filter(Workspace.id == workspace_id).first())


def save_settings(workspace: Workspace, settings: ReportsSettings) -> None:
    """Store `settings`, keeping every other feature's key. A new dict, because a JSON
    column only notices reassignment (services/time_tracking.save_settings)."""
    document = dict(workspace.settings or {}) if isinstance(workspace.settings, dict) else {}
    document[SETTINGS_KEY] = settings.model_dump()
    workspace.settings = document
