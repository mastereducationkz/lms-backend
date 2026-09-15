"""English names and addresses for teachers' Google Workspace accounts.

Owner decision 2026-09-14: every name in the Google Admin import is English, spelled the way
Kazakhstan passports and NU addresses spell names — Жансерик → Zhanserik, Нұрғалы → Nurgaly,
Ерполат → Yerpolat, Нурай → Nuray, Айша → Aisha — and the suggested address uses the same
spelling (zhanserik@). Checked against production that day: the rule reproduces the eleven
pilot addresses people chose by hand (nurgaly, bekdaulet, zhansaya, arailym, …) and the NU
addresses of the pending teachers (yerkebulan.bolat, adilet.aitzhan, abay.dulatuly, miras.aktay).

This is deliberately not the spelling ``meet_presence`` folds names with. That one only has to
make two spellings of a name compare equal; this one is a name a person reads.

Which words make the name, in order of trust:

1. the teacher's own Latin LMS name when it has a given name and a surname ("Gulzada
   Kassymbayeva" — their own spelling beats any transliteration);
2. the official ФИО the CRM syncs into ``users.official_full_name`` — «Фамилия Имя Отчество»
   since 2026-09-15, when the owner made every teacher name surname-first ("we ask for ФИО");
   until then it was mostly «Имя Фамилия», so a surname or patronymic ending still decides
   when it disagrees with the order;
3. the LMS name, also «Фамилия Имя Отчество».
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterator, List, Optional, Tuple

DOMAIN = "mastereducation.kz"

_VOWELS = frozenset("аәеёиоөуұүыіэюя")
_LETTERS = {
    "а": "a", "ә": "a", "б": "b", "в": "v", "г": "g", "ғ": "g", "д": "d", "ж": "zh", "з": "z",
    "и": "i", "к": "k", "қ": "k", "л": "l", "м": "m", "н": "n", "ң": "n", "о": "o", "ө": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ұ": "u", "ү": "u", "ф": "f", "х": "kh",
    "һ": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "і": "i", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}
_CYRILLIC = re.compile(r"[а-яёәғқңөұүһі]", re.IGNORECASE)

# Patronymic endings on the romanized word: -ұлы/-улы, -қызы/-кызы, -ович/-евич/-овна/-евна.
_PATRONYMIC = re.compile(r"(uly|kyzy|ovich|evich|ovna|evna|ichna)$")
# Surname endings no given name carries.
_SURNAME = re.compile(r"(ov|ev|ova|eva|skiy|skaya|enko)$")
# -ин/-ина also end given names (Alina, Madina, Karina), so they only count on a long word
# (Butyrin, Kobeisin), and only to turn a «Фамилия Имя» reading around.
_WEAK_SURNAME = re.compile(r"(in|yn|ina|yna)$")
# Heads' official names carry their title: "Head of NUET Альбар Керимхан".
_TITLE_PREFIX = re.compile(r"^\s*head\s+of\s+\S+\s+", re.IGNORECASE)


def romanize(word: str) -> str:
    """One word in passport-style English letters: lowercase a–z, hyphen and apostrophe."""
    text = unicodedata.normalize("NFC", word or "").lower()
    out = []
    for i, ch in enumerate(text):
        prev = text[i - 1] if i else ""
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if ch in "её":
            base = "e" if ch == "е" else "o"
            # Ye at the start of a word and after a vowel or a sign: Yernur, Kenzhebayev.
            out.append("y" + base if (not prev.isalpha() or prev in _VOWELS or prev in "ъь") else base)
        elif ch == "й":
            # i before a consonant (Aisha, Aitzhan, Arailym), y otherwise (Nuray, Abay).
            out.append("i" if nxt.isalpha() and nxt not in _VOWELS else "y")
        else:
            out.append(_LETTERS.get(ch, ch))
    latin = "".join(c for c in unicodedata.normalize("NFKD", "".join(out)) if not unicodedata.combining(c))
    return re.sub(r"[^a-z'-]", "", latin).strip("-'")


def has_cyrillic(text: Optional[str]) -> bool:
    return bool(_CYRILLIC.search(text or ""))


def _words(text: Optional[str]) -> List[str]:
    return [w for w in (romanize(part) for part in re.split(r"[\s,]+", text or "")) if w]


def _title(word: str) -> str:
    return "-".join(part[:1].upper() + part[1:] for part in word.split("-") if part)


def _strong(word: str) -> bool:
    return bool(_SURNAME.search(word) or _PATRONYMIC.search(word))


def _resolve(words: List[str], *, given_first: bool) -> Tuple[str, List[str]]:
    """(given name, surname words) from romanized words in the order they were written."""
    if len(words) >= 3 and _PATRONYMIC.search(words[-1]):
        words = words[:-1]
    if len(words) == 1:
        return words[0], []
    if len(words) == 2:
        a, b = words
        if _strong(a) != _strong(b):
            # One surname or patronymic ending decides, whichever order it was written in.
            return (b, [a]) if _strong(a) else (a, [b])
        if not given_first and not _strong(a):
            if len(b) >= 7 and _WEAK_SURNAME.search(b) and not _WEAK_SURNAME.search(a):
                return a, [b]  # Даниил Бутырин
    if given_first:
        return words[0], words[1:]
    return words[1], [words[0]] + words[2:]


def _agrees(own: List[str], official: List[str]) -> bool:
    """Whether a Latin LMS name is the same person's name as the official one.

    "Gulzada Kassymbayeva" agrees with «Гулзада Касымбаева» (a spelling difference);
    "Albar Head" does not agree with «Альбар Керимхан» (a label, not a surname)."""
    if not official:
        return True
    return any(SequenceMatcher(None, a, b).ratio() >= 0.75 for a in own[1:] for b in official)


def english_name(name: Optional[str], official_full_name: Optional[str] = None) -> Tuple[str, str]:
    """(first, last) in English. ``last`` is empty when no source names a surname."""
    own = _words(name)
    official = _words(_TITLE_PREFIX.sub("", official_full_name or ""))
    if len(own) >= 2 and not has_cyrillic(name) and _agrees(own, official):
        given, rest = _resolve(own, given_first=True)
    elif len(official) >= 2 or (official and not own):
        given, rest = _resolve(official, given_first=False)
    elif own:
        given, rest = _resolve(own, given_first=not has_cyrillic(name))
    else:
        return "", ""
    return _title(given), " ".join(_title(w) for w in rest)


def local_part(word: str) -> str:
    return re.sub(r"[^a-z]", "", (word or "").lower())


def address_candidates(first: str, last: str) -> Iterator[str]:
    """given@, then given.surname@, then given.surname2@ … — deterministic, never random,
    so the page and the import always agree on the same teacher's address."""
    given = local_part(first)
    if not given:
        return
    yield f"{given}@{DOMAIN}"
    surname = local_part((last or "").split(" ")[0])
    stem = f"{given}.{surname}" if surname else given
    if surname:
        yield f"{stem}@{DOMAIN}"
    for n in range(2, 100):
        yield f"{stem}{n}@{DOMAIN}"


def suggest_address(first: str, last: str, taken: set) -> Optional[str]:
    for candidate in address_candidates(first, last):
        if candidate not in taken:
            return candidate
    return None
