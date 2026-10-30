"""Rownolegly przebieg testow bez bazy i bez zmiany mutmuta (HAR-143).

Zwykly przebieg (`pytest -q`, bez `baza` i `e2e`) idzie rownolegle (`-n 4`) lokalnie
i w CI. Testy `baza` i `e2e` oraz mutmut zostaja sekwencyjne: testy bazy dziela jedna
baze (`alembic downgrade base`) i port 8799, a mutmut woluje pytest osobno dla
kazdego mutanta, wiec `-n` w `addopts` zepsulby jego przebieg.

Czesc pierwsza uruchamia PRODUKCYJNY tests/conftest.py w osobnym przebiegu
(pytester, podproces, prawdziwy xdist). Czesc druga przypina konfiguracje
(pyproject.toml, ci.yml, tools/testy_pelne.sh) tokenizujac polecenia, a nie szukajac
podciagow: "-n" wystepuje w nazwach (np. "--numprocesses", "pip install -e").
"""
from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

from ruamel.yaml import YAML

KORZEN = Path(__file__).resolve().parents[1]
ZMIENNA_BAZY = "COCKPIT_TEST_DATABASE_URL"
ZMIENNA_WORKERA = "PYTEST_XDIST_WORKER"
ZMIENNA_WYMOGU = "COCKPIT_WYMAGAJ_BAZY"
URL_BAZY = "postgresql+asyncpg://nikt:nic@localhost:1/nigdzie"

INI_PROBKI = (
    "[pytest]\n"
    "markers =\n"
    "    baza: test wymaga PostgreSQL\n"
    "    e2e: test w przegladarce\n"
)

TEST_BAZY = '''import os

import pytest


@pytest.mark.baza
@pytest.mark.skipif(
    not os.environ.get("COCKPIT_TEST_DATABASE_URL"), reason="brak URL bazy testowej"
)
def test_probka():
    assert True
'''

# Bez skipif: skip z markera rozstrzyga probke w hooku wtyczki skipping (tryfirst),
# zanim straznik z conftest w ogole ruszy, wiec nie dowodzi niczego o straznik.
TEST_BAZY_BEZ_SKIPIF = '''import pytest


@pytest.mark.baza
def test_probka():
    assert True
'''

TEST_BEZ_MARKERA_BAZY = '''def test_probka():
    assert True
'''


def _uruchom(
    pytester,
    monkeypatch,
    tresc: str,
    *,
    url_bazy: str | None,
    worker: str | None = None,
    argumenty: tuple[str, ...] = (),
):
    """Przebieg probki z produkcyjnym conftest. Zmienne ustawiamy lub usuwamy jawnie:
    ten plik sam moze isc w workerze xdist, a podproces dziedziczy srodowisko."""
    monkeypatch.delenv(ZMIENNA_WYMOGU, raising=False)
    if url_bazy is None:
        monkeypatch.delenv(ZMIENNA_BAZY, raising=False)
    else:
        monkeypatch.setenv(ZMIENNA_BAZY, url_bazy)
    if worker is None:
        monkeypatch.delenv(ZMIENNA_WORKERA, raising=False)
    else:
        monkeypatch.setenv(ZMIENNA_WORKERA, worker)
    pytester.makeini(INI_PROBKI)
    pytester.makeconftest((KORZEN / "tests" / "conftest.py").read_text(encoding="utf-8"))
    pytester.makepyfile(procesy=(KORZEN / "tests" / "procesy.py").read_text(encoding="utf-8"))
    pytester.makepyfile(test_probka_przebiegu=tresc)
    return pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rs", *argumenty)


def _wynik(wynik) -> dict[str, int]:
    liczby = wynik.parseoutcomes()
    return {
        "passed": liczby.get("passed", 0),
        "skipped": liczby.get("skipped", 0),
        "oblane": liczby.get("failed", 0) + liczby.get("error", 0) + liczby.get("errors", 0),
    }


# --- Straznik: test bazy w rownoleglym przebiegu ---


