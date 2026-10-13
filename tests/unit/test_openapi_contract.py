"""The committed contract digest must describe the app that exists.

Two repositories, one product. The SPA hand-maintains every response shape in
`src/types/index.ts`; the API generates them. Nothing connected the two, so a field renamed
on this side compiled on that side, passed both suites, and broke a screen in a browser.

This does not verify the SPA — it cannot, from here. What it does is make an API contract
change *visible*: `docs/openapi-digest.txt` moves in the same commit, a reviewer sees which
operation or schema changed, and "does the SPA need a matching change" becomes a question
somebody is actually asked.

Regenerating is one command (`make openapi`). The gate is that it has to be run
deliberately, not that it is hard.
"""
import difflib
import pathlib

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
DIGEST = ROOT / "docs" / "openapi-digest.txt"


def _generated() -> str:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "openapi_digest", ROOT / "scripts" / "openapi_digest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    from main import app

    return module.build(app.openapi())


class TestContractDigest:
    def test_the_snapshot_exists(self):
        assert DIGEST.exists(), "run `make openapi`"

    def test_the_snapshot_matches_the_app(self):
        committed = DIGEST.read_text()
        generated = _generated()
        if committed == generated:
            return

        diff = "\n".join(
            difflib.unified_diff(
                committed.splitlines(),
                generated.splitlines(),
                fromfile="docs/openapi-digest.txt (committed)",
                tofile="app.openapi() (now)",
                lineterm="",
                n=1,
            )
        )
        pytest.fail(
            "The API contract changed and the digest did not.\n\n"
            + diff
            + "\n\nIf the change is intended, run `make openapi` and commit the result "
            "alongside it.\nThen check whether the SPA's src/types/index.ts needs the same "
            "change."
        )

    def test_every_route_appears_in_it(self):
        """A sanity check on the generator, not on the app: a digest that silently stopped
        emitting operations would match itself forever.

        Through `tests.route_walk`, not `app.routes`: under fastapi 0.141 the flat listing
        yields the included routers rather than their routes, so `documented` came out empty
        and this passed having checked nothing.
        """
        from main import app
        from tests.route_walk import iter_api_routes

        committed = DIGEST.read_text()
        documented = {
            route.path
            for route in iter_api_routes(app.routes)
            if route.path.startswith(("/user", "/users", "/roles", "/permissions",
                                      "/activity-logs", "/api/v1/health"))
        }
        assert documented, "the route walk found nothing to check"
        missing = [path for path in documented if f"{path} " not in committed
                   and f"{path}\n" not in committed]
        assert not missing, f"not in the digest: {sorted(missing)}"
