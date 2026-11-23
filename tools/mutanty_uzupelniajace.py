"""Mutanty uzupelniajace zmienionych funkcji i stalych (HAR-241).

Bramka dla kształtow, ktorych mutmut nie generuje: element krotki `except`, instrukcja
(`finally`, samodzielne wywolanie), kolejnosc dwoch sasiednich instrukcji, element literalu
zbioru, krotki i mapy, usuniecie `sorted(...)` / `.strip()` oraz element wyrazenia regularnego
(`re.compile` oraz `re.sub`, `re.subn`, `re.split`, `re.match`, `re.search`, `re.fullmatch`,
`re.findall`, `re.finditer`). Mapuje diff wzgledem bazy (jak `tools/mutanty_zmian.py`) na funkcje, metody
i stale modulu (funkcja zdefiniowana w instrukcji zlozonej modulu lub klasy - `if TYPE_CHECKING:`,
`try`, `with`, `for`, `while`, `match` - jest funkcja jak kazda inna, a sama instrukcja zlozona
nadal jest zakresem stalej), dla kazdego mutanta uruchamia testy modulu w nowej kopii repozytorium i wypisuje
przezywajace mutanty.

Kody wyjscia: 0 - wszystkie mutanty zabite albo rownowazne (stdout: `zbadano N`, N > 0, albo
`brak zmienionych funkcji`, gdy diff nie dotyka funkcji ani stalej), 1 - przezyl mutant
(stdout: wylacznie linie `plik:linia: ksztalt: opis`), 2 - blad narzedzia (zly plik, tabela,
zero zebranych testow, test czerwony bez mutacji, nieznana baza; komunikat na stderr),
3 - brak dowodu, trzeba sprawdzic recznie (stdout: linie `plik:linia: sprawdz recznie`):
diff dotyka funkcji albo stalej, a zaden ksztalt nie daje mutanta (linia startu kazdej dotknietej
funkcji lub stalej), albo `re.compile` (lub inna funkcja `re` z wzorcem) ze wzorcem nieliteralnym
(zmienna, f-string, wynik wywolania) w dotknietym zakresie. Priorytet: 2 > 1 > 3 > 0. `[[rownowazny]]` nie wycisza kodu 3; wyjscie
to wiersz w tabeli recznych mutantow.
`[[rownowazny]]` z `tests/mutanty_reczne/*.toml` wycisza mutanta TYLKO gdy: `plik` sie zgadza,
`linia_tekst` rowna sie (po `strip()`) tekstowi linii mutanta w oryginalnym pliku, `uzasadnienie`
zawiera doslownie `kształt: <nazwa>` (nazwa z KSZTALTY, np. `kształt: regex`), a dla `except`
takze caly opis mutanta (np. `usunieto element ValueError z krotki except`) - wpis dotyczy
jednego elementu krotki. Wpis bez tego (sama linia, wpis z tabeli innego ticketu) nie wycisza.
Samodzielne wywolanie to `f(...)` oraz `await f(...)`. Przebieg kontrolny (testy bez mutacji)
idzie na `ast.unparse(ast.parse(zrodlo))`, czyli tym samym tekstem co mutanty. `SyntaxError`,
`ValueError`, `RecursionError` i `MemoryError` z `ast.parse` oraz `RecursionError` i `MemoryError`
z `ast.unparse` to blad narzedzia (kod 2). Tak samo wzorzec `re.compile`, ktory przyjmuje `re`,
a narzedzie nie umie go rozebrac (np. nawias w komentarzu `(?x)`, flaga `re.VERBOSE` albo `re.TEMPLATE`
(`re.T`, tez jako liczba 1) albo flagi nieodczytywalne z wywolania, grupa z `|` nierozpoznanego rodzaju,
np. warunkowa `(?(1)a|b)`):
`plik:linia: nie umiem rozebrac wzorca`, nigdy `zbadano 0`. Wzorce bajtowe (`rb"..."`) sa
rozbierane jak tekstowe; oryginal i mutanty sa kompilowane z flagami z wywolania (drugi argument
albo `flags=`). Wzorzec odrzucony przez `re.compile` (`re.error`, `OverflowError`) nie ma mutantow
regex.
Powtorzony `pattern=` i gwiazdka w argumentach pozycyjnych `re.compile` to blad narzedzia (kod 2).
To samo dotyczy `re.sub`, `re.subn`, `re.split`, `re.match`, `re.search`, `re.fullmatch`, `re.findall` i
`re.finditer` (stala `RE_FUNKCJE_Z_WZORCEM`): wzorzec jest pierwszym argumentem albo `pattern=`, flagi stoja na
miejscu danej funkcji albo w `flags=`, a argument nazwany spoza jej listy to blad narzedzia. Inne wywolania
`re.*` (`re.escape`, ...) nie maja wzorca i nie sa objete bramka.
Flagi globalne z poczatku wzorca moga byc rozdzielone komentarzem `(?#...)` (a po `(?x)` takze bialymi
znakami i komentarzem `#...`); usuniecie pierwszej alternatywy nie rusza ani flag, ani tego, co je rozdziela.
Petle rozbioru wzorca pilnuje `_wymagaj_postepu`: indeks, ktory stoi w miejscu, to blad narzedzia (kod 2),
nie zawieszenie.
Ksztalty spoza KSZTALTY nie sa objete bramka.
Mutant, ktory przy uruchomieniu testow daje kod pytesta inny niz 0/1 (np. blad importu po
mutacji), to blad narzedzia - brak dowodu, nie "zabity".
Skrypt niczego nie zmienia w repozytorium: mutanty zyja w kopiach w katalogach tymczasowych.
"""
from __future__ import annotations

import argparse
import ast
import math
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import mutanty_reczne
import mutanty_zmian

KSZTALTY = ("except", "instrukcja", "kolejnosc", "kolekcja", "wywolanie", "regex")
WYWOLANIA_USUWALNE = ("sorted", "strip")
LIMIT_DOMYSLNY = 300.0
RAPORT_JUNIT = ".mutanty-uzupelniajace-junit.xml"
KATALOG_TABEL = mutanty_reczne.KATALOG_TABEL
DLUGOSC_OPISU = 60


class BladNarzedzia(Exception):
    """Blad dziedziny skryptu (kod wyjscia 2)."""


@dataclass(frozen=True)
class Mutant:
    plik: str
    linia: int
    ksztalt: str
    opis: str
    zrodlo: str


@dataclass(frozen=True)
class _Zamiar:
    """Jedna mutacja drzewa: `zastosuj` mutuje w miejscu i zwraca funkcje cofajaca."""

    linia: int
    ksztalt: str
    opis: str
    zastosuj: Callable[[], Callable[[], None]]


def _tekst(wezel: ast.AST) -> str:
    tekst = " ".join(ast.unparse(wezel).split())
    return tekst if len(tekst) <= DLUGOSC_OPISU else tekst[: DLUGOSC_OPISU - 3] + "..."


# --- wyrazenia regularne ---------------------------------------------------------------------

