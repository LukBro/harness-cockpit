"""Mutanty zmienionych funkcji przed oddaniem pracy (HAR-133).

Skrypt `tools/mutanty_zmian.py` mapuje diff względem bazy na wzorce mutantów
(tylko funkcje i metody, które zmieniono), uruchamia na nich mutmut i zgłasza
przeżywające mutanty. Testy nie uruchamiają ani gita, ani mutmuta: diff,
źródła i wyniki mutmuta są daną w `tmp_path`, a `uruchom` jest podmieniane.
"""
import contextlib
import fnmatch
import io
import json
import os
import re
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from procesy import czy_zyje, uruchom_proces_sync, zakoncz_proces_sync

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import mutanty_zmian  # noqa: E402

# Zabezpieczenie przed zawieszeniem, nie oczekiwany czas: fork/exec gita pod obciazeniem
# rownoleglych sesji trwa dziesiatki sekund (HAR-236, runda 3).
LIMIT_PROCESU_POMOCNICZEGO = 180

BladNarzedzia = mutanty_zmian.BladNarzedzia
# Prawdziwe `uruchom` z chwili importu: testy trybu przyrostowego puszczaja przez nie gita,
# a podmieniaja tylko `python -m mutmut`.
PRAWDZIWE_URUCHOM = mutanty_zmian.uruchom
ZNAK = "\u01c1"  # separator klasy i metody w nazwach mutantów mutmuta

# Linie: 1 stała, 4-6 zapisz, 9-10 zapisz_wiele.
DWIE_FUNKCJE = """\
X = 1


def zapisz():
    a = 1
    return a


def zapisz_wiele():
    return 2
"""
SCIEZKA = "src/cockpit/services/runs.py"
MODUL = "cockpit.services.runs"


def _wzorce(zrodlo: str, *linie: int, sciezka: str = SCIEZKA) -> list[str]:
    return mutanty_zmian.wzorce_dla_pliku(sciezka, zrodlo, set(linie))


# --- modul_ze_sciezki -------------------------------------------------------


@pytest.mark.parametrize(
    ("sciezka", "oczekiwany"),
    [
        pytest.param("src/cockpit/services/runs.py", "cockpit.services.runs", id="zwykly_modul"),
        pytest.param("src/runner/__init__.py", "runner", id="init_pakietu_to_pakiet"),
        pytest.param(
            "src/cockpit/services/__init__.py", "cockpit.services", id="init_zagniezdzonego_pakietu"
        ),
        pytest.param("src/runner/src/x.py", "runner.src.x", id="src_zdejmowane_tylko_na_poczatku"),
        pytest.param("tools/x.py", "tools.x", id="bez_prefiksu_src"),
    ],
)
def test_nazwa_modulu_ze_sciezki_pliku_jak_u_mutmuta(sciezka, oczekiwany):
    assert mutanty_zmian.modul_ze_sciezki(sciezka) == oczekiwany


# --- zmienione_linie --------------------------------------------------------


def _diff(plik: str, *hunki: str) -> str:
    return f"diff --git a/{plik} b/{plik}\n--- a/{plik}\n+++ b/{plik}\n" + "".join(
        f"{h}\n+x\n" for h in hunki
    )


@pytest.mark.parametrize(
    ("hunk", "oczekiwane"),
    [
        pytest.param("@@ -1,2 +5,3 @@", {5, 6, 7}, id="hunk_z_licznikiem"),
        pytest.param("@@ -1 +8 @@", {8}, id="hunk_bez_licznika_to_jedna_linia"),
        pytest.param("@@ -3,2 +2,0 @@", set(), id="czyste_usuniecie_nie_daje_linii"),
        pytest.param("@@ -3 +9,1 @@", {9}, id="licznik_jeden_to_jedna_linia"),
    ],
)
def test_naglowek_hunka_daje_zmienione_linie_strony_nowej(hunk, oczekiwane):
    zmiany = mutanty_zmian.zmienione_linie(_diff("a.py", hunk))
    assert zmiany.get("a.py", set()) == oczekiwane


def test_kilka_hunkow_jednego_pliku_sumuje_linie():
    zmiany = mutanty_zmian.zmienione_linie(_diff("a.py", "@@ -1 +2 @@", "@@ -9 +20,2 @@"))
    assert zmiany["a.py"] == {2, 20, 21}


def test_kilka_plikow_w_diffie_ma_osobne_zbiory_linii():
    diff = _diff("a.py", "@@ -1 +2 @@") + _diff("b/c.py", "@@ -1 +7,2 @@")
    zmiany = mutanty_zmian.zmienione_linie(diff)
    assert zmiany["a.py"] == {2}
    assert zmiany["b/c.py"] == {7, 8}


@pytest.mark.parametrize(
    "dodana_linia",
    [
        pytest.param("+++ b/zly.py", id="tresc_wyglada_jak_naglowek_nowego_pliku"),
        pytest.param("+++ /dev/null", id="tresc_wyglada_jak_naglowek_usunietego_pliku"),
        pytest.param("+++ w/inny.py", id="tresc_wyglada_jak_naglowek_z_obcym_prefiksem"),
    ],
)
def test_dodana_linia_zaczynajaca_sie_od_plusow_nie_zmienia_biezacego_pliku(dodana_linia):
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        f"@@ -1 +2 @@\n{dodana_linia}\n"
        "@@ -9 +20 @@\n+x\n"
    )
    zmiany = mutanty_zmian.zmienione_linie(diff)
    assert zmiany == {"a.py": {2, 20}}


def test_plik_usuniety_w_diffie_jest_pominiety():
    diff = (
        "diff --git a/stary.py b/stary.py\n--- a/stary.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n"
        + _diff("a.py", "@@ -1 +2 @@")
    )
    zmiany = mutanty_zmian.zmienione_linie(diff)
    assert zmiany.get("stary.py", set()) == set()
    assert "/dev/null" not in zmiany
    assert zmiany["a.py"] == {2}


def test_plik_usuniety_z_niezerowym_hunkiem_strony_nowej_nie_daje_klucza_ani_bledu():
    diff = "diff --git a/stary.py b/stary.py\n--- a/stary.py\n+++ /dev/null\n@@ -1,2 +1 @@\n-a\n"
    zmiany = mutanty_zmian.zmienione_linie(diff)
    assert zmiany == {}


@pytest.mark.parametrize(
    "naglowek",
    [
        pytest.param("+++ w/src/x.py", id="prefiks_w_z_mnemonicPrefix"),
        pytest.param("+++ src/x.py", id="brak_prefiksu_z_noprefix"),
    ],
)
def test_naglowek_pliku_z_obcym_prefiksem_to_blad_narzedzia_nie_ciche_pominiecie(naglowek):
    diff = f"diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n{naglowek}\n@@ -1 +2 @@\n+x\n"
    with pytest.raises(BladNarzedzia):
        mutanty_zmian.zmienione_linie(diff)


# --- uruchom (prawdziwy proces, bez gita i mutmuta) -------------------------


def test_brak_binarki_do_uruchomienia_to_blad_narzedzia(tmp_path):
    with pytest.raises(BladNarzedzia):
        mutanty_zmian.uruchom([str(tmp_path / "nie_ma_takiej_binarki")])


def test_uruchom_zwraca_wyjscie_i_kod_procesu():
    wynik = mutanty_zmian.uruchom([sys.executable, "-c", "print('ok')"])
    assert wynik.returncode == 0
    assert wynik.stdout == "ok\n"


