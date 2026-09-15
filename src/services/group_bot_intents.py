"""What a message in a group chat is asking the bot for.

Rules decide nearly everything, deterministically and for free; a very small model is asked for
ONE label only when the rules see a lesson question they cannot place ("когда у нас уроки?" — the
weekly timetable or the next dates?) or a question with no topic they know. The model is given the
question and, for a reply, the first line of the bot message it replies to. It never sees the
group's data and never writes the answer, so whatever a student types can at worst pick the wrong
one of our own answers.

Owner, 2026-09-15: «расписание» is the group's regular weekly timetable; the next dated lessons are
their own question (/lessons), and the bot must tell the two apart.
"""
from __future__ import annotations

import logging
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

MODEL_URL = "https://api.openai.com/v1/chat/completions"
# Cheapest first. A model the key cannot use is skipped for the life of the process.
INTENT_MODELS = tuple(filter(None, (os.getenv("GROUP_BOT_INTENT_MODEL", "gpt-4.1-nano"), "gpt-4o-mini")))
MODEL_TIMEOUT_SECONDS = 4
_CACHE_SIZE = 1000

COMMANDS = ("help", "schedule", "lessons", "next", "weekly", "homework", "recording")
SPANS = ("today", "tomorrow", "weekend", "this_week", "next_week", "week")


@dataclass(frozen=True)
class Intent:
    """``name`` — schedule | lessons | next | homework | recording | weekly | help | personal |
    curator | courtesy | none. ``span`` narrows ``lessons`` to a day or a week. ``source`` is who
    decided: ``command``, ``rules``, ``context``, a model name, or ``default`` when the model was
    needed and unavailable."""

    name: str
    span: Optional[str] = None
    source: str = "rules"


def normalize(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold().replace("ё", "е")).strip()


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


# ── what is never a group question ─────────────────────────────────────────────────────────

# Possession, not topic: «мой балл», «сколько у меня пропусков», «я сдал?» — while «когда дедлайн»
# and «можете мне скинуть дз» stay group questions.
_PERSONAL = _rx(
    r"(?:\bмо[йяеи]\w*|\bу\s+меня|\bмне|\bменің|\bмаған|\bmy)\s+(?:\w+\s+){0,2}?"
    r"(?:балл|оцен|балан|долг|задолж|оплат|плат[её]ж|счет|посещаем|пропуск|отработ|результат|"
    r"баға|бағам|төлем|қарыз|mark|grade|score|balance|payment|debt|attendance|result)"
    r"|(?:балл|оцен|балан|оплат|плат[её]ж|задолжен|результат)\w*\s+(?:мо[йяеи]\w*|за\s+меня)"
    r"|\bсколько\s+у\s+меня\b|\bчто\s+у\s+меня\s+с\b"
    r"|\bмо[йяеи]\w*\s+(?:дз|домашк\w*|задани\w*|работ\w*|сочинени\w*|эссе)"
    r"|\bя\s+(?:сдал|сдала|оплатил|оплатила|должен|должна|пропустил|пропустила|получил|получила)\b"
    r"|\b(?:проверили|оценили)\s+(?:мо[йяеи]\w*|у\s+меня)"
    r"|\b(?:did|have)\s+i\s+(?:pass|submit|pay)|\bmy\s+(?:homework|submission)"
)

