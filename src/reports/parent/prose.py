"""Прозаические строки родительского отчёта.

Модель пишет только прозу и никогда — результаты: все числа результата рендерит
``template.render``. Но в прозе цифры законны («есть рост по Math на 3 балла»), поэтому
запрет цифр не годится. Вместо него — белый список: каждое число, встреченное в
фактическом слоте, обязано присутствовать в ``WeekFacts``.

Белый список применяется НЕ ко всем слотам. ``recommendation`` и ``proposal`` — это
предписания («учить по 10 слов в день», «созвониться на 15 минут»), их числа не обязаны
встречаться в отчёте и проверять их по фактам бессмысленно.
"""
import json
import logging
import re
from typing import Any, Dict, Optional, Tuple

from src.reports.parent.template import PROSE_SLOTS

logger = logging.getLogger("parent_reports.prose")

#: Числа в фактах. Обычный «любой набор цифр» — им сканируются сами данные.
_DIGITS = re.compile(r"\d+")

#: Числа в прозе. Цифры, приклеенные к буквам, числами не считаются: «B2», «Unit5»,
#: «IELTS9» — это ярлыки уровней и тем, и требовать их присутствия среди фактов значит
#: выбрасывать совершенно честные фразы.
_NUMBER = re.compile(r"(?<![^\W\d_])\d+(?![^\W\d_])")

#: Счёт вида «17/27». Проза не имеет права его содержать вообще: результаты печатает
#: ``template.render`` из фактов. Белый список отдельных чисел такую фразу пропустил бы —
#: «17/22» целиком состоит из настоящих цифр, но такой пары не было ни в одном тесте.
_SCORE = re.compile(r"\d+\s*/\s*\d+")

MAX_SLOT_CHARS = 200

#: Слоты, описывающие то, что произошло. К ним применяется белый список чисел.
FACTUAL_SLOTS = frozenset({
    "progress", "strength", "weakness", "observation", "activity", "forecast", "cause",
})

_SLOT_BRIEF = {
    "progress": "динамика по сравнению с прошлым тестом, 1–2 предложения",
    "strength": "что получается хорошо — только про указанную сильную сторону",
    "weakness": "над чем работаем — только про указанную слабую зону",
    "recommendation": "одна конкретная рекомендация для дома",
    "forecast": "прогноз при текущем темпе, либо цель на следующую неделю",
    "activity": "как ученик работал на уроках, по данным о речи",
    "observation": "что вы заметили за неделю, конкретно и без оценок личности",
    "cause": "предположение о причине, мягко и без обвинений",
    "proposal": "что предлагаете сделать, 1–2 пункта",
}


def allowed_numbers(facts: Dict[str, Any]) -> set:
    """Все числа, которые модель имеет право упомянуть в фактическом слоте."""
    allowed: set = set()

    def put(value: Any) -> None:
        if value is None or isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            # Дробную часть отбрасывать нельзя: при avg_seconds = 75.5 модель законно
            # напишет «75.5», «5» не окажется в списке, и честная фраза будет выброшена.
            allowed.update(_DIGITS.findall(str(abs(value))))
            allowed.add(str(int(abs(value))))

    def put_date(iso_day: Optional[str]) -> None:
        """День и месяц в обоих написаниях — «16»/«09» и «16»/«9»."""
        if not iso_day or len(iso_day) < 10:
            return
        year, month, day = iso_day[:10].split("-")
        allowed.update({day, month, str(int(day)), str(int(month))})

    test = facts.get("test") or {}
    for source in (test, test.get("prev") or {}):
        for key in ("verbal", "math"):
            side = source.get(key) or {}
            put(side.get("correct"))
            put(side.get("total"))
        # Ярлык недели («Week 5») печатается платформой и вполне может быть упомянут.
        allowed.update(_DIGITS.findall(str(source.get("label") or "")))
    for value in (test.get("delta") or {}).values():
        put(value)
    # Дату теста ``template._test_lines`` печатает в шапке результатов — значит она факт.
    put_date(test.get("date"))
    # Причина, по которой отчёт вообще стал тревожным: «два теста подряд без роста».
    put(facts.get("no_growth_streak"))

    homework = facts.get("homework") or {}
    put(homework.get("assigned"))
    put(homework.get("submitted"))

    attendance = facts.get("attendance") or {}
    for key in ("lessons", "present", "late"):
        put(attendance.get(key))
    put(len(attendance.get("absences") or []))
    for absence in attendance.get("absences") or []:
        put_date(absence.get("date"))

    talk = facts.get("talk") or {}
    for key in ("lessons", "lessons_spoke", "avg_seconds", "questions", "answers"):
        put(talk.get(key))

    for side in ("strength", "weakness"):
        candidate = facts.get(side) or {}
        put(candidate.get("pct"))

    return allowed


