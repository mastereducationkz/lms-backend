"""
Unit tests for the NUET parser's AI-independent post-processing:
- 5-option critical-thinking question normalizes to letters A-E with correct is_correct
- 4-option math question keeps A-D
- markdown-fenced JSON is stripped and parsed
- garbage (non-JSON) raises

The live OpenAI call (parse_file) is NOT tested here (external dependency).
"""
import pytest

from src.services.nuet_parser import NuetParser


def test_process_five_option_question_letters_a_to_e():
    p = NuetParser()
    raw = [{
        "question_text": "Which is an assumption?",
        "question_type": "single_choice",
        "options": ["opt a", "opt b", "opt c", "opt d", "opt e"],
        "correct_answer": 4,
        "content_text": "A long argument passage.",
    }]
    out = p._process_questions(raw)
    assert len(out) == 1
    opts = out[0]["options"]
    assert [o["letter"] for o in opts] == ["A", "B", "C", "D", "E"]
    assert [o["is_correct"] for o in opts] == [False, False, False, False, True]
    assert out[0]["correct_answer"] == 4
    assert out[0]["content_text"] == "A long argument passage."


def test_process_four_option_math_question_letters_a_to_d():
    p = NuetParser()
    raw = [{
        "question_text": "Solve $x^2 = 4$",
        "question_type": "single_choice",
        "options": ["$x=1$", "$x=2$", "$x=3$", "$x=4$"],
        "correct_answer": 1,
    }]
    out = p._process_questions(raw)
    opts = out[0]["options"]
    assert [o["letter"] for o in opts] == ["A", "B", "C", "D"]
    assert opts[1]["is_correct"] is True
    assert out[0]["needs_image"] is False


def test_extract_json_strips_markdown_fence():
    p = NuetParser()
    text = '```json\n[{"question_text": "hi", "question_type": "single_choice", "options": ["a","b"], "correct_answer": 0}]\n```'
    parsed = p._extract_json(text)
    assert isinstance(parsed, list)
    assert parsed[0]["question_text"] == "hi"


def test_extract_json_raises_on_garbage():
    p = NuetParser()
    with pytest.raises(Exception):
        p._extract_json("this is not json at all {[}")
