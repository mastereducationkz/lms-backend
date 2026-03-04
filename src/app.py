from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.requests import Request
from datetime import datetime
from dotenv import load_dotenv
import logging
import os

from src.config import init_db
from src.routes import register_routes

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

app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:8080",
        "http://localhost:5173",
        "http://localhost:5174",
        "https://lms.mastereducation.kz",
        "https://lmsapi.mastereducation.kz",
        "https://lms-master.vercel.app"
    ],
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

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

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
def health_check():
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
try:
    from src.services.rabbitmq_consumer import start_rabbitmq_consumer_thread
    if os.getenv('RABBITMQ_URL'):
        start_rabbitmq_consumer_thread()
        logging.info("RabbitMQ consumer initialized")
    else:
        logging.warning("RabbitMQ URL not configured, skipping consumer")
except Exception as e:
    logging.error(f"Failed to initialize RabbitMQ consumer: {e}")

try:
    from src.services.lesson_reminder_scheduler import start_lesson_reminder_scheduler
    disable_scheduler = os.getenv('DISABLE_SCHEDULER', 'false').lower() == 'true'
    if disable_scheduler:
        logging.info("Lesson reminder scheduler disabled (DISABLE_SCHEDULER=true)")
    elif os.getenv('RESEND_API_KEY'):
        start_lesson_reminder_scheduler()
        logging.info("Lesson reminder scheduler initialized")
    else:
        logging.warning("RESEND_API_KEY not configured, skipping lesson reminder scheduler")
except Exception as e:
    logging.error(f"Failed to initialize lesson reminder scheduler: {e}")

try:
    from src.curator.services import start_curator_task_scheduler
    disable_scheduler = os.getenv('DISABLE_SCHEDULER', 'false').lower() == 'true'
    if disable_scheduler:
        logging.info("Curator task scheduler disabled (DISABLE_SCHEDULER=true)")
    else:
        start_curator_task_scheduler()
        logging.info("Curator task scheduler initialized")
except Exception as e:
    logging.error(f"Failed to initialize curator task scheduler: {e}")


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