def test_uruchom_nie_rzuca_przy_niezerowym_kodzie_procesu():
    wynik = mutanty_zmian.uruchom([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert wynik.returncode == 3


# --- wzorce_dla_pliku -------------------------------------------------------


def test_zmiana_w_ciele_funkcji_modulu_daje_wzorzec_tylko_tej_funkcji():
    assert _wzorce(DWIE_FUNKCJE, 5) == [f"{MODUL}.x_zapisz__mutmut_*"]


def test_zmiana_w_drugiej_funkcji_pliku_nie_dolacza_pierwszej():
    assert _wzorce(DWIE_FUNKCJE, 10) == [f"{MODUL}.x_zapisz_wiele__mutmut_*"]


def test_zmiany_w_dwoch_funkcjach_daja_dwa_wzorce_posortowane_bez_powtorzen():
    assert _wzorce(DWIE_FUNKCJE, 6, 5, 10) == [
        f"{MODUL}.x_zapisz__mutmut_*",
        f"{MODUL}.x_zapisz_wiele__mutmut_*",
    ]


def test_zmiana_w_pierwszej_linii_funkcji_daje_jej_wzorzec():
    assert _wzorce(DWIE_FUNKCJE, 4) == [f"{MODUL}.x_zapisz__mutmut_*"]


def test_zmiana_w_ostatniej_linii_funkcji_daje_jej_wzorzec():
    assert _wzorce(DWIE_FUNKCJE, 6) == [f"{MODUL}.x_zapisz__mutmut_*"]


def test_zmiana_w_linii_tuz_za_funkcja_nie_daje_jej_wzorca():
    zrodlo = "def f():\n    return 1\nY = 2\n"
    assert _wzorce(zrodlo, 3) == []


def test_zmiana_w_pustej_linii_miedzy_funkcjami_nie_daje_wzorca():
    assert _wzorce(DWIE_FUNKCJE, 7) == []
    assert _wzorce(DWIE_FUNKCJE, 8) == []


def test_zmiana_w_linii_dekoratora_staticmethod_daje_wzorzec_metody():
    # HAR-152: mutmut mutuje tylko funkcje bez dekoratora albo z samym staticmethod/classmethod.
    zrodlo = "class K:\n    @staticmethod\n    def m():\n        return 1\n"
    assert _wzorce(zrodlo, 2) == [f"{MODUL}.x{ZNAK}K{ZNAK}m__mutmut_*"]


def test_zmiana_w_linii_przed_dekoratorem_nie_daje_wzorca():
    zrodlo = "import functools\n\n@functools.cache\ndef f():\n    return 1\n"
    assert _wzorce(zrodlo, 2) == []


def test_zmiana_stalej_modulu_nie_daje_wzorca():
    assert _wzorce(DWIE_FUNKCJE, 1) == []


def test_zmiana_importu_nie_daje_wzorca():
    assert _wzorce("import os\n\n\ndef f():\n    return 1\n", 1) == []


def test_funkcja_async_ma_wzorzec_jak_zwykla_funkcja():
    assert _wzorce("async def pobierz():\n    return 1\n", 2) == [f"{MODUL}.x_pobierz__mutmut_*"]


KLASA = """\
class Serwis:
    LIMIT = 3

    def a(self):
        return 1

    async def b(self):
        return 2

    def __init__(self):
        self.x = 1
"""


def test_zmiana_w_metodzie_klasy_daje_wzorzec_tylko_tej_metody():
    assert _wzorce(KLASA, 5) == [f"{MODUL}.x{ZNAK}Serwis{ZNAK}a__mutmut_*"]


def test_zmiana_w_metodzie_async_klasy_daje_wzorzec_metody():
    assert _wzorce(KLASA, 8) == [f"{MODUL}.x{ZNAK}Serwis{ZNAK}b__mutmut_*"]


def test_zmiana_w_metodzie_dunder_zachowuje_podwojne_podkreslenia_nazwy():
    assert _wzorce(KLASA, 11) == [f"{MODUL}.x{ZNAK}Serwis{ZNAK}__init____mutmut_*"]


def test_zmiana_atrybutu_klasy_nie_daje_wzorca():
    assert _wzorce(KLASA, 2) == []


def test_zmiana_naglowka_klasy_nie_daje_wzorca():
    assert _wzorce(KLASA, 1) == []


def test_zmiana_w_funkcji_zagniezdzonej_daje_wzorzec_tylko_funkcji_zewnetrznej():
    zrodlo = "def zewn():\n    def wewn():\n        return 1\n    return wewn\n"
    assert _wzorce(zrodlo, 3) == [f"{MODUL}.x_zewn__mutmut_*"]


def test_funkcja_zagniezdzona_w_metodzie_nalezy_do_metody():
    zrodlo = "class K:\n    def m(self):\n        def w():\n            return 1\n        return w\n"
    assert _wzorce(zrodlo, 4) == [f"{MODUL}.x{ZNAK}K{ZNAK}m__mutmut_*"]


def test_modul_we_wzorcu_pochodzi_ze_sciezki_pliku():
    assert _wzorce("def f():\n    return 1\n", 2, sciezka="src/runner/__init__.py") == [
        "runner.x_f__mutmut_*"
    ]


def test_blad_skladni_pliku_to_blad_narzedzia():
    with pytest.raises(BladNarzedzia):
        _wzorce("def f(:\n", 1)


# --- wzorce (filtr source_paths, czytanie z dysku) --------------------------

ZRODLA = ["src/cockpit/services/"]


def _plik(korzen: Path, sciezka: str, tresc: str = "def f():\n    return 1\n") -> None:
    cel = korzen / sciezka
    cel.parent.mkdir(parents=True, exist_ok=True)
    cel.write_text(tresc, encoding="utf-8")


def test_plik_w_source_paths_daje_wzorce(tmp_path):
    _plik(tmp_path, "src/cockpit/services/a.py")
    wynik = mutanty_zmian.wzorce({"src/cockpit/services/a.py": {2}}, ZRODLA, tmp_path)
    assert wynik == ["cockpit.services.a.x_f__mutmut_*"]


def test_plik_spoza_source_paths_jest_pominiety(tmp_path):
    _plik(tmp_path, "src/cockpit/db/a.py")
    assert mutanty_zmian.wzorce({"src/cockpit/db/a.py": {2}}, ZRODLA, tmp_path) == []


def test_plik_w_katalogu_o_wspolnym_prefiksie_jest_pominiety(tmp_path):
    _plik(tmp_path, "src/cockpit/servicesX/a.py")
    assert mutanty_zmian.wzorce({"src/cockpit/servicesX/a.py": {2}}, ZRODLA, tmp_path) == []


def test_wpis_source_paths_bez_ukosnika_obejmuje_pliki_katalogu(tmp_path):
    _plik(tmp_path, "src/cockpit/services/a.py")
    wynik = mutanty_zmian.wzorce(
        {"src/cockpit/services/a.py": {2}}, ["src/cockpit/services"], tmp_path
    )
    assert wynik == ["cockpit.services.a.x_f__mutmut_*"]


def test_wpis_source_paths_bez_ukosnika_nie_obejmuje_katalogu_o_wspolnym_prefiksie(tmp_path):
    _plik(tmp_path, "src/cockpit/servicesX/a.py")
    zmiany = {"src/cockpit/servicesX/a.py": {2}}
    assert mutanty_zmian.wzorce(zmiany, ["src/cockpit/services"], tmp_path) == []


def test_wpis_source_paths_wskazujacy_pojedynczy_plik_obejmuje_ten_plik(tmp_path):
    _plik(tmp_path, "src/runner/x.py")
    wynik = mutanty_zmian.wzorce({"src/runner/x.py": {2}}, ["src/runner/x.py"], tmp_path)
    assert wynik == ["runner.x.x_f__mutmut_*"]


def test_wpis_source_paths_wskazujacy_plik_nie_obejmuje_innych_plikow(tmp_path):
    _plik(tmp_path, "src/runner/y.py")
    assert mutanty_zmian.wzorce({"src/runner/y.py": {2}}, ["src/runner/x.py"], tmp_path) == []


def test_plik_nie_python_jest_pominiety(tmp_path):
    _plik(tmp_path, "src/cockpit/services/notatki.md", "def f():\n    return 1\n")
    assert mutanty_zmian.wzorce({"src/cockpit/services/notatki.md": {2}}, ZRODLA, tmp_path) == []


def test_plik_nieistniejacy_na_dysku_jest_pominiety(tmp_path):
    assert mutanty_zmian.wzorce({"src/cockpit/services/brak.py": {2}}, ZRODLA, tmp_path) == []


def test_wiele_plikow_daje_wzorce_posortowane_bez_powtorzen(tmp_path):
    _plik(tmp_path, "src/cockpit/services/b.py")
    _plik(tmp_path, "src/cockpit/services/a.py")
    wynik = mutanty_zmian.wzorce(
        {"src/cockpit/services/b.py": {2}, "src/cockpit/services/a.py": {1, 2}}, ZRODLA, tmp_path
    )
    assert wynik == ["cockpit.services.a.x_f__mutmut_*", "cockpit.services.b.x_f__mutmut_*"]


def test_plik_nie_utf8_to_blad_narzedzia(tmp_path):
    cel = tmp_path / "src/cockpit/services/zly.py"
    cel.parent.mkdir(parents=True)
    cel.write_bytes(b"def f():\n    return '\xff\xfe'\n")
    with pytest.raises(BladNarzedzia):
        mutanty_zmian.wzorce({"src/cockpit/services/zly.py": {2}}, ZRODLA, tmp_path)


def test_katalog_zamiast_pliku_py_to_blad_narzedzia(tmp_path):
    (tmp_path / "src/cockpit/services/katalog.py").mkdir(parents=True)
    with pytest.raises(BladNarzedzia):
        mutanty_zmian.wzorce({"src/cockpit/services/katalog.py": {2}}, ZRODLA, tmp_path)


# --- source_paths_z_pyproject -----------------------------------------------


def _pyproject(tmp_path: Path, tresc: str) -> Path:
    plik = tmp_path / "pyproject.toml"
    plik.write_text(tresc, encoding="utf-8")
    return plik


def test_source_paths_czytane_z_sekcji_mutmut(tmp_path):
    plik = _pyproject(tmp_path, '[tool.mutmut]\nsource_paths = ["src/a/", "src/b/"]\n')
    assert mutanty_zmian.source_paths_z_pyproject(plik) == ["src/a/", "src/b/"]


@pytest.mark.parametrize(
    "tresc",
    [
        pytest.param(None, id="brak_pliku"),
        pytest.param("[tool.mutmut\nsource_paths = ", id="zly_toml"),
        pytest.param("[tool.mutmut]\nprzyklad = 1\n", id="brak_klucza"),
        pytest.param("[tool.pytest]\nx = 1\n", id="brak_sekcji_mutmut"),
        pytest.param('[tool.mutmut]\nsource_paths = "src/a/"\n', id="napis_zamiast_listy"),
        pytest.param("[tool.mutmut]\nsource_paths = [1]\n", id="lista_liczb_zamiast_napisow"),
        pytest.param(
            '[tool.mutmut]\nsource_paths = ["src/", 2]\n', id="liczba_wsrod_poprawnych_napisow"
        ),
        pytest.param('tool = "x"\n', id="tool_nie_jest_tabela"),
        pytest.param("[tool]\nmutmut = 1\n", id="mutmut_nie_jest_tabela"),
    ],
)
def test_niepoprawna_konfiguracja_mutmuta_to_blad_narzedzia(tmp_path, tresc):
    plik = tmp_path / "pyproject.toml" if tresc is None else _pyproject(tmp_path, tresc)
    with pytest.raises(BladNarzedzia):
        mutanty_zmian.source_paths_z_pyproject(plik)


# --- przezywajace -----------------------------------------------------------

WZ_FN = [f"{MODUL}.x_zapisz__mutmut_*"]


def _wyniki(*pary: tuple[str, str]) -> str:
    return "Results:\n" + "".join(f"    {k}: {s}\n" for k, s in pary)


@pytest.mark.parametrize(
    ("status", "przezyl"),
    [
        pytest.param("survived", True, id="survived"),
        pytest.param("no tests", True, id="brak_testow_to_survivor"),
        pytest.param("killed", False, id="killed"),
        pytest.param("timeout", False, id="timeout"),
        pytest.param("not checked", False, id="not_checked"),
        pytest.param("skipped", False, id="skipped"),
    ],
)
def test_status_mutanta_decyduje_czy_jest_survivorem(status, przezyl):
    klucz = f"{MODUL}.x_zapisz__mutmut_1"
    oczekiwane = [klucz] if przezyl else []
    assert mutanty_zmian.przezywajace(_wyniki((klucz, status)), WZ_FN) == oczekiwane


def test_survivor_spoza_wzorcow_zmian_nie_jest_zglaszany():
    wyniki = _wyniki((f"{MODUL}.x_inna__mutmut_1", "survived"))
    assert mutanty_zmian.przezywajace(wyniki, WZ_FN) == []


def test_linia_ostrzezenia_z_dwukropkiem_nie_jest_mutantem():
    wyniki = (
        "UserWarning: cos: survived\n"
        f"    {MODUL}.x_zapisz__mutmut_2: survived\n"
        "    ostrzezenie bez znacznika mutanta: survived\n"
    )
    assert mutanty_zmian.przezywajace(wyniki, ["*"]) == [f"{MODUL}.x_zapisz__mutmut_2"]


def test_survivory_sa_posortowane():
    wyniki = _wyniki(
        (f"{MODUL}.x_zapisz__mutmut_2", "survived"), (f"{MODUL}.x_zapisz__mutmut_1", "survived")
    )
    assert mutanty_zmian.przezywajace(wyniki, WZ_FN) == [
        f"{MODUL}.x_zapisz__mutmut_1",
        f"{MODUL}.x_zapisz__mutmut_2",
    ]


def test_wzorzec_funkcji_nie_lapie_mutanta_funkcji_o_dluzszej_nazwie():
    wyniki = _wyniki((f"{MODUL}.x_zapisz_wiele__mutmut_1", "survived"))
    assert mutanty_zmian.przezywajace(wyniki, WZ_FN) == []


def test_mutant_metody_pasuje_do_wzorca_metody():
    klucz = f"{MODUL}.x{ZNAK}Serwis{ZNAK}a__mutmut_1"
    assert mutanty_zmian.przezywajace(_wyniki((klucz, "survived")), [
        f"{MODUL}.x{ZNAK}Serwis{ZNAK}a__mutmut_*"
    ]) == [klucz]


def test_mutant_metody_nie_pasuje_do_wzorca_innej_metody_tej_samej_klasy():
    wyniki = _wyniki((f"{MODUL}.x{ZNAK}Serwis{ZNAK}b__mutmut_1", "survived"))
    assert mutanty_zmian.przezywajace(wyniki, [f"{MODUL}.x{ZNAK}Serwis{ZNAK}a__mutmut_*"]) == []


def test_mutant_metody_nie_pasuje_do_wzorca_funkcji_modulu_o_tej_samej_nazwie():
    wyniki = _wyniki((f"{MODUL}.x{ZNAK}Serwis{ZNAK}zapisz__mutmut_1", "survived"))
    assert mutanty_zmian.przezywajace(wyniki, WZ_FN) == []


def test_mutant_metody_nie_pasuje_do_wzorca_metody_innej_klasy():
    wyniki = _wyniki((f"{MODUL}.x{ZNAK}Inna{ZNAK}a__mutmut_1", "survived"))
    assert mutanty_zmian.przezywajace(wyniki, [f"{MODUL}.x{ZNAK}Serwis{ZNAK}a__mutmut_*"]) == []


# --- linie_bez_mutantow (HAR-144) -------------------------------------------


def _bez(zrodlo: str, *linie: int, w_zrodlach: bool = True) -> list[int]:
    return mutanty_zmian.linie_bez_mutantow(zrodlo, set(linie), w_zrodlach=w_zrodlach)


def test_zmieniona_stala_modulu_jest_zgloszona_jako_kod_bez_mutantow():
    assert _bez("LIMIT = 3\n\n\ndef f():\n    return 1\n", 1) == [1]


def test_zmienione_pole_klasy_z_ograniczeniem_jest_zgloszone():
    zrodlo = "class Model:\n    x: str = Field(max_length=5)\n\n    def m(self):\n        return 1\n"
    assert _bez(zrodlo, 2) == [2]


def test_zmieniony_naglowek_klasy_jest_zgloszony():
    zrodlo = "class Model:\n    def m(self):\n        return 1\n"
    assert _bez(zrodlo, 1) == [1]


def test_linia_tuz_za_funkcja_jest_zgloszona():
    assert _bez("def f():\n    return 1\nY = 2\n", 3) == [3]


@pytest.mark.parametrize(
    ("zrodlo", "linia"),
    [
        pytest.param("def f():\n    return 1\n", 2, id="cialo_funkcji"),
        pytest.param("def f():\n    return 1\n", 1, id="naglowek_funkcji"),
        pytest.param("async def f():\n    return 1\n", 2, id="cialo_funkcji_async"),
        pytest.param("class K:\n    def m(self):\n        return 1\n", 3, id="cialo_metody"),
        pytest.param("class K:\n    async def m(self):\n        return 1\n", 3, id="cialo_metody_async"),
    ],
)
def test_linia_w_funkcji_lub_metodzie_z_source_paths_ma_mutanty_wiec_nie_jest_zgloszona(zrodlo, linia):
    assert _bez(zrodlo, linia) == []


@pytest.mark.parametrize(
    ("zrodlo", "linia"),
    [
        pytest.param("def f():\n    return 1\n", 2, id="cialo_funkcji"),
        pytest.param("async def f():\n    return 1\n", 2, id="cialo_funkcji_async"),
        pytest.param("class K:\n    def m(self):\n        return 1\n", 3, id="cialo_metody"),
    ],
)
def test_linia_w_funkcji_poza_source_paths_jest_zgloszona_bo_mutmut_jej_nie_mutuje(zrodlo, linia):
    assert _bez(zrodlo, linia, w_zrodlach=False) == [linia]


@pytest.mark.parametrize(
    ("zrodlo", "linie"),
    [
        pytest.param("@functools.cache\ndef f():\n    return 1\n", (1, 2, 3), id="funkcja_cache"),
        pytest.param('@router.get("/x")\ndef f():\n    return 1\n', (1, 2, 3), id="funkcja_trasa"),
        pytest.param(
            "class K:\n    @property\n    def m(self):\n        return 1\n", (2, 3, 4), id="metoda_property"
        ),
        pytest.param(
            "@a\n@b\ndef f():\n    return 1\n", (1, 2, 3, 4), id="dwa_dekoratory"
        ),
        pytest.param(
            "class K:\n    @staticmethod\n    @other\n    def m():\n        return 1\n",
            (2, 3, 4, 5),
            id="staticmethod_z_drugim_dekoratorem",
        ),
        pytest.param(
            "class K:\n    @pkg.staticmethod\n    def m():\n        return 1\n",
            (2, 3, 4),
            id="staticmethod_jako_atrybut_a_nie_goła_nazwa",
        ),
    ],
)
def test_funkcja_z_dekoratorem_jest_poza_zasiegiem_mutmuta_wiec_jej_linie_sa_zgloszone(zrodlo, linie):
    assert _bez(zrodlo, *linie) == list(linie)


@pytest.mark.parametrize("dekorator", ["staticmethod", "classmethod"])
def test_metoda_z_jednym_dekoratorem_staticmethod_lub_classmethod_jest_mutowana_wiec_nie_jest_zgloszona(
    dekorator,
):
    zrodlo = f"class K:\n    @{dekorator}\n    def m(*a):\n        return 1\n"
    assert _bez(zrodlo, 2, 3, 4) == []


def test_metoda_bez_dekoratora_w_klasie_z_dekoratorem_jest_mutowana_a_atrybut_klasy_zgloszony():
    zrodlo = "@dataclass\nclass K:\n    X = 1\n    def m(self):\n        return 1\n"
    assert _bez(zrodlo, 3, 4, 5) == [3]


def test_wielokropek_jako_pierwsza_instrukcja_klasy_nie_jest_docstringiem():
    assert _bez("class K:\n    ...\n", 2) == [2]


def test_wielokropek_jako_pierwsza_instrukcja_modulu_nie_jest_docstringiem():
    assert _bez("...\nX = 1\n", 1) == [1]


def test_liczba_jako_pierwsza_instrukcja_modulu_nie_jest_docstringiem():
    assert _bez("1\n", 1) == [1]


def test_wielokropek_jako_pierwsza_instrukcja_funkcji_poza_source_paths_nie_jest_docstringiem():
    assert _bez("def f():\n    ...\n", 2, w_zrodlach=False) == [2]


@pytest.mark.parametrize("w_zrodlach", [True, False])
@pytest.mark.parametrize("linie", [(), (1,)])
def test_pusty_plik_nie_ma_linii_bez_mutantow(w_zrodlach, linie):
    assert _bez("", *linie, w_zrodlach=w_zrodlach) == []


def test_dekorator_funkcji_poza_source_paths_jest_zgloszony():
    assert _bez("@functools.cache\ndef f():\n    return 1\n", 1, w_zrodlach=False) == [1]


@pytest.mark.parametrize(
    "zrodlo",
    [
        pytest.param("A = 1\n\nB = 2\n", id="pusta_linia"),
        pytest.param("A = 1\n   \nB = 2\n", id="linia_z_samymi_bialymi_znakami"),
    ],
)
def test_pusta_linia_na_poziomie_modulu_nie_jest_zgloszona(zrodlo):
    assert _bez(zrodlo, 2) == []


@pytest.mark.parametrize(
    "zrodlo",
    [
        pytest.param("A = 1\n# komentarz\nB = 2\n", id="komentarz_na_poziomie_modulu"),
        pytest.param("if A:\n    # komentarz\n    B = 2\n", id="komentarz_wciety_w_instrukcji"),
    ],
)
def test_linia_z_samym_komentarzem_nie_jest_zgloszona(zrodlo):
    assert _bez(zrodlo, 2) == []


def test_komentarz_na_koncu_linii_kodu_nie_ukrywa_tej_linii():
    assert _bez("A = 1  # tlumaczenie\n", 1) == [1]


def test_docstring_modulu_wieloliniowy_nie_jest_zgloszony_a_kod_po_nim_tak():
    zrodlo = '"""Opis\nkolejna linia opisu\nostatnia linia\n"""\nA = 1\n'
    assert _bez(zrodlo, 1, 2, 3, 4, 5) == [5]


def test_luzny_napis_po_pierwszej_instrukcji_modulu_nie_jest_docstringiem():
    assert _bez('A = 1\n"""luzny napis"""\n', 2) == [2]


def test_docstring_klasy_wieloliniowy_nie_jest_zgloszony_a_atrybut_po_nim_tak():
    zrodlo = 'class K:\n    """Opis\n    druga linia\n    """\n    X = 1\n'
    assert _bez(zrodlo, 2, 3, 4, 5) == [5]


@pytest.mark.parametrize("slowo", ["def", "async def"])
def test_docstring_funkcji_poza_source_paths_nie_jest_zgloszony_a_kod_po_nim_tak(slowo):
    zrodlo = f'{slowo} f():\n    """Opis\n    druga linia\n    """\n    return 1\n'
    assert _bez(zrodlo, 2, 3, 4, 5, w_zrodlach=False) == [5]


def test_docstring_metody_poza_source_paths_nie_jest_zgloszony_a_kod_po_nim_tak():
    zrodlo = 'class K:\n    def m(self):\n        """Opis\n        druga linia\n        """\n        return 1\n'
    assert _bez(zrodlo, 3, 4, 5, 6, w_zrodlach=False) == [6]


def test_luzny_napis_w_funkcji_poza_source_paths_jest_zgloszony():
    zrodlo = 'def f():\n    a = 1\n    "luzny napis"\n    return a\n'
    assert _bez(zrodlo, 3, w_zrodlach=False) == [3]


def test_import_jednoliniowy_nie_jest_zgloszony():
    assert _bez("import os\nA = 1\n", 1) == []


def test_import_wieloliniowy_nie_jest_zgloszony_w_calym_zakresie():
    zrodlo = "from x import (\n    a,\n    b,\n)\nA = 1\n"
    assert _bez(zrodlo, 1, 2, 3, 4, 5) == [5]


def test_import_wewnatrz_instrukcji_warunkowej_nie_jest_zgloszony():
    assert _bez("if A:\n    import os\n", 2) == []


def test_import_wewnatrz_funkcji_poza_source_paths_nie_jest_zgloszony():
    zrodlo = "def f():\n    import os\n    return os\n"
    assert _bez(zrodlo, 2, 3, w_zrodlach=False) == [3]


def test_wieloliniowa_stala_zglasza_tylko_zmienione_linie():
    zrodlo = "X = {\n    'a': 1,\n    'b': 2,\n}\n"
    assert _bez(zrodlo, 2) == [2]


def test_zgloszone_linie_sa_posortowane_rosnaco():
    zrodlo = "A = 1\nB = 2\nC = 3\nD = 4\n"
    assert _bez(zrodlo, 4, 2, 3) == [2, 3, 4]


def test_numer_linii_poza_plikiem_jest_pominiety():
    assert _bez("A = 1\n", 1, 2, 99) == [1]


def test_numer_linii_zero_jest_pominiety_a_nie_czytany_od_konca_pliku():
    assert _bez("X = 1\nY = 2\n", 0) == []


def test_blad_skladni_przy_szukaniu_linii_bez_mutantow_to_blad_narzedzia():
    with pytest.raises(BladNarzedzia):
        _bez("def f(:\n", 1)


# --- bez_mutantow (pliki z diffu, HAR-144) ----------------------------------

POZA_ZRODLAMI = "src/cockpit/db/models.py"
FUNKCJA = "def f():\n    return 1\n"


def _bez_pliku(korzen: Path, zmiany: dict[str, set[int]]) -> list[str]:
    return mutanty_zmian.bez_mutantow(zmiany, ZRODLA, korzen)


def test_plik_src_spoza_source_paths_zglasza_takze_linie_w_funkcjach(tmp_path):
    _plik(tmp_path, POZA_ZRODLAMI, FUNKCJA)
    assert _bez_pliku(tmp_path, {POZA_ZRODLAMI: {2}}) == [f"{POZA_ZRODLAMI}:2"]


def test_plik_w_source_paths_zglasza_tylko_linie_poza_funkcjami(tmp_path):
    sciezka = "src/cockpit/services/stale.py"
    _plik(tmp_path, sciezka, "LIMIT = 3\n\n\ndef f():\n    return 1\n")
    assert _bez_pliku(tmp_path, {sciezka: {1, 5}}) == [f"{sciezka}:1"]


@pytest.mark.parametrize(
    "sciezka",
    [
        pytest.param("tools/x.py", id="narzedzia"),
        pytest.param("tests/x.py", id="testy"),
        pytest.param("web/x.py", id="front"),
        pytest.param("web/src/x.py", id="src_zagniezdzone_w_innym_katalogu"),
        pytest.param("srcx/x.py", id="katalog_o_wspolnym_prefiksie_z_src"),
        pytest.param("src/cockpit/x.md", id="plik_nie_python_pod_src"),
    ],
)
def test_zmiana_poza_kodem_src_nie_trafia_na_liste_bez_mutantow(tmp_path, sciezka):
    _plik(tmp_path, sciezka, "X = 1\n")
    assert _bez_pliku(tmp_path, {sciezka: {1}}) == []


def test_plik_nieistniejacy_jest_pominiety_a_pozostale_pliki_sa_zgloszone(tmp_path):
    _plik(tmp_path, POZA_ZRODLAMI, "X = 1\n")
    zmiany = {"src/cockpit/db/brak.py": {1}, POZA_ZRODLAMI: {1}}
    assert _bez_pliku(tmp_path, zmiany) == [f"{POZA_ZRODLAMI}:1"]


def test_plik_z_bajtami_spoza_utf8_to_blad_narzedzia(tmp_path):
    cel = tmp_path / "src/cockpit/db/zly.py"
    cel.parent.mkdir(parents=True)
    cel.write_bytes(b"X = '\xff\xfe'\n")
    with pytest.raises(BladNarzedzia):
        _bez_pliku(tmp_path, {"src/cockpit/db/zly.py": {1}})


def test_katalog_zamiast_pliku_py_spoza_source_paths_to_blad_narzedzia(tmp_path):
    (tmp_path / "src/cockpit/db/katalog.py").mkdir(parents=True)
    with pytest.raises(BladNarzedzia):
        _bez_pliku(tmp_path, {"src/cockpit/db/katalog.py": {1}})


def test_blad_skladni_w_pliku_spoza_source_paths_to_blad_narzedzia(tmp_path):
    _plik(tmp_path, POZA_ZRODLAMI, "def f(:\n")
    with pytest.raises(BladNarzedzia) as blad:
        _bez_pliku(tmp_path, {POZA_ZRODLAMI: {1}})
    assert POZA_ZRODLAMI in str(blad.value)


def test_wpisy_bez_mutantow_sa_posortowane_po_pliku_i_numerycznie_po_linii(tmp_path):
    _plik(tmp_path, "src/cockpit/db/a.py", "A = 1\nB = 2\n")
    _plik(tmp_path, "src/cockpit/db/b.py", "".join(f"V{n} = {n}\n" for n in range(1, 11)))
    zmiany = {"src/cockpit/db/b.py": {10, 9}, "src/cockpit/db/a.py": {2}}
    assert _bez_pliku(tmp_path, zmiany) == [
        "src/cockpit/db/a.py:2",
        "src/cockpit/db/b.py:9",
        "src/cockpit/db/b.py:10",
    ]


# --- main z podmienionym uruchom --------------------------------------------

ZRODLO_A = "def zapisz():\n    return 1\n\n\ndef inna():\n    return 2\n"
KLUCZ_A = "cockpit.services.a.x_zapisz__mutmut_1"


class _Srodowisko:
    """Zapisuje wywołania i odpowiada na polecenia git i mutmuta zgodnie z ustawieniami."""

    def __init__(self, **ustawienia):
        self.wywolania: list[list[str]] = []
        self.u = {
            "merge_rc": 0,
            "diff_rc": 0,
            "diff": "",
            "nowe_rc": 0,
            "nowe": "",
            "run_rc": 0,
            "run_out": "",
            "run_err": "",
            "results_rc": 0,
            "results": "",
        }
        self.u.update(ustawienia)

    def __call__(self, argv):
        self.wywolania.append(list(argv))
        if argv[:2] == ["git", "merge-base"]:
            return self._wynik(argv, self.u["merge_rc"], "abc123\n", "fatal: brak bazy")
        if argv[:2] == ["git", "diff"]:
            return self._wynik(argv, self.u["diff_rc"], self.u["diff"], "fatal: diff")
        if argv[:2] == ["git", "ls-files"]:
            return self._wynik(argv, self.u["nowe_rc"], self.u["nowe"], "fatal: ls-files")
        if argv[1:3] == ["-m", "mutmut"] and argv[3] == "run":
            return self._wynik(argv, self.u["run_rc"], self.u["run_out"], self.u["run_err"])
        if argv[1:3] == ["-m", "mutmut"] and argv[3] == "results":
            return self._wynik(argv, self.u["results_rc"], self.u["results"])
        raise AssertionError(f"nieoczekiwane polecenie: {argv}")

    @staticmethod
    def _wynik(argv, rc, out, err=""):
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)

    def mutmut(self, tryb: str) -> list[list[str]]:
        return [w for w in self.wywolania if w[1:3] == ["-m", "mutmut"] and w[3] == tryb]


def _repo(tmp_path: Path) -> Path:
    _pyproject(tmp_path, '[tool.mutmut]\nsource_paths = ["src/cockpit/services/"]\n')
    _plik(tmp_path, "src/cockpit/services/a.py", ZRODLO_A)
    return tmp_path


def _diff_a(linia: int = 2) -> str:
    return _diff("src/cockpit/services/a.py", f"@@ -1 +{linia} @@")


def _main(monkeypatch, tmp_path, srodowisko, *argv, pliki=None):
    """`pliki`: sciezka -> tresc dodatkowych plikow na dysku (np. spoza source_paths)."""
    monkeypatch.setattr(mutanty_zmian, "uruchom", srodowisko)
    korzen = _repo(tmp_path)
    for sciezka, tresc in (pliki or {}).items():
        _plik(korzen, sciezka, tresc)
    return mutanty_zmian.main(["--korzen", str(korzen), *argv])


def test_brak_zmienionych_funkcji_daje_kod_0_i_nie_woła_mutmuta(monkeypatch, tmp_path):
    # Linia 4 to pusta przerwa między funkcjami: nic poza zasięgiem mutmuta, więc nadal 0 (HAR-144).
    s = _Srodowisko(diff=_diff_a(4))
    assert _main(monkeypatch, tmp_path, s) == 0
    assert s.mutmut("run") == []
    assert s.mutmut("results") == []


def test_pusty_diff_daje_kod_0_i_nie_woła_mutmuta(monkeypatch, tmp_path):
    s = _Srodowisko()
    assert _main(monkeypatch, tmp_path, s) == 0
    assert s.mutmut("run") == []


def test_survivor_w_zmienionej_funkcji_daje_kod_1_i_jest_wypisany(monkeypatch, tmp_path, capsys):
    s = _Srodowisko(diff=_diff_a(), results=_wyniki((KLUCZ_A, "survived")))
    assert _main(monkeypatch, tmp_path, s) == 1
    assert KLUCZ_A in capsys.readouterr().out.splitlines()


def test_kazdy_survivor_jest_w_osobnej_linii(monkeypatch, tmp_path, capsys):
    drugi = "cockpit.services.a.x_zapisz__mutmut_2"
    s = _Srodowisko(
        diff=_diff_a(), results=_wyniki((KLUCZ_A, "survived"), (drugi, "no tests"))
    )
    assert _main(monkeypatch, tmp_path, s) == 1
    assert capsys.readouterr().out.splitlines() == [KLUCZ_A, drugi]


def test_brak_survivorow_daje_kod_0(monkeypatch, tmp_path):
    s = _Srodowisko(diff=_diff_a(), results=_wyniki((KLUCZ_A, "killed")))
    assert _main(monkeypatch, tmp_path, s) == 0
    assert len(s.mutmut("run")) == 1


def test_survivor_w_niezmienionej_funkcji_nie_psuje_wyniku(monkeypatch, tmp_path):
    inna = "cockpit.services.a.x_inna__mutmut_1"
    s = _Srodowisko(diff=_diff_a(), results=_wyniki((inna, "survived")))
    assert _main(monkeypatch, tmp_path, s) == 0


def test_mutmut_run_bez_pasujacych_mutantow_daje_kod_0(monkeypatch, tmp_path):
    # Zmieniona linia 2 jest wewnątrz funkcji (jej mutanty istnieją), więc nic poza zasięgiem (HAR-144).
    s = _Srodowisko(
        diff=_diff_a(),
        run_rc=1,
        run_out="AssertionError: Filtered for specific mutants, but nothing matches",
    )
    assert _main(monkeypatch, tmp_path, s) == 0


def test_komunikat_o_braku_pasujacych_mutantow_tylko_na_stderr_daje_kod_0(monkeypatch, tmp_path):
    s = _Srodowisko(
        diff=_diff_a(),
        run_rc=1,
        run_out="",
        run_err="AssertionError: Filtered for specific mutants, but nothing matches",
    )
    assert _main(monkeypatch, tmp_path, s) == 0
    assert s.mutmut("results") == []


def test_blad_git_diff_daje_kod_2_i_nie_woła_mutmuta(monkeypatch, tmp_path, capsys):
    s = _Srodowisko(diff_rc=128, diff=_diff_a())
    assert _main(monkeypatch, tmp_path, s) == 2
    assert s.mutmut("run") == []
    assert capsys.readouterr().err != ""


def test_blad_git_ls_files_daje_kod_2_i_nie_woła_mutmuta(monkeypatch, tmp_path, capsys):
    _plik(tmp_path, "src/cockpit/services/nowy.py", "def f():\n    return 1\n")
    s = _Srodowisko(nowe_rc=128, nowe="src/cockpit/services/nowy.py\n")
    assert _main(monkeypatch, tmp_path, s) == 2
    assert s.mutmut("run") == []
    assert capsys.readouterr().err != ""


def test_nowy_plik_nie_python_z_bajtami_spoza_utf8_jest_pominiety(monkeypatch, tmp_path):
    cel = tmp_path / "src/cockpit/services/dane.bin"
    cel.parent.mkdir(parents=True)
    cel.write_bytes(b"\xff\xfe\x00")
    s = _Srodowisko(nowe="src/cockpit/services/dane.bin\n")
    assert _main(monkeypatch, tmp_path, s) == 0
    assert s.mutmut("run") == []


def test_nowy_plik_python_z_bajtami_spoza_utf8_daje_kod_2(monkeypatch, tmp_path, capsys):
    cel = tmp_path / "src/cockpit/services/zly.py"
    cel.parent.mkdir(parents=True)
    cel.write_bytes(b"def f():\n    return '\xff\xfe'\n")
    s = _Srodowisko(nowe="src/cockpit/services/zly.py\n")
    assert _main(monkeypatch, tmp_path, s) == 2
    assert s.mutmut("run") == []
    assert capsys.readouterr().err != ""


def test_nowy_plik_z_ls_files_nieistniejacy_pod_korzeniem_jest_pominiety(monkeypatch, tmp_path):
    s = _Srodowisko(nowe="src/cockpit/services/brak.py\n")
    assert _main(monkeypatch, tmp_path, s) == 0
    assert s.mutmut("run") == []


def test_mutmut_run_z_innym_bledem_daje_kod_2(monkeypatch, tmp_path, capsys):
    s = _Srodowisko(diff=_diff_a(), run_rc=1, run_out="Traceback: cos innego")
    assert _main(monkeypatch, tmp_path, s) == 2
    assert s.mutmut("results") == []
    assert capsys.readouterr().err != ""


def test_mutmut_results_z_bledem_daje_kod_2(monkeypatch, tmp_path):
    s = _Srodowisko(diff=_diff_a(), results_rc=1)
    assert _main(monkeypatch, tmp_path, s) == 2


def test_blad_merge_base_daje_kod_2_i_nie_czyta_diffu(monkeypatch, tmp_path, capsys):
    s = _Srodowisko(merge_rc=128)
    assert _main(monkeypatch, tmp_path, s) == 2
    assert [w for w in s.wywolania if w[:2] == ["git", "diff"]] == []
    assert s.mutmut("run") == []
    assert capsys.readouterr().err != ""


def test_nowy_nieśledzony_plik_trafia_do_wzorcow_mutmuta(monkeypatch, tmp_path):
    _plik(tmp_path, "src/cockpit/services/nowy.py", "def f():\n    return 1\n")
    s = _Srodowisko(nowe="src/cockpit/services/nowy.py\n")
    assert _main(monkeypatch, tmp_path, s) == 0
    assert s.mutmut("run")[0][4:] == ["cockpit.services.nowy.x_f__mutmut_*"]


def test_wzorce_sa_osobnymi_argumentami_po_mutmut_run(monkeypatch, tmp_path):
    s = _Srodowisko(diff=_diff("src/cockpit/services/a.py", "@@ -1,5 +1,5 @@"))
    _main(monkeypatch, tmp_path, s)
    assert s.mutmut("run")[0] == [
        sys.executable,
        "-m",
        "mutmut",
        "run",
        "cockpit.services.a.x_inna__mutmut_*",
        "cockpit.services.a.x_zapisz__mutmut_*",
    ]


def test_baza_porownania_trafia_do_merge_base(monkeypatch, tmp_path):
    s = _Srodowisko()
    _main(monkeypatch, tmp_path, s, "--baza", "origin/develop")
    assert s.wywolania[0] == ["git", "merge-base", "origin/develop", "HEAD"]


def test_domyslna_baza_to_origin_main(monkeypatch, tmp_path):
    s = _Srodowisko()
    _main(monkeypatch, tmp_path, s)
    assert s.wywolania[0] == ["git", "merge-base", "origin/main", "HEAD"]


def test_diff_liczony_od_wspolnego_przodka_obejmuje_caly_src_i_source_paths(monkeypatch, tmp_path):
    s = _Srodowisko()
    _main(monkeypatch, tmp_path, s)
    diff = next(w for w in s.wywolania if w[:2] == ["git", "diff"])
    assert diff == [
        "git",
        "diff",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "abc123",
        "--",
        "src/",
        "src/cockpit/services/",
    ]


def test_nowe_pliki_szukane_w_calym_src_i_source_paths(monkeypatch, tmp_path):
    s = _Srodowisko()
    _main(monkeypatch, tmp_path, s)
    nowe = next(w for w in s.wywolania if w[:2] == ["git", "ls-files"])
    assert nowe == [
        "git",
        "ls-files",
        "--others",
        "--exclude-standard",
        "--",
        "src/",
        "src/cockpit/services/",
    ]


# --- zmiany poza zasięgiem mutmuta: kod 3 (HAR-144) -------------------------

NAGLOWEK_BEZ_MUTANTOW = "bez mutantów — sprawdź ręcznie"
STALE = "src/cockpit/services/stale.py"
MODELE = "src/cockpit/db/models.py"
ZRODLO_STALE = "LIMIT = 3\n\n\ndef f():\n    return 1\n"


def _diff_linii(plik: str, linia: int) -> str:
    return _diff(plik, f"@@ -1 +{linia} @@")


def test_zmiana_stalej_modulu_w_source_paths_daje_kod_3_bez_wolania_mutmuta(
    monkeypatch, tmp_path, capsys
):
    s = _Srodowisko(diff=_diff_linii(STALE, 1))
    assert _main(monkeypatch, tmp_path, s, pliki={STALE: ZRODLO_STALE}) == 3
    assert s.mutmut("run") == []
    assert s.mutmut("results") == []
    wyjscie = capsys.readouterr()
    assert wyjscie.out.splitlines() == [NAGLOWEK_BEZ_MUTANTOW, f"{STALE}:1"]
    assert wyjscie.err != ""


def test_zmiana_pola_klasy_z_ograniczeniem_daje_kod_3_z_numerem_linii(monkeypatch, tmp_path, capsys):
    plik = "src/cockpit/services/model.py"
    zrodlo = "class Model:\n    x: str = Field(max_length=5)\n\n    def m(self):\n        return 1\n"
    s = _Srodowisko(diff=_diff_linii(plik, 2))
    assert _main(monkeypatch, tmp_path, s, pliki={plik: zrodlo}) == 3
    assert capsys.readouterr().out.splitlines() == [NAGLOWEK_BEZ_MUTANTOW, f"{plik}:2"]


def test_zmiana_w_pliku_spoza_source_paths_daje_kod_3_takze_dla_linii_w_funkcji(
    monkeypatch, tmp_path, capsys
):
    s = _Srodowisko(diff=_diff_linii(MODELE, 2))
    assert _main(monkeypatch, tmp_path, s, pliki={MODELE: FUNKCJA}) == 3
    assert s.mutmut("run") == []
    assert capsys.readouterr().out.splitlines() == [NAGLOWEK_BEZ_MUTANTOW, f"{MODELE}:2"]


def test_nowy_nieśledzony_plik_spoza_source_paths_daje_kod_3_z_linia_kodu(
    monkeypatch, tmp_path, capsys
):
    nowy = "src/cockpit/db/nowy.py"
    s = _Srodowisko(nowe=f"{nowy}\n")
    assert _main(monkeypatch, tmp_path, s, pliki={nowy: "X = 1\n\n\ndef f():\n    return 1\n"}) == 3
    assert capsys.readouterr().out.splitlines() == [
        NAGLOWEK_BEZ_MUTANTOW,
        f"{nowy}:1",
        f"{nowy}:4",
        f"{nowy}:5",
    ]


def test_wpisy_bez_mutantow_z_kilku_plikow_sa_w_osobnych_liniach_posortowane(
    monkeypatch, tmp_path, capsys
):
    s = _Srodowisko(diff=_diff_linii(MODELE, 2) + _diff_linii(STALE, 1))
    assert _main(monkeypatch, tmp_path, s, pliki={MODELE: FUNKCJA, STALE: ZRODLO_STALE}) == 3
    assert capsys.readouterr().out.splitlines() == [
        NAGLOWEK_BEZ_MUTANTOW,
        f"{MODELE}:2",
        f"{STALE}:1",
    ]


def test_zmiana_tylko_w_funkcji_z_zabitymi_mutantami_daje_kod_0_bez_naglowka(
    monkeypatch, tmp_path, capsys
):
    s = _Srodowisko(diff=_diff_a(), results=_wyniki((KLUCZ_A, "killed")))
    assert _main(monkeypatch, tmp_path, s) == 0
    assert NAGLOWEK_BEZ_MUTANTOW not in capsys.readouterr().out


def test_przezywajacy_mutant_i_zmiana_poza_zasiegiem_daja_kod_1_a_lista_bez_mutantow_idzie_na_stderr(
    monkeypatch, tmp_path, capsys
):
    s = _Srodowisko(
        diff=_diff_a() + _diff_linii(MODELE, 2), results=_wyniki((KLUCZ_A, "survived"))
    )
    assert _main(monkeypatch, tmp_path, s, pliki={MODELE: FUNKCJA}) == 1
    wyjscie = capsys.readouterr()
    assert wyjscie.out.splitlines() == [KLUCZ_A]
    assert NAGLOWEK_BEZ_MUTANTOW in wyjscie.err
    assert f"{MODELE}:2" in wyjscie.err


def test_brak_pasujacych_mutantow_i_zmiana_poza_zasiegiem_daje_kod_3(monkeypatch, tmp_path, capsys):
    s = _Srodowisko(
        diff=_diff_a() + _diff_linii(MODELE, 2),
        run_rc=1,
        run_out="AssertionError: Filtered for specific mutants, but nothing matches",
    )
    assert _main(monkeypatch, tmp_path, s, pliki={MODELE: FUNKCJA}) == 3
    assert s.mutmut("results") == []
    assert capsys.readouterr().out.splitlines() == [NAGLOWEK_BEZ_MUTANTOW, f"{MODELE}:2"]


def test_wszystkie_mutanty_zabite_i_zmiana_poza_zasiegiem_daje_kod_3(monkeypatch, tmp_path, capsys):
    s = _Srodowisko(
        diff=_diff_a() + _diff_linii(MODELE, 2), results=_wyniki((KLUCZ_A, "killed"))
    )
    assert _main(monkeypatch, tmp_path, s, pliki={MODELE: FUNKCJA}) == 3
    assert len(s.mutmut("run")) == 1
    assert capsys.readouterr().out.splitlines() == [NAGLOWEK_BEZ_MUTANTOW, f"{MODELE}:2"]


def test_zmiana_tylko_w_narzedziach_i_testach_daje_kod_0_bez_naglowka(monkeypatch, tmp_path, capsys):
    s = _Srodowisko(diff=_diff_linii("tools/x.py", 1) + _diff_linii("tests/test_x.py", 1))
    pliki = {"tools/x.py": "X = 1\n", "tests/test_x.py": "X = 1\n"}
    assert _main(monkeypatch, tmp_path, s, pliki=pliki) == 0
    assert s.mutmut("run") == []
    assert NAGLOWEK_BEZ_MUTANTOW not in capsys.readouterr().out


def test_blad_narzedzia_przy_zmianie_poza_zasiegiem_daje_kod_2_a_nie_3(monkeypatch, tmp_path, capsys):
    s = _Srodowisko(diff=_diff_a() + _diff_linii(MODELE, 2), results_rc=1)
    assert _main(monkeypatch, tmp_path, s, pliki={MODELE: FUNKCJA}) == 2
    assert NAGLOWEK_BEZ_MUTANTOW not in capsys.readouterr().out


def test_plik_spoza_source_paths_z_bledem_skladni_daje_kod_2_ze_sciezka_pliku_na_stderr(
    monkeypatch, tmp_path, capsys
):
    s = _Srodowisko(diff=_diff_linii(MODELE, 1))
    assert _main(monkeypatch, tmp_path, s, pliki={MODELE: "def f(:\n"}) == 2
    assert s.mutmut("run") == []
    assert MODELE in capsys.readouterr().err


def test_nowy_pusty_plik_spoza_source_paths_daje_kod_0(monkeypatch, tmp_path, capsys):
    pusty = "src/cockpit/db/pusty.py"
    s = _Srodowisko(nowe=f"{pusty}\n")
    assert _main(monkeypatch, tmp_path, s, pliki={pusty: ""}) == 0
    assert NAGLOWEK_BEZ_MUTANTOW not in capsys.readouterr().out


def test_zmiana_w_ciele_funkcji_z_dekoratorem_trasy_przy_braku_mutantow_daje_kod_3(
    monkeypatch, tmp_path, capsys
):
    plik = "src/cockpit/services/trasy.py"
    zrodlo = '@router.get("/x")\ndef h():\n    return 1\n'
    s = _Srodowisko(
        diff=_diff_linii(plik, 3),
        run_rc=1,
        run_out="AssertionError: Filtered for specific mutants, but nothing matches",
    )
    assert _main(monkeypatch, tmp_path, s, pliki={plik: zrodlo}) == 3
    assert capsys.readouterr().out.splitlines() == [NAGLOWEK_BEZ_MUTANTOW, f"{plik}:3"]


def test_brak_pyproject_daje_kod_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mutanty_zmian, "uruchom", _Srodowisko())
    assert mutanty_zmian.main(["--korzen", str(tmp_path)]) == 2
    assert capsys.readouterr().err != ""


