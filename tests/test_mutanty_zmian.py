"""Mutanty zmienionych funkcji przed oddaniem pracy (HAR-133).

Skrypt `tools/mutanty_zmian.py` mapuje diff względem bazy na wzorce mutantów
(tylko funkcje i metody, które zmieniono), uruchamia na nich mutmut i zgłasza
przeżywające mutanty. Testy nie uruchamiają ani gita, ani mutmuta: diff,
źródła i wyniki mutmuta są daną w `tmp_path`, a `uruchom` jest podmieniane.
"""
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_zmiana_w_linii_dekoratora_daje_wzorzec_funkcji():
    zrodlo = "import functools\n\n@functools.cache\ndef f():\n    return 1\nx = 2\n"
    assert _wzorce(zrodlo, 3) == [f"{MODUL}.x_f__mutmut_*"]


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
