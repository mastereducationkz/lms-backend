"""Load the SAT Checkpoints question bank into the checkpoint quizzes and set their required units.

    python -m scripts.import_sat_checkpoint_questions --course-id 1 [--weeks 1,2] [--dry-run] [--activate]
        [--data scripts/data/sat_checkpoints_questions.json] [--no-units]

For every checkpoint in the bank (built by scripts/build_sat_checkpoint_bank.py from the MasterSAT
weekly documents):
  * the quiz step of the checkpoint's lesson gets the bank's questions — replacing whatever the
    step held, keeping the rest of the quiz payload (title, display_mode, …);
  * every question carries its difficulty (easy | medium | hard) and, when the key had one, an
    explanation shown after answering;
  * the definition's required units are replaced with the bank's list (the IT mapping PDF), unless
    --no-units;
  * with --activate, a definition whose quiz now holds exactly total_questions questions is
    activated. Without it, definitions are left as they are (inactive after the seed);
  * a checkpoint whose bank entry has no answers yet (no teacher key) still gets its units bound,
    but its quiz is left untouched; --units-only does that for every checkpoint.

Idempotent: re-running writes the same content again. The lesson caches are invalidated so the
new questions are served immediately. Run the seed first — a missing definition is an error.
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from src.checkpoints.models import CheckpointDefinition, CheckpointRequiredUnit
from src.courses.models import Lesson, Module, Step

DEFAULT_DATA = Path(__file__).resolve().parent / "data" / "sat_checkpoints_questions.json"
DIFFICULTIES = ("easy", "medium", "hard")
NUMERIC_OPTION = re.compile(r"^[-−+]?[\d.,/%\s]+%?$")


class ImportError_(Exception):
    pass


def _accepted_answers(answer: str) -> str:
    """Player rule for short answers: '|'-separated accepted strings, compared trimmed and
    lower-cased. Offer the integer and the '.0' spelling of a whole number, and both spellings of
    a decimal that starts with a dot."""
    a = answer.strip()
    variants = [a]
    if re.fullmatch(r"-?\d+", a):
        variants.append(f"{a}.0")
    elif re.fullmatch(r"-?\d*\.\d+", a):
        variants.append(a.replace(".", "0.", 1) if a.startswith(".") else a)
        if a.startswith("-."):
            variants.append("-0" + a[1:])
    return "|".join(dict.fromkeys(variants))


def _option_order(qid: str, options: List[str], shuffle: bool) -> List[int]:
    """The generated documents key the same letter far too often (three checkpoints have "A" on
    about three quarters of their questions), so the four options are dealt into a new order — a
    fixed order per question id, so a re-import writes the same quiz again. Lists of plain
    numbers keep their ascending order, the SAT convention."""
    order = list(range(len(options)))
    if not shuffle or len(options) != 4 or all(NUMERIC_OPTION.match(o.strip()) for o in options):
        return order
    random.Random(qid).shuffle(order)
    return order


def _relabel(text: str, mapping: Dict[str, str]) -> str:
    """Rewrite 'Choice B' / 'option C' style references in an explanation after a shuffle."""
    return re.sub(r"\b([Cc]hoice|[Oo]ption) ([A-D])\b",
                  lambda m: f"{m.group(1)} {mapping.get(m.group(2), m.group(2))}", text)


def build_questions(cp: Dict[str, Any], *, shuffle_options: bool = True) -> List[Dict[str, Any]]:
    """Bank entries -> quiz player questions (the shape QuizLessonEditor saves)."""
    n = cp["number"]
    out: List[Dict[str, Any]] = []
    for q in cp["questions"]:
        if q.get("answer") in (None, ""):
            raise ImportError_(f"checkpoint {n} q{q['number']}: no answer in the bank")
        if q["difficulty"] not in DIFFICULTIES:
            raise ImportError_(f"checkpoint {n} q{q['number']}: difficulty {q['difficulty']!r}")
        qid = f"cp{n}-q{q['number']}"
        base = {
            "id": qid, "assignment_id": "", "question_text": q["text"], "points": 1,
            "order_index": q["number"] - 1, "difficulty": q["difficulty"],
            "explanation": q.get("explanation") or "", "skill": q.get("skill") or "",
        }
        if q["type"] == "single_choice":
            if len(q["options"]) != 4 or q["answer"] not in "ABCD":
                raise ImportError_(f"checkpoint {n} q{q['number']}: malformed choice question")
            src_idx = "ABCD".index(q["answer"])
            order = _option_order(qid, q["options"], shuffle_options)     # new position -> source index
            idx = order.index(src_idx)
            mapping = {"ABCD"[src]: "ABCD"[pos] for pos, src in enumerate(order)}
            base["explanation"] = _relabel(base["explanation"], mapping)
            base.update({
                "question_type": "single_choice",
                "options": [{"id": f"{qid}-{'abcd'[k]}", "text": q["options"][src], "is_correct": src == src_idx,
                             "letter": "ABCD"[k]} for k, src in enumerate(order)],
                "correct_answer": idx,
            })
        elif q["type"] == "short_answer":
            base.update({"question_type": "short_answer", "options": [],
                         "correct_answer": _accepted_answers(str(q["answer"]))})
        else:
            raise ImportError_(f"checkpoint {n} q{q['number']}: unknown type {q['type']!r}")
        out.append(base)
    return out


def _quiz_step(db: Session, definition: CheckpointDefinition) -> Step:
    if definition.quiz_lesson_id is None:
        raise ImportError_(f"checkpoint {definition.number}: no quiz lesson (run the seed)")
    step = db.query(Step).filter(Step.lesson_id == definition.quiz_lesson_id,
                                 Step.content_type == "quiz").order_by(Step.order_index).first()
    if step is None:
        raise ImportError_(f"checkpoint {definition.number}: quiz lesson {definition.quiz_lesson_id} has no quiz step")
    return step


def _resolve_units(db: Session, course_id: int, units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ids = [int(u["lesson_id"]) for u in units]
    if len(set(ids)) != len(ids):
        raise ImportError_(f"duplicate unit ids {ids}")
    rows = dict(db.query(Lesson.id, Lesson.title).join(Module, Module.id == Lesson.module_id).filter(
        Lesson.id.in_(ids), Module.course_id == course_id, Lesson.kind != "checkpoint").all())
    missing = [i for i in ids if i not in rows]
    if missing:
        raise ImportError_(f"unit ids {missing} are not units of course {course_id}")
    kinds = [u["kind"] for u in units]
    if kinds.count("verbal") < 2 or kinds.count("math") < 1:
        raise ImportError_(f"units {ids}: need at least 2 verbal and 1 math")
    return [{"lesson_id": i, "kind": k, "title": rows[i]} for i, k in zip(ids, kinds)]


def import_checkpoint(db: Session, course_id: int, cp: Dict[str, Any], *, remap_units: bool = True,
                      activate: bool = False, dry_run: bool = False,
                      shuffle_options: bool = True, write_questions: bool = True) -> Dict[str, Any]:
    definition = db.query(CheckpointDefinition).filter_by(course_id=course_id, number=cp["number"]).first()
    if definition is None:
        raise ImportError_(f"checkpoint {cp['number']}: no definition for course {course_id} (run the seed first)")
    has_answers = bool(cp["questions"]) and all(q.get("answer") not in (None, "") for q in cp["questions"])
    questions = build_questions(cp, shuffle_options=shuffle_options) if (write_questions and has_answers) else []
    step = _quiz_step(db, definition)
    units = _resolve_units(db, course_id, cp["units"]) if remap_units else None

    report: Dict[str, Any] = {
        "number": definition.number, "definition_id": definition.id, "quiz_lesson_id": definition.quiz_lesson_id,
        "questions": len(questions),
        "by_difficulty": {d: sum(1 for q in questions if q["difficulty"] == d) for d in DIFFICULTIES},
        "short_answer": sum(1 for q in questions if q["question_type"] == "short_answer"),
        "answer_letters": {L: sum(1 for q in questions if q["question_type"] == "single_choice" and q["correct_answer"] == i)
                           for i, L in enumerate("ABCD")},
        "units": units, "activated": False, "dry_run": dry_run,
        "questions_written": bool(questions), "answers_missing": not has_answers,
    }
    if dry_run:
        return report

    if questions:
        try:
            content = json.loads(step.content_text) if step.content_text else {}
            if not isinstance(content, dict):
                content = {}
        except ValueError:
            content = {}
        content.setdefault("title", cp.get("title") or f"Checkpoint {definition.number}")
        content.setdefault("display_mode", "all_at_once")
        content["questions"] = questions
        step.content_text = json.dumps(content, ensure_ascii=False)

    if units is not None:
        db.query(CheckpointRequiredUnit).filter(CheckpointRequiredUnit.checkpoint_id == definition.id).delete(
            synchronize_session=False)
        db.flush()
        db.expire(definition, ["required_units"])
        definition.required_units = [CheckpointRequiredUnit(lesson_id=u["lesson_id"], kind=u["kind"], position=i)
                                     for i, u in enumerate(units)]
    if activate and questions and len(questions) == definition.total_questions:
        definition.is_active = True
        report["activated"] = True
    db.commit()
    return report


def run(db: Session, *, course_id: int, data: Path, weeks: List[int], remap_units: bool, activate: bool,
        dry_run: bool, shuffle_options: bool = True, write_questions: bool = True) -> List[Dict[str, Any]]:
    bank = json.loads(Path(data).read_text())
    reports = []
    for cp in bank["checkpoints"]:
        if weeks and cp["number"] not in weeks:
            continue
        if cp.get("problems"):
            raise ImportError_(f"checkpoint {cp['number']} has unresolved problems in the bank: {cp['problems'][:3]}")
        reports.append(import_checkpoint(db, course_id, cp, remap_units=remap_units, activate=activate,
                                         dry_run=dry_run, shuffle_options=shuffle_options,
                                         write_questions=write_questions))
    if reports and not dry_run:
        from src.checkpoints import service
        service._invalidate_lesson_caches()
    return reports


def main() -> None:  # pragma: no cover - operator CLI
    from src.config import SessionLocal
    ap = argparse.ArgumentParser(description="Import the SAT Checkpoints question bank")
    ap.add_argument("--course-id", type=int, required=True)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--weeks", default="", help="comma-separated checkpoint numbers (default: all in the bank)")
    ap.add_argument("--no-units", action="store_true", help="leave the definitions' required units untouched")
    ap.add_argument("--units-only", action="store_true", help="bind the required units, leave every quiz untouched")
    ap.add_argument("--activate", action="store_true")
    ap.add_argument("--keep-option-order", action="store_true",
                    help="keep the documents' A-D order (default: deal the options into a fixed per-question order)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    weeks = [int(w) for w in args.weeks.split(",") if w.strip()]
    db = SessionLocal()
    try:
        reports = run(db, course_id=args.course_id, data=args.data, weeks=weeks, remap_units=not args.no_units,
                      activate=args.activate, dry_run=args.dry_run, shuffle_options=not args.keep_option_order,
                      write_questions=not args.units_only)
    except ImportError_ as exc:
        raise SystemExit(f"error: {exc}")
    finally:
        db.close()
    for r in reports:
        d = r["by_difficulty"]
        units = ", ".join(f"{u['lesson_id']} {u['title']} ({u['kind'][0]})" for u in r["units"]) if r["units"] else "(unchanged)"
        letters = "/".join(str(r["answer_letters"][L]) for L in "ABCD")
        if r["questions_written"]:
            summary = (f"{r['questions']} questions ({r['short_answer']} short-answer), "
                       f"difficulty {d['easy']}/{d['medium']}/{d['hard']}, correct letter A/B/C/D {letters}")
        else:
            summary = "quiz untouched" + (" (the bank has no answers for it yet)" if r["answers_missing"] else " (--units-only)")
        print(f"Checkpoint {r['number']}: {summary}, {'ACTIVATED' if r['activated'] else 'inactive'}"
              f"{' [dry-run]' if r['dry_run'] else ''}\n    units: {units}")


if __name__ == "__main__":
    main()
