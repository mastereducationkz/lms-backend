"""
Storage abstraction for uploaded files.

Two backends, selected by env STORAGE_BACKEND:
  - "local" (default): read/write the local ``uploads/`` dir (dev parity, unchanged behavior).
  - "s3": store objects in an S3 bucket under the ``uploads/`` key prefix.

The DB always stores the SAME stable relative path ``/uploads/<key>`` regardless of backend,
so nothing in the DB or frontend changes. Only *serving* differs: url_for() resolves the key.

Hybrid access model (course content private, user uploads public) is decided by key prefix
(see PRIVATE_PREFIXES). To stay compatible with modern buckets (ACLs disabled + Block Public
Access on), the DEFAULT is presigned URLs for everything — private prefixes get a short TTL,
public prefixes a long TTL. Set S3_PUBLIC_OBJECTS=true (with a bucket policy / CloudFront that
makes the public prefixes readable) to serve public files via stable, fully-cacheable URLs.
"""
import logging
import mimetypes
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# --- config ---------------------------------------------------------------
STORAGE_BACKEND = (os.getenv("STORAGE_BACKEND", "local") or "local").lower()

AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_S3_ENDPOINT = os.getenv("AWS_S3_ENDPOINT") or None  # for S3-compatible providers
S3_PUBLIC_BASE_URL = (os.getenv("S3_PUBLIC_BASE_URL", "") or "").rstrip("/")  # CDN/custom domain

S3_PRESIGN_TTL = int(os.getenv("S3_PRESIGN_TTL", "3600"))            # private objects: 1h
S3_PUBLIC_PRESIGN_TTL = int(os.getenv("S3_PUBLIC_PRESIGN_TTL", "604800"))  # public objs: 7d
# Serve public-prefix files via a stable direct URL (requires the bucket/prefix to be
# publicly readable via bucket policy or CloudFront). Off by default so a fully-private
# bucket works with zero AWS-side config.
S3_PUBLIC_OBJECTS = (os.getenv("S3_PUBLIC_OBJECTS", "false") or "false").lower() == "true"
# Send an object ACL on upload. Off by default: modern buckets disable ACLs and reject this.
S3_USE_ACL = (os.getenv("S3_USE_ACL", "false") or "false").lower() == "true"

# S3 keys mirror the local tree: local ``uploads/<rel>`` -> S3 key ``uploads/<rel>``.
UPLOADS_PREFIX = "uploads"
_LOCAL_ROOT = Path("uploads")

# Course-authoring content -> private. Everything else (teacher/student uploads,
# avatars, chat) -> public.
PRIVATE_PREFIXES = ("courses", "materials", "step_attachments", "sat_images", "question_media", "steps")


def use_s3() -> bool:
    return STORAGE_BACKEND == "s3"


def _norm_key(key: str) -> str:
    """Accept 'cat/file', '/uploads/cat/file' or 'uploads/cat/file' -> 'cat/file'."""
    k = (key or "").strip().lstrip("/")
    if k.startswith("uploads/"):
        k = k[len("uploads/"):]
    return k


def is_public(key: str) -> bool:
    top = _norm_key(key).split("/", 1)[0]
    return top not in PRIVATE_PREFIXES


def stored_path(key: str) -> str:
    """The stable relative path persisted in the DB (and served under /uploads/)."""
    return f"/uploads/{_norm_key(key)}"


def _s3_key(key: str) -> str:
    return f"{UPLOADS_PREFIX}/{_norm_key(key)}"


_s3 = None


def _client():
    global _s3
    if _s3 is None:
        import boto3  # imported lazily so local dev needs no AWS deps at import time
        _s3 = boto3.client("s3", region_name=AWS_REGION, endpoint_url=AWS_S3_ENDPOINT)
    return _s3


def _content_type(key: str, explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    return mimetypes.guess_type(_norm_key(key))[0] or "application/octet-stream"


def save(key: str, data: bytes, content_type: Optional[str] = None) -> str:
    """Persist bytes under ``key`` and return the stable /uploads/<key> path for the DB."""
    nkey = _norm_key(key)
    if use_s3():
        params = {
            "Bucket": AWS_S3_BUCKET,
            "Key": _s3_key(nkey),
            "Body": data,
            "ContentType": _content_type(nkey, content_type),
        }
        if S3_USE_ACL:
            params["ACL"] = "public-read" if is_public(nkey) else "private"
        _client().put_object(**params)
    else:
        p = _LOCAL_ROOT / nkey
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    return stored_path(nkey)


def delete(key: str) -> None:
    nkey = _norm_key(key)
    if use_s3():
        try:
            _client().delete_object(Bucket=AWS_S3_BUCKET, Key=_s3_key(nkey))
        except Exception as e:  # never fail the request over a cleanup error
            logger.warning("S3 delete failed for %s: %s", nkey, e)
    else:
        try:
            (_LOCAL_ROOT / nkey).unlink()
        except FileNotFoundError:
            pass


def url_for(key: str) -> str:
    """Resolve a stored key to a fetchable URL (used by the /uploads serving route)."""
    nkey = _norm_key(key)
    if not use_s3():
        return stored_path(nkey)  # served locally by the app route

    if is_public(nkey):
        if S3_PUBLIC_OBJECTS:
            base = S3_PUBLIC_BASE_URL or f"https://{AWS_S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com"
            return f"{base}/{_s3_key(nkey)}"
        ttl = S3_PUBLIC_PRESIGN_TTL
    else:
        ttl = S3_PRESIGN_TTL

    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": AWS_S3_BUCKET, "Key": _s3_key(nkey)},
        ExpiresIn=ttl,
    )


def local_path(key: str) -> Optional[Path]:
    """Local file path if it exists (used for local serving and migration)."""
    p = _LOCAL_ROOT / _norm_key(key)
    return p if p.exists() else None