def test_blad_skladni_w_zmienionym_pliku_daje_kod_2(monkeypatch, tmp_path):
    s = _Srodowisko(diff=_diff_a())
    _repo(tmp_path)
    _plik(tmp_path, "src/cockpit/services/a.py", "def zapisz(:\n")
    monkeypatch.setattr(mutanty_zmian, "uruchom", s)
    assert mutanty_zmian.main(["--korzen", str(tmp_path)]) == 2
    assert s.mutmut("run") == []


# --- HAR-152: __new__, __setattr__, __getattribute__, dekoratory i pragma no mutate --

NAZWY_POMIJANE_PRZEZ_MUTMUTA = ["__new__", "__setattr__", "__getattribute__"]


@pytest.mark.parametrize("nazwa", NAZWY_POMIJANE_PRZEZ_MUTMUTA)
def test_zmieniona_metoda_pomijana_przez_mutmuta_jest_zgloszona_bez_docstringu_i_komentarza(nazwa):
    zrodlo = (
        "class K:\n"
        f"    def {nazwa}(self):\n"
        '        """Opis."""\n'
        "        # komentarz\n"
        "        x = 1\n"
        "        return x\n"
    )
    assert _bez(zrodlo, 2, 3, 4, 5, 6) == [2, 5, 6]


