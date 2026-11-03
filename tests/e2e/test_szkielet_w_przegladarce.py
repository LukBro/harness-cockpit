"""Przelot szkieletu w prawdziwej przegladarce na PostgreSQL (HAR-3)."""
from __future__ import annotations

import json
import subprocess
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.baza]

KOMUNIKAT_OFFLINE = "Runner jest offline. Uruchom runnera na swojej maszynie."
LIMIT_WYNIKU_MS = 3000
LIMIT_PRZYCISKU_MS = 5000
LIMIT_POLLINGU_MS = 7000
OKNO_NA_POST_MS = 1000
PODPOWIEDZ_OFFLINE = "Wymaga runnera online. Uruchom runnera na swojej maszynie."


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
    argumenty = subprocess.run(
        ["ps", "-o", "args=", "-p", str(runner_online.pid)],
        capture_output=True, text=True, check=True,
    ).stdout

    assert "runner" in argumenty, "ps nie pokazal procesu runnera"
    assert token_runnera not in argumenty
