"""Przelot szkieletu w prawdziwej przegladarce na PostgreSQL (HAR-3)."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import expect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from procesy import uruchom_i_zbierz

pytestmark = [pytest.mark.e2e, pytest.mark.baza]

KOMUNIKAT_OFFLINE = "Runner jest offline. Uruchom runnera na swojej maszynie."
LIMIT_WYNIKU_MS = 3000
LIMIT_PRZYCISKU_MS = 5000
LIMIT_POLLINGU_MS = 7000
OKNO_NA_POST_MS = 1000
PODPOWIEDZ_OFFLINE = "Wymaga runnera online. Uruchom runnera na swojej maszynie."
ODSTEP_MIEDZY_ZDARZENIAMI_S = 1.5


def _id_runu(page) -> str:
    return parse_qs(urlparse(page.url).query)["run"][0]


def test_klikniecie_uruchom_pokazuje_wynik_procesu(runner_online, zalogowana_strona):
    page = zalogowana_strona

    page.get_by_role("button", name="Uruchom").click()

    expect(page.locator("li", has_text="szkielet dziala")).to_be_visible(
        timeout=LIMIT_WYNIKU_MS
    )
    expect(page.get_by_text("status: to_accept")).to_be_visible(timeout=LIMIT_WYNIKU_MS)


def test_przeladowanie_strony_pokazuje_ten_sam_run_i_te_same_zdarzenia(
    runner_online, zalogowana_strona
):
    page = zalogowana_strona
    page.get_by_role("button", name="Uruchom").click()
    expect(page.get_by_text("status: to_accept")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    id_przed = _id_runu(page)
    zdarzenia_przed = page.locator("li").all_inner_texts()
    assert zdarzenia_przed, "run nie pokazal zadnego zdarzenia przed przeladowaniem"

    page.reload()

    expect(page.get_by_text(f"run {id_przed}")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(page.get_by_text("status: to_accept")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(page.locator("li")).to_have_text(zdarzenia_przed, timeout=LIMIT_WYNIKU_MS)
    assert _id_runu(page) == id_przed


def test_odpowiedz_409_po_zatrzymaniu_runnera_daje_komunikat_offline_i_nie_tworzy_runu(
    zatrzymaj_runnera, zalogowana_strona, liczba_runow
):
    page = zalogowana_strona
    runow_przed_kliknieciem = zatrzymaj_runnera()
    # HAR-77: przy runnerze offline przycisk jest wyłączony. Ścieżka 409 dotyczy runnera, który
    # rozłączył się po ostatnim pollingu, więc przeglądarka dostaje "runner online", a backend
    # (naprawdę zatrzymany) odpowiada 409.
    page.route("**/api/runners", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps([{
            "id": "runner-podstawiony", "name": "runner-podstawiony", "version": "0",
            "backends": [], "online": True, "last_seen_at": "2026-10-30T12:05:00+00:00",
        }]),
    ))
    page.reload()
    przycisk = page.get_by_role("button", name="Uruchom", exact=True)
    expect(przycisk).to_be_enabled(timeout=LIMIT_PRZYCISKU_MS)

    przycisk.click()

    expect(page.get_by_text(KOMUNIKAT_OFFLINE)).to_be_visible()
    expect(przycisk).to_be_enabled()
    assert liczba_runow() == runow_przed_kliknieciem, (
        "przy runnerze offline klikniecie nie moze zostawic runu w bazie"
    )


def test_zatrzymany_runner_wylacza_uruchom_z_podpowiedzia_i_nie_tworzy_runu(
    zatrzymaj_runnera, zalogowana_strona, liczba_runow
):
    page = zalogowana_strona
    runow_przed = zatrzymaj_runnera()

    # polling stanu runnera co 5 s + 2 s zapasu
    przycisk = page.get_by_role("button", name="Uruchom", exact=True)
    expect(przycisk).to_be_disabled(timeout=LIMIT_POLLINGU_MS)
    expect(przycisk).to_have_accessible_description(PODPOWIEDZ_OFFLINE)
    # Próba akcji: przeglądarka nie odpala handlera na wyłączonym przycisku, więc run nie powstaje.
    # Przycisk bez `disabled` wyśle POST /api/runs. Czekamy na ewentualny POST przed odczytem bazy.
    # WHY: asercja "nie powstało" bez próby akcji przechodzi zawsze.
    przycisk.dispatch_event("click")
    page.wait_for_timeout(OKNO_NA_POST_MS)
    assert liczba_runow() == runow_przed, "wyłączony przycisk nie może zostawić runu w bazie"


def test_token_runnera_nie_jest_widoczny_w_argumentach_procesu(runner_online, token_runnera):
    argumenty = uruchom_i_zbierz(
        ["ps", "-o", "args=", "-p", str(runner_online.pid)],
        capture_output=True, text=True, check=True,
    ).stdout

    assert "runner" in argumenty, "ps nie pokazal procesu runnera"
    assert token_runnera not in argumenty


# --- HAR-78: status runu przychodzi ze zdarzenia konca runu, nie z konca kroku ---

def _utworz_run_w_toku() -> str:
    """Run `running` zespolu zasianego w bazie, przypisany do jego runnera; zadnego procesu
    runnera nie trzeba, bo zdarzenia wysyla test (jak prawdziwy runner: Bearer + POST)."""
    run_id = str(uuid.uuid4())

    async def _wstaw():
        engine = create_async_engine(os.environ["COCKPIT_TEST_DATABASE_URL"])
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO run (id, tenant_id, runner_id, runner_kind, workflow, "
                         "status, created_at) "
                         "SELECT :id, tenant_id, id, 'local', 'szkielet', 'running', now() "
                         "FROM runner LIMIT 1"),
                    {"id": run_id},
                )
        finally:
            await engine.dispose()

    with ThreadPoolExecutor(max_workers=1) as pula:
        pula.submit(lambda: asyncio.run(_wstaw())).result(timeout=60)
    return run_id


def _zdarzenie_runnera(adres, token, run_id, kind, tekst=""):
    odpowiedz = httpx.post(
        f"{adres}/api/events",
        headers={"Authorization": f"Bearer {token}"},
        json={"run_id": run_id, "event_id": str(uuid.uuid4()), "step_id": "s0",
              "kind": kind, "text": tekst},
        timeout=5,
    )
    assert odpowiedz.status_code == 200, odpowiedz.text


def test_status_do_odbioru_pojawia_sie_dopiero_po_zdarzeniu_konca_runu_przyslanym_pozniej(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    page.goto(f"{adres_kokpitu}/?run={run_id}")
    expect(page.get_by_text("status: running")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    page.wait_for_timeout(OKNO_NA_POST_MS)    # strumien runu jest otwarty, zanim ruszą zdarzenia

    _zdarzenie_runnera(adres_kokpitu, token_runnera, run_id, "step_finished", "kod wyjscia 0")
    expect(page.locator("li", has_text="step_finished")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    time.sleep(ODSTEP_MIEDZY_ZDARZENIAMI_S)
    # sam koniec kroku nie konczy runu
    expect(page.get_by_text("status: running")).to_be_visible()

    _zdarzenie_runnera(adres_kokpitu, token_runnera, run_id, "run_finished")

    expect(page.get_by_text("status: to_accept")).to_be_visible(timeout=LIMIT_WYNIKU_MS)


def test_porazka_kroku_pokazuje_status_nieudany_z_powodem_dopiero_po_zakonczeniu_runu(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    page.goto(f"{adres_kokpitu}/?run={run_id}")
    expect(page.get_by_text("status: running")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    page.wait_for_timeout(OKNO_NA_POST_MS)

    _zdarzenie_runnera(adres_kokpitu, token_runnera, run_id, "step_failed", "kod wyjscia 3")
    expect(page.locator("li", has_text="step_failed")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    time.sleep(ODSTEP_MIEDZY_ZDARZENIAMI_S)
    expect(page.get_by_text("status: running")).to_be_visible()

    _zdarzenie_runnera(adres_kokpitu, token_runnera, run_id, "run_finished")

    expect(page.get_by_text("status: failed")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(page.get_by_text("nieudane kroki: s0")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
