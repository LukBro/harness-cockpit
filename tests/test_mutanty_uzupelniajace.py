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
- `regex`: w literale pierwszego argumentu `re.compile(...)` (tekstowym albo bajtowym `rb"..."`,
  rozbieranym tak samo; mutanty bajtowego sa bajtami): usuniecie jednej kotwicy (`^`, `$`),
  jednego elementu klasy znakow (znak albo sekwencja ucieczki `\\[` to JEDEN element; zakres
  `0-9` to JEDEN element; `^` negacji klasy nie jest elementem) albo jednej alternatywy
  (w grupie `(`, `(?:`, `(?P<n>`, `(?=`, `(?!`, `(?<=`, `(?<!`, `(?>`, `(?i:` i podobnych oraz poza
  grupa, w calym wzorcu; flagi globalne `(?i)` z poczatku wzorca nie nalezą do pierwszej alternatywy).
  Oryginal i mutanty sa kompilowane z flagami z wywolania (drugi argument albo `flags=`).
  Mutant, ktorego wzorzec nie kompiluje sie (np. pusta klasa `[]`), jest pomijany i nie liczy sie
  do `zbadano N`; to samo dotyczy wzorca zrodlowego odrzuconego przez `re.compile` (`re.error`,
  `OverflowError`). Literal, ktory wyglada na objety ksztaltem, a ktorego narzedzie nie umie
  rozebrac, to BLAD NARZEDZIA `plik:linia: nie umiem rozebrac wzorca (powod)`, nigdy zero
  mutantow: nawias albo `[` w komentarzu `(?x)`, `\\N{` bez `}`, grupa z `|` nierozpoznanego
  rodzaju (np. warunkowa `(?(1)a|b)`), flaga `re.X`/`re.VERBOSE` (takze w `re.I | re.VERBOSE`),
  flagi nieodczytywalne z kodu (zmienna, nieznana nazwa, wiele argumentow, `*args`,
  nieznany argument nazwany) i flagi niezgodne z wzorcem (`rb"ab", re.U`).

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
Diff dotyka funkcji albo stalej (`X = 1`, `42`, `print()` na poziomie modulu), ale zaden ksztalt
nie daje mutanta: to KOD 3 (brak dowodu) i linia `plik:linia: sprawdz recznie` na kazda dotknieta
funkcje lub stala (linia jej startu), nigdy `zbadano 0`. Sam import, sam docstring modulu albo sama
zmiana poza instrukcjami (komentarz) to `brak zmienionych funkcji` (kod 0).
`re.compile` ze wzorcem nieliteralnym (zmienna, f-string, wynik wywolania, takze `pattern=`) w
dotknietym zakresie to tez kod 3: linia wywolania (`plik:linia: sprawdz recznie`); `re.compile()`
bez argumentow nie. Powtorzony `pattern=` i gwiazdka w argumentach pozycyjnych `re.compile` to
blad narzedzia (kod 2) z `plik:linia` wywolania.
Priorytet kodow: 2 > 1 > 3 > 0. Przy kodzie 1 stdout to WYLACZNIE linie mutantow (bez linii
`sprawdz recznie`); `[[rownowazny]]` nie wycisza kodu 3.

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
- 3: brak dowodu (stdout to WYLACZNIE linie `<plik>:<linia>: sprawdz recznie`, posortowane): diff
  dotyka funkcji albo stalej, a zaden ksztalt nie daje mutanta (linia startu kazdej dotknietej
  funkcji lub stalej, takze `re.compile` ze wzorcem nieliteralnym), albo wszystkie mutanty
  zabite, a w dotknietym zakresie jest `re.compile` ze wzorcem nieliteralnym (wtedy tylko linie
  tych wywolan). Przezyl mutant -> kod 1 (pierwszenstwo nad 3).

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
import mutanty_zmian  # noqa: E402

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


def test_mutant_regex_kotwicy_zgloszony_przez_uzupelniajace_nie_wystepuje_w_wyniku_mutantow_zmian(
    tmp_path, capsys, monkeypatch
):
    # AC4: mutmut nie mutuje stalej modulu, wiec `mutanty_zmian` oddaje te linie jako "sprawdz recznie",
    # a mutanty regex tej linii daje dopiero narzedzie uzupelniajace. Oba na tym samym diffie HAR-176.
    linia = _linia(ZRODLO_KOTWIC_NOWE, "KOTWICA = re.compile")
    repo = _repo_kotwic(tmp_path, TEST_KOTWIC_SLABY)

    kod_uzupelniajacych, wyjscie_uzupelniajacych, _ = _narzedzie(repo, capsys)
    assert kod_uzupelniajacych == 1
    zgloszone = wyjscie_uzupelniajacych.splitlines()
    assert _wyniki(wyjscie_uzupelniajacych) == [(SCIEZKA_KOTWIC, linia, "regex")] * 3
    opisy = {wiersz.split(": ", 2)[2] for wiersz in zgloszone}
    assert len(opisy) == 3

    monkeypatch.chdir(repo)  # `mutanty_zmian.uruchom` wola gita w biezacym katalogu, nie w `--korzen`
    kod_zmian = mutanty_zmian.main(["--korzen", str(repo), "--baza", "HEAD"])
    wyjscie_zmian = capsys.readouterr()

    assert kod_zmian == 3, wyjscie_zmian.err
    assert wyjscie_zmian.out.splitlines() == [mutanty_zmian.NAGLOWEK_BEZ_MUTANTOW, f"{SCIEZKA_KOTWIC}:{linia}"]
    for opis in opisy:
        assert opis not in wyjscie_zmian.out
        assert opis not in wyjscie_zmian.err
    assert "regex" not in wyjscie_zmian.out


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
        return 0, ["zbadano 1"]

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


def _zepsuj_git(monkeypatch, podpolecenie: str, bez_argumentu: str | None = None) -> None:
    """Psuje `git <podpolecenie>`; z `bez_argumentu` tylko wywolania, w ktorych tego argumentu nie ma."""
    oryginal = mutanty_uzupelniajace.mutanty_reczne.uruchom

    def uruchom(argv, cwd):
        if list(argv[:2]) == ["git", podpolecenie] and (bez_argumentu is None or bez_argumentu not in argv):
            return subprocess.CompletedProcess(argv, 128, stdout="", stderr=ZNACZNIK_GITA)
        return oryginal(argv, cwd)

    monkeypatch.setattr(mutanty_uzupelniajace.mutanty_reczne, "uruchom", uruchom)


@pytest.mark.parametrize(
    ("podpolecenie", "bez_argumentu", "oczekiwany", "obcy"),
    [
        pytest.param(
            "merge-base", None, f"nieznana baza 'HEAD': {ZNACZNIK_GITA}", "git diff nie powiodl", id="merge_base"
        ),
        pytest.param("diff", None, f"git diff nie powiodl sie: {ZNACZNIK_GITA}", "nieznana baza", id="diff"),
        # `skopiuj_repo` tez wola `git ls-files` (z `--cached`); psuje sie tylko wywolanie ze strazy `_zmiany`
        pytest.param(
            "ls-files",
            "--cached",
            f"git ls-files nie powiodl sie: {ZNACZNIK_GITA}",
            "git diff nie powiodl",
            id="ls_files_nowe_pliki",
        ),
    ],
)
def test_nieudane_polecenie_gita_to_kod_2_z_komunikatem_wlasnego_straznika(
    tmp_path, capsys, monkeypatch, podpolecenie, bez_argumentu, oczekiwany, obcy
):
    repo = _standardowy(tmp_path, PRZYPADKI[0].zrodlo, PRZYPADKI[0].slaby_test)
    _zepsuj_git(monkeypatch, podpolecenie, bez_argumentu)
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


def _miejsce(plik: str, linia: int) -> str:
    return f"{plik}:{linia}: sprawdz recznie"


@pytest.mark.parametrize(
    ("zrodlo", "linia_startu"),
    [
        pytest.param("X = 1\n", 1, id="stala_bez_ksztaltu"),
        pytest.param("def f():\n    return 0\n", 1, id="funkcja_bez_ksztaltu"),
        pytest.param("async def f():\n    return 0\n", 1, id="funkcja_async_bez_ksztaltu"),
        pytest.param("42\n", 1, id="wyrazenie_liczbowe_nie_jest_docstringiem"),
        pytest.param('print("a")\n', 1, id="wywolanie_na_poziomie_modulu"),
        pytest.param("import os\n\n\ndef f():\n    return 0\n", 4, id="funkcja_po_imporcie_linia_startu_to_def"),
        pytest.param(
            "import functools\n\n\n@functools.cache\ndef f():\n    return 0\n", 4, id="funkcja_z_dekoratorem_start_w_dekoratorze"
        ),
        pytest.param("class A:\n    X = 1\n", 2, id="stala_w_ciele_klasy_linia_stalej"),
    ],
)
def test_diff_dotykajacy_stalej_albo_funkcji_bez_mutanta_to_kod_3_z_linia_startu_do_recznego_sprawdzenia(
    tmp_path, capsys, zrodlo, linia_startu
):
    repo = _standardowy(tmp_path, zrodlo, "def test_x():\n    pass\n")
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 3, blad
    assert wyjscie.splitlines() == [_miejsce(SCIEZKA, linia_startu)]
    assert "zbadano" not in wyjscie


@pytest.mark.parametrize(
    "zrodlo",
    [
        pytest.param("import os\n", id="sam_import"),
        pytest.param("from os import path\n", id="sam_import_from"),
        pytest.param('"""Opis modulu."""\n', id="sam_docstring"),
        pytest.param("# komentarz\n", id="sam_komentarz"),
    ],
)
def test_diff_z_samym_importem_docstringiem_albo_komentarzem_to_brak_zmienionych_funkcji_kod_0(
    tmp_path, capsys, zrodlo
):
    repo = _standardowy(tmp_path, zrodlo, "def test_x():\n    pass\n")
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 0
    assert wyjscie.splitlines() == ["brak zmienionych funkcji"]


def test_kilka_dotknietych_funkcji_bez_mutantow_to_linie_posortowane_liczbowo_a_nie_kolejnoscia_zbioru(
    tmp_path, capsys
):
    # osiem pozycji: kolejnosc iteracji zbioru krotek (napis, liczba) jest losowa, wiec bez sortowania
    # wynik jest rosnacy z prawdopodobienstwem 1/40320
    zrodlo = "\n\n".join(f"def f{n}():\n    return {n}" for n in range(8)) + "\n"
    linie = [_linia(zrodlo, f"def f{n}") for n in range(8)]
    assert linie == [1, 4, 7, 10, 13, 16, 19, 22]
    repo = _standardowy(tmp_path, zrodlo, "def test_x():\n    pass\n")
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 3
    assert wyjscie.splitlines() == [_miejsce(SCIEZKA, n) for n in linie]


def test_dotkniete_stale_w_dwoch_plikach_daja_linie_obu_plikow_posortowane_po_pliku(tmp_path, capsys):
    baza = {"src/pakiet/__init__.py": "", "src/pakiet/a.py": "# a\n", "src/pakiet/z.py": "# z\n"}
    repo = _repo(tmp_path, baza, {"src/pakiet/a.py": "X = 1\n", "src/pakiet/z.py": "Y = 2\n"})
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 3
    assert wyjscie.splitlines() == [_miejsce("src/pakiet/a.py", 1), _miejsce("src/pakiet/z.py", 1)]


def test_funkcja_bez_mutantow_zmieniona_tylko_wewnatrz_ma_miejsce_w_linii_def_a_nie_zmienionej(tmp_path, capsys):
    baza = {"src/pakiet/__init__.py": "", SCIEZKA: "def f():\n    x = 1\n    return 0\n"}
    repo = _repo(tmp_path, baza, {SCIEZKA: "def f():\n    x = 2\n    return 0\n"})
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 3
    assert wyjscie.splitlines() == [_miejsce(SCIEZKA, 1)]


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
    katalog, utworzone = _katalog_kopii(tmp_path, monkeypatch)
    przypadek = PRZYPADKI[0]
    repo = _standardowy(tmp_path, przypadek.zrodlo, przypadek.slaby_test)
    kod, _, _ = _narzedzie(repo, capsys)
    assert kod == 1
    assert len(utworzone) == 3  # dokladnie: jedna kontrola i dwa mutanty
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


