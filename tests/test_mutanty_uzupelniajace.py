"""Mutanty uzupelniajace (HAR-241): `tools/mutanty_uzupelniajace.py`.

Bramka dla kształtów, których mutmut nie generuje: element krotki `except`, instrukcja
(`finally`, samodzielne wywołanie), kolejność dwóch sąsiednich instrukcji, element literału
zbioru, krotki i mapy, usunięcie `sorted(...)` / `.strip()` oraz element wyrażenia regularnego.

UMOWA NARZEDZIA (testy ja definiuja; implementacja ma sie do niej dostosowac)
-----------------------------------------------------------------------------
Modul `tools/mutanty_uzupelniajace.py`:

    KSZTALTY = ("except", "instrukcja", "kolejnosc", "kolekcja", "wywolanie", "regex")
    WYWOLANIA_USUWALNE = ("sorted", "strip")
    class BladNarzedzia(Exception)          # kod wyjscia 2

    @dataclass(frozen=True)
    class Mutant:
        plik: str       # sciezka wzgledna wobec korzenia, np. "src/pakiet/modul.py"
        linia: int      # numer linii (od 1) zmutowanego elementu; przy zamianie dwoch sasiednich
                        # instrukcji - linia pierwszej z nich
        ksztalt: str    # jeden z KSZTALTY
        opis: str       # niepusty, czytelny opis mutacji
        zrodlo: str     # CALY tekst pliku po mutacji

    def generuj_mutanty(sciezka: str, zrodlo: str, linie: set[int]) -> list[Mutant]
    def main(argv: list[str] | None = None) -> int

Zakres `generuj_mutanty`: funkcje i metody (takze async), ktorych zakres (z dekoratorami)
przecina `linie` - WSZYSTKIE ich mutanty, nie tylko z zmienionych linii - oraz instrukcje na
poziomie modulu (stale), ktorych linie przecinaja `linie`. Niesparsowalny plik to `BladNarzedzia`.

Kazdy mutant to dokladnie jedna zmiana:
- `except`: usuniecie jednego elementu krotki `except` (krotka zostaje krotka, takze
  jednoelementowa: `except (TypeError,):`); element krotki `except` NIE jest dodatkowo
  mutantem `kolekcja`.
- `instrukcja`: zastapienie instrukcji `pass` - samodzielnego wywolania w dowolnym miejscu
  funkcji (pola `body`, `orelse`, `finalbody`), w tym w `finally`. SAMODZIELNE WYWOLANIE to
  instrukcja-wyrazenie `f(...)` ORAZ `await f(...)` (HAR-241 runda 2); `await x` bez wywolania,
  `y = await f()` i wyrazenia w `IfExp`/`lambda` nim nie sa.
- `kolejnosc`: zamiana miejscami dwoch SASIEDNICH samodzielnych wywolan (jedna zamiana na pare;
  takze dla `await`). Zamiana dwoch identycznych wywolan nie zmienia kodu, wiec nie jest mutantem.
- `kolekcja`: usuniecie jednego elementu literalu zbioru, krotki albo mapy (nie `except`).
  Linia mutanta to linia USUWANEGO elementu (takze w literale wieloliniowym). Elementy bez
  skutku: krotka w kontekscie zapisu/usuwania (`a, b = x`, `del (a, b)`, `for a, b in x`) i krotka
  w indeksie (`d[1, 2]`, `dict[str, int]`) nie sa literalami z elementami. Dwa usuniecia dajace ten
  sam tekst w tej samej linii to jeden mutant; w roznych liniach - dwa.
- `wywolanie`: `sorted(x)` -> `x` oraz `x.strip()` -> `x` (zamkniete: WYWOLANIA_USUWALNE).
- `regex`: w literale pierwszego argumentu `re.compile(...)`: usuniecie jednej kotwicy (`^`, `$`),
  jednego elementu klasy znakow (znak albo sekwencja ucieczki `\\[` to JEDEN element; zakres
  `0-9` to JEDEN element; `^` negacji klasy nie jest elementem) albo jednej alternatywy
  grupy. Mutant, ktorego wzorzec nie kompiluje sie (np. pusta klasa `[]`), jest pomijany
  i nie liczy sie do `zbadano N`. Wzorzec, ktorego narzedzie nie umie rozebrac (np. nawias
  w komentarzu `(?x)`, niedomkniete `[`, `\\N{` bez `}`), nie daje mutantow regex.

OPIS (`Mutant.opis`, ostatnia czesc linii wyniku `plik:linia: ksztalt: opis` - opis jest w
calej linii, nie tylko jej poczatku). Wymagane formaty:
- `except`: `usunieto element <tekst usunietego elementu> z krotki except`, np.
  `usunieto element ValueError z krotki except` (dla KAZDEGO mutanta nazwa jego elementu);
- mapa: `usunieto element 'k': 1 z mapy` oraz dla rozwiniecia `usunieto element **a z mapy`.

ZRODLO MUTANTA I PRZEBIEG KONTROLNY. `Mutant.zrodlo` to `ast.unparse` calego zmutowanego drzewa.
Przebieg kontrolny (testy "bez mutacji") idzie na TYM SAMYM tekstem co mutanty, czyli na
`ast.unparse(ast.parse(zrodlo))` niezmienionego pliku, nie na oryginale: test zalezny od tekstu
modulu (komentarz, numer linii) jest wtedy czerwony BEZ mutacji (kod 2, stderr zawiera
`nie przechodza bez mutacji` i sciezke testu) i nie "zabija" po cichu kazdego mutanta.
Komunikat przebiegu kontrolnego: `testy <sciezki po przecinku, posortowane> nie przechodza bez
mutacji (kod pytesta N): brak dowodu`.

BLEDY NARZEDZIA (kod 2, `BladNarzedzia`, komunikat zaczyna sie od sciezki pliku): kazde
`RecursionError` i `MemoryError` z `ast.parse` i `ast.unparse` (takze w opisie mutanta, np.
element mapy z 3000-czlonowym wyrazeniem) oraz `ValueError` z `ast.parse`
(np. `UnicodeEncodeError` dla samotnego surrogatu) to `BladNarzedzia`, nigdy traceback ani kod 1.
Komunikaty strazników gita (kazdy ma wlasny, bo kazdy nieudany git to kod 2, nie "brak zmian"):
- `git merge-base` -> `nieznana baza '<baza>': <stderr gita>`;
- `git diff` -> `git diff nie powiodl sie: <stderr gita>`;
- `git ls-files` -> `git ls-files nie powiodl sie: <stderr gita>`;
- brak katalogu `tests/` -> `nie da sie przejrzec katalogu testow: ...`;
- plik `.py` pod `tests/` nieczytelny (OSError, UnicodeDecodeError) -> `<sciezka>: nie da sie
  odczytac testu: ...`;
- `_kopia_repo`: `nie da sie utworzyc katalogu tymczasowego: ...` i
  `nie da sie usunac katalogu tymczasowego <katalog>: ...`; kopie sa sprzatane.

ZAKRES: w `_zmiany` licza sie tylko pliki `.py` pod `src/` (zmiana `src/x/dane.txt` to nie
funkcja i nie blad skladni), nieSledzone pliki z `.gitignore` (np. `__pycache__/`) nie wchodza.
Granice: linia `def` i ostatnia linia funkcji nalezą do funkcji, linia przed `def` i po
ostatniej - nie. Stala w ciele klasy jest stala modulu (zakres = jej instrukcja, wieloliniowa
tez), metoda klasy ma wlasny zakres.
`zbadano 0` (kod 0) to JAWNA galaz: diff dotyka funkcji albo stalej (`X = 1`, `42`, `print()`
na poziomie modulu), ale zaden ksztalt nie daje mutanta. Sam import, sam docstring modulu
albo sama zmiana poza instrukcjami (komentarz) to `brak zmienionych funkcji`.

Wybor testow: pliki `.py` pod `tests/` (bez `tests/mutanty_reczne/` na korzeniu - katalog o
tej nazwie glebiej JEST przegladany), bez plikow nie-`.py`. Kopia z testami nie dostaje zmiennej
`mutanty_reczne.ZMIENNA_WYMAGANIA_BAZY`.

CLI: `main(["--korzen", KATALOG, "--baza", REF, "--limit", SEKUNDY])`; domyslnie korzen =
biezacy katalog, baza = `origin/main`, limit = 300 s na jedno uruchomienie pytesta. Zmiany:
`git diff` wzgledem merge-base(baza, HEAD) dla `src/` plus nowe nieSledzone pliki `.py` pod
`src/` (jak `tools/mutanty_zmian.py`).

Wybor testow: pliki `.py` pod `tests/` (bez `tests/mutanty_reczne/`), ktorych TRESC zawiera
nazwe modulu `modul_ze_sciezki(sciezka)` z `tools/mutanty_zmian.py` (np. `pakiet.modul`).
Kazdy mutant: nowa kopia repozytorium (`mutanty_reczne.skopiuj_repo`) w nowym katalogu
tymczasowym, pytest (`-p no:cacheprovider -o addopts=`, `PYTHONPATH=<kopia>/src`) na wybranych
plikach; kod pytesta 1 = mutant zabity, 0 = przezyl. Przed mutantami wybrane testy musza
przechodzic na kopii BEZ mutacji.

Kody wyjscia i wyjscie:
- 0: wszystkie zabite (albo rownowazne). stdout to dokladnie jedna linia `zbadano N`
  (N = liczba uruchomionych mutantow, takze tych wyciszonych `[[rownowazny]]`), albo, gdy
  diff nie dotyka zadnej funkcji ani stalej, dokladnie `brak zmienionych funkcji`.
- 1: przezyl co najmniej jeden mutant nieuznany za rownowazny. stdout to WYLACZNIE linie
  `<plik>:<linia>: <ksztalt>: <opis>`, posortowane po pliku i liczbowo po linii.
- 2: blad narzedzia (komunikat na stderr, stdout bez linii wynikow ani `zbadano`): nieznana baza,
  niesparsowalny plik, zepsuta tabela `tests/mutanty_reczne/*.toml`, ZERO zebranych testow
  (stderr zawiera nazwe modulu), test czerwony bez mutacji (stderr zawiera sciezke testu).

`[[rownowazny]]`: tabele `tests/mutanty_reczne/*.toml` w korzeniu czyta
`mutanty_reczne.wczytaj_tabele_z_rownowaznymi`; mutant jest wyciszony TYLKO gdy wpis spelnia
WSZYSTKIE trzy warunki:
1. `plik` wpisu rowna sie plikowi mutanta, a `linia_tekst.strip()` rowna sie `strip()` tekstu
   linii mutanta w ORYGINALNYM pliku;
2. `uzasadnienie` zawiera DOSLOWNIE podciag `kształt: <nazwa>` (z "ł", jedna spacja po dwukropku),
   gdzie `<nazwa>` to `Mutant.ksztalt` (np. `kształt: except`, `kształt: regex`);
3. dla ksztaltu `except` `uzasadnienie` zawiera tez DOSLOWNIE caly `Mutant.opis` mutanta
   (np. `usunieto element ValueError z krotki except`) - wpis dotyczy jednego elementu krotki.
Wpis bez tych podciagow (sama linia, zly ksztalt, brak opisu dla `except`) NIE wycisza: wpis
z tabeli innego ticketu dla tej samej linii nie gasi cudzych mutantow. Bez wpisu mutant jest
zglaszany. Wyciszony mutant liczy sie do `zbadano N`.
"""
import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from procesy import uruchom_proces_sync, zakoncz_proces_sync

KORZEN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KORZEN / "tools"))
import mutanty_uzupelniajace  # noqa: E402

SCIEZKA = "src/pakiet/modul.py"
TEST_MODULU = "tests/test_modul.py"
LIMIT_TESTU_S = "120"

LINIA_WYNIKU = re.compile(
    r"^(?P<plik>\S+?):(?P<linia>[1-9][0-9]*): (?P<ksztalt>[a-z]+): (?P<opis>\S.*)$"
)


# --- przypadki: po jednym na wariant kształtu --------------------------------------------------


@dataclass(frozen=True)
class Przypadek:
    nazwa: str
    ksztalt: str  # kształt, którego dotyczy przypadek
    zrodlo: str
    fragment: str  # tekst linii, którą narzędzie ma wskazać dla tego kształtu
    mutanty: tuple[tuple[str, str], ...]  # (szukaj, zamien) - wszystkie oczekiwane mutanty
    slaby_test: str  # przechodzi z mutantem, więc nie chroni elementu
    mocny_test: str  # oblewa każdy z mutantów
    liczby: dict[str, int]  # oczekiwana liczba mutantów według kształtu


def _przypadek(nazwa, ksztalt, zrodlo, fragment, mutanty, slaby, mocny, liczby=None) -> Przypadek:
    return Przypadek(
        nazwa, ksztalt, zrodlo, fragment, tuple(mutanty), slaby, mocny,
        liczby or {ksztalt: len(mutanty)},
    )


ZRODLO_EXCEPT = """\
def wczytaj(tekst):
    try:
        return int(tekst)
    except (ValueError, TypeError):
        return None
"""

