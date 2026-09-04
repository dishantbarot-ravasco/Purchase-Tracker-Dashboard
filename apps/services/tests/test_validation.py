"""
Unit tests for apps/services/validation.py - the pure, dependency-free GSTIN/
email format checks backing the inline "Edit Everywhere" correction feature.
Same convention as test_parsers_common.py: no Django DB, no mocking.
"""

from apps.services.validation import is_valid_email, is_valid_gstin


# ── is_valid_gstin(): format check backing the "save but warn" GSTIN correction flow ──

class TestIsValidGstin:
    def test_real_shaped_gstin(self):
        """A real, correctly-shaped 15-character GSTIN passes."""
        assert is_valid_gstin("27AAPFU0939F1ZV") is True

    def test_blank_is_allowed(self):
        """A blank/None GSTIN is allowed - "not yet known" isn't invalid data,
        it's an editor clearing an unhelpful placeholder."""
        assert is_valid_gstin("") is True
        assert is_valid_gstin(None) is True

    def test_not_available_placeholder_is_allowed(self):
        """The literal placeholder values real vendors sometimes use in place
        of an actual GSTIN ("Not available", "N/A") are accepted as valid,
        not flagged as malformed."""
        assert is_valid_gstin("Not available") is True
        assert is_valid_gstin("N/A") is True

    def test_too_short_is_invalid(self):
        """A GSTIN one character short of the real 15-character format is invalid."""
        assert is_valid_gstin("27AAPFU0939F1Z") is False

    def test_missing_literal_z_is_invalid(self):
        """A real GSTIN's 14th character is always the literal 'Z' - a value
        that's otherwise the right shape but has a different character there
        must still be rejected."""
        assert is_valid_gstin("27AAPFU0939F1XV") is False

    def test_lowercase_is_still_valid(self):
        # Real-world typed corrections may not be uppercased by the user -
        # is_valid_gstin should normalize case before matching.
        """Real-world typed corrections may not be uppercased by the user -
        is_valid_gstin must normalize case before matching, not reject a
        correctly-shaped GSTIN just because of casing."""
        assert is_valid_gstin("27aapfu0939f1zv") is True


# ── is_valid_email(): format check backing the "save but warn" vendor-email correction flow ──

class TestIsValidEmail:
    def test_real_shaped_email(self):
        """A normal, well-formed email address passes."""
        assert is_valid_email("vendor@example.com") is True

    def test_blank_is_allowed(self):
        """A blank/None email is allowed, same reasoning as a blank GSTIN."""
        assert is_valid_email("") is True
        assert is_valid_email(None) is True

    def test_missing_at_sign_is_invalid(self):
        """A string with no '@' at all is rejected."""
        assert is_valid_email("vendor.example.com") is False

    def test_missing_domain_dot_is_invalid(self):
        """A domain with no dot (no real TLD) is rejected."""
        assert is_valid_email("vendor@example") is False

    def test_whitespace_only_is_treated_as_blank(self):
        """Whitespace-only input is treated the same as a genuinely blank
        value (allowed), not as malformed text."""
        assert is_valid_email("   ") is True