@pytest.mark.parametrize("nazwa", NAZWY_POMIJANE_PRZEZ_MUTMUTA)
def test_zmieniona_funkcja_modulu_o_nazwie_pomijanej_przez_mutmuta_jest_zgloszona(nazwa):
    assert _bez(f"def {nazwa}(a):\n    return 1\n", 1, 2) == [1, 2]


@pytest.mark.parametrize("nazwa", ["__init__", "__call__", "__new", "new__", "__getattr__"])
def test_zmieniona_metoda_dunder_spoza_listy_pomijanej_przez_mutmuta_ma_mutanty(nazwa):
    zrodlo = f"class K:\n    def {nazwa}(self):\n        return 1\n"
    assert _bez(zrodlo, 2, 3) == []


@pytest.mark.parametrize("nazwa", NAZWY_POMIJANE_PRZEZ_MUTMUTA)
def test_metoda_pomijana_przez_mutmuta_nie_zglasza_linii_sasiedniej_zwyklej_metody(nazwa):
    zrodlo = (
        f"class K:\n    def {nazwa}(self):\n        return 1\n\n    def m(self):\n        return 2\n"
    )
    assert _bez(zrodlo, 3, 6) == [3]


def test_pragma_na_koncu_linii_zglasza_te_linie_a_inne_linie_funkcji_nie():
    zrodlo = "def f():\n    a = 1\n    x = 2  # pragma: no mutate\n    return a\n"
    assert _bez(zrodlo, 2, 3, 4) == [3]


@pytest.mark.parametrize(
    ("zmienione", "oczekiwane"),
    [
        pytest.param((3,), [3], id="linia_startu_instrukcji"),
        pytest.param((4,), [], id="linia_wewnatrz_instrukcji_ma_mutant"),
        pytest.param((2, 3, 4, 6), [3], id="tylko_linia_startu_bez_sasiadow"),
    ],
)
def test_pragma_na_ostatniej_linii_instrukcji_wieloliniowej_wylacza_tylko_linie_startu(
    zmienione, oczekiwane
):
    # mutmut: pragma na koncu instrukcji liczy sie dla linii, w ktorej instrukcja startuje;
    # `a + 1` w linii 4 ma mutant (zweryfikowane przez create_mutations).
    zrodlo = (
        "def f():\n"
        "    a = 1\n"
        "    wynik = g(\n"
        "        a + 1,\n"
        "    )  # pragma: no mutate\n"
        "    return wynik + 1\n"
    )
    assert _bez(zrodlo, *zmienione) == oczekiwane


def test_komentarz_pragma_wewnatrz_nawiasow_nie_jest_pragma_wiec_linie_maja_mutanty():
    zrodlo = (
        "def f():\n"
        "    wynik = g(  # pragma: no mutate\n"
        "        1 + 1,\n"
        "    )\n"
        "    return wynik + 1\n"
    )
    assert _bez(zrodlo, 2, 3, 5) == []


def test_pragma_z_dopiskiem_po_tokenie_nadal_liczy_sie():
    zrodlo = "def f():\n    a = 1  # pragma: no mutate (uzasadnienie)\n    return a\n"
    assert _bez(zrodlo, 2, 3) == [2]


def test_pragma_block_na_linii_def_zglasza_cala_funkcje_bez_docstringu_i_nie_sasiednia():
    zrodlo = (
        "class K:\n"
        "    def m(self):  # pragma: no mutate block\n"
        '        """Opis."""\n'
        "        a = 1\n"
        "        return a\n"
        "\n"
        "    def n(self):\n"
        "        return 2\n"
    )
    assert _bez(zrodlo, 2, 3, 4, 5, 6, 7, 8) == [2, 4, 5]


def test_pragma_block_na_def_funkcji_modulu_zglasza_jej_linie():
    zrodlo = "def f():  # pragma: no mutate block\n    a = 1\n    return a\n\n\ndef g():\n    return 2\n"
    assert _bez(zrodlo, 1, 2, 3, 6, 7) == [1, 2, 3]


def test_pragma_block_na_naglowku_if_zglasza_caly_if_a_nie_linie_po_nim():
    zrodlo = (
        "def f(x):\n"
        "    a = 0\n"
        "    if x:  # pragma: no mutate block\n"
        "        a = 1\n"
        "        a += 1\n"
        "    return a\n"
    )
    assert _bez(zrodlo, 2, 3, 4, 5, 6) == [3, 4, 5]


@pytest.mark.parametrize(
    "zrodlo",
    [
        pytest.param(
            "def f(x):\n    a = 0\n    if x:  # pragma: no mutate\n        a = 1\n    return a\n",
            id="if",
        ),
        pytest.param(
            "def f(x):\n    a = 0\n    for i in x:  # pragma: no mutate\n        a = i\n    return a\n",
            id="for",
        ),
    ],
)
def test_pragma_bez_block_na_naglowku_zglasza_tylko_naglowek_a_nie_cialo(zrodlo):
    assert _bez(zrodlo, 2, 3, 4, 5) == [3]


def test_osobna_linia_pragma_block_zglasza_kolejne_instrukcje_bloku_a_poprzednie_nie():
    zrodlo = (
        "def f():\n"
        "    a = 1\n"
        "    # pragma: no mutate block\n"
        "    b = 2\n"
        "    c = 3\n"
        "    return a + b + c\n"
    )
    assert _bez(zrodlo, 2, 3, 4, 5, 6) == [3, 4, 5, 6]


def test_osobna_linia_pragma_block_w_ciele_if_nie_siega_poza_ten_blok():
    zrodlo = (
        "def f(x):\n"
        "    if x:\n"
        "        # pragma: no mutate block\n"
        "        a = 1\n"
        "    b = 2\n"
        "    return b\n"
    )
    assert _bez(zrodlo, 2, 3, 4, 5, 6) == [3, 4]


def test_pragma_start_end_zglasza_linie_miedzy_znacznikami_a_nie_poza_nimi():
    zrodlo = (
        "def f():\n"
        "    a = 1\n"
        "    # pragma: no mutate start\n"
        "    b = 2\n"
        "    c = 3\n"
        "    # pragma: no mutate end\n"
        "    d = 4\n"
        "    return a\n"
    )
    assert _bez(zrodlo, 2, 3, 4, 5, 6, 7, 8) == [3, 4, 5, 6]


def test_osobna_linia_pragma_bez_tokenu_nie_obejmuje_nastepnej_linii_kodu():
    zrodlo = "def f():\n    a = 1\n    # pragma: no mutate\n    b = 2\n"
    # sama linia pragmy jest u mutmuta pomijana, wiec jej zmiana idzie do "sprawdz recznie";
    # nastepna linia kodu (4) nadal ma mutanty
    assert _bez(zrodlo, 2, 3, 4) == [3]


@pytest.mark.parametrize(
    "zrodlo",
    [
        pytest.param("def f():\n    a = 1  # pragma: no cover\n    return a\n", id="no_cover"),
        pytest.param("def f():\n    a = 1  # no mutate\n    return a\n", id="bez_slowa_pragma"),
        pytest.param('def f():\n    a = "# pragma: no mutate"\n    return a\n', id="w_literale"),
        pytest.param(
            'def f():\n    a = """\n# pragma: no mutate block\n"""\n    b = 2\n    return b\n',
            id="w_wieloliniowym_literale",
        ),
    ],
)
def test_tekst_ktory_nie_jest_komentarzem_pragma_no_mutate_nie_wylacza_mutantow(zrodlo):
    ostatnia = len(zrodlo.splitlines())
    assert _bez(zrodlo, *range(2, ostatnia + 1)) == []


def test_pragma_w_niezmienionej_linii_nie_zglasza_zmienionej_linii_innej_instrukcji():
    zrodlo = "def f():\n    a = 1\n    b = 2  # pragma: no mutate\n    c = 3\n"
    assert _bez(zrodlo, 4) == []


# --- wzorce_dla_pliku: miejsca, ktorych mutmut nie mutuje --------------------


@pytest.mark.parametrize("nazwa", NAZWY_POMIJANE_PRZEZ_MUTMUTA)
def test_zmieniona_metoda_pomijana_przez_mutmuta_nie_daje_wzorca(nazwa):
    assert _wzorce(f"class K:\n    def {nazwa}(self):\n        return 1\n", 3) == []


@pytest.mark.parametrize("nazwa", NAZWY_POMIJANE_PRZEZ_MUTMUTA)
def test_zmieniona_funkcja_modulu_pomijana_przez_mutmuta_nie_daje_wzorca(nazwa):
    assert _wzorce(f"def {nazwa}(a):\n    return 1\n", 2) == []


def test_zmieniony_init_obok_new_daje_wzorzec_tylko_init():
    zrodlo = (
        "class K:\n    def __new__(cls):\n        return 1\n\n    def __init__(self):\n        self.x = 1\n"
    )
    assert _wzorce(zrodlo, 3, 6) == [f"{MODUL}.x{ZNAK}K{ZNAK}__init____mutmut_*"]


@pytest.mark.parametrize(
    "zrodlo",
    [
        pytest.param("@functools.cache\ndef f():\n    return 1\n", id="funkcja_modulu_z_cache"),
        pytest.param("class K:\n    @property\n    def m(self):\n        return 1\n", id="property"),
        pytest.param("@a\n@b\ndef f():\n    return 1\n", id="dwa_dekoratory"),
        pytest.param(
            "class K:\n    @staticmethod\n    @other\n    def m():\n        return 1\n",
            id="staticmethod_z_drugim_dekoratorem",
        ),
        pytest.param(
            "class K:\n    @pkg.staticmethod\n    def m():\n        return 1\n",
            id="staticmethod_jako_atrybut",
        ),
    ],
)
def test_zmieniona_funkcja_z_dekoratorem_innym_niz_goly_staticmethod_nie_daje_wzorca(zrodlo):
    ostatnia = len(zrodlo.splitlines())
    assert _wzorce(zrodlo, *range(1, ostatnia + 1)) == []


@pytest.mark.parametrize("dekorator", ["staticmethod", "classmethod"])
def test_zmieniona_metoda_z_golym_staticmethod_lub_classmethod_daje_wzorzec(dekorator):
    zrodlo = f"class K:\n    @{dekorator}\n    def m(*a):\n        return 1\n"
    assert _wzorce(zrodlo, 4) == [f"{MODUL}.x{ZNAK}K{ZNAK}m__mutmut_*"]


def test_zmieniona_funkcja_z_dekoratorem_nie_odbiera_wzorca_sasiedniej_zwyklej_funkcji():
    zrodlo = "@functools.cache\ndef f():\n    return 1\n\n\ndef g():\n    return 2\n"
    assert _wzorce(zrodlo, 3, 7) == [f"{MODUL}.x_g__mutmut_*"]


# --- main: zmiany poza zasiegiem mutmuta (HAR-152) ---------------------------

NOWY = "src/cockpit/services/nowy.py"
KLUCZ_G = "cockpit.services.nowy.x_g__mutmut_1"


def test_zmiana_w_metodzie_new_w_source_paths_daje_kod_3_bez_wolania_mutmuta(
    monkeypatch, tmp_path, capsys
):
    zrodlo = "class K:\n    def __new__(cls):\n        return super().__new__(cls)\n"
    s = _Srodowisko(diff=_diff_linii(NOWY, 3))
    assert _main(monkeypatch, tmp_path, s, pliki={NOWY: zrodlo}) == 3
    assert s.mutmut("run") == []
    assert s.mutmut("results") == []
    assert capsys.readouterr().out.splitlines() == [NAGLOWEK_BEZ_MUTANTOW, f"{NOWY}:3"]


@pytest.mark.parametrize("nazwa", ["__setattr__", "__getattribute__"])
def test_zmiana_w_pozostalych_metodach_pomijanych_przez_mutmuta_daje_kod_3(
    monkeypatch, tmp_path, capsys, nazwa
):
    zrodlo = f"class K:\n    def {nazwa}(self, k):\n        return 1\n"
    s = _Srodowisko(diff=_diff_linii(NOWY, 3))
    assert _main(monkeypatch, tmp_path, s, pliki={NOWY: zrodlo}) == 3
    assert s.mutmut("run") == []
    assert capsys.readouterr().out.splitlines() == [NAGLOWEK_BEZ_MUTANTOW, f"{NOWY}:3"]


def test_zmieniona_linia_z_pragma_no_mutate_w_funkcji_z_zabitymi_mutantami_daje_kod_3(
    monkeypatch, tmp_path, capsys
):
    zrodlo = "def g():\n    a = 1\n    b = 2  # pragma: no mutate\n    return a\n"
    s = _Srodowisko(diff=_diff_linii(NOWY, 3), results=_wyniki((KLUCZ_G, "killed")))
    assert _main(monkeypatch, tmp_path, s, pliki={NOWY: zrodlo}) == 3
    assert len(s.mutmut("run")) == 1
    assert capsys.readouterr().out.splitlines() == [NAGLOWEK_BEZ_MUTANTOW, f"{NOWY}:3"]


def test_zmieniona_linia_bez_pragmy_w_tej_samej_funkcji_z_zabitymi_mutantami_daje_kod_0(
    monkeypatch, tmp_path, capsys
):
    zrodlo = "def g():\n    a = 1\n    b = 2  # pragma: no mutate\n    return a\n"
    s = _Srodowisko(diff=_diff_linii(NOWY, 4), results=_wyniki((KLUCZ_G, "killed")))
    assert _main(monkeypatch, tmp_path, s, pliki={NOWY: zrodlo}) == 0
    assert NAGLOWEK_BEZ_MUTANTOW not in capsys.readouterr().out


def test_zmiana_w_zwyklym_init_z_zabitymi_mutantami_daje_kod_0_nie_3(
    monkeypatch, tmp_path, capsys
):
    zrodlo = "class Klasa:\n    def __init__(self):\n        self.x = 1\n"
    klucz = f"cockpit.services.nowy.x{ZNAK}Klasa{ZNAK}__init____mutmut_1"
    s = _Srodowisko(diff=_diff_linii(NOWY, 3), results=_wyniki((klucz, "killed")))
    assert _main(monkeypatch, tmp_path, s, pliki={NOWY: zrodlo}) == 0
    assert len(s.mutmut("run")) == 1
    assert s.mutmut("run")[0][4:] == [f"cockpit.services.nowy.x{ZNAK}Klasa{ZNAK}__init____mutmut_*"]
    assert NAGLOWEK_BEZ_MUTANTOW not in capsys.readouterr().out


def test_zmiana_w_new_i_przezywajacy_mutant_w_innej_funkcji_daja_kod_1_a_lista_na_stderr(
    monkeypatch, tmp_path, capsys
):
    zrodlo = (
        "class K:\n    def __new__(cls):\n        return super().__new__(cls)\n\n\n"
        "def g():\n    return 1\n"
    )
    diff = _diff(NOWY, "@@ -1 +3 @@", "@@ -1 +7 @@")
    s = _Srodowisko(diff=diff, results=_wyniki((KLUCZ_G, "survived")))
    assert _main(monkeypatch, tmp_path, s, pliki={NOWY: zrodlo}) == 1
    wyjscie = capsys.readouterr()
    assert wyjscie.out.splitlines() == [KLUCZ_G]
    assert NAGLOWEK_BEZ_MUTANTOW in wyjscie.err
    assert f"{NOWY}:3" in wyjscie.err
    assert f"{NOWY}:7" not in wyjscie.err


def test_zmieniony_plik_z_pragma_start_bez_end_daje_kod_2_ze_sciezka_pliku_a_nie_kod_0_ani_3(
    monkeypatch, tmp_path, capsys
):
    zrodlo = "def g():\n    # pragma: no mutate start\n    return 1\n"
    s = _Srodowisko(diff=_diff_linii(NOWY, 3), results=_wyniki((KLUCZ_G, "killed")))
    assert _main(monkeypatch, tmp_path, s, pliki={NOWY: zrodlo}) == 2
    wyjscie = capsys.readouterr()
    assert NAGLOWEK_BEZ_MUTANTOW not in wyjscie.out
    assert NOWY in wyjscie.err
    assert "bledna pragma no mutate" in wyjscie.err


