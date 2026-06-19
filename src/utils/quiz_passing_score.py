import json
from typing import Optional

DEFAULT_QUIZ_PASSING_SCORE_REQUIRED = 50.0
DEFAULT_QUIZ_PASSING_SCORE_OPTIONAL = 30.0


def resolve_quiz_passing_score_percent(
    content_text: Optional[str],
    *,
    is_optional: bool = False,
) -> float:
    if content_text:
        try:
            data = json.loads(content_text)
            raw = data.get("passing_score_percent")
            if raw is not None:
                return max(0.0, min(100.0, float(raw)))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    if is_optional:
        return DEFAULT_QUIZ_PASSING_SCORE_OPTIONAL
    return DEFAULT_QUIZ_PASSING_SCORE_REQUIRED
