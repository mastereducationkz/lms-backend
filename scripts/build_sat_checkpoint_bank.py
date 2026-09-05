"""Build the SAT Checkpoints question bank from the MasterSAT weekly checkpoint documents.

    python -m scripts.build_sat_checkpoint_bank --docs docs/checkpoints_LMS \
        --out scripts/data/sat_checkpoints_questions.json [--weeks 1,2,3]

Input (per week N, in --docs):
  MasterSAT_Week_N_*Checkpoint*45Q*.docx      student version, 45 questions
  MasterSAT_Week_N_*Teacher_Key*.docx         answers, difficulty, skill, explanation

Two document generators were used, and both are understood:
  format A (week 1): "Section n: Lesson n — Verbal/Math" headings, questions as "n. text",
      "Easy — 5 questions" marker rows; key is a table "n | Difficulty | Answer | Skill | Explanation".
  format B (weeks 2+): "[Question]n. text" paragraphs (a passage may run over several lines),
      "Student-produced response" items with no options; key lines are either
      "n. Answer: X | Difficulty: D | Skill: S" or "n. X | D | Skill", each followed by an explanation.

A week without a teacher key is skipped (the import refuses questions without answers) unless
--allow-missing-key is given, in which case answers are left empty and difficulty falls back to the
document's own order (questions 1-5 of each lesson block easy, 6-10 medium, 11-15 hard).

Required units per checkpoint come from docs/SAT_Checkpoints_LMS_Mapping_IT.pdf (lesson ids of
the production SAT course); see PDF_UNITS below.
"""
import argparse
import datetime as dt
import html
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# docs/SAT_Checkpoints_LMS_Mapping_IT.pdf — "Trigger IDs" column. A course lesson can hold two LMS
# units (checkpoints 2, 3, 4), which is why some checkpoints list four.
PDF_UNITS: Dict[int, List[Tuple[int, str]]] = {
    1: [(1, "verbal"), (2, "verbal"), (7, "math")],
    2: [(5, "verbal"), (6, "verbal"), (8, "math"), (9, "math")],
    3: [(12, "verbal"), (43, "verbal"), (44, "verbal"), (10, "math")],
    4: [(45, "verbal"), (4, "verbal"), (11, "math"), (57, "math")],
    5: [(46, "verbal"), (47, "verbal"), (58, "math")],
    6: [(48, "verbal"), (49, "verbal"), (59, "math")],
    7: [(50, "verbal"), (51, "verbal"), (60, "math")],
    8: [(52, "verbal"), (53, "verbal"), (61, "math")],
    9: [(54, "verbal"), (55, "verbal"), (62, "math")],
}

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
LETTERS = "ABCD"
DIFFICULTIES = ("easy", "medium", "hard")


# ---------------------------------------------------------------- docx -> lines

def docx_lines(path: Path) -> List[str]:
    """Paragraphs and table rows of a .docx as plain lines. Table rows become '[ROW] a | b | c',
    paragraphs with a [Question] style keep a '[Question]' prefix, headings '[Heading1]'."""
    root = ET.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
    body = root.find(W + "body")

    def para(p) -> Tuple[str, str]:
        parts = []
        for node in p.iter():
            if node.tag == W + "t" and node.text:
                parts.append(node.text)
            elif node.tag == W + "tab":
                parts.append("\t")
            elif node.tag == W + "br":
                parts.append("\n")
        style = p.find(W + "pPr/" + W + "pStyle")
        sty = style.get(W + "val") if style is not None else ""
        return (f"[{sty}]" if sty else ""), "".join(parts)

    lines: List[str] = []
    for child in body:
        if child.tag == W + "p":
            # A soft line break inside a paragraph (a passage followed by its prompt, a key line
            # followed by its explanation) becomes its own line; the style tag marks the first only.
            prefix, text = para(child)
            for k, part in enumerate(text.split("\n")):
                lines.append((prefix if k == 0 else "") + part)
        elif child.tag == W + "tbl":
            for tr in child.iter(W + "tr"):
                cells = [" / ".join(para(p)[1].replace("\n", " ") for p in tc.findall(W + "p")).strip()
                         for tc in tr.findall(W + "tc")]
                lines.append("[ROW] " + " | ".join(cells))
    return lines


# ---------------------------------------------------------------- student docs

def _clean(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s.replace(" ", " ")).strip()


