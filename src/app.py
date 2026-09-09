from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, FileResponse, StreamingResponse
from starlette.requests import Request
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv
import logging
import os

from src.config import init_db
from src.routes import register_routes
from src.services import cache_service
from src.services import storage_service
from src.services.media_tokens import normalise_key, verify_media_token

load_dotenv()

MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB
ENABLE_PUBLIC_DOCS = os.getenv("ENABLE_PUBLIC_DOCS", "false").lower() == "true"

app = FastAPI(
    title="LMS Platform API",
    description="Learning Management System API",
    version="1.26.0",
    docs_url="/docs" if ENABLE_PUBLIC_DOCS else None,
    redoc_url="/redoc" if ENABLE_PUBLIC_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_PUBLIC_DOCS else None,
)

init_db()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Warm up the Redis cache client so we surface connection issues at startup
# instead of on the first request. Failure is non-fatal: requests still work.
try:
    if cache_service.is_available():
        logging.info("Cache service ready (Redis connected)")
    else:
        logging.info("Cache service disabled or unreachable; running without Redis cache")
except Exception as exc:
    logging.warning("Cache service init failed: %s", exc)

app.add_middleware(GZipMiddleware, minimum_size=1000)

# Allowed CORS origins. Native mobile requests usually omit Origin and are
# unaffected by CORS, but Expo web/dev tooling sends one. Extend at runtime with
# EXTRA_CORS_ORIGINS="exp://192.168.1.5:8081,http://192.168.1.5:8081" for LAN dev.
_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:8080",
    "http://localhost:5173",
    "http://localhost:5174",
    "https://lms.mastereducation.kz",
    "https://lmsapi.mastereducation.kz",
    "https://lms-master.vercel.app",
    # Expo / React Native dev origins
    "http://localhost:8081",
    "http://localhost:19006",
    "exp://localhost:8081",
]
_ALLOWED_ORIGINS += [o.strip() for o in os.getenv("EXTRA_CORS_ORIGINS", "").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def check_file_size(request: Request, call_next):
    if request.method in ["POST", "PUT", "PATCH"]:
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_UPLOAD_SIZE:
            return JSONResponse(
                status_code=413,
                content={"detail": f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)}MB"}
            )
    response = await call_next(request)
    return response


# Map mutation path prefixes to cache namespaces that may now be stale.
# Keys are leading path segments after the leading slash (lowercase).
# Values are glob patterns understood by ``cache_service.invalidate``.
_MUTATION_INVALIDATION_RULES: dict[str, tuple[str, ...]] = {
    "dashboard": ("dashboard:*", "progress:*"),
    "courses": ("courses:*", "progress:*", "dashboard:*", "admin:*", "analytics:*"),
    "modules": ("courses:*", "progress:*", "analytics:*"),
    "lessons": ("courses:*", "progress:*", "analytics:*"),
    "steps": ("courses:*", "progress:*", "analytics:*"),
    "assignments": ("assignments:*", "progress:*", "dashboard:*", "admin:*", "analytics:*"),
    # Assignment Zero owns planned exam dates, which the exams grid reads, so a
    # planned-date change must invalidate exam reads too.
    "assignment-zero": ("assignment-zero:*", "dashboard:*", "analytics:*", "exams:*"),
    "exams": ("exams:*", "assignment-zero:*", "dashboard:*", "analytics:*"),
    "progress": ("progress:*", "dashboard:*", "courses:*", "analytics:*"),
    "quizzes": ("progress:*", "courses:*", "analytics:*"),
    "events": ("events:*", "dashboard:*"),
    "users": ("admin:*", "dashboard:*", "analytics:*"),
    "groups": ("courses:*", "admin:*", "dashboard:*", "analytics:*"),
    "admin": ("admin:*", "dashboard:*", "courses:*", "analytics:*"),
    "media": ("courses:*",),
    "curator-tasks": ("curator-tasks:*", "dashboard:*"),
    "student-journal": ("student-journal:*",),
    "flashcards": ("flashcards:*",),
    "lesson-requests": ("lesson-requests:*", "events:*"),
    "head-teacher": ("head-teacher:*", "dashboard:*", "courses:*", "events:*", "analytics:*"),
    "trials": ("courses:*", "progress:*", "dashboard:*", "admin:*"),
    # Platform events (IELTS/SAT pushes) feed student progress + dashboard views and, since
    # Phase 2, create/deactivate platform_test assignments.
    "integrations": ("progress:*", "dashboard:*", "assignments:*", "events:*"),
    # Student targets (E5) feed the dashboard tile.
    "targets": ("dashboard:*",),
}

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def invalidate_cache_on_mutation(request: Request, call_next):
    """Drop stale cache entries after any successful mutation.

    Invalidation is intentionally coarse (by domain prefix). Combined with the
    short TTLs configured on individual cache decorators this keeps responses
    fresh without per-endpoint bookkeeping.
    """
    response = await call_next(request)
    try:
        if request.method not in _MUTATING_METHODS:
            return response
        if response.status_code < 200 or response.status_code >= 300:
            return response
        path = request.url.path or ""
        segments = [seg for seg in path.split("/") if seg]
        if not segments:
            return response
        rule = _MUTATION_INVALIDATION_RULES.get(segments[0].lower())
        if not rule:
            return response
        if cache_service.is_available():
            cache_service.invalidate(*rule)
    except Exception as exc:  # never let cache logic break a real response
        logging.debug("Cache invalidation middleware failed: %s", exc)
    return response

