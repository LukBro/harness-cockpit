"""Pominiety test bazy oblewa przebieg, gdy CI wymaga bazy (HAR-12, HAR-134).

Regule wyznacza marker `baza`, nie sciezka pliku: test oznaczony `baza`, ktory
zostal pominiety przy COCKPIT_WYMAGAJ_BAZY=1, oblewa przebieg w dowolnym pliku
i przy dowolnym rootdir. Test bez markera (takze w tests/test_migracje.py albo
tests/e2e/) nie jest ruszany, podobnie test z innym markerem.

Czesc pierwsza uruchamia PRODUKCYJNY tests/conftest.py w osobnym przebiegu
(pytester, podproces); marker rejestrujemy w ini probki, a literowka w nazwie
markera konczy przebieg bledem. Zmienna COCKPIT_WYMAGAJ_BAZY ustawiamy lub
usuwamy jawnie w kazdym tescie, bo CI moze miec ja ustawiona globalnie.
Nazwy plikow w probce sa unikalne, by nie zderzyly sie przy imporcie.

Czesc druga zbiera testy PRAWDZIWEGO repozytorium po markerze `baza` i sprawdza,
ze obejmuje on testy wymagajace PostgreSQL (nazwy czytamy z plikow).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

KORZEN = Path(__file__).resolve().parent.parent
ZMIENNA = "COCKPIT_WYMAGAJ_BAZY"
POWOD = "brak bazy w tym przebiegu"

SKIP_W_SETUP = f'''import pytest

pytestmark = [pytest.mark.baza, pytest.mark.skipif(True, reason="{POWOD}")]


def test_probka():
    pass
'''

SKIP_W_FIXTURE = f'''import pytest


@pytest.fixture
def baza_probki():
    pytest.skip("{POWOD}")


@pytest.mark.baza
def test_probka(baza_probki):
    pass
'''

SKIP_W_CIELE = f'''import pytest


@pytest.mark.baza
def test_probka():
    pytest.skip("{POWOD}")
'''

SKIP_W_SETUP_MARKER_NA_FUNKCJI = f'''import pytest


@pytest.mark.baza
@pytest.mark.skipif(True, reason="{POWOD}")
def test_probka():
    pass
'''

SKIP_W_CIELE_MARKER_NA_KLASIE = f'''import pytest


@pytest.mark.baza
class TestProbka:
    def test_probka(self):
        pytest.skip("{POWOD}")
'''

SKIP_W_SETUP_MARKER_NA_KLASIE = f'''import pytest


@pytest.mark.baza
@pytest.mark.skipif(True, reason="{POWOD}")
class TestProbka:
    def test_probka(self):
        pass
'''

SKIP_W_CIELE_MARKER_NA_MODULE = f'''import pytest

pytestmark = pytest.mark.baza


def test_probka():
    pytest.skip("{POWOD}")
'''

# Bez markera `baza`: pominiecie zostaje pominieciem.
SKIP_W_CIELE_BEZ_MARKERA = f'''import pytest


def test_probka():
    pytest.skip("{POWOD}")
'''

SKIP_W_CIELE_INNY_MARKER = f'''import pytest


@pytest.mark.e2e
def test_probka():
    pytest.skip("{POWOD}")
'''

XFAIL = '''import pytest


@pytest.mark.baza
@pytest.mark.xfail(reason="znany blad", strict=False)
def test_probka():
    assert False
'''

PRZECHODZI = '''import pytest


@pytest.mark.baza
def test_probka():
    assert True
'''

INI_PROBKI = (
    "[pytest]\n"
    "markers =\n"
    "    baza: test wymaga PostgreSQL\n"
    "    e2e: test w przegladarce\n"
)


def _uruchom(
    pytester,
    monkeypatch,
    pliki: dict[str, str],
    zmienna: str | None,
    dodatkowe_argumenty: tuple[str, ...] = (),
):
    if zmienna is None:
        monkeypatch.delenv(ZMIENNA, raising=False)
    else:
        monkeypatch.setenv(ZMIENNA, zmienna)
    pytester.makeini(INI_PROBKI)  # rootpath = katalog pytestera, nie wyzej
    pytester.makeconftest((KORZEN / "tests" / "conftest.py").read_text(encoding="utf-8"))
    for sciezka, tresc in pliki.items():
        plik = pytester.path / sciezka
        plik.parent.mkdir(parents=True, exist_ok=True)
        plik.write_text(tresc, encoding="utf-8")
    return pytester.runpytest_subprocess(
        "-p",
        "no:cacheprovider",
        "-rs",
        # Literowka w nazwie markera nie moze po cichu wylaczyc reguly.
        "-W",
        "error::pytest.PytestUnknownMarkWarning",
        *dodatkowe_argumenty,
    )


def _oblany_z_powodem(wynik, faza: str):
    """Przebieg oblany: FAILED w fazie call, ERROR w fazie setup (semantyka pytest)."""
    if faza == "setup":
        wynik.assert_outcomes(errors=1)
    elif faza == "call":
        wynik.assert_outcomes(failed=1)
    else:
        raise ValueError(faza)
    assert wynik.ret != 0
    tekst = wynik.stdout.str()
    assert "COCKPIT_WYMAGAJ_BAZY=1" in tekst
    # Dokladny fragment komunikatu: sam POWOD przezylby str(krotki) albo
    # pozostawiony prefiks "Skipped: ".
    assert f"Powod pominiecia: {POWOD}" in tekst
    assert "Powod pominiecia: Skipped:" not in tekst
    assert "Powod pominiecia: (" not in tekst


def _pominiety_bez_bledu(wynik):
    wynik.assert_outcomes(skipped=1)
    assert wynik.ret == 0


def test_pominiety_test_bazy_w_pliku_o_dowolnej_nazwie_oblewa_przebieg(pytester, monkeypatch):
    wynik = _uruchom(pytester, monkeypatch, {"tests/test_cokolwiek.py": SKIP_W_SETUP}, "1")

    _oblany_z_powodem(wynik, "setup")


def test_pominiecie_w_ciele_testu_bazy_w_pliku_o_dowolnej_nazwie_oblewa_przebieg(
    pytester, monkeypatch
):
    wynik = _uruchom(pytester, monkeypatch, {"tests/test_cokolwiek.py": SKIP_W_CIELE}, "1")

    _oblany_z_powodem(wynik, "call")


def test_pominiecie_w_fixture_testu_bazy_w_pliku_o_dowolnej_nazwie_oblewa_przebieg(
    pytester, monkeypatch
):
    wynik = _uruchom(pytester, monkeypatch, {"tests/test_cokolwiek.py": SKIP_W_FIXTURE}, "1")

    _oblany_z_powodem(wynik, "setup")


def test_pominiety_test_bazy_w_glebokim_podkatalogu_oblewa_przebieg(pytester, monkeypatch):
    wynik = _uruchom(
        pytester, monkeypatch, {"pakiet/glebiej/test_inny_modul.py": SKIP_W_CIELE}, "1"
    )

    _oblany_z_powodem(wynik, "call")


def test_marker_bazy_na_funkcji_oblewa_pominiety_test(pytester, monkeypatch):
    wynik = _uruchom(
        pytester,
        monkeypatch,
        {"tests/test_cokolwiek.py": SKIP_W_SETUP_MARKER_NA_FUNKCJI},
        "1",
    )

    _oblany_z_powodem(wynik, "setup")


def test_marker_bazy_na_klasie_oblewa_pominiety_test_w_ciele(pytester, monkeypatch):
    wynik = _uruchom(
        pytester, monkeypatch, {"tests/test_cokolwiek.py": SKIP_W_CIELE_MARKER_NA_KLASIE}, "1"
    )

    _oblany_z_powodem(wynik, "call")


def test_marker_bazy_na_klasie_oblewa_test_pominiety_przed_uruchomieniem(pytester, monkeypatch):
    wynik = _uruchom(
        pytester, monkeypatch, {"tests/test_cokolwiek.py": SKIP_W_SETUP_MARKER_NA_KLASIE}, "1"
    )

    _oblany_z_powodem(wynik, "setup")


def test_marker_bazy_na_calym_module_oblewa_pominiety_test(pytester, monkeypatch):
    wynik = _uruchom(
        pytester, monkeypatch, {"tests/test_cokolwiek.py": SKIP_W_CIELE_MARKER_NA_MODULE}, "1"
    )

    _oblany_z_powodem(wynik, "call")


def test_pominiety_test_bazy_bez_wymogu_bazy_zostaje_pominiety(pytester, monkeypatch):
    wynik = _uruchom(pytester, monkeypatch, {"tests/test_cokolwiek.py": SKIP_W_SETUP}, None)

    _pominiety_bez_bledu(wynik)


def test_wartosc_inna_niz_jeden_nie_wlacza_wymogu_bazy(pytester, monkeypatch):
    wynik = _uruchom(pytester, monkeypatch, {"tests/test_cokolwiek.py": SKIP_W_SETUP}, "0")

    _pominiety_bez_bledu(wynik)


def test_pominiety_test_bez_markera_bazy_w_pliku_migracji_zostaje_pominiety(pytester, monkeypatch):
    wynik = _uruchom(
        pytester, monkeypatch, {"tests/test_migracje.py": SKIP_W_CIELE_BEZ_MARKERA}, "1"
    )

    _pominiety_bez_bledu(wynik)


def test_pominiety_test_bez_markera_bazy_w_katalogu_e2e_zostaje_pominiety(pytester, monkeypatch):
    wynik = _uruchom(
        pytester, monkeypatch, {"tests/e2e/test_x.py": SKIP_W_CIELE_BEZ_MARKERA}, "1"
    )

    _pominiety_bez_bledu(wynik)


def test_pominiety_test_z_innym_markerem_niz_baza_zostaje_pominiety(pytester, monkeypatch):
    wynik = _uruchom(
        pytester, monkeypatch, {"tests/test_cokolwiek.py": SKIP_W_CIELE_INNY_MARKER}, "1"
    )

    _pominiety_bez_bledu(wynik)


def test_oczekiwana_porazka_testu_bazy_zostaje_xfailed_mimo_wymogu_bazy(pytester, monkeypatch):
    wynik = _uruchom(pytester, monkeypatch, {"tests/test_cokolwiek.py": XFAIL}, "1")

    wynik.assert_outcomes(xfailed=1)
    assert wynik.ret == 0


def test_przechodzacy_test_bazy_zostaje_zaliczony_przy_wymaganej_bazie(pytester, monkeypatch):
    wynik = _uruchom(pytester, monkeypatch, {"tests/test_cokolwiek.py": PRZECHODZI}, "1")

    wynik.assert_outcomes(passed=1)
    assert wynik.ret == 0


def test_pominiety_test_bazy_oblewa_przebieg_mimo_rootdir_spoza_repozytorium(
    pytester, monkeypatch
):
    # Ini i conftest w katalogu pytestera, ale korzeniem przebiegu jest "inny":
    # plik testu lezy poza nim. Stara regula po sciezkach wypuszczala taki test
    # po cichu; regula po markerze nie zalezy od rootdir.
    inny = pytester.mkdir("inny")

    wynik = _uruchom(
        pytester,
        monkeypatch,
        {"tests/test_cokolwiek.py": SKIP_W_CIELE},
        "1",
        ("--rootdir", str(inny), "--confcutdir", str(pytester.path)),
    )

    _oblany_z_powodem(wynik, "call")


# --- Prawdziwe repozytorium: marker `baza` obejmuje testy wymagajace PostgreSQL ---


def _nazwy_testow(plik: str, po_dekoratorze: str | None = None) -> list[str]:
    tresc = (KORZEN / "tests" / plik).read_text(encoding="utf-8")
    if po_dekoratorze is None:
        wzorzec = r"^(?:async )?def (test_\w+)"
    else:
        wzorzec = rf"^@{po_dekoratorze}\n(?:async )?def (test_\w+)"
    return re.findall(wzorzec, tresc, flags=re.MULTILINE)


def _zbierz_w_repo(wyrazenie: str) -> set[str]:
    env = {k: v for k, v in os.environ.items() if k != ZMIENNA}
    wynik = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            wyrazenie,
            "-p",
            "no:cacheprovider",
            str(KORZEN / "tests"),
        ],
        cwd=KORZEN,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert wynik.returncode == 0, wynik.stdout + wynik.stderr
    # Sufiks parametryzacji (np. "[chromium]" z pytest-playwright) nie jest
    # częścią nazwy testu, więc go zdejmujemy przed porównaniem.
    return {
        linia.strip().split("[", 1)[0] for linia in wynik.stdout.splitlines() if "::" in linia
    }


def _oczekiwane_testy_bazy() -> dict[str, set[str]]:
    migracje = {f"tests/test_migracje.py::{n}" for n in _nazwy_testow("test_migracje.py")}
    numeracja = {
        f"tests/test_numeracja_zdarzen.py::{n}"
        for n in _nazwy_testow("test_numeracja_zdarzen.py", "tylko_postgres")
    }
    demo = {
        f"tests/test_demo_szkielet.py::{n}"
        for n in _nazwy_testow("test_demo_szkielet.py", "wymaga_postgresa")
    }
    e2e = {
        f"tests/e2e/test_szkielet_w_przegladarce.py::{n}"
        for n in _nazwy_testow("e2e/test_szkielet_w_przegladarce.py")
    }
    # Zabezpieczenie przed pustymi zbiorami (zepsuty wzorzec = test, ktory nic nie sprawdza).
    assert len(migracje) == 3
    assert len(numeracja) == 3
    assert len(demo) == 4
    assert len(e2e) == 4
    return {"migracje": migracje, "numeracja": numeracja, "demo": demo, "e2e": e2e}


def test_marker_baza_obejmuje_wszystkie_testy_migracji_numeracji_demo_i_e2e():
    oczekiwane = _oczekiwane_testy_bazy()

    zebrane = _zbierz_w_repo("baza")

    for grupa, ids in oczekiwane.items():
        brakujace = ids - zebrane
        assert not brakujace, f"{grupa}: bez markera baza: {sorted(brakujace)}"


def test_marker_baza_nie_obejmuje_testow_numeracji_na_sqlite():
    oczekiwane = _oczekiwane_testy_bazy()
    numeracja_sqlite = {
        f"tests/test_numeracja_zdarzen.py::{n}" for n in _nazwy_testow("test_numeracja_zdarzen.py")
    } - oczekiwane["numeracja"]
    assert numeracja_sqlite  # plik ma testy SQLite, inaczej test niczego nie chroni

    zebrane = _zbierz_w_repo("baza")

    assert not (zebrane & numeracja_sqlite)


def test_wyrazenie_ci_baza_bez_e2e_zbiera_migracje_numeracje_i_demo_bez_e2e():
    oczekiwane = _oczekiwane_testy_bazy()

    zebrane = _zbierz_w_repo("baza and not e2e")

    assert oczekiwane["migracje"] <= zebrane
    assert oczekiwane["numeracja"] <= zebrane
    assert oczekiwane["demo"] <= zebrane
    assert not [id_ for id_ in zebrane if id_.startswith("tests/e2e/")]
