"""Parser raportu mutacji (cicd/mutation_report.py): mutanty funkcji i metod klas (HAR-10).

Skrypt jest samodzielny (nie pakiet), wiec ladujemy go z pliku. Raporty to male pliki
w tmp_path; wejscie opisuje format `mutmut results --all true`, a wynik czytamy tak,
jak czyta go skill: JSON z `main` albo slownik z `parse_report`.
"""
import importlib.util
import json
from pathlib import Path

import pytest

SKRYPT = Path(__file__).resolve().parents[1] / "cicd" / "mutation_report.py"


def _zaladuj():
    spec = importlib.util.spec_from_file_location("mutation_report", SKRYPT)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


raport_mutacji = _zaladuj()

# Linie z mutmut 3.8.0, `mutmut results --all true`, harness-cockpit 2026-10-23.
# Wciecie 4 spacji jest czescia formatu.
METODA_PRZEZYLA = "    cockpit.services.errors.xǁNaruszonyNiezmiennikǁ__init____mutmut_1: survived"
METODA_ZABITA = "    cockpit.services.errors.xǁNaruszonyNiezmiennikǁ__init____mutmut_3: killed"
FUNKCJA_ZABITA = "    cockpit.services.runs.x_hash_token__mutmut_1: killed"
FUNKCJA_ZABITA_DRUGA = "    cockpit.services.runs.x_runner_po_tokenie__mutmut_2: killed"
# Ostrzezenie narzedzia pasuje skladniowo do "<token>: <reszta>", ale nie jest mutantem.
OSTRZEZENIE = "/usr/local/lib/python3.13/site-packages/mutmut/configuration.py:143: UserWarning: coś"


def zapisz_raport(tmp_path: Path, *linie: str) -> Path:
    plik = tmp_path / "wyniki.txt"
    plik.write_text("\n".join(linie) + "\n", encoding="utf-8")
    return plik


def wynik(tmp_path: Path, *linie: str) -> dict:
    return raport_mutacji.parse_report(zapisz_raport(tmp_path, *linie), "mutmut")


def test_raport_z_funkcjami_i_metodami_klas_liczy_wszystkie_mutanty(tmp_path):
    plik = zapisz_raport(
        tmp_path,
        OSTRZEZENIE,
        METODA_PRZEZYLA,
        METODA_ZABITA,
        FUNKCJA_ZABITA,
        FUNKCJA_ZABITA_DRUGA,
        "    cockpit.services.runs.x_zamknij_run__mutmut_4: no tests",
        "    cockpit.services.runs.x_wyslij_polecenie__mutmut_5: timeout",
        "    cockpit.services.runs.x_pomin__mutmut_6: skipped",
    )

    wynik_raportu = raport_mutacji.parse_report(plik, "mutmut")

    # skipped nie wchodzi do total; timeout zabity, no tests i survived przezyly
    assert wynik_raportu["total"] == 6
    assert wynik_raportu["killed"] == 4
    assert wynik_raportu["survived"] == 2
    assert wynik_raportu["score"] == 66.67
    assert len(wynik_raportu["survivors"]) == 2


def test_mutant_metody_klasy_jest_liczony_w_wyniku(tmp_path):
    w = wynik(tmp_path, METODA_PRZEZYLA, METODA_ZABITA)

    assert (w["total"], w["killed"], w["survived"]) == (2, 1, 1)
    assert w["score"] == 50.0


def test_przezyly_mutant_metody_wskazuje_plik_klasy_i_metode(tmp_path):
    w = wynik(tmp_path, METODA_ZABITA, METODA_PRZEZYLA)

    assert w["survivors"] == [
        {
            "file": "cockpit/services/errors.py",
            "line": None,
            "mutator": "cockpit.services.errors.xǁNaruszonyNiezmiennikǁ__init____mutmut_1",
            "original": None,
            "replacement": None,
            "status": "survived",
            "description": "NaruszonyNiezmiennik.__init__",
        }
    ]


def test_przezyly_mutant_funkcji_modulu_wskazuje_plik_i_nazwe_funkcji(tmp_path):
    w = wynik(tmp_path, "    cockpit.services.runs.x_hash_token__mutmut_2: survived", FUNKCJA_ZABITA)

    assert [(s["file"], s["description"]) for s in w["survivors"]] == [
        ("cockpit/services/runs.py", "hash_token")
    ]


