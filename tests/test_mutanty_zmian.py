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


def _main(monkeypatch, tmp_path, srodowisko, *argv):
    monkeypatch.setattr(mutanty_zmian, "uruchom", srodowisko)
    return mutanty_zmian.main(["--korzen", str(_repo(tmp_path)), *argv])


def test_brak_zmienionych_funkcji_daje_kod_0_i_nie_woła_mutmuta(monkeypatch, tmp_path):
    s = _Srodowisko(diff=_diff_a(4))  # linia 4 to przerwa między funkcjami
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


def test_diff_liczony_od_wspolnego_przodka_ograniczony_do_source_paths(monkeypatch, tmp_path):
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
        "src/cockpit/services/",
    ]


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