def _regex_zrodlo(wzorzec: str, przedrostek: str = "r", reszta: str = "") -> str:
    """`X = re.compile(<przedrostek>"<wzorzec>"<reszta>)` w linii 3; `reszta` to np. `, re.I`."""
    return f'import re\n\nX = re.compile({przedrostek}"{wzorzec}"{reszta})\n'


def _wzorce_regex(wzorzec: str, przedrostek: str = "r", reszta: str = "") -> list[str]:
    zrodlo = _regex_zrodlo(wzorzec, przedrostek, reszta)
    mutanty = mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, zrodlo, {3})
    assert {m.ksztalt for m in mutanty} <= {"regex"}
    return sorted(_drzewo(m.zrodlo) for m in mutanty)


def _oczekiwane_wzorce(*wzorce: str, przedrostek: str = "r", reszta: str = "") -> list[str]:
    return sorted(_drzewo(_regex_zrodlo(w, przedrostek, reszta)) for w in wzorce)


def _blad_zrodla(zrodlo: str) -> str:
    """Tekst `BladNarzedzia` z `generuj_mutanty` dla zrodla, ktorego narzedzie nie umie rozebrac."""
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia) as wyjatek:
        mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, zrodlo, _wszystkie_linie(zrodlo))
    return str(wyjatek.value)


PREFIKSY_GRUP = [
    "(?P<n>", "(?<=", "(?<!", "(?=", "(?!", "(?:", "(", "(?>",
    "(?i:", "(?ai:", "(?i-s:", "(?i-sm:", "(?-i:", "(?-im:",
]  # fmt: skip


@pytest.mark.parametrize("prefiks", PREFIKSY_GRUP)
def test_alternatywy_grupy_kazdego_rozpoznawanego_rodzaju_sa_mutowane(prefiks):
    wzorzec = f"{prefiks}ab|cd)"
    assert _wzorce_regex(wzorzec) == _oczekiwane_wzorce(f"{prefiks}cd)", f"{prefiks}ab)")


@pytest.mark.parametrize(
    "wzorzec",
    [
        pytest.param("ab", id="wzorzec_bez_elementow"),
        pytest.param("(a)(?(1)b)", id="grupa_warunkowa_bez_alternatywy"),
        # bez `|` nie ma alternatyw; usuniecie calej zawartosci `(?i:ab)` dalo by kompilujacy sie `(?i)`
        pytest.param("(?i:ab)", id="grupa_z_flaga_bez_alternatywy"),
        pytest.param("(", id="wzorzec_odrzucony_przez_re_compile_niedomkniety_nawias"),
        pytest.param("a)", id="wzorzec_odrzucony_przez_re_compile_nawias_zamykajacy"),
    ],
)
def test_wzorzec_bez_rozpoznawanych_elementow_albo_odrzucony_przez_re_compile_nie_daje_mutantow(wzorzec):
    assert _wzorce_regex(wzorzec) == []


def test_wzorzec_bajtowy_bez_rozpoznawanych_elementow_nie_daje_mutantow():
    assert _wzorce_regex("ab", "rb") == []


def test_wzorzec_ktorego_kompilacja_rzuca_overflow_error_nie_ma_mutantow_a_nie_traceback():
    # powtorzenie ponad MAXREPEAT: OverflowError z `re.compile`, nie `re.error`
    with pytest.raises(OverflowError):
        re.compile("a{4294967296}")
    assert _wzorce_regex("a{4294967296}") == []


def test_mutant_ktorego_kompilacja_rzuca_overflow_error_jest_pominiety_a_nie_traceback():
    # `{1^0000000000}` to literal; po usunieciu `^` powstaje powtorzenie `{10000000000}` (OverflowError)
    re.compile("a{1^0000000000}")
    with pytest.raises(OverflowError):
        re.compile("a{10000000000}")
    assert _wzorce_regex("a{1^0000000000}") == []


def test_alternatywy_poza_grupa_sa_mutowane_jak_w_grupie():
    assert _wzorce_regex("ab|cd") == _oczekiwane_wzorce("cd", "ab")


@pytest.mark.parametrize(
    ("wzorzec", "powod"),
    [
        pytest.param(r"(?x)^a # (", "niezamknieta grupa", id="niezamkniety_nawias_w_komentarzu"),
        pytest.param(r"(?x)^a # )", "nawias zamykajacy bez otwierajacego", id="nawias_zamykajacy_bez_otwierajacego"),
        pytest.param(r"(?x)^a # [", "niezamknieta klasa znakow", id="niezamknieta_klasa_w_komentarzu"),
        pytest.param(r"(?x)^a # \N{", r"brak } w \N{...}", id="nazwa_znaku_bez_zamkniecia"),
        pytest.param(r"(?x)^a # (?#", "niezamkniety komentarz (?#", id="niezamkniety_komentarz_grupy"),
        pytest.param(
            "(a)(?(1)b|c)", "alternatywy w grupie nierozpoznanego rodzaju (znak 9)", id="grupa_warunkowa"
        ),
    ],
)
def test_wzorzec_ktorego_narzedzie_nie_umie_rozebrac_to_blad_narzedzia_z_linia_a_nie_zero_mutantow_regex(
    wzorzec, powod
):
    oczekiwany = f"{SCIEZKA}:3: nie umiem rozebrac wzorca ({powod})"
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia) as wyjatek:
        _wzorce_regex(wzorzec)
    assert str(wyjatek.value) == oczekiwany


UCIECZKA_U = "[" + "\\" + "u0041b]"  # sklejone: w pliku nie ma dosłownej sekwencji backslash-u


def test_wzorzec_ucieczki_u_w_testach_to_nawias_backslash_u_cztery_cyfry_a_nie_litera():
    # bajty: `[`, backslash (0x5C), `u` (0x75), `0041`, `b`, `]` - nie litera `A` w miejscu ucieczki
    assert UCIECZKA_U.encode("utf-8") == bytes([0x5B, 0x5C, 0x75, 0x30, 0x30, 0x34, 0x31, 0x62, 0x5D])


@pytest.mark.parametrize(
    ("wzorzec", "oczekiwane"),
    [
        pytest.param(r"[\x41b]", (r"[b]", r"[\x41]"), id="ucieczka_x"),
        pytest.param(UCIECZKA_U, ("[b]", "[" + "\\" + "u0041]"), id="ucieczka_u"),
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
        pytest.param("[aa]", ("[a]",), id="dwa_usuniecia_dajace_ten_sam_wzorzec_to_jeden_mutant"),
        pytest.param(r"(?x)a #\N$", (r"(?x)a #\N",), id="N_bez_nawiasu_klamrowego_to_dwa_znaki"),
    ],
)
def test_elementy_wzorca_regex_sa_usuwane_po_jednym_z_poprawnym_podzialem(wzorzec, oczekiwane):
    assert _wzorce_regex(wzorzec) == _oczekiwane_wzorce(*oczekiwane)


@pytest.mark.parametrize("cyfra", "01234567")
def test_ucieczka_osemkowa_zaczynajaca_sie_od_kazdej_cyfry_bierze_druga_cyfre_do_tego_samego_elementu(cyfra):
    wzorzec = "[" + "\\" + cyfra + "0b]"
    assert _wzorce_regex(wzorzec) == _oczekiwane_wzorce("[b]", "[" + "\\" + cyfra + "0]")


@pytest.mark.parametrize("cyfra", "01234567")
def test_ucieczka_osemkowa_przyjmuje_kazda_cyfre_od_0_do_7_na_drugiej_pozycji(cyfra):
    wzorzec = "[" + "\\0" + cyfra + "b]"
    assert _wzorce_regex(wzorzec) == _oczekiwane_wzorce("[b]", "[" + "\\0" + cyfra + "]")


# --- flagi globalne na poczatku wzorca nie naleza do pierwszej alternatywy -----------------------------

OPIS_ALT_AB = "usunieto alternatywe 'ab' z grupy"
OPIS_ALT_CD = "usunieto alternatywe 'cd' z grupy"


@pytest.mark.parametrize("flaga", "aimsux")
def test_pierwsza_alternatywa_wzorca_z_flaga_globalna_nie_obejmuje_flagi_ani_w_opisie_ani_w_wzorcu(flaga):
    wzorzec = "(?" + flaga + ")ab|cd"
    assert mutanty_uzupelniajace._warianty_wzorca(wzorzec) == [
        (OPIS_ALT_AB, "(?" + flaga + ")cd"),
        (OPIS_ALT_CD, "(?" + flaga + ")ab"),
    ]


@pytest.mark.parametrize(
    "wzorzec",
    [
        pytest.param("(?im)ab|cd", id="kilka_liter_w_jednej_fladze"),
        pytest.param("(?i)(?m)ab|cd", id="kilka_flag_globalnych_pod_rzad"),
    ],
)
def test_wszystkie_flagi_globalne_z_poczatku_wzorca_zostaja_poza_alternatywami(wzorzec):
    flagi = wzorzec[: wzorzec.index("ab")]
    assert mutanty_uzupelniajace._warianty_wzorca(wzorzec) == [
        (OPIS_ALT_AB, flagi + "cd"),
        (OPIS_ALT_CD, flagi + "ab"),
    ]


def test_flaga_lokale_na_poczatku_wzorca_bajtowego_zostaje_poza_alternatywami():
    assert mutanty_uzupelniajace._warianty_wzorca(b"(?L)ab|cd") == [
        (OPIS_ALT_AB, b"(?L)cd"),
        (OPIS_ALT_CD, b"(?L)ab"),
    ]


def test_dwa_usuniecia_dajace_ten_sam_wzorzec_to_jeden_wariant_w_samej_funkcji_wariantow():
    # `generuj_mutanty` tez deduplikuje, wiec tylko wywolanie funkcji wariantow widzi duplikat
    assert mutanty_uzupelniajace._warianty_wzorca("[aa]") == [("usunieto element 'a' z klasy znakow", "[a]")]


def test_mutanty_wzorca_z_flaga_globalna_to_dokladnie_dwie_alternatywy_bez_zmiany_flagi():
    assert _wzorce_regex("(?i)ab|cd") == _oczekiwane_wzorce("(?i)cd", "(?i)ab")


# --- wzorce bajtowe sa rozbierane jak tekstowe, a mutanty sa bajtami ---------------------------------------


@pytest.mark.parametrize(
    ("wzorzec", "oczekiwane"),
    [
        pytest.param("^a$", ("a$", "^a"), id="kotwice"),
        pytest.param("[ab]", ("[b]", "[a]"), id="klasa_znakow"),
        pytest.param("(?:ab|cd)", ("(?:cd)", "(?:ab)"), id="alternatywa_w_grupie"),
        pytest.param("ab|cd", ("cd", "ab"), id="alternatywa_poza_grupa"),
        pytest.param("(?L)ab|cd", ("(?L)cd", "(?L)ab"), id="flaga_lokale_na_poczatku"),
    ],
)
def test_wzorzec_bajtowy_jest_rozbierany_jak_tekstowy_a_mutanty_zostaja_bajtami(wzorzec, oczekiwane):
    assert _wzorce_regex(wzorzec, "rb") == _oczekiwane_wzorce(*oczekiwane, przedrostek="rb")


def test_mutanty_wzorca_bajtowego_maja_opisy_jak_tekstowego_i_typ_bytes():
    assert mutanty_uzupelniajace._warianty_wzorca(b"^a$") == [
        ("usunieto kotwice '^' z wzorca", b"a$"),
        ("usunieto kotwice '$' z wzorca", b"^a"),
    ]


ZRODLO_BAJTY_8BIT = 'import re\n\nX = re.compile(b"^\\xe9$")\n'