ZRODLO_FINALLY = """\
def uzyj(kanal):
    try:
        return kanal.czytaj()
    finally:
        kanal.zamknij()
"""

KANAL = """\
class Kanal:
    def __init__(self):
        self.zamkniety = False

    def czytaj(self):
        return "dane"

    def zamknij(self):
        self.zamkniety = True
"""

ZRODLO_KOLEJNOSC = """\
def zapisz(dziennik):
    dziennik.dopisz("artefakt")
    dziennik.dopisz("checkpoint")
"""

DZIENNIK = """\
class Dziennik:
    def __init__(self):
        self.wpisy = []

    def dopisz(self, wpis):
        self.wpisy.append(wpis)
"""

ZRODLO_ZBIOR = """\
STANY_KONCOWE = {"zakonczony", "przerwany", "odrzucony"}


def czy_koniec(stan):
    return stan in STANY_KONCOWE
"""

ZRODLO_KROTKA = """\
def czy_zapis(rola):
    return rola in ("author", "executor")
"""

ZRODLO_MAPA = """\
def etykieta(stan):
    return {"ok": "zielony", "blad": "czerwony"}[stan]
"""

ZRODLO_SORTED = """\
def kolejne(kroki):
    return sorted(kroki)
"""

ZRODLO_STRIP = """\
def nazwa(wpis):
    return wpis.strip()
"""

ZRODLO_KLASA_ZNAKOW = """\
import re

LITERY = re.compile(r"^[abc]$")


def czy_litera(tekst):
    return LITERY.search(tekst) is not None
"""

ZRODLO_ALTERNATYWA = """\
import re

NAZWA = re.compile(r"^(?:ab|cd)$")


def czy_nazwa(tekst):
    return NAZWA.search(tekst) is not None
"""

ZRODLO_PUSTA_KLASA = """\
import re

JEDNA = re.compile(r"^[a]$")


def czy_a(tekst):
    return JEDNA.search(tekst) is not None
"""

ZRODLO_AWAIT_FINALLY = """\
async def zakoncz(klient):
    try:
        return await klient.czytaj()
    finally:
        await klient.anuluj()
        await klient.stop()
"""

KLIENT = """\
class Klient:
    def __init__(self):
        self.zapisy = []

    async def czytaj(self):
        return "dane"

    async def anuluj(self):
        self.zapisy.append("anuluj")

    async def stop(self):
        self.zapisy.append("stop")
"""

PRZYPADKI = [
    _przypadek(
        "krotka_except",
        "except",
        ZRODLO_EXCEPT,
        "except (ValueError, TypeError):",
        [
            ("except (ValueError, TypeError):", "except (TypeError,):"),
            ("except (ValueError, TypeError):", "except (ValueError,):"),
        ],
        # slaby: nie wchodzi w `except`, wiec przezywa usuniecie KAZDEGO elementu krotki
        "from pakiet.modul import wczytaj\n\n\n"
        "def test_tekst_z_liczba_daje_liczbe():\n"
        '    assert wczytaj("12") == 12\n',
        "from pakiet.modul import wczytaj\n\n\n"
        "def test_tekst_nie_liczba_daje_brak():\n"
        '    assert wczytaj("abc") is None\n\n\n'
        "def test_brak_tekstu_daje_brak():\n"
        "    assert wczytaj(None) is None\n",
    ),
    _przypadek(
        "zwolnienie_w_finally",
        "instrukcja",
        ZRODLO_FINALLY,
        "kanal.zamknij()",
        [("kanal.zamknij()", "pass")],
        "from pakiet.modul import uzyj\n\n" + KANAL + "\n\n"
        "def test_uzyj_zwraca_dane_kanalu():\n"
        "    assert uzyj(Kanal()) == 'dane'\n",
        "from pakiet.modul import uzyj\n\n" + KANAL + "\n\n"
        "def test_uzyj_zwraca_dane_kanalu():\n"
        "    assert uzyj(Kanal()) == 'dane'\n\n\n"
        "def test_uzyj_zamyka_kanal():\n"
        "    kanal = Kanal()\n"
        "    uzyj(kanal)\n"
        "    assert kanal.zamkniety\n",
    ),
    _przypadek(
        "kolejnosc_dwoch_zapisow",
        "kolejnosc",
        ZRODLO_KOLEJNOSC,
        'dziennik.dopisz("artefakt")',
        [
            (
                'dziennik.dopisz("artefakt")\n    dziennik.dopisz("checkpoint")',
                'dziennik.dopisz("checkpoint")\n    dziennik.dopisz("artefakt")',
            ),
            ('dziennik.dopisz("artefakt")', "pass"),
            ('dziennik.dopisz("checkpoint")', "pass"),
        ],
        "from pakiet.modul import zapisz\n\n" + DZIENNIK + "\n\n"
        "def test_oba_zapisy_trafiaja_do_dziennika():\n"
        "    dziennik = Dziennik()\n"
        "    zapisz(dziennik)\n"
        "    assert sorted(dziennik.wpisy) == ['artefakt', 'checkpoint']\n",
        "from pakiet.modul import zapisz\n\n" + DZIENNIK + "\n\n"
        "def test_artefakt_przed_checkpointem():\n"
        "    dziennik = Dziennik()\n"
        "    zapisz(dziennik)\n"
        "    assert dziennik.wpisy == ['artefakt', 'checkpoint']\n",
        liczby={"kolejnosc": 1, "instrukcja": 2},
    ),
    _przypadek(
        "stala_zbioru_stanow",
        "kolekcja",
        ZRODLO_ZBIOR,
        "STANY_KONCOWE = {",
        [
            (
                '{"zakonczony", "przerwany", "odrzucony"}',
                '{"przerwany", "odrzucony"}',
            ),
            (
                '{"zakonczony", "przerwany", "odrzucony"}',
                '{"zakonczony", "odrzucony"}',
            ),
            (
                '{"zakonczony", "przerwany", "odrzucony"}',
                '{"zakonczony", "przerwany"}',
            ),
        ],
        "from pakiet.modul import czy_koniec\n\n\n"
        "def test_stany_zakonczony_i_przerwany_sa_koncowe_a_trwa_nie():\n"
        "    assert czy_koniec('zakonczony')\n"
        "    assert czy_koniec('przerwany')\n"
        "    assert not czy_koniec('trwa')\n",
        "from pakiet.modul import czy_koniec\n\n\n"
        "def test_kazdy_stan_koncowy_jest_koncowy_a_trwa_nie():\n"
        "    for stan in ('zakonczony', 'przerwany', 'odrzucony'):\n"
        "        assert czy_koniec(stan)\n"
        "    assert not czy_koniec('trwa')\n",
    ),
    _przypadek(
        "krotka_w_funkcji",
        "kolekcja",
        ZRODLO_KROTKA,
        'rola in ("author", "executor")',
        [
            ('("author", "executor")', '("executor",)'),
            ('("author", "executor")', '("author",)'),
        ],
        "from pakiet.modul import czy_zapis\n\n\n"
        "def test_autor_zapisuje_a_recenzent_nie():\n"
        "    assert czy_zapis('author')\n"
        "    assert not czy_zapis('reviewer')\n",
        "from pakiet.modul import czy_zapis\n\n\n"
        "def test_autor_i_wykonawca_zapisuja_a_recenzent_nie():\n"
        "    assert czy_zapis('author')\n"
        "    assert czy_zapis('executor')\n"
        "    assert not czy_zapis('reviewer')\n",
    ),
    _przypadek(
        "mapa_w_funkcji",
        "kolekcja",
        ZRODLO_MAPA,
        '{"ok": "zielony", "blad": "czerwony"}',
        [
            ('{"ok": "zielony", "blad": "czerwony"}', '{"blad": "czerwony"}'),
            ('{"ok": "zielony", "blad": "czerwony"}', '{"ok": "zielony"}'),
        ],
        "from pakiet.modul import etykieta\n\n\n"
        "def test_ok_jest_zielone():\n"
        "    assert etykieta('ok') == 'zielony'\n",
        "from pakiet.modul import etykieta\n\n\n"
        "def test_ok_jest_zielone_a_blad_czerwony():\n"
        "    assert etykieta('ok') == 'zielony'\n"
        "    assert etykieta('blad') == 'czerwony'\n",
    ),
    _przypadek(
        "sorted_w_funkcji",
        "wywolanie",
        ZRODLO_SORTED,
        "return sorted(kroki)",
        [("sorted(kroki)", "kroki")],
        "from pakiet.modul import kolejne\n\n\n"
        "def test_kroki_juz_po_kolei_zostaja_po_kolei():\n"
        "    assert kolejne([1, 2, 3]) == [1, 2, 3]\n",
        "from pakiet.modul import kolejne\n\n\n"
        "def test_kroki_poprzeplatane_sa_po_kolei():\n"
        "    assert kolejne([3, 1, 2]) == [1, 2, 3]\n",
    ),
    _przypadek(
        "strip_w_funkcji",
        "wywolanie",
        ZRODLO_STRIP,
        "return wpis.strip()",
        [("wpis.strip()", "wpis")],
        "from pakiet.modul import nazwa\n\n\n"
        "def test_nazwa_bez_bialych_znakow_zostaje_bez_zmian():\n"
        "    assert nazwa('abc') == 'abc'\n",
        "from pakiet.modul import nazwa\n\n\n"
        "def test_nazwa_z_bialymi_znakami_jest_przycieta():\n"
        "    assert nazwa('  abc \\n') == 'abc'\n",
    ),
    _przypadek(
        "klasa_znakow_i_kotwice",
        "regex",
        ZRODLO_KLASA_ZNAKOW,
        "LITERY = re.compile",
        [
            ('r"^[abc]$"', 'r"[abc]$"'),
            ('r"^[abc]$"', 'r"^[abc]"'),
            ('r"^[abc]$"', 'r"^[bc]$"'),
            ('r"^[abc]$"', 'r"^[ac]$"'),
            ('r"^[abc]$"', 'r"^[ab]$"'),
        ],
        # slaby: sprawdza tylko, ze odpowiedz jest tak/nie, wiec przezywa kazdy z 5 mutantow
        "from pakiet.modul import czy_litera\n\n\n"
        "def test_odpowiedz_dla_litery_jest_tak_albo_nie():\n"
        "    assert isinstance(czy_litera('a'), bool)\n",
        "from pakiet.modul import czy_litera\n\n\n"
        "def test_kazda_litera_z_klasy_pasuje_a_obce_otoczenie_nie():\n"
        "    for litera in 'abc':\n"
        "        assert czy_litera(litera)\n"
        "    assert not czy_litera('xa')\n"
        "    assert not czy_litera('ax')\n",
    ),
    _przypadek(
        "alternatywa_w_grupie",
        "regex",
        ZRODLO_ALTERNATYWA,
        "NAZWA = re.compile",
        [
            ('r"^(?:ab|cd)$"', 'r"(?:ab|cd)$"'),
            ('r"^(?:ab|cd)$"', 'r"^(?:ab|cd)"'),
            ('r"^(?:ab|cd)$"', 'r"^(?:cd)$"'),
            ('r"^(?:ab|cd)$"', 'r"^(?:ab)$"'),
        ],
        "from pakiet.modul import czy_nazwa\n\n\n"
        "def test_ab_pasuje_a_x_nie():\n"
        "    assert czy_nazwa('ab')\n"
        "    assert not czy_nazwa('x')\n",
        "from pakiet.modul import czy_nazwa\n\n\n"
        "def test_ab_i_cd_pasuja_a_obce_otoczenie_nie():\n"
        "    assert czy_nazwa('ab')\n"
        "    assert czy_nazwa('cd')\n"
        "    assert not czy_nazwa('x')\n"
        "    assert not czy_nazwa('xab')\n"
        "    assert not czy_nazwa('abx')\n",
    ),
    _przypadek(
        "regex_z_mutantem_niekompilowalnym",
        "regex",
        ZRODLO_PUSTA_KLASA,
        "JEDNA = re.compile",
        [
            ('r"^[a]$"', 'r"[a]$"'),
            ('r"^[a]$"', 'r"^[a]"'),
        ],
        "from pakiet.modul import czy_a\n\n\n"
        "def test_a_pasuje():\n"
        "    assert czy_a('a')\n",
        "from pakiet.modul import czy_a\n\n\n"
        "def test_a_pasuje_a_obce_otoczenie_nie():\n"
        "    assert czy_a('a')\n"
        "    assert not czy_a('xa')\n"
        "    assert not czy_a('ax')\n",
    ),
    _przypadek(
        "await_w_finally",
        "kolejnosc",
        ZRODLO_AWAIT_FINALLY,
        "await klient.anuluj()",
        [
            (
                "await klient.anuluj()\n        await klient.stop()",
                "await klient.stop()\n        await klient.anuluj()",
            ),
            ("await klient.anuluj()", "pass"),
            ("await klient.stop()", "pass"),
        ],
        "import asyncio\nfrom pakiet.modul import zakoncz\n\n" + KLIENT + "\n\n"
        "def test_zakoncz_zwraca_dane_i_wykonuje_oba_kroki():\n"
        "    klient = Klient()\n"
        "    assert asyncio.run(zakoncz(klient)) == 'dane'\n"
        "    assert sorted(klient.zapisy) == ['anuluj', 'stop']\n",
        "import asyncio\nfrom pakiet.modul import zakoncz\n\n" + KLIENT + "\n\n"
        "def test_anulowanie_przed_zatrzymaniem():\n"
        "    klient = Klient()\n"
        "    assert asyncio.run(zakoncz(klient)) == 'dane'\n"
        "    assert klient.zapisy == ['anuluj', 'stop']\n",
        liczby={"kolejnosc": 1, "instrukcja": 2},
    ),
]
IDY =[p.nazwa for p in PRZYPADKI]


