"""Password policy — kept in lockstep with the Zitadel org complexity policy (min 8 + a digit) so
every locally-accepted password also passes Zitadel's SetPassword (otherwise the mirror silently
fails and the local/SSO passwords diverge)."""

from src.utils.password_policy import (
    generate_compliant_password,
    is_policy_compliant,
    password_policy_error,
)


def test_rejects_short_password():
    assert password_policy_error("abc1234") is not None      # 7 chars
    assert password_policy_error("") is not None
    assert password_policy_error(None) is not None


def test_rejects_no_digit():
    assert password_policy_error("abcdefgh") is not None      # 8 chars, no digit


def test_accepts_min_8_with_digit():
    assert password_policy_error("abcdefg1") is None          # exactly 8 + a digit
    assert password_policy_error("mypassword2024") is None
    assert is_policy_compliant("student01")


def test_generated_passwords_are_always_compliant():
    for length in (8, 10, 12, 16):
        for _ in range(200):
            pw = generate_compliant_password(length)
            assert len(pw) >= 8
            assert any(c.isdigit() for c in pw)
            assert is_policy_compliant(pw)


def test_generate_never_below_minimum():
    # even if asked for fewer, never drop below the policy minimum
    assert len(generate_compliant_password(4)) >= 8
