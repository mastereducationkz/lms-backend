"""Уважительный пропуск: инвариант, запись, чтение.

Уважительность — не статус, а флаг строки. Эти тесты закрепляют главное свойство:
словарь статусов от неё не меняется, поэтому ни проценты посещаемости, ни признак
«отмечено», ни биллинг не сдвигаются от самого факта её появления.
"""
import pytest

from src.services.attendance_status import (
    ABSENT_STATUSES,
    MARKED_STATUSES,
    PRESENT_STATUSES,
    is_excused,
    normalize_status,
    validate_excused,
)


def test_vocabulary_is_untouched_by_the_new_flag():
    """Если этот тест упал — значит в множества добавили значение, и вместе с ним
    поехали проценты, «отмечено» и списание урока с баланса."""
    assert PRESENT_STATUSES == frozenset({"present", "presented", "1", "yes", "late"})
    assert ABSENT_STATUSES == frozenset({"absent", "0", "no", "missed"})
    assert MARKED_STATUSES == PRESENT_STATUSES | ABSENT_STATUSES
    assert normalize_status("absent") == "absent"


def test_excused_needs_an_absent_status():
    with pytest.raises(ValueError) as exc:
        validate_excused("present", True, "заболел")
    assert str(exc.value) == "excused_requires_absent"


def test_excused_needs_a_non_empty_note():
    with pytest.raises(ValueError) as exc:
        validate_excused("absent", True, "   ")
    assert str(exc.value) == "excused_requires_note"


def test_a_valid_excused_row_passes():
    assert validate_excused("absent", True, "предупредил заранее") is None
    assert validate_excused("missed", True, "болел") is None


def test_an_unexcused_row_is_never_checked():
    """excused=false — обычный пропуск; причина не нужна и статус не проверяется."""
    assert validate_excused("present", False, None) is None
    assert validate_excused(None, False, None) is None


def test_is_excused_requires_both_halves():
    assert is_excused("absent", True) is True
    assert is_excused("absent", False) is False
    assert is_excused("present", True) is False
