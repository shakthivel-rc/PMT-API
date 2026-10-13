"""The inbound limiter, and the middleware that applies it.

The limiter is disabled for the rest of the suite (see `tests/conftest.py` for why), so
this file is the only place its behaviour is pinned. It builds its own app rather than
using the shared one, which also means the numbers here are the test's own and cannot
drift with a deployment's `.env`.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import get_settings
from core.middleware import CREDENTIAL_PATHS, RateLimitMiddleware, SecurityHeadersMiddleware
from core.ratelimit import SlidingWindowLimiter
from routes.auth_middleware import UNAUTHENTICATED_ROUTES

pytestmark = pytest.mark.unit


class TestSlidingWindowLimiter:
    def test_allows_up_to_the_limit_then_refuses(self):
        clock = [0.0]
        limiter = SlidingWindowLimiter(3, 60.0, clock=lambda: clock[0])
        assert [limiter.check("a") for _ in range(3)] == [None, None, None]
        assert limiter.check("a") == pytest.approx(60.0)

    def test_the_window_slides_rather_than_resetting(self):
        """The reason this is not a fixed window.

        Three hits at t=0 and three more at t=59.9 must NOT both be allowed just because a
        minute boundary passed between them — that is the 2x burst a login limiter exists
        to prevent. At t=60.1 the first hit has aged out and exactly one slot is free.
        """
        clock = [0.0]
        limiter = SlidingWindowLimiter(3, 60.0, clock=lambda: clock[0])
        # Staggered, because that is what real traffic looks like and because three hits
        # at the same instant would all age out together, which tests nothing about
        # sliding.
        for at in (0.0, 1.0, 2.0):
            clock[0] = at
            assert limiter.check("a") is None

        clock[0] = 59.9
        assert limiter.check("a") is not None, "a fixed window would have reset by now"

        # Only the t=0 hit has aged out, so exactly one slot is free — not three.
        clock[0] = 60.5
        assert limiter.check("a") is None
        assert limiter.check("a") is not None

    def test_a_refused_hit_is_not_recorded(self):
        """Otherwise retrying extends the block, which makes this a lockout, not a limit."""
        clock = [0.0]
        limiter = SlidingWindowLimiter(2, 60.0, clock=lambda: clock[0])
        limiter.check("a")
        limiter.check("a")
        for _ in range(10):
            limiter.check("a")  # refused, and must not push the window forward

        clock[0] = 61.0
        assert limiter.check("a") is None

    def test_keys_are_independent(self):
        limiter = SlidingWindowLimiter(1, 60.0, clock=lambda: 0.0)
        assert limiter.check("a") is None
        assert limiter.check("b") is None
        assert limiter.check("a") is not None

    def test_zero_disables_the_limit(self):
        limiter = SlidingWindowLimiter(0, 60.0, clock=lambda: 0.0)
        assert all(limiter.check("a") is None for _ in range(1000))

    def test_the_key_space_is_bounded(self):
        """Keys are client addresses, so an attacker chooses them. Memory must not grow."""
        limiter = SlidingWindowLimiter(5, 60.0, max_keys=10, clock=lambda: 0.0)
        for n in range(500):
            limiter.check(f"key-{n}")
        assert len(limiter._hits) <= 10

    def test_eviction_keeps_a_key_that_is_still_being_used(self):
        """LRU, so the client currently being limited is not the one forgotten.

        `busy` is re-checked between the other keys, which is what a client actually
        hitting its limit looks like. A key that goes quiet for longer than the key space
        can hold IS forgotten and does get fresh budget — that is the documented cost of
        bounding the memory, and the bound is not optional when the keys are addresses
        chosen by the caller.
        """
        limiter = SlidingWindowLimiter(2, 60.0, max_keys=3, clock=lambda: 0.0)
        limiter.check("busy")
        limiter.check("busy")
        for n in range(10):
            limiter.check(f"other-{n}")
            assert limiter.check("busy") is not None, "the busy key was evicted mid-attack"


def _app(**overrides) -> FastAPI:
    """A minimal app carrying the middleware and three representative routes."""
    from dataclasses import replace

    fields = {
        "rate_limit_enabled": True,
        "rate_limit_credential_per_minute": 3,
        "rate_limit_general_per_minute": 5,
        "trusted_proxy_ips": (),
    }
    fields.update(overrides)
    settings = replace(get_settings(), **fields)
    app = FastAPI()

    @app.post("/user/login")
    def login():
        return {"ok": True}

    @app.get("/api/v1/users")
    def users():
        return {"ok": True}

    @app.get("/api/v1/health/ready")
    def ready():
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    return app


def _client(**overrides) -> TestClient:
    """A TestClient whose peer address is the loopback.

    TestClient's default peer is the *hostname* "testclient", which is not an IP address
    at all — so every trusted-proxy assertion would silently fall through to the
    peer-address path and pass for the wrong reason. Naming the peer is what makes
    "trust 127.0.0.0/8" mean something in these tests.
    """
    return TestClient(_app(**overrides), client=("127.0.0.1", 50000))


class TestRateLimitMiddleware:
    def test_credential_paths_get_the_small_budget(self):
        client = TestClient(_app())
        codes = [client.post("/user/login").status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]

    def test_the_refusal_says_when_to_come_back(self):
        client = TestClient(_app())
        for _ in range(3):
            client.post("/user/login")
        refused = client.post("/user/login")
        assert refused.status_code == 429
        assert int(refused.headers["Retry-After"]) >= 1
        body = refused.json()
        assert body["status"] == "error" and body["status_code"] == 429

    def test_the_two_budgets_do_not_share_a_bucket(self):
        """Browsing the app must not use up somebody's ability to sign in."""
        client = TestClient(_app())
        for _ in range(5):
            assert client.get("/api/v1/users").status_code == 200
        assert client.post("/user/login").status_code == 200

    def test_health_is_never_limited(self):
        """A liveness probe that trips a rate limit takes the service out of rotation."""
        client = TestClient(_app())
        codes = {client.get("/api/v1/health/ready").status_code for _ in range(50)}
        assert codes == {200}

    def test_disabled_means_disabled(self):
        client = TestClient(_app(rate_limit_enabled=False))
        codes = {client.post("/user/login").status_code for _ in range(50)}
        assert codes == {200}

    def test_a_forwarded_header_from_an_untrusted_peer_is_ignored(self):
        """The default. Otherwise every request can mint itself a fresh bucket."""
        client = TestClient(_app())
        for n in range(3):
            client.post("/user/login", headers={"X-Forwarded-For": f"10.0.0.{n}"})
        spoofed = client.post("/user/login", headers={"X-Forwarded-For": "10.0.0.99"})
        assert spoofed.status_code == 429

    def test_a_forwarded_header_from_a_trusted_peer_identifies_the_client(self):
        """TestClient's peer is 127.0.0.1, so trusting the loopback makes it a proxy."""
        client = _client(trusted_proxy_ips=("127.0.0.0/8",))
        for _ in range(3):
            client.post("/user/login", headers={"X-Forwarded-For": "10.0.0.1"})
        assert client.post("/user/login",
                           headers={"X-Forwarded-For": "10.0.0.1"}).status_code == 429
        assert client.post("/user/login",
                           headers={"X-Forwarded-For": "10.0.0.2"}).status_code == 200

    def test_the_rightmost_untrusted_hop_wins(self):
        """nginx APPENDS the peer to whatever the client sent, so the left-most entry is
        attacker-supplied and the right-most non-proxy entry is what nginx actually saw.
        Taking the left-most would let one client mint a bucket per request through a
        trusted proxy — the same hole the allow-list exists to close."""
        client = _client(trusted_proxy_ips=("127.0.0.0/8",))
        for n in range(3):
            # A different forged left-most entry each time; the real client is constant.
            client.post("/user/login",
                        headers={"X-Forwarded-For": f"9.9.9.{n}, 10.0.0.7"})
        blocked = client.post("/user/login",
                              headers={"X-Forwarded-For": "9.9.9.250, 10.0.0.7"})
        assert blocked.status_code == 429, "the forged left-most entry minted a new bucket"

    def test_a_chain_of_trusted_proxies_is_walked_through(self):
        client = _client(trusted_proxy_ips=("127.0.0.0/8", "172.18.0.0/16"))
        for _ in range(3):
            client.post("/user/login",
                        headers={"X-Forwarded-For": "203.0.113.5, 172.18.0.9"})
        assert client.post(
            "/user/login",
            headers={"X-Forwarded-For": "203.0.113.5, 172.18.0.9"}).status_code == 429
        assert client.post(
            "/user/login",
            headers={"X-Forwarded-For": "203.0.113.6, 172.18.0.9"}).status_code == 200

    def test_an_unparseable_entry_is_ignored_rather_than_fatal(self):
        """A typo in TRUSTED_PROXY_IPS must not stop the service booting, and the safe
        direction on a bad entry is to trust one fewer proxy."""
        client = _client(trusted_proxy_ips=("not-an-ip", "127.0.0.0/8"))
        for _ in range(3):
            client.post("/user/login", headers={"X-Forwarded-For": "10.0.0.1"})
        assert client.post("/user/login",
                           headers={"X-Forwarded-For": "10.0.0.2"}).status_code == 200


class TestTheTwoPathListsHaveNotDrifted:
    def test_every_credential_path_is_a_real_unauthenticated_route(self):
        """`CREDENTIAL_PATHS` naming a path that no longer exists limits nothing.

        This is the failure mode a hand-maintained path list always has. The two lists here
        cannot be merged — one decides authentication and the other decides cost — so they
        are checked against each other instead.
        """
        unknown = CREDENTIAL_PATHS - set(UNAUTHENTICATED_ROUTES)
        assert not unknown, f"CREDENTIAL_PATHS names routes that do not exist: {sorted(unknown)}"

    def test_every_unauthenticated_user_route_is_rate_limited(self):
        """The direction that matters: a new open `/user/` endpoint must not be free.

        Adding one and forgetting this list gives an unauthenticated, unlimited endpoint,
        which is exactly the shape of the thing being defended against.
        """
        open_user_routes = {p for p in UNAUTHENTICATED_ROUTES if p.startswith("/user/")}
        assert open_user_routes <= CREDENTIAL_PATHS, (
            f"not rate limited: {sorted(open_user_routes - CREDENTIAL_PATHS)}"
        )