def test_test_bazy_w_rownoleglym_przebiegu_z_baza_konczy_sie_porazka_z_wskazowka(
    pytester, monkeypatch
):
    wynik = _uruchom(
        pytester, monkeypatch, TEST_BAZY, url_bazy=URL_BAZY, argumenty=("-n", "2")
    )

    liczby = _wynik(wynik)
    assert liczby == {"passed": 0, "skipped": 0, "oblane": 1}
    assert wynik.ret != 0
    tekst = wynik.stdout.str()
    assert "rownolegle" in tekst
    assert '-m "baza and not e2e"' in tekst


def test_test_bazy_w_rownoleglym_przebiegu_bez_bazy_zostaje_pominiety_jak_dotad(
    pytester, monkeypatch
):
    wynik = _uruchom(pytester, monkeypatch, TEST_BAZY, url_bazy="", argumenty=("-n", "2"))

    assert _wynik(wynik) == {"passed": 0, "skipped": 1, "oblane": 0}
    assert wynik.ret == 0


def test_test_bazy_w_rownoleglym_przebiegu_bez_adresu_bazy_nie_jest_zatrzymywany_przez_straznika(
    pytester, monkeypatch
):
    wynik = _uruchom(
        pytester, monkeypatch, TEST_BAZY_BEZ_SKIPIF, url_bazy="", argumenty=("-n", "2")
    )

    assert _wynik(wynik) == {"passed": 1, "skipped": 0, "oblane": 0}
    assert wynik.ret == 0


def test_test_bazy_w_przebiegu_sekwencyjnym_z_baza_wykonuje_sie_normalnie(
    pytester, monkeypatch
):
    wynik = _uruchom(pytester, monkeypatch, TEST_BAZY, url_bazy=URL_BAZY)

    assert _wynik(wynik) == {"passed": 1, "skipped": 0, "oblane": 0}
    assert wynik.ret == 0


def test_pusta_zmienna_workera_nie_czyni_przebiegu_rownoleglym(pytester, monkeypatch):
    wynik = _uruchom(pytester, monkeypatch, TEST_BAZY, url_bazy=URL_BAZY, worker="")

    assert _wynik(wynik) == {"passed": 1, "skipped": 0, "oblane": 0}
    assert wynik.ret == 0


def test_test_bez_markera_bazy_w_rownoleglym_przebiegu_z_baza_przechodzi(
    pytester, monkeypatch
):
    wynik = _uruchom(
        pytester,
        monkeypatch,
        TEST_BEZ_MARKERA_BAZY,
        url_bazy=URL_BAZY,
        argumenty=("-n", "2"),
    )

    assert _wynik(wynik) == {"passed": 1, "skipped": 0, "oblane": 0}
    assert wynik.ret == 0


# --- Pomocniki parsujace polecenia ---

SEPARATORY = {"|", "||", "&&", ";", "2>&1"}


def _argumenty_pytest(linia: str) -> list[str] | None:
    """Tokeny po `pytest` albo `-m pytest` w jednym poleceniu; None, gdy linia nie wola pytesta."""
    tokeny = shlex.split(linia, comments=True)
    for i, token in enumerate(tokeny):
        if token == "pytest":
            wynik: list[str] = []
            for dalej in tokeny[i + 1 :]:
                if dalej in SEPARATORY:
                    break
                wynik.append(dalej)
            return wynik
    return None


def _ma_xdist(argumenty: list[str]) -> bool:
    for a in argumenty:
        if a == "-n" or (a.startswith("-n") and not a.startswith("--")):
            return True
        if a.split("=", 1)[0] in ("--numprocesses", "--dist"):
            return True
    return False


def _liczba_procesow(argumenty: list[str]) -> str | None:
    for i, a in enumerate(argumenty):
        if a == "-n" and i + 1 < len(argumenty):
            return argumenty[i + 1]
        if a.startswith("-n") and not a.startswith("--") and len(a) > 2:
            return a[2:]
    return None


def _wyrazenie_markerow(argumenty: list[str]) -> str | None:
    for i, a in enumerate(argumenty):
        if a == "-m" and i + 1 < len(argumenty):
            return argumenty[i + 1]
    return None


