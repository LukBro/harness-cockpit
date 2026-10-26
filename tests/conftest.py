"""Wspolny conftest testow (HAR-12): pominiety test bazy oblewa przebieg w CI.

Niezmiennik `evidence_required`: brak dowodu to blad, nigdy zielone przejscie.
Gdy CI ustawia COCKPIT_WYMAGAJ_BAZY=1, SKIPPED testow z tests/test_migracje.py
i tests/e2e/ (czyli testow, ktore bez PostgreSQL niczego nie sprawdzaja)
zamienia sie na porazke testu z powodem pominiecia. Pytest pokazuje ja jako
ERROR (faza setup, np. skipif albo skip w fixture) albo FAILED (faza call);
w obu przypadkach przebieg konczy sie kodem niezerowym. Bez zmiennej nic sie
nie zmienia.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

ZMIENNA = "COCKPIT_WYMAGAJ_BAZY"
PLIK_MIGRACJI = ("tests", "test_migracje.py")
KATALOG_E2E = ("tests", "e2e")


def _wymaga_bazy(item: pytest.Item) -> bool:
    try:
        czesci = Path(item.path).relative_to(item.config.rootpath).parts
    except ValueError:
        return False
    return czesci == PLIK_MIGRACJI or (
        len(czesci) > len(KATALOG_E2E) and czesci[: len(KATALOG_E2E)] == KATALOG_E2E
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
