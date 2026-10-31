"""Reguly architektury sprawdzane testem, nie dyscyplina."""
import ast
import importlib.util
import sys
from pathlib import Path

import pytest

from cockpit.services.invariants import WSZYSTKIE, Niezmiennik
from cockpit.services.walidacja import EGZEKWOWANE, SPRAWDZENIA

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from check_layers import sprawdz  # noqa: E402


def test_warstwa_api_nie_dotyka_bazy():
    assert sprawdz() == []


def test_lista_niezmiennikow_ma_siedem_pozycji_i_kazda_ma_sprawdzenie():
    """Dopisanie niezmiennika bez sprawdzenia w walidatorze ma wywalic test."""
    assert len(WSZYSTKIE) == 7
    assert Niezmiennik.measured_or_null in WSZYSTKIE
    assert Niezmiennik.evidence_required in WSZYSTKIE
    assert set(SPRAWDZENIA) == WSZYSTKIE
    for niezmiennik, sprawdzenia in SPRAWDZENIA.items():
        assert len(sprawdzenia) > 0, f"niezmiennik {niezmiennik} nie ma zadnego sprawdzenia"


# --- Egzekucja niezmiennikow: rejestr EGZEKWOWANE a faktyczne wywolania ---

_ZAGNIEZDZENIA = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _wolane_nazwy(zrodlo: str, funkcja: str) -> set[str]:
    """Nazwy (ast.Name) wolane w ciele funkcji modulu, wykonywane przez samo jej wywolanie.

    Zagniezdzone def/async def/lambda/class sa pomijane. Wywolania przez atrybut
    (`modul.funkcja(...)`) nie sa obslugiwane: kod produkcyjny importuje nazwy wprost.
    Brak funkcji na poziomie modulu to LookupError, nie pusty wynik.
    """
    drzewo = ast.parse(zrodlo)
    cel = next(
        (
            w
            for w in drzewo.body
            if isinstance(w, ast.FunctionDef | ast.AsyncFunctionDef) and w.name == funkcja
        ),
        None,
    )
    if cel is None:
        raise LookupError(f"brak funkcji {funkcja} w module")
    nazwy: set[str] = set()
    stos: list[ast.AST] = list(cel.body)
    while stos:
        wezel = stos.pop()
        if isinstance(wezel, _ZAGNIEZDZENIA):
            continue
        if isinstance(wezel, ast.Call) and isinstance(wezel.func, ast.Name):
            nazwy.add(wezel.func.id)
        stos.extend(ast.iter_child_nodes(wezel))
    return nazwy


def _zrodlo_punktu(punkt: str) -> tuple[str, str]:
    """Rozbija "modul:funkcja" i czyta zrodlo modulu tam, gdzie lezy (tez pod mutmutem)."""
    modul, dwukropek, funkcja = punkt.partition(":")
    if not dwukropek or not modul or not funkcja:
        raise ValueError(f"punkt wywolania {punkt!r} nie ma formatu modul:funkcja")
    spec = importlib.util.find_spec(modul)
    if spec is None or spec.origin is None:
        raise LookupError(f"nie znaleziono modulu {modul}")
    return Path(spec.origin).read_text(encoding="utf-8"), funkcja


def _wola_sprawdzenie(punkt: str, niezmiennik: Niezmiennik) -> bool:
    zrodlo, funkcja = _zrodlo_punktu(punkt)
    oczekiwane = {s.__name__ for s in SPRAWDZENIA[niezmiennik]}
    return bool(_wolane_nazwy(zrodlo, funkcja) & oczekiwane)


def test_funkcja_z_wywolaniem_sprawdzenia_egzekwuje_niezmiennik():
    assert "sprawdz" in _wolane_nazwy("def f(x):\n    sprawdz(x)\n", "f")


def test_funkcja_bez_wywolania_sprawdzenia_nie_egzekwuje_niezmiennika():
    assert "sprawdz" not in _wolane_nazwy("def f(x):\n    return x\n", "f")


def test_wywolanie_innej_funkcji_nie_liczy_sie_jako_sprawdzenie():
    assert _wolane_nazwy("def f(x):\n    inna(x)\n", "f") == {"inna"}


def test_samo_odwolanie_do_sprawdzenia_bez_wywolania_nie_egzekwuje_niezmiennika():
    assert "sprawdz" not in _wolane_nazwy("def f(x):\n    g = sprawdz\n    return g\n", "f")


def test_funkcja_asynchroniczna_tez_jest_punktem_wywolania():
    assert "sprawdz" in _wolane_nazwy("async def f(x):\n    sprawdz(x)\n", "f")


def test_wywolanie_w_instrukcjach_sterujacych_jest_wykryte():
    zrodlo = "def f(x):\n    if x:\n        for i in x:\n            sprawdz(i)\n"
    assert "sprawdz" in _wolane_nazwy(zrodlo, "f")


def test_wywolanie_w_zagniezdzonej_funkcji_nie_egzekwuje_niezmiennika():
    zrodlo = "def f(x):\n    def g():\n        sprawdz(x)\n    return g\n"
    assert "sprawdz" not in _wolane_nazwy(zrodlo, "f")


