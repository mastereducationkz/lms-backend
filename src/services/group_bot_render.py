"""The bot's answers in a group chat, word for word, in Russian, Kazakh and English.

Every answer is a template filled from the group's own facts — the model never writes a sentence
here, so a date, a link or a title in an answer is always one that is in the LMS. Every answer
opens with the group's name (owner, 2026-09-15): one person is often in several chats, and a
forwarded answer must still say which group it is about.

Telegram HTML: everything dynamic goes through :func:`html.escape`.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from html import escape, unescape
from typing import Iterable, Optional, Sequence

ALMATY_OFFSET = timedelta(hours=5)      # Kazakhstan has no DST; the schedule generator uses +5 too

WEEKDAYS = {
    "ru": ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"),
    "kk": ("Дс", "Сс", "Ср", "Бс", "Жм", "Сн", "Жс"),
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
}
MONTHS = {
    "ru": ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
           "октября", "ноября", "декабря"),
    "kk": ("қаңтар", "ақпан", "наурыз", "сәуір", "мамыр", "маусым", "шілде", "тамыз", "қыркүйек",
           "қазан", "қараша", "желтоқсан"),
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
}

TEXT = {
    "ru": {
        "today": "Сегодня", "tomorrow": "Завтра",
        "schedule": "🗓 Расписание группы (время Алматы):",
        "changes": "⚠️ Изменения в ближайшие 7 дней:",
        "missing": "{day} — урока не будет",
        "moved": "{day} — {new} вместо {old}",
        "extra": "{day}, {time} — дополнительный урок",
        "first": "Первый урок: {when}",
        "more": "Ближайшие уроки с датами — /lessons",
        "finished": "Курс группы завершён — уроков больше нет.",
        "no_schedule": "Расписание группы пока не заполнено.",
        "lessons": "📅 Ближайшие уроки (время Алматы):",
        "span_today": "📅 Уроки сегодня (время Алматы):",
        "span_tomorrow": "📅 Уроки завтра (время Алматы):",
        "span_weekend": "📅 Уроки на выходных (время Алматы):",
        "empty_weekend": "На выходных уроков нет.",
        "span_this_week": "📅 Уроки на этой неделе (время Алматы):",
        "span_next_week": "📅 Уроки на следующей неделе (время Алматы):",
        "span_week": "📅 Уроки в ближайшие 7 дней (время Алматы):",
        "empty_today": "Сегодня уроков нет.", "empty_tomorrow": "Завтра уроков нет.",
        "empty_this_week": "На этой неделе уроков больше нет.",
        "empty_next_week": "На следующей неделе уроков нет.",
        "empty_week": "В ближайшие 7 дней уроков нет.",
        "next": "⏭ Следующий урок: {when} (время Алматы)",
        "now": "🟢 Урок идёт сейчас: {when} (время Алматы)",
        "topic": "Тема: {topic}",
        "no_upcoming": "Ближайших уроков в расписании пока нет.",
        "homework": "📝 Домашние задания:",
        "due": "до {date}", "overdue": "срок прошёл {date}", "no_due": "без срока",
        "open": "Открыть в LMS: {url}",
        "no_homework": "Открытых домашних заданий сейчас нет.",
        "recordings": "🎥 Записи уроков (нужен вход в LMS):",
        "no_recordings": "Записей уроков пока нет.",
        "weekly": "🧪 Weekly mock:",
        "weekly_live": "идёт сейчас", "weekly_done": "завершён", "weekly_open_until": "открыт до {date}",
        "no_weekly": "Ближайший weekly mock для группы пока не опубликован.",
        "help": ("Отвечаю на вопросы по группе:\n"
                 "/schedule — расписание: дни и время\n/lessons — ближайшие уроки с датами\n"
                 "/next — следующий урок и ссылка\n/weekly — weekly mock\n"
                 "/homework — домашние задания\n/recording — записи уроков\n"
                 "Можно и словами: «когда следующий урок?». По личным вопросам напишите мне в личные сообщения."),
        "private": ("Это личный вопрос — по баллам, оплате и своим заданиям напишите мне "
                    "в личные сообщения, там отвечу."),
        "curator": "Не могу ответить на это здесь — передал куратору группы, с вами свяжутся.",
        "no_curator": "Не могу ответить на это здесь — спросите преподавателя или куратора группы.",
        "not_live": "Бот скоро заработает в этой группе. Пока расписание, задания и записи — в LMS: {url}",
    },
    "kk": {
        "today": "Бүгін", "tomorrow": "Ертең",
        "schedule": "🗓 Топтың сабақ кестесі (Алматы уақыты):",
        "changes": "⚠️ Алдағы 7 күндегі өзгерістер:",
        "missing": "{day} — сабақ болмайды",
        "moved": "{day} — {old} орнына {new}",
        "extra": "{day}, {time} — қосымша сабақ",
        "first": "Алғашқы сабақ: {when}",
        "more": "Жақын сабақтардың күндері — /lessons",
        "finished": "Топтың курсы аяқталды — сабақтар енді жоқ.",
        "no_schedule": "Топтың кестесі әлі толтырылмаған.",
        "lessons": "📅 Жақын сабақтар (Алматы уақыты):",
        "span_today": "📅 Бүгінгі сабақтар (Алматы уақыты):",
        "span_tomorrow": "📅 Ертеңгі сабақтар (Алматы уақыты):",
        "span_weekend": "📅 Демалыс күндердегі сабақтар (Алматы уақыты):",
        "empty_weekend": "Демалыс күндері сабақ жоқ.",
        "span_this_week": "📅 Осы аптадағы сабақтар (Алматы уақыты):",
        "span_next_week": "📅 Келесі аптадағы сабақтар (Алматы уақыты):",
        "span_week": "📅 Алдағы 7 күндегі сабақтар (Алматы уақыты):",
        "empty_today": "Бүгін сабақ жоқ.", "empty_tomorrow": "Ертең сабақ жоқ.",
        "empty_this_week": "Осы аптада басқа сабақ жоқ.",
        "empty_next_week": "Келесі аптада сабақ жоқ.",
        "empty_week": "Алдағы 7 күнде сабақ жоқ.",
        "next": "⏭ Келесі сабақ: {when} (Алматы уақыты)",
        "now": "🟢 Сабақ қазір жүріп жатыр: {when} (Алматы уақыты)",
        "topic": "Тақырып: {topic}",
        "no_upcoming": "Кестеде жақын арада сабақ жоқ.",
        "homework": "📝 Үй тапсырмалары:",
        "due": "мерзімі {date}", "overdue": "мерзімі өтті: {date}", "no_due": "мерзімсіз",
        "open": "LMS-те ашу: {url}",
        "no_homework": "Қазір ашық үй тапсырмасы жоқ.",
        "recordings": "🎥 Сабақ жазбалары (LMS-ке кіру керек):",
        "no_recordings": "Сабақ жазбалары әзірге жоқ.",
        "weekly": "🧪 Weekly mock:",
        "weekly_live": "қазір жүріп жатыр", "weekly_done": "аяқталды", "weekly_open_until": "{date} дейін ашық",
        "no_weekly": "Топ үшін жақын weekly mock әлі жарияланбаған.",
        "help": ("Топ бойынша сұрақтарға жауап беремін:\n"
                 "/schedule — сабақ кестесі: күндер мен уақыт\n/lessons — жақын сабақтар\n"
                 "/next — келесі сабақ және сілтеме\n/weekly — weekly mock\n"
                 "/homework — үй тапсырмалары\n/recording — сабақ жазбалары\n"
                 "Сөзбен де сұрауға болады: «келесі сабақ қашан?». Жеке сұрақтар бойынша маған жеке жазыңыз."),
        "private": "Бұл жеке сұрақ — баға, төлем және өз тапсырмаларыңыз бойынша маған жеке хабарлама жазыңыз.",
        "curator": "Бұл сұраққа мұнда жауап бере алмаймын — топ кураторына жібердім, сізбен байланысады.",
        "no_curator": "Бұл сұраққа мұнда жауап бере алмаймын — топтың мұғаліміне немесе кураторына жазыңыз.",
        "not_live": "Бот бұл топта жақында іске қосылады. Әзірге кесте, тапсырмалар мен жазбалар — LMS-те: {url}",
    },
    "en": {
        "today": "Today", "tomorrow": "Tomorrow",
        "schedule": "🗓 Group schedule (Almaty time):",
        "changes": "⚠️ Changes in the next 7 days:",
        "missing": "{day} — no lesson",
        "moved": "{day} — {new} instead of {old}",
        "extra": "{day}, {time} — extra lesson",
        "first": "First lesson: {when}",
        "more": "Upcoming lesson dates — /lessons",
        "finished": "This group's course has finished — there are no more lessons.",
        "no_schedule": "The group's schedule has not been set yet.",
        "lessons": "📅 Upcoming lessons (Almaty time):",
        "span_today": "📅 Lessons today (Almaty time):",
        "span_tomorrow": "📅 Lessons tomorrow (Almaty time):",
        "span_weekend": "📅 Lessons this weekend (Almaty time):",
        "empty_weekend": "No lessons this weekend.",
        "span_this_week": "📅 Lessons this week (Almaty time):",
        "span_next_week": "📅 Lessons next week (Almaty time):",
        "span_week": "📅 Lessons in the next 7 days (Almaty time):",
        "empty_today": "No lessons today.", "empty_tomorrow": "No lessons tomorrow.",
        "empty_this_week": "No more lessons this week.",
        "empty_next_week": "No lessons next week.",
        "empty_week": "No lessons in the next 7 days.",
        "next": "⏭ Next lesson: {when} (Almaty time)",
        "now": "🟢 Lesson in progress: {when} (Almaty time)",
        "topic": "Topic: {topic}",
        "no_upcoming": "There are no upcoming lessons in the schedule yet.",
        "homework": "📝 Homework:",
        "due": "due {date}", "overdue": "overdue since {date}", "no_due": "no deadline",
        "open": "Open in the LMS: {url}",
        "no_homework": "There is no open homework right now.",
        "recordings": "🎥 Lesson recordings (LMS login required):",
        "no_recordings": "There are no lesson recordings yet.",
        "weekly": "🧪 Weekly mock:",
        "weekly_live": "live now", "weekly_done": "finished", "weekly_open_until": "open until {date}",
        "no_weekly": "The group's next weekly mock has not been published yet.",
        "help": ("I answer questions about this group:\n"
                 "/schedule — schedule: days and times\n/lessons — upcoming lesson dates\n"
                 "/next — next lesson and link\n/weekly — weekly mock\n"
                 "/homework — homework\n/recording — lesson recordings\n"
                 "You can also just ask: “when is the next lesson?”. For personal questions, message me privately."),
        "private": "That's a personal question — for marks, payments and your own work, message me privately.",
        "curator": "I can't answer that here — I've passed it to the group's curator, they will get back to you.",
        "no_curator": "I can't answer that here — please ask the group's teacher or curator.",
        "not_live": "The bot will start working in this group soon. Meanwhile the schedule, homework and recordings are in the LMS: {url}",
    },
}

_KK_LETTERS = set("әіңғүұқөһ")
_KK_WORDS = ("сабак", "кашан", "кесте", "бугин", "ертен", "келеси", "тапсырма", "рахмет", "бар ма")


def language(text: Optional[str]) -> str:
    t = (text or "").casefold()
    if any(ch in _KK_LETTERS for ch in t) or any(word in t for word in _KK_WORDS):
        return "kk"
    if any("a" <= ch <= "z" for ch in t) and not any("а" <= ch <= "я" or ch == "ё" for ch in t):
        return "en"
    return "ru"


def t(lang: str, key: str, **values) -> str:
    template = TEXT.get(lang, TEXT["ru"])[key]
    return template.format(**values) if values else template


def local(value: datetime) -> datetime:
    """Naive UTC (as stored) → naive Almaty wall-clock time."""
    if value.tzinfo is not None:
        value = value.replace(tzinfo=None) - value.utcoffset()
    return value + ALMATY_OFFSET


_LINK = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.DOTALL)


def to_plain(text: str) -> str:
    """The same answer for a caller that cannot post HTML: a hyperlink becomes «label: url», other
    tags are dropped, entities decoded — a link must survive, not vanish with its tag."""
    text = _LINK.sub(lambda match: f"{match.group(2)}: {match.group(1)}", text)
    return unescape(re.sub(r"</?b>", "", text))


def header(group_name: str) -> str:
    return f"📚 <b>{escape(group_name or '')}</b>"


def join(*blocks: Optional[str]) -> str:
    return "\n".join(block for block in blocks if block)


def day_label(day: date, today: date, lang: str) -> str:
    months = MONTHS.get(lang, MONTHS["ru"])
    dm = f"{months[day.month - 1]} {day.day}" if lang == "en" else f"{day.day} {months[day.month - 1]}"
    if day == today:
        return f"{t(lang, 'today')}, {dm}"
    if day == today + timedelta(days=1):
        return f"{t(lang, 'tomorrow')}, {dm}"
    return f"{WEEKDAYS.get(lang, WEEKDAYS['ru'])[day.weekday()]}, {dm}"


def when(start: datetime, end: Optional[datetime], now: datetime, lang: str) -> str:
    s, today = local(start), local(now).date()
    text = f"{day_label(s.date(), today, lang)}, {s:%H:%M}"
    return f"{text}–{local(end):%H:%M}" if end is not None else text


# ── the weekly timetable ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True, order=True)
class Slot:
    weekday: int        # Monday = 0, Almaty
    start: time
    minutes: int = 60

    @property
    def key(self) -> tuple[int, time]:
        return self.weekday, self.start

    def hours(self) -> str:
        end = (datetime.combine(date(2000, 1, 3), self.start) + timedelta(minutes=self.minutes)).time()
        return f"{self.start:%H:%M}–{end:%H:%M}"


def config_slots(schedule_config) -> list[Slot]:
    config = schedule_config if isinstance(schedule_config, dict) else {}
    slots = set()
    for item in config.get("schedule_items") or []:
        try:
            weekday = int(item.get("day_of_week"))
            start = datetime.strptime(str(item.get("time_of_day")), "%H:%M").time()
            minutes = int(item.get("duration_minutes") or 60)
        except (AttributeError, TypeError, ValueError):
            continue
        if 0 <= weekday <= 6 and minutes > 0:
            slots.add(Slot(weekday, start, minutes))
    return sorted(slots)


def event_slot(event) -> Slot:
    s = local(event.start_datetime)
    minutes = max(1, int((event.end_datetime - event.start_datetime).total_seconds() // 60))
    return Slot(s.weekday(), s.time().replace(second=0, microsecond=0), minutes)


def weekly_pattern(schedule_config, upcoming: Sequence, now: datetime) -> list[Slot]:
    """The group's regular week. The generated plan (``schedule_config``) is the source unless
    the lessons actually on the calendar have clearly moved away from it — then the calendar
    wins, because that is where the students will be."""
    planned = config_slots(schedule_config)
    soon = [e for e in upcoming if e.start_datetime < now + timedelta(days=21)]
    if planned and len(soon) >= 3:
        keys = {slot.key for slot in planned}
        if sum(event_slot(e).key in keys for e in soon) * 2 < len(soon):
            planned = []
    if planned:
        return planned
    counts = Counter(event_slot(e) for e in upcoming if e.start_datetime < now + timedelta(days=28))
    regular = [slot for slot, count in counts.items() if count >= 2]
    return sorted(regular or counts)


def _weekday_list(days: Iterable[int], lang: str) -> str:
    names = WEEKDAYS.get(lang, WEEKDAYS["ru"])
    days = sorted(set(days))
    runs, run = [], [days[0]]
    for day in days[1:]:
        if day == run[-1] + 1:
            run.append(day)
        else:
            runs.append(run)
            run = [day]
    runs.append(run)
    parts = []
    for run in runs:
        if len(run) >= 3:
            parts.append(f"{names[run[0]]}–{names[run[-1]]}")
        else:
            parts.extend(names[day] for day in run)
    return ", ".join(parts)


def pattern_lines(slots: Sequence[Slot], lang: str) -> list[str]:
    by_hours: dict[tuple[time, int], list[int]] = {}
    for slot in slots:
        by_hours.setdefault((slot.start, slot.minutes), []).append(slot.weekday)
    rows = sorted(by_hours.items(), key=lambda item: (min(item[1]), item[0]))
    return [f"• {_weekday_list(days, lang)} — {Slot(0, start, minutes).hours()}" for (start, minutes), days in rows]


def week_changes(slots: Sequence[Slot], upcoming: Sequence, now: datetime, lang: str,
                 start_date: Optional[date] = None, days: int = 7) -> list[str]:
    """Where the next ``days`` differ from the regular week: a slot with no lesson, a lesson at
    another time the same day, a lesson outside the pattern. A missing slot is only reported
    while the course goes on after it — the week after the last lesson is not «no lesson»."""
    now_local = local(now)
    today = now_local.date()
    future = [e for e in upcoming if e.start_datetime > now]
    if not slots or not future:
        return []
    last_day = local(max(e.start_datetime for e in future)).date()
    first_day = start_date or local(min(e.start_datetime for e in future)).date()
    lines = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        expected = [s for s in slots if s.weekday == day.weekday()
                    and datetime.combine(day, s.start) > now_local]
        actual = [e for e in future if local(e.start_datetime).date() == day]
        actual_keys = {event_slot(e).key for e in actual}
        missing = [s for s in expected if s.key not in actual_keys]
        expected_keys = {s.key for s in slots if s.weekday == day.weekday()}
        extra = [e for e in actual if event_slot(e).key not in expected_keys]
        label = day_label(day, today, lang)
        if len(missing) == 1 and len(extra) == 1:
            moved = event_slot(extra[0])
            lines.append("• " + t(lang, "moved", day=label, new=moved.hours(), old=missing[0].start.strftime("%H:%M")))
            continue
        if first_day <= day < last_day:
            lines.extend("• " + t(lang, "missing", day=f"{label}, {s.start:%H:%M}") for s in missing)
        lines.extend("• " + t(lang, "extra", day=label, time=event_slot(e).hours()) for e in extra)
    return lines


# ── answers ────────────────────────────────────────────────────────────────────────────────

def schedule_answer(group, upcoming: Sequence, now: datetime, lang: str) -> str:
    if group.is_over and not upcoming:
        return join(header(group.name), t(lang, "finished"))
    slots = weekly_pattern(group.schedule_config, upcoming, now)
    if not slots:
        return join(header(group.name), t(lang, "no_schedule"))
    config = group.schedule_config if isinstance(group.schedule_config, dict) else {}
    try:
        start_date = date.fromisoformat(str(config.get("start_date")))
    except ValueError:
        start_date = None
    changes = week_changes(slots, upcoming, now, lang, start_date)
    first = None
    if upcoming and local(upcoming[0].start_datetime).date() > local(now).date() + timedelta(days=6):
        first = t(lang, "first", when=when(upcoming[0].start_datetime, upcoming[0].end_datetime, now, lang))
    return join(header(group.name), t(lang, "schedule"), *pattern_lines(slots, lang), first,
                "\n" + t(lang, "changes") if changes else None, *changes,
                "\n" + t(lang, "more") if upcoming else None)


def _lesson_lines(lessons: Sequence, now: datetime, lang: str) -> list[str]:
    lines = []
    for number, lesson in enumerate(lessons, start=1):
        lines.append(f"{number}. {escape(when(lesson.start_datetime, lesson.end_datetime, now, lang))}")
        if lesson.topic:
            lines.append(f"   {t(lang, 'topic', topic=escape(lesson.topic))}")
        if lesson.meeting_url:
            lines.append(f"   🔗 {escape(lesson.meeting_url)}")
    return lines


def next_line(lesson, now: datetime, lang: str) -> str:
    key = "now" if lesson.start_datetime <= now else "next"
    lines = [t(lang, key, when=escape(when(lesson.start_datetime, lesson.end_datetime, now, lang)))]
    if lesson.topic:
        lines.append(t(lang, "topic", topic=escape(lesson.topic)))
    if lesson.meeting_url:
        lines.append(f"🔗 {escape(lesson.meeting_url)}")
    return "\n".join(lines)


def lessons_answer(group, lessons: Sequence, span: Optional[str], following, now: datetime, lang: str) -> str:
    """``lessons`` are the ones in the span (or the next few); ``following`` is the first lesson
    after an empty span, so «сегодня урока нет» still says when the next one is."""
    if not lessons:
        if following is None:
            return join(header(group.name), t(lang, "finished" if group.is_over else "no_upcoming"))
        empty = t(lang, f"empty_{span}") if span else t(lang, "no_upcoming")
        return join(header(group.name), empty, next_line(following, now, lang))
    title = t(lang, f"span_{span}") if span else t(lang, "lessons")
    return join(header(group.name), title, *_lesson_lines(lessons, now, lang))


def next_answer(group, lesson, now: datetime, lang: str) -> str:
    if lesson is None:
        return join(header(group.name), t(lang, "finished" if group.is_over else "no_upcoming"))
    return join(header(group.name), next_line(lesson, now, lang))


def deadline_label(value: datetime, now: datetime, lang: str) -> str:
    """«15 сентября, 23:59 (сегодня)» — a date inside a sentence, never «до Сегодня»."""
    d, today = local(value), local(now).date()
    months = MONTHS.get(lang, MONTHS["ru"])
    text = f"{months[d.month - 1]} {d.day}" if lang == "en" else f"{d.day} {months[d.month - 1]}"
    text = f"{text}, {d:%H:%M}"
    if d.date() == today:
        return f"{text} ({t(lang, 'today').lower()})"
    if d.date() == today + timedelta(days=1):
        return f"{text} ({t(lang, 'tomorrow').lower()})"
    return text


def task_title(task, homework_url: str) -> str:
    """A homework title that opens that very assignment in the LMS (owner, 2026-09-15: «there is no
    link to the exact homework»). ``homework_url`` is the LMS's ``/homework`` page."""
    return f'<a href="{escape(homework_url.rstrip("/"))}/{int(task.id)}"><b>{escape(task.title or "")}</b></a>'