def test_brak_mutmuta_w_interpreterze_przy_zmianie_funkcji_daje_kod_2_bez_wolania_mutmuta(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setitem(sys.modules, "mutmut.mutation.file_mutation", None)
    s = _Srodowisko(diff=_diff_linii(NOWY, 2))
    assert _main(monkeypatch, tmp_path, s, pliki={NOWY: "def g():\n    return 1\n"}) == 2
    assert s.mutmut("run") == []
    assert "brak mutmuta w interpreterze" in capsys.readouterr().err


# --- HAR-152: zakres `# pragma: no mutate` liczy PragmaVisitor mutmuta -----------
#
# Oczekiwania zweryfikowane niezaleznie od skryptu: `mutmut.mutation.file_mutation.create_mutations`
# nie tworzy mutantow w zglaszanych liniach, a w niezglaszanych liniach kodu je tworzy.


def test_pragma_start_na_koncu_linii_kodu_dziala_jak_zwykla_pragma_i_nie_otwiera_zakresu():
    zrodlo = "def f():\n    a = 1  # pragma: no mutate start\n    b = 2\n    return b + 1\n"
    assert _bez(zrodlo, 2, 3, 4) == [2]


def test_pragma_end_na_koncu_linii_kodu_nie_zamyka_zakresu_start_z_osobnej_linii():
    # `end` w linii z kodem to zwykla pragma; zakres konczy dopiero `end` w osobnej linii (6)
    zrodlo = (
        "def f(x):\n"
        "    # pragma: no mutate start\n"
        "    a = x + 1\n"
        "    b = x + 2  # pragma: no mutate end\n"
        "    c = x + 3\n"
        "    # pragma: no mutate end\n"
        "    return a + b + c\n"
    )
    assert _bez(zrodlo, 3, 4, 5, 7) == [3, 4, 5]


def test_pragma_end_w_poprzedzajacych_liniach_else_nie_zamyka_zakresu_start():
    # `end` w linii 5 stoi przed `else:` i mutmut go nie widzi; zakres konczy dopiero `end` w linii 9
    zrodlo = (
        "def f(a):\n"
        "    # pragma: no mutate start\n"
        "    if a:\n"
        "        x = 1\n"
        "    # pragma: no mutate end\n"
        "    else:\n"
        "        y = 2\n"
        "    z = 3\n"
        "    # pragma: no mutate end\n"
        "    return 4 + 1\n"
    )
    assert _bez(zrodlo, *range(2, 11)) == [2, 3, 4, 5, 6, 7, 8, 9]


def test_komentarze_start_i_end_wewnatrz_nawiasow_nie_sa_pragma_wiec_wartosci_maja_mutanty():
    zrodlo = (
        "def f():\n"
        "    a = g(\n"
        "        1,\n"
        "        # pragma: no mutate start\n"
        "        2,\n"
        "        # pragma: no mutate end\n"
        "    )\n"
        "    return a + 1\n"
    )
    assert _bez(zrodlo, 3, 5, 8) == []


_BLEDNE_PRAGMY = [
    pytest.param(
        "def f():\n    # pragma: no mutate start\n    a = 1\n    return a + 1\n",
        "<zrodlo>:2",
        id="start_bez_end",
    ),
    pytest.param(
        "def f():\n    a = 1\n    # pragma: no mutate end\n    return a + 1\n",
        "<zrodlo>:3",
        id="end_bez_start",
    ),
    pytest.param(
        "def f():\n    # pragma: no mutate start\n    a = 1\n    # pragma: no mutate end\n"
        "    b = 2\n    # pragma: no mutate end\n    return b + 1\n",
        "<zrodlo>:6",
        id="drugi_end_po_zamknietym_zakresie",
    ),
    pytest.param(
        "def f():\n    # pragma: no mutate start\n    a = 1\n    # pragma: no mutate start\n"
        "    return a + 1\n",
        "<zrodlo>:4",
        id="start_w_otwartym_start",
    ),
    pytest.param(
        "def f():\n    # pragma: no mutate start\n    a = 1\n    # pragma: no mutate block\n"
        "    return a + 1\n",
        "<zrodlo>:4",
        id="block_w_otwartym_start",
    ),
    pytest.param(
        "def f():\n    # pragma: no mutate start\n    a = g(\n        1,\n"
        "        # pragma: no mutate end\n    )\n    return a + 1\n",
        "<zrodlo>:2",
        id="end_w_nawiasach_nie_zamyka_start",
    ),
]


@pytest.mark.parametrize(("zrodlo", "polozenie"), _BLEDNE_PRAGMY)
def test_bledna_para_pragm_start_end_to_blad_narzedzia_z_polozeniem_w_zrodle(zrodlo, polozenie):
    # mutmut sam rzuca PragmaParseError na takim pliku, wiec brak dowodu nie moze byc "czysto"
    with pytest.raises(BladNarzedzia, match="bledna pragma no mutate") as blad:
        _bez(zrodlo, 2, 3)
    assert polozenie in str(blad.value)
    assert type(blad.value.__cause__).__name__ == "PragmaParseError"


@pytest.mark.parametrize(
    "zrodlo",
    [
        pytest.param("def f(:\n", id="blad_skladni"),
        pytest.param("x = 1\x0b\n", id="znak_sterujacy"),
    ],
)
def test_zrodlo_odrzucone_przez_parser_libcst_to_blad_narzedzia(zrodlo):
    # Wywolanie prawdziwej `_linie_pragmy`: kazde znalezione zrodlo odrzucane przez libcst
    # odrzuca tez `ast.parse` w `linie_bez_mutantow`, wiec ta galaz nie ma wejscia z tej sciezki.
    import libcst

    with pytest.raises(BladNarzedzia, match="nie da sie sparsowac: Syntax Error") as blad:
        mutanty_zmian._linie_pragmy(zrodlo)
    assert isinstance(blad.value.__cause__, libcst.ParserSyntaxError)


@pytest.mark.parametrize(
    "modul", ["mutmut.mutation.file_mutation", "mutmut.mutation.pragma_handling"]
)
def test_brak_mutmuta_w_interpreterze_to_blad_narzedzia_z_podpowiedzia_o_interpreterze(
    monkeypatch, modul
):
    monkeypatch.setitem(sys.modules, modul, None)
    komunikat = "brak mutmuta w interpreterze.*uruchom interpreterem"
    with pytest.raises(BladNarzedzia, match=komunikat) as blad:
        _bez("def f():\n    return 1\n", 2)
    assert modul in str(blad.value)
    assert isinstance(blad.value.__cause__, ImportError)


def test_brak_libcst_w_interpreterze_to_blad_narzedzia_a_nie_wyjatek_importu(monkeypatch):
    mutanty_zmian._reguly_mutmuta()   # mutmut zaladowany: brakuje tylko libcst
    monkeypatch.setitem(sys.modules, "libcst", None)
    with pytest.raises(BladNarzedzia, match="brak libcst w interpreterze.*uruchom interpreterem") as blad:
        _bez("def f():\n    return 1\n", 2)
    assert isinstance(blad.value.__cause__, ImportError)


def test_brak_libcst_przy_zmianie_pliku_bez_funkcji_daje_kod_2_nie_1(monkeypatch, tmp_path, capsys):
    mutanty_zmian._reguly_mutmuta()   # mutmut zaladowany: brakuje tylko libcst
    monkeypatch.setitem(sys.modules, "libcst", None)
    s = _Srodowisko(diff=_diff_linii(NOWY, 1))
    assert _main(monkeypatch, tmp_path, s, pliki={NOWY: "X = 1\n"}) == 2
    assert "brak libcst w interpreterze" in capsys.readouterr().err


def test_zmieniona_osobna_linia_pragmy_bez_zmiany_kodu_jest_zgloszona_do_sprawdzenia():
    # dodanie samej linii `# pragma: no mutate block` wylacza mutacje niezmienionego kodu pod nia;
    # bez zgloszenia tej linii wynik bylby "czysto" (kod 0), a mutmut niczego by nie sprawdzil
    zrodlo = (
        "def f():\n"
        "    a = 1\n"
        "    # pragma: no mutate block\n"
        "    b = 2\n"
        "    return a + b\n"
    )
    assert _bez(zrodlo, 3) == [3]


def test_zwykly_komentarz_w_funkcji_nadal_nie_jest_zgloszony():
    zrodlo = "def f():\n    a = 1\n    # zwykly komentarz\n    return a\n"
    assert _bez(zrodlo, 3) == []


def test_nazwy_niemutowanych_funkcji_pochodza_z_zainstalowanego_mutmuta(monkeypatch):
    from mutmut.mutation import file_mutation

    monkeypatch.setattr(file_mutation, "NEVER_MUTATE_FUNCTION_NAMES", {"pomijana_przez_mutmuta"})
    zrodlo = "def pomijana_przez_mutmuta():\n    return 1\n\n\ndef __new__():\n    return 2\n"
    assert _bez(zrodlo, 2, 5, 6) == [2]


@pytest.mark.parametrize("nazwa", NAZWY_POMIJANE_PRZEZ_MUTMUTA)
def test_zmieniona_zagniezdzona_metoda_pomijana_przez_mutmuta_jest_zgloszona_a_kod_obok_nie(nazwa):
    zrodlo = (
        "def f():\n"
        "    class X:\n"
        f"        def {nazwa}(cls):\n"
        "            return 1 + 1\n"
        "    return X + 2\n"
    )
    assert _bez(zrodlo, 3, 4, 5) == [3, 4]


def test_zmieniona_zagniezdzona_funkcja_z_dekoratorem_jest_zgloszona_a_kod_obok_nie():
    zrodlo = "def f():\n    @dec\n    def g():\n        return 1 + 1\n    return g + 1\n"
    assert _bez(zrodlo, 2, 3, 4, 5) == [2, 3, 4]


def test_zmieniona_zagniezdzona_funkcja_async_z_dekoratorem_jest_zgloszona_a_kod_obok_nie():
    zrodlo = "def f():\n    @dec\n    async def g():\n        return 1 + 1\n    return g + 1\n"
    assert _bez(zrodlo, 2, 3, 4, 5) == [2, 3, 4]


def test_zmieniona_zagniezdzona_zwykla_funkcja_ma_mutanty_wiec_nie_jest_zgloszona():
    zrodlo = "def f():\n    def g():\n        return 1 + 1\n    return g + 1\n"
    assert _bez(zrodlo, 2, 3, 4) == []


@pytest.mark.parametrize(
    ("zrodlo", "zmienione", "oczekiwane"),
    [
        pytest.param(
            "def f(x):\n    if x:\n        a = 1\n    else:  # pragma: no mutate block\n"
            "        a = 2\n    return a + 1\n",
            (2, 3, 4, 5, 6),
            [4, 5],
            id="block_na_else_obejmuje_tylko_galaz_else",
        ),
        pytest.param(
            "def f(x):\n    if x:\n        a = 1\n    else:  # pragma: no mutate\n"
            "        a = 2\n    return a + 1\n",
            (2, 3, 4, 5, 6),
            [4],
            id="bare_na_else_tylko_naglowek",
        ),
        pytest.param(
            "def f():\n    try:\n        a = 1\n    except ValueError:  # pragma: no mutate\n"
            "        a = 2\n    return a + 1\n",
            (2, 3, 4, 5, 6),
            [4],
            id="bare_na_except_tylko_naglowek",
        ),
        pytest.param(
            "def f():\n    try:\n        a = 1\n    finally:  # pragma: no mutate\n"
            "        a = 2\n    return a + 1\n",
            (2, 3, 4, 5, 6),
            [4],
            id="bare_na_finally_tylko_naglowek",
        ),
        pytest.param(
            "def f(x):\n    match x:  # pragma: no mutate block\n        case 1:\n"
            "            return 2\n    return 3\n",
            (2, 3, 4, 5),
            [2, 3, 4],
            id="block_na_match_obejmuje_wszystkie_case",
        ),
        pytest.param(
            "def f(x):\n    match x:  # pragma: no mutate\n        case 1:\n"
            "            return 2\n    return 3\n",
            (2, 3, 4, 5),
            [2],
            id="bare_na_match_tylko_naglowek",
        ),
        pytest.param(
            "def f(x):\n    match x:\n        case 1:  # pragma: no mutate\n"
            "            return 2\n    return 3\n",
            (2, 3, 4, 5),
            [3],
            id="bare_na_case_tylko_ta_linia",
        ),
    ],
)
def test_pragma_na_naglowku_galezi_i_match_obejmuje_zakres_wedlug_rodzaju_tokenu(
    zrodlo, zmienione, oczekiwane
):
    assert _bez(zrodlo, *zmienione) == oczekiwane


def test_osobna_linia_pragma_block_przed_funkcja_modulu_zglasza_ja_a_poprzednia_nie():
    zrodlo = "def g():\n    return 1 + 1\n\n\n# pragma: no mutate block\ndef f():\n    return 2 + 1\n"
    assert _bez(zrodlo, 2, 6, 7) == [6, 7]


def test_osobna_linia_pragma_block_przed_metoda_zglasza_ja_a_poprzednia_nie():
    zrodlo = (
        "class K:\n"
        "    def a(self):\n"
        "        return 1 + 1\n"
        "\n"
        "    # pragma: no mutate block\n"
        "    def b(self):\n"
        "        return 2 + 1\n"
    )
    assert _bez(zrodlo, 3, 6, 7) == [6, 7]


def test_pragma_block_w_ciele_except_zmienionej_funkcji_zglasza_tylko_to_cialo():
    zrodlo = (
        "def f():\n"
        "    try:\n"
        "        a = 1\n"
        "    except ValueError:\n"
        "        # pragma: no mutate block\n"
        "        b = 2\n"
        "    return a\n"
    )
    assert _bez(zrodlo, 3, 6, 7) == [6]


@pytest.mark.parametrize("token", ["", " block"])
def test_osobna_linia_pragma_wewnatrz_nawiasow_nie_jest_pragma_wiec_wartosc_ma_mutant(token):
    zrodlo = (
        "def f():\n"
        "    a = g(\n"
        f"        # pragma: no mutate{token}\n"
        "        1,\n"
        "    )\n"
        "    return a + 1\n"
    )
    assert _bez(zrodlo, 2, 4, 6) == []


# --- HAR-152: osobna linia `block` przed `case` obejmuje kolejne przypadki `match` ---

ZRODLO_MATCH_Z_PRAGMA_BLOCK = (
    "def f(x):\n"
    "    match x:\n"
    "        case 1:\n"
    '            return "a"\n'
    "        # pragma: no mutate block\n"
    "        case 2 if x > 1:\n"
    '            return "b"\n'
    "        case 3:\n"
    '            return "d"\n'
    '    return "c"\n'
)


def test_zmieniony_wzorzec_przypadku_po_osobnej_pragmie_block_jest_zgloszony_a_przed_pragma_nie():
    zrodlo = (
        "def f(x):\n"
        "    match x:\n"
        "        case 1:\n"
        '            return "a"\n'
        "        # pragma: no mutate block\n"
        "        case 2 if x > 1:\n"
        '            return "b"\n'
        '    return "c"\n'
    )
    assert _bez(zrodlo, 3, 6) == [6]


def test_zmieniony_drugi_przypadek_po_pragmie_block_jest_zgloszony_a_linia_po_match_nie():
    assert _bez(ZRODLO_MATCH_Z_PRAGMA_BLOCK, 8, 10) == [8]


def test_pragma_block_przed_przypadkiem_zglasza_wzorzec_i_cialo_kazdego_kolejnego_przypadku():
    assert _bez(ZRODLO_MATCH_Z_PRAGMA_BLOCK, 4, 6, 7, 8, 9, 10) == [6, 7, 8, 9]


def test_pragma_block_przed_przypadkiem_zglasza_wszystkie_linie_wieloliniowej_instrukcji_ciala():
    zrodlo = (
        "def f(x):\n"
        "    match x:\n"
        "        case 1:\n"
        "            a = 1\n"
        "        # pragma: no mutate block\n"
        "        case 2:\n"
        "            b = 2\n"
        "            c = (\n"
        "                3,\n"
        "            )\n"
        "    return 0 + 1\n"
    )
    assert _bez(zrodlo, 4, 6, 7, 8, 9, 11) == [6, 7, 8, 9]


def test_pragma_block_w_ciele_przypadku_zglasza_dalsze_instrukcje_ciala_ale_nie_nastepny_przypadek():
    zrodlo = (
        "def f(x):\n"
        "    match x:\n"
        "        case 1:\n"
        "            a = 1\n"
        "            # pragma: no mutate block\n"
        "            b = 2\n"
        "        case 2:\n"
        "            c = 3\n"
        "    return 0 + 1\n"
    )
    assert _bez(zrodlo, 4, 6, 8, 9) == [6]


# =============================================================================
# HAR-236: pomiar faz, --max-children i tryb przyrostowy --od-przebiegu
# =============================================================================

FAZY_CZASU = [
    "wybor_funkcji",
    "mutmut_statystyki",
    "mutmut_przygotowanie",
    "mutmut_mutanty",
    "mutmut_results",
    "razem",
]
BRAK_POMIARU = "brak pomiaru"


def _czasy(err: str) -> dict[str, str]:
    """Faza -> tekst po dwukropku z linii `czas fazy ...`, w kolejnosci wypisania."""
    wynik: dict[str, str] = {}
    for linia in err.splitlines():
        if linia.startswith("czas fazy "):
            nazwa, _, wartosc = linia.removeprefix("czas fazy ").partition(": ")
            assert nazwa not in wynik, f"faza {nazwa} wypisana dwa razy"
            wynik[nazwa] = wartosc
    return wynik


def _sekundy(wartosc: str) -> float:
    assert re.fullmatch(r"\d+\.\d s", wartosc), f"to nie jest pomiar z jednym miejscem: {wartosc!r}"
    return float(wartosc.removesuffix(" s"))


# --- czas faz na stderr (mutmut podmieniony, bez linii markerow) -------------------------------


def test_przebieg_wypisuje_na_stderr_czas_kazdej_fazy_w_ustalonej_kolejnosci(
    monkeypatch, tmp_path, capsys
):
    s = _Srodowisko(diff=_diff_a(), results=_wyniki((KLUCZ_A, "killed")))

    assert _main(monkeypatch, tmp_path, s) == 0

    czasy = _czasy(capsys.readouterr().err)
    assert list(czasy) == FAZY_CZASU
    for faza in ("wybor_funkcji", "mutmut_results", "razem"):
        _sekundy(czasy[faza])


def test_faza_mutmuta_bez_linii_markera_w_wyjsciu_nie_ma_pomiaru_zamiast_zera(
    monkeypatch, tmp_path, capsys
):
    # Wyjscie `mutmut run` nie ma ani `Running stats`, ani `Running clean tests`, ani
    # `Running mutation testing`: nic nie wolno zapisac jako `0.0 s` (measured_or_null).
    s = _Srodowisko(
        diff=_diff_a(), run_out="Generating mutants\ndone\n", results=_wyniki((KLUCZ_A, "killed"))
    )

    assert _main(monkeypatch, tmp_path, s) == 0

    czasy = _czasy(capsys.readouterr().err)
    for faza in ("mutmut_statystyki", "mutmut_przygotowanie", "mutmut_mutanty"):
        assert czasy[faza] == BRAK_POMIARU, faza


def test_gdy_mutmut_nie_byl_wolany_zadna_jego_faza_nie_ma_pomiaru(monkeypatch, tmp_path, capsys):
    s = _Srodowisko()

    assert _main(monkeypatch, tmp_path, s) == 0

    assert s.mutmut("run") == []
    czasy = _czasy(capsys.readouterr().err)
    assert list(czasy) == FAZY_CZASU
    for faza in ("mutmut_statystyki", "mutmut_przygotowanie", "mutmut_mutanty", "mutmut_results"):
        assert czasy[faza] == BRAK_POMIARU, faza
    _sekundy(czasy["wybor_funkcji"])
    _sekundy(czasy["razem"])


def test_czas_faz_jest_wypisany_takze_przy_bledzie_mutmuta_z_pomiarem_tylko_faz_ktore_sie_odbyly(
    monkeypatch, tmp_path, capsys
):
    s = _Srodowisko(diff=_diff_a(), run_rc=1, run_out="Traceback: cos innego")

    assert _main(monkeypatch, tmp_path, s) == 2

    zrzut = capsys.readouterr()
    assert zrzut.out == ""
    czasy = _czasy(zrzut.err)
    assert list(czasy) == FAZY_CZASU
    _sekundy(czasy["wybor_funkcji"])
    _sekundy(czasy["razem"])
    for faza in ("mutmut_statystyki", "mutmut_przygotowanie", "mutmut_mutanty", "mutmut_results"):
        assert czasy[faza] == BRAK_POMIARU, faza  # `mutmut results` w ogole sie nie odbylo


def test_linie_czasu_nie_zmieniaja_stdout_ani_kodu_wyjscia(monkeypatch, tmp_path, capsys):
    s = _Srodowisko(diff=_diff_a(), results=_wyniki((KLUCZ_A, "survived")))

    assert _main(monkeypatch, tmp_path, s) == 1

    zrzut = capsys.readouterr()
    assert zrzut.out == f"{KLUCZ_A}\n"
    assert "czas fazy" not in zrzut.out


# --- --max-children ---------------------------------------------------------------------------


def test_max_children_trafia_do_mutmut_run_przed_wzorcami(monkeypatch, tmp_path):
    s = _Srodowisko(diff=_diff("src/cockpit/services/a.py", "@@ -1,5 +1,5 @@"))

    _main(monkeypatch, tmp_path, s, "--max-children", "3")

    assert s.mutmut("run")[0] == [
        sys.executable,
        "-m",
        "mutmut",
        "run",
        "--max-children",
        "3",
        "cockpit.services.a.x_inna__mutmut_*",
        "cockpit.services.a.x_zapisz__mutmut_*",
    ]


@pytest.mark.parametrize("liczba", ["1", "8", "64"])
def test_max_children_przekazuje_wartosc_doslownie_takze_najmniejsza_dozwolona(
    monkeypatch, tmp_path, liczba
):
    s = _Srodowisko(diff=_diff_a())

    _main(monkeypatch, tmp_path, s, "--max-children", liczba)

    assert s.mutmut("run")[0][4:6] == ["--max-children", liczba]


def test_bez_max_children_mutmut_run_nie_dostaje_tej_opcji(monkeypatch, tmp_path):
    s = _Srodowisko(diff=_diff_a())

    _main(monkeypatch, tmp_path, s)

    assert "--max-children" not in s.mutmut("run")[0]


# "٣" (cyfra arabsko-indyjska) przechodzi isdigit() i int(), "+3", " 3" i "1_0" przechodza int():
# kazdy odrzuca tylko jeden warunek `_liczba_dzieci`.
@pytest.mark.parametrize(
    "wartosc",
    [
        "0",
        "-1",
        "abc",
        "1.5",
        "",
        pytest.param("٣", id="cyfra_arabsko_indyjska"),
        pytest.param("+3", id="plus_przed_liczba"),
        pytest.param(" 3", id="spacja_przed_liczba"),
        pytest.param("1_0", id="podkreslnik_w_liczbie"),
    ],
)
def test_zla_wartosc_max_children_to_blad_argumentow_kodem_2_bez_uruchamiania_czegokolwiek(
    monkeypatch, tmp_path, wartosc
):
    s = _Srodowisko(diff=_diff_a())

    with pytest.raises(SystemExit) as wyjscie:
        _main(monkeypatch, tmp_path, s, "--max-children", wartosc)

    assert wyjscie.value.code == 2
    assert s.wywolania == []


# --- zapis przebiegu: repozytorium git w tmp_path, mutmut podmieniony -------------------------

SCIEZKA_A = "src/cockpit/services/a.py"
SCIEZKA_K = "src/cockpit/services/k.py"
KLUCZ_ZAPISZ = KLUCZ_A  # cockpit.services.a.x_zapisz__mutmut_1
KLUCZ_INNA = "cockpit.services.a.x_inna__mutmut_1"
WZ_ZAPISZ = "cockpit.services.a.x_zapisz__mutmut_*"
WZ_INNA = "cockpit.services.a.x_inna__mutmut_*"
WZ_METODA_K = f"cockpit.services.k.x{ZNAK}K{ZNAK}m__mutmut_*"
OBA_WZORCE = [WZ_INNA, WZ_ZAPISZ]  # posortowane, jak w `wzorce`
PYPROJECT_PRZEBIEGU = '[tool.mutmut]\nsource_paths = ["src/cockpit/services/"]\n'
TRESC_TESTU_A = "def test_a():\n    assert True\n"
SKROT_SHA256 = re.compile(r"[0-9a-f]{64}")


def _zrodlo_a(zapisz: int, inna: int) -> str:
    return f"def zapisz():\n    return {zapisz}\n\n\ndef inna():\n    return {inna}\n"


def _git_t(katalog: Path, *argv: str) -> str:
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
        wyjscie, blad = proces.communicate(timeout=LIMIT_PROCESU_POMOCNICZEGO)
    finally:
        zakoncz_proces_sync(proces, nazwa="git w repozytorium przebiegu")
    assert proces.returncode == 0, blad
    return wyjscie


def _repo_git(korzen: Path) -> Path:
    """Repozytorium z jednym commitem: funkcje `zapisz` i `inna` (zwracaja 1 i 2) oraz dwa pliki testow."""
    pliki = {
        "pyproject.toml": PYPROJECT_PRZEBIEGU,
        ".gitignore": "__pycache__/\n",
        SCIEZKA_A: _zrodlo_a(1, 2),
        "tests/test_a.py": TRESC_TESTU_A,
        "tests/test_c.py": "def test_c():\n    assert True\n",
    }
    for sciezka, tresc in pliki.items():
        cel = korzen / sciezka
        cel.parent.mkdir(parents=True, exist_ok=True)
        cel.write_text(tresc, encoding="utf-8")
    _git_t(korzen, "init", "-q")
    _git_t(korzen, "add", "-A")
    _git_t(korzen, "commit", "-q", "-m", "start")
    return korzen


def _wzorce_z_argv(argv: list[str]) -> list[str]:
    reszta = argv[4:]
    return reszta[2:] if reszta[:1] == ["--max-children"] else reszta


class _FalszywyMutmut:
    """Zastepuje `python -m mutmut run|results`; git idzie przez prawdziwe `uruchom`.

    `results` zwraca statusy mutantow z `mutanty` tylko dla wzorcow OSTATNIEGO `run`, a nie
    z calego cache: przezywajacy mutant niezmutowanej funkcji moze wiec trafic do wyniku
    wylacznie z zapisu przebiegu."""

    def __init__(self) -> None:
        self.mutanty: dict[str, str] = {}
        self.runy: list[list[str]] = []
        self.run_rc = 0
        self.run_out = ""
        self.results_wszystkie = False  # prawdziwe `results` pokazuje caly cache, nie tylko ostatni run
        self._wzorce_ostatniego_runu: list[str] = []

    def __call__(self, argv, *args, **kwargs):
        if argv[1:3] != ["-m", "mutmut"]:
            return PRAWDZIWE_URUCHOM(argv, *args, **kwargs)
        if argv[3] == "run":
            self.runy.append(list(argv))
            self._wzorce_ostatniego_runu = _wzorce_z_argv(argv)
            return subprocess.CompletedProcess(argv, self.run_rc, stdout=self.run_out, stderr="")
        assert argv[3] == "results", argv
        linie = "".join(
            f"    {klucz}: {status}\n"
            for klucz, status in self.mutanty.items()
            if self.results_wszystkie
            or any(fnmatch.fnmatchcase(klucz, w) for w in self._wzorce_ostatniego_runu)
        )
        return subprocess.CompletedProcess(argv, 0, stdout=f"Results:\n{linie}", stderr="")

    def wzorce_runow(self) -> list[list[str]]:
        return [_wzorce_z_argv(argv) for argv in self.runy]


class _Przyrost:
    """Repozytorium + zapis przebiegu w tmp_path; `przebieg()` to prawdziwe `main` z `--od-przebiegu`."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.korzen = _repo_git(tmp_path / "repo")
        self.zapis = tmp_path / "przebieg.json"
        self.mutmut = _FalszywyMutmut()
        monkeypatch.setattr(mutanty_zmian, "uruchom", self.mutmut)
        monkeypatch.chdir(self.korzen)

    def zrodlo(self, zapisz: int = 1, inna: int = 2) -> None:
        self.plik(SCIEZKA_A, _zrodlo_a(zapisz, inna))

    def plik(self, sciezka: str, tresc: str) -> None:
        cel = self.korzen / sciezka
        cel.parent.mkdir(parents=True, exist_ok=True)
        cel.write_text(tresc, encoding="utf-8")

    def git(self, *argv: str) -> None:
        _git_t(self.korzen, *argv)

    def przebieg(self, *argv: str, z_zapisem: bool = True) -> int:
        opcje = ["--od-przebiegu", str(self.zapis)] if z_zapisem else []
        return mutanty_zmian.main(["--korzen", str(self.korzen), "--baza", "HEAD", *opcje, *argv])

    def wczytaj_zapis(self) -> dict:
        return json.loads(self.zapis.read_text(encoding="utf-8"))


@pytest.fixture
def przyrost(tmp_path, monkeypatch) -> _Przyrost:
    return _Przyrost(tmp_path, monkeypatch)


def _linie_stderr(zrzut) -> list[str]:
    return zrzut.err.splitlines()


def test_runda_poprawek_mutuje_tylko_funkcje_zmieniona_od_poprzedniego_przebiegu(
    przyrost, capsys
):
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg() == 0
    pierwszy = capsys.readouterr()

    przyrost.zrodlo(zapisz=5, inna=4)  # poprawka tylko w `zapisz`; `inna` ma ten sam tekst co w przebiegu 1
    assert przyrost.przebieg() == 0
    drugi = capsys.readouterr()

    assert przyrost.mutmut.wzorce_runow() == [OBA_WZORCE, [WZ_ZAPISZ]]
    assert "przyrostowo: mutuję 2 z 2 funkcji" in _linie_stderr(pierwszy)
    assert "przyrostowo: mutuję 1 z 2 funkcji" in _linie_stderr(drugi)


def test_pierwszy_przebieg_bez_zapisu_mutuje_wszystko_z_powodem_brak_zapisu(przyrost, capsys):
    przyrost.zrodlo(zapisz=3, inna=4)
    assert not przyrost.zapis.exists()

    assert przyrost.przebieg() == 0

    linie = _linie_stderr(capsys.readouterr())
    assert "przyrostowo: zapis unieważniony — brak zapisu" in linie
    assert "przyrostowo: mutuję 2 z 2 funkcji" in linie
    assert przyrost.mutmut.wzorce_runow() == [OBA_WZORCE]


def test_przezywajacy_mutant_z_poprzedniego_przebiegu_w_niezmienionej_funkcji_nadal_daje_kod_1(
    przyrost, capsys
):
    przyrost.mutmut.mutanty[KLUCZ_INNA] = "survived"
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg() == 1
    assert capsys.readouterr().out == f"{KLUCZ_INNA}\n"
    przyrost.mutmut.mutanty[KLUCZ_ZAPISZ] = "killed"

    przyrost.zrodlo(zapisz=5, inna=4)  # `inna` bez zmian, `zapisz` mutowana od nowa i zabita
    kod = przyrost.przebieg()

    zrzut = capsys.readouterr()
    assert przyrost.mutmut.wzorce_runow()[-1] == [WZ_ZAPISZ]  # `mutmut results` nie widzi `inna`
    assert kod == 1
    assert zrzut.out == f"{KLUCZ_INNA}\n"


def test_przezywajace_z_zapisu_i_nowo_przezywajace_sa_na_stdout_razem_posortowane(
    przyrost, capsys
):
    przyrost.mutmut.mutanty[KLUCZ_INNA] = "no tests"
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg() == 1
    capsys.readouterr()
    przyrost.mutmut.mutanty[KLUCZ_ZAPISZ] = "survived"

    przyrost.zrodlo(zapisz=5, inna=4)

    assert przyrost.przebieg() == 1
    assert capsys.readouterr().out == f"{KLUCZ_INNA}\n{KLUCZ_ZAPISZ}\n"


def test_funkcja_zmutowana_od_nowa_nie_dziedziczy_przezywajacych_z_zapisu_gdy_nowy_przebieg_je_zabil(
    przyrost, capsys
):
    przyrost.mutmut.mutanty[KLUCZ_ZAPISZ] = "survived"
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg() == 1
    capsys.readouterr()
    przyrost.mutmut.mutanty[KLUCZ_ZAPISZ] = "killed"  # po poprawce testy zabijaja mutanta

    przyrost.zrodlo(zapisz=5, inna=4)

    assert przyrost.przebieg() == 0
    assert capsys.readouterr().out == ""
    assert przyrost.wczytaj_zapis()["funkcje"][WZ_ZAPISZ]["przezywajace"] == []


@pytest.mark.parametrize(
    ("ocalaly", "lista_kodu_3", "kod"),
    [
        pytest.param(True, False, 1, id="przezywajacy_w_zapisie_to_kod_1"),
        pytest.param(False, False, 0, id="zapis_bez_przezywajacych_to_kod_0"),
        pytest.param(False, True, 3, id="lista_kodu_3_bez_przezywajacych_to_kod_3"),
    ],
)
def test_przebieg_bez_zmian_funkcji_bierze_wynik_z_zapisu_i_nie_wola_mutmuta(
    przyrost, capsys, ocalaly, lista_kodu_3, kod
):
    if lista_kodu_3:  # nowy plik spoza `source_paths`: mutmut go nie mutuje (kod 3 w kazdym przebiegu)
        przyrost.plik("src/cockpit/api/nowy.py", "def f():\n    return 1\n")
    if ocalaly:
        przyrost.mutmut.mutanty[KLUCZ_INNA] = "survived"
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg() == kod
    pierwszy = capsys.readouterr()

    assert przyrost.przebieg() == kod

    drugi = capsys.readouterr()
    assert len(przyrost.mutmut.runy) == 1  # drugi przebieg nie zawolal `mutmut run`
    assert drugi.out == pierwszy.out
    assert "przyrostowo: mutuję 0 z 2 funkcji" in _linie_stderr(drugi)


def test_przebieg_z_samego_zapisu_nie_mierzy_faz_mutmuta(przyrost, capsys):
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg() == 0
    capsys.readouterr()

    assert przyrost.przebieg() == 0

    czasy = _czasy(capsys.readouterr().err)
    for faza in ("mutmut_statystyki", "mutmut_przygotowanie", "mutmut_mutanty", "mutmut_results"):
        assert czasy[faza] == BRAK_POMIARU, faza
    _sekundy(czasy["wybor_funkcji"])
    _sekundy(czasy["razem"])


def test_funkcja_nieobecna_w_zapisie_jest_mutowana_a_reszta_idzie_z_zapisu(przyrost, capsys):
    przyrost.zrodlo(zapisz=3, inna=2)  # `inna` bez zmian wzgledem bazy: poza lista
    assert przyrost.przebieg() == 0
    assert list(przyrost.wczytaj_zapis()["funkcje"]) == [WZ_ZAPISZ]
    capsys.readouterr()

    przyrost.zrodlo(zapisz=3, inna=4)

    assert przyrost.przebieg() == 0
    assert przyrost.mutmut.wzorce_runow()[-1] == [WZ_INNA]
    assert "przyrostowo: mutuję 1 z 2 funkcji" in _linie_stderr(capsys.readouterr())
    assert set(przyrost.wczytaj_zapis()["funkcje"]) == set(OBA_WZORCE)


def _uszkodzenie_bez_klucza(*sciezka: str):
    def uszkodz(zapis: dict) -> str:
        kopia = json.loads(json.dumps(zapis))
        cel = kopia
        for klucz in sciezka[:-1]:
            cel = cel[klucz]
        del cel[sciezka[-1]]
        return json.dumps(kopia)

    return uszkodz


def _uszkodzenie_wartosci(*sciezka: str, wartosc):
    def uszkodz(zapis: dict) -> str:
        kopia = json.loads(json.dumps(zapis))
        cel = kopia
        for klucz in sciezka[:-1]:
            cel = cel[klucz]
        cel[sciezka[-1]] = wartosc
        return json.dumps(kopia)

    return uszkodz


USZKODZENIA_ZAPISU = {
    "nie_json": lambda zapis: "{niepoprawny json",
    "pusty_plik": lambda zapis: "",
    "bajty_spoza_utf8": lambda zapis: b"\xff\xfe\xfa{",
    "json_nie_jest_obiektem": lambda zapis: "[]",
    "inna_wersja": _uszkodzenie_wartosci("wersja", wartosc=2),
    "brak_wersji": _uszkodzenie_bez_klucza("wersja"),
    "brak_odcisku_testow": _uszkodzenie_bez_klucza("odcisk_testow"),
    "funkcje_nie_sa_obiektem": _uszkodzenie_wartosci("funkcje", wartosc=[]),
    "wpis_funkcji_bez_odcisku": _uszkodzenie_bez_klucza("funkcje", WZ_INNA, "odcisk"),
    "wpis_funkcji_bez_przezywajacych": _uszkodzenie_bez_klucza(
        "funkcje", WZ_INNA, "przezywajace"
    ),
    "przezywajace_nie_sa_lista": _uszkodzenie_wartosci(
        "funkcje", WZ_INNA, "przezywajace", wartosc="mutant"
    ),
    # JSON `true` == 1 w Pythonie: tylko straznik typu bool odrzuca taka wersje
    "wersja_to_logiczne_true": _uszkodzenie_wartosci("wersja", wartosc=True),
    "odcisk_testow_nie_jest_napisem": _uszkodzenie_wartosci("odcisk_testow", wartosc=5),
    "wpis_funkcji_nie_jest_obiektem": _uszkodzenie_wartosci("funkcje", WZ_INNA, wartosc="tekst"),
    "odcisk_funkcji_nie_jest_napisem": _uszkodzenie_wartosci(
        "funkcje", WZ_INNA, "odcisk", wartosc=7
    ),
    "przezywajace_zawieraja_nie_napis": _uszkodzenie_wartosci(
        "funkcje", WZ_INNA, "przezywajace", wartosc=[1]
    ),
    # json.loads rzuca RecursionError (nie ValueError) na zbyt glebokim zagniezdzeniu
    "zbyt_gleboko_zagniezdzony_json": lambda zapis: "[" * 200000,
}


@pytest.mark.parametrize("uszkodzenie", list(USZKODZENIA_ZAPISU))
def test_nieczytelny_zapis_daje_pelny_przebieg_z_powodem_nieczytelny_zapis(
    przyrost, capsys, uszkodzenie
):
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg() == 0
    capsys.readouterr()
    nowa_tresc = USZKODZENIA_ZAPISU[uszkodzenie](przyrost.wczytaj_zapis())
    if isinstance(nowa_tresc, bytes):
        przyrost.zapis.write_bytes(nowa_tresc)
    else:
        przyrost.zapis.write_text(nowa_tresc, encoding="utf-8")

    # Zrodla bez zmian od przebiegu 1: gdyby zapis nie byl odrzucony, nic nie bylo do mutowania.
    assert przyrost.przebieg() == 0

    linie = _linie_stderr(capsys.readouterr())
    assert "przyrostowo: zapis unieważniony — nieczytelny zapis" in linie
    assert "przyrostowo: mutuję 2 z 2 funkcji" in linie
    assert przyrost.mutmut.wzorce_runow() == [OBA_WZORCE, OBA_WZORCE]
    assert przyrost.wczytaj_zapis()["wersja"] == 1  # zapis naprawiony po pelnym przebiegu


def _zmien_tresc_pliku_testow(przyrost: _Przyrost) -> None:
    przyrost.plik("tests/test_a.py", "def test_a():\n    assert 1 == 1\n")


def _dodaj_plik_testow(przyrost: _Przyrost) -> None:
    przyrost.plik("tests/test_nowy.py", "def test_nowy():\n    assert True\n")


def _przenies_plik_testow_bez_zmiany_tresci(przyrost: _Przyrost) -> None:
    przyrost.git("mv", "tests/test_a.py", "tests/test_b.py")


def _usun_plik_testow(przyrost: _Przyrost) -> None:
    przyrost.git("rm", "-q", "tests/test_c.py")


def _zmien_pyproject(przyrost: _Przyrost) -> None:
    przyrost.plik("pyproject.toml", PYPROJECT_PRZEBIEGU + "# zmiana konfiguracji\n")


@pytest.mark.parametrize(
    "zmiana",
    [
        pytest.param(_zmien_tresc_pliku_testow, id="zmiana_tresci_sledzonego_pliku_testow"),
        pytest.param(_dodaj_plik_testow, id="nowy_nieslezony_plik_testow"),
        pytest.param(_przenies_plik_testow_bez_zmiany_tresci, id="ta_sama_tresc_inna_sciezka"),
        pytest.param(_usun_plik_testow, id="usuniecie_pliku_testow"),
        pytest.param(_zmien_pyproject, id="zmiana_pyproject_toml"),
    ],
)
def test_zmiana_testow_albo_pyproject_uniewaznia_zapis_i_przelicza_wszystkie_funkcje(
    przyrost, capsys, zmiana
):
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg() == 0
    capsys.readouterr()
    zmiana(przyrost)

    assert przyrost.przebieg() == 0

    linie = _linie_stderr(capsys.readouterr())
    assert "przyrostowo: zapis unieważniony — zmiana testów" in linie
    assert "przyrostowo: mutuję 2 z 2 funkcji" in linie
    assert przyrost.mutmut.wzorce_runow() == [OBA_WZORCE, OBA_WZORCE]


def _dodaj_plik_poza_testami(przyrost: _Przyrost) -> None:
    przyrost.plik("docs/notatka.md", "notatka\n")


def _dodaj_plik_ignorowany_przez_git_pod_testami(przyrost: _Przyrost) -> None:
    przyrost.plik("tests/__pycache__/test_a.cpython-314.pyc", "bajtkod")


@pytest.mark.parametrize(
    "zmiana",
    [
        pytest.param(_dodaj_plik_poza_testami, id="plik_poza_testami_i_pyproject"),
        pytest.param(_dodaj_plik_ignorowany_przez_git_pod_testami, id="plik_ignorowany_przez_git"),
    ],
)
def test_zmiana_poza_testami_i_pyproject_nie_uniewaznia_zapisu(przyrost, capsys, zmiana):
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg() == 0
    capsys.readouterr()
    zmiana(przyrost)

    assert przyrost.przebieg() == 0

    linie = _linie_stderr(capsys.readouterr())
    assert not [x for x in linie if x.startswith("przyrostowo: zapis unieważniony")]
    assert "przyrostowo: mutuję 0 z 2 funkcji" in linie
    assert len(przyrost.mutmut.runy) == 1


def test_odcisk_funkcji_obejmuje_jej_dekorator(przyrost, capsys):
    przyrost.plik(SCIEZKA_K, "class K:\n    @staticmethod\n    def m():\n        return 1\n")
    przyrost.git("add", "-A")
    przyrost.git("commit", "-q", "-m", "klasa")
    przyrost.plik(SCIEZKA_K, "class K:\n    @staticmethod\n    def m():\n        return 2\n")
    assert przyrost.przebieg() == 0
    capsys.readouterr()

    # Ten sam tekst ciala, zmieniony wylacznie dekorator.
    przyrost.plik(SCIEZKA_K, "class K:\n    @classmethod\n    def m():\n        return 2\n")

    assert przyrost.przebieg() == 0
    assert przyrost.mutmut.wzorce_runow() == [[WZ_METODA_K], [WZ_METODA_K]]
    assert "przyrostowo: mutuję 1 z 1 funkcji" in _linie_stderr(capsys.readouterr())


@pytest.mark.parametrize(
    ("przezywajaca_inna", "plik_poza_zasiegiem", "kod"),
    [
        pytest.param(False, False, 0, id="kod_0"),
        pytest.param(True, False, 1, id="kod_1"),
        pytest.param(False, True, 3, id="kod_3"),
    ],
)
def test_po_przebiegu_z_kodem_0_1_albo_3_zapis_ma_wpis_dla_kazdego_wzorca(
    przyrost, przezywajaca_inna, plik_poza_zasiegiem, kod
):
    if przezywajaca_inna:
        przyrost.mutmut.mutanty[KLUCZ_INNA] = "survived"
    if plik_poza_zasiegiem:
        przyrost.plik("src/cockpit/api/nowy.py", "def f():\n    return 1\n")
    przyrost.zrodlo(zapisz=3, inna=4)

    assert przyrost.przebieg() == kod

    zapis = przyrost.wczytaj_zapis()
    assert zapis["wersja"] == 1
    assert SKROT_SHA256.fullmatch(zapis["odcisk_testow"])
    assert set(zapis["funkcje"]) == set(OBA_WZORCE)
    for wpis in zapis["funkcje"].values():
        assert SKROT_SHA256.fullmatch(wpis["odcisk"])
    # Rozne funkcje maja rozne odciski (odcisk liczony z tekstu funkcji, nie stala).
    assert zapis["funkcje"][WZ_INNA]["odcisk"] != zapis["funkcje"][WZ_ZAPISZ]["odcisk"]
    assert zapis["funkcje"][WZ_INNA]["przezywajace"] == ([KLUCZ_INNA] if przezywajaca_inna else [])
    assert zapis["funkcje"][WZ_ZAPISZ]["przezywajace"] == []


def test_nowy_zapis_przenosi_wpis_niezmienionej_funkcji_bez_zmian_a_zmienionej_przelicza(
    przyrost,
):
    przyrost.mutmut.mutanty[KLUCZ_INNA] = "survived"
    przyrost.zrodlo(zapisz=3, inna=4)
    przyrost.przebieg()
    stary = przyrost.wczytaj_zapis()

    przyrost.zrodlo(zapisz=5, inna=4)
    przyrost.przebieg()

    nowy = przyrost.wczytaj_zapis()
    assert nowy["funkcje"][WZ_INNA] == stary["funkcje"][WZ_INNA]
    assert nowy["funkcje"][WZ_ZAPISZ]["odcisk"] != stary["funkcje"][WZ_ZAPISZ]["odcisk"]
    assert nowy["odcisk_testow"] == stary["odcisk_testow"]


@pytest.mark.parametrize("zmien_pozostala", [False, True], ids=["bez_mutowania", "z_mutowaniem"])
def test_po_przebiegu_zapis_zawiera_tylko_wzorce_biezacej_listy(
    przyrost, capsys, zmien_pozostala
):
    przyrost.mutmut.mutanty[KLUCZ_INNA] = "survived"
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg() == 1
    assert set(przyrost.wczytaj_zapis()["funkcje"]) == set(OBA_WZORCE)
    capsys.readouterr()

    # `inna` wraca do tekstu z bazy, wiec wypada z listy zmienionych funkcji.
    przyrost.zrodlo(zapisz=5 if zmien_pozostala else 3, inna=2)
    kod = przyrost.przebieg()

    zrzut = capsys.readouterr()
    assert list(przyrost.wczytaj_zapis()["funkcje"]) == [WZ_ZAPISZ]
    assert kod == 0  # przezywajacy mutant `inna` nie moze wrocic z zapisu: funkcji nie ma na liscie
    assert zrzut.out == ""
    ile = 1 if zmien_pozostala else 0
    assert f"przyrostowo: mutuję {ile} z 1 funkcji" in _linie_stderr(zrzut)


def test_blad_mutmuta_nie_nadpisuje_zapisu_a_nastepny_przebieg_liczy_od_starego_stanu(
    przyrost, capsys
):
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg() == 0
    bajty = przyrost.zapis.read_bytes()
    capsys.readouterr()
    przyrost.zrodlo(zapisz=5, inna=4)
    przyrost.mutmut.run_rc = 1
    przyrost.mutmut.run_out = "Traceback: cos innego"

    assert przyrost.przebieg() == 2

    assert przyrost.zapis.read_bytes() == bajty
    przyrost.mutmut.run_rc = 0
    assert przyrost.przebieg() == 0
    # Gdyby nieudany przebieg zapisal odcisk `zapisz`, trzeci przebieg nie mialby czego mutowac.
    assert przyrost.mutmut.wzorce_runow() == [OBA_WZORCE, [WZ_ZAPISZ], [WZ_ZAPISZ]]


@pytest.mark.parametrize("wariant", ["brak_katalogu_zapisu", "zapis_jest_katalogiem"])
def test_nieudany_zapis_przebiegu_to_blad_narzedzia_z_kodem_2(przyrost, tmp_path, capsys, wariant):
    if wariant == "brak_katalogu_zapisu":
        przyrost.zapis = tmp_path / "nie-ma-takiego-katalogu" / "przebieg.json"
    else:
        przyrost.zapis.mkdir()
    przyrost.zrodlo(zapisz=3, inna=4)

    assert przyrost.przebieg() == 2

    assert "blad: " in capsys.readouterr().err


def test_bez_opcji_od_przebiegu_nie_ma_linii_przyrostowo_ani_zapisu(przyrost, capsys):
    przyrost.zrodlo(zapisz=3, inna=4)

    assert przyrost.przebieg(z_zapisem=False) == 0

    assert not [x for x in _linie_stderr(capsys.readouterr()) if x.startswith("przyrostowo:")]
    assert not przyrost.zapis.exists()
    assert przyrost.mutmut.wzorce_runow() == [OBA_WZORCE]


def test_max_children_dziala_razem_z_trybem_przyrostowym(przyrost):
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg("--max-children", "2") == 0
    przyrost.zrodlo(zapisz=5, inna=4)

    assert przyrost.przebieg("--max-children", "2") == 0

    assert przyrost.mutmut.runy[-1] == [
        sys.executable,
        "-m",
        "mutmut",
        "run",
        "--max-children",
        "2",
        WZ_ZAPISZ,
    ]


# --- pomiar faz z prawdziwego strumienia (atrapa `python -m mutmut` jako skrypt) -----------------

# Atrapa pisze markery jak spinner mutmuta: czesc z `\r` (bez konca linii), czesc z `\n`.
# Pomiar nie zalezy od czasu scianowego (HAR-236, runda 3: sny i przedzialy oblewaly sie pod
# obciazeniem 200+): narzedzie widzi zegar sterowany przez atrape. Atrapa ustawia czas wirtualny
# (plik `czas`), drukuje marker i czeka, az narzedzie ODCZYTA zegar (licznik `odczyty`), dopiero
# potem przesuwa czas dalej. Dzieki temu kazdy znacznik dostaje dokladnie ten czas, w ktorym
# zostal wydrukowany, a kazdy blad wyboru znacznika daje inna, dokladna liczbe sekund.
CZAS_POCZATKOWY = 1000  # nie zero: pomiar "od zera zegara" dalby ~1000 s
ZEGAR_ATRAPY = """T="$(dirname "$0")"
ustaw_czas() { echo "$1" > "$T/czas.nowy"; mv "$T/czas.nowy" "$T/czas"; }
# krok CZAS TEKST: czas wirtualny, marker, czekanie na odczyt zegara przez narzedzie
# (do 3000 prob; bez odczytu atrapa konczy sie kodem 98, a test oblewa na pomiarze)
krok() {
  n="$(cat "$T/odczyty")"
  ustaw_czas "$1"
  printf "$2"
  i=0
  while [ "$(cat "$T/odczyty")" -le "$n" ]; do
    i=$((i + 1))
    [ "$i" -le 3000 ] || exit 98
    sleep 0.02
  done
}
"""


class _ZegarSterowany:
    """Podstawia `time` w `mutanty_zmian`: `monotonic()` czyta czas wirtualny z pliku atrapy i
    publikuje licznik odczytow (po odczycie czasu, wiec atrapa nie przesunie zegara przed nim)."""

    def __init__(self, katalog: Path) -> None:
        self.katalog = katalog
        self.odczyty = 0
        self._zapisz("czas", float(CZAS_POCZATKOWY))
        self._zapisz("odczyty", 0)

    def _zapisz(self, nazwa: str, wartosc: float) -> None:
        tymczasowy = self.katalog / f"{nazwa}.zapis"
        tymczasowy.write_text(f"{wartosc:g}" if nazwa == "czas" else f"{wartosc}")
        os.replace(tymczasowy, self.katalog / nazwa)

    def monotonic(self) -> float:
        czas = float((self.katalog / "czas").read_text())
        self.odczyty += 1
        self._zapisz("odczyty", self.odczyty)
        return czas


ATRAPA_MUTMUTA_PELNA = (
    """#!/bin/sh
"""
    + ZEGAR_ATRAPY
    + """case "$3" in
  run)
    printf '\\r⠙ Generating mutants\\r'
    krok 1002 '\\rRunning stats\\r'
    krok 1003 '\\rRunning clean tests\\r'
    krok 1005 'Running mutation testing\\n'
    ustaw_czas 1009
    echo done ;;
  results)
    ustaw_czas 1010
    echo "Results:"
    echo "    cockpit.services.a.x_zapisz__mutmut_1: killed" ;;
