"""The payslip answers with the same shape whether or not there were lessons.

An empty period used to return early with its own hand-written dict, which spelled
`total_amount_tenge` as `total_amount` and `message_text` as `message`. The teacher dashboard
reads `total_amount_tenge`, got `undefined`, and crashed the whole page on
`.toLocaleString()` — for the one case the empty branch existed to handle.

Pinned here by comparing the two shapes directly, so a future divergence fails a test rather
than a browser.
"""
from __future__ import annotations

import inspect
import re

from src.admin.routes import dashboard


def _returned_key_sets(func) -> list[set[str]]:
    """Every `return {...}` literal's top-level keys, in source order."""
    source = inspect.getsource(func)
    return [
        set(re.findall(r'"([a-z_]+)":', block))
        for block in re.findall(r"return \{(.*?)\n    \}", source, re.S)
    ]


def test_the_payslip_has_exactly_one_exit():
    """Two exits is how the shapes drifted. One cannot."""
    returns = _returned_key_sets(dashboard.get_teacher_salary_breakdown)

    assert len(returns) == 1, (
        f"expected a single return, found {len(returns)} — the empty-period branch must fall "
        "through to the same one"
    )


def test_the_response_carries_the_keys_the_dashboard_reads():
    """The names the LMS frontend actually indexes. `total_amount` is not one of them."""
    (keys,) = _returned_key_sets(dashboard.get_teacher_salary_breakdown)

    for required in (
        "teacher_id",
        "teacher_name",
        "period_start",
        "period_end",
        "lesson_rate",
        "individual_rate",
        "rate_source",
        "level",
        "group_band",
        "groups",
        "total_lessons",
        "total_amount_tenge",
        "message_text",
        "contacts",
    ):
        assert required in keys, f"the response lost {required!r}"


def test_the_names_that_caused_the_crash_are_not_used():
    (keys,) = _returned_key_sets(dashboard.get_teacher_salary_breakdown)

    assert "total_amount" not in keys, "the dashboard reads total_amount_tenge"
    assert "message" not in keys, "the dashboard reads message_text"
