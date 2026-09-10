"""API datetimes survive a cache round trip: UTC with a Z, never an offset AND a Z.

On a cache hit the stored JSON is re-validated into the response model, which turned "…Z" into
an aware datetime; the old encoder then wrote "…+00:00Z" and every browser read it as Invalid
Date. Reproduced here exactly as cache_service stores and FastAPI re-serializes.
"""
import json
from datetime import datetime, timedelta, timezone

from fastapi.encoders import jsonable_encoder

from src.events.schemas import EventSchema
from src.utils.utc_json import utc_z


def _event():
    return EventSchema(id=1, title="t", event_type="class", start_datetime=datetime(2026, 8, 1, 5, 0),
                       end_datetime=datetime(2026, 8, 1, 6, 0), is_online=True, created_by=1,
                       is_active=True, is_recurring=False)


def test_naive_and_aware_are_written_the_same_way():
    assert utc_z(datetime(2026, 8, 1, 5)) == "2026-08-01T05:00:00Z"
    assert utc_z(datetime(2026, 8, 1, 5, tzinfo=timezone.utc)) == "2026-08-01T05:00:00Z"
    assert utc_z(datetime(2026, 8, 1, 10, tzinfo=timezone(timedelta(hours=5)))) == "2026-08-01T05:00:00Z"
    assert utc_z(None) is None


def test_a_cache_hit_answers_exactly_like_a_miss():
    miss = jsonable_encoder(_event())
    stored = json.loads(json.dumps(miss))  # what cache_service keeps in Redis
    hit = jsonable_encoder(EventSchema.model_validate(stored))  # FastAPI re-validates on a hit
    assert hit["start_datetime"] == miss["start_datetime"]
    assert "+00:00Z" not in hit["start_datetime"] and hit["start_datetime"].endswith("Z")
