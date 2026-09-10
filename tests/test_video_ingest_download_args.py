"""yt-dlp's argv for the video ingest download step.

Incident (2026-09-10): the video ingest worker went live and immediately wedged. YouTube
steered this server's downloads to a Google Global Cache node embedded in a different ISP
that accepts a TCP connection but never completes the TLS handshake — it just isn't serving
non-subscribers. DNS, MTU, IPv4-vs-IPv6 and the format selector were all ruled out by direct
network tests; the CDN itself is unreachable for this host.

yt-dlp's default retry counts (10 fragment/download retries) are meant for a flaky-but-
reachable host. Against a host that never completes a handshake, they instead turned an
unreachable CDN into a 15+ minute hang at 0.1% CPU with 0 bytes downloaded — the single-
threaded worker only ever claims one job at a time, so that one download blocked all 196
jobs queued behind it, and the job (stuck in "processing") was never retried.

These tests pin `_download`'s argv so the retry bound can't silently regress back to
yt-dlp's defaults, and check the optional `YTDLP_PROXY` escape hatch (the only real fix for
a CDN that refuses to serve this network at all) is wired correctly.
"""
import os
from pathlib import Path

import pytest

from src.services import video_ingest


@pytest.fixture
def fake_run(monkeypatch):
    """Capture the argv `_download` hands to `_run`, without touching the network.

    `_download` globs the workdir for `source.*` after `_run` returns and raises if
    nothing is there, so the fake also drops an empty `source.mp4` — that keeps the test
    honest about what `_download` actually does (return a Path to the downloaded file)
    rather than just asserting on the raise it would otherwise hit.
    """
    calls = []

    def _fake(cmd, timeout, capture=False):
        calls.append({"cmd": cmd, "timeout": timeout})
        workdir = Path(cmd[cmd.index("-o") + 1]).parent
        (workdir / "source.mp4").write_bytes(b"")
        return ""

    monkeypatch.setattr(video_ingest, "_run", _fake)
    return calls


def test_download_bounds_retries(fake_run, tmp_path, monkeypatch):
    """Regression guard for the 15-minute hang: retries must be bounded, not yt-dlp's default."""
    monkeypatch.delenv("YTDLP_PROXY", raising=False)

    video_ingest._download("https://youtu.be/abc12345678", tmp_path)

    cmd = fake_run[0]["cmd"]
    assert "--retries" in cmd
    assert cmd[cmd.index("--retries") + 1] == str(video_ingest.YTDLP_RETRIES)
    assert "--fragment-retries" in cmd
    assert cmd[cmd.index("--fragment-retries") + 1] == str(video_ingest.YTDLP_FRAGMENT_RETRIES)
    # Bounded, not yt-dlp's unbounded/high default of 10.
    assert int(cmd[cmd.index("--retries") + 1]) < 10
    assert int(cmd[cmd.index("--fragment-retries") + 1]) < 10


def test_download_no_proxy_when_env_unset(fake_run, tmp_path, monkeypatch):
    monkeypatch.delenv("YTDLP_PROXY", raising=False)

    video_ingest._download("https://youtu.be/abc12345678", tmp_path)

    assert "--proxy" not in fake_run[0]["cmd"]


def test_download_no_proxy_when_env_empty(fake_run, tmp_path, monkeypatch):
    monkeypatch.setenv("YTDLP_PROXY", "")

    video_ingest._download("https://youtu.be/abc12345678", tmp_path)

    assert "--proxy" not in fake_run[0]["cmd"]


def test_download_passes_proxy_when_env_set(fake_run, tmp_path, monkeypatch):
    monkeypatch.setenv("YTDLP_PROXY", "socks5://127.0.0.1:1080")

    video_ingest._download("https://youtu.be/abc12345678", tmp_path)

    cmd = fake_run[0]["cmd"]
    assert "--proxy" in cmd
    assert cmd[cmd.index("--proxy") + 1] == "socks5://127.0.0.1:1080"


def test_download_keeps_existing_args(fake_run, tmp_path, monkeypatch):
    """The existing invocation shape must survive the retry/proxy changes untouched."""
    monkeypatch.delenv("YTDLP_PROXY", raising=False)
    url = "https://youtu.be/abc12345678"

    video_ingest._download(url, tmp_path)

    cmd = fake_run[0]["cmd"]
    assert cmd[0] == "yt-dlp"
    assert "--no-playlist" in cmd
    assert "-f" in cmd
    assert cmd[cmd.index("-f") + 1] == "bv*[height<=1080]+ba/b[height<=1080]/b"
    assert "--merge-output-format" in cmd
    assert cmd[cmd.index("--merge-output-format") + 1] == "mp4"
    assert "-o" in cmd
    assert cmd[cmd.index("-o") + 1] == str(tmp_path / "source.%(ext)s")
    assert cmd[-1] == url
    assert fake_run[0]["timeout"] == video_ingest.DOWNLOAD_TIMEOUT
