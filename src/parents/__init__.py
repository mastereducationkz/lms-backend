"""Parent domain: parent↔student links and parent-facing endpoints.

Follows the project's domain-module pattern (models.py / schemas.py / routes.py).

NOTE: keep this __init__ import-free. `src.parents.models` is imported very early
(by src/models/__init__.py), so importing routes here would create a circular
import through src.schemas.models. The router is imported directly from
`src.parents.routes` where it's registered (see src/routes/__init__.py).
"""