def test_wzorzec_bajtowy_z_bajtem_spoza_ascii_wraca_w_mutantach_jako_te_same_bajty():
    mutanty = mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, ZRODLO_BAJTY_8BIT, {3})
    oczekiwane = _drzewa(
        ZRODLO_BAJTY_8BIT.replace('b"^\\xe9$"', 'b"\\xe9$"'),
        ZRODLO_BAJTY_8BIT.replace('b"^\\xe9$"', 'b"^\\xe9"'),
    )
    assert sorted(_drzewo(m.zrodlo) for m in mutanty) == oczekiwane
    assert mutanty_uzupelniajace._warianty_wzorca(b"^\xe9$") == [
        ("usunieto kotwice '^' z wzorca", b"\xe9$"),
        ("usunieto kotwice '$' z wzorca", b"^\xe9"),
    ]


# --- flagi z wywolania `re.compile` ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reszta",
    [
        pytest.param(", re.I", id="nazwa_flagi_jako_drugi_argument"),
        pytest.param(", flags=re.I", id="flagi_jako_argument_nazwany"),
        pytest.param(", re.I | re.M", id="suma_bitowa_dwoch_flag"),
        pytest.param(", re.IGNORECASE | re.MULTILINE | re.DOTALL", id="suma_trzech_flag"),
        pytest.param(", 2", id="liczba_zamiast_nazwy"),
        pytest.param(", 0", id="liczba_zero"),
        pytest.param(", re.A", id="flaga_ascii"),
    ],
)
def test_flagi_odczytywalne_z_wywolania_nie_blokuja_mutantow_kotwic(reszta):
    assert _wzorce_regex("^a$", reszta=reszta) == _oczekiwane_wzorce("a$", "^a", reszta=reszta)


def _komunikat_wzorca(powod: str, linia: int = 3) -> str:
    return f"{SCIEZKA}:{linia}: nie umiem rozebrac wzorca ({powod})"


POWOD_VERBOSE = "flaga re.VERBOSE zmienia skladnie wzorca, narzedzie jej nie obsluguje"
POWOD_ARGUMENTY = "nie umiem odczytac argumentow re.compile (gwiazdka albo wiele flag)"
POWOD_KWARG = "nie umiem odczytac argumentow re.compile (nieznany argument nazwany)"
POWOD_GWIAZDKA_POZYCYJNA = "nie umiem odczytac argumentow re.compile (gwiazdka w argumentach pozycyjnych)"
POWOD_POWTORZONY_PATTERN = "nie umiem odczytac argumentow re.compile (powtorzony argument pattern=)"


@pytest.mark.parametrize(
    "reszta",
    [
        pytest.param(", re.X", id="skrot_verbose"),
        pytest.param(", re.VERBOSE", id="pelna_nazwa_verbose"),
        pytest.param(", re.I | re.VERBOSE", id="verbose_w_sumie_z_inna_flaga"),
        pytest.param(", re.I | re.M | re.X", id="verbose_w_sumie_trzech_flag"),
        pytest.param(", re.X | re.X", id="ta_sama_flaga_dwa_razy"),
        pytest.param(", flags=re.X", id="verbose_jako_argument_nazwany"),
        pytest.param(", 64", id="verbose_jako_liczba"),
    ],
)
def test_flaga_verbose_w_wywolaniu_to_blad_narzedzia_bo_zmienia_skladnie_wzorca(reszta):
    assert _blad_zrodla(_regex_zrodlo("^a$", reszta=reszta)) == _komunikat_wzorca(POWOD_VERBOSE)


@pytest.mark.parametrize(
    ("reszta", "wyrazenie"),
    [
        pytest.param(", FLAGI", "FLAGI", id="zmienna"),
        pytest.param(", flags=FLAGI", "FLAGI", id="zmienna_jako_argument_nazwany"),
        pytest.param(", re.NIEMA", "re.NIEMA", id="nieznana_nazwa_flagi_w_re"),
        pytest.param(", x.I", "x.I", id="nazwa_flagi_nie_z_modulu_re"),
        pytest.param(", a.re.I", "a.re.I", id="atrybut_atrybutu_modulu_re"),
        pytest.param(", re.I + re.M", "re.I + re.M", id="suma_arytmetyczna_zamiast_bitowej"),
        # komunikat nazywa nieodczytywalny operand, nie cala sume
        pytest.param(", re.I | FLAGI", "FLAGI", id="prawy_operand_sumy_nieodczytywalny"),
        pytest.param(", FLAGI | re.I", "FLAGI", id="lewy_operand_sumy_nieodczytywalny"),
        pytest.param(", False", "False", id="wartosc_logiczna_to_nie_liczba_flag"),
        pytest.param(', "i"', "'i'", id="napis_zamiast_flag"),
    ],
)
def test_flagi_nieodczytywalne_z_kodu_to_blad_narzedzia_z_wyrazeniem_a_nie_zero_mutantow(reszta, wyrazenie):
    oczekiwany = _komunikat_wzorca(f"nie umiem odczytac flag re.compile: {wyrazenie}")
    assert _blad_zrodla(_regex_zrodlo("^a$", reszta=reszta)) == oczekiwany


@pytest.mark.parametrize(
    ("reszta", "powod"),
    [
        pytest.param(", *FLAGI", POWOD_GWIAZDKA_POZYCYJNA, id="gwiazdka"),
        pytest.param(", re.I, re.M", POWOD_ARGUMENTY, id="dwa_argumenty_pozycyjne"),
        pytest.param(", re.I, flags=re.M", POWOD_ARGUMENTY, id="pozycyjny_i_nazwany"),
        pytest.param(", **OPCJE", POWOD_KWARG, id="rozwiniecie_slownika"),
        pytest.param(", flagz=re.I", POWOD_KWARG, id="nieznany_argument_nazwany"),
    ],
)
def test_argumenty_re_compile_ktorych_narzedzie_nie_umie_odczytac_to_blad_narzedzia(reszta, powod):
    assert _blad_zrodla(_regex_zrodlo("^a$", reszta=reszta)) == _komunikat_wzorca(powod)


# --- wzorzec podany jako `pattern=` ---------------------------------------------------------------------------

POWOD_PATTERN_I_POZYCYJNY = "nie umiem odczytac argumentow re.compile (pattern= razem z argumentem pozycyjnym)"


def _zrodlo_wywolania(argumenty: str) -> str:
    """Wywolanie `re.compile` rozlozone na trzy linie: nawias w linii 3, argumenty w linii 4."""
    return f"import re\n\nX = re.compile(\n    {argumenty}\n)\n"


def _wzorce_wywolania(argumenty: str) -> list[str]:
    zrodlo = _zrodlo_wywolania(argumenty)
    mutanty = mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, zrodlo, _wszystkie_linie(zrodlo))
    assert {m.ksztalt for m in mutanty} <= {"regex"}
    return sorted(_drzewo(m.zrodlo) for m in mutanty)


def _oczekiwane_wywolania(*argumenty: str) -> list[str]:
    return sorted(_drzewo(_zrodlo_wywolania(a)) for a in argumenty)


@pytest.mark.parametrize(
    ("argumenty", "oczekiwane"),
    [
        pytest.param('pattern=r"^a$"', ('pattern=r"a$"', 'pattern=r"^a"'), id="sam_wzorzec_nazwany"),
        pytest.param(
            'pattern=r"^a$", flags=re.I',
            ('pattern=r"a$", flags=re.I', 'pattern=r"^a", flags=re.I'),
            id="wzorzec_nazwany_z_flagami_nazwanymi",
        ),
        pytest.param(
            'flags=re.I, pattern=r"^a$"',
            ('flags=re.I, pattern=r"a$"', 'flags=re.I, pattern=r"^a"'),
            id="flagi_przed_wzorcem_nazwanym",
        ),
    ],
)
def test_wzorzec_podany_jako_pattern_ma_te_same_mutanty_kotwic_co_pozycyjny(argumenty, oczekiwane):
    assert _wzorce_wywolania(argumenty) == _oczekiwane_wywolania(*oczekiwane)


@pytest.mark.parametrize(
    ("argumenty", "linia", "powod"),
    [
        pytest.param(
            'r"^a$", pattern=r"b"', 3, POWOD_PATTERN_I_POZYCYJNY, id="pattern_razem_z_pozycyjnym_literalem"
        ),
        pytest.param("p, pattern=q", 3, POWOD_PATTERN_I_POZYCYJNY, id="pattern_razem_z_pozycyjnym_nie_literalem"),
        pytest.param('pattern=r"^a$", foo=1', 4, POWOD_KWARG, id="literal_nazwany_z_nieznanym_argumentem"),
        pytest.param('r"^a$", foo=1', 4, POWOD_KWARG, id="literal_pozycyjny_z_nieznanym_argumentem"),
        pytest.param("p, foo=1", 3, POWOD_KWARG, id="wzorzec_zmienna_z_nieznanym_argumentem"),
        pytest.param("pattern=p, foo=1", 3, POWOD_KWARG, id="wzorzec_nazwany_zmienna_z_nieznanym_argumentem"),
        pytest.param("**kw", 3, POWOD_KWARG, id="rozwiniecie_slownika_bez_wzorca"),
        pytest.param(
            'pattern=r"^a$", pattern=r"b"', 3, POWOD_POWTORZONY_PATTERN, id="powtorzony_pattern_z_literalami"
        ),
        pytest.param("pattern=p, pattern=q", 3, POWOD_POWTORZONY_PATTERN, id="powtorzony_pattern_ze_zmiennymi"),
        pytest.param(
            "p, pattern=q, pattern=r", 3, POWOD_POWTORZONY_PATTERN, id="powtorzony_pattern_wazniejszy_niz_pozycyjny"
        ),
        pytest.param(
            "*ARGS, pattern=p, pattern=q", 3, POWOD_POWTORZONY_PATTERN, id="powtorzony_pattern_wazniejszy_niz_gwiazdka"
        ),
        pytest.param("*ARGS", 3, POWOD_GWIAZDKA_POZYCYJNA, id="sama_gwiazdka_w_pozycyjnych"),
        pytest.param(
            'r"^a$", *FLAGI', 3, POWOD_GWIAZDKA_POZYCYJNA, id="gwiazdka_po_literale_wskazuje_linie_wywolania"
        ),
        pytest.param(
            "*ARGS, pattern=p", 3, POWOD_GWIAZDKA_POZYCYJNA, id="gwiazdka_wazniejsza_niz_pattern_z_pozycyjnym"
        ),
    ],
)
def test_argumenty_nazwane_re_compile_ktorych_narzedzie_nie_umie_odczytac_to_blad_z_linia_literalu_albo_wywolania(
    argumenty, linia, powod
):
    assert _blad_zrodla(_zrodlo_wywolania(argumenty)) == _komunikat_wzorca(powod, linia=linia)


@pytest.mark.parametrize(
    "argumenty",
    [
        pytest.param("p", id="wzorzec_zmienna_pozycyjnie"),
        pytest.param("pattern=p", id="wzorzec_zmienna_nazwany"),
        pytest.param("p, flags=re.I", id="wzorzec_zmienna_z_flagami_nazwanymi"),
        pytest.param("pattern=p, flags=re.I", id="wzorzec_zmienna_nazwany_z_flagami_nazwanymi"),
    ],
)
def test_wzorzec_ktory_nie_jest_literalem_nie_daje_mutantow_ani_bledu(argumenty):
    assert _wzorce_wywolania(argumenty) == []


def test_blad_flag_wskazuje_linie_literalu_wzorca_a_nie_linie_wywolania():
    zrodlo = 'import re\n\nX = re.compile(\n    r"^a$",\n    re.X,\n)\n'
    assert _blad_zrodla(zrodlo) == _komunikat_wzorca(POWOD_VERBOSE, linia=4)


def test_blad_rozbioru_wzorca_wskazuje_linie_literalu_a_nie_linie_wywolania():
    zrodlo = 'import re\n\nX = re.compile(\n    r"(?x)^a # (",\n)\n'
    assert _blad_zrodla(zrodlo) == _komunikat_wzorca("niezamknieta grupa", linia=4)