def test_pomocnik_wykrywa_xdist_w_kazdej_postaci_i_nie_myli_go_z_innymi_opcjami():
    assert _ma_xdist(_argumenty_pytest("pytest -q -n 4") or [])
    assert _ma_xdist(_argumenty_pytest("pytest -q -n4") or [])
    assert _ma_xdist(_argumenty_pytest("pytest -q -nauto") or [])
    assert _ma_xdist(_argumenty_pytest("pytest --numprocesses=4") or [])
    assert _ma_xdist(_argumenty_pytest("pytest --numprocesses 4") or [])
    assert _ma_xdist(_argumenty_pytest("pytest --dist loadfile") or [])
    assert not _ma_xdist(_argumenty_pytest("pytest -q") or [])
    assert not _ma_xdist(_argumenty_pytest('pytest -m "baza and not e2e"') or [])
    assert not _ma_xdist(_argumenty_pytest("pytest -p no:cacheprovider --no-header") or [])


def test_pomocnik_wyciaga_argumenty_pytesta_i_ucina_przy_potoku():
    linia = '"$PY" -m pytest -q -rs -n 4 -m "not baza and not e2e" 2>&1 | tee "$LOG" || rc=$?'
    argumenty = _argumenty_pytest(linia)

    assert argumenty == ["-q", "-rs", "-n", "4", "-m", "not baza and not e2e"]
    assert _liczba_procesow(argumenty) == "4"
    assert _wyrazenie_markerow(argumenty) == "not baza and not e2e"
    assert _argumenty_pytest("pip install -e .[dev]") is None
    assert _argumenty_pytest("# pytest -n 4") is None
    assert _liczba_procesow(["-q"]) is None


# --- Konfiguracja: pyproject.toml ---


def _pyproject() -> dict:
    return tomllib.loads((KORZEN / "pyproject.toml").read_text(encoding="utf-8"))


def test_domyslne_opcje_pytesta_nie_wlaczaja_rownoleglosci_bo_mutmut_woluje_pytest_per_mutant():
    addopts = _pyproject()["tool"]["pytest"]["ini_options"]["addopts"]
    tokeny = shlex.split(addopts) if isinstance(addopts, str) else list(addopts)

    assert tokeny  # pusty addopts oznaczalby, ze czytamy zla sekcje
    assert not _ma_xdist(tokeny)


def test_xdist_jest_w_zaleznosciach_deweloperskich_z_gorna_granica_wersji():
    dev = _pyproject()["project"]["optional-dependencies"]["dev"]

    wpisy = [w for w in dev if re.match(r"pytest-xdist\s*([<>=!~]|$)", w)]

    assert len(wpisy) == 1, f"oczekiwano jednego wpisu pytest-xdist w dev, jest: {wpisy}"
    assert "<" in wpisy[0], f"brak gornej granicy wersji: {wpisy[0]}"


# --- Konfiguracja: .github/workflows/ci.yml ---


def _joby_ci() -> dict:
    sciezka = KORZEN / ".github" / "workflows" / "ci.yml"
    return YAML(typ="safe").load(sciezka.read_text(encoding="utf-8"))["jobs"]


def _polecenia_pytest(job: dict) -> list[tuple[str, list[str]]]:
    wynik = []
    for krok in job.get("steps", []):
        for linia in str(krok.get("run", "")).splitlines():
            argumenty = _argumenty_pytest(linia)
            if argumenty is not None:
                wynik.append((linia.strip(), argumenty))
    return wynik


def test_job_python_uruchamia_zwykly_przebieg_na_czterech_procesach():
    kroki = [str(k.get("run", "")).strip() for k in _joby_ci()["python"]["steps"]]

    assert "pytest -q -n 4" in kroki


def test_job_python_nie_ma_drugiego_przebiegu_pytesta_bez_rownoleglosci():
    polecenia = _polecenia_pytest(_joby_ci()["python"])

    assert [linia for linia, _ in polecenia] == ["pytest -q -n 4"]


def test_joby_z_baza_i_e2e_zostaja_sekwencyjne():
    joby = _joby_ci()
    inne = {nazwa: job for nazwa, job in joby.items() if nazwa != "python"}
    wszystkie = [(n, linia, a) for n, j in inne.items() for linia, a in _polecenia_pytest(j)]

    assert wszystkie, "poza jobem python nie znaleziono zadnego wywolania pytest"
    for nazwa, linia, argumenty in wszystkie:
        assert not _ma_xdist(argumenty), f"job {nazwa}: rownolegle wywolanie: {linia}"