PREFIKS_GRUPY = re.compile(r"\((?:\?(?:P<\w+>|<[=!]|[=!:>]|[a-zA-Z]+(?:-[a-zA-Z]+)?:|-[a-zA-Z]+:)|(?!\?))")
FLAGI_GLOBALNE = re.compile(r"(?:\(\?[aiLmsux]+\))*")
# Miedzy flagami globalnymi `re` przyjmuje komentarz `(?#...)` (konczy sie na pierwszym `)`), a po fladze `x`
# takze biale znaki (zbior sre `" \t\n\r\v\f"`, nie `\s`) i komentarz `#...` do konca linii. Bez flagi `x`
# wzorzec z bialym znakiem albo `#` przed flaga nie kompiluje sie, wiec taki wzorzec nie dociera do rozbioru.
POMIJANE_MIEDZY_FLAGAMI = re.compile(r"(?:\(\?#[^)]*\)|[ \t\n\r\v\f]|#[^\n]*)*")
# funkcje `re` z wzorcem jako pierwszym argumentem (poza `compile`, ktory ma wlasna sciezke):
# nazwa -> (indeks flag wsrod argumentow pozycyjnych, dozwolone argumenty nazwane)
RE_FUNKCJE_Z_WZORCEM = {
    "sub": (4, ("pattern", "repl", "string", "count", "flags")),
    "subn": (4, ("pattern", "repl", "string", "count", "flags")),
    "split": (3, ("pattern", "string", "maxsplit", "flags")),
    "match": (2, ("pattern", "string", "flags")),
    "search": (2, ("pattern", "string", "flags")),
    "fullmatch": (2, ("pattern", "string", "flags")),
    "findall": (2, ("pattern", "string", "flags")),
    "finditer": (2, ("pattern", "string", "flags")),
}
FLAGA_TEMPLATE = 1  # re.TEMPLATE / re.T; od Pythona 3.13 nie ma jej w `re`, wiec wartosc zamiast nazwy
NAZWY_FLAGI_TEMPLATE = ("T", "TEMPLATE")


class _NiepoprawnyWzorzec(Exception):
    """Wzorzec poprawny dla `re.compile`, ktorego narzedzie nie umie rozebrac."""

    linia: int | None = None


def _wymagaj_postepu(przed: int, po: int) -> None:
    """Petla rozbioru wzorca idzie tylko do przodu; stanie w miejscu to blad, a nie zawieszenie narzedzia."""
    if po <= przed:
        raise _NiepoprawnyWzorzec(f"rozbior wzorca nie posuwa sie naprzod (znak {po})")


def _koniec_flag_globalnych(wzorzec: str) -> int:
    """Indeks za ostatnia flaga globalna z poczatku wzorca (0, gdy jej nie ma).

    Flagi moga byc rozdzielone komentarzem `(?#...)`, a po fladze `x` takze bialymi znakami i komentarzem
    `#...`. Wszystko, co rozdziela flagi albo stoi przed pierwsza z nich, zostaje przed tym indeksem, wiec
    usuniecie pierwszej alternatywy nie rusza flag ani tego, co je rozdziela. To, co stoi za ostatnia flaga
    (komentarz, bialy znak), jest juz poczatkiem pierwszej alternatywy.
    """
    pozycja = 0
    koniec = 0
    while True:
        poczatek = pozycja
        flagi = FLAGI_GLOBALNE.match(wzorzec, pozycja)
        if flagi.end() > pozycja:
            pozycja = koniec = flagi.end()
        pomijane = POMIJANE_MIEDZY_FLAGAMI.match(wzorzec, pozycja)
        if pomijane.end() == pozycja:
            return koniec
        pozycja = pomijane.end()
        _wymagaj_postepu(poczatek, pozycja)


def _koniec_ucieczki(wzorzec: str, poczatek: int) -> int:
    """Indeks za sekwencja ucieczki zaczynajaca sie od `\\` na `poczatek` (wzorzec nie konczy sie `\\`)."""
    znak = wzorzec[poczatek + 1]
    if znak == "x":
        return poczatek + 4
    if znak == "u":
        return poczatek + 6
    if znak == "U":
        return poczatek + 10
    if znak == "N" and wzorzec[poczatek + 2 : poczatek + 3] == "{":
        zamkniecie = wzorzec.find("}", poczatek)
        if zamkniecie < 0:
            raise _NiepoprawnyWzorzec("brak } w \\N{...}")
        return zamkniecie + 1
    if znak in "01234567":
        koniec = poczatek + 2
        while koniec < poczatek + 4 and wzorzec[koniec : koniec + 1] in tuple("01234567"):
            przed = koniec
            koniec += 1
            _wymagaj_postepu(przed, koniec)
        return koniec
    return poczatek + 2


def _koniec_elementu(wzorzec: str, poczatek: int) -> int:
    if wzorzec[poczatek] == "\\":
        return _koniec_ucieczki(wzorzec, poczatek)
    return poczatek + 1


def _klasa_znakow(wzorzec: str, poczatek: int, elementy: list[tuple[int, int]]) -> int:
    """Dopisuje przedzialy elementow klasy zaczynajacej sie od `[`; zwraca indeks za `]`."""
    i = poczatek + 1
    if wzorzec[i : i + 1] == "^":
        i += 1
    pierwszy = True
    while True:
        if i >= len(wzorzec):
            raise _NiepoprawnyWzorzec("niezamknieta klasa znakow")
        if wzorzec[i] == "]" and not pierwszy:
            return i + 1
        start = i
        i = _koniec_elementu(wzorzec, i)
        _wymagaj_postepu(start, i)
        if wzorzec[i : i + 1] == "-" and i + 1 < len(wzorzec) and wzorzec[i + 1] != "]":
            i = _koniec_elementu(wzorzec, i + 1)
        elementy.append((start, i))
        pierwszy = False


def _dopisz_alternatywy(alternatywy: list[tuple[int, int]], poczatek: int, kreski: list[int], koniec: int) -> None:
    """Przedzialy do usuniecia (kazdy z jednym `|`) dla grupy z zawartoscia `[poczatek, koniec)`."""
    granice = [poczatek - 1, *kreski, koniec]
    for k in range(len(kreski) + 1):
        if k < len(kreski):
            alternatywy.append((granice[k] + 1, granice[k + 1] + 1))
        else:
            alternatywy.append((granice[k], koniec))


def _skompiluj(wzorzec: str, bajtowy: bool, flagi: int) -> None:
    """`re.compile` z tymi samymi flagami co w kodzie (bez DEBUG, ktory pisze na stdout)."""
    re.compile(wzorzec.encode("latin-1") if bajtowy else wzorzec, flagi & ~re.DEBUG)


