from unittest.mock import MagicMock
from types import SimpleNamespace

from src.routes import crm_internal


def _user(email="stu@example.com", role="student", is_active=True, name="Aizhan"):
    return SimpleNamespace(id=1, email=email, name=name, role=role, is_active=is_active)


def test_deliver_invite_sends_for_real_student(monkeypatch):
    sent = MagicMock(return_value={"id": "resend-123"})
    monkeypatch.setattr(crm_internal, "send_invite_email", sent)
    result = crm_internal._deliver_invite(_user(), "TempPass123")
    assert result == {"sent": True, "reason": None}
    sent.assert_called_once_with("stu@example.com", "Aizhan", "stu@example.com", "TempPass123")


def test_deliver_invite_skips_synthetic_email(monkeypatch):
    sent = MagicMock()
    monkeypatch.setattr(crm_internal, "send_invite_email", sent)
    result = crm_internal._deliver_invite(_user(email="phone7701@import.local"), "x")
    assert result == {"sent": False, "reason": "no_email"}
    sent.assert_not_called()


def test_deliver_invite_skips_non_student(monkeypatch):
    sent = MagicMock()
    monkeypatch.setattr(crm_internal, "send_invite_email", sent)
    result = crm_internal._deliver_invite(_user(role="teacher"), "x")
    assert result == {"sent": False, "reason": "not_student"}
    sent.assert_not_called()
