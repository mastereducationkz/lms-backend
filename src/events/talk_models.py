"""Talk time: who spoke in a lesson and for how long — and, when switched on, what was said.

Owner decisions, 2026-09-11: an admin switch inside the LMS turns it on and off for every
lesson; Meet's own transcript says who spoke when (exactly, per Google account — even in
Russian, though its Russian *text* is unusable, so none of it is kept); Deepgram supplies the
readable words, and each line is named by lining it up with Meet's timing. What a lesson's
talk *means* — shares, silent students, questions — is worked out when it is read, in
``meet_talk``, the same way ``meet_presence`` reads attendance: confirming an account later
names its speech in every lesson at once.
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from src.models.base import Base

# JSONB on Postgres, plain JSON elsewhere.
_JSON = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AppSetting(Base):
    """A switch an admin flips inside the LMS, by name. The first one is ``talk_time``."""

    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value = Column(_JSON, nullable=False, default=dict)
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_utcnow)


class MeetRoomTranscription(Base):
    """Whether a lesson room has Meet transcription switched on — as last set by the worker.

    Kept so the worker only asks Google when the switch and the room disagree, instead of
    patching every upcoming room on every tick.
    """

    __tablename__ = "meet_room_transcription"

    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    transcription = Column(Boolean, nullable=False)
    applied_at = Column(DateTime, nullable=False, default=_utcnow)
    # Set when Google refused for good (a teacher's own room the robot cannot change).
    error = Column(Text, nullable=True)


class MeetSpeech(Base):
    """Who spoke when in one Meet call, from Meet's transcript — timing only, no words.

    ``participants`` holds Meet participant resource names; each entry in ``entries`` is
    ``[index into participants, start ms, end ms]`` from ``origin``. Compact on purpose: a
    lesson is a few hundred entries. ``state='none'`` means the call had no transcript
    (transcription was off), which is final.
    """

    __tablename__ = "meet_speech"

    id = Column(Integer, primary_key=True)
    conference_id = Column(Integer, ForeignKey("meet_conferences.id", ondelete="CASCADE"),
                           nullable=False, unique=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    state = Column(String(16), nullable=False)
    origin = Column(DateTime, nullable=True)
    participants = Column(_JSON, nullable=True)
    entries = Column(_JSON, nullable=True)
    # The Google Doc Meet wrote for the transcript, in the robot's Drive — for a later clean-up.
    document_id = Column(String, nullable=True)
    saved_at = Column(DateTime, nullable=False, default=_utcnow)


class LessonTranscript(Base):
    """The words of a lesson's recording, from Deepgram, as utterances by anonymous voice.

    ``utterances`` are ``[start s, end s, voice, text]`` from the recording's first second;
    ``recording_started_at`` puts them on the clock, so Meet's timing can name each one.
    """

    __tablename__ = "lesson_transcripts"

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, unique=True)
    status = Column(String(16), nullable=False, default="pending")  # pending | ready | failed
    provider = Column(String(16), nullable=False, default="deepgram")
    recording_started_at = Column(DateTime, nullable=True)
    audio_seconds = Column(Float, nullable=True)
    utterances = Column(_JSON, nullable=True)
    languages = Column(_JSON, nullable=True)
    error = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    completed_at = Column(DateTime, nullable=True)
