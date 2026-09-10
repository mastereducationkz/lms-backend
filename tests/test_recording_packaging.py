"""A Meet recording is repackaged for streaming, not re-encoded — unless it can't be.

Re-encoding three quality levels cost ~23 minutes of all four shared cores per lesson-hour
on production (measured 2026-09-10); at 45–66 lessons a day the queue would never drain.
Meet already records H.264/AAC, so copying the streams into HLS is enough — when the file
cooperates. These run real ffmpeg on tiny generated clips, and pin both halves: the normal
case never touches the encoder, and every unusual case still ends in a playable lesson.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from src.services import recording_ingest

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg"
)


def _clip(path, *, seconds=20, gop_seconds=2, vcodec="libx264", pix_fmt="yuv420p"):
    """A small video with sound, keyframes every ``gop_seconds`` — shaped like a Meet file."""
    fps = 10
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=320x180:rate={fps}",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-t", str(seconds), "-c:v", vcodec, "-pix_fmt", pix_fmt, "-g", str(gop_seconds * fps),
         "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path


@pytest.fixture
def out(tmp_path):
    d = tmp_path / "hls"
    d.mkdir()
    return d


@pytest.fixture
def encoder(monkeypatch):
    """Records whether the full re-encode ran, and stands in for it with a valid playlist."""
    calls = []

    def _transcode(src, out):
        calls.append(src.name)
        (out / "master.m3u8").write_text("#EXTM3U\n")

    monkeypatch.setattr(recording_ingest.video_ingest, "_transcode_hls", _transcode)
    return calls


def test_a_meet_shaped_file_is_repackaged_without_touching_the_encoder(tmp_path, out, encoder):
    src = _clip(tmp_path / "lesson.mp4")

    assert recording_ingest.package_hls(src, out) == "repackaged"
    assert encoder == [], "no re-encoding for an ordinary H.264/AAC recording"
    assert (out / "master.m3u8").exists() and (out / "v0.m3u8").exists()
    assert sorted(out.glob("v0_*.ts")), "segments are written beside the playlists"


def test_the_repackage_keeps_the_player_contract(tmp_path, out, encoder):
    """Same layout the ladder wrote: one master naming its variant, the variant a VOD list."""
    src = _clip(tmp_path / "lesson.mp4")
    recording_ingest.package_hls(src, out)

    master = (out / "master.m3u8").read_text()
    variant = (out / "v0.m3u8").read_text()
    assert "v0.m3u8" in master and "#EXT-X-STREAM-INF" in master
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in variant and "#EXT-X-ENDLIST" in variant
    assert recording_ingest._target_duration(out / "v0.m3u8") <= recording_ingest.MAX_SEGMENT_SECONDS


def test_the_repackaged_segments_are_still_h264_and_aac(tmp_path, out, encoder):
    src = _clip(tmp_path / "lesson.mp4")
    recording_ingest.package_hls(src, out)

    first = sorted(out.glob("v0_*.ts"))[0]
    info = recording_ingest._probe_streams(first)
    assert (info["video"], info["pix_fmt"], info["audio"]) == ("h264", "yuv420p", "aac")


def test_keyframes_too_far_apart_fall_back_to_re_encoding(tmp_path, out, encoder):
    """One keyframe in 40 seconds would make a single 40-second segment: seeking would stall."""
    src = _clip(tmp_path / "sparse.mp4", seconds=40, gop_seconds=40)

    assert recording_ingest.package_hls(src, out) == "re-encoded"
    assert encoder == ["sparse.mp4"]
    assert not list(out.glob("v0_*.ts")), "the abandoned repackage is cleared before re-encoding"


def test_h264_that_browsers_cannot_decode_is_re_encoded(tmp_path, out, encoder):
    """4:4:4 H.264 repackages without complaint and then never plays — the probe must catch it."""
    src = _clip(tmp_path / "full-chroma.mp4", pix_fmt="yuv444p")

    assert recording_ingest.package_hls(src, out) == "re-encoded"
    assert encoder == ["full-chroma.mp4"]


def test_another_codec_is_re_encoded(tmp_path, out, encoder):
    src = _clip(tmp_path / "old.mp4", vcodec="mpeg4")

    assert recording_ingest.package_hls(src, out) == "re-encoded"
    assert encoder == ["old.mp4"]


def test_an_unreadable_file_is_left_to_the_encoder(tmp_path, out, encoder):
    """The encoder is the path that reports a broken file properly; the probe only chooses."""
    src = tmp_path / "junk.mp4"
    src.write_bytes(b"not a video")

    assert recording_ingest.package_hls(src, out) == "re-encoded"
    assert encoder == ["junk.mp4"]


# --- duration and preview -----------------------------------------------------------------


def _black_then_busy(path):
    """10 s of black (a camera that is off), then 10 s of a detailed picture (a slide)."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=640x360:d=10:r=10",
         "-f", "lavfi", "-i", "testsrc=s=640x360:d=10:r=10",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return path


def test_the_duration_is_read_from_the_file(tmp_path):
    assert recording_ingest.probe_duration(_clip(tmp_path / "lesson.mp4", seconds=20)) == 20


def test_an_unreadable_file_has_no_duration(tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")
    assert recording_ingest.probe_duration(junk) is None


def test_the_preview_is_the_most_detailed_frame_not_the_first(tmp_path, out):
    src = _black_then_busy(tmp_path / "lesson.mp4")
    poster = recording_ingest.make_poster(src, out, duration=20)

    assert poster == out / "poster.jpg" and poster.exists()
    black = tmp_path / "black.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "2", "-i", str(src), "-frames:v", "1",
                    "-q:v", "3", str(black)], check=True)
    assert poster.stat().st_size > 3 * black.stat().st_size, "a slide beats a camera that was off"
    assert not list(out.glob(".poster_candidate_*")), "candidates are cleaned up"


def test_no_duration_means_no_preview_rather_than_an_error(tmp_path, out):
    assert recording_ingest.make_poster(_clip(tmp_path / "lesson.mp4"), out, duration=None) is None
