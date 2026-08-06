"""
Tests for the LMS consumer side of the external exam-platform integration:
- fetch_batch_scores_by_date / fetch_scores_by_date forward exam_type.
- _post attaches the X-Exam-Type header only when an exam type is given.

Scaled weekly-set scores are no longer consumed by the leaderboard, so
SATService.extract_weekly_set (and its tests) are gone; the raw per-section
correct/total counts are what remains.

No DB or network needed — httpx is stubbed.
"""
import asyncio

from src.services.sat_service import SATService


def test_section_scores_fall_back_to_sat_question_counts():
    # SAT is the default product: a payload without totals gets the fixed 22/27.
    scores = SATService.extract_section_scores({"mathCorrectCount": 15, "verbalCorrectCount": 20})
    assert scores == {"math_correct": 15, "verbal_correct": 20,
                      "math_total": 22, "verbal_total": 27}
    assert SATService.extract_section_scores(
        {"mathCorrectCount": 15}, exam_type="SAT")["math_total"] == 22


def test_non_sat_products_report_no_total_instead_of_sat_denominators():
    """22/27 are SAT question counts — emitting them for NUET would render a
    NUET result as e.g. 15/22, which is silently wrong. No total is correct."""
    scores = SATService.extract_section_scores(
        {"mathCorrectCount": 15, "verbalCorrectCount": 20}, exam_type="NUET")
    assert scores == {"math_correct": 15, "verbal_correct": 20,
                      "math_total": None, "verbal_total": None}


def test_totals_present_in_payload_win_for_every_product():
    item = {"mathCorrectCount": 8, "mathTotalCount": 10,
            "verbalCorrectCount": 9, "verbalQuestionCount": 12}
    for exam_type in (None, "SAT", "NUET"):
        scores = SATService.extract_section_scores(item, exam_type=exam_type)
        assert scores["math_total"] == 10, exam_type
        assert scores["verbal_total"] == 12, exam_type


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


def test_fetch_by_week_forwards_week_and_exam_type(monkeypatch):
    captured = {}

    async def fake_post(path, payload, timeout=20.0, exam_type=None):
        captured["path"] = path
        captured["payload"] = payload
        captured["exam_type"] = exam_type
        return {"results": []}

    monkeypatch.setattr(SATService, "_post", staticmethod(fake_post))

    asyncio.run(SATService.fetch_batch_scores_by_week(["a@b.c"], 5, exam_type="NUET"))
    assert captured["path"] == "/students/batch-scores-by-week"
    assert captured["payload"] == {"emails": ["a@b.c"], "week": 5}
    assert captured["exam_type"] == "NUET"

    # No emails / no week → short-circuits without calling the API
    captured.clear()
    assert asyncio.run(SATService.fetch_batch_scores_by_week([], 5)) == {"results": []}
    assert asyncio.run(SATService.fetch_batch_scores_by_week(["a@b.c"], 0)) == {"results": []}
    assert "path" not in captured


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