def test_flagi_z_wywolania_sa_uzyte_do_kompilacji_oryginalu_wiec_niezgodne_z_wzorcem_to_blad_narzedzia():
    assert _wzorce_regex("(?a)^x$") == _oczekiwane_wzorce("(?a)x$", "(?a)^x")
    komunikat = _blad_zrodla(_regex_zrodlo("(?a)^x$", reszta=", re.U"))
    assert komunikat.startswith(_komunikat_wzorca("flagi niezgodne z wzorcem: ")[:-1])
    assert komunikat.endswith(")")


@pytest.mark.parametrize(
    ("przedrostek", "reszta", "fragment"),
    [
        pytest.param("rb", ", re.U", "UNICODE", id="wzorzec_bajtowy_z_flaga_unicode"),
        pytest.param("rb", ", flags=re.U", "UNICODE", id="wzorzec_bajtowy_z_flaga_unicode_nazwana"),
        pytest.param("r", ", re.L", "LOCALE", id="wzorzec_tekstowy_z_flaga_lokale"),
    ],
)
def test_flaga_niezgodna_z_typem_wzorca_to_blad_narzedzia_a_nie_zero_mutantow(przedrostek, reszta, fragment):
    komunikat = _blad_zrodla(_regex_zrodlo("ab", przedrostek, reszta))
    assert komunikat.startswith(_komunikat_wzorca("flagi niezgodne z wzorcem: ")[:-1])
    assert fragment in komunikat
    assert komunikat.endswith(")")


def test_flaga_debug_nie_wypisuje_zrzutu_wzorca_na_stdout_podczas_rozbioru(capsys):
    assert _wzorce_regex("^a$", reszta=", re.DEBUG") == _oczekiwane_wzorce("a$", "^a", reszta=", re.DEBUG")
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "zrodlo",
    [
        pytest.param("import re\n\nX = re.compile(WZORZEC)\n", id="wzorzec_ze_zmiennej"),
        pytest.param("import re\n\nX = re.compile(modul.WZORZEC)\n", id="wzorzec_z_atrybutu"),
        pytest.param("import re\n\nX = re.compile()\n", id="bez_argumentow"),
    ],
)
def test_re_compile_bez_literalu_wzorca_nie_daje_mutantow_regex_ani_wyjatku(zrodlo):
    assert mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, zrodlo, {3}) == []


# --- wzorzec nieliteralny: narzedzie go nie zbada, wiec to miejsce do recznego sprawdzenia ---------------------


def _miejsca_nieliteralne(zrodlo: str, linie: set[int] | None = None) -> list[int]:
    """Czwarty element `_analiza`: linie `re.compile` z wzorcem, ktorego narzedzie nie umie zbadac."""
    return mutanty_uzupelniajace._analiza(
        SCIEZKA, zrodlo, linie if linie is not None else _wszystkie_linie(zrodlo)
    )[3]


@pytest.mark.parametrize(
    "zrodlo",
    [
        pytest.param("import re\n\nX = re.compile(WZORZEC)\n", id="zmienna"),
        pytest.param("import re\n\nX = re.compile(modul.WZORZEC)\n", id="atrybut"),
        pytest.param('import re\n\nX = re.compile(f"{PRZEDROSTEK}a")\n', id="f_string"),
        pytest.param("import re\n\nX = re.compile(zbuduj())\n", id="wynik_wywolania"),
        pytest.param("import re\n\nX = re.compile(pattern=WZORZEC)\n", id="zmienna_jako_pattern"),
        pytest.param('import re\n\nX = re.compile(pattern=f"{P}a")\n', id="f_string_jako_pattern"),
        pytest.param("import re\n\nX = re.compile(WZORZEC, re.I)\n", id="zmienna_z_flagami"),
        pytest.param("import re\n\nX = re.compile(WZORZEC, flags=FLAGI)\n", id="zmienna_z_flagami_nieodczytywalnymi"),
    ],
)
def test_re_compile_ze_wzorcem_nieliteralnym_w_dotknietym_zakresie_to_miejsce_do_recznego_sprawdzenia(zrodlo):
    assert _miejsca_nieliteralne(zrodlo, {3}) == [3]


def test_re_compile_bez_argumentow_i_z_literalem_nie_sa_miejscami_do_recznego_sprawdzenia():
    assert _miejsca_nieliteralne("import re\n\nX = re.compile()\n") == []
    assert _miejsca_nieliteralne(ZRODLO_KLASA_ZNAKOW) == []
    assert _miejsca_nieliteralne(_regex_zrodlo("^a$", "rb")) == []


def test_wywolanie_inne_niz_re_compile_ze_zmienna_nie_jest_miejscem_do_recznego_sprawdzenia():
    zrodlo = "import re\n\nX = re.sub(WZORZEC, '', 'a')\nY = zbuduj.compile(WZORZEC)\nZ = compile(WZORZEC)\n"
    assert _miejsca_nieliteralne(zrodlo) == []


def test_re_compile_ze_zmienna_poza_dotknietym_zakresem_nie_jest_miejscem_do_recznego_sprawdzenia():
    zrodlo = "import re\n\nX = re.compile(WZORZEC)\n\n\ndef f():\n    return 0\n"
    assert _miejsca_nieliteralne(zrodlo, {7}) == []
    assert _miejsca_nieliteralne(zrodlo, {3}) == [3]


def test_dwa_wywolania_re_compile_ze_zmienna_w_jednej_linii_to_jedno_miejsce():
    assert _miejsca_nieliteralne("import re\n\nX = re.compile(A) or re.compile(B)\n") == [3]


def test_miejsca_nieliteralne_sa_posortowane_rosnaco_choc_ast_odwiedza_plytsze_wywolanie_pierwsze():
    # linie 7 i 8: sam zbior `{7, 8}` iteruje jako [8, 7], wiec tylko `sorted` daje kolejnosc rosnaca
    zrodlo = "import re\n\n\n\ndef f(t, c):\n    if c:\n        a = re.compile(t)\n    return re.compile(t)\n"
    assert _linia(zrodlo, "a = re.compile") < _linia(zrodlo, "return re.compile")
    assert list({_linia(zrodlo, "a = re.compile"), _linia(zrodlo, "return re.compile")}) == [8, 7]
    assert _miejsca_nieliteralne(zrodlo) == [7, 8]


def test_wzorzec_nieliteralny_obok_literalu_nie_blokuje_mutantow_literalu():
    zrodlo = 'import re\n\nA = re.compile(r"^a$")\nB = re.compile(WZORZEC)\n'
    analiza = mutanty_uzupelniajace._analiza(SCIEZKA, zrodlo, _wszystkie_linie(zrodlo))
    assert {m.linia for m in analiza[0]} == {3}
    assert len(analiza[0]) == 2
    assert analiza[3] == [4]


def test_dotkniete_zakresy_z_analizy_to_linie_startu_funkcji_i_stalych_bez_importow():
    zrodlo = "X = 1\nY = {\n    'a': 1,\n}\n\n\ndef f():\n    return 0\n"
    analiza = mutanty_uzupelniajace._analiza(SCIEZKA, zrodlo, _wszystkie_linie(zrodlo))
    assert analiza[1] == [1, 2, 7]


# --- opis mutanta: caly wiersz `plik:linia: ksztalt: opis` dla kazdego ksztaltu ------------------------------


OPISY_PRZYPADKOW = {
    "krotka_except": [
        (4, "except", "usunieto element ValueError z krotki except"),
        (4, "except", "usunieto element TypeError z krotki except"),
    ],
    "zwolnienie_w_finally": [(5, "instrukcja", "zastapiono wywolanie kanal.zamknij() przez pass")],
    "kolejnosc_dwoch_zapisow": [
        (
            2,
            "kolejnosc",
            "zamieniono kolejnosc wywolan dziennik.dopisz('artefakt') i dziennik.dopisz('checkpoint')",
        ),
        (2, "instrukcja", "zastapiono wywolanie dziennik.dopisz('artefakt') przez pass"),
        (3, "instrukcja", "zastapiono wywolanie dziennik.dopisz('checkpoint') przez pass"),
    ],
    "stala_zbioru_stanow": [
        (1, "kolekcja", "usunieto element 'zakonczony' z zbioru"),
        (1, "kolekcja", "usunieto element 'przerwany' z zbioru"),
        (1, "kolekcja", "usunieto element 'odrzucony' z zbioru"),
    ],
    "krotka_w_funkcji": [
        (2, "kolekcja", "usunieto element 'author' z krotki"),
        (2, "kolekcja", "usunieto element 'executor' z krotki"),
    ],
    "mapa_w_funkcji": [
        (2, "kolekcja", "usunieto element 'ok': 'zielony' z mapy"),
        (2, "kolekcja", "usunieto element 'blad': 'czerwony' z mapy"),
    ],
    "sorted_w_funkcji": [(2, "wywolanie", "usunieto wywolanie w sorted(kroki)")],
    "strip_w_funkcji": [(2, "wywolanie", "usunieto wywolanie w wpis.strip()")],
    "klasa_znakow_i_kotwice": [
        (3, "regex", "usunieto kotwice '^' z wzorca"),
        (3, "regex", "usunieto kotwice '$' z wzorca"),
        (3, "regex", "usunieto element 'a' z klasy znakow"),
        (3, "regex", "usunieto element 'b' z klasy znakow"),
        (3, "regex", "usunieto element 'c' z klasy znakow"),
    ],
    "alternatywa_w_grupie": [
        (3, "regex", "usunieto kotwice '^' z wzorca"),
        (3, "regex", "usunieto kotwice '$' z wzorca"),
        (3, "regex", "usunieto alternatywe 'ab' z grupy"),
        (3, "regex", "usunieto alternatywe 'cd' z grupy"),
    ],
    "regex_z_mutantem_niekompilowalnym": [
        (3, "regex", "usunieto kotwice '^' z wzorca"),
        (3, "regex", "usunieto kotwice '$' z wzorca"),
    ],
    "await_w_finally": [
        (
            5,
            "kolejnosc",
            "zamieniono kolejnosc wywolan await klient.anuluj() i await klient.stop()",
        ),
        (5, "instrukcja", "zastapiono wywolanie await klient.anuluj() przez pass"),
        (6, "instrukcja", "zastapiono wywolanie await klient.stop() przez pass"),
    ],
}


def test_kazdy_przypadek_ma_oczekiwane_wiersze_wyniku():
    assert set(OPISY_PRZYPADKOW) == set(IDY)


@pytest.mark.parametrize("przypadek", PRZYPADKI, ids=IDY)
def test_wiersz_wyniku_kazdego_mutanta_to_plik_linia_ksztalt_i_pelny_opis(przypadek):
    mutanty = mutanty_uzupelniajace.generuj_mutanty(
        SCIEZKA, przypadek.zrodlo, _wszystkie_linie(przypadek.zrodlo)
    )
    wiersze = sorted(f"{m.plik}:{m.linia}: {m.ksztalt}: {m.opis}" for m in mutanty)
    oczekiwane = sorted(
        f"{SCIEZKA}:{linia}: {ksztalt}: {opis}" for linia, ksztalt, opis in OPISY_PRZYPADKOW[przypadek.nazwa]
    )
    assert wiersze == oczekiwane


def _opisy_stalej(zrodlo: str) -> set[str]:
    return {m.opis for m in mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, zrodlo, {1})}


def test_opis_elementu_do_60_znakow_jest_pelny_a_dluzszy_ucinany_do_57_znakow_i_trzech_kropek():
    pelny = "x" * 58  # z cudzyslowami repr ma dokladnie 60 znakow
    za_dlugi = "x" * 59  # 61 znakow
    assert _opisy_stalej(f'X = ("{pelny}", "b")\n') == {
        f"usunieto element '{pelny}' z krotki",
        "usunieto element 'b' z krotki",
    }
    assert _opisy_stalej(f'X = ("{za_dlugi}", "b")\n') == {
        "usunieto element '" + "x" * 56 + "... z krotki",
        "usunieto element 'b' z krotki",
    }


def test_opis_elementu_sciska_kazdy_ciag_bialych_znakow_do_jednej_spacji():
    assert _opisy_stalej('X = ("a  b", "c")\n') == {
        "usunieto element 'a b' z krotki",
        "usunieto element 'c' z krotki",
    }


