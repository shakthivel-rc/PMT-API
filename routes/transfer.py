"""Transfer and inventory routes: move issues between lists and spaces; the inventory.

docs/design/09-restructuring.md §7 and §8.1. Thin, like every route module (CLAUDE.md §2).
"""
from fastapi import APIRouter

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["Restructure"])
