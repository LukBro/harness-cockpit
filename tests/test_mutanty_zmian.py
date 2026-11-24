"""Mutanty zmienionych funkcji przed oddaniem pracy (HAR-133).

Skrypt `tools/mutanty_zmian.py` mapuje diff względem bazy na wzorce mutantów
(tylko funkcje i metody, które zmieniono), uruchamia na nich mutmut i zgłasza
przeżywające mutanty. Testy nie uruchamiają ani gita, ani mutmuta: diff,
źródła i wyniki mutmuta są daną w `tmp_path`, a `uruchom` jest podmieniane.
"""
import errno
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from procesy import czy_zyje

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import mutanty_zmian  # noqa: E402

BladNarzedzia = mutanty_zmian.BladNarzedzia
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
            "nowe_poza_zasiegiem_rc": 0,  # `ls-files` nowych plikow w katalogach poza zasiegiem mutmuta
            "run_rc": 0,
            "run_out": "",
            "run_err": "",
            "results_rc": 0,
            "results": "",
            "opoznienie_wynikow": 0,
            "opoznienie_diffu": 0,
            "opoznienie_biegu": 0,
            "linie_biegu": [],  # (opoznienie przed linia w s, linia wyjscia `mutmut run`)
            "zmienione_pliki": "",  # wyjscie `git diff --name-only` (ostrzezenia poza zasiegiem)
            "nazwy_rc": 0,
            "tests_rc": 0,
            "tests_dodatkowe": "",  # dopisane do listy plikow testow (np. katalog, plik, ktorego nie ma)
        }
        self.u.update(ustawienia)
        # HAR-236: odcisk testow moze pytac gita o pliki pod tests/; atrapa zna kopie robocza
        # (`korzen`) i wie, ktore pliki sa nieSledzone (zwraca je tylko przy fladze --others / -o).
        self.korzen: Path | None = None
        self.niesledzone: set[str] = set()
        self.ignorowane: set[str] = set()

    def _pliki_testow(self, argv: list[str]) -> str:
        if self.korzen is None:
            return ""
        flagi = [a for a in argv if a.startswith("-")]
        z_niesledzonymi = any(a == "--others" or (a[:2] != "--" and "o" in a[1:]) for a in flagi)
        z_zerami = any(a == "-z" or (a[:2] != "--" and "z" in a[1:]) for a in flagi)
        pliki = sorted(
            p.relative_to(self.korzen).as_posix()
            for p in (self.korzen / "tests").rglob("*")
            if p.is_file()
        )
        pliki = [p for p in pliki if z_niesledzonymi or p not in self.niesledzone]
        # jak prawdziwy git: plik ignorowany (np. __pycache__) pojawia sie przy -o bez --exclude-standard
        pliki = [p for p in pliki if "--exclude-standard" not in flagi or p not in self.ignorowane]
        return "".join(p + ("\0" if z_zerami else "\n") for p in pliki)

    def __call__(self, argv, na_linie=None):
        self.wywolania.append(list(argv))
        if argv[:2] == ["git", "merge-base"]:
            return self._wynik(argv, self.u["merge_rc"], "abc123\n", "fatal: brak bazy")
        if argv[:2] == ["git", "diff"] and "--name-only" in argv:
            return self._wynik(argv, self.u["nazwy_rc"], self.u["zmienione_pliki"], "fatal: name-only")
        if argv[:2] == ["git", "diff"]:
            time.sleep(self.u["opoznienie_diffu"])
            return self._wynik(argv, self.u["diff_rc"], self.u["diff"], "fatal: diff")
        if argv[:2] == ["git", "ls-files"] and "tests" in argv:
            return self._wynik(
                argv,
                self.u["tests_rc"],
                self._pliki_testow(argv) + self.u["tests_dodatkowe"],
                "fatal: ls-files tests",
            )
        if argv[:2] == ["git", "ls-files"] and "tools/" in argv:
            return self._wynik(argv, self.u["nowe_poza_zasiegiem_rc"], self.u["nowe"], "fatal: ls-files poza")
        if argv[:2] == ["git", "ls-files"]:
            return self._wynik(argv, self.u["nowe_rc"], self.u["nowe"], "fatal: ls-files")
        if argv[1:3] == ["-m", "mutmut"] and argv[3] == "run":
            # HAR-236: `na_linie` dostaje linie wyjscia w trakcie trwania procesu, z opoznieniami
            for opoznienie, linia in self.u["linie_biegu"]:
                time.sleep(opoznienie)
                if na_linie is not None:
                    na_linie(linia)
            time.sleep(self.u["opoznienie_biegu"])
            return self._wynik(argv, self.u["run_rc"], self.u["run_out"], self.u["run_err"])
        if argv[1:3] == ["-m", "mutmut"] and argv[3] == "results":
            time.sleep(self.u["opoznienie_wynikow"])
            return self._wynik(argv, self.u["results_rc"], self.u["results"])
        if argv[2:3] == ["mutmut-run"]:
            # HAR-255: opakowanie mutmuta z dowodami shardow zamiast `-m mutmut run`.
            if "--przydzial" in argv and self.u.get("przydzial") is not None:
                Path(argv[argv.index("--przydzial") + 1]).write_text(json.dumps(self.u["przydzial"]), encoding="utf-8")
            return self._wynik(argv, self.u["run_rc"], self.u["run_out"], self.u["run_err"])
        raise AssertionError(f"nieoczekiwane polecenie: {argv}")

    @staticmethod
    def _wynik(argv, rc, out, err=""):
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)

    def mutmut(self, tryb: str) -> list[list[str]]:
        return [w for w in self.wywolania if w[1:3] == ["-m", "mutmut"] and w[3] == tryb]

    def opakowane_run(self) -> list[list[str]]:
        return [w for w in self.wywolania if w[2:3] == ["mutmut-run"]]


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


# --- HAR-255: dowody shardow zamiast "clean tests" mutmuta -------------------------------------
#
# Z flaga `--katalog-dowodow` mutmut biegnie przez opakowanie `tools/statystyki_mutmuta.py mutmut-run`,
# ktore przed podmiana `run_clean_tests` sprawdza dowody (brak dowodu to kod 2). Bez flagi: `-m mutmut run`.

FLAGA_DOWODOW = "--katalog-dowodow"
DIFF_DWIE_FUNKCJE = _diff("src/cockpit/services/a.py", "@@ -1,5 +1,5 @@")
WZORCE_DWOCH_FUNKCJI = ["cockpit.services.a.x_inna__mutmut_*", "cockpit.services.a.x_zapisz__mutmut_*"]


def _katalog_dowodow(tmp_path: Path) -> Path:
    katalog = tmp_path / "dowody-ci"
    katalog.mkdir()
    return katalog


def test_z_katalogiem_dowodow_mutmut_biegnie_przez_opakowanie_z_dowodami_a_nie_zwyklym_run(
    monkeypatch, tmp_path
):
    katalog = _katalog_dowodow(tmp_path)
    s = _Srodowisko(diff=DIFF_DWIE_FUNKCJE)

    assert _main(monkeypatch, tmp_path, s, FLAGA_DOWODOW, str(katalog)) == 0
    assert s.mutmut("run") == []
    assert s.opakowane_run() == [
        [
            sys.executable,
            str(Path(mutanty_zmian.__file__).with_name("statystyki_mutmuta.py")),
            "mutmut-run",
            FLAGA_DOWODOW,
            str(katalog),
            *WZORCE_DWOCH_FUNKCJI,
        ]
    ]


def test_z_katalogiem_dowodow_wyniki_mutantow_czyta_zwykly_mutmut_results(monkeypatch, tmp_path):
    katalog = _katalog_dowodow(tmp_path)
    s = _Srodowisko(diff=DIFF_DWIE_FUNKCJE, results=_wyniki((KLUCZ_A, "survived")))

    assert _main(monkeypatch, tmp_path, s, FLAGA_DOWODOW, str(katalog)) == 1
    assert s.mutmut("results") == [[sys.executable, "-m", "mutmut", "results"]]


def test_bez_katalogu_dowodow_mutmut_biegnie_zwyklym_run_z_oryginalnymi_clean_tests(monkeypatch, tmp_path):
    s = _Srodowisko(diff=DIFF_DWIE_FUNKCJE)

    assert _main(monkeypatch, tmp_path, s) == 0
    assert s.opakowane_run() == []
    assert s.mutmut("run") == [[sys.executable, "-m", "mutmut", "run", *WZORCE_DWOCH_FUNKCJI]]


def test_katalog_dowodow_ktory_nie_istnieje_to_kod_2_z_brakiem_dowodu_bez_wolania_gita_i_mutmuta(
    monkeypatch, tmp_path, capsys
):
    katalog = tmp_path / "nie-ma-dowodow"
    s = _Srodowisko(diff=DIFF_DWIE_FUNKCJE)

    assert _main(monkeypatch, tmp_path, s, FLAGA_DOWODOW, str(katalog)) == 2
    assert s.wywolania == []
    assert f"blad: brak dowodu: katalog dowodow {katalog} nie istnieje" in capsys.readouterr().err.splitlines()


def test_zwykly_plik_zamiast_katalogu_dowodow_to_kod_2_z_brakiem_dowodu(monkeypatch, tmp_path, capsys):
    plik = tmp_path / "dowody-ci"
    plik.write_text("x", encoding="utf-8")
    s = _Srodowisko(diff=DIFF_DWIE_FUNKCJE)

    assert _main(monkeypatch, tmp_path, s, FLAGA_DOWODOW, str(plik)) == 2
    assert s.wywolania == []
    assert f"blad: brak dowodu: katalog dowodow {plik} nie istnieje" in capsys.readouterr().err.splitlines()


def test_brak_dowodu_zgloszony_przez_opakowanie_daje_kod_2_z_trescia_na_stderr_i_bez_czytania_wynikow(
    monkeypatch, tmp_path, capsys
):
    katalog = _katalog_dowodow(tmp_path)
    s = _Srodowisko(diff=DIFF_DWIE_FUNKCJE, run_rc=2, run_err="blad: brak dowodu: brak pliku shardu 1 z 4")

    assert _main(monkeypatch, tmp_path, s, FLAGA_DOWODOW, str(katalog)) == 2
    assert s.mutmut("results") == []
    assert "brak dowodu: brak pliku shardu 1 z 4" in capsys.readouterr().err


def test_opakowanie_bez_pasujacych_mutantow_daje_kod_0_jak_zwykly_mutmut(monkeypatch, tmp_path):
    katalog = _katalog_dowodow(tmp_path)
    s = _Srodowisko(
        diff=DIFF_DWIE_FUNKCJE,
        run_rc=1,
        run_out="AssertionError: Filtered for specific mutants, but nothing matches",
    )

    assert _main(monkeypatch, tmp_path, s, FLAGA_DOWODOW, str(katalog)) == 0
    assert s.mutmut("results") == []


def test_z_katalogiem_dowodow_i_bez_zmienionych_funkcji_nie_wola_opakowania(monkeypatch, tmp_path):
    katalog = _katalog_dowodow(tmp_path)
    s = _Srodowisko()

    assert _main(monkeypatch, tmp_path, s, FLAGA_DOWODOW, str(katalog)) == 0
    assert s.opakowane_run() == []
    assert s.mutmut("run") == []


