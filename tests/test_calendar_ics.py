"""The ICS text: what Apple Calendar and Outlook will actually parse."""
from datetime import date, datetime

from src.services.calendar_ics import CalendarItem, build, escape_text, fold, sequence

NOW = datetime(2026, 9, 15, 6, 0)


def _unfold(text: str) -> list[str]:
    return text.replace("\r\n ", "").split("\r\n")


def test_a_calendar_has_the_headers_that_make_clients_refresh_it():
    text = build("IELTS July 8 2026 - Саид", [], NOW)
    lines = _unfold(text)
    assert lines[0] == "BEGIN:VCALENDAR" and "END:VCALENDAR" in lines
    assert "REFRESH-INTERVAL;VALUE=DURATION:PT15M" in lines and "X-PUBLISHED-TTL:PT15M" in lines
    assert "X-WR-CALNAME:IELTS July 8 2026 - Саид" in lines
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")


def test_a_lesson_is_a_timed_utc_event_with_a_stable_uid():
    item = CalendarItem(key="lesson-18844", summary="IELTS July 8: урок", start=datetime(2026, 9, 15, 12, 0),
                        end=datetime(2026, 9, 15, 13, 0), url="https://lms.mastereducation.kz/calendar?event=18844",
                        updated=datetime(2026, 9, 14, 10, 0))
    lines = _unfold(build("G", [item], NOW))
    assert "UID:lesson-18844@lms.mastereducation.kz" in lines
    assert "DTSTART:20260915T120000Z" in lines and "DTEND:20260915T130000Z" in lines
    assert "URL:https://lms.mastereducation.kz/calendar?event=18844" in lines
    assert f"SEQUENCE:{sequence(datetime(2026, 9, 14, 10, 0))}" in lines
    assert build("G", [item], NOW) == build("G", [item], NOW), "same input, same feed"


def test_a_deadline_is_an_all_day_entry_on_its_date():
    item = CalendarItem(key="deadline-7", summary="📝 Дедлайн: Essay до 23:59", day=date(2026, 9, 16))
    lines = _unfold(build("G", [item], NOW))
    assert "DTSTART;VALUE=DATE:20260916" in lines and "DTEND;VALUE=DATE:20260917" in lines
    assert "TRANSP:TRANSPARENT" in lines


def test_text_is_escaped():
    assert escape_text("a;b,c\\d\nnext") == "a\\;b\\,c\\\\d\\nnext"


def test_long_cyrillic_lines_fold_without_breaking_characters():
    line = "SUMMARY:" + "Расписание группы " * 10
    folded = fold(line)
    for part in folded.split("\r\n"):
        assert len(part.encode("utf-8")) <= 75
        part.encode("utf-8").decode("utf-8")
    assert folded.replace("\r\n ", "") == line


def test_staff_typed_titles_lose_their_stray_spaces():
    """Seen in the first real group calendar (2026-09-15): « Maps » gave «Дедлайн:  Maps  до 17:00»."""
    from types import SimpleNamespace

    from src.services.calendar_items import deadline_item, weekly_item

    task = SimpleNamespace(id=6974, title="  Maps \n", due_date=datetime(2026, 9, 8, 12, 0), updated_at=None)
    assert deadline_item(task).summary == "📝 Дедлайн: Maps до 17:00"
    weekly = SimpleNamespace(id=1, title=" IELTS  Weekly Test ", meeting_url=None,
                             start_datetime=NOW, end_datetime=NOW, updated_at=None)
    assert weekly_item(weekly).summary == "IELTS Weekly Test"


def test_sequence_grows_with_the_update_time():
    assert sequence(None) == 0
    assert sequence(datetime(2026, 9, 15, 10, 1)) > sequence(datetime(2026, 9, 15, 10, 0))