def _linia(zrodlo: str, fragment: str) -> int:
    """Numer (od 1) pierwszej linii zawierającej fragment."""
    for numer, tekst in enumerate(zrodlo.splitlines(), start=1):
        if fragment in tekst:
            return numer
    raise AssertionError(f"brak fragmentu {fragment!r} w źródle")


def _zastosuj(zrodlo: str, szukaj: str, zamien: str) -> str:
    assert zrodlo.count(szukaj) == 1, f"{szukaj!r} musi wystąpić dokładnie raz"
    return zrodlo.replace(szukaj, zamien, 1)


def _drzewo(zrodlo: str) -> str:
    return ast.dump(ast.parse(zrodlo))


def _oczekiwane_drzewa(przypadek: Przypadek) -> list[str]:
    return sorted(_drzewo(_zastosuj(przypadek.zrodlo, s, z)) for s, z in przypadek.mutanty)


def _wszystkie_linie(zrodlo: str) -> set[int]:
    return set(range(1, len(zrodlo.splitlines()) + 1))


# --- zamknięte listy umowy ----------------------------------------------------------------------


def test_ksztalty_to_zamknieta_lista_szesciu_nazw():
    assert mutanty_uzupelniajace.KSZTALTY == (
        "except",
        "instrukcja",
        "kolejnosc",
        "kolekcja",
        "wywolanie",
        "regex",
    )


def test_usuwalne_wywolania_to_tylko_sorted_i_strip():
    assert set(mutanty_uzupelniajace.WYWOLANIA_USUWALNE) == {"sorted", "strip"}


def test_kazdy_ksztalt_ma_przypadek_w_testach():
    assert {p.ksztalt for p in PRZYPADKI} == set(mutanty_uzupelniajace.KSZTALTY)


# --- generator mutantów (czysta funkcja) -----------------------------------------------------


@pytest.mark.parametrize("przypadek", PRZYPADKI, ids=IDY)
def test_generator_daje_dokladnie_oczekiwane_mutanty_kazdego_ksztaltu(przypadek):
    mutanty = mutanty_uzupelniajace.generuj_mutanty(
        SCIEZKA, przypadek.zrodlo, _wszystkie_linie(przypadek.zrodlo)
    )
    assert Counter(m.ksztalt for m in mutanty) == przypadek.liczby
    assert sorted(_drzewo(m.zrodlo) for m in mutanty) == _oczekiwane_drzewa(przypadek)


@pytest.mark.parametrize("przypadek", PRZYPADKI, ids=IDY)
def test_generator_wskazuje_linie_zmutowanego_elementu(przypadek):
    mutanty = mutanty_uzupelniajace.generuj_mutanty(
        SCIEZKA, przypadek.zrodlo, _wszystkie_linie(przypadek.zrodlo)
    )
    oczekiwana = _linia(przypadek.zrodlo, przypadek.fragment)
    wlasciwe = [m for m in mutanty if m.ksztalt == przypadek.ksztalt]
    assert wlasciwe
    assert {m.linia for m in wlasciwe} == {oczekiwana}


@pytest.mark.parametrize("przypadek", PRZYPADKI, ids=IDY)
def test_mutant_niesie_plik_ksztalt_z_listy_i_opis(przypadek):
    mutanty = mutanty_uzupelniajace.generuj_mutanty(
        SCIEZKA, przypadek.zrodlo, _wszystkie_linie(przypadek.zrodlo)
    )
    assert mutanty
    for mutant in mutanty:
        assert mutant.plik == SCIEZKA
        assert mutant.ksztalt in mutanty_uzupelniajace.KSZTALTY
        assert mutant.opis.strip()
        assert mutant.zrodlo != przypadek.zrodlo


def test_instrukcje_usuniete_z_pary_wywolan_sa_na_swoich_liniach_a_zamiana_na_pierwszej():
    zrodlo = ZRODLO_KOLEJNOSC
    mutanty = mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, zrodlo, _wszystkie_linie(zrodlo))
    linie = {(m.ksztalt, m.linia) for m in mutanty}
    assert linie == {("kolejnosc", 2), ("instrukcja", 2), ("instrukcja", 3)}


KOTWICA_WZOR = r"\[(?P<plik>[^\[\]:]+):(?P<linia>[0-9]+)\]"
ZRODLO_KOTWICA = f'import re\n\nKOTWICA = re.compile(r"{KOTWICA_WZOR}")\n'


def test_kotwica_jak_w_har_176_ma_trzy_mutanty_po_jednym_na_znak_klasy_wykluczonych():
    # `\[`, `\]` i `:` to po jednym elemencie klasy; `[0-9]` to jeden element (jego usuniecie
    # daje pusta klasa, czyli mutant niekompilowalny, pomijany); `^` negacji nie jest elementem.
    mutanty = mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, ZRODLO_KOTWICA, {3})
    klasa = r"[^\[\]:]"
    oczekiwane = sorted(
        _drzewo(ZRODLO_KOTWICA.replace(klasa, nowa, 1))
        for nowa in (r"[^\]:]", r"[^\[:]", r"[^\[\]]")
    )
    assert {m.ksztalt for m in mutanty} == {"regex"}
    assert {m.linia for m in mutanty} == {3}
    assert sorted(_drzewo(m.zrodlo) for m in mutanty) == oczekiwane


def test_mutant_regex_z_wzorcem_ktory_sie_nie_kompiluje_jest_pominiety():
    zrodlo = ZRODLO_PUSTA_KLASA
    mutanty = mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, zrodlo, _wszystkie_linie(zrodlo))
    assert len(mutanty) == 2
    assert all("[]" not in m.zrodlo for m in mutanty)


ZRODLO_ZASIEGU = """\
# stany
STANY_KONCOWE = {"zakonczony", "przerwany", "odrzucony"}


def czy_koniec(stan):
    return stan in STANY_KONCOWE


def kolejne(kroki):
    return sorted(kroki)


def poczatek():
    return 0


def wczytaj(tekst):
    try:
        return int(tekst)
    except (ValueError, TypeError):
        return None


class Czytnik:
    def wczytaj(self, tekst):
        try:
            return int(tekst)
        except (ValueError, TypeError):
            return None
"""


def _ksztalty_i_linie(linie: set[int]) -> Counter:
    mutanty = mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, ZRODLO_ZASIEGU, linie)
    return Counter((m.ksztalt, m.linia) for m in mutanty)


def test_zmieniona_stala_modulu_wystarczy_do_mutantow_jej_elementow():
    assert _ksztalty_i_linie({2}) == Counter({("kolekcja", 2): 3})


def test_zmiana_w_innej_linii_funkcji_niz_mutowany_element_daje_mutanty_calej_funkcji():
    naglowek = _linia(ZRODLO_ZASIEGU, "def kolejne")
    sorted_ = _linia(ZRODLO_ZASIEGU, "sorted(kroki)")
    assert naglowek != sorted_
    assert _ksztalty_i_linie({naglowek}) == Counter({("wywolanie", sorted_): 1})


def test_zmiana_w_funkcji_bez_kształtow_nie_daje_mutantow_sasiednich_funkcji():
    assert _ksztalty_i_linie({_linia(ZRODLO_ZASIEGU, "return 0")}) == Counter()


def test_zmiana_wewnatrz_funkcji_z_except_daje_mutanty_calej_funkcji():
    linia_except = _linia(ZRODLO_ZASIEGU, "except (ValueError, TypeError):")
    assert _ksztalty_i_linie({linia_except - 2}) == Counter({("except", linia_except): 2})


def test_zmiana_w_metodzie_klasy_daje_mutanty_tylko_tej_metody():
    pierwsze = _linia(ZRODLO_ZASIEGU, "except (ValueError, TypeError):")
    drugie = pierwsze + 8
    assert ZRODLO_ZASIEGU.splitlines()[drugie - 1].strip() == "except (ValueError, TypeError):"
    assert _ksztalty_i_linie({drugie - 2}) == Counter({("except", drugie): 2})


def test_brak_zmienionych_linii_nie_daje_mutantow():
    assert _ksztalty_i_linie(set()) == Counter()


def test_niesparsowalny_plik_to_blad_narzedzia():
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia):
        mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, "def f(:\n", {1})


# --- mini-repozytorium i uruchomienie narzędzia -------------------------------------------------


