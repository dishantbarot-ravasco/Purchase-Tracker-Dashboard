"""
Unit tests for apps/services/validation.py - the pure, dependency-free GSTIN/
email format checks backing the inline "Edit Everywhere" correction feature.
Same convention as test_parsers_common.py: no Django DB, no mocking.
"""

from apps.services.validation import is_valid_email, is_valid_gstin


class TestIsValidGstin:
    def test_real_shaped_gstin(self):
        assert is_valid_gstin("27AAPFU0939F1ZV") is True

    def test_blank_is_allowed(self):
        assert is_valid_gstin("") is True
        assert is_valid_gstin(None) is True

    def test_not_available_placeholder_is_allowed(self):
        assert is_valid_gstin("Not available") is True
        assert is_valid_gstin("N/A") is True

    def test_too_short_is_invalid(self):
        assert is_valid_gstin("27AAPFU0939F1Z") is False

    def test_missing_literal_z_is_invalid(self):
        assert is_valid_gstin("27AAPFU0939F1XV") is False

    def test_lowercase_is_still_valid(self):
        # Real-world typed corrections may not be uppercased by the user -
        # is_valid_gstin should normalize case before matching.
        assert is_valid_gstin("27aapfu0939f1zv") is True


class TestIsValidEmail:
    def test_real_shaped_email(self):
        assert is_valid_email("vendor@example.com") is True

    def test_blank_is_allowed(self):
        assert is_valid_email("") is True
        assert is_valid_email(None) is True

    def test_missing_at_sign_is_invalid(self):
        assert is_valid_email("vendor.example.com") is False

    def test_missing_domain_dot_is_invalid(self):
        assert is_valid_email("vendor@example") is False

    def test_whitespace_only_is_treated_as_blank(self):
        assert is_valid_email("   ") is True
