"""
Tests for the LMS consumer side of the Weekly Set integration:
- SATService.extract_weekly_set normalizes the producer's camelCase weeklySet
  object into snake_case (and returns None when absent).
- fetch_batch_scores_by_date / fetch_scores_by_date forward exam_type.
- _post attaches the X-Exam-Type header only when an exam type is given.

No DB or network needed — httpx is stubbed.
"""
import asyncio

from src.services.sat_service import SATService


def test_extract_weekly_set_normalizes():
    item = {
        "weeklySet": {
            "id": 42, "name": "Week 5 (13.06-14.06)", "weekNumber": 5,
            "examType": "SAT", "verbalScaled": 720, "mathScaled": 700,
            "total": 1420, "completed": True, "completedAt": "2026-06-14T18:03:00Z",
        }
    }
    assert SATService.extract_weekly_set(item) == {
        "id": 42, "name": "Week 5 (13.06-14.06)", "week_number": 5,
        "exam_type": "SAT", "verbal_scaled": 720, "math_scaled": 700,
        "total": 1420, "completed": True, "completed_at": "2026-06-14T18:03:00Z",
    }


def test_extract_weekly_set_partial_and_missing():
    # Section not taken → scaled null, total = the taken section, completed False
    partial = {"weeklySet": {"id": 7, "examType": "NUET", "verbalScaled": 96,
                             "mathScaled": None, "total": 96, "completed": False}}
    ws = SATService.extract_weekly_set(partial)
    assert ws["math_scaled"] is None and ws["verbal_scaled"] == 96 and ws["total"] == 96
    assert ws["exam_type"] == "NUET" and ws["completed"] is False

    # No / null weekly set → None
    assert SATService.extract_weekly_set({}) is None
    assert SATService.extract_weekly_set({"weeklySet": None}) is None


def test_fetch_forwards_exam_type(monkeypatch):
    captured = {}

    async def fake_post(path, payload, timeout=20.0, exam_type=None):
        captured["exam_type"] = exam_type
        return {"results": []}

    monkeypatch.setattr(SATService, "_post", staticmethod(fake_post))

    asyncio.run(SATService.fetch_batch_scores_by_date(["a@b.c"], "13.06", exam_type="NUET"))
    assert captured["exam_type"] == "NUET"

    asyncio.run(SATService.fetch_batch_scores_by_date(["a@b.c"], "13.06"))
    assert captured["exam_type"] is None  # absent ⇒ SAT

    asyncio.run(SATService.fetch_scores_by_date("a@b.c", "13.06", exam_type="NUET"))
    assert captured["exam_type"] == "NUET"


def test_post_sets_exam_type_header(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"ok": True}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None, timeout=None):
            captured["headers"] = headers
            return FakeResp()

    monkeypatch.setattr("src.services.sat_service.httpx.AsyncClient", lambda: FakeClient())

    asyncio.run(SATService._post("/students/scores-by-date", {}, exam_type="NUET"))
    assert captured["headers"]["X-Exam-Type"] == "NUET"

    asyncio.run(SATService._post("/students/scores-by-date", {}))
    assert "X-Exam-Type" not in captured["headers"]  # SAT default sends no header
