"""Kotwice `plik:linia` w notatce zmiany i ich sprawdzenie wzgledem diffu runu (HAR-176).

Notatka `notatka-zmiany.md` (skill `explain-change`) opisuje ryzyka. Kazda pozycja sekcji
`## Jakie ryzyko` zaczyna sie od kotwicy `- [plik:linia] scenariusz: ...`, a kotwica musi
lezec w zmienionych liniach diffu (`zmienione-linie.json` kroku `diff`) z tolerancja
+-`TOLERANCJA` linii. Sekcja `## Jak to sprawdzic` jest obowiazkowa. Brak dowodu to
`evidence_required`: funkcje zwracaja `Naruszenie`, nigdy nie rzucaja na zlej notatce.

Czyste funkcje, bez I/O poza `wczytaj_zmienione_linie`, ktora tlumaczy bledy dysku i JSON-a
na wartosc `None` (brak czytelnego pliku).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from cockpit.services.walidacja import Naruszenie, Niezmiennik

PLIK_NOTATKI = "notatka-zmiany.md"
# Kodowanie plikow dowodu: stala modulu, wiec nie zalezy od srodowiska ani od zapisu nazwy kodeka.
KODOWANIE = "utf-8"
TOLERANCJA = 3
NAGLOWEK_RYZYKA = "## Jakie ryzyko"
NAGLOWEK_SPRAWDZENIA = "## Jak to sprawdzić"
# kotwica na samym poczatku pozycji (po "- "): [plik:linia], linia liczbą
KOTWICA = re.compile(r"\[(?P<plik>[^\[\]:]+):(?P<linia>[0-9]+)\]")


def _naruszenie(szczegol: str) -> Naruszenie:
    return Naruszenie(Niezmiennik.evidence_required, szczegol)


def _sekcja(linie: list[str], naglowek: str) -> list[str] | None:
    """Linie pierwszej sekcji o danym naglowku (do nastepnego `## `) albo None, gdy jej nie ma."""
    poczatek = next((i for i, wiersz in enumerate(linie) if wiersz.rstrip() == naglowek), None)
    if poczatek is None:
        return None
    wynik: list[str] = []
    for linia in linie[poczatek + 1:]:
        if linia.startswith("## "):
            break
        wynik.append(linia)
    return wynik


def _pozycje_ryzyka(sekcja: list[str]) -> list[str]:
    """Pozycje listy sekcji (tekst bez `- `), w kolejnosci wystapienia."""
    return [wiersz[2:].strip() for wiersz in sekcja if wiersz.startswith("- ")]


def _kotwica(pozycja: str) -> tuple[str, int] | None:
    """(plik, linia) z poczatku pozycji ryzyka albo None, gdy pozycja nie zaczyna sie kotwica."""
    trafienie = KOTWICA.match(pozycja)
    if trafienie is None:
        return None
    return trafienie["plik"], int(trafienie["linia"])


def wyciagnij_kotwice(notatka: str) -> list[str]:
    """Kotwice `plik:linia` z pozycji sekcji `## Jakie ryzyko`, w kolejnosci wystapienia."""
    sekcja = _sekcja(notatka.splitlines(), NAGLOWEK_RYZYKA)
    if sekcja is None:
        return []
    kotwice = (_kotwica(pozycja) for pozycja in _pozycje_ryzyka(sekcja))
    return [f"{plik}:{linia}" for plik, linia in filter(None, kotwice)]


def _zakresy_pliku(zmienione_linie: list[dict], plik: str) -> list[tuple[int, int]]:
    return [
        (od, do)
        for wpis in zmienione_linie
        if wpis["plik"] == plik
        for od, do in wpis["linie"]
    ]


def _naruszenia_ryzyka(sekcja: list[str] | None, zmienione_linie: list[dict]) -> list[Naruszenie]:
    if sekcja is None:
        return [_naruszenie(f"brak sekcji {NAGLOWEK_RYZYKA}")]
    pozycje = _pozycje_ryzyka(sekcja)
    if not pozycje:
        return [_naruszenie(f"brak ryzyka w sekcji {NAGLOWEK_RYZYKA}")]
    wynik: list[Naruszenie] = []
    for pozycja in pozycje:
        kotwica = _kotwica(pozycja)
        if kotwica is None:
            wynik.append(_naruszenie(f"ryzyko bez kotwicy plik:linia: {pozycja}"))
            continue
        plik, linia = kotwica
        zakresy = _zakresy_pliku(zmienione_linie, plik)
        if not zakresy:
            wynik.append(_naruszenie(f"kotwica {plik}:{linia}: plik bez zmienionych linii w diffie"))
        elif not any(od - TOLERANCJA <= linia <= do + TOLERANCJA for od, do in zakresy):
            wynik.append(_naruszenie(
                f"kotwica {plik}:{linia}: linia poza zmienionymi liniami (tolerancja {TOLERANCJA})"))
    return wynik


def _naruszenia_sprawdzenia(sekcja: list[str] | None) -> list[Naruszenie]:
    if sekcja is None:
        return [_naruszenie(f"brak sekcji {NAGLOWEK_SPRAWDZENIA}")]
    if not any(wiersz.strip() for wiersz in sekcja):
        return [_naruszenie(f"pusta sekcja {NAGLOWEK_SPRAWDZENIA}")]
    return []


def sprawdz_kotwice(notatka: str, zmienione_linie: list[dict]) -> list[Naruszenie]:
    """Naruszenia notatki: najpierw ryzyka w kolejnosci pozycji, na koncu `Jak to sprawdzic`."""
    linie = notatka.splitlines()
    return (
        _naruszenia_ryzyka(_sekcja(linie, NAGLOWEK_RYZYKA), zmienione_linie)
        + _naruszenia_sprawdzenia(_sekcja(linie, NAGLOWEK_SPRAWDZENIA))
    )


def _liczba(wartosc: object) -> bool:
    return isinstance(wartosc, int) and not isinstance(wartosc, bool)


def _poprawny_wpis(wpis: object) -> bool:
    if not isinstance(wpis, dict) or not isinstance(wpis.get("plik"), str):
        return False
    linie = wpis.get("linie")
    return isinstance(linie, list) and all(
        isinstance(zakres, list) and len(zakres) == 2 and all(_liczba(n) for n in zakres)
        for zakres in linie
    )


def wczytaj_zmienione_linie(plik: Path | None) -> list[dict] | None:
    """Zawartosc `zmienione-linie.json` albo None, gdy pliku nie ma, nie da sie go odczytac
    albo nie ma ksztaltu: lista slownikow `plik: str`, `linie: [[int, int], ...]`."""
    if plik is None:
        return None
    try:
        tresc = json.loads(plik.read_text(encoding=KODOWANIE))
    except (OSError, RecursionError, ValueError):   # UnicodeDecodeError jest podklasa ValueError
        return None
    if not isinstance(tresc, list) or not all(_poprawny_wpis(w) for w in tresc):
        return None
    return tresc