# --- ksztalt `kolejnosc`: tylko dwa samodzielne wywolania obok siebie -------------------------------------------


def test_wywolanie_przed_instrukcja_niebedaca_wywolaniem_nie_ma_mutanta_kolejnosci():
    zrodlo = "def f(d):\n    d.a()\n    x = 1\n    return x\n"
    assert _wszystkie(zrodlo) == Counter({("instrukcja", 2): 1})


# --- kod 3 bez mutantow: stala tekstowa nie jest docstringiem, flagi diffu z wielu plikow ---------------------------


def test_stala_tekstowa_przypisana_do_nazwy_to_stala_do_zbadania_a_nie_docstring(tmp_path, capsys):
    repo = _standardowy(tmp_path, 'X = "a"\n', "def test_x():\n    pass\n")
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 3
    assert wyjscie.splitlines() == [_miejsce(SCIEZKA, 1)]


@pytest.mark.parametrize(
    ("pierwszy", "drugi", "plik_ze_stala"),
    [
        pytest.param("X = 1\n", "import os\n", "src/pakiet/a.py", id="stala_w_pierwszym_pliku_import_w_drugim"),
        pytest.param("import os\n", "X = 1\n", "src/pakiet/z.py", id="import_w_pierwszym_pliku_stala_w_drugim"),
    ],
)
def test_diff_dotykajacy_stalej_w_jednym_z_dwoch_plikow_to_miejsce_tylko_tego_pliku_niezaleznie_od_kolejnosci(
    tmp_path, capsys, pierwszy, drugi, plik_ze_stala
):
    baza = {"src/pakiet/__init__.py": "", "src/pakiet/a.py": "# a\n", "src/pakiet/z.py": "# z\n"}
    repo = _repo(tmp_path, baza, {"src/pakiet/a.py": pierwszy, "src/pakiet/z.py": drugi})
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 3
    assert wyjscie.splitlines() == [_miejsce(plik_ze_stala, 1)]


# --- kod 3: wzorzec nieliteralny i priorytet kodow 2 > 1 > 3 > 0 ----------------------------------------------

ZRODLO_WZORZEC_ZMIENNY = "import re\n\nX = re.compile(WZORZEC)\n"
ZRODLO_EXCEPT_I_WZORZEC = "import re\n\n\n" + ZRODLO_EXCEPT + "\n\ndef wzorzec(tekst):\n    return re.compile(tekst)\n"
LINIA_WYWOLANIA_RE_COMPILE = _linia(ZRODLO_EXCEPT_I_WZORZEC, "return re.compile(tekst)")
LINIA_DEF_WZORZEC = _linia(ZRODLO_EXCEPT_I_WZORZEC, "def wzorzec")
TEST_NIC = "def test_x():\n    pass\n"


def test_re_compile_ze_zmienna_bez_mutantow_to_kod_3_z_jedna_linia_wywolania(tmp_path, capsys):
    repo = _standardowy(tmp_path, ZRODLO_WZORZEC_ZMIENNY, TEST_NIC)
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 3, blad
    # linia startu stalej i linia wywolania to ta sama linia: jedna pozycja, nie dwie
    assert wyjscie.splitlines() == [_miejsce(SCIEZKA, 3)]


def test_re_compile_ze_zmienna_w_funkcji_bez_mutantow_daje_linie_def_i_linie_wywolania(tmp_path, capsys):
    zrodlo = "import re\n\n\ndef zbuduj(wzorzec):\n    x = 1\n    return re.compile(wzorzec)\n"
    repo = _standardowy(tmp_path, zrodlo, TEST_NIC)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 3
    assert wyjscie.splitlines() == [_miejsce(SCIEZKA, 4), _miejsce(SCIEZKA, 6)]


def test_wszystkie_mutanty_zabite_ale_re_compile_ze_zmienna_w_zakresie_to_kod_3_tylko_z_linia_wywolania(
    tmp_path, capsys
):
    repo = _standardowy(tmp_path, ZRODLO_EXCEPT_I_WZORZEC, PRZYPADKI[0].mocny_test)
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 3, blad
    assert wyjscie.splitlines() == [_miejsce(SCIEZKA, LINIA_WYWOLANIA_RE_COMPILE)]
    assert LINIA_DEF_WZORZEC != LINIA_WYWOLANIA_RE_COMPILE
    assert "zbadano" not in wyjscie


def test_przezyl_mutant_i_jest_re_compile_ze_zmienna_to_kod_1_a_stdout_ma_tylko_linie_mutantow(tmp_path, capsys):
    repo = _standardowy(tmp_path, ZRODLO_EXCEPT_I_WZORZEC, PRZYPADKI[0].slaby_test)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    linia = _linia(ZRODLO_EXCEPT_I_WZORZEC, TEKST_EXCEPT)
    assert kod == 1
    assert wyjscie.splitlines() == [
        f"{SCIEZKA}:{linia}: except: {OPIS_TYPE}",
        f"{SCIEZKA}:{linia}: except: {OPIS_VALUE}",
    ]
    assert "sprawdz recznie" not in wyjscie


def test_wpis_rownowazny_wycisza_mutanty_ale_nie_wycisza_miejsca_do_recznego_sprawdzenia(tmp_path, capsys):
    tabela = _tabela(
        WPIS_OBA_ELEMENTY,
        (SCIEZKA, "return re.compile(tekst)", _uzasadnienie("regex")),
        (SCIEZKA, "def wzorzec(tekst):", _uzasadnienie("instrukcja")),
    )
    repo = _standardowy(tmp_path, ZRODLO_EXCEPT_I_WZORZEC, PRZYPADKI[0].slaby_test, tabela)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 3
    assert wyjscie.splitlines() == [_miejsce(SCIEZKA, LINIA_WYWOLANIA_RE_COMPILE)]


def test_wpis_rownowazny_nie_wycisza_miejsca_do_recznego_sprawdzenia_gdy_nie_ma_mutantow(tmp_path, capsys):
    zrodlo = "import re\n\n\ndef zbuduj(wzorzec):\n    x = 1\n    return re.compile(wzorzec)\n"
    tabela = _tabela((SCIEZKA, "return re.compile(wzorzec)", _uzasadnienie("regex")))
    repo = _standardowy(tmp_path, zrodlo, TEST_NIC, tabela)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 3
    assert wyjscie.splitlines() == [_miejsce(SCIEZKA, 4), _miejsce(SCIEZKA, 6)]


def test_plik_bez_mutantow_z_re_compile_ze_zmienna_nie_wymaga_testow_a_mutanty_innego_pliku_sa_badane(
    tmp_path, capsys
):
    test_z = PRZYPADKI[0].mocny_test.replace("pakiet.modul", "pakiet.z")
    baza = {"src/pakiet/__init__.py": "", "src/pakiet/a.py": "# a\n", "src/pakiet/z.py": "# z\n", "tests/test_z.py": test_z}
    repo = _repo(tmp_path, baza, {"src/pakiet/a.py": ZRODLO_WZORZEC_ZMIENNY, "src/pakiet/z.py": ZRODLO_EXCEPT})
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 3, blad
    assert wyjscie.splitlines() == [_miejsce("src/pakiet/a.py", 3)]


def test_re_compile_ze_zmienna_poza_zmienionym_zakresem_to_brak_zmienionych_funkcji(tmp_path, capsys):
    baza = {"src/pakiet/__init__.py": "", SCIEZKA: ZRODLO_WZORZEC_ZMIENNY}
    repo = _repo(tmp_path, baza, {SCIEZKA: "import os\n" + ZRODLO_WZORZEC_ZMIENNY})
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 0
    assert wyjscie.splitlines() == ["brak zmienionych funkcji"]


def test_blad_narzedzia_w_jednym_pliku_ma_pierwszenstwo_nad_miejscem_do_recznego_sprawdzenia_w_drugim(
    tmp_path, capsys
):
    baza = {"src/pakiet/__init__.py": "", "src/pakiet/a.py": "# a\n", "src/pakiet/z.py": "# z\n"}
    repo = _repo(tmp_path, baza, {"src/pakiet/a.py": ZRODLO_WZORZEC_ZMIENNY, "src/pakiet/z.py": "def f(:\n"})
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert "src/pakiet/z.py" in blad
    assert wyjscie == ""


@pytest.mark.parametrize(
    ("zrodlo", "powod"),
    [
        pytest.param(
            "import re\n\nX = re.compile(pattern=r'^a$', pattern=r'b')\n",
            POWOD_POWTORZONY_PATTERN,
            id="powtorzony_pattern",
        ),
        pytest.param("import re\n\nX = re.compile(*ARGS)\n", POWOD_GWIAZDKA_POZYCYJNA, id="gwiazdka_w_pozycyjnych"),
    ],
)
def test_re_compile_ktorego_argumentow_narzedzie_nie_czyta_to_kod_2_z_plikiem_i_linia_a_nie_kod_3(
    tmp_path, capsys, zrodlo, powod
):
    repo = _standardowy(tmp_path, zrodlo, TEST_NIC)
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert blad == f"blad: {SCIEZKA}:3: nie umiem rozebrac wzorca ({powod})\n"
    assert wyjscie == ""


# --- czytanie zmienionego pliku zrodlowego -----------------------------------------------------------------------


def test_zmieniony_plik_bedacy_wiszacym_dowiazaniem_jest_pominiety_a_nie_bledem(tmp_path, capsys):
    baza = {"src/pakiet/__init__.py": "", SCIEZKA: "# modul\n", TEST_MODULU: PRZYPADKI[0].slaby_test}
    repo = _repo(tmp_path, baza)
    (repo / SCIEZKA).unlink()
    (repo / SCIEZKA).symlink_to("nie-ma-takiego-pliku.py")
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert (kod, blad) == (0, "")
    assert wyjscie.splitlines() == ["brak zmienionych funkcji"]


def test_zmieniony_plik_zrodlowy_ktorego_nie_da_sie_odczytac_to_kod_2_ze_sciezka(tmp_path, capsys):
    baza = {"src/pakiet/__init__.py": "", SCIEZKA: "# modul\n", TEST_MODULU: PRZYPADKI[0].slaby_test}
    repo = _repo(tmp_path, baza)
    (repo / SCIEZKA).unlink()
    (repo / SCIEZKA).symlink_to("modul.py")  # dowiazanie do samego siebie: ELOOP, nie brak pliku
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert blad.startswith(f"blad: {SCIEZKA}: nie da sie odczytac: ")
    assert wyjscie == ""


def test_zmieniony_plik_zrodlowy_nie_w_utf8_to_kod_2_ze_sciezka(tmp_path, capsys):
    baza = {"src/pakiet/__init__.py": "", SCIEZKA: "# modul\n", TEST_MODULU: PRZYPADKI[0].slaby_test}
    repo = _repo(tmp_path, baza)
    (repo / SCIEZKA).write_bytes(b"X = '\xff'\n")
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert blad.startswith(f"blad: {SCIEZKA}: nie da sie odczytac: ")
    assert "utf-8" in blad
    assert wyjscie == ""


# --- tabele `[[rownowazny]]`: tylko pliki .toml, katalog nieczytelny to blad -------------------------------------


def test_plik_nie_bedacy_tabela_toml_w_katalogu_tabel_nie_psuje_wyniku(tmp_path, capsys):
    przypadek = PRZYPADKI[0]
    repo = _standardowy(tmp_path, przypadek.zrodlo, przypadek.slaby_test)
    _zapisz(repo, {"tests/mutanty_reczne/README.md": "[[mutant\nzepsute = \n"})
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 1
    assert len(wyjscie.splitlines()) == 2


def test_katalog_tabel_ktory_jest_plikiem_to_kod_2_a_nie_brak_wpisow_rownowaznych(tmp_path, capsys):
    przypadek = PRZYPADKI[0]
    repo = _standardowy(tmp_path, przypadek.zrodlo, przypadek.slaby_test)
    _zapisz(repo, {"tests/mutanty_reczne": "to nie jest katalog\n"})
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert "nie da sie odczytac katalogu tabel" in blad
    assert "tests/mutanty_reczne" in blad
    assert wyjscie == ""