def _warianty_wzorca(wzorzec: str | bytes, flagi: int = 0) -> list[tuple[str, str | bytes]]:
    """(opis, nowy wzorzec) dla kazdej kotwicy, elementu klasy znakow i alternatywy (takze poza grupa).

    Wzorzec bajtowy jest rozbierany jak tekstowy (znaki latin-1 1:1), a mutanty wracaja jako bajty.
    Oryginal i mutanty sa kompilowane z `flagi` z wywolania `re.<funkcja>`: `re.compile` albo
    dowolnej funkcji z `RE_FUNKCJE_Z_WZORCEM` (`sub`, `subn`, `split`, `match`, `search`, `fullmatch`,
    `findall`, `finditer`), gdzie flagi stoja na miejscu danej funkcji albo w `flags=`.
    Wzorzec, ktorego `re.compile` nie przyjmuje (`re.error`, `OverflowError`), nie ma mutantow (`[]`).
    Wzorzec poprawny dla `re.compile`, ktorego nie da sie rozebrac (np. nawias w komentarzu `(?x)`,
    grupa z `|` nierozpoznanego rodzaju, np. warunkowa), rzuca `_NiepoprawnyWzorzec` - to blad
    narzedzia, nie "zero mutantow". Tak samo flagi niezgodne z wzorcem (`ValueError`).
    """
    bajtowy = isinstance(wzorzec, bytes)
    if bajtowy:
        wzorzec = wzorzec.decode("latin-1")
    try:
        _skompiluj(wzorzec, bajtowy, flagi)
    except (re.error, OverflowError):
        return []
    except ValueError as blad:
        raise _NiepoprawnyWzorzec(f"flagi niezgodne z wzorcem: {blad}") from blad
    kotwice: list[int] = []
    elementy: list[tuple[int, int]] = []
    # grupa: [indeks pierwszego znaku zawartosci albo None dla nieobslugiwanej, indeksy `|`];
    # dolna ramka to niejawna grupa calego wzorca (alternatywy poza nawiasami); zaczyna sie za
    # globalnymi flagami z poczatku wzorca, wiec usuniecie pierwszej alternatywy ich nie rusza
    stos: list[list] = [[_koniec_flag_globalnych(wzorzec), []]]
    alternatywy: list[tuple[int, int]] = []
    # naglowek flag globalnych (z komentarzami miedzy flagami) jest pominiety takze w tym przejsciu:
    # `|`, `^`, `(` z komentarza `#...` po `(?x)` nie sa alternatywa, kotwica ani grupa
    i = _koniec_flag_globalnych(wzorzec)
    poprzedni = i - 1
    while i < len(wzorzec):
        _wymagaj_postepu(poprzedni, i)
        poprzedni = i
        znak = wzorzec[i]
        if znak == "\\":
            i = _koniec_ucieczki(wzorzec, i)
        elif znak == "[":
            i = _klasa_znakow(wzorzec, i, elementy)
        elif znak == "(":
            if wzorzec.startswith("(?#", i):
                zamkniecie = wzorzec.find(")", i)
                if zamkniecie < 0:
                    raise _NiepoprawnyWzorzec("niezamkniety komentarz (?#")
                i = zamkniecie + 1
                continue
            dopasowanie = PREFIKS_GRUPY.match(wzorzec, i)
            stos.append([dopasowanie.end() if dopasowanie else None, []])
            i += 1
        elif znak == ")":
            if len(stos) < 2:
                raise _NiepoprawnyWzorzec("nawias zamykajacy bez otwierajacego")
            poczatek, kreski = stos.pop()
            if kreski:
                _dopisz_alternatywy(alternatywy, poczatek, kreski, i)
            i += 1
        elif znak == "|":
            if stos[-1][0] is None:
                raise _NiepoprawnyWzorzec(f"alternatywy w grupie nierozpoznanego rodzaju (znak {i})")
            stos[-1][1].append(i)
            i += 1
        else:
            if znak in "^$":
                kotwice.append(i)
            i += 1
    if len(stos) != 1:
        raise _NiepoprawnyWzorzec("niezamknieta grupa")
    if stos[0][1]:
        _dopisz_alternatywy(alternatywy, stos[0][0], stos[0][1], len(wzorzec))

    warianty: list[tuple[str, str]] = []
    for indeks in kotwice:
        warianty.append((f"usunieto kotwice {wzorzec[indeks]!r} z wzorca", wzorzec[:indeks] + wzorzec[indeks + 1 :]))
    for start, koniec in elementy:
        warianty.append(
            (f"usunieto element {wzorzec[start:koniec]!r} z klasy znakow", wzorzec[:start] + wzorzec[koniec:])
        )
    for start, koniec in alternatywy:
        warianty.append(
            (f"usunieto alternatywe {wzorzec[start:koniec].strip('|')!r} z grupy", wzorzec[:start] + wzorzec[koniec:])
        )
    wynik: list[tuple[str, str]] = []
    widziane: set[str] = set()
    for opis, nowy in warianty:
        if nowy in widziane:
            continue
        try:
            _skompiluj(nowy, bajtowy, flagi)
        except (re.error, OverflowError):
            continue  # np. pusta klasa `[]`: mutant nie istnieje, nie liczy sie do `zbadano`
        widziane.add(nowy)
        wynik.append((opis, nowy.encode("latin-1") if bajtowy else nowy))
    return wynik


# --- zamiary mutacji na drzewie ---------------------------------------------------------------


def _usuniecia_elementow(wezel: ast.Set | ast.Tuple | ast.Dict, ksztalt: str, rodzaj: str) -> list[_Zamiar]:
    """Po jednym zamiarze na kazdy element literalu (tylko gdy ma co najmniej dwa elementy)."""
    zamiary: list[_Zamiar] = []
    if isinstance(wezel, ast.Dict):
        liczba = len(wezel.keys)
    else:
        liczba = len(wezel.elts)
    if liczba < 2:
        return zamiary

    def fabryka(indeks: int) -> Callable[[], Callable[[], None]]:
        def zastosuj() -> Callable[[], None]:
            if isinstance(wezel, ast.Dict):
                klucz, wartosc = wezel.keys.pop(indeks), wezel.values.pop(indeks)

                def cofnij() -> None:
                    wezel.keys.insert(indeks, klucz)
                    wezel.values.insert(indeks, wartosc)

                return cofnij
            element = wezel.elts.pop(indeks)

            def cofnij_element() -> None:
                wezel.elts.insert(indeks, element)

            return cofnij_element

        return zastosuj

    for indeks in range(liczba):
        if isinstance(wezel, ast.Dict):
            klucz, wartosc = wezel.keys[indeks], wezel.values[indeks]
            linia = (klucz or wartosc).lineno
            opis_elementu = f"{_tekst(klucz)}: {_tekst(wartosc)}" if klucz is not None else f"**{_tekst(wartosc)}"
        else:
            linia = wezel.elts[indeks].lineno
            opis_elementu = _tekst(wezel.elts[indeks])
        zamiary.append(_Zamiar(linia, ksztalt, f"usunieto element {opis_elementu} z {rodzaj}", fabryka(indeks)))
    return zamiary


def _podmien(rodzic: ast.AST, pole: str, indeks: int | None, nowy: ast.AST) -> Callable[[], None]:
    if indeks is None:
        stary = getattr(rodzic, pole)
        setattr(rodzic, pole, nowy)
        return lambda: setattr(rodzic, pole, stary)
    lista = getattr(rodzic, pole)
    stary = lista[indeks]
    lista[indeks] = nowy

    def cofnij() -> None:
        lista[indeks] = stary

    return cofnij