def parse_student_doc(lines: List[str]) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Returns (lesson titles, questions). Each question: number, text_lines, options, spr(bool)."""
    fmt_a = any(l.startswith("[Heading1]Section") for l in lines)
    lessons: List[str] = []
    questions: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None

    def flush():
        nonlocal cur
        if cur is not None:
            questions.append(cur)
        cur = None

    i = 0
    while i < len(lines):
        raw = lines[i]
        line = _clean(re.sub(r"^\[(Heading\d|Title|ListBullet|Question)\]", "", raw))
        i += 1
        if re.match(r"^(\[Heading1\])?Answer Sheet", raw):
            break
        if fmt_a:
            m = re.match(r"^\[Heading1\]Section \d+: Lesson \d+ — (Verbal|Math)", raw)
            if m:
                flush()
                lessons.append(_clean(lines[i]) if i < len(lines) else "")
                continue
        else:
            m = re.match(r"^Lesson (\d+) - (.+)$", line)
            if m and not raw.startswith("[Question]") and "|" not in line:
                flush()
                lessons.append(m.group(2).strip())
                continue
        if raw.startswith("[ROW]") or not line:
            continue
        qm = re.match(r"^\[Question\](\d+)\.\s*(.*)$", raw) if not fmt_a else re.match(r"^(\d+)\.\s+(.*)$", raw)
        if qm and not raw.startswith("[ROW]"):
            flush()
            text = _clean(qm.group(2))
            spr = False
            if text.lower().startswith("student-produced response:"):
                text = _clean(text.split(":", 1)[1])
                spr = True
            cur = {"number": int(qm.group(1)), "text_lines": [text] if text else [], "options": [], "spr": spr}
            continue
        if cur is None:
            continue
        om = re.match(r"^([A-D])\.\s+(.*)$", line)
        if om and len(cur["options"]) == LETTERS.index(om.group(1)):
            cur["options"].append(_clean(om.group(2)))
            continue
        if re.match(r"^(Student-produced response|Answer)\s*:?\s*_{3,}", line):
            cur["spr"] = True
            continue
        if not cur["options"]:
            cur["text_lines"].append(line)
        # anything after the options (stray lines) is ignored
    flush()
    return lessons, questions


# ---------------------------------------------------------------- teacher keys

def parse_key(lines: List[str]) -> Dict[int, Dict[str, Any]]:
    """number -> {answer, difficulty, skill, explanation}. Understands the three key layouts."""
    out: Dict[int, Dict[str, Any]] = {}
    cur: Optional[int] = None
    for raw in lines:
        line = _clean(re.sub(r"^\[(Heading\d|Title)\]", "", raw))
        if raw.startswith("[ROW]"):
            cells = [c.strip() for c in raw[len("[ROW]"):].split("|")]
            if len(cells) >= 5 and cells[0].isdigit() and cells[1] in ("Easy", "Medium", "Hard"):
                out[int(cells[0])] = {"answer": cells[2], "difficulty": cells[1].lower(),
                                      "skill": cells[3], "explanation": " ".join(cells[4:]).strip()}
            cur = None
            continue
        m = re.match(r"^(\d+)\.\s*(?:Answer:\s*)?(\S+)\s*\|\s*(?:Difficulty:\s*)?(Easy|Medium|Hard)\s*\|\s*(?:Skill:\s*)?(.*)$", line)
        if m:
            cur = int(m.group(1))
            out[cur] = {"answer": m.group(2).strip(), "difficulty": m.group(3).lower(),
                        "skill": m.group(4).strip(), "explanation": ""}
            continue
        if cur is not None and line and not re.match(r"^(Lesson \d+|Answer, difficulty)", line):
            out[cur]["explanation"] = (out[cur]["explanation"] + " " + line).strip()
        elif re.match(r"^Lesson \d+", line):
            cur = None
    return out


# ---------------------------------------------------------------- assembly

def _html(text: str) -> str:
    # The player renders question and option text as HTML (and treats $...$ as LaTeX), so escape
    # markup characters and neutralise literal dollar signs such as "$25 plus $18".
    return html.escape(text, quote=False).replace("$", "&#36;")


def _position_difficulty(index_in_lesson: int) -> str:
    return DIFFICULTIES[min(index_in_lesson // 5, 2)]


def build_week(number: int, student: Path, key: Optional[Path], *, allow_missing_key: bool) -> Dict[str, Any]:
    lessons, raw_questions = parse_student_doc(docx_lines(student))
    answers = parse_key(docx_lines(key)) if key else {}
    problems: List[str] = []
    if len(raw_questions) != 45:
        problems.append(f"expected 45 questions, parsed {len(raw_questions)}")
    if [q["number"] for q in raw_questions] != list(range(1, len(raw_questions) + 1)):
        problems.append("question numbering is not 1..45 in order")
    if len(lessons) != 3:
        problems.append(f"expected 3 lesson headings, found {len(lessons)}: {lessons}")
    if key is None and not allow_missing_key:
        problems.append("no teacher key")

    questions: List[Dict[str, Any]] = []
    for idx, q in enumerate(raw_questions):
        k = answers.get(q["number"], {})
        answer = k.get("answer")
        difficulty = k.get("difficulty") or _position_difficulty(idx % 15)
        source = "teacher_key" if k else ("position" if key is None else "missing")
        if q["spr"]:
            qtype = "short_answer"
            if q["options"]:
                problems.append(f"q{q['number']}: student-produced response with options")
            if answer is not None and re.fullmatch(r"[A-D]", answer):
                problems.append(f"q{q['number']}: student-produced response keyed with a letter")
        else:
            qtype = "single_choice"
            if len(q["options"]) != 4:
                problems.append(f"q{q['number']}: {len(q['options'])} options")
            if answer is not None and not re.fullmatch(r"[A-D]", answer):
                problems.append(f"q{q['number']}: choice question keyed with {answer!r}")
        if not q["text_lines"]:
            problems.append(f"q{q['number']}: empty text")
        if key is not None and not k:
            problems.append(f"q{q['number']}: no key entry")
        questions.append({
            "number": q["number"],
            "lesson_index": idx // 15,
            "type": qtype,
            "text": "<br>".join(_html(t) for t in q["text_lines"]),
            "options": [_html(o) for o in q["options"]],
            "answer": answer,
            "difficulty": difficulty,
            "skill": k.get("skill", ""),
            "explanation": _html(k.get("explanation", "")),
            "answer_source": source,
        })

    per_lesson = {}
    for q in questions:
        per_lesson.setdefault(q["lesson_index"], {"easy": 0, "medium": 0, "hard": 0})[q["difficulty"]] += 1
    for li, counts in per_lesson.items():
        if any(counts[d] != 5 for d in DIFFICULTIES):
            problems.append(f"lesson block {li + 1} difficulty split {counts} (expected 5/5/5)")

    return {
        "number": number,
        "title": f"Checkpoint {number}",
        "lessons": lessons,
        "units": [{"lesson_id": lid, "kind": kind} for lid, kind in PDF_UNITS[number]],
        "source": {"student": student.name, "key": key.name if key else None},
        "answers_complete": all(q["answer"] is not None for q in questions),
        "questions": questions,
        "problems": problems,
    }


def find_docs(docs: Path, week: int) -> Tuple[Optional[Path], Optional[Path]]:
    student = key = None
    for f in sorted(docs.glob(f"MasterSAT_Week_{week}_*.docx")):
        if "Teacher_Key" in f.name:
            key = f
        elif "45Q" in f.name:
            student = f
    return student, key


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--docs", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--weeks", default="1,2,3,4,5,6,7,8,9")
    ap.add_argument("--allow-missing-key", action="store_true")
    args = ap.parse_args()

    bank = {"built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "source_dir": str(args.docs), "checkpoints": []}
    exit_code = 0
    for week in [int(w) for w in args.weeks.split(",") if w.strip()]:
        student, key = find_docs(args.docs, week)
        if student is None:
            print(f"week {week}: no student document, skipped")
            continue
        if key is None and not args.allow_missing_key:
            print(f"week {week}: no teacher key, skipped (use --allow-missing-key to include without answers)")
            continue
        cp = build_week(week, student, key, allow_missing_key=args.allow_missing_key)
        qs = cp["questions"]
        split = {d: sum(1 for q in qs if q["difficulty"] == d) for d in DIFFICULTIES}
        spr = sum(1 for q in qs if q["type"] == "short_answer")
        keyed = sum(1 for q in qs if q["answer"] is not None)
        status = "OK" if not cp["problems"] else "PROBLEMS"
        print(f"week {week}: {len(qs)} questions ({spr} student-produced), answers {keyed}/45, "
              f"difficulty {split['easy']}/{split['medium']}/{split['hard']}, units {[u['lesson_id'] for u in cp['units']]} — {status}")
        for p in cp["problems"]:
            print(f"    ! {p}")
            exit_code = 1
        bank["checkpoints"].append(cp)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(bank, ensure_ascii=False, indent=1) + "\n")
    print(f"wrote {args.out} ({len(bank['checkpoints'])} checkpoints)")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
