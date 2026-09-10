"""The video ingest worker's behaviour when YouTube's media CDN refuses this host.

Incident (2026-09-10, measured on the production host): YouTube steers this server's
media URLs to the ``sn-hxb54vo-*`` Google Global Cache cluster, whose nodes live inside
other ISPs' networks (83.169.197.237, 178.176.159.x, 188.170.164.x). Those nodes answer
ICMP in ~32ms with 0% loss but silently drop TCP SYN on 443, so every download stalls and
then times out. It is not per-video and it is not fixable client-side:

* the node hostname is baked into the signed URL — replaying that URL against a reachable
  Google edge returns 400 or 421 (verified against 173.194.188.38 and 142.251.110.190);
* the HLS variant only moves the *playlist* to manifest.googlevideo.com — the segment URLs
  inside it point straight back at the same dead node (verified);
* every player client that returns formats at all returns the same cluster.

So the failure is environmental: while it lasts, every one of the queued jobs fails the
same way. The danger is the bookkeeping, not the downtime — `_process_one` increments
`attempts` when it claims a job, and `_fail` flips a job to ``failed`` (and its ru step to
``video_status='failed'``) once `attempts` reaches MAX_ATTEMPTS. Left alone, switching
ingest on against a dead CDN walks the whole queue into a permanently failed state within
hours, over a fault that has nothing to do with the jobs.

These tests pin the two halves of the guard: classification (a CDN refusal is raised as
`MediaCdnUnreachable`, anything else is not) and the worker's response (give the attempt
back, leave the job pending, and stop claiming jobs until the backoff expires).
"""
from pathlib import Path

import pytest

from src.services import video_ingest


# --- classification ----------------------------------------------------------

def _raise_from_run(monkeypatch, message: str):
    def _fake(cmd, timeout, capture=False):
        raise RuntimeError(message)

    monkeypatch.setattr(video_ingest, "_run", _fake)


def test_cdn_timeout_is_raised_as_environmental(monkeypatch, tmp_path):
    """The real stderr tail from the incident must classify as MediaCdnUnreachable."""
    monkeypatch.delenv("YTDLP_PROXY", raising=False)
    _raise_from_run(monkeypatch, (
        "yt-dlp exited 1: ERROR: [download] Got error: HTTPSConnectionPool("
        "host='rr2---sn-hxb54vo-304z.googlevideo.com', port=443): Read timed out. "
        "(read timeout=30.0). Giving up after 2 retries"
    ))

    with pytest.raises(video_ingest.MediaCdnUnreachable):
        video_ingest._download("https://youtu.be/abc12345678", tmp_path)


def test_ordinary_failure_is_not_environmental(monkeypatch, tmp_path):
    """A private/removed video is the job's problem and must keep burning its attempts."""
    monkeypatch.delenv("YTDLP_PROXY", raising=False)
    _raise_from_run(monkeypatch, "yt-dlp exited 1: ERROR: [youtube] abc: This video is unavailable")

    with pytest.raises(RuntimeError) as excinfo:
        video_ingest._download("https://youtu.be/abc12345678", tmp_path)
    assert not isinstance(excinfo.value, video_ingest.MediaCdnUnreachable)


def test_unrelated_host_timeout_is_not_environmental(monkeypatch, tmp_path):
    """Only googlevideo timeouts count — a timeout elsewhere must not silence the queue."""
    monkeypatch.delenv("YTDLP_PROXY", raising=False)
    _raise_from_run(monkeypatch, "yt-dlp exited 1: HTTPSConnectionPool(host='example.com', "
                                 "port=443): Read timed out. (read timeout=30.0)")

    with pytest.raises(RuntimeError) as excinfo:
        video_ingest._download("https://youtu.be/abc12345678", tmp_path)
    assert not isinstance(excinfo.value, video_ingest.MediaCdnUnreachable)


# --- worker response ---------------------------------------------------------

class _FakeJob:
    def __init__(self):
        self.id = 1
        self.step_id = 42
        self.lang = "ru"
        self.youtube_url = "https://youtu.be/abc12345678"
        self.status = "pending"
        self.attempts = 0
        self.error = None


class _FakeQuery:
    def __init__(self, job):
        self._job = job

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self._job


class _FakeSession:
    """Just enough Session for `_process_one`; records whether it was rolled back."""

    def __init__(self, job):
        self._job = job
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def query(self, *a, **k):
        return _FakeQuery(self._job)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def get(self, *a, **k):
        return None

    def close(self):
        self.closed = True


@pytest.fixture
def worker_against_dead_cdn(monkeypatch):
    """A worker whose one queued job always hits the dead CDN.

    `attempts` alone cannot prove the backoff gate works: without it the worker re-claims
    the job (attempts 0->1), fails, and `_requeue_environmental` refunds it to 0 — the
    same end state. `download_attempts` counts the claims themselves, which is the thing
    the gate is supposed to prevent.
    """
    job = _FakeJob()
    session = _FakeSession(job)
    download_attempts = []
    monkeypatch.setattr(video_ingest, "SessionLocal", lambda: session)

    def _boom(db, j):
        download_attempts.append(j.id)
        raise video_ingest.MediaCdnUnreachable(
            "HTTPSConnectionPool(host='rr2---sn-hxb54vo-304z.googlevideo.com', port=443): "
            "Read timed out."
        )

    monkeypatch.setattr(video_ingest, "process_job", _boom)
    return video_ingest.VideoIngestWorker(), job, download_attempts


def test_dead_cdn_does_not_spend_the_job_attempt(worker_against_dead_cdn):
    """The queue must survive the outage: job back to pending, attempt refunded."""
    worker, job, _attempts = worker_against_dead_cdn

    worker._process_one()

    assert job.status == "pending"
    assert job.attempts == 0, "a CDN outage must not count against MAX_ATTEMPTS"
    assert "googlevideo.com" in (job.error or ""), "operators need the reason recorded"


def test_dead_cdn_stops_the_worker_claiming_more_jobs(worker_against_dead_cdn):
    """One probe per backoff window, not one failed download per queued job."""
    worker, _job, attempts = worker_against_dead_cdn

    assert worker._process_one() is False
    assert worker._cdn_backoff_until > 0
    assert len(attempts) == 1

    # Ticks inside the window must not reach the CDN again.
    assert worker._process_one() is False
    assert worker._process_one() is False
    assert len(attempts) == 1, "the backoff gate must stop the worker re-claiming jobs"


def test_worker_resumes_after_backoff_expires(worker_against_dead_cdn, monkeypatch):
    """The stand-down is a pause, not a stop — the queue drains once egress is fixed."""
    worker, job, _attempts = worker_against_dead_cdn

    worker._process_one()
    assert worker._process_one() is False  # still backed off

    monkeypatch.setattr(
        video_ingest, "process_job", lambda db, j: setattr(j, "status", "done")
    )
    worker._cdn_backoff_until = 0.0  # window elapsed

    assert worker._process_one() is True
    assert job.status == "done"
