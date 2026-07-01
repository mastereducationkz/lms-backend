"""Self-hosted video ingest: YouTube URL -> yt-dlp download -> ffmpeg HLS -> S3.

The worker runs in the scheduler container (see ``run_scheduler``). One job per
(step, language). Enqueue helpers are safe to import from the API process (they only
touch the DB); the actual download/transcode requires yt-dlp + ffmpeg on PATH.

Storage layout (private, streamed through the backend — see storage_service.is_video):
    videos/<step_id>/<lang>/master.m3u8   (+ variant playlists + .ts segments, flat)
The stored ``/uploads/videos/<step_id>/<lang>/master.m3u8`` path is written to
``Step.hls_url`` (ru) / ``Step.hls_url_en`` (en); YouTube stays as the fallback.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional

from src.config import SessionLocal
from src.courses.models import Step, VideoIngestJob
from src.services import storage_service

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

# --- YouTube URL helpers (self-contained; mirrors utils/youtube.ts) ----------
_YT_PATTERNS = [
    re.compile(r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})'),
    re.compile(r'(?:https?://)?(?:www\.)?youtu\.be/([a-zA-Z0-9_-]{11})'),
    re.compile(r'(?:https?://)?(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})'),
    re.compile(r'(?:https?://)?(?:m\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})'),
]
_EN_META_RE = re.compile(r"<!--\s*video_lang_urls:\s*(\{.*?\})\s*-->", re.I | re.S)


def extract_youtube_id(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    for pat in _YT_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def is_youtube_url(url: Optional[str]) -> bool:
    return extract_youtube_id(url) is not None


def extract_en_url(content_text: Optional[str]) -> Optional[str]:
    """EN video URL is stored in a content_text HTML comment: ``<!-- video_lang_urls: {"en": "..."} -->``."""
    if not content_text:
        return None
    m = _EN_META_RE.search(content_text)
    if not m:
        return None
    try:
        return (json.loads(m.group(1)) or {}).get("en")
    except (json.JSONDecodeError, ValueError):
        return None


def _lang_urls(step: Step) -> dict:
    """Map of lang -> YouTube URL for a step (ru = video_url, en = content_text metadata)."""
    urls = {}
    if is_youtube_url(step.video_url):
        urls["ru"] = step.video_url.strip()
    en = extract_en_url(step.content_text)
    if is_youtube_url(en):
        urls["en"] = en.strip()
    return urls


# --- enqueue -----------------------------------------------------------------
def enqueue_step(db, step: Step, *, force: bool = False) -> bool:
    """Upsert ingest jobs for a step's YouTube videos. Idempotent: skips a lang whose
    URL is unchanged and already handled (unless ``force``). Does NOT commit."""
    changed = False
    for lang, url in _lang_urls(step).items():
        job = db.query(VideoIngestJob).filter_by(step_id=step.id, lang=lang).one_or_none()
        if job and not force and job.youtube_url == url and job.status in ("pending", "processing", "done"):
            continue
        if job is None:
            job = VideoIngestJob(step_id=step.id, lang=lang, youtube_url=url, status="pending")
            db.add(job)
        else:
            job.youtube_url = url
            job.status = "pending"
            job.attempts = 0
            job.error = None
        if lang == "ru":
            step.video_status = "pending"
        changed = True
    return changed


def safe_enqueue(db, step: Step) -> None:
    """Enqueue + commit, swallowing all errors — never breaks the caller's request."""
    try:
        if enqueue_step(db, step):
            db.commit()
    except Exception as e:  # pragma: no cover - best-effort
        logger.warning("safe_enqueue failed for step %s: %s", getattr(step, "id", "?"), e)
        try:
            db.rollback()
        except Exception:
            pass


# --- transcode pipeline ------------------------------------------------------
# (height, width, v_bitrate, maxrate, bufsize, a_bitrate)
RENDITIONS = [
    (360, 640, "800k", "856k", "1200k", "96k"),
    (480, 854, "1400k", "1498k", "2100k", "128k"),
    (720, 1280, "2800k", "2996k", "4200k", "128k"),
]


def tools_available() -> bool:
    return bool(shutil.which("yt-dlp") and shutil.which("ffmpeg"))


def _run(cmd: list, timeout: int, capture: bool = False) -> str:
    logger.info("run: %s", " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1500:]
        raise RuntimeError(f"{cmd[0]} exited {proc.returncode}: {tail}")
    return proc.stdout if capture else ""


def _download(url: str, workdir: Path) -> Path:
    out_tmpl = str(workdir / "source.%(ext)s")
    _run(
        ["yt-dlp", "--no-playlist", "-f",
         "bv*[height<=1080]+ba/b[height<=1080]/b",
         "--merge-output-format", "mp4", "-o", out_tmpl, url],
        timeout=1800,
    )
    files = sorted(workdir.glob("source.*"))
    if not files:
        raise RuntimeError("yt-dlp produced no output file")
    return files[0]


def _probe_height(src: Path) -> int:
    try:
        out = _run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", str(src)],
            timeout=60, capture=True,
        )
        return int(out.strip().splitlines()[0])
    except Exception:
        return 720


