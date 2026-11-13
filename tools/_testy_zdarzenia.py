"""Wtyczka pytesta dla `tools/testy.py` (HAR-240): zapisuje start i koniec kazdego testu.

Plik zdarzen wskazuje zmienna `COCKPIT_TESTY_ZDARZENIA`; bez niej wtyczka nic nie robi.
Linia: `S <node id>` (start) albo `K <node id>` (koniec). Pod xdist hooki wolaja tez workery,
wiec ta sama linia moze pojawic sie dwa razy - dla odbiorcy to ten sam stan.
"""
from __future__ import annotations

import os

ZMIENNA = "COCKPIT_TESTY_ZDARZENIA"


def _zapisz(znak: str, nodeid: str) -> None:
    sciezka = os.environ.get(ZMIENNA)
    if not sciezka:
        return
    with open(sciezka, "a", encoding="utf-8") as plik:
        plik.write(f"{znak} {nodeid}\n")


def pytest_runtest_logstart(nodeid: str, location: object) -> None:
    _zapisz("S", nodeid)


def pytest_runtest_logfinish(nodeid: str, location: object) -> None:
    _zapisz("K", nodeid)
