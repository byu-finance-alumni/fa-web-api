"""Shared test fixtures.

The capability guards resolve the editable permission config per request via the
``get_permission_config`` dependency (#164). The route tests across this suite
use hand-rolled in-memory fake sessions that return canned rows for ANY query —
which would collide with the config read. So by default we override
``get_permission_config`` to the historical defaults (``DEFAULT_GRANTS``, which
reproduces the pre-#164 hardcoded guards), decoupling authorization from each
test's fake session. Tests that exercise the editor still override it explicitly
with their own config, and ``load_grants`` (the real DB read) is unit-tested
directly in tests/test_permissions.py.
"""

import pytest

from app.api.dependencies.auth import get_permission_config
from app.core import rate_limit
from app.core.capabilities import DEFAULT_GRANTS
from app.main import app


@pytest.fixture(autouse=True)
def _default_permission_config():
    app.dependency_overrides[get_permission_config] = lambda: dict(DEFAULT_GRANTS)
    yield
    app.dependency_overrides.pop(get_permission_config, None)


@pytest.fixture(autouse=True)
def _fresh_rate_limit_windows():
    """Start every test with empty rate-limit counters.

    ``app/core/rate_limit.py`` keeps its fixed-window state in a module-level
    dict, so without this the budget a test spends leaks into every later test in
    the session. That matters most for the unauthenticated limiters (#423): they
    key on the CLIENT IP, and every TestClient request in the whole suite shares
    the same one, so the login routes' budgets would otherwise be consumed
    collectively and a test could fail depending only on what ran before it.
    """
    rate_limit.reset()
    yield