# --- HAR-236: --max-children, czasy faz, tryb przyrostowy (--od-przebiegu) ---------------------

PLIK_A = "src/cockpit/services/a.py"
KLUCZ_INNA = "cockpit.services.a.x_inna__mutmut_1"
WZ_ZAPISZ = "cockpit.services.a.x_zapisz__mutmut_*"
WZ_INNA = "cockpit.services.a.x_inna__mutmut_*"
WSZYSTKIE = [WZ_INNA, WZ_ZAPISZ]  # kolejnosc wzorcow: posortowane
ZAPIS = "przebieg.json"
ZABITE = _wyniki((KLUCZ_A, "killed"), (KLUCZ_INNA, "killed"))
ZRODLO_ZMIENIONE = "def zapisz():\n    return 10\n\n\ndef inna():\n    return 2\n"
ZRODLO_INNA_ZMIENIONA = "def zapisz():\n    return 1\n\n\ndef inna():\n    return 20\n"
# Hunk obejmuje wszystkie linie pliku, takze po dopisaniu funkcji: zawsze trafiaja WSZYSTKIE funkcje,
# wiec o tym, ktore z nich mutujemy, decyduje wylacznie odcisk z zapisu.
DIFF_CALY_PLIK = _diff(PLIK_A, "@@ -1,30 +1,30 @@")


def _main_kod(*argv: str) -> int:
    """Kod wyjscia `main`; wyjscie argparse (SystemExit) liczy sie jako jego kod."""
    try:
        return mutanty_zmian.main(list(argv))
    except SystemExit as wyjscie:
        return wyjscie.code


def _wzorce_biegu(s: _Srodowisko) -> list[str]:
    """Wzorce z jedynego `mutmut run` (bez flag), a gdy go nie bylo - pusta lista."""
    biegi = s.mutmut("run")
    assert len(biegi) <= 1, biegi
    return [a for a in biegi[0][4:] if not a.startswith("-") and "mutmut_" in a] if biegi else []


class _Sesja:
    """Repo w `tmp_path/repo` z dwoma funkcjami, plikiem testow i atrapa gita/mutmuta;
    kolejne `main` dziela kopie robocza i katalog zapisu `--od-przebiegu`."""

    def __init__(self, monkeypatch, tmp_path, **ustawienia):
        self.korzen = tmp_path / "repo"
        self.korzen.mkdir()
        _repo(self.korzen)
        _plik(self.korzen, "tests/test_a.py", "def test_a():\n    assert True\n")
        _plik(self.korzen, "tests/test_b.py", "def test_b():\n    assert True\n")
        self.stan = tmp_path / "stan"
        ustawienia.setdefault("diff", DIFF_CALY_PLIK)
        ustawienia.setdefault("results", ZABITE)
        self.s = _Srodowisko(**ustawienia)
        self.s.korzen = self.korzen
        monkeypatch.setattr(mutanty_zmian, "uruchom", self.s)

    def main(self, *argv: str, przyrostowo: bool = True) -> int:
        self.s.wywolania.clear()
        dodatkowe = ["--od-przebiegu", str(self.stan)] if przyrostowo else []
        return mutanty_zmian.main(["--korzen", str(self.korzen), *dodatkowe, *argv])

    def wzorce(self) -> list[str]:
        return _wzorce_biegu(self.s)

    def zrodlo(self, tresc: str, sciezka: str = PLIK_A) -> None:
        _plik(self.korzen, sciezka, tresc)

    @property
    def plik_zapisu(self) -> Path:
        return self.stan / ZAPIS

    def zapis(self) -> dict:
        return json.loads(self.plik_zapisu.read_text(encoding="utf-8"))


@pytest.fixture
def sesja(monkeypatch, tmp_path):
    return _Sesja(monkeypatch, tmp_path)


# --max-children


def test_liczba_dzieci_mutmuta_trafia_do_mutmut_run_przed_albo_miedzy_wzorcami(
    monkeypatch, tmp_path
):
    s = _Srodowisko(diff=_diff_a(), results=_wyniki((KLUCZ_A, "killed")))
    assert _main(monkeypatch, tmp_path, s, "--max-children", "3") == 0
    biegi = s.mutmut("run")
    assert len(biegi) == 1
    argv = biegi[0]
    assert argv.count("--max-children") == 1
    i = argv.index("--max-children")
    assert argv[i + 1] == "3"
    assert argv[:4] == [sys.executable, "-m", "mutmut", "run"]
    assert argv[:i] + argv[i + 2 :] == [*argv[:4], "cockpit.services.a.x_zapisz__mutmut_*"]


def test_bez_flagi_max_children_mutmut_run_jej_nie_dostaje(monkeypatch, tmp_path):
    s = _Srodowisko(diff=_diff_a(), results=_wyniki((KLUCZ_A, "killed")))
    assert _main(monkeypatch, tmp_path, s) == 0
    assert not [a for a in s.mutmut("run")[0] if a.startswith("--")]


@pytest.mark.parametrize(
    "wartosc", ["0", "-1", "abc", "1.5", "", "+3", " 3", "٣"]  # ostatnia: arabsko-indyjska 3
)
def test_liczba_dzieci_inna_niz_calkowita_od_jednego_to_kod_2_i_mutmut_nie_rusza(
    monkeypatch, tmp_path, wartosc
):
    s = _Srodowisko(diff=_diff_a())
    monkeypatch.setattr(mutanty_zmian, "uruchom", s)
    korzen = _repo(tmp_path)
    assert _main_kod("--korzen", str(korzen), "--max-children", wartosc) == 2
    assert s.mutmut("run") == []


def test_najmniejsza_dozwolona_liczba_dzieci_to_jeden(monkeypatch, tmp_path):
    s = _Srodowisko(diff=_diff_a(), results=_wyniki((KLUCZ_A, "killed")))
    assert _main(monkeypatch, tmp_path, s, "--max-children", "1") == 0
    argv = s.mutmut("run")[0]
    assert argv[argv.index("--max-children") + 1] == "1"


# czas faz na stderr

FAZY = ["wybor-funkcji", "statystyki", "mutanty", "wyniki"]
LINIA_FAZY = re.compile(r"czas fazy ([a-z-]+): (\d+\.\d s|brak pomiaru)")


def _czasy_faz(err: str) -> dict[str, str]:
    """Faza -> tekst pomiaru; kazda linia `czas fazy ...` musi miec dokladnie ten format."""
    linie = [wiersz for wiersz in err.splitlines() if wiersz.startswith("czas fazy")]
    pary = []
    for linia in linie:
        dopasowanie = LINIA_FAZY.fullmatch(linia)
        assert dopasowanie, linia
        pary.append(dopasowanie.groups())
    assert [p[0] for p in pary] == FAZY, linie  # po jednej linii na faze, w tej kolejnosci
    return dict(pary)


def test_bez_zmienionych_funkcji_zmierzony_jest_tylko_wybor_a_pozostale_fazy_to_brak_pomiaru(
    monkeypatch, tmp_path, capsys
):
    s = _Srodowisko(diff=_diff_a(4))
    assert _main(monkeypatch, tmp_path, s) == 0
    czasy = _czasy_faz(capsys.readouterr().err)
    assert re.fullmatch(r"\d+\.\d s", czasy["wybor-funkcji"])
    assert {czasy[f] for f in ("statystyki", "mutanty", "wyniki")} == {"brak pomiaru"}


def test_fazy_wyboru_funkcji_i_mutantow_mierza_czas_od_startu_i_z_wywolania_mutmut_run(
    monkeypatch, tmp_path, capsys
):
    s = _Srodowisko(
        diff=_diff_a(),
        results=_wyniki((KLUCZ_A, "killed")),
        opoznienie_diffu=0.3,
        opoznienie_biegu=0.3,
    )
    assert _main(monkeypatch, tmp_path, s) == 0
    czasy = _czasy_faz(capsys.readouterr().err)
    assert 0.2 <= float(czasy["wybor-funkcji"].removesuffix(" s")) < 60
    assert 0.2 <= float(czasy["mutanty"].removesuffix(" s")) < 60


def test_faza_wynikow_ma_zmierzony_czas_z_wywolania_mutmut_results(monkeypatch, tmp_path, capsys):
    s = _Srodowisko(
        diff=_diff_a(), results=_wyniki((KLUCZ_A, "killed")), opoznienie_wynikow=0.3
    )
    assert _main(monkeypatch, tmp_path, s) == 0
    czasy = _czasy_faz(capsys.readouterr().err)
    assert re.fullmatch(r"\d+\.\d s", czasy["wybor-funkcji"])
    assert 0.2 <= float(czasy["wyniki"].removesuffix(" s")) < 60


def test_faza_wynikow_przy_uruchomieniu_bez_mutmuta_nie_ma_zera(monkeypatch, tmp_path, capsys):
    s = _Srodowisko()
    assert _main(monkeypatch, tmp_path, s) == 0
    assert "czas fazy wyniki: brak pomiaru" in capsys.readouterr().err.splitlines()


# zapis przebiegu: ksztalt, miejsce, atomowosc


def test_przebieg_zapisuje_wersje_calkowita_odcisk_testow_i_odciski_funkcji_w_katalogu(
    monkeypatch, tmp_path
):
    s = _Sesja(monkeypatch, tmp_path)
    s.stan = tmp_path / "jeszcze" / "nie-ma"  # katalog (takze posredni) powstaje przy zapisie
    assert s.main() == 0
    assert s.plik_zapisu.is_file()
    zapis = s.zapis()
    assert type(zapis["wersja"]) is int
    mapy = [k for k, v in zapis.items() if isinstance(v, dict)]
    assert len(mapy) == 1
    wpisy = zapis[mapy[0]]
    assert len(wpisy) == 2
    assert sorted("inna" in k for k in wpisy) == [False, True]
    assert sorted("zapisz" in k for k in wpisy) == [False, True]
    skroty = [v for v in zapis.values() if isinstance(v, str)]
    assert len(skroty) == 1 and re.fullmatch(r"[0-9a-f]{64}", skroty[0])


def test_odcisk_funkcji_to_sha256_jej_tekstu_z_dekoratorami(monkeypatch, tmp_path):
    zrodlo = "class K:\n    @staticmethod\n    def m():\n        return 1\n"
    s = _Sesja(monkeypatch, tmp_path, diff=_diff(PLIK_A, "@@ -2,3 +2,3 @@"))  # bez linii `class`
    s.zrodlo(zrodlo)
    assert s.main() == 0
    wpisy = next(v for v in s.zapis().values() if isinstance(v, dict))
    assert len(wpisy) == 1
    (wpis,) = wpisy.values()
    tekst = "    @staticmethod\n    def m():\n        return 1"  # pierwsza linia z wcieciem
    kandydaci = {
        hashlib.sha256(t.encode("utf-8")).hexdigest() for t in (tekst, tekst + "\n")
    }
    assert {v for v in wpis.values() if isinstance(v, str)} & kandydaci


