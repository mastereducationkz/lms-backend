"""Single source of truth for the password policy, matched to the Master Education (Zitadel)
complexity policy so any password we accept locally will also be accepted by Zitadel's SetPassword
API — otherwise the mirror silently fails and the local and SSO passwords diverge.

Policy (agreed 2026-07-12): minimum 8 characters, at least one digit. No case/symbol requirement
(student-friendly). Keep this in lockstep with the Zitadel org password-complexity policy.
"""
from __future__ import annotations

import secrets
import string

MIN_LENGTH = 8

# Shown to users when validation fails (Russian — matches the rest of the auth UX).
POLICY_MESSAGE_RU = "Пароль должен быть не короче 8 символов и содержать хотя бы одну цифру"


def password_policy_error(password: str | None) -> str | None:
    """Return a user-facing error message if the password violates the policy, else None."""
    if not password or len(password) < MIN_LENGTH:
        return POLICY_MESSAGE_RU
    if not any(c.isdigit() for c in password):
        return POLICY_MESSAGE_RU
    return None


def is_policy_compliant(password: str | None) -> bool:
    return password_policy_error(password) is None


def generate_compliant_password(length: int = 12) -> str:
    """Generate a random password that ALWAYS satisfies the policy (>= MIN_LENGTH, >= 1 digit), so
    admin-generated/temporary passwords mirror to Zitadel cleanly."""
    length = max(length, MIN_LENGTH)
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isdigit() for c in pw):  # guarantee a digit (loop retries in the rare all-letter case)
            return pw