esac
exit 0
"""
)
ATRAPA_MUTMUTA_BEZ_STATYSTYK = """#!/bin/sh
case "$3" in
  run)
    printf 'Generating mutants\\n'
    printf 'Running clean tests\\n'
    printf 'Running mutation testing\\n'
    echo done ;;
  results) echo "Results:" ;;
esac
exit 0
"""
ATRAPA_MUTMUTA_BEZ_MUTOWANIA = """#!/bin/sh
case "$3" in
  run)
    printf 'Running stats\\n'
    printf 'Running clean tests\\n'
    echo done ;;
  results) echo "Results:" ;;
esac
exit 0
"""


def _przebieg_z_atrapa(katalog: Path, skrypt: str) -> types.SimpleNamespace:
    """`main` z prawdziwym `uruchom` i prawdziwym gitem; `sys.executable` wskazuje atrapa mutmuta,
    a zegar narzedzia jest sterowany przez atrape (`_ZegarSterowany`)."""
    korzen = _repo_git(katalog / "repo")
    (korzen / SCIEZKA_A).write_text(_zrodlo_a(3, 4), encoding="utf-8")
    atrapa = katalog / "atrapa_mutmuta.sh"
    atrapa.write_text(skrypt, encoding="utf-8")
    atrapa.chmod(0o755)
    zegar = _ZegarSterowany(katalog)
    wyjscie, blad = io.StringIO(), io.StringIO()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "executable", str(atrapa))
        mp.setattr(mutanty_zmian, "time", types.SimpleNamespace(monotonic=zegar.monotonic))
        mp.chdir(korzen)
        with contextlib.redirect_stdout(wyjscie), contextlib.redirect_stderr(blad):
            kod = mutanty_zmian.main(["--korzen", str(korzen), "--baza", "HEAD"])
    return types.SimpleNamespace(kod=kod, out=wyjscie.getvalue(), err=blad.getvalue())


@pytest.fixture(scope="module")
def przebieg_z_markerami(tmp_path_factory) -> types.SimpleNamespace:
    return _przebieg_z_atrapa(tmp_path_factory.mktemp("markery"), ATRAPA_MUTMUTA_PELNA)


def test_przebieg_ze_strumienia_mutmuta_nie_zmienia_stdout_i_kodu_a_fazy_ida_w_kolejnosci(
    przebieg_z_markerami,
):
    assert przebieg_z_markerami.kod == 0
    assert przebieg_z_markerami.out == ""
    assert list(_czasy(przebieg_z_markerami.err)) == FAZY_CZASU


def test_czas_statystyk_liczony_od_running_stats_do_running_clean_tests(przebieg_z_markerami):
    sekundy = _sekundy(_czasy(przebieg_z_markerami.err)["mutmut_statystyki"])
    # czas wirtualny 1002 -> 1003; do `Running mutation testing` byloby 3 s, do konca procesu 7 s,
    # od startu do `Running clean tests` tez 3 s
    assert sekundy == 1.0


def test_czas_przygotowania_liczony_od_startu_mutmut_run_do_running_mutation_testing(
    przebieg_z_markerami,
):
    sekundy = _sekundy(_czasy(przebieg_z_markerami.err)["mutmut_przygotowanie"])
    # start 1000 -> `Running mutation testing` 1005; od `Running stats` byloby 3 s, od
    # `Running clean tests` 2 s, do konca procesu 9 s
    assert sekundy == 5.0


def test_czas_mutantow_liczony_od_running_mutation_testing_do_konca_procesu(
    przebieg_z_markerami,
):
    sekundy = _sekundy(_czasy(przebieg_z_markerami.err)["mutmut_mutanty"])
    # `Running mutation testing` 1005 -> koniec procesu 1009; od `Running clean tests` byloby 6 s,
    # od startu 9 s
    assert sekundy == 4.0


def test_czas_mutmut_results_obejmuje_wywolanie_results(przebieg_z_markerami):
    sekundy = _sekundy(_czasy(przebieg_z_markerami.err)["mutmut_results"])
    assert sekundy == 1.0  # czas wirtualny 1009 -> 1010; faza nie obejmuje poprzednich (10 s)


def test_czas_calkowity_obejmuje_wszystkie_fazy_a_wybor_funkcji_jest_krotszy_od_calosci(
    przebieg_z_markerami,
):
    czasy = {faza: _sekundy(wartosc) for faza, wartosc in _czasy(przebieg_z_markerami.err).items()}
    assert czasy["razem"] == 10.0  # start 1000 -> koniec `mutmut results` 1010
    # liczone od startu przebiegu (1000), nie od zera zegara (1000 s), i do wyboru funkcji, nie do
    # konca (10 s): w czasie wirtualnym wybor funkcji nie trwa
    assert czasy["wybor_funkcji"] == 0.0
    assert czasy["wybor_funkcji"] < czasy["razem"]


def test_mutmut_bez_linii_running_stats_ma_pomiar_pozostalych_faz_ale_nie_statystyk(tmp_path):
    wynik = _przebieg_z_atrapa(tmp_path, ATRAPA_MUTMUTA_BEZ_STATYSTYK)

    czasy = _czasy(wynik.err)
    assert wynik.kod == 0
    assert czasy["mutmut_statystyki"] == BRAK_POMIARU
    _sekundy(czasy["mutmut_przygotowanie"])
    _sekundy(czasy["mutmut_mutanty"])


def test_mutmut_bez_linii_running_mutation_testing_nie_ma_pomiaru_przygotowania_ani_mutantow(
    tmp_path,
):
    wynik = _przebieg_z_atrapa(tmp_path, ATRAPA_MUTMUTA_BEZ_MUTOWANIA)

    czasy = _czasy(wynik.err)
    assert wynik.kod == 0
    _sekundy(czasy["mutmut_statystyki"])
    assert czasy["mutmut_przygotowanie"] == BRAK_POMIARU
    assert czasy["mutmut_mutanty"] == BRAK_POMIARU
    _sekundy(czasy["mutmut_results"])


# =============================================================================
# HAR-236, tura 2: elementy, ktore tools/ (poza zasiegiem mutmuta) chroni tylko tymi testami
# =============================================================================

ZRODLO_ODWROCONA_KOLEJNOSC = (
    "def b_druga():\n    return 2\n\n\ndef a_pierwsza():\n    return 1\n"
)


def test_wzorce_pliku_sa_posortowane_takze_gdy_kolejnosc_funkcji_w_pliku_jest_odwrotna():
    # Zmienione linie 2 i 6 leza w `b_druga` (kolejnosc w pliku: pierwsza) i `a_pierwsza`.
    assert _wzorce(ZRODLO_ODWROCONA_KOLEJNOSC, 2, 6) == [
        f"{MODUL}.x_a_pierwsza__mutmut_*",
        f"{MODUL}.x_b_druga__mutmut_*",
    ]


# --- uruchamianie `mutmut run` strumieniowo ----------------------------------------------------


def test_brak_interpretera_dla_mutmut_run_to_blad_narzedzia_z_nazwa_programu(tmp_path):
    brak = str(tmp_path / "nie-ma-takiego-programu")

    with pytest.raises(BladNarzedzia) as blad:
        mutanty_zmian.uruchom([brak, "-m", "mutmut", "run"])

    assert str(blad.value).startswith(f"nie da sie uruchomic {brak}: ")


# Wyjscie z bajtami, ktore psuja naiwne dekodowanie: znak U+2819 (3 bajty) rozdzielony snem
# na dwa odczyty, pojedynczy niepoprawny bajt 0xFF i urwany znak na samym koncu. Stderr idzie
# do tego samego strumienia (merge), a wyjscie jest diagnostyka kodu 2.
ATRAPA_MUTMUTA_Z_BAJTAMI = """#!/bin/sh
case "$3" in
  run)
    printf 'poczatek\\n'
    printf '\\342\\240'
    sleep 0.3
    printf '\\231 koniec-znaku\\n'
    printf 'zly-bajt-\\377-po\\n'
    echo "boom na stderr" >&2
    printf 'urwany-\\342'
    exit 1 ;;
