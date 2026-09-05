"""scripts/import_sat_checkpoint_questions: bank -> quiz steps + required units."""
import json

import pytest

from scripts.import_sat_checkpoint_questions import ImportError_, build_questions, import_checkpoint, run
from scripts.seed_sat_checkpoints import seed
from src.checkpoints.models import CheckpointDefinition
from src.courses.models import Step
from tests.checkpoint_fixtures import make_sat_course


@pytest.fixture
def db():
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available")
    trans = connection.begin()
    session = SASession(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close(); trans.rollback(); connection.close()


def _bank_entry(number, units, n_questions=3, with_short_answer=True):
    questions = []
    for i in range(1, n_questions + 1):
        questions.append({
            "number": i, "type": "single_choice", "text": f"Q{i} &lt;text&gt;",
            "options": ["a", "b", "c", "d"], "answer": "BCDA"[i % 4],
            "difficulty": ["easy", "medium", "hard"][(i - 1) % 3], "skill": "s", "explanation": f"because {i}",
            "answer_source": "teacher_key",
        })
    if with_short_answer:
        questions[-1].update({"type": "short_answer", "options": [], "answer": "12"})
    return {"number": number, "title": f"Checkpoint {number}", "lessons": ["a", "b", "c"], "units": units,
            "questions": questions, "problems": []}


def _seeded(db, blocks=2):
    course, v, m = make_sat_course(db, n_verbal=4, n_math=3)
    db.commit()
    seed(db, sat_course_id=course.id, blocks=blocks)
    return course, v, m


def test_build_questions_shapes_choice_and_short_answer():
    cp = _bank_entry(1, [{"lesson_id": 1, "kind": "verbal"}])
    qs = build_questions(cp, shuffle_options=False)
    assert [q["id"] for q in qs] == ["cp1-q1", "cp1-q2", "cp1-q3"]
    assert qs[0]["question_type"] == "single_choice" and qs[0]["correct_answer"] == 2   # "C"
    assert [o["is_correct"] for o in qs[0]["options"]] == [False, False, True, False]
    assert [o["letter"] for o in qs[0]["options"]] == ["A", "B", "C", "D"]
    assert qs[0]["difficulty"] == "easy" and qs[0]["explanation"] == "because 1"
    assert qs[2]["question_type"] == "short_answer" and qs[2]["correct_answer"] == "12|12.0"


def test_build_questions_deals_options_deterministically_and_keeps_the_right_answer():
    cp = _bank_entry(1, [], n_questions=8, with_short_answer=False)
    cp["questions"][0]["explanation"] = "Choice C is the only complete sentence."
    cp["questions"][1]["options"] = ["1", "2", "3", "4"]                       # numeric list: order kept
    first, second = build_questions(cp), build_questions(cp)
    assert first == second
    for q, src in zip(first, cp["questions"]):
        correct = q["options"][q["correct_answer"]]
        assert correct["is_correct"] and correct["text"] == src["options"]["ABCD".index(src["answer"])]
        assert sorted(o["text"] for o in q["options"]) == sorted(src["options"])
        assert [o["letter"] for o in q["options"]] == ["A", "B", "C", "D"]
    assert [o["text"] for o in first[1]["options"]] == ["1", "2", "3", "4"]
    new_letter = "ABCD"[first[0]["correct_answer"]]
    assert first[0]["explanation"] == f"Choice {new_letter} is the only complete sentence."


def test_build_questions_refuses_missing_answer():
    cp = _bank_entry(1, [])
    cp["questions"][0]["answer"] = None
    with pytest.raises(ImportError_):
        build_questions(cp)


def test_import_writes_quiz_and_units_and_is_idempotent(db):
    course, v, m = _seeded(db)
    units = [{"lesson_id": v[2].id, "kind": "verbal"}, {"lesson_id": v[3].id, "kind": "verbal"},
             {"lesson_id": m[1].id, "kind": "math"}, {"lesson_id": m[2].id, "kind": "math"}]
    cp = _bank_entry(2, units)
    report = import_checkpoint(db, course.id, cp)
    definition = db.query(CheckpointDefinition).filter_by(course_id=course.id, number=2).one()
    step = db.query(Step).filter(Step.lesson_id == definition.quiz_lesson_id, Step.content_type == "quiz").one()
    content = json.loads(step.content_text)
    assert content["display_mode"] == "all_at_once" and content["title"] == "Checkpoint 2"   # seed payload kept
    assert [q["difficulty"] for q in content["questions"]] == ["easy", "medium", "hard"]
    assert content["questions"][0]["question_text"] == "Q1 &lt;text&gt;"
    assert [(u.lesson_id, u.kind, u.position) for u in definition.required_units] == [
        (v[2].id, "verbal", 0), (v[3].id, "verbal", 1), (m[1].id, "math", 2), (m[2].id, "math", 3)]
    assert report["by_difficulty"] == {"easy": 1, "medium": 1, "hard": 1} and report["short_answer"] == 1
    assert report["activated"] is False and definition.is_active is False

    again = import_checkpoint(db, course.id, cp)
    db.refresh(definition)
    assert again["questions"] == 3 and len(definition.required_units) == 4
    assert len(json.loads(step.content_text)["questions"]) == 3


def test_import_activates_only_when_the_count_matches(db):
    course, v, m = _seeded(db)
    units = [{"lesson_id": v[0].id, "kind": "verbal"}, {"lesson_id": v[1].id, "kind": "verbal"}, {"lesson_id": m[0].id, "kind": "math"}]
    definition = db.query(CheckpointDefinition).filter_by(course_id=course.id, number=1).one()
    definition.total_questions = 3; db.flush()
    assert import_checkpoint(db, course.id, _bank_entry(1, units, n_questions=2), activate=True)["activated"] is False
    assert import_checkpoint(db, course.id, _bank_entry(1, units, n_questions=3), activate=True)["activated"] is True
    db.refresh(definition)
    assert definition.is_active is True


def test_import_rejects_foreign_units_and_bad_kind_mix(db):
    course, v, m = _seeded(db)
    other, ov, om = make_sat_course(db, n_verbal=2, n_math=1)
    with pytest.raises(ImportError_, match="not units of course"):
        import_checkpoint(db, course.id, _bank_entry(1, [{"lesson_id": ov[0].id, "kind": "verbal"},
                                                         {"lesson_id": v[0].id, "kind": "verbal"},
                                                         {"lesson_id": m[0].id, "kind": "math"}]))
    with pytest.raises(ImportError_, match="at least 2 verbal"):
        import_checkpoint(db, course.id, _bank_entry(1, [{"lesson_id": v[0].id, "kind": "verbal"},
                                                         {"lesson_id": m[0].id, "kind": "math"}]))
    definition = db.query(CheckpointDefinition).filter_by(course_id=course.id, number=1).one()
    quiz_lesson_id = definition.quiz_lesson_id
    with pytest.raises(ImportError_, match="not units of course"):   # the checkpoint's own quiz lesson is not a unit
        import_checkpoint(db, course.id, _bank_entry(1, [{"lesson_id": v[0].id, "kind": "verbal"},
                                                         {"lesson_id": v[1].id, "kind": "verbal"},
                                                         {"lesson_id": quiz_lesson_id, "kind": "math"}]))


def test_import_needs_a_seeded_definition_and_dry_run_writes_nothing(db):
    course, v, m = _seeded(db, blocks=1)
    units = [{"lesson_id": v[0].id, "kind": "verbal"}, {"lesson_id": v[1].id, "kind": "verbal"}, {"lesson_id": m[0].id, "kind": "math"}]
    with pytest.raises(ImportError_, match="run the seed"):
        import_checkpoint(db, course.id, _bank_entry(2, units))
    report = import_checkpoint(db, course.id, _bank_entry(1, units), dry_run=True)
    definition = db.query(CheckpointDefinition).filter_by(course_id=course.id, number=1).one()
    step = db.query(Step).filter(Step.lesson_id == definition.quiz_lesson_id).one()
    assert report["dry_run"] and json.loads(step.content_text)["questions"] == []
    assert [u.lesson_id for u in definition.required_units] == [v[0].id, v[1].id, m[0].id]   # seed mapping untouched


def test_run_reads_the_bank_file_refuses_problems_and_invalidates_caches(db, tmp_path, monkeypatch):
    course, v, m = _seeded(db, blocks=1)
    calls = []
    monkeypatch.setattr("src.checkpoints.service._invalidate_lesson_caches", lambda: calls.append(1))
    units = [{"lesson_id": v[0].id, "kind": "verbal"}, {"lesson_id": v[1].id, "kind": "verbal"}, {"lesson_id": m[0].id, "kind": "math"}]
    bad = _bank_entry(1, units); bad["problems"] = ["expected 45 questions, parsed 44"]
    path = tmp_path / "bank.json"
    path.write_text(json.dumps({"checkpoints": [bad]}))
    with pytest.raises(ImportError_, match="unresolved problems"):
        run(db, course_id=course.id, data=path, weeks=[], remap_units=True, activate=False, dry_run=False)
    assert calls == []
    path.write_text(json.dumps({"checkpoints": [_bank_entry(1, units)]}))
    reports = run(db, course_id=course.id, data=path, weeks=[1], remap_units=True, activate=False, dry_run=False)
    assert len(reports) == 1 and calls == [1]
