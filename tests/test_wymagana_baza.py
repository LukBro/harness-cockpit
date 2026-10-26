"""Pominiety test bazy oblewa przebieg, gdy CI wymaga bazy (HAR-12).

Testy uruchamiaja PRODUKCYJNY tests/conftest.py w osobnym przebiegu (pytester,
podproces). Zmienna COCKPIT_WYMAGAJ_BAZY ustawiamy lub usuwamy jawnie w kazdym
tescie, bo CI moze miec ja ustawiona globalnie. Nazwy plikow w probce sa
unikalne, by nie zderzyly sie przy imporcie.
"""
from __future__ import annotations

from pathlib import Path

KORZEN = Path(__file__).resolve().parent.parent
ZMIENNA = "COCKPIT_WYMAGAJ_BAZY"
POWOD = "brak bazy w tym przebiegu"

SKIP_W_SETUP = f'''import pytest

pytestmark = pytest.mark.skipif(True, reason="{POWOD}")


def test_probka():
    pass
'''

SKIP_W_FIXTURE = f'''import pytest


@pytest.fixture
def baza():
    pytest.skip("{POWOD}")


def test_probka(baza):
    pass
'''

SKIP_W_CIELE = f'''import pytest


def test_probka():
    pytest.skip("{POWOD}")
'''

XFAIL = '''import pytest


@pytest.mark.xfail(reason="znany blad", strict=False)
def test_probka():
    assert False
'''

PRZECHODZI = '''def test_probka():
    assert True
'''


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
    pytester.makeini("[pytest]\n")  # rootpath = katalog pytestera, nie wyzej
    pytester.makeconftest((KORZEN / "tests" / "conftest.py").read_text(encoding="utf-8"))
    for sciezka, tresc in pliki.items():
        plik = pytester.path / sciezka
        plik.parent.mkdir(parents=True, exist_ok=True)
        plik.write_text(tresc, encoding="utf-8")
    return pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-rs", *dodatkowe_argumenty
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


def test_pominiety_test_migracji_przy_wymaganej_bazie_oblewa_przebieg(pytester, monkeypatch):
    wynik = _uruchom(pytester, monkeypatch, {"tests/test_migracje.py": SKIP_W_SETUP}, "1")

    _oblany_z_powodem(wynik, "setup")


def test_pominiety_test_migracji_bez_wymogu_bazy_zostaje_pominiety(pytester, monkeypatch):
    wynik = _uruchom(pytester, monkeypatch, {"tests/test_migracje.py": SKIP_W_SETUP}, None)

    wynik.assert_outcomes(skipped=1)
    assert wynik.ret == 0


def test_wartosc_inna_niz_jeden_nie_wlacza_wymogu_bazy(pytester, monkeypatch):
    wynik = _uruchom(pytester, monkeypatch, {"tests/test_migracje.py": SKIP_W_SETUP}, "0")

    wynik.assert_outcomes(skipped=1)
    assert wynik.ret == 0


def test_pominiecie_w_fixture_testu_e2e_przy_wymaganej_bazie_oblewa_przebieg(pytester, monkeypatch):
    wynik = _uruchom(pytester, monkeypatch, {"tests/e2e/test_e2e_a.py": SKIP_W_FIXTURE}, "1")

    _oblany_z_powodem(wynik, "setup")


def test_pominiety_test_e2e_w_podkatalogu_przy_wymaganej_bazie_oblewa_przebieg(pytester, monkeypatch):
    wynik = _uruchom(
        pytester, monkeypatch, {"tests/e2e/podkatalog/test_e2e_b.py": SKIP_W_FIXTURE}, "1"
    )

    _oblany_z_powodem(wynik, "setup")


def test_pominiecie_w_ciele_testu_migracji_przy_wymaganej_bazie_oblewa_przebieg(pytester, monkeypatch):
    wynik = _uruchom(pytester, monkeypatch, {"tests/test_migracje.py": SKIP_W_CIELE}, "1")

    _oblany_z_powodem(wynik, "call")


def test_pominiety_test_spoza_sciezek_bazy_zostaje_pominiety_mimo_wymogu(pytester, monkeypatch):
    wynik = _uruchom(pytester, monkeypatch, {"tests/test_inne.py": SKIP_W_CIELE}, "1")

    wynik.assert_outcomes(skipped=1)
    assert wynik.ret == 0


def test_plik_o_nazwie_podobnej_do_migracji_nie_podlega_wymogowi_bazy(pytester, monkeypatch):
    wynik = _uruchom(pytester, monkeypatch, {"tests/test_migracje_stare.py": SKIP_W_CIELE}, "1")

    wynik.assert_outcomes(skipped=1)
    assert wynik.ret == 0


def test_katalog_o_nazwie_podobnej_do_e2e_nie_podlega_wymogowi_bazy(pytester, monkeypatch):
    wynik = _uruchom(pytester, monkeypatch, {"tests/e2e_stare/test_e2e_c.py": SKIP_W_CIELE}, "1")

    wynik.assert_outcomes(skipped=1)
    assert wynik.ret == 0


def test_oczekiwana_porazka_testu_migracji_zostaje_xfailed_mimo_wymogu_bazy(pytester, monkeypatch):
    wynik = _uruchom(pytester, monkeypatch, {"tests/test_migracje.py": XFAIL}, "1")

    wynik.assert_outcomes(xfailed=1)
    assert wynik.ret == 0


def test_przechodzacy_test_migracji_zostaje_zaliczony_przy_wymaganej_bazie(pytester, monkeypatch):
    wynik = _uruchom(pytester, monkeypatch, {"tests/test_migracje.py": PRZECHODZI}, "1")

    wynik.assert_outcomes(passed=1)
    assert wynik.ret == 0


def test_plik_testu_poza_katalogiem_glownym_przebiegu_nie_podlega_wymogowi_bazy(pytester, monkeypatch):
    # Ini i conftest w katalogu pytestera, ale korzeniem przebiegu jest "inny":
    # test_migracje.py lezy poza nim, wiec nie jest objety regula (decyzja: bez bledu).
    inny = pytester.mkdir("inny")

    wynik = _uruchom(
        pytester,
        monkeypatch,
        {"tests/test_migracje.py": SKIP_W_CIELE},
        "1",
        ("--rootdir", str(inny), "--confcutdir", str(pytester.path)),
    )

    wynik.assert_outcomes(skipped=1)
    assert wynik.ret == 0