esac
exit 0
"""


def test_diagnostyka_bledu_mutmuta_zawiera_stderr_i_poprawnie_zdekodowane_bajty(tmp_path):
    wynik = _przebieg_z_atrapa(tmp_path, ATRAPA_MUTMUTA_Z_BAJTAMI)

    assert wynik.kod == 2
    assert wynik.out == ""
    assert wynik.err.startswith(
        "blad: mutmut run zakonczyl sie bledem:\n"
        "poczatek\n"
        "⠙ koniec-znaku\n"
        "zly-bajt-\ufffd-po\n"
        "boom na stderr\n"
        "urwany-\ufffd\n"
    )


ATRAPA_MUTMUTA_MARKER_W_KAWALKACH = (
    """#!/bin/sh
"""
    + ZEGAR_ATRAPY
    + """case "$3" in
  run)
    ustaw_czas 1001
    printf '⠙ Generating\\rRunning mut'
    sleep 1
    krok 1002 'ation testing'
    ustaw_czas 1003
    printf ' ...\\n'
    ustaw_czas 1004 ;;
  results) echo "Results:" ;;
esac
exit 0
"""
)


def test_marker_rozdzielony_na_dwa_odczyty_jest_rozpoznany_a_czas_liczony_od_pierwszego_widzenia(
    tmp_path,
):
    wynik = _przebieg_z_atrapa(tmp_path, ATRAPA_MUTMUTA_MARKER_W_KAWALKACH)

    czasy = _czasy(wynik.err)
    assert wynik.kod == 0
    # Marker widac dopiero po drugim odczycie (czas 1002); kolejny odczyt (czas 1003, marker wciaz w
    # niedokonczonej linii) go nie przesuwa: faza mutantow to 1002 -> 1004, przygotowanie 1000 -> 1002.
    assert _sekundy(czasy["mutmut_mutanty"]) == 2.0
    assert _sekundy(czasy["mutmut_przygotowanie"]) == 2.0
    assert czasy["mutmut_statystyki"] == BRAK_POMIARU


ATRAPA_MUTMUTA_ZAWIESZONA = """#!/bin/sh
case "$3" in
  run)
    echo "$$" > "$(dirname "$0")/atrapa.pid"
    exec sleep 300 ;;
