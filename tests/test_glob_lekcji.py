"""HAR-169: glob `ścieżki=` lekcji zamieniany na wyrazenie regularne (`runner.kontekst`).

Kazdy znak specjalny globu (`*`, `**`, `**/`, `?`) i kazdy znak specjalny wyrazenia (`.`, `+`, `(`)
ma osobne wejscie, na ktorym tylko on decyduje o dopasowaniu. Zepsuty krok petli (`i -= 2`) nie
konczy parsowania, wiec dopasowanie ma twardy limit czasu (SIGALRM), a nie wisi w nieskonczonosc.
"""
from __future__ import annotations

import signal

import pytest

from runner.kontekst import _glob_na_regex

LIMIT_S = 0.5


class GlobNieKonczySieKompilowac(Exception):
    pass


def pasuje(glob: str, sciezka: str) -> bool:
    def przekroczono(_numer, _ramka):
        raise GlobNieKonczySieKompilowac(glob)

    poprzedni = signal.signal(signal.SIGALRM, przekroczono)
    signal.setitimer(signal.ITIMER_REAL, LIMIT_S)
    try:
        return _glob_na_regex(glob).fullmatch(sciezka) is not None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, poprzedni)


PRZYPADKI = [
    # `**/` = dowolna liczba katalogow, takze zero
    pytest.param("docs/**/x.md", "docs/x.md", True, id="dwie-gwiazdki-ukosnik-zero-katalogow"),
    pytest.param("docs/**/x.md", "docs/a/b/x.md", True, id="dwie-gwiazdki-ukosnik-wiele-katalogow"),
    pytest.param("docs/**/x.md", "docs/ax.md", False, id="dwie-gwiazdki-ukosnik-wymaga-ukosnika"),
    pytest.param("**/x.md", "x.md", True, id="dwie-gwiazdki-ukosnik-na-poczatku-zero-katalogow"),
    pytest.param("**/x.md", "a/b/x.md", True, id="dwie-gwiazdki-ukosnik-na-poczatku-katalogi"),
    pytest.param("tests/**/*.py", "tests/test_x.py", True, id="katalogi-zero-i-gwiazdka-pliku"),
    pytest.param("tests/**/*.py", "tests/unit/test_x.py", True, id="katalogi-jeden-i-gwiazdka-pliku"),
    pytest.param("tests/**/*.py", "src/test_x.py", False, id="katalogi-inny-prefiks"),
    # `**` bez ukosnika przekracza katalogi
    pytest.param("docs/**", "docs/a/b.md", True, id="dwie-gwiazdki-na-koncu-przekraczaja-katalogi"),
    pytest.param("docs/**", "src/a.py", False, id="dwie-gwiazdki-na-koncu-zachowuja-prefiks"),
    pytest.param("a**b", "a/x/b", True, id="dwie-gwiazdki-w-srodku-przekraczaja-katalogi"),
    pytest.param("a**b", "ac", False, id="dwie-gwiazdki-w-srodku-wymagaja-reszty-globu"),
    # `*` nie przekracza `/`
    pytest.param("a*b", "axb", True, id="gwiazdka-dopasowuje-dowolne-znaki"),
    pytest.param("a*b", "ac", False, id="gwiazdka-wymaga-reszty-globu"),
    pytest.param("a*b", "a/b", False, id="gwiazdka-nie-przekracza-ukosnika"),
    pytest.param("*.py", "a/b.py", False, id="gwiazdka-na-poczatku-nie-przekracza-ukosnika"),
    # `?` to dokladnie jeden znak poza `/`
    pytest.param("a?c", "abc", True, id="znak-zapytania-dopasowuje-jeden-znak"),
    pytest.param("a?c", "ac", False, id="znak-zapytania-wymaga-znaku"),
    pytest.param("a?c", "abbc", False, id="znak-zapytania-to-tylko-jeden-znak"),
    pytest.param("a?c", "a/c", False, id="znak-zapytania-nie-przekracza-ukosnika"),
    pytest.param("a?c", "xbc", False, id="znak-zapytania-zachowuje-prefiks"),
    # znaki specjalne wyrazenia regularnego sa literalami
    pytest.param("a.b", "a.b", True, id="kropka-dopasowuje-kropke"),
    pytest.param("a.b", "axb", False, id="kropka-nie-jest-dowolnym-znakiem"),
    pytest.param("a+b", "a+b", True, id="plus-dopasowuje-plus"),
    pytest.param("a+b", "aab", False, id="plus-nie-powtarza-znaku"),
    pytest.param("f(x)", "f(x)", True, id="nawias-dopasowuje-nawias"),
    pytest.param("f(x)", "fx", False, id="nawias-nie-tworzy-grupy"),
]


@pytest.mark.parametrize(("glob", "sciezka", "oczekiwane"), PRZYPADKI)
def test_glob_lekcji_dopasowuje_sciezke_zgodnie_ze_znaczeniem_kazdego_znaku_specjalnego(
    glob, sciezka, oczekiwane,
):
    assert pasuje(glob, sciezka) is oczekiwane
