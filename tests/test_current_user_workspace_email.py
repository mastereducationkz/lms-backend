"""``workspace_email`` reaches its owner through /auth/me — and nowhere else.

The frontend adds it to Meet links as ``authuser`` so a teacher joins on her work account.
The field is viewer-only: ``UserSchema`` also serialises other people, so if it were
declared there, teachers' work addresses would appear in every user list.
"""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth.schemas import CurrentUserSchema, UserSchema
from src.auth.user_schema import build_user_schema_response


def _teacher(**overrides):
    fields = dict(
        id=42, email="gulzada.personal@example.com", name="Gulzada", role="teacher",
        is_active=True, workspace_email="gulzada@mastereducation.kz",
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_own_record_carries_the_workspace_email():
    record = build_user_schema_response(_teacher(), db=None)
    assert isinstance(record, CurrentUserSchema)
    assert record.workspace_email == "gulzada@mastereducation.kz"


def test_a_user_without_one_gets_none_not_an_error():
    """Students have no Workspace account; their Meet links must stay untouched."""
    record = build_user_schema_response(_teacher(role="teacher", workspace_email=None), db=None)
    assert record.workspace_email is None


def test_routes_typed_as_UserSchema_never_expose_it():
    """The leak this design exists to prevent, checked against FastAPI itself.

    Profile updates and onboarding return the same builder but are typed ``UserSchema``.
    Pydantic v2 serialises a subclass through the declared parent type, so the extra field
    is dropped — but that is framework behaviour, and a framework upgrade could change it.
    """
    app = FastAPI()

    @app.get("/other", response_model=UserSchema)
    def other():
        return build_user_schema_response(_teacher(), db=None)

    @app.get("/me", response_model=CurrentUserSchema)
    def me():
        return build_user_schema_response(_teacher(), db=None)

    client = TestClient(app)
    assert "workspace_email" not in client.get("/other").json()
    assert client.get("/me").json()["workspace_email"] == "gulzada@mastereducation.kz"


def test_the_real_me_route_is_typed_to_include_it():
    """Guards against someone reverting /me to ``UserSchema`` and silently dropping it."""
    from src.auth.routes.auth import router

    me = [r for r in router.routes if getattr(r, "path", "").endswith("/me")]
    assert me, "no /me route found on the auth router"
    assert all(r.response_model is CurrentUserSchema for r in me)
