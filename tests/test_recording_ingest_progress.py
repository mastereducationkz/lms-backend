"""The ingest says where it is — phase by phase, throttled — for the recordings pages (2026-09-15)."""
import subprocess
import time
from datetime import datetime, timedelta

import pytest

from src.services import recording_ingest, recordings_status, recordings_worker
from tests.test_recording_lifecycle import _DB, _Recording


def test_the_upload_counts_bytes_across_every_file(tmp_path, monkeypatch):
    (tmp_path / "master.m3u8").write_bytes(b"x" * 10)
    (tmp_path / "v0_000.ts").write_bytes(b"x" * 90)
    monkeypatch.setattr(recording_ingest.video_ingest.storage_service, "save", lambda *a, **k: None)
    seen = []

    recording_ingest.video_ingest._upload_tree(tmp_path, "videos/recordings/1", progress=lambda d, t: seen.append((d, t)))

    assert seen == [(10, 100), (100, 100)]


def test_processing_reports_each_phase_in_order(monkeypatch):
    calls, got = [], {}

    def download(file_id, workdir, progress=None):
        progress(5, 10)
        dest = workdir / "source.mp4"
        dest.write_bytes(b"x")
        return dest

    def package(src, out, progress=None, duration=None):
        got["duration"] = duration
        progress(30, 60)
        return "repackaged"

    monkeypatch.setattr(recording_ingest, "_download_drive_file", download)
    monkeypatch.setattr(recording_ingest, "probe_duration", lambda src: 60)
    monkeypatch.setattr(recording_ingest, "package_hls", package)
    monkeypatch.setattr(recording_ingest, "make_poster", lambda s, o, d, progress=None: progress(8, 8))
    monkeypatch.setattr(recording_ingest.video_ingest, "_upload_tree", lambda d, p, progress=None: progress(1, 1))
    monkeypatch.setattr(recording_ingest.storage_service, "stored_path", lambda k: "/uploads/" + k)
    monkeypatch.setattr(recording_ingest.meet_recordings, "copy_to_shared_drive", lambda f, e: "shared-1")
    rec = _Recording(status="pending", hls_url=None, id=7, event_id=70)
    rec.event = _Recording()

    recording_ingest.process_recording(_DB([rec]), rec,
                                       progress=lambda phase, done=None, total=None: calls.append((phase, done, total)))

    assert calls == [("downloading", None, None), ("downloading", 5, 10),
                     ("packaging", None, None), ("packaging", 30, 60),
                     ("preview", None, None), ("preview", 8, 8),
                     ("uploading", None, None), ("uploading", 1, 1)]
    assert got["duration"] == 60, "packaging is told the lesson's length, so it can give a percent"
    assert rec.status == "ready"


def test_a_silent_ffmpeg_is_still_stopped_at_its_timeout(tmp_path):
    """Progress lines are not the clock: an encoder that goes quiet must not hold the ingest behind it."""
    silent = tmp_path / "ffmpeg"
    silent.write_text("#!/bin/sh\nexec sleep 30\n")
    silent.chmod(0o755)
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        recording_ingest.video_ingest._run_with_progress([str(silent)], timeout=1, duration=10.0,
                                                         progress=lambda done, total: None)

    assert time.monotonic() - started < 10, "stopped at the timeout, not whenever it would have spoken"


def test_progress_is_written_when_a_phase_changes_or_ends_and_otherwise_every_two_seconds(monkeypatch):
    saved, clock = [], [datetime(2026, 9, 15, 17, 0, 0)]
    monkeypatch.setattr(recordings_status, "_save", lambda **fields: saved.append(fields["ingest"]))
    monkeypatch.setattr(recordings_status, "_now", lambda: clock[0])
    monkeypatch.setattr(recordings_status, "_last_report", {})

    def after(seconds, *args):
        clock[0] += timedelta(seconds=seconds)
        recordings_status.ingest_progress(7, 70, *args)

    after(0, "downloading", 0, 100)
    after(1, "downloading", 10, 100)    # 1 s later: not worth a write
    after(1.5, "downloading", 30, 100)  # 2.5 s after the last write
    after(0.1, "downloading", 100, 100)  # the phase is done: always written
    after(0.1, "packaging")             # a new phase: always written

    assert [(s["phase"], s["done"]) for s in saved] == [
        ("downloading", 0), ("downloading", 30), ("downloading", 100), ("packaging", None)]
    assert saved[2]["phase_started_at"] == saved[0]["phase_started_at"], "a phase keeps its start, for the rate"
    assert saved[3]["phase_started_at"] != saved[0]["phase_started_at"]


def test_the_worker_clears_its_report_even_when_a_recording_fails(monkeypatch):
    reports = []
    monkeypatch.setattr(recordings_worker.recordings_status, "ingest_progress", lambda *a: reports.append(a[2]))
    monkeypatch.setattr(recordings_worker.recordings_status, "ingest_finished", lambda: reports.append("finished"))

    def process(_db, recording, progress=None):
        progress("uploading", 1, 2)
        raise RuntimeError("S3 said no")

    monkeypatch.setattr(recordings_worker.recording_ingest, "process_recording", process)
    monkeypatch.setattr(recordings_worker.recording_ingest, "fail_recording", lambda _db, r, e: None)

    assert recordings_worker.ingest_one_pending(_DB([_Recording(status="pending", attempts=0, id=7, event_id=70)])) is True
    assert reports == ["downloading", "uploading", "finished"], "no page may show a recording as processing after it stopped"