def test_pierwsza_zepsuta_tabela_to_pierwsza_alfabetycznie_nie_pierwsza_z_dysku(tmp_path, monkeypatch):
    for nazwa in ("HAR-1.toml", "HAR-2.toml"):
        _zapisz(tmp_path, {f"tests/mutanty_reczne/{nazwa}": "[[mutant\nzepsute = \n"})
    oryginal = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda self: iter(sorted(oryginal(self), reverse=True)))
    with pytest.raises(mutanty_uzupelniajace.mutanty_reczne.BladNarzedzia) as wyjatek:
        mutanty_uzupelniajace._rownowazne(tmp_path)
    assert "HAR-1.toml" in str(wyjatek.value)
    assert "HAR-2.toml" not in str(wyjatek.value)


# --- przegladanie testow: pliki i katalogi w porzadku alfabetycznym ----------------------------------------------


def _walk_od_konca(katalog, onerror=None):
    """Zamiennik `os.walk`, ktory podaje katalogi i pliki w kolejnosci odwrotnej do alfabetycznej."""
    katalog = Path(katalog)
    podkatalogi = sorted((p.name for p in katalog.iterdir() if p.is_dir()), reverse=True)
    pliki = sorted((p.name for p in katalog.iterdir() if p.is_file()), reverse=True)
    yield str(katalog), podkatalogi, pliki
    for nazwa in podkatalogi:
        yield from _walk_od_konca(katalog / nazwa, onerror)


NIECZYTELNY_TEST = b"\xff\xfe\x00"


def test_pierwszy_nieczytelny_plik_testu_w_katalogu_to_pierwszy_alfabetycznie_nie_pierwszy_z_dysku(
    tmp_path, monkeypatch
):
    _zapisz(tmp_path, {"tests/.keep": ""})
    for nazwa in ("test_a.py", "test_z.py"):
        (tmp_path / "tests" / nazwa).write_bytes(NIECZYTELNY_TEST)
    monkeypatch.setattr(mutanty_uzupelniajace.os, "walk", _walk_od_konca)
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia) as wyjatek:
        mutanty_uzupelniajace._tresci_testow(tmp_path)
    assert "tests/test_a.py" in str(wyjatek.value)
    assert "test_z.py" not in str(wyjatek.value)


def test_nieczytelne_pliki_testow_w_dwoch_podkatalogach_zglasza_pierwszy_alfabetycznie_podkatalog(
    tmp_path, monkeypatch
):
    for podkatalog in ("a", "b"):
        (tmp_path / "tests" / podkatalog).mkdir(parents=True)
        (tmp_path / "tests" / podkatalog / "test_x.py").write_bytes(NIECZYTELNY_TEST)
    monkeypatch.setattr(mutanty_uzupelniajace.os, "walk", _walk_od_konca)
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia) as wyjatek:
        mutanty_uzupelniajace._tresci_testow(tmp_path)
    assert "tests/a/test_x.py" in str(wyjatek.value)
    assert "tests/b/test_x.py" not in str(wyjatek.value)


# --- zapis zrodla w kopii i polecenie pytesta -----------------------------------------------------------------------


def test_zapis_zrodla_do_katalogu_ktorego_nie_ma_w_kopii_to_blad_narzedzia_ze_sciezka(tmp_path):
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia) as wyjatek:
        mutanty_uzupelniajace._zapisz_w_kopii(tmp_path, "nie/ma/katalogu.py", "X = 1\n")
    assert str(wyjatek.value).startswith("nie/ma/katalogu.py: nie da sie zapisac zrodla w kopii: ")


def test_zapis_zrodla_z_samotnym_surrogatem_w_kopii_to_blad_narzedzia_a_nie_wyjatek_unicode(tmp_path):
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia) as wyjatek:
        mutanty_uzupelniajace._zapisz_w_kopii(tmp_path, "modul.py", "X = '" + chr(0xD800) + "'\n")
    assert str(wyjatek.value).startswith("modul.py: nie da sie zapisac zrodla w kopii: ")


def test_zrodlo_w_kopii_jest_zapisywane_w_utf8_a_nie_w_kodowaniu_systemu(tmp_path, monkeypatch):
    kodowania = []
    oryginal = Path.write_text

    def podglad(self, dane, encoding=None, errors=None, newline=None):
        kodowania.append(encoding)
        return oryginal(self, dane, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "write_text", podglad)
    tekst = "X = 'zażółć'\n"
    mutanty_uzupelniajace._zapisz_w_kopii(tmp_path, "modul.py", tekst)
    assert kodowania == ["utf-8"]
    assert (tmp_path / "modul.py").read_bytes() == tekst.encode("utf-8")


def _atrapa_procesu(monkeypatch) -> list[tuple]:
    zlapane: list[tuple] = []

    def atrapa(polecenie, cwd, srodowisko, limit, co):
        zlapane.append((polecenie, cwd, srodowisko, limit, co))
        return 2, ""

    monkeypatch.setattr(mutanty_uzupelniajace.mutanty_reczne, "_uruchom_proces", atrapa)
    return zlapane


def test_pytest_w_kopii_dostaje_polecenie_katalog_limit_i_srodowisko_z_kopia_na_poczatku_sciezki(
    tmp_path, monkeypatch
):
    zlapane = _atrapa_procesu(monkeypatch)
    monkeypatch.setenv("PYTHONPATH", "/obca/sciezka")
    monkeypatch.setenv("ZNACZNIK_SRODOWISKA_TESTU", "tak")
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)  # runner mutantow ustawia ja w srodowisku
    kod = mutanty_uzupelniajace._uruchom_pytest(tmp_path, ["tests/test_a.py", "tests/test_b.py"], 12.5)
    assert kod == 2
    polecenie, cwd, srodowisko, limit, co = zlapane[0]
    assert polecenie == [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
        f"--junitxml={tmp_path / mutanty_uzupelniajace.RAPORT_JUNIT}", "tests/test_a.py", "tests/test_b.py",
    ]  # fmt: skip
    assert cwd == tmp_path
    assert limit == 12.5
    assert co == "testow tests/test_a.py, tests/test_b.py"
    assert srodowisko["PYTHONPATH"] == os.pathsep.join([str(tmp_path / "src"), "/obca/sciezka"])
    assert srodowisko["PYTHONDONTWRITEBYTECODE"] == "1"
    assert srodowisko["ZNACZNIK_SRODOWISKA_TESTU"] == "tak"


def test_pytest_w_kopii_bez_wlasnej_sciezki_pythona_dostaje_sama_kopie_bez_separatora(tmp_path, monkeypatch):
    zlapane = _atrapa_procesu(monkeypatch)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    mutanty_uzupelniajace._uruchom_pytest(tmp_path, [TEST_MODULU], 5.0)
    assert zlapane[0][2]["PYTHONPATH"] == str(tmp_path / "src")


# --- kopie repozytorium: ile ich powstaje i kiedy sa sprzatane ---------------------------------------------------


def _katalog_kopii(tmp_path: Path, monkeypatch) -> tuple[Path, list[str]]:
    katalog = tmp_path / "kopie"
    katalog.mkdir()
    utworzone: list[str] = []
    oryginal = tempfile.mkdtemp

    def mkdtemp(*argumenty, **nazwane):
        sciezka = oryginal(*argumenty, **{**nazwane, "dir": str(katalog)})
        utworzone.append(sciezka)
        return sciezka

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    return katalog, utworzone


def test_kontrola_bez_mutacji_jest_jedna_na_plik_wiec_dwa_pliki_z_dwoma_mutantami_to_szesc_kopii(
    tmp_path, capsys, monkeypatch
):
    katalog, utworzone = _katalog_kopii(tmp_path, monkeypatch)
    test = (
        "from pakiet.a import wczytaj as z_a\nfrom pakiet.z import wczytaj as z_z\n\n\n"
        "def test_obie_wczytuja_liczbe():\n    assert z_a('12') == 12\n    assert z_z('12') == 12\n"
    )
    baza = {"src/pakiet/__init__.py": "", "src/pakiet/a.py": "# a\n", "src/pakiet/z.py": "# z\n", "tests/test_dwa.py": test}
    repo = _repo(tmp_path, baza, {"src/pakiet/a.py": ZRODLO_EXCEPT, "src/pakiet/z.py": ZRODLO_EXCEPT})
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 1
    assert len(wyjscie.splitlines()) == 4
    assert len(utworzone) == 6  # po jednej kontroli i dwa mutanty na plik
    assert list(katalog.iterdir()) == []


def test_kopia_repozytorium_jest_sprzatana_takze_gdy_cialo_bloku_rzuca_wyjatek(tmp_path, monkeypatch):
    katalog, utworzone = _katalog_kopii(tmp_path, monkeypatch)
    monkeypatch.setattr(mutanty_uzupelniajace.mutanty_reczne, "skopiuj_repo", lambda korzen, kopia: None)
    with pytest.raises(RuntimeError, match="awaria w tescie"):
        with mutanty_uzupelniajace._kopia_repo(tmp_path):
            raise RuntimeError("awaria w tescie")
    assert len(utworzone) == 1
    assert list(katalog.iterdir()) == []


def test_kopia_repozytorium_jest_sprzatana_gdy_kopiowanie_plikow_sie_nie_udaje(tmp_path, monkeypatch):
    katalog, utworzone = _katalog_kopii(tmp_path, monkeypatch)

    def skopiuj(korzen, kopia):
        raise mutanty_uzupelniajace.mutanty_reczne.BladNarzedzia("zepsute kopiowanie")

    monkeypatch.setattr(mutanty_uzupelniajace.mutanty_reczne, "skopiuj_repo", skopiuj)
    with pytest.raises(mutanty_uzupelniajace.mutanty_reczne.BladNarzedzia, match="zepsute kopiowanie"):
        with mutanty_uzupelniajace._kopia_repo(tmp_path):
            pass
    assert len(utworzone) == 1
    assert list(katalog.iterdir()) == []


# --- limit czasu: dociera do kazdego uruchomienia pytesta ----------------------------------------------------------


def _rejestruj_limity(monkeypatch) -> list[float]:
    limity: list[float] = []
    oryginal = mutanty_uzupelniajace.mutanty_reczne._uruchom_proces

    def rejestrujacy(polecenie, cwd, srodowisko, limit, co):
        limity.append(limit)
        return oryginal(polecenie, cwd, srodowisko, limit, co)

    monkeypatch.setattr(mutanty_uzupelniajace.mutanty_reczne, "_uruchom_proces", rejestrujacy)
    return limity


def test_limit_z_wiersza_polecen_dociera_do_kontroli_bez_mutacji_i_do_kazdego_mutanta(
    tmp_path, capsys, monkeypatch
):
    limity = _rejestruj_limity(monkeypatch)
    przypadek = PRZYPADKI[0]
    repo = _standardowy(tmp_path, przypadek.zrodlo, przypadek.slaby_test)
    kod, _, _ = _narzedzie(repo, capsys, "--limit", "77.5")
    assert kod == 1
    assert limity == [77.5, 77.5, 77.5]  # kontrola i dwa mutanty


TEST_WOLNY = (
    "import time\nfrom pakiet.modul import wczytaj\n\n\n"
    "def test_wolny_test_konczy_sie_po_limicie():\n    time.sleep(8)\n    assert wczytaj('12') == 12\n"
)


def test_test_ktory_trwa_dluzej_niz_limit_to_kod_2_z_komunikatem_o_przekroczonym_czasie(
    tmp_path, capsys
):
    repo = _standardowy(tmp_path, ZRODLO_EXCEPT, TEST_WOLNY)
    kod, wyjscie, blad = _narzedzie(repo, capsys, "--limit", "1")
    assert kod == 2
    assert f"przekroczony czas 1.0 s dla testow {TEST_MODULU}" in blad
    assert wyjscie == ""