def test_krok_testow_bazy_w_ci_istnieje_i_jest_sekwencyjny():
    polecenia = _polecenia_pytest(_joby_ci()["e2e"])
    baza = [a for _, a in polecenia if _wyrazenie_markerow(a) == "baza and not e2e"]
    e2e = [a for _, a in polecenia if _wyrazenie_markerow(a) == "e2e"]

    assert len(baza) == 1
    assert len(e2e) == 1
    assert not _ma_xdist(baza[0])
    assert not _ma_xdist(e2e[0])


# --- Konfiguracja: tools/testy_pelne.sh ---


def _skrypt() -> str:
    return (KORZEN / "tools" / "testy_pelne.sh").read_text(encoding="utf-8")


def _przebiegi_skryptu() -> list[tuple[str, list[str]]]:
    wynik = []
    for linia in _skrypt().splitlines():
        if "-m pytest" not in linia:
            continue
        argumenty = _argumenty_pytest(linia)
        if argumenty is not None:
            wynik.append((linia.strip(), argumenty))
    return wynik


def test_pelny_zestaw_ma_trzy_przebiegi_bez_bazy_z_baza_i_e2e():
    wyrazenia = sorted(_wyrazenie_markerow(a) or "" for _, a in _przebiegi_skryptu())

    assert wyrazenia == sorted(["not baza and not e2e", "baza and not e2e", "e2e"])


def test_tylko_przebieg_bez_bazy_i_e2e_jest_rownolegly_na_czterech_procesach():
    rownolegle = [(linia, a) for linia, a in _przebiegi_skryptu() if _ma_xdist(a)]

    assert len(rownolegle) == 1
    _, argumenty = rownolegle[0]
    assert _liczba_procesow(argumenty) == "4"
    assert _wyrazenie_markerow(argumenty) == "not baza and not e2e"


def test_przebiegi_bazy_i_e2e_w_pelnym_zestawie_sa_sekwencyjne():
    przebiegi = _przebiegi_skryptu()
    baza = [a for _, a in przebiegi if _wyrazenie_markerow(a) == "baza and not e2e"]
    e2e = [a for _, a in przebiegi if _wyrazenie_markerow(a) == "e2e"]

    assert len(baza) == 1
    assert len(e2e) == 1
    assert not _ma_xdist(baza[0])
    assert not _ma_xdist(e2e[0])


def _logi_z_tee(tekst: str) -> set[str]:
    return set(re.findall(r'\btee\s+"?\$\{?(LOG_\w+)', tekst))


def _logi_w_kontroli_pominiec(tekst: str) -> set[str]:
    polaczony = tekst.replace("\\\n", " ")
    logi: set[str] = set()
    for linia in polaczony.splitlines():
        if re.search(r"\bgrep\b", linia) and "SKIPPED" in linia:
            logi |= set(re.findall(r"\$\{?(LOG_\w+)", linia))
    return logi


def test_kazdy_log_przebiegu_trafia_do_kontroli_pominiec():
    tekst = _skrypt()
    logi = _logi_z_tee(tekst)

    assert len(logi) == 3, f"oczekiwano trzech logow przebiegow, jest: {sorted(logi)}"
    nieskontrolowane = logi - _logi_w_kontroli_pominiec(tekst)
    assert not nieskontrolowane, f"logi poza kontrola SKIPPED: {sorted(nieskontrolowane)}"


def test_pomocnik_kontroli_pominiec_widzi_log_pominiety_w_grepie():
    fragment = (
        'pytest 2>&1 | tee "$LOG_A"\n'
        'pytest 2>&1 | tee "$LOG_B"\n'
        "grep -hE '^SKIPPED' \"$LOG_A\"\n"
    )

    assert _logi_z_tee(fragment) == {"LOG_A", "LOG_B"}
    assert _logi_w_kontroli_pominiec(fragment) == {"LOG_A"}


def test_kod_wyjscia_kazdego_przebiegu_oblewa_pelny_zestaw():
    tekst = _skrypt()
    przypisania = re.findall(r"\|\|\s*(rc_\w+)=\$\?", tekst)

    assert len(przypisania) == 3, f"oczekiwano trzech kodow wyjscia, jest: {przypisania}"
    for rc in przypisania:
        assert re.search(rf'\[\s*"\${rc}"\s+-ne\s+0\s*\]', tekst), f"{rc} nie wchodzi do exit 1"
