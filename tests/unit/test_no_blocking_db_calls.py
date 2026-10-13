"""No synchronous session call may sit in the body of an `async def`.

Every request this process serves shares one event loop. A plain `def` handler runs in
Starlette's threadpool, so a blocking query there costs one thread; the same query written
in the body of an `async def` runs *on the loop*, and every other request in flight waits
for it — one slow screen stalls every user at once, for as long as its queries take.

This is the mechanism that keeps the rule true. It is an AST walk rather than a runtime
check because the cost is what matters and the cost is not observable from a passing test:
blocking the loop does not fail anything, it just makes everything slower at the same time,
which is the hardest kind of regression to attribute.

**What it can and cannot see.** It catches `db.query(...)`, `db.commit()` and friends
written directly in an `async def`. It does NOT follow calls — an async function that calls
a synchronous service helper which queries is invisible to it. Those are found by
reading, and when one is found the fix is the same: move the call into a nested `def` and
`to_thread` it, which also brings it inside this check's reach.

The escape hatch is therefore deliberate and has a shape: put the work in a nested plain
`def` and hand that to `asyncio.to_thread`. A lambda would work at runtime and is rejected
here, because a rule that cannot be checked is not a rule.

**And the hatch must actually be taken.** An earlier version of this check treated *any*
nested `def` as offloaded, matching on shape alone — so deleting the `await
asyncio.to_thread(...)` line and calling `_load()` directly left the gate green while the
queries went straight back onto the event loop. The single edit that breaks the pattern was
the one edit the gate could not see. It now records which nested functions are handed to
`asyncio.to_thread` / `run_in_executor` and exempts only those.
"""
import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

#: Session methods that go to the database. `db.add` and `db.delete` only stage work, but
#: they can trigger an autoflush, and a rule with exceptions in it is a rule people get
#: wrong.
SESSION_METHODS = frozenset({
    "query", "execute", "commit", "rollback", "refresh", "flush", "merge",
    "add", "add_all", "delete", "scalar", "scalars", "get",
})

#: The names a `Session` is bound to in this codebase.
SESSION_NAMES = frozenset({"db", "session"})

#: Directories whose `async def`s are not request handlers.
SEARCH_DIRS = ("controllers", "routes", "services", "core", "dependencies")

#: Files exempted, each with the reason. None today; an entry must name a file that exists
#: and say why a blocking call cannot starve anything there.
EXEMPT: dict[str, str] = {}


#: Calls that move work to another thread. `to_thread` is the idiom this codebase uses;
#: `run_in_executor` is the older spelling and is accepted so a correct fix is never
#: reported as a violation.
THREAD_HANDOFFS = frozenset({"to_thread", "run_in_executor"})


def _names_sent_to_a_thread(node: ast.AST) -> set[str]:
    """Names passed as the callable argument of a thread hand-off, anywhere inside `node`.

    Deliberately name-based rather than binding-aware: it accepts
    `asyncio.to_thread(_load)` and `loop.run_in_executor(None, _load)`, and it does not try
    to follow a function passed through a variable. The failure direction is a false
    *positive* — an unusual but correct hand-off gets reported and the author either
    rewrites it in the house idiom or adds a reasoned exemption. That is the right way for
    this to be wrong.
    """
    names: set[str] = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        attr = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None)
        if attr not in THREAD_HANDOFFS:
            continue
        for argument in call.args:
            if isinstance(argument, ast.Name):
                names.add(argument.id)
    return names


def _offences(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue

        # Which nested functions are actually handed to a thread. Shape is not enough:
        # a nested `def` that nobody offloads runs on the event loop like any other call.
        offloaded_names = _names_sent_to_a_thread(node)

        offloaded = {
            id(inner)
            for child in ast.walk(node)
            if isinstance(child, ast.FunctionDef) and child.name in offloaded_names
            for inner in ast.walk(child)
        }

        for call in ast.walk(node):
            if id(call) in offloaded or not isinstance(call, ast.Call):
                continue
            func = call.func
            if not isinstance(func, ast.Attribute) or func.attr not in SESSION_METHODS:
                continue
            target = func.value
            if isinstance(target, ast.Name) and target.id in SESSION_NAMES:
                found.append(
                    f"{path.relative_to(ROOT)}:{call.lineno}  "
                    f"{node.name}() calls {target.id}.{func.attr}()"
                )
    return found


def _python_files():
    for directory in SEARCH_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path
    yield ROOT / "main.py"


class TestNoBlockingDbCallsOnTheEventLoop:
    def test_no_async_function_touches_a_session_directly(self):
        offences: list[str] = []
        for path in _python_files():
            if path.name in EXEMPT:
                continue
            offences.extend(_offences(path))

        assert not offences, (
            "Synchronous database calls in the body of an `async def`:\n  "
            + "\n  ".join(offences)
            + "\n\nMove the block into a nested `def` and `await asyncio.to_thread(...)` "
            "it.\nIf the handler does no `await` of its own, making it a plain `def` is "
            "simpler — Starlette runs those in a threadpool."
        )

    def test_every_exemption_carries_a_reason(self):
        for name, why in EXEMPT.items():
            assert (ROOT / name).exists(), f"{name} is exempted and does not exist"
            assert len(why) > 60, f"{name}: write down why it is exempt"

    def test_the_check_actually_detects_the_pattern(self):
        """A gate that has never failed has never been tested."""
        import tempfile

        source = (
            "async def handler(db):\n"
            "    return db.query(Thing).all()\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
            handle.write(source)
            probe = pathlib.Path(handle.name)
        try:
            # `relative_to(ROOT)` is what the reporter does; give it a path under ROOT.
            target = ROOT / "_blocking_probe_.py"
            target.write_text(source)
            assert _offences(target), "the detector missed a plain violation"
        finally:
            target.unlink(missing_ok=True)
            probe.unlink(missing_ok=True)

    def test_the_sanctioned_escape_hatch_is_not_flagged(self):
        source = (
            "import asyncio\n"
            "async def handler(db):\n"
            "    def _load():\n"
            "        return db.query(Thing).all()\n"
            "    return await asyncio.to_thread(_load)\n"
        )
        target = ROOT / "_blocking_probe_ok_.py"
        target.write_text(source)
        try:
            assert not _offences(target)
        finally:
            target.unlink(missing_ok=True)

    def test_a_nested_def_that_is_never_offloaded_is_still_a_violation(self):
        """The single edit that breaks the pattern, which shape-matching cannot see.

        Delete the `await asyncio.to_thread(...)` and call `_load()` directly: the nested
        `def` is still there, and every query is back on the event loop.
        """
        source = (
            "async def handler(db):\n"
            "    def _load():\n"
            "        return db.query(Thing).all()\n"
            "    return _load()\n"
        )
        target = ROOT / "_blocking_probe_bad_.py"
        target.write_text(source)
        try:
            assert _offences(target), "a nested def nobody offloads is not an escape hatch"
        finally:
            target.unlink(missing_ok=True)

    def test_run_in_executor_is_accepted_too(self):
        source = (
            "async def handler(db, loop):\n"
            "    def _load():\n"
            "        return db.query(Thing).all()\n"
            "    return await loop.run_in_executor(None, _load)\n"
        )
        target = ROOT / "_blocking_probe_exec_.py"
        target.write_text(source)
        try:
            assert not _offences(target)
        finally:
            target.unlink(missing_ok=True)