def test_zapis_idzie_przez_plik_tymczasowy_w_tym_samym_katalogu_i_os_replace(
    monkeypatch, tmp_path
):
    s = _Sesja(monkeypatch, tmp_path)
    wywolania = []
    prawdziwa = os.replace

    def podglad(zrodlo, cel, *a, **kw):
        wywolania.append((Path(zrodlo), Path(cel), Path(zrodlo).read_bytes()))
        return prawdziwa(zrodlo, cel, *a, **kw)

    monkeypatch.setattr(os, "replace", podglad)
    assert s.main() == 0
    (zrodlo, cel, tresc) = [w for w in wywolania if w[1] == s.plik_zapisu][-1]
    assert zrodlo != cel and zrodlo.parent == cel.parent
    assert json.loads(tresc) == s.zapis()  # plik tymczasowy byl kompletny przed podmiana
    assert not zrodlo.exists()


@pytest.mark.parametrize(
    ("ustawienia", "kod"),
    [
        pytest.param({}, 0, id="kod_0_wszystko_zabite"),
        pytest.param({"results": _wyniki((KLUCZ_INNA, "survived"))}, 1, id="kod_1_survivor"),
        pytest.param(
            {"diff": DIFF_CALY_PLIK + _diff_linii(MODELE, 2)}, 3, id="kod_3_zmiana_poza_zasiegiem"
        ),
        pytest.param({"diff": ""}, 0, id="kod_0_bez_zmienionych_funkcji"),
    ],
)
def test_zapis_przebiegu_powstaje_po_kodzie_0_1_i_3(monkeypatch, tmp_path, ustawienia, kod):
    s = _Sesja(monkeypatch, tmp_path, **ustawienia)
    s.zrodlo(FUNKCJA, MODELE)
    assert s.main() == kod
    assert type(s.zapis()["wersja"]) is int


@pytest.mark.parametrize(
    "ustawienia",
    [
        pytest.param({"results_rc": 1}, id="mutmut_results_pada"),
        pytest.param({"run_rc": 1, "run_out": "Traceback: cos innego"}, id="mutmut_run_pada"),
        pytest.param({"merge_rc": 128}, id="nieznana_baza"),
    ],
)
def test_po_bledzie_narzedzia_zapis_nie_powstaje_a_istniejacy_zostaje_bez_zmian(
    monkeypatch, tmp_path, ustawienia
):
    s = _Sesja(monkeypatch, tmp_path)
    assert s.main() == 0
    zapis_przed = s.plik_zapisu.read_bytes()
    s.zrodlo(ZRODLO_ZMIENIONE)
    s.s.u.update(ustawienia)
    assert s.main() == 2
    assert s.plik_zapisu.read_bytes() == zapis_przed


@pytest.mark.parametrize(
    "ustawienia",
    [
        pytest.param({"results_rc": 1}, id="mutmut_results_pada"),
        pytest.param({"run_rc": 1, "run_out": "Traceback: cos innego"}, id="mutmut_run_pada"),
        pytest.param({"merge_rc": 128}, id="nieznana_baza"),
    ],
)
def test_pierwszy_przebieg_zakonczony_bledem_narzedzia_nie_zostawia_zapisu(
    monkeypatch, tmp_path, ustawienia
):
    s = _Sesja(monkeypatch, tmp_path, **ustawienia)
    assert s.main() == 2
    assert not s.plik_zapisu.exists()


# kolejne przebiegi: tylko zmienione funkcje


def test_pierwszy_przebieg_bez_zapisu_mutuje_wszystkie_funkcje(sesja):
    assert sesja.main() == 0
    assert sesja.wzorce() == WSZYSTKIE


def test_niezmieniony_kod_i_testy_w_kolejnym_przebiegu_nie_wola_mutmuta_i_daja_kod_0(sesja):
    assert sesja.main() == 0
    assert sesja.main() == 0
    assert sesja.s.mutmut("run") == []
    assert sesja.s.mutmut("results") == []


@pytest.mark.parametrize(
    ("nowe_zrodlo", "oczekiwane"),
    [
        pytest.param(ZRODLO_ZMIENIONE, [WZ_ZAPISZ], id="zmieniona_pierwsza_funkcja"),
        pytest.param(ZRODLO_INNA_ZMIENIONA, [WZ_INNA], id="zmieniona_druga_funkcja"),
    ],
)
def test_zmiana_jednej_z_dwoch_funkcji_mutuje_tylko_ja(sesja, nowe_zrodlo, oczekiwane):
    assert sesja.main() == 0
    sesja.zrodlo(nowe_zrodlo)
    assert sesja.main() == 0
    assert sesja.wzorce() == oczekiwane


def test_przesuniecie_funkcji_przez_komentarz_nad_nia_nie_jest_jej_zmiana(sesja):
    assert sesja.main() == 0
    sesja.zrodlo("# nowy komentarz\n" + ZRODLO_A)
    assert sesja.main() == 0
    assert sesja.s.mutmut("run") == []


def test_zmiana_samego_dekoratora_metody_mutuje_te_metode_ponownie(sesja):
    stare = "class K:\n    @staticmethod\n    def m(*a):\n        return 1\n"
    sesja.s.u["diff"] = _diff(PLIK_A, "@@ -2,3 +2,3 @@")  # bez linii `class` (kod 3)
    sesja.zrodlo(stare)
    assert sesja.main() == 0
    sesja.zrodlo(stare.replace("@staticmethod", "@classmethod"))
    assert sesja.main() == 0
    assert sesja.wzorce() == [f"cockpit.services.a.x{ZNAK}K{ZNAK}m__mutmut_*"]


def test_nowa_funkcja_w_pliku_jest_mutowana_a_niezmieniona_nie(sesja):
    assert sesja.main() == 0
    sesja.zrodlo(ZRODLO_A + "\n\ndef nowa():\n    return 3\n")
    assert sesja.main() == 0
    assert sesja.wzorce() == ["cockpit.services.a.x_nowa__mutmut_*"]


def test_przezywajacy_mutant_w_niezmienionej_funkcji_nadal_daje_kod_1_i_jest_wypisany(
    sesja, capsys
):
    sesja.s.u["results"] = _wyniki((KLUCZ_INNA, "survived"), (KLUCZ_A, "killed"))
    assert sesja.main() == 1
    capsys.readouterr()
    sesja.zrodlo(ZRODLO_ZMIENIONE)
    sesja.s.u["results"] = _wyniki((KLUCZ_A, "killed"))  # nowy przebieg o inna juz nic nie wie
    assert sesja.main() == 1
    assert sesja.wzorce() == [WZ_ZAPISZ]
    assert capsys.readouterr().out.splitlines() == [KLUCZ_INNA]


def test_przezywajacy_mutant_w_niezmienionym_kodzie_bez_ponownego_mutowania_nadal_daje_kod_1(
    sesja, capsys
):
    sesja.s.u["results"] = _wyniki((KLUCZ_INNA, "survived"))
    assert sesja.main() == 1
    capsys.readouterr()
    assert sesja.main() == 1
    assert sesja.s.mutmut("run") == []
    assert capsys.readouterr().out.splitlines() == [KLUCZ_INNA]


def test_przezywajacy_mutant_zmienionej_funkcji_znika_gdy_nowy_przebieg_go_zabija(sesja, capsys):
    sesja.s.u["results"] = _wyniki((KLUCZ_A, "survived"))
    assert sesja.main() == 1
    capsys.readouterr()
    sesja.zrodlo(ZRODLO_ZMIENIONE)
    sesja.s.u["results"] = _wyniki((KLUCZ_A, "killed"))
    assert sesja.main() == 0
    assert capsys.readouterr().out == ""
    assert sesja.main() == 0  # takze zapis po przebiegu nie pamieta starego survivora


def test_nowy_survivor_w_zmienionej_funkcji_daje_kod_1(sesja, capsys):
    assert sesja.main() == 0
    capsys.readouterr()
    sesja.zrodlo(ZRODLO_ZMIENIONE)
    sesja.s.u["results"] = _wyniki((KLUCZ_A, "no tests"))
    assert sesja.main() == 1
    assert capsys.readouterr().out.splitlines() == [KLUCZ_A]


def test_funkcja_usunieta_z_pliku_nie_zostawia_survivora_z_zapisu(sesja, capsys):
    sesja.s.u["results"] = _wyniki((KLUCZ_INNA, "survived"))
    assert sesja.main() == 1
    capsys.readouterr()
    sesja.zrodlo("def zapisz():\n    return 1\n")
    sesja.s.u["results"] = _wyniki((KLUCZ_A, "killed"))
    assert sesja.main() == 0
    assert capsys.readouterr().out == ""


# odcisk testow: zmiana testow albo konfiguracji to pelny przebieg


def _dopisz(sciezka: Path, dopisek: str) -> None:
    sciezka.write_text(sciezka.read_text(encoding="utf-8") + dopisek, encoding="utf-8")


@pytest.mark.parametrize(
    "zmiana",
    [
        pytest.param(lambda s: _dopisz(s.korzen / "tests/test_a.py", "# x\n"), id="tresc_testu"),
        pytest.param(
            lambda s: (s.korzen / "tests/test_b.py").rename(s.korzen / "tests/test_c.py"),
            id="sciezka_testu_przy_tej_samej_tresci",
        ),
        pytest.param(
            lambda s: _plik(s.korzen, "tests/pomocnik/nowy.py", "X = 1\n"), id="nowy_plik_testowy"
        ),
        pytest.param(
            lambda s: (
                _plik(s.korzen, "tests/nieSledzony.py", "X = 1\n"),
                s.s.niesledzone.add("tests/nieSledzony.py"),
            ),
            id="nowy_niesledzony_plik_testowy",
        ),
        pytest.param(lambda s: _dopisz(s.korzen / "pyproject.toml", "# x\n"), id="pyproject"),
    ],
)
def test_zmiana_testow_lub_konfiguracji_miedzy_przebiegami_daje_pelny_przebieg(sesja, zmiana):
    assert sesja.main() == 0
    zmiana(sesja)
    assert sesja.main() == 0
    assert sesja.wzorce() == WSZYSTKIE


def test_po_pelnym_przebiegu_z_powodu_testow_kolejny_przebieg_znow_jest_przyrostowy(sesja):
    assert sesja.main() == 0
    _dopisz(sesja.korzen / "tests/test_a.py", "# x\n")
    assert sesja.main() == 0
    assert sesja.main() == 0
    assert sesja.s.mutmut("run") == []


def test_survivor_z_poprzedniego_przebiegu_nie_przetrwa_pelnego_przebiegu_po_zmianie_testow(
    sesja,
):
    sesja.s.u["results"] = _wyniki((KLUCZ_INNA, "survived"))
    assert sesja.main() == 1
    _dopisz(sesja.korzen / "tests/test_a.py", "# test zabijajacy mutanta\n")
    sesja.s.u["results"] = ZABITE
    assert sesja.main() == 0


# zapis brakujacy, uszkodzony albo obcej wersji: pelny przebieg, nigdy sukces bez dowodu


