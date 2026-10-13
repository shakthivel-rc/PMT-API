"""The API process must not load the model client.

docs/design/03-ai-strategy.md rests on one property: the API process enqueues AI work and
never performs it, so it never holds `ANTHROPIC_API_KEY` and a slow model can never occupy
an API worker. The day a route imports `ai.client` "just to call it quickly", that property
is gone and nothing else would notice — the request would still succeed, only slower, and
only under load.

A fresh interpreter, because this test process has long since imported everything.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent.parent


def test_importing_the_api_does_not_import_the_model_client():
    probe = (
        "import sys, main\n"
        "loaded = sorted(m for m in ('anthropic', 'ai.client', 'workers.ai_worker')"
        " if m in sys.modules)\n"
        "print(','.join(loaded))\n"
    )
    env = {**os.environ, "DATABASE_URL": "sqlite://", "ENVIRONMENT": "test"}
    env.setdefault("SECRET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env,
                            capture_output=True, text=True, check=True, timeout=120)
    assert result.stdout.strip() == "", (
        f"the API process loaded {result.stdout.strip()} — model calls belong in the worker"
    )
