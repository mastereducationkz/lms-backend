"""The morning post to «Штрафы учителя».

Head teachers do not open the register every day, so the day's findings go to them where they
already are. One post per day, at 09:00 Almaty about the day before: late enough that Meet has
finished reporting the previous evening's lessons, and early enough to be the first thing read.

What it says is deliberately narrower than the register.

* **Nothing that was waived.** A head teacher who has already decided a lesson costs nothing has
  closed it; repeating it in a chat reopens an argument that is over.
* **Nothing that was made up.** A teacher who joined three minutes late and stayed three minutes
  past the end gave the lesson its full length. The register still shows it — a head teacher may
  yet decide otherwise — but it is not what this channel is for.
* **A miss is named even before it is priced.** Only a person can put a number on a lesson that
  never happened, and waiting for that number would keep the most serious thing out of the post.

A fine waived *after* its post appears once more the next morning, under «Отменено» — the chat is
an archive, so nothing is edited behind a reader's back.

The row in `discipline_digest_sends` is the claim, written before the network call, so two
scheduler ticks cannot both post the same day.
"""
from __future__ import annotations

import html
import logging
import os
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.discipline import service
from src.discipline.models import DisciplineDecision, DisciplineDigestSend
from src.discipline.rules import RULE_START, period_containing
from src.schemas.models import UserInDB
from src.services import group_bot_outbox as outbox

logger = logging.getLogger(__name__)

#: Posted in the morning about the day before — Meet has had the night to finish reporting.
DIGEST_AT = time(9, 0)
#: A tick that arrives late still posts — the scheduler may have been restarting at 09:00 — but
#: a post that lands in the evening is no longer «this morning about yesterday», it is noise.
SEND_WINDOW = timedelta(hours=3)
MAX_ATTEMPTS = 3
#: Telegram refuses above 4096; leave room for the footer we always append.
TEXT_LIMIT = 3800

KIND_TEXT = {
    "late": "Опоздание {minutes} мин",
    "ended_early": "Ушёл раньше на {minutes} мин",
    "miss": "Урок не проведён",
}
REASON_LABELS = dict(service.REASONS)


def target() -> Optional[tuple]:
    """(support group, topic) for «Штрафы учителя», or None when it is not configured.

    «Штрафы учителя» is a forum topic in the staff supergroup («Кураторы Master Education»),
    which is the same supergroup the Meet staff notices already post into — so the group id
    falls back to theirs and a deployment only has to say which topic. Setting
    `DISCIPLINE_NOTICES_SUPPORT_GROUP_ID` overrides it if the two ever part company.
    """
    try:
        raw_group = (os.getenv("DISCIPLINE_NOTICES_SUPPORT_GROUP_ID", "")
                     or os.getenv("MEET_NOTICES_SUPPORT_GROUP_ID", ""))
        group_id = int(raw_group)
        topic_id = int(os.getenv("DISCIPLINE_NOTICES_TOPIC_ID", "0") or 0) or None
    except ValueError:
        return None
    return group_id, topic_id


def enabled() -> bool:
    return outbox.flag("ENABLE_DISCIPLINE_DIGEST") and target() is not None


def almaty(moment: datetime) -> datetime:
    """Naive UTC as it reads in Almaty, which is UTC+5 the whole year round."""
    return moment + service.ALMATY


def _money(tenge: int) -> str:
    return f"{tenge:,}".replace(",", " ") + " ₸"


def _reason(finding: dict) -> str:
    """What happened, and — if a person has spoken — why it was priced the way it was."""
    text = KIND_TEXT.get(finding["kind"], finding["kind"]).format(minutes=finding["minutes"])
    decision = finding.get("decision")
    if decision is not None and decision.reason_code:
        label = REASON_LABELS.get(decision.reason_code, decision.reason_code)
        text = f"{text} · {label}"
        if decision.note:
            text = f"{text}: {decision.note}"
    return text


def _skip(finding: dict, amount: int, unpriced: bool) -> bool:
    """Everything this channel deliberately stays quiet about."""
    if finding["kind"] == "late" and finding.get("made_up") and finding.get("decision") is None:
        return True                       # the minutes came back; the register still has it
    if amount <= 0 and not unpriced:
        return True                       # waived, or never worth anything
    return False


def fines_of(db: Session, day: date, now: datetime) -> list[dict]:
    """Every fine of one Almaty day, oldest lesson first."""
    period = period_containing(day)
    if period is None:
        return []
    rows = []
    for lesson in service.judged_lessons(db, period, now):
        if lesson["day"] != day:
            continue
        for finding in lesson["findings"]:
            amount, unpriced = service._amount_of(finding)
            if _skip(finding, amount, unpriced):
                continue
            rows.append({
                "teacher_id": lesson["teacher_id"],
                "group": lesson["group"] or "—",
                "starts_at": lesson["event"].start_datetime,
                "reason": _reason(finding),
                "amount": amount,
                "unpriced": unpriced,
            })
    rows.sort(key=lambda row: (row["starts_at"], row["group"]))
    return rows


def waivers_since(db: Session, day: date, now: datetime) -> list[dict]:
    """Fines cancelled or cut since the previous post, for days this chat has already seen."""
    window_start = now - timedelta(days=1)
    rows = (
        db.query(DisciplineDecision)
        .filter(DisciplineDecision.decided_at >= window_start,
                DisciplineDecision.decided_at <= now,
                DisciplineDecision.day < day,
                DisciplineDecision.day >= RULE_START)
        .order_by(DisciplineDecision.decided_at)
        .all()
    )
    out = []
    for decision in rows:
        proposed = int(decision.proposed_amount or 0)
        amount = int(decision.amount or 0)
        if amount >= proposed or proposed <= 0:
            continue                      # not a reduction: nothing was taken back
        out.append({"teacher_id": decision.teacher_id, "day": decision.day,
                    "was": proposed, "now": amount,
                    "reason": REASON_LABELS.get(decision.reason_code, decision.reason_code or "—"),
                    "note": decision.note, "decided_by": decision.decided_by})
    return out