@pytest.mark.parametrize(
    "tresc",
    [
        pytest.param(b"to nie jest json", id="nie_json"),
        pytest.param(b"", id="pusty_plik"),
        pytest.param(b"[]", id="lista_zamiast_obiektu"),
        pytest.param(b"null", id="null"),
        pytest.param(b'"tekst"', id="napis"),
        pytest.param(b"\xff\xfe\x00{", id="bajty_spoza_utf8"),
        # 100000 nawiasow parsuje sie na 3.14 bez RecursionError; milion juz go rzuca
        pytest.param(b"[" * 1_000_000, id="gleboko_zagniezdzony_json"),
    ],
)
def test_nieczytelny_zapis_przebiegu_daje_pelny_przebieg_a_nie_blad_ani_sukces_bez_dowodu(
    sesja, tresc
):
    assert sesja.main() == 0
    sesja.plik_zapisu.write_bytes(tresc)
    assert sesja.main() == 0
    assert sesja.wzorce() == WSZYSTKIE
    assert sesja.main() == 0  # zapis naprawiony: kolejny przebieg jest znow przyrostowy
    assert sesja.s.mutmut("run") == []


def test_zapis_z_survivorem_uszkodzony_daje_pelny_przebieg_z_kodem_wg_nowych_wynikow(sesja):
    sesja.s.u["results"] = _wyniki((KLUCZ_INNA, "survived"))
    assert sesja.main() == 1
    sesja.plik_zapisu.write_text("{", encoding="utf-8")
    sesja.s.u["results"] = ZABITE
    assert sesja.main() == 0
    assert sesja.wzorce() == WSZYSTKIE


def test_zapis_bedacy_katalogiem_daje_pelny_przebieg_i_kod_2_z_komunikatem_bez_pliku_tymczasowego(
    sesja, capsys
):
    assert sesja.main() == 0
    sesja.plik_zapisu.unlink()
    sesja.plik_zapisu.mkdir()
    capsys.readouterr()
    assert sesja.main() == 2  # nie da sie zapisac: blad narzedzia, nie traceback
    assert sesja.wzorce() == WSZYSTKIE  # nieczytelny zapis: mutmut dostal pelna liste
    assert f"{sesja.plik_zapisu}: nie da sie zapisac przebiegu" in capsys.readouterr().err
    assert list(sesja.stan.glob("*.tmp")) == []


def test_katalog_zapisu_pod_zwyklym_plikiem_to_kod_2_z_komunikatem(sesja, tmp_path, capsys):
    plik = tmp_path / "zwykly-plik"
    plik.write_text("to nie katalog")
    sesja.stan = plik / "podkatalog"
    assert sesja.main() == 2
    assert f"{plik / 'podkatalog' / ZAPIS}: nie da sie zapisac przebiegu" in capsys.readouterr().err


def test_blad_git_ls_files_dla_testow_daje_kod_2_i_nie_woła_mutmuta(sesja, capsys):
    sesja.s.u["tests_rc"] = 128
    assert sesja.main() == 2
    assert sesja.s.mutmut("run") == []
    assert not sesja.plik_zapisu.exists()
    assert "git ls-files tests nie powiodl sie: fatal: ls-files tests" in capsys.readouterr().err


def test_katalog_zamiast_pliku_na_liscie_testow_to_kod_2_a_nie_traceback(sesja, capsys):
    (sesja.korzen / "tests/katalog.py").mkdir()
    sesja.s.u["tests_dodatkowe"] = "tests/katalog.py\0"
    assert sesja.main() == 2
    assert sesja.s.mutmut("run") == []
    assert "tests/katalog.py: nie da sie odczytac" in capsys.readouterr().err


def test_plik_testow_z_listy_gita_usuniety_z_dysku_nie_przerywa_przebiegu(sesja):
    sesja.s.u["tests_dodatkowe"] = "tests/znikniety.py\0"
    assert sesja.main() == 0
    assert sesja.wzorce() == WSZYSTKIE


def test_survivor_z_nieaktualnych_wynikow_mutmuta_o_niezmienionej_funkcji_nie_liczy_sie(sesja):
    assert sesja.main() == 0  # zapis: inna zabita
    sesja.zrodlo(ZRODLO_ZMIENIONE)
    # mutmut results pokazuje caly cache, takze mutanty niezmienionej funkcji (tu: starszy wpis)
    sesja.s.u["results"] = _wyniki((KLUCZ_A, "killed"), (KLUCZ_INNA, "survived"))
    assert sesja.main() == 0  # o `inna` rozstrzyga zapis, nie ten wpis


def test_survivor_jednej_funkcji_nie_trafia_do_zapisu_drugiej(sesja):
    sesja.s.u["results"] = _wyniki((KLUCZ_INNA, "survived"))
    assert sesja.main() == 1
    sesja.zrodlo(ZRODLO_INNA_ZMIENIONA)
    sesja.s.u["results"] = ZABITE
    assert sesja.main() == 0  # `zapisz` w zapisie nie ma survivora `inna`


def _uszkodzenia(zapis: dict) -> list[tuple[str, dict]]:
    """Kazde uszkodzenie struktury zapisu z osobna: (opis, zapis po uszkodzeniu)."""
    wynik = []
    for klucz in zapis:
        bez = {k: v for k, v in zapis.items() if k != klucz}
        wynik.append((f"brak klucza {klucz}", bez))
        wynik.append((f"zly typ klucza {klucz}", {**zapis, klucz: 12345.5}))
    wersja = zapis["wersja"]
    wynik.append(("wersja + 1", {**zapis, "wersja": wersja + 1}))
    wynik.append(("wersja jako napis", {**zapis, "wersja": str(wersja)}))
    if wersja == 1:
        wynik.append(("wersja jako true", {**zapis, "wersja": True}))
    mapa = next(k for k, v in zapis.items() if isinstance(v, dict))
    wynik.append(("wpisy funkcji jako lista", {**zapis, mapa: list(zapis[mapa])}))
    for wzorzec, wpis in zapis[mapa].items():
        for pole in wpis:
            kopia = json.loads(json.dumps(zapis))
            del kopia[mapa][wzorzec][pole]
            wynik.append((f"wpis {wzorzec} bez pola {pole}", kopia))
            kopia = json.loads(json.dumps(zapis))
            kopia[mapa][wzorzec][pole] = 12345.5
            wynik.append((f"wpis {wzorzec}, pole {pole} zlego typu", kopia))
            if isinstance(wpis[pole], list):
                kopia = json.loads(json.dumps(zapis))
                kopia[mapa][wzorzec][pole] = [*wpis[pole], 12345.5]
                wynik.append((f"wpis {wzorzec}, lista {pole} z liczba", kopia))
        kopia = json.loads(json.dumps(zapis))
        kopia[mapa][wzorzec] = 12345.5
        wynik.append((f"wpis {wzorzec} nie jest obiektem", kopia))
    return wynik


def test_kazde_uszkodzenie_struktury_zapisu_daje_pelny_przebieg(sesja):
    sesja.s.u["results"] = _wyniki((KLUCZ_INNA, "survived"))
    assert sesja.main() == 1
    uszkodzenia = _uszkodzenia(sesja.zapis())
    assert len(uszkodzenia) >= 10
    for opis, uszkodzony in uszkodzenia:
        sesja.plik_zapisu.write_text(json.dumps(uszkodzony), encoding="utf-8")
        assert sesja.main() == 1, opis
        assert sesja.wzorce() == WSZYSTKIE, opis  # pelny przebieg nadpisuje zapis poprawnym


def test_odcisk_testow_nie_zalezy_od_kolejnosci_plikow_ani_ziarna_hasha_tylko_od_posortowanych_sciezek(
    tmp_path, monkeypatch
):
    nazwy = ["tests/z.py", "tests/b.py", "tests/m.py", "tests/a.py", "tests/q.py", "tests/c.py",
             "tests/x.py", "tests/d.py", "tests/k.py", "tests/e.py"]
    for nazwa in nazwy:
        _plik(tmp_path, nazwa, f"# {nazwa}\n")
    _pyproject(tmp_path, "[tool.mutmut]\nsource_paths = []\n")
    # atrapa gita zwraca sciezki w nieposortowanej kolejnosci
    wyjscie = "".join(n + "\0" for n in nazwy)
    monkeypatch.setattr(
        mutanty_zmian,
        "uruchom",
        lambda argv: subprocess.CompletedProcess(argv, 0, stdout=wyjscie, stderr=""),
    )
    oczekiwany = hashlib.sha256()
    for sciezka in sorted([*nazwy, "pyproject.toml"]):
        tresc = (tmp_path / sciezka).read_bytes()
        oczekiwany.update(sciezka.encode("utf-8") + b"\0" + hashlib.sha256(tresc).digest())

    assert mutanty_zmian.odcisk_testow(tmp_path) == oczekiwany.hexdigest()


def test_plik_ignorowany_przez_git_pod_tests_nie_psuje_trybu_przyrostowego(sesja):
    sciezka = "tests/__pycache__/test_a.pyc"
    _plik(sesja.korzen, sciezka, "bajty-1")
    sesja.s.ignorowane.add(sciezka)
    assert sesja.main() == 0
    _plik(sesja.korzen, sciezka, "bajty-2")  # kazdy pytest przepisuje .pyc
    assert sesja.main() == 0
    assert sesja.s.mutmut("run") == []  # przebieg przyrostowy, nie pelny


def test_przebieg_przyrostowy_bez_zmutowanych_funkcji_nie_twierdzi_ze_nie_ma_przezywajacych(
    sesja, capsys
):
    assert sesja.main() == 0
    assert "brak przezywajacych mutantow w zmienionych funkcjach" in capsys.readouterr().err
    assert sesja.main() == 0
    assert sesja.s.mutmut("run") == []
    assert "brak przezywajacych mutantow" not in capsys.readouterr().err


def test_survivory_z_zapisu_i_z_nowego_przebiegu_sa_wypisane_w_kolejnosci_alfabetycznej(
    sesja, capsys
):
    stare = [f"cockpit.services.a.x_inna__mutmut_{n}" for n in (4, 2, 3, 1)]
    nowe = [f"cockpit.services.a.x_zapisz__mutmut_{n}" for n in (4, 3, 2, 1)]  # odwrotnie
    sesja.s.u["results"] = _wyniki(*[(k, "survived") for k in stare], (KLUCZ_A, "killed"))
    assert sesja.main() == 1
    capsys.readouterr()
    sesja.zrodlo(ZRODLO_ZMIENIONE)  # zmienia sie tylko `zapisz`; survivory `inna` ida z zapisu
    sesja.s.u["results"] = _wyniki(*[(k, "survived") for k in nowe])
    assert sesja.main() == 1
    assert capsys.readouterr().out.splitlines() == sorted([*stare, *nowe])