_COURTESY_WORDS = frozenset({
    "спасибо", "спс", "пасыба", "пасиба", "рахмет", "рақмет", "thanks", "thx", "thank",
    "отдуши", "понял", "поняла", "пон", "ясно", "ок", "окей", "okay", "ok", "жақсы", "жаксы",
    "түсінікті", "тусиникти", "круто", "супер", "понятно", "хорошо", "класс", "отлично",
})
_COURTESY_PHRASES = ("от души", "все понял", "сесе понял", "базар жок")
_QUESTION_MARK = _rx(
    r"\?|\b(?:когда|где|как|какой|какая|какое|какие|сколько|почему|зачем|можно|нужно|есть\s+ли|"
    r"подскаж\w*|помоги\w*|скинь\w*|скинете|отправ\w*|пришли\w*|дай\w*|"
    r"when|where|what|which|how|why|is\s+there|"
    r"қашан|кашан|қайда|қалай|қандай|неше|неге|бола\s+ма|бар\s+ма|көмектес\w*)\b"
)
_HELP = _rx(
    r"(?:что|ч[её])\s+(?:ты\s+)?умеешь|(?:какие\s+)?команды|вопросы\s+(?:ты\s+)?(?:можешь\s+)?отвечать|"
    r"what\s+can\s+you\s+do|\bhelp\b|\bпомощь\b|не\s+істей\s+аласың|\bты\s+(?:тут|здесь)\b|"
    r"^(?:привет\w*|здравствуй\w*|здрасте|добрый\s+(?:день|вечер)|доброе\s+утро|салам\w*|"
    r"сәлем\w*|салем\w*|hi|hello|hey)[\s!.,)]*$"
)
# A request only a person can grant, or a problem only a person can fix. A past tense —
# «урок отменили?» — is a question about the timetable, and the facts answer it.
_HUMAN_REQUEST = _rx(
    r"(?:можно|можете|могли\s+бы|давайте|хочу|хотим|прошу|надо|нужно)\s+(?:\w+\s+){0,3}?"
    r"(?:перенес\w*|перенос\w*|отмен\w*|замен\w*|сдвин\w*)"
    r"|\bне\s+(?:открыва\w*|работа\w*|груз\w*|пуска\w*|могу\s+(?:зайти|войти|открыть)|получа\w*)"
    r"|\b(?:не\s+смогу|не\s+приду|опоздаю|заболел\w*|болею)\b"
    r"|\breschedul\w*|(?:can|could)\s+we\s+(?:move|cancel)|(?:does\s*n[o']?t|isn['’]?t|can['’]?t)\s+"
    r"(?:open|work|load|join)"
)

_STATEMENT = _rx(
    r"\bзаход(?:им|ите|и)\b|\bподключа\w*|\bне\s+заходи\w*|\bиспользуйте\b|"
    r"\b(?:please|pls)\s+(?:use|join)\b|\bjoin\b|\bкіріңіз\w*|\bкіреміз\b"
)

# ── what the group's facts answer ──────────────────────────────────────────────────────────

_WEEKLY = _rx(r"\b(?:мок\w*|mock\w*|викли\w*|weekly|пробн\w*)")
_HOMEWORK = _rx(
    r"\bдз\b|\bд/з\b|домашк\w*|домашн\w*|\bзадани\w*|\bзадал\w*|дедлайн\w*|homework|home\s+work|"
    r"assignment\w*|deadline\w*|тапсырма\w*"
)
_RECORDING = _rx(r"\bзапис(?:ь|и|ей|ью|ях|ям)\b|recording\w*|\brecord\b|\bвидео\w*|жазба\w*")
_SCHEDULE = _rx(
    r"р[ао]с{1,2}писани\w*|\bраписани\w*|\bграфик\w*|кесте\w*|schedule\w*|timetable\w*|"
    r"\bв\s+какие\s+дни\b|\bпо\s+каким\s+дням\b|\bкакие\s+дни\b|\bдни\s+недели\b|постоянн\w*|"
    r"\bқай\s+күн\w*|\bкай\s+кун\w*|what\s+days"
)
_LESSON = _rx(
    r"урок\w*|заняти\w*|\bпар[аыу]?\b|сабак\w*|сабақ\w*|lesson\w*|\bclass(?:es)?\b|созвон\w*|"
    r"\bвстреч\w*|\bmeet\b|\bмит\w*|ссылк\w*|\bлинк\w*|\blinks?\b|сілтеме\w*|\bzoom\b|\bзум\w*"
)
_LINK = _rx(r"ссылк\w*|\bлинк\w*|\blinks?\b|\bmeet\b|\bмит\w*|сілтеме\w*|\bzoom\b|\bзум\w*")
_PLURAL = _rx(
    r"\b(?:уроки|уроков|урокам|занятия|занятий|пары|lessons|classes|сабақтар\w*|сабактар\w*)\b"
)
_NEXT = _rx(r"следующ\w*|ближайш\w*|\bnext\b|upcoming|келесі\w*|келеси\w*|жақын\w*|жакын\w*|\bскоро\b")
_WHEN = _rx(
    r"\bкогда\b|во\s+сколько|в\s+котором\s+часу|в\s+какое\s+время|\bвремя\b|қашан|кашан|"
    r"сағат\s+неше|\bwhen\b|what\s+time|\bесть\s+ли\b|\bбудет\b|\bбар\s+ма\b"
)
_TODAY = _rx(r"сегодня\w*|бүгін\w*|бугин\w*|\btoday\b|\btonight\b")
_TOMORROW = _rx(r"\bзавтра\w*|ертең\w*|ертен\w*|\btomorrow\b")
_THIS_WEEK = _rx(r"\bэт(?:ой|у)\s+недел\w*|\bthis\s+week\b|\bосы\s+апта\w*")
_NEXT_WEEK = _rx(r"следующ(?:ей|ую|ая)\s+недел\w*|\bnext\s+week\b|келесі\s+апта\w*|келеси\s+апта\w*")
_WEEKEND = _rx(r"выходн\w*|\bweekends?\b|демалыс\w*")
_A_WEEK = _rx(r"\bна\s+неделю\b|\bнеделю\b|\bfor\s+the\s+week\b|\bаптаға\b")


