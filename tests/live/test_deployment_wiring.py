"""The deployment wiring: nginx, the /api proxy, asset caching and container health.

This is the one layer no other suite in either project can reach. The hermetic backend suite
drives a TestClient, so nginx is not in the picture at all; the SPA's Vitest and Playwright
specs intercept at the browser boundary, so the dev server is used rather than the
container's. Everything below only exists in a built image behind a reverse proxy — and two
separate defects reached a browser through that gap:

  * The SPA container reported `unhealthy` forever while serving every request correctly,
    because its healthcheck probed the hostname `localhost` — which resolves to `::1` first —
    while nginx's `listen 80;` binds IPv4 only.
  * `GET /users/profile` 500'd against the real database for the seeded superadmin, which is
    the general shape of the problem: MySQL and SQLite disagree, and only one of them ships.

Each test here is written so it would have FAILED before the corresponding fix. They assert
observable behaviour over an HTTP socket, exactly as a browser sees it, because that is the
only vantage point from which the proxy exists.

Static-asset checks go to `live_root` (the SPA origin, port 8080) with a bare httpx client
rather than through `api_base`: the point is what nginx does with a path, and `api_base` is
specifically the part of the origin that gets proxied away. Pointed at the API's own origin
(an API-only stack, `docker compose up db api`) there is no nginx to check, so the `spa`
fixture skips; the container-health checks skip on their own when a container is absent.
"""
import json
import re
import shutil
import subprocess
import uuid
from collections import deque

import httpx
import pytest

from conftest import assert_envelope  # tests/live/conftest.py — see its module docstring

# Vite emits every hashed bundle under /assets/. This finds them inside index.html and inside
# each chunk's own import map, which is how a lazy-loaded route chunk is reachable at all:
# nothing links those from the HTML, only their parent chunk names them.
_ASSET_REF = re.compile(r"(?:/)?assets/[A-Za-z0-9._\-]+\.(?:mjs|js|css)")

# Compose names the containers `<project>-<service>-<n>`; the project name comes from
# docker-compose.yml's `name: ${COMPOSE_PROJECT_NAME:-base-project}`.
_CONTAINERS = ("base-project-api-1", "base-project-app-1", "base-project-db-1")


def _essence(content_type: str | None) -> str:
    """`text/plain; charset=utf-8` -> `text/plain`. Parameters do not change the type."""
    return (content_type or "").split(";")[0].strip().lower()


def _cache_control(response: httpx.Response) -> str:
    """nginx emits `expires` and `add_header Cache-Control` as two separate header lines.

    Both are real and a browser honours the union, so join them rather than reading whichever
    one httpx hands back first.
    """
    return ", ".join(response.headers.get_list("cache-control")).lower()


@pytest.fixture(scope="session")
def spa(live_root: str) -> httpx.Client:
    """A plain browser-shaped client against the SPA origin — no bearer token, no /api base.

    Skips when `live_root` is the API's own origin, recognised by `GET /` answering JSON:
    the API answers `/` with its envelope (a 401, since `/` is not a public route), while
    nginx serves `/` itself as HTML — the SPA shell, or its own 403/404 page if the build or
    `root` is wrong — so a broken build still fails the tests below rather than skipping
    them. The one nginx fault that would look like the API origin, a `location /` proxied to
    the API, cannot skip the nightly run: that job asserts the SPA shell on `/` before it
    starts this suite.
    """
    with httpx.Client(base_url=live_root, timeout=30, follow_redirects=False) as http:
        if _essence(http.get("/").headers.get("content-type")) == "application/json":
            pytest.skip(
                f"{live_root} is the API's own origin; the nginx wiring checks need the SPA "
                "origin (LIVE_API_URL=http://localhost:8080)"
            )
        yield http


@pytest.fixture(scope="session")
def built_assets(spa: httpx.Client) -> list[str]:
    """Every /assets/ path reachable from index.html, discovered rather than hard-coded.

    Vite puts a content hash in each filename, so `UserProfile-CHFwMXne.js` is a different
    string after every build. A test that pinned it would go green by 404ing on the next
    `make up`, which is precisely the failure this file exists to catch. Crawl instead:
    index.html names the entry chunk and the entry chunk names its lazy route children.

    `.mjs` files are found but never crawled *into*: nothing in this build imports out of
    one, and regexing them would only cost time.
    """
    index = spa.get("/")
    assert index.status_code == 200, f"SPA origin did not serve an index: {index.status_code}"

    found: set[str] = set()
    visited: set[str] = set()
    queue: deque[str] = deque()

    def absorb(text: str) -> None:
        for ref in _ASSET_REF.findall(text):
            path = "/" + ref.lstrip("/")
            found.add(path)
            if path.endswith(".js") and path not in visited:
                queue.append(path)

    absorb(index.text)
    # A bound, so a pathological build cannot turn this fixture into an infinite crawl.
    for _ in range(80):
        if not queue:
            break
        path = queue.popleft()
        if path in visited:
            continue
        visited.add(path)
        chunk = spa.get(path)
        if chunk.status_code == 200:
            absorb(chunk.text)

    assert found, "crawled index.html and found no /assets/ references at all"
    return sorted(found)