def _git(katalog: Path, *argv: str) -> str:
    polecenie = [
        "git",
        "-c", "core.hooksPath=/dev/null",
        "-c", "commit.gpgsign=false",
        "-c", "user.name=LukBro",
        "-c", "user.email=lukaszbrowar@gmail.com",
        *argv,
    ]  # fmt: skip
    proces = uruchom_proces_sync(
        polecenie, cwd=katalog, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        wyjscie, blad = proces.communicate(timeout=30)
    finally:
        zakoncz_proces_sync(proces, nazwa="git w mini-repozytorium")
    assert proces.returncode == 0, blad
    return wyjscie


PYPROJECT_MINI = '[tool.mutmut]\nsource_paths = ["src/"]\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'


def _zapisz(repo: Path, pliki: dict[str, str]) -> None:
    for sciezka, tresc in pliki.items():
        cel = repo / sciezka
        cel.parent.mkdir(parents=True, exist_ok=True)
        cel.write_text(tresc, encoding="utf-8")


def _repo(
    tmp_path: Path,
    baza: dict[str, str],
    robocze: dict[str, str] | None = None,
    tabela: str | None = None,
) -> Path:
    """Mini-repozytorium: commit `baza`, potem niezacommitowane `robocze` (i tabela w working tree)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _zapisz(repo, {"pyproject.toml": PYPROJECT_MINI, ".gitignore": "__pycache__/\n", **baza})
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "start")
    _zapisz(repo, robocze or {})
    if tabela is not None:
        _zapisz(repo, {"tests/mutanty_reczne/HAR-999.toml": tabela})
    return repo


def _standardowy(
    tmp_path: Path, zrodlo: str, test: str, tabela: str | None = None
) -> Path:
    baza = {
        "src/pakiet/__init__.py": "",
        SCIEZKA: "# modul\n",
        TEST_MODULU: test,
    }
    return _repo(tmp_path, baza, {SCIEZKA: zrodlo}, tabela)


def _migawka(repo: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(repo)): p.read_bytes()
        for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    }


def _narzedzie(repo: Path, capsys, *argv: str) -> tuple[int, str, str]:
    kod = mutanty_uzupelniajace.main(
        ["--korzen", str(repo), "--baza", "HEAD", "--limit", LIMIT_TESTU_S, *argv]
    )
    wyjscie = capsys.readouterr()
    return kod, wyjscie.out, wyjscie.err


def _wyniki(stdout: str) -> list[tuple[str, int, str]]:
    wynik = []
    for linia in stdout.splitlines():
        dopasowanie = LINIA_WYNIKU.match(linia)
        assert dopasowanie, f"linia stdout nie jest wynikiem plik:linia: ksztalt: opis: {linia!r}"
        assert dopasowanie["ksztalt"] in mutanty_uzupelniajace.KSZTALTY
        wynik.append((dopasowanie["plik"], int(dopasowanie["linia"]), dopasowanie["ksztalt"]))
    return wynik


# --- kod 1 i kod 0 dla każdego kształtu ------------------------------------------------------


@pytest.mark.parametrize("przypadek", PRZYPADKI, ids=IDY)
def test_element_bez_testu_ochronnego_daje_kod_1_i_miejsce_w_pliku(tmp_path, capsys, przypadek):
    repo = _standardowy(tmp_path, przypadek.zrodlo, przypadek.slaby_test)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 1
    oczekiwane = (SCIEZKA, _linia(przypadek.zrodlo, przypadek.fragment), przypadek.ksztalt)
    assert oczekiwane in _wyniki(wyjscie)


@pytest.mark.parametrize("przypadek", PRZYPADKI, ids=IDY)
def test_ten_sam_element_z_testem_ochronnym_daje_kod_0_i_liczbe_zbadanych_mutantow(
    tmp_path, capsys, przypadek
):
    repo = _standardowy(tmp_path, przypadek.zrodlo, przypadek.mocny_test)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 0
    assert wyjscie.splitlines() == [f"zbadano {sum(przypadek.liczby.values())}"]


def test_narzedzie_nie_zmienia_plikow_repozytorium(tmp_path, capsys):
    przypadek = PRZYPADKI[0]
    repo = _standardowy(tmp_path, przypadek.zrodlo, przypadek.slaby_test)
    przed = _migawka(repo)
    kod, _, _ = _narzedzie(repo, capsys)
    assert kod == 1
    assert _migawka(repo) == przed


def test_nowy_niesledzony_plik_modulu_bez_testu_ochronnego_jest_zglaszany(tmp_path, capsys):
    przypadek = PRZYPADKI[0]
    baza = {
        "src/pakiet/__init__.py": "",
        "tests/test_nowy.py": przypadek.slaby_test.replace("pakiet.modul", "pakiet.nowy"),
    }
    repo = _repo(tmp_path, baza, {"src/pakiet/nowy.py": przypadek.zrodlo})
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 1
    linia = _linia(przypadek.zrodlo, przypadek.fragment)
    assert ("src/pakiet/nowy.py", linia, "except") in _wyniki(wyjscie)


ZRODLO_DWA_MIEJSCA = """\
# stany
STANY_KONCOWE = {"zakonczony", "przerwany", "odrzucony"}


def czy_koniec(stan):
    return stan in STANY_KONCOWE


def poczatek():
    return 0


def wczytaj(tekst):
    try:
        return int(tekst)
    except (ValueError, TypeError):
        return None
"""

TEST_DWA_MIEJSCA_SLABY = (
    "from pakiet.modul import czy_koniec, wczytaj\n\n\n"
    "def test_stany_zakonczony_i_przerwany_sa_koncowe_a_trwa_nie():\n"
    "    assert czy_koniec('zakonczony')\n"
    "    assert czy_koniec('przerwany')\n"
    "    assert not czy_koniec('trwa')\n\n\n"
    "def test_tekst_nie_liczba_daje_brak():\n"
    "    assert wczytaj('abc') is None\n"
)
LINIA_STALEJ = _linia(ZRODLO_DWA_MIEJSCA, "STANY_KONCOWE = {")
LINIA_EXCEPT = _linia(ZRODLO_DWA_MIEJSCA, "except (ValueError, TypeError):")


def test_zgloszenia_sa_posortowane_po_pliku_i_liczbowo_po_linii(tmp_path, capsys):
    assert LINIA_STALEJ < 10 <= LINIA_EXCEPT  # "2" < "13" liczbowo, ale "13" < "2" jako napis
    repo = _standardowy(tmp_path, ZRODLO_DWA_MIEJSCA, TEST_DWA_MIEJSCA_SLABY)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 1
    wyniki = _wyniki(wyjscie)
    assert {(p, n, k) for p, n, k in wyniki} == {
        (SCIEZKA, LINIA_STALEJ, "kolekcja"),
        (SCIEZKA, LINIA_EXCEPT, "except"),
    }
    assert [(p, n) for p, n, _ in wyniki] == sorted((p, n) for p, n, _ in wyniki)


def test_pusty_diff_daje_kod_0_i_brak_zmienionych_funkcji(tmp_path, capsys):
    repo = _repo(
        tmp_path,
        {"src/pakiet/__init__.py": "", SCIEZKA: ZRODLO_EXCEPT, TEST_MODULU: "# nic\n"},
    )
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 0
    assert wyjscie.splitlines() == ["brak zmienionych funkcji"]


# --- kod 2: brak dowodu to błąd narzędzia, nigdy zielone ----------------------------------------

TEST_BEZ_MODULU = "def test_cokolwiek():\n    assert True\n"


def test_zero_zebranych_testow_dla_modulu_to_kod_2_a_nie_czysto(tmp_path, capsys):
    baza = {"src/pakiet/__init__.py": "", SCIEZKA: "# modul\n", "tests/test_inne.py": TEST_BEZ_MODULU}
    repo = _repo(tmp_path, baza, {SCIEZKA: ZRODLO_EXCEPT})
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert "pakiet.modul" in blad
    assert wyjscie == ""


def test_test_czerwony_bez_mutacji_to_kod_2_a_nie_zabity_mutant(tmp_path, capsys):
    czerwony = (
        "from pakiet.modul import wczytaj\n\n\n"
        "def test_zawsze_czerwony():\n"
        "    assert False\n"
    )
    repo = _standardowy(tmp_path, ZRODLO_EXCEPT, czerwony)
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert TEST_MODULU in blad
    assert wyjscie == ""


def test_nieznana_baza_to_kod_2(tmp_path, capsys):
    przypadek = PRZYPADKI[0]
    repo = _standardowy(tmp_path, przypadek.zrodlo, przypadek.slaby_test)
    kod, wyjscie, blad = _narzedzie(repo, capsys, "--baza", "nie-ma-takiej-bazy")
    assert kod == 2
    assert "nieznana baza 'nie-ma-takiej-bazy'" in blad
    assert "git diff nie powiodl sie" not in blad
    assert wyjscie == ""


def test_plik_modulu_z_bledem_skladni_to_kod_2(tmp_path, capsys):
    repo = _standardowy(tmp_path, "def f(:\n", TEST_BEZ_MODULU)
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert blad != ""
    assert wyjscie == ""


def test_zepsuta_tabela_rownowaznych_to_kod_2_a_nie_ciche_pominiecie(tmp_path, capsys):
    przypadek = PRZYPADKI[0]
    repo = _standardowy(
        tmp_path, przypadek.zrodlo, przypadek.slaby_test, tabela="[[mutant\nzepsute = \n"
    )
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert "HAR-999.toml" in blad
    assert wyjscie == ""


# --- [[rownowazny]] -------------------------------------------------------------------------


UZASADNIENIE_OGOLNE = "zadne wejscie nie odroznia wersji, bo test"
OPIS_VALUE = "usunieto element ValueError z krotki except"
OPIS_TYPE = "usunieto element TypeError z krotki except"
TEKST_EXCEPT = "except (ValueError, TypeError):"


def _uzasadnienie(ksztalt: str | None, *opisy: str) -> str:
    """Uzasadnienie wpisu: `kształt: <nazwa>` (gdy podano) i doslowne opisy mutantow."""
    czesci = [f"kształt: {ksztalt}"] if ksztalt else []
    return "; ".join([*czesci, *opisy, UZASADNIENIE_OGOLNE])


def _tabela(*rownowazne: tuple[str, str, str]) -> str:
    """Tabela z jednym zapchajdziurowym `[[mutant]]` (wczytywacz wymaga niepustej listy)."""
    tekst = (
        "[[mutant]]\n"
        f"plik = {json.dumps(SCIEZKA)}\n"
        'szukaj = "a"\n'
        'zamien = "b"\n'
        f"test = {json.dumps(TEST_MODULU + '::test_cokolwiek')}\n"
    )
    for plik, linia_tekst, uzasadnienie in rownowazne:
        tekst += (
            "\n[[rownowazny]]\n"
            f"plik = {json.dumps(plik)}\n"
            f"linia_tekst = {json.dumps(linia_tekst)}\n"
            f"uzasadnienie = {json.dumps(uzasadnienie)}\n"
        )
    return tekst


def _except_z_tabela(tmp_path: Path, *wpisy: tuple[str, str, str]) -> Path:
    przypadek = PRZYPADKI[0]
    return _standardowy(tmp_path, przypadek.zrodlo, przypadek.slaby_test, _tabela(*wpisy))


WPIS_OBA_ELEMENTY = (SCIEZKA, TEKST_EXCEPT, _uzasadnienie("except", OPIS_VALUE, OPIS_TYPE))


def test_mutant_z_wpisem_rownowaznym_ksztaltu_i_opisu_nie_jest_zglaszany(tmp_path, capsys):
    repo = _except_z_tabela(tmp_path, WPIS_OBA_ELEMENTY)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 0
    assert wyjscie.splitlines() == ["zbadano 2"]


def test_wpis_rownowazny_dopasowuje_linie_po_obcieciu_bialych_znakow(tmp_path, capsys):
    wpis = (SCIEZKA, f"    {TEKST_EXCEPT}  ", _uzasadnienie("except", OPIS_VALUE, OPIS_TYPE))
    repo = _except_z_tabela(tmp_path, wpis)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 0
    assert wyjscie.splitlines() == ["zbadano 2"]


@pytest.mark.parametrize(
    ("plik", "linia_tekst"),
    [
        pytest.param("src/pakiet/inny.py", TEKST_EXCEPT, id="ten_sam_tekst_w_innym_pliku"),
        pytest.param(SCIEZKA, "except (ValueError,):", id="inny_tekst_linii_w_tym_samym_pliku"),
        pytest.param(SCIEZKA, "except", id="tylko_poczatek_linii"),
    ],
)
def test_mutant_bez_pasujacego_wpisu_rownowaznego_jest_zglaszany(
    tmp_path, capsys, plik, linia_tekst
):
    wpis = (plik, linia_tekst, _uzasadnienie("except", OPIS_VALUE, OPIS_TYPE))
    repo = _except_z_tabela(tmp_path, wpis)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 1
    linia = _linia(PRZYPADKI[0].zrodlo, PRZYPADKI[0].fragment)
    assert [(p, n, k) for p, n, k in _wyniki(wyjscie)] == [(SCIEZKA, linia, "except")] * 2


def test_wpis_rownowazny_wycisza_tylko_swoja_linie_a_pozostale_zgloszenia_zostaja(
    tmp_path, capsys
):
    tabela = _tabela(WPIS_OBA_ELEMENTY)
    repo = _standardowy(tmp_path, ZRODLO_DWA_MIEJSCA, TEST_DWA_MIEJSCA_SLABY, tabela)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 1
    assert [(p, n, k) for p, n, k in _wyniki(wyjscie)] == [(SCIEZKA, LINIA_STALEJ, "kolekcja")]


def test_wpis_z_opisem_jednego_elementu_except_wycisza_tylko_ten_element(tmp_path, capsys):
    wpis = (SCIEZKA, TEKST_EXCEPT, _uzasadnienie("except", OPIS_VALUE))
    repo = _except_z_tabela(tmp_path, wpis)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    linia = _linia(PRZYPADKI[0].zrodlo, PRZYPADKI[0].fragment)
    assert kod == 1
    assert wyjscie.splitlines() == [f"{SCIEZKA}:{linia}: except: {OPIS_TYPE}"]


def test_dwa_wpisy_po_jednym_na_element_except_wyciszaja_oba(tmp_path, capsys):
    wpisy = [
        (SCIEZKA, TEKST_EXCEPT, _uzasadnienie("except", OPIS_VALUE)),
        (SCIEZKA, TEKST_EXCEPT, _uzasadnienie("except", OPIS_TYPE)),
    ]
    repo = _except_z_tabela(tmp_path, *wpisy)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 0
    assert wyjscie.splitlines() == ["zbadano 2"]


@pytest.mark.parametrize(
    "uzasadnienie",
    [
        pytest.param(UZASADNIENIE_OGOLNE, id="sama_linia_bez_ksztaltu_i_opisu"),
        pytest.param(_uzasadnienie("except"), id="ksztalt_bez_opisu_elementu"),
        pytest.param(_uzasadnienie(None, OPIS_VALUE, OPIS_TYPE), id="opis_bez_ksztaltu"),
        pytest.param(_uzasadnienie("kolekcja", OPIS_VALUE, OPIS_TYPE), id="ksztalt_innego_mutanta"),
        pytest.param(
            f"ksztalt: except; {OPIS_VALUE}; {OPIS_TYPE}", id="znacznik_bez_polskiego_l"
        ),
        pytest.param(
            f"kształt:except; {OPIS_VALUE}; {OPIS_TYPE}", id="znacznik_bez_spacji_po_dwukropku"
        ),
        pytest.param(
            f"kształt: Except; {OPIS_VALUE}; {OPIS_TYPE}", id="znacznik_innej_wielkosci_liter"
        ),
    ],
)
def test_wpis_bez_ksztaltu_i_opisu_elementu_nie_wycisza_mutantow_except(
    tmp_path, capsys, uzasadnienie
):
    repo = _except_z_tabela(tmp_path, (SCIEZKA, TEKST_EXCEPT, uzasadnienie))
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    linia = _linia(PRZYPADKI[0].zrodlo, PRZYPADKI[0].fragment)
    assert kod == 1
    assert wyjscie.splitlines() == [
        f"{SCIEZKA}:{linia}: except: {OPIS_TYPE}",
        f"{SCIEZKA}:{linia}: except: {OPIS_VALUE}",
    ]


def test_wpis_z_opisem_innego_elementu_nie_wycisza_tego_elementu(tmp_path, capsys):
    opis_obcy = "usunieto element OSError z krotki except"
    repo = _except_z_tabela(tmp_path, (SCIEZKA, TEKST_EXCEPT, _uzasadnienie("except", opis_obcy)))
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 1
    assert len(wyjscie.splitlines()) == 2


def test_wpis_ksztaltu_regex_wycisza_wszystkie_mutanty_regex_w_linii_bez_opisu(tmp_path, capsys):
    przypadek = next(p for p in PRZYPADKI if p.nazwa == "klasa_znakow_i_kotwice")
    wpis = (SCIEZKA, 'LITERY = re.compile(r"^[abc]$")', _uzasadnienie("regex"))
    repo = _standardowy(tmp_path, przypadek.zrodlo, przypadek.slaby_test, _tabela(wpis))
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 0
    assert wyjscie.splitlines() == ["zbadano 5"]


def test_wpis_ksztaltu_kolekcja_nie_wycisza_mutantow_regex_tej_samej_linii(tmp_path, capsys):
    przypadek = next(p for p in PRZYPADKI if p.nazwa == "klasa_znakow_i_kotwice")
    wpis = (SCIEZKA, 'LITERY = re.compile(r"^[abc]$")', _uzasadnienie("kolekcja"))
    repo = _standardowy(tmp_path, przypadek.zrodlo, przypadek.slaby_test, _tabela(wpis))
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 1
    assert [k for _, _, k in _wyniki(wyjscie)] == ["regex"] * 5


ZRODLO_MIESZANE = """\
def kolejne(kroki):
    return list(sorted(kroki)) + list(("a", "b"))
"""
TEST_MIESZANY_SLABY = (
    "from pakiet.modul import kolejne\n\n\n"
    "def test_pierwszy_element_zostaje_pierwszy():\n"
    "    assert kolejne([1])[0] == 1\n"
)
LINIA_MIESZANA = 'return list(sorted(kroki)) + list(("a", "b"))'


def _mieszane(tmp_path: Path, *ksztalty: str) -> Path:
    wpisy = [(SCIEZKA, LINIA_MIESZANA, _uzasadnienie(k)) for k in ksztalty]
    return _standardowy(tmp_path, ZRODLO_MIESZANE, TEST_MIESZANY_SLABY, _tabela(*wpisy))


def test_wpis_jednego_ksztaltu_nie_wycisza_innego_ksztaltu_w_tej_samej_linii(tmp_path, capsys):
    kod, wyjscie, _ = _narzedzie(_mieszane(tmp_path, "wywolanie"), capsys)
    assert kod == 1
    assert [(n, k) for _, n, k in _wyniki(wyjscie)] == [(2, "kolekcja")] * 2


def test_wpis_ksztaltu_kolekcja_zostawia_zgloszone_wywolanie_w_tej_samej_linii(tmp_path, capsys):
    kod, wyjscie, _ = _narzedzie(_mieszane(tmp_path, "kolekcja"), capsys)
    assert kod == 1
    assert [(n, k) for _, n, k in _wyniki(wyjscie)] == [(2, "wywolanie")]


def test_wpisy_obu_ksztaltow_wyciszaja_cala_linie_a_wyciszone_licza_sie_do_zbadano(
    tmp_path, capsys
):
    kod, wyjscie, _ = _narzedzie(_mieszane(tmp_path, "wywolanie", "kolekcja"), capsys)
    assert kod == 0
    assert wyjscie.splitlines() == ["zbadano 3"]


# --- AC4: diff HAR-176 (kotwica regex na poziomie modułu) ---------------------------------------

SCIEZKA_KOTWIC = "src/runner/kotwice.py"
TEST_KOTWIC = "tests/test_kotwice.py"
ZRODLO_KOTWIC_BAZA = """\
\"\"\"Kotwice w raporcie.\"\"\"
import re

NAGLOWEK = "## Ryzyko"
# kotwica na poczatku pozycji: [plik:linia]
KOTWICA = re.compile(r"\\[(?P<plik>[^:]+):(?P<linia>[0-9]+)\\]")


def zbierz(tekst):
    trafienie = KOTWICA.search(tekst)
    if trafienie is None:
        return None
    return trafienie.group("plik"), trafienie.group("linia")
"""
ZRODLO_KOTWIC_NOWE = ZRODLO_KOTWIC_BAZA.replace(
    r'r"\[(?P<plik>[^:]+):(?P<linia>[0-9]+)\]"', f'r"{KOTWICA_WZOR}"'
)
TEST_KOTWIC_SLABY = (
    "from runner.kotwice import zbierz\n\n\n"
    "def test_kotwica_wskazuje_plik_i_linie():\n"
    "    assert zbierz('[a.py:3]') == ('a.py', '3')\n"
)
TEST_KOTWIC_MOCNY = TEST_KOTWIC_SLABY + (
    "\n\ndef test_nawias_otwierajacy_w_nazwie_pliku_zaczyna_kotwice_od_ostatniego():\n"
    "    assert zbierz('[a[b.py:3]') == ('b.py', '3')\n"
    "\n\ndef test_nawias_zamykajacy_w_nazwie_pliku_odrzuca_kotwice():\n"
    "    assert zbierz('[a]b.py:3]') is None\n"
    "\n\ndef test_dwukropek_w_nazwie_pliku_odrzuca_kotwice():\n"
    "    assert zbierz('[a:b:3]') is None\n"
)


def _repo_kotwic(tmp_path: Path, test: str) -> Path:
    baza = {
        "src/runner/__init__.py": "",
        SCIEZKA_KOTWIC: ZRODLO_KOTWIC_BAZA,
        TEST_KOTWIC: test,
    }
    return _repo(tmp_path, baza, {SCIEZKA_KOTWIC: ZRODLO_KOTWIC_NOWE})


def test_zmiana_tylko_stalej_kotwica_w_pliku_z_funkcja_zmienia_dokladnie_jedna_linie():
    # warunek AC4: o zasiegu decyduje WYLACZNIE to, ze stale modulu sa w zakresie
    zmienione = [
        n
        for n, (a, b) in enumerate(
            zip(ZRODLO_KOTWIC_BAZA.splitlines(), ZRODLO_KOTWIC_NOWE.splitlines(), strict=True),
            start=1,
        )
        if a != b
    ]
    assert zmienione == [_linia(ZRODLO_KOTWIC_NOWE, "KOTWICA = re.compile")]


def test_zmieniona_kotwica_regex_bez_testu_na_znak_klasy_jest_zgloszona_w_linii_stalej(
    tmp_path, capsys
):
    repo = _repo_kotwic(tmp_path, TEST_KOTWIC_SLABY)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    linia = _linia(ZRODLO_KOTWIC_NOWE, "KOTWICA = re.compile")
    assert kod == 1
    assert _wyniki(wyjscie) == [(SCIEZKA_KOTWIC, linia, "regex")] * 3


def test_zmieniona_kotwica_regex_z_testami_na_kazdy_znak_klasy_daje_kod_0(tmp_path, capsys):
    repo = _repo_kotwic(tmp_path, TEST_KOTWIC_MOCNY)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 0
    assert wyjscie.splitlines() == ["zbadano 3"]


# --- HAR-241, uzupelnienie: dowod wykonania testow, limit, git, zasieg funkcji ----------------------
#
# Brak dowodu to blad narzedzia (kod 2), nigdy "zabity" ani "czysto": testy tylko pominiete,
# brak raportu JUnit, kod pytesta inny niz 0/1 po mutacji.


def _raport_junit(katalog: Path, *zestawy: tuple[int, int]) -> Path:
    """Raport JUnit z zestawami (liczba testow, liczba pominietych)."""
    tresc = "".join(f'<testsuite tests="{t}" skipped="{s}"/>' for t, s in zestawy)
    sciezka = katalog / "raport.xml"
    sciezka.write_text(f"<testsuites>{tresc}</testsuites>", encoding="utf-8")
    return sciezka


@pytest.mark.parametrize(
    "zestawy",
    [
        pytest.param(((2, 2),), id="wszystkie_pominiete"),
        pytest.param(((0, 0),), id="zero_testow"),
        pytest.param(((3, 3), (1, 1)), id="pominiete_w_kilku_zestawach"),
    ],
)
def test_raport_w_ktorym_zaden_test_sie_nie_wykonal_to_blad_narzedzia_a_nie_dowod(tmp_path, zestawy):
    raport = _raport_junit(tmp_path, *zestawy)
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia, match="zaden test sie nie wykonal"):
        mutanty_uzupelniajace._wymagaj_wykonanych(raport, [TEST_MODULU])


@pytest.mark.parametrize(
    "zestawy",
    [
        pytest.param(((3, 1),), id="czesc_pominieta"),
        pytest.param(((2, 2), (1, 0)), id="wykonany_test_w_drugim_zestawie"),
    ],
)
def test_raport_z_wykonanym_testem_jest_dowodem(tmp_path, zestawy):
    raport = _raport_junit(tmp_path, *zestawy)
    mutanty_uzupelniajace._wymagaj_wykonanych(raport, [TEST_MODULU])


def test_brak_raportu_junit_to_blad_narzedzia(tmp_path):
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia, match="brak raportu JUnit"):
        mutanty_uzupelniajace._wymagaj_wykonanych(tmp_path / "nie-ma.xml", [TEST_MODULU])


def test_raport_junit_ktory_nie_jest_xml_to_blad_narzedzia(tmp_path):
    raport = tmp_path / "raport.xml"
    raport.write_text("to nie jest xml", encoding="utf-8")
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia, match="nie jest poprawnym XML"):
        mutanty_uzupelniajace._wymagaj_wykonanych(raport, [TEST_MODULU])


def test_raport_junit_z_licznikiem_nie_bedacym_liczba_to_blad_narzedzia(tmp_path):
    raport = tmp_path / "raport.xml"
    raport.write_text('<testsuites><testsuite tests="x" skipped="0"/></testsuites>', encoding="utf-8")
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia, match="nie bedace liczbami"):
        mutanty_uzupelniajace._wymagaj_wykonanych(raport, [TEST_MODULU])


@pytest.mark.parametrize("kod", [0, 1])
def test_kod_pytesta_0_i_1_bez_raportu_junit_to_blad_narzedzia(tmp_path, monkeypatch, kod):
    monkeypatch.setattr(mutanty_uzupelniajace.mutanty_reczne, "_uruchom_proces", lambda *a, **k: (kod, ""))
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia, match="brak raportu JUnit"):
        mutanty_uzupelniajace._uruchom_pytest(tmp_path, [TEST_MODULU], 5.0)


@pytest.mark.parametrize("kod", [2, 3, 5])
def test_inny_kod_pytesta_wraca_do_wolajacego_bez_czytania_raportu(tmp_path, monkeypatch, kod):
    monkeypatch.setattr(mutanty_uzupelniajace.mutanty_reczne, "_uruchom_proces", lambda *a, **k: (kod, ""))
    assert mutanty_uzupelniajace._uruchom_pytest(tmp_path, [TEST_MODULU], 5.0) == kod


TEST_POMINIETY = (
    "import pytest\n"
    "from pakiet.modul import wczytaj\n\n\n"
    '@pytest.mark.skip(reason="wylaczony")\n'
    "def test_tekst_nie_liczba_daje_brak():\n"
    '    assert wczytaj("abc") is None\n'
)


def test_testy_modulu_tylko_pominiete_to_kod_2_a_nie_zabity_ani_czysto(tmp_path, capsys):
    repo = _standardowy(tmp_path, ZRODLO_EXCEPT, TEST_POMINIETY)
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert "zaden test sie nie wykonal" in blad
    assert wyjscie == ""


ZRODLO_BLAD_IMPORTU = """\
STANY = {"a", "b"}
assert len(STANY) == 2


def czy(stan):
    return stan in STANY
"""
TEST_BLAD_IMPORTU = "from pakiet.modul import czy\n\n\ndef test_a_jest_stanem():\n    assert czy('a')\n"


def test_mutant_po_ktorym_modul_sie_nie_importuje_to_kod_2_a_nie_zabity_mutant(tmp_path, capsys):
    repo = _standardowy(tmp_path, ZRODLO_BLAD_IMPORTU, TEST_BLAD_IMPORTU)
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert "kodem 2" in blad
    assert wyjscie == ""


# --- limit czasu i wartosci domyslne CLI ----------------------------------------------------------


@pytest.mark.parametrize("tekst", ["abc", "", "0", "-1", "-0.5", "inf", "-inf", "nan"])
def test_limit_czasu_niebedacy_skonczona_liczba_dodatnia_jest_odrzucony(tekst):
    with pytest.raises(argparse.ArgumentTypeError):
        mutanty_uzupelniajace._limit(tekst)


def test_limit_czasu_dodatni_jest_przyjety():
    assert mutanty_uzupelniajace._limit("0.5") == 0.5
    assert mutanty_uzupelniajace._limit("120") == 120.0


def test_limit_zero_z_wiersza_polecen_konczy_program_kodem_2_argparse():
    with pytest.raises(SystemExit) as wyjscie:
        mutanty_uzupelniajace.main(["--limit", "0"])
    assert wyjscie.value.code == 2


def _zlap_przebieg(monkeypatch) -> list[tuple[Path, str, float]]:
    zlapane: list[tuple[Path, str, float]] = []

    def falszywy(korzen, baza, limit):
        zlapane.append((korzen, baza, limit))
        return 0, ["zbadano 0"]

    monkeypatch.setattr(mutanty_uzupelniajace, "_przebieg", falszywy)
    return zlapane


def test_bez_flag_baza_to_origin_main_a_limit_to_300_sekund(tmp_path, monkeypatch):
    zlapane = _zlap_przebieg(monkeypatch)
    assert mutanty_uzupelniajace.main(["--korzen", str(tmp_path)]) == 0
    assert zlapane == [(tmp_path, "origin/main", 300.0)]


def test_flagi_baza_i_limit_trafiaja_do_przebiegu(tmp_path, monkeypatch):
    zlapane = _zlap_przebieg(monkeypatch)
    assert mutanty_uzupelniajace.main(["--korzen", str(tmp_path), "--baza", "release-7", "--limit", "7.5"]) == 0
    assert zlapane == [(tmp_path, "release-7", 7.5)]


# --- blad narzedzia mutantow zmian (inny typ niz wlasny) ------------------------------------------


def test_nowy_plik_py_z_nieczytelna_trescia_to_kod_2_a_nie_pominiecie_pliku(tmp_path, capsys):
    repo = _repo(tmp_path, {"src/pakiet/__init__.py": ""})
    (repo / "src" / "pakiet" / "binarny.py").write_bytes(b"\xff\xfe\x00")
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert "binarny.py" in blad
    assert wyjscie == ""


# --- git uzytkownika nie zmienia wyniku bramki ----------------------------------------------------


@pytest.mark.parametrize(
    ("klucz", "wartosc"),
    [
        pytest.param("diff.noprefix", "true", id="diff_bez_prefiksow_sciezek"),
        pytest.param("color.diff", "always", id="diff_w_kolorze"),
        pytest.param("diff.external", "true", id="zewnetrzny_program_diffu"),
    ],
)
def test_ustawienie_gita_uzytkownika_nie_zmienia_wyniku_bramki(tmp_path, capsys, klucz, wartosc):
    przypadek = PRZYPADKI[0]
    repo = _standardowy(tmp_path, przypadek.zrodlo, przypadek.slaby_test)
    _git(repo, "config", klucz, wartosc)
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 1, blad
    linia = _linia(przypadek.zrodlo, przypadek.fragment)
    assert (SCIEZKA, linia, "except") in _wyniki(wyjscie)


# --- kolejnosc zgloszen i plikow --------------------------------------------------------------------

ZRODLO_BFS = """\
def zbierz(kroki, stan):
    if stan:
        pary = {"a": 1, "b": 2}
    return sorted(kroki)
"""
TEST_BFS_SLABY = (
    "from pakiet.modul import zbierz\n\n\n"
    "def test_jeden_krok_zostaje():\n"
    "    assert zbierz([1], False) == [1]\n"
)


def test_zgloszenia_jednej_funkcji_sa_rosnaco_po_linii_choc_generator_zaczyna_od_plytszych(tmp_path, capsys):
    # sorted(...) w linii 4 leży płycej w drzewie niż słownik w linii 3: generator daje go pierwszy.
    kolejnosc_generatora = [m.linia for m in mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, ZRODLO_BFS, {1})]
    assert kolejnosc_generatora != sorted(kolejnosc_generatora)
    repo = _standardowy(tmp_path, ZRODLO_BFS, TEST_BFS_SLABY)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 1
    linie = [n for _, n, _ in _wyniki(wyjscie)]
    assert linie == [3, 3, 4]


def test_pierwszy_blad_pliku_dotyczy_pliku_pierwszego_alfabetycznie_a_nie_w_kolejnosci_gita(tmp_path, capsys):
    # git daje najpierw pliki sledzone (z.py), potem nowe (a.py); tylko sortowanie ustawia a.py pierwszy.
    baza = {"src/pakiet/__init__.py": "", "src/pakiet/z.py": "# modul\n"}
    robocze = {"src/pakiet/z.py": "def f(:\n", "src/pakiet/a.py": "def g(:\n"}
    repo = _repo(tmp_path, baza, robocze)
    kod, _, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert "src/pakiet/a.py" in blad
    assert "src/pakiet/z.py" not in blad


# --- zasieg: async, dekoratory, klasy, literaly jednoelementowe ------------------------------------

ZRODLO_ASYNC = """\
async def zamknij(kanal):
    kanal.zamknij()
"""

ZRODLO_DEKORATOR = """\
import functools


@functools.cache
def kolejne(kroki):
    return sorted(kroki)
"""

ZRODLO_KLASA_DWIE_METODY = """\
class Dziennik:
    def pierwsza(self, d):
        d.dopisz("a")

    def druga(self, d):
        d.dopisz("b")
"""

ZRODLO_JEDEN_ELEMENT = """\
STANY = {"a"}
ROLE = ("a",)
MAPA = {"a": 1}


def czy(stan):
    try:
        return stan in STANY
    except (ValueError,):
        return False
"""


def test_funkcja_async_ma_mutanty_instrukcji_jak_zwykla_funkcja():
    mutanty = mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, ZRODLO_ASYNC, {1})
    assert Counter((m.ksztalt, m.linia) for m in mutanty) == Counter({("instrukcja", 2): 1})


def test_zmiana_tylko_w_dekoratorze_daje_mutanty_calej_funkcji():
    linia_dekoratora = _linia(ZRODLO_DEKORATOR, "@functools.cache")
    mutanty = mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, ZRODLO_DEKORATOR, {linia_dekoratora})
    assert Counter((m.ksztalt, m.linia) for m in mutanty) == Counter(
        {("wywolanie", _linia(ZRODLO_DEKORATOR, "sorted(kroki)")): 1}
    )


@pytest.mark.parametrize("zmieniona", [3, 6])
def test_zmiana_w_jednej_metodzie_klasy_nie_daje_mutantow_drugiej_a_instrukcje_metody_sa_mutowane(zmieniona):
    mutanty = mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, ZRODLO_KLASA_DWIE_METODY, {zmieniona})
    assert Counter((m.ksztalt, m.linia) for m in mutanty) == Counter({("instrukcja", zmieniona): 1})


def test_literaly_z_jednym_elementem_i_krotka_except_z_jednym_typem_nie_daja_mutantow():
    mutanty = mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, ZRODLO_JEDEN_ELEMENT, _wszystkie_linie(ZRODLO_JEDEN_ELEMENT))
    assert mutanty == []


# --- HAR-241 runda 2: `await`, opis, zasieg, cofniecie, deduplikacja -----------------------------


def _licz(zrodlo: str, linie: set[int]) -> Counter:
    mutanty = mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, zrodlo, linie)
    return Counter((m.ksztalt, m.linia) for m in mutanty)


def _wszystkie(zrodlo: str) -> Counter:
    return _licz(zrodlo, _wszystkie_linie(zrodlo))


def _drzewa_mutantow(zrodlo: str, linie: set[int] | None = None) -> list[str]:
    mutanty = mutanty_uzupelniajace.generuj_mutanty(
        SCIEZKA, zrodlo, linie if linie is not None else _wszystkie_linie(zrodlo)
    )
    return sorted(_drzewo(m.zrodlo) for m in mutanty)


def _drzewa(*zrodla: str) -> list[str]:
    return sorted(_drzewo(z) for z in zrodla)


@pytest.mark.parametrize(
    "zrodlo",
    [
        pytest.param("async def f(zadanie):\n    await zadanie\n", id="await_bez_wywolania"),
        pytest.param("async def f(g):\n    y = await g()\n    return y\n", id="await_w_przypisaniu"),
        pytest.param("async def f(g):\n    return await g()\n", id="await_w_return"),
    ],
)
def test_await_nie_bedacy_samodzielnym_wywolaniem_nie_daje_mutantow_instrukcji(zrodlo):
    assert _wszystkie(zrodlo) == Counter()


def test_await_wywolania_poza_finally_tez_jest_instrukcja_a_para_await_to_kolejnosc():
    zrodlo = "async def f(d):\n    await d.a()\n    await d.b()\n    d.c()\n"
    assert _wszystkie(zrodlo) == Counter(
        {("instrukcja", 2): 1, ("instrukcja", 3): 1, ("instrukcja", 4): 1,
         ("kolejnosc", 2): 1, ("kolejnosc", 3): 1}
    )  # fmt: skip


# --- opis mutanta niesie nazwe KAZDEGO usunietego elementu -------------------------------------


def test_opis_mutanta_except_nazywa_usuniety_element_nie_pierwszy_z_krotki():
    mutanty = mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, ZRODLO_EXCEPT, {1})
    assert {m.opis: _drzewo(m.zrodlo) for m in mutanty} == {
        OPIS_VALUE: _drzewo(_zastosuj(ZRODLO_EXCEPT, "(ValueError, TypeError)", "(TypeError,)")),
        OPIS_TYPE: _drzewo(_zastosuj(ZRODLO_EXCEPT, "(ValueError, TypeError)", "(ValueError,)")),
    }


ZRODLO_MAPA_ROZWINIECIE = """\
def polacz(a):
    return {
        **a,
        "k": 1,
    }
"""


def test_mapa_z_rozwinieciem_ma_mutant_na_rozwinieciu_i_na_parze_w_ich_liniach():
    mutanty = mutanty_uzupelniajace.generuj_mutanty(
        SCIEZKA, ZRODLO_MAPA_ROZWINIECIE, _wszystkie_linie(ZRODLO_MAPA_ROZWINIECIE)
    )
    po_opisie = {m.opis: (m.linia, _drzewo(m.zrodlo)) for m in mutanty}
    assert po_opisie == {
        "usunieto element **a z mapy": (
            3, _drzewo("def polacz(a):\n    return {'k': 1}\n")
        ),
        "usunieto element 'k': 1 z mapy": (
            4, _drzewo("def polacz(a):\n    return {**a}\n")
        ),
    }  # fmt: skip


def test_linie_mutantow_kolekcji_wieloliniowej_to_linie_usuwanych_elementow():
    zrodlo = 'STANY = {\n    "a",\n    "b",\n    "c",\n}\n'
    assert _wszystkie(zrodlo) == Counter({("kolekcja", 2): 1, ("kolekcja", 3): 1, ("kolekcja", 4): 1})


def test_linie_mutantow_except_wieloliniowej_krotki_to_linie_usuwanych_elementow():
    zrodlo = (
        "def f(t):\n    try:\n        return int(t)\n    except (\n"
        "        ValueError,\n        TypeError,\n    ):\n        return None\n"
    )
    assert _wszystkie(zrodlo) == Counter({("except", 5): 1, ("except", 6): 1})


# --- zasieg: granice funkcji, stale klas i wieloliniowe ------------------------------------------


def test_ostatnia_linia_funkcji_nalezy_do_niej_a_linia_za_nia_i_przed_def_nie():
    naglowek = _linia(ZRODLO_ZASIEGU, "def kolejne")
    ostatnia = _linia(ZRODLO_ZASIEGU, "sorted(kroki)")
    assert _ksztalty_i_linie({ostatnia}) == Counter({("wywolanie", ostatnia): 1})
    assert _ksztalty_i_linie({ostatnia + 1}) == Counter()
    assert _ksztalty_i_linie({naglowek - 1}) == Counter()
    assert _ksztalty_i_linie({1}) == Counter()


ZRODLO_STALA_W_KLASIE = """\
class Rola:
    ROLE = ("a", "b")

    def zapis(self, kroki):
        return sorted(kroki)
"""


def test_stala_w_ciele_klasy_ma_mutanty_swojej_instrukcji_a_metoda_swoje():
    assert _licz(ZRODLO_STALA_W_KLASIE, {2}) == Counter({("kolekcja", 2): 2})
    assert _licz(ZRODLO_STALA_W_KLASIE, {5}) == Counter({("wywolanie", 5): 1})


ZRODLO_STALA_WIELOLINIOWA = 'MAPA = {\n    "a": 1,\n    "b": 2,\n}\n'


@pytest.mark.parametrize("zmieniona", [1, 2, 3, 4])
def test_zmiana_w_dowolnej_linii_wieloliniowej_stalej_daje_mutanty_calej_stalej(zmieniona):
    assert _licz(ZRODLO_STALA_WIELOLINIOWA, {zmieniona}) == Counter(
        {("kolekcja", 2): 1, ("kolekcja", 3): 1}
    )


# --- zakres kazdego ksztaltu: jedno pole, jedna galaz ---------------------------------------------

ZRODLO_ORELSE = """\
def f(d, c, xs):
    if c:
        d.a()
    else:
        d.b()
    for x in xs:
        d.c()
    else:
        d.d()
"""


def test_wywolania_w_galezi_else_if_i_for_sa_instrukcjami_a_nieadjacentne_nie_maja_kolejnosci():
    assert _wszystkie(ZRODLO_ORELSE) == Counter(
        {("instrukcja", 3): 1, ("instrukcja", 5): 1, ("instrukcja", 7): 1, ("instrukcja", 9): 1}
    )


@pytest.mark.parametrize(
    "zrodlo",
    [
        pytest.param("def f(c, d):\n    return d.a() if c else d.b()\n", id="wyrazenie_warunkowe"),
        pytest.param("def f(d):\n    return lambda: d.a()\n", id="lambda"),
    ],
)
def test_wyrazenie_z_polem_orelse_albo_body_nie_bedace_lista_instrukcji_nie_daje_mutantow(zrodlo):
    assert _wszystkie(zrodlo) == Counter()


@pytest.mark.parametrize(
    "zrodlo",
    [
        pytest.param("def f(x):\n    a, b = x\n    return a\n", id="krotka_zapisu"),
        pytest.param("def f(x):\n    for a, b in x:\n        pass\n", id="krotka_celu_petli"),
        pytest.param("def f(x):\n    del (x, x)\n", id="krotka_usuwania"),
        pytest.param("def f(d):\n    return d[1, 2]\n", id="krotka_w_indeksie"),
        pytest.param("def f(x: dict[str, int]):\n    return x\n", id="krotka_w_adnotacji"),
    ],
)
def test_krotka_zapisu_usuwania_i_indeksu_nie_ma_mutantow_elementow(zrodlo):
    assert _wszystkie(zrodlo) == Counter()


@pytest.mark.parametrize(
    "zrodlo",
    [
        pytest.param("def f(x):\n    return x.strip('ab')\n", id="strip_z_argumentem"),
        pytest.param("def f(x):\n    return x.strip(chars='ab')\n", id="strip_z_argumentem_nazwanym"),
        pytest.param("def f(a, b):\n    return sorted(a, b)\n", id="sorted_z_dwoma_argumentami"),
        pytest.param("def f(a):\n    return sorted(*a)\n", id="sorted_z_rozwinieciem"),
        pytest.param("def f():\n    return sorted()\n", id="sorted_bez_argumentow"),
        pytest.param("def f(m, x):\n    return m.sorted(x)\n", id="sorted_jako_metoda"),
        pytest.param("def f(x):\n    return strip(x)\n", id="strip_jako_funkcja"),
        pytest.param("def f(a):\n    return reversed(a)\n", id="inna_funkcja_niz_sorted"),
        pytest.param("def f(x):\n    return x.lstrip() + x.rstrip()\n", id="lstrip_i_rstrip"),
        pytest.param("def f(x):\n    return x.strip\n", id="strip_bez_wywolania"),
    ],
)
def test_wywolanie_nie_bedace_dokladnie_sorted_x_ani_x_strip_nie_ma_mutanta(zrodlo):
    assert _wszystkie(zrodlo) == Counter()


# --- cofniecie mutacji: kazdy mutant to JEDNA zmiana wzgledem oryginalu -----------------------------

ZRODLO_REGEX_PRZED_KOLEKCJA = """\
import re

JEDEN = re.compile(r"^a$")
ROLE = ("author", "executor")
"""


def test_mutant_stalej_za_regex_nie_niesie_zmiany_regex_bo_ta_jest_cofnieta():
    zrodlo = ZRODLO_REGEX_PRZED_KOLEKCJA
    assert _wszystkie(zrodlo) == Counter({("regex", 3): 2, ("kolekcja", 4): 2})
    assert _drzewa_mutantow(zrodlo) == _drzewa(
        zrodlo.replace('r"^a$"', 'r"a$"'),
        zrodlo.replace('r"^a$"', 'r"^a"'),
        zrodlo.replace('("author", "executor")', '("executor",)'),
        zrodlo.replace('("author", "executor")', '("author",)'),
    )


ZRODLO_WYWOLANIA_W_LISCIE = "def f(a, b):\n    return g(sorted(a), b.strip())\n"
ZRODLO_WYWOLANIA_W_POLACH = "def f(a, b):\n    return sorted(a) + b.strip()\n"


@pytest.mark.parametrize(
    ("zrodlo", "po_usunieciu"),
    [
        pytest.param(
            ZRODLO_WYWOLANIA_W_LISCIE,
            ("def f(a, b):\n    return g(a, b.strip())\n", "def f(a, b):\n    return g(sorted(a), b)\n"),
            id="argumenty_wywolania_w_liscie",
        ),
        pytest.param(
            ZRODLO_WYWOLANIA_W_POLACH,
            ("def f(a, b):\n    return a + b.strip()\n", "def f(a, b):\n    return sorted(a) + b\n"),
            id="operandy_w_polach_pojedynczych",
        ),
    ],
)
def test_kolejne_usuniecia_wywolan_w_jednym_wyrazeniu_sa_niezalezne_od_siebie(zrodlo, po_usunieciu):
    assert _wszystkie(zrodlo) == Counter({("wywolanie", 2): 2})
    assert _drzewa_mutantow(zrodlo) == _drzewa(*po_usunieciu)


# --- deduplikacja: ten sam tekst w tej samej linii to jeden mutant ------------------------------------


def test_zamiana_dwoch_identycznych_wywolan_nie_jest_mutantem_bo_nic_nie_zmienia():
    zrodlo = "def f(d):\n    d.a()\n    d.a()\n"
    assert _wszystkie(zrodlo) == Counter({("instrukcja", 2): 1, ("instrukcja", 3): 1})
    bazowy = ast.unparse(ast.parse(zrodlo))
    assert all(
        m.zrodlo != bazowy for m in mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, zrodlo, {1, 2, 3})
    )


def test_dwa_usuniecia_dajace_ten_sam_tekst_w_tej_linii_to_jeden_mutant():
    assert _wszystkie('X = {"a", "a", "b"}\n') == Counter({("kolekcja", 1): 2})


def test_dwa_usuniecia_dajace_ten_sam_tekst_w_roznych_liniach_to_dwa_mutanty():
    zrodlo = 'X = {\n    "a",\n    "a",\n    "b",\n}\n'
    assert _wszystkie(zrodlo) == Counter({("kolekcja", 2): 1, ("kolekcja", 3): 1, ("kolekcja", 4): 1})


# --- bledy parsowania i unparse: kod 2, nigdy traceback ---------------------------------------------------


def test_samotny_surrogat_w_zrodle_to_blad_narzedzia_a_nie_wyjatek_unicode():
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia, match=re.escape(SCIEZKA)):
        mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, 'X = "\ud800"\n', {1})


class _AstZAwaria:
    """Podstawka za modul `ast` w narzedziu: `parse` albo `unparse` rzuca podany wyjatek."""

    def __init__(self, funkcja: str, wyjatek: type[BaseException]):
        self._funkcja = funkcja
        self._wyjatek = wyjatek

    def __getattr__(self, nazwa):
        if nazwa == self._funkcja:

            def awaria(*_argumenty, **_nazwane):
                raise self._wyjatek("awaria testowa")

            return awaria
        return getattr(ast, nazwa)


@pytest.mark.parametrize("funkcja", ["parse", "unparse"])
@pytest.mark.parametrize("wyjatek", [RecursionError, MemoryError])
def test_recursion_i_memory_error_z_parse_i_unparse_to_blad_narzedzia(monkeypatch, funkcja, wyjatek):
    monkeypatch.setattr(mutanty_uzupelniajace, "ast", _AstZAwaria(funkcja, wyjatek))
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia, match=re.escape(SCIEZKA)):
        mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, "X = 1\n", {1})


LANCUCH = " + ".join(["1"] * 3000)


@pytest.mark.parametrize(
    "zrodlo",
    [
        pytest.param(f"X = {LANCUCH}\n", id="glebokie_wyrazenie_w_stalej"),
        pytest.param(f'X = {{"a": {LANCUCH}, "b": 2}}\n', id="glebokie_wyrazenie_w_opisie_elementu_mapy"),
    ],
)
def test_wyrazenie_na_ktorym_unparse_przekracza_glebokosc_rekursji_to_blad_narzedzia(zrodlo):
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia, match=re.escape(SCIEZKA)):
        mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, zrodlo, {1})


def test_plik_z_wyrazeniem_ponad_glebokosc_unparse_to_kod_2_a_nie_traceback_ani_kod_1(
    tmp_path, capsys
):
    repo = _standardowy(tmp_path, f"X = {LANCUCH}\n", "def test_x():\n    pass\n")
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert SCIEZKA in blad
    assert wyjscie == ""


# --- spojnosc przebiegu kontrolnego i mutantow -------------------------------------------------------------

TEST_TEKST_MODULU = (
    "from pathlib import Path\n\nimport pakiet.modul\n\n\n"
    "def test_modul_zachowuje_komentarz_znacznik():\n"
    "    tekst = Path(pakiet.modul.__file__).read_text(encoding='utf-8')\n"
    "    assert '# znacznik' in tekst\n"
)
TEST_NUMER_LINII = (
    "from pakiet.modul import wczytaj\n\n\n"
    "def test_funkcja_zaczyna_sie_w_linii_czwartej():\n"
    "    assert wczytaj.__code__.co_firstlineno == 4\n"
)


@pytest.mark.parametrize(
    ("zrodlo", "test"),
    [
        pytest.param("# znacznik\n" + ZRODLO_EXCEPT, TEST_TEKST_MODULU, id="test_zalezny_od_komentarza"),
        pytest.param("# a\n# b\n# c\n" + ZRODLO_EXCEPT, TEST_NUMER_LINII, id="test_zalezny_od_numeru_linii"),
    ],
)
def test_test_zalezny_od_tekstu_modulu_jest_czerwony_bez_mutacji_a_nie_zabija_kazdego_mutanta(
    tmp_path, capsys, zrodlo, test
):
    repo = _standardowy(tmp_path, zrodlo, test)
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert TEST_MODULU in blad
    assert "nie przechodza bez mutacji" in blad
    assert wyjscie == ""


def test_modul_z_komentarzami_i_nietypowym_formatowaniem_jest_badany_jak_kazdy_inny(
    tmp_path, capsys
):
    zrodlo = "# opis\n" + ZRODLO_EXCEPT.replace("return None", "return None  # brak") + "\n\n"
    repo = _standardowy(tmp_path, zrodlo, PRZYPADKI[0].mocny_test)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 0
    assert wyjscie.splitlines() == ["zbadano 2"]


def test_komunikat_czerwonych_testow_bez_mutacji_podaje_pliki_posortowane_po_sciezce(tmp_path, capsys):
    czerwony = "from pakiet.modul import wczytaj\n\n\ndef test_czerwony():\n    assert False\n"
    baza = {
        "src/pakiet/__init__.py": "",
        SCIEZKA: "# modul\n",
        "tests/test_z.py": czerwony,
        "tests/a/test_b.py": czerwony,
    }
    repo = _repo(tmp_path, baza, {SCIEZKA: ZRODLO_EXCEPT})
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert "testy tests/a/test_b.py, tests/test_z.py nie przechodza bez mutacji" in blad
    assert wyjscie == ""


# --- strazniki gita: kazdy ma wlasny komunikat -------------------------------------------------------------

ZNACZNIK_GITA = "ZNACZNIK-BLEDU-GITA-7731"


def _zepsuj_git(monkeypatch, podpolecenie: str) -> None:
    oryginal = mutanty_uzupelniajace.mutanty_reczne.uruchom

    def uruchom(argv, cwd):
        if list(argv[:2]) == ["git", podpolecenie]:
            return subprocess.CompletedProcess(argv, 128, stdout="", stderr=ZNACZNIK_GITA)
        return oryginal(argv, cwd)

    monkeypatch.setattr(mutanty_uzupelniajace.mutanty_reczne, "uruchom", uruchom)


@pytest.mark.parametrize(
    ("podpolecenie", "oczekiwany", "obcy"),
    [
        pytest.param("merge-base", f"nieznana baza 'HEAD': {ZNACZNIK_GITA}", "git diff nie powiodl", id="merge_base"),
        pytest.param("diff", f"git diff nie powiodl sie: {ZNACZNIK_GITA}", "nieznana baza", id="diff"),
        pytest.param(
            "ls-files", f"git ls-files nie powiodl sie: {ZNACZNIK_GITA}", "git diff nie powiodl", id="ls_files"
        ),
    ],
)
def test_nieudane_polecenie_gita_to_kod_2_z_komunikatem_wlasnego_straznika(
    tmp_path, capsys, monkeypatch, podpolecenie, oczekiwany, obcy
):
    repo = _standardowy(tmp_path, PRZYPADKI[0].zrodlo, PRZYPADKI[0].slaby_test)
    _zepsuj_git(monkeypatch, podpolecenie)
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert oczekiwany in blad
    assert obcy not in blad
    assert wyjscie == ""


# --- zakres zmian z gita ---------------------------------------------------------------------------------------


def test_zmiana_pliku_nie_bedacego_pythonem_pod_src_to_brak_zmienionych_funkcji(tmp_path, capsys):
    baza = {"src/pakiet/__init__.py": "", "src/pakiet/dane.txt": "x\n"}
    repo = _repo(tmp_path, baza, {"src/pakiet/dane.txt": "def f(:\n"})
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 0
    assert wyjscie.splitlines() == ["brak zmienionych funkcji"]


def test_nowy_plik_py_ignorowany_przez_gitignore_nie_jest_badany(tmp_path, capsys):
    repo = _repo(tmp_path, {"src/pakiet/__init__.py": ""}, {"src/pakiet/__pycache__/zly.py": "def f(:\n"})
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 0
    assert wyjscie.splitlines() == ["brak zmienionych funkcji"]


@pytest.mark.parametrize(
    ("zrodlo", "oczekiwane"),
    [
        pytest.param("X = 1\n", "zbadano 0", id="stala_bez_ksztaltu"),
        pytest.param("def f():\n    return 0\n", "zbadano 0", id="funkcja_bez_ksztaltu"),
        pytest.param("async def f():\n    return 0\n", "zbadano 0", id="funkcja_async_bez_ksztaltu"),
        pytest.param("42\n", "zbadano 0", id="wyrazenie_liczbowe_nie_jest_docstringiem"),
        pytest.param('print("a")\n', "zbadano 0", id="wywolanie_na_poziomie_modulu"),
        pytest.param("import os\n", "brak zmienionych funkcji", id="sam_import"),
        pytest.param("from os import path\n", "brak zmienionych funkcji", id="sam_import_from"),
        pytest.param('"""Opis modulu."""\n', "brak zmienionych funkcji", id="sam_docstring"),
        pytest.param("# komentarz\n", "brak zmienionych funkcji", id="sam_komentarz"),
    ],
)
def test_diff_dotykajacy_stalej_albo_funkcji_bez_mutanta_to_zbadano_0_a_import_docstring_komentarz_to_brak(
    tmp_path, capsys, zrodlo, oczekiwane
):
    repo = _standardowy(tmp_path, zrodlo, "def test_x():\n    pass\n")
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 0
    assert wyjscie.splitlines() == [oczekiwane]


# --- wybor testow i sprzatanie --------------------------------------------------------------------------------


def _repo_z_testami(tmp_path: Path, testy: dict[str, str]) -> Path:
    baza = {"src/pakiet/__init__.py": "", SCIEZKA: "# modul\n", **testy}
    return _repo(tmp_path, baza, {SCIEZKA: ZRODLO_EXCEPT})


CZERWONY_Z_MODULEM = "from pakiet.modul import wczytaj\n\n\ndef test_czerwony():\n    assert False\n"


def test_plik_py_w_katalogu_tabel_mutantow_nie_jest_wybierany_do_testow(tmp_path, capsys):
    repo = _repo_z_testami(
        tmp_path,
        {TEST_MODULU: PRZYPADKI[0].slaby_test, "tests/mutanty_reczne/pomocnik.py": CZERWONY_Z_MODULEM},
    )
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 1
    assert len(wyjscie.splitlines()) == 2


def test_katalog_o_nazwie_mutanty_reczne_glebiej_w_tests_jest_przegladany(tmp_path, capsys):
    przypadek = PRZYPADKI[0]
    repo = _repo_z_testami(
        tmp_path,
        {TEST_MODULU: przypadek.slaby_test, "tests/inny/mutanty_reczne/test_mocny.py": przypadek.mocny_test},
    )
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 0
    assert wyjscie.splitlines() == ["zbadano 2"]


DOCTEST_CZERWONY = "pakiet.modul w opisie\n\n>>> 1 + 1\n3\n"


def test_plik_nie_bedacy_pythonem_z_nazwa_modulu_nie_jest_wybierany_do_testow(tmp_path, capsys):
    repo = _repo_z_testami(
        tmp_path, {TEST_MODULU: PRZYPADKI[0].slaby_test, "tests/test_notatki.txt": DOCTEST_CZERWONY}
    )
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 1
    assert len(wyjscie.splitlines()) == 2


def test_zmienna_wymagania_bazy_nie_trafia_do_testow_mutanta(tmp_path, capsys, monkeypatch):
    nazwa = mutanty_uzupelniajace.mutanty_reczne.ZMIENNA_WYMAGANIA_BAZY
    monkeypatch.setenv(nazwa, "1")
    test = PRZYPADKI[0].mocny_test + (
        f"\n\ndef test_zmienna_wymagania_bazy_nie_dotarla():\n    import os\n    assert {nazwa!r} not in os.environ\n"
    )
    repo = _standardowy(tmp_path, ZRODLO_EXCEPT, test)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 0
    assert wyjscie.splitlines() == ["zbadano 2"]


def test_brak_katalogu_testow_to_kod_2_z_komunikatem_o_katalogu_a_nie_zero_testow(tmp_path, capsys):
    repo = _repo(tmp_path, {"src/pakiet/__init__.py": "", SCIEZKA: "# modul\n"}, {SCIEZKA: ZRODLO_EXCEPT})
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert "nie da sie przejrzec katalogu testow" in blad
    assert wyjscie == ""


def test_plik_testu_nie_w_utf8_to_kod_2_ze_sciezka_pliku(tmp_path, capsys):
    repo = _repo_z_testami(tmp_path, {TEST_MODULU: PRZYPADKI[0].slaby_test})
    (repo / "tests" / "test_binarny.py").write_bytes(b"\xff\xfe\x00")
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert "tests/test_binarny.py" in blad
    assert "nie da sie odczytac testu" in blad
    assert wyjscie == ""


def test_plik_testu_ktorego_nie_da_sie_odczytac_to_kod_2_ze_sciezka_pliku(tmp_path, capsys):
    repo = _repo_z_testami(tmp_path, {TEST_MODULU: PRZYPADKI[0].slaby_test})
    (repo / "tests" / "test_zepsuty.py").symlink_to("nie-ma-takiego-pliku.py")
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert "tests/test_zepsuty.py" in blad
    assert "nie da sie odczytac testu" in blad
    assert wyjscie == ""


def test_kopie_repozytorium_sa_sprzatane_po_przebiegu(tmp_path, capsys, monkeypatch):
    katalog = tmp_path / "kopie"
    katalog.mkdir()
    utworzone: list[str] = []
    oryginal = tempfile.mkdtemp

    def mkdtemp(*argumenty, **nazwane):
        sciezka = oryginal(*argumenty, **{**nazwane, "dir": str(katalog)})
        utworzone.append(sciezka)
        return sciezka

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    przypadek = PRZYPADKI[0]
    repo = _standardowy(tmp_path, przypadek.zrodlo, przypadek.slaby_test)
    kod, _, _ = _narzedzie(repo, capsys)
    assert kod == 1
    assert len(utworzone) >= 3  # kontrola + dwa mutanty
    assert list(katalog.iterdir()) == []


def test_nieudane_utworzenie_katalogu_kopii_to_blad_narzedzia(tmp_path, monkeypatch):
    def mkdtemp(*_argumenty, **_nazwane):
        raise OSError("pelny dysk")

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia, match="nie da sie utworzyc katalogu tymczasowego"):
        with mutanty_uzupelniajace._kopia_repo(tmp_path):
            pass


def test_nieudane_sprzatanie_katalogu_kopii_to_blad_narzedzia(tmp_path, monkeypatch):
    oryginal = tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **k: oryginal(*a, **{**k, "dir": str(tmp_path)}))
    monkeypatch.setattr(mutanty_uzupelniajace.mutanty_reczne, "skopiuj_repo", lambda korzen, kopia: None)

    def rmtree(*_argumenty, **_nazwane):
        raise OSError("zablokowany")

    monkeypatch.setattr(mutanty_uzupelniajace.shutil, "rmtree", rmtree)
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia, match="nie da sie usunac katalogu tymczasowego"):
        with mutanty_uzupelniajace._kopia_repo(tmp_path):
            pass


# --- kolejnosc: pierwszy blad dotyczy pierwszego pliku alfabetycznie, niezaleznie od hasha ----------------


@pytest.mark.parametrize("ziarno", [1, 2, 3, 4])
def test_pierwszy_blad_zero_testow_dotyczy_pliku_pierwszego_alfabetycznie_przy_kazdym_ziarnie_hasha(
    tmp_path, ziarno
):
    pliki = [f"src/pakiet/m{numer:02d}.py" for numer in range(12)]
    baza = {"src/pakiet/__init__.py": "", "tests/test_inne.py": "def test_x():\n    pass\n"}
    baza.update({plik: "# modul\n" for plik in pliki})
    repo = _repo(tmp_path, baza, {plik: ZRODLO_EXCEPT for plik in pliki})
    proces = uruchom_proces_sync(
        [
            sys.executable, str(KORZEN / "tools" / "mutanty_uzupelniajace.py"),
            "--korzen", str(repo), "--baza", "HEAD", "--limit", LIMIT_TESTU_S,
        ],  # fmt: skip
        cwd=KORZEN,
        env={**os.environ, "PYTHONHASHSEED": str(ziarno)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wyjscie, blad = proces.communicate(timeout=120)
    finally:
        zakoncz_proces_sync(proces, nazwa="mutanty uzupelniajace z ziarnem hasha")
    assert proces.returncode == 2, blad
    assert "src/pakiet/m00.py: zero zebranych testow" in blad
    assert wyjscie == ""


# --- regex: kazdy rozpoznawany element wzorca ma mutant, nierozpoznawany go nie ma ---------------------


def _regex_zrodlo(wzorzec: str) -> str:
    return f'import re\n\nX = re.compile(r"{wzorzec}")\n'


def _wzorce_regex(wzorzec: str) -> list[str]:
    mutanty = mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, _regex_zrodlo(wzorzec), {3})
    assert {m.ksztalt for m in mutanty} <= {"regex"}
    return sorted(_drzewo(m.zrodlo) for m in mutanty)


def _oczekiwane_wzorce(*wzorce: str) -> list[str]:
    return sorted(_drzewo(_regex_zrodlo(w)) for w in wzorce)


PREFIKSY_GRUP = [
    "(?P<n>", "(?<=", "(?<!", "(?=", "(?!", "(?:", "(",
    "(?i:", "(?ai:", "(?i-s:", "(?i-sm:", "(?-i:", "(?-im:",
]  # fmt: skip


@pytest.mark.parametrize("prefiks", PREFIKSY_GRUP)
def test_alternatywy_grupy_kazdego_rozpoznawanego_rodzaju_sa_mutowane(prefiks):
    wzorzec = f"{prefiks}ab|cd)"
    assert _wzorce_regex(wzorzec) == _oczekiwane_wzorce(f"{prefiks}cd)", f"{prefiks}ab)")


@pytest.mark.parametrize(
    "wzorzec",
    [
        pytest.param("(?>ab|cd)", id="grupa_atomowa"),
        pytest.param("(a)(?(1)b|c)", id="grupa_warunkowa"),
        pytest.param("ab|cd", id="alternatywa_poza_grupa"),
        pytest.param("ab", id="wzorzec_bez_elementow"),
    ],
)
def test_wzorzec_bez_rozpoznawanych_elementow_nie_daje_mutantow(wzorzec):
    assert _wzorce_regex(wzorzec) == []


@pytest.mark.parametrize(
    "wzorzec",
    [
        pytest.param(r"(?x)^a # (", id="niezamkniety_nawias_w_komentarzu"),
        pytest.param(r"(?x)^a # )", id="nawias_zamykajacy_bez_otwierajacego"),
        pytest.param(r"(?x)^a # [", id="niezamknieta_klasa_w_komentarzu"),
        pytest.param(r"(?x)^a # \N{", id="nazwa_znaku_bez_zamkniecia"),
        pytest.param(r"(?x)^a # (?#", id="niezamkniety_komentarz_grupy"),
    ],
)
def test_wzorzec_ktorego_narzedzie_nie_umie_rozebrac_nie_daje_mutantow_regex(wzorzec):
    assert _wzorce_regex(wzorzec) == []


@pytest.mark.parametrize(
    ("wzorzec", "oczekiwane"),
    [
        pytest.param(r"[\x41b]", (r"[b]", r"[\x41]"), id="ucieczka_x"),
        pytest.param(r"[Ab]", (r"[b]", r"[A]"), id="ucieczka_u"),
        pytest.param(r"[\U00000041b]", (r"[b]", r"[\U00000041]"), id="ucieczka_U"),
        pytest.param(
            r"[\N{LATIN SMALL LETTER A}b]", (r"[b]", r"[\N{LATIN SMALL LETTER A}]"), id="nazwa_znaku"
        ),
        pytest.param(r"[\0b]", (r"[b]", r"[\0]"), id="osemkowa_jedna_cyfra"),
        pytest.param(r"[\12b]", (r"[b]", r"[\12]"), id="osemkowa_dwie_cyfry"),
        pytest.param(r"[\101b]", (r"[b]", r"[\101]"), id="osemkowa_trzy_cyfry"),
        pytest.param(r"[\1010]", (r"[0]", r"[\101]"), id="osemkowa_najwyzej_trzy_cyfry"),
        pytest.param(r"[a-cx]", (r"[x]", r"[a-c]"), id="zakres_to_jeden_element"),
        pytest.param(r"[ab-]", (r"[b-]", r"[a-]", r"[ab]"), id="myslnik_na_koncu_to_element"),
        pytest.param(r"[]a]", (r"[a]", r"[]]"), id="nawias_zamykajacy_na_poczatku_to_element"),
        pytest.param(r"[^]a]", (r"[^a]", r"[^]]"), id="nawias_po_negacji_to_element"),
        pytest.param(r"[a^b]", (r"[^b]", r"[ab]", r"[a^]"), id="daszek_w_srodku_klasy_to_element_nie_kotwica"),
        pytest.param(r"^\(a$", (r"\(a$", r"^\(a"), id="ucieczka_nawiasu_nie_otwiera_grupy"),
        pytest.param(r"^a\$", (r"a\$",), id="ucieczka_dolara_nie_jest_kotwica"),
        pytest.param(r"^a(?#x(y)$", (r"a(?#x(y)$", r"^a(?#x(y)"), id="nawias_w_komentarzu_grupy"),
    ],
)
def test_elementy_wzorca_regex_sa_usuwane_po_jednym_z_poprawnym_podzialem(wzorzec, oczekiwane):
    assert _wzorce_regex(wzorzec) == _oczekiwane_wzorce(*oczekiwane)