def test_nazwy_klasy_i_metody_z_podkresleniami_i_cyframi_nie_zlewaja_sie_w_jedna(tmp_path):
    w = wynik(
        tmp_path,
        "    pakiet.modul.xǁMoja_Klasa2ǁmetoda_x1__mutmut_12: survived",
        "    pakiet.modul.xǁDrugaǁ__call____mutmut_3: no tests",
    )

    assert [(s["file"], s["description"]) for s in w["survivors"]] == [
        ("pakiet/modul.py", "Moja_Klasa2.metoda_x1"),
        ("pakiet/modul.py", "Druga.__call__"),
    ]


def test_mutant_ze_statusem_timeout_jest_liczony_jako_zabity(tmp_path):
    w = wynik(tmp_path, FUNKCJA_ZABITA_DRUGA.replace("killed", "timeout"), METODA_PRZEZYLA)

    assert w["killed"] == 1
    assert w["survived"] == 1
    assert w["score"] == 50.0


def test_mutant_bez_testow_jest_przezyly_i_zachowuje_status_z_raportu(tmp_path):
    w = wynik(tmp_path, FUNKCJA_ZABITA, "    cockpit.services.runs.x_zamknij_run__mutmut_4: no tests")

    assert w["survived"] == 1
    assert [s["status"] for s in w["survivors"]] == ["no tests"]


def test_mutant_pominiety_nie_wchodzi_do_wyniku(tmp_path):
    w = wynik(tmp_path, FUNKCJA_ZABITA, "    cockpit.services.runs.x_pomin__mutmut_6: skipped")

    assert (w["total"], w["killed"], w["survived"]) == (1, 1, 0)
    assert w["score"] == 100.0
    assert w["survivors"] == []


def test_ostrzezenie_narzedzia_w_raporcie_nie_zmienia_wyniku(tmp_path):
    bez = wynik(tmp_path, METODA_PRZEZYLA, METODA_ZABITA, FUNKCJA_ZABITA)
    z = wynik(tmp_path, OSTRZEZENIE, METODA_PRZEZYLA, METODA_ZABITA, FUNKCJA_ZABITA, OSTRZEZENIE)

    assert z == bez


@pytest.mark.parametrize(
    "nierozpoznany",
    [
        "    cockpit.services.runs.y_dziwny__mutmut_1: killed",
        "    cockpit.services.runs.xǁKlasa__mutmut_1: killed",
        "    cockpit.services.runs.xǁAǁBǁc__mutmut_1: survived",
        "    cockpit.services.runs.x_fooǁbar__mutmut_1: killed",
    ],
)
def test_nierozpoznany_identyfikator_mutmut_jest_bledem_wejscia(tmp_path, capsys, nierozpoznany):
    plik = zapisz_raport(tmp_path, FUNKCJA_ZABITA, nierozpoznany, METODA_PRZEZYLA)

    kod = raport_mutacji.main(["--report", str(plik), "--format", "mutmut"])

    wyjscie = capsys.readouterr()
    assert kod == 2
    assert wyjscie.out == ""
    assert wyjscie.err.startswith("mutation_report: ")
    assert "__mutmut_" in wyjscie.err


def test_raport_przez_wiersz_polecen_zwraca_wynik_z_metodami_klas(tmp_path, capsys):
    plik = zapisz_raport(tmp_path, OSTRZEZENIE, METODA_PRZEZYLA, METODA_ZABITA, FUNKCJA_ZABITA)

    kod = raport_mutacji.main(["--report", str(plik), "--format", "mutmut"])

    wyjscie = capsys.readouterr()
    assert kod == 0
    assert wyjscie.err == ""
    zwrocony = json.loads(wyjscie.out)
    assert (zwrocony["total"], zwrocony["killed"], zwrocony["survived"]) == (3, 2, 1)
    assert zwrocony["survivors"][0]["description"] == "NaruszonyNiezmiennik.__init__"
    assert zwrocony["survivors"][0]["file"] == "cockpit/services/errors.py"
