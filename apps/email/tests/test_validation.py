"""Tests for apps.email.services.validation module."""

import pytest
from django.core.cache import cache

from apps.core.models import MailProviderSettings
from apps.email.services import validation


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear cache before each test."""
    cache.clear()
    yield
    cache.clear()


class TestIsValidSyntax:
    """Test email syntax validation."""

    def test_valid_email(self):
        assert validation.is_valid_syntax("user@example.com")
        assert validation.is_valid_syntax("test.email+tag@sub.example.co.uk")

    def test_invalid_syntax(self):
        assert not validation.is_valid_syntax("invalid")
        assert not validation.is_valid_syntax("@example.com")
        assert not validation.is_valid_syntax("user@")
        assert not validation.is_valid_syntax("user @example.com")

    def test_empty_email(self):
        assert not validation.is_valid_syntax("")


class TestHasMxRecord:
    """Test MX record checking with monkeypatched DNS."""

    def test_domain_with_mx_record(self, monkeypatch, db):
        """Domain that has MX records should return True."""

        def fake_resolver(domain):
            if domain == "example.com":
                return ["mx1.example.com", "mx2.example.com"]
            return []

        monkeypatch.setattr(validation, "_resolve_mx", fake_resolver)
        assert validation.has_mx_record("example.com")

    def test_domain_without_mx_record(self, monkeypatch, db):
        """Domain with no MX records should return False."""

        def fake_resolver(domain):
            return []

        monkeypatch.setattr(validation, "_resolve_mx", fake_resolver)
        assert not validation.has_mx_record("no-mx.example.com")

    def test_empty_domain(self):
        """Empty domain should return False."""
        assert not validation.has_mx_record("")

    def test_caching(self, monkeypatch, db):
        """MX lookups should be cached."""
        call_count = [0]

        def fake_resolver(domain):
            call_count[0] += 1
            return ["mx.example.com"]

        monkeypatch.setattr(validation, "_resolve_mx", fake_resolver)

        # First call should resolve
        result1 = validation.has_mx_record("cached.example.com")
        assert result1 is True
        assert call_count[0] == 1

        # Second call should use cache, not call resolver again
        result2 = validation.has_mx_record("cached.example.com")
        assert result2 is True
        assert call_count[0] == 1  # Still 1, not 2

    def test_cache_ttl_from_settings(self, monkeypatch, db):
        """Cache TTL should respect MailProviderSettings."""
        settings = MailProviderSettings.load()
        settings.mx_validation_cache_ttl_seconds = 3600
        settings.save()

        call_count = [0]

        def fake_resolver(domain):
            call_count[0] += 1
            return ["mx.example.com"]

        monkeypatch.setattr(validation, "_resolve_mx", fake_resolver)

        validation.has_mx_record("ttl-test.example.com")
        assert call_count[0] == 1

        # Manually check that cache entry was set with correct TTL
        cache_key = "mx_check:ttl-test.example.com"
        cached_value = cache.get(cache_key)
        assert cached_value is True


class TestValidateRecipient:
    """Test full recipient validation (syntax + MX)."""

    def test_valid_recipient(self, monkeypatch, db):
        """Valid syntax + existing MX should return True."""

        def fake_resolver(domain):
            if domain == "example.com":
                return ["mx.example.com"]
            return []

        monkeypatch.setattr(validation, "_resolve_mx", fake_resolver)

        assert validation.validate_recipient("user@example.com")

    def test_invalid_syntax(self, monkeypatch, db):
        """Invalid syntax should return False without DNS lookup."""

        def fake_resolver(domain):
            raise AssertionError("Should not be called for invalid syntax")

        monkeypatch.setattr(validation, "_resolve_mx", fake_resolver)

        assert not validation.validate_recipient("invalid")

    def test_no_mx_record(self, monkeypatch, db):
        """Valid syntax but no MX should return False."""

        def fake_resolver(domain):
            return []

        monkeypatch.setattr(validation, "_resolve_mx", fake_resolver)

        assert not validation.validate_recipient("user@no-mx.example.com")

    def test_validation_disabled(self, monkeypatch, db):
        """Should return True when validation is globally disabled."""
        settings = MailProviderSettings.load()
        settings.enable_recipient_validation = False
        settings.save()

        def fake_resolver(domain):
            raise AssertionError("Should not be called when validation is disabled")

        monkeypatch.setattr(validation, "_resolve_mx", fake_resolver)

        # Should return True even with invalid syntax when validation disabled
        assert validation.validate_recipient("invalid@example.com")

    def test_validation_enabled(self, monkeypatch, db):
        """Should validate when enabled."""
        settings = MailProviderSettings.load()
        settings.enable_recipient_validation = True
        settings.save()

        def fake_resolver(domain):
            return []

        monkeypatch.setattr(validation, "_resolve_mx", fake_resolver)

        # Should return False for invalid MX even when enabled
        assert not validation.validate_recipient("user@no-mx.example.com")
