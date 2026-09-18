"""Выбор шаблона родительского отчёта и его прозаические слоты.

Шаблон выбирается кодом, а не моделью: правило должно быть воспроизводимым и
объяснимым. Куратор видит причину выбора текстом и может переключить шаблон руками —
если правило ошиблось, это видно сразу, а не растворяется в «модель так решила».

Каскад читается строго сверху вниз, первое совпадение выигрывает.
"""
from datetime import date
from typing import Any, Dict, Optional, Tuple

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
    #    ``test_unavailable`` здесь обязателен: «теста не было» и «платформа не ответила,
    #    и мы не знаем» — разные вещи, а родителю уходит именно утверждение. При сбое
    #    платформы каскад идёт дальше и ничего про тест не заявляет.
    if test is None and not facts.get("test_unavailable") and talk and talk.get("lessons", 0) > 0:
        return "t2", "выбран Шаблон 2: теста на этой неделе нет, опираемся на работу в классе"

    # 3. Есть тест и понятно, над чем работать.
    if test is not None and weakness:
        return "t4", f"выбран Шаблон 4: есть слабая зона — {weakness['label']}"

    # 4. Обычная неделя.
    return "t1", "выбран Шаблон 1: неделя без особенностей"


def _score(side: Optional[Dict[str, Any]]) -> Optional[str]:
    if not side or side.get("correct") is None:
        return None
    return f"{side['correct']}/{side['total']}"


def _test_lines(test: Optional[Dict[str, Any]]) -> list:
    """Строки с результатами. Никакая из них не проходит через LLM."""
    if not test:
        return []
    lines = ["📊 Результаты теста" + (f" ({_ddmm(test['date'])})" if test.get("date") else "") + ":"]
    prev = test.get("prev") or {}
    for key, label in (("verbal", "Verbal"), ("math", "Math")):
        now = _score(test.get(key))
        if now is None:
            continue
        was = _score(prev.get(key))
        lines.append(f"{label}: {now}" + (f" (прошлая неделя: {was})" if was else ""))
    return lines


def _attendance_line(attendance: Dict[str, Any]) -> str:
    absences = attendance.get("absences") or []
    if not absences:
        return "📅 Посещаемость: ✅ Все занятия"
    days = ", ".join(_ddmm(a["date"]) for a in absences)
    return f"📅 Посещаемость: ⚠️ Пропуск ({days})"


def _homework_line(homework: Optional[Dict[str, Any]]) -> Optional[str]:
    """None означает «за неделю ничего не задавали» — строки в сообщении не будет.

    Это прямое требование кураторов: пустое «Выполнено 0 из 0» читается родителем как
    претензия к ребёнку, хотя задания просто не выдавались.
    """
    if not homework:
        return None
    line = f"📝 ДЗ: Выполнено {homework['submitted']} из {homework['assigned']}"
    missing = homework.get("missing") or []
    if missing:
        line += ". Не сдано: " + ", ".join(missing)
    return line


def render(
    facts: Dict[str, Any],
    template_key: str,
    prose: Dict[str, str],
    curator_name: Optional[str] = None,
) -> str:
    """Собрать сообщение. ``prose`` — только прозаические строки, без чисел результата.

    Слот, которого нет в ``prose`` (или пустой), не даёт строки вовсе: отчёт молчит там,
    где нечего сказать, вместо того чтобы печатать пустую рубрику.
    """
    name = facts["student"]["name"]
    attendance = facts.get("attendance") or {}
    lines: list[str] = ["Здравствуйте! 🤍"]

    def add(slot: str, prefix: str) -> None:
        value = (prose.get(slot) or "").strip()
        if value:
            lines.append(f"{prefix}{value}")

    if template_key == "t5":
        lines.append(f"Хочу обсудить ситуацию по {name}:")
        add("observation", "⚠️ Что заметила: ")
        lines += _test_lines(facts.get("test"))
        hw = _homework_line(facts.get("homework"))
        if hw:
            lines.append(hw)
        lines.append(_attendance_line(attendance))
        add("cause", "🔍 В чём может быть причина: ")
        add("proposal", "💬 Что предлагаю: ")
        lines.append(f"📞 Можем созвониться? Хочу, чтобы {name} достиг(ла) результата 🙏")
    elif template_key == "t2":
        lines.append(f"Делюсь результатами {name} за неделю:")
        add("activity", "🎓 Активность на уроках: ")
        lines += _test_lines(facts.get("test"))
        hw = _homework_line(facts.get("homework"))
        if hw:
            lines.append(hw)
        lines.append(_attendance_line(attendance))
        add("progress", "📈 Прогресс: ")
        add("recommendation", "💡 Что делать дома: ")
        lines.append("Вопросы? Пишите, на связи!")
    elif template_key == "t3":
        lines.append(f"Кратко по {name} за неделю:")
        lines += _test_lines(facts.get("test"))
        if facts.get("strength"):
            lines.append(f"✅ Сильная сторона: {facts['strength']['label']}")
        if facts.get("weakness"):
            lines.append(f"⚠️ Слабая зона: {facts['weakness']['label']}")
        lines.append(_attendance_line(attendance))
        hw = _homework_line(facts.get("homework"))
        if hw:
            lines.append(hw)
        add("recommendation", "👉 Рекомендация: ")
        lines.append("Вопросы? Пишите!")
    elif template_key == "t4":
        lines.append(f"Отчёт по {name}:")
        lines += _test_lines(facts.get("test"))
        add("observation", "🔍 Что заметила: ")
        hw = _homework_line(facts.get("homework"))
        if hw:
            lines.append(hw)
        lines.append(_attendance_line(attendance))
        add("recommendation", "📚 Рекомендации: ")
        add("forecast", "🎯 Цель на следующую неделю: ")
        lines.append("Если нужна помощь или есть вопросы — я на связи!")
    else:  # t1
        lines.append(f"Отчёт по {name} за неделю:")
        lines += _test_lines(facts.get("test"))
        add("progress", "📈 Прогресс: ")
        add("strength", "✅ Что получается хорошо: ")
        add("weakness", "⚠️ Над чем работаем: ")
        lines.append(_attendance_line(attendance))
        hw = _homework_line(facts.get("homework"))
        if hw:
            lines.append(hw)
        add("recommendation", "💡 Рекомендация для дома: ")
        add("forecast", "🎯 Прогноз: ")
        lines.append("Если есть вопросы — пишите или созвонимся!")

    signature = "С уважением, " + (curator_name + ", " if curator_name else "") + "Master Education"
    lines.append(signature)
    return "\n".join(lines)