def _samodzielne_wywolanie(instrukcja: ast.stmt) -> bool:
    """`f(...)` albo `await f(...)` jako cala instrukcja; `await x`, `y = f()` i `return f()` nie."""
    if not isinstance(instrukcja, ast.Expr):
        return False
    wartosc = instrukcja.value
    if isinstance(wartosc, ast.Await):
        wartosc = wartosc.value
    return isinstance(wartosc, ast.Call)


def _zamiary_instrukcji(wezel: ast.AST) -> list[_Zamiar]:
    zamiary: list[_Zamiar] = []
    for pole in ("body", "orelse", "finalbody"):
        lista = getattr(wezel, pole, None)
        if not isinstance(lista, list) or not lista or not isinstance(lista[0], ast.stmt):
            continue
        for indeks, instrukcja in enumerate(lista):
            if not _samodzielne_wywolanie(instrukcja):
                continue

            def usun(lista=lista, indeks=indeks, instrukcja=instrukcja) -> Callable[[], None]:
                return _podmien_w_liscie(lista, indeks, ast.copy_location(ast.Pass(), instrukcja))

            zamiary.append(
                _Zamiar(instrukcja.lineno, "instrukcja", f"zastapiono wywolanie {_tekst(instrukcja)} przez pass", usun)
            )
            nastepna = lista[indeks + 1] if indeks + 1 < len(lista) else None
            if nastepna is not None and _samodzielne_wywolanie(nastepna):

                def zamien(lista=lista, indeks=indeks) -> Callable[[], None]:
                    def zamien_pare() -> None:
                        lista[indeks], lista[indeks + 1] = lista[indeks + 1], lista[indeks]

                    zamien_pare()
                    return zamien_pare

                zamiary.append(
                    _Zamiar(
                        instrukcja.lineno,
                        "kolejnosc",
                        f"zamieniono kolejnosc wywolan {_tekst(instrukcja)} i {_tekst(nastepna)}",
                        zamien,
                    )
                )
    return zamiary


def _podmien_w_liscie(lista: list, indeks: int, nowy: ast.AST) -> Callable[[], None]:
    stary = lista[indeks]
    lista[indeks] = nowy

    def cofnij() -> None:
        lista[indeks] = stary

    return cofnij


def _usuwalne_wywolanie(wezel: ast.AST) -> ast.expr | None:
    """Wyrazenie, ktore zastepuje wywolanie `sorted(x)` albo `x.strip()`; None, gdy to nie ten ksztalt."""
    if not isinstance(wezel, ast.Call):
        return None
    funkcja = wezel.func
    if (
        "sorted" in WYWOLANIA_USUWALNE
        and isinstance(funkcja, ast.Name)
        and funkcja.id == "sorted"
        and len(wezel.args) == 1
        and not isinstance(wezel.args[0], ast.Starred)
    ):
        return wezel.args[0]
    if (
        "strip" in WYWOLANIA_USUWALNE
        and isinstance(funkcja, ast.Attribute)
        and funkcja.attr == "strip"
        and not wezel.args
        and not wezel.keywords
    ):
        return funkcja.value
    return None


def _nazwa_z_modulu_re(wezel: ast.expr) -> str | None:
    """`NAZWA` z wyrazenia `re.NAZWA`; None dla kazdego innego wyrazenia (takze `x.NAZWA` i `a.re.NAZWA`)."""
    if isinstance(wezel, ast.Attribute) and isinstance(wezel.value, ast.Name) and wezel.value.id == "re":
        return wezel.attr
    return None


def _wartosc_flag(wyrazenie: ast.expr) -> int:
    """Wartosc flagi z kodu: liczba, `re.NAZWA` albo ich suma bitowa `|`; reszta to `_NiepoprawnyWzorzec`."""
    if isinstance(wyrazenie, ast.Constant) and isinstance(wyrazenie.value, int) and not isinstance(
        wyrazenie.value, bool
    ):
        return wyrazenie.value
    if _nazwa_z_modulu_re(wyrazenie) in NAZWY_FLAGI_TEMPLATE:
        return FLAGA_TEMPLATE  # po nazwie, bo w Pythonie 3.13+ `re.T` nie istnieje, a komunikat ma byc ten sam
    if (
        isinstance(wyrazenie, ast.Attribute)
        and isinstance(wyrazenie.value, ast.Name)
        and wyrazenie.value.id == "re"
        and wyrazenie.attr in re.RegexFlag.__members__
    ):
        return int(re.RegexFlag.__members__[wyrazenie.attr])
    if isinstance(wyrazenie, ast.BinOp) and isinstance(wyrazenie.op, ast.BitOr):
        return _wartosc_flag(wyrazenie.left) | _wartosc_flag(wyrazenie.right)
    raise _NiepoprawnyWzorzec(f"nie umiem odczytac flag wywolania re: {_tekst(wyrazenie)}")


def _flagi_wywolania(wezel: ast.Call) -> int:
    """Flagi z wywolania `re.<funkcja>` (dla `compile`: `re.compile(wzorzec, flagi)` albo `flags=`).

    `re.VERBOSE` i `re.TEMPLATE` to blad narzedzia. Wywolanie `re.<funkcja>` z innym miejscem flag
    dostaje tu zastepcze wywolanie, w ktorym flagi stoja zaraz za wzorcem.
    """
    przekazane: list[ast.expr] = list(wezel.args[1:])
    for slowo in wezel.keywords:
        if slowo.arg == "pattern":
            continue  # wzorzec podany nazwanie czyta `_wzorzec_re_compile`
        if slowo.arg != "flags":
            raise _NiepoprawnyWzorzec("nie umiem odczytac argumentow re.compile (nieznany argument nazwany)")
        przekazane.append(slowo.value)
    if any(isinstance(p, ast.Starred) for p in przekazane) or len(przekazane) > 1:
        raise _NiepoprawnyWzorzec(f"nie umiem odczytac argumentow re.{wezel.func.attr} (gwiazdka albo wiele flag)")
    flagi = _wartosc_flag(przekazane[0]) if przekazane else 0
    if flagi & re.VERBOSE:
        raise _NiepoprawnyWzorzec("flaga re.VERBOSE zmienia skladnie wzorca, narzedzie jej nie obsluguje")
    if flagi & FLAGA_TEMPLATE:
        raise _NiepoprawnyWzorzec("flaga re.TEMPLATE zmienia dopasowanie wzorca, narzedzie jej nie obsluguje")
    return flagi


