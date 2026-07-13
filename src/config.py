from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import os
import logging
from dotenv import load_dotenv
from typing import Generator
from src.schemas.models import Base, UserInDB, Course, Module, Lesson, Group, Enrollment, StudentProgress, Assignment, AssignmentSubmission, Message, LessonMaterial
from passlib.context import CryptContext

load_dotenv()

# Configure logging
logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

POSTGRES_URL = os.getenv("POSTGRES_URL")
if not POSTGRES_URL:
    # No DB configured (e.g. CI / local import-only test runs). Use a dummy,
    # never-connected URL so `create_engine` (which is lazy) can be built and
    # modules import cleanly. Production always sets POSTGRES_URL.
    logger.warning("POSTGRES_URL not set; using a placeholder URL (no real database).")
    POSTGRES_URL = "postgresql+psycopg2://placeholder:placeholder@localhost:5432/placeholder"

# Azure OpenAI Configuration
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")

# Database setup with connection pooling.
# Sizing is deliberately bounded so the whole fleet stays under Postgres's default
# max_connections=100: the app runs 4 uvicorn workers (see Dockerfile), and each worker opens up
# to pool_size + max_overflow connections. 4 * (10 + 10) = 80 leaves headroom for the scheduler
# container, migrations and admin sessions. (The previous 10 + 20 allowed 4 * 30 = 120, which can
# exhaust the server's connection slots under load and surface as "too many connections" errors.)
# pool_timeout makes a brief pool shortage wait instead of erroring immediately.
engine = create_engine(
    POSTGRES_URL,
    pool_size=10,
    max_overflow=10,
    pool_timeout=30,
    pool_pre_ping=True,
    pool_recycle=3600
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

logger.info("Database connection initialized")

def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """Initialize the database and create tables if they don't exist."""
    logger.info("Initializing the database...")
    Base.metadata.create_all(bind=engine)
    create_initial_admin()

def create_initial_admin():
    """Create initial admin user from environment variables if configured."""
    # Get admin credentials from environment variables
    admin_email = os.getenv("INITIAL_ADMIN_EMAIL")
    admin_password = os.getenv("INITIAL_ADMIN_PASSWORD")
    admin_name = os.getenv("INITIAL_ADMIN_NAME", "Admin")
    
    if not admin_email or not admin_password:
        logger.info("No initial admin credentials configured (INITIAL_ADMIN_EMAIL/INITIAL_ADMIN_PASSWORD)")
        return
    
    db = SessionLocal()
    try:
        # Check if admin already exists
        admin = db.query(UserInDB).filter(UserInDB.email == admin_email).first()
        if not admin:
            logger.info(f"Creating initial admin user: {admin_name}")
            hashed_password = pwd_context.hash(admin_password)
            admin_user = UserInDB(
                email=admin_email,
                name=admin_name,
                hashed_password=hashed_password,
                role="admin",
                is_active=True
            )
            db.add(admin_user)
            db.commit()
            logger.info("Initial admin created successfully")
        else:
            logger.info("Admin user already exists")
    except Exception as e:
        logger.error(f"Error creating initial admin: {e}")
        db.rollback()
    finally:
        db.close()

def reset_db():
    logger.warning("Dropping all tables...")
    Base.metadata.drop_all(bind=engine)
    logger.info("Recreating all tables...")
    Base.metadata.create_all(bind=engine)