def is_personal(text: str) -> bool:
    return bool(_PERSONAL.search(normalize(text)))


def _has_topic(t: str) -> bool:
    return any(rx.search(t) for rx in (_WEEKLY, _HOMEWORK, _RECORDING, _SCHEDULE, _LESSON))


def is_courtesy(text: str) -> bool:
    """A thank-you or «понял» — a reply to the bot that must not restart the conversation."""
    t = normalize(text)
    if not t:
        return True
    if _QUESTION_MARK.search(t) or _has_topic(t):
        return False
    if any(phrase in t for phrase in _COURTESY_PHRASES):
        return True
    words = re.findall(r"[^\W_]+", t, re.UNICODE)
    if not words:
        return True     # emoji, «+», «...»
    return len(words) <= 7 and bool(set(words) & _COURTESY_WORDS)


def _span(t: str) -> Optional[str]:
    if _TODAY.search(t):
        return "today"
    if _TOMORROW.search(t):
        return "tomorrow"
    if _WEEKEND.search(t):
        return "weekend"
    if _NEXT_WEEK.search(t):
        return "next_week"
    if _THIS_WEEK.search(t):
        return "this_week"
    if _A_WEEK.search(t):
        return "week"
    return None


def _rules(t: str) -> tuple[Optional[Intent], Optional[Intent]]:
    """(decided, default). ``decided`` is certain; otherwise ``default`` is what to fall back on
    when the model cannot be asked — ``None`` when the rules have no idea at all."""
    if _WEEKLY.search(t):
        return Intent("weekly"), None
    if _HOMEWORK.search(t):
        return Intent("homework"), None
    if _RECORDING.search(t):
        return Intent("recording"), None
    span = _span(t)
    if _SCHEDULE.search(t):
        if span:
            return Intent("lessons", span), None
        if _NEXT.search(t):
            return Intent("lessons"), None
        return Intent("schedule"), None
    if _LESSON.search(t):
        if span:
            return Intent("lessons", span), None
        if _PLURAL.search(t):
            if _NEXT.search(t):
                return Intent("lessons"), None
            if _WHEN.search(t):
                return None, Intent("schedule", source="default")
            return None, Intent("lessons", source="default")
        if _LINK.search(t) or _NEXT.search(t) or _WHEN.search(t):
            return Intent("next"), None
        return None, Intent("next", source="default")
    if span in ("today", "tomorrow") and _WHEN.search(t):
        return Intent("lessons", span), None
    if _NEXT.search(t) and _WHEN.search(t):
        return Intent("next"), None
    return None, None


# ── the model, for what the rules cannot place ─────────────────────────────────────────────

_LABELS = {
    "schedule": Intent("schedule"), "lessons": Intent("lessons"), "next": Intent("next"),
    "today": Intent("lessons", "today"), "tomorrow": Intent("lessons", "tomorrow"),
    "this_week": Intent("lessons", "this_week"), "next_week": Intent("lessons", "next_week"),
    "homework": Intent("homework"), "recording": Intent("recording"), "weekly": Intent("weekly"),
    "help": Intent("help"), "personal": Intent("personal"), "curator": Intent("curator"),
    "none": Intent("none"),
}
SYSTEM_PROMPT = (
    "Label a message sent to an education center's bot in a student group chat. "
    "Reply with exactly one label:\n"
    "schedule: the group's regular weekly timetable (which days, what time)\n"
    "lessons: upcoming lesson dates\n"
    "next: the next lesson, its time or Meet link\n"
    "today / tomorrow / this_week / next_week: lessons on that day or week\n"
    "homework: homework or deadlines\n"
    "recording: a past lesson's recording\n"
    "weekly: the weekly mock test\n"
    "help: what the bot can do, or a greeting\n"
    "personal: the sender's own grades, payments, attendance or submissions\n"
    "curator: any other request or problem that needs a person\n"
    "none: not a request"
)
_dead_models: set[str] = set()
_cache: "OrderedDict[tuple[str, str], str]" = OrderedDict()


