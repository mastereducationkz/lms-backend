from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, FileResponse, StreamingResponse
from starlette.requests import Request
from datetime import datetime
from dotenv import load_dotenv
import logging
import os

from src.config import init_db
from src.routes import register_routes
from src.services import cache_service
from src.services import storage_service

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
    "integrations": ("progress:*", "dashboard:*", "assignments:*"),
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

@app.get("/uploads/{path:path}")
def serve_upload(path: str, request: Request):
    """Serve uploaded files. On S3 backend: HLS videos (``videos/`` prefix) are
    streamed through the backend so relative segment refs stay access-controlled and
    Range requests work; everything else redirects to the resolved (public or
    presigned) S3 URL. On local backend, stream from the uploads/ dir (dev parity;
    FileResponse handles Range for local videos)."""
    if storage_service.use_s3():
        if storage_service.is_video(path):
            result = storage_service.open_stream(path, request.headers.get("range"))
            if result is None:
                raise HTTPException(status_code=404, detail="File not found")
            status, headers, body = result
            media_type = headers.pop("Content-Type", None)
            return StreamingResponse(body, status_code=status, headers=headers, media_type=media_type)
        return RedirectResponse(storage_service.url_for(path), status_code=307)
    local = storage_service.local_path(path)
    if not local:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(local)


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


@app.exception_handler(404)
def not_found_handler(request, exc):
    return JSONResponse(
        status_code=404,
        content={"error": "Not Found", "message": "The requested resource was not found", "status_code": 404}
    )

@app.exception_handler(403)
def forbidden_handler(request, exc):
    return JSONResponse(
        status_code=403,
        content={"error": "Forbidden", "message": "You don't have permission to access this resource", "status_code": 403}
    )

@app.exception_handler(401)
def unauthorized_handler(request, exc):
    return JSONResponse(
        status_code=401,
        content={"error": "Unauthorized", "message": "Authentication required", "status_code": 401}
    )