def test_komunikat_czerwonych_testow_bez_mutacji_podaje_kod_pytesta(tmp_path, capsys):
    czerwony = "from pakiet.modul import wczytaj\n\n\ndef test_czerwony():\n    assert False\n"
    repo = _standardowy(tmp_path, ZRODLO_EXCEPT, czerwony)
    kod, _, blad = _narzedzie(repo, capsys)
    assert kod == 2
    assert f"testy {TEST_MODULU} nie przechodza bez mutacji (kod pytesta 1): brak dowodu" in blad


# --- blad opisu i odtworzenia mutanta: kazdy z dwoch wyjatkow, wlasny komunikat -----------------------------------


class _AstZAwariaUnparse:
    """Podstawka za `ast`: `unparse` rzuca wyjatek albo w opisie elementu, albo przy odtwarzaniu mutanta."""

    def __init__(self, wyjatek: type[BaseException], kiedy: str):
        self._wyjatek = wyjatek
        self._kiedy = kiedy
        self._moduly = 0

    def __getattr__(self, nazwa):
        if nazwa != "unparse":
            return getattr(ast, nazwa)

        def unparse(wezel):
            if isinstance(wezel, ast.Module):
                self._moduly += 1
                # pierwszy `unparse` modulu to zrodlo bazowe, drugi to pierwszy mutant
                if self._kiedy == "mutant" and self._moduly == 2:
                    raise self._wyjatek("awaria testowa")
            elif self._kiedy == "opis":
                raise self._wyjatek("awaria testowa")
            return ast.unparse(wezel)

        return unparse


@pytest.mark.parametrize("wyjatek", [RecursionError, MemoryError])
@pytest.mark.parametrize(
    ("kiedy", "etap"),
    [
        pytest.param("opis", "nie da sie opisac mutantow", id="opis_elementu"),
        pytest.param("mutant", "nie da sie odtworzyc mutanta", id="zrodlo_mutanta"),
    ],
)
def test_awaria_unparse_przy_opisie_i_przy_odtwarzaniu_mutanta_to_blad_narzedzia_z_wlasnym_komunikatem(
    monkeypatch, kiedy, etap, wyjatek
):
    monkeypatch.setattr(mutanty_uzupelniajace, "ast", _AstZAwariaUnparse(wyjatek, kiedy))
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia) as blad:
        mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, 'X = {"a": 1, "b": 2}\n', {1})
    assert str(blad.value) == f"{SCIEZKA}: {etap} (ast.unparse): {wyjatek('awaria testowa')!r}"


# --- git: strażnik ls-files dla nowych plikow, plik przeniesiony do src/ -------------------------------------------


def test_nowy_nieczytelny_plik_py_poza_src_jest_pomijany_a_nie_bledem(tmp_path, capsys):
    repo = _repo(tmp_path, {"src/pakiet/__init__.py": ""})
    _zapisz(repo, {"lib/inny.py": "# inny\n"})
    (repo / "lib" / "zly.py").write_bytes(NIECZYTELNY_TEST)
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert (kod, blad) == (0, "")
    assert wyjscie.splitlines() == ["brak zmienionych funkcji"]


ZRODLO_DWIE_FUNKCJE_STRIP = "def a(x):\n    return x.strip()\n\n\ndef b(x):\n    return x.strip()\n"
TEST_DWIE_FUNKCJE_STRIP = (
    "from pakiet.modul import a, b\n\n\n"
    "def test_a_zwraca_bez_zmian_a_b_dopisuje_wykrzyknik():\n"
    "    assert a('x') == 'x'\n"
    "    assert b('x') == 'x!'\n"
)


def test_plik_przeniesiony_spoza_src_do_src_jest_badany_w_calosci_jak_nowy_a_nie_tylko_zmieniona_funkcja(
    tmp_path, capsys
):
    baza = {"src/pakiet/__init__.py": "", "lib/modul.py": ZRODLO_DWIE_FUNKCJE_STRIP, TEST_MODULU: TEST_DWIE_FUNKCJE_STRIP}
    repo = _repo(tmp_path, baza)
    _git(repo, "mv", "lib/modul.py", SCIEZKA)
    _zapisz(repo, {SCIEZKA: ZRODLO_DWIE_FUNKCJE_STRIP.replace("def b(x):\n    return x.strip()", "def b(x):\n    return x.strip() + '!'")})
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 1
    assert wyjscie.splitlines() == [
        f"{SCIEZKA}:2: wywolanie: usunieto wywolanie w x.strip()",
        f"{SCIEZKA}:6: wywolanie: usunieto wywolanie w x.strip()",
    ]


# --- numeracja linii zrodla jest taka sama jak w AST ----------------------------------------------------------

ZRODLO_FORM_FEED = 'import re\n# sekcja\x0c\nA = re.compile(r"^a$")\nB = re.compile(r"^b$")\n'
TEST_FORM_FEED_SLABY = (
    "from pakiet.modul import A, B\n\n\n"
    "def test_oba_wzorce_sa_skompilowane():\n"
    "    assert A.pattern and B.pattern\n"
)


def test_linie_zrodla_dziela_tylko_na_lf_crlf_i_cr():
    assert mutanty_uzupelniajace._linie_zrodla("a\r\nb\rc\nd") == ["a", "b", "c", "d"]


def test_linie_zrodla_nie_dziela_na_znakach_ktorych_ast_nie_liczy_jako_konca_linii():
    zrodlo = "a\x0cb\x1cc\x85d e"
    assert mutanty_uzupelniajace._linie_zrodla(zrodlo) == [zrodlo]


def test_wpis_rownowazny_dla_linii_za_znakiem_form_feed_wycisza_ja_a_nie_nastepna(tmp_path, capsys):
    assert ast.parse(ZRODLO_FORM_FEED).body[1].lineno == 3  # AST nie liczy `\x0c` jako konca linii
    wpis = (SCIEZKA, 'A = re.compile(r"^a$")', _uzasadnienie("regex"))
    repo = _standardowy(tmp_path, ZRODLO_FORM_FEED, TEST_FORM_FEED_SLABY, _tabela(wpis))
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 1
    assert {(p, n, k) for p, n, k in _wyniki(wyjscie)} == {(SCIEZKA, 4, "regex")}


def test_compile_wolane_na_zagniezdzonym_atrybucie_nie_jest_re_compile_i_nie_rzuca_wyjatku():
    zrodlo = 'import a\n\nX = a.b.compile(r"^x$")\n'
    assert mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, zrodlo, {3}) == []


def test_ucieczka_nazwana_konczy_sie_na_swoim_nawiasie_a_nie_na_wczesniejszym():
    wzorzec = r"a{2}\N{LATIN SMALL LETTER A}"
    assert mutanty_uzupelniajace._koniec_ucieczki(wzorzec, wzorzec.index("\\")) == len(wzorzec)


def test_grupa_z_flaga_wielka_litera_we_wzorcu_bajtowym_ma_mutanty_alternatyw():
    assert _wzorce_regex("(?L:ab|cd)", "rb") == _oczekiwane_wzorce("(?L:cd)", "(?L:ab)", przedrostek="rb")


def test_zmiana_w_metodzie_klasy_nie_daje_mutantow_stalej_klasy_ani_linii_klasy_w_kodzie_3():
    # tylko rozwiniecie klasy do jej ciala odroznia metode od klasy jako calosci (stala ROLE, linia `class`)
    zrodlo = ZRODLO_STALA_W_KLASIE
    mutanty, dotkniete, _, _ = mutanty_uzupelniajace._analiza(SCIEZKA, zrodlo, {5})
    assert Counter((m.ksztalt, m.linia) for m in mutanty) == Counter({("wywolanie", 5): 1})
    assert dotkniete == [4]
    bez_ksztaltu = "class Rola:\n    def zapis(self):\n        return 0\n"
    assert mutanty_uzupelniajace._analiza(SCIEZKA, bez_ksztaltu, {3})[1] == [2]


# --- HAR-241: funkcje zdefiniowane w instrukcjach zlozonych modulu i klas --------------------------------------
# Funkcja w `if TYPE_CHECKING:`, `try`, `with`, `for`, `while`, `match` (i w klasie w nich) jest funkcja jak
# kazda inna: dostaje mutanty instrukcji, `finally` i kolejnosci. Wczesniej taka funkcja dawala `zbadano N`
# i kod 0 bez tych ksztaltow (falszywa zielen).

FUNKCJA_Z_FINALLY = (
    "def uzyj(k):\n    try:\n        k.otworz()\n        k.zapisz()\n    finally:\n        k.zamknij()\n"
)


def _wciete(tekst: str, poziom: int) -> str:
    wciecie = "    " * poziom
    return "".join(wciecie + linia if linia.strip() else linia for linia in tekst.splitlines(keepends=True)).rstrip("\n")


def _w_instrukcji(szablon: str, funkcja: str = FUNKCJA_Z_FINALLY) -> str:
    """Wstawia `funkcja` w miejsce znacznika `@N@` (N = poziom wciecia)."""
    for poziom in (1, 2, 3):
        szablon = szablon.replace(f"@{poziom}@", _wciete(funkcja, poziom))
    return szablon + "\n"


SZABLONY_INSTRUKCJI_ZLOZONYCH = [
    pytest.param("if CZY:\n@1@", id="if"),
    pytest.param("if CZY:\n    pass\nelse:\n@1@", id="if_galaz_else"),
    pytest.param("try:\n@1@\nexcept ImportError:\n    pass", id="try_cialo"),
    pytest.param("try:\n    pass\nexcept ImportError:\n@1@", id="except_obsluga"),
    pytest.param("try:\n    pass\nexcept ImportError:\n    pass\nelse:\n@1@", id="try_galaz_else"),
    pytest.param("try:\n    pass\nfinally:\n@1@", id="try_finally"),
    pytest.param("with kontekst():\n@1@", id="with"),
    pytest.param("async with kontekst():\n@1@", id="async_with"),
    pytest.param("for x in xs:\n@1@", id="for"),
    pytest.param("for x in xs:\n    pass\nelse:\n@1@", id="for_galaz_else"),
    pytest.param("async for x in xs:\n@1@", id="async_for"),
    pytest.param("while CZY:\n@1@", id="while"),
    pytest.param("match CZY:\n    case 1:\n@2@", id="match_case"),
    pytest.param("if CZY:\n    class K:\n@2@", id="klasa_w_if"),
    pytest.param("if CZY:\n    try:\n@2@\n    finally:\n        pass", id="dwa_poziomy"),
    pytest.param("if CZY:\n    try:\n        with kontekst():\n@3@\n    finally:\n        pass", id="trzy_poziomy"),
    pytest.param("class K:\n    if CZY:\n@2@", id="if_w_ciele_klasy"),
    pytest.param("class K:\n    if CZY:\n        class L:\n@3@", id="klasa_w_if_w_klasie"),
]


def _mutanty_funkcji_z_finally(zrodlo: str) -> Counter:
    otworz = _linia(zrodlo, "k.otworz()")
    return Counter(
        {
            ("instrukcja", otworz): 1,
            ("instrukcja", _linia(zrodlo, "k.zapisz()")): 1,
            ("instrukcja", _linia(zrodlo, "k.zamknij()")): 1,
            ("kolejnosc", otworz): 1,
        }
    )


@pytest.mark.parametrize("szablon", SZABLONY_INSTRUKCJI_ZLOZONYCH)
def test_funkcja_w_instrukcji_zlozonej_ma_mutanty_instrukcji_finally_i_kolejnosci_kazdy_raz(szablon):
    zrodlo = _w_instrukcji(szablon)
    assert _wszystkie(zrodlo) == _mutanty_funkcji_z_finally(zrodlo)


@pytest.mark.parametrize("szablon", SZABLONY_INSTRUKCJI_ZLOZONYCH)
def test_zmiana_tylko_w_finally_funkcji_w_instrukcji_zlozonej_daje_mutanty_calej_funkcji(szablon):
    zrodlo = _w_instrukcji(szablon)
    linia_finally = _linia(zrodlo, "k.zamknij()")
    assert _licz(zrodlo, {linia_finally}) == _mutanty_funkcji_z_finally(zrodlo)


def test_zmiana_poza_funkcja_w_instrukcji_zlozonej_nie_daje_jej_mutantow():
    zrodlo = _w_instrukcji("if CZY:\n@1@")
    assert _licz(zrodlo, {1}) == Counter()