def _model_key() -> Optional[str]:
    value = os.getenv("OPENAI_API_KEY")
    return value.strip() if value and value.strip() else None


def ask_model(question: str, context: str = "") -> Optional[tuple[str, str]]:
    """(label, model) or ``None``. One label, at most a few output tokens, never the facts."""
    key = _model_key()
    if not key:
        return None
    cache_key = (question[:300], context[:150])
    if cache_key in _cache:
        _cache.move_to_end(cache_key)
        return _cache[cache_key], "cache"
    import httpx

    content = f"Message: {question[:300]}"
    if context:
        content += f"\nIt replies to the bot's message: {context[:150]}"
    for model in INTENT_MODELS:
        if model in _dead_models:
            continue
        payload = {
            "model": model, "temperature": 0, "max_tokens": 4,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": content}],
        }
        try:
            with httpx.Client(timeout=MODEL_TIMEOUT_SECONDS) as client:
                response = client.post(MODEL_URL, headers={"Authorization": f"Bearer {key}"}, json=payload)
            if response.status_code in (400, 403, 404) and "model" in response.text.lower():
                logger.warning("group bot: intent model %s is unavailable (%s)", model, response.status_code)
                _dead_models.add(model)
                continue
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"]
        except Exception as e:
            logger.warning("group bot: intent model %s did not answer (%s)", model, str(e)[:200])
            return None
        label = re.sub(r"[^a-z_]", "", (raw or "").strip().split()[0].lower()) if (raw or "").strip() else ""
        if label not in _LABELS:
            logger.info("group bot: intent model %s said %r", model, (raw or "")[:40])
            return None
        _cache[cache_key] = label
        if len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
        return label, model
    return None


# ── the one entry point ────────────────────────────────────────────────────────────────────

def _context_line(reply_to_text: Optional[str]) -> str:
    """The part of a bot message that says what it was about — its first line after the
    group-name header."""
    lines = [line for line in (reply_to_text or "").splitlines() if line.strip()]
    if lines and lines[0].lstrip().startswith("📚"):
        lines = lines[1:]
    return normalize(lines[0])[:150] if lines else ""


# Each answer's title opens with its own mark (group_bot_render). The mark, not the words, says
# what a bot message was about: «⏭ Следующий урок: Завтра, …» is about the next lesson, and its
# «Завтра» is a date in the answer, not the topic.
_ANSWER_MARKS = (("⏭", "next"), ("🟢", "next"), ("🗓", "schedule"), ("📅", "lessons"),
                 ("📝", "homework"), ("🎥", "recording"), ("🧪", "weekly"))


def _context_topic(context: str) -> Optional[str]:
    for mark, topic in _ANSWER_MARKS:
        if context.startswith(mark):
            return topic
    decided, default = _rules(context)       # a lesson invitation or a homework notice
    chosen = decided or default
    return chosen.name if chosen and chosen.name in _FACT_TOPICS else None


_FACT_TOPICS = ("schedule", "lessons", "next", "homework", "recording", "weekly")


def classify(text: str, *, command: Optional[str] = None, reply_to_text: Optional[str] = None,
             use_model: bool = True) -> Intent:
    if command in COMMANDS:
        return Intent(command, source="command")
    t = normalize(text)
    if _PERSONAL.search(t):
        return Intent("personal")
    if is_courtesy(t):
        return Intent("courtesy")
    if _HUMAN_REQUEST.search(t):
        return Intent("curator")
    if not _QUESTION_MARK.search(t) and (
            _STATEMENT.search(t) or len(re.findall(r"[^\W_]+", t, re.UNICODE)) > 6):
        # «заходим по этой ссылке», «урок будет в 17:00, можете не заходить» — a teacher talking
        # to the room, not a request. A request is short («ссылка на урок») or asks something.
        return Intent("none")
    decided, default = _rules(t)
    if decided is not None:
        return decided
    if _HELP.search(t):
        return Intent("help")

    context = _context_line(reply_to_text)
    topic = _context_topic(context) if default is None and context else None
    if topic is not None:
        # «а на этой неделе есть?» under a weekly-mock post is still about the weekly mock.
        span = _span(t)
        if topic in ("schedule", "lessons", "next") and span:
            return Intent("lessons", span, "context")
        return Intent(topic, source="context")

    if use_model:
        labelled = ask_model(t, context)
        if labelled is not None:
            label, model = labelled
            chosen = _LABELS[label]
            return Intent(chosen.name, chosen.span, model)
    if default is not None:
        return default
    return Intent("curator", source="default") if _QUESTION_MARK.search(t) else Intent("none", source="default")
