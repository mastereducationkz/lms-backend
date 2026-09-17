"""Settings for the whole test session, applied before any test imports the app."""
import os

# `import src.app` starts the onboarding reconciler thread, whose first sweep runs at once over the
# whole test database while holding a Postgres advisory lock. Any test calling
# `reconcile_onboarding` during that sweep gets «skipped» and no cards — in a full run
# test_head_can_intervene_on_any_card failed exactly so (IndexError on an empty board) — and the
# sweep writes cards into the database behind the tests' backs. Tests never want it running.
os.environ["DISABLE_SCHEDULER"] = "true"