def _wzorzec_re_compile(wezel: ast.AST) -> tuple[ast.Constant, int] | ast.Call | None:
    """(literal wzorca tekstowego albo bajtowego, flagi z wywolania) dla `re.compile(literal, ...)`.

    Wzorzec podany jako `pattern=` jest czytany jak pierwszy argument pozycyjny; `pattern=` razem
    z argumentem pozycyjnym, powtorzony `pattern=`, gwiazdka w argumentach pozycyjnych i kazdy
    nieznany argument nazwany (takze `**kw`) to `_NiepoprawnyWzorzec`. Wzorzec nieliteralny
    (zmienna, f-string, wynik wywolania) zwraca samo wywolanie: narzedzie go nie zbada, wiec
    wolajacy zglasza `sprawdz recznie`. `re.compile()` bez wzorca to `None`.
    """
    if not isinstance(wezel, ast.Call):
        return None
    funkcja = wezel.func
    if not (
        isinstance(funkcja, ast.Attribute)
        and funkcja.attr == "compile"
        and isinstance(funkcja.value, ast.Name)
        and funkcja.value.id == "re"
    ):
        return None
    linia = wezel.lineno
    try:
        nazwane = [s for s in wezel.keywords if s.arg == "pattern"]
        if len(nazwane) > 1:
            raise _NiepoprawnyWzorzec("nie umiem odczytac argumentow re.compile (powtorzony argument pattern=)")
        if any(isinstance(a, ast.Starred) for a in wezel.args):
            raise _NiepoprawnyWzorzec("nie umiem odczytac argumentow re.compile (gwiazdka w argumentach pozycyjnych)")
        if nazwane and wezel.args:
            raise _NiepoprawnyWzorzec("nie umiem odczytac argumentow re.compile (pattern= razem z argumentem pozycyjnym)")
        if nazwane:
            pierwszy = nazwane[0].value
        elif wezel.args:
            pierwszy = wezel.args[0]
        else:
            pierwszy = None
        if isinstance(pierwszy, ast.Constant) and isinstance(pierwszy.value, (str, bytes)):
            linia = pierwszy.lineno
            return pierwszy, _flagi_wywolania(wezel)
        if any(s.arg not in ("flags", "pattern") for s in wezel.keywords):
            raise _NiepoprawnyWzorzec("nie umiem odczytac argumentow re.compile (nieznany argument nazwany)")
    except _NiepoprawnyWzorzec as blad:
        blad.linia = linia
        raise
    return wezel if pierwszy is not None else None


def _wzorzec_re_funkcji(wywolanie: ast.AST) -> tuple[ast.Constant, int] | ast.Call | None:
    """Jak `_wzorzec_re_compile`, ale dla `re.sub`, `re.match` i reszty funkcji z `RE_FUNKCJE_Z_WZORCEM`.

    Wzorzec jest pierwszym argumentem albo `pattern=`; flagi to argument pozycyjny z `pozycja_flag`
    albo `flags=`. Gwiazdka w argumentach pozycyjnych, `**kw`, argument nazwany spoza listy funkcji,
    powtorzony `pattern=` i `pattern=` razem z argumentem pozycyjnym to `_NiepoprawnyWzorzec`; flagi
    czyta `_flagi_wywolania` (te same reguly co dla `re.compile`). Wzorzec nieliteralny zwraca samo
    wywolanie (`sprawdz recznie`), a wywolanie bez wzorca `None`.
    """
    if not isinstance(wywolanie, ast.Call):
        return None
    odbiorca = wywolanie.func
    if not (
        isinstance(odbiorca, ast.Attribute)
        and odbiorca.attr in RE_FUNKCJE_Z_WZORCEM
        and isinstance(odbiorca.value, ast.Name)
        and odbiorca.value.id == "re"
    ):
        return None
    pozycja_flag, dozwolone = RE_FUNKCJE_Z_WZORCEM[odbiorca.attr]
    nazwa = odbiorca.attr
    miejsce = wywolanie.lineno
    try:
        if any(isinstance(arg, ast.Starred) for arg in wywolanie.args):
            raise _NiepoprawnyWzorzec(f"nie umiem odczytac argumentow re.{nazwa} (gwiazdka w argumentach pozycyjnych)")
        if any(slowo.arg not in dozwolone for slowo in wywolanie.keywords):
            raise _NiepoprawnyWzorzec(f"nie umiem odczytac argumentow re.{nazwa} (nieznany argument nazwany)")
        jawne = [slowo for slowo in wywolanie.keywords if slowo.arg == "pattern"]
        if len(jawne) > 1:
            raise _NiepoprawnyWzorzec(f"nie umiem odczytac argumentow re.{nazwa} (powtorzony argument pattern=)")
        if jawne and wywolanie.args:
            raise _NiepoprawnyWzorzec(f"nie umiem odczytac argumentow re.{nazwa} (pattern= razem z argumentem pozycyjnym)")
        wzor = jawne[0].value if jawne else (wywolanie.args[0] if wywolanie.args else None)
        if isinstance(wzor, ast.Constant) and isinstance(wzor.value, (str, bytes)):
            miejsce = wzor.lineno
            flagi = [
                *wywolanie.args[pozycja_flag : pozycja_flag + 1],
                *(slowo.value for slowo in wywolanie.keywords if slowo.arg == "flags"),
            ]
            zastepcze = ast.Call(func=odbiorca, args=[wzor, *flagi], keywords=[])
            return wzor, _flagi_wywolania(zastepcze)
    except _NiepoprawnyWzorzec as przyczyna:
        przyczyna.linia = miejsce
        raise
    return wywolanie if wzor is not None else None


def _zamiary_regex(stala: ast.Constant, flagi: int) -> list[_Zamiar]:
    zamiary: list[_Zamiar] = []
    oryginal = stala.value

    def fabryka(nowy: str) -> Callable[[], Callable[[], None]]:
        def zastosuj() -> Callable[[], None]:
            stala.value = nowy

            def cofnij() -> None:
                stala.value = oryginal

            return cofnij

        return zastosuj

    try:
        warianty = _warianty_wzorca(oryginal, flagi)
    except _NiepoprawnyWzorzec as blad:
        blad.linia = stala.lineno
        raise
    for opis, nowy in warianty:
        zamiary.append(_Zamiar(stala.lineno, "regex", opis, fabryka(nowy)))
    return zamiary