def _transcode_hls(src: Path, out: Path) -> None:
    """Transcode to an adaptive HLS ladder (flat layout: master.m3u8 + v<N>.m3u8 + v<N>_<seg>.ts)."""
    height = _probe_height(src)
    ladder = [r for r in RENDITIONS if r[0] <= height] or [RENDITIONS[0]]
    n = len(ladder)

    split_labels = "".join(f"[v{i}]" for i in range(n))
    parts = [f"[0:v]split={n}{split_labels}"]
    for i, (h, w, *_rest) in enumerate(ladder):
        parts.append(f"[v{i}]scale=w={w}:h={h}:force_original_aspect_ratio=decrease:force_divisible_by=2[v{i}out]")
    filter_complex = ";".join(parts)

    cmd = ["ffmpeg", "-y", "-i", str(src), "-filter_complex", filter_complex]
    for i, (h, w, vb, mx, bf, ab) in enumerate(ladder):
        cmd += ["-map", f"[v{i}out]", f"-c:v:{i}", "libx264", "-preset", "veryfast",
                "-g", "48", "-keyint_min", "48", "-sc_threshold", "0",
                f"-b:v:{i}", vb, f"-maxrate:v:{i}", mx, f"-bufsize:v:{i}", bf]
    for i, (h, w, vb, mx, bf, ab) in enumerate(ladder):
        cmd += ["-map", "a:0?", f"-c:a:{i}", "aac", f"-b:a:{i}", ab, "-ac", "2"]
    var_map = " ".join(f"v:{i},a:{i}" for i in range(n))
    cmd += ["-f", "hls", "-hls_time", "6", "-hls_playlist_type", "vod",
            "-hls_flags", "independent_segments",
            "-hls_segment_filename", str(out / "v%v_%03d.ts"),
            "-master_pl_name", "master.m3u8",
            "-var_stream_map", var_map,
            str(out / "v%v.m3u8")]
    _run(cmd, timeout=3600)
    if not (out / "master.m3u8").exists():
        raise RuntimeError("ffmpeg did not produce master.m3u8")


def _upload_tree(local_dir: Path, key_prefix: str) -> None:
    for f in sorted(local_dir.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(local_dir).as_posix()
        key = f"{key_prefix}/{rel}"
        storage_service.save(key, f.read_bytes(), content_type=storage_service.content_type_for(key))


def process_job(db, job: VideoIngestJob) -> None:
    """Download + transcode + upload for one job, then update the step. Raises on failure."""
    step = db.get(Step, job.step_id)
    if step is None:
        job.status = "failed"
        job.error = "step no longer exists"
        db.commit()
        return

    workdir = Path(tempfile.mkdtemp(prefix=f"vid_{job.step_id}_{job.lang}_"))
    try:
        source = _download(job.youtube_url, workdir)
        hls_dir = workdir / "hls"
        hls_dir.mkdir()
        _transcode_hls(source, hls_dir)

        prefix = f"videos/{job.step_id}/{job.lang}"
        _upload_tree(hls_dir, prefix)
        master = storage_service.stored_path(f"{prefix}/master.m3u8")

        if job.lang == "ru":
            step.hls_url = master
            step.video_status = "ready"
        else:
            step.hls_url_en = master
        job.status = "done"
        job.error = None
        db.commit()
        logger.info("Video ingest done: step=%s lang=%s -> %s", job.step_id, job.lang, master)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _fail(db, job: VideoIngestJob, exc: Exception) -> None:
    msg = str(exc)[:1900]
    job.error = msg
    if job.attempts >= MAX_ATTEMPTS:
        job.status = "failed"
        step = db.get(Step, job.step_id)
        if step is not None and job.lang == "ru":
            step.video_status = "failed"
    else:
        job.status = "pending"  # transient: retry on a later tick
    db.commit()
    logger.warning("Video ingest failed (step=%s lang=%s attempt=%s): %s",
                   job.step_id, job.lang, job.attempts, msg)


# --- worker ------------------------------------------------------------------
class VideoIngestWorker:
    def __init__(self, poll_interval: int = 15):
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not tools_available():
            logger.warning("yt-dlp/ffmpeg not on PATH — video ingest worker NOT started")
            return
        self._thread = threading.Thread(target=self._loop, name="video-ingest", daemon=True)
        self._thread.start()
        logger.info("🎬 Video ingest worker started (poll=%ss)", self.poll_interval)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            worked = False
            try:
                worked = self._process_one()
            except Exception as e:  # never let the loop die
                logger.error("Video ingest loop error: %s", e, exc_info=True)
            self._stop.wait(1 if worked else self.poll_interval)

    def _process_one(self) -> bool:
        db = SessionLocal()
        try:
            job = (
                db.query(VideoIngestJob)
                .filter(VideoIngestJob.status == "pending")
                .order_by(VideoIngestJob.created_at)
                .first()
            )
            if job is None:
                return False
            job.status = "processing"
            job.attempts += 1
            db.commit()
            try:
                process_job(db, job)
            except Exception as e:
                db.rollback()
                _fail(db, job, e)
            return True
        finally:
            db.close()
