"""The governance settings document, read with defaults (docs/design/09-restructuring.md §3.3).

The same shape as `services/reports/settings.py`: a stored document that no longer validates
falls back to the defaults rather than failing every issue create in the tenant.
"""
from sqlalchemy.orm import Session

from models.workspace import Workspace
from schemas.governance import GovernanceSettings

SETTINGS_KEY = "governance"


def settings_for(workspace: Workspace | None) -> GovernanceSettings:
    document = (workspace.settings or {}) if workspace is not None else {}
    stored = document.get(SETTINGS_KEY) if isinstance(document, dict) else None
    try:
        return GovernanceSettings.model_validate(stored or {})
    except ValueError:
        return GovernanceSettings()


def load_settings(db: Session, workspace_id: str) -> GovernanceSettings:
    return settings_for(db.query(Workspace).filter(Workspace.id == workspace_id).first())


def save_settings(workspace: Workspace, settings: GovernanceSettings) -> None:
    """Store `settings`, keeping every other feature's key. A new dict, because a JSON column
    only notices reassignment (services/time_tracking.save_settings)."""
    document = dict(workspace.settings or {}) if isinstance(workspace.settings, dict) else {}
    document[SETTINGS_KEY] = settings.model_dump()
    workspace.settings = document
