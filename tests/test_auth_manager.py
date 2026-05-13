"""Tests : vault Fernet + AuthManager + helpers domaine."""

from __future__ import annotations

import pytest

from job_agent.auth_manager import (
    AuthManager,
    Vault,
    root_domain,
    site_supports_auto_register,
)
from job_agent.models import AuthMethod


def test_root_domain_extracts_correctly():
    assert root_domain("https://www.greenhouse.io/jobs/123") == "greenhouse.io"
    assert root_domain("https://jobs.lever.co/acme/abc") == "lever.co"
    assert root_domain("https://linkedin.com/jobs/view/123") == "linkedin.com"
    assert root_domain("https://localhost/x") == "localhost"


def test_site_supports_auto_register():
    assert site_supports_auto_register("https://boards.greenhouse.io/acme/jobs/1")
    assert site_supports_auto_register("https://jobs.lever.co/acme/abc")
    assert not site_supports_auto_register("https://www.linkedin.com/jobs/view/123")
    assert not site_supports_auto_register("https://www.indeed.com/job/x")


def test_vault_roundtrip():
    v = Vault("hunter2-correct-horse")
    encrypted = v.encrypt("super secret")
    assert encrypted != b"super secret"
    assert v.decrypt(encrypted) == "super secret"


def test_vault_wrong_password_raises():
    v1 = Vault("password1")
    encrypted = v1.encrypt("secret")
    v2 = Vault("password2")
    with pytest.raises(ValueError):
        v2.decrypt(encrypted)


def test_vault_empty_master_rejected():
    with pytest.raises(ValueError):
        Vault("")


def test_auth_manager_add_and_get(temp_db):
    vault = Vault("test-master")
    am = AuthManager(temp_db, vault)
    am.add_credential(
        site_domain="greenhouse.io",
        username="alice",
        password="s3cret!",
        auth_method=AuthMethod.STORED,
    )
    cred = am.get_credential("greenhouse.io")
    assert cred is not None
    assert cred.username == "alice"
    assert cred.password == "s3cret!"
    assert cred.auth_method == AuthMethod.STORED


def test_auth_manager_resolve_ats(temp_db):
    am = AuthManager(temp_db, Vault("k"))
    cred = am.resolve_for_url("https://boards.greenhouse.io/acme/jobs/1")
    assert cred.auth_method == AuthMethod.STORED


def test_auth_manager_resolve_non_ats(temp_db):
    am = AuthManager(temp_db, Vault("k"))
    cred = am.resolve_for_url("https://www.linkedin.com/jobs/view/123")
    assert cred.auth_method == AuthMethod.SHARED_CHROME


def test_auth_manager_explicit_overrides_default(temp_db):
    am = AuthManager(temp_db, Vault("k"))
    am.add_credential(
        site_domain="linkedin.com",
        username="bob",
        password=None,
        auth_method=AuthMethod.SHARED_CHROME,
    )
    cred = am.resolve_for_url("https://www.linkedin.com/jobs/view/123")
    assert cred.auth_method == AuthMethod.SHARED_CHROME
    assert cred.username == "bob"


def test_list_credentials_does_not_leak_password(temp_db):
    am = AuthManager(temp_db, Vault("k"))
    am.add_credential(
        site_domain="lever.co",
        username="carol",
        password="dont-leak",
    )
    listed = am.list_credentials()
    assert listed[0]["site_domain"] == "lever.co"
    assert listed[0]["has_password"] is True
    assert "dont-leak" not in str(listed)


def test_remove_credential(temp_db):
    am = AuthManager(temp_db, Vault("k"))
    am.add_credential(site_domain="x.com", username="u", password="p")
    assert am.get_credential("x.com") is not None
    assert am.remove_credential("x.com") is True
    assert am.get_credential("x.com") is None
