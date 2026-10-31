"""Panel Runnery w prawdziwej przegladarce na PostgreSQL (HAR-68).

Runner i kokpit to procesy z fixture'ow conftest.py (grupa procesow, sprzatanie, limity).
"""
from __future__ import annotations

import inspect
import os
import time

import pytest
from playwright.sync_api import expect

from runner import client as runner_client

pytestmark = [pytest.mark.e2e, pytest.mark.baza]

LIMIT_ONLINE_MS = 10_000     # kryterium ticketu: uruchomiony runner widac jako online w 10 s
# Odswiezanie panelu co 5 s (ODSWIEZANIE_RUNNEROW_MS w web/src/App.tsx) + 2 s zapasu na
# opoznienie sondy. Limit celowo ciasny: przy odswiezaniu rzadszym niz ~7 s test ma oblewac.
LIMIT_OFFLINE_MS = 7_000
# Nazwa, pod ktora runner sie melduje (domyslna klienta; proces runnera nie ustawia innej).
NAZWA_RUNNERA = inspect.signature(runner_client.Runner).parameters["name"].default


def _wiersz_runnera(page):
    sekcja = page.get_by_label("Runnery")
    return sekcja.get_by_role("row").filter(has_text=NAZWA_RUNNERA)


def test_uruchomiony_runner_jest_widoczny_jako_online_z_wersja_w_ciagu_10_sekund(
    zalogowana_strona, request
):
    page = zalogowana_strona
    sekcja = page.get_by_label("Runnery")
    # Stan przed startem: zasiew zalozyl wiersz runnera bez kontaktu, wiec panel po pierwszym
    # pobraniu pokazuje go jako offline. To dowod, ze pierwsze pobranie juz sie odbylo
    # i ze nic nie jest online.
    expect(sekcja.get_by_text("offline", exact=True)).to_be_visible(timeout=LIMIT_ONLINE_MS)
    expect(sekcja.get_by_text("online", exact=True)).to_have_count(0)

    # Stoper od pierwszego pobrania (razem ze startem runnera): o wyniku decyduje cykliczne
    # odswiezanie, a nie pobranie przy wejsciu na strone.
    start = time.monotonic()
    request.getfixturevalue("runner_online")   # wraca, gdy strumien runnera jest otwarty
    pozostalo_ms = max(1, int(LIMIT_ONLINE_MS - (time.monotonic() - start) * 1000))

    wiersz = _wiersz_runnera(page)
    expect(wiersz.get_by_text("online", exact=True)).to_be_visible(timeout=pozostalo_ms)
    expect(wiersz).to_contain_text(runner_client.wersja_runnera())
    expect(wiersz.get_by_text("offline", exact=True)).to_have_count(0)


@pytest.fixture
def maszyna_z_backendem_claude(tmp_path, monkeypatch):
    """Maszyna z backendem `claude` w PATH niezaleznie od srodowiska (CI go nie ma).

    Zmiana idzie przez os.environ: fixture `runner_online` kopiuje je do procesu runnera,
    a `wykryj_backendy()` w tescie czyta te same. Musi byc w argumentach testu przed
    `runner_online`, zeby PATH byl ustawiony, zanim runner wystartuje.
    """
    katalog = tmp_path / "falszywy-bin"
    katalog.mkdir()
    claude = katalog / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(katalog), os.environ.get("PATH", "")]))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)   # stale uwierzytelnienie: subskrypcja


def test_backendy_runnera_na_liscie_zgadzaja_sie_z_wykrytymi_na_maszynie(
    maszyna_z_backendem_claude, runner_online, zalogowana_strona
):
    wiersz = _wiersz_runnera(zalogowana_strona)
    expect(wiersz.get_by_text("online", exact=True)).to_be_visible(timeout=LIMIT_ONLINE_MS)

    oczekiwane = ", ".join(
        f"{b['nazwa']} ({b['auth']})" for b in runner_client.wykryj_backendy()
    )
    # Bez backendu nie ma czego porownac: to blad (wykrywanie zepsute), nie pominiecie.
    assert oczekiwane == "claude (subscription)", f"wykryto: {oczekiwane!r}"
    expect(wiersz).to_contain_text(oczekiwane)


def test_zatrzymany_runner_jest_na_liscie_jako_offline(zatrzymaj_runnera, zalogowana_strona):
    page = zalogowana_strona
    wiersz = _wiersz_runnera(page)
    expect(wiersz.get_by_text("online", exact=True)).to_be_visible(timeout=LIMIT_ONLINE_MS)

    zatrzymaj_runnera()

    expect(wiersz.get_by_text("offline", exact=True)).to_be_visible(timeout=LIMIT_OFFLINE_MS)
    expect(wiersz.get_by_text("online", exact=True)).to_have_count(0)
