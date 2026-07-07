"""Unit tests for the audio homework feature (no DB / TestClient needed)."""

from src.assignments.services import validate_answer_format
from src.assignments.routes.assignments import _is_allowed_audio, _ALLOWED_AUDIO_MIMES


def test_validate_answer_format_audio_accepts_empty_answers():
    assert validate_answer_format("audio", {}) is True


def test_validate_answer_format_audio_accepts_extra_keys_too():
    assert validate_answer_format("audio", {"anything": "ignored"}) is True


def test_is_allowed_audio_accepts_plain_mime():
    assert _is_allowed_audio("audio/webm") is True


def test_is_allowed_audio_accepts_mime_with_codecs_suffix():
    assert _is_allowed_audio("audio/mp4;codecs=mp4a") is True


def test_is_allowed_audio_is_case_insensitive():
    assert _is_allowed_audio("AUDIO/WEBM;codecs=opus") is True


def test_is_allowed_audio_rejects_image():
    assert _is_allowed_audio("image/png") is False


def test_is_allowed_audio_rejects_pdf():
    assert _is_allowed_audio("application/pdf") is False


def test_is_allowed_audio_rejects_empty():
    assert _is_allowed_audio("") is False
    assert _is_allowed_audio(None) is False


def test_all_declared_audio_mimes_are_accepted():
    for mime in _ALLOWED_AUDIO_MIMES:
        assert _is_allowed_audio(mime) is True
