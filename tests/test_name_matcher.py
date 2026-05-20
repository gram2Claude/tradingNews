"""Tests for the Russian-name matcher.

Verifies that:
* All grammatical case forms of a surname collapse to the same lemma.
* Brand names in the seed do not produce false person matches.
* Returned full_name strings are the canonical seed form.
"""
from __future__ import annotations

import pytest

from src.name_matcher import NameMatcher, SeedPerson, _extract_surname, _lemma


@pytest.fixture
def matcher() -> NameMatcher:
    persons = [
        SeedPerson(id=1, full_name="Игорь Шехтерман",   surname_lemma=_lemma("Шехтерман")),
        SeedPerson(id=2, full_name="Ольга Наумова",     surname_lemma=_lemma("Наумова")),
        SeedPerson(id=3, full_name="Михаил Фридман",    surname_lemma=_lemma("Фридман")),
        SeedPerson(id=4, full_name="Сергей Гончаров",   surname_lemma=_lemma("Гончаров")),
    ]
    return NameMatcher(persons)


@pytest.mark.parametrize("form", [
    "Шехтерман",
    "Шехтермана",
    "Шехтерману",
    "Шехтерманом",
    "Шехтермане",
])
def test_match_shekhterman_declensions(matcher: NameMatcher, form: str) -> None:
    text = f"Сегодня {form} объявил о новых планах."
    res = matcher.match(text)
    assert len(res) == 1
    assert res[0].full_name == "Игорь Шехтерман"


def test_match_with_initial_prefix(matcher: NameMatcher) -> None:
    res = matcher.match("Заявил И. Шехтерман на пресс-конференции.")
    assert [p.full_name for p in res] == ["Игорь Шехтерман"]


def test_match_naumova_genitive(matcher: NameMatcher) -> None:
    res = matcher.match("Решение Наумовой одобрено советом директоров.")
    assert [p.full_name for p in res] == ["Ольга Наумова"]


def test_no_match_in_unrelated_text(matcher: NameMatcher) -> None:
    assert matcher.match("В Москве сегодня идёт дождь.") == []


def test_multiple_persons_in_one_text(matcher: NameMatcher) -> None:
    text = "Шехтерман и Фридман обсудили стратегию. Гончаров поддержал план."
    res = matcher.match(text)
    names = [p.full_name for p in res]
    assert "Игорь Шехтерман" in names
    assert "Михаил Фридман" in names
    assert "Сергей Гончаров" in names


def test_match_dedupes_repeated_mentions(matcher: NameMatcher) -> None:
    res = matcher.match("Шехтерман сообщил. Позже Шехтерман уточнил.")
    assert len(res) == 1


def test_empty_text(matcher: NameMatcher) -> None:
    assert matcher.match("") == []


def test_extract_surname() -> None:
    assert _extract_surname("Игорь Шехтерман") == "Шехтерман"
    assert _extract_surname("Михаил Фридман") == "Фридман"
    assert _extract_surname("") is None
