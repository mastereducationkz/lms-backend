# Backward-compatible shim: re-exports all models and schemas from their new locations.
# Existing code that does `from src.schemas.models import X` will continue to work.
# New code should import directly from the domain modules instead.

from src.models import *  # noqa: F401,F403
from src.auth.schemas import *  # noqa: F401,F403
from src.courses.schemas import *  # noqa: F401,F403
from src.assignments.schemas import *  # noqa: F401,F403
from src.progress.schemas import *  # noqa: F401,F403
from src.events.schemas import *  # noqa: F401,F403
from src.messages.schemas import *  # noqa: F401,F403
from src.gamification.schemas import *  # noqa: F401,F403
from src.content.schemas import *  # noqa: F401,F403
from src.curator.schemas import *  # noqa: F401,F403
from src.lesson_requests.schemas import *  # noqa: F401,F403
from src.trials.models import TrialAccess  # noqa: F401
