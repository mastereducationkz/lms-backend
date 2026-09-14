"""Onboarding a teacher onto the recordings pipeline: their ``workspace_email``.

Setting ``users.workspace_email`` is the whole activation: the recordings worker gives the
teacher's upcoming lessons LMS Meet rooms, auto-records them, sends Telegram invitations to
linked group chats, and makes their groups eligible for the group bot. Until 2026-09-14 this
was done by hand with one-off scripts in the production container; this module is the rule
both admin surfaces now share — the recordings page and the Manage Users edit dialog.

A Workspace account is only *referenced* here, never created: Google user creation happens in
the Admin Console (bulk CSV from ``GET /admin/recordings/teachers/export.csv``), because the
pipeline's OAuth scopes cannot touch ``admin.directory`` and service-account keys are
org-policy-disabled on this tenant.
"""
from __future__ import annotations

import re
import secrets
import string
import unicodedata
from typing import Optional

from src.schemas.models import UserInDB

WORKSPACE_DOMAIN = "mastereducation.kz"

# Roles whose lessons get LMS Meet rooms once onboarded.
TEACHER_ROLES = frozenset({"teacher", "head_teacher"})

# Who may see the onboarding list; only admins may connect or disconnect — the same split
# the group-bot switch uses (src/services/group_bot_settings.py).
READERS = frozenset({"admin", "head_curator", "head_teacher"})
WRITERS = frozenset({"admin"})

# Kazakh Cyrillic → Latin, the same mapping meet_presence uses to fold Meet display names
# against LMS names (kept identical on purpose: a suggestion should look like the name an
# admin already reads on the attendance screens).
_TO_LATIN = str.maketrans({
    "а": "a", "ә": "a", "б": "b", "в": "v", "г": "g", "ғ": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "j", "з": "z", "и": "i", "й": "i", "к": "k", "қ": "k", "л": "l", "м": "m", "н": "n",
    "ң": "n", "о": "o", "ө": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ұ": "u",
    "ү": "u", "ф": "f", "х": "h", "һ": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "",
    "ы": "i", "і": "i", "ь": "", "э": "e", "ю": "iu", "я": "ia",
})

# Patronymic endings, matched against the *transliterated* word: -ұлы/-қызы → uli/kizi,
# Russian -ович/-евна → ovich/evna, etc. Kazakh names carry the patronymic as the last word
# (… Айқынұлы) or sometimes the first (Ержанқызы Елдана), so position is never assumed.
_PATRONYMIC = re.compile(
    r"(uli|kizi|kyzi|uly|ogli|ogly|ovich|evich|ovna|evna|ichna|ich)$")

# Russian-style surname endings (-ов/-ев/-ин/-ова/-ева/-ина/-ский): when exactly one word
# carries one, the other is the given name regardless of order — "Даниил Бутырин" → daniil,
# "Махамаджанов Диербек" → dierbek.
_SURNAME = re.compile(r"(ov|ev|in|ina|ova|eva|skaia|skii|ogly|ogli)$")


def _has_cyrillic(name: Optional[str]) -> bool:
    return bool(re.search(r"[а-яёәғқңөұүһі]", (name or "").lower()))


def _latin_words(name: Optional[str]) -> list:
    text = (name or "").strip().lower().translate(_TO_LATIN)
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return [w for w in re.split(r"[^a-z]+", text) if w]


def given_name_word(name: Optional[str]) -> Optional[str]:
    """The word the existing convention builds the account from — the given name.

    LMS names arrive in two orders: Latin-script "First Last" (Aisha Temirkhan → aisha@) and
    Kazakh/Russian Cyrillic "Surname Given [Patronymic]" (Есен Нұрғалы Беғалыұлы → nurgaly@,
    Кенжебаев Арсен → arsen@). Cyrillic order resolves as: drop patronymics; when exactly
    one of the remaining pair looks like a surname the other is the given name (Даниил
    Бутырин → daniil, Махамаджанов Диербек → dierbek); otherwise the last word — the
    dominant Kazakh order.
    """
    words = _latin_words(name)
    if not words:
        return None
    if not _has_cyrillic(name):
        return words[0]
    non_pat = [w for w in words if not _PATRONYMIC.search(w)]
    if not non_pat:
        return words[-1]
    if len(non_pat) == 1:
        return non_pat[0]
    surnamed = [w for w in non_pat if _SURNAME.search(w)]
    if len(non_pat) == 2 and len(surnamed) == 1:
        return next(w for w in non_pat if w != surnamed[0])
    return non_pat[-1]


