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
# avatars, chat) -> public. ``videos`` (self-hosted HLS) is private and, unlike the
# other private prefixes, is *streamed* through the backend rather than redirected —
# HLS playlists reference their segments by relative path, which presigned URLs break.
# exam_proof holds College Board / IELTS score reports - a student's name, photo-grade
# identity data and their result. It must never fall into the public-URL class the way
# assignment_zero screenshots do, where the only protection is an unguessable filename.
PRIVATE_PREFIXES = ("courses", "materials", "step_attachments", "sat_images", "question_media", "steps", "videos", "exam_proof", "testimonial_media", "bluebook_report")

# Content types for streamed HLS assets (mimetypes doesn't know .m3u8/.ts/.m4s).
_HLS_CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".vtt": "text/vtt",
}


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
        from botocore.config import Config
        # Use the REGIONAL endpoint + SigV4 so presigned URLs validate. The global
        # s3.amazonaws.com host rejects presigned GETs for non-us-east-1 buckets (403).
        endpoint = AWS_S3_ENDPOINT or (f"https://s3.{AWS_REGION}.amazonaws.com" if AWS_REGION else None)
        _s3 = boto3.client(
            "s3",
            region_name=AWS_REGION,
            endpoint_url=endpoint,
            config=Config(signature_version="s3v4"),
        )
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


def read(key: str) -> Optional[bytes]:
    """Read an object's full bytes, whichever backend is active.

    Used where the server must re-verify a stored file rather than trust what the
    client said about it (see the Bluebook report re-parse). Returns None when the
    object is missing rather than raising, so callers can degrade instead of 500ing.
    """
    nkey = _norm_key(key)
    if use_s3():
        try:
            obj = _client().get_object(Bucket=AWS_S3_BUCKET, Key=_s3_key(nkey))
            return obj["Body"].read()
        except Exception as e:
            logger.warning("S3 read failed for %s: %s", nkey, e)
            return None
    p = _LOCAL_ROOT / nkey
    try:
        return p.read_bytes()
    except (FileNotFoundError, OSError):
        return None


def local_path(key: str) -> Optional[Path]:
    """Local file path if it exists (used for local serving and migration)."""
    p = _LOCAL_ROOT / _norm_key(key)
    return p if p.exists() else None


def content_type_for(key: str) -> str:
    ext = os.path.splitext(_norm_key(key))[1].lower()
    return _HLS_CONTENT_TYPES.get(ext) or mimetypes.guess_type(_norm_key(key))[0] or "application/octet-stream"


def is_video(key: str) -> bool:
    """True for keys under the ``videos/`` prefix (self-hosted HLS, streamed not redirected)."""
    return _norm_key(key).split("/", 1)[0] == "videos"


def open_stream(key: str, range_header: Optional[str] = None, chunk_size: int = 262144):
    """Stream an object's bytes from S3 (used for HLS, where relative segment refs
    rule out presigned redirects). Returns ``(status, headers, iterator)`` or ``None``
    if the object is missing. Honors a single HTTP Range header for seeking."""
    from botocore.exceptions import ClientError

    nkey = _norm_key(key)
    ct = content_type_for(nkey)
    params = {"Bucket": AWS_S3_BUCKET, "Key": _s3_key(nkey)}
    if range_header:
        params["Range"] = range_header
    try:
        resp = _client().get_object(**params)
    except ClientError as e:
        err = e.response.get("Error", {}) or {}
        code = err.get("Code", "")
        http_status = (e.response.get("ResponseMetadata", {}) or {}).get("HTTPStatusCode")
        if code in ("NoSuchKey", "NoSuchBucket") or http_status == 404:
            return None
        if code == "InvalidRange" or http_status == 416:
            return 416, {"Content-Type": ct, "Accept-Ranges": "bytes"}, iter(())
        raise

    body = resp["Body"]
    headers = {"Content-Type": ct, "Accept-Ranges": "bytes"}
    if resp.get("ContentLength") is not None:
        headers["Content-Length"] = str(resp["ContentLength"])
    status = 200
    if resp.get("ContentRange"):
        headers["Content-Range"] = resp["ContentRange"]
        status = 206

    def _iterator():
        try:
            for chunk in body.iter_chunks(chunk_size):
                yield chunk
        finally:
            body.close()

    return status, headers, _iterator()
