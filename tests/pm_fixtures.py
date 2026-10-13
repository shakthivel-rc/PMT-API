"""Builders for the project-management tests: a workspace with a space, a list and issues.

Constructors, not fixtures, for the reason `tests/fixtures.py` gives. Everything goes
through the HTTP API except membership, which is written directly so a test can put a
second account into a workspace in a stated role without an admin round-trip.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from models.workspace import WorkspaceMember
from tests.fixtures import auth, seed_user


def person(db: Session, user_id: str) -> dict[str, str]:
    """A signed-in account with no platform permissions, as auth headers."""
    return auth(seed_user(db, user_id, []))


def join(db: Session, workspace_id: str, user_id: str, role: str) -> None:
    db.add(WorkspaceMember(workspace_id=workspace_id, user_id=user_id, role=role))
    db.commit()


def ok(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()["data"]


def workspace(client, headers, slug: str = "acme") -> str:
    return ok(client.post("/workspaces", json={"name": slug.title(), "slug": slug},
                          headers=headers), 201)["id"]


def space(client, headers, workspace_id: str, key: str = "PAY") -> dict:
    return ok(client.post(f"/workspaces/{workspace_id}/spaces",
                          json={"name": f"{key} space", "key": key}, headers=headers), 201)


def task_list(client, headers, workspace_id: str, space_id: str, name: str = "Backlog") -> dict:
    return ok(client.post(f"/workspaces/{workspace_id}/lists",
                          json={"space_id": space_id, "name": name}, headers=headers), 201)


def issue(client, headers, workspace_id: str, list_id: str, title: str = "Do the thing",
          **fields) -> dict:
    body = {"list_id": list_id, "title": title, **fields}
    return ok(client.post(f"/workspaces/{workspace_id}/issues", json=body, headers=headers),
              201)


def board(client, headers, workspace_id: str, list_id: str) -> dict:
    return ok(client.get(f"/workspaces/{workspace_id}/board", params={"list_id": list_id},
                         headers=headers))


def column_titles(board_payload: dict, status_name: str) -> list[str]:
    for column in board_payload["columns"]:
        if column["status"]["name"] == status_name:
            return [card["title"] for card in column["cards"]]
    raise AssertionError(f"no column called {status_name}")


def status_id(space_payload: dict, name: str) -> str:
    return next(s["id"] for s in space_payload["statuses"] if s["name"] == name)


def setup_board(client, db, user_id: str = "owner-1", slug: str = "acme", key: str = "PAY"):
    """An owner, a workspace, a space with the default workflow, and one list."""
    headers = person(db, user_id)
    workspace_id = workspace(client, headers, slug)
    space_payload = space(client, headers, workspace_id, key)
    list_payload = task_list(client, headers, workspace_id, space_payload["id"])
    return headers, workspace_id, space_payload, list_payload


def log_time(client, headers, workspace_id: str, issue_id: str, seconds: int = 3600,
             work_date: str = "2026-10-01", **fields) -> dict:
    """Log `seconds` on an issue and return the entry. `work_date` is fixed and in the past
    so no test depends on what day it is when the suite runs."""
    body = {"duration_seconds": seconds, "work_date": work_date, **fields}
    return ok(client.post(f"/workspaces/{workspace_id}/issues/{issue_id}/time-entries",
                          json=body, headers=headers), 201)


def issue_time(client, headers, workspace_id: str, issue_id: str) -> dict:
    return ok(client.get(f"/workspaces/{workspace_id}/issues/{issue_id}/time-tracking",
                         headers=headers))
