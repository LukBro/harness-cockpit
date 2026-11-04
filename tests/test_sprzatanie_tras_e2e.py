"""Sprzątanie podstawionych tras Playwrighta w testach e2e (HAR-185).

Zamknięcie kontekstu przeglądarki przy handlerze trasy w locie (`route.fetch()`) kończyło
się `TargetClosedError` w następnym teście. Trasy schodzą więc z każdej strony i z kontekstu
tuż przed jego zamknięciem. Testy chodzą bez przeglądarki i bez PostgreSQL: pomocnik dostaje
atrapy kontekstu, a fixture'y z prawdziwych plików e2e biegną w zagnieżdżonym przebiegu pytestera
z atrapą przeglądarki.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from playwright._impl._errors import TargetClosedError
from playwright.sync_api import Error as BladPlaywrighta

KORZEN = Path(__file__).resolve().parents[1]
CONFTEST_E2E = KORZEN / "tests" / "e2e" / "conftest.py"
TESTY_LOGOWANIA = KORZEN / "tests" / "e2e" / "test_logowanie_w_przegladarce.py"
PROCESY = KORZEN / "tests" / "procesy.py"


def _pomocnik():
    spec = importlib.util.spec_from_file_location("conftest_e2e_sprzatanie_tras", CONFTEST_E2E)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul.zdejmij_podstawione_trasy_z_kontekstu


class AtrapaCelu:
    """Strona albo kontekst: rejestruje wywołania `unroute_all` (tylko argumenty nazwane)."""

    def __init__(self, nazwa: str, rejestr: list, blad: Exception | None = None):
        self.nazwa = nazwa
        self.rejestr = rejestr
        self.blad = blad
        self.pages: list[AtrapaCelu] = []

    def unroute_all(self, *, behavior=None):
        self.rejestr.append((self.nazwa, behavior))
        if self.blad is not None:
            raise self.blad


def _kontekst_ze_stronami(rejestr: list, *, blad_strony: Exception | None = None):
    kontekst = AtrapaCelu("kontekst", rejestr)
    kontekst.pages = [
        AtrapaCelu("strona-1", rejestr, blad_strony),
        AtrapaCelu("strona-2", rejestr),
    ]
    return kontekst


def test_trasy_schodza_z_kazdej_strony_kontekstu():
    rejestr: list = []
    _pomocnik()(_kontekst_ze_stronami(rejestr))
    nazwy = [nazwa for nazwa, _ in rejestr]
    assert "strona-1" in nazwy
    assert "strona-2" in nazwy


def test_trasy_schodza_takze_z_samego_kontekstu():
    rejestr: list = []
    _pomocnik()(_kontekst_ze_stronami(rejestr))
    assert ("kontekst", "ignoreErrors") in rejestr


def test_kontekst_bez_stron_tez_jest_sprzatany():
    rejestr: list = []
    kontekst = AtrapaCelu("kontekst", rejestr)
    _pomocnik()(kontekst)
    assert rejestr == [("kontekst", "ignoreErrors")]


def test_trasy_schodza_bez_czekania_na_handlery_w_locie_i_ich_bledy():
    rejestr: list = []
    _pomocnik()(_kontekst_ze_stronami(rejestr))
    assert rejestr == [
        ("strona-1", "ignoreErrors"),
        ("strona-2", "ignoreErrors"),
        ("kontekst", "ignoreErrors"),
    ]


def test_zamknieta_strona_nie_przerywa_sprzatania_reszty():
    rejestr: list = []
    kontekst = _kontekst_ze_stronami(rejestr, blad_strony=TargetClosedError())
    _pomocnik()(kontekst)
    assert [nazwa for nazwa, _ in rejestr] == ["strona-1", "strona-2", "kontekst"]


def test_kontekst_zamkniety_wczesniej_przez_test_nie_wywala_sprzatania():
    rejestr: list = []
    kontekst = AtrapaCelu("kontekst", rejestr, TargetClosedError())
    _pomocnik()(kontekst)
    assert rejestr == [("kontekst", "ignoreErrors")]


def test_blad_inny_niz_playwrightowy_przy_sprzataniu_leci_dalej():
    rejestr: list = []
    kontekst = _kontekst_ze_stronami(rejestr, blad_strony=ValueError("to nie zamkniecie kontekstu"))
    with pytest.raises(ValueError, match="to nie zamkniecie kontekstu"):
        _pomocnik()(kontekst)


def test_inny_blad_playwrighta_niz_zamkniecie_celu_przy_sprzataniu_leci_dalej():
    rejestr: list = []
    kontekst = _kontekst_ze_stronami(rejestr, blad_strony=BladPlaywrighta("blad protokolu"))
    with pytest.raises(BladPlaywrighta, match="blad protokolu") as info:
        _pomocnik()(kontekst)
    assert type(info.value) is BladPlaywrighta
    assert [nazwa for nazwa, _ in rejestr] == ["strona-1"]


# --- fixture'y z prawdziwych plików e2e, w zagnieżdżonym przebiegu pytestera -------------------

ATRAPY_PLUGINU = '''
import json
from pathlib import Path

import pytest

REJESTR = []


class Cel:
    def __init__(self, nazwa):
        self.nazwa = nazwa

    def unroute_all(self, *, behavior=None):
        REJESTR.append(f"unroute_all({self.nazwa}, {behavior})")


class Kontekst(Cel):
    def __init__(self):
        super().__init__("kontekst")
        self.pages = [Cel("strona-1"), Cel("strona-2")]

    def close(self):
        REJESTR.append("close")


class Przegladarka:
    def new_context(self, **opcje):
        return Kontekst()


def _zapisz():
    Path("rejestr.json").write_text(json.dumps(REJESTR))


@pytest.fixture
def rejestr():
    return REJESTR


@pytest.fixture
def context():
    # jak plugin pytest-playwright: zamyka kontekst w swoim teardownie
    kontekst = Kontekst()
    yield kontekst
    kontekst.close()
    _zapisz()


@pytest.fixture
def browser():
    yield Przegladarka()
    _zapisz()
'''

SPRZATANE_TRASY = [
    "unroute_all(strona-1, ignoreErrors)",
    "unroute_all(strona-2, ignoreErrors)",
    "unroute_all(kontekst, ignoreErrors)",
]


def _przygotuj_katalog_e2e(pytester, test_probka: str, *, dopisz_do: Path | None = None) -> None:
    pytester.makeconftest(ATRAPY_PLUGINU)
    pytester.makepyfile(procesy=PROCESY.read_text(encoding="utf-8"))
    e2e = pytester.mkdir("e2e")
    (e2e / "conftest.py").write_text(CONFTEST_E2E.read_text(encoding="utf-8"), encoding="utf-8")
    zrodlo = dopisz_do.read_text(encoding="utf-8") if dopisz_do else ""
    (e2e / "test_probka.py").write_text(zrodlo + "\n\n" + test_probka, encoding="utf-8")


def _rejestr(pytester) -> list[str]:
    return json.loads((pytester.path / "rejestr.json").read_text(encoding="utf-8"))


def test_fixture_context_zdejmuje_trasy_po_tescie_a_przed_zamknieciem_kontekstu(pytester):
    _przygotuj_katalog_e2e(
        pytester,
        "def test_probka(context, rejestr):\n"
        "    rejestr.append('test')\n",
    )
    wynik = pytester.runpytest(
        "e2e/test_probka.py", "-p", "no:cacheprovider", "-p", "no:playwright"
    )
    wynik.assert_outcomes(passed=1)
    assert _rejestr(pytester) == ["test", *SPRZATANE_TRASY, "close"]


def test_fabryka_kontekstow_zdejmuje_trasy_przed_zamknieciem_kazdego_kontekstu(pytester):
    _przygotuj_katalog_e2e(
        pytester,
        "def test_probka_fabryki(otworz_kontekst):\n"
        "    otworz_kontekst()\n",
        dopisz_do=TESTY_LOGOWANIA,
    )
    wynik = pytester.runpytest(
        "e2e/test_probka.py::test_probka_fabryki", "-p", "no:cacheprovider", "-p", "no:playwright"
    )
    wynik.assert_outcomes(passed=1)
    assert _rejestr(pytester) == [*SPRZATANE_TRASY, "close"]
