"""Migration routes: baselines, plans, the register, execute, reconcile, rollback.

docs/design/09-restructuring.md §8. Thin, like every route module (CLAUDE.md §2).
"""
from fastapi import APIRouter

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["Restructure"])