def test_komunikat_o_braku_mutantow_dla_zmienionych_funkcji_jest_na_stderr_tylko_gdy_ich_brak(
    monkeypatch, tmp_path, capsys
):
    komunikat = "mutmut nie wygenerowal mutantow dla zmienionych funkcji"
    s = _Srodowisko(
        diff=_diff_a(),
        run_rc=1,
        run_out="AssertionError: Filtered for specific mutants, but nothing matches",
    )
    assert _main(monkeypatch, tmp_path, s) == 0
    assert komunikat in capsys.readouterr().err.splitlines()
    s2 = _Srodowisko(diff=_diff_a(), results=_wyniki((KLUCZ_A, "killed")))
    (tmp_path / "drugi").mkdir()
    assert _main(monkeypatch, tmp_path / "drugi", s2) == 0
    assert komunikat not in capsys.readouterr().err


# --- HAR-236: faza `statystyki` mierzona ze znacznikow w wyjsciu `mutmut run` (`na_linie`) -----


def _czasy_z_biegu(monkeypatch, tmp_path, capsys, linie, **ust) -> dict[str, str]:
    s = _Srodowisko(
        diff=_diff_a(), results=_wyniki((KLUCZ_A, "killed")), linie_biegu=linie, **ust
    )
    assert _main(monkeypatch, tmp_path, s) == 0
    return _czasy_faz(capsys.readouterr().err)


def _sek(tekst: str) -> float:
    assert re.fullmatch(r"\d+\.\d s", tekst), tekst
    return float(tekst.removesuffix(" s"))


def test_statystyki_mierza_czas_miedzy_znacznikiem_statystyk_a_pierwszym_znacznikiem_testow(
    monkeypatch, tmp_path, capsys
):
    linie = [(0, "Running stats"), (0.4, "Running clean tests"), (0.4, "Running mutation testing")]
    czasy = _czasy_z_biegu(monkeypatch, tmp_path, capsys, linie)
    assert 0.3 <= _sek(czasy["statystyki"]) < 0.8


def test_koniec_statystyk_to_tez_znacznik_mutacji_gdy_nie_bylo_czystych_testow(
    monkeypatch, tmp_path, capsys
):
    linie = [(0, "Running stats"), (0.4, "Running mutation testing")]
    czasy = _czasy_z_biegu(monkeypatch, tmp_path, capsys, linie)
    assert 0.3 <= _sek(czasy["statystyki"]) < 0.8


def test_mutanty_licza_sie_od_konca_statystyk_a_nie_od_startu_mutmuta(
    monkeypatch, tmp_path, capsys
):
    linie = [(0, "Running stats"), (0.6, "Running mutation testing"), (0.0, "koniec")]
    czasy = _czasy_z_biegu(monkeypatch, tmp_path, capsys, linie, opoznienie_biegu=0.3)
    assert _sek(czasy["statystyki"]) >= 0.5
    assert 0.2 <= _sek(czasy["mutanty"]) < 0.55  # 0.3 s po znaczniku, bez 0.6 s statystyk


def test_mutanty_licza_sie_od_pierwszego_znacznika_testow_do_konca_procesu(monkeypatch, tmp_path, capsys):
    linie = [(0, "Running stats"), (0.0, "Running clean tests"), (0.5, "Running mutation testing")]
    czasy = _czasy_z_biegu(monkeypatch, tmp_path, capsys, linie, opoznienie_biegu=0.4)
    assert 0.8 <= _sek(czasy["mutanty"]) < 5  # od `Running clean tests`: 0.5 + 0.4 s


def test_bez_znacznika_statystyk_ta_faza_to_brak_pomiaru_a_nie_zero(monkeypatch, tmp_path, capsys):
    linie = [(0.0, "cos innego"), (0.2, "Running mutation testing")]
    czasy = _czasy_z_biegu(monkeypatch, tmp_path, capsys, linie)
    assert czasy["statystyki"] == "brak pomiaru"


def test_bez_zadnych_znacznikow_statystyki_to_brak_pomiaru_a_mutanty_licza_caly_bieg(
    monkeypatch, tmp_path, capsys
):
    czasy = _czasy_z_biegu(
        monkeypatch, tmp_path, capsys, [(0.0, "linia bez znacznika")], opoznienie_biegu=0.3
    )
    assert czasy["statystyki"] == "brak pomiaru"
    assert 0.2 <= _sek(czasy["mutanty"]) < 5


def test_znacznik_w_srodku_linii_tez_rozpoczyna_faze(monkeypatch, tmp_path, capsys):
    linie = [(0, "[12:00] Running stats..."), (0.4, "[12:01] Running mutation testing...")]
    czasy = _czasy_z_biegu(monkeypatch, tmp_path, capsys, linie)
    assert 0.3 <= _sek(czasy["statystyki"]) < 0.8


def test_uruchom_bez_na_linie_zwraca_wyjscie_jak_dotad_a_z_na_linie_wola_ja_w_trakcie_procesu():
    kod = "import sys, time; print('a', flush=True); time.sleep(0.6); print('b', flush=True)"
    zebrane: list[tuple[str, float]] = []
    wynik = mutanty_zmian.uruchom(
        [sys.executable, "-c", kod],
        na_linie=lambda linia: zebrane.append((linia.strip(), time.monotonic())),
    )
    assert wynik.returncode == 0
    assert wynik.stdout == "a\nb\n"
    assert [linia for linia, _ in zebrane] == ["a", "b"]
    assert zebrane[1][1] - zebrane[0][1] >= 0.4  # linia "a" doszla, zanim proces skonczyl

    bez = mutanty_zmian.uruchom([sys.executable, "-c", kod])
    assert (bez.returncode, bez.stdout) == (0, "a\nb\n")
    assert wynik.stderr == ""


def test_koniec_statystyk_to_pierwszy_znacznik_a_kolejne_znaczniki_konca_sa_ignorowane(
    monkeypatch, tmp_path, capsys
):
    linie = [(0, "Running stats"), (0.1, "Running clean tests"), (0.6, "Running mutation testing")]
    czasy = _czasy_z_biegu(monkeypatch, tmp_path, capsys, linie, opoznienie_biegu=0.3)
    assert _sek(czasy["statystyki"]) < 0.4  # 0.1 s, nie 0.7 s
    assert _sek(czasy["mutanty"]) >= 0.8  # od `Running clean tests`: 0.6 + 0.3 s


def test_linia_bez_znacznika_nie_konczy_statystyk(monkeypatch, tmp_path, capsys):
    linie = [(0, "Running stats"), (0.3, "cos innego"), (0.3, "Running mutation testing")]
    czasy = _czasy_z_biegu(monkeypatch, tmp_path, capsys, linie)
    assert 0.5 <= _sek(czasy["statystyki"]) < 1.2


def test_znacznik_statystyk_bez_znacznika_konca_to_brak_pomiaru_statystyk(
    monkeypatch, tmp_path, capsys
):
    czasy = _czasy_z_biegu(monkeypatch, tmp_path, capsys, [(0, "Running stats")])
    assert czasy["statystyki"] == "brak pomiaru"


def test_linia_z_obydwoma_znacznikami_zaczyna_statystyki_ale_ich_nie_konczy(
    monkeypatch, tmp_path, capsys
):
    linie = [(0, "Running stats Running clean tests"), (0.4, "Running mutation testing")]
    czasy = _czasy_z_biegu(monkeypatch, tmp_path, capsys, linie)
    assert 0.3 <= _sek(czasy["statystyki"]) < 0.8


def test_statystyki_mierza_cala_faze_gdy_mutmut_odswieza_znacznik_wielokrotnie(
    monkeypatch, tmp_path, capsys
):
    # prawdziwy mutmut dopisuje `\r<spinner> Running stats` co ~0.1 s; start to pierwszy znacznik
    linie = [
        (0, "\u280b Running stats"),
        (0.3, "\u2819 Running stats"),
        (0.3, "\u2839 Running stats"),
        (0.1, "Running clean tests"),
    ]
    czasy = _czasy_z_biegu(monkeypatch, tmp_path, capsys, linie)
    assert _sek(czasy["statystyki"]) >= 0.6


def test_czasy_faz_trafiaja_na_stderr_gdy_mutmut_run_konczy_sie_bledem(
    monkeypatch, tmp_path, capsys
):
    s = _Srodowisko(diff=_diff_a(), run_rc=1, run_out="Traceback: cos innego")
    assert _main(monkeypatch, tmp_path, s) == 2
    czasy = _czasy_faz(capsys.readouterr().err)
    _sek(czasy["wybor-funkcji"])
    assert {czasy["statystyki"], czasy["wyniki"]} == {"brak pomiaru"}


def test_czasy_faz_trafiaja_na_stderr_gdy_mutmut_results_konczy_sie_bledem(
    monkeypatch, tmp_path, capsys
):
    s = _Srodowisko(diff=_diff_a(), results_rc=1)
    assert _main(monkeypatch, tmp_path, s) == 2
    czasy = _czasy_faz(capsys.readouterr().err)
    _sek(czasy["wybor-funkcji"])
    assert czasy["statystyki"] == "brak pomiaru"


# --- `uruchom` z `na_linie` na prawdziwym procesie -----------------------------------------------


def _skrypt(tresc: str) -> list[str]:
    return [sys.executable, "-c", tresc]


def test_uruchom_z_na_linie_laczy_stderr_ze_stdout_i_zwraca_pusty_stderr():
    kod = "import sys; print('o', flush=True); print('e', file=sys.stderr, flush=True); sys.exit(3)"
    zebrane: list[str] = []
    wynik = mutanty_zmian.uruchom(_skrypt(kod), na_linie=zebrane.append)
    assert wynik.returncode == 3
    assert sorted(wynik.stdout.splitlines()) == ["e", "o"]
    assert sorted(linia.strip() for linia in zebrane) == ["e", "o"]
    assert wynik.stderr == ""


def test_uruchom_z_na_linie_nie_wywala_sie_na_bajtach_spoza_utf8():
    kod = "import sys; sys.stdout.buffer.write(b'\\xff\\n'); sys.stdout.flush()"
    wynik = mutanty_zmian.uruchom(_skrypt(kod), na_linie=lambda _linia: None)
    assert wynik.stdout == "�\n"


def test_uruchom_z_na_linie_brak_binarki_to_blad_narzedzia_z_pelnym_komunikatem(tmp_path):
    brak = str(tmp_path / "nie_ma_takiej_binarki")
    # tresc bledu systemu, jak ja zobaczy `uruchom` (Popen rzuca dokladnie ten wyjatek)
    prawdziwy = FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), brak)
    with pytest.raises(BladNarzedzia) as blad:
        mutanty_zmian.uruchom([brak], na_linie=lambda _linia: None)
    assert str(blad.value) == f"nie da sie uruchomic {brak}: {prawdziwy}"


class _ZapamietajProcesy:
    """Podmiana `subprocess.Popen`: prawdziwy proces, ale zapamietany do asercji po `uruchom`."""

    def __init__(self, prawdziwy):
        self.prawdziwy = prawdziwy
        self.procesy: list[subprocess.Popen] = []

    def __call__(self, *args, **kwargs):
        proces = self.prawdziwy(*args, **kwargs)
        self.procesy.append(proces)
        return proces


class _StrumienZBledem:
    """`proces.stdout`, ktorego odczyt rzuca OSError; `close` zamyka prawdziwy strumien."""

    def __init__(self, prawdziwy):
        self.prawdziwy = prawdziwy

    def readline(self):
        raise OSError("zerwany potok")

    def close(self):
        self.prawdziwy.close()

    @property
    def closed(self):
        return self.prawdziwy.closed


