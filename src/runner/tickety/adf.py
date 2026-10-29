"""Splaszczenie opisu Jira Cloud (ADF) do czytelnego tekstu (HAR-61, BRO-111).

Opis zgloszenia z Jiry to drzewo JSON; do ticket.json trafia z niego tylko tekst
z zachowanymi listami, blokami kodu i adresami linkow. Funkcja jest czysta, bez sieci.
"""
from __future__ import annotations

import math

from runner.tickety.bledy import BladIntegracji

# Wezly blokowe; nieznany wezel jest blokowy, gdy ma blokowe dziecko (rekurencyjnie).
_BLOKOWE = frozenset(
    {"paragraph", "heading", "codeBlock", "rule", "bulletList", "orderedList"}
)

_ZNACZNIK_PUNKTU = "- "
_MAKS_POZIOM_NAGLOWKA = 6


def adf_na_tekst(doc: dict | str | None) -> str:
    """Zamienia opis ADF na tekst; napis (wiki-markup Data Center) wraca bez zmian."""
    if doc is None:
        return ""
    if isinstance(doc, str):
        return doc
    if not isinstance(doc, dict):
        return ""
    try:
        return _wezel(doc)
    except RecursionError:
        raise BladIntegracji(
            "jira",
            "blad_dostawcy",
            "jira: opis zgłoszenia w ADF jest zbyt głęboko zagnieżdżony, "
            "nie da się go spłaszczyć.",
        ) from None


def _slowniki(wartosc: object) -> list[dict]:
    """Elementy-slowniki z listy; cokolwiek innego traktujemy jak brak."""
    if not isinstance(wartosc, list):
        return []
    return [element for element in wartosc if isinstance(element, dict)]


def _dzieci(wezel: dict) -> list[dict]:
    return _slowniki(wezel.get("content"))


def _atrybuty(wezel: dict) -> dict:
    atrybuty = wezel.get("attrs")
    return atrybuty if isinstance(atrybuty, dict) else {}


def _napis(slownik: dict, klucz: str) -> str:
    wartosc = slownik.get(klucz)
    return wartosc if isinstance(wartosc, str) else ""


def _blokowy(wezel: dict) -> bool:
    return wezel.get("type") in _BLOKOWE or any(_blokowy(d) for d in _dzieci(wezel))


def _inline(wezel: dict) -> str:
    return "".join(_wezel(dziecko) for dziecko in _dzieci(wezel))


def _bloki(wezel: dict, separator: str) -> str:
    """Skleja niepuste dzieci; puste bloki nie robia podwojnej pustej linii."""
    czesci = (_wezel(dziecko) for dziecko in _dzieci(wezel))
    return separator.join(czesc for czesc in czesci if czesc)


def _wezel(wezel: dict) -> str:
    typ = wezel.get("type")
    atrybuty = _atrybuty(wezel)
    if typ == "text":
        return _tekst(wezel)
    if typ == "hardBreak":
        return "\n"
    if typ == "rule":
        return "---"
    if typ == "inlineCard":
        return _napis(atrybuty, "url")
    if typ == "mention":
        nazwa = _napis(atrybuty, "text").removeprefix("@")
        return "@" + nazwa if nazwa else ""
    if typ == "emoji":
        return _napis(atrybuty, "shortName") or _napis(atrybuty, "text")
    if typ == "paragraph":
        return _inline(wezel)
    if typ == "heading":
        return "#" * _poziom_naglowka(atrybuty) + " " + _inline(wezel)
    if typ == "codeBlock":
        jezyk = _napis(atrybuty, "language")
        kod = "".join(_napis(dziecko, "text") for dziecko in _dzieci(wezel))
        return f"```{jezyk}\n{kod}\n```"
    if typ == "bulletList":
        return _lista(wezel, uporzadkowana=False)
    if typ == "orderedList":
        return _lista(wezel, uporzadkowana=True)
    # Nieznany wezel: nigdy nie serializujemy jego attrs, tylko tekst dzieci.
    if any(_blokowy(dziecko) for dziecko in _dzieci(wezel)):
        return _bloki(wezel, "\n\n")
    return _inline(wezel)


def _liczba_w_zakresie(wartosc: object, minimum: int, maksimum: float) -> int:
    """Liczba calkowita (nie bool) z zakresu; inaczej `minimum`."""
    if (
        isinstance(wartosc, int)
        and not isinstance(wartosc, bool)
        and minimum <= wartosc <= maksimum
    ):
        return wartosc
    return minimum


def _poziom_naglowka(atrybuty: dict) -> int:
    return _liczba_w_zakresie(atrybuty.get("level"), 1, _MAKS_POZIOM_NAGLOWKA)


def _tekst(wezel: dict) -> str:
    tekst = _napis(wezel, "text")
    znaczniki = _slowniki(wezel.get("marks"))
    # Kod zawsze wewnatrz, adres linku na zewnatrz - niezaleznie od kolejnosci w marks.
    if any(z.get("type") == "code" for z in znaczniki):
        tekst = f"`{tekst}`"
    for znacznik in znaczniki:
        if znacznik.get("type") == "link":
            adres = _napis(_atrybuty(znacznik), "href")
            if adres:
                tekst = f"{tekst} ({adres})"
    return tekst


def _lista(wezel: dict, *, uporzadkowana: bool) -> str:
    numer = _liczba_w_zakresie(_atrybuty(wezel).get("order"), 1, math.inf)
    pozycje = []
    for pozycja in _dzieci(wezel):
        znacznik = f"{numer}. " if uporzadkowana else _ZNACZNIK_PUNKTU
        numer += 1  # numeracja liczy tez pozycje pomijane jako puste
        tresc = _bloki(pozycja, "\n")
        if not tresc:
            continue
        pozycje.append(_z_znacznikiem(tresc, znacznik))
    return "\n".join(pozycje)


def _z_znacznikiem(tresc: str, znacznik: str) -> str:
    """Pierwsza linia dostaje znacznik, kolejne wciecie; puste linie zostaja puste."""
    pierwsza, *reszta = tresc.split("\n")
    wciecie = " " * len(znacznik)
    linie = [znacznik + pierwsza]
    linie.extend(wciecie + linia if linia else linia for linia in reszta)
    return "\n".join(linie)
