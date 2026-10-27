"""Wspolny conftest testow (HAR-12): pominiety test bazy oblewa przebieg w CI.

Niezmiennik `evidence_required`: brak dowodu to blad, nigdy zielone przejscie.
Gdy CI ustawia COCKPIT_WYMAGAJ_BAZY=1, SKIPPED testow z markerem `baza`
(czyli testow, ktore bez PostgreSQL niczego nie sprawdzaja, niezaleznie od
pliku i od rootdir) zamienia sie na porazke testu z powodem pominiecia.
Pytest pokazuje ja jako
ERROR (faza setup, np. skipif albo skip w fixture) albo FAILED (faza call);
w obu przypadkach przebieg konczy sie kodem niezerowym. Bez zmiennej nic sie
nie zmienia.
"""
from __future__ import annotations

import os

import pytest

pytest_plugins = ["pytester"]

ZMIENNA = "COCKPIT_WYMAGAJ_BAZY"


def _wymaga_bazy(item: pytest.Item) -> bool:
    return item.get_closest_marker("baza") is not None


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
