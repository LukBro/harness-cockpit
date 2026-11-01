"""Walidator `.ai/lessons.md` (HAR-87): lekcja bez zakresu nie przechodzi walidacji.

Zakres mowi, kiedy lekcja obowiazuje; bez niego nie wiadomo, ktorego kroku dotyczy.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cockpit.services.errors import BlednyPlikLekcji

BRAK_ZAKRESU = "brak_zakresu"
BRAK_KROKU = "brak_kroku"
BRAK_SCIEZEK = "brak_sciezek"
BRAK_DATY_OD = "brak_daty_od"
BLEDNA_DATA_OD = "bledna_data_od"
BLEDNY_NAGLOWEK = "bledny_naglowek"

_NAGLOWEK = re.compile(r"^## L-(\d{3})\b")
# Cyfry spoza ASCII przechodza `\d`, ale odrzuca je `date.fromisoformat` w `_data_poprawna`.
_DATA = re.compile(r"\d{4}-\d{2}-\d{2}")
_SEPARATOR = "·"


@dataclass(frozen=True)
class ProblemLekcji:
    wpis: str
    wiersz: int
    kod: str
    komunikat: str


def _pola(zakres: str) -> dict[str, str]:
    """Pola `klucz=wartosc` i `od: wartosc`; przy powtorzeniu liczy sie pierwsze."""
    pola: dict[str, str] = {}
    for kawalek in zakres.split(_SEPARATOR):
        kawalek = kawalek.strip()
        if kawalek.startswith("od:"):
            klucz, wartosc = "od", kawalek[len("od:"):]
        else:
            klucz, _, wartosc = kawalek.partition("=")
            klucz = unicodedata.normalize("NFC", klucz.strip())
        pola.setdefault(klucz, wartosc.strip())
    return pola


def _problemy_zakresu(wpis: str, wiersz: int, zakres: str) -> list[ProblemLekcji]:
    pola = _pola(zakres)
    problemy: list[ProblemLekcji] = []
    if not pola.get("krok"):
        problemy.append(ProblemLekcji(wpis, wiersz, BRAK_KROKU, "zakres nie podaje kroku (krok=…)"))
    if not pola.get("ścieżki"):
        problemy.append(
            ProblemLekcji(wpis, wiersz, BRAK_SCIEZEK, "zakres nie podaje ścieżek (ścieżki=…)")
        )
    data = pola.get("od")
    if not data:
        problemy.append(ProblemLekcji(wpis, wiersz, BRAK_DATY_OD, "zakres nie podaje daty (od: RRRR-MM-DD)"))
    elif not _data_poprawna(data):
        problemy.append(
            ProblemLekcji(wpis, wiersz, BLEDNA_DATA_OD, f"data „{data}” nie jest datą RRRR-MM-DD")
        )
    return problemy


def _data_poprawna(tekst: str) -> bool:
    if not _DATA.fullmatch(tekst):
        return False
    try:
        date.fromisoformat(tekst)
    except ValueError:
        return False
    return True


def _dlugosc_plotka(linia: str) -> tuple[str, int] | None:
    """Znak i dlugosc plotka, gdy linia zaczyna sie od co najmniej 3 ``` albo ~~~."""
    for znak in "`~":
        if linia.startswith(znak * 3):
            return znak, len(linia) - len(linia.lstrip(znak))
    return None


def _zamyka_blok(linia: str, otwarcie: tuple[str, int]) -> bool:
    # CommonMark: zamyka tylko ten sam znak, nie krotszy od otwierajacego, bez reszty w linii.
    znak, dlugosc = otwarcie
    gola = linia.rstrip()
    return len(gola) >= dlugosc and gola == znak * len(gola)


def waliduj_lekcje(tekst: str) -> list[ProblemLekcji]:
    # Wpis: [numer, wiersz naglowka, None albo (wiersz, tresc) zakresu, bledny naglowek];
    # None - naglowek sekcji, ktora nie jest lekcja (konczy poprzedni wpis).
    wpisy: list[list | None] = []
    otwarcie: tuple[str, int] | None = None
    for numer_wiersza, linia in enumerate(tekst.splitlines(), start=1):
        if otwarcie is not None:
            if _zamyka_blok(linia, otwarcie):
                otwarcie = None
            continue
        otwarcie = _dlugosc_plotka(linia)
        if otwarcie is not None:
            continue
        trafienie = _NAGLOWEK.match(linia)
        if trafienie:
            wpisy.append([f"L-{trafienie.group(1)}", numer_wiersza, None, False])
        elif linia.startswith("## L-"):
            wpisy.append([linia[3:].split()[0], numer_wiersza, None, True])
        elif linia.startswith("## "):
            wpisy.append(None)
        elif linia.startswith("zakres:") and wpisy and wpisy[-1] is not None and wpisy[-1][2] is None:
            wpisy[-1][2] = (numer_wiersza, linia[len("zakres:"):])

    problemy: list[ProblemLekcji] = []
    for pozycja in wpisy:
        if pozycja is None:
            continue
        wpis, wiersz_naglowka, zakres, bledny = pozycja
        if bledny:
            problemy.append(
                ProblemLekcji(
                    wpis,
                    wiersz_naglowka,
                    BLEDNY_NAGLOWEK,
                    "nagłówek wpisu nie ma numeru w formacie L-NNN (trzy cyfry)",
                )
            )
        elif zakres is None:
            problemy.append(
                ProblemLekcji(wpis, wiersz_naglowka, BRAK_ZAKRESU, "wpis nie ma linii „zakres: …”")
            )
        else:
            wiersz_zakresu, tresc = zakres
            problemy.extend(_problemy_zakresu(wpis, wiersz_zakresu, tresc))
    return problemy


def wczytaj_lekcje(plik: Path) -> list[ProblemLekcji]:
    try:
        surowe = plik.read_bytes()
    except OSError as exc:
        raise BlednyPlikLekcji(f"nie można odczytać pliku lekcji {plik}: {exc}") from exc
    try:
        tekst = surowe.decode()  # domyslnie UTF-8, scisle
    except UnicodeDecodeError as exc:
        raise BlednyPlikLekcji(f"plik lekcji {plik} nie jest w UTF-8") from exc
    return waliduj_lekcje(tekst)
