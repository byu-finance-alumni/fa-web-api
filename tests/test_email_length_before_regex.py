"""Email validators check LENGTH before running the shape regex (#597).

Both validators run in ``mode="before"``, i.e. ahead of the field's own
``max_length``, so an over-long value used to reach the regex first. These
tests swap the module's regex for a spy and prove it is never consulted for an
over-long value, while normal values still go through it.
"""

import re

import pytest
from pydantic import ValidationError

from app.api.routes import admin as admin_routes
from app.api.routes.admin import CreateUserRequest
from app.schemas import support as support_schemas
from app.schemas.support import SupportContactCreate, SupportContactUpdate

_LONG = "a" * 300 + "@example.org"


class _SpyRegex:
    def __init__(self, pattern: re.Pattern):
        self._pattern = pattern
        self.calls: list[str] = []

    def match(self, value: str):
        self.calls.append(value)
        return self._pattern.match(value)


@pytest.fixture
def admin_spy(monkeypatch):
    spy = _SpyRegex(admin_routes._EMAIL_RE)
    monkeypatch.setattr(admin_routes, "_EMAIL_RE", spy)
    return spy


@pytest.fixture
def support_spy(monkeypatch):
    spy = _SpyRegex(support_schemas._EMAIL_RE)
    monkeypatch.setattr(support_schemas, "_EMAIL_RE", spy)
    return spy


def test_create_user_rejects_long_email_without_regex(admin_spy):
    with pytest.raises(ValidationError) as excinfo:
        CreateUserRequest(email=_LONG)
    assert admin_spy.calls == []
    assert "at most 255 characters" in str(excinfo.value)


def test_create_user_valid_email_still_shape_checked(admin_spy):
    assert CreateUserRequest(email="  New.User@Example.org ").email == "new.user@example.org"
    assert admin_spy.calls == ["new.user@example.org"]
    with pytest.raises(ValidationError):
        CreateUserRequest(email="not-an-address")


def test_create_user_accepts_exactly_255_chars(admin_spy):
    local = "a" * (255 - len("@example.org"))
    assert len(CreateUserRequest(email=f"{local}@example.org").email) == 255


def test_support_contact_create_rejects_long_email_without_regex(support_spy):
    with pytest.raises(ValidationError):
        SupportContactCreate(role_label="Survey", name="Help", email=_LONG)
    assert support_spy.calls == []


def test_support_contact_update_rejects_long_email_without_regex(support_spy):
    with pytest.raises(ValidationError):
        SupportContactUpdate(email=_LONG)
    assert support_spy.calls == []


def test_support_contact_valid_email_still_shape_checked(support_spy):
    contact = SupportContactCreate(role_label="Survey", name="Help", email="Help@BYU.edu")
    assert contact.email == "help@byu.edu"
    assert support_spy.calls == ["help@byu.edu"]