def test_wywolanie_w_zagniezdzonej_funkcji_asynchronicznej_nie_egzekwuje_niezmiennika():
    zrodlo = "def f(x):\n    async def g():\n        sprawdz(x)\n    return g\n"
    assert "sprawdz" not in _wolane_nazwy(zrodlo, "f")


def test_wywolanie_w_lambdzie_nie_egzekwuje_niezmiennika():
    assert "sprawdz" not in _wolane_nazwy("def f(x):\n    return lambda: sprawdz(x)\n", "f")


def test_wywolanie_w_ciele_klasy_zagniezdzonej_nie_egzekwuje_niezmiennika():
    zrodlo = "def f(x):\n    class K:\n        y = sprawdz(x)\n    return K\n"
    assert "sprawdz" not in _wolane_nazwy(zrodlo, "f")


def test_brak_wskazanej_funkcji_w_module_jest_bledem_a_nie_brakiem_wywolania():
    with pytest.raises(LookupError, match="brak funkcji f"):
        _wolane_nazwy("def inna(x):\n    sprawdz(x)\n", "f")


def test_funkcja_zdefiniowana_tylko_wewnatrz_innej_nie_jest_punktem_wywolania():
    zrodlo = "def inna(x):\n    def f():\n        sprawdz(x)\n"
    with pytest.raises(LookupError, match="brak funkcji f"):
        _wolane_nazwy(zrodlo, "f")


def test_punkt_wywolania_bez_dwukropka_jest_bledem_formatu():
    with pytest.raises(ValueError, match="modul:funkcja"):
        _zrodlo_punktu("cockpit.services.runs")


def test_punkt_wywolania_bez_nazwy_funkcji_jest_bledem_formatu():
    with pytest.raises(ValueError, match="modul:funkcja"):
        _zrodlo_punktu("cockpit.services.runs:")


def test_punkt_wywolania_bez_nazwy_modulu_jest_bledem_formatu():
    with pytest.raises(ValueError, match="modul:funkcja"):
        _zrodlo_punktu(":zapisz_zdarzenie")


def test_nieistniejacy_modul_w_punkcie_wywolania_jest_bledem_a_nie_brakiem_wywolania():
    with pytest.raises(LookupError, match="nie znaleziono modulu"):
        _zrodlo_punktu("cockpit.services.nie_ma_takiego_modulu:f")


def test_pakiet_bez_pliku_zrodlowego_w_punkcie_wywolania_jest_bledem_a_nie_brakiem_wywolania(
    tmp_path, monkeypatch
):
    (tmp_path / "pakiet_przestrzeni_nazw_har15").mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    spec = importlib.util.find_spec("pakiet_przestrzeni_nazw_har15")
    assert spec is not None and spec.origin is None  # warunek wejscia: spec jest, origin None
    with pytest.raises(LookupError, match="nie znaleziono modulu"):
        _zrodlo_punktu("pakiet_przestrzeni_nazw_har15:f")


def test_punkt_wolajacy_sprawdzenie_innego_niezmiennika_nie_egzekwuje_go():
    """zapisz_zdarzenie wola sprawdz_pomiar (measured_or_null), nie sprawdzen reviewer_readonly."""
    punkt = "cockpit.services.runs:zapisz_zdarzenie"
    assert _wola_sprawdzenie(punkt, Niezmiennik.measured_or_null)
    assert not _wola_sprawdzenie(punkt, Niezmiennik.reviewer_readonly)
    assert not _wola_sprawdzenie(punkt, Niezmiennik.approval_required)


def test_rejestr_egzekwowanych_niezmiennikow_nie_jest_pusty_i_zawiera_pomiar_lub_null():
    """Pusty rejestr nie moze dac zielonego testu parametryzowanego (evidence_required)."""
    assert Niezmiennik.measured_or_null in EGZEKWOWANE


def test_egzekwowane_niezmienniki_naleza_do_znanych_i_maja_punkty_wywolania():
    assert set(EGZEKWOWANE) <= WSZYSTKIE
    for niezmiennik, punkty in EGZEKWOWANE.items():
        assert len(punkty) > 0, f"niezmiennik {niezmiennik} jest egzekwowany bez punktu wywolania"
        for punkt in punkty:
            modul, dwukropek, funkcja = punkt.partition(":")
            assert dwukropek and modul and funkcja, (
                f"niezmiennik {niezmiennik}: punkt {punkt!r} nie ma formatu modul:funkcja"
            )


@pytest.mark.parametrize(
    ("niezmiennik", "punkt"),
    [(n, p) for n, punkty in EGZEKWOWANE.items() for p in punkty],
)
def test_kazdy_egzekwowany_niezmiennik_ma_wywolanie_sprawdzenia_na_sciezce_wykonania(
    niezmiennik: Niezmiennik, punkt: str
):
    assert _wola_sprawdzenie(punkt, niezmiennik), (
        f"niezmiennik {niezmiennik.name} jest zadeklarowany jako egzekwowany, "
        f"ale {punkt} nie wola zadnego z jego sprawdzen"
    )
