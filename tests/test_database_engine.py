"""The database engine must never put bound parameters into error text (#595).

A failed statement's exception message embeds the values it was sent unless the
engine is built with ``hide_parameters=True``. Those messages reach the logs
through the unhandled-error handler and the import loops, so without the flag a
single constraint violation can write an alumnus's name, BYU ID or email into a
log line.

``app/core/database.py`` builds the engine at import time down one of THREE
branches (transaction pooler, serverless session pooler, long-lived session
pooler). Each test here re-executes that module under a throwaway name with the
settings and environment forcing one branch, so every branch is proven — not
just whichever one this machine happens to take.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy.ext.asyncio as sa_async

import app.core.config as config_module

_DB_MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "core" / "database.py"

_SESSION_URL = "postgresql+asyncpg://u:p@db.invalid:5432/postgres"
_TXN_URL = "postgresql+asyncpg://u:p@db.invalid:6543/postgres"


def _fake_settings(url: str) -> SimpleNamespace:
    return SimpleNamespace(
        async_database_url=url,
        sql_echo=False,
        environment="production",
        db_pool_size=5,
        db_max_overflow=2,
        db_pool_timeout=10,
        db_pool_recycle=1800,
    )


def _load_database_module(monkeypatch, url: str, *, serverless: bool):
    """Execute app/core/database.py fresh and return (module, captured kwargs)."""
    captured: list[dict] = []
    real_create = sa_async.create_async_engine

    def _recording_create(engine_url, **kwargs):
        captured.append(kwargs)
        return real_create(engine_url, **kwargs)  # builds lazily; never connects

    monkeypatch.setattr(sa_async, "create_async_engine", _recording_create)
    monkeypatch.setattr(config_module, "get_settings", lambda: _fake_settings(url))
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    if serverless:
        monkeypatch.setenv("VERCEL", "1")
    else:
        monkeypatch.delenv("VERCEL", raising=False)

    name = "_database_hide_parameters_probe"
    spec = importlib.util.spec_from_file_location(name, _DB_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module, captured


@pytest.mark.parametrize(
    ("url", "serverless"),
    [
        pytest.param(_TXN_URL, False, id="transaction-pooler"),
        pytest.param(_SESSION_URL, True, id="serverless-session-pooler"),
        pytest.param(_SESSION_URL, False, id="long-lived-session-pooler"),
    ],
)
def test_every_engine_branch_hides_parameters(monkeypatch, url, serverless):
    module, captured = _load_database_module(monkeypatch, url, serverless=serverless)
    assert len(captured) == 1, "expected exactly one engine to be built"
    assert captured[0].get("hide_parameters") is True
    assert module.engine.sync_engine.hide_parameters is True


def test_hidden_parameters_are_absent_from_the_error_text():
    """End to end on a real (SQLite) engine: the exception string carries the
    SQL, but not the value that was bound into it."""
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite://", hide_parameters=True)
    secret = "Jane Doe 123456789 jane@example.org"
    with engine.connect() as conn:
        with pytest.raises(Exception) as excinfo:
            conn.execute(text("SELECT * FROM no_such_table WHERE x = :v"), {"v": secret})
    assert secret not in str(excinfo.value)
    assert "hide_parameters" in str(excinfo.value)
