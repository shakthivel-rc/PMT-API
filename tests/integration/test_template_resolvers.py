"""The two resolvers of a workflow template reference must agree.

`services/workspace.template_definition` (space creation, the governance default) and
`services/workflow.resolve_template_definition` (the workflow routes) were written by two
lanes at once on 2026-10-05 and are kept apart on purpose — space creation does not import
the workflow editor's module — so nothing but this test stops them drifting. A drift would
show as a template that one screen offers and the other refuses: the editor lists a
template that creating a space from then answers 422 for, or the reverse.
"""
import pytest

from services import workflow as wf
from services import workspace as workspace_svc
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration


def test_both_resolvers_name_the_same_templates(client, db):
    """Every reference either both resolve — to the same stages — or neither does: the
    built-ins with and without their prefix, this workspace's template, another
    workspace's, a deleted one and nonsense."""
    headers, ws, space, _list = pm.setup_board(client, db)
    other_headers, other_ws, other_space, _ = pm.setup_board(client, db, "bob", "bravo", "BRA")
    ours = pm.ok(client.post(f"/workspaces/{ws}/workflow-templates", headers=headers,
                             json={"name": "Ours", "from_space_id": space["id"]}), 201)
    gone = pm.ok(client.post(f"/workspaces/{ws}/workflow-templates", headers=headers,
                             json={"name": "Gone", "from_space_id": space["id"]}), 201)
    pm.ok(client.delete(f"/workspaces/{ws}/workflow-templates/{gone['id']}", headers=headers))
    theirs = pm.ok(client.post(f"/workspaces/{other_ws}/workflow-templates",
                               headers=other_headers,
                               json={"name": "Theirs", "from_space_id": other_space["id"]}),
                   201)

    refs = ["default", "delivery", "support", "builtin:default", "builtin:delivery",
            "builtin:support", "builtin:nonsense", "builtin:", "", "nonsense", ours["id"],
            gone["id"], theirs["id"]]
    for ref in refs:
        stored = workspace_svc.template_definition(db, ws, ref)
        resolved = wf.resolve_template_definition(db, ws, ref)
        assert (stored is None) == (resolved is None), ref
        if stored is not None and resolved is not None:
            prepared, problem = workspace_svc.prepare_definition(stored)
            assert problem is None, (ref, problem)
            assert prepared is not None
            assert [s["name"] for s in prepared["stages"]] == \
                [s["name"] for s in resolved["stages"]], ref