SPI_PO_LINII = "import os, time; print(os.getpid(), flush=True); time.sleep(5)"


def _zapamietane(monkeypatch) -> _ZapamietajProcesy:
    zapamietane = _ZapamietajProcesy(subprocess.Popen)
    monkeypatch.setattr(subprocess, "Popen", zapamietane)
    return zapamietane


def test_blad_z_na_linie_przechodzi_jako_oryginalny_wyjatek_a_proces_jest_zabity_i_zebrany(
    monkeypatch,
):
    zapamietane = _zapamietane(monkeypatch)
    oryginalny = OSError("zerwany potok")

    def zly_odbiorca(_linia: str) -> None:
        raise oryginalny

    start = time.monotonic()
    with pytest.raises(OSError) as wyjatek:
        mutanty_zmian.uruchom(_skrypt(SPI_PO_LINII), na_linie=zly_odbiorca)
    assert wyjatek.value is oryginalny
    assert not isinstance(wyjatek.value, BladNarzedzia)
    assert time.monotonic() - start < 3  # proces zabity, a nie dopiero dojechal do konca
    (proces,) = zapamietane.procesy
    assert proces.poll() is not None
    assert not czy_zyje(proces.pid)  # takze zebrany (zombie tez zyje dla os.kill(pid, 0))


def test_oserror_z_na_linie_nie_dostaje_etykiety_bledu_odczytu_wyjscia(monkeypatch):
    _zapamietane(monkeypatch)

    def zly_odbiorca(_linia: str) -> None:
        raise OSError("zerwany potok")

    with pytest.raises(OSError, match=r"^zerwany potok$") as wyjatek:
        mutanty_zmian.uruchom(_skrypt(SPI_PO_LINII), na_linie=zly_odbiorca)
    assert "nie da sie odczytac wyjscia" not in str(wyjatek.value)
    assert wyjatek.value.args == ("zerwany potok",)


def test_blad_odczytu_strumienia_to_blad_narzedzia_z_pelnym_komunikatem_a_proces_jest_zabity(
    monkeypatch,
):
    zapamietane = _zapamietane(monkeypatch)
    prawdziwy_popen = zapamietane.prawdziwy

    def popen_ze_zlym_strumieniem(*args, **kwargs):
        proces = prawdziwy_popen(*args, **kwargs)
        proces.stdout = _StrumienZBledem(proces.stdout)
        zapamietane.procesy.append(proces)
        return proces

    monkeypatch.setattr(subprocess, "Popen", popen_ze_zlym_strumieniem)
    argv = _skrypt(SPI_PO_LINII)

    start = time.monotonic()
    with pytest.raises(BladNarzedzia) as blad:
        mutanty_zmian.uruchom(argv, na_linie=lambda _linia: None)
    assert str(blad.value) == f"nie da sie odczytac wyjscia {argv[0]}: zerwany potok"
    assert time.monotonic() - start < 3
    (proces,) = zapamietane.procesy
    assert proces.poll() is not None
    assert not czy_zyje(proces.pid)


@pytest.mark.parametrize("scenariusz", ["sukces", "wyjatek_z_na_linie", "blad_odczytu"])
def test_po_uruchom_z_na_linie_strumien_wyjscia_procesu_jest_zamkniety(monkeypatch, scenariusz):
    zapamietane = _zapamietane(monkeypatch)
    prawdziwy_popen = zapamietane.prawdziwy
    kod = "print('a', flush=True)" if scenariusz == "sukces" else SPI_PO_LINII
    if scenariusz == "blad_odczytu":

        def popen_ze_zlym_strumieniem(*args, **kwargs):
            proces = prawdziwy_popen(*args, **kwargs)
            proces.stdout = _StrumienZBledem(proces.stdout)
            zapamietane.procesy.append(proces)
            return proces

        monkeypatch.setattr(subprocess, "Popen", popen_ze_zlym_strumieniem)

    def odbiorca(_linia: str) -> None:
        if scenariusz == "wyjatek_z_na_linie":
            raise ZeroDivisionError

    oczekiwany = {
        "sukces": None,
        "wyjatek_z_na_linie": ZeroDivisionError,
        "blad_odczytu": BladNarzedzia,
    }[scenariusz]
    if oczekiwany is None:
        mutanty_zmian.uruchom(_skrypt(kod), na_linie=odbiorca)
    else:
        with pytest.raises(oczekiwany):
            mutanty_zmian.uruchom(_skrypt(kod), na_linie=odbiorca)
    (proces,) = zapamietane.procesy
    assert proces.stdout.closed is True


def test_wyjatek_z_na_linie_zabija_proces_i_go_zbiera_zanim_pojdzie_dalej():
    kod = "import os, time; print(os.getpid(), flush=True); time.sleep(5)"
    pidy: list[int] = []

    def zly_odbiorca(linia: str) -> None:
        pidy.append(int(linia))
        raise ZeroDivisionError

    start = time.monotonic()
    with pytest.raises(ZeroDivisionError):
        mutanty_zmian.uruchom(_skrypt(kod), na_linie=zly_odbiorca)
    assert time.monotonic() - start < 3
    assert not czy_zyje(pidy[0])


def test_uruchom_z_na_linie_nie_otwiera_nowej_sesji_wiec_sygnal_do_grupy_zabija_i_mutmuta():
    # Nowa sesja (start_new_session) zostawilaby mutmuta jako sierote po SIGTERM do grupy bramek.
    wynik = mutanty_zmian.uruchom(
        _skrypt("import os; print(os.getpgid(0), os.getsid(0))"), na_linie=lambda _linia: None
    )
    assert wynik.stdout.split() == [str(os.getpgid(0)), str(os.getsid(0))]


# Ostrzezenia trybu przejsciowego: kod wyjscia i stdout bez zmian.
OSTRZEZENIE_BRAK_DOWODU = "OSTRZEZENIE: brak dowodu:"
OSTRZEZENIE_POZA_ZASIEGIEM = "OSTRZEZENIE: zmiana poza zasiegiem mutmuta: "
# Sciezka skladana z czesci: pelny literal lamie kryterium `git grep` HAR-260.
DAWNNA_TABELA = "tests/mutanty_" + "reczne/HAR-1.toml"
NIC_NIE_PASUJE ="AssertionError: Filtered for specific mutants, but nothing matches"


def test_zmienione_funkcje_bez_zadnego_mutanta_daja_ostrzezenie_brak_dowodu_bez_zmiany_kodu(
    monkeypatch, tmp_path, capsys
):
    s = _Srodowisko(diff=_diff_a(), run_rc=1, run_out=NIC_NIE_PASUJE)
    assert _main(monkeypatch, tmp_path, s) == 0
    wyjscie = capsys.readouterr()
    assert wyjscie.out == ""
    assert [w for w in wyjscie.err.splitlines() if w.startswith(OSTRZEZENIE_BRAK_DOWODU)] == [
        f"{OSTRZEZENIE_BRAK_DOWODU} 1 zmienionych funkcji bez mutantow"
    ]


def test_zmienione_funkcje_z_mutantami_nie_daja_ostrzezenia_brak_dowodu(monkeypatch, tmp_path, capsys):
    s = _Srodowisko(diff=_diff_a(), results=_wyniki((KLUCZ_A, "killed")))
    assert _main(monkeypatch, tmp_path, s) == 0
    assert OSTRZEZENIE_BRAK_DOWODU not in capsys.readouterr().err


def test_pusta_lista_zmienionych_funkcji_nie_daje_ostrzezenia_brak_dowodu(monkeypatch, tmp_path, capsys):
    s = _Srodowisko(diff=_diff_a(4))
    assert _main(monkeypatch, tmp_path, s) == 0
    assert OSTRZEZENIE_BRAK_DOWODU not in capsys.readouterr().err


@pytest.mark.parametrize(
    "plik",
    [
        "tools/x.py",
        "cicd/x.sh",
        "web/src/x.ts",
        ".claude/skills/s/SKILL.md",
        ".claude/hooks/h.py",
    ],
)
def test_zmiana_w_katalogu_poza_zasiegiem_mutmuta_daje_ostrzezenie_bez_zmiany_kodu(
    monkeypatch, tmp_path, capsys, plik
):
    s = _Srodowisko(zmienione_pliki=f"{plik}\n")
    assert _main(monkeypatch, tmp_path, s) == 0
    wyjscie = capsys.readouterr()
    assert wyjscie.out == ""
    assert f"{OSTRZEZENIE_POZA_ZASIEGIEM}{plik}" in wyjscie.err.splitlines()


def test_ostrzezenie_poza_zasiegiem_nie_wspomina_o_tabeli_recznych_mutantow(monkeypatch, tmp_path, capsys):
    s = _Srodowisko(zmienione_pliki="tools/x.py\n")
    assert _main(monkeypatch, tmp_path, s) == 0
    bledy = capsys.readouterr().err
    assert "tabel" not in bledy
    assert "recznych" not in bledy


def test_ostrzezenie_poza_zasiegiem_dotyczy_kazdego_pliku_osobno_a_kod_3_zostaje(
    monkeypatch, tmp_path, capsys
):
    s = _Srodowisko(
        diff=_diff_linii(STALE, 1), zmienione_pliki="tools/a.py\nweb/b.ts\nREADME.md\n"
    )
    assert _main(monkeypatch, tmp_path, s, pliki={STALE: ZRODLO_STALE}) == 3
    wyjscie = capsys.readouterr()
    assert wyjscie.out.splitlines() == [NAGLOWEK_BEZ_MUTANTOW, f"{STALE}:1"]
    ostrzezenia = [w for w in wyjscie.err.splitlines() if w.startswith(OSTRZEZENIE_POZA_ZASIEGIEM)]
    assert ostrzezenia == [f"{OSTRZEZENIE_POZA_ZASIEGIEM}tools/a.py", f"{OSTRZEZENIE_POZA_ZASIEGIEM}web/b.ts"]


def test_zmieniony_plik_toml_w_testach_nie_wylacza_ostrzezenia_o_zmianie_poza_zasiegiem(
    monkeypatch, tmp_path, capsys
):
    # dawniej tabela recznych mutantow wylaczala ostrzezenie; teraz zaden plik .toml nie jest wyjatkiem
    s = _Srodowisko(zmienione_pliki=f"tools/x.py\n{DAWNNA_TABELA}\n")
    assert _main(monkeypatch, tmp_path, s) == 0
    assert f"{OSTRZEZENIE_POZA_ZASIEGIEM}tools/x.py" in capsys.readouterr().err.splitlines()


def test_nowy_niezacommitowany_plik_toml_w_testach_nie_wylacza_ostrzezenia_o_zmianie_poza_zasiegiem(
    monkeypatch, tmp_path, capsys
):
    s = _Srodowisko(zmienione_pliki="tools/x.py\n", nowe=f"{DAWNNA_TABELA}\n")
    assert _main(monkeypatch, tmp_path, s) == 0
    assert f"{OSTRZEZENIE_POZA_ZASIEGIEM}tools/x.py" in capsys.readouterr().err.splitlines()