def _upload_key(path: str) -> Optional[str]:
    """The one place a request path becomes a storage key.

    ``storage_service`` classifies a key by its first segment, while the backends
    resolve ``.`` and ``..`` away when they fetch — so ``videos/x`` asked for as
    ``materials/../videos/x`` used to be classified non-video, skip the token
    guard, and still land on the video bytes. Everything below is handed the
    result of this one call, so the classification and the fetch cannot disagree.
    Anchoring at ``/`` also means no run of leading ``..`` climbs out of the
    uploads root. Returns ``None`` when nothing addressable is left.
    """
    return normalise_key(path) or None


def _serve_stored(key: str, request: Request):
    """Serve an already-normalised, already-authorised storage key.

    On the S3 backend: HLS videos (``videos/`` prefix) are streamed through the
    backend so relative segment refs stay access-controlled and Range requests
    work; everything else redirects to the resolved (public or presigned) S3 URL.
    On the local backend, stream from the uploads/ dir (dev parity; FileResponse
    handles Range for local videos).

    Deliberately does not normalise ``key`` — its callers already did, once.
    """
    if storage_service.use_s3():
        if storage_service.is_video(key):
            result = storage_service.open_stream(key, request.headers.get("range"))
            if result is None:
                raise HTTPException(status_code=404, detail="File not found")
            status, headers, body = result
            media_type = headers.pop("Content-Type", None)
            return StreamingResponse(body, status_code=status, headers=headers, media_type=media_type)
        return RedirectResponse(storage_service.url_for(key), status_code=307)
    local = storage_service.local_path(key)
    if not local:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(local)


@app.get("/uploads/v/{token}/{path:path}")
def serve_upload_signed(token: str, path: str, request: Request):
    """Stream private media to a holder of a valid prefix-scoped token.

    HLS playlists reference segments relatively, so every segment request arrives
    under this same ``/v/<token>/`` prefix without the player knowing the token
    exists. The route stays confined to video: tokens are only ever minted for a
    ``videos/`` prefix, and refusing everything else here means one mis-scoped
    mint could never turn this into a general reader for ``exam_proof/``.
    """
    key = _upload_key(path)
    if key is None or not storage_service.is_video(key):
        raise HTTPException(status_code=404, detail="File not found")
    if verify_media_token(token, key) is None:
        raise HTTPException(status_code=404, detail="File not found")
    return _serve_stored(key, request)