def _zamiary_zakresu(korzen_zakresu: ast.AST, w_funkcji: bool) -> tuple[list[_Zamiar], list[int]]:
    """(zamiary mutacji, linie `re.compile` i innych funkcji `re` z wzorcem nieliteralnym, ktorego narzedzie nie zbada)."""
    wezly = list(ast.walk(korzen_zakresu))
    nieliteralne: list[int] = []
    krotki_except = {id(w.type) for w in wezly if isinstance(w, ast.ExceptHandler) and isinstance(w.type, ast.Tuple)}
    indeksowane = {id(w.slice) for w in wezly if isinstance(w, ast.Subscript)}
    zamiary: list[_Zamiar] = []
    for wezel in wezly:
        if isinstance(wezel, ast.ExceptHandler) and isinstance(wezel.type, ast.Tuple):
            zamiary += _usuniecia_elementow(wezel.type, "except", "krotki except")
        elif isinstance(wezel, (ast.Set, ast.Tuple, ast.Dict)) and id(wezel) not in krotki_except:
            # krotka w Store/Del albo w indeksie (`x[a, b]`, `dict[str, int]`) nie jest wartoscia z elementami
            pomijana = isinstance(wezel, ast.Tuple) and (
                not isinstance(wezel.ctx, ast.Load) or id(wezel) in indeksowane
            )
            if not pomijana:
                rodzaj = {ast.Set: "zbioru", ast.Tuple: "krotki", ast.Dict: "mapy"}[type(wezel)]
                zamiary += _usuniecia_elementow(wezel, "kolekcja", rodzaj)
        if w_funkcji:
            zamiary += _zamiary_instrukcji(wezel)
        wzorzec = _wzorzec_re_compile(wezel)
        if isinstance(wzorzec, ast.Call):
            nieliteralne.append(wzorzec.lineno)
        elif wzorzec is not None:
            zamiary += _zamiary_regex(*wzorzec)
        wzorzec_funkcji = _wzorzec_re_funkcji(wezel)
        if isinstance(wzorzec_funkcji, ast.Call):
            nieliteralne.append(wzorzec_funkcji.lineno)
        elif wzorzec_funkcji is not None:
            zamiary += _zamiary_regex(*wzorzec_funkcji)
        for pole, wartosc in ast.iter_fields(wezel):
            dzieci = (
                [(indeks, dziecko) for indeks, dziecko in enumerate(wartosc)]
                if isinstance(wartosc, list)
                else [(None, wartosc)]
            )
            for indeks, dziecko in dzieci:
                zastepnik = _usuwalne_wywolanie(dziecko)
                if zastepnik is None:
                    continue

                def usun(wezel=wezel, pole=pole, indeks=indeks, zastepnik=zastepnik) -> Callable[[], None]:
                    return _podmien(wezel, pole, indeks, zastepnik)

                zamiary.append(_Zamiar(dziecko.lineno, "wywolanie", f"usunieto wywolanie w {_tekst(dziecko)}", usun))
    return zamiary, nieliteralne


_FUNKCJE = (ast.FunctionDef, ast.AsyncFunctionDef)


def _zakres_funkcji(wezel: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[ast.AST, bool, int, int]:
    start = min([wezel.lineno, *(d.lineno for d in wezel.decorator_list)])
    return wezel, True, start, wezel.end_lineno or wezel.lineno


def _funkcje_w_instrukcji(wezel: ast.AST) -> Iterator[tuple[ast.AST, bool, int, int]]:
    """Funkcje i metody zdefiniowane w ciele instrukcji zlozonej (`if`, `try`, `with`, `for`, `while`, `match`).

    Przechodzi body/orelse/finalbody, `except` i `case` na dowolna glebokosc, takze przez klasy.
    Funkcji nie rozbiera dalej: funkcje w funkcjach obejmuje `ast.walk` zakresu funkcji.
    """
    for dziecko in ast.iter_child_nodes(wezel):
        if isinstance(dziecko, _FUNKCJE):
            yield _zakres_funkcji(dziecko)
        elif isinstance(dziecko, (ast.stmt, ast.ExceptHandler, ast.match_case)):
            yield from _funkcje_w_instrukcji(dziecko)


def _zakresy_instrukcji(instrukcje: list[ast.stmt]) -> Iterator[tuple[ast.AST, bool, int, int]]:
    """Zakresy zamiarow: funkcje i metody (funkcja=True) oraz stale modulu i klas (funkcja=False).

    Instrukcja zlozona modulu lub klasy jest oddawana w calosci jako zakres stalej (naglowki, np.
    `if x in {1, 2}`), a DODATKOWO kazda funkcja zdefiniowana w jej ciele (na dowolna glebokosc)
    jest oddawana jako zakres funkcji, zeby dostala mutanty instrukcji, `finally` i kolejnosci.
    Mutanty z obu przejsc laczy deduplikacja w `_analiza`.
    """
    for wezel in instrukcje:
        if isinstance(wezel, _FUNKCJE):
            yield _zakres_funkcji(wezel)
        elif isinstance(wezel, ast.ClassDef):
            yield from _zakresy_instrukcji(wezel.body)
        else:
            yield wezel, False, wezel.lineno, wezel.end_lineno or wezel.lineno
            yield from _funkcje_w_instrukcji(wezel)


def _stala_do_zbadania(wezel: ast.AST) -> bool:
    """Import i docstring nie sa stalymi (nie ma w nich czego mutowac)."""
    if isinstance(wezel, (ast.Import, ast.ImportFrom)):
        return False
    return not (
        isinstance(wezel, ast.Expr) and isinstance(wezel.value, ast.Constant) and isinstance(wezel.value.value, str)
    )


def _analiza(sciezka: str, zrodlo: str, linie: set[int]) -> tuple[list[Mutant], list[int], str, list[int]]:
    """(mutanty, linie startu dotknietych funkcji i stalych, zrodlo bazowe, linie `sprawdz recznie`).

    Zrodlo bazowe to `ast.unparse` niezmienionego drzewa: ten sam tekst co w mutantach,
    wiec przebieg kontrolny porownuje sie z mutantami bez roznic formatowania.
    Linie `sprawdz recznie` to `re.compile` i inne funkcje `re` (`RE_FUNKCJE_Z_WZORCEM`) z wzorcem
    nieliteralnym w dotknietych zakresach.
    """
    try:
        drzewo = ast.parse(zrodlo, filename=sciezka)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as blad:
        raise BladNarzedzia(f"{sciezka}: nie da sie sparsowac: {blad!r}") from blad
    zamiary: list[_Zamiar] = []
    dotkniete: list[int] = []
    reczne: list[int] = []
    try:
        for wezel, w_funkcji, start, koniec in _zakresy_instrukcji(drzewo.body):
            if not any(start <= numer <= koniec for numer in linie):
                continue
            if w_funkcji or _stala_do_zbadania(wezel):
                dotkniete.append(start)
            z_zakresu, nieliteralne = _zamiary_zakresu(wezel, w_funkcji)  # opisy mutantow wolaja ast.unparse
            zamiary += z_zakresu
            reczne += nieliteralne
    except _NiepoprawnyWzorzec as blad:
        raise BladNarzedzia(f"{sciezka}:{blad.linia}: nie umiem rozebrac wzorca ({blad})") from blad
    except (RecursionError, MemoryError) as blad:
        raise BladNarzedzia(f"{sciezka}: nie da sie opisac mutantow (ast.unparse): {blad!r}") from blad
    try:
        bazowe = ast.unparse(drzewo)
    except (RecursionError, MemoryError) as blad:
        raise BladNarzedzia(f"{sciezka}: nie da sie odtworzyc zrodla (ast.unparse): {blad!r}") from blad
    mutanty: list[Mutant] = []
    widziane: set[tuple[int, str, str]] = set()
    for zamiar in zamiary:
        cofnij = zamiar.zastosuj()
        try:
            nowe = ast.unparse(drzewo)
        except (RecursionError, MemoryError) as blad:
            raise BladNarzedzia(f"{sciezka}: nie da sie odtworzyc mutanta (ast.unparse): {blad!r}") from blad
        finally:
            cofnij()
        klucz = (zamiar.linia, zamiar.ksztalt, nowe)
        if nowe == bazowe or klucz in widziane:
            continue
        widziane.add(klucz)
        mutanty.append(Mutant(sciezka, zamiar.linia, zamiar.ksztalt, zamiar.opis, nowe))
    return mutanty, sorted(set(dotkniete)), bazowe, sorted(set(reczne))


def generuj_mutanty(sciezka: str, zrodlo: str, linie: set[int]) -> list[Mutant]:
    """Wszystkie mutanty funkcji i stalych modulu, ktorych zakres przecina `linie`."""
    return _analiza(sciezka, zrodlo, linie)[0]


# --- uruchamianie testow ----------------------------------------------------------------------


def _wymagaj_wykonanych(raport: Path, testy: list[str]) -> None:
    """Raport JUnit musi dowodzic, ze co najmniej jeden test sie wykonal (nie tylko pominieto)."""
    nazwa = ", ".join(testy)
    try:
        xml = ET.parse(raport).getroot()
    except OSError as blad:
        raise BladNarzedzia(f"{nazwa}: brak raportu JUnit {raport}: brak dowodu ({blad})") from blad
    except ET.ParseError as blad:
        raise BladNarzedzia(f"{nazwa}: raport JUnit nie jest poprawnym XML: brak dowodu ({blad})") from blad
    try:
        wykonane = sum(int(s.get("tests", "0")) - int(s.get("skipped", "0")) for s in xml.iter("testsuite"))
    except ValueError as blad:
        raise BladNarzedzia(f"{nazwa}: raport JUnit ma liczniki nie bedace liczbami: brak dowodu ({blad})") from blad
    if wykonane <= 0:
        raise BladNarzedzia(f"{nazwa}: zaden test sie nie wykonal wedlug raportu JUnit - brak dowodu")


def _uruchom_pytest(kopia: Path, testy: list[str], limit: float) -> int:
    raport = kopia / RAPORT_JUNIT
    polecenie = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
        f"--junitxml={raport}", *testy,
    ]  # fmt: skip
    stare = os.environ.get("PYTHONPATH")
    sciezki = os.pathsep.join([str(kopia / "src"), *([stare] if stare else [])])
    srodowisko = {**os.environ, "PYTHONPATH": sciezki, "PYTHONDONTWRITEBYTECODE": "1"}
    srodowisko.pop(mutanty_reczne.ZMIENNA_WYMAGANIA_BAZY, None)
    kod, _ = mutanty_reczne._uruchom_proces(polecenie, kopia, srodowisko, limit, f"testow {', '.join(testy)}")
    if kod in (0, 1):
        _wymagaj_wykonanych(raport, testy)
    return kod