def homework_answer(group, tasks: Sequence, now: datetime, lang: str, url: str) -> str:
    """Open tasks first (soonest deadline on top), then the ones whose deadline just passed."""
    if not tasks:
        return join(header(group.name), t(lang, "no_homework"))
    late = [task for task in tasks if task.due_date is not None and task.due_date < now]
    ordered = [task for task in tasks if task not in late] + late
    lines = []
    for task in ordered[:HOMEWORK_LINES]:
        if task.due_date is None:
            deadline = t(lang, "no_due")
        else:
            key = "overdue" if task.due_date < now else "due"
            deadline = t(lang, key, date=deadline_label(task.due_date, now, lang))
        lines.append(f"• {task_title(task, url)} — {escape(deadline)}")
    return join(header(group.name), t(lang, "homework"), *lines, t(lang, "open", url=escape(url)))


HOMEWORK_LINES = 5


def recordings_answer(group, recordings: Sequence[tuple], now: datetime, lang: str) -> str:
    """``recordings`` — (event, LMS link) pairs, newest first."""
    if not recordings:
        return join(header(group.name), t(lang, "no_recordings"))
    lines = [f"• {escape(when(event.start_datetime, None, now, lang))} — {escape(link)}"
             for event, link in recordings]
    return join(header(group.name), t(lang, "recordings"), *lines)