def given_name_hint(name: Optional[str], group_names) -> Optional[str]:
    """The name word a teacher's own groups point at — when they point at exactly one.

    Groups here are named "August 3 SAT - Киясбек": the trailing word is how the org calls
    the teacher. That is weaker evidence than a surname or patronymic in the name itself —
    group tails sometimes carry the surname instead ("- Исабеков") — so it is consulted
    only when word-order analysis has no anchor, and only when exactly one name word
    appears in some group's tail (a two-word "- Ақтай Мирас" tail decides nothing).
    """
    words = set(_latin_words(name))
    if not words:
        return None
    for group_name in group_names or []:
        tail = (group_name or "").rsplit(" - ", 1)[-1]
        hits = [w for w in _latin_words(tail) if w in words]
        if len(hits) == 1:
            return hits[0]
    return None


def suggest_workspace_email(name: Optional[str], taken: set, group_names=None) -> Optional[str]:
    """``given@mastereducation.kz``, falling back to ``given.surname@`` on a collision.

    ``taken`` is every workspace_email already claimed (any user). ``group_names`` (the
    teacher's groups) is consulted only when the name itself carries no surname or
    patronymic anchor (see :func:`given_name_hint`). The suggestion is only a default —
    the admin edits it before connecting, so an odd transliteration costs a keystroke,
    not a wrong account.
    """
    words = _latin_words(name)
    if not words:
        return None
    non_pat = [w for w in words if not _PATRONYMIC.search(w)]
    anchored = (
        len(non_pat) <= 1
        or len(non_pat) != len(words)
        or any(_SURNAME.search(w) for w in non_pat)
        or not _has_cyrillic(name)
    )
    given = None
    if not anchored and group_names:
        given = given_name_hint(name, group_names)
    given = given or given_name_word(name)
    if not given:
        return None
    # Disambiguation prefers the surname (the other non-patronymic word), then any other
    # word — even a patronymic — before stooping to a trailing digit.
    others = [w for w in non_pat if w != given] or [w for w in words if w != given]
    candidates = [given]
    if others:
        candidates.append(f"{given}.{others[0] if _has_cyrillic(name) else others[-1]}")
    for local in candidates:
        if f"{local}@{WORKSPACE_DOMAIN}" not in taken:
            return f"{local}@{WORKSPACE_DOMAIN}"
    return f"{candidates[-1]}{secrets.choice(string.digits)}@{WORKSPACE_DOMAIN}"


def generate_import_password(length: int = 12) -> str:
    """A temporary password for the Admin-Console import; the CSV forces a change at login."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def validate_workspace_email(db, user: UserInDB, email: Optional[str]) -> Optional[str]:
    """Normalise and check an address before it becomes the activation switch.

    Returns the cleaned value to store (None = disconnect). Raises ValueError with a
    human-readable reason — the route layer turns it into a 400.
    """
    if email is None:
        return None
    cleaned = email.strip().lower()
    if not cleaned:
        return None
    if user.role not in TEACHER_ROLES:
        raise ValueError("Only teachers and head teachers can be connected to recordings")
    if not cleaned.endswith(f"@{WORKSPACE_DOMAIN}"):
        raise ValueError(f"Workspace email must end with @{WORKSPACE_DOMAIN}")
    local = cleaned[: -len(WORKSPACE_DOMAIN) - 1]
    if not re.fullmatch(r"[a-z0-9]+([._-][a-z0-9]+)*", local):
        raise ValueError("Not a valid mailbox name")
    other = (
        db.query(UserInDB)
        .filter(UserInDB.workspace_email == cleaned, UserInDB.id != user.id)
        .first()
    )
    if other is not None:
        raise ValueError(f"{cleaned} is already connected to {other.name or other.email}")
    return cleaned


def set_workspace_email(db, actor: UserInDB, user: UserInDB, email: Optional[str]) -> UserInDB:
    """Connect or disconnect a teacher. ValueError propagates to the route as a 400."""
    import logging

    cleaned = validate_workspace_email(db, user, email)
    if cleaned == user.workspace_email:
        return user
    logging.getLogger(__name__).info(
        "recordings onboarding: %s set %s workspace_email %s -> %s (was %s)",
        getattr(actor, "id", None), user.id, user.workspace_email, cleaned, user.email,
    )
    user.workspace_email = cleaned
    db.commit()
    db.refresh(user)
    return user