@app.get("/uploads/{path:path}")
def serve_upload(path: str, request: Request):
    """Serve uploaded files. Video under ``videos/`` requires a signed token and is
    served by ``serve_upload_signed``; requests without one are refused as 404 so the
    endpoint cannot be used to confirm that a recording exists."""
    key = _upload_key(path)
    if key is None or storage_service.is_video(key):
        raise HTTPException(status_code=404, detail="File not found")
    return _serve_stored(key, request)


register_routes(app)


@app.get("/")
def root():
    ascii_art = """⠀⠀⠀⠀⠀⠀⠀⠀⣀⣤⣴⣶⣾⣿⣿⣿⣿⣷⣶⣦⣤⣀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⣠⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣦⣄⠀⠀⠀⠀⠀
⠀⠀⠀⣠⣾⣿⣿⣿⣿⣿⣿⠿⣿⣿⡿⢿⣿⣿⠿⣿⣿⣿⣿⣿⣿⣷⣄⠀⠀⠀
⠀⠀⣴⣿⣿⣿⣿⣿⡟⠻⣿⣆⠸⡿⠁⡈⢿⠏⢰⣿⡟⢻⣿⣿⣿⣿⣿⣦⠀⠀
⠀⣼⣿⣿⣿⣿⣿⣿⣿⣆⠙⢿⡄⠁⣼⣧⠈⣠⣿⠋⣠⣿⣿⣿⣿⣿⣿⣿⣧⠀
⢰⣿⣿⣿⣿⣿⣯⡀⠠⣤⣁⣄⣿⣶⣿⣿⣷⣾⣁⣈⣡⡄⢀⣽⣿⣿⣿⣿⣿⡆
⣾⣿⣿⣿⣿⠛⠛⠛⠦⠙⢿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠋⠠⠟⠛⠛⣿⣿⣿⣿⣷
⣿⣿⣿⣿⣿⣿⣿⠿⠶⠶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠶⠷⢾⣿⣿⣿⣿⣿⣿⣿
⢿⣿⣿⣿⣿⣤⣤⣶⠂⣠⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣄⠰⣦⣤⣤⣿⣿⣿⣿⣿
⠸⣿⣿⣿⣿⣿⣟⠁⠘⣉⡉⢉⡿⢿⣿⣿⠿⣿⠉⢉⡙⠂⡈⣻⣿⣿⣿⣿⣿⠇
⠀⢻⣿⣿⣿⣿⣿⣿⣿⠋⣠⣿⠃⡄⢻⡏⢀⠘⣷⣄⠙⣿⣿⣿⣿⣿⣿⣿⡟⠀
⠀⠀⠹⣿⣿⣿⣿⣿⣧⣼⣿⠇⣰⣷⡀⢀⣿⣆⠹⣿⣧⣼⣿⣿⣿⣿⣿⠟⠀⠀
⠀⠀⠀⠙⢿⣿⣿⣿⣿⣿⣿⣶⣿⣿⣷⣾⣿⣿⣶⣿⣿⣿⣿⣿⣿⡿⠋⠀⠀⠀
⠀⠀⠀⠀⠀⠙⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⠋⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠉⠛⠻⠿⢿⣿⣿⣿⣿⡿⠿⠟⠛⠉⠀⠀⠀⠀⠀⠀⠀⠀"""
    return PlainTextResponse(content=ascii_art, status_code=200)


@app.get("/health")
async def health_check():
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "version": "1.99.7",
        }
    )

# Socket.IO wrapper
from src.messages.routes.socket_messages import create_socket_app
socket_app = create_socket_app(app)