def _docker_inspect(container: str, template: str) -> str:
    """`docker inspect -f <template> <container>`, or skip if docker is not usable here."""
    if not shutil.which("docker"):
        pytest.skip("docker is not on PATH; container-health checks need it")
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", template, container],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env dependent
        pytest.skip(f"could not run docker inspect: {exc}")
    if result.returncode != 0:
        pytest.skip(
            f"docker inspect {container} failed (not a compose stack, or no daemon access): "
            f"{result.stderr.strip()[:200]}"
        )
    return result.stdout.strip()


# --------------------------------------------------------------------------------------
# The SPA is actually served
# --------------------------------------------------------------------------------------


def test_the_spa_index_is_served_as_html(spa: httpx.Client):
    """The most basic thing the container is for, and the cheapest way to catch a bad build.

    A Dockerfile that copied the wrong directory, or an nginx `root` pointing somewhere empty,
    produces nginx's own 403/404 page — still HTML, still from nginx, and easy to mistake for
    a working deploy in a smoke check that only looks at the status code. So assert the app's
    own markup is present, not merely that something answered.
    """
    response = spa.get("/")

    assert response.status_code == 200
    assert _essence(response.headers.get("content-type")) == "text/html"
    assert '<div id="root"></div>' in response.text, "served HTML is not the SPA shell"
    assert "<title>Base Project</title>" in response.text


# --------------------------------------------------------------------------------------
# The /api prefix strip
# --------------------------------------------------------------------------------------


def test_health_through_the_proxy_needs_the_api_prefix_twice(spa: httpx.Client):
    """`/api/api/v1/health/ready` is correct and is not a typo. This is why.

    nginx's `location /api/ { proxy_pass http://base_project_api/; }` strips the leading
    `/api` before the backend sees the path — the trailing slash on proxy_pass is what does
    it, and it mirrors the rewrite in `vite.config.ts` so one bundle works in both places.
    Meanwhile the health router is the single router that carries its own `/api/v1` prefix;
    every other router (`/user`, `/users`, `/roles`, `/permissions`…) is mounted bare.

    So the browser-side path is the proxy's prefix plus the router's prefix: `/api` + `/api/v1
    /health/ready`. The doubling looks like a mistake in every log line it appears in, which
    is exactly why it deserves a test that states out loud that it is intended.
    """
    response = spa.get("/api/api/v1/health/ready")

    data = assert_envelope(response)
    assert data == {"database": "ok"}


def test_a_single_api_prefix_does_not_reach_the_health_router(spa: httpx.Client):
    """The intuitive path is the wrong one, and it fails in a misleading way. Pin that.

    `/api/v1/health/ready` — which is what the backend's own origin answers on — has its `/api`
    eaten by the proxy and arrives at the backend as `/v1/health/ready`. That is not a route,
    and it is not in `UNAUTHENTICATED_ROUTES` (an exact-match list plus the `/api/v1/health`
    prefix), so `JWTAuthMiddleware` rejects it before routing and the caller gets 401 rather
    than 404. Anyone debugging a proxy misconfiguration therefore sees "Missing or invalid
    token" and goes looking at auth.

    The load-bearing assertion is that it reached the *API* at all (a JSON envelope, not
    nginx's HTML), which is what proves the strip happened rather than the path falling
    through to index.html.
    """
    response = spa.get("/api/v1/health/ready")

    assert _essence(response.headers.get("content-type")) == "application/json", (
        "single-prefixed health did not reach the API — the /api location may have stopped "
        "matching, in which case every SPA call is now served index.html with status 200"
    )
    assert response.status_code == 401
    assert response.json()["status_code"] == 401


def test_the_proxy_strips_api_on_an_ordinary_route(spa: httpx.Client, unique: str):
    """A real endpoint, not just health — health is special-cased by its own prefix.

    `/api/user/login` must arrive at `/user/login`. Getting the trailing slash on `proxy_pass`
    wrong is completely silent: nginx keeps returning 200s from index.html and every API call
    in the SPA fails to parse JSON, which reads in the browser as a broken frontend rather
    than a broken proxy. Asserting the envelope proves FastAPI answered.

    Bad credentials are used on purpose: a 401 from the login controller is proof the request
    was routed *and* handled, and it creates nothing that needs cleaning up.
    """
    response = spa.post(
        "/api/user/login",
        json={"username": f"nobody-{unique}@base-project.invalid",
              "password": "not-the-password"},
    )

    assert _essence(response.headers.get("content-type")) == "application/json"
    assert response.status_code == 401
    body = response.json()
    assert body["status"] == "error"
    assert body["status_code"] == 401


def test_paths_outside_api_are_never_proxied(spa: httpx.Client):
    """Only `/api/` is proxied; everything else belongs to the SPA.

    If the location block were widened (say to `/` or to a bare `/api` without the trailing
    slash), backend routes would start answering on the SPA's own paths and a client-side
    route named after one of them would break. `POST /user/login` with no `/api` must be
    handled by nginx's static file serving, never by FastAPI.
    """
    response = spa.post("/user/login", json={"username": "x", "password": "y"})

    assert _essence(response.headers.get("content-type")) != "application/json", (
        "an un-prefixed backend path reached the API — the proxy location is too broad"
    )


