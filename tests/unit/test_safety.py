from __future__ import annotations

import os
from pathlib import Path

import pytest

from coded_autonomous_qa.config import PermissionMode
from coded_autonomous_qa.safety.guards import (
    SafetyError,
    destructive_terms,
    require_execution_permission,
    require_test_environment,
    resolve_readable_path,
    validate_app_url,
)
from coded_autonomous_qa.safety.masking import mask_text, mask_value
from coded_autonomous_qa.safety.secrets import SecretUnavailable, resolve_secrets, secret_names
from tests.conftest import make_settings


class TestMasking:
    def test_email_card_pan_aadhaar(self):
        text = "Mail john.doe@bank.com card 4111 1111 1111 1111 PAN ABCDE1234F aadhaar 2345 6789 0123"
        out = mask_text(text)
        assert "john.doe" not in out and "j***@bank.com" in out
        assert "4111 1111 1111 1111" not in out and "1111" in out
        assert "ABCDE1234F" not in out
        assert "2345 6789 0123" not in out

    def test_short_numbers_untouched_long_digits_masked(self):
        assert mask_text("order 42 shipped") == "order 42 shipped"
        assert mask_text("account 123456789012") == "account ********9012"

    def test_key_based_masking_and_false_positives(self):
        out = mask_value(
            {
                "password": "hunter2",
                "api_key": "abc",
                "company": "Acme",
                "author": "Sam",
                "expanded": True,
                "nested": {"auth_token": "t"},
            }
        )
        assert out["password"] == "***" and out["api_key"] == "***" and out["nested"]["auth_token"] == "***"
        assert out["company"] == "Acme" and out["author"] == "Sam" and out["expanded"] is True

    def test_inline_secrets_and_url_credentials(self):
        out = mask_text("password=hunter2 Authorization: Bearer abcdefghijkl https://u:p@host/x")
        assert "hunter2" not in out and "abcdefghijkl" not in out and "u:p@" not in out


class TestGuards:
    def test_url_validation(self, tmp_path, workspace):
        s = make_settings(tmp_path, workspace)
        assert validate_app_url("http://localhost:8080/a", s)
        assert validate_app_url("https://app.qa.example.test/x", s)
        for bad, code in [
            ("https://prod.bank.com", "production_host"),
            ("ftp://localhost", "invalid_url"),
            ("http://user:pw@localhost", "invalid_url"),
            ("https://example.org", "host_not_allowed"),
            ("file:///etc/passwd", "invalid_url"),
        ]:
            with pytest.raises(SafetyError) as e:
                validate_app_url(bad, s)
            assert e.value.code == code

    def test_test_environment_only(self, tmp_path, workspace):
        s = make_settings(tmp_path, workspace)
        assert require_test_environment(s) == "qa"
        with pytest.raises(SafetyError):
            require_test_environment(s, "production")

    def test_path_restriction_and_symlink_escape(self, tmp_path, workspace):
        s = make_settings(tmp_path, workspace)
        inside = workspace / "a.txt"
        inside.write_text("x")
        assert resolve_readable_path(inside, s) == inside.resolve()
        outside = tmp_path / "secret.txt"
        outside.write_text("x")
        link = workspace / "link.txt"
        os.symlink(outside, link)
        with pytest.raises(SafetyError):
            resolve_readable_path(link, s)
        with pytest.raises(SafetyError):
            resolve_readable_path(Path("/etc/hosts"), s)

    def test_destructive_terms_word_boundaries(self, tmp_path, workspace):
        s = make_settings(tmp_path, workspace)
        assert destructive_terms("Click Delete account", s) == ["delete"]
        assert "pay" in destructive_terms("Pay now", s)
        assert destructive_terms("Open payroll report", s) == []  # 'pay' inside a word is not a hit
        assert destructive_terms("Display profile", s) == []

    def test_discovery_mode_blocks_execution(self, tmp_path, workspace):
        with pytest.raises(SafetyError) as e:
            require_execution_permission(make_settings(tmp_path, workspace, PermissionMode.DISCOVERY))
        assert e.value.code == "discovery_only"


class TestSecrets:
    def test_secret_refs(self, monkeypatch):
        assert secret_names({"a": ["${secret:USER_PW}"]}) == {"USER_PW"}
        monkeypatch.setenv("CODED_QA_SECRET_USER_PW", "s3cret")
        assert resolve_secrets({"v": "x ${secret:USER_PW}"}) == {"v": "x s3cret"}

    def test_missing_secret(self, monkeypatch):
        monkeypatch.delenv("CODED_QA_SECRET_NOPE", raising=False)
        monkeypatch.setattr("sys.platform", "linux")
        with pytest.raises(SecretUnavailable):
            resolve_secrets("${secret:NOPE}")
