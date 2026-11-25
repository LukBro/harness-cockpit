"""HAR-209: agent nie dostaje tokenu runnera w swoim srodowisku.

Agent to proces z wlasnymi narzedziami (powloka, git, `env`): token runnera w jego
srodowisku wycieka do logow i plikow repozytorium uzytkownika. Pierwsza grupa testow
wola prawdziwa `_srodowisko_agenta` (ta, z ktorej korzysta straznik przy `Popen` agenta),
druga uruchamia prawdziwy `wykonaj_krok` z prawdziwym straznikiem i czyta, co agent
zapisal na dysk.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

from cockpit.services.invariants import Rola
from runner.agent import SpecKroku
from runner.krok import wykonaj_krok
from runner.straznik import (
    ZMIENNA_BRAK_PYTHONPATH_AGENTA,
    ZMIENNA_PYTHONPATH_AGENTA,
    _srodowisko_agenta,
)

LIMIT_S = 60
TOKEN = "tajny-token-runnera-har209"

# Agent zapisuje swoje srodowisko do pliku z argv[1]; lezy w `-c`, poza katalogiem roboczym.
KOD_AGENTA = r"""
import json, os, sys
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(dict(os.environ), f)
"""


def _srodowisko_runnera() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/uzytkownik",
        "LANG": "pl_PL.UTF-8",
        "COCKPIT_RUNNER_URL": "http://localhost:8000",
        "COCKPIT_RUNNER_TOKEN": TOKEN,
    }


def test_srodowisko_agenta_nie_zawiera_tokenu_runnera():
    wynik = _srodowisko_agenta(_srodowisko_runnera())

    assert "COCKPIT_RUNNER_TOKEN" not in wynik
    assert TOKEN not in wynik.values()


@pytest.mark.parametrize("nazwa", [
    "COCKPIT_RUNNER_TOKEN",
    "COCKPIT_ZESPOL_TOKEN",          # inny srodkowy czlon niz RUNNER
    "COCKPIT_SESJA_API_TOKEN",       # srodek z podkreslnikiem
    "COCKPIT_A_TOKEN",
    "COCKPIT__TOKEN",                # pusty srodek: get_token("") czyta ta zmienna
])
def test_kazda_zmienna_cockpit_z_koncowka_token_jest_wycieta_z_srodowiska_agenta(nazwa):
    srodowisko = {**_srodowisko_runnera(), nazwa: "sekret"}

    wynik = _srodowisko_agenta(srodowisko)

    assert nazwa not in wynik
    assert "sekret" not in wynik.values()


def test_zwykle_zmienne_srodowiska_zostaja_agentowi_bez_zmian():
    wynik = _srodowisko_agenta(_srodowisko_runnera())

    assert wynik == {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/uzytkownik",
        "LANG": "pl_PL.UTF-8",
        "COCKPIT_RUNNER_URL": "http://localhost:8000",
    }


ZMIENNE_ZOSTAJACE = [
    "COCKPIT_TOKEN_FILE",
    "COCKPIT_RUNNER_TOKEN_FILE",     # token to nie koniec nazwy: kotwica konca wzorca
    "MOJ_COCKPIT_A_TOKEN",           # cockpit to nie poczatek nazwy: kotwica poczatku wzorca
    "COCKPITX_TOKEN",                # brak `_` po COCKPIT
    "COCKPIT_XTOKEN",                # brak `_` przed TOKEN
    "NPM_TOKEN",
]


@pytest.mark.parametrize("nazwa", ZMIENNE_ZOSTAJACE)
def test_zmienna_o_nazwie_niepasujacej_do_wzorca_tokenu_kokpitu_zostaje_agentowi(nazwa):
    srodowisko = {**_srodowisko_runnera(), nazwa: "wartosc-uzytkownika"}

    assert _srodowisko_agenta(srodowisko)[nazwa] == "wartosc-uzytkownika"


NAZWY_ZMIENNYCH = [
    "COCKPIT_RUNNER_TOKEN", "COCKPIT__TOKEN", "COCKPIT_SESJA_API_TOKEN", "COCKPIT_A_TOKEN",
    *ZMIENNE_ZOSTAJACE, "COCKPIT_RUNNER_URL", "PATH",
]


def test_straznik_wycina_dokladnie_te_zmienne_ktore_maskowanie_logow_maskuje(monkeypatch):
    """Dwie sciezki produkcyjne (`_srodowisko_agenta` i `FiltrSekretow.maskuj`) czytaja jeden wzorzec."""
    from runner import maskowanie

    wartosci = {nazwa: f"wartosc-{i}-abcdefghij" for i, nazwa in enumerate(NAZWY_ZMIENNYCH)}
    monkeypatch.setattr(maskowanie.os, "environ", dict(wartosci))
    monkeypatch.setattr(maskowanie, "_SEKRETY", set())
    filtr = maskowanie.FiltrSekretow()

    wycieta = {n for n in wartosci if n not in _srodowisko_agenta(wartosci)}
    zamaskowana = {n for n, w in wartosci.items() if filtr.maskuj(w) != w}

    assert wycieta == zamaskowana
    assert wycieta == {
        "COCKPIT_RUNNER_TOKEN", "COCKPIT__TOKEN", "COCKPIT_SESJA_API_TOKEN", "COCKPIT_A_TOKEN"}


def test_wycinanie_tokenu_nie_zmienia_srodowiska_przekazanego_do_funkcji():
    srodowisko = _srodowisko_runnera()

    _srodowisko_agenta(srodowisko)

    assert srodowisko == _srodowisko_runnera()


def test_wycinanie_tokenu_nie_psuje_odtwarzania_pythonpath_agenta():
    srodowisko = {
        **_srodowisko_runnera(),
        "PYTHONPATH": "/kod/kokpitu:/stary",
        ZMIENNA_PYTHONPATH_AGENTA: "/stary",
    }

    wynik = _srodowisko_agenta(srodowisko)

    assert wynik["PYTHONPATH"] == "/stary"
    assert ZMIENNA_PYTHONPATH_AGENTA not in wynik
    assert "COCKPIT_RUNNER_TOKEN" not in wynik


def test_wycinanie_tokenu_nie_psuje_usuwania_pythonpath_gdy_runner_go_nie_mial():
    srodowisko = {
        **_srodowisko_runnera(),
        "PYTHONPATH": "/kod/kokpitu",
        ZMIENNA_BRAK_PYTHONPATH_AGENTA: "1",
    }

    wynik = _srodowisko_agenta(srodowisko)

    assert "PYTHONPATH" not in wynik
    assert "COCKPIT_RUNNER_TOKEN" not in wynik


# --- prawdziwy krok: co agent faktycznie widzi --------------------------------------------------


def _spec() -> SpecKroku:
    return SpecKroku(
        run_id="run-17", step_id="krok-23", agent_id="agent-31", skill_id="pisanie",
        rola=Rola.author, wejscia=[], wyjscia=[], komendy={})


async def _srodowisko_widziane_przez_agenta(tmp_path: Path) -> dict[str, str]:
    katalog = tmp_path / "repo"
    katalog.mkdir()
    zapis = tmp_path / "srodowisko-agenta.json"

    async def emit(zdarzenie: dict) -> None:
        pass

    wynik = await asyncio.wait_for(
        wykonaj_krok(
            _spec(), [sys.executable, "-I", "-c", KOD_AGENTA, str(zapis)], katalog, emit),
        LIMIT_S)
    assert wynik.status == "zakonczony", wynik
    return json.loads(zapis.read_text(encoding="utf-8"))


async def test_agent_uruchomiony_przez_krok_nie_widzi_tokenu_runnera(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", TOKEN)
    monkeypatch.setenv("COCKPIT_ZESPOL_TOKEN", TOKEN + "-zespol")
    monkeypatch.setenv("COCKPIT_RUNNER_URL", "http://localhost:8000")

    widziane = await _srodowisko_widziane_przez_agenta(tmp_path)

    assert not [nazwa for nazwa in widziane if nazwa.startswith("COCKPIT_") and nazwa.endswith("_TOKEN")]
    assert not [w for w in widziane.values() if TOKEN in w]
    assert widziane["COCKPIT_RUNNER_URL"] == "http://localhost:8000"
    assert "PATH" in widziane