@contextmanager
def _kopia_repo(korzen: Path) -> Iterator[Path]:
    try:
        kopia = Path(tempfile.mkdtemp(prefix="mutanty-uzupelniajace-"))
    except OSError as blad:
        raise BladNarzedzia(f"nie da sie utworzyc katalogu tymczasowego: {blad}") from blad
    try:
        mutanty_reczne.skopiuj_repo(korzen, kopia)
        yield kopia
    finally:
        try:
            shutil.rmtree(kopia)
        except OSError as blad:
            raise BladNarzedzia(f"nie da sie usunac katalogu tymczasowego {kopia}: {blad}") from blad


def _zapisz_w_kopii(kopia: Path, plik: str, zrodlo: str) -> None:
    try:
        (kopia / plik).write_text(zrodlo, encoding="utf-8")
    except (OSError, UnicodeEncodeError) as blad:
        raise BladNarzedzia(f"{plik}: nie da sie zapisac zrodla w kopii: {blad}") from blad


def _sprawdz_testy_bez_mutacji(korzen: Path, plik: str, bazowe: str, testy: list[str], limit: float) -> None:
    """Testy na kopii z `bazowe` (unparse niezmienionego pliku), czyli tym samym tekstem co mutanty."""
    with _kopia_repo(korzen) as kopia:
        _zapisz_w_kopii(kopia, plik, bazowe)
        kod = _uruchom_pytest(kopia, testy, limit)
    if kod != 0:
        raise BladNarzedzia(
            f"testy {', '.join(testy)} nie przechodza bez mutacji (kod pytesta {kod}): brak dowodu"
        )


def _mutant_zabity(korzen: Path, mutant: Mutant, testy: list[str], limit: float) -> bool:
    with _kopia_repo(korzen) as kopia:
        _zapisz_w_kopii(kopia, mutant.plik, mutant.zrodlo)
        return mutanty_reczne.ocen_kod_testu(_uruchom_pytest(kopia, testy, limit))


# --- wybor testow, tabele, diff -----------------------------------------------------------------


def _blad_katalogu(blad: OSError) -> None:
    raise BladNarzedzia(f"nie da sie przejrzec katalogu testow: {blad}")


def _importuje_modul(nazwa: str, tresc: str, modul: str) -> bool:
    """Test wskazuje modul pelna nazwa (`import a.b`, `a.b.f`) albo importem `from a import b`."""
    if modul in tresc:
        return True
    pakiet, _, koncowka = modul.rpartition(".")
    try:
        drzewo = ast.parse(tresc, filename=nazwa)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as blad_testu:
        raise BladNarzedzia(f"{nazwa}: nie da sie sparsowac testu: {blad_testu}") from blad_testu
    return any(
        isinstance(wezel, ast.ImportFrom) and wezel.module == pakiet
        and any(alias.name == koncowka for alias in wezel.names)
        for wezel in ast.walk(drzewo)
    )


def _tresci_testow(korzen: Path) -> dict[str, str]:
    """Pliki `.py` pod `tests/` (bez `tests/mutanty_reczne/`) -> tresc."""
    tresci: dict[str, str] = {}
    pominiety = Path(*KATALOG_TABEL)
    for katalog, podkatalogi, pliki in os.walk(korzen / "tests", onerror=_blad_katalogu):
        wzgledny = Path(katalog).relative_to(korzen)
        podkatalogi[:] = sorted(p for p in podkatalogi if wzgledny / p != pominiety)
        for plik in sorted(pliki):
            if not plik.endswith(".py"):
                continue
            sciezka = Path(katalog) / plik
            try:
                tresci[(wzgledny / plik).as_posix()] = sciezka.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as blad:
                raise BladNarzedzia(f"{sciezka}: nie da sie odczytac testu: {blad}") from blad
    return tresci


def _rownowazne(korzen: Path) -> list[mutanty_reczne.Rownowazny]:
    katalog = korzen.joinpath(*KATALOG_TABEL)
    try:
        tabele = sorted(k for k in katalog.iterdir() if k.name.endswith(".toml"))
    except FileNotFoundError:
        return []
    except OSError as blad:
        raise BladNarzedzia(f"nie da sie odczytac katalogu tabel {katalog}: {blad}") from blad
    wynik: list[mutanty_reczne.Rownowazny] = []
    for tabela in tabele:
        wynik.extend(mutanty_reczne.wczytaj_tabele_z_rownowaznymi(tabela)[1])
    return wynik