# Background workers
# RabbitMQ is disabled by default: CRM->LMS user sync happens via direct DB writes, not this
# consumer, so leaving it on just spammed "Failed to connect to RabbitMQ" on every boot. Set
# ENABLE_RABBITMQ=true only if a broker is actually deployed and this consumer is wanted.
if os.getenv('ENABLE_RABBITMQ', 'false').strip().lower() in ('1', 'true', 'yes'):
    try:
        from src.services.rabbitmq_consumer import start_rabbitmq_consumer_thread
        start_rabbitmq_consumer_thread()
        logging.info("RabbitMQ consumer initialized")
    except Exception as e:
        logging.error(f"Failed to initialize RabbitMQ consumer: {e}")
else:
    logging.info("RabbitMQ consumer disabled (set ENABLE_RABBITMQ=true to enable)")

try:
    from src.services.lesson_reminder_scheduler import start_lesson_reminder_scheduler
    enable_lesson_in_api = os.getenv('ENABLE_LESSON_REMINDER_IN_API', 'false').lower() == 'true'
    if not enable_lesson_in_api:
        logging.info(
            "Lesson reminder scheduler not started in API (set ENABLE_LESSON_REMINDER_IN_API=true for local dev); "
            "production uses the scheduler container"
        )
    elif os.getenv('RESEND_API_KEY'):
        start_lesson_reminder_scheduler()
        logging.info("Lesson reminder scheduler initialized in API process")
    else:
        logging.warning("RESEND_API_KEY not configured, skipping lesson reminder scheduler in API")
except Exception as e:
    logging.error(f"Failed to initialize lesson reminder scheduler: {e}")

try:
    # Curator TASK scheduler is paused (feature hidden). Reversal: re-enable this
    # and remove the onboarding reconciler below.
    #   from src.curator.services import start_curator_task_scheduler
    #   start_curator_task_scheduler()
    from src.curator.onboarding_service import start_onboarding_reconciler
    if os.getenv('DISABLE_SCHEDULER', 'false').lower() == 'true':
        logging.info("Onboarding reconciler disabled (DISABLE_SCHEDULER=true)")
    else:
        start_onboarding_reconciler()
        logging.info("Onboarding reconciler initialized")
except Exception as e:
    logging.error(f"Failed to initialize onboarding reconciler: {e}")


def _reason_fields(exc):
    """The machine-readable half of a `LessonAccessDenied`, for the generic envelope.

    Additive only: `error`, `message` and `detail` keep the shape every existing client expects,
    and `reason_code` is what new code branches on.
    """
    fields = {}
    code = getattr(exc, "reason_code", None)
    if isinstance(code, str) and code:
        fields["reason_code"] = code
    details = getattr(exc, "reason_details", None)
    if isinstance(details, dict) and details:
        fields["reason_details"] = details
    return fields


@app.exception_handler(404)
def not_found_handler(request, exc):
    content = {"error": "Not Found", "message": "The requested resource was not found", "status_code": 404}
    # Only a reason we wrote ourselves is forwarded. Every other 404 — a mistyped route, an
    # internal "Module not found for this lesson" — keeps the generic body, so nothing about the
    # server's insides reaches a student.
    reason = _reason_fields(exc)
    if reason:
        detail = getattr(exc, "detail", None)
        if isinstance(detail, str) and detail:
            content["detail"] = detail
        content.update(reason)
    return JSONResponse(status_code=404, content=content)

@app.exception_handler(403)
def forbidden_handler(request, exc):
    # Keep the generic envelope, but pass the handler's own reason through as `detail`: the
    # checkpoint gates (and other guards) say *why* access is denied, and the web client reads
    # `response.data.detail` for its error toasts.
    content = {"error": "Forbidden", "message": "You don't have permission to access this resource", "status_code": 403}
    detail = getattr(exc, "detail", None)
    if isinstance(detail, str) and detail:
        content["detail"] = detail
    content.update(_reason_fields(exc))
    return JSONResponse(status_code=403, content=content)

@app.exception_handler(401)
def unauthorized_handler(request, exc):
    return JSONResponse(
        status_code=401,
        content={"error": "Unauthorized", "message": "Authentication required", "status_code": 401}
    )
