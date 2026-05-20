"""Russian-name matcher backed by pymorphy3.

Indexes seed persons by lemmatized surname. Lemmatization collapses all
case forms ("Шехтермана", "Шехтерману", "Шехтермане") to the same lemma,
so news text in any grammatical case still matches the canonical name in
the seed list.

The matcher returns *canonical* full_name strings — meaning the form
stored in ``persons.full_name``. Names not in the seed are ignored.

Limitations (acceptable on MVP):

* Surnames are assumed unique within a company. For X5 this holds (13 unique
  surnames). If two persons share a surname, we'd need first-name disambiguation;
  add that when it actually happens.
* Compound surnames ("Иванов-Петров") aren't handled specially — first token
  wins for indexing, second for fallback.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache

import pymorphy3

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[А-Яа-яЁёA-Za-z]+")


@dataclass(frozen=True)
class SeedPerson:
    id: int
    full_name: str
    surname_lemma: str


@lru_cache(maxsize=1)
def _morph() -> pymorphy3.MorphAnalyzer:
    return pymorphy3.MorphAnalyzer()


def _lemma(word: str) -> str:
    return _morph().parse(word)[0].normal_form.lower()


class NameMatcher:
    """Matches Russian-language text to seed persons by lemmatized surname."""

    def __init__(self, persons: list[SeedPerson]) -> None:
        # surname_lemma -> list[SeedPerson]  (list allows future disambiguation)
        self._index: dict[str, list[SeedPerson]] = {}
        for p in persons:
            self._index.setdefault(p.surname_lemma, []).append(p)
        log.debug("name_matcher indexed %d persons, %d unique surnames",
                  len(persons), len(self._index))

    @classmethod
    def from_db(cls, conn: sqlite3.Connection, company_id: int) -> "NameMatcher":
        rows = conn.execute(
            "SELECT id, full_name FROM persons WHERE company_id = ?",
            (company_id,),
        ).fetchall()
        seed = []
        for row in rows:
            surname = _extract_surname(row["full_name"])
            if surname is None:
                log.warning("cannot extract surname from %r", row["full_name"])
                continue
            seed.append(SeedPerson(
                id=row["id"],
                full_name=row["full_name"],
                surname_lemma=_lemma(surname),
            ))
        return cls(seed)

    def match(self, text: str) -> list[SeedPerson]:
        """Return seed persons mentioned in ``text``, deduplicated, in order
        of first appearance."""
        if not text:
            return []

        seen: set[int] = set()
        result: list[SeedPerson] = []
        for token in _WORD_RE.findall(text):
            if len(token) < 4:
                # Skip noise: initials, conjunctions, etc.
                continue
            lemma = _lemma(token)
            candidates = self._index.get(lemma)
            if not candidates:
                continue
            # MVP: assume surname unique → first candidate.
            cand = candidates[0]
            if cand.id in seen:
                continue
            seen.add(cand.id)
            result.append(cand)
        return result


def _extract_surname(full_name: str) -> str | None:
    """Take the *last* capitalised word as the surname.

    Works for "Игорь Шехтерман", "Михаил Фридман", "Екатерина Лобачёва".
    Does not yet handle middle names / patronymics (no patronymics in seed).
    """
    parts = [p for p in re.split(r"\s+", full_name.strip()) if p]
    return parts[-1] if parts else None