def sanitize(prose: Dict[str, str], facts: Dict[str, Any]) -> Dict[str, str]:
    """Выбросить слоты, которые нарушают правила. Молча — вызывающий решает, что дальше."""
    allowed = allowed_numbers(facts)
    clean: Dict[str, str] = {}
    for slot, raw in (prose or {}).items():
        text = (raw or "").strip()
        if not text or len(text) > MAX_SLOT_CHARS:
            continue
        if slot in FACTUAL_SLOTS:
            if _SCORE.search(text):
                logger.info("parent report: слот %s отброшен — проза со счётом", slot)
                continue
            if any(n not in allowed for n in _NUMBER.findall(text)):
                logger.info("parent report: слот %s отброшен — число вне фактов", slot)
                continue
        clean[slot] = text
    return clean


def requested_slots(facts: Dict[str, Any], template_key: str) -> Tuple[str, ...]:
    """Слоты шаблона минус те, под которые нет данных.

    Слот, которого здесь нет, модель не увидит вовсе — придумать тему ей просто негде.
    """
    slots = PROSE_SLOTS.get(template_key, PROSE_SLOTS["t1"])
    skip = set()
    if not facts.get("strength"):
        skip.add("strength")
    # Одобренный фидбэк преподавателя — третий источник сигнала из спеки. Ярлыка темы он
    # не даёт (свободный текст), но это настоящее человеческое наблюдение, и молчать при
    # нём так же неправильно, как выдумывать тему при его отсутствии.
    if not facts.get("weakness") and not facts.get("teacher_feedback"):
        skip.add("weakness")
    # Куратор может выбрать t2 руками у ученика без Talk Time. Описывать «активность на
    # уроках» модели тогда не из чего, а слот без данных — приглашение выдумать.
    if not facts.get("talk"):
        skip.add("activity")
    if not facts.get("test"):
        skip.add("progress")
    return tuple(s for s in slots if s not in skip)


class OpenAIProseClient:
    """Клиент OpenAI для прозы отчёта: возвращает JSON-объект со слотами."""

    def __init__(self) -> None:
        from openai import AsyncOpenAI
        from src.config import OPENAI_API_KEY, OPENAI_MODEL

        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        self.client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        self.model = OPENAI_MODEL

    async def complete(self, *, facts: Dict[str, Any], slots: Tuple[str, ...]) -> Dict[str, str]:
        wanted = "\n".join(f"- {s}: {_SLOT_BRIEF[s]}" for s in slots)
        system = (
            "Ты помогаешь куратору образовательного центра писать родителям об успехах "
            "ребёнка. Пиши по-русски, тепло и уважительно, без канцелярита и без оценок "
            "личности ребёнка.\n"
            "ЗАПРЕЩЕНО: выдумывать результаты, темы, даты и имена, которых нет в данных; "
            "писать эмодзи; писать баллы и счёт (их подставляет система); "
            "писать больше 200 символов в одном поле.\n"
            "Верни JSON-объект ровно с этими ключами и ничем больше:\n" + wanted
        )
        note = facts.get("curator_note")
        user = "Данные за неделю:\n" + json.dumps(facts, ensure_ascii=False, indent=1)
        if note:
            user += (
                "\n\nНаблюдение куратора — это живой человек, смотревший на ученика. "
                "Оно приоритетнее автоматических выводов:\n" + note
            )
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0.4,
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        return {k: v for k, v in payload.items() if isinstance(v, str)}


async def generate_prose(
    facts: Dict[str, Any], template_key: str, client: Optional[Any] = None
) -> Dict[str, str]:
    """Сгенерировать прозу и вычистить её. Пустой словарь — допустимый результат.

    Пустой результат не ошибка: ``template.render`` соберёт каркас с числами,
    посещаемостью и ДЗ, а прозу куратор допишет руками. Это всё равно быстрее, чем
    писать отчёт с нуля, и заметно лучше, чем показать ему пустой экран.
    """
    slots = requested_slots(facts, template_key)
    if not slots:
        return {}
    client = client or OpenAIProseClient()

    for attempt in (1, 2):
        try:
            raw = await client.complete(facts=facts, slots=slots)
            # sanitize внутри try намеренно: ответ может быть синтаксически валидным, но
            # с не-строкой в поле, и тогда падение произойдёт здесь, а не в запросе.
            # Правило «сбой генерации даёт пустую прозу, а не исключение» одно на оба случая.
            clean = {k: v for k, v in sanitize(raw, facts).items() if k in slots}
        except Exception as exc:  # сеть, квота, таймаут, кривой ответ — каркас важнее прозы
            logger.warning("parent report: генерация прозы не удалась: %s", exc)
            return {}
        if len(clean) == len([s for s in slots if s in (raw or {})]):
            return clean
        if attempt == 2:
            return clean
    return {}
