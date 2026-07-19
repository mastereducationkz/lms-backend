"""Trials domain: sales-granted, time-boxed lesson-allowlist access for prospects.

The aggregator import below is load-bearing. `src.models/__init__` re-exports
`TrialAccess` from `src.trials.models`, so importing `src.trials.models` as the
process's first model import would re-enter a partially initialized module
(same latent cycle every domain models module has, e.g. src.parents.models).
Importing the aggregator here breaks the cycle: package __init__ runs before
`src.trials.models` exists in sys.modules, so `src.models` finishes loading
first and its re-export imports our models module exactly once, fully.
"""

import src.models  # noqa: F401