def test_funkcja_async_w_instrukcji_zlozonej_ma_mutanty_instrukcji():
    zrodlo = "if CZY:\n    async def zamknij(k):\n        await k.zapisz()\n        await k.zamknij()\n"
    assert _wszystkie(zrodlo) == Counter({("instrukcja", 3): 1, ("instrukcja", 4): 1, ("kolejnosc", 3): 1})


def test_naglowek_instrukcji_zlozonej_dalej_daje_mutanty_kolekcji_a_funkcja_w_niej_swoje():
    zrodlo = "if STAN in {1, 2}:\n    def uzyj(k):\n        k.otworz()\n"
    assert _wszystkie(zrodlo) == Counter({("kolekcja", 1): 2, ("instrukcja", 3): 1})
    assert _licz(zrodlo, {1}) == Counter({("kolekcja", 1): 2})
    assert _licz(zrodlo, {3}) == Counter({("kolekcja", 1): 2, ("instrukcja", 3): 1})


def test_krotka_w_funkcji_w_instrukcji_zlozonej_daje_kazdy_mutant_raz_choc_widza_ja_dwa_zakresy():
    # instrukcja `if` (zakres stalej) i funkcja w niej (zakres funkcji) obie obejmuja ta krotke
    zrodlo = "if CZY:\n    def czy(k):\n        return k in (1, 2)\n"
    mutanty = mutanty_uzupelniajace.generuj_mutanty(SCIEZKA, zrodlo, _wszystkie_linie(zrodlo))
    assert Counter((m.ksztalt, m.linia) for m in mutanty) == Counter({("kolekcja", 3): 2})
    assert sorted(_drzewo(m.zrodlo) for m in mutanty) == _drzewa(
        "if CZY:\n    def czy(k):\n        return k in (2,)\n",
        "if CZY:\n    def czy(k):\n        return k in (1,)\n",
    )


@pytest.mark.parametrize("zmieniona", [2, 3, 4])
def test_def_z_dekoratorem_w_instrukcji_zlozonej_zaczyna_sie_od_dekoratora_i_konczy_na_ostatniej_linii(zmieniona):
    zrodlo = "if CZY:\n    @dekorator\n    def uzyj(k):\n        k.otworz()\n"
    assert _licz(zrodlo, {zmieniona}) == Counter({("instrukcja", 4): 1})


def test_linia_przed_dekoratorem_funkcji_w_instrukcji_zlozonej_nie_jest_jej_zakresem():
    zrodlo = "X = 1\nif CZY:\n    @dekorator\n    def uzyj(k):\n        k.otworz()\n"
    assert _licz(zrodlo, {1}) == Counter()


def test_zmiana_w_jednej_funkcji_instrukcji_zlozonej_nie_daje_mutantow_sasiedniej():
    zrodlo = "if CZY:\n    def a(k):\n        k.x()\n\n    def b(k):\n        k.y()\n"
    assert _licz(zrodlo, {3}) == Counter({("instrukcja", 3): 1})
    assert _licz(zrodlo, {6}) == Counter({("instrukcja", 6): 1})


ZRODLO_FUNKCJA_W_FUNKCJI_W_IF = (
    "if CZY:\n    def zewn(k):\n        def wewn(j):\n            j.a()\n        return wewn\n"
)


def test_funkcja_zagniezdzona_w_funkcji_w_instrukcji_zlozonej_nie_jest_osobnym_zakresem():
    zrodlo = ZRODLO_FUNKCJA_W_FUNKCJI_W_IF
    mutanty, dotkniete, _, reczne = mutanty_uzupelniajace._analiza(SCIEZKA, zrodlo, _wszystkie_linie(zrodlo))
    assert Counter((m.ksztalt, m.linia) for m in mutanty) == Counter({("instrukcja", 4): 1})
    assert dotkniete == [1, 2]
    assert reczne == []


def test_dotkniete_zakresy_instrukcji_zlozonej_to_linia_instrukcji_i_linie_startu_jej_funkcji():
    zrodlo = "try:\n    def f():\n        return 0\n\n    @dekorator\n    def g():\n        return 1\nexcept ImportError:\n    pass\n"
    mutanty, dotkniete, _, _ = mutanty_uzupelniajace._analiza(SCIEZKA, zrodlo, _wszystkie_linie(zrodlo))
    assert mutanty == []
    assert dotkniete == [1, 2, 5]


ZRODLO_FUNKCJA_W_TRY = """\
try:
    def uzyj(k):
        k.otworz()
        try:
            return k.czytaj()
        finally:
            k.zamknij()
except ImportError:
    pass


def czy_zapis(rola):
    return rola in ("author", "executor")
"""

KANAL_OTWIERANY = """\
class Kanal:
    def __init__(self):
        self.otwarty = False
        self.zamkniety = False

    def otworz(self):
        self.otwarty = True

    def czytaj(self):
        return "dane"

    def zamknij(self):
        self.zamkniety = True
"""

TEST_FUNKCJA_W_TRY_SLABY = (
    "from pakiet.modul import czy_zapis, uzyj\n\n" + KANAL_OTWIERANY + "\n\n"
    "def test_uzyj_zwraca_dane_kanalu():\n"
    "    assert uzyj(Kanal()) == 'dane'\n\n\n"
    "def test_autor_i_wykonawca_zapisuja_a_recenzent_nie():\n"
    "    assert czy_zapis('author')\n"
    "    assert czy_zapis('executor')\n"
    "    assert not czy_zapis('reviewer')\n"
)
TEST_FUNKCJA_W_TRY_MOCNY = TEST_FUNKCJA_W_TRY_SLABY + (
    "\n\ndef test_uzyj_otwiera_i_zamyka_kanal():\n"
    "    kanal = Kanal()\n"
    "    uzyj(kanal)\n"
    "    assert kanal.otwarty\n"
    "    assert kanal.zamkniety\n"
)


def test_funkcja_w_try_modulu_bez_testu_zamkniecia_daje_kod_1_z_linia_finally_a_nie_zbadano_n(tmp_path, capsys):
    repo = _standardowy(tmp_path, ZRODLO_FUNKCJA_W_TRY, TEST_FUNKCJA_W_TRY_SLABY)
    kod, wyjscie, _ = _narzedzie(repo, capsys)
    assert kod == 1
    assert "zbadano" not in wyjscie
    assert {(p, n, k) for p, n, k in _wyniki(wyjscie)} == {
        (SCIEZKA, _linia(ZRODLO_FUNKCJA_W_TRY, "k.otworz()"), "instrukcja"),
        (SCIEZKA, _linia(ZRODLO_FUNKCJA_W_TRY, "k.zamknij()"), "instrukcja"),
    }


def test_funkcja_w_try_modulu_z_testem_zamkniecia_daje_kod_0_i_liczy_jej_mutanty(tmp_path, capsys):
    repo = _standardowy(tmp_path, ZRODLO_FUNKCJA_W_TRY, TEST_FUNKCJA_W_TRY_MOCNY)
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 0, blad
    assert wyjscie.splitlines() == ["zbadano 4"]  # 2 instrukcje funkcji w try + 2 elementy krotki


def test_funkcja_bez_ksztaltu_w_instrukcji_zlozonej_to_kod_3_z_linia_instrukcji_i_linia_funkcji(tmp_path, capsys):
    zrodlo = "try:\n    def f():\n        return 0\nexcept ImportError:\n    pass\n"
    repo = _standardowy(tmp_path, zrodlo, "def test_x():\n    pass\n")
    kod, wyjscie, blad = _narzedzie(repo, capsys)
    assert kod == 3, blad
    assert wyjscie.splitlines() == [_miejsce(SCIEZKA, 1), _miejsce(SCIEZKA, 2)]


# --- wybor testow modulu: pelna nazwa albo `from pakiet import modul` (PR #206: workflowy_api) ----------


def test_test_z_importem_from_pakiet_import_modul_jest_testem_modulu():
    tresc = "from cockpit.services import workflowy_api\n"
    assert mutanty_uzupelniajace._importuje_modul("tests/t.py", tresc, "cockpit.services.workflowy_api")


def test_test_z_pelna_nazwa_modulu_jest_testem_modulu_bez_parsowania():
    tresc = "import cockpit.services.workflowy_api\ndef (\n"
    assert mutanty_uzupelniajace._importuje_modul("tests/t.py", tresc, "cockpit.services.workflowy_api")


def test_import_tej_samej_nazwy_z_innego_pakietu_nie_jest_testem_modulu():
    tresc = "from cockpit.api import workflowy_api\n"
    assert not mutanty_uzupelniajace._importuje_modul("tests/t.py", tresc, "cockpit.services.workflowy_api")


def test_import_innej_nazwy_z_tego_samego_pakietu_nie_jest_testem_modulu():
    tresc = "from cockpit.services import skille_api\n"
    assert not mutanty_uzupelniajace._importuje_modul("tests/t.py", tresc, "cockpit.services.workflowy_api")


def test_zwykly_import_bez_nazwy_modulu_nie_jest_testem_modulu():
    tresc = "import os\n"
    assert not mutanty_uzupelniajace._importuje_modul("tests/t.py", tresc, "cockpit.services.workflowy_api")


def test_niesparsowalny_test_bez_pelnej_nazwy_to_blad_narzedzia_z_nazwa_pliku():
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia, match=r"^tests/t\.py: nie da sie sparsowac testu"):
        mutanty_uzupelniajace._importuje_modul("tests/t.py", "def (\n", "cockpit.services.workflowy_api")


def test_main_bierze_test_z_importem_from_pakiet_import_modul_zamiast_bledu_zero_testow(tmp_path):
    baza = {
        "src/pakiet/__init__.py": "",
        "src/pakiet/m00.py": "# modul\n",
        "tests/test_m00.py": "from pakiet import m00\n\n\ndef test_x():\n    assert m00\n",
    }
    repo = _repo(tmp_path, baza, {"src/pakiet/m00.py": ZRODLO_EXCEPT})
    proces = uruchom_proces_sync(
        [
            sys.executable, str(KORZEN / "tools" / "mutanty_uzupelniajace.py"),
            "--korzen", str(repo), "--baza", "HEAD", "--limit", LIMIT_TESTU_S,
        ],  # fmt: skip
        cwd=KORZEN,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wyjscie, blad = proces.communicate(timeout=300)
    finally:
        zakoncz_proces_sync(proces, nazwa="mutanty uzupelniajace z importem from")
    assert "zero zebranych testow" not in blad
    assert proces.returncode in (0, 1), blad
    assert wyjscie != ""



def test_ValueError_parsera_testu_to_blad_narzedzia_z_nazwa_pliku(monkeypatch):
    def parser(*_args, **_kwargs):
        raise ValueError("atrapa")

    monkeypatch.setattr(mutanty_uzupelniajace.ast, "parse", parser)
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia, match=r"^tests/t\.py: nie da sie sparsowac testu"):
        mutanty_uzupelniajace._importuje_modul("tests/t.py", "import os\n", "cockpit.services.workflowy_api")



def test_RecursionError_parsera_testu_to_blad_narzedzia_z_nazwa_pliku(monkeypatch):
    def parser(*_args, **_kwargs):
        raise RecursionError("atrapa")

    monkeypatch.setattr(mutanty_uzupelniajace.ast, "parse", parser)
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia, match=r"^tests/t\.py: nie da sie sparsowac testu"):
        mutanty_uzupelniajace._importuje_modul("tests/t.py", "import os\n", "cockpit.services.workflowy_api")



def test_MemoryError_parsera_testu_to_blad_narzedzia_z_nazwa_pliku(monkeypatch):
    def parser(*_args, **_kwargs):
        raise MemoryError("atrapa")

    monkeypatch.setattr(mutanty_uzupelniajace.ast, "parse", parser)
    with pytest.raises(mutanty_uzupelniajace.BladNarzedzia, match=r"^tests/t\.py: nie da sie sparsowac testu"):
        mutanty_uzupelniajace._importuje_modul("tests/t.py", "import os\n", "cockpit.services.workflowy_api")
