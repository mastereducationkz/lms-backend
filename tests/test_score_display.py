"""Lead's display rule for SAT scaled ESTIMATES: staff see correct/total only; students and
parents see scaled + correct/total with the disclaimer."""

from src.integrations.score_display import ESTIMATE_NOTE, sanitize_sat_scores

RAW = {
    "scaledNote": "Scaled scores are ESTIMATES predicted from raw correct counts.",
    "testResults": [
        {"mathTest": {"correctCount": 8, "totalQuestions": 22, "percentage": 36.4, "scaledScoreEstimate": 450},
         "verbalTest": {"correctCount": 8, "totalQuestions": 27, "percentage": 29.6, "scaledScoreEstimate": 390},
         "scaledMathEstimate": 450, "scaledVerbalEstimate": 390, "scaledTotalEstimate": 840, "scaledNote": "…"},
    ],
    "testPairs": [{"scaledTotalEstimate": 840, "mathTest": {"scaledScoreEstimate": 450, "correctCount": 8}}],
}


def test_staff_never_see_scaled_estimates():
    for role in ("curator", "teacher", "admin", "head_curator", "head_teacher"):
        out = sanitize_sat_scores(RAW, role)
        flat = str(out)
        assert "scaled" not in flat.lower() and "Scaled" not in flat, role
        assert out["testResults"][0]["mathTest"]["correctCount"] == 8
        assert out["testResults"][0]["verbalTest"]["totalQuestions"] == 27
        assert "scaled_note" not in out


def test_students_and_parents_see_estimates_with_the_disclaimer():
    for role in ("student", "parent"):
        out = sanitize_sat_scores(RAW, role)
        assert out["testResults"][0]["scaledMathEstimate"] == 450
        assert out["testResults"][0]["mathTest"]["correctCount"] == 8
        assert out["scaled_note"] == ESTIMATE_NOTE and out["scaled_note"].startswith("Scaled scores are estimates")


def test_sanitize_does_not_mutate_the_input():
    sanitize_sat_scores(RAW, "curator")
    assert RAW["testResults"][0]["scaledMathEstimate"] == 450
