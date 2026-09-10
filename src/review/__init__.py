"""Quiz Review Mode — read-only teacher analytics over an already-taken unit quiz."""

# Register every domain model before any submodule of this package runs. Without this,
# entering the model graph through a leaf (src.auth.models) re-enters src/models/__init__.py
# while it is still mid-initialisation, and the import fails with:
#   ImportError: cannot import name 'UserInDB' from partially initialized module 'src.auth.models'
# Production dodges this only because register_routes happens to warm a dozen other modules
# first; any script or worker that touches src.review first (e.g. `python -m ...`) would not.
# src.checkpoints hit the same cycle first; this mirrors its fix.
import src.models  # noqa: E402,F401