def weekly_answer(group, tests: Sequence, now: datetime, lang: str) -> str:
    """Live and upcoming mocks first with when they open; a finished one (kept a few days,
    because the weekend set is asked about all week) says it is over."""
    if not tests:
        return join(header(group.name), t(lang, "no_weekly"))
    finished = [test for test in tests if test.end_datetime is not None and test.end_datetime < now]
    lines = []
    for test in [test for test in tests if test not in finished] + finished:
        if test in finished:
            status = t(lang, "weekly_done")
        elif test.start_datetime <= now and test.end_datetime is not None:
            # A set stays open for a week; "until when" is what a student needs.
            status = t(lang, "weekly_open_until", date=deadline_label(test.end_datetime, now, lang))
        elif test.start_datetime <= now:
            status = t(lang, "weekly_live")
        else:
            status = when(test.start_datetime, test.end_datetime, now, lang)
        lines.append(f"• {escape(test.title or '')} — {escape(status)}")
        if test.meeting_url and test not in finished:
            lines.append(f"  🔗 {escape(test.meeting_url)}")
    return join(header(group.name), t(lang, "weekly"), *lines)


def plain(group, key: str, lang: str, **values) -> str:
    return join(header(group.name), t(lang, key, **{k: escape(str(v)) for k, v in values.items()}))
