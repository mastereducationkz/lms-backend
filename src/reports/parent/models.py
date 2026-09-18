"""Сохранённые родительские отчёты — один на ученика в неделю."""
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from src.models.base import Base


class ParentReport(Base):
    """Отчёт родителю за одну неделю.

    ``facts_json`` — снапшот цифр на момент генерации, а не ссылка на текущее состояние
    базы. Оценки и посещаемость в этом проекте регулярно дозаливаются задним числом;
    без снапшота через неделю нельзя ответить, почему в отправленном родителю тексте
    стоит 14/27, когда в базе уже 17/27.

    ``body_generated`` хранится отдельно от ``body``: так видно, что куратор правил,
    и можно оценить, насколько генерация попадает в цель.
    """

    __tablename__ = "parent_reports"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="SET NULL"), nullable=True)
    week_start = Column(Date, nullable=False, index=True)

    template_key = Column(String(8), nullable=False)
    #: False означает, что куратор переключил шаблон руками.
    template_auto = Column(Boolean, nullable=False, default=True)

    facts_json = Column(JSONB, nullable=False)
    body_generated = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    curator_note = Column(Text, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("student_id", "week_start", name="uq_parent_report_student_week"),
    )
