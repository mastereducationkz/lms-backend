"""Проза от LLM: белый список чисел и вырезание невалидных слотов.

LLM здесь не вызывается ни разу — генерация проверяется через подменённый клиент.
"""
import pytest

from src.reports.parent.prose import (
    FACTUAL_SLOTS,
    allowed_numbers,
    generate_prose,
    sanitize,
)


def facts(**over) -> dict:
    base = {
        "student": {"id": 1, "name": "Амир"},
        "group": {"id": 10, "name": "SAT-3"},
        "week": {"start": "2026-09-14", "end": "2026-09-20"},
        "test": {
            "program": "sat", "label": "Week 5", "date": "2026-09-19",
            "verbal": {"correct": 17, "total": 27},
            "math": {"correct": 15, "total": 22},
            "prev": {"label": "Week 4",
                     "verbal": {"correct": 14, "total": 27},
                     "math": {"correct": 15, "total": 22}},
            "delta": {"verbal": 3, "math": 0},
        },
        "test_unavailable": False,
        "homework": {"assigned": 3, "submitted": 2, "missing": []},
        "attendance": {"lessons": 3, "present": 3, "late": 0, "absences": []},
        "talk": None,
        "strength": None,
        "weakness": None,
        "teacher_feedback": None,
        "no_growth_streak": 0,
        "curator_note": None,
    }
    base.update(over)
    return base


def test_allowed_numbers_collects_scores_totals_and_deltas():
    allowed = allowed_numbers(facts())
    assert {"17", "27", "15", "22", "14", "3", "2"} <= allowed


def test_legitimate_delta_passes():
    out = sanitize({"progress": "Есть рост по Verbal на 3 балла."}, facts())
    assert out["progress"] == "Есть рост по Verbal на 3 балла."


def test_invented_score_is_dropped():
    out = sanitize({"progress": "Verbal вырос до 21/27."}, facts())
    assert "progress" not in out


def test_recommendation_may_invent_numbers():
    # «10 слов в день» — предписание, а не факт из отчёта. Белый список к нему неприменим.
    out = sanitize({"recommendation": "Учить по 10 новых слов в день."}, facts())
    assert out["recommendation"] == "Учить по 10 новых слов в день."


def test_proposal_may_invent_numbers():
    out = sanitize({"proposal": "Созвониться втроём на 15 минут."}, facts())
    assert "proposal" in out


def test_recommendation_is_not_a_factual_slot():
    assert "recommendation" not in FACTUAL_SLOTS
    assert "proposal" not in FACTUAL_SLOTS
    assert "progress" in FACTUAL_SLOTS


def test_overlong_slot_is_dropped():
    out = sanitize({"progress": "а" * 201}, facts())
    assert "progress" not in out


def test_blank_slot_is_dropped():
    out = sanitize({"progress": "   "}, facts())
    assert "progress" not in out


def test_number_free_prose_always_passes():
    out = sanitize({"weakness": "Пока сложно с Reading — не хватает словарного запаса."}, facts())
    assert "weakness" in out


class FakeClient:
    """Подменяет AzureOpenAIService: отдаёт заранее заданный словарь слотов."""

    def __init__(self, payload: dict, fail: bool = False):
        self.payload = payload
        self.fail = fail
        self.calls = 0
        self.last_slots: tuple = ()

    async def complete(self, *, facts: dict, slots: tuple) -> dict:
        self.calls += 1
        self.last_slots = slots
        if self.fail:
            raise RuntimeError("azure is down")
        return self.payload


@pytest.mark.asyncio
async def test_generate_prose_requests_only_the_templates_slots():
    # t2 выбирается каскадом только когда данные о речи есть, поэтому и здесь они есть.
    client = FakeClient({"progress": "Рост есть.", "recommendation": "Читать статью в день."})
    await generate_prose(facts(talk={"lessons": 3, "lessons_spoke": 2, "avg_seconds": 75,
                                     "questions": 4, "answers": 2}), "t2", client=client)
    assert set(client.last_slots) == {"activity", "progress", "recommendation"}


@pytest.mark.asyncio
async def test_activity_slot_skipped_without_talk_data():
    # Куратор может выбрать t2 руками у ученика без Talk Time. Писать «активность на
    # уроках» модели тогда не из чего — слот не запрашиваем вовсе.
    client = FakeClient({})
    await generate_prose(facts(talk=None), "t2", client=client)
    assert "activity" not in client.last_slots


@pytest.mark.asyncio
async def test_generate_prose_drops_slots_the_template_did_not_ask_for():
    client = FakeClient({"progress": "Рост есть.", "cause": "Лишний слот."})
    out = await generate_prose(facts(), "t1", client=client)
    assert "cause" not in out


@pytest.mark.asyncio
async def test_generate_prose_retries_once_on_invalid_numbers():
    client = FakeClient({"progress": "Verbal вырос до 21/27."})
    out = await generate_prose(facts(), "t1", client=client)
    assert client.calls == 2
    assert "progress" not in out


@pytest.mark.asyncio
async def test_generate_prose_returns_empty_when_llm_fails():
    # Каркас с числами всё равно отрендерится — куратор допишет прозу руками.
    client = FakeClient({}, fail=True)
    out = await generate_prose(facts(), "t1", client=client)
    assert out == {}


@pytest.mark.asyncio
async def test_strength_slot_not_requested_without_a_candidate():
    client = FakeClient({})
    await generate_prose(facts(), "t1", client=client)
    assert "strength" not in client.last_slots
    assert "weakness" not in client.last_slots


@pytest.mark.asyncio
async def test_teacher_feedback_alone_keeps_the_weakness_slot():
    # Ярлыка темы нет, но живой преподаватель что-то написал — молчать не нужно.
    client = FakeClient({})
    await generate_prose(facts(teacher_feedback="Плывёт на длинных текстах"), "t1",
                         client=client)
    assert "weakness" in client.last_slots


@pytest.mark.asyncio
async def test_strength_slot_requested_when_candidate_exists():
    client = FakeClient({})
    await generate_prose(
        facts(strength={"label": "Algebra", "source": "quiz", "pct": 92}), "t1", client=client
    )
    assert "strength" in client.last_slots
