# tests/test_admin_auth.py
"""
Unit tests for the admin UI's auth gate (server/admin.py). require_admin_auth
is a plain function over an HTTPBasicCredentials value -- deliberately
factored that way so it's testable without spinning up the FastAPI app.
"""
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials

from server.admin import require_admin_auth


def test_rejects_when_admin_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    creds = HTTPBasicCredentials(username="admin", password="whatever")

    with pytest.raises(HTTPException) as exc_info:
        require_admin_auth(creds)
    # Fail CLOSED: an unset key must reject every request, not allow them.
    assert exc_info.value.status_code == 503


def test_rejects_wrong_password(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "correct-horse-battery-staple")
    creds = HTTPBasicCredentials(username="admin", password="wrong")

    with pytest.raises(HTTPException) as exc_info:
        require_admin_auth(creds)
    assert exc_info.value.status_code == 401
    assert exc_info.value.headers.get("WWW-Authenticate", "").startswith("Basic")


def test_accepts_correct_password_regardless_of_username(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "correct-horse-battery-staple")
    creds = HTTPBasicCredentials(username="whoever-is-on-call", password="correct-horse-battery-staple")

    assert require_admin_auth(creds) == "whoever-is-on-call"


def test_falls_back_to_admin_when_username_is_blank(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "k")
    creds = HTTPBasicCredentials(username="", password="k")

    assert require_admin_auth(creds) == "admin"
