#!/usr/bin/env python3
"""Backfill: enqueue HLS ingest jobs for every step that has a YouTube video.

Scans all steps, and for each YouTube URL (ru = video_url, en = content_text
metadata) upserts a pending job in video_ingest_jobs. The scheduler's video ingest
worker then downloads + transcodes each one. Idempotent: re-running skips steps that
already have a ready/in-flight job for the same URL.

Usage (inside the backend/scheduler container):
    python -m scripts.backfill_video_ingest            # dry run (report only)
    python -m scripts.backfill_video_ingest --apply     # enqueue jobs
    python -m scripts.backfill_video_ingest --apply --force   # re-enqueue even ready ones
"""
import argparse
import sys

# Allow running as a bare script (python scripts/backfill_video_ingest.py)
sys.path.insert(0, ".")

from src.config import SessionLocal
from src.courses.models import Step, VideoIngestJob
from src.services import video_ingest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually enqueue (default: dry run)")
    ap.add_argument("--force", action="store_true", help="re-enqueue even steps already ready")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        steps = db.query(Step).filter(Step.video_url.isnot(None)).all()
        total_steps = 0
        planned = []  # (step_id, lang, url)
        for step in steps:
            urls = video_ingest._lang_urls(step)
            if not urls:
                continue
            total_steps += 1
            for lang, url in urls.items():
                job = db.query(VideoIngestJob).filter_by(step_id=step.id, lang=lang).one_or_none()
                already = job and not args.force and job.youtube_url == url and job.status in ("pending", "processing", "done")
                if already:
                    continue
                planned.append((step.id, lang, url))

        print(f"Steps with a YouTube video: {total_steps}")
        print(f"Jobs to enqueue: {len(planned)}")
        for sid, lang, url in planned[:50]:
            print(f"  step={sid} lang={lang} {url}")
        if len(planned) > 50:
            print(f"  ... and {len(planned) - 50} more")

        if not args.apply:
            print("\nDry run only. Re-run with --apply to enqueue.")
            return

        enqueued = 0
        for step in steps:
            if video_ingest.enqueue_step(db, step, force=args.force):
                enqueued += 1
        db.commit()
        print(f"\n✅ Enqueued jobs for {enqueued} steps. The scheduler worker will process them.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
