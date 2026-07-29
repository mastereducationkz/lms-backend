"""Model registration + schema smoke tests for FavoriteStep (no DB required)."""

def test_favorite_step_model_registered():
    from src.schemas.models import FavoriteStep
    assert FavoriteStep.__tablename__ == "favorite_steps"
    cols = set(FavoriteStep.__table__.columns.keys())
    assert {"id", "user_id", "step_id", "lesson_id", "course_id", "created_at"} <= cols
    uniques = [
        tuple(c.name for c in con.columns)
        for con in FavoriteStep.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    ]
    assert ("user_id", "step_id") in uniques


def test_favorite_step_in_metadata_and_all():
    from src.models import __all__ as models_all
    assert "FavoriteStep" in models_all
    from src.models.base import Base
    assert "favorite_steps" in Base.metadata.tables


def test_favorite_step_schemas_importable():
    from src.schemas.models import FavoriteStepCreateSchema, FavoriteStepItemSchema
    payload = FavoriteStepCreateSchema(step_id=7)
    assert payload.step_id == 7
    item = FavoriteStepItemSchema(
        id=1, step_id=7, lesson_id=2, course_id=3, course_title="C",
        lesson_title="L", order_index=5, step_title="S", content_type="text",
        created_at=None,
    )
    assert item.order_index == 5 and item.course_title == "C"