def _names(db: Session, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    return {user.id: user.name
            for user in db.query(UserInDB).filter(UserInDB.id.in_(list(ids))).all()}


def digest_text(db: Session, day: date, now: datetime) -> Optional[str]:
    """The post, or None when there is nothing to say — a clean day stays silent."""
    fines = fines_of(db, day, now)
    waivers = waivers_since(db, day, now)
    if not fines and not waivers:
        return None

    names = _names(db, {row["teacher_id"] for row in fines}
                   | {row["teacher_id"] for row in waivers}
                   | {row["decided_by"] for row in waivers})

    def who(teacher_id: int) -> str:
        return html.escape(names.get(teacher_id) or f"ID {teacher_id}")

    lines = [f"⚖️ <b>Штрафы за {day.strftime('%d.%m.%Y')}</b>"]

    by_teacher: dict[int, list[dict]] = defaultdict(list)
    for row in fines:
        by_teacher[row["teacher_id"]].append(row)

    total = 0
    unpriced = 0
    for teacher_id in sorted(by_teacher, key=lambda tid: (names.get(tid) or "").lower()):
        rows = by_teacher[teacher_id]
        lines.append("")
        lines.append(f"<b>{who(teacher_id)}</b>")
        owed = 0
        for row in rows:
            when = almaty(row["starts_at"]).strftime("%H:%M")
            lines.append(f"  • {html.escape(row['group'])} · {when}")
            price = "сумма не назначена" if row["unpriced"] else _money(row["amount"])
            lines.append(f"    {html.escape(row['reason'])} — {price}")
            owed += row["amount"]
            unpriced += int(row["unpriced"])
        total += owed
        if owed:
            lines.append(f"  Итого: {_money(owed)}")

    if waivers:
        lines.append("")
        lines.append("↩️ <b>Отменено за прошлые дни</b>")
        for row in waivers:
            lines.append(f"  • {who(row['teacher_id'])} · {row['day'].strftime('%d.%m')}")
            tail = f"{_money(row['was'])} → {_money(row['now'])} · {html.escape(row['reason'])}"
            if row["note"]:
                tail += f": {html.escape(row['note'])}"
            lines.append(f"    {tail} ({who(row['decided_by'])})")

    footer = []
    if fines:
        footer.append(f"Всего за день: {_money(total)}")
    if unpriced:
        footer.append(f"без цены: {unpriced}")
    period = period_containing(day)
    if period is not None:
        closed = any(p.period_key == period.key for p in service.closed_periods(db))
        footer.append(f"период {period.label} {'закрыт' if closed else 'открыт'}")
    lines.append("")
    lines.append(" · ".join(footer))

    text = "\n".join(lines)
    if len(text) > TEXT_LIMIT:
        text = text[:TEXT_LIMIT].rsplit("\n", 1)[0] + "\n\n…список сокращён, полностью — в реестре."
    return text


def due_day(now: datetime) -> Optional[date]:
    """Which day this moment should be posting about, if any."""
    local = almaty(now)
    if local.time() < DIGEST_AT:
        return None
    if local - datetime.combine(local.date(), DIGEST_AT) > SEND_WINDOW:
        return None                       # too late to be this morning's post
    day = local.date() - timedelta(days=1)
    return day if day >= RULE_START else None


def _claim(db: Session, day: date, now: datetime) -> Optional[int]:
    row = DisciplineDigestSend(kind="fines", day=day, created_at=now)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()                     # another tick owns this day
        return None
    return row.id


def send_if_due(db: Session, now: Optional[datetime] = None) -> Optional[str]:
    """Post yesterday's fines once. Returns the status, or None when nothing was due."""
    now = now or service.now()
    day = due_day(now)
    if day is None:
        return None

    existing = (db.query(DisciplineDigestSend)
                .filter(DisciplineDigestSend.kind == "fines",
                        DisciplineDigestSend.day == day).first())
    if existing is not None:
        if existing.status not in ("pending", "failed") or existing.attempts >= MAX_ATTEMPTS:
            return None
        row_id = existing.id
    else:
        row_id = _claim(db, day, now)
        if row_id is None:
            return None

    text = digest_text(db, day, now)
    row = db.get(DisciplineDigestSend, row_id)
    if text is None:
        # A day nobody was fined is not worth a message; the row records that we looked.
        row.status, row.sent_at = "skipped", now
        db.commit()
        return "skipped"

    row.attempts += 1
    db.commit()                           # nothing held open across the network call
    group_id, topic_id = target()
    result = outbox.post(group_id, text, f"discipline-fines:{day.isoformat()}",
                         silent=False, topic_id=topic_id)
    row = db.get(DisciplineDigestSend, row_id)
    row.status, row.error = result["status"], result.get("error")
    row.telegram_message_id = result.get("telegram_message_id") or row.telegram_message_id
    if row.status == "sent":
        row.sent_at = now
    db.commit()
    (logger.info if row.status == "sent" else logger.warning)(
        "discipline digest %s: %s %s", day, row.status, row.error or "")
    return row.status


def run(db: Session, now: Optional[datetime] = None) -> dict:
    """Scheduler entry point. Never raises — a failed post must not stop the tick."""
    if not enabled():
        return {"status": "disabled"}
    try:
        return {"status": send_if_due(db, now)}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error("discipline digest failed: %s", exc, exc_info=True)
        return {"status": "error", "error": str(exc)}