def test_nowy_niezacommitowany_plik_w_tools_daje_ostrzezenie(monkeypatch, tmp_path, capsys):
    s = _Srodowisko(nowe="tools/nowe_narzedzie.py\n")
    assert _main(monkeypatch, tmp_path, s) == 0
    assert f"{OSTRZEZENIE_POZA_ZASIEGIEM}tools/nowe_narzedzie.py" in capsys.readouterr().err


@pytest.mark.parametrize(
    "plik",
    ["src/tools/x.py", "mojtools/x.py", "docs/web/x.md", ".claude/rules/r.md"],
)
def test_podobna_sciezka_poza_lista_katalogow_nie_daje_ostrzezenia_poza_zasiegiem(
    monkeypatch, tmp_path, capsys, plik
):
    s = _Srodowisko(zmienione_pliki=f"{plik}\n")
    assert _main(monkeypatch, tmp_path, s) == 0
    assert OSTRZEZENIE_POZA_ZASIEGIEM not in capsys.readouterr().err


def test_nieudane_pobranie_listy_zmienionych_plikow_daje_ostrzezenie_a_nie_kod_2(
    monkeypatch, tmp_path, capsys
):
    # nowy plik w tools/ dalby ostrzezenie, gdyby po bledzie sprawdzanie szlo dalej
    s = _Srodowisko(nazwy_rc=128, nowe="tools/x.py\n")
    assert _main(monkeypatch, tmp_path, s) == 0
    bledy = capsys.readouterr().err.splitlines()
    assert "OSTRZEZENIE: nie sprawdzono zmian poza zasiegiem: fatal: name-only" in bledy
    assert f"{OSTRZEZENIE_POZA_ZASIEGIEM}tools/x.py" not in bledy


def test_zmienione_pliki_poza_zasiegiem_liczone_wzgledem_bazy_bez_zewnetrznego_diffu(monkeypatch, tmp_path):
    s = _Srodowisko()
    _main(monkeypatch, tmp_path, s)
    nazwy = [w for w in s.wywolania if w[:2] == ["git", "diff"] and "--name-only" in w]
    assert nazwy == [["git", "diff", "--name-only", "--no-ext-diff", "abc123"]]


def test_nieudane_pobranie_nowych_plikow_poza_zasiegiem_daje_ostrzezenie_a_nie_kod_2(
    monkeypatch, tmp_path, capsys
):
    s = _Srodowisko(nowe_poza_zasiegiem_rc=128, zmienione_pliki="tools/x.py\n")
    assert _main(monkeypatch, tmp_path, s) == 0
    bledy = capsys.readouterr().err.splitlines()
    assert "OSTRZEZENIE: nie sprawdzono zmian poza zasiegiem: fatal: ls-files poza" in bledy
    assert f"{OSTRZEZENIE_POZA_ZASIEGIEM}tools/x.py" not in bledy


def test_nowe_pliki_poza_zasiegiem_szukane_tylko_w_katalogach_poza_zasiegiem_mutmuta(monkeypatch, tmp_path):
    s = _Srodowisko()
    _main(monkeypatch, tmp_path, s)
    nowe = [w for w in s.wywolania if w[:2] == ["git", "ls-files"] and "tools/" in w]
    assert nowe == [
        [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            "tools/",
            "cicd/",
            "web/",
            ".claude/skills/",
            ".claude/hooks/",
        ]
    ]


# --- shardy mutantow zmienionych funkcji (fix-mutanty-zmian-shardy) ----------------------------
# Shard mutuje co N-ty mutant zmienionych funkcji i zapisuje statusy swoich mutantow; job `mutation`
# skleja je (`--z-shardow`) i dopiero wtedy ocenia survivory. Brak dowodu w sklejeniu to kod 2.

KLUCZ_INNA_1 = "cockpit.services.a.x_inna__mutmut_1"
WSZYSTKIE_MUTANTY = [KLUCZ_INNA_1, KLUCZ_A]


def _flagi_shardu(plik: Path, indeks: int = 0, liczba: int = 2) -> list[str]:
    return ["--katalog-dowodow", "{dowody}", "--indeks-shardu", str(indeks), "--liczba-shardow", str(liczba),
            "--wynik-shardu", str(plik)]


def _shard_main(monkeypatch, tmp_path, s, plik, *dodatkowe, indeks=0):
    katalog = _katalog_dowodow(tmp_path)
    flagi = [a.replace("{dowody}", str(katalog)) for a in _flagi_shardu(plik, indeks)]
    return _main(monkeypatch, tmp_path, s, *flagi, *dodatkowe)


def test_shard_mutuje_swoja_czesc_i_zapisuje_statusy_tylko_swoich_mutantow(monkeypatch, tmp_path):
    plik = tmp_path / "zmiany" / "shard-0.json"
    s = _Srodowisko(
        diff=DIFF_DWIE_FUNKCJE,
        przydzial={"wszystkie": WSZYSTKIE_MUTANTY, "shard": [KLUCZ_INNA_1]},
        results=_wyniki((KLUCZ_INNA_1, "survived"), (KLUCZ_A, "not checked")),
    )

    assert _shard_main(monkeypatch, tmp_path, s, plik, indeks=1) == 0
    assert json.loads(plik.read_text(encoding="utf-8")) == {
        "wzorce": WZORCE_DWOCH_FUNKCJI, "wszystkie": WSZYSTKIE_MUTANTY, "wyniki": {KLUCZ_INNA_1: "survived"},
    }
    (bieg,) = s.opakowane_run()
    assert bieg[bieg.index("--liczba-shardow") + 1] == "2"
    assert bieg[bieg.index("--indeks-shardu") + 1] == "1"
    assert bieg[-2:] == WZORCE_DWOCH_FUNKCJI
    assert s.mutmut("results")[0][4:] == ["--all", "true"]


def test_shard_bez_zmienionych_funkcji_zapisuje_pusty_wynik_bez_mutmuta(monkeypatch, tmp_path):
    plik = tmp_path / "shard-0.json"
    s = _Srodowisko(diff=_diff_a(4))

    assert _shard_main(monkeypatch, tmp_path, s, plik) == 0
    assert json.loads(plik.read_text(encoding="utf-8")) == {"wzorce": [], "wszystkie": [], "wyniki": {}}
    assert s.opakowane_run() == []


def test_blad_mutmuta_w_shardzie_to_kod_2_bez_wyniku(monkeypatch, tmp_path):
    plik = tmp_path / "shard-0.json"
    s = _Srodowisko(diff=DIFF_DWIE_FUNKCJE, run_rc=1, przydzial={"wszystkie": [], "shard": []})

    assert _shard_main(monkeypatch, tmp_path, s, plik) == 2
    assert not plik.exists()


def test_shard_bez_pliku_przydzialu_to_kod_2(monkeypatch, tmp_path):
    plik = tmp_path / "shard-0.json"
    s = _Srodowisko(diff=DIFF_DWIE_FUNKCJE, results=_wyniki((KLUCZ_A, "killed")))

    assert _shard_main(monkeypatch, tmp_path, s, plik) == 2
    assert not plik.exists()


def test_mutant_shardu_nieobecny_w_wynikach_mutmuta_to_kod_2(monkeypatch, tmp_path):
    plik = tmp_path / "shard-0.json"
    s = _Srodowisko(
        diff=DIFF_DWIE_FUNKCJE,
        przydzial={"wszystkie": WSZYSTKIE_MUTANTY, "shard": [KLUCZ_INNA_1, KLUCZ_A]},
        results=_wyniki((KLUCZ_INNA_1, "killed")),
    )

    assert _shard_main(monkeypatch, tmp_path, s, plik) == 2
    assert not plik.exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["--indeks-shardu", "0", "--liczba-shardow", "2", "--wynik-shardu", "w.json"],  # bez dowodow
        ["--katalog-dowodow", "{dowody}", "--indeks-shardu", "0", "--wynik-shardu", "w.json"],
        ["--katalog-dowodow", "{dowody}", "--indeks-shardu", "0", "--liczba-shardow", "2"],
        ["--katalog-dowodow", "{dowody}", "--indeks-shardu", "2", "--liczba-shardow", "2", "--wynik-shardu", "w"],
        ["--liczba-shardow", "2"],
        ["--z-shardow", "k"],
        ["--z-shardow", "k", "--liczba-shardow", "2", "--od-przebiegu", "s"],
        ["--katalog-dowodow", "{dowody}", "--indeks-shardu", "0", "--liczba-shardow", "2", "--wynik-shardu", "w",
         "--z-shardow", "k"],
    ],
    ids=["bez-dowodow", "bez-liczby", "bez-pliku", "zly-indeks", "sama-liczba", "z-shardow-bez-liczby",
         "z-shardow-przyrostowo", "shard-i-sklejenie"],
)
def test_niespojne_flagi_shardow_to_kod_2_bez_gita_i_mutmuta(monkeypatch, tmp_path, argv):
    katalog = _katalog_dowodow(tmp_path)
    s = _Srodowisko(diff=DIFF_DWIE_FUNKCJE)

    assert _main(monkeypatch, tmp_path, s, *[a.replace("{dowody}", str(katalog)) for a in argv]) == 2
    assert s.wywolania == []


# --- sklejenie wynikow shardow -------------------------------------------------------------------


def _zapisz_shardy(katalog: Path, *shardy: dict) -> Path:
    katalog.mkdir(exist_ok=True)
    for indeks, dane in enumerate(shardy):
        (katalog / f"shard-{indeks}.json").write_text(json.dumps(dane), encoding="utf-8")
    return katalog


def _shard(wyniki: dict, wszystkie=None, wzorce=None) -> dict:
    return {"wzorce": WZORCE_DWOCH_FUNKCJI if wzorce is None else wzorce,
            "wszystkie": WSZYSTKIE_MUTANTY if wszystkie is None else wszystkie, "wyniki": wyniki}


def _sklej(monkeypatch, tmp_path, *shardy, liczba=None, diff=DIFF_DWIE_FUNKCJE, capsys=None):
    katalog = _zapisz_shardy(tmp_path / "zmiany", *shardy)
    s = _Srodowisko(diff=diff)
    kod = _main(monkeypatch, tmp_path, s, "--z-shardow", str(katalog), "--liczba-shardow",
                str(len(shardy) if liczba is None else liczba))
    assert s.mutmut("run") == [] and s.opakowane_run() == [] and s.mutmut("results") == []
    return kod


def test_sklejenie_shardow_zglasza_przezywajacego_mutanta_z_dowolnego_shardu(monkeypatch, tmp_path, capsys):
    kod = _sklej(monkeypatch, tmp_path, _shard({KLUCZ_INNA_1: "killed"}), _shard({KLUCZ_A: "survived"}))

    assert kod == 1
    assert capsys.readouterr().out.splitlines() == [KLUCZ_A]


def test_sklejenie_shardow_z_zabitymi_mutantami_to_kod_0(monkeypatch, tmp_path):
    assert _sklej(monkeypatch, tmp_path, _shard({KLUCZ_INNA_1: "killed"}), _shard({KLUCZ_A: "killed"})) == 0