esac
exit 0
"""


class _OsZepsutyOdczyt:
    """Moduł `os` widziany przez narzedzie: `read` czeka na pid atrapy, potem rzuca wyjatek."""

    def __init__(self, plik_pid: Path) -> None:
        self.plik_pid = plik_pid

    def __getattr__(self, nazwa: str):
        return getattr(os, nazwa)

    def read(self, fd: int, rozmiar: int) -> bytes:
        koniec = time.monotonic() + 30
        while not self.plik_pid.exists() and time.monotonic() < koniec:
            time.sleep(0.05)
        raise RuntimeError("zepsuty odczyt")


class _PopenZLapaniem(subprocess.Popen):
    zlapane: list = []

    def __init__(self, *argumenty, **opcje):
        super().__init__(*argumenty, **opcje)
        _PopenZLapaniem.zlapane.append(self)


def _lapanie_popen(monkeypatch) -> list:
    _PopenZLapaniem.zlapane = []
    monkeypatch.setattr(subprocess, "Popen", _PopenZLapaniem)
    return _PopenZLapaniem.zlapane


def _procesy_mutmut_run(zlapane: list) -> list:
    return [p for p in zlapane if list(p.args)[1:4] == ["-m", "mutmut", "run"]]


def test_wyjatek_podczas_czytania_strumienia_zabija_proces_mutmuta_i_zamyka_strumien(
    tmp_path, monkeypatch
):
    zlapane = _lapanie_popen(monkeypatch)
    monkeypatch.setattr(mutanty_zmian, "os", _OsZepsutyOdczyt(tmp_path / "atrapa.pid"))

    with pytest.raises(RuntimeError, match="zepsuty odczyt"):
        _przebieg_z_atrapa(tmp_path, ATRAPA_MUTMUTA_ZAWIESZONA)

    pid = int((tmp_path / "atrapa.pid").read_text())
    (proces,) = _procesy_mutmut_run(zlapane)
    assert proces.pid == pid
    assert proces.returncode is not None  # zabity i odebrany
    assert not czy_zyje(pid)
    assert proces.stdout.closed


def test_po_zwyklym_przebiegu_strumien_mutmuta_jest_zamkniety_a_proces_odebrany(
    tmp_path, monkeypatch
):
    zlapane = _lapanie_popen(monkeypatch)

    wynik = _przebieg_z_atrapa(tmp_path, ATRAPA_MUTMUTA_BEZ_STATYSTYK)

    assert wynik.kod == 0
    (proces,) = _procesy_mutmut_run(zlapane)
    assert proces.stdout.closed
    assert proces.returncode == 0


# --- tryb przyrostowy: zapis, odcisk, komunikaty -----------------------------------------------


def test_przebieg_z_zapisem_bez_zmienionych_funkcji_tworzy_pusty_zapis_i_nie_wola_mutmuta(
    przyrost, capsys
):
    # Zrodla bez zmian wzgledem bazy: lista funkcji jest pusta, a zapis i tak powstaje.
    assert przyrost.przebieg() == 0

    linie = _linie_stderr(capsys.readouterr())
    assert "przyrostowo: zapis unieważniony — brak zapisu" in linie
    assert "przyrostowo: mutuję 0 z 0 funkcji" in linie
    assert przyrost.mutmut.runy == []
    assert przyrost.wczytaj_zapis()["funkcje"] == {}

    assert przyrost.przebieg() == 0  # pusty zapis jest poprawny: nie jest unieważniany

    linie = _linie_stderr(capsys.readouterr())
    assert not [x for x in linie if x.startswith("przyrostowo: zapis unieważniony")]
    assert "przyrostowo: mutuję 0 z 0 funkcji" in linie


def test_wynik_results_z_cache_nie_wnosi_przezywajacych_funkcji_ktorych_nie_mutowano(
    przyrost, capsys
):
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg() == 0
    capsys.readouterr()
    # Stary wpis w cache mutmuta (zapis o nim nie wie); `results` pokazuje caly cache.
    przyrost.mutmut.mutanty[KLUCZ_INNA] = "survived"
    przyrost.mutmut.results_wszystkie = True

    przyrost.zrodlo(zapisz=5, inna=4)  # mutowana tylko `zapisz`

    assert przyrost.przebieg() == 0
    assert przyrost.mutmut.wzorce_runow()[-1] == [WZ_ZAPISZ]
    assert capsys.readouterr().out == ""


def test_wiele_przezywajacych_z_zapisu_i_z_nowego_przebiegu_jest_na_stdout_alfabetycznie(
    przyrost, capsys
):
    for numer in range(1, 7):
        przyrost.mutmut.mutanty[f"cockpit.services.a.x_inna__mutmut_{numer}"] = "survived"
        przyrost.mutmut.mutanty[f"cockpit.services.a.x_zapisz__mutmut_{numer}"] = "survived"
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg() == 1
    capsys.readouterr()

    przyrost.zrodlo(zapisz=5, inna=4)  # `inna` z zapisu, `zapisz` z nowego przebiegu

    assert przyrost.przebieg() == 1
    oczekiwane = sorted(przyrost.mutmut.mutanty)
    assert len(oczekiwane) == 12
    assert capsys.readouterr().out == "".join(f"{klucz}\n" for klucz in oczekiwane)


BRAK_PRZEZYWAJACYCH = "brak przezywajacych mutantow w zmienionych funkcjach"


@pytest.mark.parametrize(
    ("scenariusz", "komunikat_jest"),
    [
        pytest.param("wszystkie_zabite", True, id="wszystkie_zabite_w_tym_przebiegu"),
        pytest.param("jest_przezywajacy", False, id="jest_przezywajacy_mutant"),
        pytest.param("zmiana_poza_zasiegiem", False, id="zmiana_poza_zasiegiem_mutmuta"),
        pytest.param("z_samego_zapisu", False, id="nic_nie_mutowano_wynik_z_zapisu"),
    ],
)
def test_komunikat_o_braku_przezywajacych_tylko_gdy_ten_przebieg_mutowal_i_nic_nie_przezylo(
    przyrost, capsys, scenariusz, komunikat_jest
):
    if scenariusz == "jest_przezywajacy":
        przyrost.mutmut.mutanty[KLUCZ_INNA] = "survived"
    if scenariusz == "zmiana_poza_zasiegiem":
        przyrost.plik("src/cockpit/api/nowy.py", "def f():\n    return 1\n")
    przyrost.zrodlo(zapisz=3, inna=4)
    kod_pierwszego = przyrost.przebieg()
    if scenariusz == "z_samego_zapisu":
        assert kod_pierwszego == 0
        capsys.readouterr()
        przyrost.przebieg()  # drugi przebieg: nic do mutowania
    linie = _linie_stderr(capsys.readouterr())

    assert (BRAK_PRZEZYWAJACYCH in linie) is komunikat_jest


class _SrodowiskoZBledemLsFilesTestow(_Srodowisko):
    """Jak `_Srodowisko`, ale `git ls-files` listujacy `tests` (odcisk testow) konczy sie bledem."""

    def __call__(self, argv):
        if argv[:2] == ["git", "ls-files"] and argv[-1] == "tests":
            self.wywolania.append(list(argv))
            return self._wynik(argv, 128, "", "fatal: zepsute ls-files")
        return super().__call__(argv)


def test_blad_git_ls_files_przy_odcisku_testow_daje_kod_2_z_komunikatem_i_bez_mutmuta(
    monkeypatch, tmp_path, capsys
):
    s = _SrodowiskoZBledemLsFilesTestow(diff=_diff_a())

    kod = _main(monkeypatch, tmp_path, s, "--od-przebiegu", str(tmp_path / "zapis.json"))

    assert kod == 2
    zrzut = capsys.readouterr()
    assert "blad: git ls-files tests nie powiodl sie: fatal: zepsute ls-files" in zrzut.err
    assert s.mutmut("run") == []
    assert not (tmp_path / "zapis.json").exists()


SKRYPT_ODCISKU_TESTOW = (
    "import os, sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import mutanty_zmian\n"
    "os.chdir(sys.argv[2])\n"
    "print(mutanty_zmian.odcisk_testow(Path(sys.argv[2])))\n"
)


def _odcisk_w_procesie(repo: Path, ziarno_skrotu: str) -> str:
    korzen = Path(__file__).resolve().parent.parent
    proces = uruchom_proces_sync(
        [sys.executable, "-c", SKRYPT_ODCISKU_TESTOW, str(korzen / "tools"), str(repo)],
        cwd=korzen,
        env={**os.environ, "PYTHONHASHSEED": ziarno_skrotu},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wyjscie, blad = proces.communicate(timeout=LIMIT_PROCESU_POMOCNICZEGO)
    finally:
        zakoncz_proces_sync(proces, nazwa="odcisk testow w osobnym procesie")
    assert proces.returncode == 0, blad
    return wyjscie.strip()


def test_odcisk_testow_nie_zalezy_od_kolejnosci_zbioru_sciezek_w_procesie(tmp_path):
    repo = _repo_git(tmp_path / "repo")
    for numer in range(8):
        cel = repo / "tests" / f"test_dodatkowy_{numer}.py"
        cel.write_text(f"def test_{numer}():\n    assert True\n", encoding="utf-8")
    _git_t(repo, "add", "-A")

    odciski = {_odcisk_w_procesie(repo, ziarno) for ziarno in ("1", "2", "3", "4")}

    assert len(odciski) == 1  # losowa kolejnosc zbioru dawalaby rozne odciski w roznych procesach
    assert SKROT_SHA256.fullmatch(next(iter(odciski)))


def test_plik_testow_usuniety_z_dysku_ale_wciaz_sledzony_uniewaznia_zapis(przyrost, capsys):
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg() == 0
    capsys.readouterr()
    (przyrost.korzen / "tests" / "test_c.py").unlink()  # bez `git rm`: wciaz na liscie ls-files

    assert przyrost.przebieg() == 0

    linie = _linie_stderr(capsys.readouterr())
    assert "przyrostowo: zapis unieważniony — zmiana testów" in linie


def test_katalog_w_miejscu_sledzonego_pliku_testow_to_blad_narzedzia_z_kodem_2(
    przyrost, capsys
):
    przyrost.zrodlo(zapisz=3, inna=4)
    plik = przyrost.korzen / "tests" / "test_c.py"
    plik.unlink()
    plik.mkdir()  # `git ls-files` wciaz go listuje, ale to nie jest plik

    assert przyrost.przebieg() == 2

    assert "blad: tests/test_c.py: nie da sie odczytac: " in capsys.readouterr().err
    assert not przyrost.zapis.exists()


def test_podzial_funkcji_na_inne_linie_przy_tym_samym_tekscie_zmienia_jej_odcisk(przyrost, capsys):
    przyrost.plik(SCIEZKA_A, "def zapisz():\n    return (1,\n        2)\n\n\ndef inna():\n    return 2\n")
    przyrost.git("add", "-A")
    przyrost.git("commit", "-q", "-m", "wieloliniowe")
    przyrost.plik(SCIEZKA_A, "def zapisz():\n    return (1,\n        3)\n\n\ndef inna():\n    return 2\n")
    assert przyrost.przebieg() == 0
    capsys.readouterr()

    # Po laczeniu linii przez "" tekst bylby ten sam: `    return (1,        3)`.
    przyrost.plik(SCIEZKA_A, "def zapisz():\n    return (1,        3)\n\n\ndef inna():\n    return 2\n")

    assert przyrost.przebieg() == 0
    assert "przyrostowo: mutuję 1 z 1 funkcji" in _linie_stderr(capsys.readouterr())
    assert przyrost.mutmut.wzorce_runow() == [[WZ_ZAPISZ], [WZ_ZAPISZ]]


def test_wzorzec_metody_z_separatorem_klasy_przechodzi_przez_zapis_bez_utraty_znakow(
    przyrost, capsys
):
    przyrost.plik(SCIEZKA_K, "class K:\n    def m(self):\n        return 1\n")
    przyrost.git("add", "-A")
    przyrost.git("commit", "-q", "-m", "klasa")
    przyrost.plik(SCIEZKA_K, "class K:\n    def m(self):\n        return 2\n")
    assert przyrost.przebieg() == 0
    capsys.readouterr()

    assert przyrost.przebieg() == 0

    linie = _linie_stderr(capsys.readouterr())
    assert not [x for x in linie if x.startswith("przyrostowo: zapis unieważniony")]
    assert "przyrostowo: mutuję 0 z 1 funkcji" in linie
    assert ZNAK in przyrost.zapis.read_text(encoding="utf-8")  # zapis nie zamienia znaku na \u01c1


def test_plik_zapisu_ma_ustalony_format_klucze_posortowane_wciecie_i_koncowy_newline(przyrost):
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg() == 0

    tekst = przyrost.zapis.read_text(encoding="utf-8")

    zapis = json.loads(tekst)
    assert list(zapis) == ["funkcje", "odcisk_testow", "wersja"]
    assert tekst == json.dumps(zapis, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    assert tekst.startswith('{\n "funkcje": {\n')


class _OsBezMoznosciPodmiany:
    """Moduł `os` widziany przez narzedzie: `replace` konczy sie `OSError`."""

    def __getattr__(self, nazwa: str):
        return getattr(os, nazwa)

    def replace(self, zrodlo, cel):
        raise OSError("brak miejsca")


def test_nieudana_podmiana_zapisu_zostawia_stary_zapis_i_nie_zostawia_pliku_tymczasowego(
    przyrost, monkeypatch, capsys
):
    przyrost.zrodlo(zapisz=3, inna=4)
    assert przyrost.przebieg() == 0
    stare_bajty = przyrost.zapis.read_bytes()
    capsys.readouterr()
    monkeypatch.setattr(mutanty_zmian, "os", _OsBezMoznosciPodmiany())
    przyrost.zrodlo(zapisz=5, inna=4)

    assert przyrost.przebieg() == 2

    assert f"{przyrost.zapis}: nie da sie zapisac przebiegu: brak miejsca" in capsys.readouterr().err
    assert przyrost.zapis.read_bytes() == stare_bajty
    assert sorted(p.name for p in przyrost.zapis.parent.iterdir() if p.name.endswith(".tmp")) == []
