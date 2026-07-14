"""Parent domain: parent↔student links and parent-facing endpoints.

Follows the project's domain-module pattern (models.py / schemas.py / routes.py).
"""
from src.parents.routes import router as parents_router

__all__ = ["parents_router"]
