"""Wspolny conftest testow (HAR-12): pominiety test bazy oblewa przebieg w CI.

Niezmiennik `evidence_required`: brak dowodu to blad, nigdy zielone przejscie.
Gdy CI ustawia COCKPIT_WYMAGAJ_BAZY=1, SKIPPED testow z markerem `baza`
(czyli testow, ktore bez PostgreSQL niczego nie sprawdzaja, niezaleznie od
pliku i od rootdir) zamienia sie na porazke testu z powodem pominiecia.
Pytest pokazuje ja jako
ERROR (faza setup, np. skipif albo skip w fixture) albo FAILED (faza call);
w obu przypadkach przebieg konczy sie kodem niezerowym. Bez zmiennej nic sie
nie zmienia.

HAR-143: test z markerem `baza` w workerze xdist (przebieg `-n`) przy ustawionym
COCKPIT_TEST_DATABASE_URL konczy sie porazka, nie skipem: testy bazy dziela jedna
baze worktree (`alembic downgrade base`) i port 8799 w demo, wiec ida sekwencyjnie.
"""
from __future__ import annotations

import logging
import os
import sys

import pytest

from procesy import sprzatnij_zapamietane

pytest_plugins = ["pytester"]

ZMIENNA = "COCKPIT_WYMAGAJ_BAZY"
ZMIENNA_URL_BAZY = "COCKPIT_TEST_DATABASE_URL"


@pytest.fixture(autouse=True)
def _katalog_stanu_runnera_w_katalogu_testu(tmp_path, monkeypatch):
    """HAR-45: bufor zdarzen runnera leży w katalogu stanu. Żaden test nie pisze do
    ~/.local/state; podprocesy testów dziedziczą tę zmienną środowiskową."""
    monkeypatch.setenv("COCKPIT_STATE_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _profil_backendu_bez_pliku_uzytkownika(tmp_path, monkeypatch):
    """HAR-174: run bez podanego profilu czyta backendy.json; żaden test nie czyta
    prawdziwego ~/.config/cockpit/backendy.json. Plik wskazany zmienną nie istnieje,
    więc obowiązuje profil domyślny; testy profilu ustawiają zmienne same."""
    monkeypatch.setenv("COCKPIT_BACKENDY", str(tmp_path / "brak-backendy.json"))
    monkeypatch.delenv("COCKPIT_CONFIG", raising=False)


@pytest.fixture(autouse=True)
def _maskowanie_sekretow_nie_przecieka_miedzy_testami():
    """HAR-86: `main()` runnera uruchamiane w procesie testów instaluje filtr na
    loggerze korzenia, a `get_token` rejestruje sekret w rejestrze modułu. Bez
    sprzątania krótki token jednego testu maskuje komunikaty logów innego."""
    yield
    # Modul niezaimportowany = brak stanu; conftest kopiowany przez `pytester`
    # trafia tez do srodowisk, w ktorych `runner.maskowanie` nie istnieje.
    maskowanie = sys.modules.get("runner.maskowanie")
    if maskowanie is None:
        return
    maskowanie._SEKRETY.clear()
    obiekty = [logging.lastResort] if logging.lastResort is not None else []
    for nazwa in ("", *maskowanie._LOGGERY_MASKOWANE):
        logger = logging.getLogger(nazwa or None)
        obiekty += [logger, *logger.handlers]
    for obiekt in obiekty:
        for f in [f for f in obiekt.filters if isinstance(f, maskowanie.FiltrSekretow)]:
            obiekt.removeFilter(f)


@pytest.fixture(autouse=True)
def _sprzatnij_procesy_uruchomione_przez_helper():
    """HAR-142: procesy z `procesy.uruchom_proces*` nie przezywaja testu, takze padnietego.
    Sprzata tylko grupy utworzone przez helper (zob. `tests/procesy.py`)."""
    yield
    sprzatnij_zapamietane()


def _wymaga_bazy(item: pytest.Item) -> bool:
    return item.get_closest_marker("baza") is not None


def _w_workerze_xdist(item: pytest.Item) -> bool:
    # workerinput ustawia tylko xdist w workerze; zmienna srodowiskowa PYTEST_XDIST_WORKER
    # przechodzi do zagniezdzonych przebiegow pytestera i dalaby falszywe wykrycie.
    return hasattr(item.config, "workerinput")


def pytest_runtest_setup(item: pytest.Item) -> None:
    if not _wymaga_bazy(item) or not os.environ.get(ZMIENNA_URL_BAZY):
        return
    if _w_workerze_xdist(item):
        pytest.fail(
            "Test bazy w workerze xdist: testy bazy dziela jedna baze "
            "worktree (cockpit_test_<skrot> z tools/testy_pelne.sh albo jawny "
            "COCKPIT_TEST_DATABASE_URL) i port 8799, wiec nie wolno ich uruchamiac rownolegle. "
            'Uruchom je bez -n: pytest -m "baza and not e2e"',
            pytrace=False,
        )


def _powod_pominiecia(longrepr) -> str:
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        powod = str(longrepr[2])
    else:
        powod = str(longrepr)
    return powod.removeprefix("Skipped: ")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if os.environ.get(ZMIENNA) != "1":
        return
    if not rep.skipped or hasattr(rep, "wasxfail"):
        return
    if not _wymaga_bazy(item):
        return
    rep.outcome = "failed"
    rep.longrepr = (
        f"{ZMIENNA}=1: test wymagajacy bazy nie moze byc pominiety. "
        f"Powod pominiecia: {_powod_pominiecia(rep.longrepr)}"
    )