def _zmiany(korzen: Path, baza: str) -> dict[str, set[int]]:
    merge = mutanty_reczne.uruchom(["git", "merge-base", baza, "HEAD"], korzen)
    if merge.returncode != 0:
        raise BladNarzedzia(f"nieznana baza {baza!r}: {merge.stderr.strip()}")
    sha = merge.stdout.strip()
    roznica = mutanty_reczne.uruchom(
        [
            "git", "diff", "-U0", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/",
            sha, "--", "src/",
        ],
        korzen,
    )  # fmt: skip
    if roznica.returncode != 0:
        raise BladNarzedzia(f"git diff nie powiodl sie: {roznica.stderr.strip()}")
    nowe = mutanty_reczne.uruchom(["git", "ls-files", "--others", "--exclude-standard", "--", "src/"], korzen)
    if nowe.returncode != 0:
        raise BladNarzedzia(f"git ls-files nie powiodl sie: {nowe.stderr.strip()}")
    zmiany = mutanty_zmian.zmienione_linie(roznica.stdout)
    for sciezka, linie in mutanty_zmian._nowe_pliki(nowe.stdout, korzen).items():
        zmiany.setdefault(sciezka, set()).update(linie)
    return {s: numery for s, numery in zmiany.items() if s.endswith(".py") and s.startswith("src/")}


def _linie_zrodla(zrodlo: str) -> list[str]:
    """Linie zrodla numerowane jak w AST: dziela tylko `\\n`, `\\r\\n` i `\\r` (nie `str.splitlines()`)."""
    return re.split(r"\r\n|\n|\r", zrodlo)


def _wycisza(wpis: mutanty_reczne.Rownowazny, mutant: Mutant, tekst_linii: str) -> bool:
    """Wpis `[[rownowazny]]` wycisza mutanta tylko po plik + linia ORAZ `kształt: <nazwa>` w uzasadnieniu.

    Dla `except` wpis dotyczy jednego elementu krotki: uzasadnienie musi zawierac caly opis mutanta.
    """
    if wpis.plik != mutant.plik or wpis.linia_tekst.strip() != tekst_linii:
        return False
    if f"kształt: {mutant.ksztalt}" not in wpis.uzasadnienie:
        return False
    return mutant.ksztalt != "except" or mutant.opis in wpis.uzasadnienie


def _linie_recznie(miejsca: list[tuple[str, int]]) -> list[str]:
    return [f"{plik}:{linia}: sprawdz recznie" for plik, linia in miejsca]


def _przebieg(korzen: Path, baza: str, limit: float, procesy: int = 1) -> tuple[int, list[str]]:
    """(kod, linie stdout); bledy to wyjatki, wiec przy kodzie 2 stdout zostaje pusty.

    Priorytet: blad narzedzia 2 > przezyl mutant 1 > `sprawdz recznie` 3 > 0 (`zbadano N`, N > 0,
    albo `brak zmienionych funkcji`). Przy kodzie 1 stdout to wylacznie linie mutantow.
    """
    zmiany = _zmiany(korzen, baza)
    mutanty: list[Mutant] = []
    zrodla: dict[str, str] = {}
    bazowe: dict[str, str] = {}
    dotkniete: list[tuple[str, int]] = []
    reczne: list[tuple[str, int]] = []
    for sciezka in sorted(zmiany):
        try:
            zrodlo = (korzen / sciezka).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError) as blad:
            raise BladNarzedzia(f"{sciezka}: nie da sie odczytac: {blad}") from blad
        zrodla[sciezka] = zrodlo
        z_pliku, dotyka, bazowe[sciezka], nieliteralne = _analiza(sciezka, zrodlo, zmiany[sciezka])
        mutanty += z_pliku
        dotkniete += [(sciezka, numer) for numer in dotyka]
        reczne += [(sciezka, numer) for numer in nieliteralne]
    rownowazne = _rownowazne(korzen)
    if not mutanty:
        if reczne or dotkniete:
            # brak mutantow przy dotknietym kodzie to brak dowodu, nie "zbadano 0"
            return 3, _linie_recznie(sorted({*reczne, *dotkniete}))
        return 0, ["brak zmienionych funkcji"]

    tresci = _tresci_testow(korzen)
    testy_pliku: dict[str, list[str]] = {}
    for sciezka in sorted({m.plik for m in mutanty}):
        modul = mutanty_zmian.modul_ze_sciezki(sciezka)
        testy = sorted(nazwa for nazwa, tresc in tresci.items() if _importuje_modul(nazwa, tresc, modul))
        if not testy:
            raise BladNarzedzia(f"{sciezka}: zero zebranych testow dla modulu {modul} - brak dowodu")
        testy_pliku[sciezka] = testy

    # Kazdy przebieg w swojej kopii, wiec naraz; z ustawiona baza testy `baza` dzielilyby jedna baze.
    naraz = 1 if os.environ.get(mutanty_reczne.ZMIENNA_URL_BAZY) else procesy
    mutanty_reczne.rownolegle(
        lambda plik: _sprawdz_testy_bez_mutacji(korzen, plik, bazowe[plik], testy_pliku[plik], limit),
        sorted(testy_pliku), naraz,
    )
    zabite = mutanty_reczne.rownolegle(
        lambda m: _mutant_zabity(korzen, m, testy_pliku[m.plik], limit), mutanty, naraz
    )
    przezyly = [m for m, zabity in zip(mutanty, zabite) if not zabity]

    zglaszane = []
    for mutant in przezyly:
        tekst_linii = _linie_zrodla(zrodla[mutant.plik])[mutant.linia - 1].strip()
        if not any(_wycisza(r, mutant, tekst_linii) for r in rownowazne):
            zglaszane.append(mutant)
    if zglaszane:
        zglaszane.sort(key=lambda m: (m.plik, m.linia, m.ksztalt, m.opis))
        return 1, [f"{m.plik}:{m.linia}: {m.ksztalt}: {m.opis}" for m in zglaszane]
    if reczne:
        return 3, _linie_recznie(sorted(set(reczne)))
    return 0, [f"zbadano {len(mutanty)}"]


def _limit(tekst: str) -> float:
    try:
        wartosc = float(tekst)
    except ValueError as blad:
        raise argparse.ArgumentTypeError(f"{tekst!r} nie jest liczba") from blad
    if not math.isfinite(wartosc) or wartosc <= 0:
        raise argparse.ArgumentTypeError(f"limit musi byc skonczona liczba > 0, jest {tekst!r}")
    return wartosc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baza", default="origin/main", help="baza porownania (merge-base z HEAD)")
    parser.add_argument("--korzen", type=Path, default=None, help="korzen repozytorium (domyslnie cwd)")
    parser.add_argument("--limit", type=_limit, default=LIMIT_DOMYSLNY, help="limit czasu jednego pytesta w sekundach")
    args = parser.parse_args(argv)
    korzen = args.korzen if args.korzen is not None else Path.cwd()
    try:
        kod, linie = _przebieg(korzen, args.baza, args.limit, os.cpu_count() or 1)
    except (BladNarzedzia, mutanty_zmian.BladNarzedzia, mutanty_reczne.BladNarzedzia) as blad:
        print(f"blad: {blad}", file=sys.stderr)
        return 2
    for linia in linie:
        print(linia)
    return kod


if __name__ == "__main__":
    sys.exit(main())
