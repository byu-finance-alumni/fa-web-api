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
from app.core.capabilities import DEFAULT_GRANTS
from app.main import app


@pytest.fixture(autouse=True)
def _default_permission_config():
    app.dependency_overrides[get_permission_config] = lambda: dict(DEFAULT_GRANTS)
    yield
    app.dependency_overrides.pop(get_permission_config, None)
