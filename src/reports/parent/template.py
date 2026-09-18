"""Выбор шаблона родительского отчёта и его прозаические слоты.

Шаблон выбирается кодом, а не моделью: правило должно быть воспроизводимым и
объяснимым. Куратор видит причину выбора текстом и может переключить шаблон руками —
если правило ошиблось, это видно сразу, а не растворяется в «модель так решила».

Каскад читается строго сверху вниз, первое совпадение выигрывает.
"""
from datetime import date
from typing import Any, Dict, Tuple

#: Какие прозаические строки нужны каждому шаблону. Слот, которого здесь нет,
#: у LLM не запрашивается вообще.
PROSE_SLOTS: Dict[str, Tuple[str, ...]] = {
    "t1": ("progress", "strength", "weakness", "recommendation", "forecast"),
    "t2": ("activity", "progress", "recommendation"),
    # Короткий шаблон для занятых родителей: сильная и слабая стороны идут ярлыками
    # из фактов, прозой пишется только рекомендация.
    "t3": ("recommendation",),
    # В t4 слот forecast рендерится как «Цель на следующую неделю», в t1 — как «Прогноз».
    "t4": ("observation", "recommendation", "forecast"),
    "t5": ("observation", "cause", "proposal"),
}


def _ddmm(iso_day: str) -> str:
    """'2026-09-16' → '16.09'."""
    return date.fromisoformat(iso_day).strftime("%d.%m")


def pick_template(facts: Dict[str, Any]) -> Tuple[str, str]:
    """Вернуть ``(template_key, reason)``.

    ``reason`` — человекочитаемое объяснение с конкретикой, оно показывается куратору.
    """
    attendance = facts.get("attendance") or {}
    homework = facts.get("homework")
    test = facts.get("test")
    talk = facts.get("talk")
    weakness = facts.get("weakness")

    # 1. Есть что обсудить: неуважительный пропуск, систематически несданное ДЗ
    #    или две подряд недели без роста.
    unexcused = [a for a in attendance.get("absences") or [] if not a.get("excused")]
    if unexcused:
        days = ", ".join(_ddmm(a["date"]) for a in unexcused)
        return "t5", f"выбран Шаблон 5: пропуск без уважительной причины ({days})"

    if homework and homework["assigned"] >= 2 and homework["submitted"] * 2 < homework["assigned"]:
        return "t5", (
            f"выбран Шаблон 5: сдано {homework['submitted']} из {homework['assigned']} ДЗ"
        )

    if facts.get("no_growth_streak", 0) >= 2:
        return "t5", "выбран Шаблон 5: нет роста ни по одной секции два теста подряд"

    # 2. Теста на неделе нет — говорим про уроки и вовлечённость, если есть чем.
    if test is None and talk and talk.get("lessons", 0) > 0:
        return "t2", "выбран Шаблон 2: теста на этой неделе нет, опираемся на работу в классе"

    # 3. Есть тест и понятно, над чем работать.
    if test is not None and weakness:
        return "t4", f"выбран Шаблон 4: есть слабая зона — {weakness['label']}"

    # 4. Обычная неделя.
    return "t1", "выбран Шаблон 1: неделя без особенностей"