def test_shardy_bez_mutantow_dla_zmienionych_funkcji_daja_ostrzezenie_braku_dowodu(monkeypatch, tmp_path, capsys):
    kod = _sklej(monkeypatch, tmp_path, _shard({}, wszystkie=[]), _shard({}, wszystkie=[]))

    assert kod == 0
    assert "OSTRZEZENIE: brak dowodu: 2 zmienionych funkcji bez mutantow" in capsys.readouterr().err


def test_sklejenie_bez_zmienionych_funkcji_nie_czyta_shardow(monkeypatch, tmp_path):
    s = _Srodowisko(diff=_diff_a(4))

    assert _main(monkeypatch, tmp_path, s, "--z-shardow", str(tmp_path / "brak"), "--liczba-shardow", "2") == 0


@pytest.mark.parametrize(
    ("shardy", "liczba", "komunikat"),
    [
        ([_shard({KLUCZ_INNA_1: "killed", KLUCZ_A: "killed"})], 2, "shard-1.json: nie da sie odczytac"),
        ([_shard({KLUCZ_INNA_1: "killed"}), _shard({KLUCZ_A: "killed"}, wzorce=WZORCE_DWOCH_FUNKCJI[:1])], 2,
         "shard mutowal inne funkcje"),
        ([_shard({KLUCZ_INNA_1: "killed"}), _shard({KLUCZ_A: "killed"}, wszystkie=[KLUCZ_A])], 2,
         "inna lista mutantow niz w shardzie 0"),
        ([_shard({KLUCZ_INNA_1: "killed", KLUCZ_A: "killed"}), _shard({KLUCZ_A: "killed"})], 2,
         f"mutant {KLUCZ_A} w dwoch shardach"),
        ([_shard({KLUCZ_INNA_1: "killed"}), _shard({KLUCZ_A: "not checked"})], 2,
         f"mutant {KLUCZ_A} nie zostal sprawdzony"),
        ([_shard({KLUCZ_INNA_1: "killed"}), _shard({})], 2, "shardy nie sprawdzily 1 mutantow"),
        ([_shard({KLUCZ_INNA_1: "killed"}), _shard({KLUCZ_A: "killed", "obcy.x_f__mutmut_1": "killed"})], 2,
         "shardy nie sprawdzily 0 mutantow"),
        ([_shard({KLUCZ_INNA_1: "killed"}), {"wzorce": WZORCE_DWOCH_FUNKCJI, "wszystkie": WSZYSTKIE_MUTANTY}], 2,
         "zly format wyniku shardu"),
    ],
    ids=["brak-pliku", "inne-wzorce", "inna-lista", "dwa-shardy", "niesprawdzony", "brakujacy", "nadmiarowy",
         "zly-format"],
)
def test_sklejenie_bez_pelnego_dowodu_to_kod_2(monkeypatch, tmp_path, capsys, shardy, liczba, komunikat):
    assert _sklej(monkeypatch, tmp_path, *shardy, liczba=liczba) == 2
    assert komunikat in capsys.readouterr().err


# --- numeracja linii jak w AST (HAR-258) ---------------------------------------------------------
# AST dzieli zrodlo tylko na `\n`, `\r\n` i `\r`; `str.splitlines()` dzieli tez na `\x0c`, `\x1c`,
# `\x85`, ` ` i przesuwa numery linii wzgledem `lineno` z AST.

ZNAKI_NIEBEDACE_KONCEM_LINII = ["\x0c", "\x1c", "\x85", " "]
SCIEZKA_NOWEGO = "src/cockpit/services/n.py"


def test_linie_zrodla_dziela_tylko_na_lf_crlf_i_samotny_cr():
    assert mutanty_zmian.linie_zrodla("a\nb\r\nc\rd") == ["a", "b", "c", "d"]


@pytest.mark.parametrize("znak", ZNAKI_NIEBEDACE_KONCEM_LINII)
def test_linie_zrodla_nie_dziela_na_znakach_ktore_python_uznaje_za_zwykle(znak):
    assert mutanty_zmian.linie_zrodla(f"a{znak}b\nc") == [f"a{znak}b", "c"]


@pytest.mark.parametrize("znak", ZNAKI_NIEBEDACE_KONCEM_LINII)
def test_odcisk_funkcji_po_linii_z_form_feed_to_hash_jej_wlasnego_tekstu(znak):
    zrodlo = f"def f():\n    return 1  # {znak}\n\n\ndef g():\n    return 2\n"
    odciski = mutanty_zmian.wzorce_z_odciskami_dla_pliku(SCIEZKA, zrodlo, {5, 6})
    oczekiwany = hashlib.sha256("def g():\n    return 2".encode()).hexdigest()
    assert odciski == {f"{MODUL}.x_g__mutmut_*": oczekiwany}


def test_odcisk_funkcji_w_zrodle_z_samotnym_cr_to_hash_jej_wlasnego_tekstu():
    zrodlo = "def f():\r    return 1\r\r\rdef g():\r    return 2\r"
    odciski = mutanty_zmian.wzorce_z_odciskami_dla_pliku(SCIEZKA, zrodlo, {5, 6})
    oczekiwany = hashlib.sha256("def g():\n    return 2".encode()).hexdigest()
    assert odciski == {f"{MODUL}.x_g__mutmut_*": oczekiwany}


@pytest.mark.parametrize("znak", ZNAKI_NIEBEDACE_KONCEM_LINII)
def test_zmieniona_stala_po_linii_z_form_feed_jest_zgloszona_pod_wlasnym_numerem(znak):
    zrodlo = f"X = 1  # {znak}\nY = 2\n\n\ndef f():\n    return 1\n"
    assert _bez(zrodlo, 2) == [2]


def test_zmieniona_stala_w_zrodle_z_samotnym_cr_jest_zgloszona_pod_wlasnym_numerem():
    assert _bez("X = 1\rY = 2\r\r\rdef f():\r    return 1\r", 2) == [2]


@pytest.mark.parametrize("znak", ZNAKI_NIEBEDACE_KONCEM_LINII)
def test_nowy_plik_z_form_feed_ma_zmienione_tyle_linii_ile_ma_naprawde(tmp_path, znak):
    _plik(tmp_path, SCIEZKA_NOWEGO, f"A = 1  # {znak}\nB = 2\n")
    assert mutanty_zmian._nowe_pliki(f"{SCIEZKA_NOWEGO}\n", tmp_path) == {SCIEZKA_NOWEGO: {1, 2}}


PLIK_Z_SAMOTNYM_CR = b"A = 1\rB = 2\rC = 3\n\n\ndef g():\n    return 3\n"  # AST: g w liniach 6-7, git: 4-5
KOMUNIKAT_SAMOTNEGO_CR = (
    "samotny \\r jako koniec linii - numery linii z git diff nie zgadzaja sie z AST; zamien go na \\n"
)


def _plik_bajty(korzen: Path, sciezka: str, tresc: bytes) -> None:
    cel = korzen / sciezka
    cel.parent.mkdir(parents=True, exist_ok=True)
    cel.write_bytes(tresc)


def test_plik_z_samotnym_cr_to_blad_narzedzia_z_numerem_linii_zamiast_cichego_pominiecia_funkcji(tmp_path):
    _plik_bajty(tmp_path, SCIEZKA, PLIK_Z_SAMOTNYM_CR)
    with pytest.raises(BladNarzedzia) as wyjatek:
        mutanty_zmian.wzorce_z_odciskami({SCIEZKA: {5}}, ["src/cockpit/services/"], tmp_path)
    assert str(wyjatek.value) == f"{SCIEZKA}:1: {KOMUNIKAT_SAMOTNEGO_CR}"


def test_plik_z_samotnym_cr_nie_daje_zielonego_wyniku_w_zgloszeniu_linii_bez_mutantow(tmp_path):
    _plik_bajty(tmp_path, SCIEZKA, PLIK_Z_SAMOTNYM_CR)
    with pytest.raises(BladNarzedzia, match=r"runs\.py:1: samotny"):
        mutanty_zmian.bez_mutantow({SCIEZKA: {5}}, ["src/cockpit/services/"], tmp_path)


def test_samotny_cr_w_dalszej_linii_jest_zgloszony_pod_numerem_ast(tmp_path):
    _plik_bajty(tmp_path, SCIEZKA, b"A = 1\nB = 2\rC = 3\n")
    with pytest.raises(BladNarzedzia, match=r"runs\.py:2: samotny"):
        mutanty_zmian.wzorce_z_odciskami({SCIEZKA: {1}}, ["src/cockpit/services/"], tmp_path)


@pytest.mark.parametrize(
    ("tresc", "numer"),
    [
        pytest.param(b"A = 1\n\rB = 2\n", 2, id="po_lf_samotny_cr_liczony_jako_koniec_linii"),
        pytest.param(b"# a\x0cb\nA = 1\rB = 2\n", 2, id="form_feed_nie_jest_koncem_linii"),
    ],
)
def test_numer_linii_w_bledzie_samotnego_cr_liczy_konce_linii_jak_ast(tmp_path, tresc, numer):
    _plik_bajty(tmp_path, SCIEZKA, tresc)
    with pytest.raises(BladNarzedzia) as wyjatek:
        mutanty_zmian.zrodlo_z_dysku(tmp_path, SCIEZKA)
    assert str(wyjatek.value) == f"{SCIEZKA}:{numer}: {KOMUNIKAT_SAMOTNEGO_CR}"


def test_nowy_plik_z_samotnym_cr_to_blad_narzedzia(tmp_path):
    _plik_bajty(tmp_path, SCIEZKA_NOWEGO, PLIK_Z_SAMOTNYM_CR)
    with pytest.raises(BladNarzedzia, match=r"n\.py:1: samotny"):
        mutanty_zmian._nowe_pliki(f"{SCIEZKA_NOWEGO}\n", tmp_path)


def test_plik_z_koncami_linii_crlf_ma_te_same_numery_linii_co_git(tmp_path):
    _plik_bajty(tmp_path, SCIEZKA, b"A = 1\r\n\r\n\r\ndef g():\r\n    return 3\r\n")
    wynik = mutanty_zmian.wzorce_z_odciskami({SCIEZKA: {5}}, ["src/cockpit/services/"], tmp_path)
    assert list(wynik) == [f"{MODUL}.x_g__mutmut_*"]


@pytest.mark.parametrize(
    ("tresc", "oczekiwane"),
    [
        pytest.param("", set(), id="pusty_plik"),
        pytest.param("A = 1", {1}, id="bez_konca_linii"),
        pytest.param("A = 1\n\n", {1, 2}, id="pusta_linia_na_koncu"),
    ],
)
def test_nowy_plik_ma_zmienione_linie_od_pierwszej_do_ostatniej(tmp_path, tresc, oczekiwane):
    _plik(tmp_path, SCIEZKA_NOWEGO, tresc)
    assert mutanty_zmian._nowe_pliki(f"{SCIEZKA_NOWEGO}\n", tmp_path) == {SCIEZKA_NOWEGO: oczekiwane}
