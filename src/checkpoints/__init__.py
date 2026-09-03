"""SAT Checkpoints — unit-gated 45-question assessments (docs/superpowers/plans/2026-09-03-sat-checkpoints.md)."""

# Register every domain model before any submodule of this package runs. Without this,
# ``python -m scripts.seed_sat_checkpoints`` (and any script importing ``src.checkpoints.*``
# first) hits the ``src.models`` <-> ``src.checkpoints.models`` import cycle.
import src.models  # noqa: E402,F401