# --------------------------------------------------------------------------------------
# Client-side routing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/activity", "/some/deep/route", "/users/42/edit", "/profile"])
def test_unknown_paths_fall_through_to_the_spa(spa: httpx.Client, path: str):
    """React Router owns every path that is not a file and not `/api/`.

    Without `try_files $uri $uri/ /index.html`, a hard refresh or a pasted deep link returns
    nginx's 404 page and the app appears to have lost half its screens — while navigating to
    the same route from inside the app works perfectly, because that never touches the server.
    That asymmetry is what makes the bug survive testing.
    """
    response = spa.get(path)

    assert response.status_code == 200, f"{path} should be handled by the SPA, not 404'd"
    assert _essence(response.headers.get("content-type")) == "text/html"
    assert '<div id="root"></div>' in response.text


def test_a_missing_hashed_asset_is_a_404_not_the_spa(spa: httpx.Client):
    """The fall-through must stop at `/assets/`, and it is `try_files ... =404` that stops it.

    If a missing chunk were served index.html with status 200, the browser would parse HTML as
    JavaScript and report a syntax error at `<`, from a URL that returns 200 — one of the most
    confusing failures a stale deploy can produce. `=404` makes a missing chunk say so.
    """
    response = spa.get(f"/assets/does-not-exist-{uuid.uuid4().hex[:8]}.js")

    assert response.status_code == 404
    assert '<div id="root"></div>' not in response.text


# --------------------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------------------


def test_hashed_assets_are_immutable(spa: httpx.Client, built_assets: list[str]):
    """A content hash in the filename is a promise; `immutable` is how it is collected."""
    sample = next(p for p in built_assets if p.endswith((".js", ".css")))

    cache_control = _cache_control(spa.get(sample))

    assert "immutable" in cache_control, f"{sample} -> {cache_control!r}"
    assert "max-age=31536000" in cache_control, f"{sample} -> {cache_control!r}"


def test_index_html_is_never_cached(spa: httpx.Client):
    """index.html is the only unhashed file, so it is the only one a cache can poison.

    It names the hashed bundles. Cached, a browser keeps asking for chunks the deploy deleted
    and the app is broken for exactly as long as the cache lives — with no way for the server
    to intervene.
    """
    cache_control = _cache_control(spa.get("/"))

    assert "no-cache" in cache_control or "no-store" in cache_control, (
        f"index.html -> {cache_control!r}"
    )
    assert "immutable" not in cache_control


# --------------------------------------------------------------------------------------
# Container health
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("container", _CONTAINERS)
def test_every_container_reports_healthy(live_root: str, container: str):
    """`docker ps` is the first thing anyone looks at, so it has to be trustworthy.

    The SPA container sat at `unhealthy` indefinitely while serving every request correctly.
    That is worse than an outage in one specific way: it trains the team to ignore the health
    column, and the next container that goes unhealthy for a real reason is ignored too. It
    also blocks any orchestrator that gates rollout on health.

    `live_root` is requested but unused: these container names describe the stack
    LIVE_API_URL points at, so this must skip with the rest of the suite rather than
    quietly inspecting whatever unrelated containers happen to be on the developer's daemon.
    """
    status = _docker_inspect(container, "{{.State.Health.Status}}")

    assert status == "healthy", (
        f"{container} reports {status!r}. Run `docker inspect -f "
        f"'{{{{json .State.Health}}}}' {container}` for the failing probe's output."
    )


def test_the_spa_healthcheck_probes_an_address_nginx_actually_binds(live_root: str):
    """The specific reason the SPA container was unhealthy: IPv6, not nginx.

    The probe was `wget --spider http://localhost/`. In the nginx image `localhost` resolves to
    `::1` before `127.0.0.1`, and the config says `listen 80;` — IPv4 only. So the probe got
    ECONNREFUSED on every attempt while the identical request over IPv4 succeeded, from the
    same container, the whole time.

    Reading the probe itself rather than only its outcome is deliberate. The status assertion
    above passes today; it would also pass tomorrow on a base image whose resolver happened to
    order A before AAAA, and then fail again on the one after that. The defect is naming a
    hostname at all when the listen directive commits to one family — so that is what is
    asserted.
    """
    probe = _docker_inspect("base-project-app-1", "{{json .Config.Healthcheck.Test}}")
    command = " ".join(json.loads(probe))

    assert not re.search(r"https?://localhost\b", command), (
        "the SPA healthcheck probes the hostname `localhost`, which resolves to ::1 first "
        f"while nginx binds IPv4 only: {command!r}"
    )
    assert "127.0.0.1" in command or "[::1]" in command, (
        "the SPA healthcheck should name a loopback address literally, so which family it "
        f"reaches is not left to the image's resolver: {command!r}"
    )
