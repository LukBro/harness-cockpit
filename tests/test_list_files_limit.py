"""Lista plikow runnera przerywa przechodzenie drzewa po osiagnieciu limitu (HAR-204).

Dowod przerwania to liczba wywolan `os.scandir` i liczba odczytanych wpisow katalogow,
a nie sam rozmiar odpowiedzi (ten jest ucinany takze bez przerwania).
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from runner import operacje


@pytest.fixture
def projekt(tmp_path, monkeypatch):
    baza = tmp_path.resolve()
    korzen = baza / "projekt"
    dom = baza / "dom"
    korzen.mkdir()
    dom.mkdir()
    konfig = baza / "runner.json"
    konfig.write_text(json.dumps({"projekty": [str(korzen)]}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(dom))
    monkeypatch.setenv("COCKPIT_RUNNER_CONFIG", str(konfig))
    return SimpleNamespace(korzen=korzen)


@pytest.fixture
def przeglad(monkeypatch):
    """Zlicza wywolania `os.scandir` i wpisy odczytane z katalogow."""
    licznik = SimpleNamespace(katalogi=0, wpisy=0)
    prawdziwy = os.scandir

    class _Zliczajacy:
        def __init__(self, sciezka):
            licznik.katalogi += 1
            self._wewn = prawdziwy(sciezka)

        def __enter__(self):
            self._wewn.__enter__()
            return self

        def __exit__(self, *wyjatek):
            return self._wewn.__exit__(*wyjatek)

        def __iter__(self):
            for wpis in self._wewn:
                licznik.wpisy += 1
                yield wpis

    monkeypatch.setattr(operacje.os, "scandir", _Zliczajacy)
    return licznik


async def _lista(projekt, wzorzec: str) -> dict:
    odp = await operacje.wykonaj(
        {"type": "list_files", "req_id": "r-1", "sciezka": str(projekt.korzen), "wzorzec": wzorzec})
    assert odp["ok"] is True, odp
    return odp["wynik"]


async def test_drzewo_wieksze_od_limitu_nie_jest_przechodzone_do_konca(projekt, przeglad):
    for i in range(300):
        katalog = projekt.korzen / f"k{i:03d}"
        katalog.mkdir()
        for j in range(20):
            (katalog / f"p{j:02d}.txt").touch()
    # wszystkich katalogow jest 301, przejscie pelne to 602 odczyty katalogu
    wynik = await _lista(projekt, "**/*.txt")

    assert len(wynik["pliki"]) == operacje.LIMIT_WYNIKOW
    assert wynik["obcieto"] is True
    assert przeglad.katalogi < 560   # 301 ("**") + ok. 201 ("*.txt"); pelne przejscie to 602


async def test_katalog_z_ogromna_liczba_plikow_nie_jest_czytany_do_konca(projekt, przeglad):
    for i in range(6000):
        (projekt.korzen / f"p{i:05d}.txt").touch()

    wynik = await _lista(projekt, "*.txt")

    assert len(wynik["pliki"]) == operacje.LIMIT_WYNIKOW
    assert wynik["obcieto"] is True
    assert przeglad.wpisy <= operacje.LIMIT_KANDYDATOW + 1   # +1: wpis, przy ktorym petla staje


async def test_rekurencyjne_zejscie_w_glab_konczy_sie_na_limicie_katalogow(projekt, przeglad):
    for i in range(operacje.LIMIT_KANDYDATOW + 300):
        (projekt.korzen / f"k{i:05d}").mkdir()

    wynik = await _lista(projekt, "**")

    assert wynik["obcieto"] is True
    assert przeglad.katalogi <= operacje.LIMIT_KANDYDATOW + 1


async def test_przerwanie_przy_samych_sekretach_nadal_zaznacza_obciecie(projekt):
    # kandydaci wypelniaja limit, ale po odfiltrowaniu sekretow zostaje ich < LIMIT_WYNIKOW
    for i in range(operacje.LIMIT_KANDYDATOW + 100):
        (projekt.korzen / f".env.{i}").write_text("x", encoding="utf-8")
    for i in range(3):
        (projekt.korzen / f"zwykly{i}.txt").touch()

    wynik = await _lista(projekt, "*")

    assert len(wynik["pliki"]) < operacje.LIMIT_WYNIKOW
    assert all(".env" not in p["sciezka"] for p in wynik["pliki"])
    assert wynik["obcieto"] is True


async def test_limit_katalogow_jest_wspolny_dla_wszystkich_katalogow_skladnika(
    projekt, przeglad, monkeypatch,
):
    monkeypatch.setattr(operacje, "LIMIT_KANDYDATOW", 10)
    # kazde poddrzewo (8 katalogow) miesci sie w limicie, razem go przekraczaja
    for i in range(3):
        for j in range(7):
            (projekt.korzen / f"k{i}" / f"p{j}").mkdir(parents=True)

    wynik = await _lista(projekt, "k*/**")

    assert len(wynik["pliki"]) == 10
    assert wynik["obcieto"] is True
    assert przeglad.katalogi <= 10 + 1   # +1: odczyt korzenia dla skladnika "k*"


async def test_uciecie_we_wczesniejszym_skladniku_zaznacza_obciecie_przy_nielicznych_trafieniach(
    projekt, monkeypatch,
):
    monkeypatch.setattr(operacje, "LIMIT_KANDYDATOW", 10)
    # "**" ucina sie na 10 katalogach (w tym korzen bez a.txt), wiec "a.txt" ma 9 trafien
    for i in range(20):
        katalog = projekt.korzen / f"k{i:02d}"
        katalog.mkdir()
        (katalog / "a.txt").touch()

    wynik = await _lista(projekt, "**/a.txt")

    assert len(wynik["pliki"]) == 9
    assert wynik["obcieto"] is True


async def test_dwa_razy_tyle_kandydatow_co_limit_wynikow_przejrzanych_w_calosci_nie_jest_obciete(
    projekt,
):
    for i in range(operacje.LIMIT_WYNIKOW):
        (projekt.korzen / f"p{i:05d}.txt").touch()
        (projekt.korzen / f".env.{i}").write_text("x", encoding="utf-8")

    wynik = await _lista(projekt, "*")

    assert len(wynik["pliki"]) == operacje.LIMIT_WYNIKOW
    assert wynik["obcieto"] is False


async def test_drzewo_mniejsze_od_limitu_jest_przejrzane_w_calosci_bez_obciecia(projekt, przeglad):
    for i in range(10):
        katalog = projekt.korzen / f"k{i}"
        katalog.mkdir()
        (katalog / "a.txt").touch()

    wynik = await _lista(projekt, "**/*.txt")

    assert len(wynik["pliki"]) == 10
    assert wynik["obcieto"] is False
    assert przeglad.katalogi == 22   # 11 katalogow w skladniku "**" i 11 w "*.txt"
